//! CLI regressions that only a real process can show: a stalled consumer must
//! not delay the timeout result, and a signal death must keep its signal.
#![cfg(unix)]

use std::io::Read;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

fn binary() -> &'static str {
    env!("CARGO_BIN_EXE_run-bounded")
}

#[test]
fn signal_termination_returns_128_plus_signal() {
    let output = Command::new(binary())
        .args(["--", "/bin/sh", "-c", "kill -TERM $$"])
        .output()
        .expect("spawn run-bounded");
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert_eq!(output.status.code(), Some(143), "stderr: {stderr}");
    assert!(stderr.contains("\"class\":\"signal\""), "stderr: {stderr}");
    assert!(stderr.contains("\"exit_code\":null"), "stderr: {stderr}");
}

#[test]
fn timeout_finishes_when_stdout_is_not_read() {
    let mut child = Command::new(binary())
        .args([
            "--timeout",
            "0.5",
            "--idle",
            "0",
            "--",
            "/bin/sh",
            "-c",
            "while :; do printf 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'; done",
        ])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("spawn run-bounded");
    // Hold the pipe without reading it so the forwarding write blocks once the
    // pipe buffer fills.
    let _stdout = child.stdout.take().unwrap();
    let deadline = Instant::now() + Duration::from_secs(10);
    let status = loop {
        if let Some(status) = child.try_wait().expect("try_wait") {
            break status;
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            panic!("run-bounded did not return its timeout result while stdout was unread");
        }
        std::thread::sleep(Duration::from_millis(20));
    };
    assert_eq!(status.code(), Some(124));
    let mut stderr = String::new();
    child
        .stderr
        .take()
        .unwrap()
        .read_to_string(&mut stderr)
        .unwrap();
    assert!(stderr.contains("\"class\":\"timeout\""), "stderr: {stderr}");
}

#[test]
fn a_huge_limit_is_an_error_not_a_panic() {
    let output = Command::new(binary())
        .args(["--timeout", "1e300", "--", "/usr/bin/true"])
        .output()
        .expect("spawn run-bounded");
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert_eq!(output.status.code(), Some(125), "stderr: {stderr}");
    assert!(stderr.contains("duration range"), "stderr: {stderr}");
}

#[test]
fn a_runner_signal_terminates_the_child_group() {
    let dir = std::env::temp_dir().join(format!("run-bounded-signal-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).expect("create the pid directory");
    let pidfile = dir.join("child.pid");
    let script = format!("echo $$ > '{}'; exec sleep 30", pidfile.display());
    let mut runner = Command::new(binary())
        .args(["--timeout", "30", "--idle", "0", "--", "/bin/sh", "-c"])
        .arg(&script)
        .spawn()
        .expect("spawn run-bounded");
    let deadline = Instant::now() + Duration::from_secs(10);
    let child_pid = loop {
        if let Ok(text) = std::fs::read_to_string(&pidfile) {
            if let Ok(pid) = text.trim().parse::<i32>() {
                break pid;
            }
        }
        if Instant::now() >= deadline {
            let _ = runner.kill();
            let _ = runner.wait();
            panic!("the child never wrote its pid");
        }
        std::thread::sleep(Duration::from_millis(20));
    };
    unsafe { libc::kill(runner.id() as i32, libc::SIGINT) };
    let _ = runner.wait().expect("wait for run-bounded");
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        if unsafe { libc::kill(child_pid, 0) } != 0 {
            break;
        }
        if Instant::now() >= deadline {
            unsafe { libc::kill(child_pid, libc::SIGKILL) };
            panic!("the child outlived a SIGINT to the runner");
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    let _ = std::fs::remove_dir_all(&dir);
}
