"""Apply company application selection through an operator-owned Compose mapping."""
from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from servers.company.packs.models import Application


class ApplicationServiceGroup(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    id: Annotated[str, Field(min_length=1)]
    compose_file: Annotated[str, Field(min_length=1)]
    services: list[Annotated[str, Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]*$')]]
    applications: list[Application]
    required: bool = False


def apply_company_applications(applications: list[str], mapping_path: Path) -> None:
    """Start selected groups before stopping unused groups, preserving their volumes.

    The mapping is deployment configuration, never content from a company ZIP.
    Docker receives an argument list and only the operator's Compose/service names.
    """
    selected = set(TypeAdapter(list[Application]).validate_python(applications, strict=True))
    groups = TypeAdapter(list[ApplicationServiceGroup]).validate_json(mapping_path.read_bytes())
    enabled = [group for group in groups if group.required or selected.intersection(group.applications)]
    disabled = [group for group in groups if not group.required and not selected.intersection(group.applications)]
    enabled_services: set[tuple[Path, str]] = set()
    for group in enabled:
        compose_file = Path(group.compose_file)
        if not compose_file.is_absolute():
            compose_file = mapping_path.parent / compose_file
        enabled_services.update((compose_file.resolve(), service) for service in group.services)
    for group, start in [(group, True) for group in enabled] + [(group, False) for group in disabled]:
        compose_file = Path(group.compose_file)
        if not compose_file.is_absolute():
            compose_file = mapping_path.parent / compose_file
        services = group.services if start else [
            service for service in group.services
            if (compose_file.resolve(), service) not in enabled_services
        ]
        # An empty Compose service list targets the entire stack; never issue it.
        if not services:
            continue
        command = ['docker', 'compose', '-f', str(compose_file)]
        command += ['up', '--detach', '--pull', 'never'] if start else ['stop']
        command += services
        # Native errors retain their cause; provider output stays out of user-facing errors.
        try:
            subprocess.run(command, check=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError) as error:
            raise RuntimeError(f'Company application service group {group.id} could not be {"started" if start else "stopped"}') from error
