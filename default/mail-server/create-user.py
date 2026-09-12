#!/usr/bin/env python3
"""Create an Authentik user with the mail UPN filled in automatically.

Authentik only evaluates expressions inside flows, so a user created from the
admin UI cannot have its attributes computed. Authentik's identification stage
also hardcodes `upn` to `attributes.upn` (core/stages/identification/stage.py:
`{"email": ..., "username": ..., "upn": "attributes__upn"}`), which means the
attribute name is not free to choose.

This script keeps the admin-driven workflow but computes the value for you, so
`<username>@<domain>` is always present and WebUI logins with a full address
keep working.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
from pathlib import Path

TEMPLATE = '''import base64, json, sys
from authentik.core.models import Group, User
cfg = json.loads(base64.b64decode("__PAYLOAD__"))

if cfg["delete"]:
    user = User.objects.filter(username=cfg["username"]).first()
    if user is None:
        print("MISSING " + cfg["username"])
        raise SystemExit(4)
    user.delete()
    print("DELETED " + cfg["username"])
    raise SystemExit(0)

existing = User.objects.filter(username=cfg["username"]).first()
if existing is not None and not cfg["update"]:
    print("EXISTS " + cfg["username"])
    raise SystemExit(3)

attributes = dict(existing.attributes or {}) if existing else {}
if cfg["upn"]:
    attributes["upn"] = cfg["upn"]
else:
    attributes.pop("upn", None)

if existing is None:
    user = User.objects.create(
        username=cfg["username"],
        name=cfg["name"] or cfg["username"],
        email=cfg["email"] or "",
        is_active=True,
        attributes=attributes,
    )
    action = "CREATED"
else:
    user = existing
    if cfg["name"]:
        user.name = cfg["name"]
    if cfg["email"]:
        user.email = cfg["email"]
    user.attributes = attributes
    user.save(update_fields=["name", "email", "attributes"])
    action = "UPDATED"

for group_name in cfg["groups"]:
    group = Group.objects.filter(name=group_name).first()
    if group is None:
        print("WARN  group does not exist, skipped: " + group_name)
        continue
    user.ak_groups.add(group)

if cfg["password"]:
    user.set_password(cfg["password"])
    user.save(update_fields=["password"])

print("%s %s upn=%s groups=%s" % (
    action,
    user.username,
    attributes.get("upn", "-"),
    sorted(g.name for g in user.ak_groups.all()),
))
'''


def load_env(path: Path) -> dict[str, str]:
    env = dict(os.environ)
    if path.exists():
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            env.setdefault(key.strip(), value)
    pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
    for _ in range(10):
        changed = False
        for key, value in list(env.items()):
            expanded = pattern.sub(lambda m: env.get(m.group(1), m.group(0)), value)
            if expanded != value:
                env[key] = expanded
                changed = True
        if not changed:
            break
    return env


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("username")
    parser.add_argument("--name", default="")
    parser.add_argument("--email", default="")
    parser.add_argument("--group", action="append", default=[], help="repeatable")
    parser.add_argument("--admin", action="store_true",
                        help="also add to MAIL_OIDC_ADMIN_GROUP")
    parser.add_argument("--password", default="",
                        help="set a local password (optional; OIDC logins do not need one)")
    parser.add_argument("--no-upn", action="store_true",
                        help="leave attributes.upn unset")
    parser.add_argument("--update", action="store_true",
                        help="update the user instead of failing when it already exists")
    parser.add_argument("--domain", default="",
                        help="domain for the UPN; overrides MAIL_OIDC_USERNAME_DOMAIN/"
                             "MAIL_DOMAIN/DOMAIN from the env file")
    parser.add_argument("--delete", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--env-file", default=None)
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent
    env_file = Path(args.env_file) if args.env_file else script_dir.parent.parent / ".env"
    env = load_env(env_file)

    domain = (
        args.domain.strip()
        or env.get("MAIL_OIDC_USERNAME_DOMAIN")
        or env.get("MAIL_DOMAIN")
        or env.get("DOMAIN", "")
    ).strip()
    if not domain and not args.no_upn:
        print("A domain is required to build the UPN: pass --domain or set "
              "MAIL_DOMAIN in the env file (use --no-upn to skip it).", file=sys.stderr)
        return 1

    username = args.username.strip()
    if not username or "@" in username:
        print("Give the bare username (no @domain); the UPN adds the domain.", file=sys.stderr)
        return 1

    groups = list(args.group)
    if args.admin:
        admin_group = env.get("MAIL_OIDC_ADMIN_GROUP", "").strip()
        if not admin_group:
            print("MAIL_OIDC_ADMIN_GROUP is not set, --admin has no effect", file=sys.stderr)
        else:
            groups.append(admin_group)

    upn = "" if args.no_upn else f"{username}@{domain}"
    payload = base64.b64encode(json.dumps({
        "username": username,
        "name": args.name,
        "email": args.email,
        "groups": groups,
        "password": args.password,
        "upn": upn,
        "update": args.update,
        "delete": args.delete,
    }).encode()).decode()

    if args.dry_run:
        verb = "delete" if args.delete else ("update" if args.update else "create")
        print(f"PLAN  {verb} {username}  upn={upn or '-'}  groups={groups}")
        return 0

    proc = subprocess.run(
        ["docker", "exec", "-i", env.get("AUTHENTIK_SERVER_CONTAINER", "authentik-server"),
         "ak", "shell", "-c", TEMPLATE.replace("__PAYLOAD__", payload)],
        text=True,
        capture_output=True,
    )
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith(("CREATED ", "UPDATED ", "DELETED ", "EXISTS ", "MISSING ", "WARN ")):
            print(line)
    for line in proc.stderr.splitlines():
        if line.startswith("WARN"):
            print(line, file=sys.stderr)
    if proc.returncode not in {0}:
        if "EXISTS" in proc.stdout:
            print(f"{username} already exists; pass --update to change it.", file=sys.stderr)
        elif "MISSING" in proc.stdout:
            print(f"{username} does not exist.", file=sys.stderr)
        else:
            tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
            print("ERROR " + " | ".join(tail), file=sys.stderr)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
