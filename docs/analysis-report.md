# Overall Analysis Report

## Executive conclusion

The requested mixed Python/Rust stack is feasible with a constrained first
release.

The recommended first product slice is:

```text
Python 3.14 application -> PyO3 Rust extension
```

At a Rust breakpoint, one VS Code call stack should contain Rust user frames
followed by the active Python frames. CodeLLDB remains the only execution
controller. A small DAP proxy augments its `stackTrace` response with Python
frames read externally through CPython 3.14's remote unwinder.

This direction is lower cost than starting with Rust embedding Python because:

- the Python executable already owns interpreter startup;
- a PyO3 extension is a normal dynamically loaded native module;
- no embedded-Python path, home, or initialization policy is needed;
- remote unwinding and most DAP machinery are shared by both directions;
- repeated Python/native transitions need richer block markers, addressed after
  the one-boundary MVP.

The native-debugger-first alpha is a realistic one-engineer project. A full
mixed debugger with Python and Rust breakpoints, live object inspection,
arbitrary evaluation, and seamless stepping is a substantially larger product.

## Exact prior-art result

A public exact match was not found as of July 17, 2026: no indexed VS Code
extension or repository was found that interleaves Python and Rust frames into
one DAP stack while supporting both call directions.

The central merge operation is not unproven. `py-spy --native` successfully
merges Python and native frames, including repeated Python/native blocks, and
supports Python 3.14. Visual Studio also ships integrated Python/native
mixed-mode debugging on Windows.

This changes the implementation posture:

- do not invent frame-boundary rules without reference;
- use py-spy's eval-frame and Python 3.12+ shim-marker strategy as the baseline;
- focus original engineering on DAP identity, CodeLLDB integration, and VS Code
  behavior.

## What was proven

The repository has a CPython 3.14.6 environment and an executable probe using
`_remote_debugging.RemoteUnwinder`.

The probe has demonstrated:

- Python frames can be read from another CPython 3.14 process;
- results include OS thread IDs, function names, paths, and line numbers;
- frames are ordered from most recent to oldest;
- collection succeeds while the target is running;
- collection also succeeds while the target is stopped with `SIGSTOP`.

The last point is the critical feasibility result. Stack acquisition does not
require an in-process debug server or a thread in the target to execute.

The LLDB-owned stop gate passed with CodeLLDB 1.12.2's bundled LLDB. Remote
unwinding succeeded while CodeLLDB held both fixtures stopped under Linux
`ptrace_scope=1`.

## Product boundary

### Alpha promise

- launch a CPython 3.14 program under CodeLLDB;
- set and hit Rust breakpoints in a PyO3 extension;
- display one merged Python/Rust call stack;
- navigate to Python and Rust source;
- support multiple native threads;
- fall back to an ordinary native stack if Python unwinding fails.

### Not in alpha

- Python breakpoints;
- Python watches, object expansion, mutation, or arbitrary evaluation;
- Python code execution in the stopped target;
- automatic step from Python into an unknown Rust call;
- automatic step from Rust back to a Python line;
- Rust-outer/Python-inner launch support;
- Windows and macOS.

These exclusions are deliberate. They isolate the central user-visible result
from two-debugger coordination.

## Feasibility by direction

| Area | Python -> Rust | Rust -> Python |
| --- | --- | --- |
| Native launch | Launch Python executable | Launch Rust executable |
| Rust symbols | Loaded shared object | Main executable or library |
| Python runtime | Initialized normally | Embedder configures runtime |
| Test fixture | Small PyO3 module | PyO3 embedding or raw C API |
| Remote Python unwind | Same | Same after interpreter starts |
| Stack merge | Same | Same, with two Rust regions possible |
| Startup complexity | Low | Medium |
| Recommended order | First | Second |

The reverse scenario is not rejected. Once the merge engine can place Python
frames between native regions, Rust -> Python becomes primarily a fixture,
startup-detection, and packaging task.

The experiments exposed an additional product difference. Python -> Rust has a
natural stack-only stop: a Rust source breakpoint leaves all Python callers
active. Rust -> Python does not naturally stop on a user Python line under a
native-only debugger. A useful reverse-direction experience therefore needs a
Python breakpoint engine or deliberate instrumentation, making it more than a
stack-only follow-on.

## Implemented Follow-On

ADR 0005 now adds read-only primitive local snapshots for the fixed CPython
3.14.6 Linux fixtures. The proxy reads CPython memory through exported debug
offset metadata and evaluates only a documented AST subset against the frozen
snapshot. This improves Python-frame inspection without changing CodeLLDB's
sole ownership of execution.

## Architecture choice

### Chosen: native-first stack augmentation

```text
VS Code -> PyRust DAP proxy -> CodeLLDB -> debuggee
                 |
                 +-> CPython 3.14 remote stack helper
```

CodeLLDB owns launch, attach, breakpoints, stepping, threads, modules, Rust
variables, and process lifetime. The proxy intercepts only the requests and
events needed to construct synthetic Python frames.

Advantages:

- one component controls process execution;
- no deadlock between two adapters trying to stop or continue the process;
- no Python tracing overhead;
- failure degrades to CodeLLDB rather than ending the session;
- the first implementation surface is small.

Disadvantages:

- no Python debugging behavior in the first release;
- CodeLLDB's packaged adapter path is not a formal public integration API;
- CPython's `_remote_debugging` Python API is private;
- stack interleaving requires preserving CPython boundary markers.

### Deferred: debugpy plus CodeLLDB

A two-adapter bridge is appropriate only when Python breakpoints and stepping
become requirements. Existing Python/C++ mixed-debugger projects demonstrate
that it can be done, but their code also shows the necessary complexity:
separate sequence spaces, launch gating, PID handoff, breakpoint routing,
active-adapter routing, and coordinated shutdown.

### Validated alternative: LLDB scripted frame provider

The installed CodeLLDB 1.12.2 platform package reports LLDB
`22.1.4-codelldb` and includes real-thread scripted frame providers.

A local mock provider successfully added a Python-named frame with an `app.py`
source location. CodeLLDB returned it through its ordinary DAP `stackTrace`
response immediately before the untouched Rust frames. This proves that LLDB
can own mixed-stack presentation without a DAP response merge.

It is not the lowest-cost source of real Python frames yet:

- CodeLLDB embeds Python 3.12.7, which has no `_remote_debugging`;
- spawning the CPython 3.14 helper from LLDB failed in the tested process
  topology;
- a production provider would need to read CPython through
  `SBProcess.ReadMemory`, or query an ancestor service over IPC.

For a limited-resource first implementation, a CPython 3.14 DAP bridge remains
the shortest path because it already has permission to use `RemoteUnwinder`.
Scripted frames are the strongest later simplification if the target-memory
reader can be implemented economically.

## Core technical flow

1. VS Code sends `launch` to the `pyrust` adapter.
2. The proxy starts CodeLLDB and forwards initialization and launch messages.
3. The proxy obtains the debuggee PID. CodeLLDB did not emit a DAP `process`
   event in the research runs, so this requires an upstream/custom API, launch
   wrapper, or version-gated `process status` fallback.
5. CodeLLDB emits a native `stopped` event.
6. VS Code requests `threads`, then `stackTrace`.
7. The proxy asks CodeLLDB for the complete native stack.
8. The proxy runs the CPython 3.14 helper for the debuggee PID.
9. It matches the DAP thread to the OS thread ID.
10. It replaces or annotates CPython interpreter machinery with synthetic
    Python frames.
11. It applies DAP paging to the merged result and returns it to VS Code.

## Main unknowns

### Thread identity

CPython returns OS thread IDs. DAP thread IDs are adapter-defined and are not
guaranteed to be OS IDs. In both single-thread fixtures, CodeLLDB's stopped
thread ID and `threads[].id` exactly matched LLDB's OS TID and CPython's remote
unwinder ID. Multithread coverage remains required.

### LLDB stop ownership

Resolved for the tested Linux launch topology. The remote unwinder read both
fixtures while CodeLLDB was the active tracer and the process was stopped.

### Exact insertion point

CPython 3.11+ can execute many Python frames within relatively few C frames.
The merged stack therefore cannot rely on one interpreter frame per Python
frame. The first implementation will classify native frames by module and
symbol and insert the complete logical Python stack at the interpreter
boundary.

For arbitrary repeated Python/native transitions, the flattened CPython 3.14.6
`RemoteUnwinder` result is not sufficient by itself. `py-spy` solves this using
entry/shim markers. Phase 1 must determine how to expose equivalent markers or
use py-spy's merged result as an ordering oracle.

### Private CPython API

The 3.14.6 API returns a flat list of `ThreadInfo`. CPython's development branch
already groups threads by interpreter and adds constructor options. Our helper
must hide this shape behind a versioned adapter and fail clearly for unsupported
versions.

## Effort estimate

For one engineer familiar with TypeScript and debugging protocols:

| Deliverable | Expected effort |
| --- | --- |
| Native fixture and LLDB gate | 3-5 days |
| Transparent CodeLLDB proxy | 5-8 days |
| Stack collection and thread mapping | 3-5 days |
| Merge algorithm and VS Code presentation | 5-8 days |
| Integration tests and graceful fallback | 5-8 days |
| Stack-only Linux alpha | About 4-6 weeks |

These are planning estimates, not commitments. Python breakpoints, Python
locals, and cross-language stepping would likely add multiple months because
they require a second execution engine or a new Python debugger implementation.

## Recommendation

Fund one narrow milestone first:

> At a Rust breakpoint reached from Python 3.14, display correct Python and Rust
> source frames in one VS Code call-stack tree on Linux x86_64.

Do not begin debugpy integration until this milestone works reliably. If the
remote unwinder cannot operate under LLDB ownership or CodeLLDB thread IDs
cannot be mapped, stop and reassess before building the VS Code product shell.

## Sources

- CPython 3.14 remote debugging protocol:
  https://docs.python.org/3.14/howto/remote_debugging.html
- PEP 768:
  https://peps.python.org/pep-0768/
- CPython 3.14.6 remote unwinder source:
  https://github.com/python/cpython/blob/v3.14.6/Modules/_remote_debugging_module.c
- DAP specification:
  https://microsoft.github.io/debug-adapter-protocol/specification.html
- CodeLLDB source and manual:
  https://github.com/vadimcn/codelldb
- Python/C++ mixed DAP bridge used as prior art:
  https://github.com/bowen-xu/python-cpp-debugger-ext
- Exact prior-art search:
  research/exact-prior-art-search.md
- py-spy native stack merge:
  https://github.com/benfred/py-spy/blob/master/src/native_stack_trace.rs
- Local fixture evidence:
  research/fixture-results.md
