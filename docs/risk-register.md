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
| R11 | Multiple interpreters make thread/frame ownership ambiguous | Low | High | Traverse all interpreter/thread-state lists; require one frame-identity match; reject secondary-interpreter debugpy handoff | AC-DP-29 |
| R12 | Free-threaded CPython behaves differently | Medium | High | Explicitly detect and reject until tested | Release |
| R13 | Two-adapter expansion deadlocks or double-continues | High | Critical | Separate future project; one execution owner in alpha | Future |
| R14 | Licensing contamination from studied GPL prior art | Low | High | Independent implementation; no copied code; retain source notes | Ongoing |
| R15 | Flattened `RemoteUnwinder` frames cannot represent repeated Python/native blocks | High | High | Use py-spy markers or its merged ordering as an oracle | Phase 1 |
| R16 | CodeLLDB launch omits DAP process PID | High | High | Structured upstream/custom API or launch wrapper; console parsing only as fallback | Phase 2 |
| R17 | Scripted provider cannot directly use CPython 3.14 unwinder | High | Medium | `SBProcess` reader or ancestor IPC; retain DAP bridge as default | Architecture checkpoint |
| R18 | Permanently hung in-process unwinds accumulate daemon threads | Medium | High | ADR 0003 circuit breaker implemented; at most one daemon remains per session; later move the reader to a killable ancestor process or LLDB | Passed for reverse slice |
| R19 | Rust-outer has no natural Python source stop under native-only control | Certain | High | Explicit Rust callback breakpoint implemented for the stack proof; require a separate ADR for Python breakpoints | Passed for stack-only proof |
| R20 | Reverse merge drops or misorders lower Rust embedder frames | Medium | High | Golden callback fixture, preserved native IDs, strict fixture symbols, and native-only fallback | Passed for reverse slice |
| R21 | Container cannot trace or read the debuggee | Low | Critical | One-container ancestry, `SYS_PTRACE`, explicit preflight, native-only failure diagnostics | Automated pass |
| R22 | Broad container privileges create a false sense of sandboxing | Medium | High | No privileged mode, host PID namespace, or Docker socket; document unconfined seccomp as local-proof only | Automated pass |
| R23 | Host `.venv` or build artifacts contaminate the container result | Low | High | Fresh-state build, container-created `.venv`, artifact checks, repeated clean rebuild | Automated pass |
| R24 | CodeLLDB extension version or discovery path drifts | Low | High | Pin 1.12.2 and support explicit adapter and `liblldb` paths | Automated pass |
| R25 | DAP tests pass but the VS Code debug type or UI workflow is broken | Medium | High | Local wrapper extension, extension-host smoke test, and human Call Stack checklist | Human check passed |
| R26 | Host VS Code, Dev Containers, or extension-test versions drift | Low | High | Record tested versions, pin the extension-host runner, and report host preflight versions | Automated pass |
| R27 | Primitive local snapshot is unavailable or stale at a Python frame | Medium | Medium | Match frame name/path at the current stop; show locals unavailable on mismatch; retain native fallback and stale-ID checks | ADR 0005 acceptance |

## Top three gates

1. Preserve correct frame order across repeated Python/native boundaries.
2. Obtain the debuggee PID through a product-quality structured path.
3. Verify thread identity with multiple Python/native threads.

The original process-access and single-thread identity gates passed. If a
remaining gate fails, do not compensate by adding debugpy; reassess the native
adapter integration first.

## ADR 0003 gates

1. A timed-out in-process reader creates at most one abandoned worker per proxy
   session.
2. The reverse stack preserves both the upper Rust callback and lower Rust
   embedder frames.
3. Unknown reverse boundaries return the original native stack rather than a
   guessed ordering.

## ADR 0004 gates

1. A clean container reproduces both existing acceptance commands. Passed.
2. The local extension starts `pyrust` through VS Code inside the container.
   Passed under VS Code 1.125.0.
3. Debugger permissions remain narrower than privileged or host-PID access.
   Passed.
4. Both required mixed stacks are visible and interactive in the VS Code Call
   Stack panel. Passed on 2026-07-20.

## ADR 0005 gates

1. The reader remains read-only and bounded while obtaining primitive locals.
2. Both fixture directions expose `value = 20` and evaluate `value + 1 = 21`.
3. Calls and other unsafe expressions are rejected locally, without target
   execution.
