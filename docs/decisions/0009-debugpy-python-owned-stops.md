# ADR 0009: debugpy for Python-Owned Stops

## Status

Accepted and implemented.

## Date

2026-07-21.

## Context

ADR 0005 provides Python source frames, primitive locals, and a bounded
expression subset while CodeLLDB has stopped the target in Rust. That path is
safe because it reads the stopped process externally, but it cannot provide a
normal Python debugger experience:

- imports, function calls, object expansion, and mutation are unavailable;
- Python source breakpoints and stepping are unavailable;
- debugpy cannot answer requests while CodeLLDB externally freezes the same
  process.

The project also supports Python threads and explicitly registered child
processes. A single debugpy connection cannot represent all of those processes
without either leaking IDs or creating separate VS Code sessions.

## Decision

Add an opt-in `pyrustPythonDebug: true` launch property.

For each Python process, PyRust:

1. injects an opt-in `sitecustomize` bootstrap through `PYTHONPATH`;
2. starts a local debugpy server before user Python code runs;
3. registers that endpoint by PID in a private registry;
4. attaches one private direct-DAP transport to each registered endpoint;
5. virtualizes debugpy thread, frame, and variable IDs before exposing them to
   VS Code;
6. routes Python breakpoints, scopes, variables, evaluation, and continuation
   to debugpy while debugpy owns the stop.

For an embedded interpreter in the Rust-outer fixture, the Rust host imports
the bootstrap explicitly when the same opt-in environment is present.
The bootstrap waits only for a bounded interval. If the private debugpy attach
fails, the coordinator records that failure and the embedded interpreter
continues so CodeLLDB can still reach later Rust breakpoints.

## Ownership Rule

Each process has exactly one active execution owner:

| Stop | Owner | Python inspection |
| --- | --- | --- |
| Python source breakpoint | debugpy | Full debugpy scopes, calls, imports, objects, and evaluation |
| Rust source breakpoint | CodeLLDB | External CPython stack reader and bounded snapshot evaluation |

On a Python `continue`, PyRust acknowledges the successful debugpy request and
releases Python ownership immediately. This avoids treating a quick subsequent
Rust breakpoint as a debugpy timeout. The first native `stopped` event then
acquires CodeLLDB ownership.

PyRust does not ask debugpy for threads, frames, or variables while CodeLLDB
owns a native stop. The empirical native-stop probe shows those in-process
requests can hang.

This is a hard capability boundary for the current design. A Python frame
shown inside a Rust-owned stack is not a live debugpy frame. It is a
read-only, externally recovered snapshot and must not be presented as
supporting arbitrary Python execution. In particular, `import sys`, function
calls, object expansion, and mutation are unsupported at that stop. Users
must stop at a Python breakpoint for full Python expression evaluation.

## Concurrency

The registry is PID-scoped. Every spawned Python process receives its own
debugpy transport; every transport virtualizes its own thread/frame/variable
IDs. Python threads are queried from the selected process's debugpy transport.
The existing per-child CodeLLDB manager remains responsible for native child
stops.

This keeps process ownership explicit:

```text
process
  |- CodeLLDB transport for native stops, when configured
  `- debugpy transport for Python-owned stops, when enabled
```

## Acceptance

Run:

```bash
./scripts/accept-debugpy-slice.sh
```

The command proves:

- full Python imports and function calls at a Python breakpoint;
- Python -> Rust handoff to the original mixed native stack;
- multiple Python threads;
- spawned Python child processes;
- Rust -> embedded Python -> Rust callback handoff.

## Consequences

Positive:

- a selected Python breakpoint behaves like a normal debugpy frame;
- Python and Rust retain one VS Code-facing `pyrust` session;
- child processes use one debugpy transport per PID instead of separate user
  sessions;
- the native mixed-stack behavior remains available after continuation.

Negative:

- Python and Rust frames are not interleaved at a Python-owned stop; that stop
  displays debugpy's Python stack, then a later Rust stop displays the merged
  native stack;
- Python frames displayed at Rust-owned stops are snapshot-only and cannot
  execute arbitrary Python expressions;
- cross-language stepping remains unsupported;
- an arbitrary external Python embedder must import the opt-in bootstrap if it
  does not load `sitecustomize`;
- debugpy and CodeLLDB still cannot inspect the same process simultaneously
  while one owns a stop.
- a failed debugpy attach disables Python breakpoints for that process, but it
  falls back to native execution instead of permanently blocking the Rust host.
