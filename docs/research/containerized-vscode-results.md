# Containerized VS Code Results

## Purpose

This report records the automated implementation evidence for ADR 0004. Human
Call Stack interaction is tracked separately in the manual verification
record.

## Environment

Test date: July 18, 2026.

| Component | Version |
| --- | --- |
| Host architecture | Linux x86_64 |
| Docker client and server | 29.5.1 |
| Dev Container CLI | 0.87.0 |
| Host VS Code | 1.125.0 |
| Container base | Ubuntu 24.04 |
| CPython | 3.14.6 |
| Rust | 1.97.1 |
| Node.js | 24.18.0 |
| CodeLLDB | 1.12.2 Linux x64 |
| Extension-host VS Code | 1.125.0 |

The base images, Node archive, and CodeLLDB VSIX are pinned by digest or
SHA-256 in `.devcontainer/Dockerfile`.

## Automated Command

```bash
./scripts/accept-container.sh
```

Observed final output:

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

The first lifecycle used `--build-no-cache`. Before each lifecycle, the command
removed any project container and the project `.venv`, Node dependency, and
Cargo target volumes. The second lifecycle recreated those volumes and reran
the complete inner acceptance sequence.

## Security Boundary

Docker inspection and in-container capability checks observed:

```text
Privileged: false
PID mode: private/default
Added capability: CAP_SYS_PTRACE
Security option: seccomp=unconfined
Docker socket mount: absent
```

Both debugger directions completed under that boundary. No proxy, CodeLLDB,
helper, or fixture process remained before normal container shutdown.

## Extension Host

The TypeScript extension compiled and packaged with a zero-vulnerability
`npm audit`. The pinned VS Code 1.125.0 Electron tests:

- discovered and activated `pyrust.pyrust-debugger`;
- started a `pyrust` debug session;
- observed the adapter's DAP `initialized` event;
- launched the Python-outer fixture;
- repeated the smoke test after installing the packaged VSIX through VS Code's
  extension-management CLI, without loading `pyrust` through
  `--extensionDevelopmentPath`;
- terminated with exit code zero under `xvfb`.

DBus and GPU diagnostics from headless Electron were non-fatal and did not
affect the test result.

## Regression Results

The container run reported:

```text
AC-HP-01 through AC-HP-05 PASS
AC-SP-01 through AC-SP-04 PASS
AC-BF-01 through AC-BF-05 PASS
AC-RP-01 through AC-RP-07 PASS
```

The same regression set passed again after container and volume recreation.

## Remaining Gate

### 2026-07-20 Follow-Up

A real Dev Containers attach exposed two issues that the original headless
test did not cover:

- `onDebug:pyrust` did not activate the local debugger extension;
- copying an extension folder did not reliably register it with the live
  VS Code Server.

The implementation now uses `onDebug`, packages a VSIX, and installs it
through the VS Code extension manager. The attach-time installer was tested
inside the running Dev Container with no terminal-specific VS Code
environment, then repeated to prove it is idempotent. The updated first-slice,
reverse-slice, and both extension-host smoke modes pass.

The final `./scripts/accept-container.sh` clean two-lifecycle rerun remains
pending after these changes because it intentionally stops and removes the
active development container. Human criteria `HC-CV-01` through `HC-CV-04`
also remain pending in
[Containerized VS Code Manual Verification](../acceptance/containerized-vscode-manual.md).
