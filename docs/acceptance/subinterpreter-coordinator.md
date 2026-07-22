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

## AC-DP-30

At the Python breakpoint in `subinterpreter_payload.py`:

1. The secondary interpreter stops directly on
   `interpreter_label = "secondary-interpreter"`.
2. The stop is owned by the interpreter-local Python engine, not debugpy.
3. The top frame reports the real payload source path and configured line.
4. Evaluating `value * 2` returns `70`.
5. Step Over reaches the following `native_result` line on the same native
   TID.
6. Continue releases the lease and lets the fixture terminate normally.

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
3. Set a Python breakpoint at
   `tests/acceptance/subinterpreter_payload.py:23`.
4. Start debugging. Confirm `value * 2` returns `70`, then Step Over to line
   24 and Continue.
5. Run again with a Rust breakpoint at
   `research/fixtures/subinterpreter_outer/src/lib.rs:48`.
6. At `calculate_leaf`, select
   `subinterpreter_worker` in the mixed stack.
7. The stack refreshes to a live Python-owned stop. Evaluate `value * 2`,
   execute `import math`, then evaluate `math.factorial(5)`.
8. Change local `value` to `41`, then use Step Over, Step Into, and Step Out.
   The Python leaf is `finalize_subinterpreter`, and every stop remains on the
   same native thread.

## Backend Boundary

debugpy 1.8.20 is not subinterpreter-safe in the tested CPython 3.14.6
environment. PyRust does not import debugpy there. The interpreter-local engine
supports native-stop transfer and direct Python source breakpoints for
same-thread initialization and string-based `_interpreters.exec`. It does not
yet guarantee source-breakpoint installation for arbitrary C-created
interpreters that migrate to an uninstrumented OS thread.
