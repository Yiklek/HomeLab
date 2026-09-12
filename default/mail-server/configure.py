#!/usr/bin/env python3
"""Idempotently bootstrap and configure Stalwart v0.16 through its JMAP API."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

CAPABILITY = "urn:stalwart:jmap"


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = dict(os.environ)
    if path.exists():
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            values.setdefault(key, value)

    pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
    for _ in range(10):
        changed = False
        for key, value in list(values.items()):
            expanded = pattern.sub(lambda m: values.get(m.group(1), m.group(0)), value)
            if expanded != value:
                values[key] = expanded
                changed = True
        if not changed:
            break
    return values


def truthy(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def require(env: dict[str, str], key: str) -> str:
    value = env.get(key, "").strip()
    if not value:
        raise SystemExit(f"Missing required setting: {key}")
    return value


class Jmap:
    def __init__(self, url: str, credential: str, timeout: int = 30):
        self.url = url.rstrip("/") + "/"
        token = base64.b64encode(credential.encode()).decode()
        self.headers = {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
        }
        self.timeout = timeout
        # This API is normally bound to loopback. Ignore inherited http_proxy /
        # https_proxy values so local reconciliation cannot hang in Mihomo.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.counter = 0

    def call(self, method: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.counter += 1
        tag = f"c{self.counter}"
        payload = {
            "using": [CAPABILITY],
            "methodCalls": [[method, arguments, tag]],
        }
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload, separators=(",", ":")).encode(),
            headers=self.headers,
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                body = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise RuntimeError(f"JMAP HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Cannot reach JMAP API {self.url}: {exc.reason}") from exc

        responses = body.get("methodResponses", [])
        if len(responses) != 1:
            raise RuntimeError(f"Unexpected JMAP response: {body}")
        name, result, response_tag = responses[0]
        if response_tag != tag or name == "error":
            raise RuntimeError(f"JMAP method failed: {name}: {result}")
        return result

    def get(self, object_type: str, ids: list[str] | None = None) -> list[dict[str, Any]]:
        args: dict[str, Any] = {"ids": ids}
        return self.call(f"x:{object_type}/get", args).get("list", [])

    def set_create(self, object_type: str, value: dict[str, Any]) -> str:
        result = self.call(f"x:{object_type}/set", {"create": {"new": value}})
        if result.get("notCreated"):
            raise RuntimeError(f"Cannot create x:{object_type}: {result['notCreated']}")
        return result["created"]["new"]["id"]

    def set_update(self, object_type: str, object_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        result = self.call(f"x:{object_type}/set", {"update": {object_id: patch}})
        if result.get("notUpdated"):
            raise RuntimeError(f"Cannot update x:{object_type}/{object_id}: {result['notUpdated']}")
        return result.get("updated", {}).get(object_id) or {}


class Reconciler:
    def __init__(self, api: Jmap, dry_run: bool = False, check: bool = False):
        self.api = api
        self.dry_run = dry_run
        self.check = check
        self.changes: list[str] = []

    def plan(self, message: str) -> None:
        self.changes.append(message)
        print(f"PLAN  {message}")

    @staticmethod
    def delta(current: dict[str, Any], desired: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in desired.items() if current.get(key) != value}

    def update(self, object_type: str, current: dict[str, Any], desired: dict[str, Any], label: str) -> str:
        object_id = current["id"]
        patch = self.delta(current, desired)
        if not patch:
            print(f"OK    {label}")
            return object_id
        self.plan(f"update {label}: {', '.join(sorted(patch))}")
        if not self.dry_run and not self.check:
            self.api.set_update(object_type, object_id, patch)
        return object_id

    def create(self, object_type: str, desired: dict[str, Any], label: str) -> str:
        self.plan(f"create {label}")
        if self.dry_run or self.check:
            return f"planned-{object_type.lower()}"
        return self.api.set_create(object_type, desired)

    def finish(self) -> int:
        if self.check and self.changes:
            print(f"DRIFT {len(self.changes)} change(s) required", file=sys.stderr)
            return 3
        if self.changes and not self.dry_run:
            print(f"CHANGED {len(self.changes)} object(s)")
            return 10
        if self.dry_run:
            print(f"DRY-RUN {len(self.changes)} change(s) planned")
        else:
            print("UNCHANGED")
        return 0


def find_one(items: list[dict[str, Any]], predicate, label: str) -> dict[str, Any] | None:
    matches = [item for item in items if predicate(item)]
    if len(matches) > 1:
        raise RuntimeError(f"Multiple Stalwart objects match {label}")
    return matches[0] if matches else None


def bootstrap(api: Jmap, env: dict[str, str], dry_run: bool, check: bool) -> int:
    objects = api.get("Bootstrap", ["singleton"])
    if not objects:
        print("OK    bootstrap already completed")
        return 0

    hostname = env.get("MAIL_HOSTNAME") or f"mail.{require(env, 'DOMAIN')}"
    domain = env.get("MAIL_DOMAIN") or require(env, "DOMAIN")
    desired = {
        "serverHostname": hostname,
        "defaultDomain": domain,
        "requestTlsCertificate": False,
        "generateDkimKeys": truthy(env.get("MAIL_GENERATE_DKIM"), True),
    }
    current = objects[0]
    patch = Reconciler.delta(current, desired)
    if check:
        if patch:
            print("DRIFT bootstrap is pending", file=sys.stderr)
            return 3
        return 0
    if dry_run:
        print(f"PLAN  bootstrap Stalwart for {hostname} / {domain}")
        return 0

    print(f"APPLY bootstrap Stalwart for {hostname} / {domain}")
    response = api.set_update("Bootstrap", "singleton", desired)
    username = response.get("username")
    secret = response.get("secret")
    if username and secret:
        default_secret_file = Path.home() / ".config/homelab/mail-bootstrap-admin"
        secret_file = Path(env.get("MAIL_BOOTSTRAP_SECRET_FILE", str(default_secret_file))).expanduser()
        secret_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(secret_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(f"username={username}\npassword={secret}\n")
        secret_file.chmod(0o600)
        print(f"IMPORTANT: permanent bootstrap administrator saved to {secret_file} (mode 0600)")
    print("Bootstrap completed; restart Stalwart before running apply.")
    return 10


def apply(api: Jmap, env: dict[str, str], dry_run: bool, check: bool) -> int:
    r = Reconciler(api, dry_run=dry_run, check=check)
    domain = env.get("MAIL_DOMAIN") or require(env, "DOMAIN")
    hostname = env.get("MAIL_HOSTNAME") or f"mail.{domain}"
    client_id = require(env, "MAIL_OIDC_CLIENT_ID")
    issuer = env.get("MAIL_OIDC_ISSUER") or f"https://auth.{domain}/application/o/mail/"
    if not issuer.endswith("/"):
        issuer += "/"

    # Certificate mounted by deploy.sh.
    cert_value = {
        "certificate": {"@type": "File", "filePath": "/certs/mail.crt"},
        "privateKey": {"@type": "File", "filePath": "/certs/mail.key"},
    }
    certificates = api.get("Certificate")
    cert = find_one(
        certificates,
        lambda item: item.get("certificate", {}).get("filePath") == "/certs/mail.crt",
        "certificate /certs/mail.crt",
    )
    if cert:
        cert_id = r.update("Certificate", cert, cert_value, "mail certificate")
    else:
        # Never repurpose an unrelated certificate merely because it is the
        # only existing object; create one owned by this deployment instead.
        cert_id = r.create("Certificate", cert_value, "mail certificate")

    listener_specs = [
        ("smtp", 25, "smtp", True, False),
        ("submission", 587, "smtp", True, False),
        ("submissions", 465, "smtp", True, True),
        ("imap", 143, "imap", True, False),
        ("imaps", 993, "imap", True, True),
        ("pop3", 110, "pop3", True, False),
        ("pop3s", 995, "pop3", True, True),
        ("sieve", 4190, "manageSieve", True, False),
        ("http", 8080, "http", True, False),
        ("https", 443, "http", True, True),
    ]
    listeners = api.get("NetworkListener")
    by_name: dict[str, dict[str, Any]] = {}
    for item in listeners:
        name = item.get("name")
        if name in by_name:
            raise RuntimeError(f"Duplicate x:NetworkListener name: {name}")
        by_name[name] = item
    for name, port, protocol, use_tls, implicit in listener_specs:
        value = {
            "name": name,
            "bind": {f"[::]:{port}": True},
            "protocol": protocol,
            "useTls": use_tls,
            "tlsImplicit": implicit,
        }
        current = by_name.get(name)
        if current:
            r.update("NetworkListener", current, value, f"listener {name}")
        else:
            r.create("NetworkListener", value, f"listener {name}")

    directory_value = {
        "@type": "Oidc",
        "description": env.get("MAIL_OIDC_DESCRIPTION", "Authentik"),
        "issuerUrl": issuer,
        "requireAudience": client_id,
        "requireScopes": {
            scope.strip(): True
            for scope in env.get("MAIL_OIDC_REQUIRED_SCOPES", "openid,profile").split(",")
            if scope.strip()
        },
        "claimUsername": env.get("MAIL_OIDC_USERNAME_CLAIM", "preferred_username"),
        "usernameDomain": env.get("MAIL_OIDC_USERNAME_DOMAIN", domain),
        "claimName": env.get("MAIL_OIDC_NAME_CLAIM", "name"),
        "claimGroups": env.get("MAIL_OIDC_GROUPS_CLAIM", "groups"),
    }
    directories = api.get("Directory")
    directory = find_one(
        directories,
        lambda item: item.get("@type") == "Oidc" and (
            item.get("issuerUrl") == issuer
            or item.get("description") == directory_value["description"]
        ),
        "OIDC directory",
    )
    if directory:
        directory_id = r.update("Directory", directory, directory_value, "OIDC directory")
    else:
        directory_id = r.create("Directory", directory_value, "OIDC directory")

    domains = api.get("Domain")
    domain_object = find_one(domains, lambda item: item.get("name") == domain, f"domain {domain}")
    domain_value = {"name": domain, "isEnabled": True, "directoryId": directory_id}
    if domain_object:
        domain_id = r.update("Domain", domain_object, domain_value, f"domain {domain}")
    else:
        domain_id = r.create("Domain", domain_value, f"domain {domain}")

    settings = api.get("SystemSettings", ["singleton"])
    if not settings:
        raise RuntimeError("x:SystemSettings singleton is missing")
    r.update(
        "SystemSettings",
        settings[0],
        {
            "defaultHostname": hostname,
            "defaultDomainId": domain_id,
            "defaultCertificateId": cert_id,
        },
        "system settings",
    )

    authentication = api.get("Authentication", ["singleton"])
    if not authentication:
        raise RuntimeError("x:Authentication singleton is missing")
    r.update(
        "Authentication",
        authentication[0],
        {"directoryId": directory_id},
        "default authentication directory",
    )

    # External OIDC groups do not implicitly grant Stalwart roles. Promote
    # only explicitly listed accounts, never a group merely named "admin".
    configured_admins = {
        value.strip().lower()
        for value in env.get("MAIL_OIDC_ADMIN_ACCOUNTS", "").split(",")
        if value.strip()
    }
    if configured_admins:
        accounts = api.get("Account")
        matched: set[str] = set()
        for account in accounts:
            identities = {
                str(account.get("name", "")).lower(),
                str(account.get("emailAddress", "")).lower(),
            }
            requested = configured_admins & identities
            if requested:
                if account.get("@type") != "User":
                    raise RuntimeError(
                        f"Configured OIDC admin is not a User: {account.get('name')}"
                    )
                matched.update(requested)
                r.update(
                    "Account",
                    account,
                    {"roles": {"@type": "Admin"}},
                    f"OIDC administrator {account.get('name')}",
                )
        missing = configured_admins - matched
        for value in sorted(missing):
            print(
                f"WARN  OIDC administrator {value} does not exist yet; "
                "sign in once and rerun deploy.sh"
            )

    applications = api.get("Application")
    application = find_one(
        applications,
        lambda item: "/admin" in item.get("urlPrefix", {}),
        "Stalwart WebUI application",
    )
    if not application:
        raise RuntimeError("Stalwart WebUI x:Application object is missing")
    r.update(
        "Application",
        application,
        {"oauthClientId": env.get("MAIL_WEBUI_OAUTH_CLIENT_ID", client_id)},
        "WebUI OAuth client",
    )

    # The webmail talks JMAP straight from the browser, and it is served from a
    # different origin than this server, so the browser needs CORS permission.
    # Without it the preflight succeeds but carries no Access-Control-Allow-Origin
    # and the browser discards every reply, which surfaces as a generic login
    # failure in the webmail rather than a server-side error here.
    if env.get("WEBMAIL_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}:
        http = api.get("Http", ["singleton"])
        if not http:
            raise RuntimeError("x:Http singleton is missing")
        r.update(
            "Http",
            http[0],
            {"usePermissiveCors": True},
            "permissive CORS for the browser JMAP client",
        )

    return r.finish()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["bootstrap", "apply"])
    parser.add_argument("--env-file", default=None)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent
    env_file = Path(args.env_file) if args.env_file else script_dir.parent.parent / ".env"
    env = load_env(env_file)
    credential = require(env, "MAIL_SERVER_ADMIN")
    if ":" not in credential:
        raise SystemExit("MAIL_SERVER_ADMIN must be formatted as user:password")
    api_url = env.get("MAIL_JMAP_URL", "http://127.0.0.1:8288/jmap/")
    api = Jmap(api_url, credential)

    try:
        if args.command == "bootstrap":
            return bootstrap(api, env, args.dry_run, args.check)
        return apply(api, env, args.dry_run, args.check)
    except RuntimeError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
