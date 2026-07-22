# Architecture

## Context

The product must show Python and Rust frames in one VS Code call stack. The
initial supported scenario is a CPython 3.14 application calling a Rust
extension.

## Components

```text
+-------------------+
| VS Code debug UI  |
+---------+---------+
          | DAP
+---------v---------+
| PyRust DAP proxy  |
|                   |
| - request mapping |
| - stop epochs     |
| - frame merge     |
| - fallback policy |
+----+----------+---+
     | DAP      | helper invocation
+----v-----+  +-v------------------+
| CodeLLDB |  | CPython 3.14 helper|
| / LLDB   |  | RemoteUnwinder     |
+----+-----+  +---------+----------+
     |                  |
     +--------+---------+
              v
   CPython process + Rust extension
```

By default, the proxy also connects directly to one debugpy endpoint per
registered Python process. debugpy owns Python
source-breakpoint stops; CodeLLDB owns Rust stops. The proxy must not query
debugpy while CodeLLDB has externally stopped the target.

Foreign frames use reversible leases. A Python routing frame inserted into a
CodeLLDB stop queues CPython 3.14's documented remote-debug script on the
selected native TID and refreshes as a real debugpy frame before interaction.
At a debugpy stop, PyRust performs a hidden CodeLLDB maintenance pause to
discover outer Rust frames; selecting one reacquires CodeLLDB and resolves a
fresh native frame ID. Returning to Python releases the native lease.

## Validated alternative

CodeLLDB's bundled LLDB 22.1.4 supports `ScriptedFrameProvider`. A research
provider prepended a PC-less frame with a Python function name and source line,
and CodeLLDB surfaced it through normal DAP with the native Rust frames
unchanged.

That architecture would be:

```text
VS Code -> CodeLLDB -> LLDB scripted frame provider -> debuggee memory
```

It avoids DAP frame IDs, paging, and `scopes` interception. However, CodeLLDB's
embedded Python is 3.12.7 and cannot call CPython 3.14's private
`RemoteUnwinder`. The provider would need:

- a CPython 3.14 debug-offset reader over `SBProcess.ReadMemory`; or
- IPC to a helper process that is allowed to inspect the target.

The first prototype therefore keeps the DAP bridge architecture. The scripted
provider is a planned architecture checkpoint, not an unavailable feature.

## Process model

The VS Code extension host runs the proxy. The proxy starts:

- CodeLLDB's adapter process;
- a short-lived or persistent CPython 3.14 helper.

CodeLLDB launches the debuggee. The research runs did not receive the optional
DAP `process` event, so PID acquisition must be explicit. Preferred options are
a structured CodeLLDB request or upstream process event, followed by a launch
wrapper. Parsing `process status` output is a spike-only fallback.

### Helper choice

Start with a short-lived helper command for isolation:

```bash
python3.14 -m pyrust_stack <pid>
```

Move to a persistent helper only if measurements show startup latency affects
stack expansion. A persistent helper reduces latency but adds lifecycle and
cache invalidation complexity.

## Proxy message routing

### Pass through

- `initialize`
- `launch` / `attach`
- native `setBreakpoints`
- `configurationDone`
- native `continue`, `next`, `stepIn`, `stepOut`, `pause`
- `modules`, `disassemble`, memory requests
- output, module, thread, exited, and terminated events

### Intercept

- `process`: record `systemProcessId`
- `stopped`: increment stop epoch and clear caches
- `continued`: clear synthetic frame mappings
- `threads`: enrich thread-to-OS-ID mapping if needed
- `stackTrace`: collect, merge, page, and cache
- `scopes`: recognize synthetic frame IDs
- `evaluate`: route live Python frame IDs to debugpy and Rust lease frames to
  CodeLLDB; a Python routing frame first transfers ownership to debugpy
- `continue`, `next`, `stepIn`, `stepOut`, `pause`: route ordinary virtual
  Python stops to debugpy and native threads to CodeLLDB
- `stepIn` from a Python frame transferred out of a Rust-owned stop: resume
  debugpy's handoff helper, suppress its internal stops, and expose the
  reacquired CodeLLDB Rust frame
- `next` and `stepOut` from a transferred Python frame: install a temporary
  debugpy breakpoint at the selected source-backed destination, suppress
  helper stops, then restore the user's breakpoint set
- stepping a selected Rust lease frame: return to its saved instruction on the
  resolved native TID, restore user instruction breakpoints, then forward the
  original `next`, `stepIn`, or `stepOut` to CodeLLDB

The coordinator's frame routing table is explicit:

| Exposed frame | Owner | Supported operations |
| --- | --- | --- |
| Native CodeLLDB ID | CodeLLDB | Rust variables, expressions, source/disassembly, native stepping |
| Virtual debugpy ID | debugpy | Python variables, imports, calls, expressions, and ordinary Python stepping |
| Stop-scoped Python routing ID | PyRust -> debugpy handoff | Activates a real debugpy stop before frame interaction |
| Rust lease ID | PyRust -> CodeLLDB lease | Reacquires CodeLLDB, resolves the native frame, and routes native operations |

A transferred Python frame that is physically suspended inside a Rust call
supports live debugpy evaluation and assignment. `stepIn` returns to the real
CodeLLDB Rust frame. `next` and `stepOut` use a temporary debugpy breakpoint
to land on a source-backed next statement in the selected frame or immediate
Python caller. If that destination is ambiguous or unavailable, PyRust rejects
the operation rather than exposing the injected handoff frame.

Threads created after debugpy startup are mapped lazily. At a real Python stop,
PyRust evaluates `_thread.get_native_id()` in the selected debugpy frame and
caches that debugpy-thread-to-OS-TID mapping before asking CodeLLDB to pause,
inspect, or step the native thread. The process leader is never used as a
fallback for an unknown worker TID.

Active coroutine frames use the same ownership rules as synchronous Python
frames. PyRust transfers only the coroutine currently executing on the stopped
OS thread; debugpy supplies live Python semantics, while retained Rust async
poll frames use native leases and CodeLLDB. Suspended tasks and futures are not
invented as process-tree children.

The built-in VS Code Call Stack is authoritative for frame selection. A custom
Process Tree can navigate source and display ownership, but it cannot set
VS Code's active DAP stack frame through a public API.

Because CodeLLDB omitted `process` in the research launches, the final routing
table must also include the selected structured PID-discovery mechanism.

## Internal data model

```typescript
type PythonFrame = {
  name: string;
  path: string;
  line: number;
};

type PythonThreadStack = {
  osThreadId: number;
  frames: PythonFrame[]; // newest first
};

type FrameKind =
  | "rust-user"
  | "python-user"
  | "bridge"
  | "python-runtime"
  | "native-runtime"
  | "unknown";

type StopState = {
  epoch: number;
  pid: number;
  mergedStacks: Map<number, DapStackFrame[]>;
  syntheticFrames: Map<number, PythonFrame>;
};
```

## Stack merge algorithm

Inputs:

- complete CodeLLDB native stack;
- Python logical stack for the matching OS thread;
- frame classifier configuration.

The baseline should follow py-spy's proven native merge strategy, adapted to
preserve CodeLLDB DAP frame identities.

Algorithm:

1. classify every native frame;
2. find the first contiguous Python-runtime/bridge region below top-level user
   Rust frames;
3. retain Rust user frames above the region;
4. insert Python logical frames, newest first;
5. retain lower Rust user frames if present;
6. omit runtime frames by default, or mark them subtle when configured;
7. preserve unknown frames rather than silently deleting them;
8. allocate synthetic IDs;
9. cache by stop epoch and DAP thread ID;
10. apply requested paging.

### Boundary markers

For one Python -> Rust boundary, CPython 3.14 `RemoteUnwinder` plus a native
eval-frame boundary is sufficient.

The implemented classifier inserts active Python frames at the first
recognized PyO3/CPython bridge below the stopped native user callees. It
recognizes generated PyO3 function/method trampolines and CPython
vectorcall/eval symbols. Application function names are deliberately ignored:
`AC-DP-28` proves the merge and live debugpy handoff with unrelated
`calculate_leaf`, `dispatch_payload`, and `handle_event` names.

For repeated transitions, use py-spy-style markers:

- CPython eval-frame native symbols identify insertion points;
- Python 3.11 entry flags terminate one logical Python block;
- Python 3.12+ shim frames terminate one block;
- ordinary native frames remain in native order.

The 3.14.6 private `RemoteUnwinder` API does not expose full block markers.
Consequently, the current classifier proves the active physical PyO3 boundary
but does not claim generalized ordering for arbitrary non-PyO3 FFI stacks,
multiple interpreters, or suspended coroutine/future graphs.

### Python -> Rust expected shape

```text
Rust user frame
Rust user frame
[bridge/runtime frames]
Python caller
Python caller
[native startup frames]
```

### Rust -> Python -> Rust expected shape

```text
Rust callback/extension frame
[bridge]
Python inner frame
Python inner caller
[embedding bridge]
Rust outer/embedder frame
```

The second shape is why lower user-native frames must not be dropped.

## Thread mapping

Preferred mapping order:

1. direct equality if CodeLLDB DAP IDs are OS IDs;
2. CodeLLDB/LLDB custom query for thread OS ID;
3. match selected thread using stop metadata and top native frame;
4. no match: native-only fallback.

Never guess among multiple Python threads.

## Failure modes

| Failure | Behavior |
| --- | --- |
| Target is not CPython 3.14 | Native-only stack and one diagnostic |
| Permission denied | Native-only stack with ptrace guidance |
| Unwinder timeout | Kill helper; native-only stack |
| Thread not found | Native-only stack for that thread |
| No Python frames active | Native-only stack |
| Boundary not found | Append labeled Python block or use configured fallback |
| CodeLLDB exits | End debug session |

## Configuration

Initial settings:

```json
{
  "type": "pyrust",
  "request": "launch",
  "program": "${workspaceFolder}/.venv/bin/python",
  "args": ["app.py"],
  "pythonHelper": "${workspaceFolder}/.venv/bin/python",
  "showInterpreterFrames": false,
  "mixedStackFallback": "native",
  "adapterPath": null
}
```

## Security

- Never execute expressions in the target for stack collection.
- Validate PID is the process reported by the downstream adapter.
- Use argument arrays, not shell command strings.
- Bound helper execution time and output size.
- Do not log environment secrets or full target memory.
- Treat source paths from the target as untrusted strings.

## Evolution path

1. Stack-only CodeLLDB proxy.
2. py-spy-aligned multi-boundary merge.
3. Evaluate migration to LLDB scripted frames with an `SBProcess` CPython
   reader.
4. Rust-outer/Python-inner fixture.
5. Python frame locals through bounded remote primitive reading. Implemented
   for the fixed CPython 3.14.6 Linux fixtures by ADR 0005.
6. Live two-engine frame ownership and stepping. Implemented for the supported
   CPython 3.14.6 / PyO3 Linux topology by ADR 0011.
7. Broader non-PyO3 bridge classification, multiple interpreters,
   free-threaded CPython, and suspended async graph presentation.
