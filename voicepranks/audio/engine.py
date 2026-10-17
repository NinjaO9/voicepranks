"""Real-time voice engine: mic capture -> MinionEffect -> virtual output."""
from __future__ import annotations

import random
import threading
import wave
from typing import List, Optional, Tuple

import numpy as np
import sounddevice as sd

from .. import params
from .. import presets as presets_mod
from ..dsp.effect import MinionEffect
from ..dsp.ramp import IntensityRamp
from ..resources import resource_path
from . import wavio
from .devices import find_virtual_output


class _RingBuffer:
    """Thread-safe circular float32 sample buffer.

    Overrun (writer outrunning reader): drops the oldest samples to make
    room for the new ones. Underrun (reader outrunning writer): zero-fills
    the missing tail of the read.
    """

    def __init__(self, capacity: int) -> None:
        self.capacity = int(capacity)
        self.buf = np.zeros(self.capacity, dtype=np.float32)
        self.write_idx = 0
        self.read_idx = 0
        self.count = 0
        self.lock = threading.Lock()
        self.underruns = 0
        self.overruns = 0

    def write(self, data: np.ndarray) -> None:
        with self.lock:
            n = data.shape[0]
            if n > self.capacity:
                data = data[-self.capacity:]
                n = data.shape[0]

            free = self.capacity - self.count
            if n > free:
                overflow = n - free
                self.read_idx = (self.read_idx + overflow) % self.capacity
                self.count -= overflow
                self.overruns += 1

            end_space = self.capacity - self.write_idx
            if n <= end_space:
                self.buf[self.write_idx:self.write_idx + n] = data
            else:
                self.buf[self.write_idx:] = data[:end_space]
                self.buf[:n - end_space] = data[end_space:]
            self.write_idx = (self.write_idx + n) % self.capacity
            self.count = min(self.capacity, self.count + n)

    def read(self, n: int) -> np.ndarray:
        with self.lock:
            out = np.zeros(n, dtype=np.float32)
            avail = min(n, self.count)
            if avail > 0:
                end_space = self.capacity - self.read_idx
                if avail <= end_space:
                    out[:avail] = self.buf[self.read_idx:self.read_idx + avail]
                else:
                    out[:end_space] = self.buf[self.read_idx:]
                    out[end_space:avail] = self.buf[:avail - end_space]
                self.read_idx = (self.read_idx + avail) % self.capacity
                self.count -= avail
            if avail < n:
                self.underruns += 1
            return out


def _process_in_blocks(effect: MinionEffect, signal: np.ndarray, blocksize: int = 1024) -> np.ndarray:
    """Run a full buffer through `effect` in fixed-size chunks, mirroring
    how the real-time callback feeds it -- used by `render_current()` so
    a recorded take is rendered the same way it would sound live."""
    out_chunks = []
    n = signal.shape[0]
    for start in range(0, n, blocksize):
        chunk = signal[start:start + blocksize]
        out_chunks.append(effect.process(chunk))
    return np.concatenate(out_chunks) if out_chunks else np.zeros(0, dtype=np.float32)


class VoiceEngine:
    """Owns the DSP effect, the intensity ramp, the two audio streams, the
    live param registry, and the raw/rendered take recorder."""

    def __init__(
        self,
        sample_rate: int = 48000,
        blocksize: int = 256,
        ring_ms: float = 200.0,
    ) -> None:
        self.sample_rate = int(sample_rate)
        self.blocksize = int(blocksize)
        self.ring_ms = float(ring_ms)

        self.effect = MinionEffect(self.sample_rate, channels=1)
        self.ramp = IntensityRamp()

        ring_capacity = max(self.blocksize * 2, int(self.sample_rate * self.ring_ms / 1000.0))
        self.ring = _RingBuffer(ring_capacity)

        self.enabled = False
        # Output-mic level: a linear multiplier applied to the processed signal
        # on its way to the output ring (the virtual cable other apps hear)
        # ONLY -- not the live monitor or recorded takes. Lets a too-quiet
        # preset be sent hot to other apps without re-tuning its DSP or changing
        # what you monitor. 1.0 = unity, >1.0 boosts, 0.0 = silence.
        self.output_gain = 1.0
        # Playback (listening) level: a linear multiplier applied to everything
        # sent to your headphones/speakers via `sd.play` -- the Play button, the
        # Play Laugh button, and the automatic-laugh headphone echo. Independent
        # of `output_gain` (which is the outgoing-cable level), so you can set
        # how loud playback is in your own ears without changing the prank.
        self.playback_gain = 1.0
        self._manual_intensity: Optional[float] = None
        # Last intensity pushed to the effect from the callback, so a constant
        # intensity (e.g. after the ramp settles) doesn't re-derive every
        # sub-engine's coefficients every block. Param changes go through their
        # own setters, so they still apply immediately regardless of this.
        self._last_applied_intensity: Optional[float] = None

        self.input_device: Optional[int] = None
        self.output_device: Optional[int] = None
        self.input_stream: Optional[sd.InputStream] = None
        self.output_stream: Optional[sd.OutputStream] = None
        self.running = False
        self.last_error: Optional[str] = None

        # -- live monitor: a second output stream to an audible device
        # (speakers/headphones) so the user can HEAR the processed effect in
        # real time while the main output feeds the virtual cable. Fed the
        # same processed audio via its own ring so the two sinks don't fight
        # over one buffer. The listening/playback device is also what the
        # Play button routes takes to. `playback_device=None` -> system
        # default output.
        self.monitor_enabled = False
        self.playback_device: Optional[int] = None
        self.monitor_stream: Optional[sd.OutputStream] = None
        self.monitor_ring = _RingBuffer(ring_capacity)

        # -- live param registry (single source of truth; see params.py) --
        self._registry = params.build_engine_registry(self)
        self._pending: dict = {}
        self._param_lock = threading.Lock()

        # -- level meter (updated every input callback block) --------------
        self._dry_rms = 0.0
        self._dry_peak = 0.0
        self._proc_rms = 0.0
        self._proc_peak = 0.0

        # -- recorder ------------------------------------------------------
        # Two takes are captured per recording:
        #  - "live": the processed effect output, captured block-by-block as
        #    it is generated in real time (what you'd have heard live). Play
        #    replays this verbatim -- no re-render, nothing re-applied.
        #  - "raw": the dry (pre-effect) mic signal, kept so the effect can
        #    be re-rendered onto the same take with tweaked params (the A/B
        #    tuning flow). Optional; the primary flow is the live take.
        self._record_lock = threading.Lock()
        self._recording = False
        self._raw_chunks: List[np.ndarray] = []
        self._raw_take: Optional[np.ndarray] = None
        self._live_chunks: List[np.ndarray] = []
        self._live_take: Optional[np.ndarray] = None
        self._rendered_take: Optional[np.ndarray] = None

        # -- custom-laugh recorder ----------------------------------------
        # A separate, lightweight capture path (guarded by the same lock) used
        # only to record a replacement "goofy laugh" clip from the dry mic. Kept
        # apart from the take recorder above so recording a laugh never disturbs
        # an in-progress take, and vice versa.
        self._laugh_recording = False
        self._laugh_chunks: List[np.ndarray] = []

        # -- on-demand laugh overlay (Play Laugh -> outgoing cable) ---------
        # The Play Laugh button mixes the active clip onto the outgoing signal
        # so other apps hear it too (the automatic laugh already mixes itself
        # in via the DSP). Set from the UI thread, consumed on the audio thread
        # in `_input_callback`, hence its own lock. `None` = nothing playing.
        self._laugh_overlay: Optional[np.ndarray] = None
        self._laugh_overlay_pos = 0
        self._laugh_overlay_lock = threading.Lock()

        # -- automatic-laugh headphone echo --------------------------------
        # The automatic laugh reaches the cable (and the monitor, if on), but
        # not your headphones when the monitor is off. The main-thread status
        # poll (`poll_pending_laugh_echo`) watches the laugh's monotonic fire
        # counter against this last-seen value to detect a fire and echo it.
        self._last_laugh_fire_count = self.effect.laugh.fire_count
        self._last_reaction_fire_count = self.effect.reaction.fire_count

        # -- random preset bursts -----------------------------------------
        # config sutff for random preset bursts
        self.random_burst_enabled = False
        self.random_burst_interval_min_s = 10.0
        self.random_burst_interval_max_s = 30.0
        self.random_burst_duration_min_s = 2.0
        self.random_burst_duration_max_s = 5.0
        self.random_burst_presets = {name: True for name in presets_mod.preset_names()}
        self._random = random.Random()
        self._random_burst_samples_left: Optional[int] = None
        self._random_burst_restore: Optional[dict] = None
        self._random_burst_name: Optional[str] = None

    # -- param registry (shared by Tkinter UI + HTTP control server) -----

    def set_param(self, name: str, value) -> None:
        """Set one live param by its dotted name (see `params.PARAM_SPECS`).

        `io.*` params manage devices/streams (restart the main streams or
        re-open the monitor stream), which must happen on the caller thread —
        opening/tearing down a stream from inside the audio callback is unsafe.
        So every `io.*` param is applied immediately via its registry handler
        (which encodes the right action). Everything else is stashed and
        applied at the top of the next `_input_callback` while the engine is
        running, so DSP-stage rebuilds never race the audio thread using them;
        when stopped, there is no audio thread to race, so it too is applied
        immediately.
        """
        handlers = self._registry.get(name)
        if handlers is None:
            raise KeyError(f"unknown param: {name}")
        spec = params.PARAM_SPECS_BY_NAME.get(name)
        apply_now = (spec is not None and spec.group == "io") or not self.running
        if apply_now:
            handlers.set(value)
        else:
            with self._param_lock:
                self._pending[name] = value

    def snapshot(self) -> dict:
        """Return the current value of every registered param.

        Params queued by `set_param` (stashed in `_pending` while the engine is
        running, applied on the next audio block) are reflected here too, so a
        snapshot never lags a value that was just set -- e.g. saving settings or
        reading `/api/state` right after applying a preset returns the intended
        values, not the pre-change ones."""
        snap = {name: handlers.get() for name, handlers in self._registry.items()}
        with self._param_lock:
            for name, value in self._pending.items():
                if name in snap:
                    snap[name] = value
        return snap

    def apply_params(self, mapping: dict) -> dict:
        """Feed each `{param: value}` in `mapping` through `set_param`. Works
        whether the engine is running or stopped. Returns the full snapshot."""
        for name, value in mapping.items():
            self.set_param(name, value)
        return self.snapshot()

    def apply_preset(self, name: str) -> dict:
        """Apply a built-in preset (see `presets.PRESETS`): feed each of its
        sound-character params through `set_param`. Works whether the engine
        is running or stopped. Returns the resulting full snapshot. Raises
        KeyError for an unknown preset name."""
        return self.apply_params(presets_mod.get_preset(name))

    def _drain_pending(self) -> None:
        """Apply any params queued by `set_param` since the last block.
        Called at the top of `_input_callback`, i.e. on the audio thread."""
        with self._param_lock:
            if not self._pending:
                return
            pending, self._pending = self._pending, {}
        reprime = False
        for name, value in pending.items():
            handlers = self._registry.get(name)
            if handlers is None:
                continue
            try:
                handlers.set(value)
            except Exception as exc:  # keep the audio thread alive
                self.last_error = str(exc)
                continue
            if self._random_burst_restore is not None and name in self._random_burst_restore:
                # Adjustment saves to end of audio. Because person might do a goof and modify stuff while playing
                self._random_burst_restore[name] = value
            # Params that rebuild a DSP stage (`needs_reset`) or switch the
            # gibberish engine can change the active path's buffering latency
            # -- notably the shuffle window. Re-prime the output cushion so a
            # freshly-enlarged window can't starve the stream mid-run.
            spec = params.PARAM_SPECS_BY_NAME.get(name)
            if name == "minionese.use_shuffle" or (spec is not None and spec.needs_reset):
                reprime = True
        if reprime and self.running:
            self._prime_for_effect()

    def set_io_param(self, name: str, value) -> None:
        """Apply an `io.*` param, restarting the streams (stop then start)
        if the engine is currently running."""
        was_running = self.running
        if was_running:
            self.stop()

        rebuild_effect = False
        if name == "io.sample_rate":
            self.sample_rate = int(value)
            rebuild_effect = True
        elif name == "io.blocksize":
            self.blocksize = int(value)
        elif name == "io.ring_ms":
            self.ring_ms = float(value)
        elif name == "io.input_device":
            self.input_device = None if value is None or int(value) < 0 else int(value)
        elif name == "io.output_device":
            self.output_device = None if value is None or int(value) < 0 else int(value)
        else:
            raise KeyError(f"unknown io param: {name}")

        if rebuild_effect:
            # Pitch/Minionese internal windows are sized in samples off the
            # sample rate, so a rate change needs a fresh DSP chain.
            self.effect = MinionEffect(self.sample_rate, channels=1)
            # The fresh effect's laugh starts its fire counter at 0; resync our
            # last-seen value so the counter reset doesn't read as a "new laugh"
            # and fire a spurious headphone echo on the next poll.
            self._last_laugh_fire_count = self.effect.laugh.fire_count
            self._last_reaction_fire_count = self.effect.reaction.fire_count

        # Devices/blocksize/ring_ms don't change effect identity, but
        # rebuilding the registry unconditionally is cheap and keeps every
        # closure bound to the current `self.effect`.
        self._registry = params.build_engine_registry(self)

        ring_capacity = max(self.blocksize * 2, int(self.sample_rate * self.ring_ms / 1000.0))
        self.ring = _RingBuffer(ring_capacity)

        if was_running:
            self.start(input_device=self.input_device, output_device=self.output_device)

    # -- lifecycle -----------------------------------------------------

    def start(self, input_device: Optional[int] = None, output_device: Optional[int] = None) -> None:
        if input_device is None:
            input_device = self.input_device
        if output_device is None:
            output_device = self.output_device
        if output_device is None:
            output_device = find_virtual_output()

        self.input_device = input_device
        self.output_device = output_device
        # A restart may follow an effect rebuild (io.sample_rate); force the
        # first callback to re-apply intensity rather than trust the cache.
        self._last_applied_intensity = None

        out_info = sd.query_devices(output_device)
        out_channels = max(1, int(out_info.get("max_output_channels", 1)))

        self.input_stream = sd.InputStream(
            device=input_device,
            channels=1,
            samplerate=self.sample_rate,
            blocksize=self.blocksize,
            dtype="float32",
            callback=self._input_callback,
        )
        self.output_stream = sd.OutputStream(
            device=output_device,
            channels=out_channels,
            samplerate=self.sample_rate,
            blocksize=self.blocksize,
            dtype="float32",
            callback=self._output_callback,
        )
        # Pre-fill the ring with a silence cushion sized to the active
        # effect path's internal buffering latency, so the output stream
        # doesn't starve while that latency fills in after start. Critical
        # for the shuffle gibberish engine, whose latency grows with the
        # scramble amount (a full shuffle window can be ~1s).
        self._prime_for_effect()
        self.input_stream.start()
        self.output_stream.start()
        self.running = True
        if self.monitor_enabled:
            self._open_monitor_stream()

    def stop(self) -> None:
        self._close_monitor_stream()
        if self.input_stream is not None:
            self.input_stream.stop()
            self.input_stream.close()
            self.input_stream = None
        if self.output_stream is not None:
            self.output_stream.stop()
            self.output_stream.close()
            self.output_stream = None
        self.running = False
        # Drop any in-flight Play-Laugh overlay so a clip queued just before a
        # stop (e.g. a device change) can't resurface and burst onto the cable
        # at the next start().
        with self._laugh_overlay_lock:
            self._laugh_overlay = None
            self._laugh_overlay_pos = 0

    # -- live monitor ----------------------------------------------------

    def set_monitor_enabled(self, b: bool) -> None:
        """Turn the live monitor (audible processed-audio output) on/off.
        Opens/closes the monitor stream immediately if the engine is
        running; otherwise it takes effect on the next `start()`."""
        self.monitor_enabled = bool(b)
        if not self.running:
            return
        if self.monitor_enabled:
            self._open_monitor_stream()
        else:
            self._close_monitor_stream()

    def set_playback_device(self, device: Optional[int]) -> None:
        """Pick the device used for *listening* -- both the live monitor and
        the Play button route here (None = system default output). Re-opens
        the monitor stream if it's currently active."""
        self.playback_device = None if device is None else int(device)
        if self.running and self.monitor_enabled:
            self._close_monitor_stream()
            self._open_monitor_stream()

    def _open_monitor_stream(self) -> None:
        if self.monitor_stream is not None:
            return
        try:
            device = self.playback_device  # None -> system default output
            # `query_devices(kind='output')` resolves the system default when
            # no explicit device is given, without assuming the shape of
            # `sd.default.device`.
            info = sd.query_devices(device) if device is not None else sd.query_devices(kind="output")
            channels = max(1, int(info.get("max_output_channels", 1)))
            self.monitor_ring = _RingBuffer(self.ring.capacity)
            self._prime_ring_target(self.monitor_ring, self._cushion_ms())
            self.monitor_stream = sd.OutputStream(
                device=device,
                channels=channels,
                samplerate=self.sample_rate,
                blocksize=self.blocksize,
                dtype="float32",
                callback=self._monitor_output_callback,
            )
            self.monitor_stream.start()
        except Exception as exc:
            self.last_error = f"Live monitor failed: {exc}"
            self.monitor_stream = None

    def _close_monitor_stream(self) -> None:
        if self.monitor_stream is not None:
            try:
                self.monitor_stream.stop()
                self.monitor_stream.close()
            except Exception:
                pass
            self.monitor_stream = None

    def _monitor_output_callback(self, outdata, frames, time_info, status) -> None:  # noqa: D401
        try:
            mono = self.monitor_ring.read(frames)
            channels = outdata.shape[1]
            if channels == 1:
                outdata[:, 0] = mono
            else:
                outdata[:, :] = mono[:, None]  # broadcast mono across channels (no temp)
        except Exception as exc:
            self.last_error = str(exc)
            outdata.fill(0.0)

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        # Both branches change the effect's intensity out-of-band (the ramp
        # restart, or the direct set_intensity(0) below), so invalidate the
        # callback's cache -- otherwise a re-enable with no ramp could match
        # the stale cached value and skip re-applying, leaving the effect at 0.
        self._last_applied_intensity = None
        if self.enabled:
            self._manual_intensity = None
            self.ramp.start()
        else:
            self.ramp.stop()
            self.effect.set_intensity(0.0)

    def set_random_burst_enabled(self, enabled: bool) -> None:
        self.random_burst_enabled = bool(enabled)
        if not self.random_burst_enabled:
            self._stop_random_burst()
        elif self._random_burst_samples_left is None:
            self._schedule_random_burst_wait()

    def set_random_burst_interval_min_s(self, seconds: float) -> None:
        self.random_burst_interval_min_s = max(0.0, float(seconds))

    def set_random_burst_interval_max_s(self, seconds: float) -> None:
        self.random_burst_interval_max_s = max(0.0, float(seconds))

    def set_random_burst_duration_min_s(self, seconds: float) -> None:
        self.random_burst_duration_min_s = max(0.1, float(seconds))

    def set_random_burst_duration_max_s(self, seconds: float) -> None:
        self.random_burst_duration_max_s = max(0.1, float(seconds))

    def set_random_burst_preset(self, name: str, selected: bool) -> None:
        if name not in self.random_burst_presets:
            raise KeyError(f"unknown random-burst preset: {name}")
        self.random_burst_presets[name] = bool(selected)

    def _random_seconds_to_samples(self, low: float, high: float) -> int:
        seconds = self._random.uniform(min(low, high), max(low, high))
        return max(1, int(round(seconds * self.sample_rate)))

    def _schedule_random_burst_wait(self) -> None:
        self._random_burst_samples_left = self._random_seconds_to_samples(
            self.random_burst_interval_min_s, self.random_burst_interval_max_s
        )

    def _start_random_burst(self) -> None:
        choices = [name for name, selected in self.random_burst_presets.items() if selected]
        if not choices:
            self._schedule_random_burst_wait()
            return
        name = self._random.choice(choices)
        values = presets_mod.get_preset(name)
        self._random_burst_restore = {key: self._registry[key].get() for key in values}
        for key, value in values.items():
            self._registry[key].set(value)
        self._random_burst_name = name
        self._random_burst_samples_left = self._random_seconds_to_samples(
            self.random_burst_duration_min_s, self.random_burst_duration_max_s
        )
        self._prime_for_effect()

    def _stop_random_burst(self) -> None:
        if self._random_burst_restore is not None:
            for key, value in self._random_burst_restore.items():
                self._registry[key].set(value)
            self._prime_for_effect()
        self._random_burst_restore = None
        self._random_burst_name = None
        self._random_burst_samples_left = None

    def _advance_random_burst(self, frames: int) -> bool:
        """Advance the callback-owned scheduler; return whether a burst is on."""
        if not self.random_burst_enabled:
            return False
        if self._random_burst_samples_left is None:
            self._schedule_random_burst_wait()
        self._random_burst_samples_left -= frames
        if self._random_burst_samples_left <= 0:
            if self._random_burst_restore is None:
                self._start_random_burst()
            else:
                self._stop_random_burst()
                self._schedule_random_burst_wait()
        return self._random_burst_restore is not None

    def set_manual_intensity(self, t: float) -> None:
        """Override the auto-ramp with a fixed intensity (e.g. slider drag)."""
        self._manual_intensity = min(max(float(t), 0.0), 1.0)

    def set_output_gain(self, gain: float) -> None:
        """Set the post-effect output level (linear multiplier). Clamped at 0
        (no negatives); no upper bound so a quiet preset can be pushed hot."""
        self.output_gain = max(0.0, float(gain))

    def set_playback_gain(self, gain: float) -> None:
        """Set the listening-side level (linear multiplier) applied to Play,
        Play Laugh, and the automatic-laugh headphone echo. Clamped at 0 (no
        negatives); no upper bound, matching `set_output_gain`."""
        self.playback_gain = max(0.0, float(gain))

    def restart_ramp(self, duration_s: Optional[float] = None) -> None:
        """Restart the intensity ramp from 0 with the current (or given)
        duration, handing intensity control back to the ramp (clears any
        manual override). `duration_s` is set directly so the new value takes
        effect immediately, without waiting for the pending-param drain."""
        if duration_s is not None:
            self.ramp.set_duration(float(duration_s))
        self._manual_intensity = None
        self.ramp.start()

    def set_gibberish(self, b: bool) -> None:
        self.effect.set_gibberish(b)
        # Switching into a gibberish engine mid-run resets its internal
        # pipeline, so re-prime the ring to that engine's latency to avoid a
        # toggle-on dropout.
        if b and self.running:
            self._prime_for_effect()

    def _prime_ring_target(self, ring: "_RingBuffer", ms: float) -> None:
        """Top a ring up to `ms` of buffered silence — a startup / mode-switch
        cushion against the effect pipeline's latency."""
        target = int(self.sample_rate * ms / 1000.0)
        need = target - ring.count
        if need > 0:
            ring.write(np.zeros(need, dtype=np.float32))

    def _cushion_ms(self) -> float:
        """Output pre-fill needed to cover the active effect path's internal
        buffering latency, plus headroom. The shuffle gibberish engine must
        accumulate a full shuffle window before emitting, so its latency (and
        thus the cushion) grows with the scramble amount; if we primed a
        fixed cushion smaller than that window the output would starve every
        window. Headroom (1.5x + 60ms) absorbs block jitter."""
        try:
            eff_latency = self.effect.latency_ms()
        except Exception:
            eff_latency = 0.0
        return eff_latency * 1.5 + 60.0

    def _grown_ring(self, ring: "_RingBuffer", ms: float) -> "_RingBuffer":
        """Return a ring with capacity for at least `ms`, carrying over any
        buffered audio and the cumulative counters. Returns `ring` unchanged
        if it is already big enough. Growing on the audio thread is safe here
        because `_RingBuffer` is only read/written under its own lock and we
        swap in a pre-filled replacement."""
        target = int(self.sample_rate * ms / 1000.0)
        want_capacity = max(self.blocksize * 2, target + self.blocksize * 2)
        if want_capacity <= ring.capacity:
            return ring
        new = _RingBuffer(want_capacity)
        # `read`/`write` each lock internally; don't hold `ring.lock` here
        # (it's non-reentrant -> would deadlock). Carry over buffered audio
        # and the cumulative under/overrun counters.
        carried = ring.read(ring.count) if ring.count > 0 else np.zeros(0, dtype=np.float32)
        new.underruns = ring.underruns
        new.overruns = ring.overruns
        if carried.size > 0:
            new.write(carried)
        return new

    def _prime_for_effect(self) -> None:
        """Size the ring(s) and pre-fill to cover the active effect path's
        latency. Called on start, on gibberish toggle, and after a scramble
        param reset — anywhere the effect's internal latency may have grown.
        The monitor ring gets the same treatment so the live monitor doesn't
        starve on a big shuffle window either."""
        cushion = self._cushion_ms()
        capacity_ms = cushion + self.blocksize / self.sample_rate * 1000.0
        self.ring = self._grown_ring(self.ring, capacity_ms)
        self._prime_ring_target(self.ring, cushion)
        if self.monitor_enabled and self.monitor_stream is not None:
            self.monitor_ring = self._grown_ring(self.monitor_ring, capacity_ms)
            self._prime_ring_target(self.monitor_ring, cushion)

    def get_status(self) -> dict:
        return {
            "intensity": self.effect.intensity,
            "underruns": self.ring.underruns,
            "overruns": self.ring.overruns,
            "running": self.running,
            "enabled": self.enabled,
            "random_burst_active": self._random_burst_restore is not None,
            "random_burst_preset": self._random_burst_name,
            "gibberish": self.effect.gibberish,
            "monitor": self.monitor_enabled,
            "latency_ms": round(self.effect.latency_ms(), 1),
            "last_error": self.last_error,
            "recording": self._recording,
            "laugh_recording": self._laugh_recording,
            "custom_laugh": self.effect.custom_laugh,
            "active_laugh": self.effect.active_laugh,
            "reaction_fires": self.effect.reaction.fire_count,
            "take_seconds": self._current_take_seconds(),
            "has_live_take": self._live_take is not None and self._live_take.size > 0,
            "has_raw_take": self._raw_take is not None and self._raw_take.size > 0,
            "has_rendered_take": self._rendered_take is not None and self._rendered_take.size > 0,
            "level": {
                "dry_rms": self._dry_rms,
                "dry_peak": self._dry_peak,
                "processed_rms": self._proc_rms,
                "processed_peak": self._proc_peak,
            },
        }

    # -- recorder --------------------------------------------------------

    def record_start(self) -> None:
        """Begin a new take: capture the live processed output (and the dry
        mic for optional re-render)."""
        with self._record_lock:
            self._raw_chunks = []
            self._raw_take = None
            self._live_chunks = []
            self._live_take = None
            self._rendered_take = None
            self._recording = True

    def record_stop(self) -> np.ndarray:
        """Stop recording; finalize both takes and return the live processed
        take (what Play replays)."""
        with self._record_lock:
            self._recording = False
            self._raw_take = (
                np.concatenate(self._raw_chunks) if self._raw_chunks else np.zeros(0, dtype=np.float32)
            )
            self._live_take = (
                np.concatenate(self._live_chunks) if self._live_chunks else np.zeros(0, dtype=np.float32)
            )
            self._raw_chunks = []
            self._live_chunks = []
        return self._live_take

    # -- custom-laugh recorder -------------------------------------------

    def record_laugh_start(self) -> None:
        """Begin capturing the dry mic into a replacement goofy-laugh clip.
        Independent of the take recorder -- laugh Record/Stop and take
        Record/Stop can even overlap without clobbering each other."""
        with self._record_lock:
            self._laugh_chunks = []
            self._laugh_recording = True

    def record_laugh_stop(self) -> np.ndarray:
        """Stop laugh capture and install the recording as the active goofy
        laugh (persisted to `~/.voicepranks/custom_laugh.wav`). Returns the
        captured mono take. Raises if the recording was empty/silent, leaving
        the current clip untouched."""
        with self._record_lock:
            self._laugh_recording = False
            take = (
                np.concatenate(self._laugh_chunks)
                if self._laugh_chunks
                else np.zeros(0, dtype=np.float32)
            )
            self._laugh_chunks = []
        self.effect.save_custom_laugh(take)
        return take

    def reset_laugh_to_stock(self) -> None:
        """Discard any custom laugh recording and revert to the bundled clip."""
        self.effect.reset_custom_laugh()

    def select_laugh(self, name: str) -> None:
        """Set the active laugh to a bundled pre-recorded preset ('goofy',
        'scooby', ...). Persists across restarts (see GoofyLaugh.select_laugh)."""
        self.effect.select_laugh(name)

    def _current_take_seconds(self) -> float:
        with self._record_lock:
            if self._recording:
                total = sum(c.shape[0] for c in self._raw_chunks)
            elif self._raw_take is not None:
                total = self._raw_take.shape[0]
            else:
                total = 0
        return total / self.sample_rate if self.sample_rate else 0.0

    def render_current(self) -> np.ndarray:
        """Build a fresh `MinionEffect` from the current param snapshot and
        run the raw take through it, block by block -- an A/B "re-render"
        of the last take with whatever settings are dialed in *now*,
        without disturbing the live engine's own effect/state."""
        if self._raw_take is None or self._raw_take.size == 0:
            raise RuntimeError("No raw take recorded yet -- call record_start()/record_stop() first.")

        values = self.snapshot()
        fresh = MinionEffect(self.sample_rate, channels=1)
        fresh.set_gibberish(bool(values.get("gibberish", False)))

        effect_registry = params.build_effect_registry(fresh)
        for name, handlers in effect_registry.items():
            if name in values:
                handlers.set(values[name])

        fresh.set_intensity(float(values.get("intensity", 1.0)))

        out = _process_in_blocks(fresh, self._raw_take, blocksize=self.blocksize)
        # No output-volume trim here: that trims only the output mic, and the
        # re-rendered take is played to your listening device (Play), not the
        # virtual cable.
        self._rendered_take = out
        return out

    def _get_take(self, which: str) -> np.ndarray:
        if which == "live":
            buf = self._live_take
        elif which == "raw":
            buf = self._raw_take
        elif which == "rendered":
            buf = self._rendered_take
        else:
            raise ValueError(f"unknown take '{which}' (expected 'live', 'raw' or 'rendered')")
        if buf is None or buf.size == 0:
            raise RuntimeError(f"no {which} take available")
        return buf

    def _play_to_headphones(self, clip: np.ndarray) -> None:
        """Play `clip` to the listening device (speakers/headphones) at the
        current playback level. Shared by Play, Play Laugh, and the automatic-
        laugh echo so the Playback-volume knob governs all three."""
        buf = clip
        if self.playback_gain != 1.0:
            buf = np.asarray(clip, dtype=np.float32) * np.float32(self.playback_gain)
        sd.play(buf, self.sample_rate, device=self.playback_device)

    def _queue_output_overlay(self, clip: np.ndarray) -> None:
        """Queue `clip` to be mixed onto the outgoing (virtual-cable) signal by
        the audio thread. A fresh call restarts any overlay still playing."""
        overlay = np.asarray(clip, dtype=np.float32).copy()
        with self._laugh_overlay_lock:
            self._laugh_overlay = overlay
            self._laugh_overlay_pos = 0

    def _take_overlay_chunk(self, n: int) -> Optional[np.ndarray]:
        """Pop the next `n` samples of the queued laugh overlay (zero-padded if
        it ends mid-block), or None when nothing is queued or `n <= 0`. Clears
        the overlay once exhausted. Called on the audio thread."""
        # Lock-free resting fast-path: reading the object reference is atomic
        # under the GIL, and the overlay is queued/cleared only rarely (a button
        # press), so keep the mutex off the hot path when nothing is playing. A
        # freshly queued overlay simply starts one block later -- harmless.
        if n <= 0 or self._laugh_overlay is None:
            return None
        with self._laugh_overlay_lock:
            overlay = self._laugh_overlay
            if overlay is None:
                return None
            pos = self._laugh_overlay_pos
            take = min(n, overlay.shape[0] - pos)
            # `np.empty` + tail-zero avoids memset-ing the whole block on the
            # common full-cover blocks (only the final partial block needs it).
            chunk = np.empty(n, dtype=np.float32)
            chunk[:take] = overlay[pos:pos + take]
            if take < n:
                chunk[take:] = 0.0
            pos += take
            if pos >= overlay.shape[0]:
                self._laugh_overlay = None
                self._laugh_overlay_pos = 0
            else:
                self._laugh_overlay_pos = pos
            return chunk

    def play(self, which: str = "live") -> None:
        # Route playback to the listening device (speakers/headphones), NOT
        # the virtual cable -- Play is for hearing the take yourself.
        buf = self._get_take(which)
        self._play_to_headphones(buf)

    def play_laugh(self) -> None:
        # Play the active laugh clip on demand to BOTH outputs: mixed onto the
        # outgoing cable (so other apps/the prank target hear it) when running,
        # and always to your listening device. Stopped -> headphones-only
        # audition, since there's no live output stream to mix into.
        clip = self.effect.laugh_clip()
        if self.running:
            self._queue_output_overlay(clip)
        self._play_to_headphones(clip)

    def play_random_sound_effect(self) -> str:
        """Play a random sound WAV file through the same outputs as Play Laugh.

        Any WAV placed in ``voicepranks/assets`` can play. Can add any number of sounds.
        """
        paths = sorted(resource_path("assets").glob("*.wav"))
        if not paths:
            raise RuntimeError("no WAV sound effects found in voicepranks/assets")

        path = random.choice(paths)
        try:
            source_rate, clip = wavio.read_wav(str(path))
        except (OSError, ValueError, wave.Error) as exc:
            raise RuntimeError(f"could not load sound effect '{path.name}': {exc}") from exc

        clip = np.asarray(clip, dtype=np.float32)
        if source_rate != self.sample_rate and clip.size > 1:
            count = int(round(clip.size / source_rate * self.sample_rate))
            if count > 1:
                positions = np.linspace(0.0, clip.size - 1, count)
                left = positions.astype(np.int64)
                right = np.minimum(left + 1, clip.size - 1)
                fraction = positions - left
                clip = (
                    clip[left] * (1.0 - fraction) + clip[right] * fraction
                ).astype(np.float32)

        if clip.size == 0:
            raise RuntimeError(f"sound effect '{path.name}' is empty")
        if self.running:
            self._queue_output_overlay(clip) # Add to virtual cable output
        self._play_to_headphones(clip)
        return path.stem

    def poll_pending_laugh_echo(self) -> None:
        """Echo a just-fired automatic laugh to your headphones. Called from the
        main-thread status poll (never the audio thread -- `sd.play` mustn't run
        there). Detects a fire by watching the laugh's monotonic `fire_count`;
        multiple laughs fired between polls collapse into a single echo. Skips
        the echo when the live monitor is on, since the monitor already carries
        the laugh to your listening device -- echoing too would double it."""
        fc = self.effect.laugh.fire_count
        if fc == self._last_laugh_fire_count:
            return
        self._last_laugh_fire_count = fc
        if not self.monitor_enabled:
            self._play_to_headphones(self.effect.laugh_clip())

    def poll_pending_reaction_echo(self) -> None:
        """Echo a reaction sound locally when the live monitor is off.

        As with automatic laughs, this is invoked by the UI thread so the
        callback never opens a playback stream or calls ``sd.play``.
        """
        fc = self.effect.reaction.fire_count
        if fc == self._last_reaction_fire_count:
            return
        self._last_reaction_fire_count = fc
        if not self.monitor_enabled:
            self._play_to_headphones(self.effect.reaction_clip())

    def save(self, path: str, which: str = "live") -> None:
        buf = self._get_take(which)
        wavio.write_wav(path, self.sample_rate, buf)

    def take_bytes(self, which: str = "live") -> bytes:
        buf = self._get_take(which)
        return wavio.wav_bytes(self.sample_rate, buf)

    # -- stream callbacks ------------------------------------------------

    @staticmethod
    def _rms_peak(sig: np.ndarray) -> Tuple[float, float]:
        """(rms, peak) of a block as plain floats, or (0.0, 0.0) when empty.
        Shared by the dry-input and output-mic level meters."""
        if sig.size:
            s64 = sig.astype(np.float64)
            return float(np.sqrt(np.mean(s64 ** 2))), float(np.max(np.abs(s64)))
        return 0.0, 0.0

    def _input_callback(self, indata, frames, time_info, status) -> None:  # noqa: D401
        try:
            self._drain_pending()

            mono = indata[:, 0] if indata.ndim > 1 else indata
            mono = np.asarray(mono, dtype=np.float32)

            self._dry_rms, self._dry_peak = self._rms_peak(mono)

            random_burst_active = self._advance_random_burst(frames)
            if self.enabled or random_burst_active:
                t = self._manual_intensity if self._manual_intensity is not None else self.ramp.current()
                if random_burst_active and not self.enabled:
                    t = 1.0
                if t != self._last_applied_intensity:
                    self.effect.set_intensity(t)
                    self._last_applied_intensity = t
                processed = self.effect.process(mono)
            else:
                processed = mono

            # Capture both takes under one lock: the live processed output
            # (the real-time effect result, replayed verbatim by Play) and
            # the dry mic (for optional re-render). The effect emits a
            # variable number of samples per block, so the live take is the
            # concatenation of exactly what went out. Captured pre-gain: Output
            # volume trims only the output mic (below), and Play routes takes to
            # your listening device, which that trim must not touch.
            if self._recording or self._laugh_recording:
                with self._record_lock:
                    # One dry-mic copy shared by both recorders (each only reads
                    # it), so an overlapping take + laugh capture doesn't copy
                    # the same block twice on the audio hot path.
                    dry = mono.copy()
                    if self._recording:
                        self._raw_chunks.append(dry)
                        if processed.size:
                            self._live_chunks.append(np.asarray(processed, dtype=np.float32).copy())
                    # Custom-laugh capture uses the DRY mic (the laugh clip is a
                    # plain recording of you, not the voice effect).
                    if self._laugh_recording:
                        self._laugh_chunks.append(dry)

            # Output volume trims ONLY the output mic (the virtual cable other
            # apps hear), leaving the live monitor and recorded takes at their
            # natural level -- so a quiet preset can be sent hot to other apps
            # without changing what you monitor. Applied just before the ring.
            out_mic = processed
            if self._laugh_recording and not self.enabled:
                # The engine was started only to capture a custom laugh clip
                # (effect off), so don't leak the raw mic to the virtual cable
                # while recording -- other apps would otherwise hear you laughing
                # into the sample. The clip is still captured from the dry mic
                # above; when the effect is ON we leave the cable alone (you're
                # performing, and the laugh capture is incidental).
                out_mic = np.zeros_like(processed)
            elif self.output_gain != 1.0 and processed.size:
                out_mic = processed * np.float32(self.output_gain)

            # Mix any on-demand laugh overlay (Play Laugh) onto the OUTGOING
            # signal only -- deliberately not `processed`/the monitor feed, so
            # your headphone side stays a single source (the sd.play echo).
            # `out_mic = out_mic + ...` rebinds to a fresh array, so `processed`
            # (fed to the monitor below) is never mutated. Scaled by output_gain
            # so the one outgoing-level knob governs the injected laugh too.
            overlay_chunk = self._take_overlay_chunk(out_mic.shape[0])
            if overlay_chunk is not None:
                out_mic = out_mic + overlay_chunk * np.float32(self.output_gain)
                np.clip(out_mic, -1.0, 1.0, out=out_mic)

            # Meter the output-mic signal (post-gain) so the "out" level shows
            # what other apps actually receive, revealing clipping when the
            # volume is pushed hot.
            self._proc_rms, self._proc_peak = self._rms_peak(out_mic)

            self.ring.write(out_mic)
            # Feed the live monitor the ungained processed audio (its own ring
            # so the two output streams don't consume each other's samples).
            if self.monitor_enabled and self.monitor_stream is not None:
                self.monitor_ring.write(processed)
        except Exception as exc:  # keep the audio thread alive
            self.last_error = str(exc)

    def _output_callback(self, outdata, frames, time_info, status) -> None:  # noqa: D401
        try:
            mono = self.ring.read(frames)
            channels = outdata.shape[1]
            if channels == 1:
                outdata[:, 0] = mono
            else:
                outdata[:, :] = mono[:, None]  # broadcast mono across channels (no temp)
        except Exception as exc:
            self.last_error = str(exc)
            outdata.fill(0.0)
