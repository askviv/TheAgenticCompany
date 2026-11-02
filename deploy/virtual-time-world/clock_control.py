#!/usr/bin/env python3
"""Control the isolated VM's candidate guest clock on its libvirt host."""
import argparse
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
import xml.etree.ElementTree as ET

import libvirt

DEFAULT_ROOT = Path('/data/theagentcompany/virtual-time-world')
DEFAULT_NAME = 'tac-virtual-time-world'
DEFAULT_PORT = 22220
CONNECTION = 'qemu:///system'
_event_impl_registered = False
LOG = logging.getLogger('virtual_time_world.clock_control')


def register_event_impl():
    global _event_impl_registered
    if not _event_impl_registered:
        libvirt.virEventRegisterDefaultImpl()
        _event_impl_registered = True


class Controller:
    def __init__(self, root, name, port):
        self.root, self.name, self.port = root, name, port
        self.config, self.images = root / 'config', root / 'images'
        self.db_path = self.config / 'clock-state.sqlite3'
        self.ssh_options = ['-i', str(self.config / 'ssh_ed25519'), '-o', 'BatchMode=yes',
                            '-o', f'UserKnownHostsFile={self.config / "known_hosts"}']

    @contextmanager
    def state(self):
        self.config.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(self.db_path, timeout=30)
        os.chmod(self.db_path, 0o600)
        try:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('''CREATE TABLE IF NOT EXISTS clock_anchor (
                id INTEGER PRIMARY KEY CHECK (id=1), guest_ns INTEGER NOT NULL,
                host_ns INTEGER NOT NULL, rate INTEGER NOT NULL CHECK(rate > 0))''')
            db.execute('''CREATE TABLE IF NOT EXISTS candidate (
                id INTEGER PRIMARY KEY CHECK (id=1), release TEXT NOT NULL,
                root_uuid TEXT NOT NULL, manifest TEXT NOT NULL,
                kernel_sha256 TEXT NOT NULL, initrd_sha256 TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS rate_change (
                id INTEGER PRIMARY KEY CHECK (id=1), requested_rate INTEGER NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS shutdown_intent (
                id INTEGER PRIMARY KEY CHECK (id=1), requested INTEGER NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS boot_pending (
                id INTEGER PRIMARY KEY CHECK (id=1), desired_rate INTEGER NOT NULL,
                requested_ns INTEGER NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS company_target_pending (
                id INTEGER PRIMARY KEY CHECK (id=1))''')
            db.execute('''CREATE TABLE IF NOT EXISTS clock_api_requests (
                idempotency_key TEXT PRIMARY KEY, requested_speed INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('processing','complete','uncertain')),
                response_json TEXT, created_ns INTEGER NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS issue_deadlines (
                issue_key TEXT PRIMARY KEY, deadline_at TEXT NOT NULL, created_ns INTEGER NOT NULL)''')
            db.commit()
            yield db
        finally:
            db.close()

    @contextmanager
    def operation_lock(self):
        self.config.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = os.open(self.config / 'clock-control.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            os.close(lock)

    def ssh(self, *command):
        return subprocess.check_output(
            ['ssh', *self.ssh_options, '-p', str(self.port), 'ubuntu@127.0.0.1',
             shlex.join(command)], text=True, stderr=subprocess.PIPE).strip()

    def register_issue_deadline(self, issue_key, deadline_at):
        import re
        if not re.fullmatch(r'[0-9a-f]{64}', issue_key):
            raise ValueError('invalid issue timer identity')
        with self.state() as db:
            existing = db.execute('SELECT deadline_at FROM issue_deadlines WHERE issue_key=?',
                                   (issue_key,)).fetchone()
            if existing and existing[0] != deadline_at:
                raise RuntimeError('registered issue deadline is immutable')
            db.execute('INSERT OR IGNORE INTO issue_deadlines(issue_key,deadline_at,created_ns) VALUES(?,?,?)',
                       (issue_key, deadline_at, time.time_ns()))
            db.commit()
        self.reconcile_issue_deadlines()

    def reconcile_issue_deadlines(self):
        with self.state() as db:
            deadlines = db.execute('SELECT issue_key,deadline_at FROM issue_deadlines').fetchall()
        if not deadlines:
            return
        source_root_value = os.environ.get('VIRTUAL_TIME_SOURCE_ROOT')
        if not source_root_value:
            raise RuntimeError('VIRTUAL_TIME_SOURCE_ROOT is required for host issue deadline reconciliation')
        source_root = Path(source_root_value)
        if not (source_root / 'servers/simulation/issue_handoffs.py').is_file():
            raise FileNotFoundError('host issue handoff source is unavailable for timer reconciliation')
        from servers.simulation.issue_handoffs import SystemdIssueScheduler
        scheduler = SystemdIssueScheduler(
            Path('/data/theagentcompany/enterprise/config/runtime.json'), executable=sys.executable,
            source_root=source_root, state_root=self.config,
            command_factory=lambda key: [sys.executable, str(Path(__file__).resolve()),
                '--root', str(self.root), '--name', self.name, '--ssh-port', str(self.port),
                'process-issue', '--issue-key', key],
        )
        for issue_key, deadline_at in deadlines:
            scheduler.schedule(issue_key, datetime.fromisoformat(deadline_at))

    def process_issue(self, issue_key):
        import re
        if not re.fullmatch(r'[0-9a-f]{64}', issue_key):
            raise ValueError('invalid issue timer identity')
        python = '/data/theagentcompany/enterprise/venv/bin/python'
        config = '/data/theagentcompany/enterprise/config/runtime.json'
        guest_source = os.environ.get('VIRTUAL_TIME_GUEST_SOURCE_ROOT',
                                      '/data/theagentcompany/enterprise/source')
        invocation = shlex.join([python, '-m', 'servers.simulation.issue_handoff_runtime',
                                 '--config', config, '--issue-key', issue_key])
        script = f'cd {shlex.quote(guest_source)} && exec {invocation}'
        output = self.ssh('sudo', '-n', 'sh', '-c', script)
        try:
            receipt = json.loads(output)
        except json.JSONDecodeError as error:
            raise RuntimeError('guest issue runtime returned no valid status receipt') from error
        status = receipt.get('status')
        if receipt.get('issue_key') != issue_key or status not in {
                'waiting', 'read', 'submitting', 'uncertain', 'sent'}:
            raise RuntimeError('guest issue runtime returned an invalid status receipt')
        if status in {'read', 'submitting', 'uncertain', 'sent'}:
            with self.state() as db:
                db.execute('DELETE FROM issue_deadlines WHERE issue_key=?', (issue_key,))
                db.commit()

    def domain(self, connection):
        return connection.lookupByName(self.name)

    @staticmethod
    def guest_ns(domain):
        timestamp = domain.getTime(0)
        return timestamp['seconds'] * 1_000_000_000 + timestamp['nseconds']

    def running_rate(self, domain, db=None):
        rate = int(self.ssh('cat', '/sys/kernel/virtual_time_rate'))
        if rate < 1:
            raise RuntimeError('guest runtime-rate interface returned an invalid rate')
        return rate

    def row(self, db):
        return db.execute('SELECT guest_ns, host_ns, rate FROM clock_anchor WHERE id=1').fetchone()

    def effective(self, row, host_ns):
        guest_ns, anchor_host_ns, rate = row
        if host_ns < anchor_host_ns:
            raise RuntimeError('host realtime moved backwards; refusing to rewind guest time')
        return guest_ns + ((host_ns - anchor_host_ns) * rate)

    def save_anchor(self, db, guest_ns, host_ns, rate):
        previous = self.row(db)
        if previous and host_ns < previous[1]:
            raise RuntimeError('host realtime moved backwards; refusing to replace the durable anchor')
        if previous and guest_ns < previous[0]:
            raise RuntimeError('guest realtime moved backwards; refusing to replace the durable anchor')
        written = db.execute('''INSERT INTO clock_anchor(id, guest_ns, host_ns, rate) VALUES(1,?,?,?)
            ON CONFLICT(id) DO UPDATE SET guest_ns=excluded.guest_ns,
            host_ns=excluded.host_ns, rate=excluded.rate
            WHERE excluded.guest_ns >= clock_anchor.guest_ns
              AND excluded.host_ns >= clock_anchor.host_ns''', (guest_ns, host_ns, rate))
        if written.rowcount != 1:
            raise RuntimeError('clock checkpoint moved backwards; previous durable anchor was preserved')
        db.commit()

    def prepare_candidate(self):
        connection = libvirt.open(CONNECTION)
        if connection is None:
            raise RuntimeError('system libvirt connection unavailable')
        try:
            domain = self.domain(connection)
            if not domain.isActive():
                raise RuntimeError('prepare requires the guest to be running')
        finally:
            connection.close()
        release = self.ssh('sudo', 'cat', '/opt/agentic-world-build/candidate-release')
        manifest_text = self.ssh('sudo', 'cat', '/opt/agentic-world-build/candidate.json')
        manifest = json.loads(manifest_text)
        required = {'kernel_release': release, 'source_version': '6.12.115',
                    'features': ['runtime_rate', 'boot_epoch'], 'rtc_hctosys': False}
        if any(manifest.get(key) != value for key, value in required.items()):
            raise RuntimeError('candidate manifest does not declare the required clock features')
        digest = manifest.get('patch_digest')
        if (not isinstance(digest, str) or len(digest) != 64 or
                any(char not in '0123456789abcdef' for char in digest.lower())):
            raise RuntimeError('candidate manifest patch_digest must be a SHA-256 hex digest')
        kernel_config = self.ssh('cat', f'/boot/config-{release}')
        if '# CONFIG_RTC_HCTOSYS is not set' not in kernel_config:
            raise RuntimeError('candidate kernel must have CONFIG_RTC_HCTOSYS disabled')
        root_uuid = self.ssh('findmnt', '-n', '-o', 'UUID', '/')
        if not root_uuid:
            raise RuntimeError('guest root filesystem UUID is unavailable')
        for source, target in ((f'/boot/vmlinuz-{release}', 'candidate-vmlinuz'),
                               (f'/boot/initrd.img-{release}', 'candidate-initrd')):
            # Ubuntu initramfs files may be root-readable only. Stream through
            # the provisioned guest sudo account without loosening guest modes.
            with tempfile.NamedTemporaryFile(dir=self.images, prefix='.candidate-',
                                             delete=False) as output:
                temporary = Path(output.name)
                subprocess.run(
                    ['ssh', *self.ssh_options, '-p', str(self.port), 'ubuntu@127.0.0.1',
                     shlex.join(['sudo', 'cat', source])], stdout=output, check=True)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, 0o644)
            os.replace(temporary, self.images / target)
        kernel_digest = hashlib.sha256((self.images / 'candidate-vmlinuz').read_bytes()).hexdigest()
        initrd_digest = hashlib.sha256((self.images / 'candidate-initrd').read_bytes()).hexdigest()
        with self.state() as db:
            db.execute('''INSERT INTO candidate(id, release, root_uuid, manifest,
                kernel_sha256, initrd_sha256) VALUES(1,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET release=excluded.release,
                root_uuid=excluded.root_uuid, manifest=excluded.manifest,
                kernel_sha256=excluded.kernel_sha256, initrd_sha256=excluded.initrd_sha256''',
                       (release, root_uuid, manifest_text, kernel_digest, initrd_digest))
            db.commit()
        print(f'prepared candidate {release} and saved host boot artifacts')

    def candidate_xml(self, domain, release, root_uuid, epoch_ns, host_anchor_ns):
        original = domain.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
        backup = self.config / 'domain-before-candidate.xml'
        if not backup.exists():
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as stream:
                stream.write(original)
        tree = ET.fromstring(original)
        operating_system = tree.find('os')
        if operating_system is None:
            raise RuntimeError('libvirt domain has no operating system configuration')
        sec, nsec = divmod(epoch_ns, 1_000_000_000)
        host_sec, host_nsec = divmod(host_anchor_ns, 1_000_000_000)
        for tag, value in (
            ('kernel', str(self.images / 'candidate-vmlinuz')),
            ('initrd', str(self.images / 'candidate-initrd')),
            ('cmdline', f'root=UUID={root_uuid} ro console=ttyS0 virtual_time=1 '
                        f'virtual_time_epoch_sec={sec} virtual_time_epoch_nsec={nsec} '
                        f'virtual_time_host_epoch_sec={host_sec} '
                        f'virtual_time_host_epoch_nsec={host_nsec}'),
        ):
            element = operating_system.find(tag)
            if element is None:
                element = ET.SubElement(operating_system, tag)
            element.text = value
        reboot = tree.find('on_reboot')
        if reboot is None:
            reboot = ET.SubElement(tree, 'on_reboot')
        reboot.text = 'destroy'
        return ET.tostring(tree, encoding='unicode')

    def status(self):
        register_event_impl()
        connection = libvirt.open(CONNECTION)
        if connection is None:
            raise RuntimeError('system libvirt connection unavailable')
        try:
            domain = self.domain(connection)
            state = domain.info()[0]
            state_name = {libvirt.VIR_DOMAIN_RUNNING: 'running',
                          libvirt.VIR_DOMAIN_BLOCKED: 'blocked',
                          libvirt.VIR_DOMAIN_PAUSED: 'paused',
                          libvirt.VIR_DOMAIN_SHUTDOWN: 'shutting down',
                          libvirt.VIR_DOMAIN_SHUTOFF: 'shut off',
                          libvirt.VIR_DOMAIN_CRASHED: 'crashed'}.get(state, str(state))
            print(f'domain: {domain.name()} ({state_name})')
            if domain.isActive():
                print(f'guest_epoch_ns: {self.guest_ns(domain)} (libvirt guest-agent time)')
            with self.state() as db:
                row = self.row(db)
                pending = db.execute(
                    'SELECT requested_rate FROM rate_change WHERE id=1').fetchone()
                bootstrap = db.execute(
                    'SELECT desired_rate FROM boot_pending WHERE id=1').fetchone()
                if row:
                    rate = row[2]
                    print(f'persisted_rate: {rate}x')
                    if not domain.isActive() and not pending:
                        epoch = self.effective(row, time.time_ns())
                        print(f'projected_start_epoch_ns: {epoch}')
                else:
                    print('clock anchor: not initialized')
                if pending:
                    print(f'rate_change_pending: requested {pending[0]}x; actual rate needs reconciliation')
                if bootstrap:
                    print(f'clock_bootstrap_pending: guest starts at 1x; synchronize to {bootstrap[0]}x before company target')
        finally:
            connection.close()

    def clock_snapshot(self):
        """Read the active guest clock and require a durable, reconciled anchor."""
        register_event_impl()
        connection = libvirt.open(CONNECTION)
        if connection is None:
            raise RuntimeError('system libvirt connection unavailable')
        try:
            domain = self.domain(connection)
            if not domain.isActive():
                raise RuntimeError('virtual-time guest is not active')
            with self.state() as db:
                anchor = self.row(db)
                if anchor is None:
                    raise RuntimeError('clock anchor is uninitialized')
                if db.execute('SELECT 1 FROM rate_change WHERE id=1').fetchone():
                    raise RuntimeError('clock rate change is unresolved')
                if db.execute('SELECT 1 FROM boot_pending WHERE id=1').fetchone():
                    raise RuntimeError('guest clock bootstrap is not synchronized yet')
                epoch_ns = self.guest_ns(domain)
                if epoch_ns < anchor[0]:
                    raise RuntimeError('active guest clock is behind its durable anchor')
                # Host control owns rate writes and records their confirmed
                # outcome. An unresolved write is rejected above; status reads
                # need no new SSH session into the guest.
                speed = anchor[2]
                return {'speed': speed, 'epoch_ns': epoch_ns, 'mode': 'guest-kernel'}
        finally:
            connection.close()

    def start(self, requested_rate=None):
        register_event_impl()
        connection = libvirt.open(CONNECTION)
        if connection is None:
            raise RuntimeError('system libvirt connection unavailable')
        try:
            domain = self.domain(connection)
            if domain.isActive():
                raise RuntimeError('VM is already running')
            with self.state() as db:
                candidate = db.execute(
                    'SELECT release, root_uuid, kernel_sha256, initrd_sha256 '
                    'FROM candidate WHERE id=1').fetchone()
                if not candidate or not all((self.images / name).is_file() for name in
                                            ('candidate-vmlinuz', 'candidate-initrd')):
                    raise RuntimeError('candidate is not prepared; build it, run prepare, then stop')
                release, root_uuid, kernel_hash, initrd_hash = candidate
                for filename, expected in (('candidate-vmlinuz', kernel_hash),
                                           ('candidate-initrd', initrd_hash)):
                    actual = hashlib.sha256((self.images / filename).read_bytes()).hexdigest()
                    if actual != expected:
                        raise RuntimeError(f'prepared candidate artifact changed: {filename}')
                row = self.row(db)
                if row is None:
                    raise RuntimeError('clock anchor is uninitialized; run initialize with the current company epoch')
                pending = db.execute(
                    'SELECT requested_rate FROM rate_change WHERE id=1').fetchone()
                if pending:
                    raise RuntimeError('a rate change is unresolved; run resolve-pending-rate with operator evidence before starting')
                boot_pending = db.execute('SELECT desired_rate FROM boot_pending WHERE id=1').fetchone()
                if boot_pending and requested_rate is None:
                    requested_rate = boot_pending[0]
                rate = requested_rate or row[2]
                now = time.time_ns()
                epoch_ns = self.effective(row, now)
                domain = connection.defineXML(self.candidate_xml(domain, release, root_uuid,
                                                                   epoch_ns, now))
                if domain is None:
                    raise RuntimeError('libvirt did not retain candidate domain configuration')
                # Commit an anchor before returning; a crash during boot still advances
                # from this epoch and can never start earlier on the next invocation.
                self.save_anchor(db, epoch_ns, now, rate)
                db.execute('DELETE FROM rate_change WHERE id=1')
                db.execute('''INSERT INTO boot_pending(id, desired_rate, requested_ns)
                    VALUES(1, ?, ?) ON CONFLICT(id) DO UPDATE SET
                    desired_rate=excluded.desired_rate, requested_ns=excluded.requested_ns''',
                           (rate, now))
                db.execute('INSERT OR IGNORE INTO company_target_pending(id) VALUES(1)')
                db.execute('''INSERT INTO shutdown_intent(id, requested) VALUES(1,0)
                    ON CONFLICT(id) DO UPDATE SET requested=0''')
                db.commit()
                domain.create()
            print(f'started candidate {release} at {rate}x, epoch {epoch_ns}')
        finally:
            connection.close()

    def stop(self):
        register_event_impl()
        connection = libvirt.open(CONNECTION)
        if connection is None:
            raise RuntimeError('system libvirt connection unavailable')
        try:
            domain = self.domain(connection)
            if not domain.isActive():
                print('VM already stopped')
                return
            with self.state() as db:
                row = self.row(db)
                db.execute('''INSERT INTO shutdown_intent(id, requested) VALUES(1,1)
                    ON CONFLICT(id) DO UPDATE SET requested=1''')
                db.commit()
            lifecycle_state = {'active': True, 'stop_detail': None}
            shutdown_accepted = False
            def lifecycle(_conn, _event_domain, _event, _detail, _opaque):
                if _event == libvirt.VIR_DOMAIN_EVENT_STOPPED:
                    lifecycle_state['active'] = False
                    lifecycle_state['stop_detail'] = _detail
            callback = connection.domainEventRegisterAny(
                domain, libvirt.VIR_DOMAIN_EVENT_ID_LIFECYCLE, lifecycle, None)
            # Recheck once after callback registration to cover a stop racing with
            # subscription, then rely on lifecycle events while the event loop runs.
            if not domain.isActive():
                lifecycle_state['active'] = False
            failure = None
            rate = row[2] if row else None
            rate_known = row is not None
            pending_rate = False
            bootstrap_pending = False
            if row:
                with self.state() as db:
                    bootstrap_pending = bool(db.execute(
                        'SELECT 1 FROM boot_pending WHERE id=1').fetchone())
                    pending_rate = bool(db.execute(
                        'SELECT 1 FROM rate_change WHERE id=1').fetchone())
                    if pending_rate:
                        try:
                            rate = self.running_rate(domain, db)
                        except Exception as error:
                            failure = error
                            rate_known = False
                    if rate_known and not bootstrap_pending:
                        try:
                            guest_now = self.guest_ns(domain)
                            host_now = time.time_ns()
                            self.save_anchor(db, guest_now, host_now, rate)
                            if pending_rate:
                                db.execute('DELETE FROM rate_change WHERE id=1')
                                db.commit()
                        except libvirt.libvirtError as error:
                            expected = {getattr(libvirt, 'VIR_ERR_AGENT_UNRESPONSIVE', -1),
                                        getattr(libvirt, 'VIR_ERR_AGENT_COMMAND_TIMEOUT', -2)}
                            if pending_rate:
                                rate_known = False
                                failure = RuntimeError(
                                    'pending rate cannot be reconciled without guest-agent time')
                                failure.__cause__ = error
                            elif error.get_error_code() not in expected:
                                failure = error
                        except Exception as error:
                            failure = error
                            if pending_rate:
                                rate_known = False
                    elif rate_known and bootstrap_pending:
                        try:
                            host_now = time.time_ns()
                            self.save_anchor(db, self.effective(row, host_now), host_now, rate)
                        except Exception as error:
                            failure = error
                            rate_known = False
            def clear_shutdown_intent_if_active():
                if domain.isActive():
                    with self.state() as db:
                        db.execute('DELETE FROM shutdown_intent WHERE id=1')
                        db.commit()
            try:
                try:
                    if lifecycle_state['active']:
                        domain.shutdownFlags(libvirt.VIR_DOMAIN_SHUTDOWN_GUEST_AGENT |
                                             libvirt.VIR_DOMAIN_SHUTDOWN_ACPI_POWER_BTN)
                        shutdown_accepted = True
                except Exception as shutdown_error:
                    clear_shutdown_intent_if_active()
                    if failure is not None:
                        raise shutdown_error from failure
                    raise
                try:
                    while lifecycle_state['active']:
                        libvirt.virEventRunDefaultImpl()
                except Exception as shutdown_wait_error:
                    clear_shutdown_intent_if_active()
                    if failure is not None:
                        raise shutdown_wait_error from failure
                    raise
                if row and rate_known:
                    try:
                        with self.state() as db:
                            row = self.row(db)
                            unresolved = db.execute(
                                'SELECT 1 FROM rate_change WHERE id=1').fetchone()
                            if not unresolved:
                                host_now = time.time_ns()
                                self.save_anchor(db, self.effective(row, host_now), host_now, rate)
                    except Exception as projection_error:
                        if failure is not None:
                            raise projection_error from failure
                        raise
                if not shutdown_accepted:
                    raise RuntimeError('VM stopped before the graceful shutdown request was accepted')
                if lifecycle_state['stop_detail'] != libvirt.VIR_DOMAIN_EVENT_STOPPED_SHUTDOWN:
                    raise RuntimeError('VM did not report a graceful shutdown lifecycle event')
                if failure is not None:
                    raise RuntimeError('guest clock checkpoint failed; graceful shutdown completed') from failure
            finally:
                connection.domainEventDeregisterAny(callback)
            print('VM shut down gracefully; persisted clock anchor advanced' if row else
                  'VM shut down gracefully; no clock anchor existed to advance')
        finally:
            connection.close()

    def prepare(self):
        self.prepare_candidate()

    def set_rate(self, rate):
        if rate < 1:
            raise ValueError('rate must be a positive integer')
        register_event_impl()
        connection = libvirt.open(CONNECTION)
        if connection is None:
            raise RuntimeError('system libvirt connection unavailable')
        try:
            domain = self.domain(connection)
            if not domain.isActive():
                with self.state() as db:
                    row = self.row(db)
                    if row is None:
                        raise RuntimeError('clock anchor is uninitialized; run initialize before changing the offline rate')
                    if db.execute('SELECT 1 FROM rate_change WHERE id=1').fetchone():
                        raise RuntimeError('a rate change is unresolved; reconcile it before changing the offline rate')
                    now = time.time_ns()
                    epoch = self.effective(row, now)
                    self.save_anchor(db, epoch, now, rate)
                    if db.execute('SELECT 1 FROM boot_pending WHERE id=1').fetchone():
                        db.execute('UPDATE boot_pending SET desired_rate=? WHERE id=1', (rate,))
                    db.execute('DELETE FROM rate_change WHERE id=1')
                    db.commit()
                print(f'persisted rate {rate}x for next start')
                return
            with self.state() as db:
                if db.execute('SELECT 1 FROM boot_pending WHERE id=1').fetchone():
                    raise RuntimeError('runtime rate changes wait until guest clock bootstrap is synchronized')
                old_rate = self.running_rate(domain, db)
                if self.row(db) is None:
                    raise RuntimeError('clock anchor is uninitialized; run initialize before changing the active rate')
                before_write = self.guest_ns(domain)
                now = time.time_ns()
                self.save_anchor(db, before_write, now, old_rate)
                db.execute('''INSERT INTO rate_change(id, requested_rate) VALUES(1,?)
                    ON CONFLICT(id) DO UPDATE SET requested_rate=excluded.requested_rate''', (rate,))
                db.commit()
            self.ssh('sudo', 'sh', '-c',
                     f"printf '%s\\n' {rate} > /sys/kernel/virtual_time_rate")
            actual = int(self.ssh('cat', '/sys/kernel/virtual_time_rate'))
            if actual != rate:
                raise RuntimeError(f'guest accepted unexpected virtual clock rate: {actual}')
            current_guest = self.guest_ns(domain)
            with self.state() as db:
                self.save_anchor(db, current_guest, time.time_ns(), rate)
                db.execute('DELETE FROM rate_change WHERE id=1')
                db.commit()
            print(f'guest clock rate set to {rate}x')
        finally:
            connection.close()

    def synchronize_boot_clock(self, domain):
        """Reconcile the slow bootstrap clock to its durable world-time projection."""
        with self.state() as db:
            pending = db.execute(
                'SELECT desired_rate FROM boot_pending WHERE id=1').fetchone()
            if pending is None:
                return
            desired_rate = pending[0]
            row = self.row(db)
            if row is None:
                raise RuntimeError('clock anchor disappeared during guest bootstrap')
            if row[2] != desired_rate:
                raise RuntimeError('bootstrap rate differs from the durable world-clock rate')
        actual_rate = self.running_rate(domain)
        if actual_rate != desired_rate:
            self.ssh('sudo', 'sh', '-c',
                     f"printf '%s\\n' {desired_rate} > /sys/kernel/virtual_time_rate")
            actual_rate = self.running_rate(domain)
            if actual_rate != desired_rate:
                raise RuntimeError('guest runtime rate did not reach the persisted bootstrap rate')

        observed_ns = self.guest_ns(domain)
        if observed_ns < row[0]:
            raise RuntimeError('guest bootstrap clock is behind the last durable company epoch')
        target_ns = self.effective(row, time.time_ns())
        if observed_ns < target_ns:
            seconds, nseconds = divmod(target_ns, 1_000_000_000)
            domain.setTime({'seconds': seconds, 'nseconds': nseconds}, 0)
            observed_ns = self.guest_ns(domain)
            if observed_ns < target_ns:
                raise RuntimeError('guest time synchronization was not confirmed')
        host_now = time.time_ns()
        with self.state() as db:
            current = db.execute(
                'SELECT desired_rate FROM boot_pending WHERE id=1').fetchone()
            if current is None or current[0] != desired_rate:
                raise RuntimeError('bootstrap rate intent changed during synchronization')
            self.save_anchor(db, observed_ns, host_now, desired_rate)
            db.execute('DELETE FROM boot_pending WHERE id=1')
            db.commit()

    def start_company_target(self):
        with self.state() as db:
            pending = db.execute('SELECT 1 FROM company_target_pending WHERE id=1').fetchone()
        if not pending:
            return
        self.ssh('sudo', 'systemctl', '--no-block', 'start', 'agentic-company.target')
        with self.state() as db:
            db.execute('DELETE FROM company_target_pending WHERE id=1')
            db.commit()
        print('company target start requested after clock synchronization', flush=True)

    def requested_domain_shutdown(self, domain, error):
        if error.get_error_code() != getattr(libvirt, 'VIR_ERR_OPERATION_INVALID', -1):
            return False
        try:
            with self.state() as db:
                intent = db.execute(
                    'SELECT requested FROM shutdown_intent WHERE id=1').fetchone()
            if not intent or not intent[0]:
                return False
            state, reason = domain.state(0)
            return (state == libvirt.VIR_DOMAIN_SHUTOFF and
                    reason == libvirt.VIR_DOMAIN_SHUTOFF_SHUTDOWN)
        except Exception:
            LOG.exception('Could not confirm whether the invalid domain operation came from the requested graceful shutdown')
            return False

    def monitor(self):
        register_event_impl()
        connection = libvirt.open(CONNECTION)
        if connection is None:
            raise RuntimeError('system libvirt connection unavailable')
        try:
            domain = self.domain(connection)
            if not domain.isActive():
                raise RuntimeError('monitor requires a running VM')
            with self.state() as db:
                if self.row(db) is None:
                    raise RuntimeError('clock anchor is uninitialized; initialize before monitoring')
            failure = {'error': None}
            lifecycle_state = {'active': True, 'stop_detail': None,
                               'agent_connected': False}
            timer_state = {'timer': None}
            agent_notice = {'waiting': False}
            target_notice = {'failed': False}
            bootstrap_notice = {'failed': False}
            def checkpoint(timer, _opaque):
                if lifecycle_state['active']:
                    try:
                        with self.operation_lock():
                            if not lifecycle_state['active']:
                                return
                            with self.state() as db:
                                row = self.row(db)
                                if row is None:
                                    raise RuntimeError('clock anchor disappeared while monitoring')
                                shutdown_intent = db.execute(
                                    'SELECT requested FROM shutdown_intent WHERE id=1'
                                ).fetchone()
                                shutdown_requested = bool(shutdown_intent and shutdown_intent[0])
                                boot_pending = db.execute(
                                    'SELECT 1 FROM boot_pending WHERE id=1').fetchone()
                            bootstrap_failed = False
                            if not shutdown_requested:
                                if boot_pending:
                                    try:
                                        self.synchronize_boot_clock(domain)
                                        if bootstrap_notice['failed']:
                                            print('guest clock bootstrap synchronized', flush=True)
                                            bootstrap_notice['failed'] = False
                                    except Exception:
                                        if not bootstrap_notice['failed']:
                                            LOG.exception('Guest clock bootstrap is still pending; VM supervision continues and synchronization will retry')
                                            bootstrap_notice['failed'] = True
                                        bootstrap_failed = True
                                else:
                                    guest = self.guest_ns(domain)
                                    host = time.time_ns()
                                    with self.state() as db:
                                        row = self.row(db)
                                        if row is None:
                                            raise RuntimeError('clock anchor disappeared while monitoring')
                                        pending = db.execute(
                                            'SELECT 1 FROM rate_change WHERE id=1').fetchone()
                                        rate = self.running_rate(domain, db) if pending else row[2]
                                        self.save_anchor(db, guest, host, rate)
                                        db.execute('DELETE FROM rate_change WHERE id=1')
                                        db.commit()
                            if not shutdown_requested and not bootstrap_failed:
                                try:
                                    self.start_company_target()
                                    if target_notice['failed']:
                                        print('company target start request acknowledged', flush=True)
                                        target_notice['failed'] = False
                                except Exception:
                                    if not target_notice['failed']:
                                        LOG.exception('Company target start request failed; clock supervision continues and will retry')
                                        target_notice['failed'] = True
                        if not shutdown_requested and agent_notice['waiting']:
                            print('guest agent time checkpoints resumed', flush=True)
                            agent_notice['waiting'] = False
                        if not shutdown_requested:
                            try:
                                self.reconcile_issue_deadlines()
                            except Exception:
                                LOG.exception('Issue deadline reconciliation failed; clock checkpoints continue')
                    except libvirt.libvirtError as error:
                        expected = {getattr(libvirt, 'VIR_ERR_AGENT_UNRESPONSIVE', -1),
                                    getattr(libvirt, 'VIR_ERR_AGENT_COMMAND_TIMEOUT', -2)}
                        if error.get_error_code() in expected:
                            if not agent_notice['waiting']:
                                print('waiting for guest agent; VM stays under libvirt supervision',
                                      flush=True)
                                agent_notice['waiting'] = True
                            libvirt.virEventUpdateTimeout(timer, 5000)
                            return
                        if self.requested_domain_shutdown(domain, error):
                            libvirt.virEventUpdateTimeout(timer, 5000)
                            return
                        failure['error'] = error
                    except Exception as error:
                        failure['error'] = error
                if failure['error'] is None:
                    libvirt.virEventUpdateTimeout(timer, 5000)
            def lifecycle(_conn, _domain, _event, _detail, _opaque):
                if _event == libvirt.VIR_DOMAIN_EVENT_STOPPED:
                    lifecycle_state['stop_detail'] = _detail
                    lifecycle_state['active'] = False
            lifecycle_callback = connection.domainEventRegisterAny(
                domain, libvirt.VIR_DOMAIN_EVENT_ID_LIFECYCLE, lifecycle, None)
            # Close the registration race once; subsequent state changes arrive
            # through the event callback without polling libvirt on every loop.
            if not domain.isActive():
                lifecycle_state['active'] = False
            def agent_lifecycle(_conn, _domain, state, _reason, _opaque):
                lifecycle_state['agent_connected'] = (
                    state == libvirt.VIR_CONNECT_DOMAIN_EVENT_AGENT_LIFECYCLE_STATE_CONNECTED)
                if lifecycle_state['agent_connected'] and timer_state['timer'] is not None:
                    libvirt.virEventUpdateTimeout(timer_state['timer'], 1)
            agent_callback = connection.domainEventRegisterAny(
                domain, libvirt.VIR_DOMAIN_EVENT_ID_AGENT_LIFECYCLE,
                agent_lifecycle, None)
            timer = libvirt.virEventAddTimeout(5000, checkpoint, None)
            timer_state['timer'] = timer
            try:
                print('clock monitor active; durable guest-time checkpoints every 5 seconds',
                      flush=True)
                while lifecycle_state['active'] and failure['error'] is None:
                    libvirt.virEventRunDefaultImpl()
                if failure['error'] is not None:
                    raise RuntimeError('clock monitor encountered a checkpoint failure') from failure['error']
                if not lifecycle_state['active']:
                    intentional = (lifecycle_state['stop_detail'] ==
                                   libvirt.VIR_DOMAIN_EVENT_STOPPED_SHUTDOWN)
                    with self.state() as db:
                        intent = db.execute(
                            'SELECT requested FROM shutdown_intent WHERE id=1').fetchone()
                    if not (intent and intent[0] and intentional):
                        raise RuntimeError('VM stopped unexpectedly; requesting supervised fresh-epoch restart')
            finally:
                libvirt.virEventRemoveTimeout(timer)
                connection.domainEventDeregisterAny(agent_callback)
                connection.domainEventDeregisterAny(lifecycle_callback)
        finally:
            connection.close()

    def initialize(self, epoch_ns, rate, host_epoch_ns=None):
        if epoch_ns <= 0 or rate < 1:
            raise ValueError('epoch nanoseconds and rate must be positive')
        connection = libvirt.open(CONNECTION)
        if connection is None:
            raise RuntimeError('system libvirt connection unavailable')
        try:
            if self.domain(connection).isActive():
                raise RuntimeError('initialize requires a stopped VM so the supplied epoch remains authoritative')
        finally:
            connection.close()
        with self.state() as db:
            if self.row(db) is not None:
                raise RuntimeError('clock anchor already exists; initialize never overwrites it')
            anchor = time.time_ns() if host_epoch_ns is None else host_epoch_ns
            if anchor <= 0 or anchor > time.time_ns():
                raise ValueError('the observed host anchor must be a past or current Unix timestamp')
            self.save_anchor(db, epoch_ns, anchor, rate)
        print(f'clock anchor initialized at epoch {epoch_ns} with offline rate {rate}x')

    def resolve_pending_rate(self, rate, epoch_ns, host_epoch_ns):
        if rate < 1 or epoch_ns <= 0 or host_epoch_ns <= 0:
            raise ValueError('rate and observed epoch anchors must be positive')
        connection = libvirt.open(CONNECTION)
        if connection is None:
            raise RuntimeError('system libvirt connection unavailable')
        try:
            if self.domain(connection).isActive():
                raise RuntimeError('resolve-pending-rate requires a stopped VM')
        finally:
            connection.close()
        with self.state() as db:
            row = self.row(db)
            pending = db.execute('SELECT 1 FROM rate_change WHERE id=1').fetchone()
            if row is None or pending is None:
                raise RuntimeError('there is no pending rate change to resolve')
            self.save_anchor(db, epoch_ns, host_epoch_ns, rate)
            db.execute('DELETE FROM rate_change WHERE id=1')
            db.commit()
        print(f'pending rate resolved as {rate}x using operator evidence')

    def run(self):
        with self.operation_lock():
            connection = libvirt.open(CONNECTION)
            if connection is None:
                raise RuntimeError('system libvirt connection unavailable')
            try:
                active = self.domain(connection).isActive()
            finally:
                connection.close()
            if not active:
                self.start()
        self.monitor()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=DEFAULT_ROOT)
    parser.add_argument('--name', default=DEFAULT_NAME)
    parser.add_argument('--ssh-port', type=int, default=DEFAULT_PORT)
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('status')
    start = sub.add_parser('start')
    start.add_argument('--rate', type=int)
    sub.add_parser('stop')
    sub.add_parser('prepare')
    sub.add_parser('monitor')
    sub.add_parser('run')
    process_issue = sub.add_parser('process-issue')
    process_issue.add_argument('--issue-key', required=True)
    rate = sub.add_parser('set-rate')
    rate.add_argument('rate', type=int)
    initialize = sub.add_parser('initialize')
    epoch = initialize.add_mutually_exclusive_group(required=True)
    epoch.add_argument('--epoch-ns', type=int)
    epoch.add_argument('--host-now', action='store_true')
    initialize.add_argument('--rate', type=int, required=True)
    initialize.add_argument('--host-epoch-ns', type=int,
                            help='host epoch paired with a previously observed company epoch')
    resolve = sub.add_parser('resolve-pending-rate')
    resolve.add_argument('--rate', type=int, required=True,
                         help='actual rate supported by operator evidence')
    resolve.add_argument('--epoch-ns', type=int, required=True,
                         help='observed guest epoch, not a projection of an uncertain rate')
    resolve.add_argument('--host-epoch-ns', type=int, required=True,
                         help='host epoch at the same observation')
    args = parser.parse_args()
    controller = Controller(args.root, args.name, args.ssh_port)
    try:
        if args.action == 'run':
            controller.run()
        elif args.action == 'process-issue':
            controller.process_issue(args.issue_key)
        elif args.action == 'monitor':
            controller.monitor()
        elif args.action == 'status':
            controller.status()
        elif args.action == 'start':
            if args.rate is not None and args.rate < 1:
                parser.error('--rate must be a positive integer')
            with controller.operation_lock():
                controller.start(args.rate)
        elif args.action == 'stop':
            with controller.operation_lock():
                controller.stop()
        elif args.action == 'prepare':
            with controller.operation_lock():
                controller.prepare()
        elif args.action == 'initialize':
            with controller.operation_lock():
                if args.host_now and args.host_epoch_ns is not None:
                    parser.error('--host-epoch-ns belongs with --epoch-ns')
                epoch_ns = time.time_ns() if args.host_now else args.epoch_ns
                controller.initialize(epoch_ns, args.rate, args.host_epoch_ns)
        elif args.action == 'resolve-pending-rate':
            with controller.operation_lock():
                controller.resolve_pending_rate(args.rate, args.epoch_ns, args.host_epoch_ns)
        else:
            with controller.operation_lock():
                controller.set_rate(args.rate)
    except Exception as error:
        traceback.print_exc(file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
