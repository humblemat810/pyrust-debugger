# Research Report: CodeLLDB and LLDB

## Finding

CodeLLDB is the best execution engine for the first Rust-focused release. It
already supplies Rust launch behavior, breakpoints, symbols, variables,
formatters, stepping, module events, and VS Code integration. Reimplementing
those capabilities would dominate the project.

## Current baseline

At the researched revision:

- CodeLLDB release line: 1.12.x;
- installed platform package: 1.12.2;
- installed bundled LLDB: `22.1.4-codelldb`;
- license: MIT;
- supported focus: Rust, C++, and other native languages.

The repository's latest inspected commit was:

```text
2e71e6587af0b0a9839711b0921ec66709353ac4
```

## Integration options

### A. Spawn packaged CodeLLDB adapter

The CodeLLDB extension starts `adapter/codelldb` and supplies the bundled
`liblldb`. A mixed-debugger prototype in the ecosystem also locates and starts
that executable directly.

Benefits:

- smallest implementation;
- preserves CodeLLDB's Rust behavior;
- no maintained fork;
- MIT-compatible use.

Risks:

- executable location and invocation are not a documented stable API;
- platform packages may be downloaded after extension activation;
- adapter/liblldb environment setup must match CodeLLDB;
- upgrades require compatibility tests.

Recommendation: use this for alpha, with an explicit adapter-path override.

### B. Use upstream `lldb-dap`

Benefits:

- official LLVM DAP adapter;
- straightforward process boundary;
- fewer CodeLLDB packaging assumptions.

Risks:

- does not automatically reproduce CodeLLDB's Rust formatters and behavior;
- users must install a matching LLVM toolchain;
- the project inherits more Rust-specific setup work.

Recommendation: diagnostic backend only during the first phase.

### C. Fork CodeLLDB

Benefits:

- direct control over stack construction;
- no proxy paging or synthetic-ID translation;
- access to LLDB frame/module details.

Risks:

- larger Rust/TypeScript build and release surface;
- ongoing upstream merge cost;
- platform binary distribution;
- raises the minimum staffing requirement.

Recommendation: fallback only if the proxy cannot obtain thread identity or
complete native stacks reliably.

### D. LLDB scripted frame provider

Current LLDB documentation describes scripted providers that can augment or
replace real stack frames.

Local validation with the installed backend succeeded:

- `target frame-provider register` is available;
- the Python `ScriptedFrameProvider` API is present;
- providers can preserve original input frames;
- a PC-less synthetic frame can carry a Python name, path, and line;
- CodeLLDB includes that frame in its standard DAP stack response.

The remaining difficulty is stack acquisition, not presentation. CodeLLDB
embeds Python 3.12.7, while `_remote_debugging.RemoteUnwinder` is a CPython 3.14
facility. A provider must read target memory through LLDB or communicate with a
permitted external service.

Recommendation: keep as a validated architectural alternative. Use it if a
small CPython debug-offset reader over `SBProcess.ReadMemory` proves cheaper
than DAP stack merging.

## Launching Python under LLDB

For Python -> Rust, the debug target is the Python executable:

```text
program: /path/to/python3.14
args: [app.py]
```

The PyO3 extension is loaded dynamically. LLDB supports pending breakpoints and
module-load resolution, but the fixture must verify that Rust symbols are
preserved:

- debug profile;
- no stripping;
- matching source paths;
- deterministic extension path;
- no aggressive optimization for the first tests.

## Native frame classification

The merge engine needs a boundary classifier, not a Python-frame unwinder.

Candidate signals:

- module path contains `libpython3.14`;
- symbols begin with `_Py`, `Py`, or `cfunction_`;
- PyO3 trampoline modules/symbols;
- native frame source belongs to CPython internals;
- user Rust frames have workspace source paths or Rust crate modules.

The classifier should produce categories:

```text
user-rust
bridge
python-runtime
native-runtime
unknown
```

The default view keeps user Rust and synthetic Python frames, marks bridge
frames subtle, and optionally hides Python/native runtime frames.

Never permanently discard the raw native stack. A configuration flag and debug
log should expose it for diagnosis.

## CodeLLDB-specific risks

- DAP thread IDs may not equal OS thread IDs.
- CodeLLDB may page or filter stack frames.
- adapter invocation may change between releases.
- CodeLLDB commands/views are enabled only for debug type `lldb`, so a new
  `pyrust` type may not inherit every extension-side UI command.
- the adapter's Python environment is CodeLLDB's own and should not be confused
  with the CPython 3.14 helper environment.

## Local CodeLLDB results

CodeLLDB 1.12.2's bundled LLDB successfully:

- resolved and hit a pending Rust source breakpoint after Python loaded a PyO3
  extension;
- resolved and hit a pending `libpython` function breakpoint in an embedded
  interpreter;
- returned Rust/PyO3 source paths, lines, instruction pointers, modules, and
  frame IDs;
- used OS thread IDs as DAP thread IDs in both single-thread fixtures.
- returned a mock scripted Python source frame through ordinary DAP before the
  native Rust frames.

It did not emit a DAP `process` event for either launch. `process status`
through the debug console exposed the PID through output events.

Ubuntu's system LLDB 18 hung in this environment even for a `/bin/true` launch,
while the bundled LLDB worked. This is local evidence, not a general comparison
of the LLDB versions.

## Recommendation

Use CodeLLDB as a black-box downstream adapter for the alpha. Pin a tested
version range and implement a compatibility self-test that reports:

- CodeLLDB extension version;
- adapter path;
- liblldb path/version;
- Python helper version;
- target PID;
- whether thread mapping succeeded.

## Sources

- CodeLLDB repository:
  https://github.com/vadimcn/codelldb
- CodeLLDB changelog:
  https://github.com/vadimcn/codelldb/blob/master/CHANGELOG.md
- CodeLLDB manual:
  https://github.com/vadimcn/codelldb/blob/master/MANUAL.md
- CodeLLDB adapter startup:
  https://github.com/vadimcn/codelldb/blob/master/extension/novsc/adapter.ts
- LLDB Python extensions:
  https://lldb.llvm.org/python_extensions.html
- LLDB scripted frame provider:
  https://lldb.llvm.org/python_api/lldb.plugins.scripted_frame_provider.ScriptedFrameProvider.html
