# Research Report: VS Code and DAP

## Finding

DAP supports the data needed to present a merged stack, but it does not define
mixed-language stack composition. One adapter must construct and own the final
`stackTrace` response.

## DAP sequence at a stop

The normal client flow is:

```text
stopped event
  -> threads request
  -> stackTrace request
  -> scopes request for selected frame
  -> variables requests
```

This makes `stackTrace` the natural interception point.

## Required protocol behavior

### Process ID

The DAP `process` event may include `systemProcessId`.

Observed result: CodeLLDB did not emit this event in either launch fixture. The
proxy therefore needs a fallback:

- derive PID from launch/attach configuration;
- query LLDB through a console/custom request;
- use a launch wrapper that reports its child PID;
- fail to native-only mode.

The research probe successfully parsed `process status` output events, but a
structured CodeLLDB API or upstream process event is preferable.

### Stack frame IDs

DAP requires stack frame IDs to be unique across all threads. Synthetic Python
IDs must not collide with CodeLLDB IDs.

Recommended allocation:

- maintain a monotonically increasing stop epoch;
- map `(epoch, thread, python-index)` to a proxy-owned positive int32;
- discard mappings on continue, next, step-in, step-out, restart, or terminate.

### Paging

`stackTrace` supports `startFrame` and `levels`. The proxy cannot forward those
values unchanged and then insert Python frames, because the requested window is
defined over the final stack.

Correct algorithm:

1. request the complete native stack from CodeLLDB;
2. merge Python frames;
3. set `totalFrames` to the merged size;
4. apply `startFrame` and `levels`;
5. return the selected window.

This may cost more than CodeLLDB's delayed loading. The alpha should optimize
correctness first and add caching per stop epoch.

### Synthetic scopes

Selecting a Python frame causes a `scopes` request.

For the implemented fixed fixture, return one adapter-owned `Python Locals`
scope and serve bounded primitive snapshot variables. A safe expression subset
is evaluated in the proxy from that snapshot. The adapter must not forward a
synthetic Python frame ID to CodeLLDB or execute Python in the target process.
See [ADR 0005](../decisions/0005-read-only-python-frame-locals.md).

### Presentation

DAP stack frames support a `presentationHint` of `normal`, `label`, or
`subtle`. Python user frames should be normal. CPython machinery retained for
diagnostics can be subtle.

### Capabilities

The proxy should forward CodeLLDB capabilities except where synthetic frames
make them inaccurate. In particular:

- synthetic Python frames cannot be restarted;
- data breakpoints remain native;
- disassembly remains native;
- delayed stack loading is effectively implemented by the proxy only after
  merge caching exists.

## VS Code extension shape

The extension should contribute a new debugger type, for example `pyrust`, and
register its own `DebugAdapterDescriptorFactory`.

It should not attempt to register a descriptor for CodeLLDB's `lldb` type.
VS Code extensions are expected to own the debug types they contribute.

The extension can use a `DebugAdapterInlineImplementation` so the TypeScript
proxy runs in the extension host while it communicates with a child CodeLLDB
adapter.

## Adapter process discovery

CodeLLDB's VS Code extension does not currently expose a documented API that
returns a ready adapter connection. Its source starts the packaged
`adapter/codelldb` executable itself.

Practical alpha strategy:

1. declare CodeLLDB as an extension dependency;
2. locate it with `vscode.extensions.getExtension("vadimcn.vscode-lldb")`;
3. find the packaged adapter under its extension path;
4. allow `pyrust.adapterPath` as an override;
5. fall back to `lldb-dap` only for diagnostics, not as the supported Rust
   experience.

This relies on CodeLLDB's packaging layout, so it needs a startup self-test and
clear version diagnostics.

## Proxy state model

Minimum state:

```text
client sequence
downstream sequence
pending request maps
debuggee PID
stop epoch
run/stopped/terminated state
native thread -> OS thread mapping
synthetic frame map
cached merged stacks
```

Every DAP request forwarded downstream needs a remapped sequence number and a
pending entry so the response can be restored to the client's original
`request_seq`.

## Error policy

Mixed-stack augmentation is optional at runtime:

- CodeLLDB launch failure: fail the session.
- Remote Python unwind failure: log once per stop and return native stack.
- Unknown thread mapping: return native stack for that thread.
- Merge heuristic failure: return Python frames as a labeled block or native
  stack, depending on configuration.
- Helper timeout: kill helper, return native stack, preserve debug session.

## Observed CodeLLDB mapping

In both single-thread research fixtures:

```text
stoppedEvent.threadId == threads[0].id == OS tid == CPython thread_id
```

This is strong Linux feasibility evidence but not a protocol guarantee.
Multithread testing remains in the implementation plan.

## Sources

- DAP overview:
  https://microsoft.github.io/debug-adapter-protocol/overview.html
- DAP specification:
  https://microsoft.github.io/debug-adapter-protocol/specification.html
- VS Code debugger extension guide:
  https://code.visualstudio.com/api/extension-guides/debugger-extension
- VS Code API:
  https://code.visualstudio.com/api/references/vscode-api
- CodeLLDB adapter startup source:
  https://github.com/vadimcn/codelldb/blob/master/extension/main.ts
- CodeLLDB child-process helper:
  https://github.com/vadimcn/codelldb/blob/master/extension/novsc/adapter.ts
