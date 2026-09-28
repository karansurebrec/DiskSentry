# -*- mode: python ; coding: utf-8 -*-
# DiskSentry PyInstaller spec — single-file Windows executable.
# Build:  pyinstaller --clean --noconfirm DiskSentry.spec
# Output: dist/DiskSentry.exe
#
# v1.3 — NATIVE DESKTOP APPLICATION:
#   * console=False  -> no black terminal window behind the app
#   * bundles pywebview + pythonnet/clr_loader so the dashboard renders in a
#     native OS window (Microsoft Edge WebView2, preinstalled on Windows 11)
#   * run.py falls back to the default browser automatically if the desktop
#     shell is unavailable, so this exe never strands the user

from PyInstaller.utils.hooks import collect_all

_datas, _binaries, _hidden = [], [], []
# pywebview native window shell. pythonnet's clr_loader ships native .NET
# bridge files that PyInstaller's static analysis does NOT discover on its
# own — collect everything for these two packages.
for _pkg in ("webview", "clr_loader"):
    try:
        _d, _b, _h = collect_all(_pkg)
        _datas += _d
        _binaries += _b
        _hidden += _h
    except Exception:
        pass  # package absent -> build still works (browser-fallback exe)
_hidden += ["clr", "webview.platforms.winforms", "webview.platforms.edgechromium"]

a = Analysis(
    ['run.py'],
    pathex=[],
    binaries=_binaries,
    datas=[
        ('templates', 'templates'),
        ('static', 'static'),
    ] + _datas,
    hiddenimports=_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='DiskSentry',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,           # v1.3: real GUI app — no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/icon.ico',
    uac_admin=True,          # SMART access requires elevation on Windows
)
