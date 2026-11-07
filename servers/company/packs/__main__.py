"""Offline validation only; no imports, network calls or experiment execution."""
import argparse
import json
from pathlib import Path
from .archive import load_pack
from .models import Manifest

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    validate = commands.add_parser('validate_zip', help='Validate an archive without changing company state')
    validate.add_argument('path', type=Path)
    schema = commands.add_parser('schema', help='Write the authoritative JSON schema')
    schema.add_argument('path', type=Path)
    args = parser.parse_args()
    if args.command == 'schema':
        args.path.write_text(json.dumps(Manifest.model_json_schema(), indent=2) + '\n')
    else:
        try:
            pack = load_pack(args.path.read_bytes())
        except (ValueError, OSError) as exc:
            parser.exit(1, f'Invalid pack: {exc}\n')
        print(json.dumps({'id': pack.manifest.id, 'version': pack.manifest.version, 'sha256': pack.digest, 'records': len(pack.manifest.records), 'experiments': len(pack.manifest.experiments)}))

if __name__ == '__main__':
    main()
