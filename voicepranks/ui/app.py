"""Minimal tkinter GUI for the VoicePranks engine.

Every tunable in `params.PARAM_SPECS` gets a generated slider/checkbox in
a scrollable grid here, so the desktop UI never hardcodes the param list
-- add a knob to the registry and it shows up automatically. The same
registry backs the HTTP control API (`control_server.py`), which this app
also starts by default (set `VOICEPRANKS_NO_SERVER=1` to disable; the legacy
`MINION_NO_SERVER` name is still honored), so Claude
or a `curl` can tweak params live while this window is open; sliders pick
up out-of-band API changes on the next status poll.
"""
from __future__ import annotations

import os
import sys
import traceback
import tkinter as tk
from tkinter import filedialog, ttk
from typing import Dict, List, Optional, Tuple

from .. import presets as presets_mod
from .. import settings as settings_mod
from ..audio.devices import list_input_devices, list_output_devices
from ..audio.engine import VoiceEngine
from ..control_server import ControlServer
from ..jumpscare_assets import copy_sound, copy_visual, stored_path, validate_sound, validate_visual, visual_path
from ..params import PARAM_SPECS, PARAM_SPECS_BY_NAME, ParamSpec
from .jumpscare import FullscreenJumpscare

STATUS_REFRESH_MS = 250
SAVE_DEBOUNCE_MS = 400

# Params NOT persisted across sessions: on/off + live monitor are session
# state, and io.* devices are stored separately (see settings.py). Note this
# intentionally does NOT skip `intensity` -- unlike the preset exclusions in
# presets.py, we *do* want to remember the user's intensity setting.
_PERSIST_SKIP_NAMES = {"enabled", "monitor"}


def _log(message: str) -> None:
    """Write a diagnostic line to stderr, tolerating a missing stream.

    In a PyInstaller windowed build on Windows there is no attached console,
    so `sys.stderr` is None and a bare `.write()` raises AttributeError. Guard
    it so logging is best-effort and never crashes the GUI on launch.
    """
    stream = sys.stderr
    if stream is None:
        return
    try:
        stream.write(message)
    except (OSError, ValueError):
        pass


GROUP_LABELS = {
    "global": "Global",
    "random_burst": "Random voice bursts",
    "effect": "Plain mode (pitch + EQ)",
    "growl": "Growl (ring mod)",
    "distortion": "Distortion (gravel)",
    "deep_fry": "Deep Fry (crunch + bit crush)",
    "reverb": "Reverb",
    "laugh": "Random laugh",
    "reaction": "Reaction sounds",
    "jumpscare": "Fullscreen jumpscare",
    "minionese": "Minionese — formant engine",
    "shuffle": "Minionese — shuffle engine",
    "pitch": "Pitch engine (advanced)",
    "io": "I/O (restarts audio)",
}
GROUP_ORDER = [
    "global", "random_burst", "effect", "growl", "distortion", "deep_fry", "reverb", "laugh", "reaction", "jumpscare",
    "minionese", "shuffle", "pitch", "io",
]

# Params handled by dedicated controls (the Settings dialog device pickers,
# the Turn On button, the Live monitor checkbox), so they are left out of the
# generated grid to avoid two controls for one param.
_SKIP_PARAM_NAMES = {
    "enabled",
    "monitor",
    "output_gain",  # dedicated always-visible Output volume row instead of a slider
    "playback_gain",  # dedicated always-visible Playback volume row instead of a slider
    "ramp.duration_s",  # dedicated H/M/S row instead of a slider
    "io.input_device",
    "io.output_device",
    "io.playback_device",
}

_PRESET_NONE = "None"

# Display strings for the active laugh, keyed by the engine's `active_laugh`
# token. Built once here rather than per status-poll tick.
_LAUGH_LABELS = {
    "goofy": "goofy laugh active",
    "scooby": "scooby laugh active",
    "custom": "your recorded laugh active",
}


class VoicePranksApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("VoicePranks")

        self.engine = VoiceEngine()
        self.error_message: Optional[str] = None

        self._dragging: Dict[str, bool] = {}
        self._param_widgets: Dict[str, dict] = {}
        self._suppress_commands = False
        self._save_after_id: Optional[str] = None
        self._last_jumpscare_fires = 0
        self._active_jumpscare: Optional[FullscreenJumpscare] = None

        self.control_server: Optional[ControlServer] = None
        # `VOICEPRANKS_NO_SERVER` is the current name; `MINION_NO_SERVER` is the
        # pre-rebrand alias, still honored so existing launch scripts keep working.
        no_server = os.environ.get(
            "VOICEPRANKS_NO_SERVER", os.environ.get("MINION_NO_SERVER")
        )
        if no_server != "1":
            self.control_server = ControlServer(self.engine)
            url = self.control_server.start()
            _log(f"[voicepranks] control API listening at {url}\n")
            # The API can open the mic, so it requires a token. Opening the URL
            # in a browser just works (the page is served with the token baked
            # in); scripted callers need it spelled out.
            _log(f"[voicepranks] control token: {self.control_server.token}\n")
            _log(f"[voicepranks] token file: {self.control_server.token_path}\n")

        # Device lists + persisted device selection (see settings.py). Loaded
        # before the widgets so the Settings dialog and engine start with the
        # saved devices.
        self._input_devices: List[Tuple[int, str]] = []
        self._output_devices: List[Tuple[int, str]] = []
        self.settings: Dict = settings_mod.load()
        self._load_devices_and_apply_settings()

        self._build_widgets()
        self._apply_saved_settings()  # restore last preset + params (#4)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._refresh_status()

    # -- UI construction -------------------------------------------------

    def _build_widgets(self) -> None:
        frame = ttk.Frame(self.root, padding=12)
        frame.grid(row=0, column=0, sticky="nsew")
        self.root.rowconfigure(0, weight=1)
        self.root.columnconfigure(0, weight=1)
        frame.rowconfigure(8, weight=1)
        frame.columnconfigure(0, weight=1)

        # -- top bar: Turn On | Settings… | Preset dropdown ------------------
        top = ttk.Frame(frame)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.toggle_button = ttk.Button(top, text="Turn On", command=self._on_toggle)
        self.toggle_button.pack(side="left")
        ttk.Button(top, text="Settings…", command=self._open_settings).pack(side="left", padx=(6, 0))
        ttk.Label(top, text="Preset:").pack(side="left", padx=(16, 4))
        self.preset_var = tk.StringVar(value=_PRESET_NONE)
        preset_values = [_PRESET_NONE] + [n.title() for n in presets_mod.preset_names()]
        self.preset_combo = ttk.Combobox(
            top, textvariable=self.preset_var, values=preset_values, state="readonly", width=14
        )
        self.preset_combo.pack(side="left")
        self.preset_combo.bind("<<ComboboxSelected>>", self._on_preset_selected)

        self._build_recorder_row(frame, row=1)
        self._build_ramp_row(frame, row=2)
        self._build_output_volume_row(frame, row=3)
        self._build_playback_volume_row(frame, row=4)
        self._build_custom_laugh_row(frame, row=5)
        self._build_jumpscare_assets_row(frame, row=6)

        ttk.Label(frame, text="Parameters (click a section to expand)").grid(
            row=7, column=0, sticky="w", pady=(6, 0)
        )

        # -- scrollable area of collapsible param sections -------------------
        scroll_container = ttk.Frame(frame)
        scroll_container.grid(row=8, column=0, sticky="nsew", pady=(2, 8))
        scroll_container.rowconfigure(0, weight=1)
        scroll_container.columnconfigure(0, weight=1)

        canvas = tk.Canvas(scroll_container, borderwidth=0, highlightthickness=0)
        vsb = ttk.Scrollbar(scroll_container, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        self._param_grid = ttk.Frame(canvas)
        self._param_grid.bind(
            "<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=self._param_grid, anchor="nw")
        self._param_grid.columnconfigure(0, weight=1)

        self._build_param_controls(self._param_grid)

        self.status_label = ttk.Label(frame, text="", justify="left")
        self.status_label.grid(row=9, column=0, sticky="w", pady=(4, 0))

    def _build_recorder_row(self, frame: ttk.Frame, row: int) -> None:
        rec_frame = ttk.Frame(frame)
        rec_frame.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        ttk.Button(rec_frame, text="Record", command=self._on_record).pack(side="left", padx=(0, 4))
        ttk.Button(rec_frame, text="Stop", command=self._on_record_stop).pack(side="left", padx=4)
        # Primary flow: Play replays the live processed take verbatim (the
        # effect as it was applied in real time -- no re-render).
        ttk.Button(rec_frame, text="Play", command=self._on_play).pack(side="left", padx=4)
        # Secondary A/B flow: re-render the dry take with current params.
        ttk.Button(rec_frame, text="Re-render & Play", command=self._on_rerender_play).pack(side="left", padx=4)

        # Live monitor: also hear the processed effect on your speakers while
        # recording. Off by default -- Record works without it. Bound to the
        # same `monitor` param as the API, hence skipped from the generated
        # grid (see `_SKIP_PARAM_NAMES`).
        self.monitor_var = tk.BooleanVar(value=self.engine.monitor_enabled)
        ttk.Checkbutton(
            rec_frame,
            text="Live monitor",
            variable=self.monitor_var,
            command=self._on_monitor_toggle,
        ).pack(side="left", padx=(12, 0))

    def _build_custom_laugh_row(self, frame: ttk.Frame, row: int) -> None:
        # Pick which laugh the goofy preset punctuates speech with: a bundled
        # pre-recorded clip (goofy/scooby) or one you record yourself. "Play
        # Laugh" auditions the active clip on demand.
        
        lframe = ttk.Frame(frame)

        lframe.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(4, 0))

        ttk.Label(lframe, text="Laugh:").pack(side="left", padx=(0, 6))

        ttk.Button(lframe, text="Play Laugh", command=self._on_play_laugh).pack(
            side="left", padx=(0, 4)
        )

        ttk.Button(
            lframe, text="Goofy laugh", command=lambda: self._on_select_laugh("goofy")
        ).pack(side="left", padx=4)

        ttk.Button(
            lframe, text="Scooby laugh", command=lambda: self._on_select_laugh("scooby")
        ).pack(side="left", padx=4)

        self.laugh_record_button = ttk.Button(
            lframe, text="Record laugh", command=self._on_record_laugh
        )

        self.laugh_record_button.pack(side="left", padx=(12, 4))

        ttk.Button(lframe, text="Stop & use", command=self._on_record_laugh_stop).pack(
            side="left", padx=4
        )

        # Which clip is live, kept fresh by the status poll.
        self.laugh_source_label = ttk.Label(lframe, text="")
        self.laugh_source_label.pack(side="left", padx=(10, 0))

    def _build_jumpscare_assets_row(self, frame: ttk.Frame, row: int) -> None:
        assets = ttk.Frame(frame)
        assets.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        ttk.Label(assets, text="Jumpscare assets:").pack(side="left", padx=(0, 6))
        ttk.Button(assets, text="Choose PNG/GIF", command=self._choose_jumpscare_visual).pack(side="left", padx=4)
        ttk.Button(assets, text="Choose WAV", command=self._choose_jumpscare_sound).pack(side="left", padx=4)
        self.jumpscare_assets_label = ttk.Label(assets, text=self._jumpscare_asset_text())
        self.jumpscare_assets_label.pack(side="left", padx=(8, 0))

    def _jumpscare_asset_text(self) -> str:
        visual = self.settings.get("jumpscare_visual") or "built-in Foxy GIF"
        sound = self.settings.get("jumpscare_sound")
        return f"visual: {visual or 'not set'}; sound: {sound or 'not set'}"

    def _choose_jumpscare_visual(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("Images", "*.png *.gif")])
        if not path:
            return
        try:
            self.settings["jumpscare_visual"] = copy_visual(path)
            self.jumpscare_assets_label.config(text=self._jumpscare_asset_text())
            self._save_settings()
            self.error_message = None
        except Exception as exc:
            self.error_message = f"Jumpscare visual must be a PNG or GIF: {exc}"

    def _choose_jumpscare_sound(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("16-bit PCM WAV", "*.wav")])
        if not path:
            return
        try:
            self.settings["jumpscare_sound"] = copy_sound(path)
            self.jumpscare_assets_label.config(text=self._jumpscare_asset_text())
            self._save_settings()
            self.error_message = None
        except Exception as exc:
            self.error_message = str(exc)

    def _build_ramp_row(self, frame: ttk.Frame, row: int) -> None:
        # Intensity ramp-in duration as hours/minutes/seconds text boxes,
        # plus a Clear button for no ramp (effect at full immediately).
        rframe = ttk.Frame(frame)
        rframe.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        ttk.Label(rframe, text="Ramp-in:").pack(side="left", padx=(0, 6))
        self.ramp_h = tk.StringVar()
        self.ramp_m = tk.StringVar()
        self.ramp_s = tk.StringVar()
        for var, unit in ((self.ramp_h, "h"), (self.ramp_m, "m"), (self.ramp_s, "s")):
            entry = ttk.Entry(rframe, textvariable=var, width=5, justify="right")
            entry.pack(side="left")
            entry.bind("<Return>", lambda _e: self._on_ramp_set())
            ttk.Label(rframe, text=unit).pack(side="left", padx=(1, 6))
        ttk.Button(rframe, text="Set", command=self._on_ramp_set).pack(side="left", padx=(4, 0))
        ttk.Button(rframe, text="Clear (no ramp)", command=self._on_ramp_clear).pack(side="left", padx=4)
        self._populate_ramp_fields()

    def _build_output_volume_row(self, frame: ttk.Frame, row: int) -> None:
        # Post-effect outgoing level (what other apps hear).
        self._build_gain_row(frame, row, "output_gain", self.engine.output_gain)

    def _build_playback_volume_row(self, frame: ttk.Frame, row: int) -> None:
        # Listening-side level: Play, Play Laugh, and the automatic-laugh
        # headphone echo. Sits next to Output volume so the two read as a pair.
        self._build_gain_row(frame, row, "playback_gain", self.engine.playback_gain)

    def _build_gain_row(
        self, frame: ttk.Frame, row: int, param_name: str, current_value: float
    ) -> None:
        # Shared builder for the always-visible linear-gain rows (Output volume,
        # Playback volume), pulled out of the collapsed param grid so they're
        # adjustable at a glance. Built from the shared `_build_one_control`
        # slider so the var, drag-guard, and status-poll sync wiring come for
        # free (same as the generated grid) -- only the always-visible placement,
        # the percent readout, and the Reset button are bespoke. These are the
        # same params as the API, skipped from the grid (see `_SKIP_PARAM_NAMES`).
        # Seed from the live engine value (not spec.default) so an out-of-band
        # change made before this window built -- e.g. via the control API, which
        # starts before the widgets -- shows correctly rather than 100% until the
        # first status poll.
        vframe = ttk.Frame(frame)
        vframe.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        vframe.columnconfigure(1, weight=1)  # let the slider stretch
        spec = PARAM_SPECS_BY_NAME[param_name]
        self._build_one_control(vframe, spec, row=0)
        widget = self._param_widgets[spec.name]
        widget["readout_fmt"] = self._format_gain
        widget["var"].set(float(current_value))
        widget["readout"].config(text=self._format_gain(current_value))
        ttk.Button(
            vframe, text="Reset", command=lambda n=param_name: self._on_gain_reset(n)
        ).grid(row=0, column=3, padx=(6, 0))

    @staticmethod
    def _format_gain(gain) -> str:
        return f"{int(round(float(gain) * 100))}%"

    def _on_gain_reset(self, param_name: str) -> None:
        # `var.set` moves the slider knob; the explicit `_on_param_slider` call
        # then applies the value to the engine and refreshes the readout
        # synchronously (the Scale's own command fires via the Tk event loop,
        # so we can't rely on it having run by the time this returns).
        spec = PARAM_SPECS_BY_NAME[param_name]
        self._param_widgets[spec.name]["var"].set(1.0)
        self._on_param_slider(spec, 1.0)

    @staticmethod
    def _split_seconds(total: float) -> Tuple[int, int, float]:
        total = max(0.0, float(total))
        hours = int(total // 3600)
        minutes = int((total % 3600) // 60)
        seconds = total - hours * 3600 - minutes * 60
        return hours, minutes, seconds

    def _populate_ramp_fields(self) -> None:
        hours, minutes, seconds = self._split_seconds(self.engine.ramp.duration_s)
        self.ramp_h.set(str(hours))
        self.ramp_m.set(str(minutes))
        self.ramp_s.set(f"{seconds:g}")

    @staticmethod
    def _parse_field(var: tk.StringVar) -> float:
        text = (var.get() or "").strip()
        if not text:
            return 0.0
        try:
            return max(0.0, float(text))
        except ValueError:
            return 0.0

    def _on_ramp_set(self) -> None:
        total = (
            self._parse_field(self.ramp_h) * 3600.0
            + self._parse_field(self.ramp_m) * 60.0
            + self._parse_field(self.ramp_s)
        )
        try:
            # Set the duration AND restart the ramp timer from 0, so the
            # fade-in replays with the new duration.
            self.engine.restart_ramp(total)
        except Exception as exc:
            self.error_message = f"Failed to set ramp: {exc}"
        self._populate_ramp_fields()  # normalize the display (e.g. 90s -> 1m 30s)

    def _on_ramp_clear(self) -> None:
        try:
            self.engine.set_param("ramp.duration_s", 0.0)
        except Exception as exc:
            self.error_message = f"Failed to clear ramp: {exc}"
        self.ramp_h.set("0")
        self.ramp_m.set("0")
        self.ramp_s.set("0")

    def _build_param_controls(self, parent: ttk.Frame) -> None:
        groups: Dict[str, List[ParamSpec]] = {}
        for spec in PARAM_SPECS:
            if spec.name in _SKIP_PARAM_NAMES:
                continue
            groups.setdefault(spec.group, []).append(spec)

        # One collapsible section per group: a clickable header toggles a body
        # frame of that group's controls. All collapsed by default.
        self._group_bodies: Dict[str, ttk.Frame] = {}
        self._group_headers: Dict[str, ttk.Label] = {}
        self._group_expanded: Dict[str, bool] = {}

        row = 0
        for group in GROUP_ORDER:
            specs = groups.get(group)
            if not specs:
                continue

            header = ttk.Label(parent, text="", cursor="hand2")
            header.configure(font=("TkDefaultFont", 10, "bold"))
            header.grid(row=row, column=0, sticky="w", pady=(8, 2))
            header.bind("<Button-1>", lambda _e, g=group: self._toggle_group(g))
            row += 1

            body = ttk.Frame(parent)
            body.grid(row=row, column=0, sticky="ew")
            body.columnconfigure(1, weight=1)
            body.grid_remove()  # collapsed by default
            row += 1

            for brow, spec in enumerate(specs):
                self._build_one_control(body, spec, brow)

            self._group_bodies[group] = body
            self._group_headers[group] = header
            self._group_expanded[group] = False
            self._update_group_header(group)

    def _group_header_text(self, group: str, expanded: bool) -> str:
        arrow = "▾" if expanded else "▸"  # ▾ expanded / ▸ collapsed
        return f"{arrow}  {GROUP_LABELS.get(group, group)}"

    def _update_group_header(self, group: str) -> None:
        self._group_headers[group].config(
            text=self._group_header_text(group, self._group_expanded[group])
        )

    def _toggle_group(self, group: str) -> None:
        expanded = not self._group_expanded[group]
        self._group_expanded[group] = expanded
        body = self._group_bodies[group]
        if expanded:
            body.grid()
        else:
            body.grid_remove()
        self._update_group_header(group)

    def _build_one_control(self, parent: ttk.Frame, spec: ParamSpec, row: int) -> None:
        ttk.Label(parent, text=spec.label).grid(row=row, column=0, sticky="w", padx=(4, 4))

        if spec.kind == "bool":
            var = tk.BooleanVar(value=bool(spec.default))
            cb = ttk.Checkbutton(
                parent, variable=var, command=lambda s=spec, v=var: self._on_param_checkbox(s, v)
            )
            cb.grid(row=row, column=1, columnspan=2, sticky="w")
            self._param_widgets[spec.name] = {"kind": "bool", "var": var, "spec": spec}
            return

        var = tk.DoubleVar(value=float(spec.default))
        scale = ttk.Scale(
            parent,
            from_=spec.min,
            to=spec.max,
            orient="horizontal",
            variable=var,
            command=lambda val, s=spec: self._on_param_slider(s, val),
        )
        scale.grid(row=row, column=1, sticky="ew", padx=(4, 4))
        readout = ttk.Label(parent, text=self._format_value(spec, spec.default), width=8)
        readout.grid(row=row, column=2, sticky="w")
        scale.bind("<ButtonPress-1>", lambda _e, n=spec.name: self._dragging.__setitem__(n, True))
        scale.bind("<ButtonRelease-1>", lambda _e, n=spec.name: self._dragging.__setitem__(n, False))
        # `readout_fmt` lets a caller override the readout text (e.g. the
        # always-visible Output volume row shows a percentage); None -> the
        # default `_format_value`.
        self._param_widgets[spec.name] = {
            "kind": spec.kind, "var": var, "readout": readout, "spec": spec, "readout_fmt": None
        }

    def _readout_text(self, widget: dict, value) -> str:
        """Format a slider's readout, honoring an optional per-widget
        `readout_fmt` override (falls back to the generic `_format_value`)."""
        fmt = widget.get("readout_fmt")
        return fmt(value) if fmt else self._format_value(widget["spec"], value)

    @staticmethod
    def _format_value(spec: ParamSpec, value) -> str:
        if spec.kind == "int":
            return str(int(round(float(value))))
        return f"{float(value):.3g}"

    # -- devices + settings ------------------------------------------------

    def _load_devices_and_apply_settings(self) -> None:
        """List devices, resolve the persisted device selection, and apply it
        to the engine (before it starts)."""
        try:
            self._input_devices = list_input_devices()
        except Exception as exc:
            self.error_message = f"Could not list input devices: {exc}"
        try:
            self._output_devices = list_output_devices()
        except Exception as exc:
            self.error_message = f"Could not list output devices: {exc}"

        resolved = settings_mod.resolve_all(self.settings, self._input_devices, self._output_devices)
        # Input/output feed the recording/processing chain; playback is the
        # listening device (Play button + live monitor). Unset -> engine
        # defaults (auto virtual cable for output, system default elsewhere).
        if resolved["input_device"] is not None:
            self.engine.input_device = resolved["input_device"]
        if resolved["output_device"] is not None:
            self.engine.output_device = resolved["output_device"]
        self.engine.set_playback_device(resolved["playback_device"])

    def _device_name(self, devices: List[Tuple[int, str]], index: Optional[int]) -> Optional[str]:
        if index is None:
            return None
        for idx, name in devices:
            if idx == index:
                return name
        return None

    def _open_settings(self) -> None:
        """Modal dialog to pick the three audio devices; persisted on Save."""
        dlg = tk.Toplevel(self.root)
        dlg.title("Settings — audio devices")
        dlg.transient(self.root)
        dlg.resizable(False, False)
        body = ttk.Frame(dlg, padding=12)
        body.grid(row=0, column=0, sticky="nsew")
        body.columnconfigure(1, weight=1)

        def combo_for(devices, current_idx):
            values = ["(default)"] + [f"{idx}: {name}" for idx, name in devices]
            # No `textvariable`: a readonly combobox holds its own selection
            # (set via `cb.current()`, read back in `parse` via `cb.current()`).
            # A linked StringVar here would only be a local that CPython
            # garbage-collects on return, unsetting the Tcl variable and
            # blanking the display -- so we deliberately don't use one.
            cb = ttk.Combobox(body, values=values, state="readonly", width=34)
            sel = 0
            for i, (idx, _name) in enumerate(devices):
                if idx == current_idx:
                    sel = i + 1
                    break
            cb.current(sel)
            return cb

        # Preselect each dropdown from the user's persisted choice (resolved
        # against the current device list), NOT from engine.*: once the engine
        # has started, engine.start() overwrites engine.output_device with the
        # auto-resolved virtual cable, which would otherwise show here as an
        # explicit selection instead of "(default)".
        resolved = settings_mod.resolve_all(
            self.settings, self._input_devices, self._output_devices
        )
        rows = [
            ("Input mic (record from)", self._input_devices, resolved["input_device"]),
            ("Output mic (to other apps)", self._output_devices, resolved["output_device"]),
            ("Output playback (listen)", self._output_devices, resolved["playback_device"]),
        ]
        combos = {}
        for r, (label, devices, current) in enumerate(rows):
            ttk.Label(body, text=label).grid(row=r, column=0, sticky="w", pady=4, padx=(0, 8))
            cb = combo_for(devices, current)
            cb.grid(row=r, column=1, sticky="ew", pady=4)
            combos[label] = (cb, devices)

        ttk.Label(
            body,
            text="Input/Output feed recording; Playback is only for listening.\n"
            "Saved to ~/.voicepranks/settings.json.",
            foreground="#666",
            justify="left",
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 4))

        btns = ttk.Frame(body)
        btns.grid(row=4, column=0, columnspan=2, sticky="e", pady=(8, 0))

        def parse(cb, devices):
            i = cb.current()
            if i <= 0:
                return None
            return devices[i - 1][0]

        def on_save():
            in_idx = parse(*combos["Input mic (record from)"])
            out_idx = parse(*combos["Output mic (to other apps)"])
            play_idx = parse(*combos["Output playback (listen)"])
            self._apply_and_save_devices(in_idx, out_idx, play_idx)
            dlg.destroy()

        ttk.Button(btns, text="Cancel", command=dlg.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(btns, text="Save", command=on_save).pack(side="right")

        dlg.grab_set()
        dlg.focus_set()

    def _apply_and_save_devices(
        self, in_idx: Optional[int], out_idx: Optional[int], play_idx: Optional[int]
    ) -> None:
        """Apply the three chosen devices to the engine and persist them."""
        try:
            # set_param handles a running engine's stream restart; playback is
            # applied directly (re-opens the monitor stream if active).
            self.engine.set_param("io.input_device", -1 if in_idx is None else in_idx)
            self.engine.set_param("io.output_device", -1 if out_idx is None else out_idx)
            self.engine.set_playback_device(play_idx)
        except Exception as exc:
            self.error_message = f"Failed to apply devices: {exc}"

        self.settings = settings_mod.update_devices(
            self.settings,
            input_device=settings_mod.device_entry(in_idx, self._device_name(self._input_devices, in_idx)),
            output_device=settings_mod.device_entry(out_idx, self._device_name(self._output_devices, out_idx)),
            playback_device=settings_mod.device_entry(play_idx, self._device_name(self._output_devices, play_idx)),
        )
        try:
            settings_mod.save(self.settings)
        except Exception as exc:
            self.error_message = f"Failed to save settings: {exc}"

    # -- event handlers ----------------------------------------------------

    def _on_toggle(self) -> None:
        if not self.engine.running:
            try:
                self.engine.start()
            except RuntimeError as exc:
                self.error_message = str(exc)
                self._refresh_status()
                return
            except Exception as exc:
                self.error_message = f"Failed to start audio: {exc}"
                self._refresh_status()
                return

        self.engine.set_enabled(not self.engine.enabled)
        self.toggle_button.config(text="Turn Off" if self.engine.enabled else "Turn On")

    def _on_preset_selected(self, _evt) -> None:
        value = self.preset_var.get()
        try:
            if value == _PRESET_NONE:
                # "None" removes all effects: neutral plain voice (#2).
                self.engine.apply_params(presets_mod.NEUTRAL)
            else:
                self.engine.apply_preset(value.lower())
        except Exception as exc:
            self.error_message = f"Apply preset '{value}' failed: {exc}"
        self._save_settings()

    def _on_param_slider(self, spec: ParamSpec, value_str: str) -> None:
        if self._suppress_commands:
            return
        try:
            value = float(value_str)
        except (TypeError, ValueError):
            return
        if spec.kind == "int":
            value = int(round(value))
        widget = self._param_widgets.get(spec.name)
        if widget is not None:
            widget["readout"].config(text=self._readout_text(widget, value))
        try:
            self.engine.set_param(spec.name, value)
        except Exception as exc:
            self.error_message = f"Failed to set {spec.name}: {exc}"
        self._schedule_save()

    def _on_param_checkbox(self, spec: ParamSpec, var: tk.BooleanVar) -> None:
        if self._suppress_commands:
            return
        try:
            self.engine.set_param(spec.name, bool(var.get()))
        except Exception as exc:
            self.error_message = f"Failed to set {spec.name}: {exc}"
        self._schedule_save()

    # -- persistence (remember preset + params across sessions, #4) --------

    @staticmethod
    def _is_persistable(name: str) -> bool:
        """Whether a param is remembered across sessions (not session/device)."""
        return name not in _PERSIST_SKIP_NAMES and not name.startswith("io.")

    def _persistable_params(self) -> Dict:
        """Snapshot of the sound params to remember (skips session/device)."""
        return {
            name: value
            for name, value in self.engine.snapshot().items()
            if self._is_persistable(name)
        }

    def _save_settings(self) -> None:
        """Persist the current preset + params into the shared settings file
        (device entries, written elsewhere, are preserved)."""
        self.settings["preset"] = self.preset_var.get()
        self.settings["params"] = self._persistable_params()
        try:
            settings_mod.save(self.settings)
        except OSError as exc:
            self.error_message = f"Could not save settings: {exc}"

    def _schedule_save(self) -> None:
        """Debounced save so a slider drag doesn't hammer the disk."""
        if self._save_after_id is not None:
            self.root.after_cancel(self._save_after_id)
        self._save_after_id = self.root.after(SAVE_DEBOUNCE_MS, self._save_settings)

    def _apply_saved_settings(self) -> None:
        """Restore the last-used params + preset selection on startup."""
        params = self.settings.get("params")
        if isinstance(params, dict):
            self._suppress_commands = True
            try:
                for name, value in params.items():
                    if not self._is_persistable(name):
                        continue
                    try:
                        self.engine.set_param(name, value)  # validates the name
                    except Exception:
                        pass  # ignore a stale/invalid/renamed saved key
            finally:
                self._suppress_commands = False
        preset = self.settings.get("preset")
        if preset in self.preset_combo["values"]:
            self.preset_var.set(preset)

    # -- recorder ----------------------------------------------------------

    def _ensure_engine_running(self, context: str) -> bool:
        """Start the audio engine if it isn't already. Returns True on success;
        on failure records an error mentioning `context` and returns False."""
        if self.engine.running:
            return True
        try:
            self.engine.start()
            return True
        except Exception as exc:
            self.error_message = f"Could not start audio for {context}: {exc}"
            return False

    def _on_record(self) -> None:
        # Run the effect live during capture so the user hears the real-time
        # result (via the live monitor) while recording, not just the offline
        # re-render. Recording still captures the DRY signal, so re-render
        # with tweaked params keeps working.
        if not self._ensure_engine_running("recording"):
            return
        if not self.engine.enabled:
            self.engine.set_enabled(True)
            self.toggle_button.config(text="Turn Off")
        self.engine.record_start()

    def _on_record_stop(self) -> None:
        self.engine.record_stop()

    def _on_record_laugh(self) -> None:
        # Capturing the laugh needs the input stream live, but not the effect
        # enabled -- the clip is a dry recording of you, so we don't force the
        # voice changer on the way the take recorder does.
        if not self._ensure_engine_running("laugh recording"):
            return
        self.engine.record_laugh_start()
        self.error_message = None

    def _on_record_laugh_stop(self) -> None:
        try:
            self.engine.record_laugh_stop()
            self.error_message = None  # clear any stale "empty/silent" error
        except ValueError:
            self.error_message = "Laugh recording was empty or silent -- nothing saved."
        except Exception as exc:
            self.error_message = f"Saving custom laugh failed: {exc}"

    def _on_select_laugh(self, name: str) -> None:
        # Switch the active laugh to a bundled pre-recorded clip (goofy/scooby).
        try:
            self.engine.select_laugh(name)
            self.error_message = None
        except Exception as exc:
            self.error_message = f"Selecting {name} laugh failed: {exc}"

    def _on_play_laugh(self) -> None:
        # Audition the active laugh clip immediately through the speakers. Works
        # whether or not the engine is running (routes to the output device).
        try:
            self.engine.play_laugh()
            self.error_message = None
        except Exception as exc:
            self.error_message = f"Play laugh failed: {exc}"

    def _on_monitor_toggle(self) -> None:
        self.engine.set_param("monitor", self.monitor_var.get())

    def _on_play(self) -> None:
        # Replay the live processed take exactly as it was captured -- no
        # re-render, no params re-applied.
        try:
            self.engine.play("live")
        except Exception as exc:
            self.error_message = f"Play failed: {exc}"

    def _on_rerender_play(self) -> None:
        try:
            self.engine.render_current()
            self.engine.play("rendered")
        except Exception as exc:
            self.error_message = f"Re-render/play failed: {exc}"

    # -- status polling ------------------------------------------------

    def _dismiss_jumpscare(self) -> None:
        self._active_jumpscare = None

    def _show_pending_jumpscare(self, fire_count: int) -> None:
        """Present one scheduled scare from the Tk thread, never the callback."""
        if fire_count == self._last_jumpscare_fires:
            return
        self._last_jumpscare_fires = fire_count
        visual_asset_path = visual_path(self.settings.get("jumpscare_visual"))
        sound_path = stored_path(self.settings.get("jumpscare_sound"))
        if not visual_asset_path.exists() or sound_path is None or not sound_path.exists():
            self.error_message = "Jumpscare skipped: choose a 16-bit PCM WAV sound first."
            return
        try:
            validate_visual(visual_asset_path)
            sample_rate, clip = validate_sound(sound_path)
            if self._active_jumpscare is not None:
                self._active_jumpscare.dismiss()
            scare = FullscreenJumpscare(
                self.root, visual_asset_path, len(clip) / sample_rate, self._dismiss_jumpscare
            )
            self._active_jumpscare = scare
            scare.show()
            self.engine.play_local_clip(clip, sample_rate)
            self.error_message = None
        except Exception as exc:
            self.error_message = f"Jumpscare skipped: {exc}"

    def _refresh_status(self) -> None:
        # Echo a just-fired automatic laugh to the headphones (monitor-off case).
        # Runs on this main-thread poll -- `sd.play` must not run on the audio
        # thread -- and is best-effort so a playback hiccup never kills the loop.
        try:
            self.engine.poll_pending_laugh_echo()
            self.engine.poll_pending_reaction_echo()
            self._show_pending_jumpscare(self.engine.get_status().get("jumpscare_fires", 0))
        except Exception as exc:
            self.error_message = f"Sound-effect echo failed: {exc}"

        status = self.engine.get_status()
        snapshot = self.engine.snapshot()

        # Reflect any out-of-band (API-driven) param changes in the
        # widgets, without re-triggering their commands and without
        # yanking a slider the user is currently dragging.
        self._suppress_commands = True
        try:
            for name, widget in self._param_widgets.items():
                if name not in snapshot:
                    continue
                value = snapshot[name]
                if widget["kind"] == "bool":
                    if bool(widget["var"].get()) != bool(value):
                        widget["var"].set(bool(value))
                else:
                    if self._dragging.get(name):
                        continue
                    try:
                        changed = abs(float(widget["var"].get()) - float(value)) > 1e-9
                    except (TypeError, ValueError):
                        changed = True
                    if changed:
                        widget["var"].set(value)
                        widget["readout"].config(text=self._readout_text(widget, value))
            # Keep the dedicated Live monitor checkbox in sync with
            # API-driven changes (it's not in the generated grid).
            if bool(self.monitor_var.get()) != bool(status.get("monitor", False)):
                self.monitor_var.set(bool(status.get("monitor", False)))
        finally:
            self._suppress_commands = False

        # Reflect the custom-laugh state: which clip is live and whether a
        # laugh is currently being captured.
        laugh_recording = bool(status.get("laugh_recording"))
        active_laugh = status.get("active_laugh", "goofy")
        label = _LAUGH_LABELS.get(active_laugh, f"{active_laugh} laugh active")
        self.laugh_source_label.config(text=label)
        self.laugh_record_button.config(
            text="Recording laugh…" if laugh_recording else "Record laugh"
        )

        level = status.get("level", {})
        error_suffix = f"\nError: {self.error_message}" if self.error_message else ""
        take_state = "recording" if status.get("recording") else "stopped"
        monitor_state = "on" if status.get("monitor") else "off"
        text = (
            f"Running: {status['running']}   Enabled: {status['enabled']}   "
            f"Monitor: {monitor_state}   Latency: {status.get('latency_ms', 0.0):.0f}ms\n"
            f"Underruns: {status['underruns']}   Overruns: {status['overruns']}\n"
            f"Level  dry: {level.get('dry_rms', 0.0):.3f} rms / {level.get('dry_peak', 0.0):.3f} pk   "
            f"out: {level.get('processed_rms', 0.0):.3f} rms / {level.get('processed_peak', 0.0):.3f} pk\n"
            f"Take: {take_state}, {status.get('take_seconds', 0.0):.1f}s   "
            f"live={'yes' if status.get('has_live_take') else 'no'}   "
            f"raw={'yes' if status.get('has_raw_take') else 'no'}   "
            f"rendered={'yes' if status.get('has_rendered_take') else 'no'}"
            f"{error_suffix}"
        )
        self.status_label.config(text=text)
        self.root.after(STATUS_REFRESH_MS, self._refresh_status)

    # -- shutdown ------------------------------------------------------

    def _on_close(self) -> None:
        try:
            if self._active_jumpscare is not None:
                self._active_jumpscare.dismiss()
            if self._save_after_id is not None:
                self.root.after_cancel(self._save_after_id)
                self._save_after_id = None
            self._save_settings()  # flush final state before exit
            if self.control_server is not None:
                self.control_server.stop()
            if self.engine.running:
                self.engine.stop()
        finally:
            self.root.destroy()


def _warn_if_broken_tk() -> None:
    """Apple's system-Python Tk (8.5) renders blank/broken windows on modern
    macOS. Warn loudly with the fix instead of leaving the user staring at an
    empty window."""
    if tk.TkVersion < 8.6:
        _log(
            "\n[voicepranks] WARNING: this Python is using Tk "
            f"{tk.TkVersion}, which renders blank windows on modern macOS.\n"
            "  Fix: install a Python with a newer Tk, e.g.\n"
            "    brew install python-tk\n"
            "  then recreate your venv with that Python "
            "(/opt/homebrew/bin/python3).\n\n"
        )


def _report_callback_exception(exc, val, tb) -> None:
    """Tk invokes this on an uncaught exception inside a callback. The stock
    handler writes the traceback to sys.stderr, which is None in a windowed
    build -- the exact crash `_log` guards against -- so route through it."""
    _log("".join(traceback.format_exception(exc, val, tb)))


def run() -> None:
    _warn_if_broken_tk()
    root = tk.Tk()
    root.report_callback_exception = _report_callback_exception
    VoicePranksApp(root)
    root.mainloop()
