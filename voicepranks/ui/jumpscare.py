"""Tk-only fullscreen presentation for user-provided jumpscare assets."""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from typing import Callable, Optional


class FullscreenJumpscare:
    """A disposable primary-display window with cleanly cancellable timers."""

    def __init__(self, root: tk.Tk, visual_path: Path, duration_s: float, on_dismiss: Callable[[], None]):
        self.root = root
        self.visual_path = visual_path
        self.duration_s = duration_s
        self.on_dismiss = on_dismiss
        self.window: Optional[tk.Toplevel] = None
        self._frames = []
        self._frame_index = 0
        self._frame_after: Optional[str] = None
        self._close_after: Optional[str] = None

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

        self._frames = self._load_frames(win)
        label = tk.Label(win, background="black", image=self._frames[0])
        label.pack(fill="both", expand=True)
        # Keep references alive on the widget too (Tk images otherwise vanish).
        label._jumpscare_frames = self._frames  # type: ignore[attr-defined]
        self._label = label
        if len(self._frames) > 1:
            self._schedule_frame()
        self._close_after = win.after(max(1, int(self.duration_s * 1000)), self.dismiss)

    def _load_frames(self, win: tk.Toplevel):
        if self.visual_path.suffix.lower() != ".gif":
            return [tk.PhotoImage(master=win, file=str(self.visual_path))]
        frames = []
        index = 0
        while True:
            try:
                frames.append(tk.PhotoImage(master=win, file=str(self.visual_path), format=f"gif -index {index}"))
            except tk.TclError:
                break
            index += 1
        if not frames:
            raise ValueError("GIF contains no readable frames")
        return frames

    def _schedule_frame(self) -> None:
        if self.window is not None:
            self._frame_after = self.window.after(60, self._advance_frame)

    def _advance_frame(self) -> None:
        if self.window is None:
            return
        self._frame_index = (self._frame_index + 1) % len(self._frames)
        self._label.configure(image=self._frames[self._frame_index])
        self._schedule_frame()

    def dismiss(self) -> None:
        win, self.window = self.window, None
        if win is None:
            return
        for after_id in (self._frame_after, self._close_after):
            if after_id is not None:
                try:
                    win.after_cancel(after_id)
                except tk.TclError:
                    pass
        self._frame_after = self._close_after = None
        try:
            win.destroy()
        finally:
            self.on_dismiss()
