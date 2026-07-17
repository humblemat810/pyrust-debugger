# Research Report: Prior Art

## Finding

Mixed Python/native debugging is a proven product category, but existing VS
Code approaches generally coordinate two sessions or switch the active adapter.
They do not remove the need for this project's defining work: constructing one
interleaved Python/Rust stack.

An explicit search for the exact product is documented in
[Exact Prior-Art Search](exact-prior-art-search.md). No public exact match was
found as of July 17, 2026.

## Visual Studio mixed-mode debugger

Microsoft Visual Studio on Windows has a dedicated Python/native mixed-mode
debugger. This establishes that integrated stacks, breakpoints, and transitions
are possible at product quality.

It is not directly reusable:

- it is a Visual Studio engine, not a VS Code DAP adapter;
- it is Windows-specific;
- its CPython support and limitations differ by version;
- it is not an implementation dependency for this project.

Value to this project: product-behavior reference and evidence that users expect
more than two side-by-side sessions.

## Compound/dual VS Code sessions

Common recipes start debugpy, then attach GDB or LLDB to the same process.

Strengths:

- easy to configure;
- both breakpoint engines remain intact;
- useful for expert manual debugging.

Weaknesses:

- two call-stack trees;
- two active session selectors;
- separate continue/step ownership;
- native stops can prevent in-process Python debugger components from running;
- not the requested single mixed stack.

Value: troubleshooting fallback, not product architecture.

## Python C++ Mixed Debugger extension

The inspected open-source extension
`bowen-xu/python-cpp-debugger-ext` uses one proxy session with:

- debugpy launching Python;
- LLDB attaching after debugpy reports the PID;
- breakpoint routing by source extension;
- active-adapter switching on stopped events;
- request sequence remapping;
- coordinated configuration and shutdown.

Its code is useful evidence for:

- finding packaged debugpy/CodeLLDB adapters;
- obtaining PID from DAP `process` events;
- the amount of coordination required by a two-adapter design.

Its current stack behavior routes `stackTrace` to whichever adapter is active.
It does not interleave both stacks in one response.

License note: the project is GPL-3.0. We may study its architecture, but should
not copy code into a differently licensed implementation without deliberate
license alignment.

## Older Python/C++ VS Code extensions

Several extensions launch a Python session and attach a native debugger. They
mostly automate the compound-session workflow and do not provide a unified DAP
stack.

## CPython GDB helpers

CPython's `python-gdb.py` and commands such as `py-bt` reconstruct logical
Python frames from a native process. This is strong evidence that external
mixed-stack reconstruction is technically sound.

For this project, CPython 3.14's debug-offset-based remote unwinder is preferable
because it does not depend on GDB, Python debug symbols, or optimized local
variables in interpreter C frames.

## py-spy native stack merging

`py-spy --native` is the closest successful implementation of the core
algorithm. It is a Rust profiler that combines Python logical frames with
native extension frames.

Its merge code:

- recognizes CPython eval-frame insertion points;
- handles Python 3.12+ shim markers;
- supports multiple alternating Python/native blocks;
- retains unresolved native frames;
- includes fallbacks for incomplete native unwinds.

This is more directly reusable than designing a boundary heuristic from first
principles. The remaining work is adapting the ordering result to DAP while
preserving CodeLLDB frame IDs.

## Implications

1. Do not spend the first milestone recreating dual-adapter coordination.
2. Reuse the proven PID handoff and DAP proxy concepts.
3. Build the unique value first: merged stack construction.
4. Base merge research on py-spy's proven marker strategy.
5. Treat debugpy integration as a later capability phase.
6. Keep project code independently implemented with a clear license.

## Sources

- Visual Studio mixed-mode Python/native debugging:
  https://learn.microsoft.com/en-us/visualstudio/python/debugging-mixed-mode-c-cpp-python-in-visual-studio
- Python C++ Mixed Debugger:
  https://github.com/bowen-xu/python-cpp-debugger-ext
- CPython GDB helpers:
  https://docs.python.org/3/howto/gdb_helpers.html
- py-spy native merge implementation:
  https://github.com/benfred/py-spy/blob/master/src/native_stack_trace.rs
- DAP overview:
  https://microsoft.github.io/debug-adapter-protocol/overview.html
