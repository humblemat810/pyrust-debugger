# Codex Agent Layout

These project-scoped agents support the implemented first workable slice in
ADR 0002 and the stabilization and reverse-direction slice in ADR 0003.

## Parent Goal Mode

Start the root goal with:

```text
Model: gpt-5.6-sol
Level: Ultra
```

Ultra is appropriate because the goal intentionally delegates three independent
workstreams and then runs a separate reviewer. If Ultra is unavailable or too
expensive, use Extra High. The goal prompt still requests delegation
explicitly.

The project `config.toml` does not force this selection because doing so would
apply the expensive root setting to ordinary non-goal chats as well.

## Roles

ADR 0002:

| Agent | Model | Effort | Ownership | Purpose |
| --- | --- | --- | --- | --- |
| `dap_proxy_builder` | `gpt-5.6-sol` | Extra High (`xhigh`) | `prototype/adapter/**` | Transparent DAP transport and CodeLLDB forwarding |
| `cpython_stack_builder` | `gpt-5.6-terra` | High | `prototype/python/**` | Stable CPython 3.14 stack-reader command and tests |
| `slice_acceptance_builder` | `gpt-5.6-luna` | Medium | `tests/acceptance/**`, `scripts/accept-first-slice.sh` | Black-box acceptance harness |
| `slice_reviewer` | `gpt-5.6-sol` | Extra High (`xhigh`) | Read-heavy whole-repository review | Independent completion audit |

ADR 0003:

| Agent | Model | Effort | Ownership | Purpose |
| --- | --- | --- | --- | --- |
| `unwinder_stability_builder` | `gpt-5.6-terra` | High | Mixed-stack hook and tests | Circuit breaker and resource bounds |
| `reverse_stack_builder` | `gpt-5.6-sol` | Extra High (`xhigh`) | Rust-outer fixture and merge policy | Reverse stack construction |
| `reverse_acceptance_builder` | `gpt-5.6-luna` | Medium | Reverse acceptance files and command | Black-box reverse proof |
| `reverse_slice_reviewer` | `gpt-5.6-sol` | Extra High (`xhigh`) | Read-heavy whole-repository review | Independent ADR 0003 audit |

The custom-agent TOML files pin these models. In configuration files, the UI
label **Extra High** is written as `model_reasoning_effort = "xhigh"`.

The root goal agent owns:

- shared interface decisions;
- stack augmentation integration;
- edits outside an agent's ownership;
- resolving conflicts between agent outputs;
- the final acceptance run.

## Orchestration

ADR 0002:

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

ADR 0003 uses two serial gates:

1. Run `unwinder_stability_builder` alone and pass the original acceptance
   command.
2. Run `reverse_stack_builder` and `reverse_acceptance_builder` in parallel.
3. Integrate and pass both acceptance commands.
4. Run `reverse_slice_reviewer`, fix findings, and rerun both commands.

## Reusable Goals

- `goals/first-workable-slice.md` reproduces the ADR 0002 implementation goal.
- `goals/rust-outer-stabilization.md` reproduces the ADR 0003 stabilization
  and reverse-direction proof.

The files under `.codex/goals` are reusable prompts, not automatically loaded
configuration.
