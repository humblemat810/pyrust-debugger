# Documentation Index

## Decision material

- [Overall analysis report](analysis-report.md)
- [Architecture](architecture.md)
- [ADR 0001: Python-outer first](decisions/0001-python-outer-first.md)
- [ADR 0002: 72-hour first workable slice](decisions/0002-72-hour-first-workable-slice.md)
- [ADR 0003: Stabilize before Rust-outer](decisions/0003-stabilize-before-rust-outer.md)
- [ADR 0004: Containerized VS Code validation](decisions/0004-containerized-vscode-validation.md)
- [ADR 0005: Read-only Python frame locals](decisions/0005-read-only-python-frame-locals.md)
- [ADR 0006: Process-tree coordinator](decisions/0006-process-tree-coordinator.md)
- [Feasibility summary](feasibility.md)

## Research reports

- [CPython 3.14 remote unwinding](research/cpython-3.14.md)
- [VS Code and Debug Adapter Protocol](research/vscode-dap.md)
- [CodeLLDB and LLDB](research/native-debugger.md)
- [Mixed-debugger prior art](research/prior-art.md)
- [Exact prior-art search](research/exact-prior-art-search.md)
- [Executable fixture results](research/fixture-results.md)
- [Containerized VS Code results](research/containerized-vscode-results.md)
- [Research completion audit](research/completion-audit.md)

## Planning

- [Project plan](project-plan.md)
- [MVP details](mvp.md)
- [First workable slice acceptance](acceptance/first-workable-slice.md)
- [Rust-outer stabilization acceptance](acceptance/rust-outer-stabilization.md)
- [Containerized VS Code acceptance](acceptance/containerized-vscode.md)
- [Containerized VS Code manual verification](acceptance/containerized-vscode-manual.md)
- [Python frame locals acceptance](acceptance/python-frame-locals.md)
- [Process-tree coordinator acceptance](acceptance/process-tree-coordinator.md)
- [Async coordinator acceptance](acceptance/async-coordinator.md)
- [Test strategy](test-plan.md)
- [Risk register](risk-register.md)

## Current conclusion

Start with **Python calling Rust**, CPython 3.14, Linux x86_64, and a
native-debugger-first DAP proxy. CodeLLDB controls the process. A CPython 3.14
helper reads Python frames directly from the stopped process and the proxy
merges them into CodeLLDB's stack response.

ADR 0003 is implemented. In-process unwinder timeouts are bounded by a session
circuit breaker, and **Rust embedding Python** is proven at an explicit Rust
callback breakpoint while lower Rust frames remain usable.

ADR 0005 adds read-only primitive local snapshots for the supported Python
frames in both directions, plus safe snapshot expressions. It does not add
Python breakpoints, stepping, object expansion, mutation, or in-target Python
execution.

ADR 0006 implements the first process-tree coordinator. Two-worker
Python-thread and Rust-thread fixtures pass. Python-parent and Rust-parent
multiprocess fixtures also pass through dedicated child CodeLLDB transports;
see
[Multiprocess Native Attach Result](research/multiprocess-native-attach.md).

Async native-stop support is verified for Python `asyncio` tasks and Rust
`async fn` futures crossing embedded Python `async def`; see
[Async coordinator acceptance](acceptance/async-coordinator.md).

ADR 0004 is implemented. A pinned Linux Dev Container, local `pyrust`
extension, both launch configurations, and the two-lifecycle acceptance command
pass `AC-CV-01` through `AC-CV-10`. Human Call Stack criteria `HC-CV-01`
through `HC-CV-04` remain pending.
