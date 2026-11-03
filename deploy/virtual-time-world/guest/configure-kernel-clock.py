#!/usr/bin/env python3
"""Bind fresh coordinator runtime to the host-supervised kernel clock."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys
import tempfile

RUNTIME = Path('/data/theagentcompany/enterprise/config/runtime.json')
TOKEN = Path('/etc/company-workplace/secrets/clock-api-token')


def main() -> int:
    token = TOKEN.stat()
    if token.st_uid != 0 or stat.S_IMODE(token.st_mode) != 0o600:
        raise PermissionError('clock API token must be root-owned mode 0600')
    runtime = RUNTIME.stat()
    if runtime.st_uid != 0 or stat.S_IMODE(runtime.st_mode) != 0o600:
        raise PermissionError('coordinator runtime config must be root-owned mode 0600')
    config = json.loads(RUNTIME.read_text(encoding='utf-8'))
    if not isinstance(config, dict):
        raise ValueError('coordinator runtime config must contain a JSON object')
    config.update({
        'kernel_company_clock_enabled': True,
        'kernel_company_clock_url': 'http://10.0.2.2:8133',
        'kernel_company_clock_token_file': str(TOKEN),
        'shared_company_clock_enabled': False,
    })
    sys.path.insert(0, '/opt/agentic-world/source')
    from servers.simulation.runtime import RuntimeConfig
    RuntimeConfig.model_validate(config)
    descriptor, temporary = tempfile.mkstemp(prefix='.runtime-', dir=RUNTIME.parent)
    try:
        os.fchown(descriptor, runtime.st_uid, runtime.st_gid)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            json.dump(config, output, indent=2, sort_keys=True)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, RUNTIME)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
