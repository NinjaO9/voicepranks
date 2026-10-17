# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for VoicePranks.

One committed spec drives both macOS and Windows one-folder builds.
Build with:  pyinstaller voicepranks.spec --noconfirm

Notes:
- sounddevice wraps PortAudio; its native lib must be collected explicitly.
- tkinter / Tcl-Tk are pulled in by PyInstaller's standard hooks.
- macOS wraps the output in a .app BUNDLE and declares microphone usage so
  the OS actually grants mic access (without it the input stream is denied).
- Unsigned by design (casual distribution). Users click past the OS
  "unidentified developer" warning; see INSTALL.txt.

Only values that differ from PyInstaller's defaults are set below.
"""

import sys

from PyInstaller.utils.hooks import collect_dynamic_libs

APP_NAME = "VoicePranks"  # macOS .app display name
DIST_NAME = "voicepranks"  # Windows folder / exe name

# Bundle the PortAudio native lib sounddevice loads at runtime. It ships in the
# separate `_sounddevice_data` package (NOT inside `sounddevice`, which is a
# single module), so collect from there — collecting from "sounddevice" finds
# nothing. Preserving the `_sounddevice_data/portaudio-binaries` layout is what
# lets sounddevice locate the lib inside the frozen bundle.
binaries = collect_dynamic_libs("_sounddevice_data")

# Non-Python data files PyInstaller won't pick up from the import graph. Each
# `dest` keeps the package-relative layout so the code can locate the file via
# `__file__` inside the frozen bundle (same as when running from source).
#   - webui/index.html: the control-server status page (served at "/").
#   - assets/*.wav: the stock laugh clips the picker switches between; without
#     them the goofy/scooby laughs fall back to the synthesized giggle (or fail
#     to load) in the bundle. Every clip in `_PRESET_LAUGHS` (dsp/laugh.py) must
#     be listed here or its picker button can't switch to it in a packaged build.
datas = [
    ("voicepranks/webui/index.html", "voicepranks/webui"),
    # Includes goofy_laugh.wav and scooby_laugh.wav plus user-added effects.
    ("voicepranks/assets/*.wav", "voicepranks/assets"),
]

a = Analysis(
    ["launcher.py"],
    binaries=binaries,
    datas=datas,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=DIST_NAME,
    console=False,  # GUI app — no terminal window behind it
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    name=DIST_NAME,
)

# On macOS, wrap the one-folder output in a proper .app bundle.
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        bundle_identifier="com.voicepranks.app",
        info_plist={
            "NSMicrophoneUsageDescription": (
                "VoicePranks needs your microphone to apply the voice effect."
            ),
            "NSHighResolutionCapable": True,
        },
    )
