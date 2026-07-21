# Submission Verification Record: 2026-07-21

## Scope

This record captures two successful submission-gate runs:

```bash
PYRUST_VERIFY_CONTAINER=0 ./scripts/verify-submission.sh

PYRUST_VERIFY_CONTAINER=1 ./scripts/verify-submission.sh
```

The command completed with:

```text
SUBMISSION-DEMO-GATE PASS
```

## Verified Evidence

| Area | Result |
| --- | --- |
| Adapter unit coverage | `prototype.adapter.tests.test_mixed_stack`: 19 tests passed |
| Python -> Rust stack | `AC-HP-01` through `AC-HP-05` passed |
| Python -> Rust fallback behavior | `AC-SP-01` through `AC-SP-04` passed |
| Rust -> Python -> Rust stack | `AC-BF-01` through `AC-BF-05` and `AC-RP-01` through `AC-RP-07` passed |
| Python entry with Rust child threads | `AC-PRT-01` through `AC-PRT-04` passed |
| Python thread regression | `AC-MT-01` through `AC-MT-04` passed |
| Rust thread regression | `AC-RT-01` through `AC-RT-04` passed |
| Process/thread hierarchy | `AC-PTM-01` through `AC-PTM-07` passed |
| Process/thread idle breakpoint | `AC-PTM-IDLE` remained stopped and queryable for 65 seconds, beyond the old 45-second fixture cutoff, on both host and Dev Container |
| Python async non-nesting | `AC-AT-01` through `AC-AT-04` passed |
| Rust async non-nesting | `AC-RA-01` through `AC-RA-04` passed |
| VS Code extension | TypeScript compile and VSIX package passed |
| Current Dev Container extension check | `scripts/accept-container-inside.sh extension` passed |
| Container build definition | `docker buildx build --check --file .devcontainer/Dockerfile .` passed |
| Clean Dev Container rebuild and acceptance | `AC-CV-01` through `AC-CV-10` passed |

## Not Covered By This Record

- Manual VS Code visual confirmation after the current VSIX is installed.
- The public screen recording, Devpost form completion, and final submission
  URL.

Those items remain intentionally unchecked in the
[Submission Checklist](checklist.md).

Before recording, run `./scripts/submission-status.sh` after committing the
submission assets. It prints the exact commit SHA and fails if the worktree is
dirty.
