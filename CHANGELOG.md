# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses `MAJOR.MINOR.PATCH`.

## [0.1.3] - 2026-11-19

### Added

- `--stdin null|inherit` and `--input TEXT` in the CLI, plus `stdin` and `input` parameters in `bounded_run.run()`.
- `--cwd DIR` in the CLI and `cwd` in `bounded_run.run()` to set the child working directory.
- Separate stream caps (`--max-stdout`, `--max-stderr` in the CLI; `max_stdout`, `max_stderr` in `bounded_run.run()`), falling back to `--max-output` / `max_output`.
- `--kill-on-cap` in the CLI and `kill_on_cap` in `bounded_run.run()` to terminate the child process group or Windows Job Object when either stream exceeds its cap, reporting classification `output_limit` (CLI exit code 1).

### Changed

- Child `stdin` is now closed by default (`null` / `DEVNULL`) in both editions instead of inheriting parent `stdin`.

### Fixed

- Python edition on macOS: a timeout whose process group needed SIGKILL could raise
  `PermissionError` from the cleanup, because macOS answers EPERM for a group left with only
  zombies. The cleanup now treats it as an empty group, as the Rust edition already did.

## [0.1.2] - 2026-11-18

### Changed

- The minimum Rust version is 1.82, declared in `Cargo.toml`, so `cargo build --locked` works on the
  stated minimum. The previous 1.75 claim could not compile the `unsafe extern` block or read the v4
  lockfile.

### Fixed

- A consumer that stops reading output can no longer block a timeout result: the deadline kill
  reports the timeout instead of waiting for a blocked write.
- A child terminated by a signal returns 128 plus that signal, instead of always 129.
- The Python edition reports the timeout after a bounded drain, instead of waiting for a descendant
  that left the process group and kept stdout or stderr open.
- SIGINT, SIGTERM and SIGHUP stop the child's process group before the runner exits, in both
  editions, so a parent exit no longer leaves the child tree behind.
- The Windows Job Object sets `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` in both editions, so a runner
  that dies without cleanup still ends the child tree.
- `--timeout` and `--idle` reject values outside the representable duration range with exit 125
  instead of panicking; the deadline arithmetic uses `checked_add`.

## [0.1.1] - 2026-11-17

### Added

- `run-bounded` Rust CLI: `--timeout`, `--idle`, `--max-output` and one JSON result line on stderr.
- `bounded_run` Python library with the same limits and result fields.
- Process-group termination on Unix and a Job Object on Windows, so grandchildren end with the
  child.
- Wheels that install the binary and the importable module together.
- Recorded demo and saved `hyperfine` measurements on macOS arm64, Ubuntu 24.04 arm64 and Windows
  x64.

## [0.1.0] - 2026-11-17

### Added

- First public source release of the bounded command runner.
