# PyRust: Developer Tools Submission

## Track

**Developer Tools**

## One-Line Pitch

PyRust makes mixed CPython 3.14 and Rust debugging legible in VS Code by
combining CodeLLDB's native stack with read-only Python stack recovery and an
honest process-and-thread tree.

## Problem

PyO3 applications cross a language boundary that normal debuggers do not
present as one developer workflow. At a Rust breakpoint, a developer can see
native frames but lose the Python caller that supplied the request. In
concurrent programs, it is also easy to mistake a process, Python thread, Rust
thread, coroutine, or child process for another kind of execution context.

## What We Built

PyRust is a local VS Code debugger prototype for Linux x86_64 that:

- launches the target through CodeLLDB;
- recovers CPython 3.14 frames from the stopped target without executing Python
  in that target;
- returns one mixed Python/Rust Call Stack through DAP;
- supplies read-only primitive Python locals and a bounded snapshot expression
  subset;
- adds **PyRust Process Tree**, a custom Debug-sidebar view for real processes
  and native threads;
- keeps thread and process grouping honest: processes own threads, sibling
  threads stay siblings, and `asyncio` tasks/Rust futures are not fabricated as
  a hierarchy;
- provides reproducible Dev Container setup and black-box acceptance tests.

## Live Demo

Use the `PyRust: Python and Rust Threads` launch configuration.

1. Open the repository in its Dev Container.
2. Set a Rust breakpoint at `research/fixtures/python_outer/src/lib.rs:6`.
3. Start `PyRust: Python and Rust Threads`.
4. At the breakpoint, show the normal Call Stack with `rust_inner` and its
   Rust parent function.
5. Open **PyRust Process Tree** and expand the stopped `rust-child-*` thread.
6. Click `rust_inner` in the tree to open the source location and show the
   PyRust amber navigation decoration.
7. Expand a sibling thread and explain that it is a sibling, not a fake child
   of the Python caller, because it is a separate OS thread.

The exact manual sequence is in
[Demo Runbook](demo-runbook.md). The automated gate verifies the core
mixed-stack, thread, process, async, and VSIX claims:

```bash
./scripts/verify-submission.sh
```

## Why This Is Useful

The project focuses on a concrete developer failure mode rather than adding
another chat surface: when a Python service enters Rust for performance or
embeds Python from Rust, developers need to inspect the actual mixed execution
context without manually correlating separate native and Python debugger
sessions.

## Built With Codex

Codex was used as the implementation partner across the project:

- researched the CPython 3.14 unwinder, DAP, CodeLLDB, and prior art;
- created fixture-driven Python-to-Rust and Rust-to-Python integrations;
- built the DAP proxy, VS Code extension, Dev Container, and acceptance
  harnesses;
- added process/thread UI behavior, source-navigation fixes, and regression
  tests;
- produced the ADRs, acceptance contracts, reuse templates, and demo material.

The repository keeps this collaboration auditable through commits, ADRs,
acceptance criteria, and reproducible commands rather than claiming that Codex
replaced technical verification.

## Evidence

| Claim | Evidence |
| --- | --- |
| Python -> Rust mixed stack | `./scripts/accept-first-slice.sh` |
| Rust -> Python -> Rust callback stack | `./scripts/accept-reverse-slice.sh` |
| Python threads entering Rust | `./scripts/accept-thread-slice.sh` |
| Python entry creating Rust worker threads | `./scripts/accept-python-rust-threads.sh` |
| Process/thread hierarchy and lifecycle | `./scripts/accept-process-thread-mode.sh` |
| Packaged VS Code extension | `npm run --prefix vscode-extension package` |
| Clean Dev Container validation | `PYRUST_VERIFY_CONTAINER=1 ./scripts/verify-submission.sh` |

## Deliberate Limits

- Linux x86_64 only.
- CPython 3.14 and the pinned CodeLLDB environment only.
- Rust breakpoints are supported; Python breakpoints are not.
- Python expressions run only against a read-only captured local snapshot.
- Cross-language stepping is not supported.
- The VSIX is a local prototype, not a Marketplace product.
- A custom Process Tree cannot take ownership of VS Code's built-in yellow
  active-stack-frame marker.

## Roadmap

1. Bundle or package the adapter so the VSIX can be installed outside this
   repository without absolute-path configuration.
2. Improve manual-demo timeouts independently from short acceptance-fixture
   timeouts.
3. Add production-oriented source mapping, platform support, and broader
   Python/Rust boundary classification.
4. Investigate a DAP-native presentation for process/thread hierarchy where VS
   Code permits it, while preserving correct ownership semantics.
