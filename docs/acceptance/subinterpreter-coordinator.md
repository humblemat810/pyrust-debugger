# Subinterpreter Coordinator Acceptance

## Scope

This contract verifies CPython 3.14 processes where one native OS thread has
thread states in both the main interpreter and a secondary interpreter.

The Rust fixture uses `pyo3-ffi` multi-phase initialization with
`Py_MOD_PER_INTERPRETER_GIL_SUPPORTED`. PyO3 0.29 modules themselves reject
subinterpreter loading, so they cannot truthfully provide this fixture.

## AC-DP-29

At the Rust `calculate_leaf` breakpoint:

1. CodeLLDB stops on the secondary-interpreter worker's native TID.
2. The merged stack contains `subinterpreter_worker` above the Rust frames.
3. Snapshot locals contain:

```text
interpreter_label = "secondary-interpreter"
value = 35
```

4. The memory reader chooses the thread state whose active frame matches that
   function and source path, rather than the parked same-TID state in the main
   interpreter.
5. Selecting `subinterpreter_worker` transfers that exact interpreter and
   native TID to the interpreter-local live engine.
6. Live scopes expose `interpreter_label` and `value`.
7. Arbitrary evaluation, a REPL `import`, retained module state, and
   write-through local assignment succeed.
8. `next`, Python-to-Python `stepIn`, `stepOut`, and continue remain on the
   same native TID and preserve live values.

## Commands

```bash
PYTHONPATH=prototype/python .venv/bin/python -m unittest \
  prototype.python.tests.test_locals \
  prototype.python.tests.test_remote_debug \
  prototype.python.tests.test_live_lease

./scripts/accept-debugpy-slice.sh
```

## Manual VS Code Check

1. Build or reinstall the current VSIX.
2. Select **PyRust: Python Subinterpreter (live)**.
3. Set a Rust breakpoint at
   `research/fixtures/subinterpreter_outer/src/lib.rs:48`.
4. Start debugging. At `calculate_leaf`, select
   `subinterpreter_worker` in the mixed stack.
5. The stack refreshes to a live Python-owned stop. Evaluate `value * 2`,
   execute `import math`, then evaluate `math.factorial(5)`.
6. Change local `value` to `41`, then use Step Over, Step Into, and Step Out.
   The Python leaf is `finalize_subinterpreter`, and every stop remains on the
   same native thread.

## Backend Boundary

debugpy 1.8.20 is not subinterpreter-safe in the tested CPython 3.14.6
environment. PyRust does not import debugpy there. The interpreter-local engine
is live but begins from a native stop; it does not yet implement Python source
breakpoints inside the secondary interpreter.
