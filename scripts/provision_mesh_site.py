#!/usr/bin/env python3
"""Idempotently provision Mesh Pages and DNS without altering the former site."""
import json
import os
import re
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

PROJECT = "mesh"
DOMAIN = "mesh.ziran-lang.org"
ZONE = "ziran-lang.org"


class Cloudflare:
    def __init__(self, token):
        self.token = token

    def request(self, method, path, body=None, missing=False):
        payload = None if body is None else json.dumps(body).encode()
        request = Request("https://api.cloudflare.com/client/v4" + path, data=payload, method=method,
                          headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=30) as response:
                result = json.load(response)
        except HTTPError as error:
            if missing and error.code == 404:
                return None
            raise RuntimeError(f"Cloudflare {method} {path}: HTTP {error.code}; verify Pages/DNS token permissions") from None
        if not result.get("success"):
            raise RuntimeError(f"Cloudflare {method} {path} failed; verify Pages/DNS token permissions")
        return result["result"]


def provision(api, account):
    base = f"/accounts/{account}/pages/projects"
    project = api.request("GET", base + "/" + PROJECT, missing=True)
    if project is None:
        project = api.request("POST", base, {"name": PROJECT, "production_branch": "master"})
    target = project.get("subdomain", "")
    if not re.fullmatch(r"[a-z0-9-]+\.pages\.dev", target):
        raise RuntimeError("Pages did not return a valid project subdomain; DNS unchanged")
    zones = api.request("GET", "/zones?" + urlencode({"name": ZONE, "account.id": account}))
    if len(zones) != 1:
        raise RuntimeError("DNS zone ziran-lang.org unavailable; token needs Zone Read and DNS Edit for that zone")
    records = f'/zones/{zones[0]["id"]}/dns_records'
    existing = api.request("GET", records + "?" + urlencode({"name": DOMAIN}))
    if existing:
        if len(existing) != 1 or existing[0]["type"] != "CNAME" or existing[0]["content"].rstrip(".") != target:
            raise RuntimeError("mesh.ziran-lang.org has conflicting DNS; retained existing record")
    else:
        api.request("POST", records, {"type": "CNAME", "name": DOMAIN, "content": target, "proxied": True, "ttl": 1})
    domains = api.request("GET", base + "/" + PROJECT + "/domains")
    if not any(domain["name"] == DOMAIN for domain in domains):
        api.request("POST", base + "/" + PROJECT + "/domains", {"name": DOMAIN})
    print("Mesh Pages project and canonical domain configured; former site retained")


if __name__ == "__main__":
    provision(Cloudflare(os.environ["CLOUDFLARE_API_TOKEN"]), os.environ["CLOUDFLARE_ACCOUNT_ID"])
