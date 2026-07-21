# Process and Threads Manual QC

This guide verifies the real VS Code presentation for the
`PyRust: Process and Threads` launch configuration. It complements the
black-box command:

```bash
./scripts/accept-process-thread-mode.sh
```

## Start

1. Use the repository's Dev Container and install the current local extension.
2. Open **Run and Debug** and select **PyRust: Process and Threads**.
3. Start debugging. The launch uses:
   - binary `rust-outer-python-process-threads`;
   - `PYRUST_CHILD_REGISTRY`;
   - `PYRUST_PYTHON`;
   - `PYRUST_PROCESS_THREAD_WORKER`;
   - `pyrustProcessMode: children`;
   - the `PyRust: Build Rust Process and Threads` pre-launch task.

**Do not run** `PyRust: Focus Process-Tree Thread` or
`PyRust: Focus Process-Tree Stack Frame` from the Command Palette. Those are
tree-item actions and require a clicked thread or frame in **PyRust Process
Tree**.

## Breakpoint

Set the native source breakpoint exactly at:

```text
research/fixtures/python_outer/src/lib.rs:6
```

The breakpoint is `rust_inner`. Do not add Python breakpoints.

## Expected Process Tree

Open **PyRust Process Tree** while a worker is stopped. PIDs and TIDs vary by
run, but the labels, roles, commands, indentation, and states must match this
shape:

```text
Rust parent process (pid <P>, command .../rust-outer-python-process-threads, running)
  process-A (pid <A>, role Python child process, command .../process_thread_worker.py process-A 20, stopped)
    process-A-worker-1 (tid <A1>, stopped)
      pyrust_native::rust_inner             lib.rs:6
      pyrust_native::rust_outer             lib.rs:13
      python_worker                         process_thread_worker.py:...
    process-A-worker-2 (tid <A2>, running)
  process-B (pid <B>, role Python child process, command .../process_thread_worker.py process-B 40, running or stopped)
    process-B-worker-1 (tid <B1>, running or stopped)
    process-B-worker-2 (tid <B2>, running)
```

`<P>`, `<A>`, `<B>`, and all TIDs must be distinct where appropriate.
`process-A` and `process-B` are siblings directly under the Rust parent.
CodeLLDB may also show each child's Python runtime/main thread as an additional
direct leaf; the two named `process-*-worker-*` TIDs above are mandatory.
Worker threads are direct leaves of their owning process and never parents.
Within each child, worker 2 intentionally waits until worker 1 continues;
this makes per-worker progression deterministic while keeping both real OS
threads visible.
The process description and tooltip must show the role, PID, state, and
command summary. Expand a stopped worker to see its mixed Rust/Python frames
under that thread. Running threads do not expose a stack until they stop.

Clicking a worker thread must focus its top source frame at
`lib.rs:6`. The standard DAP Threads view remains flat.
The selected source line also receives a high-contrast amber PyRust decoration.
It is navigation feedback from this custom tree, not the built-in debugger's
yellow active-frame marker.

## Expected Call Stack

For the selected stopped worker, the standard **Call Stack** must retain the
mixed boundary in this order at the top:

```text
rust_inner                 research/fixtures/python_outer/src/lib.rs:6
rust_outer
python_worker              tests/acceptance/process_thread_worker.py
```

Lower fixture/runtime frames may follow. The selected process label and worker
TID must agree with the process-tree node; selecting the sibling must not show
the selected child's Python locals or frames.

## Continue And Cleanup

1. Continue one stopped worker with the normal debugger control.
2. Confirm its synthetic Python frame becomes stale and its child subtree
   eventually disappears.
3. Before continuing the sibling, confirm the Rust parent and sibling process
   remain visible with the sibling's two worker TIDs and names intact.
4. Continue the sibling and confirm its subtree then disappears.
5. Confirm no child process or worker thread remains after the Rust parent
   finishes.

## Async Nonnesting

Run both existing async configurations or commands after the process/thread
check:

```bash
./scripts/accept-async-slice.sh
./scripts/accept-rust-async-slice.sh
```

While an async worker is stopped, the process tree may show its owning OS
process and native thread only. It must not show `asyncio` tasks, Rust futures,
await nodes, or any task/future hierarchy. Async context switches remain
ordinary activity on the owning native thread, and the standard Call Stack
continues to show the existing mixed async frames.

Record the exact launch name, breakpoint path, parent/child PIDs, worker TIDs,
and the observed cleanup result in the QC run notes.
