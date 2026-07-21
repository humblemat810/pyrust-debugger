# PyRust Debugger

This local workspace extension registers the fixture-bound `pyrust` debug type
and launches the Python DAP proxy in the repository.

It is intended for the Linux x86_64 Dev Container defined by ADR 0004.
The launch configuration can be adapted to another PyO3 application, but the
current VSIX does not bundle the Python DAP adapter; external workspaces must
point `pyrust.adapterPath` and `pyrust.pythonPath` at a PyRust debugger
checkout. Marketplace packaging is not supported.

Set `pyrustPythonDebug: true` in a launch configuration to enable debugpy for
Python-owned source-breakpoint stops. That mode supports normal Python
variables and expression evaluation, including imports and calls. Rust stops
remain CodeLLDB-owned and show the existing merged stack with bounded Python
snapshot evaluation.

## Process Tree

During a PyRust session, open **Run and Debug** and locate **PyRust Process
Tree** in the Debug sidebar. It groups real parent/child processes and their
attached native threads. Selecting a thread runs **PyRust: Focus Process-Tree
Thread** and opens that thread's top mixed-stack source frame. If a paused
native thread is blocked in a library frame without workspace source, PyRust
opens CodeLLDB's current native source content or a bounded forward
disassembly. Invalid source references and unreadable instruction addresses
stay local to the clicked frame instead of disturbing the debug session.
Process Tree rows are valid only for the debug session and stop generation
that produced them.

The normal VS Code Call Stack remains responsible for Rust/Python caller/callee
frames. Independent threads, sibling child processes, and `asyncio` tasks are
not falsely nested.
