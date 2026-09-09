#!/usr/bin/env python3
"""
Admin accounts from a shell — the escape hatch for the chicken-and-egg problem.

A fresh deployment has no accounts, and only an owner may create one, so the first owner has to come
from outside the API. This script talks straight to Postgres with the same `DATABASE_URL` the API uses.

    python scripts/admin_user.py create-owner luis@example.com --name "Luis"
    python scripts/admin_user.py create luis@example.com --role admin --cities bogota
    python scripts/admin_user.py passwd luis@example.com      # also revokes their sessions
    python scripts/admin_user.py list

The password is read from a prompt (never echoed, never in your shell history). `--password-stdin`
reads it from a pipe for non-interactive use; there is deliberately no `--password` flag, because that
would put the secret in the process list.

`create-owner` refuses once any account exists — the same rule as the ADMIN_BOOTSTRAP_* variables — so
it is safe to leave in a provisioning script.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.admin_auth import (  # noqa: E402
    MIN_PASSWORD_LEN,
    PgAdminUserStore,
    bootstrap_owner,
    hash_password,
    validate_cities,
    validate_email,
    validate_role,
)
from app.config import settings  # noqa: E402
from app.db import close_pool, init_pool  # noqa: E402


def read_password(from_stdin: bool) -> str:
    if from_stdin:
        pw = sys.stdin.readline().rstrip("\n")
    else:
        pw = getpass.getpass(f"Password (min {MIN_PASSWORD_LEN} chars): ")
        if pw != getpass.getpass("Repeat: "):
            sys.exit("passwords do not match")
    if len(pw) < MIN_PASSWORD_LEN:
        sys.exit(f"password must be at least {MIN_PASSWORD_LEN} characters")
    return pw


def known_cities() -> set[str]:
    from app.cities import load_registry
    return set(load_registry(settings().CITIES_DIR))


async def run(args: argparse.Namespace) -> int:
    await init_pool()
    store = PgAdminUserStore()
    try:
        if args.cmd == "list":
            users = await store.list_users()
            if not users:
                print("no accounts yet — run `create-owner`")
            for u in users:
                scope = ",".join(u["cities"]) or "all cities"
                print(f"{u['id']:>4}  {u['email']:<32} {u['role']:<7} {scope:<24}"
                      f"{'disabled' if u['disabled'] else ''}")
            return 0

        email = validate_email(args.email)
        if args.cmd == "create-owner":
            created = await bootstrap_owner(store, email, read_password(args.password_stdin), args.name)
            if created is None:
                print("refused: an account already exists (use `create` as an owner instead)", file=sys.stderr)
                return 1
            print(f"created owner {created['email']} (id {created['id']})")
            return 0

        if args.cmd == "create":
            row = await store.create_user(
                email=email, password_hash=hash_password(read_password(args.password_stdin)),
                name=args.name, role=validate_role(args.role),
                cities=validate_cities(args.cities, known_cities()))
            print(f"created {row['email']} ({row['role']}, {','.join(row['cities']) or 'all cities'})")
            return 0

        user = await store.by_email(email)
        if user is None:
            print(f"no account for {email}", file=sys.stderr)
            return 1
        if args.cmd == "passwd":
            await store.update_user(user["id"], password_hash=hash_password(read_password(args.password_stdin)))
            n = await store.delete_sessions_of(user["id"])
            print(f"password changed for {user['email']}; {n} session(s) revoked")
        elif args.cmd == "disable":
            await store.update_user(user["id"], disabled=True)
            n = await store.delete_sessions_of(user["id"])
            print(f"disabled {user['email']}; {n} session(s) revoked")
        elif args.cmd == "enable":
            await store.update_user(user["id"], disabled=False)
            print(f"enabled {user['email']}")
        return 0
    finally:
        await close_pool()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def with_password(p):
        p.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
        return p

    p = with_password(sub.add_parser("create-owner", help="the first owner; refuses if any account exists"))
    p.add_argument("email")
    p.add_argument("--name", default="")

    p = with_password(sub.add_parser("create", help="any account"))
    p.add_argument("email")
    p.add_argument("--name", default="")
    p.add_argument("--role", default="viewer", help="viewer | admin | owner")
    p.add_argument("--cities", default="", help="comma-separated city ids; empty means every city")

    for cmd, help_text in (("passwd", "set a new password"), ("disable", "disable an account"),
                           ("enable", "re-enable an account")):
        p = sub.add_parser(cmd, help=help_text)
        p.add_argument("email")
        if cmd == "passwd":
            with_password(p)

    sub.add_parser("list", help="list accounts")

    args = ap.parse_args()
    if getattr(args, "cities", None) is not None:
        args.cities = [c.strip() for c in str(args.cities).split(",") if c.strip()]
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
