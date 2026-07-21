# Silent Video And Voiceover Workflow

Record the visual demo and narration separately, then combine them into one
upload-ready MP4. This prevents a spoken mistake from requiring a new
debugging run or a new screen recording.

## What You Need

On the local Windows computer that displays VS Code:

- OBS Studio 32.1.2 or newer;
- `ffmpeg` and `ffprobe` available on `PATH`;
- the silent screen recording;
- five short narration clips.

Do not run the merge script on the remote Linux host or in the Dev Container.
They do not own the local VS Code display or the final video files.

## Record The Silent Video

1. Follow the OBS setup in the [Demo Runbook](demo-runbook.md).
2. Mute **Mic/Aux** in OBS's Audio Mixer.
3. Record the visual sequence from the Demo Runbook. Leave a brief pause at
   the beginning and at each intended narration transition.
4. Save it as `recordings/pyrust-demo-silent.mp4`.

The `recordings/` directory is intentionally Git-ignored.

## Record The Voice Clips

Use Windows Sound Recorder, OBS audio-only capture, or any editor that can
export WAV, M4A, or MP3. Record one clip for each section in
[75-Second Demo Narration](narration.md).

Create this local layout:

```text
recordings/
  pyrust-demo-silent.mp4
  cues.json
  audio/
    01-problem.wav
    02-start.wav
    03-mixed-stack.wav
    04-process-tree.wav
    05-interaction-and-limits.wav
```

Copy [voiceover-cues.example.json](voiceover-cues.example.json) to
`recordings/cues.json`. Its timestamp offsets are 0, 10, 25, 45, and 65
seconds. Shorten or re-record a clip rather than letting it overlap the next
section.

## Merge

In Windows PowerShell at the repository root, run:

```powershell
.\scripts\merge-demo-voiceover.ps1 `
  -Video .\recordings\pyrust-demo-silent.mp4 `
  -Cues .\recordings\cues.json `
  -Output .\recordings\pyrust-demo-final.mp4
```

The script copies the recorded video stream without re-encoding it, delays
each narration clip to its specified timestamp, encodes the combined audio as
AAC, and writes a fast-start MP4 suitable for upload.

## Review

1. Play `recordings/pyrust-demo-final.mp4` from start to finish.
2. Confirm the final duration is 60-90 seconds.
3. Confirm that narration starts at the expected visual moments.
4. Confirm the Call Stack, Process Tree, `rust-child-*` expansion, and amber
   source navigation are visible.
5. Confirm no terminal secrets, unrelated windows, or private paths appear.
