"""Trusted, run-scoped native Frappe site creation and golden restore operations."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import contextlib
import json
import os
import shutil
import stat
import subprocess
import sys
import traceback
import uuid
from pathlib import Path


SITE_PREFIX = "erp-sim-"
SITE_SUFFIX = ".localhost"
BENCH_ROOT = Path("/home/frappe/frappe-bench")
SITES_ROOT = BENCH_ROOT / "sites"
BENCH_EXECUTABLE = Path("/usr/local/bin/bench")
GOLDEN_ROOT = Path("/opt/erpnext-golden")
RUNTIME_ROOT = Path("/opt/erpnext-runtime")
COMPANY_SITE = "erp-sim-4472c9ce758f548598f1be6379f9f42b.localhost"


def site_for_run(run_id: str) -> str:
    try:
        run_uuid = uuid.UUID(run_id)
    except (ValueError, AttributeError) as error:
        raise ValueError("run_id must be a canonical UUID") from error
    if str(run_uuid) != run_id.lower():
        raise ValueError("run_id must be a canonical UUID")
    return f"{SITE_PREFIX}{run_uuid.hex}{SITE_SUFFIX}"


def secret(path: str) -> str:
    value = Path(path).read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError("Required private setup secret is empty")
    return value


def private_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        raise FileExistsError("Owned ERP run manifest already exists")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump(value, output, sort_keys=True)
        output.write("\n")
    path.chmod(0o600)


def lock_for(site: str):
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = RUNTIME_ROOT / f"{site}.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, stat.S_IRUSR | stat.S_IWUSR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _erpnext_is_installed(site: str) -> bool:
    if not (SITES_ROOT / site / "site_config.json").is_file():
        return False
    completed = subprocess.run(
        [str(BENCH_EXECUTABLE), "--site", site, "list-apps"],
        check=True,
        capture_output=True,
        text=True,
        cwd=BENCH_ROOT,
        timeout=30,
    )
    return any(line.split() and line.split()[0] == "erpnext" for line in completed.stdout.splitlines())


def _create(site: str) -> dict[str, str]:
    site_path = SITES_ROOT / site
    manifest_path = RUNTIME_ROOT / f"{site}.json"
    golden_dir = GOLDEN_ROOT / site
    if manifest_path.exists() and golden_dir.is_dir():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        hashes = json.loads(manifest.get("golden_files_sha256", "{}"))
        if manifest.get("site_name") != site or not (site_path / "site_config.json").is_file():
            raise RuntimeError("Existing ERP run manifest does not match its native site")
        if not hashes or any(
            not (golden_dir / name).is_file()
            or hashlib.sha256((golden_dir / name).read_bytes()).hexdigest() != digest
            for name, digest in hashes.items()
        ):
            raise RuntimeError("Existing ERP run golden baseline failed its recorded integrity check")
        return public_binding(manifest)
    if manifest_path.exists() or golden_dir.exists():
        raise RuntimeError("ERP run has an incomplete manifest or golden baseline; operator recovery is required")

    if site_path.exists() and not _erpnext_is_installed(site):
        # Recover only an incomplete site whose name was derived from this
        # coordinator UUID. Use Frappe's own drop-site path; never remove
        # arbitrary directories or a site with a run manifest/golden baseline.
        sys.path.insert(0, str(BENCH_ROOT / "apps"))
        from frappe.commands.site import drop_site

        os.chdir(SITES_ROOT)
        drop_site.callback(
            site,
            db_root_username="root",
            db_root_password=secret("/run/secrets/db-root-password"),
            no_backup=True,
            force=True,
        )
        os.chdir(BENCH_ROOT)
        credential_path = RUNTIME_ROOT / "api-credentials" / f"{site}.json"
        credential_path.unlink(missing_ok=True)

    if not (site_path / "site_config.json").is_file() or not _erpnext_is_installed(site):
        os.chdir(SITES_ROOT)
        sys.path.insert(0, str(BENCH_ROOT / "apps"))
        from frappe.commands.site import new_site

        with contextlib.redirect_stdout(sys.stderr):
            new_site.callback(
                site,
                db_root_username="root",
                db_root_password=secret("/run/secrets/db-root-password"),
                admin_password=secret("/run/secrets/site-admin-password"),
                no_mariadb_socket=True,
                mariadb_user_host_login_scope="%",
                install_app=("erpnext",),
                set_default=False,
                db_host="db",
                db_port=3306,
            )
        import frappe

        frappe.destroy()
        os.chdir(BENCH_ROOT)

    sys.path.insert(0, "/opt/erpnext-admin")
    from seed_a1 import seed

    (BENCH_ROOT / "logs").mkdir(mode=0o700, parents=True, exist_ok=True)
    (site_path / "logs").mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chdir(SITES_ROOT)
    with contextlib.redirect_stdout(sys.stderr):
        seeded = seed(site, str(RUNTIME_ROOT))
    os.chdir(BENCH_ROOT)
    backup_dir = site_path / "private" / "backups"
    before = {file.resolve() for file in backup_dir.glob("*")} if backup_dir.exists() else set()
    subprocess.run(
        [str(BENCH_EXECUTABLE), "--site", site, "backup", "--with-files"],
        check=True,
        stdout=sys.stderr,
        stderr=sys.stderr,
    )
    backups = [file for file in backup_dir.glob("*") if file.resolve() not in before]
    sql = [file for file in backups if file.name.endswith("-database.sql.gz")]
    if len(sql) != 1:
        raise RuntimeError("Frappe backup did not produce exactly one database archive")
    golden_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
    copied: dict[str, str] = {}
    for backup in backups:
        if backup == sql[0]:
            destination = golden_dir / "database.sql.gz"
        elif backup.name.endswith("-public-files.tar"):
            destination = golden_dir / "public-files.tar"
        elif backup.name.endswith("-private-files.tar"):
            destination = golden_dir / "private-files.tar"
        else:
            continue
        shutil.copyfile(backup, destination)
        destination.chmod(0o600)
        copied[destination.name] = hashlib.sha256(destination.read_bytes()).hexdigest()
    if "database.sql.gz" not in copied:
        raise RuntimeError("Golden ERP backup is incomplete")

    manifest = {**seeded, "golden_files_sha256": json.dumps(copied, sort_keys=True)}
    private_json(manifest_path, manifest)
    return public_binding(manifest)


def public_binding(manifest: dict[str, str]) -> dict[str, object]:
    site = manifest["site_name"]
    return {
        "site_name": site,
        "documents": {
            "company": manifest["company_id"],
            "supplier": manifest["supplier_id"],
            "item": manifest["item_id"],
            "warehouse": manifest["warehouse_id"],
            "purchase_order": {
                "name": manifest["purchase_order_id"],
                "item_row": manifest["purchase_order_item_id"],
            },
            "purchase_receipt": manifest["purchase_receipt_id"],
            "purchase_invoice": manifest["purchase_invoice_id"],
            "customer": manifest["customer_id"],
            "customer_order": manifest["sales_order_id"],
        },
        "credentials_ref": f"/data/theagentcompany/enterprise/erpnext/runtime/api-credentials/{site}.json",
    }


def _reset(site: str) -> dict[str, str]:
    manifest_path = RUNTIME_ROOT / f"{site}.json"
    golden_dir = GOLDEN_ROOT / site
    if not manifest_path.is_file() or not (golden_dir / "database.sql.gz").is_file():
        raise FileNotFoundError("Run-owned golden ERP baseline is unavailable")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("site_name") != site:
        raise RuntimeError("Run manifest does not own this ERP site")

    sys.path.insert(0, str(BENCH_ROOT / "apps"))
    import frappe
    from frappe.commands.site import _restore

    os.chdir(SITES_ROOT)
    frappe.init(site, sites_path=str(SITES_ROOT))
    try:
        _restore(
            site=site,
            sql_file_path=str(golden_dir / "database.sql.gz"),
            db_root_username="root",
            db_root_password=secret("/run/secrets/db-root-password"),
            force=True,
            with_public_files=str(golden_dir / "public-files.tar") if (golden_dir / "public-files.tar").is_file() else None,
            with_private_files=str(golden_dir / "private-files.tar") if (golden_dir / "private-files.tar").is_file() else None,
        )
    finally:
        frappe.destroy()
        os.chdir(BENCH_ROOT)
    idempotency = RUNTIME_ROOT / f"{site}.idempotency.sqlite"
    for path in (idempotency, Path(f"{idempotency}-wal"), Path(f"{idempotency}-shm")):
        path.unlink(missing_ok=True)
    return public_binding(manifest)


def _ensure_erp_contract(site: str) -> dict[str, object]:
    """Apply the native marker field and UTC setting to an already-owned site."""
    manifest_path = RUNTIME_ROOT / f"{site}.json"
    golden_dir = GOLDEN_ROOT / site
    site_path = SITES_ROOT / site
    if (not manifest_path.is_file() or not (golden_dir / "database.sql.gz").is_file()
            or not (site_path / "site_config.json").is_file()):
        raise FileNotFoundError("Run-owned ERP site and golden baseline are required")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("site_name") != site:
        raise RuntimeError("Run manifest does not own this ERP site")

    sys.path.insert(0, "/opt/erpnext-admin")
    from seed_a1 import ensure_shipping_marker_field, ensure_utc_timezone

    sys.path.insert(0, str(BENCH_ROOT / "apps"))
    import frappe

    os.chdir(SITES_ROOT)
    frappe.init(site, sites_path=str(SITES_ROOT))
    try:
        frappe.connect()
        frappe.set_user("Administrator")
        with contextlib.redirect_stdout(sys.stderr):
            fieldname = ensure_shipping_marker_field()
            timezone = ensure_utc_timezone()
        frappe.db.commit()
    finally:
        frappe.destroy()
        os.chdir(BENCH_ROOT)
    return {"site_name": site, "custom_field": fieldname, "unique": True, "system_timezone": timezone}


def _seed_tau_retail(experiment_id: str, task_id: str) -> dict[str, object]:
    """Import one private task manifest into the already-owned company site."""
    if str(uuid.UUID(experiment_id)) != experiment_id.lower():
        raise ValueError("experiment_id must be a canonical UUID")
    if not task_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in task_id):
        raise ValueError("task_id contains unsupported characters")
    site_path = SITES_ROOT / COMPANY_SITE
    golden_dir = GOLDEN_ROOT / COMPANY_SITE
    manifest_path = RUNTIME_ROOT / "tau-retail" / experiment_id / f"{task_id}.json"
    if not (site_path / "site_config.json").is_file() or not (golden_dir / "database.sql.gz").is_file():
        raise FileNotFoundError("Existing company ERPNext site and golden baseline are required")
    if not manifest_path.is_file() or manifest_path.resolve().parent.parent != (RUNTIME_ROOT / "tau-retail").resolve():
        raise FileNotFoundError("Private τ retail task manifest is unavailable")

    sys.path.insert(0, str(BENCH_ROOT / "apps"))
    sys.path.insert(0, "/opt/erpnext-admin")
    import frappe
    from tau_retail import ensure_retail_contract, seed_retail_task

    lock_fd = lock_for(COMPANY_SITE)
    try:
        os.chdir(SITES_ROOT)
        frappe.init(COMPANY_SITE, sites_path=str(SITES_ROOT))
        frappe.connect()
        frappe.set_user("Administrator")
        if not frappe.db.exists("Company", "The Agent Company"):
            raise RuntimeError("Owned company ERPNext site has no canonical Company")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        try:
            with contextlib.redirect_stdout(sys.stderr):
                retail_contract = ensure_retail_contract(manifest.get("retail_operator_users"))
            # Custom fields, workflow, scoped user permissions and the native
            # bank-account default are site setup. Commit them independently
            # so a later document import can be transactionally rolled back.
            frappe.db.commit()
            with contextlib.redirect_stdout(sys.stderr):
                result = seed_retail_task(manifest, experiment_id, manifest.get("projection_as_of"))
            if result.get("task_id") != task_id:
                raise RuntimeError("Private τ retail manifest task id does not match its path")
            frappe.db.commit()
            return {**result, "site_name": COMPANY_SITE, "retail_contract": retail_contract}
        except Exception:
            frappe.db.rollback()
            raise
    finally:
        frappe.destroy()
        os.chdir(BENCH_ROOT)
        os.close(lock_fd)


def _observe_tau_retail(experiment_id: str, task_id: str) -> dict[str, object]:
    if str(uuid.UUID(experiment_id)) != experiment_id.lower():
        raise ValueError("experiment_id must be a canonical UUID")
    if not task_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in task_id):
        raise ValueError("task_id contains unsupported characters")
    site_path = SITES_ROOT / COMPANY_SITE
    manifest_path = RUNTIME_ROOT / "tau-retail" / experiment_id / f"{task_id}.json"
    if not (site_path / "site_config.json").is_file() or not manifest_path.is_file():
        raise FileNotFoundError("Existing company ERPNext site and private τ retail manifest are required")
    sys.path.insert(0, str(BENCH_ROOT / "apps"))
    sys.path.insert(0, "/opt/erpnext-admin")
    import frappe
    from tau_retail import observe_retail_task
    lock_fd = lock_for(COMPANY_SITE)
    try:
        os.chdir(SITES_ROOT)
        frappe.init(COMPANY_SITE, sites_path=str(SITES_ROOT))
        frappe.connect()
        frappe.set_user("Administrator")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        with contextlib.redirect_stdout(sys.stderr):
            result = observe_retail_task(manifest, experiment_id)
        if result.get("task_id") != task_id:
            raise RuntimeError("Private τ retail manifest task id does not match its path")
        return {**result, "site_name": COMPANY_SITE}
    finally:
        frappe.destroy()
        os.chdir(BENCH_ROOT)
        os.close(lock_fd)


def _run_finance_source_operation(operation_id: str, operation: str) -> dict[str, object]:
    if str(uuid.UUID(operation_id)) != operation_id.lower():
        raise ValueError("finance operation_id must be a canonical UUID")
    if operation not in ("reconcile-finance-sources", "read-due-obligations",
                         "reconcile-company-issues", "company-issue-notifications", "company-evidence-setup", "reconcile-ecommerce-finance",
                         "capture-evaluation-evidence"):
        raise ValueError("unsupported native finance source operation")
    payload_path = RUNTIME_ROOT / "finance-sources" / f"{operation_id}.json"
    if not payload_path.is_file() or payload_path.resolve().parent != (RUNTIME_ROOT / "finance-sources").resolve():
        raise FileNotFoundError("Private finance source operation input is unavailable")
    if not (SITES_ROOT / COMPANY_SITE / "site_config.json").is_file():
        raise FileNotFoundError("Existing company ERPNext site is required")
    sys.path.insert(0, str(BENCH_ROOT / "apps"))
    sys.path.insert(0, "/opt/erpnext-admin")
    import frappe

    lock_fd = lock_for(COMPANY_SITE)
    try:
        os.chdir(SITES_ROOT)
        frappe.init(COMPANY_SITE, sites_path=str(SITES_ROOT))
        frappe.connect()
        frappe.set_user("Administrator")
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        if operation == "capture-evaluation-evidence":
            if payload.get("erp_company") != "The Agent Company":
                raise ValueError("Evaluation evidence capture must target the existing company")
        elif operation in ("reconcile-finance-sources", "reconcile-company-issues", "reconcile-ecommerce-finance"):
            binding = payload.get("company_binding")
            if (not isinstance(binding, dict) or binding.get("company_id") != "company"
                    or binding.get("erp_company") != "The Agent Company"):
                raise ValueError("Finance source operation is not bound to the existing company")
        elif payload.get("erp_company") != "The Agent Company":
            raise ValueError("Due-obligation read must target the existing company")
        with contextlib.redirect_stdout(sys.stderr):
            if operation == "capture-evaluation-evidence":
                from evaluation_evidence import (
                    capture_company_evaluation_evidence,
                    evaluation_evidence_version_manifest,
                )
                from tau_retail import observe_retail_task

                # Capture the evaluator's projection and native-version evidence
                # in one MariaDB consistent read view, then always release it.
                capture_error = None
                try:
                    evidence = capture_company_evaluation_evidence(payload["entity_refs"], payload["erp_company"])
                    retail = None
                    experiment_id = payload.get("experiment_id")
                    task_id = payload.get("task_id")
                    if experiment_id is not None or task_id is not None:
                        if (not isinstance(experiment_id, str) or str(uuid.UUID(experiment_id)) != experiment_id.lower()
                                or not isinstance(task_id, str) or not task_id
                                or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in task_id)):
                            raise ValueError("Evaluation evidence task binding is invalid")
                        manifest_path = RUNTIME_ROOT / "tau-retail" / experiment_id / f"{task_id}.json"
                        if not manifest_path.is_file() or manifest_path.resolve().parent.parent != (RUNTIME_ROOT / "tau-retail").resolve():
                            raise FileNotFoundError("Private τ retail task manifest is unavailable for evaluation capture")
                        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                        retail = observe_retail_task(manifest, experiment_id)
                    version_manifest = evaluation_evidence_version_manifest(evidence)
                    native_evidence = version_manifest if payload.get("versions_only") is True else evidence
                    result = {"native_evidence": native_evidence,
                              "version_manifest": version_manifest,
                              "retail_observation": retail}
                except BaseException as error:
                    capture_error = error
                    raise
                finally:
                    try:
                        frappe.db.rollback()
                    except Exception as rollback_error:
                        if capture_error is None:
                            raise
                        capture_error.add_note(
                            f"Read-only evidence rollback failed: {type(rollback_error).__name__}"
                        )
            elif operation == "reconcile-ecommerce-finance":
                from ecommerce_finance import reconcile
                result = reconcile(payload["records"], payload["company_binding"], payload["as_of"])
                frappe.db.commit()
            elif operation == "company-evidence-setup":
                from company_evidence import ensure_evidence_webhooks, collect_mapped_records, collect_evidence_deliveries, collect_company_issue_events
                observer = ensure_evidence_webhooks(payload["endpoint"], payload["secret"])
                frappe.db.commit()
                result = {"observer": observer, "records": collect_mapped_records(), **collect_evidence_deliveries(payload.get("cursor")), **collect_company_issue_events()}
            elif operation == "reconcile-finance-sources":
                from finance_sources import reconcile
                result = reconcile(payload["source_records"], payload["company_binding"], payload["as_of"])
            elif operation == "read-due-obligations":
                from finance_sources import read_due_obligations
                result = read_due_obligations(payload["erp_company"], payload["as_of"])
            else:
                from company_issues import ensure_company_issue_contract, reconcile_disruption_issues
                from company_issue_notifications import (
                    ensure_notification_contract, queue_issue_notification,
                    list_owned_notification_receipts, send_owned_notification,
                )
                from integration_contracts import resolve_company_issue_route
                ensure_company_issue_contract()
                ensure_notification_contract()
                # Establish schema before the atomic Issue + Notification queue transaction.
                frappe.db.commit()
                if operation == "reconcile-company-issues":
                    profile = json.loads(Path("/opt/company-identities.json").read_text())
                    route, employee = resolve_company_issue_route(profile)
                    issues = reconcile_disruption_issues(
                        payload["events"], payload["company_binding"],
                        responsible_employee=employee["email"], responsible_role=route.responsible_role,
                        requested_outcome=route.requested_outcome,
                    )
                    for receipt in issues:
                        queue_issue_notification(frappe.get_doc("Issue", receipt["name"]))
                    frappe.db.commit()
                # Only this workflow's exact native queues are dispatched. The
                # site's unrelated scheduled jobs and outbound mail stay untouched.
                send_errors = []
                for receipt in list_owned_notification_receipts():
                    if receipt["status"] != "Sent":
                        try:
                            send_owned_notification(receipt["email_queue"], receipt["issue_key"])
                            frappe.db.commit()
                        except Exception as error:
                            # Native Email Queue retains its own delivery failure.
                            # Keep this issue visible while processing siblings.
                            causes = []
                            cause = error.__cause__ or error.__context__
                            while cause is not None:
                                causes.append(type(cause).__name__)
                                cause = cause.__cause__ or cause.__context__
                            send_errors.append({
                                "issue_key": receipt["issue_key"], "error_type": type(error).__name__,
                                "causes": causes,
                                "stack": [(frame.filename, frame.lineno, frame.name)
                                          for frame in traceback.extract_tb(error.__traceback__)],
                            })
                result = {"notifications": list_owned_notification_receipts(), "errors": send_errors}
        if operation == "reconcile-finance-sources":
            frappe.db.commit()
        return {"operation_id": operation_id, "operation": operation, "result": result}
    except Exception as error:
        if "frappe" in locals() and getattr(frappe, "db", None):
            try:
                frappe.db.rollback()
            except Exception as rollback_error:
                error.add_note(f"Frappe rollback failed: {type(rollback_error).__name__}")
        raise
    finally:
        if "frappe" in locals():
            frappe.destroy()
        os.chdir(BENCH_ROOT)
        os.close(lock_fd)


def _refresh_seed(site: str) -> dict[str, object]:
    manifest_path = RUNTIME_ROOT / f"{site}.json"
    golden_dir = GOLDEN_ROOT / site
    if not manifest_path.is_file() or not (golden_dir / "database.sql.gz").is_file():
        raise FileNotFoundError("Run-owned ERP golden baseline is unavailable")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("site_name") != site or not (SITES_ROOT / site / "site_config.json").is_file():
        raise RuntimeError("Run manifest does not own this ERP site")

    sys.path.insert(0, "/opt/erpnext-admin")
    from seed_a1 import seed

    (BENCH_ROOT / "logs").mkdir(mode=0o700, parents=True, exist_ok=True)
    (SITES_ROOT / site / "logs").mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chdir(SITES_ROOT)
    with contextlib.redirect_stdout(sys.stderr):
        seeded = seed(site, str(RUNTIME_ROOT))
    os.chdir(BENCH_ROOT)

    backup_dir = SITES_ROOT / site / "private" / "backups"
    before = {file.resolve() for file in backup_dir.glob("*")} if backup_dir.exists() else set()
    subprocess.run(
        [str(BENCH_EXECUTABLE), "--site", site, "backup", "--with-files"],
        check=True,
        stdout=sys.stderr,
        stderr=sys.stderr,
        timeout=900,
    )
    backups = [file for file in backup_dir.glob("*") if file.resolve() not in before]
    name_map = {
        "-database.sql.gz": "database.sql.gz",
        "-public-files.tar": "public-files.tar",
        "-private-files.tar": "private-files.tar",
    }
    copied: dict[str, str] = {}
    for backup in backups:
        for suffix, target_name in name_map.items():
            if backup.name.endswith(suffix):
                destination = golden_dir / f".{target_name}.refresh"
                shutil.copyfile(backup, destination)
                destination.chmod(0o600)
                os.replace(destination, golden_dir / target_name)
                copied[target_name] = hashlib.sha256((golden_dir / target_name).read_bytes()).hexdigest()
                break
    if "database.sql.gz" not in copied:
        raise RuntimeError("Native seed refresh did not produce a database archive")
    hashes = json.loads(manifest.get("golden_files_sha256", "{}"))
    hashes.update(copied)
    manifest.update(seeded)
    manifest["golden_files_sha256"] = json.dumps(hashes, sort_keys=True)
    temporary = manifest_path.with_suffix(".json.refresh")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump(manifest, output, sort_keys=True)
        output.write("\n")
    os.replace(temporary, manifest_path)
    return public_binding(manifest)


def main() -> None:
    Path("/home/frappe/logs").mkdir(mode=0o700, parents=True, exist_ok=True)
    parser = argparse.ArgumentParser(description="Manage coordinator-owned ERPNext sites and native task imports")
    parser.add_argument("operation", choices=("create", "reset", "probe", "refresh-seed", "ensure-erp-contract", "seed-tau-retail", "observe-tau-retail", "reconcile-finance-sources", "read-due-obligations", "reconcile-company-issues", "company-issue-notifications", "company-evidence-setup", "reconcile-ecommerce-finance", "capture-evaluation-evidence"))
    parser.add_argument("run_id", help="run UUID or τ experiment UUID")
    parser.add_argument("task_id", nargs="?", help="τ retail task id for seed-tau-retail")
    args = parser.parse_args()
    if args.operation in ("seed-tau-retail", "observe-tau-retail"):
        if not args.task_id:
            parser.error("seed-tau-retail requires a task_id")
        result = (_seed_tau_retail(args.run_id, args.task_id) if args.operation == "seed-tau-retail"
                  else _observe_tau_retail(args.run_id, args.task_id))
        print(json.dumps(result, sort_keys=True))
        return
    if args.operation in ("reconcile-finance-sources", "read-due-obligations", "reconcile-company-issues", "company-issue-notifications", "company-evidence-setup", "reconcile-ecommerce-finance", "capture-evaluation-evidence"):
        result = _run_finance_source_operation(args.run_id, args.operation)
        print(json.dumps(result, sort_keys=True))
        return
    if args.task_id:
        parser.error("task_id is valid only for seed-tau-retail")
    site = site_for_run(args.run_id)
    lock_fd = lock_for(site)
    try:
        if args.operation == "create":
            result = _create(site)
        elif args.operation == "reset":
            result = _reset(site)
        elif args.operation == "refresh-seed":
            result = _refresh_seed(site)
        elif args.operation == "ensure-erp-contract":
            result = _ensure_erp_contract(site)
        else:
            sys.path.insert(0, "/opt/erpnext-admin")
            from probe_a1 import probe

            result = probe(site)
        print(json.dumps(result, sort_keys=True))
    finally:
        os.close(lock_fd)


if __name__ == "__main__":
    main()
