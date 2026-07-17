# Exact Prior-Art Search

## Question

Has someone already shipped the exact idea successfully?

> A VS Code debugger that shows Python and Rust frames interleaved in the same
> call-stack tree for both Python -> Rust and Rust -> Python execution.

## Search date

July 17, 2026.

## Conclusion

No public implementation of the exact VS Code + Python + Rust + one interleaved
debug stack product was found.

This is not a claim that no private or unindexed implementation exists. It
means the searched public repositories, issue trackers, Marketplace entries,
documentation, and web results did not reveal one.

The underlying technical operation has been implemented successfully:

- `py-spy --native` merges Python and native extension frames, including
  multiple Python/native blocks;
- CPython's perf trampoline enables mixed Python/native profiler stacks;
- Visual Studio's Python mixed-mode debugger presents integrated Python/native
  debugging on Windows;
- VS Code extensions successfully coordinate debugpy and native adapters in one
  workflow, but expose separate stacks or route to one active adapter.

The project is therefore not novel at the stack-unwinding-algorithm level. Its
specific contribution is bringing a proven mixed-stack technique into a VS Code
DAP session while preserving CodeLLDB's Rust frame identities and debugger
behavior.

## Search coverage

### Terms

Representative searches included:

```text
VS Code mixed Python Rust debugger same call stack
GitHub Python Rust mixed debugger DAP stackTrace merge
single call stack Python native VSCode
merged stack Python C++ debugger VS Code
interleaved Python native stack
RemoteUnwinder VSCode debugger stackTrace
```

### Sources

- Visual Studio Marketplace;
- GitHub repository and issue search;
- CodeLLDB, debugpy, and PyO3 issue trackers;
- DAP specification and issues;
- CPython and LLVM documentation and source;
- Stack Overflow;
- existing Python/C++ VS Code debugger extensions;
- Python native profilers.

## Candidate assessment

| Candidate | One VS Code session | Python + native | One interleaved stack | Rust-specific | Result |
| --- | --- | --- | --- | --- | --- |
| `bowen-xu/python-cpp-debugger-ext` | Yes | Yes | No, routes to active adapter | No | Adjacent |
| `benibenj/vscode-pythonCpp` | Wrapper | Yes | No, launches two sessions | No | Adjacent |
| `dap-mux` | One upstream session shared by clients | Per adapter | No cross-adapter merge | Tested separately with CodeLLDB | Infrastructure |
| CodeLLDB + debugpy compound launch | Two sessions | Yes | No | Rust capable | Workaround |
| `py-spy --native` | No debugger UI | Yes | Yes | Native Rust symbols are possible | Core algorithm proven |
| CPython perf trampoline + `perf` | No debugger UI | Yes | Yes for profiling | Native-language agnostic | Mechanism proven |
| Visual Studio Python mixed mode | Visual Studio, not VS Code | Yes | Yes | C/C++ focused | Product concept proven |

## Closest successful implementation: py-spy

`py-spy` is written in Rust and supports Python 3.14. Its native mode:

1. reads the Python logical stack;
2. unwinds the native stack;
3. identifies Python interpreter addresses and eval-frame symbols;
4. inserts Python frame blocks at native eval boundaries;
5. uses CPython entry/shim markers to handle multiple blocks;
6. retains ordinary native frames;
7. falls back when the native unwind is incomplete.

The implementation is in `src/native_stack_trace.rs`.

Important details:

- Python 3.11 uses entry markers;
- Python 3.12+ uses shim frames before blocks;
- eval-frame symbols become insertion points;
- unresolved native addresses are retained as stubs;
- native unwinding and stack merging can fail independently;
- explicit tolerance exists for race-shaped frame-count mismatches.

`py-spy` is MIT licensed. Its algorithm can be studied or reused subject to the
license notice requirements.

### Local verification

`py-spy 0.4.2` was run in native mode against both a standard-library native
call and the repository's actual CPython 3.14.6 -> PyO3/Rust fixture on
July 17, 2026.

Result:

```text
Python/Rust samples: 99
Errors: 0
```

The raw Python/Rust samples contained chains spanning:

```text
<module> -> python_outer -> python_inner -> CPython runtime -> Rust/PyO3 symbol
```

This does not prove CodeLLDB integration, but it confirms that the cited merge
tool works with the selected CPython, PyO3, and Rust combination.

## Gap between py-spy and this debugger

`py-spy` produces profiler frames, not DAP frames. It does not need to preserve:

- CodeLLDB stack-frame IDs;
- Rust scopes and variables;
- selected-frame evaluation;
- restart and step semantics;
- DAP paging;
- VS Code stop epochs.

Our adapter must merge display frames while retaining a mapping back to every
original CodeLLDB Rust frame.

## Impact on architecture

### MVP

CPython 3.14 `RemoteUnwinder` remains a good first spike for a single
Python/native boundary. It is already working locally and has a small API.

### General mixed stack

The 3.14.6 `RemoteUnwinder` output is flattened and does not expose py-spy's
entry/shim block markers. That is insufficient for arbitrary repeated
Python/native alternation.

Before claiming both directions generally, choose one:

1. integrate py-spy's Python stack reader and markers into a sidecar;
2. implement the relevant CPython debug-offset reads and expose block markers;
3. use CPython perf-trampoline markers if LLDB reliably exposes them;
4. use py-spy's complete merged stack as an ordering oracle, then align native
   addresses to CodeLLDB frames.

Option 4 is the strongest first research spike because it reuses a working
algorithm without giving up CodeLLDB's debugger state.

## DAP ecosystem result

The DAP multi-adapter request remains open. Existing bridges implement their own
coordination. DAP does not currently provide a standard mixed-engine launch or
stack-composition facility.

## Sources

- py-spy:
  https://github.com/benfred/py-spy
- py-spy merge implementation:
  https://github.com/benfred/py-spy/blob/master/src/native_stack_trace.rs
- py-spy Python 3.12+ native merge fix:
  https://github.com/benfred/py-spy/pull/751
- CPython perf profiling:
  https://docs.python.org/3/howto/perf_profiling.html
- Visual Studio Python mixed mode:
  https://learn.microsoft.com/en-us/visualstudio/python/debugging-mixed-mode-c-cpp-python-in-visual-studio
- Python C++ Mixed Debugger:
  https://github.com/bowen-xu/python-cpp-debugger-ext
- older Python C++ Debugger:
  https://github.com/benibenj/vscode-pythonCpp
- dap-mux:
  https://github.com/dap-mux/dap-mux
- DAP multi-adapter issue:
  https://github.com/microsoft/debug-adapter-protocol/issues/139
- report that existing VS Code workflows produce two stacks:
  https://stackoverflow.com/questions/77486102/get-python-and-c-stack-trace-at-once-in-vs-code
