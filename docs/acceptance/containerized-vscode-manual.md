# Containerized VS Code Manual Verification

## Purpose

Use this checklist after `./scripts/accept-container.sh` reports all automated
criteria as passing. It records the human Call Stack and frame-interaction
evidence required by ADR 0004.

## Test Record

```text
Date:
Tester:
Desktop VS Code version:
Connection topology: local | Remote-SSH
Remote SSH host (if used):
Remote repository path:
Remote Docker version:
Container image ID:
Git revision:
Automated acceptance log:
```

Do not record SSH credentials, private key paths, public IP addresses, or other
secrets in this file. A stable host alias is sufficient.

## Supported Connection Topologies

The human check can use either:

```text
Local desktop VS Code -> local Docker -> Dev Container
```

or:

```text
Local desktop VS Code -> Remote-SSH Linux host -> remote Docker -> Dev Container
```

The second topology is the supported route when the repository and Docker
Engine are on a remote machine. The remote machine does not need a desktop
environment. VS Code's UI remains on the local computer, while the VS Code
Server, extension host, debugger, and debuggee run remotely.

Do not start from a local checkout and point `DOCKER_HOST` at the remote
machine for this acceptance run. ADR 0004 bind-mounts the repository path into
the container, so the checkout and Docker daemon must be on the same remote
host.

## Remote Host Prerequisites

On the remote Linux machine:

1. The machine is native Linux x86_64.
2. Docker Engine is running.
3. The SSH user can run `docker info` without `sudo`.
4. The repository is checked out on that machine.
5. The SSH user can write the checkout and create Docker containers, images,
   and named volumes.
6. Outbound HTTPS access is available for the pinned build dependencies.

Verify these from a normal Remote-SSH terminal, before entering the Dev
Container:

```bash
cd /path/to/pyrust-debugger
uname -m
docker info --format '{{.Architecture}}'
git rev-parse --show-toplevel
```

Both architecture commands must report `x86_64`, and `git rev-parse` must
print the remote checkout being tested.

## Remote-SSH Unblocking Steps

On the local computer:

1. Install the VS Code **Remote - SSH** and **Dev Containers** extensions.
2. Run `Remote-SSH: Connect to Host...` and select the remote Linux machine.
3. In the resulting `SSH: <host>` window, open the remote repository folder.
   Do not open a similarly named local checkout.
4. Open a VS Code terminal and verify that `pwd` is the remote repository and
   that `docker info` succeeds.
5. Run the automated gate on the remote host:

```bash
./scripts/accept-container.sh
```

6. Confirm it reports `AC-CV-01 PASS` through `AC-CV-10 PASS`.
7. After the script exits and cleans up its test container, run
   `Dev Containers: Reopen in Container` from the same Remote-SSH window.
8. Wait for `.devcontainer/bootstrap.sh` to finish.
9. Confirm the lower-left remote indicator identifies the PyRust Dev
   Container, not only the SSH host.
10. In the container terminal, verify:

```bash
pwd
python --version
rustc --version
```

The expected workspace is `/workspaces/pyrust-debugger`, Python is `3.14.6`,
and Rust is `1.97.1`.

If this repository is already open in an `SSH: <host>` VS Code window, start
at step 3. No port forwarding, remote desktop, browser-hosted VS Code, or
Docker socket mount inside the Dev Container is required.

## Debugger Preparation

1. Confirm the Run and Debug selector contains both `PyRust` configurations.
2. Set a source breakpoint at
   `research/fixtures/python_outer/src/lib.rs:6`.
3. Set a source breakpoint at
   `research/fixtures/rust_outer/src/main.rs:8`.

## Remote Troubleshooting

- If `Dev Containers: Reopen in Container` is missing, ensure **Dev
  Containers** is installed and enabled for the current Remote-SSH window.
- If Docker reports permission denied, fix remote Docker access and reconnect
  the SSH window before retrying. Do not work around it by running VS Code or
  the acceptance script as root.
- If the command builds against the wrong checkout, close the window,
  reconnect with Remote-SSH, and open the repository by its remote absolute
  path.
- If the Remote-SSH connection drops, reconnect to the SSH host, reopen the
  remote folder, and run `Dev Containers: Reopen in Container` again.
- If a debug configuration is absent, wait for bootstrap to finish and run
  `Developer: Reload Window` while still attached to the Dev Container.
- If a breakpoint is unverified, stop the session, confirm both fixture source
  paths above, and restart the corresponding `PyRust` configuration.

Official VS Code reference:
[Open a folder on a remote SSH host in a container](https://code.visualstudio.com/docs/remote/ssh#_open-a-folder-on-a-remote-ssh-host-in-a-container).

## HC-CV-01: Python-Outer Call Stack

1. Select `PyRust: Python Outer`.
2. Start debugging.
3. At the `rust_inner` stop, confirm this Call Stack subsequence:

```text
rust_inner
rust_outer
python_inner
python_outer
```

Record:

```text
HC-CV-01 PENDING
Evidence:
Notes:
```

## HC-CV-02: Rust-Outer Call Stack

1. Select `PyRust: Rust Outer`.
2. Start debugging.
3. At the `rust_callback` stop, confirm this Call Stack subsequence:

```text
rust_callback
python_inner
python_outer
rust_outer
main
```

Record:

```text
HC-CV-02 PENDING
Evidence:
Notes:
```

## HC-CV-03: Frame Interaction

At each required stop:

1. Click every required frame and confirm source navigation reaches the
   expected file and line.
2. Select a Rust frame and confirm its scopes load.
3. In the Python-outer `rust_inner` frame, evaluate `value` and confirm it is
   `20`.
4. Select each Python frame and confirm its scopes are empty.
5. Attempt evaluation on a Python frame and confirm the adapter reports it as
   unsupported without ending the session.

Record:

```text
HC-CV-03 PENDING
Rust expression:
Observed value:
Evidence:
Notes:
```

## HC-CV-04: Second Stop

1. Continue the Rust-outer session to its second callback stop.
2. Confirm the mixed stack is collected again.
3. Confirm Python frames from the first stop are no longer selectable as
   current frames.

Record:

```text
HC-CV-04 PENDING
Evidence:
Notes:
```

## Completion

Replace each `PENDING` result with `PASS` or `FAIL`, attach any screenshot or
log paths, and record failures in the project risk register before changing
ADR 0004 from implemented to accepted.
