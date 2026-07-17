# Research Fixture Results

## Purpose

Two minimal programs were built to replace architectural assumptions with
observed debugger behavior:

```text
Python -> Rust
Rust -> Python
```

These fixtures and the DAP probe are research tools only. They do not implement
mixed-stack presentation.

## Environment

Test date: July 17, 2026.

| Component | Version |
| --- | --- |
| OS | Ubuntu 24.04, Linux x86_64 |
| CPython | 3.14.6 |
| Rust | 1.97.1 |
| PyO3 | 0.29.0 |
| CodeLLDB | 1.12.2 |
| CodeLLDB bundled LLDB | 22.1.4-codelldb |
| System LLDB | 18.1.3 |
| GDB | 15.1 |
| py-spy | 0.4.2 |
| Yama `ptrace_scope` | 1 |

The system LLDB 18 package hung during launch, including a `/bin/true` control
case. CodeLLDB's bundled LLDB launched and stopped both fixtures correctly.
This is an environment-specific result, but it supports using CodeLLDB's
bundled backend as the research baseline.

## Fixture A: Python outer, Rust inner

Source:

```text
research/fixtures/python_outer
```

Call shape:

```text
app.py::<module>
  -> python_outer
     -> python_inner
        -> PyO3 trampoline
           -> rust_outer
              -> rust_inner
```

Normal result:

```text
python -> rust result: 42
```

### Breakpoint behavior

A source breakpoint at `lib.rs:6` was initially returned by CodeLLDB as pending
and unverified because the extension module was not loaded yet:

```text
Resolved locations: 0
```

After Python imported the extension, LLDB added locations and stopped in:

```text
pyrust_native::rust_inner(value=20)
```

The pending breakpoint therefore works, although the adapter's initial response
does not communicate its eventual resolution.

### Native stack

The important regions were:

```text
rust_inner
rust_outer
PyO3 generated function/trampoline frames
CPython call machinery
CPython evaluation and script startup
native process startup
```

CodeLLDB returned instruction-pointer references and stable native frame IDs for
the Rust frames.

### Python stack while CodeLLDB was stopped

The CPython 3.14 helper returned:

```text
python_inner  app.py:5
python_outer  app.py:9
<module>      app.py:13
```

The helper succeeded while CodeLLDB owned the stopped process with
`ptrace_scope=1`.

## Fixture B: Rust outer, Python inner

Source:

```text
research/fixtures/rust_outer
```

Call shape:

```text
Rust main
  -> rust_outer
     -> PyO3 attach/from_code
        -> embedded.py::<module>
           -> python_outer
              -> python_inner
                 -> signal.raise_signal
```

The deliberate signal is only a research stop point. Outside a debugger the
program exits with `SIGTRAP`.

### Breakpoint behavior

CodeLLDB used a pending function breakpoint on:

```text
signal_raise_signal_impl
```

It resolved when `libpython3.14` loaded and stopped before the signal was raised.

### Native stack

The important regions were:

```text
signal_raise_signal_impl
CPython call/evaluation machinery
CPython module execution
PyO3 PyModule::from_code
rust_outer closure
Python::attach
rust_outer
Rust main
native process startup
```

This confirms that outer Rust user frames remain unwindable below active
CPython execution.

### Python stack while CodeLLDB was stopped

The CPython helper returned:

```text
python_inner  embedded.py:5
python_outer  embedded.py:9
<module>      embedded.py:12
```

The source path was preserved as an absolute workspace path.

## DAP observations

### Thread identity

For both single-thread fixtures:

```text
CodeLLDB stoppedEvent.threadId
  == CodeLLDB threads[].id
  == LLDB OS tid
  == CPython RemoteUnwinder thread_id
```

This resolves the basic thread-mapping question for Linux/CodeLLDB. It must
still be tested with multiple threads before becoming a general guarantee.

### PID discovery

CodeLLDB did not emit a DAP `process` event for either launch. The optional
`systemProcessId` path cannot be assumed.

The research probe obtained the PID by sending:

```text
evaluate(context="repl", expression="process status")
```

CodeLLDB emitted the result through DAP `output` events:

```text
Process <pid> stopped
```

The planned adapter needs a supported PID source. Parsing console output is
acceptable for a spike but brittle for a product. Better options are:

- request a small CodeLLDB custom API;
- contribute a `process` event upstream;
- use a launch wrapper that reports its child PID;
- retain console parsing as a version-gated fallback.

### Stack response

CodeLLDB supplies:

- native frame IDs;
- instruction pointer references;
- source paths and lines for Rust/PyO3 frames;
- module IDs;
- `subtle` presentation hints for many runtime frames;
- delayed stack-trace capability.

These fields are sufficient for a proxy to preserve native frame identity while
inserting synthetic Python frames.

### Scripted frame provider

A mock LLDB `ScriptedFrameProvider` prepended:

```text
[research mock] python_inner  app.py:5
```

CodeLLDB's DAP stack then continued with:

```text
pyrust_native::rust_inner  lib.rs:6
pyrust_native::rust_outer  lib.rs:13
```

This proves that CodeLLDB needs no adapter modification to display mixed source
frames generated inside LLDB.

CodeLLDB's scripting runtime is Python 3.12.7 and does not contain
`_remote_debugging`. Invoking the external 3.14 helper from inside LLDB failed
to locate the target's `PyRuntime` in the tested topology. Real stack
acquisition therefore still needs a target-memory reader or an IPC design.

## py-spy comparison

`py-spy --native` was run against the actual Python -> Rust fixture:

```text
Samples: 99
Errors: 0
```

The samples contained Python:

```text
<module> -> python_outer -> python_inner
```

followed by native Rust/PyO3 symbols from the extension. This confirms that the
closest prior-art merge technique operates on the selected CPython 3.14 and
PyO3/Rust combination, not only on C extensions.

## Direction comparison from observed behavior

| Question | Python -> Rust | Rust -> Python |
| --- | --- | --- |
| Minimal program works | Yes | Yes |
| Native user frames visible | Yes | Yes |
| Python logical frames remotely readable | Yes | Yes |
| Thread IDs match in tested case | Yes | Yes |
| Natural user stop for stack-only MVP | Rust source breakpoint | No Python source stop without Python debugger/instrumentation |
| Extra runtime deployment | None beyond extension | `libpython` discovery/linking |
| Dynamic breakpoint issue | Rust extension breakpoint starts pending | Python/runtime breakpoint starts pending |
| Mixed-stack insertion shape | Rust user frames above Python callers | Python frames between CPython and outer Rust |

## Recommendation after experiments

Choose **Python outer, Rust inner**.

The decisive reason is not only setup cost. It supports a useful native-first
MVP: the user hits a Rust source breakpoint while the Python caller stack is
still active.

For Rust outer, Python inner, an outer Rust breakpoint normally occurs before
or after Python runs, so there is no active Python stack to display. Stopping at
a user Python line requires debugpy, monitoring-based breakpoints, or another
Python execution controller. That moves the reverse direction beyond a
stack-only MVP.

## Reproduction

Build commands are in:

```text
research/README.md
```

The CodeLLDB DAP probe is:

```text
research/tools/codelldb_dap_probe.py
```

Generated JSON transcripts are intentionally ignored because they contain
run-specific PIDs and instruction addresses.
