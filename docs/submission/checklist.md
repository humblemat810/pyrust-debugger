# Submission Checklist

This checklist distinguishes repository evidence from actions that require a
human account, recording, or upload. Do not mark a handoff item complete until
its external artifact exists.

## Product Evidence

- [x] `./scripts/verify-submission.sh` passed on 2026-07-21 in the current
  Linux workspace.
- [x] [Dated repository-side verification record](verification-2026-07-21.md)
  captures the accepted criteria and exclusions.
- [x] Developer Tools track selected.
- [x] Python -> Rust mixed stack has a black-box acceptance command.
- [x] Rust -> Python -> Rust callback has a black-box acceptance command.
- [x] Python and Rust native-thread behavior has an automated proof.
- [x] Process Tree refresh is checked against the DAP thread inventory.
- [x] VS Code extension packages as a VSIX.
- [x] Reusable launch and settings templates exist for another PyO3 project.
- [x] Explicit limitations are documented.

## Before Recording

- [ ] Commit the submission assets and run `./scripts/submission-status.sh`.
- [ ] Record the printed commit SHA in the recording notes.
- [ ] Run `./scripts/verify-submission.sh` from a clean Dev Container session.
- [ ] If Docker capacity permits, run
  `PYRUST_VERIFY_CONTAINER=1 ./scripts/verify-submission.sh` for a clean
  container rebuild and acceptance pass.
  The 2026-07-21 host had about 3.6 GB free, so do not attempt this until more
  disk space is available.
- [ ] Confirm `pyrust.pyrust-debugger@0.0.5` is installed in the Dev Container.
- [ ] Stop any previous debug session and reload the VS Code window.
- [ ] Open the Run and Debug sidebar and ensure **PyRust Process Tree** is
  visible.
- [ ] Use the [Demo Runbook](demo-runbook.md) once without recording.

## Recording

- [ ] Record a 60-90 second video using the Python-entry/Rust-thread demo.
- [ ] Show the Rust source breakpoint at `lib.rs:6`.
- [ ] Show the built-in Call Stack and PyRust Process Tree together.
- [ ] Expand at least one stopped `rust-child-*` thread.
- [ ] Click a source-backed Process Tree frame.
- [ ] Explain that sibling OS threads are intentionally not shown as nested
  caller/callee frames.
- [ ] Do not show secrets, private paths, browser tabs, or unrelated terminal
  output.

## Submission Form

- [ ] Use the title **PyRust: Mixed Python and Rust Debugging in VS Code**.
- [ ] Select **Developer Tools**.
- [ ] Paste the copy from [Form Copy](form-copy.md).
- [ ] Link the repository and video.
- [ ] Confirm the repository visibility and licensing choice. The current
  extension license does not permit redistribution; do not represent PyRust as
  open source unless that is deliberately changed.
- [ ] Include the limitations section without edits that weaken it.
- [ ] Describe Codex use with the evidence in the `Built With Codex` section.
- [ ] Review every claim against its linked command before submitting.
- [ ] Check the live form's current required fields, character limits, and
  rules before final upload.

## After Submission

- [ ] Save the final submission URL.
- [ ] Record the submission date and the commit SHA used for the recording.
- [ ] Keep the demo environment intact until judging is complete.
