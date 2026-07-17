# MVP Plan

## Relationship to the First Workable Slice

This document describes the stack-only Linux alpha. The smaller unattended
two-to-three-day proof is governed by
[ADR 0002](decisions/0002-72-hour-first-workable-slice.md) and its
[acceptance criteria](acceptance/first-workable-slice.md). That proof is
fixture-bound and does not replace the broader alpha milestones below.

## Goal

At a breakpoint inside a Rust function called from Python, VS Code shows:

```text
rust_extension::inner
Python: service.py::calculate
Python: app.py::main
```

Selecting each frame navigates to the correct source line. Continuing and
stopping again refreshes the stack without stale frame IDs.

## Milestones

### 0. Validate frame capture

- Read Python stacks with CPython 3.14's remote unwinder.
- Confirm collection while the process is running and externally stopped.
- Confirm OS thread IDs match the IDs reported by CodeLLDB.
- Cover calls, exceptions, generators, and multiple threads.

Exit criterion: a separate CPython 3.14 helper can read correct stacks while
LLDB owns and has stopped the process.

### 1. Build a deterministic mixed fixture

- Add a tiny PyO3 extension with two nested Rust functions.
- Add Python callers with nested functions and a worker thread.
- Build with debug symbols and disabled stripping.
- Document a CodeLLDB launch configuration.

Exit criterion: LLDB reliably stops in Rust and reports the expected native
thread ID and Rust symbols.

### 2. Implement transparent DAP forwarding

- Create a VS Code extension contributing a `pyrust` debug type.
- Locate the installed CodeLLDB extension and start its adapter executable.
- Proxy initialization, launch, breakpoints, threads, execution control, and
  events without behavior changes.
- Add protocol transcript tests with a fake downstream adapter.

Exit criterion: a normal Rust debug session works through the proxy with no
mixed-stack logic enabled.

### 3. Merge stack frames

- Invoke the CPython 3.14 remote unwinder on `stackTrace`.
- Map CodeLLDB thread IDs to OS thread IDs.
- Allocate synthetic frame IDs per stop epoch.
- Detect Python/native boundaries and merge frames.
- Apply DAP paging after the merge.
- Return empty scopes for Python frames in this milestone.

Exit criterion: the acceptance stack appears in one VS Code call-stack tree.

### 4. Harden the first release

- Add generator, exception, recursion, and multithread integration cases.
- Add a `showInterpreterFrames` escape hatch.
- Detect missing/incompatible monitor data and fall back to native-only stacks.
- Report clear diagnostics without terminating the native debug session.

Exit criterion: unsupported cases degrade to ordinary CodeLLDB behavior.

## Explicit non-goals

- Python breakpoints;
- Python variable inspection;
- Python expression evaluation;
- stepping from a Python line into an unknown Rust function;
- Rust -> embedded Python launch support;
- Windows support.

These are valuable, but none is required to prove that one mixed stack is
possible.

## Go/no-go checks

Continue after milestone 1 only if:

- remote unwinding latency is acceptable for interactive stack expansion;
- the private `_remote_debugging` API is present in the selected 3.14 build;
- thread IDs match CodeLLDB's reported OS IDs;
- remote unwinding succeeds at Rust breakpoints;
- Python source locations are accurate enough for navigation.

Continue after milestone 3 only if:

- CodeLLDB can be launched and proxied without relying on private VS Code APIs;
- the frame merge works across supported CPython versions;
- failures reliably fall back to native-only debugging.

If the CodeLLDB proxy proves too brittle, the fallback is a small maintained
CodeLLDB fork. If CPython's private remote-unwinder API proves too unstable, the
fallback is a small external reader built from its exported debug-offset format.
