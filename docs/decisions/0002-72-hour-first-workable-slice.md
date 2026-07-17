# ADR 0002: Limit the First Workable Slice to a 72-Hour DAP Proof

## Status

Accepted.

## Date

2026-07-17.

## Context

ADR 0001 selects Python calling Rust as the first product direction. The full
stack-only alpha still includes extension packaging, compatibility work,
multithread behavior, and broad failure handling. That scope is too large for
an unattended two-to-three-day coding-agent run.

The first workable slice must prove the central integration without pretending
to be a releasable debugger.

## Decision

Build one automated, fixture-bound vertical proof:

```text
CPython 3.14 fixture
  -> PyO3 Rust extension
  -> Rust source breakpoint owned by CodeLLDB
  -> PyRust DAP proxy stackTrace response
  -> Rust frames followed by Python caller frames
```

The proof will:

- run only on the installed Ubuntu Linux x86_64 environment;
- use CPython 3.14.6 and the installed CodeLLDB 1.12.2;
- support launch, not attach;
- support the existing single-thread Python-outer fixture;
- stop only at a Rust source breakpoint;
- use the existing CPython remote-unwinder helper;
- permit the research `process status` PID-discovery fallback;
- use a simple boundary rule that is correct for the fixture;
- preserve downstream CodeLLDB frame IDs and behavior;
- allocate Python synthetic frame IDs for one stop epoch;
- prove behavior through an automated DAP client and transcript assertions.

The proxy may be implemented as a Python 3.14 executable for this proof. A
production TypeScript extension and packaging are not required by this slice.

## Happy Paths

The slice supports:

1. launching the existing Python-outer fixture through the proxy;
2. setting and hitting the `rust_inner` Rust breakpoint;
3. returning `rust_inner`, `rust_outer`, `python_inner`, and `python_outer` in
   logical order;
4. returning correct source paths and lines for those user frames;
5. forwarding Rust-frame scopes and evaluation to CodeLLDB;
6. using an acceptance-only driver that calls the existing fixture path twice,
   then continuing and producing a fresh mixed stack at the second stop.

## Simple Sad Paths

The slice handles only:

1. CPython helper failure by returning the unmodified native stack;
2. CPython helper timeout by returning the unmodified native stack and a
   diagnostic;
3. `scopes` for a synthetic Python frame by returning an empty scope list;
4. downstream launch or protocol failure by ending the proof with a clear
   error instead of hanging.

## Explicit Non-Goals

- Python breakpoints, stepping, locals, or expression evaluation;
- Rust-outer/Python-inner debugging;
- multiple Python/Rust transitions;
- multiple threads, subprocesses, attach, or restart;
- general native/Python boundary classification;
- VS Code UI automation or extension marketplace packaging;
- macOS, Windows, free-threaded Python, subinterpreters, or other versions;
- optimized, stripped, or production binaries;
- product-quality PID discovery;
- release-quality security, performance, or compatibility guarantees.

## Agent Execution Model

The root goal agent owns integration and completion. It may delegate independent
work to the project agents in `.codex/agents`, but parallel agents must not edit
the same files.

The preferred sequence is:

1. run the DAP proxy, CPython helper, and acceptance-harness work in parallel;
2. wait for all three results;
3. integrate stack augmentation in the root thread;
4. run the independent slice reviewer;
5. fix findings and rerun the acceptance command without relying on state from
   an earlier debugger process.

Subagents must not recursively spawn additional agents.

Run the root Goal mode agent with `gpt-5.6-sol` at Ultra, falling back to Extra
High when Ultra is unavailable or too expensive. Project agent files pin Sol at
Extra High for DAP implementation and review, Terra at High for the CPython
helper, and Luna at Medium for the acceptance harness. In TOML, Extra High is
represented by `model_reasoning_effort = "xhigh"`.

## Consequences

Positive:

- agents can complete and verify a meaningful vertical proof quickly;
- failures are constrained to known components and one deterministic fixture;
- the result directly validates the highest-risk DAP stack-merging path;
- no human observation is required for acceptance.

Negative:

- the proof is not a usable general-purpose VS Code extension;
- fixture-specific assumptions will need replacement during alpha work;
- the Python implementation may later be packaged as a sidecar or replaced;
- success does not estimate Python breakpoints, evaluation, or stepping.

## Completion

The decision is satisfied only when every criterion in
[First Workable Slice Acceptance](../acceptance/first-workable-slice.md) passes
through one documented command.
