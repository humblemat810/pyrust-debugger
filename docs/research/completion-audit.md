# Research Phase Completion Audit

## Scope

This audit covers the requested research phase only. No mixed debugger,
production DAP proxy, CPython memory reader, or VS Code extension has been
implemented.

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
