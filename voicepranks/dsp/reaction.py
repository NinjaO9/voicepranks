"""Streaming loud-outburst detector and selected-clip overlay.

This deliberately detects a *shape* of audio (a short, loud utterance), not
words or a speaker's identity.  It is inexpensive enough to run in the audio
callback and keeps all clip loading outside that path.
"""
from __future__ import annotations

import numpy as np


class ReactionSound:
    """Recognize a brief high-energy utterance and mix a preloaded clip over it."""

    MIN_BURST_S = 0.08
    MAX_BURST_S = 0.90
    END_SILENCE_S = 0.12
    COOLDOWN_S = 2.0
    VOICE_DUCK = 0.6
    FADE_IN_S = 0.04
    FADE_OUT_S = 0.12

    def __init__(self, sample_rate: int) -> None:
        self.sample_rate = int(sample_rate)
        self.sensitivity = 0.5
        self.fire_count = 0
        self.reset()

    def set_sensitivity(self, value: float) -> None:
        self.sensitivity = min(max(float(value), 0.0), 1.0)

    def reset(self) -> None:
        self._burst_samples = 0
        self._burst_rms = 0.0
        self._quiet_samples = 0
        self._discarding = False
        self._cooldown_samples = 0
        self._clip = np.zeros(0, dtype=np.float32)
        self._clip_pos = 0
        self._clip_amp = 0.0

    @property
    def active(self) -> bool:
        return self._clip_pos < self._clip.shape[0]

    @property
    def clip(self) -> np.ndarray:
        """The clip belonging to the last fired reaction, for headphone echo."""
        return self._clip

    def _threshold(self) -> float:
        # 0 is deliberately conservative and 1 is suitable for quieter mics.
        return 0.18 - 0.14 * self.sensitivity

    def observe(self, mono: np.ndarray, clip: np.ndarray) -> None:
        """Advance recognition state and start `clip` after a qualifying burst."""
        x = np.asarray(mono, dtype=np.float32)
        if x.size == 0:
            return
        n = x.shape[0]
        self._cooldown_samples = max(0, self._cooldown_samples - n)
        rms = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
        loud = rms >= self._threshold()

        if loud:
            self._quiet_samples = 0
            self._burst_samples += n
            self._burst_rms = max(self._burst_rms, rms)
            if self._burst_samples > int(self.MAX_BURST_S * self.sample_rate):
                self._discarding = True
            return

        if self._burst_samples == 0:
            return
        self._quiet_samples += n
        if self._quiet_samples < int(self.END_SILENCE_S * self.sample_rate):
            return

        burst = self._burst_samples
        burst_rms = self._burst_rms
        rejected = self._discarding
        self._burst_samples = 0
        self._burst_rms = 0.0
        self._quiet_samples = 0
        self._discarding = False
        if self._cooldown_samples > 0 or rejected or self.active:
            return
        if burst >= int(self.MIN_BURST_S * self.sample_rate):
            self._start(clip, burst_rms)

    def _start(self, clip: np.ndarray, recent_rms: float) -> None:
        sample = np.asarray(clip, dtype=np.float32)
        if sample.size == 0:
            return
        self._clip = sample
        self._clip_pos = 0
        clip_rms = float(np.sqrt(np.mean(sample.astype(np.float64) ** 2))) + 1e-9
        clip_peak = float(np.max(np.abs(sample))) + 1e-9
        # Use the qualifying burst's level, but retain a small floor so quiet
        # microphones do not make the effect inaudible.
        amp = max(recent_rms, self._threshold()) / clip_rms
        self._clip_amp = min(amp, 0.9 / clip_peak)
        self._cooldown_samples = int(self.COOLDOWN_S * self.sample_rate)
        self.fire_count += 1

    def mix(self, mono: np.ndarray) -> np.ndarray:
        """Overlay the active clip while preserving mono float32 block shape."""
        x = np.asarray(mono, dtype=np.float32)
        if not self.active or x.size == 0:
            return x
        out = x.copy()
        take = min(out.shape[0], self._clip.shape[0] - self._clip_pos)
        positions = np.arange(self._clip_pos, self._clip_pos + take)
        remaining = self._clip.shape[0] - positions
        fade_in = max(1, int(self.FADE_IN_S * self.sample_rate))
        fade_out = max(1, int(self.FADE_OUT_S * self.sample_rate))
        fade = np.minimum(1.0, (positions + 1) / fade_in)
        fade = np.minimum(fade, remaining / fade_out).astype(np.float32)
        wet = self._clip[self._clip_pos:self._clip_pos + take] * fade * self._clip_amp
        duck = 1.0 - (1.0 - self.VOICE_DUCK) * fade
        out[:take] = np.clip(out[:take] * duck + wet, -1.0, 1.0)
        self._clip_pos += take
        return out
