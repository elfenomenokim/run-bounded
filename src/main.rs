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
        pub fn SetConsoleCtrlHandler(handler: *const c_void, add: i32) -> i32;
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
use std::sync::atomic::AtomicI32;
use std::{
    collections::VecDeque,
    env,
    io::{self, Read, Write},
    process::{Command, ExitStatus, Stdio},
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Arc, Mutex,
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
const DEFAULT_TERM_GRACE: Duration = Duration::from_millis(200);

/// The child's process group, so a signal that stops the runner also stops the tree.
#[cfg(unix)]
static CHILD_GROUP: AtomicI32 = AtomicI32::new(0);

/// Set by the stop-signal (or console-control) handler; the main loop turns it
/// into a `cancelled` verdict and exit code 130.
static STOP_REQUESTED: AtomicBool = AtomicBool::new(false);
static STOP_SIGNAL: AtomicI32 = AtomicI32::new(0);

/// The assigned Windows Job Object, so the console-control handler can kill the tree.
#[cfg(windows)]
static CHILD_JOB: std::sync::atomic::AtomicIsize = std::sync::atomic::AtomicIsize::new(0);

#[derive(Clone, Copy, PartialEq, Eq)]
enum Keep {
    Head,
    Tail,
}

enum Destination {
    Stdout,
    Stderr,
    File(std::fs::File),
}

impl Destination {
    fn write_chunk(&mut self, bytes: &[u8]) {
        match self {
            Destination::Stdout => {
                let _ = io::stdout().write_all(bytes);
                let _ = io::stdout().flush();
            }
            Destination::Stderr => {
                let _ = io::stderr().write_all(bytes);
                let _ = io::stderr().flush();
            }
            Destination::File(file) => {
                let _ = file.write_all(bytes);
            }
        }
    }
}

/// Newest-`cap`-bytes ring for `--*-keep tail`: bounded by construction, so a tail
/// stream never trips `--kill-on-cap` for its own stream.
struct TailRing {
    cap: usize,
    parts: VecDeque<Vec<u8>>,
    total: usize,
}

impl TailRing {
    fn new(cap: usize) -> Self {
        TailRing { cap, parts: VecDeque::new(), total: 0 }
    }

    fn push(&mut self, bytes: &[u8]) {
        if bytes.is_empty() {
            return;
        }
        self.parts.push_back(bytes.to_vec());
        self.total += bytes.len();
        while self.total > self.cap {
            let excess = self.total - self.cap;
            let head_len = self.parts.front().map_or(0, Vec::len);
            if head_len <= excess {
                self.total -= head_len;
                self.parts.pop_front();
            } else {
                let mut head = self.parts.pop_front().expect("a front part");
                head.drain(..excess);
                self.total -= excess;
                self.parts.push_front(head);
            }
        }
    }

    fn into_bytes(self) -> Vec<u8> {
        self.parts.into_iter().flatten().collect()
    }
}

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
    abandoned: Option<(u32, &'static str)>,
}
const USAGE: &str = "usage: run-bounded [OPTIONS] -- COMMAND [ARGS...]";
const HELP: &str = "run-bounded: run a command with time, output and process-tree limits.

usage: run-bounded [OPTIONS] -- COMMAND [ARGS...]
COMMAND must be an absolute path or a bare name on PATH.

options:
  --timeout SEC         wall-clock limit (default 60; 0 disables)
  --idle SEC            limit on time without output (default 20; 0 disables)
  --idle-stream STREAM  either (default) or stdout for the idle clock
  --max-output BYTES    default byte cap for each stream (default 1048576; K, M, G = 1024^n)
  --max-stdout BYTES    cap for stdout, overriding --max-output
  --max-stderr BYTES    cap for stderr, overriding --max-output
  --max-total-output BYTES  shared byte cap across stdout and stderr
  --kill-on-cap         stop the process tree when a cap is reached
  --capture-overflow-byte  forward the first byte beyond each cap for diagnostics
  --term-grace SEC      Unix TERM grace before KILL (default 0.2; 0 sends KILL directly)
  --no-reap            force cleanup at a limit and return without waiting for exit
  --stdout-file PATH    write child stdout to PATH instead of forwarding it
  --stderr-file PATH    write child stderr to PATH instead of forwarding it
  --append              open --stdout-file/--stderr-file for appending
  --merge-stderr        send child stderr to the stdout sink in arrival order
  --stdout-keep MODE    head (default) or tail: keep the newest --max-stdout bytes
  --stderr-keep MODE    head (default) or tail: keep the newest --max-stderr bytes
  --cwd DIR             working directory for the command
  --stdin null|inherit  standard input (default: closed)
  --input TEXT          send TEXT on standard input, then close it
  --env-exact           pass the runner's whole environment to the command
  -h, --help            print this help
  -V, --version         print the version

The command keeps its stdout and stderr unless a file sink is given; one JSON
result line goes to stderr at the end. A tail keep buffers the newest bytes and
writes them when the stream closes.
Exit status: the command's own; 124 on a time or idle limit; 1 when --kill-on-cap stopped it;
130 when a signal (SIGINT/SIGTERM/SIGHUP, or Windows CTRL_BREAK/console close) cancelled the
run; 128+N when a signal N ended the command; 125 on a usage error.";

fn main() {
    match run() {
        Ok((r, code)) => {
            let abandoned = r.abandoned.map_or_else(String::new, |(pid, limit)| {
                format!(",\"abandoned_pid\":{pid},\"limit\":\"{limit}\"")
            });
            eprintln!("{{\"class\":\"{}\",\"duration_ms\":{},\"exit_code\":{},\"stdout_truncated\":{},\"stderr_truncated\":{}{abandoned}}}", r.class, r.duration_ms, r.exit_code.map(|v| v.to_string()).unwrap_or("null".into()), r.stdout_truncated, r.stderr_truncated);
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
    let mut stdout_only_idle = false;
    let mut cap = 1_048_576usize;
    let mut max_stdout: Option<usize> = None;
    let mut max_stderr: Option<usize> = None;
    let mut max_total_output: Option<usize> = None;
    let mut kill_on_cap = false;
    let mut capture_overflow_byte = false;
    let mut term_grace = DEFAULT_TERM_GRACE;
    let mut no_reap = false;
    let mut env_exact = false;
    let mut stdout_file: Option<String> = None;
    let mut stderr_file: Option<String> = None;
    let mut sink_append = false;
    let mut merge_stderr = false;
    let mut stdout_keep = Keep::Head;
    let mut stderr_keep = Keep::Head;
    let mut cwd: Option<String> = None;
    let mut stdin_mode: Option<String> = None;
    let mut input_text: Option<String> = None;
    let mut args = env::args().skip(1);
    while let Some(a) = args.next() {
        match a.as_str() {
            "--timeout" => timeout = parse(&args.next().ok_or("missing --timeout")?)?,
            "--idle" => idle = parse(&args.next().ok_or("missing --idle")?)?,
            "--idle-stream" => {
                stdout_only_idle = match args.next().as_deref() {
                    Some("either") => false,
                    Some("stdout") => true,
                    _ => return Err("--idle-stream expects either or stdout".into()),
                };
            }
            "--max-output" => cap = parse_size(&args.next().ok_or("missing --max-output")?)?,
            "--max-stdout" => max_stdout = Some(parse_size(&args.next().ok_or("missing --max-stdout")?)?),
            "--max-stderr" => max_stderr = Some(parse_size(&args.next().ok_or("missing --max-stderr")?)?),
            "--max-total-output" => {
                max_total_output = Some(parse_size(&args.next().ok_or("missing --max-total-output")?)?);
            }
            "--kill-on-cap" => kill_on_cap = true,
            "--capture-overflow-byte" => capture_overflow_byte = true,
            "--term-grace" => {
                let seconds = parse(&args.next().ok_or("missing --term-grace")?)?;
                term_grace = Duration::from_secs_f64(seconds);
            }
            "--no-reap" => no_reap = true,
            "--stdout-file" => stdout_file = Some(args.next().ok_or("missing --stdout-file")?),
            "--stderr-file" => stderr_file = Some(args.next().ok_or("missing --stderr-file")?),
            "--append" => sink_append = true,
            "--merge-stderr" => merge_stderr = true,
            "--stdout-keep" => stdout_keep = parse_keep(&args.next().ok_or("missing --stdout-keep")?)?,
            "--stderr-keep" => stderr_keep = parse_keep(&args.next().ok_or("missing --stderr-keep")?)?,
            "--env-exact" => env_exact = true,
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
            "-h" | "--help" => {
                println!("{HELP}");
                std::process::exit(0);
            }
            "-V" | "--version" => {
                println!("run-bounded {}", env!("CARGO_PKG_VERSION"));
                std::process::exit(0);
            }
            "--" => break,
            _ => return Err(format!("unknown option {a} (see run-bounded --help)")),
        }
    }
    if stdin_mode.as_deref() == Some("inherit") && input_text.is_some() {
        return Err("--stdin inherit cannot be combined with --input".into());
    }
    if no_reap {
        term_grace = Duration::ZERO;
    }
    if merge_stderr && stderr_file.is_some() {
        return Err("--merge-stderr cannot be combined with --stderr-file".into());
    }
    if merge_stderr && stderr_keep != Keep::Head {
        return Err("--stderr-keep has no effect with --merge-stderr".into());
    }
    if merge_stderr && max_stderr.is_some() {
        return Err("--max-stderr cannot be combined with --merge-stderr".into());
    }
    if stdout_file.is_some() && stdout_keep == Keep::Tail {
        return Err("--stdout-keep tail cannot be combined with --stdout-file".into());
    }
    if stderr_file.is_some() && stderr_keep == Keep::Tail {
        return Err("--stderr-keep tail cannot be combined with --stderr-file".into());
    }
    if sink_append && stdout_file.is_none() && stderr_file.is_none() {
        return Err("--append requires --stdout-file or --stderr-file".into());
    }
    let open_sink = |path: &str| -> Result<std::fs::File, String> {
        std::fs::OpenOptions::new()
            .write(true)
            .create(true)
            .append(sink_append)
            .truncate(!sink_append)
            .open(path)
            .map_err(|error| format!("cannot open {path}: {error}"))
    };
    let stdout_sink_file = stdout_file.as_deref().map(open_sink).transpose()?;
    let stderr_sink_file = stderr_file.as_deref().map(open_sink).transpose()?;
    #[cfg(not(unix))]
    let _ = term_grace;
    let command: Vec<String> = args.collect();
    if command.is_empty() {
        return Err(USAGE.into());
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
    #[cfg(windows)]
    install_console_ctrl_handler();
    let mut cmd = Command::new(&command[0]);
    cmd.args(&command[1..])
        .env_clear()
        .stdin(stdin_stdio)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if let Some(ref dir) = cwd {
        cmd.current_dir(dir);
    }
    if env_exact {
        cmd.envs(env::vars_os());
    } else {
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
    #[cfg(windows)]
    store_job(job);
    let mut child = match cmd.spawn() {
        Ok(child) => child,
        Err(error) => {
            #[cfg(windows)]
            close_job(job);
            return Err(error.to_string());
        }
    };
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
    #[cfg(windows)]
    store_job(job);
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
    let out_truncated = Arc::new(AtomicBool::new(false));
    let err_truncated = Arc::new(AtomicBool::new(false));
    // With --merge-stderr both streams deliver into the stdout sink in read order,
    // sharing one budget: --max-total-output when given, otherwise --max-stdout.
    let shared_room: Option<Arc<Mutex<usize>>> = if merge_stderr {
        Some(Arc::new(Mutex::new(max_total_output.unwrap_or(stdout_cap))))
    } else {
        max_total_output.map(|cap| Arc::new(Mutex::new(cap)))
    };
    let (tx, rx) = mpsc::channel();
    let out_dest: Arc<Mutex<Destination>> = Arc::new(Mutex::new(match stdout_sink_file {
        Some(file) => Destination::File(file),
        None => Destination::Stdout,
    }));
    let err_dest: Arc<Mutex<Destination>> = if merge_stderr {
        out_dest.clone()
    } else {
        Arc::new(Mutex::new(match stderr_sink_file {
            Some(file) => Destination::File(file),
            None => Destination::Stderr,
        }))
    };
    let a = reader(
        out, out_truncated.clone(),
        if merge_stderr { usize::MAX } else { stdout_cap },
        stdout_keep, out_dest, shared_room.clone(), kill_on_cap, capture_overflow_byte,
        idle > 0.0, tx.clone(),
    );
    let b = reader(
        err, err_truncated.clone(),
        if merge_stderr { usize::MAX } else { stderr_cap },
        stderr_keep, err_dest, shared_room, kill_on_cap, capture_overflow_byte,
        idle > 0.0 && !stdout_only_idle, tx.clone(),
    );
    let waiter = thread::spawn(move || {
        let _ = tx.send(Event::Exited(child.wait()));
    });
    let mut class = "ok";
    let mut killed = false;
    let mut cancelled = false;
    let mut last_output = start;
    let mut stream_count = 0;
    let mut status = None;
    let mut drain_until: Option<Instant> = None;
    let wall_deadline = deadline(start, timeout);
    while status.is_none() || stream_count < 2 {
        if !cancelled && !killed && STOP_REQUESTED.load(Ordering::SeqCst) {
            // The handler already terminated the tree; make sure (the group may
            // not have existed yet) and report a distinct cancelled verdict.
            cancelled = true;
            #[cfg(unix)]
            terminate_current(pid, term_grace);
            #[cfg(windows)]
            terminate_current(job);
            killed = true;
            class = "cancelled";
        }
        if killed && status.is_some() && drain_until.is_none() {
            drain_until = Some(Instant::now() + DRAIN_GRACE);
        }
        let next_deadline = if killed {
            drain_until
        } else {
            let idle_deadline = deadline(last_output, idle);
            wall_deadline.into_iter().chain(idle_deadline).min()
        };
        let event = if no_reap && wall_deadline.is_some_and(|wall| Instant::now() >= wall) {
            // Continuous output can keep the channel ready beyond the wall limit.
            Err(mpsc::RecvTimeoutError::Timeout)
        } else {
            match next_deadline {
                Some(deadline) => rx.recv_timeout(deadline.saturating_duration_since(Instant::now())),
                None => rx.recv().map_err(|_| mpsc::RecvTimeoutError::Disconnected),
            }
        };
        match event {
            Ok(Event::Output(at)) => last_output = last_output.max(at),
            Ok(Event::CapExceeded) => {
                if !killed {
                    class = "output_limit";
                    #[cfg(unix)]
                    terminate_current(pid, term_grace);
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
                terminate_current(pid, term_grace);
                #[cfg(windows)]
                terminate_current(job);
                killed = true;
            }
            Err(mpsc::RecvTimeoutError::Disconnected) => {
                return Err("process monitor disconnected".into())
            }
        }
        if no_reap && killed {
            break;
        }
    }
    if stream_count == 2 {
        let _ = a.join();
        let _ = b.join();
    }
    if !(no_reap && killed) {
        let _ = waiter.join();
    }
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
    let stdout_truncated = out_truncated.load(Ordering::SeqCst);
    let stderr_truncated = err_truncated.load(Ordering::SeqCst);
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
    let abandoned = (no_reap && killed && !cancelled && !matches!(status, Some(Ok(_))))
        .then_some((pid, class));
    let r = ResultRow {
        class: if abandoned.is_some() { "unknown" } else { class },
        duration_ms: start.elapsed().as_millis(),
        exit_code: exit,
        stdout_truncated,
        stderr_truncated,
        abandoned,
    };
    let code = match class {
        "ok" => 0,
        "timeout" | "idle" => 124,
        "output_limit" => 1,
        "cancelled" => 130,
        "signal" => 128 + signal.unwrap_or(1),
        _ => exit.unwrap_or(1),
    };
    Ok((r, code))
}
fn reader(
    mut input: impl Read + Send + 'static,
    truncated: Arc<AtomicBool>,
    cap: usize,
    keep: Keep,
    dest: Arc<Mutex<Destination>>,
    total_room: Option<Arc<Mutex<usize>>>,
    kill_on_cap: bool,
    capture_overflow_byte: bool,
    counts_for_idle: bool,
    tx: mpsc::Sender<Event>,
) -> thread::JoinHandle<()> {
    thread::spawn(move || {
        let mut room = cap;
        let mut capture_room = cap.saturating_add(usize::from(capture_overflow_byte));
        let mut ring = TailRing::new(cap);
        let mut observed = 0usize;
        let mut buf = [0u8; 8192];
        while let Ok(n) = input.read(&mut buf) {
            if n == 0 {
                break;
            }
            if counts_for_idle {
                let _ = tx.send(Event::Output(Instant::now()));
            }
            let shared_room = total_room.as_ref().map_or(usize::MAX, |remaining| {
                let mut remaining = remaining.lock().unwrap();
                let available = *remaining;
                *remaining = available.saturating_sub(n);
                available
            });
            match keep {
                Keep::Head => {
                    if n > room.min(shared_room) && !truncated.swap(true, Ordering::SeqCst) && kill_on_cap {
                        let _ = tx.send(Event::CapExceeded);
                    }
                    let keep_len = capture_room.min(shared_room).min(n);
                    room = room.saturating_sub(n);
                    capture_room = capture_room.saturating_sub(n);
                    if keep_len > 0 {
                        dest.lock().unwrap().write_chunk(&buf[..keep_len]);
                    }
                }
                Keep::Tail => {
                    observed += n;
                    let bound = cap.min(shared_room);
                    if n > bound || observed > cap {
                        truncated.store(true, Ordering::SeqCst);
                    }
                    if bound > 0 {
                        ring.cap = bound;
                        ring.push(&buf[..n]);
                    }
                }
            }
        }
        if keep == Keep::Tail {
            let bytes = ring.into_bytes();
            if !bytes.is_empty() {
                dest.lock().unwrap().write_chunk(&bytes);
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
/// Async-signal-safe cleanup: stop the child tree and let the main loop write
/// the `cancelled` verdict and exit 130.
#[cfg(unix)]
extern "C" fn on_stop_signal(signal: libc::c_int) {
    STOP_SIGNAL.store(signal, Ordering::SeqCst);
    STOP_REQUESTED.store(true, Ordering::SeqCst);
    let group = CHILD_GROUP.load(Ordering::SeqCst);
    if group > 0 {
        terminate_group(group, DEFAULT_TERM_GRACE);
    }
}

/// Turn CTRL_BREAK and console close into the same cancelled verdict as a POSIX
/// stop signal. Other control events keep their default handling.
#[cfg(windows)]
fn install_console_ctrl_handler() {
    const CTRL_CLOSE_EVENT: u32 = 2;
    const CTRL_BREAK_EVENT: u32 = 1;
    unsafe extern "system" fn handler(ctrl_type: u32) -> i32 {
        if ctrl_type == CTRL_BREAK_EVENT || ctrl_type == CTRL_CLOSE_EVENT {
            STOP_SIGNAL.store(ctrl_type as i32, Ordering::SeqCst);
            STOP_REQUESTED.store(true, Ordering::SeqCst);
            let job = CHILD_JOB.load(Ordering::SeqCst) as win_job::Handle;
            if !job.is_null() {
                unsafe { let _ = win_job::TerminateJobObject(job, 1); }
            }
            return 1;
        }
        0
    }
    unsafe {
        win_job::SetConsoleCtrlHandler(handler as *const std::ffi::c_void, 1);
    }
}

#[cfg(windows)]
fn store_job(job: win_job::Handle) {
    if job.is_null() {
        return;
    }
    CHILD_JOB.store(job as isize, Ordering::SeqCst);
}
#[cfg(unix)]
fn terminate_current(pid: u32, term_grace: Duration) {
    if pid <= i32::MAX as u32 {
        terminate_group(pid as i32, term_grace);
    }
}
/// Give the group its requested TERM grace before KILL. Only uses async-signal-safe calls.
#[cfg(unix)]
fn terminate_group(group: i32, mut term_grace: Duration) {
    if !term_grace.is_zero() {
        unsafe { libc::kill(-group, libc::SIGTERM) };
    }
    while !term_grace.is_zero() {
        if unsafe { libc::kill(-group, 0) } != 0 {
            return;
        }
        let interval = term_grace.min(Duration::from_millis(5));
        let pause = libc::timespec {
            tv_sec: 0,
            tv_nsec: interval.subsec_nanos() as _,
        };
        unsafe { libc::nanosleep(&pause, std::ptr::null_mut()) };
        term_grace -= interval;
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
fn parse_keep(s: &str) -> Result<Keep, String> {
    match s {
        "head" => Ok(Keep::Head),
        "tail" => Ok(Keep::Tail),
        _ => Err(format!("invalid keep mode: {s} (expected head or tail)")),
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
