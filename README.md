<div align="center">

<img src="https://raw.githubusercontent.com/elfenomenokim/run-bounded/main/docs/logo.svg" width="96" height="96" alt="run-bounded: a command arrow stopped by a limit">

# run-bounded
[한국어](README.ko.md)

A command-line tool and Python library for running commands with time, output, and process-tree limits.

Rust 1.75+ · Python 3.10+

[![rust](https://img.shields.io/badge/rust-1.75%2B-lightgrey)](https://github.com/elfenomenokim/run-bounded/blob/main/Cargo.toml)
[![license](https://img.shields.io/github/license/elfenomenokim/run-bounded)](https://github.com/elfenomenokim/run-bounded/blob/main/LICENSE)

5.3 ms per run; a Python `subprocess.run` call to the same command takes 37.9 ms. Both from the saved 30-run `hyperfine` file.

[Install](#install) · [Quick start](#quick-start) · [Why](#why)

</div>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/elfenomenokim/run-bounded/main/docs/demo/run-bounded-dark.gif">
  <img src="https://raw.githubusercontent.com/elfenomenokim/run-bounded/main/docs/demo/run-bounded.gif" alt="The run-bounded CLI prints child output and a JSON result" width="1200">
</picture>

The demo uses `printf` with fixed text. It shows command output, not a benchmark.

## Install

```sh
git clone https://github.com/elfenomenokim/run-bounded
cd run-bounded
cargo install --locked --path .
```

`cargo install` puts the `run-bounded` binary in `~/.cargo/bin`. For the Python library, build the
wheel from the same checkout: `python3 -m pip wheel --no-deps -w dist .` and install the file in
`dist/`. The package is not on PyPI yet.

Rust 1.75 or newer is needed; the wheel installs the same binary and the importable Python package
`bounded_run`.

## Quick start

```sh
run-bounded --timeout 5 --idle 2 --max-output 1K -- printf 'bounded output\n'
```

`printf` resolves through `PATH`. Absolute executable paths also work. Relative paths containing `/` or `\` are rejected.

The child keeps stdout and stderr. The command writes one JSON result line to stderr:

```text
bounded output
{"class":"ok","duration_ms":6,"exit_code":0,"stdout_truncated":false,"stderr_truncated":false}
```

`--timeout` sets wall seconds. `--idle` sets seconds without output. `--max-output` sets a per-stream byte cap; K, M, and G use powers of 1024. Zero disables a time limit. The child receives a small environment with PATH, locale, home, temporary-directory, and Windows system variables.

## Why

`run-bounded` drains stdout and stderr independently and limits each stream. On Unix, it starts a new process group and sends TERM followed by KILL when a limit expires. Windows assigns the child to a Job Object and terminates the job on timeout. The Rust CLI is one binary; the Python edition is importable without a native extension. feno-cli keeps its existing `run_process_group` API.

| Tool | Wall timeout | Output cap | Grandchild after timeout |
|---|---|---|---|
| run-bounded | yes | per stream | terminated on macOS, Linux, and Windows checks |
| coreutils `gtimeout` | yes | no | remained alive in the macOS check |
| Python `subprocess.run(timeout=...)` | yes | no | remained alive in the macOS check |

## How it works

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/elfenomenokim/run-bounded/main/docs/diagrams/flow.dark.svg">
  <img src="https://raw.githubusercontent.com/elfenomenokim/run-bounded/main/docs/diagrams/flow.light.svg" alt="The runner starts a child, drains both streams, waits for exit or a deadline, and terminates the process tree on timeout">
</picture>

The source is [`flow.mmd`](docs/diagrams/flow.mmd); the Korean README has Korean labels.

## Measurements

These are `hyperfine` 2.0.0 wall-time means and standard deviations for `/usr/bin/true`. The host was macOS 27.0 on arm64. Each command ran 3 warmups and 30 measured runs. Python command rows include starting the Python interpreter for each run.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/elfenomenokim/run-bounded/main/docs/diagrams/bench.dark.svg">
  <img src="https://raw.githubusercontent.com/elfenomenokim/run-bounded/main/docs/diagrams/bench.light.svg" alt="Mean wall time: run-bounded 5.3 milliseconds, gtimeout 6.2, Python subprocess.run 37.9, Python library command 61.7">
</picture>

| Command | Mean ± standard deviation |
|---|---:|
| run-bounded CLI | 5.3 ± 0.8 ms |
| `gtimeout` | 6.2 ± 0.7 ms |
| Python `subprocess.run` | 37.9 ± 3.5 ms |
| Python library command | 61.7 ± 15.4 ms |

Re-run from the CCH repository root. The saved measurements are in [`macos-27-arm64-20261117.json`](docs/bench/macos-27-arm64-20261117.json).

```sh
cargo build --locked --release --manifest-path docs/products/feno-cli/bounded-run/Cargo.toml
hyperfine --shell=none --warmup 3 --runs 30 \
  --export-json docs/products/feno-cli/bounded-run/docs/bench/macos-27-arm64-20261117.json \
  --command-name 'run-bounded' './docs/products/feno-cli/bounded-run/target/release/run-bounded --timeout 5 --idle 2 -- /usr/bin/true' \
  --command-name 'gtimeout' '/opt/homebrew/bin/gtimeout 5 /usr/bin/true' \
  --command-name 'Python subprocess.run' '/usr/bin/python3 -c "import subprocess; subprocess.run([\"/usr/bin/true\"], check=True)"' \
  --command-name 'Python library' '/usr/bin/python3 -c "import sys; sys.path.insert(0, \"docs/products/feno-cli/bounded-run/python\"); from bounded_run import run; run([\"/usr/bin/true\"])'
```

Inside an already-started Python interpreter, `bounded_run.run()` had a 3.5 ms median over 30 calls; [the saved samples](docs/bench/python-in-process-20261117.json) include the command and host details. The grandchild comparison used a child that ignored TERM and slept for 30 seconds. `run-bounded` removed it; `gtimeout` and `subprocess.run(timeout=...)` left it alive.

## Reference

```text
run-bounded [--timeout SEC] [--idle SEC] [--max-output BYTES] -- COMMAND [ARGS...]
```

`COMMAND` can be an absolute executable path or a bare name found on PATH. The library is `bounded_run.run(command, timeout=60, idle=20, max_output=1048576)`. It returns a `RunResult` with `classification`, `duration`, `returncode`, captured `stdout` and `stderr` bytes, and one truncation flag per stream. Classifications are `ok`, `failed`, `timeout`, `idle`, or `signal`.

## Platforms

The CLI was built and exercised on macOS 27.0 arm64, Ubuntu 24.04 arm64 on A1, and Windows build 26340 x64 (reported OS version 10.0.26340.0). The Python library was exercised on macOS and Windows. The Windows Rust build used the laptop's existing Rust 1.100 GNU toolchain. Timeout and idle return 124, CLI errors return 125, and normal child failures retain the child's exit code. A child terminated by a signal returns 128 plus the signal number, and its JSON `exit_code` is null. A consumer that stops reading its output cannot delay a timeout result: once a limit terminates the child, the CLI reports the timeout without waiting for the blocked output write.

## Contributing

Run `cargo build --locked` from this directory. Issues and platform reports are welcome.
See [CONTRIBUTING.md](CONTRIBUTING.md) for the tests and the review process.

## License

MIT. See [LICENSE](LICENSE).
