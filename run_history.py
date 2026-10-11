"""Disk-backed run records and isolated inputs for follow-up workflows."""
from __future__ import annotations

import copy
import json
import re
import shutil
from pathlib import Path


class RunHistory:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        # Bring forward snapshots saved by earlier versions of the web workbench.
        for path in self.directory.parent.glob('web-run-*.json'):
            try:
                payload = json.loads(path.read_text(encoding='utf-8'))
                run = payload.get('run', payload)
                if not self._path(run['id']).exists():
                    self.save(run)
            except (OSError, ValueError, KeyError, TypeError):
                continue

    def _path(self, run_id):
        if not isinstance(run_id, str) or not re.fullmatch(r'[a-f0-9]{32}', run_id):
            raise ValueError('Choose a saved run.')
        return self.directory / (run_id + '.json')

    def save(self, run):
        path = self._path(run['id'])
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(run, ensure_ascii=False), encoding='utf-8')
        temporary.replace(path)

    def get(self, run_id):
        try:
            run = json.loads(self._path(run_id).read_text(encoding='utf-8'))
        except FileNotFoundError as exc:
            raise ValueError('That saved run could not be found.') from exc
        if not isinstance(run, dict) or run.get('id') != run_id:
            raise ValueError('Saved run is invalid.')
        if run['status'] in {'running', 'paused', 'stopping'}:
            run['status'] = 'interrupted'
            run['error'] = 'The server stopped before this run finished. Start a follow-up to continue its work.'
        return run

    def list(self):
        runs = []
        for path in self.directory.glob('*.json'):
            try:
                runs.append(self.get(path.stem))
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return sorted(runs, key=lambda r: r.get('started', 0), reverse=True)


def artifact_path(run, filename):
    if not any(a['name'] == filename for a in run.get('artifacts', [])):
        raise ValueError('That file is not a saved result of this run.')
    workspace = Path(run['cwd']).resolve(strict=True)
    directory = (workspace / run['artifact_dir']).resolve(strict=True)
    path = (directory / filename).resolve(strict=True)
    if not directory.is_relative_to(workspace) or not path.is_relative_to(directory) or not path.is_file():
        raise ValueError('Result must stay inside the run directory.')
    return path


def prepare_source(source, directory):
    """Copy registered results, so follow-ups can work in a different workspace."""
    inputs = directory / 'source'
    inputs.mkdir()
    files = []
    total = 0
    # Validate everything before copying. Missing originals must be surfaced.
    for artifact in source.get('artifacts', []):
        name = artifact['name']
        if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*\.(md|html|pdf)', name):
            raise ValueError('Saved result has an invalid filename.')
        path = artifact_path(source, name)
        total += path.stat().st_size
        if total > 100 * 1024 * 1024:
            raise ValueError('Source results exceed the 100 MB follow-up limit.')
        files.append((name, path))
    for name, path in files:
        shutil.copyfile(path, inputs / name)
    # Context lives separately from copied filenames to avoid overwriting a result.
    context = {
        'id': source['id'], 'goal': source['goal'], 'status': source['status'],
        'error': source.get('error', ''), 'source_run_id': source.get('source_run_id'),
        'files': [name for name, _ in files], 'turns': copy.deepcopy(source.get('turns', [])),
        'final_response': source.get('output', ''),
    }
    (directory / 'source-context.json').write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding='utf-8')
    return [name for name, _ in files]
