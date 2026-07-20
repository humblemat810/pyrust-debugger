# First Workable Slice Acceptance

## Purpose

These criteria define completion for the unattended 72-hour technical proof.
They intentionally cover happy paths and simple sad paths only.

## Fixed Environment

The proof targets exactly:

```text
Ubuntu Linux x86_64
CPython 3.14.6 from .venv
CodeLLDB 1.12.2 with its bundled LLDB
Rust stable debug profile
research/fixtures/python_outer
```

Different versions or environments may be reported as unsupported.

## Required Acceptance Command

The implementation must provide:

```bash
./scripts/accept-first-slice.sh
```

The command must build prerequisites, run deterministic tests, run the real
CodeLLDB integration, print a concise result for every criterion below, and
exit nonzero if any required criterion fails.

It must be safe to run repeatedly from the repository root.

## Happy-Path Criteria

### AC-HP-01: Transparent Native Launch

Given the Python-outer fixture, when the acceptance client launches it through
the PyRust DAP proxy, then CodeLLDB must initialize, bind or resolve the Rust
source breakpoint, and emit a stopped event for `rust_inner`.

### AC-HP-02: Mixed User Stack

Given the stop in `rust_inner`, when the client requests `stackTrace` for the
stopped thread, then the response must contain these user frames in this order:

```text
rust_inner
rust_outer
python_inner
python_outer
```

Additional native runtime frames may remain below the required user frames.

### AC-HP-03: Source Navigation Data

Each required Rust and Python user frame must contain:

- the expected absolute or workspace-resolvable source path;
- a positive line number matching the fixture source;
- a frame name that identifies the expected function.

### AC-HP-04: Native Request Preservation

For an original CodeLLDB Rust frame:

- its downstream frame ID must remain usable;
- `scopes` must be forwarded successfully;
- a deterministic Rust/native evaluation request must receive a successful
  CodeLLDB response.

The acceptance test chooses an expression already known to work in the fixture.

### AC-HP-05: Stop-Epoch Refresh

Given an acceptance-only Python driver that invokes the existing fixture call
path twice, after continuing from the first stop to the second deterministic
stop:

- the mixed stack must be collected again;
- new synthetic Python frame IDs must be allocated;
- synthetic IDs from the prior stop must not be accepted as current frames.

The driver may live under `tests/acceptance/`; it must not alter the research
fixture's normal behavior.

## Simple Sad-Path Criteria

### AC-SP-01: Helper Failure Fallback

Given a configured helper that exits with an error, `stackTrace` must return the
unmodified CodeLLDB native stack. The debug session must remain usable and emit
one concise diagnostic.

### AC-SP-02: Helper Timeout Fallback

Given a helper that exceeds the configured timeout, the proxy must terminate or
abandon that helper, return the native stack within two seconds, and emit one
concise diagnostic.

### AC-SP-03: Synthetic Python Scopes

Given a current synthetic Python frame ID, a `scopes` request must return an
adapter-owned `Python Locals` scope without forwarding that frame ID to
CodeLLDB. Its variables must include the fixture's `value = 20`, and the safe
snapshot expression `value + 1` must return `21`.

This criterion was expanded by
[ADR 0005](../decisions/0005-read-only-python-frame-locals.md). The snapshot
reader is read-only and does not execute Python code in the stopped process.

### AC-SP-04: Clean Protocol Failure

If CodeLLDB exits early or returns malformed/incomplete DAP traffic, the proxy
and acceptance command must fail with a bounded timeout and a layer-specific
message. They must not hang indefinitely.

## Required Automated Tests

The acceptance command must include:

- DAP framing tests for fragmented and multiple messages;
- request/response sequence-remapping tests;
- synthetic-frame ID epoch tests;
- mixed-stack ordering and paging tests for the fixture shape;
- helper failure and timeout tests;
- one real CodeLLDB integration run.

## Completion Evidence

The command output must report:

```text
AC-HP-01 PASS
AC-HP-02 PASS
AC-HP-03 PASS
AC-HP-04 PASS
AC-HP-05 PASS
AC-SP-01 PASS
AC-SP-02 PASS
AC-SP-03 PASS
AC-SP-04 PASS
```

A DAP transcript may be written under `research/results/` for diagnosis, but it
is not required to be committed.

## Not Accepted as Completion

The following do not satisfy this contract by themselves:

- only unit tests with a fake adapter;
- only direct use of the existing research probe;
- screenshots without DAP assertions;
- a mixed stack that loses or replaces usable Rust frame IDs;
- a process that must be manually paused, continued, or inspected;
- undocumented commands or manual edits between test steps.
