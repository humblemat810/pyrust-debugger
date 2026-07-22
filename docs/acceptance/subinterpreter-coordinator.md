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
5. Requesting live debugpy scopes returns an explicit subinterpreter error.
6. CodeLLDB remains stopped at `pyrust_subinterp::calculate_leaf`; the target
   does not resume, abort, or terminate.

## Commands

```bash
PYTHONPATH=prototype/python .venv/bin/python -m unittest \
  prototype.python.tests.test_locals \
  prototype.python.tests.test_remote_debug

./scripts/accept-debugpy-slice.sh
```

## Deliberate Limitation

debugpy 1.8.20 is not subinterpreter-safe in the tested CPython 3.14.6
environment. PyRust supports correct stack and snapshot ownership for the
active secondary interpreter, but it does not claim live Python evaluation,
assignment, breakpoints, or stepping there.
