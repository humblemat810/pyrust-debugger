# Research Report: CPython 3.14

## Finding

CPython 3.14 makes external stack reconstruction practical enough to be the
primary design, not merely a fallback.

The target interpreter publishes runtime and layout metadata that an external
tool can discover and use to read interpreter state. The standard CPython build
also contains the private `_remote_debugging` module with a `RemoteUnwinder`
implementation.

## Relevant 3.14 facilities

### Debug-offset metadata

The remote debugging protocol documents how an external tool:

1. locates the Python executable or `libpython` in the process map;
2. locates the `.PyRuntime`/platform-equivalent binary section;
3. reads runtime and interpreter structures;
4. follows thread states and frame data using exported offsets.

This is better than hard-coding CPython structure layouts. The target describes
the offsets needed to interpret its own memory.

### `_remote_debugging.RemoteUnwinder`

In CPython 3.14.6:

```python
RemoteUnwinder(
    pid,
    *,
    all_threads=False,
    only_active_thread=False,
    debug=False,
)
```

`get_stack_trace()` returns `ThreadInfo` values containing:

- OS thread ID;
- `FrameInfo` values;
- source filename;
- line number;
- function name.

Frames are most-recent first.

The implementation reads the target process's memory; it does not inject code
or wait for the target's Python interpreter to run.

### `sys.remote_exec`

PEP 768 adds safe external code scheduling. It is useful for future breakpoint
or tracing setup, but it is not a stack-collection mechanism for a process
currently stopped in Rust. The target must reach a safe Python evaluation point
before the injected script executes.

For the stack-only MVP, `RemoteUnwinder` is the correct tool.

## Local experiment

Environment:

```text
CPython 3.14.6
Linux x86_64
standard GIL build
```

The prototype launched a child with:

```text
<module> -> outer -> inner -> time.sleep
```

Observed remote stack:

```text
inner
outer
<module>
```

The same result was returned:

- while the process was running;
- after the process received `SIGSTOP`.
- while GDB held a Python -> Rust process at a Rust source breakpoint;
- while CodeLLDB held both Python -> Rust and Rust -> Python processes stopped.

Automated coverage lives in
`prototype/python/tests/test_unwinder.py`.

Full debugger observations are in
[the fixture results](fixture-results.md).

## Compatibility analysis

### Private API

The leading underscore is meaningful: this is not a stable public Python API.
The implementation can change without the compatibility guarantees expected of
documented standard-library interfaces.

### Shape changes after 3.14

CPython 3.14.6 returns a flat sequence of `ThreadInfo`. CPython's current
development source groups threads by interpreter and has additional constructor
options for native markers, GC frames, caching, and statistics.

The project should:

- pin the helper runtime to the target CPython minor version;
- normalize all CPython return values into project-owned data classes;
- feature-detect fields rather than importing them throughout the adapter;
- reject incompatible targets with a native-only fallback;
- keep the CPython bridge in one replaceable process/module.

### Patch releases

The project should test every supported 3.14 patch release. Even if the public
debug-offset format remains readable, the private Python wrapper may change.

### Free-threaded builds

CPython's source contains explicit free-threaded branches. They should be a
separate compatibility target. The first release should detect and reject an
untested free-threaded target rather than claim support.

### Subinterpreters

The private `RemoteUnwinder` result shape does not expose interpreter identity.
The implemented memory reader compensates by traversing CPython's exported
interpreter/thread-state offsets and matching the selected function and source
path when one native TID has multiple states. Stack display and snapshots are
proven for a subinterpreter-safe Rust extension. Live debugpy operations remain
main-interpreter-only and fail closed for a secondary interpreter.

## Permissions

Remote process-memory access is platform constrained:

- Linux commonly applies ptrace/Yama policy;
- containers may require `SYS_PTRACE`;
- macOS usually requires debugger entitlements or elevated privileges;
- Windows may require administrative/debug privileges.

The MVP's Linux launch topology makes the proxy an ancestor of the debuggee,
which is favorable under common Yama settings, but this must be tested with
LLDB attached.

## Recommendation

Use `_remote_debugging.RemoteUnwinder` for the 3.14 alpha and keep it isolated
behind the `read_python_stacks(pid)` interface.

Before beta, decide between:

1. continuing to pin and adapt the CPython helper;
2. vendoring a small external reader based on CPython's debug-offset format;
3. proposing or consuming a supported upstream interface.

## Sources

- CPython 3.14.6 implementation:
  https://github.com/python/cpython/blob/v3.14.6/Modules/_remote_debugging_module.c
- CPython remote debugging protocol:
  https://docs.python.org/3.14/howto/remote_debugging.html
- PEP 768:
  https://peps.python.org/pep-0768/
- Python 3.14 release schedule:
  https://peps.python.org/pep-0745/
- CPython development implementation, useful for change tracking:
  https://github.com/python/cpython/tree/main/Modules/_remote_debugging
