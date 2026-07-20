# Multiprocess Native Attach Result

## Date

2026-07-20.

## Question

Can one PyRust DAP session expose two Python `spawn` children, each with a
private CodeLLDB adapter, as selectable Rust/Python mixed stacks?

## Fixture

`tests/acceptance/multiprocess_fixture_driver.py` starts two
`multiprocessing.get_context("spawn")` children. Each child:

1. registers its PID and parent PID in a coordinator directory;
2. permits the local coordinator to ptrace it for this test fixture;
3. waits until the coordinator has attached;
4. calls `pyrust_native.rust_outer(value)`.

The coordinator starts a CodeLLDB DAP process per registered
child, installs the Rust breakpoint, and virtualizes child frame and variable
IDs before they reach VS Code.

## Observed Result

The child registrations succeed. Both private CodeLLDB adapters reach the
Rust breakpoint and emit a DAP `stopped` event such as:

```text
reason = breakpoint
threadId = <child PID>
systemProcessId = <child PID>
```

At that stop, the child adapter's `threads` response contains the stopped
thread and its `stackTrace` response contains the Rust extension frames.
PyRust then inserts the matching CPython frames and routes expressions to the
selected Rust or Python frame.

The test command is:

```bash
./scripts/accept-multiprocess-slice.sh
```

It is a green acceptance command. The Rust-parent equivalent is:

```bash
./scripts/accept-rust-multiprocess-slice.sh
```

## Interpretation

The process coordinator and DAP identity routing are implemented and covered
by unit and end-to-end tests. The false empty-stack result came from sending
`levels: 0` in the child `stackTrace` request; CodeLLDB treats it as a request
for zero frames. A child-only launch mode starts the parent directly and
attaches CodeLLDB only to registered children.

## Required Next Experiment

Follow-on work should add debugpy-owned Python breakpoints per child and
validate `forkserver`. Plain `fork()` remains outside this release.
