# Containerized VS Code Acceptance

## Purpose

This contract completes ADR 0004 by proving that a clean Linux Dev Container
can run both debugger directions through an installed local VS Code extension.

## Fixed Environment

```text
Host: Docker Engine or Docker Desktop with Dev Containers support
Container: Ubuntu 24.04 Linux x86_64
CPython 3.14.6
CodeLLDB 1.12.2 with bundled LLDB
Rust 1.97.1
Pinned Node.js LTS version from the container definition
Recorded VS Code version for extension-host tests
VS Code Dev Containers
One container for adapter, CodeLLDB, helper, and debuggee
```

Native ARM64 or emulated x86_64 environments are not accepted by this
contract.

## Required Host Command

The implementation must provide:

```bash
./scripts/accept-container.sh
```

It must build a fresh environment, run bounded checks, print every automated
criterion below, and exit nonzero on failure.

## Automated Criteria

### AC-CV-01: Clean Container Build

The container builds without a host `.venv`, host Rust target directory,
host-installed CodeLLDB, or an already-running project container. Python and
Cargo build state must use container-owned paths or volumes.

### AC-CV-02: Fixed Toolchain

Inside the container:

```text
.venv/bin/python --version == Python 3.14.6
CodeLLDB == 1.12.2
rustc == 1.97.1
node == the version pinned by the container definition
architecture == x86_64
```

Python, Rust, and Node installation commands must honor their committed lock
files.

### AC-CV-03: Bounded Debugger Privileges

The debugger can trace the fixed child process with `SYS_PTRACE`. The container
must not use privileged mode, the host PID namespace, or a mounted Docker
socket.

### AC-CV-04: VS Code Extension Registration

The local extension builds successfully, contributes the `pyrust` debug type,
and launches the Python DAP adapter through a registered adapter descriptor.

### AC-CV-05: Explicit CodeLLDB Resolution

The adapter starts the pinned CodeLLDB and bundled `liblldb` through explicit
configured paths. Acceptance must not depend only on scanning a mutable
extension directory.

### AC-CV-06: Python-Outer Regression

Inside the container:

```bash
./scripts/accept-first-slice.sh
```

reports every ADR 0002 criterion as passing.

### AC-CV-07: Rust-Outer Regression

Inside the container:

```bash
./scripts/accept-reverse-slice.sh
```

reports every ADR 0003 criterion as passing.

### AC-CV-08: Extension-Host Smoke Test

A VS Code extension test installs the packaged VSIX through VS Code's
extension-management CLI, starts a `pyrust` session, reaches an initialized
adapter, launches one fixed fixture, and shuts down cleanly with a bounded
timeout.

### AC-CV-09: Fresh-State Repeatability

Destroying and rebuilding the development container must produce the same
passing result without manual file edits or copying a host environment into the
container.

### AC-CV-10: Clean Shutdown

After acceptance completes:

- no CodeLLDB, proxy, helper, or debuggee process remains;
- the development container can stop normally;
- no generated debugger transcript or build artifact appears as an untracked
  source file.

## Human VS Code Criteria

### HC-CV-01: Python-Outer Call Stack

After `Dev Containers: Reopen in Container`, launch `PyRust: Python Outer`,
stop at `rust_inner`, and observe:

```text
rust_inner
rust_outer
python_inner
python_outer
```

If Docker is on a remote host, this command must be run from a VS Code window
that first opened the remote checkout through Remote-SSH. The repository
checkout and Docker daemon must be on that same remote host.

### HC-CV-02: Rust-Outer Call Stack

Launch `PyRust: Rust Outer`, stop at `rust_callback`, and observe:

```text
rust_callback
python_inner
python_outer
rust_outer
main
```

### HC-CV-03: Frame Interaction

For every required frame:

- clicking the frame opens the expected source and line;
- Rust scopes load through CodeLLDB;
- one deterministic Rust expression evaluates successfully;
- Python scopes are empty;
- Python evaluation reports unsupported without ending the session.

### HC-CV-04: Second Stop

Continue to the second callback stop. The mixed stack must be recollected, and
Python frames from the first stop must not remain selectable as current frames.

## Required Output

The host command must report:

```text
AC-CV-01 PASS
AC-CV-02 PASS
AC-CV-03 PASS
AC-CV-04 PASS
AC-CV-05 PASS
AC-CV-06 PASS
AC-CV-07 PASS
AC-CV-08 PASS
AC-CV-09 PASS
AC-CV-10 PASS
```

The human checklist must be recorded separately as:

```text
HC-CV-01 PASS
HC-CV-02 PASS
HC-CV-03 PASS
HC-CV-04 PASS
```

Use
[Containerized VS Code Manual Verification](containerized-vscode-manual.md)
as the test record.

Screenshots are useful supporting evidence but do not replace the automated DAP
and extension-host assertions.

## Not Accepted as Completion

- running acceptance only on the host;
- installing host `.venv` or Rust artifacts into the container workspace;
- using `--privileged` or `--pid=host`;
- manually starting CodeLLDB outside the container;
- using a second VS Code debug session for Python;
- only displaying raw CodeLLDB frames;
- only proving that the extension compiles;
- a container that requires undocumented manual repair after rebuild.
