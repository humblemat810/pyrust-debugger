# Test Strategy

## Principles

- Test protocol logic without VS Code where possible.
- Test debugger integration with deterministic tiny fixtures.
- Treat graceful native-only fallback as a first-class result.
- Record versions and DAP transcripts for every integration failure.

## Unit tests

### Python helper

- normalize CPython 3.14.6 `ThreadInfo`;
- preserve newest-first frame ordering;
- serialize paths, names, lines, and thread IDs;
- report permission and incompatible-runtime errors;
- reject non-3.14 helper runtime.

### DAP framing

- fragmented headers and bodies;
- multiple messages in one read;
- invalid content length;
- UTF-8 payload lengths;
- downstream/client sequence remapping.

### Stack merge

Golden cases:

- Python -> Rust;
- Python -> Rust -> Python callback;
- Rust -> Python;
- Rust -> Python -> Rust;
- no Python runtime region;
- unknown frames around boundary;
- hidden and visible runtime-frame modes;
- empty Python stack.

### Paging

- omitted paging;
- zero levels;
- first/middle/final windows;
- windows crossing language boundaries;
- accurate `totalFrames`.

### Synthetic IDs

- unique across threads;
- stable within one stop;
- invalidated on continue;
- no collision with native IDs;
- scopes routing.

### LLDB scripted-frame alternative

- PC-less Python source frames appear in CodeLLDB DAP;
- original native frames retain their order and identity;
- provider filtering applies only to Python-hosting threads;
- provider failure falls back to the unmodified native stack;
- if selected, target-memory reads are tested across CPython 3.14 patch
  releases.

## Process tests

Run CPython child processes and verify unwinding:

- running;
- `SIGSTOP`;
- multiple threads;
- recursion;
- exception unwind;
- generator suspended/resumed;
- asyncio task;
- target exits during read;
- access denied.

## Adapter contract tests

Use a fake downstream DAP adapter:

- process event PID capture;
- stopped/continued epoch changes;
- full native stack request despite client paging;
- native-only fallback on helper failure;
- clean terminate/disconnect;
- downstream error propagation.

## Native integration tests

Fixture matrix:

| Fixture | Purpose |
| --- | --- |
| Python -> one Rust function | Basic boundary |
| Python -> nested Rust | Native frame ordering |
| Python worker -> Rust | Thread mapping |
| Python asyncio task -> Rust | Active coroutine locals on one OS thread |
| Python -> Rust -> Python | Multiple boundary regions |
| Rust -> Python | Reverse direction |
| Rust -> Python -> Rust | Full interleaving |
| Rust async future -> Python async -> Rust | Async poll-frame boundary |

Assertions:

- breakpoint binds;
- expected stop thread;
- expected Rust source and line;
- expected Python source and line;
- expected frame order;
- continue reaches next stop;
- second stack does not reuse stale source data.

## VS Code end-to-end tests

Use VS Code's extension test host for:

- launch configuration resolution;
- CodeLLDB discovery;
- adapter descriptor creation;
- visible debug session startup;
- breakpoint registration;
- source navigation from synthetic frames.

UI screenshots are useful evidence but not the primary assertion mechanism.
DAP transcript assertions should carry correctness.

## Compatibility matrix

Alpha:

```text
Ubuntu 24.04
x86_64
CPython 3.14.6
standard GIL build
current tested CodeLLDB 1.12.x
Rust stable debug profile
PyO3 fixture
```

Every expanded platform or runtime adds a new matrix row only after dedicated
CI coverage exists.

## Performance checks

Measure:

- helper startup;
- remote unwind duration;
- native `stackTrace` duration;
- merged response duration;
- cache hit duration;
- behavior with 100+ native/Python frames;
- helper memory use.

Initial target:

- first expanded stack under 500 ms on local Linux;
- cached repeat under 50 ms;
- timeout and native fallback by 2 seconds.

These are provisional UX targets and should be revised from measurements.
