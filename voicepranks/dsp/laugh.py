"""Random procedural "goofy laugh" injector.

A *generator* stage (unlike the transform stages): it emits a short laugh and
randomly mixes it on top of the voice, ducking the voice a touch underneath it.
Used by the "goofy" preset to punctuate speech with an occasional laugh.

The laugh comes from a real audio clip. A *user-recorded* clip at
`~/.voicepranks/custom_laugh.wav` takes priority (recorded in-app via the
"Record laugh" button); absent that, the bundled stock clip at
`voicepranks/assets/goofy_laugh.wav` is used. Both are loaded via the stdlib
`wave` module -- no MP3/ffmpeg dependency; resampled to the engine rate if
needed. If neither is present it falls back to a fully synthesized giggle so
the feature still works with no bundled audio. `save_custom()` installs a fresh
recording and `reset_to_stock()` drops back to the bundled clip.

The synthesized fallback models a real laugh rather than a pure tone: a
band-limited sawtooth *glottal source* (buzzy voiced excitation with a natural
harmonic rolloff), shaped by three *vocal formant* resonances (reusing
`PeakingEQ`) so it lands on a laugh vowel, with *breath noise* aspiration
puffing at each syllable onset and a short noisy "-ck" release at the end.
Pitch rises across the laugh (the "a-hyuck" lift) with a gentle warble.

Triggering is speech-gated (it only laughs while you're actually talking, so it
punctuates rather than fires into dead air) and rate-limited by a cooldown so
it never spams. Randomness comes from a seeded `np.random.default_rng` (same
convention as the other DSP stages) so tests are reproducible; `reset()`
restores the seed and clears any in-flight laugh.

`process()` returns the same number of samples it receives (it mixes onto the
incoming voice block), so it slots into the plain path like any other stage.
Intensity scales both how often it laughs and how loud, so at intensity 0 it is
silent -- the whole effect fades under the master intensity control.
"""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import json
import os

from .. import presets
from ..audio import wavio
from ..settings import SETTINGS_DIR
from .biquad import PeakingEQ

# Vocal-tract formant resonances (Hz, Q, boost dB) that color the glottal
# source into a laugh vowel (roughly an "uh/ah"). Used by the synth fallback.
_FORMANTS = ((650.0, 4.0, 10.0), (1100.0, 5.0, 9.0), (2600.0, 6.0, 7.0))

# Bundled stock laugh clips. Resolved via `resource_path` so they're found both
# from source and inside a PyInstaller build (a `__file__`-relative path misses
# them in the packaged macOS .app -- see resources.py).


# Selectable pre-recorded laughs, by name. "goofy" is the built-in stock clip
# (`_SAMPLE_PATH`); the others are alternates the user can switch to. Switching
# presets is a live, in-memory choice (see `select_laugh`) -- it never touches
# the persisted recording slot, so picking a preset can't clobber a laugh the
# user recorded.


# User-recorded laugh clip. Lives in the shared user config dir (`settings.
# SETTINGS_DIR`, i.e. `~/.voicepranks/`) so it survives restarts and stays
# writable even when the app is a read-only bundled build. When present it
# overrides the stock clip.
_CUSTOM_PATH = SETTINGS_DIR / "custom_laugh.wav"


def _load_sample(path: Path, target_sr: int):
    """Load a WAV (via the shared `wavio` reader) as mono float32 at
    `target_sr`, linear-resampled if needed. A stereo file collapses to its
    left channel (the app-wide `wavio` convention). Returns None if the file is
    missing or unreadable, so the caller can fall back to synthesis."""
    try:
        sr, mono = wavio.read_wav(str(path))
    except (OSError, ValueError, wave.Error):
        return None

    data = mono.astype(np.float64)
    if data.size == 0:
        return None
    if sr != target_sr and data.size > 1:
        count = int(round(data.size / sr * target_sr))
        if count > 1:
            pos = np.linspace(0.0, data.size - 1, count)
            i0 = pos.astype(np.int64)
            frac = pos - i0
            i1 = np.minimum(i0 + 1, data.size - 1)
            data = data[i0] * (1.0 - frac) + data[i1] * frac
    return data.astype(np.float32)

# Speech-envelope threshold above which we consider the user to be talking.
_SPEECH_GATE = 0.01
# How much the voice is ducked (attenuated) at the peak of an active laugh.
_VOICE_DUCK = 0.6
# Fade in/out applied to the laugh (and to the voice ducking) so it blends in
# and out smoothly instead of popping.
_FADE_IN_S = 0.04
_FADE_OUT_S = 0.12
# Ceiling on the matched laugh peak, leaving headroom over the ducked voice.
_LAUGH_PEAK_CEIL = 0.9


class GoofyLaugh:
    """Speech-gated laugh generator that fires on a set interval, mixed over
    the voice."""

    def __init__(self, sample_rate: int, seed: int = 0) -> None:
        self.sample_rate = int(sample_rate)
        self._seed = seed

        self.interval_s = 15.0   # laugh roughly once every this many seconds
        self.gain = 0.0          # configured laugh mix level (at full intensity)
        self._scale = 1.0        # intensity scale, 0..1 (0 = laughs disabled)

        # Where a user-recorded clip is stored (overridable so tests can point
        # it at a temp file instead of the real user config dir).
        self._custom_path = _CUSTOM_PATH
        # Name of the active laugh (the single source of truth for which clip
        # is live): a bundled preset name ("goofy"/"scooby"), or "custom" for a
        # saved recording. Set by `_load_active_sample`; presets picked later
        # update it in memory only (see `select_laugh`).
        self.active_laugh = "goofy"
        self._sample = self._load_active_sample()

        # Monotonic count of laughs actually started, so an outside observer
        # (the engine) can detect a fire by watching it change -- without
        # polling the playhead. Deliberately NOT reset by `_init_state`/`reset`:
        # a reset that rewound it to 0 would read as a spurious "new laugh" to a
        # watcher holding a higher last-seen value.
        self.fire_count = 0
        self._init_state()

    @property
    def using_custom(self) -> bool:
        """True when a non-stock clip (a recording or a selected preset) is
        active. Derived from `active_laugh`, so there's one source of truth."""
        return self.active_laugh != "goofy"

    def _load_active_sample(self):
        """Pick the active laugh clip: a persisted override (a recording or a
        previously selected preset) if present, else the bundled stock clip,
        else None. Sets `active_laugh` accordingly."""
        if self._custom_path.exists():
            custom = _load_sample(self._custom_path, self.sample_rate)
            if custom is not None and custom.size > 0:
                self.active_laugh = "custom"
                return custom
        self.active_laugh = "goofy"
        return _load_sample(presets._SAMPLE_LAUGH_PATH, self.sample_rate)

    # -- configuration ---------------------------------------------------

    def set_interval_s(self, seconds: float) -> None:
        """Laugh roughly once every this many seconds of talking (0 disables)."""
        self.interval_s = max(float(seconds), 0.0)

    def set_gain(self, gain: float) -> None:
        """Laugh mix level (at full intensity)."""
        self.gain = min(max(float(gain), 0.0), 2.0)

    def set_intensity_scale(self, s: float) -> None:
        """Master-intensity gate: 0 disables laughs, > 0 enables them."""
        self._scale = min(max(float(s), 0.0), 1.0)

    # -- custom clip -----------------------------------------------------

    def save_custom(self, mono: np.ndarray) -> None:
        """Install a user recording as the active laugh, replacing the stock
        clip, and persist it to `~/.voicepranks/custom_laugh.wav` so it sticks
        across restarts. `mono` is float32 [-1, 1] at the engine sample rate.
        A silent/empty recording is ignored (the clip is left unchanged)."""
        data = np.asarray(mono, dtype=np.float32)
        if data.size == 0 or float(np.max(np.abs(data))) < 1e-4:
            raise ValueError("recording is empty or silent")
        self._custom_path.parent.mkdir(parents=True, exist_ok=True)
        wavio.write_wav(str(self._custom_path), self.sample_rate, data)
        # Copy so a later mutation of the caller's array can't corrupt our clip.
        self._sample = data.copy()
        self.active_laugh = "custom"

    def select_laugh(self, name: str) -> None:
        """Switch the live laugh to a bundled pre-recorded preset by name
        ("goofy" is the built-in stock clip). This is an in-memory switch only:
        it never writes to or deletes the persisted recording slot, so picking a
        preset can't destroy a laugh the user recorded. The choice lasts for the
        session; on the next start a saved recording (if any) is restored,
        otherwise the stock clip."""
        sounds_config = {}
        
        with open(presets._SOUNDS_CONFIG_PATH, "r") as sounds_config_file:
            sounds_config = json.load(sounds_config_file)

        if name not in sounds_config[presets._LISTED_SOUNDS_KEY]:
            raise ValueError(f"unknown laugh preset {name!r}")
        if name == self.active_laugh:
            return  # already live -- skip the redundant clip reload

        sample = _load_sample(presets._ASSETS_DIR / sounds_config[presets._LISTED_SOUNDS_KEY][name], self.sample_rate)
        if (sample is None or sample.size == 0):
            raise ValueError(f"laugh preset {name!r} could not be loaded")

        # For "goofy" a missing stock asset is tolerated: _sample=None falls
        # back to the synth giggle, matching reset_to_stock.
        self._sample = sample
        self.active_laugh = name

    def reset_to_stock(self) -> None:
        """Drop any user recording and revert to the bundled stock clip.
        Deletes the saved custom file so the revert also survives a restart."""
        # Best-effort delete: revert the in-memory clip even if the file can't
        # be removed (FileNotFoundError is an OSError, so this covers "already
        # gone" too).
        try:
            self._custom_path.unlink()
        except OSError:
            pass

        sounds_config = {}
        
        with open(presets._SOUNDS_CONFIG_PATH, "r") as sounds_config_file:
            sounds_config = json.load(sounds_config_file)
        
        self._sample = _load_sample(sounds_config[presets._LISTED_SOUNDS_KEY], self.sample_rate)
        self.active_laugh = "goofy"

    def reset(self) -> None:
        self._init_state()

    def _init_state(self) -> None:
        self._rng = np.random.default_rng(self._seed)
        self._laugh = np.zeros(0, dtype=np.float32)  # active laugh waveform
        self._fade = np.zeros(0, dtype=np.float64)   # its fade-in/out envelope
        self._laugh_amp = 0.0                        # volume-match multiplier
        self._pos = 0                                # playhead into _laugh
        self._since_laugh = 0                        # samples since the last laugh fired
        self._speech_env = 0.0                       # smoothed voice level

    # -- processing ------------------------------------------------------

    def process(self, mono: np.ndarray) -> np.ndarray:
        x = np.asarray(mono, dtype=np.float64)
        if x.size == 0:
            return np.zeros(0, dtype=np.float32)

        n = x.shape[0]
        out = x.copy()

        # Track a smoothed voice level (for gating, and to volume-match the
        # laugh to the voice when one fires).
        block_rms = float(np.sqrt(np.mean(x * x)))
        self._speech_env += 0.1 * (block_rms - self._speech_env)

        # Interval is counted in samples of processed audio passing through
        # (playback time). For the duration-preserving effects this equals real
        # time; gibberish engines are also duration-preserving in steady state,
        # so any deviation is a one-time priming offset, not cumulative drift.
        self._since_laugh += n
        playing = self._pos < self._laugh.shape[0]

        if not playing:
            # Fire once the interval has elapsed, while enabled and talking.
            due = self._since_laugh >= self.interval_s * self.sample_rate
            if self._scale > 0.0 and self.interval_s > 0.0 and due and self._speech_env > _SPEECH_GATE:
                self._begin_laugh()
                self._since_laugh = 0
                playing = self._laugh.shape[0] > 0

        if playing:
            take = min(n, self._laugh.shape[0] - self._pos)
            seg = self._laugh[self._pos:self._pos + take].astype(np.float64)
            fade = self._fade[self._pos:self._pos + take]
            wet = seg * fade * (self._laugh_amp * self.gain)
            # Duck the voice under the laugh, easing the duck in/out with the
            # same fade so the transition is smooth.
            duck = 1.0 - (1.0 - _VOICE_DUCK) * fade
            out[:take] = np.clip(out[:take] * duck + wet, -1.0, 1.0)
            self._pos += take

        result = out.astype(np.float32)
        np.nan_to_num(result, copy=False, nan=0.0, posinf=0.0, neginf=0.0)
        return result

    def laugh_clip(self) -> np.ndarray:
        """The waveform of one laugh (custom or stock clip, else synthesized) --
        the same audio the automatic overlay would play, for on-demand playback."""
        return self._next_laugh()

    @property
    def loaded_clip(self) -> np.ndarray | None:
        """Currently selected preloaded clip, without synthesizing on demand.

        Real-time consumers use this rather than :meth:`laugh_clip`, whose
        missing-asset fallback intentionally generates a fresh synthetic laugh.
        """
        return self._sample

    def _next_laugh(self) -> np.ndarray:
        """The waveform for one laugh: the real clip if bundled, else synth."""
        if self._sample is not None and self._sample.size > 0:
            return self._sample
        return self._synth_laugh()

    def _begin_laugh(self) -> None:
        """Start a laugh: pick the waveform, volume-match it to the current
        voice level, and build its fade-in/out envelope."""
        laugh = self._next_laugh()
        self._laugh = laugh
        self._pos = 0
        length = laugh.shape[0]
        if length == 0:
            self._fade = np.zeros(0, dtype=np.float64)
            self._laugh_amp = 0.0
            return

        # Volume-match: scale so the laugh's RMS ~ the recent voice RMS, then
        # cap so its peak leaves headroom over the ducked voice.
        clip = laugh.astype(np.float64)
        laugh_rms = float(np.sqrt(np.mean(clip * clip))) + 1e-9
        laugh_peak = float(np.max(np.abs(clip))) + 1e-9
        amp = self._speech_env / laugh_rms
        self._laugh_amp = min(amp, _LAUGH_PEAK_CEIL / laugh_peak)

        # Fade-in/out envelope over the whole clip.
        fade = np.ones(length, dtype=np.float64)
        fade_in = min(int(self.sample_rate * _FADE_IN_S), length // 2)
        fade_out = min(int(self.sample_rate * _FADE_OUT_S), length // 2)
        if fade_in > 0:
            fade[:fade_in] = np.linspace(0.0, 1.0, fade_in)
        if fade_out > 0:
            fade[-fade_out:] = np.linspace(1.0, 0.0, fade_out)
        self._fade = fade

        # Signal the fire only once the laugh is real (non-empty clip), so a
        # watcher's headphone echo lines up with actual audio going out.
        self.fire_count += 1

    def _synth_laugh(self) -> np.ndarray:
        """Synthesize one giggle: glottal source + breath + formants, "a-hyuck"."""
        sr = self.sample_rate
        rng = self._rng

        n_syllables = int(rng.integers(3, 5))  # 3-4 "huh"s + the "-uck"
        syllable_s = 0.15
        dur_s = n_syllables * syllable_s + 0.08
        num = int(dur_s * sr)
        if num <= 0:
            return np.zeros(0, dtype=np.float32)
        t = np.arange(num, dtype=np.float64) / sr

        # Pitch: a goofy upward "a-hyuuuck" lift with per-laugh variation and a
        # gentle warble.
        base_hz = 150.0 * (2.0 ** float(rng.uniform(-0.1, 0.1)))
        f0 = base_hz * (1.0 + 0.45 * (t / dur_s))
        f0 *= 1.0 + 0.04 * np.sin(2.0 * np.pi * 6.5 * t)
        phase = np.cumsum(2.0 * np.pi * f0 / sr)

        # Glottal source: a band-limited sawtooth (harmonics at 1/k, capped
        # below Nyquist) -- a buzzy voiced excitation with a natural rolloff.
        src = np.zeros(num, dtype=np.float64)
        max_hz = sr * 0.45
        k = 1
        while k * float(np.max(f0)) < max_hz and k <= 40:
            src += np.sin(k * phase) / k
            k += 1

        # Syllable amplitude envelope (raised-cosine "huh"s), overall taper.
        env = np.zeros(num, dtype=np.float64)
        breath = np.zeros(num, dtype=np.float64)
        half = syllable_s * 0.5
        for i in range(n_syllables):
            center = (i + 0.5) * syllable_s
            near = np.abs(t - center) < half
            env[near] += 0.5 * (1.0 + np.cos(np.pi * (t[near] - center) / half))
            # Breath "h" puff just before each vowel onset.
            onset = center - half
            puff = (t >= onset) & (t < onset + 0.045)
            breath[puff] += np.exp(-(t[puff] - onset) / 0.02)
        env *= np.linspace(1.0, 0.72, num)

        # "-ck" release: a short noise burst right at the end.
        ck = (t >= dur_s - 0.05) & (t < dur_s - 0.02)

        noise = rng.standard_normal(num)
        voiced = src * env
        aspiration = noise * (breath * 0.5 + env * 0.06 + ck * 0.6)
        laugh = voiced + aspiration

        # Formant shaping: color the source into a laugh vowel (reuse the
        # PeakingEQ biquad as resonant boosts).
        shaped = laugh.astype(np.float32)
        for center_hz, q, gain_db in _FORMANTS:
            shaped = PeakingEQ(sr, center_hz=center_hz, q=q, gain_db=gain_db).process(shaped)

        peak = float(np.max(np.abs(shaped))) + 1e-9
        return (shaped / peak * 0.85).astype(np.float32)
