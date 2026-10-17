"""User-owned assets for the optional desktop jumpscare.

Nothing in this module refers to Foxy/FNAF artwork or sound files: callers
choose files they are licensed to use, which are copied into the user's
VoicePranks directory so their original location can later change.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional, Tuple

from . import settings
from .audio import wavio
from .resources import resource_path

_PNG = b"\x89PNG\r\n\x1a\n"
_GIF = (b"GIF87a", b"GIF89a")


def validate_visual(path) -> str:
    """Validate a PNG/GIF by signature and return its lowercase extension."""
    source = Path(path)
    try:
        header = source.read_bytes()[:8]
    except OSError as exc:
        raise ValueError(f"Could not read visual: {exc}") from exc
    if header.startswith(_PNG):
        return ".png"
    if any(header.startswith(marker) for marker in _GIF):
        return ".gif"
    raise ValueError("Visual must be a PNG or GIF file")


def validate_sound(path) -> Tuple[int, object]:
    """Return a 16-bit PCM WAV's rate and mono float32 data, or raise."""
    try:
        return wavio.read_wav(str(path))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Sound must be a readable 16-bit PCM WAV: {exc}") from exc


def copy_visual(path) -> str:
    """Validate then install a visual; returns the stored filename only."""
    suffix = validate_visual(path)
    target = settings.SETTINGS_DIR / f"jumpscare_visual{suffix}"
    settings.SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, target)
    # Avoid a stale alternate extension being selected by hand later.
    alternate = target.with_suffix(".gif" if suffix == ".png" else ".png")
    if alternate.exists():
        alternate.unlink()
    return target.name


def copy_sound(path) -> str:
    """Validate then install a sound; returns the stored filename only."""
    validate_sound(path)
    target = settings.SETTINGS_DIR / "jumpscare_sound.wav"
    settings.SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, target)
    return target.name


def stored_path(filename: object) -> Optional[Path]:
    """Safely resolve a persisted filename under the VoicePranks data dir."""
    if not isinstance(filename, str) or Path(filename).name != filename:
        return None
    return settings.SETTINGS_DIR / filename


def visual_path(filename: object) -> Path:
    """Use a saved replacement when present, otherwise the bundled default."""
    saved = stored_path(filename)
    if saved is not None and saved.exists():
        return saved
    return resource_path("assets", "fnaf-foxy-jumpscare.gif")


def sound_path(filename: object) -> Path:
    """Use a saved replacement when present, otherwise the bundled default."""
    saved = stored_path(filename)
    if saved is not None and saved.exists():
        return saved
    return resource_path("assets", "Fnaf_Jumpscare_Sound_Effect.wav")
