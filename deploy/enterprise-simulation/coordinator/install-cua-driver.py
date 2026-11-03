#!/usr/bin/env python3
"""Install the pinned upstream Cua Driver binary for employee desktops."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
import shutil
import sys
import tarfile
import tempfile
from urllib.request import Request, urlopen


VERSION = "0.38.0"
ASSET = f"cua-driver-rs-{VERSION}-linux-x86_64-binary.tar.gz"
URL = f"https://github.com/trycua/cua/releases/download/cua-driver-rs-v{VERSION}/{ASSET}"
SHA256 = "31ab9709dd91ce73677e49b9f3e75c6f0d3e4d9625c286c9e58262bfbea698dd"
DEFAULT_DESTINATION = Path("/data/theagentcompany/enterprise/tools/cua-driver/bin/cua-driver")


def download_archive(path: Path) -> None:
    request = Request(URL, headers={"User-Agent": "theagentcompany-cua-driver-installer/1"})
    with urlopen(request) as response, path.open("wb") as output:
        shutil.copyfileobj(response, output)
        output.flush()
        os.fsync(output.fileno())
    with path.open("rb") as downloaded:
        if hashlib.file_digest(downloaded, "sha256").hexdigest() != SHA256:
            raise RuntimeError("Cua Driver archive SHA-256 did not match the pinned release")


def extract_binary(archive_path: Path, destination: Path) -> None:
    with tarfile.open(archive_path, mode="r:gz") as archive:
        matches = [
            member for member in archive.getmembers()
            if PurePosixPath(member.name).name == "cua-driver"
        ]
        if len(matches) != 1:
            raise RuntimeError("Cua Driver archive must contain exactly one cua-driver binary")
        member = matches[0]
        if not member.isfile() or member.size <= 0:
            raise RuntimeError("Cua Driver archive contains an invalid cua-driver entry")
        source = archive.extractfile(member)
        if source is None:
            raise RuntimeError("Cua Driver archive binary could not be read")
        with source, destination.open("wb") as output:
            shutil.copyfileobj(source, output)
            output.flush()
            os.fsync(output.fileno())


def install(destination: Path) -> None:
    if sys.platform != "linux" or os.geteuid() != 0:
        raise PermissionError("Run this installer as root on Linux")

    destination = destination.absolute()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    with tempfile.TemporaryDirectory(prefix="cua-driver-install-") as scratch:
        archive_path = Path(scratch) / ASSET
        download_archive(archive_path)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", dir=destination.parent
        )
        temporary = Path(temporary_name)
        try:
            os.close(descriptor)
            extract_binary(archive_path, temporary)
            temporary.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR
                            | stat.S_IRGRP | stat.S_IXGRP
                            | stat.S_IROTH | stat.S_IXOTH)
            os.replace(temporary, destination)
            directory_fd = os.open(destination.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--destination", type=Path, default=DEFAULT_DESTINATION,
        help=f"binary path (default: {DEFAULT_DESTINATION})",
    )
    arguments = parser.parse_args()
    try:
        install(arguments.destination)
    except Exception as error:
        print(f"Cua Driver installation failed: {error}", file=sys.stderr)
        return 1
    print(f"Installed Cua Driver {VERSION} at {arguments.destination.absolute()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
