# Async Coordinator Acceptance

## Scope

This contract verifies native-breakpoint debugging across:

1. Python `asyncio` tasks calling Rust;
2. Rust `async fn` futures calling embedded Python `async def`, which calls
   back into Rust.

CodeLLDB owns the native breakpoint. The CPython 3.14 reader supplies the
currently executing Python coroutine frames, locals, and safe expressions.

## Python Async Criteria

### AC-AT-01: Shared OS Thread

Two named `asyncio.Task` instances stop at `rust_inner` on the same CodeLLDB
thread ID.

### AC-AT-02: Active Coroutine Stack

Each stop contains:

```text
rust_inner
rust_outer
async_worker
```

### AC-AT-03: Task-Local Snapshot

The selected coroutine exposes independent locals:

```text
async-A: label = "async-A", value = 20, task_name = "async-A"
async-B: label = "async-B", value = 40, task_name = "async-B"
```

`value + 1` is evaluated from the selected snapshot.

### AC-AT-04: Epoch Isolation

Continuing the first task invalidates its synthetic Python frame before the
second task is inspected.

## Rust Async Criteria

### AC-RA-01 through AC-RA-04

Two Rust futures cross:

```text
async fn rust_outer
  -> Python async def python_outer
  -> Python async def python_inner
  -> rust_callback
```

The selected stop must show the Rust callback, a Rust async poll frame
containing `rust_outer`, active Python async frames, task-specific
`label`/`value` locals, safe expression evaluation, and stale-frame
invalidation after continue.

## Commands

```bash
./scripts/accept-async-slice.sh
./scripts/accept-rust-async-slice.sh
```

## Deliberate Limitation

At a native breakpoint, PyRust identifies the **currently executing**
coroutine on the stopped OS thread. It does not enumerate every suspended
`asyncio.Task`, reconstruct an await graph, or support async-aware Python
stepping. Those capabilities require a Python-owned debugger such as debugpy.
