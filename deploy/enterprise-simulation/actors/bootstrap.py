#!/usr/bin/env python3
"""Prepare isolated actor environments and private configuration; never start them."""

from __future__ import annotations

import argparse
import grp
import json
import os
from pathlib import Path
import pwd
import secrets
import stat
import subprocess
import sys
import tempfile


ROOT = Path("/data/theagentcompany/enterprise")
SOURCE = ROOT / "source"
ACTORS = ROOT / "actors"
CONFIG = ROOT / "config" / "runtime.json"
CODEX_HOME = Path("/home/company/.codex")
UV_VERSION = "0.9.24"
UV = ROOT / "tooling" / "venv" / "bin" / "uv"


def atomic_write(path: Path, content: str, mode: int, owner: int, group: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.chown(temporary, owner, group)
        os.replace(temporary, path)
        os.chown(path, owner, group)
        path.chmod(mode)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def run(command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    subprocess.run(command, cwd=cwd, env=env, check=True)


def install_uv(owner: pwd.struct_passwd, group_id: int) -> None:
    tooling = ROOT / "tooling"
    tooling.mkdir(parents=True, exist_ok=True)
    os.chown(tooling, 0, group_id)
    tooling.chmod(0o750)
    venv = tooling / "venv"
    if not venv.exists():
        run([sys.executable, "-m", "venv", str(venv)])
        os.chown(venv, owner.pw_uid, group_id)
    requirements = SOURCE / "deploy/enterprise-simulation/actors/uv-requirements.txt"
    run([str(venv / "bin/python"), "-m", "pip", "install", "--no-deps", "--require-hashes",
         "-r", str(requirements)])
    version = subprocess.run([str(UV), "--version"], check=True, text=True,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()
    if version != f"uv {UV_VERSION}":
        raise RuntimeError(f"uv version check failed: expected {UV_VERSION}")
    os.chown(venv, owner.pw_uid, group_id)


def prepare_environment(project: Path, environment: Path, owner: pwd.struct_passwd,
                        group_id: int, cache: Path) -> None:
    environment.parent.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chown(environment.parent, owner.pw_uid, group_id)
    os.chmod(environment.parent, 0o750)
    os.chown(cache, owner.pw_uid, owner.pw_gid)
    os.chmod(cache, 0o700)
    env = {
        "PATH": f"{UV.parent}:/usr/local/bin:/usr/bin:/bin",
        "HOME": owner.pw_dir,
        "UV_CACHE_DIR": str(cache),
        "UV_PROJECT_ENVIRONMENT": str(environment),
        "UV_NO_CONFIG": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    run(["runuser", "-u", owner.pw_name, "--", "env",
         *[f"{key}={value}" for key, value in env.items()], str(UV), "sync", "--locked",
         "--no-dev", "--no-config", "--python", "/usr/bin/python3.12"], cwd=project)


def private_env(path: Path, data: dict[str, str], owner: pwd.struct_passwd) -> None:
    if path.exists():
        info = path.stat()
        if info.st_uid != owner.pw_uid or stat.S_IMODE(info.st_mode) != 0o600:
            raise PermissionError(f"{path.name} must be owned by company and mode 0600")
        saved = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if line and "=" in line:
                key, value = line.split("=", 1)
                saved[key] = value
        for key, value in data.items():
            if key in saved and saved[key] != value:
                raise RuntimeError(f"{path.name} contains a conflicting private setting: {key}")
        data = {**saved, **data}
    body = "".join(f"{key}={value}\n" for key, value in data.items())
    atomic_write(path, body, 0o600, owner.pw_uid, owner.pw_gid)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    if os.geteuid() != 0:
        raise PermissionError("run this bootstrap with sudo")
    owner = pwd.getpwnam("company")
    source_info = SOURCE.stat()
    if not source_info.st_mode & stat.S_IRUSR:
        raise PermissionError("enterprise source is not readable")
    if CONFIG.stat().st_uid != 0 or stat.S_IMODE(CONFIG.stat().st_mode) != 0o600:
        raise PermissionError("runtime.json must remain root-owned and mode 0600")
    codex_info = CODEX_HOME.stat()
    if codex_info.st_uid != owner.pw_uid or not CODEX_HOME.is_dir():
        raise PermissionError("Codex home must be an existing directory owned by company")
    # Tighten only the directory mode. Existing authenticated CLI state is untouched.
    CODEX_HOME.chmod(0o700)

    group_id = grp.getgrnam("company").gr_gid
    os.chown(ROOT, 0, group_id)
    ROOT.chmod(0o750)
    ACTORS.mkdir(parents=True, exist_ok=True)
    for path in (ACTORS, ACTORS / "state", ACTORS / "state/customer", ACTORS / "state/supplier",
                 ACTORS / "secrets", ACTORS / "cache", ACTORS / "venvs"):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chown(path, owner.pw_uid, owner.pw_gid)
        path.chmod(0o700)

    install_uv(owner, group_id)
    prepare_environment(SOURCE / "servers/simulation/tau2_actor_runtime",
                        ACTORS / "venvs/tau2", owner, group_id,
                        ACTORS / "cache/tau2")
    prepare_environment(SOURCE / "third_party/coffeebench/upstream",
                        ACTORS / "venvs/coffeebench", owner, group_id,
                        ACTORS / "cache/coffeebench")

    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    workers = config.setdefault("actor_workers", {})
    if not isinstance(workers, dict):
        raise ValueError("runtime.json actor_workers must be an object")
    customer_token = workers.get("customer_token") or secrets.token_urlsafe(48)
    supplier_token = workers.get("supplier_token") or secrets.token_urlsafe(48)
    workers.update({
        "customer_url": "http://127.0.0.1:8141",
        "customer_token": customer_token,
        "supplier_url": "http://127.0.0.1:8142",
        "supplier_token": supplier_token,
        "request_timeout_seconds": 600,
    })
    atomic_write(CONFIG, json.dumps(config, indent=2, sort_keys=True) + "\n", 0o600, 0, 0)

    private_env(ACTORS / "secrets/customer.env", {
        "TAU2_CUSTOMER_JOB_DB": str(ACTORS / "state/customer/turns.sqlite"),
        "TAU2_ACTOR_BEARER_TOKEN": customer_token,
        "TAU2_CUSTOMER_MODEL": "gpt-6-luna",
        "TAU2_CODEX_EXECUTABLE": "/usr/local/bin/codex",
        "TAU2_CODEX_HOME": str(CODEX_HOME),
        "TAU2_CODEX_TIMEOUT_SECONDS": "180",
    }, owner)
    private_env(ACTORS / "secrets/supplier.env", {
        "ACTOR_WORKER_DATABASE_PATH": str(ACTORS / "state/supplier/turns.sqlite"),
        "ACTOR_WORKER_BEARER_SECRET": supplier_token,
        "CODEX_MODEL": "gpt-6-luna",
        "CODEX_EXECUTABLE": "/usr/local/bin/codex",
        "CODEX_HOME": str(CODEX_HOME),
        "CODEX_TIMEOUT_SECONDS": "180",
    }, owner)
    print(f"Actor environments prepared with uv {UV_VERSION}; service units are not installed or started.")
    print("Private actor settings written; credentials are not displayed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
