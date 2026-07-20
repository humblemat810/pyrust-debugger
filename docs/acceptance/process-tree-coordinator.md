# Process-Tree Coordinator Acceptance

## Purpose

This contract expands the fixed fixture debugger from one implicit process and
thread to explicit process/thread ownership.

## Completed Thread Criteria

### AC-MT-01: Python Worker Identity

Two `threading.Thread` workers must reach `rust_inner` with distinct CodeLLDB
thread IDs.

### AC-MT-02: Python Worker Stack

Each selected native worker must show:

```text
rust_inner
rust_outer
python_worker
```

### AC-MT-03: Python Worker Locals

The two stops must expose exactly these independent local snapshots:

```text
worker-A: label = "worker-A", value = 20, value + 1 = 21
worker-B: label = "worker-B", value = 40, value + 1 = 41
```

### AC-MT-04: Worker Epoch Isolation

After continuing worker A, its synthetic Python frame must be stale before
worker B's stop is inspected.

### AC-RT-01 through AC-RT-04: Rust Worker Equivalents

The same assertions apply when two Rust `std::thread` workers enter embedded
Python and stop at `rust_callback`.

## Completed Multiprocess Criteria

### AC-MP-01: Child Registration

The coordinator records a child PID, parent PID, and native transport identity
without overwriting the parent process session.

### AC-MP-02: Process-Scoped Thread Identity

Thread and synthetic frame identity is unique across `(process ID, thread ID)`.

### AC-MP-03: Child Mixed Stack

A selected child process can show an independently merged Python/Rust stack
without returning parent frames or parent locals.

### AC-MP-04: Ownership Arbitration

The coordinator routes continue only to the selected child transport and
invalidates that child's synthetic Python frames immediately.

### AC-MP-05: Lifecycle Cleanup

Child exit removes its transport, stop lease, and synthetic frame state while
leaving the parent session usable.

### AC-RMP-01 through AC-RMP-05: Rust Parent Equivalents

The same process identity, mixed-stack, expression, resume, and cleanup
assertions apply when a Rust parent starts two Python worker processes.

## Commands

```bash
./scripts/accept-thread-slice.sh
./scripts/accept-rust-thread-slice.sh
./scripts/accept-multiprocess-slice.sh
./scripts/accept-rust-multiprocess-slice.sh
PYTHONPATH=prototype/python .venv/bin/python -m unittest prototype.adapter.tests.test_coordinator -v
```

`AC-MP-01` through `AC-MP-05` pass for a Python parent using `spawn`.
`AC-RMP-01` through `AC-RMP-05` pass for a Rust parent launching Python child
workers. Both paths show Rust and Python frames, independent Python and Rust
expressions, child identity isolation, stale-frame invalidation, and cleanup.
