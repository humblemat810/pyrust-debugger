# ADR 0005: Read-Only Python Frame Locals

## Status

Accepted and implemented.

Superseded in part by ADR 0009 for Python-owned debugpy stops. This ADR remains
the active policy for Python frames observed while CodeLLDB owns a Rust stop.

## Date

2026-07-20.

## Context

ADR 0002 through ADR 0004 prove that the proxy can show CPython frames inside
one CodeLLDB Call Stack in both supported fixed fixtures:

```text
Python -> Rust
Rust -> embedded Python -> Rust callback
```

Those synthetic Python frames originally supported source navigation only.
VS Code could select them, but their scopes were empty and expression
evaluation was intentionally unavailable. The missing interaction made it hard
to validate that a selected Python frame represented useful Python state.

CPython 3.14's remote unwinder supplies function names and locations, but not
locals. Running Python code through `sys.remote_exec` is not safe or reliable
while CodeLLDB has stopped the process in Rust. Therefore this capability must
not introduce debugpy, a second debugger, or arbitrary target-process
execution.

## Decision

For the fixed Linux x86_64, CPython 3.14.6 environment, add a bounded,
read-only CPython memory reader and evaluate a small expression subset against
the captured snapshot.

The reader:

1. uses CPython's exported `.PyRuntime` debug-offset metadata instead of
   hard-coded private structure offsets;
2. reads target memory with Linux `process_vm_readv`;
3. matches CodeLLDB's stopped OS thread ID to CPython's thread state;
4. captures newest-first active Python frames;
5. exposes only `None`, booleans, integers, floats, strings, bytes, and typed
   placeholders for unsupported values;
6. enforces bounds on frames, local names, strings, and integer digits;
7. never writes memory or executes code in the debuggee.

For a current synthetic Python frame, the proxy returns one `Python Locals`
DAP scope. `variables` returns the captured local snapshot. `evaluate` parses
the expression locally and permits only:

```text
literal values
local names
+ - not
+ - * / // %
and or
== != < <= > >=
```

Calls, attributes, subscriptions, comprehensions, assignments, imports, and
all other AST forms are rejected. The result is a view of the snapshot taken
while processing `stackTrace`; it is not live Python execution.

If local capture fails or frame matching is uncertain, retain the synthetic
frame and report its local state as unavailable. Stack collection must still
fall back to native CodeLLDB frames under the existing unwinder failure rules.

## Acceptance

The detailed contract is
[Python Frame Locals Acceptance](../acceptance/python-frame-locals.md).
The existing Python-outer and Rust-outer DAP acceptance commands prove the
fixture behavior in both directions.

## Explicit Non-Goals

- Python breakpoints or debugpy;
- executing arbitrary Python expressions in the target process;
- object graphs, collection expansion, mutation, or assignment;
- Python stepping;
- arbitrary CPython versions, platforms, layouts, or free-threaded builds;
- generalized mixed-language boundary inference.

## Consequences

Positive:

- selecting a Python frame in VS Code now exposes meaningful fixture locals;
- expression results are deterministic and cannot mutate the paused target;
- both fixture directions use the same DAP frame/scope/evaluate flow;
- layout dependence is constrained by CPython's exported debug metadata and
  explicit version checks.

Negative:

- the capability is intentionally limited to primitive snapshots;
- locals can be unavailable when permission, timing, or layout checks fail;
- snapshot expressions do not provide full Python semantics;
- the reader is Linux and CPython 3.14.6 specific.
