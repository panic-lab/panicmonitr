# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller onefile build for panic-monitr.
#
# Produces a single self-contained executable that bundles CPython, the iroh
# Rust FFI, the crypto stack and the Flask dashboard, so end users need no
# Python installed at all.
#
#   pyinstaller --clean --noconfirm panic-monitor.spec   ->   dist/panic-monitor
#
# Targets PyInstaller 6.x.

from PyInstaller.utils.hooks import (
    collect_all,
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)

binaries = []
hiddenimports = ["iroh", "iroh.iroh_ffi"]

# The systemd unit template is read at install time (src/service_install.py);
# keep it under src/templates/ so the module's __file__/_MEIPASS lookup finds it.
datas = [("src/templates/panic-monitor.service.tmpl", "src/templates")]

# iroh ships libiroh_ffi.so (~31 MB) and loads it relative to its own package
# directory at import time. This is the single most likely thing to go missing.
binaries += collect_dynamic_libs("iroh")

# Pin the remaining compiled extensions defensively. PyInstaller's hooks
# usually catch these, but being explicit avoids a silent runtime ImportError.
for _pkg in (
    "nacl",
    "argon2",
    "_argon2_cffi_bindings",
    "psutil",
    "pydantic_core",
    "cffi",
    "_cffi_backend",
):
    binaries += collect_dynamic_libs(_pkg)

# keyring discovers its password backends through entry points at runtime; a
# bare `import keyring` would otherwise succeed while --password-from keyring
# fails to find any backend.
_kr_datas, _kr_bins, _kr_hidden = collect_all("keyring")
datas += _kr_datas
binaries += _kr_bins
hiddenimports += _kr_hidden

# APScheduler resolves executors / triggers / jobstores via plugin lookup.
hiddenimports += collect_submodules("apscheduler")

# Textual ships .tcss stylesheets and lazily imports widgets (--tui mode).
datas += collect_data_files("textual")
hiddenimports += collect_submodules("textual")

# Dashboard websocket terminal stack + the lazily-imported docker SDK
# (src/stats.py imports `docker` inside a try/except).
hiddenimports += ["flask_sock", "simple_websocket", "werkzeug.serving"]
hiddenimports += collect_submodules("docker")

# plotly (the Python package) is unused server-side — the dashboard pulls
# Plotly.js from a CDN — so exclude it to shave tens of MB off the binary.
excludes = ["plotly"]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="panic-monitor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
