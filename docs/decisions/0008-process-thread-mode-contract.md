# ADR 0008: Process and Thread Mode Payload Contract

## Status

Accepted.

## Date

2026-07-20.

## Context

ADR 0007 added a coordinator-owned VS Code tree for real process and native
thread ownership. The next fixture needs to show a Rust parent, Python child
processes, and multiple native worker threads per child. A PID alone is not
enough for a human to identify the launched process.

## Decision

`pyrust/processTree` returns a bounded, coordinator-owned snapshot. Each
process has this shape:

```json
{
  "processId": 123,
  "parentProcessId": 100,
  "label": "process-A",
  "role": "Python child process",
  "command": ".venv/bin/python tests/acceptance/process_thread_worker.py process-A",
  "isActive": true,
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

The command is a concise launch summary. It comes only from DAP launch
arguments or an explicit child-registry record. PyRust does not scan arbitrary
live processes, read remote memory for command lines, or infer ancestry from
scheduling.

The custom tree renders only:

```text
parent process
  direct native thread
  child process
    direct native thread
```

Threads are never parents. Sibling processes and sibling threads are never
nested. `asyncio` tasks and Rust futures remain activity on the owning native
thread; PyRust does not create task or await-graph nodes.

The standard DAP Threads and Call Stack responses remain unchanged. The Call
Stack continues to present the selected Rust/Python caller/callee stack.

## Consequences

- Humans can identify process nodes by role, label, PID, and launch summary.
- The adapter retains a bounded data source with no new process-inspection
  permission or privacy surface.
- The fixture and acceptance tests can verify true process ancestry and
  direct native-thread ownership without depending on UI text parsing.
