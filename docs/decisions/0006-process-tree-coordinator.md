# ADR 0006: Coordinate a Python/Rust Process Tree

## Status

Accepted and in progress.

## Date

2026-07-20.

## Context

ADR 0005 added read-only Python locals for one stopped process. The original
prototype still stored one implicit process ID and assumed a selected CodeLLDB
thread could be treated as that process. That is invalid for worker threads
and cannot represent a multiprocessing process tree.

The project must support:

```text
Python threads -> Rust
Rust threads -> embedded Python -> Rust callback
Python and Rust child processes
```

It must retain one VS Code-facing `pyrust` DAP session and prevent CodeLLDB and
debugpy from independently resuming the same process.

## Decision

Make PyRust a process-tree coordinator with one shared coordinator registry.
Every process is represented by:

```text
process ID
parent process ID, when known
native adapter connection state
Python adapter connection state, when available
native OS thread -> Python debugger thread mapping
current execution-owner lease
```

Synthetic frame IDs remain proxy-owned and are scoped to the current stop
epoch and native thread. A future multi-transport DAP endpoint must inject the
same `ProxySessionState` and `ProcessCoordinator` into each process adapter.

### Execution Ownership

Only one engine can own a stopped process:

| Stop type | Owner | Python state source |
| --- | --- | --- |
| Rust/native breakpoint | CodeLLDB | CPython external reader |
| Python breakpoint | debugpy | debugpy |
| Continue / step | coordinator routes only to the active owner | n/a |

The coordinator must reject an operation from the non-owner rather than trying
to resume both adapters.

### debugpy Boundary

An empirical local probe attached debugpy to a CPython process, confirmed it
answered `threads`, then externally sent `SIGSTOP`. A subsequent debugpy
`threads` request timed out. Therefore debugpy cannot provide stack/locals
while CodeLLDB has frozen the process in Rust.

At native stops, PyRust will continue using CPython's read-only external
unwinder and memory reader. debugpy remains the candidate backend for
Python-owned breakpoints, Python stepping, and rich Python object evaluation.

### Threading Implementation

The first completed coordinator slice supports two workers in one process:

1. Python `threading.Thread` workers entering Rust;
2. Rust `std::thread` workers entering embedded Python and calling Rust.

The proxy records CodeLLDB's launched process ID, maps the native `threads`
response to that process, and uses Linux `/proc/<tid>/status` to recover the
thread-group leader when an adapter omits a process event.

### Async Implementation

The coordinator supports native stops reached from:

1. named Python `asyncio.Task` coroutines calling Rust;
2. Rust `async fn` futures calling embedded Python `async def` code and then
   Rust callbacks.

At a native stop, CPython's remote reader identifies the active coroutine
frames on the selected OS thread. This is intentionally not an `asyncio` task
enumerator or await-graph debugger: suspended sibling tasks remain outside the
native-stop stack until a future Python-owned debugpy slice is added.

### Multiprocessing Implementation

Multiprocessing requires a separate native debug transport for each child
process. A single CodeLLDB process can follow either parent or child at a fork
boundary; it cannot be treated as a general process-tree debugger.

The implemented first process-tree release:

1. explicitly registers child PID and parent PID;
2. starts one CodeLLDB transport per registered child;
3. merges frames only from the selected `(process, thread)` pair;
4. virtualizes child frame and variable IDs before returning them to VS Code;
5. launches the parent directly in child-only mode so CodeLLDB owns only
   children, not the parent process tree;
6. supports a Python `spawn` parent and a Rust parent launching Python workers.

A future debugpy server per child remains necessary for Python-owned
breakpoints, stepping, and rich Python object evaluation.

### Observed Child-Follow Limitation

With `target.process.follow-fork-mode child`, CodeLLDB switched to one spawned
Python child and reported its `exec` stop. It did not expose a complete child
process tree or automatically restore the Rust breakpoint after that exec.
This confirms the coordinator must own explicit child registration and
per-child native transport startup rather than relying on one follow-fork
CodeLLDB session.

The original empty-stack result was a DAP request bug: the coordinator sent
`stackTrace` with `levels: 0`, which CodeLLDB interpreted as zero requested
frames. Omitting that field when the client has not paged the stack returns the
complete child native stack. The final child-only launch design also avoids
debugging the parent with CodeLLDB, so each child transport has unambiguous
native execution ownership.

## Acceptance

The detailed contract is
[Process-Tree Coordinator Acceptance](../acceptance/process-tree-coordinator.md).

## Explicit Non-Goals

- unrestricted attach to arbitrary descendant processes;
- distributed or multi-host process trees;
- free-threaded CPython and subinterpreters;
- Python breakpoints at a CodeLLDB-owned native stop;
- fork-based multiprocessing support in the initial process-tree release.
