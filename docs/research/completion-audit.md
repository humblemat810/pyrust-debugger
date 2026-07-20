# Research Phase Completion Audit

## Scope

This is the historical audit from the end of the requested research phase. At
that point no mixed debugger, production DAP proxy, CPython memory reader, or
VS Code extension had been implemented.

Later on 2026-07-17, ADR 0002 and ADR 0003 implemented fixture-bound DAP proofs
for both Python-to-Rust and Rust-to-Python-to-Rust callback stacks. Those later
results do not change whether the earlier research deliverables were complete.

| Requirement | Evidence | Status |
| --- | --- | --- |
| Study Python outer, Rust inner | `research/fixtures/python_outer`, CodeLLDB and remote-unwinder results | Complete |
| Study Rust outer, Python inner | `research/fixtures/rust_outer`, CodeLLDB and remote-unwinder results | Complete |
| Choose the easier initial direction | `docs/analysis-report.md`, ADR 0001, fixture comparison | Complete: Python outer |
| Provide overall analysis | `docs/analysis-report.md` | Complete |
| Provide planning documents | project plan, MVP, architecture, test plan, risk register | Complete |
| Provide research reports | CPython, DAP, native debugger, prior art, exact search, fixture results | Complete |
| Search for exact successful implementation | `docs/research/exact-prior-art-search.md` | Complete |
| Set up Python 3.14 environment | `.python-version`, `pyproject.toml`, local `.venv` at 3.14.6 | Complete |
| Set up Rust/native debugger environment | Rust 1.97.1, GDB 15.1, CodeLLDB 1.12.2, bundled LLDB 22.1.4 | Complete |
| Build Python -> Rust hello world | returns `42`; Rust breakpoint binds and stops | Complete |
| Build Rust -> Python hello world | embedded Python runs; native stop retains outer Rust frames | Complete |
| Observe Python stacks while native debugger owns stop | CPython 3.14 unwinder succeeds in both CodeLLDB cases | Complete |
| Verify CodeLLDB thread mapping | DAP thread ID equals CPython OS thread ID in both single-thread fixtures | Complete with documented multithread limitation |
| Check prior-art merge on actual Rust extension | py-spy native mode: 99 samples, 0 errors | Complete |
| Check LLDB synthetic-frame route | mock Python source frame appears in CodeLLDB DAP stack | Complete |
| Avoid implementing the solution | only fixtures, diagnostic probes, and a mock provider exist | Complete |
| Ignore generated artifacts | `.gitignore` covers venv, targets, crash dumps, caches, and transcripts | Complete |

## Post-Research Implementation

The current implementation is verified by:

```bash
./scripts/accept-first-slice.sh
./scripts/accept-reverse-slice.sh
./scripts/accept-container.sh
```

The second command verifies the unwinder circuit breaker and the required
reverse user-stack subsequence:

```text
rust_callback
python_inner
python_outer
rust_outer
main
```

ADR 0004 adds a local fixture-bound VS Code extension and pinned Linux Dev
Container. Its final clean two-lifecycle automated acceptance passed
`AC-CV-01` through `AC-CV-10` on 2026-07-20. This remains a fixed,
single-thread, stack-only proof. It does not provide Python breakpoints,
Python evaluation, arbitrary boundary ordering, Marketplace packaging, or
product-quality PID discovery. Human VS Code criteria `HC-CV-01` through
`HC-CV-04` also passed on 2026-07-20.

To unblock those criteria when this checkout is on a remote machine, use:

```text
local VS Code desktop
  -> Remote-SSH to the Linux Docker host
  -> open the checkout on that host
  -> Dev Containers: Reopen in Container
  -> complete HC-CV-01 through HC-CV-04
```

Local Docker is not required. The checkout and Docker daemon must be on the
same remote host. Follow the prerequisite checks, commands, and troubleshooting
steps in
[Containerized VS Code Manual Verification](../acceptance/containerized-vscode-manual.md).

## Verified commands

```bash
PYTHONPATH=prototype/python \
  .venv/bin/python -W error::ResourceWarning \
  -m unittest discover -s prototype/python/tests -v

cargo check --manifest-path research/fixtures/python_outer/Cargo.toml
cargo check --manifest-path research/fixtures/rust_outer/Cargo.toml

.venv/bin/python research/tools/codelldb_dap_probe.py python-outer
.venv/bin/python research/tools/codelldb_dap_probe.py rust-outer

.venv/bin/python research/tools/codelldb_dap_probe.py \
  python-outer --mock-frame-provider
```

## Final research decision

Focus implementation on:

```text
CPython 3.14 -> Rust extension
```

Reasons proven by the fixtures:

- a Rust source breakpoint is a natural user stop with active Python callers;
- CodeLLDB preserves the required Rust frames and source locations;
- CPython exposes the logical Python stack while CodeLLDB owns the stop;
- thread IDs align in the tested Linux topology;
- no embedded-interpreter deployment is required.

Defer:

```text
Rust application -> embedded CPython
```

Although its stack is readable, a native-only debugger has no natural user
Python source breakpoint. A useful reverse-direction product requires a Python
breakpoint engine or deliberate instrumentation in addition to stack display.
