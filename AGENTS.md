# Agent guide

## Layout

- `src/main.rs`: Rust CLI and process lifecycle.
- `python/bounded_run/`: importable Python library.
- `docs/demo/`: VHS tapes, recorded demos, and social-preview source image.
- `pyproject.toml` and `.github/workflows/wheels.yml`: PyPI package and wheel publishing workflow.
- `docs/bench/`: saved benchmark results.
- `docs/diagrams/`: Mermaid sources and light/dark SVG output.
- `public.json`: exported public files.

## Build and use

- Run `cargo build --locked` from this directory.
- Run `cargo build --release --locked` for timing measurements.
- Install the CLI from PyPI with `pipx install run-bounded` or `uv tool install run-bounded`; the wheel includes the Rust command and `bounded_run` Python module.
- Install from a source checkout with `cargo install --path .`.
- Build a local wheel with `python3 -m pip wheel --no-deps --wheel-dir target/wheels .`.
- Run the demo with `vhs-safe docs/demo/run-bounded.tape` from the project root.

## Implementation rules

- Keep child stdout and stderr independent and drain both while enforcing caps.
- Accept absolute executable paths and bare names resolved through PATH; reject relative paths containing `/` or `\`. Pass only the documented minimal environment.
- Unix cleanup must signal the process group, not only the direct child.
- Windows cleanup must use a Job Object in both editions.
- Keep the JSON result on stderr so child stdout remains usable as command output.
- Preserve exit codes for child failures; timeout and idle use 124.
- Never put real home paths, hostnames, credentials, or account data in demo inputs.
- Keep README.md and README.ko.md aligned in commands, measurements, and platform claims.
- Do not publish the CLI to crates.io or Python to PyPI without owner review.
- Keep dependency versions locked in Cargo.lock and build with `--locked`.
- Keep the CLI and Python library result classes and cap semantics aligned.
- Regenerate charts from the saved benchmark JSON; do not type benchmark values into graph sources.
