"""Create a portable Windows release ZIP without repository or session data."""
import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


def package(root, version):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', version):
        raise ValueError('Use a simple version identifier.')
    folder = root / 'dist' / 'WorkbenchTray'
    for name in ['WorkbenchTray.exe', '_internal/mockup/index-v2.html', '_internal/web/live.js', '_internal/web/preview.js']:
        if not (folder / name).is_file():
            raise ValueError(f'Incomplete application bundle: {name}')
    commit = subprocess.check_output(['git','rev-parse','HEAD'], cwd=root, text=True).strip()
    dirty = bool(subprocess.check_output(['git','status','--porcelain'], cwd=root, text=True, stderr=subprocess.DEVNULL).strip())
    info = {'version': version, 'source_commit': commit, 'source_dirty': dirty, 'platform': 'windows-x64'}
    output = root / 'dist' / 'releases'
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f'WorkbenchTray-{version}-windows-x64.zip'
    with ZipFile(archive, 'w', ZIP_DEFLATED) as bundle:
        for file in sorted(folder.rglob('*')):
            if file.is_file():
                if file.is_symlink() or not file.resolve().is_relative_to(folder.resolve()):
                    raise ValueError(f'Unexpected external file in bundle: {file.name}')
                bundle.write(file, file.relative_to(folder.parent).as_posix())
        bundle.writestr('WorkbenchTray/BUILD_INFO.json', json.dumps(info, indent=2))
        bundle.writestr('WorkbenchTray/README.txt', (root / '.github' / 'tray-release-notes.md').read_text(encoding='utf-8'))
    with ZipFile(archive) as bundle:
        if bundle.testzip() is not None:
            raise ValueError('Release ZIP integrity check failed.')
    with archive.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    archive.with_suffix('.zip.sha256').write_text(f'{digest}  {archive.name}\n', encoding='ascii')
    return archive


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True)
    args = parser.parse_args()
    print(package(Path(__file__).resolve().parents[1], args.version))
