# Project Plan

## Objective

Deliver a Linux alpha that displays one Python/Rust call stack in VS Code when
CPython 3.14 calls a Rust extension and stops at a Rust breakpoint.

## Team assumption

One engineer, limited infrastructure, and no dedicated QA or release engineer.
The plan therefore uses sequential risk gates and avoids parallel product
features.

## Phase 0: Research and environment

Status: complete.

Deliverables:

- Python 3.14.6 local environment;
- external unwinder prototype;
- running/stopped process tests;
- architecture and research reports.

Exit gate:

- tests pass under `.venv`;
- documents identify unsupported behavior and fallbacks.

## Phase 1: Native feasibility fixture

Status: complete for the research phase.

Work:

- install Rust and CodeLLDB development prerequisites;
- create a minimal PyO3 extension;
- create nested Python and Rust calls;
- build unoptimized with debug symbols;
- launch Python under CodeLLDB;
- hit Rust source breakpoints after dynamic module load;
- capture CodeLLDB DAP transcripts;
- verify remote unwinding while LLDB owns the stopped process;
- determine DAP-thread-to-OS-thread mapping.
- run `py-spy --native` against the fixture and capture its mixed ordering;
- determine whether CPython shim/entry markers or py-spy's merged output can be
  aligned with CodeLLDB instruction addresses.

Exit gate:

- one script reproduces a Rust breakpoint;
- the helper reads the correct Python stack at that stop;
- the stopped thread is matched without ambiguity.
- a proven prior-art marker strategy is identified rather than relying only on
  new symbol-name heuristics.

Observed:

- both direction fixtures build and stop under CodeLLDB;
- remote unwinding works while CodeLLDB owns the process;
- CodeLLDB and CPython thread IDs match in the single-thread cases;
- py-spy produces mixed Python/Rust samples;
- CodeLLDB PID discovery still needs a product-quality structured path.
- LLDB scripted frames appear in CodeLLDB DAP, but obtaining real CPython
  frames inside the provider needs a separate memory-reader decision.

No-go conditions evaluated:

- process-memory permissions: passed in normal launch topology;
- OS thread identity: passed for single-thread fixtures;
- exact repeated-boundary markers: remains a future implementation spike.

## Phase 2: Transparent DAP proxy

Estimated effort: 5-8 days.

Work:

- scaffold VS Code extension and `pyrust` debugger type;
- locate and launch CodeLLDB adapter;
- implement DAP framing and sequence remapping;
- forward initialize/launch/configuration/breakpoint/execution traffic;
- preserve CodeLLDB capabilities and events;
- add fake-adapter transcript tests;
- add adapter startup diagnostics.

Exit gate:

- the fixture debugs through `pyrust` exactly as it does through `lldb`;
- no mixed-stack code is enabled;
- shutdown and failed launch are clean.

Architecture checkpoint before implementation:

- estimate a CPython debug-offset reader using `SBProcess.ReadMemory`;
- compare it with direct DAP `stackTrace` merging;
- choose the smaller maintained surface;
- default to the DAP bridge unless the LLDB reader is demonstrably smaller.

## Phase 3: Stack augmentation

Estimated effort: 5-8 days.

Work:

- capture debuggee PID;
- invoke Python helper with timeout;
- normalize Python stacks;
- implement thread mapping;
- fetch complete native stacks;
- classify native frames;
- merge and page results;
- allocate synthetic frame IDs;
- intercept scopes for Python frames;
- cache per stop epoch.

Exit gate:

- one call-stack tree contains expected Rust and Python source frames;
- repeated stops do not show stale frames;
- unsupported targets show native stacks.

## Phase 4: Hardening

Estimated effort: 5-8 days.

Test cases:

- nested Python and Rust calls;
- recursion;
- Python exception paths;
- Rust panic with unwind/abort variants;
- worker threads;
- generator/coroutine frames;
- no active Python frames;
- stripped/optimized Python runtime;
- missing source files;
- permission denial;
- helper crash/timeout;
- CodeLLDB upgrade compatibility.

Exit gate:

- no augmentation failure terminates native debugging;
- diagnostics identify the failed layer;
- acceptance matrix passes on clean Ubuntu environments.

## Phase 5: Reverse direction

Estimated effort: 5-10 days after alpha.

Work:

- build Rust binary embedding CPython 3.14;
- cover Rust -> Python and Rust -> Python -> Rust callback shapes;
- detect interpreter readiness;
- preserve lower Rust/embedder frames;
- add launch templates.

Exit gate:

- the same merge engine displays all three logical regions correctly.

## Later capability projects

Each requires a separate decision:

- Python locals and object rendering;
- Python breakpoints;
- language-routed evaluation;
- coordinated cross-language stepping;
- macOS support;
- Windows support;
- free-threaded CPython;
- Python 3.15+.

## Delivery checkpoints

| Checkpoint | Evidence |
| --- | --- |
| R0 | Research docs and Python probe |
| R1 | LLDB stop plus Python unwind transcript |
| R2 | Transparent proxy demo |
| R3 | Merged-stack demo |
| Alpha | Hardened Linux package and compatibility matrix |

## Resource policy

- Support one OS, architecture, Python minor, and native backend first.
- Prefer tests and diagnostics over broad configuration.
- Do not build Python breakpoint support before stack alpha acceptance.
- Stop at each no-go gate rather than carrying a weak assumption forward.
