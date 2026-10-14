"""Disk-backed run records and isolated inputs for follow-up workflows."""
from __future__ import annotations

import copy
import json
import re
import shutil
import os
import hashlib
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
        if run['status'] == 'paused':
            run['status'] = 'suspended'
        elif run['status'] in {'running', 'stopping'}:
            run['status'] = 'interrupted'
            run['error'] = 'The server stopped before this run finished. Start a follow-up to continue its work.'
            if run.get('plan'):
                run['plan']['outcome'] = 'partial'
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
    # Carry the evidence tree as data, preserving relative links in reports.
    # Reject links/junctions rather than following them outside the source run.
    if source.get('artifact_dir'):
        workspace = Path(source['cwd']).resolve(strict=True)
        root = (workspace / source['artifact_dir']).resolve(strict=True)
        if not root.is_relative_to(workspace):
            raise ValueError('Source evidence must remain inside its workspace.')
        if directory.resolve().is_relative_to(root):
            raise ValueError('Follow-up output cannot be nested inside the source evidence directory.')
        seen = {name for name, _ in files}
        for parent, directories, filenames in os.walk(root, followlinks=False):
            directories[:] = [name for name in directories if name != '__pycache__']
            for name in [*directories, *filenames]:
                path = Path(parent) / name
                if path.is_symlink() or getattr(path.lstat(), 'st_file_attributes', 0) & 1024:
                    raise ValueError('Source evidence cannot contain symbolic links or junctions.')
            for name in filenames:
                path = Path(parent) / name
                relative = path.relative_to(root).as_posix()
                if relative in seen:
                    continue
                total += path.stat().st_size
                if total > 100 * 1024 * 1024 or len(files) >= 5000:
                    raise ValueError('Source evidence exceeds the 100 MB / 5000 file follow-up limit.')
                files.append((relative, path))
                seen.add(relative)
    manifest = {}
    for name, path in files:
        target = inputs / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        manifest[name] = hashlib.sha256(target.read_bytes()).hexdigest()
    (directory / 'source-evidence-manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    # Context lives separately from copied filenames to avoid overwriting a result.
    context = {
        'id': source['id'], 'goal': source['goal'], 'status': source['status'],
        'error': source.get('error', ''), 'source_run_id': source.get('source_run_id'),
        'files': [a['name'] for a in source.get('artifacts', [])],
        'evidence_manifest': 'source-evidence-manifest.json',
        'total_turns': len(source.get('turns', [])),
        'turns': [{**turn, 'response': turn.get('response', '')[-2000:]} for turn in source.get('turns', [])[-10:]],
        'context_note': 'Recent responses are excerpts. Full archived reports and supporting evidence are copied under source/. Original history is unchanged.',
        'final_response': source.get('output', '')[-6000:], 'plan': copy.deepcopy(source.get('plan')),
    }
    (directory / 'source-context.json').write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding='utf-8')
    return [a['name'] for a in source.get('artifacts', [])]
