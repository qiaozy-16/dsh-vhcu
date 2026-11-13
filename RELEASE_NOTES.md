# VHCU V1.0.1 Release Notes

**Release date:** 2026-11-13  
**Platform:** Windows 10/11 x64, DSH Desktop App only  
**Previous release:** V1.0.0

V1.0.1 is a platform-boundary and packaging release. It does not change the
normal Windows interaction model; it makes the daemon easier to move, package
and interrupt safely, and it closes the remaining offline-release gaps.

## Highlights

### Platform abstraction

- Added explicit `InputBackend`, `CaptureBackend` and `AudioBackend` contracts.
- Added `DriverFactory` with a single `sys.platform` decision point.
- Extracted `WindowsInputDriver`, `WindowsCaptureDriver` and
  `WindowsAudioDriver` adapters over the existing SendInput, mss and
  SoundCard/faster-whisper implementations.
- Existing Windows behavior is preserved; no macOS implementation is included
  in this release.

### Immediate interrupt channel

- Added a token-protected loopback `POST /interrupt` channel.
- The Node supervisor can interrupt a Python macro or Hear inference without
  waiting for the blocked stdio command loop.
- Soft pause (`paused_by_user`) and recovery now raise/clear the interrupt
  cooperatively.
- Added the `E_INTERRUPTED` protocol error and UI/error mapping.
- Input macro steps and interruptible waits stop promptly; SIGINT/SIGTERM also
  request an interrupt before shutdown.

### Absolute path sandbox

- Node resolves and injects absolute `VHCU_MODEL_PATH`, `VHCU_MACROS_PATH` and
  `VHCU_LOG_DIR` values into the Python daemon.
- Python validates the injected paths and rejects relative values instead of
  resolving against the process current directory.
- `models`, `macros.json` and log locations are stable for packaged or
  one-directory deployments, with no CWD-dependent fallback.

### Macro queue acknowledgement and timeout

- Macro queue entries carry an explicit acknowledgement deadline
  (`VHCU_MACRO_ACK_TIMEOUT_MS`, default 45000 ms).
- A missing acknowledgement pauses the queue with `macroQueue.status = "hung"`.
- The sidebar shows an alert instead of silently continuing later macros.

### Packaging

- Added a PyInstaller entry point and a maintained onedir spec.
- Release daemon is built as `dist/vhcu-daemon/vhcu-daemon.exe` plus
  `_internal/`; no onefile extraction into `%TEMP%`.
- Bundled full Hear runtime dependencies: faster-whisper, ctranslate2,
  onnxruntime, av, soundcard, webrtcvad-wheels, numpy, tokenizers and their
  runtime closure.
- Added explicit `excludes` for unused GUI/data-science modules (`tkinter`,
  `pandas`, `matplotlib`, `scipy`, PyQt/PySide, wx, torch, TensorFlow,
  scikit-learn, jupyter, pytest, notebook).
- Kept required FFmpeg/ctranslate2/onnxruntime/OpenBLAS binaries; an attempt to
  strip optional FFmpeg video encoders broke `av._core` and was reverted.

### Documentation and release

- README now documents the Release zip as the supported installation path.
- Added this `RELEASE_NOTES.md`.
- Updated the open-source release checklist and `.gitignore` backup rules.

## Verification

- Node test suite: **103/103**
- Python test suite: **111/111**
- Contract suite: **14/14**
- PyInstaller onedir build: successful
- Packaged daemon READY: successful
- Packaged HTTP `/interrupt`: `200`, `interrupt.set = true`
- Packaged `sys.info` paths: absolute and identical to injected values
- Packaged Hear dependency import: `hear.start` acknowledged successfully
- `pip check`: no broken requirements

## Upgrade notes

1. Download the V1.0.1 Release zip and extract it to a permanent folder.
2. Keep `dsh-better-sidebar` installed/enabled in the same DSH profile.
3. Run the included installer once and fully restart DSH.
4. Existing `macros.json` and model directories can be reused; no migration is
   required.
5. The packaged daemon does not bundle model weights. Use the offline
   `faster-whisper-base` Release asset or the built-in HF mirror download.

## Known limitations

- Windows only in this release; macOS/Linux drivers are not implemented.
- Primary-monitor capture remains the supported capture target.
- Hear is CPU-only (`base`, int8) and requires a WASAPI playback device.
- The onedir release is approximately 238 MB because the complete Hear and
  native runtime stack is included.
- A `VHCU_LOG_DIR` contract exists, but no new rotating file-log writer is
  introduced in this release.
