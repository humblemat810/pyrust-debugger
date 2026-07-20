# debugpy at a Native Stop

## Question

Can debugpy provide Python thread and frame data while CodeLLDB has externally
stopped the same CPython process in Rust?

## Environment

```text
CPython 3.14.6
debugpy 1.8.20 bundled with ms-python.debugpy 2026.6.0
Linux x86_64
localhost TCP attach
```

## Method

1. Start a CPython process that calls `debugpy.listen()`.
2. Attach a DAP client and complete `initialize`, `attach`, and
   `configurationDone`.
3. Verify `threads` returns `MainThread`.
4. Send the debuggee `SIGSTOP`, representing an external native debugger stop.
5. Request `threads` again with a two-second deadline.

## Result

The initial `threads` request succeeded. The request after `SIGSTOP` timed
out. debugpy requires its in-process server to run in order to answer that DAP
request.

## Decision Impact

debugpy is not a replacement for CPython's external reader at CodeLLDB-owned
Rust stops. PyRust must use the reader for native-stop Python stacks and
primitive locals. debugpy remains useful when Python owns the stop, including
Python breakpoints, Python stepping, rich variables, and Python
multiprocessing instrumentation.

