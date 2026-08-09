"""Non-MCP registry management CLI (plan 04 §4.5, R13).

Secrets enter only via stdin or a 0600 file; they never appear on argv or
stdout. All output is safe metadata JSON.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys

from .db import connect, init_db, now
from . import registry


def _positive_days(value: str) -> int:
    days = int(value)
    if days <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return days


def _read_token(args) -> str:
    if args.token_file:
        st = os.stat(args.token_file)
        if stat.S_IMODE(st.st_mode) & 0o077:
            raise SystemExit("token file must be 0600")
        token = open(args.token_file, encoding="utf-8").read().strip()
    else:
        token = sys.stdin.readline().strip()
    if len(token) < 43:  # 256-bit urlsafe-base64 minimum
        raise SystemExit("token too short: need >=256-bit random value")
    return token


def _pepper() -> bytes:
    pepper = os.environ.get("HIPPOCAMPUS_TOKEN_PEPPER", "")
    if not pepper:
        raise SystemExit("HIPPOCAMPUS_TOKEN_PEPPER is required")
    return pepper.encode()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hippocampus-registry")
    ap.add_argument("--db", required=True)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init")

    p = sub.add_parser("issue")
    p.add_argument("client_id")
    p.add_argument("--source-tag", required=True)
    p.add_argument("--readable", default="")
    p.add_argument("--writable", default="")
    p.add_argument("--types", default=",".join(("decision", "procedure", "fact", "incident",
                                                "preference", "constraint", "reference")))
    p.add_argument("--expires-days", type=_positive_days, default=None)
    p.add_argument("--disabled", action="store_true")
    p.add_argument("--token-file", default=None)

    p = sub.add_parser("rotate")
    p.add_argument("client_id")
    p.add_argument("--renew-expires-days", type=_positive_days, default=None)
    p.add_argument("--token-file", default=None)

    p = sub.add_parser("revoke")
    p.add_argument("client_id")

    p = sub.add_parser("grant")
    p.add_argument("client_id")
    p.add_argument("--readable", required=True)
    p.add_argument("--writable", required=True)

    p = sub.add_parser("bind-peer")
    p.add_argument("client_id")
    p.add_argument("ip")

    p = sub.add_parser("revoke-peer")
    p.add_argument("client_id")
    p.add_argument("ip")

    sub.add_parser("list-safe")

    args = ap.parse_args(argv)
    init_db(args.db)
    conn = connect(args.db)
    try:
        if args.cmd == "init":
            print(json.dumps({"ok": True}))
        elif args.cmd == "issue":
            token = _read_token(args)
            expires = now() + args.expires_days * 86400 if args.expires_days else None
            registry.create_client(
                conn, args.client_id, token, _pepper(), source_tag=args.source_tag,
                readable=[x for x in args.readable.split(",") if x],
                writable=[x for x in args.writable.split(",") if x],
                types=[x for x in args.types.split(",") if x],
                expires_at=expires, disabled=args.disabled)
            print(json.dumps({"ok": True, "client_id": args.client_id}))
        elif args.cmd == "rotate":
            registry.rotate_token(
                conn, args.client_id, _read_token(args), _pepper(),
                renew_expires_days=args.renew_expires_days,
            )
            print(json.dumps({"ok": True, "client_id": args.client_id}))
        elif args.cmd == "revoke":
            registry.revoke_client(conn, args.client_id)
            print(json.dumps({"ok": True, "client_id": args.client_id}))
        elif args.cmd == "grant":
            registry.set_grants(conn, args.client_id,
                                readable=[x for x in args.readable.split(",") if x],
                                writable=[x for x in args.writable.split(",") if x])
            print(json.dumps({"ok": True, "client_id": args.client_id}))
        elif args.cmd == "bind-peer":
            registry.bind_peer(conn, args.client_id, args.ip)
            print(json.dumps({"ok": True}))
        elif args.cmd == "revoke-peer":
            registry.revoke_peer(conn, args.client_id, args.ip)
            print(json.dumps({"ok": True}))
        elif args.cmd == "list-safe":
            print(json.dumps(registry.list_safe(conn), ensure_ascii=False, indent=2))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
