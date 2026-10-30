#!/usr/bin/env python3
"""Keep the local midPoint connector routes to the Mini available with native SSH."""

import argparse
import os
from pathlib import Path
import plistlib
import subprocess


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssh-key", type=Path, required=True)
    parser.add_argument("--mini", default="company@192.0.2.10")
    args = parser.parse_args()
    key = args.ssh_key.expanduser().resolve(strict=True)
    label = "org.example.tac-midpoint-connectors"
    private = Path.home() / ".local/share/tac-midpoint"
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    launch = Path.home() / "Library/LaunchAgents"
    launch.mkdir(parents=True, exist_ok=True)
    plist = launch / (label + ".plist")
    argv = ["/usr/bin/ssh", "-F", "/dev/null", "-i", str(key), "-N", "-T",
            "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3"]
    for local, remote in ((13000, 3000), (18112, 8112), (8092, 8092)):
        argv += ["-L", f"127.0.0.1:{local}:127.0.0.1:{remote}"]
    # Scenario execution on the Mini resolves the authoritative employee record
    # through this private loopback route; the admin UI stays Mac-loopback only.
    argv += ["-R", "127.0.0.1:18121:127.0.0.1:18120"]
    argv.append(args.mini)
    plist.write_bytes(plistlib.dumps({
        "Label": label, "ProgramArguments": argv, "RunAtLoad": True, "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": str(private / "connectors.out.log"),
        "StandardErrorPath": str(private / "connectors.err.log"),
    }))
    target = f"gui/{os.getuid()}/{label}"
    if subprocess.run(["launchctl", "print", target], capture_output=True).returncode == 0:
        subprocess.run(["launchctl", "bootout", target], check=True)
    subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)], check=True)
    print(f"Installed {label}")


if __name__ == "__main__":
    main()
