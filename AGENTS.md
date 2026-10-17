# VoicePranks contributor guide

## Purpose and architecture

VoicePranks is a local, real-time Python voice changer for macOS and Windows.
It captures a microphone with `sounddevice`, processes mono audio through the
DSP chain, and routes it to a virtual audio device. The Tkinter UI and optional
local HTTP control API both drive the same `VoiceEngine`.

Key areas:

- `voicepranks/audio/engine.py` owns streams, the real-time callback, recording,
  playback, and the thread-safe parameter queue.
- `voicepranks/dsp/` contains stateful, streaming effects. Each stage should
  accept and return a mono `float32` array and preserve the expected block
  contract.
- `voicepranks/params.py` is the registry used by the UI and control API.
  Add a user-facing parameter there rather than creating a second source of
  truth.
- `voicepranks/ui/app.py` is the Tkinter UI; `control_server.py` is the
  localhost-only, token-protected API.
- `voicepranks/dsp/laugh.py` loads bundled WAV assets or the user-writable
  `~/.voicepranks/custom_laugh.wav` override.
- `voicepranks/resources.py` is the only supported route to bundled files.
  It handles both source runs and PyInstaller bundles.

## Set up and validate

Use Python 3.9 or newer. A virtual environment named `venv` is conventional,
but not required.

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q
```

The test suite is designed to run without audio hardware. Before submitting a
change, run the smallest relevant test file and then the full suite when the
change touches shared engine, parameter, packaging, or persistence code.

Useful smoke checks:

```powershell
python -m voicepranks.selftest out.wav
python -m voicepranks.selftest input.wav out.wav --semitones 8 --eq-db 8
python -m voicepranks
```

The first two work without a microphone. The GUI needs real devices and should
be tested manually only when they are available.

## Audio and concurrency rules

- Keep the audio callback bounded: do not perform disk I/O, network I/O, UI
  work, long allocations, or blocking operations on the callback path.
- Preserve audio types and shapes. The engine and DSP tests expect mono
  `numpy.float32` samples in the normalized `[-1, 1]` range.
- Treat DSP stages as streaming and stateful. Reset or reinitialize internal
  state deliberately, and test behavior across multiple blocks, not only a
  single array.
- Use the engine's pending-parameter mechanism for changes that can arrive
  while streams run. Do not mutate shared DSP state directly from the UI or an
  HTTP handler.
- Keep tests deterministic: seed randomness and use synthetic signals. Do not
  depend on a physical input/output device or the developer's home directory.

## Custom and bundled audio

The current custom-audio feature is specifically for the laugh overlay:

- The UI records a replacement clip into `~/.voicepranks/custom_laugh.wav`.
- The loader accepts **16-bit PCM WAV** only; it takes the first channel and
  resamples it to the engine sample rate. MP3, AAC, float WAV, and 24/32-bit
  WAV are not supported without extending `audio/wavio.py`.
- A manually supplied WAV can replace that path while the app is stopped; it
  will load at next engine creation. Never store user recordings inside the
  installed package or repository.
- Bundled clips belong in `voicepranks/assets/`, must be resolved with
  `resource_path("assets", name)`, and must also be added to `datas` in
  `voicepranks.spec`. Add a resource test when adding a selectable bundled
  clip.
- `.gitignore` ignores WAVs by default. Explicitly unignore only intentional,
  redistributable stock assets; do not commit private recordings or
  unlicensed audio.

## Security and user data

- Do not weaken the control API's loopback binding, token checks, or recording
  save-path containment without a focused security review and tests.
- User state belongs under `~/.voicepranks/`; tests must redirect it to
  `tmp_path` and must not read, modify, or delete a real user recording.
- Avoid logging tokens, recordings, device identifiers, or full request bodies.

## Packaging and release changes

`voicepranks.spec` is shared by Windows and macOS. Use `build-windows.ps1` or
`build-macos.sh` rather than inventing a second packaging path. A packaging
change should preserve source runs and frozen builds; test `resource_path` and
keep `datas` synchronized with all assets the app selects.

GitHub Actions publishes releases from `main`. Do not change release triggers,
versioning, signing posture, or artifact names as an incidental part of a
feature change.

## Change discipline

- Keep changes scoped and preserve existing user changes in a dirty tree.
- Prefer small, focused tests next to the behavior changed.
- Update `README.md`, `INSTALL.txt`, or `DISTRIBUTING.md` whenever a
  user-visible workflow, supported file format, API endpoint, or build output
  changes.
- There is no formatter or linter configured. Follow the surrounding Python
  style; do not add tooling or reformat unrelated files merely to satisfy a
  change.
