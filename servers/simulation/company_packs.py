"""Durable, operator-owned imports; network and native work never hold SQLite locks."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from graphlib import TopologicalSorter
import json
import logging
import os
import re
from pathlib import Path
import sqlite3
import tempfile
import threading
import traceback
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from servers.company.packs import load_pack
from servers.company.packs.models import record_dependencies

LOG = logging.getLogger(__name__)


class PackVersionConflict(ValueError):
    """An immutable pack ID/version was reused with different archive bytes."""


class CompanyPackService:
    def __init__(self, root: Path, *, engine: Any, native: Any,
                 binding: dict[str, Any], profile_path: Path, owncloud_factory: Any, erp_public_url: str, apply_applications: Any):
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        self.database = root / "imports.sqlite"
        self.engine, self.native = engine, native
        self.binding, self.profile_path = binding, profile_path
        self.apply_applications = apply_applications
        self.owncloud_factory = owncloud_factory
        self.erp_public_url = erp_public_url.rstrip("/")
        self.changed = threading.Event()
        self.stopped = threading.Event()
        self.worker: threading.Thread | None = None
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS packs (id TEXT PRIMARY KEY, pack_id TEXT NOT NULL, version TEXT NOT NULL, digest TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(pack_id,version))")
        self.database.chmod(0o600)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _save(self, row):
        with self._db() as db:
            db.execute("UPDATE packs SET payload=? WHERE id=?", (json.dumps(row), row["id"]))

    def get(self, identifier: str):
        with self._db() as db:
            row = db.execute("SELECT payload FROM packs WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise KeyError(identifier)
        return json.loads(row["payload"])

    def list(self):
        with self._db() as db:
            rows = db.execute("SELECT payload FROM packs ORDER BY rowid DESC").fetchall()
        return [self.public(json.loads(row["payload"])) for row in rows]

    def applications(self):
        profile = json.loads(self.profile_path.read_text())
        persona = profile["company"].get("persona")
        return persona["applications"] if persona is not None else None

    @staticmethod
    def public(row):
        return {key: value for key, value in row.items() if key not in {"manifest", "anchor_time", "steps", "native_records", "files", "active_step"}}

    def upload(self, data: bytes):
        loaded = load_pack(data)
        manifest = loaded.manifest
        profile = json.loads(self.profile_path.read_text())
        canonical = {employee["id"]: employee for employee in profile["employees"]}
        departments = {department.id: department for department in manifest.departments}
        for employee in manifest.employees:
            current = canonical.get(employee.id)
            if current is None or current["department"] != departments[employee.department_id].name:
                raise ValueError("Pack employee must resolve to the canonical company directory")
        with self._db() as db:
            previous = db.execute("SELECT payload FROM packs WHERE pack_id=? AND version=?", (manifest.id, manifest.version)).fetchone()
        if previous:
            row = json.loads(previous["payload"])
            if row["digest"] != loaded.digest:
                raise PackVersionConflict("Pack ID and version already have a different archive digest")
            return self.public(row)
        identifier = str(uuid4())
        private = self.root / identifier
        private.mkdir(mode=0o700)
        archive_path = private / "pack.zip"
        with archive_path.open("xb") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(data)
        row = {"id": identifier, "pack_id": manifest.id, "version": manifest.version,
               "digest": loaded.digest, "title": manifest.title,
               "status": "validated", "created_at": datetime.now(timezone.utc).isoformat(),
               "persona": {"name": manifest.company_persona.company_name,
                           "business_type": manifest.company_persona.business_type,
                           "applications": list(manifest.company_persona.applications),
                           "description": manifest.company_persona.description},
               "counts": {"employees": len(manifest.employees), "departments": len(manifest.departments),
                          "records": len(manifest.records), "files": len(manifest.files),
                          "experiments": len(manifest.experiments)},
               "completed_steps": 0, "total_steps": 3 + len(manifest.files), "steps": [],
               "manifest": manifest.model_dump(mode="json"), "error": None}
        try:
            with self._db() as db:
                db.execute("INSERT INTO packs VALUES(?,?,?,?,?)", (identifier, manifest.id, manifest.version, loaded.digest, json.dumps(row)))
        except sqlite3.IntegrityError:
            archive_path.unlink()
            private.rmdir()
            with self._db() as db:
                previous = db.execute("SELECT payload FROM packs WHERE pack_id=? AND version=?", (manifest.id, manifest.version)).fetchone()
            if previous is None:
                raise
            existing = json.loads(previous["payload"])
            if existing["digest"] != loaded.digest:
                raise PackVersionConflict("Pack ID and version already have a different archive digest")
            return self.public(existing)
        return self.public(row)

    def queue(self, identifier: str):
        current = self.get(identifier)
        anchor = current.get("anchor_time")
        if not anchor and current["status"] not in {"queued", "importing", "completed"}:
            provider = getattr(self.engine, "company_time_provider", None)
            moment = provider() if callable(provider) else (self.engine.company_state() or {}).get("simulated_time")
            if moment is None:
                raise ValueError("Company clock is unavailable")
            anchor = moment.isoformat() if isinstance(moment, datetime) else str(moment)
        with self._db() as db:
            # Recheck after observing the clock: another request may have queued it.
            stored = db.execute("SELECT payload FROM packs WHERE id=?", (identifier,)).fetchone()
            if stored is None:
                raise KeyError(identifier)
            row = json.loads(stored["payload"])
            if row["status"] in {"queued", "importing", "completed"}:
                if row["status"] != "completed":
                    self.changed.set()
                return self.public(row)
            row.setdefault("anchor_time", anchor)
            row.update(status="queued", error=None)
            db.execute("UPDATE packs SET payload=? WHERE id=?", (json.dumps(row), identifier))
        self.changed.set()
        return self.public(row)

    def reconcile_applications(self):
        profile = json.loads(self.profile_path.read_text())
        persona = profile["company"].get("persona")
        if persona is not None:
            self.apply_applications(persona["applications"])

    def start(self):
        with self._db() as db:
            rows = db.execute("SELECT payload FROM packs").fetchall()
            for stored in rows:
                row = json.loads(stored["payload"])
                if row["status"] == "importing":
                    row["status"] = "queued"
                    db.execute("UPDATE packs SET payload=? WHERE id=?", (json.dumps(row), row["id"]))
        self.worker = threading.Thread(target=self._work, name="company-pack-import", daemon=True)
        self.worker.start()
        self.changed.set()

    def signal_stop(self):
        self.stopped.set()
        self.changed.set()

    def stop(self):
        self.signal_stop()
        if self.worker is not None:
            self.worker.join()

    def _work(self):
        while not self.stopped.is_set():
            self.changed.wait()
            self.changed.clear()
            try:
                self._drain_queue()
            except Exception as error:
                # Retry on the next queue/retry event, never spin on a broken DB.
                # Persisted importing records remain resumable via native provenance.
                frames = [(f.filename, f.lineno, f.name) for f in traceback.extract_tb(error.__traceback__)]
                LOG.error("company pack worker persistence failed exception_type=%s stack=%s", type(error).__name__, frames)

    def _drain_queue(self):
        with self._db() as db:
            rows = db.execute("SELECT payload FROM packs ORDER BY rowid").fetchall()
        for stored in rows:
            row = json.loads(stored["payload"])
            if self.stopped.is_set():
                return
            if row["status"] not in {"queued", "importing"}:
                continue
            row["status"] = "importing"
            self._save(row)
            try:
                self._import(row)
                row["status"] = "completed"
            except Exception as error:
                frames = [(f.filename, f.lineno, f.name) for f in traceback.extract_tb(error.__traceback__)]
                causes = []
                diagnostic = str(error)
                safe_code = diagnostic if re.fullmatch(r"[A-Z][A-Z0-9_]{1,100}", diagnostic) else None
                cause = error.__cause__ or error.__context__
                while cause is not None:
                    causes.append(type(cause).__name__)
                    cause = cause.__cause__ or cause.__context__
                LOG.error("company pack import failed import_id=%s step=%s exception_type=%s code=%s status_code=%s causes=%s stack=%s", row["id"], row.get("active_step"), type(error).__name__, safe_code, getattr(error, "status_code", None), causes, frames)
                row.update(status="failed", error=f"Import failed ({type(error).__name__}). Retry this import after the operator resolves the recorded failure.")
            self._save(row)

    def _checkpoint(self, row, step):
        row["steps"].append(step)
        row["completed_steps"] = len(row["steps"])
        self._save(row)

    def _directory(self, manifest):
        company = self.binding["documents"]["company"]
        if isinstance(company, dict):
            company = company["name"]
        bindings = self.native.resolve_company_pack_directory({
            "company_binding": {"company_id": "company", "erp_company": company},
            "employee_ids": [employee.id for employee in manifest.employees],
            "departments": {department.id: department.name for department in manifest.departments},
        })
        return company, bindings

    def _import(self, row):
        loaded = load_pack((self.root / row["id"] / "pack.zip").read_bytes())
        if loaded.digest != row["digest"]:
            raise ValueError("Stored pack archive digest changed")
        manifest = loaded.manifest
        row["active_step"] = "applications"
        self.apply_applications(manifest.company_persona.applications)
        if "applications" not in row["steps"]:
            self._checkpoint(row, "applications")
        if "native" not in row["steps"] and not manifest.records:
            row["native_records"] = {}
            self._checkpoint(row, "native")
        if "native" not in row["steps"]:
            row["active_step"] = "native"
            company, bindings = self._directory(manifest)
            records = {record.id: record for record in manifest.records}
            graph = {record.id: record_dependencies(record.fields, set(records), {e.id for e in manifest.employees}, {d.id for d in manifest.departments}) for record in manifest.records}
            result = self.native.import_company_pack({"pack_id": manifest.id, "version": manifest.version,
                "digest": loaded.digest, "anchor_time": row["anchor_time"],
                "company_binding": {"company_id": "company", "erp_company": company},
                "directory_bindings": bindings,
                "records": [records[key].model_dump(mode="json") for key in TopologicalSorter(graph).static_order()]})
            row["native_records"] = result["records"]
            self._checkpoint(row, "native")
        for file in manifest.files:
            step = "file:" + file.id
            if step in row["steps"]:
                continue
            row["active_step"] = step
            if file.visibility == "evaluation":
                private = self.root / row["id"] / "evaluation"
                private.mkdir(mode=0o700, exist_ok=True)
                path = private / file.id
                descriptor, temporary = tempfile.mkstemp(dir=private, prefix=".evaluation-")
                try:
                    os.fchmod(descriptor, 0o600)
                    with os.fdopen(descriptor, "wb") as output:
                        output.write(loaded.files[file.path])
                    os.replace(temporary, path)
                finally:
                    Path(temporary).unlink(missing_ok=True)
            else:
                client, owncloud_config = self.owncloud_factory()
                path = "Company Packs/" + row["id"] + "/" + file.drive_path
                try:
                    metadata = client.upload_pack_file(path, loaded.files[file.path])
                    if file.department_ids:
                        policy = owncloud_config["access_policy_candidates"][owncloud_config["selected_access_policy"]]
                        native_groups = {group["department"]: group["group_id"] for group in policy["department_groups"]}
                        departments = {department.id: department.name for department in manifest.departments}
                        groups = [native_groups[departments[key]] for key in file.department_ids]
                    else:
                        groups = [owncloud_config["company_staff_group_id"]]
                    shares = client.list_shares()
                    for group in groups:
                        matching = [share for share in shares if str(share.get("file_source", share.get("item_source", ""))) == str(metadata["file_id"]) and str(share.get("share_with")) == group and int(share.get("share_type", -1)) == 1]
                        if not matching:
                            client.create_group_share(path, group, 1 | 2)
                    row.setdefault("files", {})[file.id] = {"path": path, "file_id": metadata["file_id"],
                        "url": client.base_url.rstrip("/") + "/index.php/apps/files/?fileid=" + quote(str(metadata["file_id"]), safe="")}
                finally:
                    client.session.close()
            self._checkpoint(row, step)
        if "persona" not in row["steps"]:
            row["active_step"] = "persona"
            profile = json.loads(self.profile_path.read_text())
            profile["company"]["persona"] = manifest.company_persona.model_dump(mode="json")
            profile["company"]["packDepartments"] = [d.model_dump(mode="json") for d in manifest.departments]
            profile["company"]["roleUnderEvaluation"] = manifest.role_under_evaluation.model_dump(mode="json")
            contexts = {e.id: e.model_dump(mode="json") for e in manifest.employees}
            for employee in profile["employees"]:
                if employee["id"] in contexts:
                    employee["businessContext"] = contexts[employee["id"]]
            descriptor, temporary = tempfile.mkstemp(dir=self.profile_path.parent, prefix=".company-pack-")
            try:
                os.fchmod(descriptor, self.profile_path.stat().st_mode & 0o777)
                with os.fdopen(descriptor, "w") as output:
                    json.dump(profile, output, ensure_ascii=False, indent=2)
                    output.write("\n")
                os.replace(temporary, self.profile_path)
            finally:
                Path(temporary).unlink(missing_ok=True)
            self._checkpoint(row, "persona")

    def catalog(self):
        tasks = []
        for row in self.list():
            if row["status"] != "completed":
                continue
            private = self.get(row["id"])
            manifest = private["manifest"]
            departments = {department["id"]: department["name"] for department in manifest["departments"]}
            record_types = {record["id"]: record["doctype"] for record in manifest["records"]}
            for experiment in manifest["experiments"]:
                links = []
                for key in experiment["records"]:
                    native_name = private["native_records"][key]
                    doctype = record_types[key]
                    links.append(f"{doctype} {key}: {self.erp_public_url}/app/{doctype.lower().replace(' ', '-')}/{quote(native_name, safe='')}")
                for key in experiment["files"]:
                    links.append(f"File {key}: {private['files'][key]['url']}")
                description = experiment["request_message"]
                if links:
                    description += "\n\nNative work records and files:\n" + "\n".join(links)
                tasks.append({"id": "company-pack-" + row["id"] + "-" + experiment["id"],
                    "title": experiment["title"], "summary": experiment["brief"],
                    "role": departments[experiment["department_id"]], "dependencies": [],
                    "description": description, "display_description": experiment["brief"],
                    "kind": "workplace", "sender_employee_id": experiment["sender_employee_id"]})
        return {"experiments": tasks}
