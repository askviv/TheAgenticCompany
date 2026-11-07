"""Read-only, bounded ZIP loading. Never extracts members to disk."""
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import json
import stat
from zipfile import ZipFile, BadZipFile
from .models import Manifest, safe_path

@dataclass(frozen=True)
class ArchiveLimits:
    max_archive_bytes: int = 50 * 1024 * 1024
    max_member_bytes: int = 30 * 1024 * 1024
    max_total_bytes: int = 100 * 1024 * 1024
    max_members: int = 500

@dataclass(frozen=True)
class LoadedPack:
    manifest: Manifest
    files: dict[str, bytes]
    digest: str


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON object key')
        result[key] = value
    return result


def load_pack(data: bytes, limits: ArchiveLimits | None = None) -> LoadedPack:
    limits = limits or ArchiveLimits()
    if len(data) > limits.max_archive_bytes:
        raise ValueError('Pack archive exceeds compressed size limit')
    try:
        with ZipFile(BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > limits.max_members:
                raise ValueError('Pack archive exceeds member limit')
            names, total = set(), 0
            for entry in entries:
                safe_path(entry.filename)
                if entry.filename in names:
                    raise ValueError('Duplicate archive member')
                names.add(entry.filename)
                mode = entry.external_attr >> 16
                file_type = stat.S_IFMT(mode)
                if entry.is_dir() or file_type not in (0, stat.S_IFREG) or entry.flag_bits & 1:
                    raise ValueError('Pack members must be unencrypted regular files')
                if entry.file_size > limits.max_member_bytes:
                    raise ValueError('Pack member exceeds size limit')
                total += entry.file_size
            if total > limits.max_total_bytes:
                raise ValueError('Pack archive exceeds expanded size limit')
            if 'manifest.json' not in names:
                raise ValueError('Pack requires manifest.json')
            raw = json.loads(archive.read('manifest.json'), object_pairs_hook=_unique_object)
            manifest = Manifest.model_validate(raw)
            expected = {'manifest.json'} | {file.path for file in manifest.files}
            if names != expected:
                raise ValueError('Archive members must exactly match declared files and manifest.json')
            files = {file.path: archive.read(file.path) for file in manifest.files}
    except BadZipFile as exc:
        raise ValueError('Invalid ZIP archive') from exc
    return LoadedPack(manifest=manifest, files=files, digest=sha256(data).hexdigest())
