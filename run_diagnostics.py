"""Bounded, local crash diagnostics; no subprocesses or model calls."""
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


class RunDiagnostics:
    def __init__(self, directory, max_bytes=8 * 1024 * 1024):
        self.directory = Path(directory)
        self.max_bytes = max_bytes
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None

    def write(self, kind, **data):
        record = {'utc': datetime.now(timezone.utc).isoformat(),
                  'monotonic_seconds': time.monotonic(), 'kind': kind, **data}
        payload = (json.dumps(record, ensure_ascii=True) + '\n').encode('utf-8')
        with self.lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self.directory / 'activity.jsonl'
            if path.exists() and path.stat().st_size + len(payload) > self.max_bytes:
                for index in (2, 1):
                    source = self.directory / f'activity.{index}.jsonl'
                    if source.exists():
                        source.replace(self.directory / f'activity.{index + 1}.jsonl')
                path.replace(self.directory / 'activity.1.jsonl')
            with path.open('ab') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())

    def start(self, turn, interval=5):
        self.stop_event = threading.Event()
        stop = self.stop_event

        def sample():
            try:
                import psutil
                psutil.cpu_percent()  # Prime the interval measurement.
                while not stop.wait(interval):
                    parent = psutil.Process(os.getpid())
                    processes = []
                    for process in [parent, *parent.children(recursive=True)]:
                        try:
                            with process.oneshot():
                                cpu = process.cpu_times()
                                processes.append(dict(pid=process.pid, ppid=process.ppid(),
                                    name=process.name(), created=process.create_time(),
                                    rss_bytes=process.memory_info().rss,
                                    cpu_user_seconds=cpu.user, cpu_system_seconds=cpu.system,
                                    threads=process.num_threads()))
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            continue
                    memory = psutil.virtual_memory()
                    self.write('resources', turn=turn, system_cpu_percent=psutil.cpu_percent(),
                               available_memory_bytes=memory.available,
                               memory_percent=memory.percent, swap_percent=psutil.swap_memory().percent,
                               processes=processes)
            except Exception as exc:
                try:
                    self.write('sampling_error', turn=turn, error=str(exc))
                except OSError:
                    pass

        self.thread = threading.Thread(target=sample, daemon=True, name='workbench-diagnostics')
        self.thread.start()

    def stop(self):
        self.stop_event.set()
