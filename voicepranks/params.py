"""Single source of truth for every tunable parameter in the app.

`PARAM_SPECS` describes the full set of knobs (name, group, UI label,
kind, range, default, whether applying it causes a brief DSP-stage
rebuild/glitch). Both the Tkinter UI (`ui/app.py`) and the HTTP control
server (`control_server.py`) drive the engine exclusively through this
registry (`VoiceEngine.set_param`/`snapshot`) so neither one ever
hardcodes the param list -- add a knob here and it shows up everywhere.

Params are named with a dotted `group.field` convention, e.g.
`"minionese.chunk_ms"`. The `global` group has no dot prefix (`enabled`,
`gibberish`, `intensity`, `ramp.duration_s`) since it isn't tied to one
DSP stage.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict

from .dsp import minionese as minionese_mod
from .dsp import minionese_shuffle as shuffle_mod
from .dsp.effect import MinionEffect
from .dsp.pitch import PitchShifter
from .dsp.ramp import IntensityRamp


@dataclass(frozen=True)
class ParamSpec:
    """Describes one adjustable parameter, independent of any live object."""

    name: str
    group: str
    label: str
    kind: str  # "float" | "int" | "bool"
    min: float
    max: float
    step: float
    default: float
    needs_reset: bool = False  # applying it rebuilds a DSP stage (brief glitch)


PARAM_SPECS = [
    # -- global ----------------------------------------------------------
    ParamSpec("enabled", "global", "Enabled", "bool", 0, 1, 1, False),
    ParamSpec("gibberish", "global", "Minionese (gibberish)", "bool", 0, 1, 1, False),
    ParamSpec("intensity", "global", "Intensity (manual)", "float", 0.0, 1.0, 0.01, 1.0),
    ParamSpec("output_gain", "global", "Output mic volume", "float", 0.0, 4.0, 0.05, 1.0),
    ParamSpec("playback_gain", "global", "Playback volume", "float", 0.0, 4.0, 0.05, 1.0),
    ParamSpec("monitor", "global", "Live monitor (hear effect)", "bool", 0, 1, 1, False),
    ParamSpec("ramp.duration_s", "global", "Ramp duration (s)", "float", 0.0, 86400.0, 1.0, IntensityRamp().duration_s),

    # Desktop-only visual/audio scare. The timer is owned by the engine so
    # the localhost API can configure it too; only the Tk app presents it.
    ParamSpec("jumpscare.enabled", "jumpscare", "Enable fullscreen jumpscare", "bool", 0, 1, 1, False),
    ParamSpec("jumpscare.interval_s", "jumpscare", "Jumpscare every N seconds", "float", 1.0, 86400.0, 1.0, 60.0),


    # Random sound bursts
    ParamSpec("random_burst.enabled", "random_burst", "Enable random voice bursts", "bool", 0, 1, 1, False),
    ParamSpec("random_burst.interval_min_s", "random_burst", "Wait at least (s)", "float", 0.0, 100.0, 0.5, 10.0),
    ParamSpec("random_burst.interval_max_s", "random_burst", "Wait at most (s)", "float", 0.0, 100.0, 0.5, 30.0),
    ParamSpec("random_burst.duration_min_s", "random_burst", "Burst lasts at least (s)", "float", 0.1, 100.0, 0.1, 2.0),
    ParamSpec("random_burst.duration_max_s", "random_burst", "Burst lasts at most (s)", "float", 0.1, 100.0, 0.1, 5.0),
    ParamSpec("random_burst.use_animalese", "random_burst", "Use Animalese", "bool", 0, 1, 1, True),
    ParamSpec("random_burst.use_minion", "random_burst", "Use Minion", "bool", 0, 1, 1, True),
    ParamSpec("random_burst.use_scary", "random_burst", "Use Scary", "bool", 0, 1, 1, True),
    ParamSpec("random_burst.use_goofy", "random_burst", "Use Goofy", "bool", 0, 1, 1, True),
    ParamSpec("random_brurst.use_deepfried", "random_burst", "Use DeepFried", "bool", 0, 1, 1, True),

    # -- plain mode (effect.py, flat pitch shift + optional EQ) ----------
    # Range spans negatives so the plain path can pitch *down* (deeper voice,
    # e.g. the "scary" preset) as well as up (chipmunk). Default stays positive.
    ParamSpec("effect.max_semitones", "effect", "Max semitones", "float", -12.0, 12.0, 0.1, MinionEffect.MAX_SEMITONES),
    ParamSpec("effect.eq_enabled", "effect", "EQ enabled", "bool", 0, 1, 1, False),
    ParamSpec("effect.eq_gain_db", "effect", "EQ gain (dB)", "float", -12.0, 12.0, 0.5, MinionEffect.MAX_EQ_GAIN_DB),
    ParamSpec("effect.eq_center_hz", "effect", "EQ center (Hz)", "float", 200.0, 6000.0, 50.0, MinionEffect.EQ_CENTER_HZ),
    ParamSpec("effect.eq_q", "effect", "EQ Q", "float", 0.3, 4.0, 0.1, MinionEffect.EQ_Q),
    ParamSpec("effect.nasality", "effect", "Nasality", "float", 0.0, 1.0, 0.05, 0.0),

    # -- growl (ring/AM modulation, plain-path post-effect, off by default) -
    # Rate spans from slow tremolo/warble (a few Hz, e.g. the "goofy" wobble)
    # up to fast ring-mod roughness (the "scary" growl).
    ParamSpec("growl.enabled", "growl", "Growl enabled", "bool", 0, 1, 1, False),
    ParamSpec("growl.rate_hz", "growl", "Growl rate (Hz)", "float", 3.0, 200.0, 1.0, 70.0),
    ParamSpec("growl.depth", "growl", "Growl depth", "float", 0.0, 1.0, 0.05, 0.0),

    # -- distortion (plain-path post-effect, off by default) --------------
    ParamSpec("distortion.enabled", "distortion", "Distortion enabled", "bool", 0, 1, 1, False),
    ParamSpec("distortion.drive", "distortion", "Drive (grit)", "float", 1.0, 25.0, 0.5, 1.0),

    # -- deep fry (saturation + bit crushing + rate reduction) ------------
    ParamSpec("deep_fry.enabled", "deep_fry", "Deep Fry enabled", "bool", 0, 1, 1, False),
    ParamSpec("deep_fry.drive", "deep_fry", "Saturation", "float", 1.0, 30.0, 0.5, 1.0),
    ParamSpec("deep_fry.bit_depth", "deep_fry", "Bit depth", "int", 2, 16, 1, 16),
    ParamSpec("deep_fry.downsample", "deep_fry", "Rate reduction", "int", 1, 32, 1, 1),

    # -- reverb (plain-path post-effect, off by default) ------------------
    ParamSpec("reverb.enabled", "reverb", "Reverb enabled", "bool", 0, 1, 1, False),
    ParamSpec("reverb.mix", "reverb", "Wet/dry mix", "float", 0.0, 1.0, 0.05, 0.0),
    ParamSpec("reverb.room_size", "reverb", "Room size", "float", 0.0, 1.0, 0.05, 0.5),
    ParamSpec("reverb.damp", "reverb", "Damping", "float", 0.0, 1.0, 0.05, 0.5),

    # -- laugh (random procedural giggle generator, off by default) -------
    ParamSpec("laugh.enabled", "laugh", "Random laugh enabled", "bool", 0, 1, 1, False),
    ParamSpec("laugh.interval_s", "laugh", "Laugh every N seconds", "float", 1.0, 120.0, 1.0, 15.0),
    ParamSpec("laugh.gain", "laugh", "Laugh volume", "float", 0.0, 2.0, 0.05, 1.0),

    # -- reaction sounds (brief loud outburst -> selected laugh clip) ------
    ParamSpec("reaction.enabled", "reaction", "Reaction sounds enabled", "bool", 0, 1, 1, False),
    ParamSpec("reaction.sensitivity", "reaction", "Reaction sensitivity", "float", 0.0, 1.0, 0.05, 0.5),

    # -- minionese (gibberish mode) ---------------------------------------
    ParamSpec("minionese.semitones", "minionese", "Semitones", "float", 0.0, 12.0, 0.1, minionese_mod.SEMITONES),
    ParamSpec("minionese.wobble_ms", "minionese", "Wobble depth (ms)", "float", 0.0, 12.0, 0.1, minionese_mod.VIB_MS),
    ParamSpec("minionese.chunk_ms", "minionese", "Syllable length (ms)", "float", 40.0, 400.0, 5.0, minionese_mod.SYLL_MS, needs_reset=True),
    ParamSpec("minionese.shuffle_k", "minionese", "Vowel pool size", "int", 1, 8, 1, 5, needs_reset=True),
    ParamSpec(
        "minionese.fade_ms",
        "minionese",
        "Syllable fade (ms)",
        "float",
        0.0,
        60.0,
        1.0,
        minionese_mod.ATK * minionese_mod.SYLL_MS,
        needs_reset=True,
    ),
    ParamSpec("minionese.max_slew", "minionese", "Max output slew", "float", 0.02, 1.0, 0.01, minionese_mod.MAX_SLEW),
    ParamSpec("minionese.floor", "minionese", "Gate floor", "float", 0.0, 1.0, 0.05, minionese_mod.FLOOR),
    ParamSpec("minionese.resid_f", "minionese", "Residual mix", "float", 0.0, 1.0, 0.05, minionese_mod.RESID_F),
    ParamSpec("minionese.vad_thresh", "minionese", "VAD threshold", "float", 0.0, 0.5, 0.01, minionese_mod.VAD_REL_THRESH),
    ParamSpec("minionese.use_shuffle", "minionese", "Shuffle engine (vs formant)", "bool", 0, 1, 1, True),

    # -- shuffle gibberish engine (time-domain WSOLA + chunk shuffle) -----
    ParamSpec("shuffle.semitones", "shuffle", "Semitones", "float", 0.0, 12.0, 0.1, shuffle_mod.SEMITONES),
    ParamSpec("shuffle.wobble_ms", "shuffle", "Wobble depth (ms)", "float", 0.0, 12.0, 0.1, shuffle_mod.WOBBLE_MS),
    ParamSpec("shuffle.chunk_ms", "shuffle", "Chunk length (ms)", "float", 40.0, 400.0, 5.0, shuffle_mod.CHUNK_MS, needs_reset=True),
    ParamSpec("shuffle.shuffle_k", "shuffle", "Chunks per window", "int", 1, 8, 1, shuffle_mod.SHUFFLE_K, needs_reset=True),
    ParamSpec("shuffle.fade_ms", "shuffle", "Crossfade (ms)", "float", 0.0, 60.0, 1.0, shuffle_mod.FADE_MS, needs_reset=True),
    ParamSpec("shuffle.reverse_prob", "shuffle", "Reverse chunks (prob)", "float", 0.0, 1.0, 0.05, 0.0),
    ParamSpec("shuffle.nasality", "shuffle", "Nasality", "float", 0.0, 1.0, 0.05, 0.0),
    ParamSpec("shuffle.max_slew", "shuffle", "Max output slew", "float", 0.02, 2.0, 0.01, shuffle_mod.MAX_SLEW),

    # -- pitch engine (advanced, shared WSOLA implementation) -------------
    ParamSpec("pitch.frame", "pitch", "WSOLA frame size", "int", 256, 4096, 64, 1024, needs_reset=True),
    ParamSpec("pitch.tol", "pitch", "WSOLA search tolerance", "int", 32, 512, 8, 256, needs_reset=True),

    # -- I/O (apply on (re)start) -----------------------------------------
    ParamSpec("io.sample_rate", "io", "Sample rate (Hz)", "int", 8000, 192000, 1000, 48000, needs_reset=True),
    ParamSpec("io.blocksize", "io", "Block size (samples)", "int", 64, 4096, 64, 256, needs_reset=True),
    ParamSpec("io.ring_ms", "io", "Output ring buffer (ms)", "float", 50.0, 1000.0, 10.0, 200.0, needs_reset=True),
    ParamSpec("io.input_device", "io", "Input device", "int", -1, 999, 1, -1, needs_reset=True),
    ParamSpec("io.output_device", "io", "Output device", "int", -1, 999, 1, -1, needs_reset=True),
    ParamSpec("io.playback_device", "io", "Playback device (-1=default)", "int", -1, 999, 1, -1),
]

PARAM_SPECS_BY_NAME: Dict[str, ParamSpec] = {spec.name: spec for spec in PARAM_SPECS}


@dataclass(frozen=True)
class ParamHandlers:
    """Getter/setter closures bound to one live object (engine or effect)."""

    get: Callable[[], Any]
    set: Callable[[Any], None]


def build_effect_registry(effect: MinionEffect) -> Dict[str, ParamHandlers]:
    """Registry for the params that live entirely on a `MinionEffect`
    instance (`effect.*`, `minionese.*`, `pitch.*`). Used both by
    `VoiceEngine` (bound to its live `effect`) and by the recorder's
    `render_current()`, which configures a scratch `MinionEffect` from a
    snapshot without needing a running engine.
    """
    pitch: PitchShifter = effect.pitch
    m = effect.minionese
    sh = effect.shuffle
    reg: Dict[str, ParamHandlers] = {
        "effect.max_semitones": ParamHandlers(
            get=lambda: effect.max_semitones,
            set=lambda v: effect.set_max_semitones(float(v)),
        ),
        "effect.eq_enabled": ParamHandlers(
            get=lambda: effect.eq_enabled,
            set=lambda v: effect.set_eq_enabled(bool(v)),
        ),
        "effect.eq_gain_db": ParamHandlers(
            get=lambda: effect.max_eq_gain_db,
            set=lambda v: effect.set_eq_gain_db(float(v)),
        ),
        "effect.eq_center_hz": ParamHandlers(
            get=lambda: effect.eq_center_hz,
            set=lambda v: effect.set_eq_center_hz(float(v)),
        ),
        "effect.eq_q": ParamHandlers(
            get=lambda: effect.eq_q,
            set=lambda v: effect.set_eq_q(float(v)),
        ),
        "effect.nasality": ParamHandlers(
            get=lambda: effect.nasality,
            set=lambda v: effect.set_nasality(float(v)),
        ),
        "growl.enabled": ParamHandlers(
            get=lambda: effect.growl_enabled,
            set=lambda v: effect.set_growl_enabled(bool(v)),
        ),
        "growl.rate_hz": ParamHandlers(
            get=lambda: effect.growl.rate_hz,
            set=lambda v: effect.set_growl_rate_hz(float(v)),
        ),
        "growl.depth": ParamHandlers(
            get=lambda: effect.growl_depth,
            set=lambda v: effect.set_growl_depth(float(v)),
        ),
        "distortion.enabled": ParamHandlers(
            get=lambda: effect.distortion_enabled,
            set=lambda v: effect.set_distortion_enabled(bool(v)),
        ),
        "distortion.drive": ParamHandlers(
            get=lambda: effect.distortion_drive,
            set=lambda v: effect.set_distortion_drive(float(v)),
        ),
        "deep_fry.enabled": ParamHandlers(
            get=lambda: effect.deep_fry_enabled,
            set=lambda v: effect.set_deep_fry_enabled(bool(v)),
        ),
        "deep_fry.drive": ParamHandlers(
            get=lambda: effect.deep_fry_drive,
            set=lambda v: effect.set_deep_fry_drive(float(v)),
        ),
        "deep_fry.bit_depth": ParamHandlers(
            get=lambda: effect.deep_fry_bit_depth,
            set=lambda v: effect.set_deep_fry_bit_depth(int(v)),
        ),
        "deep_fry.downsample": ParamHandlers(
            get=lambda: effect.deep_fry_downsample,
            set=lambda v: effect.set_deep_fry_downsample(int(v)),
        ),
        "reverb.enabled": ParamHandlers(
            get=lambda: effect.reverb_enabled,
            set=lambda v: effect.set_reverb_enabled(bool(v)),
        ),
        "reverb.mix": ParamHandlers(
            get=lambda: effect.reverb_mix,
            set=lambda v: effect.set_reverb_mix(float(v)),
        ),
        "reverb.room_size": ParamHandlers(
            get=lambda: effect.reverb.room_size,
            set=lambda v: effect.set_reverb_room_size(float(v)),
        ),
        "reverb.damp": ParamHandlers(
            get=lambda: effect.reverb.damp,
            set=lambda v: effect.set_reverb_damp(float(v)),
        ),
        "laugh.enabled": ParamHandlers(
            get=lambda: effect.laugh_enabled,
            set=lambda v: effect.set_laugh_enabled(bool(v)),
        ),
        "laugh.interval_s": ParamHandlers(
            get=lambda: effect.laugh.interval_s,
            set=lambda v: effect.set_laugh_interval_s(float(v)),
        ),
        "laugh.gain": ParamHandlers(
            get=lambda: effect.laugh.gain,
            set=lambda v: effect.set_laugh_gain(float(v)),
        ),
        "reaction.enabled": ParamHandlers(
            get=lambda: effect.reaction_enabled,
            set=lambda v: effect.set_reaction_enabled(bool(v)),
        ),
        "reaction.sensitivity": ParamHandlers(
            get=lambda: effect.reaction.sensitivity,
            set=lambda v: effect.set_reaction_sensitivity(float(v)),
        ),
        "minionese.semitones": ParamHandlers(
            get=lambda: m.semitones,
            set=lambda v: m.set_semitones(float(v)),
        ),
        "minionese.wobble_ms": ParamHandlers(
            get=lambda: m.wobble_ms,
            set=lambda v: m.set_wobble_ms(float(v)),
        ),
        "minionese.chunk_ms": ParamHandlers(
            get=lambda: m.chunk_ms,
            set=lambda v: m.set_chunk_ms(float(v)),
        ),
        "minionese.shuffle_k": ParamHandlers(
            get=lambda: m.shuffle_k,
            set=lambda v: m.set_shuffle_k(int(v)),
        ),
        "minionese.fade_ms": ParamHandlers(
            get=lambda: m.fade_ms,
            set=lambda v: m.set_fade_ms(float(v)),
        ),
        "minionese.max_slew": ParamHandlers(
            get=lambda: m.max_slew,
            set=lambda v: m.set_max_slew(float(v)),
        ),
        "minionese.floor": ParamHandlers(
            get=lambda: m.floor,
            set=lambda v: m.set_floor(float(v)),
        ),
        "minionese.resid_f": ParamHandlers(
            get=lambda: m.resid_f,
            set=lambda v: m.set_resid_f(float(v)),
        ),
        "minionese.vad_thresh": ParamHandlers(
            get=lambda: m.vad_thresh,
            set=lambda v: m.set_vad_thresh(float(v)),
        ),
        "minionese.use_shuffle": ParamHandlers(
            get=lambda: effect.use_shuffle,
            set=lambda v: effect.set_use_shuffle(bool(v)),
        ),
        "shuffle.semitones": ParamHandlers(
            get=lambda: sh.semitones,
            set=lambda v: sh.set_semitones(float(v)),
        ),
        "shuffle.wobble_ms": ParamHandlers(
            get=lambda: sh.wobble_ms,
            set=lambda v: sh.set_wobble_ms(float(v)),
        ),
        "shuffle.chunk_ms": ParamHandlers(
            get=lambda: sh.chunk_ms,
            set=lambda v: sh.set_chunk_ms(float(v)),
        ),
        "shuffle.shuffle_k": ParamHandlers(
            get=lambda: sh.shuffle_k,
            set=lambda v: sh.set_shuffle_k(int(v)),
        ),
        "shuffle.fade_ms": ParamHandlers(
            get=lambda: sh.fade_ms,
            set=lambda v: sh.set_fade_ms(float(v)),
        ),
        "shuffle.reverse_prob": ParamHandlers(
            get=lambda: sh.reverse_prob,
            set=lambda v: sh.set_reverse_prob(float(v)),
        ),
        "shuffle.nasality": ParamHandlers(
            get=lambda: sh.nasality,
            set=lambda v: sh.set_nasality(float(v)),
        ),
        "shuffle.max_slew": ParamHandlers(
            get=lambda: sh.max_slew,
            set=lambda v: sh.set_max_slew(float(v)),
        ),
        "pitch.frame": ParamHandlers(
            get=lambda: pitch.L,
            set=lambda v: pitch.set_frame(int(v)),
        ),
        "pitch.tol": ParamHandlers(
            get=lambda: pitch.tol,
            set=lambda v: pitch.set_tol(int(v)),
        ),
    }
    return reg


def build_engine_registry(engine) -> Dict[str, ParamHandlers]:
    """Full registry for a live `VoiceEngine`: global + effect + I/O."""
    reg: Dict[str, ParamHandlers] = {
        "enabled": ParamHandlers(
            get=lambda: engine.enabled,
            set=lambda v: engine.set_enabled(bool(v)),
        ),
        "gibberish": ParamHandlers(
            get=lambda: engine.effect.gibberish,
            set=lambda v: engine.set_gibberish(bool(v)),
        ),
        "intensity": ParamHandlers(
            # `effect.intensity` is only refreshed inside the audio
            # callback (from the manual override or the auto-ramp), so
            # while stopped -- or before the next block runs -- it can lag
            # a manual override that was just set. Prefer the manual
            # override itself when one is pending so `snapshot()` (and
            # thus `render_current()`) reflects the *intended* value
            # immediately, not just whatever the last processed block saw.
            get=lambda: (
                engine._manual_intensity if engine._manual_intensity is not None else engine.effect.intensity
            ),
            set=lambda v: engine.set_manual_intensity(float(v)),
        ),
        "output_gain": ParamHandlers(
            get=lambda: engine.output_gain,
            set=lambda v: engine.set_output_gain(float(v)),
        ),
        "playback_gain": ParamHandlers(
            get=lambda: engine.playback_gain,
            set=lambda v: engine.set_playback_gain(float(v)),
        ),
        "monitor": ParamHandlers(
            get=lambda: engine.monitor_enabled,
            set=lambda v: engine.set_monitor_enabled(bool(v)),
        ),
        "ramp.duration_s": ParamHandlers(
            get=lambda: engine.ramp.duration_s,
            set=lambda v: engine.ramp.set_duration(float(v)),
        ),
        "jumpscare.enabled": ParamHandlers(
            get=lambda: engine.jumpscare_enabled,
            set=lambda v: engine.set_jumpscare_enabled(bool(v)),
        ),
        "jumpscare.interval_s": ParamHandlers(
            get=lambda: engine.jumpscare_interval_s,
            set=lambda v: engine.set_jumpscare_interval_s(float(v)),
        ),
        "random_burst.enabled": ParamHandlers(
            get=lambda: engine.random_burst_enabled,
            set=lambda v: engine.set_random_burst_enabled(bool(v)),
        ),
        "random_burst.interval_min_s": ParamHandlers(
            get=lambda: engine.random_burst_interval_min_s,
            set=lambda v: engine.set_random_burst_interval_min_s(float(v)),
        ),
        "random_burst.interval_max_s": ParamHandlers(
            get=lambda: engine.random_burst_interval_max_s,
            set=lambda v: engine.set_random_burst_interval_max_s(float(v)),
        ),
        "random_burst.duration_min_s": ParamHandlers(
            get=lambda: engine.random_burst_duration_min_s,
            set=lambda v: engine.set_random_burst_duration_min_s(float(v)),
        ),
        "random_burst.duration_max_s": ParamHandlers(
            get=lambda: engine.random_burst_duration_max_s,
            set=lambda v: engine.set_random_burst_duration_max_s(float(v)),
        ),
        "random_burst.use_animalese": ParamHandlers(
            get=lambda: engine.random_burst_presets["animalese"],
            set=lambda v: engine.set_random_burst_preset("animalese", bool(v)),
        ),
        "random_burst.use_minion": ParamHandlers(
            get=lambda: engine.random_burst_presets["minion"],
            set=lambda v: engine.set_random_burst_preset("minion", bool(v)),
        ),
        "random_burst.use_scary": ParamHandlers(
            get=lambda: engine.random_burst_presets["scary"],
            set=lambda v: engine.set_random_burst_preset("scary", bool(v)),
        ),
        "random_burst.use_goofy": ParamHandlers(
            get=lambda: engine.random_burst_presets["goofy"],
            set=lambda v: engine.set_random_burst_preset("goofy", bool(v)),
        ),
        "io.sample_rate": ParamHandlers(
            get=lambda: engine.sample_rate,
            set=lambda v: engine.set_io_param("io.sample_rate", int(v)),
        ),
        "io.blocksize": ParamHandlers(
            get=lambda: engine.blocksize,
            set=lambda v: engine.set_io_param("io.blocksize", int(v)),
        ),
        "io.ring_ms": ParamHandlers(
            get=lambda: engine.ring_ms,
            set=lambda v: engine.set_io_param("io.ring_ms", float(v)),
        ),
        "io.input_device": ParamHandlers(
            get=lambda: -1 if engine.input_device is None else int(engine.input_device),
            set=lambda v: engine.set_io_param("io.input_device", None if int(v) < 0 else int(v)),
        ),
        "io.output_device": ParamHandlers(
            get=lambda: -1 if engine.output_device is None else int(engine.output_device),
            set=lambda v: engine.set_io_param("io.output_device", None if int(v) < 0 else int(v)),
        ),
        "io.playback_device": ParamHandlers(
            get=lambda: -1 if engine.playback_device is None else int(engine.playback_device),
            set=lambda v: engine.set_playback_device(None if int(v) < 0 else int(v)),
        ),
    }
    reg.update(build_effect_registry(engine.effect))
    return reg
