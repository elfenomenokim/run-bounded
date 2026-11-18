# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses `MAJOR.MINOR.PATCH`.

## [Unreleased]

### Fixed

- A consumer that stops reading output can no longer block a timeout result: the deadline kill
  reports the timeout instead of waiting for a blocked write.
- A child terminated by a signal returns 128 plus that signal, instead of always 129.

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
