# pyrust-debugger

An experimental VS Code debugger that presents Python and Rust frames in one
call stack.

## Initial direction

The first target is:

```text
Python application -> Rust extension
```

This is the cheaper direction to validate because LLDB can launch the Python
executable and debug the Rust shared library normally. The project only needs
to add Python's logical frames to the native stack.

The reverse direction:

```text
Rust application -> embedded Python
```

uses the same stack-merging mechanism, but is deferred until the core approach
works. It adds interpreter initialization, packaging, and attach timing without
reducing the main technical risk.

## MVP boundary

The first milestone is deliberately stack-only:

- launch Python under CodeLLDB;
- stop at a Rust breakpoint;
- show active Python frames and native Rust frames in one VS Code call stack;
- support CPython 3.14 on Linux first.

Python breakpoints, Python locals, and cross-language stepping are later
milestones. See [the feasibility study](docs/feasibility.md) and
[the documentation index](docs/README.md).

The two executable research fixtures and observed CodeLLDB results are
documented in [the fixture report](docs/research/fixture-results.md).

## Early probe

CPython 3.14 exports debugger metadata and includes a remote unwinder that can
read Python stacks directly from another process. `prototype/python` wraps that
facility and proves it can read a debuggee both while running and while stopped.
The target process needs no tracing hooks or injected recorder.

## Development environment

The repository uses CPython 3.14.6. Create the local environment with:

```bash
uv python install 3.14
uv venv --python 3.14 .venv
```

The environment has already been created in this workspace. Verify it with:

```bash
.venv/bin/python --version
```

Run the prototype tests with:

```bash
PYTHONPATH=prototype/python \
  .venv/bin/python -m unittest discover -s prototype/python/tests -v
```

## First workable slice

The fixture-bound DAP proof is implemented under `prototype/adapter`. Run its
complete automated contract, including the real CodeLLDB integration, with:

```bash
./scripts/accept-first-slice.sh
```

The command must report `PASS` for `AC-HP-01` through `AC-SP-04`. This slice is
limited to the documented CPython 3.14, Linux, single-thread Python-to-Rust
fixture; it is not yet a packaged VS Code extension.
