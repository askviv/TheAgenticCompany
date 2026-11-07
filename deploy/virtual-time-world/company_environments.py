#!/usr/bin/env python3
"""Durable single-active-company application-data namespaces on one running VM."""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import fcntl
import hmac
import json
import logging
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
import traceback
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

LOG = logging.getLogger(__name__)
Name = Annotated[str, Field(min_length=1, max_length=160)]


class EnvironmentRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    operation: Literal['clone', 'switch']
    target_id: str | None = None
    name: Name | None = None
    idempotency_key: Annotated[str, Field(min_length=1, max_length=240)]


class CompanyEnvironmentManager:
    def __init__(self, controller):
        self.controller = controller
        self.root = controller.root / 'company-environments'
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.database = self.root / 'registry.sqlite3'
        self.changed, self.stopped = threading.Event(), threading.Event()
        self.worker = None
        self.worker_failure = None
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS companies (id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, key TEXT UNIQUE NOT NULL, request TEXT NOT NULL, payload TEXT NOT NULL)')
            if db.execute("SELECT 1 FROM settings WHERE key='active_id'").fetchone() is None:
                company = {'id': 'default', 'name': 'Current company', 'created_at': self._now(), 'snapshot': None}
                db.execute('INSERT INTO companies VALUES(?,?)', ('default', json.dumps(company)))
                db.execute("INSERT INTO settings VALUES('active_id','default')")
        self.database.chmod(0o600)

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat()

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def _namespace_lock(self):
        with (self.root / 'manager.lock').open('a') as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    @property
    def busy(self):
        with self._db() as db:
            return any(json.loads(row['payload'])['status'] in {'queued', 'running'}
                       and json.loads(row['payload']).get('acknowledged', True)
                       for row in db.execute('SELECT payload FROM jobs'))

    @staticmethod
    def _public(job):
        public = {key: value for key, value in job.items() if key not in {'status_token', 'plan', 'boot_allowed'}}
        if isinstance(public.get('error'), dict):
            public['error_code'] = public['error']['code']
            public['error'] = ('Company operation requires recovery; an operator can inspect its job ID.'
                               if job.get('status') == 'running' else
                               'Company operation failed; the previous company was restored.')
        return public

    def list(self):
        with self._db() as db:
            active = db.execute("SELECT value FROM settings WHERE key='active_id'").fetchone()['value']
            companies = [json.loads(row['payload']) for row in db.execute('SELECT payload FROM companies ORDER BY rowid')]
            jobs = [json.loads(row['payload']) for row in db.execute('SELECT payload FROM jobs ORDER BY rowid DESC')]
        result = {'active_id': active, 'companies': [
            {key: value for key, value in company.items() if key != 'snapshot'} | {'active': company['id'] == active}
            for company in companies]}
        operation = next((job for job in jobs if job['status'] in {'queued', 'running'}), None)
        if operation:
            # This list is controller-authenticated, unlike the scoped public
            # progress endpoint. Recover a receipt lost before dispatch starts.
            result['operation'] = self._public(operation) | {'status_token': operation['status_token']}
        if self.worker_failure:
            result['worker_error'] = self.worker_failure
        return result

    def _company(self, identifier):
        with self._db() as db:
            row = db.execute('SELECT payload FROM companies WHERE id=?', (identifier,)).fetchone()
        if row is None:
            raise KeyError(identifier)
        return json.loads(row['payload'])

    def _save_job(self, job):
        with self._db() as db:
            db.execute('UPDATE jobs SET payload=? WHERE id=?', (json.dumps(job), job['id']))

    def _phase(self, job, stage, *, boot_allowed=False):
        job.update(stage=stage, boot_allowed=boot_allowed)
        self._save_job(job)

    def get_job(self, identifier, status_token=None):
        UUID(identifier)
        with self._db() as db:
            row = db.execute('SELECT payload FROM jobs WHERE id=?', (identifier,)).fetchone()
        if row is None:
            raise KeyError(identifier)
        job = json.loads(row['payload'])
        if status_token is None or not hmac.compare_digest(job['status_token'].encode(), status_token.encode()):
            raise PermissionError('Company operation status token is invalid')
        return self._public(job)

    def submit(self, operation, target_id=None, name=None, idempotency_key=None):
        request = EnvironmentRequest(operation=operation, target_id=target_id, name=name,
                                     idempotency_key=idempotency_key)
        encoded = request.model_dump_json()
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT request,payload FROM jobs WHERE key=?', (request.idempotency_key,)).fetchone()
            if previous:
                if previous['request'] != encoded:
                    raise ValueError('Company operation idempotency key has different arguments')
                job = json.loads(previous['payload'])
            else:
                if any(json.loads(row['payload'])['status'] in {'queued', 'running'} for row in db.execute('SELECT payload FROM jobs')):
                    raise RuntimeError('A company operation is already active')
                source = db.execute("SELECT value FROM settings WHERE key='active_id'").fetchone()['value']
                if request.operation == 'switch':
                    if not request.target_id or not db.execute('SELECT 1 FROM companies WHERE id=?', (request.target_id,)).fetchone():
                        raise ValueError('Target company does not exist')
                    target = request.target_id
                    if target == source:
                        raise ValueError('Target company is already active')
                else:
                    target = str(uuid4())
                job = {'id': str(uuid4()), 'operation': operation, 'source_id': source, 'target_id': target,
                       'name': name, 'status': 'queued', 'stage': 'queued', 'created_at': self._now(),
                       'status_token': secrets.token_urlsafe(32), 'acknowledged': False,
                       'plan': {'completed': []}}
                db.execute('INSERT INTO jobs VALUES(?,?,?,?)', (job['id'], request.idempotency_key, encoded, json.dumps(job)))
        self.changed.set()
        return self._public(job) | {'status_token': job['status_token']}

    def start_job(self, identifier, status_token):
        self.get_job(identifier, status_token=status_token)
        with self._db() as db:
            stored = db.execute('SELECT payload FROM jobs WHERE id=?', (identifier,)).fetchone()
            job = json.loads(stored['payload'])
            if job['status'] == 'queued':
                job['acknowledged'] = True
                db.execute('UPDATE jobs SET payload=? WHERE id=?', (json.dumps(job), identifier))
        self.start()
        self.changed.set()
        return self._public(job)

    def rename(self, identifier, name):
        name = EnvironmentRequest(operation='switch', target_id=identifier, name=name, idempotency_key='rename').name
        with self._db() as db:
            row = db.execute('SELECT payload FROM companies WHERE id=?', (identifier,)).fetchone()
            if row is None:
                raise KeyError(identifier)
            company = json.loads(row['payload'])
            company['name'] = name
            db.execute('UPDATE companies SET payload=? WHERE id=?', (json.dumps(company), identifier))
        return self.list()

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        self.stopped.clear()
        self.worker_failure = None
        self.worker = threading.Thread(target=self._work, name='company-environments', daemon=True)
        self.worker.start()
        self.changed.set()

    def stop(self):
        self.stopped.set()
        self.changed.set()
        if self.worker:
            self.worker.join()

    def _work(self):
        while not self.stopped.is_set():
            self.changed.wait()
            self.changed.clear()
            job = None
            try:
                with self._namespace_lock():
                    with self._db() as db:
                        jobs = [json.loads(row['payload']) for row in db.execute('SELECT payload FROM jobs ORDER BY rowid')]
                    for job in jobs:
                        if self.stopped.is_set():
                            return
                        if job['status'] in {'queued', 'running'} and job.get('acknowledged', True):
                            self._execute(job)
            except Exception as error:
                self._log_failure('worker', error)
                self.worker_failure = {'code': 'company_worker_failed', 'exception_type': type(error).__name__}
                if job and job['status'] in {'queued', 'running'}:
                    if job.get('plan', {}).get('activation_started') or 'commit' in job.get('plan', {}).get('completed', []):
                        job.update(status='running', stage='finalizing')
                    elif job.get('stage') != 'queued':
                        job['plan']['recovering'] = True
                        job.update(status='running', stage='recovery_required')
                    else:
                        job.update(status='failed', finished_at=self._now())
                    job['error'] = {'code': 'company_worker_failed', 'exception_type': type(error).__name__}
                    try:
                        self._save_job(job)
                    except Exception as reporting_error:
                        self._log_failure('worker_status_persistence', reporting_error)
                # A broken registry cannot be made successful or retried in a hot loop.
                # Durable queued/running checkpoints are reconciled on process restart.
                return

    @staticmethod
    def _log_failure(stage, error):
        chain = []
        current = error
        seen = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            chain.append({'type': type(current).__name__, 'stack': [
                (frame.filename, frame.lineno, frame.name)
                for frame in traceback.extract_tb(current.__traceback__)]})
            current = current.__cause__ or current.__context__
        LOG.error('Company namespace failure stage=%s causes=%s', stage, chain)

    def _helper(self, command, company_id, job_id, *options):
        # The existing controller owns SSH authentication and safe argument quoting.
        output = self.controller.ssh('sudo', '/usr/local/lib/virtual-time-world/company-data',
                                     '--config', '/etc/company-workplace/company-environments.json',
                                     command, '--company-id', company_id, '--job-id', job_id, *options)
        return json.loads(output) if output else {}

    def _checkpoint(self, job, phase):
        if phase not in job['plan']['completed']:
            job['plan']['completed'].append(phase)
        job['stage'] = phase
        self._save_job(job)

    def _step(self, job, phase, action):
        if phase in job['plan']['completed']:
            return
        self._phase(job, phase)
        action()
        self._checkpoint(job, phase)

    @staticmethod
    def _backup(source_path, target_path, anchor=None):
        target_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = target_path.with_name('.' + target_path.name + '.' + str(uuid4()))
        source = sqlite3.connect(Path(source_path).resolve().as_uri() + '?mode=ro', uri=True, timeout=30)
        destination = sqlite3.connect(temporary, timeout=30)
        try:
            source.backup(destination)
            if anchor is not None:
                cursor = destination.execute('UPDATE clock_anchor SET guest_ns=?,host_ns=?,rate=? WHERE id=1', anchor)
                if cursor.rowcount != 1:
                    raise RuntimeError('Company clock has no initialized anchor')
                destination.commit()
            destination.close()
            destination = None
            temporary.chmod(0o600)
            os.replace(temporary, target_path)
            directory = os.open(target_path.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if destination is not None:
                destination.close()
            source.close()
            temporary.unlink(missing_ok=True)

    def _capture_data(self, job):
        result = self._helper('capture', job['source_id'], job['id'])
        job['plan']['source_generation'] = result['snapshot']
        job['plan']['source_snapshot']['generation'] = result['snapshot']
        self._save_job(job)

    def _duplicate_data(self, job):
        result = self._helper('duplicate', job['target_id'], job['id'], '--source-id', job['source_id'],
                              '--snapshot', job['plan']['source_generation'])
        job['plan']['target_generation'] = result['snapshot']
        self._save_job(job)

    def _capture_clock(self, job):
        path = self.root / 'clocks' / job['source_id'] / (job['id'] + '.sqlite3')
        if not path.exists():
            import libvirt
            connection = libvirt.open('qemu:///system')
            if connection is None:
                raise RuntimeError('System libvirt connection unavailable')
            try:
                domain = self.controller.domain(connection)
                if not domain.isActive():
                    raise RuntimeError('Company application-data operations require the existing VM running')
                epoch = self.controller.guest_ns(domain)
                rate = self.controller.running_rate(domain)
            finally:
                connection.close()
            self._backup(self.controller.db_path, path, (epoch, time.time_ns(), rate))
        # The clock checkpoint becomes immutable at atomic rename, even if the
        # worker stops before recording its next registry phase.
        with closing(sqlite3.connect(path)) as db:
            epoch, rate = db.execute('SELECT guest_ns,rate FROM clock_anchor WHERE id=1').fetchone()
        job['plan']['source_snapshot'] = {'generation': None, 'clock': str(path), 'guest_ns': epoch, 'rate': rate}
        self._save_job(job)

    def _restore_clock(self, snapshot):
        """Rebase a paused namespace, retaining the shared VM/kernel configuration."""
        import libvirt
        self.controller.cancel_issue_timers()
        # SQLite backup operates on connections; replacing the file beneath WAL
        # users would preserve stale pages. No database transaction spans SSH.
        source = sqlite3.connect(Path(snapshot['clock']).resolve().as_uri() + '?mode=ro', uri=True, timeout=30)
        destination = sqlite3.connect(self.controller.db_path, timeout=30)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        connection = libvirt.open('qemu:///system')
        if connection is None:
            raise RuntimeError('System libvirt connection unavailable')
        try:
            domain = self.controller.domain(connection)
            if not domain.isActive():
                raise RuntimeError('Company application-data switch requires the existing VM running')
            seconds, nseconds = divmod(snapshot['guest_ns'], 1_000_000_000)
            domain.setTime({'seconds': seconds, 'nseconds': nseconds}, 0)
            with self.controller.state() as db:
                db.execute('UPDATE clock_anchor SET guest_ns=?,host_ns=?,rate=? WHERE id=1',
                           (snapshot['guest_ns'], time.time_ns(), snapshot['rate']))
                db.commit()
            self.controller.set_rate(snapshot['rate'])
        finally:
            connection.close()

    def _publish_source(self, job):
        with self._db() as db:
            row = db.execute('SELECT payload FROM companies WHERE id=?', (job['source_id'],)).fetchone()
            source = json.loads(row['payload'])
            source['snapshot'] = job['plan']['source_snapshot']
            db.execute('UPDATE companies SET payload=? WHERE id=?', (json.dumps(source), source['id']))

    def _publish_target(self, job):
        with self._db() as db:
            if job['operation'] == 'clone':
                company = {'id': job['target_id'], 'name': job['name'] or 'Company copy',
                           'created_at': job['created_at'], 'snapshot': job['plan']['target_snapshot']}
                existing = db.execute('SELECT payload FROM companies WHERE id=?', (company['id'],)).fetchone()
                if existing is None:
                    db.execute('INSERT INTO companies VALUES(?,?)', (company['id'], json.dumps(company)))
            else:
                db.execute("UPDATE settings SET value=? WHERE key='active_id'", (job['target_id'],))

    def _execute(self, job):
        job.update(status='running')
        self._save_job(job)
        if job['plan'].get('recovering'):
            self._recover(job)
            return
        try:
            self._step(job, 'quiesce', lambda: self._helper('quiesce', job['source_id'], job['id']))
            # Clock monitor and API mutations use this same host lock. Hold it
            # across all guest restore and clock rebase transitions, but never
            # hold a SQLite transaction while waiting for the guest.
            with self.controller.operation_lock():
                self._step(job, 'capture_source_clock', lambda: self._capture_clock(job))
                self._step(job, 'capture_source_data', lambda: self._capture_data(job))
                self._step(job, 'publish_source', lambda: self._publish_source(job))
                if job['operation'] == 'clone':
                    self._step(job, 'duplicate_data', lambda: self._duplicate_data(job))
                    def duplicate_clock():
                        source = job['plan']['source_snapshot']
                        path = self.root / 'clocks' / job['target_id'] / (job['id'] + '.sqlite3')
                        if not path.exists():
                            self._backup(Path(source['clock']), path)
                        job['plan']['target_snapshot'] = {**source, 'clock': str(path), 'generation': job['plan']['target_generation']}
                        self._save_job(job)
                    self._step(job, 'duplicate_clock', duplicate_clock)
                    self._step(job, 'restore_source_clock', lambda: self._restore_clock(job['plan']['source_snapshot']))
                else:
                    if 'target_snapshot' not in job['plan']:
                        snapshot = self._company(job['target_id'])['snapshot']
                        if snapshot is None:
                            raise RuntimeError('Inactive company has no captured application data')
                        job['plan']['target_snapshot'] = snapshot
                        self._save_job(job)
                    self._step(job, 'restore_target_data', lambda: self._restore_data(job, job['target_id'], job['plan']['target_snapshot']))
                    self._step(job, 'restore_target_clock', lambda: self._restore_clock(job['plan']['target_snapshot']))
                self._step(job, 'publish_target', lambda: self._publish_target(job))
            active = job['source_id'] if job['operation'] == 'clone' else job['target_id']
            # Once services may accept work, the installed target is authoritative.
            # A failed start/finalization must resume forward, never restore an older
            # snapshot over writes accepted by the newly activated applications.
            if not job['plan'].get('activation_started'):
                job['plan']['activation_started'] = True
                self._save_job(job)
            self._step(job, 'start', lambda: self._helper('start', active, job['id']))
            self._step(job, 'commit', lambda: self._helper('commit', active, job['id']))
            job.update(status='completed', stage='completed', finished_at=self._now())
            self._save_job(job)
            self.controller.reconcile_issue_deadlines()
        except Exception as error:
            self._log_failure(job['stage'], error)
            if job['plan'].get('activation_started') or 'commit' in job['plan']['completed']:
                job.update(status='running', stage='finalizing', error={
                    'code': 'company_finalization_pending', 'exception_type': type(error).__name__})
                self._save_job(job)
                return
            job['plan']['recovering'] = True
            job['error'] = {'code': 'company_operation_failed', 'exception_type': type(error).__name__}
            self._save_job(job)
            self._recover(job)

    def _recover(self, job):
        try:
            self._rollback(job)
        except Exception as recovery_error:
            self._log_failure('rollback', recovery_error)
            # Preserve the active-operation interlock until data and clock are
            # coherent. A process restart resumes this durable recovery phase.
            job.update(status='running', stage='recovery_required', recovery_error={
                'code': 'company_recovery_failed', 'exception_type': type(recovery_error).__name__})
            self._save_job(job)
            return
        job.pop('recovery_error', None)
        job.update(status='failed', stage='rolled_back', finished_at=self._now())
        self._save_job(job)
        self.controller.reconcile_issue_deadlines()

    def _restore_data(self, job, company_id, snapshot):
        return self._helper('restore', company_id, job['id'], '--snapshot', snapshot['generation'])

    def _rollback(self, job):
        self._phase(job, 'rollback')
        self._helper('quiesce', job['source_id'], job['id'])
        with self.controller.operation_lock():
            self._helper('rollback', job['source_id'], job['id'])
            snapshot = job['plan'].get('source_snapshot')
            if snapshot:
                self._restore_clock(snapshot)
            with self._db() as db:
                db.execute("UPDATE settings SET value=? WHERE key='active_id'", (job['source_id'],))
                if job['operation'] == 'clone':
                    db.execute('DELETE FROM companies WHERE id=?', (job['target_id'],))
        self._helper('start', job['source_id'], job['id'])
