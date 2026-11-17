#!/usr/bin/env python3
"""Verify deployed canonical pages and assets against the working tree."""
import hashlib
from pathlib import Path
import time
from urllib.request import Request, urlopen

BASE = "https://mesh.ziran-lang.org"
FILES = (
    "index.html", "protocol.html", "mesh.html", "apps.html", "nodes.html",
    "tokens.html", "styles.css", "manifest.json", "nodes.json",
    "assets/mesh-logo-pixel-128.png", "assets/mesh-logo-pixel-64.png",
    "og-image.png", "social-card-square.png",
)


def verify():
    site = Path(__file__).resolve().parents[1] / "site"
    for name in FILES:
        route = "/" if name == "index.html" else "/" + name
        request = Request(BASE + route, headers={
            "Cache-Control": "no-cache",
            "User-Agent": "Mesh-Site-Verification/1.0 (+https://github.com/ziranlang/mesh)",
        })
        digest = hashlib.sha256()
        with urlopen(request, timeout=30) as response:
            # Pages normalizes *.html to the same-origin extensionless route.
            destinations = {BASE + route}
            if route.endswith(".html"):
                destinations.add(BASE + route[:-5])
            if response.url not in destinations:
                raise RuntimeError("Unexpected canonical-domain redirect")
            while chunk := response.read(65536):
                digest.update(chunk)
        if digest.digest() != hashlib.sha256((site / name).read_bytes()).digest():
            raise RuntimeError("Deployed bytes differ: " + name)


if __name__ == "__main__":
    for attempt in range(12):
        try:
            verify()
            print("Mesh canonical pages and assets verified by SHA-256")
            break
        except Exception:
            if attempt == 11:
                raise
            time.sleep(5)
