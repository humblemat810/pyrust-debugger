# ADR 0004: Validate the Debugger in One Linux Dev Container

## Status

Implemented; human acceptance pending.

## Date

2026-07-18.

## Context

ADR 0002 and ADR 0003 prove both fixture-bound mixed-stack directions through
automated DAP clients:

```text
Python -> Rust
Rust -> Python -> Rust callback
```

The proofs do not yet provide a normal VS Code F5 workflow. The repository has
no extension manifest registering a `pyrust` debug type, no launch
configurations, and no reproducible container definition.

The debugger also depends on a sensitive Linux process topology:

- the proxy launches CodeLLDB;
- CodeLLDB launches the debuggee;
- the proxy reads the descendant CPython process;
- CodeLLDB requires `ptrace`;
- the fixed proof requires CPython 3.14.6 and CodeLLDB 1.12.2.

Testing this from an arbitrary host environment risks version drift, stale
build artifacts, missing process permissions, and false conclusions caused by
host configuration rather than debugger behavior.

## Decision

Build one reproducible Linux x86_64 VS Code Dev Container for development,
automated acceptance, and human Call Stack validation.

### Process Boundary

Use this topology:

```text
Host
  VS Code desktop UI
        |
        | Dev Containers connection
        v
Linux x86_64 container
  VS Code Server and extensions
  local pyrust extension
  PyRust DAP proxy
  CodeLLDB 1.12.2
  CPython 3.14.6 and Rust toolchain
  debuggee fixture
```

The VS Code desktop UI remains on the host. The extension host, adapter,
CodeLLDB, helper, and debuggee run inside one container.

Do not split CodeLLDB, the helper, and the debuggee into separate containers.
Keeping them together preserves the proven parent/descendant process topology
and avoids cross-container PID namespace and memory-access coordination.

### Container Definition

Add:

```text
.devcontainer/Dockerfile
.devcontainer/devcontainer.json
.devcontainer/bootstrap.sh
```

The environment will pin:

```text
Ubuntu 24.04 x86_64
CPython 3.14.6
CodeLLDB 1.12.2 with bundled LLDB
Rust 1.97.1
a pinned Node.js LTS version for extension development
```

The container must be buildable without using a host `.venv`, host Rust target
directory, or host-installed CodeLLDB. The `.venv` used by the fixed scripts
must be created inside the container. Generated container, Node, extension,
and build artifacts must be covered by `.gitignore`.

Use container-owned storage for environment and build state:

- mount a named volume at the workspace `.venv`;
- set `CARGO_TARGET_DIR` to a container-only path;
- make fixture launch code honor `CARGO_TARGET_DIR`;
- keep extension dependencies and output in container-owned volumes or ignored
  paths.

Use the existing Cargo lock files. Add and enforce lock files for Python tooling
and the Node extension dependencies before claiming reproducibility. Record the
VS Code version used by the extension-host test.

CodeLLDB installation must be version-pinned. The adapter must also gain an
explicit configured-path option so container startup does not depend only on
searching `~/.vscode-server/extensions`.

### Debugger Permissions

For this local proof, start the development container with:

```json
{
  "runArgs": [
    "--cap-add=SYS_PTRACE",
    "--security-opt=seccomp=unconfined"
  ]
}
```

Do not use:

- `--privileged`;
- `--pid=host`;
- a mounted Docker socket;
- host process namespaces.

`seccomp=unconfined` is accepted only for this fixed local debugger proof. A
later hardening task should replace it with the narrowest tested seccomp
profile.

This environment provides reproducible toolchain and process isolation. It is
not a sandbox for hostile debuggee code because `SYS_PTRACE`, an unconfined
seccomp profile, and a writable workspace intentionally weaken isolation.

### VS Code Wrapper

Add a minimal local extension under:

```text
vscode-extension/
```

It will:

- contribute the `pyrust` debug type;
- register a debug-adapter descriptor;
- launch `.venv/bin/python prototype/adapter/__main__.py`;
- pass an explicit CodeLLDB adapter and `liblldb` path when configured;
- provide initial launch configurations for both fixed fixtures;
- preserve the proxy as the only DAP endpoint seen by VS Code.

The extension will not start or coordinate debugpy.

Add two workspace launch configurations:

```text
PyRust: Python Outer
PyRust: Rust Outer
```

### Automated Validation

Add:

```text
scripts/accept-container.sh
```

From a host with Docker and the Dev Container CLI, it must:

1. build the development container from a clean image state;
2. verify the fixed tool versions inside the container;
3. verify the required debugger capability without `--privileged`;
4. build and test the local VS Code extension;
5. run `./scripts/accept-first-slice.sh` inside the container;
6. run `./scripts/accept-reverse-slice.sh` inside the container;
7. run a bounded VS Code extension-host smoke test, using `xvfb` when the test
   runner requires a display, that starts the `pyrust` adapter;
8. terminate the container without leaving debugger processes.

The command must not rely on a host `.venv`, host CodeLLDB installation, or a
previously running development container.

### Human VS Code Validation

One human validation run is required after automated acceptance:

1. open the repository with `Dev Containers: Reopen in Container`;
   when Docker is on a remote machine, first connect with Remote-SSH and open
   the checkout on that same remote machine;
2. select `PyRust: Python Outer`;
3. stop at `rust_inner` and verify this Call Stack subsequence:

```text
rust_inner
rust_outer
python_inner
python_outer
```

4. select `PyRust: Rust Outer`;
5. stop at `rust_callback` and verify this Call Stack subsequence:

```text
rust_callback
python_inner
python_outer
rust_outer
main
```

6. click every required frame and verify source navigation;
7. verify Rust-frame scopes and one deterministic Rust expression;
8. verify Python-frame scopes are empty and evaluation reports unsupported;
9. continue to the second callback stop and verify stale Python frame IDs are
   not reused.

The detailed contract is
[Containerized VS Code Acceptance](../acceptance/containerized-vscode.md).
Remote-host setup and recovery steps are recorded in
[Containerized VS Code Manual Verification](../acceptance/containerized-vscode-manual.md).

## Implementation Order

1. Add explicit CodeLLDB path configuration to the proxy.
2. Add the local VS Code wrapper extension and extension-host tests.
3. Add both launch configurations.
4. Add the pinned Dev Container environment.
5. Run the existing acceptance commands inside the container.
6. Add `scripts/accept-container.sh`.
7. Perform the human Call Stack checklist.
8. Review container permissions, version pinning, and host dependencies.

## Explicit Non-Goals

- production or deployment container images;
- running the VS Code desktop GUI inside Docker;
- browser-hosted VS Code or code-server;
- multiple debugger/debuggee containers;
- Kubernetes, remote Docker hosts, or Docker-in-Docker;
- debugpy, Python breakpoints, or Python expression evaluation;
- arbitrary user applications or generalized launch configuration;
- macOS, Windows, ARM64, or emulated x86_64 validation;
- security hardening for untrusted debuggee code;
- publishing the extension to the VS Code Marketplace.

## Consequences

Positive:

- clean machines can reproduce the proven debugger environment;
- automated DAP and human VS Code behavior are tested in the same userspace;
- tool versions and CodeLLDB paths become explicit;
- debugger privileges are visible and reviewable;
- host Python and Rust installations no longer affect acceptance.

Negative:

- the container requires elevated `ptrace` capability;
- the initial seccomp choice is intentionally broad;
- the wrapper extension adds a Node/TypeScript toolchain;
- the workspace remains writable from the container;
- success still proves only the fixed Linux x86_64 fixtures.

## Alternatives Considered

### Run the VS Code GUI inside the container

Rejected. Dev Containers already place the extension host and debugger
processes in the container while retaining the normal host UI.

### Use two containers

Rejected for this slice. Sharing target PIDs and process memory would require
extra namespace and permission configuration while weakening the proven
process ancestry.

### Use a virtual machine

Deferred. A VM provides a stronger kernel boundary but has higher setup and CI
cost than needed for the current fixed proof.

## Implementation Evidence

Implemented on 2026-07-18.

The final automated command:

```bash
./scripts/accept-container.sh
```

passed `AC-CV-01` through `AC-CV-10`. It performed a no-cache image build,
created isolated `.venv`, Node dependency, and Cargo target volumes, ran both
mixed-stack acceptance commands, started a pinned VS Code 1.125.0 extension
host in both development-path and CLI-installed VSIX modes, stopped the
container cleanly, destroyed the volumes, and repeated the result in a second
container lifecycle. The Dev Container installs that VSIX through
`postAttachCommand`; copying an extension directory into the image is
insufficient because the running VS Code Server owns extension registration.

The detailed environment and observed output are recorded in
[Containerized VS Code Results](../research/containerized-vscode-results.md).
After the 2026-07-20 attach-path correction, the final clean two-lifecycle
acceptance command must be rerun before automated evidence is considered
current. The ADR remains short of accepted status until that rerun and one
human complete
[Containerized VS Code Manual Verification](../acceptance/containerized-vscode-manual.md).

### Test only with the existing DAP clients

Rejected as the completion condition. The DAP tests remain primary correctness
evidence, but they do not prove VS Code registration, launch configuration,
Call Stack interaction, or extension deployment.

## Sources

- VS Code Dev Containers:
  https://code.visualstudio.com/docs/devcontainers/containers
- Creating a Dev Container:
  https://code.visualstudio.com/docs/devcontainers/create-dev-container
- Dev Container CLI:
  https://code.visualstudio.com/docs/devcontainers/devcontainer-cli
- VS Code debugger extensions:
  https://code.visualstudio.com/api/extension-guides/debugger-extension
- Docker seccomp profiles:
  https://docs.docker.com/engine/security/seccomp/
- Docker container capabilities:
  https://docs.docker.com/engine/containers/run/
