//! CLI regressions that only a real process can show: a stalled consumer must
//! not delay the timeout result, and a signal death must keep its signal.
#![cfg(unix)]

use std::io::{Read, Write};
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

#[test]
fn stdin_is_closed_by_default_and_can_inherit_or_take_input() {
    let mut default_child = Command::new(binary())
        .args(["--", "/bin/cat"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("spawn run-bounded");
    default_child
        .stdin
        .take()
        .unwrap()
        .write_all(b"should-not-arrive\n")
        .unwrap();
    let default_out = default_child.wait_with_output().expect("wait");
    assert_eq!(default_out.status.code(), Some(0));
    assert!(default_out.stdout.is_empty());

    let mut inherit_child = Command::new(binary())
        .args(["--stdin", "inherit", "--", "/bin/cat"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("spawn run-bounded");
    inherit_child
        .stdin
        .take()
        .unwrap()
        .write_all(b"from-parent\n")
        .unwrap();
    let inherit_out = inherit_child.wait_with_output().expect("wait");
    assert_eq!(inherit_out.status.code(), Some(0));
    assert_eq!(inherit_out.stdout, b"from-parent\n");

    let input_out = Command::new(binary())
        .args(["--input", "from-flag\n", "--", "/bin/cat"])
        .output()
        .expect("spawn run-bounded");
    assert_eq!(input_out.status.code(), Some(0));
    assert_eq!(input_out.stdout, b"from-flag\n");

    // Exceed the 64 KiB OS pipe buffer while staying below Linux's 128 KiB MAX_ARG_STRLEN
    // (131,072 bytes including the NUL terminator); larger streams belong on --stdin inherit.
    let large_input = "x".repeat(98_304);
    let ignored_input_out = Command::new(binary())
        .args(["--input", &large_input, "--", "/usr/bin/true"])
        .output()
        .expect("spawn run-bounded");
    assert_eq!(ignored_input_out.status.code(), Some(0));

    let conflict_out = Command::new(binary())
        .args(["--stdin", "inherit", "--input", "x", "--", "/usr/bin/true"])
        .output()
        .expect("spawn run-bounded");
    assert_eq!(conflict_out.status.code(), Some(125));
}

#[test]
fn cwd_sets_working_directory() {
    let dir = std::env::temp_dir().join(format!("run-bounded-cwd-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).expect("create cwd dir");
    let canonical = std::fs::canonicalize(&dir).expect("canonicalize cwd");

    let output = Command::new(binary())
        .args(["--cwd"])
        .arg(&dir)
        .args(["--", "/bin/pwd"])
        .output()
        .expect("spawn run-bounded");
    let _ = std::fs::remove_dir_all(&dir);

    assert_eq!(output.status.code(), Some(0));
    let actual = String::from_utf8_lossy(&output.stdout).trim().to_string();
    assert_eq!(actual, canonical.to_string_lossy());
}

#[test]
fn separate_stdout_and_stderr_caps_truncate_independently() {
    let output = Command::new(binary())
        .args([
            "--max-stdout",
            "5",
            "--max-stderr",
            "10",
            "--",
            "/bin/sh",
            "-c",
            "printf '12345678'; printf 'abcdefgh' >&2",
        ])
        .output()
        .expect("spawn run-bounded");
    assert_eq!(output.status.code(), Some(0));
    assert_eq!(output.stdout, b"12345");
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.starts_with("abcdefgh{"), "stderr: {stderr}");
    assert!(stderr.contains("\"class\":\"ok\""), "stderr: {stderr}");
    assert!(stderr.contains("\"stdout_truncated\":true"), "stderr: {stderr}");
    assert!(stderr.contains("\"stderr_truncated\":false"), "stderr: {stderr}");
}

#[test]
fn kill_on_cap_terminates_process_group_and_reports_output_limit() {
    let dir = std::env::temp_dir().join(format!("run-bounded-cap-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).expect("create pid dir");
    let pidfile = dir.join("child.pid");
    let script = format!(
        "sleep 30 & echo $! > '{}'; printf '0123456789abcdef' >&2; sleep 30",
        pidfile.display()
    );
    let started = Instant::now();
    let output = Command::new(binary())
        .args([
            "--timeout",
            "10",
            "--idle",
            "0",
            "--max-stdout",
            "100",
            "--max-stderr",
            "8",
            "--kill-on-cap",
            "--",
            "/bin/sh",
            "-c",
            &script,
        ])
        .output()
        .expect("spawn run-bounded");
    let elapsed = started.elapsed();
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert_eq!(output.status.code(), Some(1), "stderr: {stderr}");
    assert!(elapsed < Duration::from_secs(3), "elapsed: {elapsed:?}");
    assert!(stderr.contains("\"class\":\"output_limit\""), "stderr: {stderr}");
    assert!(stderr.contains("\"stdout_truncated\":false"), "stderr: {stderr}");
    assert!(stderr.contains("\"stderr_truncated\":true"), "stderr: {stderr}");

    let child_pid: i32 = std::fs::read_to_string(&pidfile)
        .expect("read descendant pidfile")
        .trim()
        .parse()
        .expect("parse descendant pid");
    let deadline = Instant::now() + Duration::from_secs(3);
    while Instant::now() < deadline {
        if unsafe { libc::kill(child_pid, 0) } != 0 {
            break;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    assert_ne!(unsafe { libc::kill(child_pid, 0) }, 0, "descendant survived kill-on-cap");
    let _ = std::fs::remove_dir_all(&dir);
}
