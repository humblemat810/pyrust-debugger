# Process Tree Manual QC Guide

## Purpose

Verify the **PyRust Process Tree** view in a Remote-SSH + Dev Container VS
Code window. This is supplementary human evidence; automated DAP and extension
tree-model tests remain the primary correctness checks.

## Before Testing

1. Rebuild the Dev Container only after changing
   `.devcontainer/Dockerfile` or `.devcontainer/devcontainer.json`.
2. Package and install the current extension from the Dev Container terminal:

```bash
env -u NODE_OPTIONS /opt/node/bin/npm run --prefix vscode-extension package
rm -f ~/.pyrust-debugger/vsix.sha256
bash .devcontainer/install-vscode-extension.sh
env -u NODE_OPTIONS code --list-extensions --show-versions | grep -Fx \
  'pyrust.pyrust-debugger@0.0.5'
```

3. Run `Developer: Reload Window`. If the extension was updated while a
   debug session was running, stop that session first.
   If the attach log says the extension install was deferred, run
   `bash .devcontainer/install-vscode-extension.sh` once from a fresh
   integrated Dev Container terminal, then reload the window.
4. Open **Run and Debug**. In the Debug sidebar, find **PyRust Process Tree**.
   It stays visible even before a PyRust session starts and is empty until one
   is active.

The normal **Call Stack** view remains flat across DAP threads. The PyRust
view is the intentional nested representation.

## Rule To Verify

Indent only real structure:

```text
process
  thread
  child process
    child thread
```

Do not indent two independent threads, sibling processes, or two `asyncio`
tasks merely because execution context switched between them.

## QC-PT-01: Python Thread Siblings

1. Select `PyRust: Python Threads`.
2. Set a breakpoint at `research/fixtures/python_outer/src/lib.rs:6`.
3. Start debugging.
4. At the first stop, inspect **PyRust Process Tree**.

Expected shape:

```text
Python process
  worker-A / worker-B thread (stopped)
  sibling thread(s), if reported by CodeLLDB
```

The two worker threads must be siblings under one process. Neither worker is a
child of the other.

Click the stopped worker thread. The editor must navigate to `rust_inner`, and
the standard Call Stack must contain `rust_inner`, `rust_outer`, and
`python_worker`.

## QC-PT-02: Rust Thread Siblings

1. Select `PyRust: Rust Threads`.
2. Set a breakpoint at
   `research/fixtures/rust_outer/src/threaded_main.rs:12`.
3. Start debugging.

Expected shape:

```text
Rust process
  rust-worker-A / rust-worker-B thread (stopped)
  sibling thread(s), if reported by CodeLLDB
```

The Rust workers must remain siblings. Clicking one opens `rust_callback`;
Call Stack shows the embedded Python frames.

## QC-PT-03: Python Entry With Rust Child Threads

1. Select `PyRust: Python and Rust Threads`.
2. Set a breakpoint at `research/fixtures/python_outer/src/lib.rs:6`.
3. Start debugging.

Expected shape:

```text
Python process
  python-worker-A / python-worker-B thread
  rust-child-20-1 / rust-child-20-2 thread (stopped)
  rust-child-40-1 / rust-child-40-2 thread (stopped or running)
```

All entries are siblings under one process. The Rust child threads must not be
shown below their Python caller thread: creating a Rust OS thread breaks the
caller/callee stack relationship. Clicking a stopped `rust-child-*` thread
opens `rust_inner`; its Call Stack includes `rust_inner` and
`rust_outer_with_rust_threads`, but does not fabricate a Python frame from a
different OS thread.

Every paused sibling is selectable. A Python main/worker sibling can be
blocked in a libc synchronization frame, which has no workspace source file.
Clicking that frame or thread must open CodeLLDB-provided disassembly rather
than silently doing nothing. Only workspace-backed frames receive the amber
PyRust source-line decoration.

Some platform/runtime frames expose an address that LLDB cannot decode. PyRust
first asks for CodeLLDB's current native source content, then attempts a
bounded forward disassembly. If neither works, the frame reports one concise
unavailable message and the debuggee must remain paused and usable.

Continue invalidates the expanded frame rows. PyRust tags each row with its
debug session and stop generation; an old row must report that it is stale and
must not send a cached CodeLLDB source reference. Collapse and expand the
current thread to obtain its new rows.

This fixture has four named Rust-child breakpoint stops. It remains paused
without a timer while you inspect a stop. After you press Continue on the
fourth stop, the fixture finishes and VS Code closes the debug session by
design; press `F5` to start another run.

## QC-PT-03B: Processes And Threads

1. Select `PyRust: Process and Threads`.
2. Set a breakpoint at `research/fixtures/python_outer/src/lib.rs:6`.
3. Start debugging and expand both Python child processes.
4. Leave one stopped worker at the breakpoint for at least 60 seconds.

The workspace launch sets `PYRUST_BREAKPOINT_HOLD_TIMEOUT_SECONDS=3600`.
The process tree and debug session must remain alive while you inspect it.
The short 20/45-second watchdog defaults remain active only when the fixture is
run by automation without this launch setting.

Automated long-idle proof:

```bash
.venv/bin/python -m tests.acceptance.run_process_thread_idle_acceptance
```

## QC-PT-04: Python Parent With Child Processes

1. Select `PyRust: Python Processes`.
2. Set a breakpoint at `research/fixtures/python_outer/src/lib.rs:6`.
3. Start debugging.

Expected shape after the first child stop:

```text
Python parent process (pid ...)
  process-A (pid ...)
    Main thread (tid ..., stopped)
  process-B (pid ...)
    Main thread (tid ...)
```

The parent is a launcher-only node in child-only mode. It intentionally has no
selectable CodeLLDB thread because PyRust does not attach CodeLLDB to the
parent. Do not treat that as a failure and do not expect a fabricated parent
thread.

Click `process-A`'s stopped main thread. The editor must open `rust_inner`;
Call Stack must include `rust_inner`, `rust_outer`, and `python_worker`.
Continue. The stopped marker must move to the sibling child, never become
nested under the first child.

## QC-PT-05: Rust Parent With Python Child Processes

1. Select `PyRust: Rust Processes`.
2. Set a breakpoint at `research/fixtures/python_outer/src/lib.rs:6`.
3. Start debugging.

Expected shape:

```text
Rust parent process (pid ...)
  process-A (pid ...)
    Main thread (tid ..., stopped)
  process-B (pid ...)
    Main thread (tid ...)
```

The same child-thread focus behavior must work. On session end, all nodes
disappear. If an individual child exits before its sibling, only that child
and its thread disappear.

## QC-PT-06: Async Is Not a Task Tree

1. Select `PyRust: Python Async`.
2. Set the breakpoint at `research/fixtures/python_outer/src/lib.rs:6`.
3. Start debugging.

Expected:

```text
Python process
  one stopped OS thread
```

The standard Call Stack identifies the active `async_worker` coroutine. The
tree must not claim that `async-A` is a parent of `async-B`, or show an
invented await graph.

## Record Template

```text
Date:
VS Code / Remote-SSH / Dev Container version:
Configuration:
Breakpoint file and line:
Expected shape:
Observed shape:
Focused thread result:
Call Stack result:
Lifecycle result after continue/exit:
PASS / FAIL:
Screenshot or log path (optional):
```
