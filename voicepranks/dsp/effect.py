"""The "Minion" voice effect: pitch shift only (flat, no EQ).

An EQ presence-boost stage was previously stacked on top of the pitch
shift, but chosen-by-ear testing showed the pitch shift alone reads as
correct/desired, and the EQ boost was not needed (and slightly hurt) once
the pitch shifter itself changed to WSOLA. The EQ stage (`PeakingEQ` in
`biquad.py`) is left intact and wired up (but at 0 dB / unused in the
process chain) so it can be re-enabled later without touching
`engine.py`/`ui/app.py`.

A second, alternate mode -- "Minionese" gibberish -- can be toggled on via
`set_gibberish()`. When enabled, `process()` routes through a gibberish
engine instead of the plain WSOLA pitch shift. There are two gibberish
engines and `set_use_shuffle()` picks between them:
  - `Minionese` (`dsp/minionese.py`): STFT formant-replacement.
  - `MinioneseShuffle` (`dsp/minionese_shuffle.py`): time-domain WSOLA +
    syllable-chunk shuffle (cleaner tone, scrambles real syllables).

The plain (non-gibberish) path also carries optional post-effects,
`Distortion` (`dsp/distortion.py`), `DeepFry` (`dsp/deep_fry.py`), and
`Reverb` (`dsp/reverb.py`), all disabled by default so the plain voice is
unchanged. Enabling distortion and reverb (plus a downward pitch shift via a
negative `max_semitones`) is how the "scary" preset builds a deeper,
gravelly, reverberant voice; DeepFry instead adds saturation, bit crushing,
and sample-hold aliasing.
"""
from __future__ import annotations

import numpy as np

from .biquad import PeakingEQ
from .deep_fry import DeepFry
from .distortion import Distortion
from .growl import Growl
from .laugh import GoofyLaugh
from .minionese import Minionese
from .minionese_shuffle import MinioneseShuffle
from .pitch import PitchShifter
from .reverb import Reverb

def _scale_from_neutral(neutral: float, target: float, t: float) -> float:
    """Interpolate a stage amount from its `neutral` value (at intensity 0) to
    its `target` value (at intensity 1). Most stages neutralize at 0, but some
    (e.g. distortion drive) neutralize at 1, so the neutral is explicit."""
    return neutral + (target - neutral) * t


class MinionEffect:
    """Pitch-shift-up voice effect, driven by intensity. Flat (no EQ) by
    default; the EQ stage can be re-enabled live via `set_eq_enabled`."""

    MAX_SEMITONES = 8.5
    MAX_EQ_GAIN_DB = 0.0
    EQ_CENTER_HZ = 2000.0
    EQ_Q = 1.0

    # Nasalizer: boost the nasal formant (~1 kHz) and a bit of high nasal
    # buzz (~2.4 kHz) while cutting the open-vowel formant (~550 Hz), which
    # together read as a pinched/honky "nasal" voice. Gains are the amounts at
    # nasality=1 and are scaled by (nasality * intensity).
    NASAL_PEAK_HZ, NASAL_PEAK_Q, NASAL_PEAK_DB = 1000.0, 1.5, 11.0
    NASAL_CUT_HZ, NASAL_CUT_Q, NASAL_CUT_DB = 550.0, 1.0, -9.0
    NASAL_HI_HZ, NASAL_HI_Q, NASAL_HI_DB = 2400.0, 2.0, 6.0

    def __init__(self, sample_rate: int, channels: int = 1) -> None:
        self.sample_rate = int(sample_rate)

        # Instance copies of the class-level defaults above, so each
        # MinionEffect can be tuned independently without touching the
        # module constants (which existing tests/selftest still rely on
        # as defaults).
        self.max_semitones = self.MAX_SEMITONES
        self.max_eq_gain_db = self.MAX_EQ_GAIN_DB
        self.eq_center_hz = self.EQ_CENTER_HZ
        self.eq_q = self.EQ_Q
        self.eq_enabled = False

        self.pitch = PitchShifter(sample_rate)
        self.eq = PeakingEQ(sample_rate, center_hz=self.eq_center_hz, q=self.eq_q, gain_db=0.0)
        # Nasalizer bands (transparent at nasality 0).
        self.nasality = 0.0
        self._nasal_peak = PeakingEQ(sample_rate, self.NASAL_PEAK_HZ, self.NASAL_PEAK_Q, 0.0)
        self._nasal_cut = PeakingEQ(sample_rate, self.NASAL_CUT_HZ, self.NASAL_CUT_Q, 0.0)
        self._nasal_hi = PeakingEQ(sample_rate, self.NASAL_HI_HZ, self.NASAL_HI_Q, 0.0)
        self.minionese = Minionese(sample_rate)
        self.shuffle = MinioneseShuffle(sample_rate)
        self.gibberish = False
        self.use_shuffle = True  # which gibberish engine: shuffle (True) or formant

        # Optional post-effects on the plain path (all off by default, so the
        # plain voice is untouched unless a preset like "scary" enables them).
        # Their *amount* knobs are stored as targets (the value at full
        # intensity) and scaled by intensity in `set_intensity`, so the whole
        # character fades in with the intensity control -- at intensity 0 the
        # voice is unprocessed, at 1 it is the full effect.
        self.growl = Growl(sample_rate)
        self.distortion = Distortion(sample_rate)
        self.deep_fry = DeepFry(sample_rate)
        self.reverb = Reverb(sample_rate)
        self.laugh = GoofyLaugh(sample_rate)
        self.growl_enabled = False
        self.distortion_enabled = False
        self.deep_fry_enabled = False
        self.reverb_enabled = False
        self.laugh_enabled = False
        self.growl_depth = self.growl.depth          # target at intensity 1
        self.distortion_drive = self.distortion.drive  # target at intensity 1
        self.deep_fry_drive = self.deep_fry.drive
        self.deep_fry_bit_depth = self.deep_fry.bit_depth
        self.deep_fry_downsample = self.deep_fry.downsample
        self.reverb_mix = self.reverb.mix            # target at intensity 1

        self.intensity = 0.0

    def _gibberish_engine(self):
        return self.shuffle if self.use_shuffle else self.minionese

    def set_intensity(self, t: float) -> None:
        t = min(max(float(t), 0.0), 1.0)
        self.intensity = t
        self.pitch.set_semitones(self.max_semitones * t)
        self.eq.set_gain_db(self.max_eq_gain_db * t)
        # Nasalizer bands scale with (nasality * intensity).
        g = self.nasality * t
        self._nasal_peak.set_gain_db(self.NASAL_PEAK_DB * g)
        self._nasal_cut.set_gain_db(self.NASAL_CUT_DB * g)
        self._nasal_hi.set_gain_db(self.NASAL_HI_DB * g)
        self.minionese.set_intensity(t)
        self.shuffle.set_intensity(t)
        # Plain-path post-effects scale with intensity too, each fading to its
        # neutral value at t=0: growl -> no modulation, distortion -> clean
        # (drive 1.0), reverb -> fully dry.
        self.growl.set_depth(_scale_from_neutral(0.0, self.growl_depth, t))
        self.distortion.set_drive(_scale_from_neutral(1.0, self.distortion_drive, t))

        self.deep_fry.set_drive(_scale_from_neutral(1.0, self.deep_fry_drive, t))
        self.deep_fry.set_bit_depth(round(_scale_from_neutral(16, self.deep_fry_bit_depth, t)))
        self.deep_fry.set_downsample(round(_scale_from_neutral(1, self.deep_fry_downsample, t)))
        
        self.reverb.set_mix(_scale_from_neutral(0.0, self.reverb_mix, t))
        self.laugh.set_intensity_scale(t)

    def set_gibberish(self, b: bool) -> None:
        self.gibberish = bool(b)

    def set_use_shuffle(self, b: bool) -> None:
        """Pick the gibberish engine: True -> time-domain shuffle
        (`MinioneseShuffle`), False -> STFT formant (`Minionese`)."""
        self.use_shuffle = bool(b)

    def latency_ms(self) -> float:
        """Algorithmic buffering latency of the *currently active* path, in
        ms. Zero for the plain pitch shift (WSOLA emits continuously); the
        active gibberish engine's own latency when gibberish is on. The
        engine uses this to size its output pre-fill so the live path never
        starves while the effect's internal buffers prime."""
        if self.gibberish:
            return self._gibberish_engine().latency_ms()
        return 0.0

    def set_max_semitones(self, semitones: float) -> None:
        self.max_semitones = float(semitones)
        self.set_intensity(self.intensity)

    def set_eq_enabled(self, b: bool) -> None:
        self.eq_enabled = bool(b)

    def set_eq_gain_db(self, gain_db: float) -> None:
        self.max_eq_gain_db = float(gain_db)
        self.set_intensity(self.intensity)

    def set_eq_center_hz(self, center_hz: float) -> None:
        self.eq_center_hz = float(center_hz)
        self.eq.set_center_hz(self.eq_center_hz)

    def set_eq_q(self, q: float) -> None:
        self.eq_q = float(q)
        self.eq.set_q(self.eq_q)

    def set_nasality(self, n: float) -> None:
        """Nasal (honky/pinched) coloration amount, 0..1; scaled by intensity."""
        self.nasality = min(max(float(n), 0.0), 1.0)
        self.set_intensity(self.intensity)

    def set_growl_enabled(self, b: bool) -> None:
        self.growl_enabled = bool(b)

    def set_growl_rate_hz(self, rate_hz: float) -> None:
        self.growl.set_rate_hz(float(rate_hz))

    def set_growl_depth(self, depth: float) -> None:
        self.growl_depth = float(depth)
        self.set_intensity(self.intensity)

    def set_distortion_enabled(self, b: bool) -> None:
        self.distortion_enabled = bool(b)

    def set_distortion_drive(self, drive: float) -> None:
        self.distortion_drive = float(drive)
        self.set_intensity(self.intensity)

    def set_deep_fry_enabled(self, b: bool) -> None:
        self.deep_fry_enabled = bool(b)

    def set_deep_fry_drive(self, drive: float) -> None:
        self.deep_fry_drive = float(drive)
        self.set_intensity(self.intensity)

    def set_deep_fry_bit_depth(self, bit_depth: int) -> None:
        self.deep_fry_bit_depth = int(bit_depth)
        self.set_intensity(self.intensity)

    def set_deep_fry_downsample(self, downsample: int) -> None:
        self.deep_fry_downsample = int(downsample)
        self.set_intensity(self.intensity)

    def set_reverb_enabled(self, b: bool) -> None:
        self.reverb_enabled = bool(b)

    def set_reverb_mix(self, mix: float) -> None:
        self.reverb_mix = float(mix)
        self.set_intensity(self.intensity)

    def set_reverb_room_size(self, room_size: float) -> None:
        self.reverb.set_room_size(float(room_size))

    def set_reverb_damp(self, damp: float) -> None:
        self.reverb.set_damp(float(damp))

    def set_laugh_enabled(self, b: bool) -> None:
        self.laugh_enabled = bool(b)

    def set_laugh_interval_s(self, seconds: float) -> None:
        self.laugh.set_interval_s(float(seconds))

    def set_laugh_gain(self, gain: float) -> None:
        self.laugh.set_gain(float(gain))

    def save_custom_laugh(self, mono) -> None:
        """Install a recording as the goofy-laugh clip (see GoofyLaugh.save_custom)."""
        self.laugh.save_custom(mono)

    def reset_custom_laugh(self) -> None:
        """Revert the goofy laugh to the bundled stock clip."""
        self.laugh.reset_to_stock()

    def select_laugh(self, name: str) -> None:
        """Switch to a bundled pre-recorded laugh preset (see GoofyLaugh.select_laugh)."""
        self.laugh.select_laugh(name)

    def laugh_clip(self):
        """One laugh waveform for on-demand playback (see GoofyLaugh.laugh_clip)."""
        return self.laugh.laugh_clip()

    @property
    def active_laugh(self) -> str:
        """Name of the active laugh clip ('goofy', 'scooby', or 'custom')."""
        return self.laugh.active_laugh

    @property
    def custom_laugh(self) -> bool:
        """True when a non-stock laugh is live -- a recording or a selected
        preset -- vs the built-in goofy stock clip."""
        return self.laugh.using_custom

    def reset(self) -> None:
        self.pitch.reset()
        self.eq.reset()
        self._nasal_peak.reset()
        self._nasal_cut.reset()
        self._nasal_hi.reset()
        self.minionese.reset()
        self.shuffle.reset()
        self.growl.reset()
        self.distortion.reset()
        self.deep_fry.reset()
        self.reverb.reset()
        self.laugh.reset()

    def process(self, mono: np.ndarray) -> np.ndarray:
        if self.gibberish:
            out = self._gibberish_engine().process(mono)
        else:
            out = self.pitch.process(mono)
            if self.eq_enabled:
                out = self.eq.process(out)
            if self.growl_enabled:
                out = self.growl.process(out)
            if self.distortion_enabled:
                out = self.distortion.process(out)
            if self.deep_fry_enabled:
                out = self.deep_fry.process(out)
            # Nasal shaping after distortion so it colors the final tone (grit
            # added by the distortion would otherwise re-flatten the resonances).
            if self.nasality > 0.0:
                out = self._nasal_cut.process(self._nasal_peak.process(out))
                out = self._nasal_hi.process(out)
            if self.reverb_enabled:
                out = self.reverb.process(out)
        # The laugh is a top-level overlay: it works on top of *any* voice
        # (plain or gibberish), controlled by its own toggle, not by presets.
        if self.laugh_enabled:
            out = self.laugh.process(out)
        # Safety brick-wall: EQ boost + reverb wet + distortion can sum past
        # full scale; clamp so the output stays in range instead of the audio
        # device hard-clipping arbitrary out-of-range samples. Transparent
        # below 0 dBFS (only touches samples that would have clipped anyway).
        np.clip(out, -1.0, 1.0, out=out)
        return out
