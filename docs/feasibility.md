# Mixed Python/Rust Stack Feasibility

## Decision

Build the first proof of concept for **outer Python, inner Rust**:

```text
app.py -> PyO3 extension -> Rust function
```

Use CodeLLDB as the process-controlling debugger and place a small Debug Adapter
Protocol (DAP) proxy between VS Code and CodeLLDB. The proxy augments CodeLLDB's
`stackTrace` responses with Python frames read through CPython 3.14's remote
unwinding support.

Do not run debugpy and CodeLLDB as independent VS Code sessions for the MVP.
That produces two call-stack trees and creates stop/continue ownership races;
it does not produce one interleaved stack.

## Comparison

| Concern | Python -> Rust | Rust -> Python |
| --- | --- | --- |
| Process launch | Launch `python` in LLDB | Launch Rust binary in LLDB |
| Rust symbols | Shared library loads after startup | Present in main executable |
| Python startup hook | `sitecustomize` or wrapper module | Embedder must invoke bootstrap |
| Test fixture | Small PyO3 module | Rust binary plus CPython embedding setup |
| Stack collection | Same external reader | Same external reader |
| Packaging risk | Python and extension packaging | Python runtime discovery and embedding |
| First-spike cost | Lower | Higher |

Both directions are technically feasible for a stack-only debugger. The
Python-first direction removes unrelated embedding work from the critical path.

Executable fixtures confirmed remote stack collection in both directions.
However, only Python -> Rust has a natural native breakpoint that leaves the
other language's user frames active. Rust -> Python needs a Python stop
mechanism before it becomes a useful user-facing debugger scenario.

## Proposed architecture

```text
VS Code
   |
   | DAP
   v
PyRust DAP proxy
   |
   | DAP
   v
CodeLLDB / LLDB
   |
   | controls process and Rust stack
   v
CPython 3.14 process + Rust extension
   ^
   |
   +-- proxy helper reads exported debug offsets and process memory
```

### Why a DAP proxy

The DAP `stackTrace` response is owned by one debug adapter. A proxy can preserve
CodeLLDB behavior for native debugging and only intercept the small set of
messages needed for mixed frames:

- `threads`: retain CodeLLDB thread IDs and native thread metadata;
- `stackTrace`: fetch native frames, remotely unwind the matching Python
  thread, and merge;
- `scopes`: initially return an empty scope for synthetic Python frames;
- all other requests and events: pass through unchanged.

Synthetic frame IDs must occupy a proxy-owned range and remain stable for one
stop epoch. Paging (`startFrame` and `levels`) must be applied after merging,
not independently to the native and Python frame lists.

### Why CPython 3.14 changes the design

CPython 3.14 places interpreter debugger metadata in a discoverable binary
section and ships `_remote_debugging.RemoteUnwinder`. It reads thread states,
frames, code objects, source paths, and line numbers from another process.

The unwinder returns OS thread IDs and works while the target is externally
stopped. That removes the original need for an always-on `sys.monitoring`
recorder and shared-memory transport:

- no debuggee instrumentation;
- no event overhead;
- no stale snapshot window;
- attach works after Python has already started;
- both call directions use the same external reader.

The module name starts with an underscore, so its Python API is private. For the
MVP, pinning both helper and debuggee to CPython 3.14 is an acceptable tradeoff.
A production version can either vendor the relevant CPython unwinding code or
consume the exported debug-offset metadata directly.

`sys.remote_exec` is useful for later Python breakpoint support, but stack
collection does not require code execution in the target. This distinction is
important because remote execution waits for a safe Python evaluation point,
while LLDB may have stopped the process inside Rust.

## Stack merge heuristic

At a Rust breakpoint reached from Python, a typical native stack contains:

```text
Rust user frames
PyO3/native trampoline frames
CPython call machinery
CPython evaluation loop
process startup frames
```

The MVP should:

1. keep Rust user frames at the top;
2. insert active Python frames at the first Python/native boundary;
3. mark or hide CPython interpreter internals by default;
4. retain lower native startup frames in a collapsed/subtle form.

The first implementation can identify the boundary by module and symbol-name
patterns (`libpython`, `_PyEval_*`, `PyObject_*`, and PyO3 trampolines). The
proxy should expose a configuration switch to show the unfiltered native stack
when the heuristic is wrong.

For Rust -> Python, the same algorithm inserts Python frames between a Rust
callback/extension segment and the outer Rust embedder segment. This is why the
reverse direction is a follow-on, not a new architecture.

## Scope by capability

### Feasible in MVP

- one interleaved visual stack at native breakpoints;
- source navigation for Python and Rust frames;
- multiple Python threads, mapped by native OS thread ID;
- exceptions and generators as represented by CPython's remote unwinder;
- attach after interpreter startup without a bootstrap hook.

### Feasible later

- Python locals by publishing bounded, opt-in snapshots;
- Python breakpoints by adding a Python tracing/monitoring breakpoint engine;
- language-aware evaluate routing based on selected synthetic frame;
- cross-boundary step-in using coordinated temporary breakpoints.

### High risk

- transparent support outside CPython 3.14;
- free-threaded CPython without dedicated compatibility testing;
- subinterpreters with overlapping thread activity;
- exact interleaving when several Python/native transitions exist on one
  native stack;
- Windows parity, where debugger backend and symbol behavior differ.

## Rejected starting points

### Two independent debug sessions

VS Code compound configurations can start debugpy and CodeLLDB together, but
each adapter owns a separate stack and execution state. A native stop can also
prevent debugpy's in-process components from answering.

### Maintaining a shadow stack

`sys.monitoring` can maintain a portable logical stack and publish it through
shared memory. That remains a fallback for future Python versions, but CPython
3.14's external unwinder provides fresher data with no debuggee overhead.

### LLDB scripted frame providers first

New LLDB scripted-frame support can augment real thread stacks directly, but it
is newer than the LLDB version currently bundled by CodeLLDB and is still
evolving. It may eventually remove the DAP proxy, but it should not gate the
first prototype.

## Platform assumptions

The initial compatibility contract should be narrow:

- Linux x86_64;
- CPython 3.14;
- Rust debug builds with DWARF;
- PyO3 extension modules;
- current VS Code and CodeLLDB;
- one CPython interpreter per process.

Expansion should follow measured demand rather than being designed up front.
