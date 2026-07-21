# ADR 0011: Fully Live Foreign-Language Frames

## Status

Accepted product invariant; implementation incomplete.

The current dual-engine implementation does not satisfy this ADR because
Python frames at a CodeLLDB-owned stop are snapshots and Rust frames are not
yet exposed through CodeLLDB at a debugpy-owned stop.

## Context

The required user experience is stronger than an interleaved visual stack.
The following invariant applies to every frame PyRust displays:

> Regardless of which breakpoint or debugger stopped the process, selecting a
> stack frame must route source, scopes, variables, watches, evaluation,
> assignment, and stepping to the real debugger for that frame's language.

Therefore:

1. At a Python-owned stop, selecting an outer Rust frame must expose current
   Rust variables and permit normal CodeLLDB evaluation and assignment.
2. At a Rust-owned stop, selecting an outer Python frame must expose current
   Python objects and permit normal debugpy evaluation and assignment.
3. Moving between frames must not terminate, continue, or silently replace
   the physical stop.
4. A snapshot, cached value, bounded expression evaluator, source-only frame,
   or second unrelated VS Code session does not satisfy the invariant.

The current coordinator guarantees live behavior only for frames owned by the
engine that owns the stop and is therefore an interim implementation:

| Physical stop owner | Live frames | Foreign frames |
| --- | --- | --- |
| debugpy | Python | Rust is not yet merged into the live Python stack |
| CodeLLDB | Rust | Python is a read-only CPython memory snapshot |

PyRust routes DAP `setVariable` and `setExpression` to debugpy for live Python
routes. Native Rust requests pass through to CodeLLDB. This verifies owner
routes only; it is not acceptance of the product invariant.

This does not make a synthetic Python frame live. The CPython reader currently
copies bounded primitive values and intentionally never writes target memory
or executes target code.

## Decision

Treat this invariant as the completion criterion for the mixed debugger, not
as an optional follow-on feature. Do not describe the current snapshot
evaluator or owner-only routing as the final mixed-debugger behavior.

Implement the two directions independently.

### Python-Owned Stop With Outer Rust Frames

1. Preserve the debugpy suspension.
2. Internally pause CodeLLDB without forwarding its maintenance
   `stopped`/`continued` events to VS Code.
3. Capture and virtualize the native stack by process, thread, frame index,
   program counter, and stop generation.
4. Resume CodeLLDB so debugpy can continue servicing Python requests.
5. For Rust scopes, evaluate, or assignment, briefly reacquire the native
   pause, validate the frame identity, perform the CodeLLDB operation, and
   resume to the preserved debugpy suspension.

This path must prove that the debugpy stop remains stable across repeated
native maintenance pauses.

### Rust-Owned Stop With Outer Python Frames

The final implementation must use debugpy for the selected Python frame.
An LLDB-side snapshot reader or custom Python expression evaluator does not
satisfy the invariant.

The architecture investigation must choose and prove one of these paths:

1. Modify or extend debugpy so its adapter can inspect and mutate a CPython
   frame while the target is under a native debugger stop.
2. Implement a reversible stop-ownership transfer that preserves the exact
   process, thread, frame, and variable state while debugpy becomes the active
   engine.
3. Replace the independent-engine design with a backend that provides true
   Python and Rust language services over one physical stop, while retaining
   debugpy-equivalent Python behavior.

If none can preserve the same physical stop and real language-debugger
behavior, the invariant is infeasible with the selected CodeLLDB/debugpy
pair and the architecture decision must be revisited explicitly.

## Non-Goals

- claiming simultaneous CodeLLDB and debugpy ownership of one physical stop;
- writing immutable CPython object memory with `process_vm_writev`;
- treating cached values as live after the target resumes;
- calling a snapshot or custom bounded evaluator equivalent to debugpy.

## Acceptance

The slice is complete only when automated and manual tests prove:

1. Python-owned stop: Python and outer Rust frames are both visible.
2. Every visible Rust frame supports source, scopes, evaluate, and assignment.
3. Rust-owned stop: Rust and outer Python frames are both visible.
4. Every visible Python frame reports fresh values on each request.
5. Python assignment changes the selected real frame and a subsequent read
   returns the new value.
6. Switching repeatedly between Python and Rust frames preserves one stop.
7. Thread, process, async, restart, and cross-language breakpoint handoff
   acceptance remain green.

Until all seven pass, the product documentation must say:

> The current prototype does not yet meet the per-frame real-debugger
> invariant. Owner-language frames are live; foreign-language frames are
> incomplete.
