# Codex Agent Layout

These project-scoped agents support the 72-hour first workable slice defined by
`docs/decisions/0002-72-hour-first-workable-slice.md`.

## Roles

| Agent | Ownership | Purpose |
| --- | --- | --- |
| `dap_proxy_builder` | `prototype/adapter/**` | Transparent DAP transport and CodeLLDB forwarding |
| `cpython_stack_builder` | `prototype/python/**` | Stable CPython 3.14 stack-reader command and tests |
| `slice_acceptance_builder` | `tests/acceptance/**`, `scripts/accept-first-slice.sh` | Black-box acceptance harness |
| `slice_reviewer` | Read-heavy whole-repository review | Independent completion audit |

The root goal agent owns:

- shared interface decisions;
- stack augmentation integration;
- edits outside an agent's ownership;
- resolving conflicts between agent outputs;
- the final acceptance run.

## Orchestration

1. Read the ADR and acceptance criteria before spawning work.
2. Spawn `dap_proxy_builder`, `cpython_stack_builder`, and
   `slice_acceptance_builder` in parallel.
3. Tell each agent to stay inside its ownership boundary and return a concise
   integration summary.
4. Wait for all three before editing shared integration points.
5. Integrate in the root thread.
6. Spawn `slice_reviewer` after the acceptance command first passes.
7. Address findings and rerun `./scripts/accept-first-slice.sh`.

Do not let parallel agents edit the same files. Do not expand scope to Python
debugging features or compatibility work to compensate for a failed acceptance
criterion.

The files under `.codex/goals` are reusable prompts, not automatically loaded
configuration.
