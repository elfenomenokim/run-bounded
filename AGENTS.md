# Agent guide

## Set up for a user

Goal: `run-bounded` runs on the user's machine and stops a command that runs too long, goes silent or prints too much. Nothing here needs the person.

1. Check what is present.
   - `uv --version` prints a version. If not: `curl -LsSf https://astral.sh/uv/install.sh | sh`, then `export PATH="$HOME/.local/bin:$PATH"` (or open a new shell). On Windows: `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`.
   - Python and Rust are not needed: the wheel carries the binary, and uv fetches a Python by itself.
2. Install: `uv tool install run-bounded`. It ends with `Installed 1 executable: run-bounded`. `uv tool update-shell` puts `~/.local/bin` on PATH for later shells. (Python code that wants the library instead: `uv pip install run-bounded` inside the project's venv, then `import bounded_run`.)
3. Prove it:
   `run-bounded --timeout 5 --idle 2 --max-output 1K -- printf 'bounded output\n'` prints `bounded output` and then one JSON line on stderr starting `{"class":"ok","duration_ms":`, and exits 0.
   `run-bounded --timeout 1 -- sleep 3; echo $?` prints a JSON line with `"class":"timeout"` and then `124`.
4. If it fails, in this order:
   - `run-bounded: command not found`: `~/.local/bin` is not on PATH (step 1).
   - Exit 125: a bad option or a limit out of range; read the printed message. `--help` and `--version` do not exist; the options are listed in README.md.
   - `relative command paths with a separator are not accepted`: give a bare name (`printf`) or an absolute path, not `./tool`.
   - `No such file or directory (os error 2)`: the command is not on PATH in the minimal environment `run-bounded` passes; use its absolute path.

## Layout

- `src/main.rs`: Rust CLI and process lifecycle.
- `python/bounded_run/`: importable Python library.
- `tests/cli.rs` and `tests/test_python.py`: regressions that need a real process (`cargo test --locked`, `python3 tests/test_python.py`).
- `docs/demo/`: VHS tapes, recorded demos, and social-preview source image.
- `pyproject.toml` and `.github/workflows/`: PyPI package workflow and the automatic pull request review.
- `docs/bench/`: saved benchmark results.
- `docs/diagrams/`: Mermaid sources and light/dark SVG output.
- `public.json`: exported public files.

## Build and use

- Run `cargo build --locked` from this directory.
- Run `cargo build --release --locked` for timing measurements.
- Install from a checkout with `cargo install --locked --path .`; the wheel built by `python3 -m pip wheel --no-deps -w dist .` includes the same Rust command and the `bounded_run` Python module. The wheel is published on PyPI as `run-bounded`; it is not on crates.io.
- Install from a source checkout with `cargo install --path .`.
- Build a local wheel with `python3 -m pip wheel --no-deps --wheel-dir target/wheels .`.
- Run the demo with `vhs-safe docs/demo/run-bounded.tape` from the project root.

## Implementation rules

- Keep child `stdin` closed by default unless `inherit` or `input` is requested; keep child stdout and stderr independent and drain both while enforcing per-stream caps (`--max-stdout`/`--max-stderr`, defaulting to `--max-output`).
- Accept absolute executable paths and bare names resolved through PATH; reject relative paths containing `/` or `\`. Use the documented minimal environment by default. Exact environment forwarding requires explicit opt-in.
- Unix cleanup must signal the process group, not only the direct child, and a signal that stops the runner must do the same.
- Windows cleanup must use a Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` in both editions.
- Keep the JSON result on stderr so child stdout remains usable as command output.
- Preserve exit codes for child failures; timeout and idle use 124; `output_limit` (when `--kill-on-cap` stops the child) uses 1; a signal death returns 128 + the signal number.
- A deadline kill must report its timeout even when output forwarding is blocked on a consumer that stopped reading, in both editions.
- A limit value outside the representable duration range is a CLI error (125), never a panic.
- Never put real home paths, hostnames, credentials, or account data in demo inputs.
- Keep README.md and README.ko.md aligned in commands, measurements, and platform claims.
- Do not publish the CLI to crates.io or Python to PyPI without owner review.
- Keep dependency versions locked in Cargo.lock and build with `--locked`.
- Keep the CLI and Python library result classes and cap semantics aligned.
- Regenerate charts from the saved benchmark JSON; do not type benchmark values into graph sources.

## Code Review Rules

A change is a real defect when it:

- lets a child process or its process group survive a timeout, an idle timeout or a parent exit on any platform.
- drops or interleaves child stdout and stderr, or loses bytes when a consumer stops reading.
- breaks an output cap, an idle deadline, or the absolute-path rule for the executable.
- changes a documented exit code: 124 for timeout and idle, 1 for `output_limit`, 128 + signal number for a signal death.
- writes the JSON result to stdout instead of stderr, or emits a result that does not parse.
- leaves the CLI and the Python library disagreeing on result fields or cap semantics.
- inherits the full parent environment without explicit opt-in, or resolves a relative path or a PATH entry the tool must reject.

Not defects: wording, formatting, type annotations, comment style, requested extra tests, or a renamed helper.
