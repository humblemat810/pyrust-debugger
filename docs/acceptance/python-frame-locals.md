# Python Frame Locals Acceptance

## Purpose

This contract validates ADR 0005's read-only Python local snapshots for the
two fixed mixed-stack fixtures.

## Supported Environment

```text
Linux x86_64
CPython 3.14.6, non-free-threaded
CodeLLDB owns and has stopped the debuggee
Python -> Rust and Rust -> embedded Python -> Rust callback fixtures
```

## Automated Criteria

### AC-PF-01: Primitive Local Reader

The CPython memory reader must capture newest-first frames from a child Python
3.14 process and expose deterministic primitive locals:

```text
inner: value = 20, label = "remote", enabled = True, ratio = 1.5
outer: marker = 7
```

### AC-PF-02: Python-Outer DAP Interaction

At the `rust_inner` stop, `python_inner` must expose a `Python Locals` scope
with `value = 20`. Evaluating `value + 1` on that frame must return `21`.

### AC-PF-03: Rust-Outer DAP Interaction

At the `rust_callback` stop, `python_inner` must expose a `Python Locals`
scope with `value = 20`. Evaluating `value + 1` on that frame must return
`21`.

### AC-PF-04: No Target Execution

An expression containing a call, such as `__import__('os')`, must fail with a
clear local evaluation error. It must not be forwarded to CodeLLDB or execute
in the debuggee.

### AC-PF-05: Frame and Epoch Safety

Native CodeLLDB frame IDs remain usable. Synthetic frame and local scope IDs
are accepted only for the current stop epoch; stale IDs fail cleanly.

### AC-PF-06: Degradation

If the local reader cannot obtain a matched frame snapshot, the synthetic
frame remains navigable and reports its local state as unavailable. Existing
unwinder failures still return the unmodified native stack.

## Required Commands

```bash
PYTHONPATH=prototype/python .venv/bin/python -m unittest discover -s prototype/python/tests -v
./scripts/accept-first-slice.sh
./scripts/accept-reverse-slice.sh
```

The container gate remains:

```bash
./scripts/accept-container.sh
```

## Manual VS Code Check

Use [Containerized VS Code Manual Verification](containerized-vscode-manual.md)
and, in each launch configuration, select `python_inner`, expand `Python
Locals`, confirm `value = 20`, and evaluate `value + 1`.

