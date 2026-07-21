# ADR 0011: Fully Live Foreign-Language Frames

## Status

Proposed. The owner-frame mutation routes are implemented; same-stop live
foreign-language frames are not.

## Context

The required user experience is stronger than an interleaved visual stack:

1. At a Python-owned stop, selecting an outer Rust frame must expose current
   Rust variables and permit normal debugger assignment.
2. At a Rust-owned stop, selecting an outer Python frame must expose current
   Python objects and permit normal debugpy-style evaluation and assignment.
3. Moving between frames must not terminate, continue, or silently replace
   the physical stop.

The current coordinator guarantees live behavior only for frames owned by the
engine that owns the stop:

| Physical stop owner | Live frames | Foreign frames |
| --- | --- | --- |
| debugpy | Python | Rust is not yet merged into the live Python stack |
| CodeLLDB | Rust | Python is a read-only CPython memory snapshot |

PyRust now routes DAP `setVariable` and `setExpression` to debugpy for live
Python routes. Native Rust requests continue to pass through to CodeLLDB.
Acceptance verifies that both engines can change a local from `20` to `41`
and immediately read `41` while they own the stop.

This does not make a synthetic Python frame live. The CPython reader currently
copies bounded primitive values and intentionally never writes target memory
or executes target code.

## Decision

Treat fully live foreign-language frames as a separate architecture slice.
Do not describe the current snapshot evaluator as equivalent to debugpy.

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

debugpy cannot service requests while CodeLLDB has frozen the process. CPython
3.14 remote execution can bootstrap debugger code at interpreter safe points,
but it does not make a frozen interpreter execute debugpy commands.

Use an explicit in-target CPython evaluation bridge instead:

1. Resolve the stopped OS thread to its `PyThreadState`.
2. Resolve the selected interpreter-frame depth.
3. Acquire a real frame object and its write-through locals mapping.
4. Evaluate or assign through CPython APIs while the native debugger owns the
   stop and the selected thread safely holds the GIL.
5. Serialize the result into debugger-owned bounded storage.
6. Reject calls when no matching Python thread state exists, the GIL
   precondition is false, the frame changed, or re-entry is unsafe.

This bridge is not debugpy. It must be opt-in, version-gated to the supported
CPython build, bounded, and tested for reference-count correctness and
deadlocks.

## Non-Goals

- claiming simultaneous CodeLLDB and debugpy ownership of one physical stop;
- writing immutable CPython object memory with `process_vm_writev`;
- treating cached values as live after the target resumes;
- enabling arbitrary function calls before bridge safety is proven.

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

> Owner-language frames are live. Python frames reconstructed inside a
> Rust-owned stop are read-only snapshots.
