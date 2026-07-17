# Goal: Complete the First Workable Mixed-Stack Slice

Start the root Goal mode agent with `gpt-5.6-sol` at Ultra. If Ultra is not
available or its resource use is undesirable, use Extra High; the instructions
below still require explicit delegation.

Implement the unattended 72-hour technical proof defined by:

- `docs/decisions/0002-72-hour-first-workable-slice.md`
- `docs/acceptance/first-workable-slice.md`
- `.codex/README.md`

Use the project-scoped custom agents. Spawn `dap_proxy_builder`,
`cpython_stack_builder`, and `slice_acceptance_builder` for independent work,
wait for all three, and integrate their results in the root thread. Do not
allow parallel agents to edit the same files. After the acceptance command
passes, spawn `slice_reviewer`, address all blocking findings, and rerun the
acceptance command.

Do not expand the scope. Support only the fixed Python-outer, single-thread,
launch-only fixture and the documented happy paths and simple sad paths. Do not
add debugpy, Python breakpoints, Python evaluation, cross-language stepping,
reverse-direction debugging, subprocess support, multithread support, VS Code
packaging, or platform compatibility work.

Completion requires:

```bash
./scripts/accept-first-slice.sh
```

to pass every documented `AC-HP-*` and `AC-SP-*` criterion without manual
interaction or reliance on an earlier debugger process. Preserve unrelated
changes, do not commit unless explicitly requested, and finish with changed
files, verification results, known shortcuts, and residual risks.
