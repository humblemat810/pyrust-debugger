# Rust-Outer Stabilization Acceptance

## Purpose

This contract completes ADR 0003. It first hardens the Python-to-Rust slice,
then proves a Rust application embedding Python can display one mixed stack at
an explicit Rust callback breakpoint.

## Fixed Environment

The contract targets:

```text
Ubuntu Linux x86_64
CPython 3.14.6 from .venv
CodeLLDB 1.12.2 with its bundled LLDB
Rust stable debug profile
research/fixtures/python_outer
research/fixtures/rust_outer
```

## Required Commands

Both commands must pass from the repository root:

```bash
./scripts/accept-first-slice.sh
./scripts/accept-reverse-slice.sh
```

Each command must start a fresh proxy process and may not rely on an earlier
debug session.

## Stabilization Criteria

### AC-BF-01: Single In-Process Worker

Given an in-process unwinder call that remains blocked, concurrent and repeated
`stackTrace` requests must create no more than one unwinder worker.

### AC-BF-02: Session Circuit Breaker

After the first in-process unwind timeout:

- the triggering request returns the unmodified native stack within two
  seconds;
- the circuit remains open for the proxy session;
- later stack requests return native stacks without waiting for the helper
  timeout;
- no replacement in-process worker is started.

### AC-BF-03: One Session Diagnostic

The timeout emits exactly one concise diagnostic. Continuing to another stop
and requesting additional stacks must not emit duplicate circuit-open
diagnostics.

### AC-BF-04: Existing Slice Regression

`./scripts/accept-first-slice.sh` must continue to report all ADR 0002
`AC-HP-*` and `AC-SP-*` criteria as passing.

### AC-BF-05: Epoch and Identity Safety

Late helper completion must not allocate synthetic frames into a newer stop
epoch. Native frame IDs must remain native even if an old synthetic allocator
previously used the same integer.

## Reverse-Direction Criteria

### AC-RP-01: Rust Launch and Callback Stop

The acceptance client must launch `research/fixtures/rust_outer` through the
proxy, bind a Rust source breakpoint in `rust_callback`, and receive a stopped
event for that callback.

### AC-RP-02: Mixed User Stack

The stack must contain this ordered user-frame subsequence:

```text
rust_callback
python_inner
python_outer
rust_outer
main
```

Additional embedded-module and runtime frames are allowed.

### AC-RP-03: Source Navigation

The required Python and Rust frames must provide the expected workspace source
path and a positive line number matching the fixture.

### AC-RP-04: Upper and Lower Native Identity

The original CodeLLDB IDs for `rust_callback` and at least one lower Rust frame
must remain usable. A `scopes` request for each selected native frame must be
routed to CodeLLDB successfully.

### AC-RP-05: Synthetic Python Behavior

Synthetic Python frame IDs must be valid only for the current stop epoch.
`scopes` returns an adapter-owned `Python Locals` scope. The `python_inner`
fixture frame exposes `value = 20`, and snapshot evaluation of `value + 1`
returns `21` without forwarding the synthetic ID to CodeLLDB or executing
Python in the target process.

This criterion was expanded by
[ADR 0005](../decisions/0005-read-only-python-frame-locals.md).

### AC-RP-06: Reverse Fallback

If Python collection fails, times out, returns no matching thread, or cannot be
placed between proven native boundaries, `stackTrace` must return the
unmodified native stack and leave the debug session usable.

### AC-RP-07: Repeated Stop

The fixture or acceptance-only driver must reach `rust_callback` twice.
Continuing to the second stop must recollect the mixed stack and reject
synthetic IDs from the first stop.

## Required Automated Coverage

The two acceptance commands together must cover:

- the circuit breaker under a permanently blocked reader;
- repeated requests and repeated stop epochs after a timeout;
- golden Python-to-Rust and Rust-to-Python merge shapes;
- preservation of upper and lower native frame IDs;
- post-merge paging across both language boundaries;
- helper failure, timeout, missing-thread, and unknown-boundary fallback;
- one real CodeLLDB integration run for each call direction.

## Completion Evidence

The reverse command must report:

```text
AC-BF-01 PASS
AC-BF-02 PASS
AC-BF-03 PASS
AC-BF-04 PASS
AC-BF-05 PASS
AC-RP-01 PASS
AC-RP-02 PASS
AC-RP-03 PASS
AC-RP-04 PASS
AC-RP-05 PASS
AC-RP-06 PASS
AC-RP-07 PASS
```

## Not Accepted as Completion

- merely stopping in a CPython runtime or signal function;
- placing all Python frames above or below every Rust frame;
- losing lower Rust embedder frames;
- spawning a new daemon worker after every timeout;
- two separate VS Code call-stack trees;
- screenshots without DAP assertions;
- claiming Python breakpoint or evaluation support.
