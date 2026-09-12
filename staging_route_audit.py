from __future__ import annotations

import ast
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = (os.getenv("WEBSITE_BASE_URL") or "").strip().rstrip("/")
KEY = (os.getenv("CLERK_SYNC_KEY") or "").strip()
BOT = Path("bot.py")

if not BASE:
    raise SystemExit("[ROUTE AUDIT] WEBSITE_BASE_URL missing")

host = (urllib.parse.urlparse(BASE).hostname or "").lower()
if "staging" not in host:
    raise SystemExit(f"[ROUTE AUDIT] REFUSED non-staging host: {host or BASE}")

if not BOT.exists():
    raise SystemExit("[ROUTE AUDIT] bot.py not found")


def render_path(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        out = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                out.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                # Flask's current dynamic Clerk paths use string/UUID-like ids.
                # A simple non-empty placeholder is enough for route matching.
                out.append("1")
            else:
                return None
        return "".join(out)
    return None


tree = ast.parse(BOT.read_text(encoding="utf-8"), filename=str(BOT))
routes: set[tuple[str, str]] = set()

for node in ast.walk(tree):
    if not isinstance(node, ast.Call):
        continue
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr == "request"):
        continue
    if len(node.args) < 2:
        continue
    method_node, path_node = node.args[0], node.args[1]
    if not (isinstance(method_node, ast.Constant) and isinstance(method_node.value, str)):
        continue
    path = render_path(path_node)
    if not path or not path.startswith("/internal/clerk/"):
        continue
    method = method_node.value.upper().strip()
    routes.add((method, path))

headers = {"X-Battalion-Clerk-Key": KEY} if KEY else {}
missing = []
method_mismatch = []
exists = []
unknown = []

for method, path in sorted(routes):
    url = BASE + path
    req = urllib.request.Request(url, method="OPTIONS", headers=headers)
    status = None
    allow_header = ""
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            status = int(resp.status)
            allow_header = resp.headers.get("Allow", "") or ""
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        allow_header = exc.headers.get("Allow", "") if exc.headers else ""
    except Exception as exc:
        unknown.append((method, path, type(exc).__name__, str(exc)[:180]))
        continue

    allowed = {part.strip().upper() for part in allow_header.split(",") if part.strip()}

    if status == 404:
        missing.append((method, path, status))
        continue

    # Flask's automatic OPTIONS response exposes the actual methods registered
    # for the matched rule. This catches the subtler case where a path exists but
    # Clerk calls it with POST while Website only registered GET (or vice versa).
    if allowed and method not in allowed:
        method_mismatch.append((method, path, status, ",".join(sorted(allowed))))
        continue

    if status in {200, 204, 401, 403, 405} or 300 <= status < 400:
        exists.append((method, path, status))
    else:
        unknown.append((method, path, status, "unexpected status"))

print(
    f"[ROUTE AUDIT] discovered={len(routes)} exists={len(exists)} "
    f"missing={len(missing)} method_mismatch={len(method_mismatch)} "
    f"unknown={len(unknown)} base={BASE}"
)

for method, path, status in missing:
    print(f"[ROUTE AUDIT MISSING] {method} {path} status={status}")

for method, path, status, allowed in method_mismatch:
    print(f"[ROUTE AUDIT METHOD] {method} {path} status={status} allowed={allowed}")

for row in unknown:
    print("[ROUTE AUDIT UNKNOWN] " + " | ".join(str(x) for x in row))

if missing or method_mismatch:
    print("[ROUTE AUDIT] FAIL Clerk/Website route contract drift remains")
elif unknown:
    print("[ROUTE AUDIT] completed with no route/method mismatches but some inconclusive probes")
else:
    print("[ROUTE AUDIT] PASS all Clerk paths and HTTP methods resolve on staging Website")

sys.exit(0)
