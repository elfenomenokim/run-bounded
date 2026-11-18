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
