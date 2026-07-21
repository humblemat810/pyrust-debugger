# Form Copy

Use this as the starting point for the submission form. Before submitting,
compare each field with the live form and its character limits.

## Title

```text
PyRust: Mixed Python and Rust Debugging in VS Code
```

## Tagline

```text
One honest debugging view for CPython 3.14, PyO3, Rust threads, and processes.
```

## Short Description

```text
PyRust is a VS Code debugging prototype for CPython 3.14 and PyO3 applications. It merges CodeLLDB native frames with read-only Python stack recovery, then shows real process and native-thread ownership without inventing false async or caller relationships.
```

## Full Description

```text
PyRust tackles a frustrating systems-debugging gap: Python applications often
call into Rust through PyO3, while Rust applications can embed Python and call
back into Rust. At a native breakpoint, conventional tooling exposes the Rust
stack but commonly loses the Python context that led there. Concurrent code
makes this worse because processes, Python threads, Rust threads, and async
tasks can be confused for each other.

PyRust runs one DAP session through CodeLLDB and augments stopped native stacks
with read-only CPython 3.14 frame recovery. In VS Code, the normal Call Stack
shows the selected mixed Rust/Python frame sequence. A companion PyRust Process
Tree shows actual process and native-thread ownership, keeping sibling OS
threads as siblings and refusing to invent a hierarchy for asyncio tasks or
Rust futures.

The demo starts Python worker threads that enter a PyO3 extension. Rust then
creates named Rust worker threads, and PyRust stops at a Rust source
breakpoint. The user can inspect the Call Stack, expand the corresponding
Process Tree thread, and navigate back to the Rust source. The key correctness
rule is visible: a Rust OS thread is not falsely rendered as a child Python
call frame.

PyRust is intentionally an alpha Developer Tools prototype. It targets Linux
x86_64, CPython 3.14, and the pinned CodeLLDB environment. It supports Rust
breakpoints, opt-in debugpy Python breakpoints, and read-only Python snapshots
at Rust-owned stops. Cross-language stepping is not supported.
```

## Built With

```text
Codex, GPT-5.6, Python 3.14, Rust, PyO3, CodeLLDB, LLDB, VS Code, DAP, Docker Dev Containers
```

## How Codex Was Used

```text
Codex was the implementation partner for research, fixture design, Python and
Rust integration, DAP proxy work, VS Code extension work, Dev Container setup,
test automation, debugging, and documentation. The project keeps that work
auditable through commits, ADRs, fixture-bound acceptance criteria, and a
repeatable verification command. Codex accelerated implementation, while the
black-box DAP tests and manual VS Code runbook remain the source of behavioral
evidence.
```

## Repository Evidence

```text
Run ./scripts/verify-submission.sh in the Dev Container. It proves:
- Python -> Rust mixed stacks
- Rust -> Python -> Rust callback stacks
- Python and Rust worker-thread behavior
- Process/thread lifecycle and async non-nesting
- VS Code extension compilation and VSIX packaging

For a Docker-backed clean container rebuild and acceptance pass, run:
PYRUST_VERIFY_CONTAINER=1 ./scripts/verify-submission.sh
```

## Suggested Tags

```text
developer-tools, vscode, debugging, rust, python, pyo3, dap, codex
```

## Form Review

- [ ] Verify the live category/track selection is **Developer Tools**.
- [ ] Verify whether the form requires a public repository, public video, or
  additional license declaration.
- [x] Repository license: Apache-2.0.
- [ ] Verify whether the form has a required disclosure for GPT-5.6 and Codex.
- [ ] Trim the full description only after preserving the limitations paragraph.
