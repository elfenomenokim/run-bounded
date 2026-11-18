#[cfg(windows)]
use std::process::Child;
#[cfg(windows)]
mod win_job {
    use std::ffi::c_void;

    pub type Handle = *mut c_void;

    #[link(name = "kernel32")]
    unsafe extern "system" {
        pub fn CreateJobObjectW(attributes: *const c_void, name: *const u16) -> Handle;
        pub fn AssignProcessToJobObject(job: Handle, process: Handle) -> i32;
        pub fn TerminateJobObject(job: Handle, exit_code: u32) -> i32;
        pub fn CloseHandle(handle: Handle) -> i32;
    }
}
use std::{
    env,
    io::{self, Read, Write},
    process::{Command, ExitStatus, Stdio},
    sync::{
        atomic::{AtomicUsize, Ordering},
        mpsc, Arc,
    },
    thread,
    time::{Duration, Instant},
};

enum Event {
    Output(Instant),
    StreamDone,
    Exited(io::Result<ExitStatus>),
}

#[derive(Debug)]
struct ResultRow {
    class: &'static str,
    duration_ms: u128,
    exit_code: Option<i32>,
    stdout_truncated: bool,
    stderr_truncated: bool,
}
fn main() {
    match run() {
        Ok((r, code)) => {
            eprintln!("{{\"class\":\"{}\",\"duration_ms\":{},\"exit_code\":{},\"stdout_truncated\":{},\"stderr_truncated\":{}}}", r.class, r.duration_ms, r.exit_code.map(|v| v.to_string()).unwrap_or("null".into()), r.stdout_truncated, r.stderr_truncated);
            std::process::exit(code);
        }
        Err(e) => {
            eprintln!("run-bounded: {e}");
            std::process::exit(125);
        }
    }
}
fn run() -> Result<(ResultRow, i32), String> {
    let mut timeout = 60f64;
    let mut idle = 20f64;
    let mut cap = 1_048_576usize;
    let mut args = env::args().skip(1);
    while let Some(a) = args.next() {
        match a.as_str() {
            "--timeout" => timeout = parse(&args.next().ok_or("missing --timeout")?)?,
            "--idle" => idle = parse(&args.next().ok_or("missing --idle")?)?,
            "--max-output" => cap = parse_size(&args.next().ok_or("missing --max-output")?)?,
            "--" => break,
            _ => return Err(format!("unknown option {a}")),
        }
    }
    let command: Vec<String> = args.collect();
    if command.is_empty() {
        return Err("usage: run-bounded [--timeout SEC] [--idle SEC] [--max-output BYTES] -- COMMAND [ARGS...]".into());
    }
    if !std::path::Path::new(&command[0]).is_absolute()
        && (command[0].contains('/') || command[0].contains('\\'))
    {
        return Err("relative command paths with a separator are not accepted; use a bare PATH name or an absolute path".into());
    }
    let start = Instant::now();
    let mut cmd = Command::new(&command[0]);
    cmd.args(&command[1..])
        .env_clear()
        .stdin(Stdio::inherit())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    for key in [
        "PATH",
        "HOME",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
    ] {
        if let Some(value) = env::var_os(key) {
            cmd.env(key, value);
        }
    }
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        unsafe {
            cmd.pre_exec(|| {
                if libc::setsid() < 0 {
                    return Err(io::Error::last_os_error());
                }
                Ok(())
            });
        }
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x00000200); // CREATE_NEW_PROCESS_GROUP
    }
    let mut child = cmd.spawn().map_err(|e| e.to_string())?;
    #[cfg(unix)]
    let pid = child.id();
    #[cfg(windows)]
    let job = match create_job(&child) {
        Ok(job) => job,
        Err(error) => {
            let _ = child.kill();
            let _ = child.wait();
            return Err(format!(
                "cannot create or assign Windows Job Object: {error}"
            ));
        }
    };
    let out = child.stdout.take().unwrap();
    let err = child.stderr.take().unwrap();
    let out_count = Arc::new(AtomicUsize::new(0));
    let err_count = Arc::new(AtomicUsize::new(0));
    let (tx, rx) = mpsc::channel();
    let a = reader(out, out_count.clone(), cap, tx.clone(), io::stdout());
    let b = reader(err, err_count.clone(), cap, tx.clone(), io::stderr());
    let waiter = thread::spawn(move || {
        let _ = tx.send(Event::Exited(child.wait()));
    });
    let mut class = "ok";
    let mut killed = false;
    let mut last_output = start;
    let mut stream_count = 0;
    let mut status = None;
    while status.is_none() || stream_count < 2 {
        let next_deadline = if killed {
            None
        } else {
            let wall = (timeout > 0.0).then(|| start + Duration::from_secs_f64(timeout));
            let idle_deadline = (idle > 0.0).then(|| last_output + Duration::from_secs_f64(idle));
            wall.into_iter().chain(idle_deadline).min()
        };
        let event = match next_deadline {
            Some(deadline) => rx.recv_timeout(deadline.saturating_duration_since(Instant::now())),
            None => rx.recv().map_err(|_| mpsc::RecvTimeoutError::Disconnected),
        };
        match event {
            Ok(Event::Output(at)) => last_output = at,
            Ok(Event::StreamDone) => stream_count += 1,
            Ok(Event::Exited(result)) => status = Some(result),
            Err(mpsc::RecvTimeoutError::Timeout) => {
                let now = Instant::now();
                if timeout > 0.0 && now.duration_since(start).as_secs_f64() >= timeout {
                    class = "timeout";
                } else if idle > 0.0 && now.duration_since(last_output).as_secs_f64() >= idle {
                    class = "idle";
                } else {
                    continue;
                }
                #[cfg(unix)]
                terminate_current(pid);
                #[cfg(windows)]
                terminate_current(job);
                killed = true;
            }
            Err(mpsc::RecvTimeoutError::Disconnected) => {
                return Err("process monitor disconnected".into())
            }
        }
    }
    let _ = a.join();
    let _ = b.join();
    let _ = waiter.join();
    #[cfg(windows)]
    close_job(job);
    let exit = status
        .as_ref()
        .and_then(|result| result.as_ref().ok())
        .and_then(|s| s.code());
    if !killed {
        if let Some(Ok(s)) = status.as_ref() {
            if s.success() {
                class = "ok"
            } else if s.code().is_some() {
                class = "failed"
            } else {
                class = "signal"
            }
        }
    }
    let r = ResultRow {
        class,
        duration_ms: start.elapsed().as_millis(),
        exit_code: exit,
        stdout_truncated: out_count.load(Ordering::Relaxed) > cap,
        stderr_truncated: err_count.load(Ordering::Relaxed) > cap,
    };
    let code = match class {
        "ok" => 0,
        "timeout" => 124,
        "idle" => 124,
        "signal" => 128 + 1,
        _ => exit.unwrap_or(1),
    };
    Ok((r, code))
}
fn reader<R: Read + Send + 'static, W: Write + Send + 'static>(
    mut input: R,
    count: Arc<AtomicUsize>,
    cap: usize,
    tx: mpsc::Sender<Event>,
    mut output: W,
) -> thread::JoinHandle<()> {
    thread::spawn(move || {
        let mut buf = [0u8; 8192];
        while let Ok(n) = input.read(&mut buf) {
            if n == 0 {
                break;
            }
            let _ = tx.send(Event::Output(Instant::now()));
            let previous = count.fetch_add(n, Ordering::Relaxed);
            if previous < cap {
                let keep = (cap - previous).min(n);
                let _ = output.write_all(&buf[..keep]);
                let _ = output.flush();
            }
        }
        let _ = tx.send(Event::StreamDone);
    })
}
#[cfg(unix)]
fn terminate_current(pid: u32) {
    unsafe { libc::kill(-(pid as i32), libc::SIGTERM) };
    let grace = Instant::now() + Duration::from_millis(200);
    while Instant::now() < grace {
        if unsafe { libc::kill(-(pid as i32), 0) } != 0 {
            return;
        }
        thread::sleep(Duration::from_millis(5));
    }
    if unsafe { libc::kill(-(pid as i32), 0) } == 0 {
        unsafe { libc::kill(-(pid as i32), libc::SIGKILL) };
    }
}
#[cfg(windows)]
fn terminate_current(job: win_job::Handle) {
    unsafe {
        if !job.is_null() {
            let _ = win_job::TerminateJobObject(job, 1);
        }
    }
}
#[cfg(windows)]
fn create_job(child: &Child) -> io::Result<win_job::Handle> {
    use std::os::windows::io::AsRawHandle;
    unsafe {
        let job = win_job::CreateJobObjectW(std::ptr::null(), std::ptr::null());
        if job.is_null() {
            return Err(io::Error::last_os_error());
        }
        if win_job::AssignProcessToJobObject(job, child.as_raw_handle() as _) == 0 {
            let err = io::Error::last_os_error();
            win_job::CloseHandle(job);
            return Err(err);
        }
        Ok(job)
    }
}
#[cfg(windows)]
fn close_job(job: win_job::Handle) {
    if !job.is_null() {
        unsafe {
            win_job::CloseHandle(job);
        }
    }
}
fn parse(s: &str) -> Result<f64, String> {
    let n = s
        .parse::<f64>()
        .map_err(|_| format!("invalid seconds: {s}"))?;
    if !n.is_finite() || n < 0.0 {
        Err("seconds must be finite and nonnegative".into())
    } else {
        Ok(n)
    }
}
fn parse_size(s: &str) -> Result<usize, String> {
    let (num, mult) = if let Some(v) = s.strip_suffix('K').or_else(|| s.strip_suffix('k')) {
        (v, 1024usize)
    } else if let Some(v) = s.strip_suffix('M').or_else(|| s.strip_suffix('m')) {
        (v, 1024 * 1024)
    } else if let Some(v) = s.strip_suffix('G').or_else(|| s.strip_suffix('g')) {
        (v, 1024 * 1024 * 1024)
    } else {
        (s, 1)
    };
    num.parse::<usize>()
        .map_err(|_| format!("invalid byte limit: {s}"))?
        .checked_mul(mult)
        .ok_or("byte limit overflow".into())
}
