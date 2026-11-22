#!/usr/bin/env python3
"""End-to-end HTTP performance benchmark for the native Mesh Linux server."""
import base64
import concurrent.futures
import hashlib
import http.client
import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tests", "native"))
from check_http_listener import sign_with_oracle


def http_req(port, method, path, headers=None, body=b""):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, data
    finally:
        conn.close()


def login(port, oracle):
    keys = sign_with_oracle(oracle, "keypair")
    user_id = hashlib.sha256(base64.b64decode(keys["PublicKey"])).hexdigest()

    st, raw = http_req(port, "GET", f"/api/v1/sync/challenge?user_id={user_id}")
    assert st == 200, (st, raw)
    nonce = json.loads(raw)["nonce"]

    body = json.dumps({
        "user_id_hash": user_id,
        "client_id": "bench-client-1",
        "public_key": keys["PublicKey"],
    }).encode("utf-8")
    body_hash = hashlib.sha256(body).hexdigest()
    msg = f"daochi-sync-v1\nPOST\n/api/v1/sync/login\n{body_hash}\n{nonce}\n".encode("utf-8")
    sig = sign_with_oracle(oracle, "sign", PrivateKey=keys["PrivateKey"],
                           Message=base64.b64encode(msg).decode())["Signature"]
    st, raw = http_req(
        port,
        "POST",
        "/api/v1/sync/login",
        headers={
            "Content-Type": "application/json",
            "X-Mesh-User": user_id,
            "X-Mesh-Signature": sig,
        },
        body=body,
    )
    assert st == 200, (st, raw)
    token = json.loads(raw)["auth_token"]
    return user_id, token


def bench_concurrent(name, workers, total, fn):
    t0 = time.perf_counter()
    latencies = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(fn, i) for i in range(total)]
        for f in futs:
            latencies.append(f.result())
    dt = time.perf_counter() - t0
    latencies.sort()
    p50 = latencies[int(len(latencies) * 0.50)] * 1000
    p95 = latencies[int(len(latencies) * 0.95)] * 1000
    print(f"{name:38s}: {total:4d} reqs in {dt*1000:7.1f} ms | {total/dt:7.1f} req/s | p50={p50:5.2f} ms p95={p95:5.2f} ms")


def main():
    binary = sys.argv[1]
    oracle = sys.argv[2]
    with tempfile.TemporaryDirectory() as tmp:
        env = os.environ.copy()
        env.update({
            "MESH_ADDR": "127.0.0.1:0",
            "MESH_DB": os.path.join(tmp, "bench.db"),
            "MESH_TOKEN_SECRET_HEX": "11" * 32,
            "MESH_LAN_DISCOVERY": "0",
            "MESH_XMR_RATE_REFRESH_ENABLED": "0",
        })
        proc = subprocess.Popen(
            [binary, "serve"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            port = int(proc.stdout.readline().strip())
            user_id, token = login(port, oracle)
            auth_headers = {
                "Authorization": f"Bearer {token}",
                "X-Mesh-User": user_id,
                "Accept-Encoding": "zstd",
            }

            # W1: Healthz (pure HTTP / MHD baseline)
            def w_healthz(_):
                t = time.perf_counter()
                st, _ = http_req(port, "GET", "/healthz")
                assert st == 200
                return time.perf_counter() - t

            # W2: Authenticated export (Bearer auth + SQLite read + JSON + Zstd)
            def w_export(_):
                t = time.perf_counter()
                st, _ = http_req(port, "GET", "/api/v1/account/export", headers=auth_headers)
                assert st == 200, st
                return time.perf_counter() - t

            # W3: Authenticated encrypted envelope sync (Bearer auth + SQLite WAL write + Zstd)
            sync_headers = dict(auth_headers)
            sync_headers["Content-Type"] = "application/json"
            def w_sync(i):
                t = time.perf_counter()
                payload = json.dumps({
                    "v": 2,
                    "nonce": f"nonce-{i:06d}-aaaaaaaaaaaaaaaaaaaa",
                    "ciphertext": "B" * 2048,
                }).encode("utf-8")
                st, _ = http_req(port, "POST", "/api/v1/sync", headers=sync_headers, body=payload)
                assert st == 200, st
                return time.perf_counter() - t

            # W4: Repeated sync reads (with Since-Version)
            read_headers = dict(sync_headers)
            read_headers["X-Mesh-Since-Version"] = "0"
            read_payload = json.dumps({
                "v": 2,
                "nonce": "read-nonce-bbbbbbbbbbbbbbbbbbbb",
                "ciphertext": "C" * 256,
            }).encode("utf-8")
            def w_sync_read(_):
                t = time.perf_counter()
                st, _ = http_req(port, "POST", "/api/v1/sync", headers=read_headers, body=read_payload)
                assert st == 200, st
                return time.perf_counter() - t

            print(f"Benchmarking {binary} on 127.0.0.1:{port} (8 concurrent workers)")
            bench_concurrent("W1 GET /healthz (HTTP baseline)", 8, 800, w_healthz)
            bench_concurrent("W2 GET /api/v1/account/export (zstd)", 8, 250, w_export)
            bench_concurrent("W3 POST /api/v1/sync (2KiB envelope)", 8, 200, w_sync)
            bench_concurrent("W4 POST /api/v1/sync (delta+zstd)", 8, 80, w_sync_read)
        finally:
            proc.terminate()
            proc.wait(timeout=5)


if __name__ == "__main__":
    main()
