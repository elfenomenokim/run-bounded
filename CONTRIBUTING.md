# Contributing to run-bounded

Fixes and reports are welcome. You do not need to ask before opening a pull request for a bug fix.

## Before you start

- **A bug:** open an [issue](https://github.com/elfenomenokim/run-bounded/issues/new/choose) with
  the command and its output, the version (`run-bounded --version`) and your platform. A child
  command that reproduces it helps most.
- **A bigger change** (a new flag, a change to the exit codes or the JSON result): open an issue
  first so the shape can be agreed before you write it.
- **A security problem:** do not open a public issue. See [SECURITY.md](SECURITY.md).

## Set up

Rust 1.75 or newer. The test suite needs nothing else.

```sh
git clone https://github.com/elfenomenokim/run-bounded
cd run-bounded
cargo build --locked
cargo test --locked
```

The Python library lives in `python/bounded_run/` and is built by maturin; build it with
`python3 -m pip wheel --no-deps --wheel-dir target/wheels .` after the Rust build passes.

## What a good patch has

- **A test that fails without the change** in `tests/cli.rs`, run once against the old code.
- **The flags, JSON fields and exit codes aligned** between the CLI and the Python library. A change
  to one that is not made in the other is a defect, not a follow-up.
- **A line under Unreleased** in [CHANGELOG.md](CHANGELOG.md) when a user will notice.
- **The platforms you did not test** written in the pull request. Say macOS arm64, Linux arm64,
  Windows x64 or nothing.

## Review

Every pull request runs an automatic DeepSeek review. Ask for another pass with `@codex review` or
`@claude review` in a comment. If a finding looks wrong, say why in the pull request; the review is
advice, not a gate.

## Style

Match the code around you; run `cargo fmt` and `cargo clippy --all-targets -- -D warnings` before
pushing. Keep one change per pull request. Demo records use fixed text only: never a real home
path, hostname or account.

## Licence

Contributions are licensed under the MIT licence in [LICENSE](LICENSE).
