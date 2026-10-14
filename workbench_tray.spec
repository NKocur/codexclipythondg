# Windows directory distribution: keep the .exe and _internal folder together.
from pathlib import Path
from workbench_tray import tray_image

root = Path(SPECPATH)
icon = root / 'build' / 'tray-assets' / 'workbench.ico'
icon.parent.mkdir(parents=True, exist_ok=True)
tray_image().save(icon, sizes=[(16,16),(32,32),(48,48),(64,64)])
a = Analysis(
    [str(root / 'workbench_tray.py')], pathex=[str(root)],
    datas=[(str(root / 'mockup' / 'index-v2.html'), 'mockup'), (str(root / 'web'), 'web')],
    hiddenimports=['pystray._win32','markdown_it'],
    excludes=['PyQt6','dragongui'],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='WorkbenchTray',
          console=False, icon=str(icon), upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name='WorkbenchTray', upx=False)
