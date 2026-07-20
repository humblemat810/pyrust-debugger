# Goal: Process and Thread Mode With Mixed Python/Rust Fixture

Start the root Goal mode agent with `gpt-5.6-sol` at Ultra. Use Extra High
only when Ultra is unavailable.

Implement a process-and-thread presentation slice on top of ADR 0007. This is
not a replacement for the standard Call Stack. The normal Call Stack continues
to show the selected mixed Rust/Python caller/callee stack. **PyRust Process
Tree** shows only real operating-system structure:

```text
Rust parent process (pid ..., command ...)
  Python process-A (pid ..., command ...)
    process-A-worker-1 (tid ...)
    process-A-worker-2 (tid ..., stopped)
  Python process-B (pid ..., command ...)
    process-B-worker-1 (tid ...)
    process-B-worker-2 (tid ...)
```

Read before editing:

- `docs/decisions/0006-process-tree-coordinator.md`
- `docs/decisions/0007-process-tree-view.md`
- `docs/acceptance/process-tree-coordinator.md`
- `docs/acceptance/process-tree-manual.md`

## Required Behavior

1. Add an explicit `PyRust: Process and Threads` launch configuration.
2. The fixture must run a Rust parent process which launches two Python child
   processes. Each Python child starts two named OS threads, and each worker
   crosses Python -> Rust at a source breakpoint.
3. Every process node must display:
   - a readable role and label;
   - PID;
   - the launch command or another durable identifying command summary;
   - stopped/running state.
4. Each known native thread is displayed beneath its owning process, with TID,
   CodeLLDB thread name when available, and stopped/running state.
5. Parent -> child process indentation is real ancestry only. Threads are
   direct children of their process. Sibling processes and sibling threads
   never nest under one another.
6. Preserve the existing async fixtures and behavior. `asyncio` tasks and Rust
   futures remain ordinary activity on their owning OS thread: do not invent a
   task hierarchy or claim caller/child structure from a context switch.
7. Clicking a thread in the custom tree still opens the top source frame for
   that thread. The standard Call Stack must retain the mixed Python/Rust
   frames and existing expression behavior.
8. Child exit removes only that child and its threads. The parent and sibling
   child remain visible until their own lifecycle ends.

## Shared Contract

Before spawning write-heavy agents, the root agent must define and record the
`pyrust/processTree` process payload. At minimum it must include:

```json
{
  "processId": 123,
  "parentProcessId": 100,
  "label": "process-A",
  "role": "Python child process",
  "command": ".venv/bin/python tests/acceptance/process_thread_worker.py process-A",
  "isStopped": true,
  "threads": [
    {
      "threadId": 456,
      "name": "process-A-worker-2",
      "isStopped": true
    }
  ]
}
```

The command is informational only. It must be supplied by the launch or child
registry record; never read arbitrary live process memory or shell out to an
unbounded process inspector just to populate the tree.

## Agents and Order

1. Define the shared payload and acceptance criteria in the root thread.
2. Spawn `process_thread_presentation_builder` and
   `process_thread_fixture_builder` in parallel. They must not edit each
   other's ownership paths.
3. Integrate the payload contract, then run the targeted fixture manually or
   through its black-box harness.
4. Spawn `process_thread_acceptance_builder` after the fixture interface is
   stable. It owns acceptance, launch/task wiring, and the manual QC guide.
5. Run the full existing acceptance suite, then spawn
   `process_thread_mode_reviewer`.
6. Address every blocking review finding and rerun the new and existing
   acceptance commands.

## Acceptance

Add a bounded command, for example:

```bash
./scripts/accept-process-thread-mode.sh
```

It must prove:

- `AC-PTM-01`: Rust parent and two Python children have distinct PIDs and true
  parent/child links.
- `AC-PTM-02`: each child reports two distinct native worker TIDs.
- `AC-PTM-03`: the process-tree response includes readable labels and command
  summaries, not PID-only nodes.
- `AC-PTM-04`: the stopped worker's mixed stack includes `rust_inner`, the
  Python worker frame, and the expected process/thread identity.
- `AC-PTM-05`: continuing one worker does not resume or erase a sibling worker
  or sibling child.
- `AC-PTM-06`: child exit removes only that child subtree.
- `AC-PTM-07`: existing Python async and Rust async acceptance commands still
  pass and no task hierarchy is introduced.

Completion requires the new command plus:

```bash
./scripts/accept-first-slice.sh
./scripts/accept-reverse-slice.sh
./scripts/accept-thread-slice.sh
./scripts/accept-rust-thread-slice.sh
./scripts/accept-multiprocess-slice.sh
./scripts/accept-rust-multiprocess-slice.sh
./scripts/accept-async-slice.sh
./scripts/accept-rust-async-slice.sh
npm run --prefix vscode-extension compile
```

Update ADR/acceptance/manual documentation and `.vscode/launch.json`.
Do not add debugpy, Python breakpoints, Python task enumeration, arbitrary
process inspection, multiprocess stepping, or a fake nested DAP Threads list.
Do not commit unless explicitly requested.
