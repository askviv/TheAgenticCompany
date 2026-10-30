#!/usr/bin/env python3
"""Authenticated operator client for the private coordinator API."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

import requests


PRINCIPALS_FILE = Path("/data/theagentcompany/enterprise/secrets/principals.json")
DEFAULT_BASE_URL = "http://192.0.2.10:8131"
ROLES = {
    "start": "controller", "advance": "controller", "pause": "controller",
    "schedule-event": "controller", "pending-events": "controller",
    "reset": "controller", "pump": "controller", "recover-coffee": "controller",
    "evaluate": "evaluator", "case": "agent", "inbox": "agent",
    "erp-read": "agent", "message": "agent", "action": "agent", "review": "agent",
}


def private_principals(path: Path) -> dict:
    if path.stat().st_mode & 0o077:
        raise PermissionError("Principals file must be mode 0600")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Principals file must contain a JSON object")
    return value


def payload_file(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Request payload file must contain a JSON object")
    return value


def parser_for_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--principals", type=Path, default=PRINCIPALS_FILE)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def mutation(name: str, role: str, endpoint):
        sub = subparsers.add_parser(name)
        sub.set_defaults(role=role, endpoint=endpoint, method="POST")
        sub.add_argument("--key", required=True, help="Stable Idempotency-Key; reuse it only for the same request")
        return sub

    mutation("start", "controller", lambda args: "/scenarios/start").add_argument("--payload", type=Path, required=True)
    mutation("schedule-event", "controller", lambda args: "/events").add_argument("--payload", type=Path, required=True)
    mutation("advance", "controller", lambda args: "/clock/advance").add_argument("--payload", type=Path, required=True)
    mutation("pause", "controller", lambda args: "/clock/pause").add_argument("--payload", type=Path, required=True)
    mutation("recover-coffee", "controller", lambda args: "/company/background/coffeebench/recover").add_argument("--payload", type=Path, required=True)
    sub = subparsers.add_parser("pending-events")
    sub.set_defaults(role="controller", endpoint=lambda args: f"/events?run_id={args.run_id}", method="GET")
    sub.add_argument("run_id")
    mutation("reset", "controller", lambda args: f"/scenarios/{args.run_id}/reset").add_argument("run_id")
    mutation("pump", "controller", lambda args: f"/mail/pump?run_id={args.run_id}").add_argument("run_id")
    mutation("message", "agent", lambda args: "/messages").add_argument("--payload", type=Path, required=True)
    sub = mutation("review", "agent", lambda args: f"/runs/{args.run_id}/invoice-review/{args.invoice_id}")
    sub.add_argument("run_id")
    sub.add_argument("invoice_id")
    sub = mutation("action", "agent", lambda args: f"/runs/{args.run_id}/erp/actions/{args.operation}")
    sub.add_argument("run_id")
    sub.add_argument("operation", choices=("create_goods_receipt", "commit_expedited_shipping"))
    sub.add_argument("--payload", type=Path, required=True)

    sub = subparsers.add_parser("evaluate")
    sub.set_defaults(role="evaluator", endpoint=lambda args: f"/scenarios/{args.run_id}/evaluate", method="POST")
    sub.add_argument("run_id")

    sub = subparsers.add_parser("case")
    sub.set_defaults(role="agent", endpoint=lambda args: f"/cases/{args.case_id}?run_id={args.run_id}", method="GET")
    sub.add_argument("run_id")
    sub.add_argument("case_id")

    sub = subparsers.add_parser("inbox")
    sub.set_defaults(role="agent", endpoint=lambda args: f"/agents/{args.actor_id}/inbox?run_id={args.run_id}", method="GET")
    sub.add_argument("run_id")
    sub.add_argument("actor_id")

    sub = subparsers.add_parser("erp-read")
    sub.set_defaults(role="agent", endpoint=lambda args: f"/runs/{args.run_id}/erp/{args.operation}/{args.entity_id}", method="GET")
    sub.add_argument("run_id")
    sub.add_argument("operation", choices=("get_purchase_order", "get_purchase_receipt", "get_purchase_invoice",
                  "get_supplier", "get_sales_order", "get_customer", "get_stock_balance", "read_payment_status"))
    sub.add_argument("entity_id")

    sub = subparsers.add_parser("health")
    sub.set_defaults(role=None, endpoint=lambda _args: "/health", method="GET")
    return parser


def request_for(args: argparse.Namespace) -> requests.Response:
    url = args.base_url.rstrip("/") + args.endpoint(args)
    headers = {"Accept": "application/json"}
    if args.role is not None:
        principal_map = private_principals(args.principals)
        token = next((value for value, record in principal_map.items()
                      if isinstance(record, dict) and record.get("role") == args.role), None)
        if token is None:
            raise RuntimeError(f"No configured {args.role} principal is available")
        headers["Authorization"] = "Bearer " + token
    if hasattr(args, "key"):
        headers["Idempotency-Key"] = args.key
    payload = payload_file(args.payload) if hasattr(args, "payload") else None
    session = requests.Session()
    session.trust_env = False
    try:
        return session.request(args.method, url, json=payload, headers=headers, timeout=(3.05, 960))
    finally:
        session.close()


def main() -> int:
    args = parser_for_cli().parse_args()
    response = request_for(args)
    try:
        value = response.json()
    except ValueError:
        value = None
    if not response.ok:
        detail = value.get("detail") if isinstance(value, dict) else None
        print(json.dumps({"http_status": response.status_code, "detail": detail}), file=sys.stderr)
        return 1
    if value is None:
        print(json.dumps({"http_status": response.status_code}))
    else:
        print(json.dumps(value, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Coordinator request failed ({type(error).__name__})", file=sys.stderr)
        raise
