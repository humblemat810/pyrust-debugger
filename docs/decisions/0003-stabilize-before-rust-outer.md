# ADR 0003: Stabilize Before the Rust-Outer Stack Proof

## Status

Accepted and implemented.

## Date

2026-07-17.

## Context

ADR 0002 produced a working, fixture-bound Python-to-Rust DAP proof. The next
direction is a Rust application embedding CPython:

```text
Rust application -> embedded Python -> Rust callback
```

The research fixture already proves that CodeLLDB can retain lower Rust frames
while CPython's remote unwinder reads active Python frames. It does not yet
provide a user Rust stop inside the active Python call path, and the current
merge policy recognizes only the Python-outer fixture.

The first slice also exposed a bounded but real resource defect. An in-process
CPython unwind that never returns is abandoned as a daemon thread. A later
`stackTrace` request can start another thread, so repeated timeouts can
accumulate abandoned workers until the proxy exits.

Rust-to-Python does not have a natural Python source breakpoint while CodeLLDB
is the only execution controller. Adding debugpy or a Python breakpoint engine
would turn this into a much larger two-debugger project.

## Decision

Execute the next work in two serial gates. Gate 1 must pass before reverse-
direction implementation begins.

### Gate 1: Stabilize the Existing Slice

Add a session-level circuit breaker around the default in-process unwinder:

1. permit at most one in-process unwind worker at a time;
2. if that worker times out, open the circuit for the rest of the DAP session;
3. return native CodeLLDB stacks immediately for later requests;
4. do not create replacement unwind threads after the circuit opens;
5. emit one concise timeout diagnostic for the session;
6. retain the daemon worker only so proxy shutdown cannot be blocked;
7. clear the circuit only by starting a new proxy process.

Python threads will not be forcefully terminated. CPython does not provide a
safe general mechanism for killing an arbitrary thread.

The external helper-command path remains separately killable by its subprocess
timeout and does not share this circuit.

Regression coverage must also prove:

- concurrent or repeated stack requests cannot create multiple in-process
  workers;
- a late worker result cannot allocate frames into a newer stop epoch;
- native frame IDs continue to take precedence over synthetic-ID history;
- repeated diagnostics are suppressed across stop epochs;
- the complete ADR 0002 acceptance command still passes.

This gate bounds the defect to at most one abandoned daemon thread per proxy
session. Eliminating even that one thread requires a later process-topology
decision in which a killable helper is an ancestor of CodeLLDB and the
debuggee, or the memory reader moves into LLDB.

### Gate 2: Prove Rust-Outer Stack Composition

Extend the existing `research/fixtures/rust_outer` program to create this
deterministic call shape:

```text
Rust main
  -> rust_outer
     -> embedded.py::python_outer
        -> embedded.py::python_inner
           -> rust_callback
```

CodeLLDB will stop at a Rust source breakpoint in `rust_callback`. This is
deliberate native instrumentation, not a Python breakpoint.

At that stop, the proxy must:

- preserve the top Rust callback frame and its native frame ID;
- insert the active Python frames in newest-first order;
- preserve lower Rust embedder frames such as `rust_outer` and `main`;
- keep lower native frame IDs usable through CodeLLDB;
- apply DAP paging only after the complete mixed stack is assembled;
- allocate Python synthetic IDs for the current stop epoch;
- fall back to the unmodified native stack when the Python stack or boundaries
  cannot be proven.

The merge implementation must separate fixture boundary policy from DAP
transport. It may use explicit, tested boundary rules for the two supported
fixtures. It must not claim arbitrary repeated Python/native transition
support until marker-based ordering is implemented.

## Required Stack Result

The reverse-direction acceptance stack must contain this user-frame
subsequence:

```text
rust_callback
python_inner
python_outer
rust_outer
main
```

An embedded-module frame and subtle runtime frames may also be present, but
they must not change the order of the required user frames.

## Execution Plan

1. Run `./scripts/accept-first-slice.sh` to establish the baseline.
2. Implement and unit-test the in-process unwinder circuit breaker.
3. Rerun the original acceptance command before changing merge behavior.
4. Add the Rust callback fixture and verify its raw CodeLLDB and CPython stack
   shapes.
5. Extract the fixture-specific boundary policy from the current merge method.
6. Add the Rust-outer merge policy and golden unit tests.
7. Add `./scripts/accept-reverse-slice.sh` for the real CodeLLDB proof.
8. Run both acceptance commands from clean proxy processes.
9. Perform an independent review focused on resource bounds, frame identity,
   stale epochs, and unsupported-topology fallback.

The detailed completion contract is
[Rust-Outer Stabilization Acceptance](../acceptance/rust-outer-stabilization.md).

## Estimated Work

For the current coding-agent workflow:

| Work | Expected agent execution |
| --- | --- |
| Circuit breaker and regressions | 1-2 hours |
| Reverse fixture and merge policy | 2-4 hours |
| Acceptance integration and review | 1-2 hours |
| Total | Roughly 4-8 hours |

These are planning ranges, not commitments. A conventional single-engineer
schedule should retain a two-to-four-day allowance for debugger and process-
topology surprises.

## Explicit Non-Goals

- Python source breakpoints;
- Python locals, watches, expression evaluation, or stepping;
- debugpy coordination;
- arbitrary Python/native/Python/native transition ordering;
- multithreaded or subprocess Rust servers;
- attach, restart, or hot-reload behavior;
- replacing the in-process reader with a killable helper topology;
- product-quality PID discovery;
- VS Code extension packaging;
- platforms or versions outside the ADR 0002 fixed environment.

## Consequences

Positive:

- repeated unwinder hangs have a strict per-session resource bound;
- the reverse direction reuses the existing execution controller and reader;
- lower Rust frames become an explicit tested invariant;
- success proves the stack-composition architecture in both call directions.

Negative:

- one permanently hung daemon thread may remain until proxy exit;
- the reverse proof stops in Rust, not on a Python source breakpoint;
- two fixture policies are not yet a general multi-boundary merge algorithm;
- the PID and helper-isolation shortcuts remain visible follow-up work.

## Completion

Implemented on 2026-07-17.

The final verification command:

```bash
./scripts/accept-reverse-slice.sh
```

passes `AC-BF-01` through `AC-BF-05` and `AC-RP-01` through `AC-RP-07`. The
command also runs `./scripts/accept-first-slice.sh` from a fresh proxy process.

The observed reverse user stack is:

```text
rust_callback
python_inner
python_outer
rust_outer
main
```

An independent review found and drove fixes for stale fallback across stop
epochs, optional embedded-module frames, and timeout diagnostics when a stop
changes. Its final pass reported no remaining findings.

## Follow-Up Decisions

Separate ADRs are required before implementing either:

1. a killable helper topology or an LLDB-resident CPython memory reader;
2. Python breakpoints and language-routed evaluation through debugpy or a
   dedicated Python execution engine.
