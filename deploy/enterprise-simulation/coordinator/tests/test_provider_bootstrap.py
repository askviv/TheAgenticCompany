from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[4]
SPEC = importlib.util.spec_from_file_location(
    "enterprise_coordinator_bootstrap",
    ROOT / "deploy/enterprise-simulation/coordinator/bootstrap.py",
)
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)


class ProviderBootstrapTests(unittest.TestCase):
    @staticmethod
    def compose_config(*, admin_target="/opt/erpnext-admin", named_data_volumes=False):
        source_mounts = {
            "/opt/erpnext-admin": ROOT / "deploy/enterprise-simulation/erpnext/admin",
            "/opt/erpnext-contracts/integration_contracts.py": ROOT / "servers/simulation/integration_contracts.py",
            "/opt/company-identities.json": ROOT / "deploy/single-host/company-identities.json",
            "/opt/erpnext_adapter.py": ROOT / "servers/simulation/erpnext.py",
        }
        data_mounts = {
            "/home/frappe/frappe-bench/sites": "operator_sites" if named_data_volumes else "/operator/managed/sites",
            "/opt/erpnext-golden": "operator_golden" if named_data_volumes else "/operator/managed/golden",
            "/opt/erpnext-runtime": "operator_runtime" if named_data_volumes else "/operator/managed/runtime",
        }
        site_admin = dict(data_mounts)
        site_admin.update({target: str(path) for target, path in source_mounts.items()})
        if admin_target != "/opt/erpnext-admin":
            site_admin[admin_target] = site_admin.pop("/opt/erpnext-admin")

        def volumes(mounts):
            return [{"type": "bind", "source": source, "target": target} for target, source in mounts.items()]

        return {
            "services": {
                "backend": {"volumes": volumes({
                    "/home/frappe/frappe-bench/sites": data_mounts["/home/frappe/frappe-bench/sites"],
                    "/opt/erpnext_adapter.py": str(ROOT / "servers/simulation/erpnext.py"),
                })},
                "frontend": {"volumes": volumes({
                    "/home/frappe/frappe-bench/sites": data_mounts["/home/frappe/frappe-bench/sites"],
                })},
                "db": {"volumes": volumes({"/var/lib/mysql": "operator_db" if named_data_volumes else "/operator/managed/db"})},
                "site-admin": {"volumes": volumes(site_admin)},
            }
        }

    def test_runtime_rebuild_preserves_binding_and_actor_credentials_without_unknown_settings_or_output(self):
        previous = {
            "company_binding": {"site_name": "erp-sim-existing.localhost", "credentials_ref": "private-ref"},
            "actor_workers": {
                "customer_url": "http://127.0.0.1:8141", "customer_token": "CUSTOMER_SECRET_SENTINEL",
                "supplier_url": "http://127.0.0.1:8142", "supplier_token": "SUPPLIER_SECRET_SENTINEL",
                "request_timeout_seconds": 600,
            },
            "background_model_id": "gpt-6-luna",
            "company_background_enabled": True,
            "unknown_legacy_setting": "must-not-carry-forward",
        }
        base = {"state_root": "/private/state", "workers_enabled": True}
        captured_out, captured_err = io.StringIO(), io.StringIO()
        with redirect_stdout(captured_out), redirect_stderr(captured_err):
            result = bootstrap.preserve_runtime_settings(
                base, previous,
                {"site_name": "erp-sim-existing.localhost", "credentials_ref": "private-ref"},
            )
        self.assertEqual(result["company_binding"], previous["company_binding"])
        self.assertEqual(result["actor_workers"], previous["actor_workers"])
        self.assertEqual(result["background_model_id"], "gpt-6-luna")
        self.assertNotIn("unknown_legacy_setting", result)
        self.assertEqual(captured_out.getvalue(), "")
        self.assertEqual(captured_err.getvalue(), "")
        self.assertNotIn("CUSTOMER_SECRET_SENTINEL", captured_out.getvalue() + captured_err.getvalue())

    def test_runtime_rebuild_refuses_replacing_an_existing_company_binding(self):
        with self.assertRaisesRegex(RuntimeError, "differs from the persisted company binding") as error:
            bootstrap.preserve_runtime_settings(
                {"state_root": "/private/state"},
                {"company_binding": {"site_name": "erp-sim-existing.localhost"}},
                {"site_name": "erp-sim-new.localhost"},
            )
        self.assertNotIn("erp-sim-existing", str(error.exception))
        self.assertNotIn("erp-sim-new", str(error.exception))

    def test_managed_company_mail_directory_covers_all_profile_employees_and_native_providers(self):
        profile = json.loads((ROOT / "deploy/single-host/company-identities.json").read_text())
        company = profile["company"]
        added = bootstrap.managed_employee_actors(profile)
        existing_employee_emails = {
            actor["email"].casefold() for actor in bootstrap.ACTORS
            if actor["kind"] in {"employee", "warehouse", "logistics", "approver"}
        }
        configured_emails = existing_employee_emails | {actor["email"].casefold() for actor in added}
        profile_emails = {employee["email"].casefold() for employee in profile["employees"]}
        self.assertEqual(configured_emails, profile_emails)
        self.assertEqual(len(profile_emails), 17)

        accounts, actors = bootstrap.external_provider_mail_directory(company)
        self.assertEqual(set(accounts), {"airline-support", "telecom-support", "bank-support"})
        self.assertEqual({actor["actor_id"] for actor in actors}, set(accounts))
        self.assertTrue(all(row["kind"] == "counterparty" for row in actors))

    def test_one_missing_employee_credential_does_not_remove_other_existing_accounts(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "employee-credentials.json"
            path.write_text(json.dumps({"employees": {
                "employee-one": {"mail": {"address": "one@example.test", "password": "private"}},
            }}))
            path.chmod(0o600)
            actors = [
                {"actor_id": "employee-one", "email": "one@example.test", "kind": "employee"},
                {"actor_id": "employee-two", "email": "two@example.test", "kind": "employee"},
            ]
            result = bootstrap.existing_employee_credentials(Path(temporary), actors)
        self.assertEqual(set(result), {"one@example.test"})
        self.assertEqual(result["one@example.test"]["password"], "private")

    def test_viv_mail_auth_maps_public_actor_to_private_relay_account_without_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "service-credentials.json"
            path.write_text(json.dumps({"accounts": {
                "viv_reply_relay": {
                    "allow_spoofing": False,
                    "enable_imap": False,
                    "mail": {
                        "address": "viv_reply_relay@example.com",
                        "password": "test-password-sentinel",
                        "status": "ready",
                    },
                },
            }}))
            path.chmod(0o600)
            captured_out, captured_err = io.StringIO(), io.StringIO()
            with redirect_stdout(captured_out), redirect_stderr(captured_err):
                credentials = bootstrap.viv_relay_credentials(Path(temporary))
            self.assertEqual(credentials, {
                "username": "viv_reply_relay@example.com",
                "password": "test-password-sentinel",
            })
            self.assertEqual(captured_out.getvalue(), "")
            self.assertEqual(captured_err.getvalue(), "")

    def test_viv_mail_auth_refuses_missing_or_unsafe_relay_account(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "service-credentials.json"
            path.write_text(json.dumps({"accounts": {}}))
            path.chmod(0o600)
            with self.assertRaisesRegex(RuntimeError, "non-spoofing Viv relay credentials"):
                bootstrap.viv_relay_credentials(Path(temporary))

    def test_existing_compose_preserves_operator_volumes_networks_and_sensitive_settings(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "erpnext-compose.yml"
            bootstrap.private_erp_compose(ROOT, destination)
            rendered = destination.read_text()
            customized = rendered.replace(
                "- /data/theagentcompany/enterprise/erpnext/sites:/home/frappe/frappe-bench/sites",
                "- /operator/managed/sites:/home/frappe/frappe-bench/sites",
                1,
            ).replace(
                "name: tac-enterprise-erp",
                "name: tac-enterprise-erp\nnetworks:\n  operator-net:\n    external: true",
                1,
            )
            destination.write_text(customized)
            destination.chmod(0o600)

            with patch.object(bootstrap.subprocess, "run", return_value=SimpleNamespace(
                stdout=json.dumps(self.compose_config(named_data_volumes=True)),
            )) as compose_parser:
                result = bootstrap.private_erp_compose(ROOT, destination)

            self.assertEqual(result, destination)
            self.assertEqual(destination.read_text(), customized)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
            self.assertEqual(compose_parser.call_args.args[0], [
                "docker", "compose", "--file", str(destination), "config", "--format", "json", "--no-interpolate",
            ])
            self.assertEqual(compose_parser.call_args.kwargs, {"check": True, "capture_output": True, "text": True})

    def test_existing_compose_with_changed_source_mount_fails_without_rewriting(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "erpnext-compose.yml"
            bootstrap.private_erp_compose(ROOT, destination)
            customized = destination.read_text().replace(
                ":/opt/erpnext-admin:ro", ":/opt/wrong-admin:ro", 1
            )
            destination.write_text(customized)
            with patch.object(bootstrap.subprocess, "run", return_value=SimpleNamespace(
                stdout=json.dumps(self.compose_config(admin_target="/opt/wrong-admin")),
            )):
                with self.assertRaisesRegex(RuntimeError, "explicit migration is required"):
                    bootstrap.private_erp_compose(ROOT, destination)
            self.assertEqual(destination.read_text(), customized)


if __name__ == "__main__":
    unittest.main()
