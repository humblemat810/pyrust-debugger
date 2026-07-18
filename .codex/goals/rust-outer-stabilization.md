# Goal: Stabilize and Complete the Rust-Outer Stack Slice

Start the root Goal mode agent with `gpt-5.6-sol` at Ultra, or Extra High when
Ultra is unavailable.

Implement the two serial gates defined by:

- `docs/decisions/0003-stabilize-before-rust-outer.md`
- `docs/acceptance/rust-outer-stabilization.md`

First run `./scripts/accept-first-slice.sh`. Complete the unwinder circuit
breaker and its regression tests before modifying the Rust-outer fixture or
merge policy. Rerun the original acceptance command at that gate.

For the reverse proof, extend `research/fixtures/rust_outer` with an explicit
Rust callback invoked by nested embedded Python functions. Stop at that Rust
callback, preserve upper and lower CodeLLDB frame identities, and insert the
Python frames between them.

Use the ADR 0003 project agents in this order:

1. Spawn `unwinder_stability_builder`, wait for it, integrate its result, and
   rerun `./scripts/accept-first-slice.sh`.
2. Spawn `reverse_stack_builder` and `reverse_acceptance_builder` in parallel.
3. Integrate their results in the root thread and run both acceptance commands.
4. Spawn `reverse_slice_reviewer`, address every blocking finding, and rerun
   both commands.

The root agent owns shared interface decisions, integration, documentation, and
any helper-contract change that proves necessary.

Do not let parallel agents edit the same files. Do not begin reverse-direction
work until the stabilization gate passes. Do not add debugpy, Python
breakpoints, Python evaluation, general multiboundary ordering, process
topology redesign, multithread support, or extension packaging.

Completion requires both:

```bash
./scripts/accept-first-slice.sh
./scripts/accept-reverse-slice.sh
```

to pass from fresh proxy processes. Finish with changed files, criterion
results, known shortcuts, and residual risks. Do not commit unless explicitly
requested.
