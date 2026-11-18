# Agent guide

## Layout

- `src/main.rs`: Rust CLI and process lifecycle.
- `python/bounded_run/`: importable Python library.
- `docs/demo/`: VHS tapes, recorded demos, and social-preview source image.
- `pyproject.toml` and `.github/workflows/`: PyPI package workflow and the automatic pull request review.
- `docs/bench/`: saved benchmark results.
- `docs/diagrams/`: Mermaid sources and light/dark SVG output.
- `public.json`: exported public files.

## Build and use

- Run `cargo build --locked` from this directory.
- Run `cargo build --release --locked` for timing measurements.
- Install from a checkout with `cargo install --locked --path .`; the wheel built by `python3 -m pip wheel --no-deps -w dist .` includes the same Rust command and the `bounded_run` Python module. The package is not published on PyPI or crates.io.
- Install from a source checkout with `cargo install --path .`.
- Build a local wheel with `python3 -m pip wheel --no-deps --wheel-dir target/wheels .`.
- Run the demo with `vhs-safe docs/demo/run-bounded.tape` from the project root.

## Implementation rules

- Keep child stdout and stderr independent and drain both while enforcing caps.
- Accept absolute executable paths and bare names resolved through PATH; reject relative paths containing `/` or `\`. Pass only the documented minimal environment.
- Unix cleanup must signal the process group, not only the direct child.
- Windows cleanup must use a Job Object in both editions.
- Keep the JSON result on stderr so child stdout remains usable as command output.
- Preserve exit codes for child failures; timeout and idle use 124; a signal death returns 128 + the signal number.
- A deadline kill must report its timeout even when output forwarding is blocked on a consumer that stopped reading.
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
- changes a documented exit code: 124 for timeout and idle, 128 + signal number for a signal death.
- writes the JSON result to stdout instead of stderr, or emits a result that does not parse.
- leaves the CLI and the Python library disagreeing on result fields or cap semantics.
- inherits the full parent environment again, or resolves a relative path or a PATH entry the tool must reject.

Not defects: wording, formatting, type annotations, comment style, requested extra tests, or a renamed helper.
