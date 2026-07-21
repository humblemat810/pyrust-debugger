# Demo Runbook

## Goal

Show one real debugging story in under 90 seconds:

```text
Python worker -> Rust function -> named Rust worker thread -> rust_inner breakpoint
```

The Process Tree shows real native-thread ownership. The built-in Call Stack
shows the selected thread's frames.

## Prepare

Inside the Dev Container:

```bash
./scripts/submission-status.sh
./scripts/verify-submission.sh
rm -f ~/.pyrust-debugger/vsix.sha256
bash .devcontainer/install-vscode-extension.sh
```

`submission-status.sh` must print `SUBMISSION-STATUS READY`. If it prints
`DIRTY`, commit the exact version that will be recorded before continuing.

Confirm the extension version:

```bash
env -u NODE_OPTIONS code --list-extensions --show-versions | grep -Fx \
  'pyrust.pyrust-debugger@0.0.5'
```

Run **Developer: Reload Window** before recording.

## Record

Use [75-Second Demo Narration](narration.md) if a concise voiceover is useful.

1. Open `research/fixtures/python_outer/src/lib.rs`.
2. Set one breakpoint on line 6, inside `rust_inner`.
3. Select `PyRust: Python and Rust Threads` in **Run and Debug**.
4. Start debugging.
5. Wait for the stopped `rust-child-*` thread.
6. Show the built-in **Call Stack**:
   - `pyrust_native::rust_inner`;
   - `pyrust_native::rust_outer_with_rust_threads`;
   - lower Rust runtime frames as applicable.
7. Show **PyRust Process Tree**:
   - one Python process;
   - Python worker threads and named `rust-child-*` threads as direct
     siblings;
   - the selected stopped Rust thread expanded to its stack frames.
8. Click `pyrust_native::rust_inner` in the Process Tree.
9. Point out the source navigation and amber PyRust line decoration.
10. State the key correctness rule: a Rust OS thread is a sibling of the
    Python thread that created it, not a child frame of that Python thread.

## Avoid

- Do not claim Python breakpoints or cross-language stepping.
- Do not enter Python expressions in a synthetic Python frame.
- Do not wait at the breakpoint for the fixture timeout; keep the recording
  moving and continue/end the session after the explanation.

## Fallback

If the Process Tree is not visible:

1. End the session.
2. Run `Developer: Reload Window`.
3. Verify the extension version command above.
4. Start `PyRust: Python and Rust Threads` again.
