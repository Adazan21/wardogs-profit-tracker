# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[
        # ctypes.WinDLL loads this dynamically (richpresence.py) — PyInstaller's
        # static analysis can't see that the way it sees a normal `import`, so
        # it has to be listed explicitly or the frozen exe won't have it.
        (r'A:\python\lib\site-packages\steamworkspy\steam_api64.dll', '.'),
    ],
    datas=[
        # Role XP icons (app.py's role_icon()) — plain PNGs, so PyInstaller's
        # static analysis can't find them the way it finds imported modules;
        # bundled explicitly and read back via sys._MEIPASS when frozen.
        ('icons', 'icons'),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['torch', 'torchvision', 'torchaudio', 'scipy', 'sympy', 'IPython', 'jupyter', 'notebook', 'jupyter_client', 'jupyter_core', 'ipykernel', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'tensorflow', 'sklearn', 'numba', 'llvmlite', 'transformers', 'nltk', 'spacy', 'tornado', 'zmq', 'pytest'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='profitdog',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
