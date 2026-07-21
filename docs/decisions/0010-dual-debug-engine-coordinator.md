# ADR 0010: Dual-Debug-Engine Coordinator

## Status

Accepted and implemented for the supported CPython 3.14 / PyO3 fixtures.

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
       -> CPython reader: snapshot-only Python frames at Rust stops
```

Every exposed frame belongs to exactly one route:

| Frame route | Source of truth | Operations |
| --- | --- | --- |
| Native CodeLLDB frame | Downstream native DAP ID | Rust scopes, variables, expressions, disassembly, native stepping |
| Live debugpy frame | Virtual PyRust ID -> PID, debugpy thread ID, debugpy frame ID | Python scopes, variables, imports, calls, expressions, and Python stepping |
| Snapshot Python frame | Stop-epoch-scoped synthetic PyRust ID | Source navigation, bounded locals, safe snapshot expressions only |

Virtual Python and child-native IDs are process-scoped. Synthetic IDs are
stop-epoch-scoped and cannot be reused after continuation. Native CodeLLDB
frame IDs always remain native.

Execution commands route by thread ownership:

- CodeLLDB owns native `continue`, `next`, `stepIn`, `stepOut`, and `pause`.
- debugpy owns the same commands for a virtual Python thread.
- a Python resume immediately releases its debugpy stop lease; the next
  CodeLLDB `stopped` event acquires the native lease.

VS Code may omit `frameId` from Debug Console evaluation. PyRust records the
most recent built-in Call Stack frame for which VS Code requested `scopes`;
that stop-scoped selection is used only when `frameId` is absent. An explicit
`frameId` always takes precedence.

## Consequences

Positive:

- selecting a live Python frame routes the Debug Console and variables to
  debugpy;
- selecting a Rust frame routes them to CodeLLDB;
- Python stepping works at a Python-owned stop without switching VS Code
  sessions;
- breakpoint edits propagate to attached debugpy processes as well as future
  processes;
- process and thread identity remains PID-scoped across both engines.

Limitations:

- a snapshot Python frame nested inside a Rust-owned stop is not a live
  debugpy frame and cannot import, call functions, mutate state, or step;
- cross-language step-through remains unsupported: users continue from a
  Python stop to a later Rust stop or vice versa;
- Process Tree selection is supplemental. The built-in Call Stack is the
  authoritative VS Code selection for Debug Console routing.

## Acceptance

`./scripts/accept-debugpy-slice.sh` verifies:

- full Python evaluation at Python-owned stops;
- Python `stepIn` routing through debugpy;
- Python -> Rust and Rust -> Python -> Rust handoffs;
- Python threads and child processes with virtualized IDs.
