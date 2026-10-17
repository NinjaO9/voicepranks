"""Built-in voice presets: named bundles of sound-character params.

A preset is a curated `{param_name: value}` dict drawn from
`params.PARAM_SPECS`. It intentionally covers only *sound-character* params
(the gibberish/shuffle knobs) and leaves out `io.*`, `enabled`, `monitor`,
and `intensity` -- so applying a preset changes the voice without disturbing
your devices, on/off state, live monitor, or your intensity setting.

`intensity` is deliberately excluded so it stays a live master control: it
scales the applied preset's character (wobble depth + scramble strength), and
applying a preset never resets it.

Applying is done by `VoiceEngine.apply_preset`, which just feeds each entry
through `set_param`. The two presets below were dialed in by ear during live
tuning; edit the values here (or add entries) to change/extend them.
"""
from __future__ import annotations

from typing import Dict, List

from .params import PARAM_SPECS_BY_NAME
from . import resources

_ASSETS_DIR = resources.resource_path("assets")

_PRESET_LAUGHS = {
    "goofy": _ASSETS_DIR / "goofy_laugh.wav",
    "scooby": _ASSETS_DIR / "scooby_laugh.wav",
    "spooky": _ASSETS_DIR / "spooky_laugh.wav"
}

_SAMPLE_LAUGH = _PRESET_LAUGHS["goofy"]
_LAUGH_CONFIG_PATH = _ASSETS_DIR / "laugh_config.json"

# Params a preset is never allowed to set: environment/session state, not
# voice character.
_EXCLUDED_PREFIXES = ("io.",)
# `intensity` is a live master control the user owns -- presets must not
# reset it (they scale under it instead).
_EXCLUDED_NAMES = {"enabled", "monitor", "intensity"}

# Neutral/off values for every plain-path (non-gibberish) control. Gibberish
# presets spread this in so they leave NO stale plain-voice effects behind:
# without it, applying e.g. "scary" (which enables distortion/reverb) and then
# a gibberish preset would leave those on, and toggling gibberish off would
# surface the old scary voice instead of a clean one. It also turns the random
# laugh off, so only presets that explicitly opt in (goofy) laugh.
_PLAIN_OFF = {
    "effect.max_semitones": 0.0,
    "effect.eq_enabled": False,
    "effect.nasality": 0.0,
    "growl.enabled": False,
    "distortion.enabled": False,
    "deep_fry.enabled": False,
    "reverb.enabled": False,
    "laugh.enabled": False,
}

# Full neutral reset (the "None" preset): every effect off and plain voice.
NEUTRAL = {**_PLAIN_OFF, "gibberish": False}


PRESETS: Dict[str, Dict] = {
    # Chopped / blippy / scrambled -- discrete staccato syllable blips, the
    # Animal-Crossing-villager sound.
    "animalese": {
        **_PLAIN_OFF,
        # Plain-path voice (when gibberish is toggled off): a clean pitch-up
        # mirroring the gibberish pitch, so the toggle switches between
        # "babble" and "high voice" rather than doing nothing.
        "effect.max_semitones": 5.5,
        "gibberish": True,
        "minionese.use_shuffle": True,
        "shuffle.semitones": 5.5,
        "shuffle.wobble_ms": 1.0,
        "shuffle.chunk_ms": 80.0,
        "shuffle.shuffle_k": 6,
        "shuffle.fade_ms": 18.0,
        "shuffle.reverse_prob": 0.5,
        "shuffle.nasality": 0.0,
        "shuffle.max_slew": 1.0,
    },
    # Connected / pitched / lilting -- no scramble, so speech stays flowing;
    # a moderate pitch-up plus sing-song wobble reads as "Minion".
    "minion": {
        **_PLAIN_OFF,
        # Plain-path voice (gibberish off): a clean pitch-up "minion" voice.
        "effect.max_semitones": 5.6,
        "gibberish": True,
        "minionese.use_shuffle": True,
        "shuffle.semitones": 5.6,  # lowered 30% from 8.0
        "shuffle.wobble_ms": 4.0,
        "shuffle.chunk_ms": 150.0,
        "shuffle.shuffle_k": 1,
        "shuffle.fade_ms": 25.0,
        "shuffle.reverse_prob": 0.0,
        "shuffle.nasality": 0.7,
        "shuffle.max_slew": 1.0,
    },
    # Deep / harsh / gravelly monster -- NOT gibberish: it transforms your
    # real speech (via the plain pitch path). A downward pitch shift drops it
    # (formants drop too -> bigger/darker), an upper-mid EQ bump adds bite, a
    # ring-mod "growl" adds gnarly roughness, a hard-clipping distortion rips
    # it up, and a dense but restrained dark reverb sets it in a space without
    # drowning the grit. Pitch depth still scales under the live intensity.
    "scary": {
        **_PLAIN_OFF,  # start from the neutral baseline, then turn on what we need
        "gibberish": False,
        "effect.max_semitones": -7.0,
        "effect.eq_enabled": True,
        "effect.eq_gain_db": 6.0,
        "effect.eq_center_hz": 2500.0,
        "effect.eq_q": 1.1,
        "growl.enabled": True,
        "growl.rate_hz": 70.0,
        "growl.depth": 0.55,
        "distortion.enabled": True,
        "distortion.drive": 11.0,
        "reverb.enabled": True,
        "reverb.mix": 0.22,
        "reverb.room_size": 0.6,
        "reverb.damp": 0.6,
    },
    # Goofy -- deep, gravelly, resonant drawl that randomly punctuates your
    # speech with a silly laugh. A big *fixed* downward pitch shift makes it
    # deep (stable -- no real-time tracking, so no octave jumps or wandering
    # pitch), a low-frequency EQ boost adds resonant body, a hard-clip
    # distortion adds gravel/grit, and a slow growl adds a loose warble. Not
    # gibberish -- your real words come through; everything scales with intensity.
    "goofy": {
        **_PLAIN_OFF,  # start from the neutral baseline, then turn on what we need
        "gibberish": False,
        "effect.max_semitones": -6.0,   # deep but not too low, STABLE (no tracking)
        "effect.eq_enabled": True,
        "effect.eq_gain_db": 7.0,
        "effect.eq_center_hz": 220.0,   # deep resonant low-mid body
        "effect.eq_q": 1.0,
        "effect.nasality": 0.85,        # strong nasal honk
        "growl.enabled": True,
        "growl.rate_hz": 5.5,           # slow loose warble
        "growl.depth": 0.28,
        "distortion.enabled": True,     # gravelly grit
        "distortion.drive": 6.0,
        # reverb stays off (from _PLAIN_OFF). Goofy is the one preset that turns
        # the random laugh on (every other preset leaves it off via _PLAIN_OFF).
        "laugh.enabled": True,
        "laugh.gain": 1.0,
        "laugh.interval_s": 15.0,
    },
    
    # I JUST WANT THE AIR FRYER BRO PLEASE
    "deep_fried": {
        **_PLAIN_OFF,
        "gibberish": False,
        "effect.max_semitones": 0.0,
        "deep_fry.enabled": True,
        "deep_fry.drive": 14.0,
        "deep_fry.bit_depth": 5,
        "deep_fry.downsample": 6,
    },
}


def preset_names() -> List[str]:
    """Preset names in definition order."""
    return list(PRESETS.keys())


def get_preset(name: str) -> Dict:
    """Return a copy of the named preset's param dict.

    Raises KeyError if there is no such preset.
    """
    if name not in PRESETS:
        raise KeyError(f"unknown preset: {name!r} (have {preset_names()})")
    return dict(PRESETS[name])


def _validate() -> None:
    """Fail loudly at import if a preset references an unknown or excluded
    param -- guards against typos and against a preset reaching into
    device/session state it shouldn't touch."""
    for preset_name, values in PRESETS.items():
        for key in values:
            if key not in PARAM_SPECS_BY_NAME:
                raise ValueError(f"preset {preset_name!r} sets unknown param {key!r}")
            if key in _EXCLUDED_NAMES or key.startswith(_EXCLUDED_PREFIXES):
                raise ValueError(
                    f"preset {preset_name!r} must not set {key!r} "
                    "(io/enabled/monitor are session state, not voice character)"
                )


_validate()
