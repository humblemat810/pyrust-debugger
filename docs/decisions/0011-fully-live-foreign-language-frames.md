# ADR 0011: Fully Live Foreign-Language Frames

## Status

Accepted and implemented for the supported CPython 3.14 / PyO3 Linux
fixtures, including direct and child-process sessions.

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
3. Moving between frames must not terminate the debug session. If the other
   engine cannot service the same physical stop, PyRust must perform an
   explicit, hidden ownership transfer and refresh the selected frame before
   serving debugger operations.
4. A snapshot, cached value, bounded expression evaluator, source-only frame,
   or second unrelated VS Code session does not satisfy the invariant.

The coordinator implements the invariant with reversible execution leases:

| Starting owner | Selected foreign frame | Ownership transfer |
| --- | --- | --- |
| debugpy | outer Rust frame | hidden CodeLLDB pause, native frame resolution, CodeLLDB scopes/evaluate/assignment |
| CodeLLDB | outer Python frame | queue a CPython 3.14 remote-debug script on the selected native TID, release native execution, refresh as a real debugpy frame |

Returning from a leased Rust frame to Python releases CodeLLDB and restores
the still-held debugpy stop. The reverse transfer uses CPython 3.14's exported
debug offsets and documented remote-debug control fields, not source
breakpoints or callable-name discovery.

## Decision

Treat this invariant as the completion criterion for the mixed debugger.
Snapshot evaluation remains available only when
`pyrustPythonDebug: false` is selected explicitly for legacy diagnostics.
debugpy is enabled by default.

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

Maintenance `stopped` and `continued` events are suppressed from VS Code.
Native frame IDs are resolved again whenever CodeLLDB reacquires the lease.

### Rust-Owned Stop With Outer Python Frames

Selecting a Python routing frame records its function, source path, PID, and
native TID. While CodeLLDB still owns the stop, PyRust:

1. Locates that exact `PyThreadState` through CPython's exported
   `_Py_DebugOffsets`.
2. Writes the handoff script path into the thread's
   `_PyRemoteDebuggerSupport`, sets `debugger_pending_call`, and raises the
   documented eval-breaker bit.
3. Waits for any preceding private debugpy resume request to settle, queues a
   process-wide debugpy pause, and releases the injected script rendezvous.
4. Releases CodeLLDB. The selected thread executes the script at its next
   Python safe point while its user frame is still live.
5. Resolves the requested function and source across debugpy's stopped thread
   inventory before emitting the VS Code `stopped` event. Internal injected
   frames are trimmed, while scopes, variables, evaluation, and assignment
   retain their real debugpy IDs.

The same mechanism applies to direct Python processes, child processes,
Python-created workers, Rust-created workers, and PyO3-embedded interpreters.
It does not depend on the native module name, callable spelling, source-line
layout, or a post-call Python line.

## Non-Goals

- claiming simultaneous CodeLLDB and debugpy ownership of one physical stop;
- writing immutable CPython object memory with `process_vm_writev`;
- treating cached values as live after the target resumes;
- calling a snapshot or custom bounded evaluator equivalent to debugpy.

## Acceptance

The slice is complete only when automated and manual tests prove:

1. Python-owned stop: Python and outer Rust frames are both visible.
2. Visible Rust frames support source, scopes, evaluate, and assignment when
   the selected frame has a writable local.
3. Rust-owned stop: Rust and outer Python frames are both visible.
4. Every visible Python frame reports fresh values on each request.
5. Python assignment changes the selected real frame and a subsequent read
   returns the new value.
6. Switching repeatedly between Python and Rust frames preserves one debug
   session and valid engine ownership.
7. Thread, process, async, restart, and cross-language breakpoint handoff
   acceptance remain green.

Automated evidence is `AC-DP-11` through `AC-DP-17`:

- direct and child-process Rust-stop to live-debugpy transfers;
- a prior Python breakpoint followed by Python -> Rust -> Python ownership;
- dynamically selected native callables with no Rust-like function name;
- Python-created worker threads; and
- Rust-created worker threads in an embedded interpreter.
