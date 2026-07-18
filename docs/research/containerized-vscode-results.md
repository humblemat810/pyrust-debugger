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
- repeated the smoke test after discovering `pyrust` only from
  `/root/.vscode-server/extensions/pyrust.pyrust-debugger-0.0.1`, without
  loading it through `--extensionDevelopmentPath`;
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

Automated ADR 0004 acceptance is complete. Human criteria `HC-CV-01` through
`HC-CV-04` remain pending in
[Containerized VS Code Manual Verification](../acceptance/containerized-vscode-manual.md).
