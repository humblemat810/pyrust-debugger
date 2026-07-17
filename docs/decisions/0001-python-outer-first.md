# ADR 0001: Implement Python-Outer First

## Status

Accepted.

## Decision

The first supported mixed stack is:

```text
CPython 3.14 -> PyO3 Rust extension
```

The debugger will be native-first. CodeLLDB controls execution and the proxy
adds Python frames through CPython 3.14 remote unwinding.

## Rationale

- It tests the shared hard problem without embedded-interpreter setup.
- A PyO3 extension is a compact deterministic fixture.
- CodeLLDB can debug the Rust shared library normally.
- CPython 3.14 can be remotely unwound while stopped.
- The resulting merge engine is reusable for Rust embedding Python.
- A single execution controller is much simpler than coordinating debugpy and
  LLDB.

## Consequences

Positive:

- smallest credible stack-only MVP;
- no Python tracing overhead;
- graceful native-only fallback;
- fast path to user-visible proof.

Negative:

- Python breakpoints and locals are absent initially;
- CPython 3.14 is mandatory;
- CodeLLDB packaging and a private CPython helper API are dependencies;
- reverse-direction launch support waits until phase two.

## Alternatives considered

- Rust-outer first: rejected for initial work due to embedding setup.
- debugpy plus CodeLLDB immediately: deferred due to control-plane complexity.
- fork CodeLLDB: reserved as fallback.
- LLDB scripted frame provider: deferred until available in the supported
  backend.
- maintain a `sys.monitoring` shadow stack: retained only as a fallback because
  CPython 3.14 remote unwinding has no target-side overhead.
