# Risk Register

| ID | Risk | Likelihood | Impact | Mitigation | Gate |
| --- | --- | --- | --- | --- | --- |
| R1 | Remote unwinder cannot read while LLDB traces target | Low | Critical | Passed for both fixtures under CodeLLDB and `ptrace_scope=1`; retain CI coverage | Phase 1 |
| R2 | CodeLLDB DAP thread IDs do not map to OS IDs | Medium | Critical | Passed for single-thread fixtures; verify multithread behavior | Phase 1/4 |
| R3 | `_remote_debugging` API changes | High | High | Pin 3.14, isolate bridge, feature-detect, native fallback | Every release |
| R4 | CodeLLDB adapter path/invocation changes | Medium | High | Version range, self-test, override path, compatibility CI | Phase 2 |
| R5 | Native boundary heuristic misorders frames | Medium | High | Golden fixtures, preserve raw stack, visible-runtime option | Phase 3 |
| R6 | DAP paging produces missing/duplicate frames | Medium | Medium | Merge complete stack before paging; protocol unit tests | Phase 3 |
| R7 | Synthetic frame IDs collide or become stale | Medium | Medium | Proxy-owned allocator and stop epochs | Phase 3 |
| R8 | Process-memory permissions fail in containers | High | Medium | Detect errno, document `SYS_PTRACE`, native fallback | Phase 4 |
| R9 | Dynamic Rust extension symbols do not bind | Medium | High | Debug profile, pending breakpoints, module-load tests | Phase 1 |
| R10 | Helper latency makes stack UI feel broken | Medium | Medium | Cache per stop, persistent helper if measured necessary | Phase 4 |
| R11 | Multiple interpreters make thread/frame ownership ambiguous | Medium | High | Single-interpreter alpha; detect unsupported topology | Phase 4 |
| R12 | Free-threaded CPython behaves differently | Medium | High | Explicitly detect and reject until tested | Release |
| R13 | Two-adapter expansion deadlocks or double-continues | High | Critical | Separate future project; one execution owner in alpha | Future |
| R14 | Licensing contamination from studied GPL prior art | Low | High | Independent implementation; no copied code; retain source notes | Ongoing |
| R15 | Flattened `RemoteUnwinder` frames cannot represent repeated Python/native blocks | High | High | Use py-spy markers or its merged ordering as an oracle | Phase 1 |
| R16 | CodeLLDB launch omits DAP process PID | High | High | Structured upstream/custom API or launch wrapper; console parsing only as fallback | Phase 2 |
| R17 | Scripted provider cannot directly use CPython 3.14 unwinder | High | Medium | `SBProcess` reader or ancestor IPC; retain DAP bridge as default | Architecture checkpoint |

## Top three gates

1. Preserve correct frame order across repeated Python/native boundaries.
2. Obtain the debuggee PID through a product-quality structured path.
3. Verify thread identity with multiple Python/native threads.

The original process-access and single-thread identity gates passed. If a
remaining gate fails, do not compensate by adding debugpy; reassess the native
adapter integration first.
