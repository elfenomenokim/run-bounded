#[cfg(windows)]
use std::process::Child;
#[cfg(windows)]
mod win_job {
    use std::ffi::c_void;

    pub type Handle = *mut c_void;

    #[repr(C)]
    #[allow(dead_code)]
    struct BasicLimitInformation {
        per_process_user_time_limit: i64,
        per_job_user_time_limit: i64,
        limit_flags: u32,
        minimum_working_set_size: usize,
        maximum_working_set_size: usize,
        active_process_limit: u32,
        affinity: usize,
        priority_class: u32,
        scheduling_class: u32,
    }

    #[repr(C)]
    #[allow(dead_code)]
    struct IoCounters {
        read_operation_count: u64,
        write_operation_count: u64,
        other_operation_count: u64,
        read_transfer_count: u64,
        write_transfer_count: u64,
        other_transfer_count: u64,
    }

    #[repr(C)]
    #[allow(dead_code)]
    struct ExtendedLimitInformation {
        basic: BasicLimitInformation,
        io: IoCounters,
        process_memory_limit: usize,
        job_memory_limit: usize,
        peak_process_memory_used: usize,
        peak_job_memory_used: usize,
    }

    const KILL_ON_JOB_CLOSE: u32 = 0x2000;
    const EXTENDED_LIMIT_INFORMATION_CLASS: i32 = 9;
    pub const ERROR_ACCESS_DENIED: i32 = 5;
    pub const WAIT_OBJECT_0: u32 = 0;

    #[link(name = "kernel32")]
    unsafe extern "system" {
        pub fn CreateJobObjectW(attributes: *const c_void, name: *const u16) -> Handle;
        pub fn SetInformationJobObject(
            job: Handle,
            info_class: i32,
            info: *const c_void,
            length: u32,
        ) -> i32;
        pub fn AssignProcessToJobObject(job: Handle, process: Handle) -> i32;
        pub fn WaitForSingleObject(handle: Handle, milliseconds: u32) -> u32;
        pub fn TerminateJobObject(job: Handle, exit_code: u32) -> i32;
        pub fn CloseHandle(handle: Handle) -> i32;
    }

    /// Closing the last handle to a job with this flag kills every process still in it, so a
    /// runner that dies without cleanup cannot leave the child tree behind.
    pub unsafe fn kill_on_close(job: Handle) -> bool {
        let mut information: ExtendedLimitInformation = std::mem::zeroed();
        information.basic.limit_flags = KILL_ON_JOB_CLOSE;
        SetInformationJobObject(
            job,
            EXTENDED_LIMIT_INFORMATION_CLASS,
            std::ptr::addr_of!(information).cast(),
            std::mem::size_of::<ExtendedLimitInformation>() as u32,
        ) != 0
    }
}
#[cfg(unix)]
use std::sync::atomic::AtomicI32;
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
    CapExceeded,
    StreamDone,
    Exited(io::Result<ExitStatus>),
}

/// After a deadline kill, forwarding may be blocked on a consumer that stopped
/// reading. Let the streams drain for this long, then report the timeout anyway.
const DRAIN_GRACE: Duration = Duration::from_millis(100);

/// The child's process group, so a signal that stops the runner also stops the tree.
#[cfg(unix)]
static CHILD_GROUP: AtomicI32 = AtomicI32::new(0);

/// A deadline that a finite, in-range limit can represent; an unrepresentable one becomes none.
fn deadline(base: Instant, seconds: f64) -> Option<Instant> {
    if seconds <= 0.0 {
        return None;
    }
    base.checked_add(Duration::try_from_secs_f64(seconds).ok()?)
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
    let mut max_stdout: Option<usize> = None;
    let mut max_stderr: Option<usize> = None;
    let mut kill_on_cap = false;
    let mut cwd: Option<String> = None;
    let mut stdin_mode: Option<String> = None;
    let mut input_text: Option<String> = None;
    let mut args = env::args().skip(1);
    while let Some(a) = args.next() {
        match a.as_str() {
            "--timeout" => timeout = parse(&args.next().ok_or("missing --timeout")?)?,
            "--idle" => idle = parse(&args.next().ok_or("missing --idle")?)?,
            "--max-output" => cap = parse_size(&args.next().ok_or("missing --max-output")?)?,
            "--max-stdout" => max_stdout = Some(parse_size(&args.next().ok_or("missing --max-stdout")?)?),
            "--max-stderr" => max_stderr = Some(parse_size(&args.next().ok_or("missing --max-stderr")?)?),
            "--kill-on-cap" => kill_on_cap = true,
            "--cwd" => {
                let dir = args.next().ok_or("missing --cwd")?;
                if dir.is_empty() {
                    return Err("invalid --cwd: path must not be empty".into());
                }
                cwd = Some(dir);
            }
            "--stdin" => {
                let mode = args.next().ok_or("missing --stdin")?;
                if mode != "null" && mode != "inherit" {
                    return Err(format!("invalid --stdin mode: {mode} (expected null or inherit)"));
                }
                stdin_mode = Some(mode);
            }
            "--input" => input_text = Some(args.next().ok_or("missing --input")?),
            "--" => break,
            _ => return Err(format!("unknown option {a}")),
        }
    }
    if stdin_mode.as_deref() == Some("inherit") && input_text.is_some() {
        return Err("--stdin inherit cannot be combined with --input".into());
    }
    let command: Vec<String> = args.collect();
    if command.is_empty() {
        return Err("usage: run-bounded [--timeout SEC] [--idle SEC] [--max-output BYTES] [--max-stdout BYTES] [--max-stderr BYTES] [--kill-on-cap] [--cwd DIR] [--stdin null|inherit] [--input TEXT] -- COMMAND [ARGS...]".into());
    }
    if !std::path::Path::new(&command[0]).is_absolute()
        && (command[0].contains('/') || command[0].contains('\\'))
    {
        return Err("relative command paths with a separator are not accepted; use a bare PATH name or an absolute path".into());
    }
    let stdout_cap = max_stdout.unwrap_or(cap);
    let stderr_cap = max_stderr.unwrap_or(cap);
    let stdin_stdio = if let Some(ref text) = input_text {
        if text.is_empty() {
            Stdio::null()
        } else {
            Stdio::piped()
        }
    } else if stdin_mode.as_deref() == Some("inherit") {
        Stdio::inherit()
    } else {
        Stdio::null()
    };
    let start = Instant::now();
    #[cfg(unix)]
    install_signal_handlers();
    let mut cmd = Command::new(&command[0]);
    cmd.args(&command[1..])
        .env_clear()
        .stdin(stdin_stdio)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if let Some(ref dir) = cwd {
        cmd.current_dir(dir);
    }
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
    #[cfg(windows)]
    let job = new_job().map_err(|error| format!("cannot create Windows Job Object: {error}"))?;
    let mut child = match cmd.spawn() {
        Ok(child) => child,
        Err(error) => {
            #[cfg(windows)]
            close_job(job);
            return Err(error.to_string());
        }
    };
    #[cfg(unix)]
    let pid = child.id();
    #[cfg(unix)]
    if pid <= i32::MAX as u32 {
        CHILD_GROUP.store(pid as i32, Ordering::SeqCst);
    }
    #[cfg(windows)]
    let job = match assign_job(job, &child) {
        Ok(job) => job,
        Err(error) => {
            let _ = child.kill();
            let _ = child.wait();
            return Err(format!("cannot assign Windows Job Object: {error}"));
        }
    };
    if let Some(data) = input_text.filter(|t| !t.is_empty()).map(String::into_bytes) {
        if let Some(mut stdin_pipe) = child.stdin.take() {
            thread::spawn(move || {
                let _ = stdin_pipe.write_all(&data);
                let _ = stdin_pipe.flush();
            });
        }
    }
    let out = child.stdout.take().unwrap();
    let err = child.stderr.take().unwrap();
    let out_count = Arc::new(AtomicUsize::new(0));
    let err_count = Arc::new(AtomicUsize::new(0));
    let (tx, rx) = mpsc::channel();
    let a = reader(out, out_count.clone(), stdout_cap, kill_on_cap, tx.clone(), io::stdout());
    let b = reader(err, err_count.clone(), stderr_cap, kill_on_cap, tx.clone(), io::stderr());
    let waiter = thread::spawn(move || {
        let _ = tx.send(Event::Exited(child.wait()));
    });
    let mut class = "ok";
    let mut killed = false;
    let mut last_output = start;
    let mut stream_count = 0;
    let mut status = None;
    let mut drain_until: Option<Instant> = None;
    while status.is_none() || stream_count < 2 {
        if killed && status.is_some() && drain_until.is_none() {
            drain_until = Some(Instant::now() + DRAIN_GRACE);
        }
        let next_deadline = if killed {
            drain_until
        } else {
            let wall = deadline(start, timeout);
            let idle_deadline = deadline(last_output, idle);
            wall.into_iter().chain(idle_deadline).min()
        };
        let event = match next_deadline {
            Some(deadline) => rx.recv_timeout(deadline.saturating_duration_since(Instant::now())),
            None => rx.recv().map_err(|_| mpsc::RecvTimeoutError::Disconnected),
        };
        match event {
            Ok(Event::Output(at)) => last_output = at,
            Ok(Event::CapExceeded) => {
                if !killed {
                    class = "output_limit";
                    #[cfg(unix)]
                    terminate_current(pid);
                    #[cfg(windows)]
                    terminate_current(job);
                    killed = true;
                }
            }
            Ok(Event::StreamDone) => stream_count += 1,
            Ok(Event::Exited(result)) => status = Some(result),
            Err(mpsc::RecvTimeoutError::Timeout) => {
                if killed {
                    // The child is gone; a reader is still blocked writing to a
                    // pipe nobody reads. Do not lose the timeout result to it.
                    break;
                }
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
    if stream_count == 2 {
        let _ = a.join();
        let _ = b.join();
    }
    let _ = waiter.join();
    #[cfg(windows)]
    close_job(job);
    let exit = status
        .as_ref()
        .and_then(|result| result.as_ref().ok())
        .and_then(|s| s.code());
    let signal = status
        .as_ref()
        .and_then(|result| result.as_ref().ok())
        .and_then(terminating_signal);
    let stdout_truncated = out_count.load(Ordering::SeqCst) > stdout_cap;
    let stderr_truncated = err_count.load(Ordering::SeqCst) > stderr_cap;
    if !killed {
        if kill_on_cap && (stdout_truncated || stderr_truncated) {
            class = "output_limit";
        } else if let Some(Ok(s)) = status.as_ref() {
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
        stdout_truncated,
        stderr_truncated,
    };
    let code = match class {
        "ok" => 0,
        "timeout" | "idle" => 124,
        "output_limit" => 1,
        "signal" => 128 + signal.unwrap_or(1),
        _ => exit.unwrap_or(1),
    };
    Ok((r, code))
}
fn reader<R: Read + Send + 'static, W: Write + Send + 'static>(
    mut input: R,
    count: Arc<AtomicUsize>,
    cap: usize,
    kill_on_cap: bool,
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
            let previous = count.fetch_add(n, Ordering::SeqCst);
            if kill_on_cap && previous <= cap && previous.saturating_add(n) > cap {
                let _ = tx.send(Event::CapExceeded);
            }
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
fn terminating_signal(status: &ExitStatus) -> Option<i32> {
    use std::os::unix::process::ExitStatusExt;
    status.signal()
}
#[cfg(windows)]
fn terminating_signal(_status: &ExitStatus) -> Option<i32> {
    None
}
#[cfg(unix)]
fn install_signal_handlers() {
    unsafe {
        for signal in [libc::SIGINT, libc::SIGTERM, libc::SIGHUP] {
            libc::signal(signal, on_stop_signal as *const () as libc::sighandler_t);
        }
    }
}
/// Async-signal-safe cleanup: stop the child tree, then die by the signal that arrived.
#[cfg(unix)]
extern "C" fn on_stop_signal(signal: libc::c_int) {
    let group = CHILD_GROUP.load(Ordering::SeqCst);
    if group > 0 {
        terminate_group(group);
    }
    unsafe {
        libc::signal(signal, libc::SIG_DFL);
        libc::raise(signal);
        libc::_exit(128 + signal);
    }
}
#[cfg(unix)]
fn terminate_current(pid: u32) {
    if pid <= i32::MAX as u32 {
        terminate_group(pid as i32);
    }
}
/// TERM the group, give it 200 ms, then KILL. Uses only async-signal-safe calls.
#[cfg(unix)]
fn terminate_group(group: i32) {
    unsafe { libc::kill(-group, libc::SIGTERM) };
    let pause = libc::timespec {
        tv_sec: 0,
        tv_nsec: 5_000_000,
    };
    for _ in 0..40 {
        if unsafe { libc::kill(-group, 0) } != 0 {
            return;
        }
        unsafe { libc::nanosleep(&pause, std::ptr::null_mut()) };
    }
    if unsafe { libc::kill(-group, 0) } == 0 {
        unsafe { libc::kill(-group, libc::SIGKILL) };
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
fn new_job() -> io::Result<win_job::Handle> {
    unsafe {
        let job = win_job::CreateJobObjectW(std::ptr::null(), std::ptr::null());
        if job.is_null() {
            return Err(io::Error::last_os_error());
        }
        if !win_job::kill_on_close(job) {
            let err = io::Error::last_os_error();
            win_job::CloseHandle(job);
            return Err(err);
        }
        Ok(job)
    }
}
#[cfg(windows)]
fn assign_job(job: win_job::Handle, child: &Child) -> io::Result<win_job::Handle> {
    use std::os::windows::io::AsRawHandle;
    unsafe {
        let handle = child.as_raw_handle() as _;
        if win_job::AssignProcessToJobObject(job, handle) == 0 {
            let err = io::Error::last_os_error();
            win_job::CloseHandle(job);
            if err.raw_os_error() == Some(win_job::ERROR_ACCESS_DENIED)
                && win_job::WaitForSingleObject(handle, 0) == win_job::WAIT_OBJECT_0
            {
                return Ok(std::ptr::null_mut());
            }
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
    let limit = Duration::try_from_secs_f64(n).map_err(|_| {
        format!("seconds must be finite, nonnegative and within the duration range: {s}")
    })?;
    if Instant::now().checked_add(limit).is_none() {
        return Err(format!("seconds are too large for this host's clock: {s}"));
    }
    Ok(n)
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
