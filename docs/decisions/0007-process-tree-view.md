# ADR 0007: Show a Coordinator-Owned Process Tree

## Status

Accepted.

## Date

2026-07-20.

## Context

The standard DAP `threads` response is a flat collection. It does not provide
a portable parent-thread or parent-process tree that VS Code can render as an
indented hierarchy.

PyRust already knows the actual relationships:

```text
parent process -> child process -> native thread
```

It must not invent thread IDs merely to force nesting in VS Code's built-in
Call Stack.

## Decision

Keep normal DAP `threads` and Call Stack behavior unchanged. Add a custom VS
Code Debug-sidebar tree named **PyRust Process Tree**.

The adapter exposes a read-only `pyrust/processTree` custom request from
`ProcessCoordinator` state. The extension renders:

```text
parent process
  attached thread
    mixed Rust/Python frame
  child process
    attached thread
      mixed Rust/Python frame
```

Only structural relationships are indented:

- process -> child process;
- process -> attached native thread.

Sibling threads, sibling child processes, and `asyncio` tasks that merely
context-switch remain siblings. Rust/Python caller/callee nesting stays in the
standard Call Stack, not this tree.

Selecting a thread invokes **PyRust: Focus Process-Tree Thread**, requests that
thread's DAP `stackTrace`, and opens its top source frame. It does not alter
normal Call Stack selection. Expanding a stopped thread lazily renders its
mixed stack beneath that thread in the custom tree.

## Consequences

- The tree accurately represents the coordinator's process ownership model.
- Standard VS Code Call Stack remains compatible with CodeLLDB and DAP.
- A child-only parent launcher is displayed as a process node but has no
  selectable native thread because PyRust intentionally does not attach
  CodeLLDB to it. No fake thread is created.
- Full Python async task enumeration and await-graph visualization remain out
  of scope until a Python-owned debug backend is added.
