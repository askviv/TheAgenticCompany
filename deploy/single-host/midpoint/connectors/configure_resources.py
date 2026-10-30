#!/usr/bin/env python3
"""Import native resources using private credentials from the POC."""

from __future__ import annotations

import argparse
import base64
import json
import os
import stat
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SECRETS = Path.home() / ".config/theagentcompany/connector-secrets.json"
ADMIN_PASSWORD = Path.home() / ".config/theagentcompany/midpoint-4.11-poc-admin-password"
COMMON = "http://midpoint.evolveum.com/xml/ns/public/common/common-3"
TYPES = "http://prism.evolveum.com/xml/ns/public/types-3"
ICFC = "http://midpoint.evolveum.com/xml/ns/public/connector/icf-1/connector-schema-3"
ROCKETCHAT = "http://midpoint.evolveum.com/xml/ns/public/connector/icf-1/bundle/com.evolveum.polygon.scimrest.connector-rocketchat/com.evolveum.polygon.rocketchat.RocketChatConnector"
MAILU = "http://midpoint.evolveum.com/xml/ns/public/connector/icf-1/bundle/com.evolveum.polygon.scimrest.connector-mailu/com.evolveum.polygon.mailu.MailuConnector"
OWNCLOUD = "http://midpoint.evolveum.com/xml/ns/public/connector/icf-1/bundle/com.evolveum.polygon.scimrest.connector-owncloud/com.evolveum.polygon.owncloud.OwnCloudConnector"

ET.register_namespace("", COMMON)
ET.register_namespace("c", COMMON)
ET.register_namespace("q", "http://prism.evolveum.com/xml/ns/public/query-3")
ET.register_namespace("icfc", ICFC)
ET.register_namespace("rc", ROCKETCHAT)
ET.register_namespace("mu", MAILU)
ET.register_namespace("oc", OWNCLOUD)
ET.register_namespace("ri", "http://midpoint.evolveum.com/xml/ns/public/resource/instance-3")
ET.register_namespace("t", TYPES)


def read_private_json(path: Path) -> dict:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise RuntimeError(f"Credential file {path} must be mode 0600")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise RuntimeError("Credential file must contain a JSON object")
    return value


def import_object(base_url: str, admin_password: str, endpoint: str, xml: bytes, *, replace: bool) -> tuple[int, bytes]:
    root = ET.fromstring(xml)
    oid = root.attrib.get("oid")
    if not oid:
        raise RuntimeError("Resource template has no OID")
    url = base_url.rstrip("/") + f"/ws/rest/{endpoint}/{oid}" if replace else base_url.rstrip("/") + f"/ws/rest/{endpoint}"
    credentials = base64.b64encode(f"theagentcompany:{admin_password}".encode()).decode()
    request = Request(
        url,
        data=xml,
        method="PUT" if replace else "POST",
        headers={
            "Authorization": f"Basic {credentials}",
            "Accept": "application/xml",
            "Content-Type": "application/xml",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            return response.status, response.read()
    except HTTPError as error:
        status = error.code
        error.close()
        raise RuntimeError(f"midPoint {endpoint} import returned HTTP {status}") from error
    except URLError as error:
        raise RuntimeError(f"midPoint {endpoint} API could not be reached") from error


def configure_template(path: Path, secret_namespace: str, secret_values: dict[str, str], base_address: str | None) -> bytes:
    tree = ET.parse(path)
    root = tree.getroot()
    # MidPoint parses QName-valued text fields such as ``ri:User`` from the
    # original XML prefix declaration; ElementTree otherwise drops unused ns.
    root.set("xmlns:ri", "http://midpoint.evolveum.com/xml/ns/public/resource/instance-3")
    root.set("xmlns:ea", "https://theagentcompany.com/xml/ns/midpoint/employee-accounts")
    properties = root.find(f"{{{COMMON}}}connectorConfiguration/{{{ICFC}}}configurationProperties")
    if properties is None:
        raise RuntimeError(f"Resource template {path.name} has no connector configuration properties")
    if base_address:
        address = properties.find(f"{{{secret_namespace}}}baseAddress")
        if address is None:
            raise RuntimeError(f"Resource template {path.name} has no base address property")
        address.text = base_address
    for name, value in secret_values.items():
        prop = ET.SubElement(properties, f"{{{secret_namespace}}}{name}")
        if name in ("restTokenValue", "restPassword"):
            clear_value = ET.SubElement(prop, f"{{{TYPES}}}clearValue")
            clear_value.text = value
        else:
            prop.text = value
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--midpoint", default="http://127.0.0.1:18120/midpoint")
    parser.add_argument("--secrets", type=Path, default=DEFAULT_SECRETS)
    parser.add_argument("--rocketchat-base-address", default=os.environ.get("ROCKETCHAT_BASE_ADDRESS"))
    parser.add_argument("--mailu-base-address", default=os.environ.get("MAILU_BASE_ADDRESS"))
    owncloud_mode = parser.add_mutually_exclusive_group()
    owncloud_mode.add_argument("--with-owncloud", action="store_true", help="include the ownCloud resource; requires an existing operator app password in the private credential file")
    owncloud_mode.add_argument("--owncloud-only", action="store_true", help="replace only the ownCloud resource")
    parser.add_argument("--replace-existing", action="store_true", help="replace the selected Resource objects from their checked-in XML definitions")
    args = parser.parse_args()

    secrets = read_private_json(args.secrets)
    admin_password = ADMIN_PASSWORD.read_text().strip()
    rocket = secrets["rocketchat"]
    mailu = secrets["mailu"]
    definitions = [] if args.owncloud_only else [
        (ROOT / "resource-rocketchat.xml", ROCKETCHAT, {"restTokenValue": rocket["auth_token"], "rocketChatUserId": rocket["user_id"]}, args.rocketchat_base_address),
        (ROOT / "resource-mailu.xml", MAILU, {"restTokenValue": mailu["api_token"]}, args.mailu_base_address),
    ]
    if args.with_owncloud or args.owncloud_only:
        owncloud = secrets.get("owncloud")
        if not isinstance(owncloud, dict) or not owncloud.get("username") or not owncloud.get("app_password"):
            raise RuntimeError("--with-owncloud requires an existing ownCloud operator username and app_password in the private credential file")
        definitions.append((ROOT / "resource-owncloud.xml", OWNCLOUD, {"restUsername": owncloud["username"], "restPassword": owncloud["app_password"]}, os.environ.get("OWNCLOUD_BASE_ADDRESS")))
        for policy_file, endpoint in (
            (ROOT / "value-policy-owncloud-sandbox.xml", "valuePolicies"),
            (ROOT / "security-policy-owncloud-sandbox.xml", "securityPolicies"),
        ):
            status, _body = import_object(args.midpoint, admin_password, endpoint, policy_file.read_bytes(), replace=args.replace_existing)
            print(f"{'Replaced' if args.replace_existing else 'Imported'} {policy_file.stem}: HTTP {status}")
    for template, namespace, properties, base_address in definitions:
        status, _body = import_object(args.midpoint, admin_password, "resources", configure_template(template, namespace, properties, base_address), replace=args.replace_existing)
        print(f"{'Replaced' if args.replace_existing else 'Imported'} {template.stem}: HTTP {status}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Resource configuration failed ({type(exc).__name__}): {exc}", file=sys.stderr)
        raise
