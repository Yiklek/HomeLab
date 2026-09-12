#!/usr/bin/env python3
"""Configure Authentik redirect URIs for Stalwart WebUI on the same Docker host."""

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
    payload = base64.b64encode(json.dumps({
        "client_id": client_id,
        "desired": desired,
        "dry_run": args.dry_run,
        "check": args.check,
    }).encode()).decode()

    code = f'''import base64, json, sys
from authentik.providers.oauth2.models import OAuth2Provider
cfg=json.loads(base64.b64decode("{payload}"))
try:
    provider=OAuth2Provider.objects.get(client_id=cfg["client_id"])
except OAuth2Provider.DoesNotExist:
    print("ERROR Authentik OAuth2 provider not found for client_id", file=sys.stderr)
    raise SystemExit(1)
current=list(provider._redirect_uris or [])
# Keep valid custom callbacks but remove the bootstrap placeholder.
kept=[item for item in current if item.get("url") != "https://127.0.0.1"]
for item in cfg["desired"]:
    if item not in kept:
        kept.append(item)
if kept == current:
    print("UNCHANGED Authentik redirect URIs")
    raise SystemExit(0)
print("PLAN  Authentik redirect URIs:")
for item in kept:
    print("  "+item["url"])
if cfg["check"]:
    raise SystemExit(3)
if cfg["dry_run"]:
    raise SystemExit(0)
provider._redirect_uris=kept
provider.save(update_fields=["_redirect_uris"])
print("CHANGED Authentik redirect URIs")
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
