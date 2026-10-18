"""The jumpscare's callback-only random/maximum scheduler."""
from __future__ import annotations

import numpy as np

from voicepranks.audio.engine import VoiceEngine


def _advance(engine: VoiceEngine, frames: int) -> None:
    engine._input_callback(np.zeros((frames, 1), dtype=np.float32), frames, None, None)


def test_successful_one_second_roll_fires_and_resets_maximum_timer(monkeypatch):
    engine = VoiceEngine(sample_rate=10)
    engine.running = True
    engine.set_jumpscare_interval_s(5)
    engine.set_jumpscare_enabled(True)
    monkeypatch.setattr(engine._jumpscare_random, "randrange", lambda _n: 0)

    _advance(engine, 10)
    assert engine.get_status()["jumpscare_fires"] == 1
    _advance(engine, 49)
    assert engine.get_status()["jumpscare_fires"] == 1
    _advance(engine, 1)
    assert engine.get_status()["jumpscare_fires"] == 2


def test_failed_rolls_force_a_fire_at_the_configured_maximum(monkeypatch):
    engine = VoiceEngine(sample_rate=10)
    engine.running = True
    engine.set_jumpscare_interval_s(3)
    engine.set_jumpscare_enabled(True)
    monkeypatch.setattr(engine._jumpscare_random, "randrange", lambda _n: 1)

    _advance(engine, 29)
    assert engine.get_status()["jumpscare_fires"] == 0
    _advance(engine, 1)
    assert engine.get_status()["jumpscare_fires"] == 1
