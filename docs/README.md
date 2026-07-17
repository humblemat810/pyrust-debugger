# Documentation Index

## Decision material

- [Overall analysis report](analysis-report.md)
- [Architecture](architecture.md)
- [ADR 0001: Python-outer first](decisions/0001-python-outer-first.md)
- [ADR 0002: 72-hour first workable slice](decisions/0002-72-hour-first-workable-slice.md)
- [Feasibility summary](feasibility.md)

## Research reports

- [CPython 3.14 remote unwinding](research/cpython-3.14.md)
- [VS Code and Debug Adapter Protocol](research/vscode-dap.md)
- [CodeLLDB and LLDB](research/native-debugger.md)
- [Mixed-debugger prior art](research/prior-art.md)
- [Exact prior-art search](research/exact-prior-art-search.md)
- [Executable fixture results](research/fixture-results.md)
- [Research completion audit](research/completion-audit.md)

## Planning

- [Project plan](project-plan.md)
- [MVP details](mvp.md)
- [First workable slice acceptance](acceptance/first-workable-slice.md)
- [Test strategy](test-plan.md)
- [Risk register](risk-register.md)

## Current conclusion

Start with **Python calling Rust**, CPython 3.14, Linux x86_64, and a
native-debugger-first DAP proxy. CodeLLDB controls the process. A CPython 3.14
helper reads Python frames directly from the stopped process and the proxy
merges them into CodeLLDB's stack response.

The reverse direction, **Rust embedding Python**, is retained as the next
scenario because it reuses the same unwinder and merge engine.
