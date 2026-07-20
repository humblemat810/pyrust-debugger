# PyRust Debugger

This local workspace extension registers the fixture-bound `pyrust` debug type
and launches the Python DAP proxy in the repository.

It is intended only for the Linux x86_64 Dev Container defined by ADR 0004.
Python breakpoints, Python evaluation, and Marketplace packaging are not
supported.

## Process Tree

During a PyRust session, open **Run and Debug** and locate **PyRust Process
Tree** in the Debug sidebar. It groups real parent/child processes and their
attached native threads. Selecting a thread runs **PyRust: Focus Process-Tree
Thread** and opens that thread's top mixed-stack source frame.

The normal VS Code Call Stack remains responsible for Rust/Python caller/callee
frames. Independent threads, sibling child processes, and `asyncio` tasks are
not falsely nested.
