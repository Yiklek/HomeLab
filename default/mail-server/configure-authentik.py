#!/usr/bin/env python3
"""Configure Authentik callbacks and UPN aliases for Stalwart WebUI."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
from pathlib import Path


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
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=None)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent
    env_file = Path(args.env_file) if args.env_file else script_dir.parent.parent / ".env"
    env = load_env(env_file)
    domain = env.get("MAIL_DOMAIN") or env.get("DOMAIN", "")
    hostname = env.get("MAIL_HOSTNAME") or (f"mail.{domain}" if domain else "")
    client_id = env.get("MAIL_OIDC_CLIENT_ID", "")
    container = env.get("AUTHENTIK_SERVER_CONTAINER", "authentik-server")
    if not domain or not hostname or not client_id:
        print("DOMAIN/MAIL_DOMAIN and MAIL_OIDC_CLIENT_ID are required", file=sys.stderr)
        return 1

    desired = [
        {
            "url": f"https://{hostname}/admin/oauth/callback",
            "matching_mode": "strict",
            "redirect_uri_type": "authorization",
        },
        {
            "url": f"https://{hostname}/account/oauth/callback",
            "matching_mode": "strict",
            "redirect_uri_type": "authorization",
        },
    ]
    upn_users = [
        value.strip()
        for value in env.get("MAIL_OIDC_UPN_ACCOUNTS", "").split(",")
        if value.strip()
    ]
    payload = base64.b64encode(json.dumps({
        "client_id": client_id,
        "domain": domain,
        "desired": desired,
        "upn_users": upn_users,
        "dry_run": args.dry_run,
        "check": args.check,
    }).encode()).decode()

    code = f'''import base64, json, sys
from authentik.core.models import User
from authentik.flows.models import FlowStageBinding
from authentik.providers.oauth2.models import OAuth2Provider
from authentik.stages.identification.models import IdentificationStage
cfg=json.loads(base64.b64decode("{payload}"))
try:
    provider=OAuth2Provider.objects.get(client_id=cfg["client_id"])
except OAuth2Provider.DoesNotExist:
    print("ERROR Authentik OAuth2 provider not found for client_id", file=sys.stderr)
    raise SystemExit(1)
changes=[]

current=list(provider._redirect_uris or [])
# Keep valid custom callbacks but remove the bootstrap placeholder.
kept=[item for item in current if item.get("url") != "https://127.0.0.1"]
for item in cfg["desired"]:
    if item not in kept:
        kept.append(item)
if kept != current:
    changes.append(("redirects", provider, kept))
    print("PLAN  Authentik redirect URIs:")
    for item in kept:
        print("  "+item["url"])
else:
    print("OK    Authentik redirect URIs")

# Stalwart needs a full address for OIDC discovery and passes that address as
# login_hint. Authentik's UPN identifier lets user@domain match an existing
# username without replacing the user's personal email attribute.
stages=[]
for binding in FlowStageBinding.objects.filter(target=provider.authentication_flow).order_by("order"):
    if isinstance(binding.stage, IdentificationStage):
        stages.append(binding.stage)
if cfg["upn_users"] and not stages:
    print("ERROR no IdentificationStage found in provider authentication flow", file=sys.stderr)
    raise SystemExit(1)
for stage in stages:
    fields=list(stage.user_fields or [])
    if cfg["upn_users"]:
        if "upn" not in fields:
            fields.append("upn")
            changes.append(("stage", stage, fields))
            print("PLAN  enable UPN matching on IdentificationStage "+str(stage.pk))
        else:
            print("OK    IdentificationStage UPN matching")
    elif "upn" in fields:
        fields=[f for f in fields if f != "upn"]
        changes.append(("stage", stage, fields))
        print("PLAN  disable UPN matching on IdentificationStage "+str(stage.pk))
    else:
        print("OK    IdentificationStage UPN matching disabled")

for username in cfg["upn_users"]:
    try:
        user=User.objects.get(username=username)
    except User.DoesNotExist:
        print("WARN  Authentik user does not exist yet, skipping UPN: "+username)
        continue
    attributes=dict(user.attributes or {{}})
    upn=username+"@"+cfg["domain"]
    if attributes.get("upn") != upn:
        attributes["upn"]=upn
        changes.append(("user", user, attributes))
        print("PLAN  set UPN "+upn+" for Authentik user "+username)
    else:
        print("OK    Authentik UPN "+upn)

# An empty list disables UPN login again. Only aliases owned by this
# deployment (upn == username@MAIL_DOMAIN) are removed, so unrelated
# attributes are never touched.
if not cfg["upn_users"]:
    for user in User.objects.all():
        attributes=dict(user.attributes or {{}})
        owned=user.username+"@"+cfg["domain"]
        if attributes.get("upn") == owned:
            attributes.pop("upn", None)
            changes.append(("user", user, attributes))
            print("PLAN  remove UPN "+owned+" from Authentik user "+user.username)

if not changes:
    print("UNCHANGED Authentik OIDC integration")
    raise SystemExit(0)
if cfg["check"]:
    raise SystemExit(3)
if cfg["dry_run"]:
    raise SystemExit(0)
for kind, obj, value in changes:
    if kind == "redirects":
        obj._redirect_uris=value
        obj.save(update_fields=["_redirect_uris"])
    elif kind == "stage":
        obj.user_fields=value
        obj.save(update_fields=["user_fields"])
    elif kind == "user":
        obj.attributes=value
        obj.save(update_fields=["attributes"])
print("CHANGED Authentik OIDC integration")
raise SystemExit(10)
'''
    proc = subprocess.run(
        ["docker", "exec", container, "ak", "shell", "-c", code],
        text=True,
        capture_output=True,
    )
    if proc.stdout:
        print(proc.stdout.strip())
    if proc.returncode not in {0, 3, 10} and proc.stderr:
        print(proc.stderr.strip(), file=sys.stderr)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
