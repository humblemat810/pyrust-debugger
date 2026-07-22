# ADR 0010: Dual-Debug-Engine Coordinator

## Status

Accepted and implemented with ADR 0011's reversible per-frame engine leases.

## Context

CodeLLDB and debugpy each provide correct language-specific debugging, but
neither can safely own the same stopped process at the same time. A compound
VS Code launch exposes two unrelated call stacks and allows conflicting
continue or step commands.

PyRust needs one VS Code-facing DAP session that can present Python and Rust
frames while routing every operation to the engine that owns the selected
thread and frame.

## Decision

PyRust is the sole VS Code-facing DAP adapter and coordinates:

```text
VS Code
  -> PyRust coordinator
       -> CodeLLDB: Rust-owned stops and native frames
       -> debugpy: Python-owned stops and live Python frames
       -> CPython reader: routing-frame discovery and explicit legacy fallback
```

Every exposed frame belongs to exactly one route:

| Frame route | Source of truth | Operations |
| --- | --- | --- |
| Native CodeLLDB frame | Downstream native DAP ID | Rust scopes, variables, expressions, disassembly, native stepping |
| Live debugpy frame | Virtual PyRust ID -> PID, debugpy thread ID, debugpy frame ID | Python scopes, variables, imports, calls, expressions, and Python stepping |
| Python routing frame at Rust stop | Stop-epoch-scoped synthetic PyRust ID | Transfers to a refreshed live debugpy frame before interaction |
| Rust lease frame at Python stop | Process/thread-scoped PyRust ID | Reacquires CodeLLDB and resolves a fresh native frame ID |

Virtual Python and child-native IDs are process-scoped. Synthetic IDs are
stop-epoch-scoped and cannot be reused after continuation. Native CodeLLDB
frame IDs always remain native.

Execution commands route by thread ownership:

- CodeLLDB owns native `continue`, `next`, `stepIn`, `stepOut`, and `pause`.
- debugpy owns the same commands for a virtual Python thread.
- debugpy resume and step requests are tracked asynchronously. CodeLLDB may
  stop at a Rust breakpoint before debugpy can answer the request, so PyRust
  must not block the native stop while waiting for that answer.
- debugpy's `continued` event releases the Python stop lease; the next
  CodeLLDB `stopped` event acquires the native lease.

Restart is also coordinated. PyRust caches the transformed CodeLLDB launch
arguments, retires the old debugpy process routes, and supplies those cached
arguments in the downstream DAP `restart` request. Forwarding VS Code's empty
restart arguments directly is not sufficient for the supported CodeLLDB
version.

VS Code may omit `frameId` from Debug Console evaluation. PyRust records the
most recent built-in Call Stack frame for which VS Code requested `scopes`;
that stop-scoped selection is used only when `frameId` is absent. An explicit
`frameId` always takes precedence.

## Consequences

Positive:

- selecting a live Python frame routes the Debug Console and variables to
  debugpy;
- selecting a Rust frame routes them to CodeLLDB;
- selecting a Python routing frame at a Rust stop transfers to debugpy before
  variables, evaluation, or assignment are served;
- live Python and Rust owner frames support DAP variable assignment through
  their owning engines;
- Python stepping works at a Python-owned stop without switching VS Code
  sessions;
- Step Into from a live Python call site can hand off to a Rust breakpoint in
  the called native function;
- restart preserves the Python bootstrap and returns to live debugpy stops;
- breakpoint edits propagate to attached debugpy processes as well as future
  processes;
- process and thread identity remains PID-scoped across both engines.

Limitations:

- `pyrustPythonDebug: false` explicitly selects the legacy snapshot fallback
  and does not satisfy ADR 0011;
- Python-to-Rust Step Into recognizes a conservative direct call whose target
  name begins with `rust_`, installs a temporary CodeLLDB function breakpoint,
  preserves user function breakpoints, and restores them at the next stop;
- arbitrary Python expressions, aliases, dynamically selected callables, and
  native targets without a direct `rust_` call name are not yet inferred;
- Rust-to-Python cross-language stepping still requires continuing to a
  configured Python breakpoint;
- after a Rust-owned stop transfers an outer Python frame to debugpy, live
  evaluation and assignment are supported and `stepIn` returns to CodeLLDB;
  `next` and `stepOut` at that suspended-inside-Rust boundary are rejected
  until a coordinated run-to-line transaction is implemented;
- Process Tree selection is supplemental. The built-in Call Stack is the
  authoritative VS Code selection for Debug Console routing.

## Acceptance

`./scripts/accept-debugpy-slice.sh` verifies:

- full Python evaluation at Python-owned stops;
- Python `stepIn` routing through debugpy;
- automatic direct-call Python-to-Rust Step Into;
- Python -> Rust and Rust -> Python -> Rust handoffs;
- Rust-outer restart with live debugpy restored;
- Rust-stop to live-debugpy frame transfer;
- Python-stop outer Rust frame evaluation and mutation through CodeLLDB;
- child-process Rust-stop to live-debugpy transfer;
- child-process handoff after a user breakpoint on the Python-to-Rust call line;
- dynamic callable handoff without native-name discovery;
- exact-thread handoff for Python-created and Rust-created workers;
- selected Python-frame `stepIn` back to the current CodeLLDB Rust frame;
- Python threads and child processes with virtualized IDs.
