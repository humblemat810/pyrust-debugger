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

## OBS Studio 32.1.2 Setup (Windows)

Use OBS on the local Windows computer that displays the VS Code window, not on
the remote Linux host or inside the Dev Container.

1. In the main OBS window, click **Settings** in the bottom-right **Controls**
   panel.
2. Click **Output** in the left sidebar.
3. At the top of the Output page, set **Output Mode** to **Simple**.
4. In the **Recording** section:
   - choose a recording path with a few hundred MB free;
   - set **Recording Quality** to **High Quality, Medium File Size**;
   - set **Recording Format** to **Hybrid MP4**;
   - if OBS shows an **Encoder** selector, choose a hardware H.264 encoder
     when available; otherwise leave the selected software encoder.
5. Click **Audio** in the left sidebar and choose the intended microphone for
   **Mic/Auxiliary Audio**. Mute **Desktop Audio** in the main-window Audio
   Mixer unless its sound is useful in the demo.
6. Click **Video** in the left sidebar. Use 1920x1080 and 30 FPS, or retain
   the displayed resolution if the local display is smaller.
7. Click **Apply**, then **OK**.
8. In the main window's **Sources** panel, hide any unused source such as
   **Media Source**. Click **+**, select **Window Capture**, and choose the
   local VS Code window. Resize the red capture border so only the intended
   VS Code content is shown.
9. Make a five-second test using **Start Recording** in the **Controls**
   panel. Play it before the real take to confirm readable text and working
   narration.

For a shorter silent recording, Windows 11 Snipping Tool is also suitable.
For a separately recorded narration merged at fixed timestamps, use the
[Silent Video And Voiceover Workflow](voiceover-workflow.md).

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

- Do not claim arbitrary cross-language stepping. The implemented
  Python-to-Rust Step Into requires a native breakpoint at the Rust
  destination.
- Do not enter imports or calls in a synthetic Python frame at a Rust stop;
  use a `(debugpy)` launch configuration for full Python expressions.
- Keep the recording moving, but the manual process/thread launch now permits a
  one-hour breakpoint hold and must not terminate during normal inspection.

## Fallback

If the Process Tree is not visible:

1. End the session.
2. Run `Developer: Reload Window`.
3. Verify the extension version command above.
4. Start `PyRust: Python and Rust Threads` again.
