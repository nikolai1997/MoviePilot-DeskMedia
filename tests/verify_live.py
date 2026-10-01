#!/usr/bin/env python3
"""Verify a live DeskMedia deployment using environment-provided secrets."""

from __future__ import annotations

import json
import hashlib
import hmac
import os
import secrets
import urllib.request


def request_json(base_url: str, path: str, token: str, body: dict | None = None) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    method = "POST" if data is not None else "GET"
    nonce = secrets.token_hex(16)
    body_hash = hashlib.sha256(data or b"").hexdigest()
    canonical = f"{method}\n{path}\n{body_hash}\n{nonce}"
    signature = hmac.new(token.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}", data=data,
        headers={"X-Desk-Nonce": nonce, "X-Desk-Signature": signature,
                 "Accept": "application/json", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.headers.get("Cache-Control") != "private, no-store":
            raise RuntimeError("JSON response is missing the private no-store policy")
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    base_url = os.environ.get("DESKMEDIA_BASE_URL", "").strip()
    token = os.environ.get("DESKMEDIA_DEVICE_TOKEN", "").strip()
    if not base_url or not token:
        raise SystemExit("Set DESKMEDIA_BASE_URL and DESKMEDIA_DEVICE_TOKEN")
    health = request_json(base_url, "/health", token)
    summary: dict[str, object] = {"plugin": health.get("plugin"), "views": {}}
    for view, expected_type in (("movies", "movie"), ("tv", "tv"), ("subscribed", None)):
        result = request_json(base_url, f"/feed?view={view}", token)
        items = result.get("items") or []
        types = sorted({str(item.get("type")) for item in items if isinstance(item, dict)})
        if expected_type and any(item_type != expected_type for item_type in types):
            raise RuntimeError(f"{view} returned unexpected media types: {types}")
        summary["views"][view] = {"count": len(items), "types": types}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
