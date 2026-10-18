"""Tk-only fullscreen presentation for user-provided jumpscare assets."""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from typing import Callable, Optional

class FullscreenJumpscare:
    """A disposable primary-display window with cleanly cancellable timers."""

    def __init__(
        self,
        root: tk.Tk,
        visual_path: Path,
        duration_s: float,
        on_dismiss: Callable[[], None],
        on_present: Optional[Callable[[], None]] = None,
    ):
        self.root = root
        self.visual_path = visual_path
        self.duration_s = duration_s
        self.on_dismiss = on_dismiss
        self.on_present = on_present
        self.window: Optional[tk.Toplevel] = None
        self._frames = []
        self._frame_delays = []
        self._frame_index = 0
        self._frame_after: Optional[str] = None
        self._static_after: Optional[str] = None

    def show(self) -> None:
        width, height = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        win = tk.Toplevel(self.root)
        self.window = win
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.geometry(f"{width}x{height}+0+0")
        win.configure(background="black")
        win.bind("<Escape>", lambda _event: self.dismiss())
        win.focus_force()
        # Start local audio before decoding/scaling GIF frames, which can be
        # expensive enough to make a perfectly valid sound seem missing.
        if self.on_present is not None:
            self.on_present()

        self._frames, self._frame_delays = self._load_frames(win, width, height)
        label = tk.Label(win, background="black", image=self._frames[0])
        label.pack(fill="both", expand=True)
        # Keep references alive on the widget too (Tk images otherwise vanish).
        label._jumpscare_frames = self._frames  # type: ignore[attr-defined]
        self._label = label
        if len(self._frames) > 1:
            self._schedule_frame()
        else:
            # A static PNG has no animation pass to complete; retain the
            # previous duration-based behavior for that user-provided format.
            self._static_after = win.after(max(1, int(self.duration_s * 1000)), self.dismiss)

    @staticmethod
    def _fit_size(source_width: int, source_height: int, max_width: int, max_height: int):
        """Largest aspect-ratio-preserving size wholly inside the display."""
        scale = min(max_width / source_width, max_height / source_height)
        return max(1, round(source_width * scale)), max(1, round(source_height * scale))

    def _load_frames(self, win: tk.Toplevel, max_width: int, max_height: int):
        try:
            from PIL import Image, ImageSequence, ImageTk
        except ImportError as exc:
            raise RuntimeError("Install Pillow (pip install -r requirements.txt) to display jumpscare images") from exc
        try:
            image = Image.open(self.visual_path)
        except OSError as exc:
            raise ValueError(f"Could not load visual: {exc}") from exc
        target_size = self._fit_size(image.width, image.height, max_width, max_height)
        frames, delays = [], []
        for frame in ImageSequence.Iterator(image):
            resized = frame.convert("RGBA").resize(target_size, Image.Resampling.LANCZOS)
            frames.append(ImageTk.PhotoImage(resized, master=win))
            delays.append(max(20, int(frame.info.get("duration", 60))))
        if not frames:
            raise ValueError("Image contains no readable frames")
        return frames, delays

    def _schedule_frame(self) -> None:
        if self.window is not None:
            self._frame_after = self.window.after(
                self._frame_delays[self._frame_index], self._advance_frame
            )

    def _advance_frame(self) -> None:
        if self.window is None:
            return
        if self._frame_index >= len(self._frames) - 1:
            self.dismiss()
            return
        self._frame_index = (self._frame_index + 1) % len(self._frames)
        self._label.configure(image=self._frames[self._frame_index])
        self._schedule_frame()

    def dismiss(self) -> None:
        win, self.window = self.window, None
        if win is None:
            return
        for after_id in (self._frame_after, self._static_after):
            if after_id is not None:
                try:
                    win.after_cancel(after_id)
                except tk.TclError:
                    pass
        self._frame_after = self._static_after = None
        try:
            win.destroy()
        finally:
            self.on_dismiss()
