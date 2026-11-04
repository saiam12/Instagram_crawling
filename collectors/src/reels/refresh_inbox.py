"""Closed-workbook inbox: durable enqueue before removing accepted input rows."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import hashlib
import io
import json
import math
import os
import re
from pathlib import Path
import tempfile
import threading
import time
from datetime import datetime, timezone
from xml.etree import ElementTree as ET
import zipfile

from src.exporters.instagram_collector import read_reel_records_from_xlsx
from .refresh_import import import_refresh_snapshots
from .process_lock import AtomicProcessLock
from .reel_history import LongReelStore
from .instagram_reels_browser import REEL_HISTORY_DIRECTORY, REEL_HISTORY_FILENAME
from .collection_log import append_collection_log


REFRESH_LOCAL_SETTINGS = Path(__file__).resolve().parents[2] / 'refresh.local.json'


def read_hourly_limit(path):
    try:
        settings = json.loads(path.read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return None
    if not isinstance(settings, dict):
        raise ValueError(f'{path.name}: settings must be a JSON object.')
    value = settings.get('max_collections_per_hour')
    if value is not None and (type(value) is not int or value < 1):
        raise ValueError(f'{path.name}: max_collections_per_hour must be a positive integer or null.')
    return value


@contextmanager
def closed_workbook(path):
    if path.with_name('~$' + path.name).exists():
        raise PermissionError('Input workbook is open in Excel')
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        import msvcrt
        create = ctypes.WinDLL('kernel32', use_last_error=True).CreateFileW
        create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        create.restype = wintypes.HANDLE
        # Deny reads/writes while accepting a batch; allow our atomic replacement.
        handle = create(str(path.resolve()), 0xC0000000, 4, None, 3, 0x80, None)
        if handle == ctypes.c_void_p(-1).value:
            raise PermissionError(ctypes.get_last_error(), 'Input workbook is busy', str(path))
        with os.fdopen(msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY), 'r+b') as file:
            yield file
    else:
        with path.open('r+b') as file:
            yield file


def rewrite_rows(content, sheet_path, keep):
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(content)) as source, zipfile.ZipFile(buffer, 'w') as target:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename == sheet_path:
                namespaces = list(ET.iterparse(io.BytesIO(data), events=('start-ns',)))
                for _, (prefix, uri) in namespaces:
                    try:
                        ET.register_namespace(prefix, uri)
                    except ValueError:
                        pass
                root = ET.fromstring(data)
                ns = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
                body = root.find(ns + 'sheetData')
                for row in list(body):
                    if not keep(int(row.attrib['r'])):
                        body.remove(row)
                data = ET.tostring(root, encoding='utf-8', xml_declaration=True)
                # Excel's mc:Ignorable may refer to otherwise unused namespace prefixes.
                for _, (prefix, uri) in namespaces:
                    if prefix and f'xmlns:{prefix}='.encode() not in data:
                        root.set('xmlns:' + prefix, uri)
                data = ET.tostring(root, encoding='utf-8', xml_declaration=True)
            target.writestr(info, data)
    return buffer.getvalue()


def atomic_bytes(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
    try:
        with os.fdopen(descriptor, 'wb') as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class RefreshInbox:
    def __init__(self, workbook, data_dir, *, start_row=None, end_row=None, limit=None, new_url_cost=None):
        self.workbook = Path(workbook).resolve()
        self.data_dir = Path(data_dir).resolve()
        if self.workbook.parent == self.data_dir and self.workbook.name in ('reels.xlsx', 'collection_data.xlsx', 'refresh_data.xlsx'):
            raise ValueError('Input and output must be separate: use an input workbook such as input.xlsx.')
        self.folder = self.data_dir / '.collector' / 'refresh_inbox'
        self.state_path = self.folder / 'queue.json'
        self.jobs = json.loads(self.state_path.read_text(encoding='utf-8')) if self.state_path.exists() else []
        self.start_row, self.end_row, self.limit = start_row, end_row, limit
        self.mutex = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None
        self.signature = None
        self.error = ''
        self.capacity_path = self.folder / 'capacity.json'
        self.capacity = json.loads(self.capacity_path.read_text(encoding='utf-8')) if self.capacity_path.exists() else {'samples': [], 'reservations': {}, 'new_url_cost': 1, 'workload_known': False}
        self.capacity['workload_known'] = False  # Rebuild active work before trusting a previous process's reservations.
        if new_url_cost is not None:
            self.capacity['new_url_cost'] = new_url_cost
        self.settings_path = REFRESH_LOCAL_SETTINGS
        self.hourly_limit = read_hourly_limit(self.settings_path)
        self.capacity['status'] = self.capacity_status()
        self.capacity_revision = 0
        self.lock = AtomicProcessLock(self.folder / 'watcher.lock.json')

    def cancelled_urls(self):
        with self.mutex:
            return {url for job in self.jobs if job.get('kind') == 'cancel' for url in job['urls']}

    def capacity_status(self, now=None):
        now = time.time() if now is None else now
        samples = [sample for sample in self.capacity['samples'] if now - 3600 <= sample['at'] <= now]
        seconds = sum(sample['seconds'] for sample in samples)
        completed = sum(sample['completed'] for sample in samples)
        measured = seconds >= 60 or completed >= 10
        estimated = math.floor(completed * 3600 / seconds * .8) if measured and seconds > 0 else None
        hourly = min(estimated, self.hourly_limit) if estimated is not None and self.hourly_limit is not None else estimated
        return {'measured': measured, 'hourly_capacity': hourly, 'measured_hourly_capacity': estimated,
                'max_collections_per_hour': self.hourly_limit, 'sample_seconds': seconds, 'completed': completed, 'startup_url_limit': 10}

    def record_batch(self, seconds, completed):
        if not math.isfinite(seconds) or seconds <= 0:
            return
        with self.mutex:
            now = time.time()
            self.capacity['samples'] = [sample for sample in self.capacity['samples'] if now - 3600 <= sample['at'] <= now]
            self.capacity['samples'].append({'at': now, 'seconds': seconds, 'completed': max(0, completed)})
            self.capacity_revision += 1
            self.capacity['status'] = self.capacity_status(now)
            atomic_bytes(self.capacity_path, json.dumps(self.capacity).encode('utf-8'))
            status = self.capacity['status']
        label = f"{status['hourly_capacity']} collections/hour" if status['measured'] else 'warming up; at most 10 pilot URLs'
        if self.hourly_limit is not None:
            label += f'; machine limit={self.hourly_limit}/hour'
        print(f"[REFRESH] CAPACITY_MEASURED: {label}.", flush=True)

    def update_workload(self, reservations, new_url_cost):
        with self.mutex:
            payload = (reservations, new_url_cost)
            acknowledged = any(job.get('capacity_waiting') and job['state'] == 'registered' for job in self.jobs)
            for job in self.jobs:
                if job['state'] == 'registered':
                    job['capacity_waiting'] = False
            if not acknowledged and self.capacity['workload_known'] and payload == (self.capacity['reservations'], self.capacity['new_url_cost']):
                return
            self.capacity.update(reservations=reservations, new_url_cost=new_url_cost, workload_known=True)
            self.capacity_revision += 1
            atomic_bytes(self.capacity_path, json.dumps(self.capacity).encode('utf-8'))

    def save(self):
        atomic_bytes(self.state_path, json.dumps(self.jobs, ensure_ascii=False).encode('utf-8'))

    def poll(self):
        hourly_limit = read_hourly_limit(self.settings_path)
        with self.mutex:
            if hourly_limit != self.hourly_limit:
                self.hourly_limit = hourly_limit
                self.capacity['status'] = self.capacity_status()
                self.capacity_revision += 1
                atomic_bytes(self.capacity_path, json.dumps(self.capacity).encode('utf-8'))
        stat = self.workbook.stat()
        signature = (stat.st_mtime_ns, stat.st_size, self.capacity_revision)
        if signature == self.signature:
            return
        with closed_workbook(self.workbook) as opened:
            content = opened.read()
            digest = hashlib.sha256(content).hexdigest()
            stage = self.folder / f'{digest}.xlsx'
            atomic_bytes(stage, content)  # Recovery copy precedes both queue registration and row removal.
            records = read_reel_records_from_xlsx(stage, start_row=self.start_row, end_row=self.end_row, include_row_numbers=True)
            stop_rows = read_reel_records_from_xlsx(stage, sheet_name='수집종료', include_row_numbers=True)
            cancellations = []
            for row in stop_rows:
                value = str(row.get('shortcode') or row.get('url') or '').strip()
                match = re.fullmatch(r'[A-Za-z0-9_-]+', value)
                if match:
                    code = value
                else:
                    match = re.fullmatch(r'https://(?:www\.)?instagram\.com/reels?/([A-Za-z0-9_-]+)/?(?:[?#].*)?', value)
                    code = match[1] if match else None
                if code:
                    cancellations.append((row, f'https://www.instagram.com/reels/{code}/'))
                elif value:
                    print(f'[REFRESH] Invalid shortcode retained in 수집종료: {value}', flush=True)
            with self.mutex:
                if cancellations and not any(job['id'] == digest + ':cancel' for job in self.jobs):
                    self.jobs.append({'id': digest + ':cancel', 'kind': 'cancel', 'state': 'cancelled',
                                      'urls': list(dict.fromkeys(url for _, url in cancellations)),
                                      'queued_at': datetime.now(timezone.utc).isoformat()})
                blocked = {url for job in self.jobs if job.get('kind') == 'cancel' for url in job['urls']}
                known = {url for job in self.jobs for url in job['urls'] if url not in blocked}
                pending = {url for job in self.jobs if job['state'] == 'queued' or (job['state'] == 'registered' and job.get('capacity_waiting')) for url in job['urls']}
                reservations = self.capacity['reservations']
                active = (set(reservations) | pending) if self.capacity['workload_known'] else known
                active -= blocked
                cost = max(1, self.capacity['new_url_cost'])
                reserved = sum(reservations.get(url, cost) for url in active) if self.capacity['workload_known'] else len(active) * cost
                capacity = self.capacity_status()
                deferred = 0
                accepted = []
                for row in records:
                    url = row['url']
                    if url in blocked:
                        continue
                    if url not in active:
                        fits = reserved + cost <= capacity['hourly_capacity'] if capacity['measured'] else len(active) < 10
                        if self.hourly_limit is not None:
                            fits = fits and reserved + cost <= self.hourly_limit
                        if not fits:
                            deferred += 1
                            continue
                        active.add(url)
                        reserved += cost
                    if url in known or self.limit is None or len(known) < self.limit:
                        accepted.append(row)
                        known.add(url)
                if not accepted and not cancellations:
                    stage.unlink(missing_ok=True)
                    self.signature = signature
                    if deferred:
                        print(f"[REFRESH] CAPACITY_FULL: {deferred} input rows retained; reserved {reserved}, hourly capacity {capacity['hourly_capacity']}, machine limit {self.hourly_limit}.", flush=True)
                    return
                urls = list(dict.fromkeys(row['url'] for row in accepted))
                if accepted and not any(job['id'] == digest for job in self.jobs):
                    self.jobs.append({'id': digest, 'urls': urls, 'stage': str(stage), 'state': 'queued', 'capacity_waiting': True,
                                      'start_row': self.start_row, 'end_row': self.end_row,
                                      'queued_at': datetime.now(timezone.utc).isoformat()})
                self.save()  # Fsync + atomic replace: only now may input rows be deleted.
                row_numbers = {int(row['_xlsx_row_number']) for row in accepted}
                cleared = rewrite_rows(content, accepted[0]['_xlsx_sheet_path'], lambda row: row not in row_numbers) if accepted else content
                if cancellations:
                    stop_numbers = {int(row['_xlsx_row_number']) for row, _ in cancellations}
                    cleared = rewrite_rows(cleared, cancellations[0][0]['_xlsx_sheet_path'], lambda row: row not in stop_numbers)
                # Do not clear a replacement saved by another application during enqueue.
                current = self.workbook.stat()
                if (current.st_mtime_ns, current.st_size) != signature[:2]:
                    return
                # Windows cannot replace our own open destination handle. Recheck
                # after releasing it; Excel reopening prevents replacement or creates its owner file.
                opened.close()
                if self.workbook.with_name('~$' + self.workbook.name).exists() or self.workbook.read_bytes() != content:
                    return
                atomic_bytes(self.workbook, cleared)
                self.signature = None
            if cancellations:
                print(f'[REFRESH] COLLECTION_CANCELLED: {len(cancellations)} shortcode requests; saved data retained.', flush=True)
                append_collection_log(self.data_dir, 'python', 'refresh_inbox_cancelled', urls=[url for _, url in cancellations], batch_id=digest)
            if deferred:
                print(f"[REFRESH] CAPACITY_FULL: {deferred} excess input rows retained; reserved {reserved}, hourly capacity {capacity['hourly_capacity']}, machine limit {self.hourly_limit}.", flush=True)
            print(f'[REFRESH] INPUT_CLOSED -> QUEUED: {len(urls)} Reels; accepted input rows removed.', flush=True)
            append_collection_log(self.data_dir, 'python', 'refresh_inbox_queued', input=str(self.workbook), count=len(urls), batch_id=digest)

    def drain(self):
        with self.mutex:
            blocked = {url for job in self.jobs if job.get('kind') == 'cancel' for url in job['urls']}
            for job in self.jobs:
                if job['state'] != 'queued':
                    continue
                active = set(job['urls']) - blocked
                if not active:
                    job['state'] = 'cancelled'
                    self.save()
                    continue
                source = Path(job['stage'])
                rows = read_reel_records_from_xlsx(source, include_row_numbers=True)
                selected = {int(row['_xlsx_row_number']) for row in rows if row['url'] in active}
                filtered = source.with_name(source.stem + '_accepted.xlsx')
                content = source.read_bytes()
                atomic_bytes(filtered, rewrite_rows(content, rows[0]['_xlsx_sheet_path'], lambda row: row < min(int(r['_xlsx_row_number']) for r in rows) or row in selected))
                import_refresh_snapshots(filtered, self.data_dir)
                async def publish():
                    store = await LongReelStore.create(self.data_dir / REEL_HISTORY_DIRECTORY / REEL_HISTORY_FILENAME, disable_recollect_cooldown=True)
                    await store.flush()
                    await store.export_outputs()
                asyncio.run(publish())
                job['state'] = 'registered'
                self.save()
            return list(dict.fromkeys(url for job in self.jobs if job['state'] == 'registered' for url in job['urls'] if url not in blocked))

    def start(self):
        if not self.lock.acquire():
            raise RuntimeError('Another refresh inbox watcher is running.')
        def watch():
            while not self.stop_event.is_set():
                try:
                    self.poll()
                    self.error = ''
                except (OSError, zipfile.BadZipFile, ET.ParseError, ValueError) as error:
                    message = str(error)
                    if message != self.error:
                        print(f'[REFRESH] Input retained; retry after close/save: {message}', flush=True)
                        self.error = message
                self.stop_event.wait(1)
        self.thread = threading.Thread(target=watch, name='refresh-inbox', daemon=True)
        self.thread.start()
        return self

    def close(self):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join()
        self.lock.release()
