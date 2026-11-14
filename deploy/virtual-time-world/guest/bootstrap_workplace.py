#!/usr/bin/env python3
"""Prepare fresh guest-local TLS and workplace Compose configuration."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import posixpath
import re
import secrets
import grp
import shutil
import subprocess
import sys
import tempfile
import tarfile
import uuid
from pathlib import Path
from typing import Any


PROFILE_PATH = Path("/etc/company-workplace/deployment-profile.json")
TLS_DIR = Path("/etc/company-clock-tls")
ENV_PATH = Path("/etc/virtual-time-world/company-clock-caddy.env")
CADDY_PATH = Path("/etc/caddy/CompanyClockCaddyfile")
ROOT_TRUST_PATH = Path("/usr/local/share/ca-certificates/company-clock-root.crt")
DOMAINS = ("owncloud", "chat", "plane", "mail", "erp", "midpoint", "experiments")
HOST_RE = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?$")


def _write(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _profile(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read deployment profile {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError("Deployment profile must be a JSON object")
    domains = value.get("domains")
    if not isinstance(domains, dict):
        raise RuntimeError("Deployment profile must include a domains object")
    missing = [name for name in DOMAINS if not isinstance(domains.get(name), str) or not domains[name]]
    if missing:
        raise RuntimeError("Deployment profile is missing domains: " + ", ".join(missing))
    if not isinstance(value.get("sandbox_password"), str) or not value["sandbox_password"]:
        raise RuntimeError("Deployment profile must include sandbox_password")
    public_ip = value.get("public_ip")
    if not isinstance(public_ip, str) or not public_ip:
        raise RuntimeError("Deployment profile must include public_ip")
    try:
        value["public_ip"] = str(ipaddress.ip_address(public_ip))
    except ValueError as exc:
        raise RuntimeError("public_ip must be a valid IP address") from exc
    normalized: dict[str, str] = {}
    for name in DOMAINS:
        host = domains[name].strip().rstrip(".")
        if len(host) > 253 or not HOST_RE.fullmatch(host) or ".." in host:
            raise RuntimeError(f"Invalid hostname in domains.{name}")
        normalized[name] = host.lower()
    return {**value, "domains": normalized}


def _create_tls(domains: dict[str, str], public_ip: str | None) -> tuple[Path, Path, Path]:
    TLS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    generations = TLS_DIR / "generations"
    generations.mkdir(mode=0o750, exist_ok=True)
    current = TLS_DIR / "current"
    names = ("root-ca.key", "root-ca.crt", "server.key", "server.crt", "fullchain.pem")
    if not current.is_symlink():
        stage = Path(tempfile.mkdtemp(prefix=".tls-stage-", dir=generations))
        try:
            san = [f"DNS:{name}" for name in sorted(set(domains.values()))]
            if public_ip:
                san.append(f"IP:{ipaddress.ip_address(public_ip)}")
            ext = stage / "server.ext"
            ext.write_text("basicConstraints=critical,CA:FALSE\n"
                           "keyUsage=critical,digitalSignature,keyEncipherment\n"
                           "extendedKeyUsage=serverAuth\n"
                           f"subjectAltName={','.join(san)}\n", encoding="ascii")
            ca_key, ca_cert = stage / names[0], stage / names[1]
            server_key, server_cert = stage / names[2], stage / names[3]
            csr = stage / "server.csr"
            subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:3072", "-nodes", "-sha256",
                 "-days", "3650", "-subj", "/CN=Company Clock Root CA", "-keyout", str(ca_key),
                 "-out", str(ca_cert), "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
                 "-addext", "keyUsage=critical,keyCertSign,cRLSign"], check=True, stdout=subprocess.DEVNULL,
            )
            subprocess.run(
                ["openssl", "req", "-new", "-newkey", "rsa:3072", "-nodes", "-sha256",
                 "-subj", "/CN=Company Clock Services", "-keyout", str(server_key), "-out", str(csr)],
                check=True, stdout=subprocess.DEVNULL,
            )
            subprocess.run(
                ["openssl", "x509", "-req", "-in", str(csr), "-CA", str(ca_cert),
                 "-CAkey", str(ca_key), "-CAcreateserial", "-days", "3650", "-sha256",
                 "-extfile", str(ext), "-out", str(server_cert)], check=True, stdout=subprocess.DEVNULL,
            )
            (stage / names[4]).write_bytes(server_cert.read_bytes() + ca_cert.read_bytes())
            (stage / "server.ext").unlink()
            csr.unlink()
            (stage / "root-ca.srl").unlink(missing_ok=True)
            for path, mode in ((ca_key, 0o600), (ca_cert, 0o644), (server_key, 0o640),
                               (server_cert, 0o644), (stage / names[4], 0o644)):
                path.chmod(mode)
            generation = generations / uuid.uuid4().hex
            os.replace(stage, generation)
            staged_link = TLS_DIR / f".current-{uuid.uuid4().hex}"
            staged_link.symlink_to(generation.relative_to(TLS_DIR))
            os.replace(staged_link, current)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    generation_dir = current.resolve(strict=True)
    if not all((generation_dir / name).is_file() for name in names):
        raise RuntimeError("The active TLS generation is incomplete")
    for name in names:
        alias = TLS_DIR / name
        if not alias.is_symlink() or os.readlink(alias) != f"current/{name}":
            staged_link = TLS_DIR / f".{name}.{uuid.uuid4().hex}"
            staged_link.symlink_to(f"current/{name}")
            os.replace(staged_link, alias)
    root_key, root_cert, leaf_key, leaf_cert, chain = (TLS_DIR / name for name in names)
    caddy_gid = grp.getgrnam("caddy").gr_gid
    TLS_DIR.chmod(0o750)
    os.chown(TLS_DIR, 0, caddy_gid)
    generations.chmod(0o750)
    os.chown(generations, 0, caddy_gid)
    os.chmod(generation_dir, 0o750)
    os.chown(generation_dir, 0, caddy_gid)
    root_key.chmod(0o600)
    os.chown(root_key, 0, 0)
    leaf_key.chmod(0o640)
    os.chown(leaf_key, 0, caddy_gid)
    for path in (root_cert, leaf_cert, chain):
        path.chmod(0o644)
    trust_matches = ROOT_TRUST_PATH.is_file() and root_cert.read_bytes() == ROOT_TRUST_PATH.read_bytes()
    if not trust_matches:
        _write(ROOT_TRUST_PATH, root_cert.read_text(encoding="ascii"), 0o644)
    subprocess.run(["update-ca-certificates"], check=True)
    for host in sorted(set(domains.values())):
        checked = subprocess.run(["openssl", "x509", "-in", str(leaf_cert), "-noout", "-checkhost", host],
                                 capture_output=True, text=True)
        if checked.returncode:
            raise RuntimeError(f"Persisted TLS certificate does not cover configured hostname {host}")
    if public_ip:
        checked = subprocess.run(["openssl", "x509", "-in", str(leaf_cert), "-noout", "-checkip", public_ip],
                                 capture_output=True, text=True)
        if checked.returncode:
            raise RuntimeError("Persisted TLS certificate does not cover configured public_ip")
    return root_cert, chain, leaf_key


def _configure_caddy(profile: dict[str, Any], source: Path, root_cert: Path, chain: Path, key: Path) -> None:
    domains = profile["domains"]
    env = {
        "COMPANY_CLOCK_TLS_CERT": str(chain),
        "COMPANY_CLOCK_TLS_KEY": str(key),
        "TAC_CLOCK_OWNCLOUD_HOST": domains["owncloud"],
        "TAC_CLOCK_CHAT_HOST": domains["chat"],
        "TAC_CLOCK_PLANE_HOST": domains["plane"],
        "TAC_CLOCK_MAIL_HOST": domains["mail"],
        "TAC_CLOCK_ERP_HOST": domains["erp"],
        "TAC_CLOCK_MIDPOINT_HOST": domains["midpoint"],
        "TAC_CLOCK_EXPERIMENTS_HOST": domains["experiments"],
        "TAC_CLOCK_OWNCLOUD_UPSTREAM_HOST": domains["owncloud"],
        "TAC_CLOCK_ERP_UPSTREAM_HOST": domains["erp"],
    }
    _write(ENV_PATH, "".join(f"{name}={value}\n" for name, value in env.items()), 0o600)
    _write(TLS_DIR / 'client-trust.env',
           (source / 'deploy/virtual-time-world/guest/client-trust.env').read_text(), 0o600)
    template = source / "deploy/virtual-time-world/guest/CompanyClockCaddyfile.template"
    if not template.is_file():
        raise RuntimeError(f"Missing Caddy template: {template}")
    _write(CADDY_PATH, template.read_text(encoding="utf-8"), 0o644)
    # The root certificate is installed into the guest trust store; this return
    # also makes the intended persistent location explicit to callers.
    del root_cert


def _configure_native_hosts(domains: dict[str, str]) -> None:
    hosts_path = Path("/etc/hosts")
    current = hosts_path.read_text(encoding="utf-8")
    begin, end = "# BEGIN company workplace domains", "# END company workplace domains"
    block = f"{begin}\n127.0.0.1 {' '.join(domains.values())}\n{end}"
    start = current.find(begin)
    finish = current.find(end, start + len(begin)) if start >= 0 else -1
    if start >= 0 and finish >= 0:
        finish += len(end)
        updated = current[:start] + block + current[finish:]
    elif start >= 0 or end in current:
        raise RuntimeError("The managed company hosts block is incomplete")
    else:
        updated = current.rstrip() + "\n\n" + block + "\n"
    if updated != current:
        _write(hosts_path, updated, hosts_path.stat().st_mode & 0o777)


def _write_compose_config(compose_file: Path, project_dir: Path, project: str,
                          env_file: Path, destination: Path) -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["docker", "compose", "--project-name", project, "--project-directory", str(project_dir),
             "--env-file", str(env_file), "--file", str(compose_file), "config", "--format", "json"],
            check=True, capture_output=True, text=True,
        )
        config = json.loads(result.stdout)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        detail = getattr(exc, "stderr", None)
        suffix = f": {detail.strip()}" if detail and detail.strip() else ""
        raise RuntimeError(f"Could not resolve Compose project {project}{suffix}") from exc
    config["name"] = project
    _write(destination, json.dumps(config, indent=2, sort_keys=True) + "\n", 0o600)
    return config


def _loopback_port(service: dict[str, Any], host: int, target: int) -> None:
    service["ports"] = [{"host_ip": "127.0.0.1", "published": str(host),
                         "target": target, "protocol": "tcp"}]


def _report_cleanup_failure(operation: str, error: BaseException) -> None:
    message = f"workplace bootstrap cleanup failed ({operation}, {type(error).__name__})"
    try:
        print(message, file=sys.stderr)
    except OSError:
        try:
            os.write(2, b"workplace bootstrap cleanup diagnostic could not be written\n")
        except OSError:
            pass


def _add_network(service: dict[str, Any], name: str) -> None:
    networks = service.get("networks")
    if networks is None:
        networks = {"default": {}}
    elif isinstance(networks, list):
        networks = {item: {} for item in networks}
    networks[name] = {}
    service["networks"] = networks


def _prepare_app_compose(source: Path, profile: dict[str, Any], output: Path) -> list[Path]:
    domains = profile["domains"]
    sandbox_password = profile["sandbox_password"]
    secret_dir = Path("/etc/company-workplace/secrets")
    secret_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    secret_dir.chmod(0o700)
    secret_names = ("plane-db", "plane-secret-key", "minio-access", "minio-secret", "midpoint-db", "midpoint-admin")
    secret_values: dict[str, str] = {}
    for name in secret_names:
        path = secret_dir / name
        if path.exists():
            if not path.is_file() or path.stat().st_mode & 0o077:
                raise RuntimeError(f"Secret file {path} must be a regular owner-only file")
            value = path.read_text(encoding="utf-8").rstrip("\n")
            if not value:
                raise RuntimeError(f"Secret file {path} is empty")
        else:
            value = secrets.token_urlsafe(36)
            _write(path, value + "\n", 0o600)
        secret_values[name] = value

    app_dir = output / "app-env"
    app_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    app_dir.chmod(0o700)
    base_env = app_dir / "base.env"
    _write(base_env, "HOST_PORT=3000\nPORT=3000\nBIND_IP=127.0.0.1\n"
                      f"ROOT_URL=https://{domains['chat']}/\nMONGODB_VERSION=5.0\n"
                      "ALLOW_EMPTY_PASSWORD=yes\nMONGODB_ENABLE_JOURNAL=true\n", 0o600)
    base_path = output / "tac-workplace.json"
    base = _write_compose_config(source / "servers/docker-compose.yml", source,
                                 "tac-workplace", base_env, base_path)
    services = base["services"]
    gateway_hosts = [f"{host}:host-gateway" for host in sorted(set(domains.values()) | {"host.docker.internal"})]
    services["mongodb"]["environment"]["MONGODB_EXTRA_FLAGS"] = "--setParameter ttlMonitorEnabled=false"
    owncloud = services["owncloud"]
    owncloud["environment"].update({
        "OWNCLOUD_DOMAIN": f"{domains['owncloud']}:8092",
        "OWNCLOUD_TRUSTED_DOMAINS": f"localhost,{domains['owncloud']},{domains['owncloud']}:8092",
        "OWNCLOUD_ADMIN_USERNAME": "theagentcompany", "OWNCLOUD_ADMIN_PASSWORD": sandbox_password,
    })
    _loopback_port(owncloud, 8092, 80)
    owncloud["extra_hosts"] = gateway_hosts
    _add_network(owncloud, "provisioning")
    _loopback_port(services["owncloud-collabora"], 9980, 9980)
    services["owncloud-collabora"]["extra_hosts"] = gateway_hosts
    _add_network(services["owncloud-collabora"], "provisioning")
    chat = services["rocketchat"]
    chat["environment"].update({
        "ADMIN_USERNAME": "theagentcompany", "ADMIN_PASS": sandbox_password,
        "ADMIN_EMAIL": f"theagentcompany@{domains['mail']}", "ADMIN_NAME": "theagentcompany",
        "ROOT_URL": f"https://{domains['chat']}/", "PORT": "3000",
    })
    chat["extra_hosts"] = gateway_hosts
    _add_network(chat, "provisioning")
    _loopback_port(chat, 3000, 3000)
    redis = services["redis-stack"]
    # Preserve the exact credentials expected by the supplied NPC seed image.
    _loopback_port(redis, 6379, 6379)
    services["mongodb"]["healthcheck"] = {
        "test": ["CMD-SHELL", "mongosh --quiet --eval 'quit(db.adminCommand({ping:1}).ok ? 0 : 1)'"],
        "interval": "2s", "timeout": "3s", "retries": 60,
    }
    base["networks"]["provisioning"] = {"name": "tac-company-provisioning", "driver": "bridge"}
    _write(base_path, json.dumps(base, indent=2, sort_keys=True) + "\n", 0o600)

    plane_env = app_dir / "plane.env"
    plane_values = {
        "NGINX_PORT": "8091", "WEB_URL": f"https://{domains['plane']}",
        "ADMIN_BASE_URL": f"https://{domains['plane']}", "SPACE_BASE_URL": f"https://{domains['plane']}",
        "APP_BASE_URL": f"https://{domains['plane']}", "CORS_ALLOWED_ORIGINS": f"https://{domains['plane']}",
        "POSTGRES_USER": "plane", "POSTGRES_PASSWORD": secret_values["plane-db"], "POSTGRES_DB": "plane",
        "DATABASE_URL": f"postgresql://plane:{secret_values['plane-db']}@plane-db/plane",
        "SECRET_KEY": secret_values["plane-secret-key"], "USE_MINIO": "1",
        "AWS_ACCESS_KEY_ID": secret_values["minio-access"], "AWS_SECRET_ACCESS_KEY": secret_values["minio-secret"],
        "MINIO_ROOT_USER": secret_values["minio-access"], "MINIO_ROOT_PASSWORD": secret_values["minio-secret"],
        "AWS_S3_ENDPOINT_URL": "http://plane-minio:9000", "AWS_S3_BUCKET_NAME": "uploads",
        "BUCKET_NAME": "uploads", "DEBUG": "0", "SENTRY_DSN": "",
    }
    _write(plane_env, "".join(f"{key}={value}\n" for key, value in plane_values.items()), 0o600)
    plane_path = output / "tac-plane.json"
    plane = _write_compose_config(source / "deploy/single-host/plane.compose.yml", source,
                                  "tac-plane", plane_env, plane_path)
    plane["services"]["plane-minio"]["image"] = "minio/minio:RELEASE.2024-11-07T00-52-20Z"
    plane["services"]["plane-minio"].pop("pull_policy", None)
    for service in plane["services"].values():
        service["extra_hosts"] = gateway_hosts
    _loopback_port(plane["services"]["proxy"], 8091, 80)
    plane["services"]["plane-minio-init"] = {
        "image": "minio/mc:RELEASE.2024-11-21T17-21-54Z",
        "depends_on": {"plane-minio": {"condition": "service_started"}},
        "entrypoint": ["/bin/sh", "-ec"],
        "command": [f"mc alias set local http://plane-minio:9000 {secret_values['minio-access']} "
                    f"{secret_values['minio-secret']} && mc mb --ignore-existing local/uploads"],
        "restart": "on-failure:10",
    }
    _write(plane_path, json.dumps(plane, indent=2, sort_keys=True) + "\n", 0o600)

    midpoint_dir = source / "deploy/single-host/midpoint"
    midpoint_env = app_dir / "midpoint.env"
    midpoint_secrets = output / "midpoint-secrets"
    midpoint_secrets.mkdir(mode=0o700, parents=True, exist_ok=True)
    midpoint_secrets.chmod(0o700)
    for name in ("midpoint-db", "midpoint-admin"):
        _write(midpoint_secrets / name, secret_values[name] + "\n", 0o600)
    _write(midpoint_env,
           f"MIDPOINT_PORT=18121\nMIDPOINT_DB_PASSWORD_FILE={midpoint_secrets / 'midpoint-db'}\n"
           f"MIDPOINT_ADMIN_PASSWORD_FILE={midpoint_secrets / 'midpoint-admin'}\n", 0o600)
    midpoint_path = output / "tac-midpoint.json"
    midpoint = _write_compose_config(midpoint_dir / "compose.yml", midpoint_dir,
                                     "tac-midpoint", midpoint_env, midpoint_path)
    midpoint["services"]["midpoint"]["extra_hosts"] = gateway_hosts
    _add_network(midpoint["services"]["midpoint"], "provisioning")
    midpoint["networks"]["provisioning"] = {"name": "tac-company-provisioning", "external": True}
    _write(midpoint_path, json.dumps(midpoint, indent=2, sort_keys=True) + "\n", 0o600)
    return [base_path, plane_path, midpoint_path]


def _extract_seed_files() -> dict[str, Any]:
    image = "ghcr.io/theagentcompany/servers-api-server:1.0.0"
    seed_container = f"tac-seed-export-{uuid.uuid4().hex[:12]}"
    scratch_parent = Path("/data/theagentcompany/.bootstrap-tmp")
    scratch_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    scratch_parent.chmod(0o700)
    for stale in scratch_parent.glob("tac-workplace-seed-*"):
        if stale.is_dir():
            shutil.rmtree(stale)
        else:
            stale.unlink()
    temp_dir = Path(tempfile.mkdtemp(prefix="tac-workplace-seed-", dir=scratch_parent))
    container_created = False
    try:
        subprocess.run(["docker", "pull", image], check=True)
        subprocess.run(["docker", "create", "--name", seed_container, image], check=True,
                       stdout=subprocess.DEVNULL)
        container_created = True
        result = {}
        for name, remote_path in (
            ("rocketchat-db.dump", "/rocketchat/db.dump"),
            ("plane-pgdata.tar.gz", "/plane/pgdata.tar.gz"),
            ("plane-redisdata.tar.gz", "/plane/redisdata.tar.gz"),
            ("plane-uploads.tar.gz", "/plane/uploads.tar.gz"),
        ):
            destination = temp_dir / name
            subprocess.run(["docker", "cp", f"{seed_container}:{remote_path}", str(destination)], check=True)
            if not destination.is_file() or destination.stat().st_size == 0:
                raise RuntimeError(f"Seed image artifact is missing or empty: {remote_path}")
            result[name] = destination
        # Validate archive paths before native extraction from the pinned seed image.
        for filename, expected_root in (("plane-pgdata.tar.gz", "pgdata"),
                                        ("plane-redisdata.tar.gz", "redisdata"),
                                        ("plane-uploads.tar.gz", "uploads")):
            with tarfile.open(result[filename], "r:gz") as archive:
                roots = set()
                for member in archive.getmembers():
                    path = Path(member.name)
                    if path.is_absolute() or ".." in path.parts:
                        raise RuntimeError(f"Seed archive {filename} contains an unsafe path")
                    if member.issym() or member.islnk():
                        link = Path(member.linkname)
                        link_base = path.parent if member.issym() else Path()
                        resolved_link = Path(posixpath.normpath(str(link_base / link)))
                        if link.is_absolute() or not resolved_link.parts or resolved_link.parts[0] != expected_root:
                            raise RuntimeError(f"Seed archive {filename} contains an unsafe link")
                    if path.parts:
                        roots.add(path.parts[0])
                if roots != {expected_root}:
                    raise RuntimeError(f"Seed archive {filename} has an unsupported directory layout")
        # The scratch container is removed before the temporary artifact directory is released.
        subprocess.run(["docker", "rm", seed_container], check=True, stdout=subprocess.DEVNULL)
        return result | {"_scratch": temp_dir}
    except BaseException:
        if container_created:
            try:
                subprocess.run(["docker", "rm", "--force", seed_container], check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except BaseException as cleanup_error:
                _report_cleanup_failure("seed container removal", cleanup_error)
        try:
            shutil.rmtree(temp_dir)
        except BaseException as cleanup_error:
            _report_cleanup_failure("seed artifact directory removal", cleanup_error)
        raise


def _restore_plane_archives(config: Path, artifacts: dict[str, Path], state_dir: Path) -> None:
    marker = state_dir / "plane-seed-restored"
    if marker.exists():
        return
    # `create` gives the empty named volumes their normal Compose ownership labels.
    subprocess.run(["docker", "compose", "--file", str(config), "create", "plane-db", "plane-redis", "plane-minio"], check=True)
    project = json.loads(config.read_text(encoding="utf-8"))
    for logical, archive_name, source_root in (
        ("pgdata", "plane-pgdata.tar.gz", "pgdata"),
        ("redisdata", "plane-redisdata.tar.gz", "redisdata"),
        ("uploads", "plane-uploads.tar.gz", "uploads"),
    ):
        volume = project["volumes"][logical]
        name = volume.get("name", f"{project['name']}_{logical}")
        mountpoint = Path(subprocess.run(
            ["docker", "volume", "inspect", "--format", "{{.Mountpoint}}", name],
            check=True, capture_output=True, text=True,
        ).stdout.strip())
        if not mountpoint.is_dir():
            raise RuntimeError(f"Plane volume {name} has no available mountpoint")
        unexpected = [entry.name for entry in mountpoint.iterdir() if entry.name != "lost+found"]
        if unexpected and not marker.exists():
            # A missing restore marker means this bootstrap did not finish the
            # archive copy. Reapplying the pinned archive is safe for the fresh,
            # Compose-owned volume and makes interrupted startup resumable.
            for entry in mountpoint.iterdir():
                if entry.name == "lost+found":
                    continue
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
        extract_root = artifacts["_scratch"] / f"extract-{source_root}"
        extract_root.mkdir(mode=0o700)
        with tarfile.open(artifacts[archive_name], "r:gz") as archive:
            # Preserve the upstream volume ownership from these trusted, pinned
            # seed-image archives; tar's safety filter still rejects path escapes.
            archive.extractall(extract_root, filter="tar")
        data_root = extract_root / source_root
        if not data_root.is_dir() or not any(data_root.iterdir()):
            raise RuntimeError(f"Plane seed archive {archive_name} does not contain data")
        subprocess.run(["cp", "--archive", f"{data_root}/.", f"{mountpoint}/"], check=True)
    _write(marker, "restored from pinned standard seed image\n", 0o600)


def _restore_rocketchat(config: Path, artifacts: dict[str, Path], state_dir: Path) -> None:
    marker = state_dir / "rocketchat-seed-restored"
    if marker.exists():
        return
    subprocess.run(["docker", "compose", "--file", str(config), "up", "--detach", "mongodb", "--wait"], check=True)
    # The stock Mongo image includes mongorestore. Restore the supplied archive into
    # the fresh database before the Rocket.Chat service creates its default admin.
    with artifacts["rocketchat-db.dump"].open("rb") as archive:
        subprocess.run(
            ["docker", "compose", "--file", str(config), "exec", "-T", "mongodb",
             "mongorestore", "--drop", "--archive"],
            stdin=archive, check=True,
        )
    _write(marker, "restored from pinned standard seed image\n", 0o600)


def _preflight_projects(configs: list[Path]) -> bool:
    state_dir = Path("/etc/company-workplace/state")
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_dir.chmod(0o700)
    bootstrap_state = state_dir / "workplace-bootstrap-started"
    initialized_state = state_dir / "midpoint-initialized"
    known_volumes = set(subprocess.run(["docker", "volume", "ls", "--format", "{{.Name}}"],
                                       check=True, capture_output=True, text=True).stdout.splitlines())
    any_existing = False
    for path in configs:
        config = json.loads(path.read_text(encoding="utf-8"))
        project = config.get("name")
        if not isinstance(project, str) or not project:
            raise RuntimeError(f"Resolved Compose config has no project name: {path}")
        for logical, volume in config.get("volumes", {}).items():
            name = volume.get("name", f"{project}_{logical}")
            if name not in known_volumes:
                continue
            any_existing = True
            inspected = subprocess.run(
                ["docker", "volume", "inspect", "--format", "{{json .Labels}}", name],
                check=True, capture_output=True, text=True,
            )
            labels = json.loads(inspected.stdout)
            if labels.get("com.docker.compose.project") != project or \
                    labels.get("com.docker.compose.volume") != logical:
                raise RuntimeError(f"Refusing to use unowned Compose volume {name}")
        container_ids = subprocess.run(
            ["docker", "compose", "--file", str(path), "ps", "--all", "--quiet"],
            check=True, capture_output=True, text=True,
        ).stdout.splitlines()
        if container_ids:
            any_existing = True
            inspected = subprocess.run(
                ["docker", "inspect", *container_ids], check=True, capture_output=True, text=True,
            )
            containers = json.loads(inspected.stdout)
            for container in containers:
                labels = container.get("Config", {}).get("Labels") or {}
                service_name = labels.get("com.docker.compose.service")
                if labels.get("com.docker.compose.project") != project or service_name not in expected:
                    raise RuntimeError(f"Refusing to reuse an unowned container in project {project}")
    if not bootstrap_state.exists() and any_existing:
        raise RuntimeError("Fresh workplace bootstrap found pre-existing project state without its ownership record")
    if not bootstrap_state.exists():
        _write(bootstrap_state, "projects are owned by bootstrap_workplace.py\n", 0o600)
    midpoint_config = json.loads(configs[2].read_text(encoding="utf-8"))
    if initialized_state.exists():
        midpoint_volumes = midpoint_config.get("volumes", {})
        required = {volume.get("name") for volume in midpoint_volumes.values()}
        if not required.issubset(known_volumes):
            raise RuntimeError("midPoint initialization marker exists but its owned storage is missing")
    base_config = json.loads(configs[0].read_text(encoding="utf-8"))
    plane_config = json.loads(configs[1].read_text(encoding="utf-8"))
    if (state_dir / "rocketchat-seed-restored").exists():
        mongo_volume = base_config["volumes"]["mongodb_data"].get("name")
        if mongo_volume not in known_volumes:
            raise RuntimeError("Rocket.Chat seed marker exists but its owned database volume is missing")
    if (state_dir / "plane-seed-restored").exists():
        plane_volumes = plane_config.get("volumes", {})
        required = {volume.get("name") for volume in plane_volumes.values()
                    if volume.get("name") and volume.get("name").endswith(("_pgdata", "_redisdata", "_uploads"))}
        if len(required) != 3 or not required.issubset(known_volumes):
            raise RuntimeError("Plane seed marker exists but one or more owned data volumes are missing")
    return initialized_state.exists()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, default=PROFILE_PATH)
    parser.add_argument("--source", type=Path, default=Path("/opt/agentic-world/source"))
    parser.add_argument("--compose-dir", type=Path, default=Path("/etc/company-workplace/compose"))
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("must run as root")
    profile = _profile(args.profile)
    root_cert, chain, key = _create_tls(profile["domains"], profile.get("public_ip"))
    _configure_caddy(profile, args.source, root_cert, chain, key)
    _configure_native_hosts(profile["domains"])
    args.compose_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    args.compose_dir.chmod(0o700)
    configs = _prepare_app_compose(args.source, profile, args.compose_dir)
    application_services = Path('/etc/company-workplace/application-services.json')
    if not application_services.exists():
        mapping = json.loads((args.source / 'deploy/virtual-time-world/application-services.example.json').read_text())
        for group in mapping:
            group['compose_file'] = str(args.compose_dir / Path(group['compose_file']).name)
        _write(application_services, json.dumps(mapping, indent=2) + '\n', 0o600)
    midpoint_initialized = _preflight_projects(configs)
    state_dir = Path("/etc/company-workplace/state")
    seed_markers = (state_dir / "rocketchat-seed-restored", state_dir / "plane-seed-restored")
    if not all(marker.exists() for marker in seed_markers):
        artifacts = _extract_seed_files()
        try:
            _restore_plane_archives(configs[1], artifacts, state_dir)
            _restore_rocketchat(configs[0], artifacts, state_dir)
        except BaseException:
            try:
                shutil.rmtree(artifacts["_scratch"])
            except BaseException as cleanup_error:
                _report_cleanup_failure("seed artifact directory removal", cleanup_error)
            raise
        else:
            shutil.rmtree(artifacts["_scratch"])
    # Initialize fresh midPoint storage exactly once before the regular project start.
    midpoint = configs[2]
    if not midpoint_initialized:
        subprocess.run(["docker", "compose", "--file", str(midpoint), "up", "--detach", "midpoint-db"], check=True)
        subprocess.run(["docker", "compose", "--file", str(midpoint), "--profile", "initialize",
                        "run", "--rm", "midpoint-init"], check=True)
        _write(Path("/etc/company-workplace/state/midpoint-initialized"), "initialized\n", 0o600)
    for config in configs:
        subprocess.run(["docker", "compose", "--file", str(config), "up", "--detach", "--pull", "always",
                        "--wait"], check=True)
    _configure_owncloud(profile["domains"]["owncloud"], configs[0])
    print("Fresh core workplace Compose projects prepared")
    return 0


def _configure_owncloud(host: str, config: Path) -> None:
    """Set externally visible ownCloud URLs through its native OCC interface."""
    for key, value in (("overwritehost", host), ("overwriteprotocol", "https"),
                       ("overwrite.cli.url", f"https://{host}"), ("trusted_domains 1", host)):
        subprocess.run(
            ["docker", "compose", "--file", str(config), "exec", "-T", "owncloud",
             "occ", "config:system:set", *key.split(), "--value", value],
            check=True,
        )


if __name__ == "__main__":
    raise SystemExit(main())
