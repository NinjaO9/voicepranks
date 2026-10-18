"""Bundled-asset resolution + packaging guards.

The stock laugh clips (and the webui page) are loaded from package data files.
A `__file__`-relative path works from source but breaks in a PyInstaller build
-- notably the macOS .app, which splits code and data -- so they must resolve
via `resource_path`. And every clip the picker offers must be listed in the
PyInstaller spec, or its button can't switch to it in a packaged build (the
exact bug where "scooby never plays" in a release while working from source).
"""
from __future__ import annotations

from pathlib import Path

from voicepranks.dsp.laugh import _PRESET_LAUGHS
from voicepranks.resources import resource_path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SPEC = _REPO_ROOT / "voicepranks.spec"


def test_resource_path_from_source_points_into_package():
    p = resource_path("assets", "goofy_laugh.wav")
    assert p == _REPO_ROOT / "voicepranks" / "assets" / "goofy_laugh.wav"


def test_resource_path_uses_meipass_when_frozen(monkeypatch, tmp_path):
    # Simulate a PyInstaller runtime: sys._MEIPASS set to the unpack dir.
    monkeypatch.setattr("voicepranks.resources.sys._MEIPASS", str(tmp_path), raising=False)
    p = resource_path("assets", "scooby_laugh.wav")
    assert p == tmp_path / "voicepranks" / "assets" / "scooby_laugh.wav"


def test_every_preset_laugh_asset_exists():
    # A missing source asset would mean the picker button falls back to synth.
    for name, path in _PRESET_LAUGHS.items():
        assert path.exists(), f"{name} laugh asset missing: {path}"


def test_every_preset_laugh_is_bundled_in_spec():
    # Guards the "scooby not in datas -> never plays in a release" regression:
    # every picker clip must be referenced in the PyInstaller spec's datas.
    spec_text = _SPEC.read_text()
    for name, path in _PRESET_LAUGHS.items():
        assert path.name in spec_text, (
            f"{name} laugh ({path.name}) is not bundled in voicepranks.spec -- "
            f"its picker button won't work in a packaged build"
        )


def test_default_jumpscare_assets_exist_and_are_bundled():
    visual = resource_path("assets", "fnaf-foxy-jumpscare.gif")
    sound = resource_path("assets", "Fnaf_Jumpscare_Sound_Effect.wav")
    spec_text = _SPEC.read_text()
    assert visual.exists()
    assert sound.exists()
    assert visual.name in spec_text
    assert "voicepranks/assets/*.wav" in spec_text


def test_reaction_fahh_asset_exists_and_is_covered_by_the_wav_bundle():
    assert resource_path("assets", "fahh.wav").exists()
    assert "voicepranks/assets/*.wav" in _SPEC.read_text()
