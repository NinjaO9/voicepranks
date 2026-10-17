# VoicePranks — real-time voice changer

Real-time "Minion voice" microphone filter for macOS and Windows, written in
pure Python (numpy + sounddevice, hand-rolled DSP — no scipy/librosa).

It captures your physical microphone, pitch-shifts it up (chipmunk-style,
formants move up too) with a presence-EQ boost that ramps in over ~1.2s, and
routes the result to a virtual audio device so apps like Discord, Zoom, or
OBS can pick it up as their microphone input.

## How it works

```
physical mic -> capture -> pitch shift up + presence EQ (ramped in) -> virtual output device
```

The virtual device is [VB-CABLE](https://vb-audio.com/Cable/) ("CABLE Input")
on Windows, or [BlackHole](https://existential.audio/blackhole/) on macOS.
Install one of those first, then point your voice/video app's microphone
input at it.

## Download & run (no Python)

Not a developer? Grab a prebuilt bundle and double-click it — no Python, no
`pip`. You only need to install one small free driver first.

1. **Install the free virtual audio cable** (this is *not* bundled — it's a
   system driver): [BlackHole](https://existential.audio/blackhole/) on macOS,
   [VB-CABLE](https://vb-audio.com/Cable/) on Windows.
2. **Unzip and open the app.** It's unsigned, so the OS warns you the first time:
   - macOS: right-click `VoicePranks.app` → **Open** → **Open**.
   - Windows: on the SmartScreen prompt, **More info → Run anyway**.
3. In Discord / Zoom / OBS, set the **microphone** to the virtual cable
   (BlackHole / CABLE Output).

Full end-user steps ship inside each zip as `INSTALL.txt`. To *build* these
bundles yourself, see [`DISTRIBUTING.md`](DISTRIBUTING.md).

## Requirements (running from source)

- Python 3.9+
- `numpy`, `sounddevice` (see `requirements.txt`)
- A virtual audio cable driver (VB-CABLE on Windows, BlackHole on macOS)

## Install

```
python -m venv .venv
source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
```

## Run the GUI

```
python -m voicepranks
```

Toggle the effect on/off, choose a **Preset** from the dropdown, and open
**Settings…** to pick your three audio devices (see below). Every DSP knob
(pitch amount, EQ, Minionese gibberish-mode params, WSOLA pitch-engine
internals, I/O) lives in collapsible **parameter sections** (collapsed by
default; click a section header to expand) -- one control per entry in
`params.PARAM_SPECS`, so nothing is hidden behind a hardcoded module constant
anymore. Hit **Record** and the effect runs in real time while it captures the
processed output; **Play** replays that live take verbatim (no re-render).
Tick **Live monitor** to also hear it on your speakers while recording.
**Re-render & Play** is a secondary A/B flow: it re-runs the *dry* mic through
a fresh effect with whatever settings are currently dialed in.

The **Random sounds** control plays one random audio file at the selected interval
(between 2 and 20 seconds). Click **Start random** to enable it and **Stop random** to disable it. Each sound is played to your listening device and, when the engine is running,
mixed into the virtual microphone output.
To add more audio files, just add to assets folder.

### Fullscreen jumpscare

The **Fullscreen jumpscare** parameter section is off by default. It uses the
bundled Foxy GIF and FNAF jumpscare WAV, while the Jumpscare asset buttons let
you replace either with your own PNG/GIF or 16-bit PCM WAV. The visual expands
uniformly to the largest size that fits the primary display, preserving its
aspect ratio. The sound plays only through the selected playback device, never
through the virtual microphone or into recordings. Escape dismisses the scare;
otherwise it closes when the sound ends.

### Audio devices (Settings…)

Three devices, configured in the **Settings** dialog and saved to
`~/.voicepranks/settings.json` (editable outside the app):

- **Input mic** — the physical microphone to record/process from.
- **Output mic** — the virtual cable other apps hear as a microphone
  (VB-CABLE / BlackHole); where processed audio is routed.
- **Output playback** — your speakers/headphones, used *only* for listening:
  the **Play** button and the **Live monitor** route here, never the virtual
  cable.

The GUI also starts a small local HTTP control API by default (see below),
so the same params can be tuned live from the command line or a script
while the window is open. Set `VOICEPRANKS_NO_SERVER=1` to disable it (the
legacy `MINION_NO_SERVER` name still works too).

## Live tuning over HTTP (control server)

Every param in the registry (`voicepranks/params.py`) is readable/writable
over a tiny stdlib-only JSON API, so you (or Claude, or a shell script) can
tweak the sound *while the engine is running*, without touching the UI.

Run it headless (no window, no display needed):

```
python -m voicepranks.server
```

This prints the base URL (default `http://127.0.0.1:8765`) and blocks. The
Tkinter app (`python -m voicepranks`) starts the same server in a
background thread automatically, so `curl` and the sliders drive one shared
engine and stay in sync -- an API-driven change shows up in the Tkinter
sliders on their next status poll, and vice versa.

Endpoints (JSON in/out unless noted):

| Method | Path | Body | Description |
| --- | --- | --- | --- |
| GET | `/api/state` | | `{specs, values, status}` -- full param schema, current values, engine status (incl. level meter) |
| POST | `/api/params` | `{"name": value, ...}` | Set one or many params by dotted name |
| POST | `/api/engine/start` | `{input_device?, output_device?}` | Start the audio streams |
| POST | `/api/engine/stop` | | Stop the audio streams |
| POST | `/api/engine/toggle` | | Start if needed, then flip the effect on/off |
| GET | `/api/devices` | | List input/output devices |
| POST | `/api/devices` | `{input_device?, output_device?}` | Switch devices (restarts streams if running) |
| POST | `/api/record/start` | | Start recording (captures the live processed output + the dry mic) |
| POST | `/api/record/stop` | | Stop recording |
| POST | `/api/render` | | Re-render the dry take with the *current* params (A/B flow) |
| POST | `/api/play` | `{"which": "live"\|"raw"\|"rendered"}` | Play a take (default `live`) through the output device |
| POST | `/api/save` | `{"path": ..., "which": "live"\|"raw"\|"rendered"}` | Save a take as a WAV **inside `~/.voicepranks/recordings/`** (relative paths only) |
| GET | `/api/recording.wav?which=live\|raw\|rendered` | | Download a take as WAV bytes |
| GET | `/` | | Minimal read-only status/params page |

### Authentication

The API can switch on your microphone and read recordings back, and *any*
web page you visit can send requests to `127.0.0.1`. So every `/api/*` call
must present a control token:

- It's generated per process and printed at startup (and by the Tkinter app
  on stderr). A copy lands in `~/.voicepranks/control-token` (mode 0600).
- Pass it as the `X-VoicePranks-Token` header or a `?token=...` query param.
- Set `VOICEPRANKS_CONTROL_TOKEN` to pin a fixed value for scripts.

Requests are also rejected unless the `Host` header names loopback (blocking
DNS rebinding) and any `Origin` header is loopback (blocking drive-by
requests from a site you have open). Opening `http://127.0.0.1:8765/` in a
browser still just works -- the page is served with its token baked in.

Example tuning loop:

```
TOKEN=$(cat ~/.voicepranks/control-token)
curl -s -H "X-VoicePranks-Token: $TOKEN" localhost:8765/api/state | jq .values
curl -s -H "X-VoicePranks-Token: $TOKEN" -XPOST localhost:8765/api/params -d '{"minionese.semitones": 6.5}'
curl -s -H "X-VoicePranks-Token: $TOKEN" -XPOST localhost:8765/api/record/start
# ...speak...
curl -s -H "X-VoicePranks-Token: $TOKEN" -XPOST localhost:8765/api/record/stop
curl -s -H "X-VoicePranks-Token: $TOKEN" -XPOST localhost:8765/api/play   # plays the live processed take
```

## Record and playback

Recording captures two takes at once:

- **live** — the processed effect output, captured block-by-block exactly as
  it is produced in real time. `play()` replays this verbatim; nothing is
  re-rendered or re-applied, so it's a faithful "as it sounded live" capture.
  This is the default take for `play`/`save`/`recording.wav`.
- **raw** — the dry (pre-effect) mic signal, kept so the effect can be
  **re-rendered** onto the same take with different params (`/api/render` →
  the `rendered` take), an optional A/B tuning flow that doesn't disturb the
  live engine's own state.

`play()` plays a take (`live`, `raw`, or `rendered`) through the selected
output device; `save()`/`GET /api/recording.wav` write it out as a 16-bit PCM
WAV. Tick **Live monitor** (or `POST /api/params {"monitor": true}`) to also
hear the processed audio on your speakers while recording.

## Presets

Two built-in voice presets ship in `presets.py`:

- **minion** — connected, pitched-up, sing-song (no scramble).
- **animalese** — chopped/scrambled staccato blips.

Click the **Minion** / **Animalese** buttons in the GUI (or `POST
/api/presets/apply {"name": "minion"}`; list them with `GET /api/presets`) to
apply one instantly. Presets set the **voice character only** — they don't
touch your input/output devices, on/off state, or the live monitor. Edit
`PRESETS` in `presets.py` to change them or add your own.

## Record your own goofy laugh

The **goofy** preset punctuates your speech with a bundled "goofy laugh" clip.
You can swap that clip for your own recording without touching any files:

- **Record laugh** — starts the audio engine (if needed) and captures your
  mic. Laugh it up, then hit **Stop & use** to install what you recorded as the
  active laugh. It's saved to `~/.voicepranks/custom_laugh.wav`, so it sticks
  across restarts. (A silent/empty recording is ignored — the current clip is
  left alone.)
- **Use stock laugh** — reverts to the bundled clip and deletes your recording.

The label next to the buttons shows which clip is live (**custom clip active**
or **stock clip**). The same flow is on the HTTP API: `POST
/api/laugh/record/start`, `POST /api/laugh/record/stop`, `POST /api/laugh/reset`
(the current clip is reported as `custom_laugh` in `GET /api/state`).

## Audition the effect with no audio hardware

`selftest.py` reads/writes WAV files directly, so you can hear the effect
without any mic or virtual cable set up:

```
python -m voicepranks.selftest out.wav
python -m voicepranks.selftest in.wav out.wav --semitones 8 --eq-db 8
```

If no input WAV is given, a 2-second synthetic test tone (200 Hz with a
little vibrato) is generated and processed instead.

## Troubleshooting

**Blank / empty window on macOS.** Apple's *system* Python (`/usr/bin/python3`)
ships with Tk 8.5, which renders blank windows on modern macOS. Use a Python
built against a newer Tk:

```
brew install python-tk
python3 -m venv .venv          # uses Homebrew's python3 (Tk 8.6+/9.0)
source .venv/bin/activate
pip install -r requirements.txt
```

The app prints a warning at startup if it detects a broken Tk.

**BlackHole not detected right after installing it.** macOS only scans for
audio drivers when `coreaudiod` starts, so a fresh install may be invisible
until you restart the audio daemon (`sudo killall coreaudiod`) or reboot.

## Run the tests

```
pytest -q
```

## Project layout

```
voicepranks/
  dsp/             pitch shifting, peaking EQ, intensity ramp, Minionese
                    gibberish mode, combined effect
  audio/           device discovery, WAV read/write, the real-time
                    capture/output engine (param registry, level meter,
                    recorder)
  ui/               tkinter GUI (generated param grid + record/playback)
  webui/            minimal read-only status page served by the control API
  params.py         PARAM_SPECS -- single source of truth for every knob
  control_server.py stdlib HTTP JSON control API
  server.py         headless entrypoint (`python -m voicepranks.server`)
  selftest.py       CLI WAV-file audition tool
```
