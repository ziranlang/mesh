"""Exercise the native HTTP listener on a disposable loopback socket."""

import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing, contextmanager
import gzip
import hashlib
import hmac
import json
import os
import select
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from http.client import HTTPConnection
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlencode


UPGRADE = (
    b"GET /socket HTTP/1.1\r\nHost: localhost\r\nConnection: Upgrade\r\n"
    b"Upgrade: websocket\r\nSec-WebSocket-Version: 13\r\n"
    b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
    b"Sec-WebSocket-Protocol: inbe-sync-v1, mesh-sync-v1\r\n\r\n"
)


def frame_length(length):
    if length < 126:
        return bytes([length])
    return b"\x7e" + length.to_bytes(2, "big")


def masked_frame(payload, opcode=1):
    mask = b"mask"
    length = bytearray(frame_length(len(payload)))
    length[0] |= 0x80
    return bytes([0x80 | opcode]) + length + mask + bytes(value ^ mask[i % 4] for i, value in enumerate(payload))


# The family names the Go /metrics handler always writes. The by-reason
# families only appear after their first event, so they are checked by
# test_metrics_parity.py, not by this request fixture.
GO_METRIC_FAMILIES = (
    "mesh_build_info",
    "mesh_sync_requests_total",
    "mesh_sync_failures_total",
    "mesh_sync_full_snapshots_total",
    "mesh_sync_encrypted_records_applied_total",
    "mesh_sync_encrypted_payloads_applied_total",
    "mesh_rate_limited_requests_total",
    "mesh_auth_failures_total",
    "mesh_legacy_client_hints_total",
    "mesh_websocket_accepted_total",
    "mesh_websocket_rejected_total",
    "mesh_monero_stuck_invoices_total",
    "mesh_websocket_active",
    "mesh_connected_users",
    "mesh_registered_users",
    "mesh_active_users_30d",
    "mesh_registered_clients",
    "mesh_active_clients_30d",
    "mesh_websocket_connection_limit_per_user",
    "mesh_storage_database_total_bytes",
    "mesh_storage_sqlite_page_bytes",
    "mesh_storage_logical_bytes",
    "mesh_storage_encrypted_record_bytes",
    "mesh_storage_encrypted_payload_bytes",
    "mesh_storage_app_logical_bytes",
    "mesh_storage_collection_logical_bytes",
    "ksync_sync_requests_total",
    "ksync_sync_failures_total",
    "ksync_sync_full_snapshots_total",
    "ksync_sync_encrypted_records_applied_total",
    "ksync_sync_encrypted_payloads_applied_total",
    "ksync_rate_limited_requests_total",
    "ksync_auth_failures_total",
    "ksync_legacy_client_hints_total",
    "ksync_websocket_accepted_total",
    "ksync_websocket_rejected_total",
    "ksync_websocket_active",
    "mesh_http_requests_total",
    "mesh_http_request_duration_milliseconds_total",
    "ksync_http_requests_total",
    "ksync_http_request_duration_milliseconds_total",
)


NODE_CAPABILITIES = [
    "aliases", "friends", "v3-typed-sync", "v4-encrypted-records", "v4-dual-write-transition",
    "v5-encrypted-primary", "v5-private-hierarchy", "v5-dual-read", "v5-legacy-encrypted-collections",
    "protocol-v1-v5-valid-through-2027-09-01", "protocol-previous-version-grace-days-365",
    "v6-signed-transactions", "v6-signed-app-manifests", "profile-stats", "pub-relay",
    "node-mesh-encrypted-records", "node-identity-v1", "node-pairing-v1", "trust-space-names-v1",
    "monero-account-addresses", "monero-gifts", "record-pages-metadata-v1", "http-gzip-v1",
    "record-gzip-v1", "basic-storage-free-v1",
]


def read_response(peer):
    result = bytearray()
    while chunk := peer.recv(4096):
        result.extend(chunk)
        if len(result) > 16384:
            raise AssertionError("test listener exceeded the response limit")
    return result


def check_http(host, port):
    connection = HTTPConnection(host, port, timeout=3)
    try:
        for method, path, status, body in (
            ("GET", "/healthz", 200, b'{"status":"ok"}\n'),
            (
                "GET", "/readyz", 503,
                b'{"checks":{"database":"ok","token_issuer":"disabled",'
                b'"token_secret":"ephemeral","verifier":"ok"},"status":"not_ready"}\n',
            ),
            ("HEAD", "/healthz", 200, b""),
            ("GET", "/socket", 400, b'{"error":"websocket upgrade required"}\n'),
        ):
            connection.request(method, path)
            reply = connection.getresponse()
            assert (reply.status, reply.getheader("Content-Type"), reply.read()) == (status, "application/json", body)

        connection.request("GET", "/missing")
        reply = connection.getresponse()
        assert (reply.status, reply.read()) == (404, b"")
    finally:
        connection.close()

    with socket.create_connection((host, port), timeout=3) as peer:
        peer.sendall(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\nX-Padding: "
                     + b"x" * 70000 + b"\r\n\r\n")
        assert read_response(peer).startswith(b"HTTP/1.1 431 ")


def check_client_address(host, port):
    cases = (
        ({}, host),
        ({"CF-Connecting-IP": "::ffff:192.0.2.10", "X-Forwarded-For": "192.0.2.11"}, "192.0.2.10"),
        ({"CF-Connecting-IP": "bogus", "X-Forwarded-For": "203.0.113.9, 2001:0DB8:0:0::2"}, "2001:db8::2"),
        ({"X-Forwarded-For": "198.51.100.9, invalid"}, host),
    )
    connection = HTTPConnection(host, port, timeout=3)
    try:
        for headers, expected in cases:
            connection.request("GET", "/client?value=%61+b", headers=headers)
            response = connection.getresponse()
            assert (response.status, response.read()) == (200, f"{expected}\na b".encode())
    finally:
        connection.close()

    with socket.create_connection((host, port), timeout=3) as peer:
        peer.sendall(b"GET /client HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n"
                     b"X-Forwarded-For: 192.0.2.3\r\nx-forwarded-for: 192.0.2.4\r\n\r\n")
        response = read_response(peer)
        assert response.startswith(b"HTTP/1.1 200 ") and response.endswith(b"\r\n\r\n192.0.2.4\n"), response


def check_request_target(host, port):
    with closing(HTTPConnection(host, port, timeout=3)) as connection:
        for target in ("/tar%67et?value=a+b&value=%2b&empty=", "/target?z=2&x=%2F", "/target"):
            connection.request("GET", target)
            response = connection.getresponse()
            assert (response.status, response.read()) == (200, target.encode("ascii"))


def make_bearer(payload):
    def encode(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    return f"Bearer {encode(payload)}.{encode(hmac.digest(bytes(32), payload, 'sha256'))}"


def check_authentication(host, port, database):
    user = "0123456789abcdef" * 4
    fresh = "d" * 64
    mismatch = "f" * 64
    v2 = make_bearer(f"v2\n{user}\n200\n100\n{'1' * 64}\n".encode("ascii"))
    v1 = make_bearer(f"v1\n{user}\n300\n".encode("ascii"))
    bootstrap = make_bearer(f"v1\n{fresh}\n300\n".encode("ascii"))
    tombstone = make_bearer(f"v1\n{'e' * 64}\n300\n".encode("ascii"))
    connection = HTTPConnection(host, port, timeout=3)

    def expect(path, headers, status, expected):
        connection.request("GET", path, headers=headers)
        reply = connection.getresponse()
        actual = reply.status, reply.getheader("Content-Type"), json.loads(reply.read())
        assert actual == (status, "application/json", expected), (path, actual)

    account = {"user": user, "bootstrap": False}
    invalid = {"error": "invalid bearer token"}
    missing = {"error": "sync account not found"}
    try:
        expect("/account", {}, 401, {"error": "bearer token required"})
        expect("/account", {"Authorization": v2[:-1]}, 401, invalid)
        expect("/account", {"Authorization": v2}, 200, account)
        for name in ("X-Mesh-User", "X-Ksync-User", "X-Inbe-User"):
            expect("/account", {"Authorization": v2, name: user.upper()}, 200, account)
        expect("/account", {"Authorization": v2, "X-Mesh-User": mismatch, "X-Ksync-User": user},
               401, {"error": "token user mismatch"})
        expect("/account", {"Authorization": v1}, 200, account)
        expect("/account", {"Authorization": bootstrap}, 401, missing)
        expect("/api/v1/sync", {"Authorization": bootstrap}, 200, {"user": fresh, "bootstrap": True})
        expect("/api/v1/sync", {"Authorization": tombstone}, 401, missing)
        with sqlite3.connect(database) as writer:
            writer.execute("UPDATE server_auth_sessions SET revoked_at='revoked' WHERE issued_at=100")
        expect("/account", {"Authorization": v2}, 401, invalid)
    finally:
        connection.close()


def sign_with_oracle(oracle, operation, **values):
    request = {"Operation": operation, **values}
    process = subprocess.run([oracle], input=json.dumps([request]).encode(),
                             capture_output=True, timeout=5, check=True)
    result, = json.loads(process.stdout)
    assert not result["Error"]
    return result


def check_signed_records(request, database, oracle, keys, user, auth, device_auth, notification=None):
    path = "/api/v1/sync"
    devices_path = "/api/v1/account/devices"
    app = "inbe"
    device_id = "record-device"
    client_id = "record-client"
    device = sign_with_oracle(oracle, "device-keypair")
    public_key = base64.b64decode(device["PublicKey"]).hex()

    def device_change(nonce, revoke=False, target=app):
        expiry = int(time.time()) + 60
        operation = "revocation" if revoke else "registration"
        fields = [f"daochi-device-{operation}-v1", user, target, device_id]
        payload = {"app_id": target, "device_key_id": device_id, "nonce": nonce, "expires_at": expiry}
        if not revoke:
            fields += [client_id, public_key]
            payload.update(client_id=client_id, public_key=public_key.upper())
        message = "\n".join(fields + [nonce, str(expiry), ""])
        payload["signature"] = sign_with_oracle(oracle, "sign", PrivateKey=keys["PrivateKey"],
                                                Message=base64.b64encode(message.encode()).decode())["Signature"]
        return payload

    assert request("GET", devices_path) == (401, {"error": "bearer token required"})
    assert request("GET", devices_path, headers=auth) == (200, {"devices": None})
    registration = device_change("enrollment")
    tampered = json.dumps(dict(registration, client_id="different-client")).encode()
    tampered_result = request("POST", devices_path, tampered, auth)
    assert tampered_result == (401, {"error": "device registration rejected"}), tampered_result
    body = json.dumps(registration).encode()
    status, enrolled = request("POST", devices_path, gzip.compress(body), auth | {"Content-Encoding": "gzip"})
    assert status == 200 and enrolled == {"account_id": user, "app_id": app, "client_id": client_id,
                                         "device_key_id": device_id, "public_key": public_key}, (status, enrolled)
    assert request("POST", devices_path, body, auth) == (409, {"error": "device registration replay"})
    status, listed = request("GET", devices_path, headers=device_auth)
    assert status == 200 and listed["devices"][0]["created_at"] and "revoked_at" not in listed["devices"][0]

    scope = {"protocol_version": 6, "app_id": app, "client_id": client_id, "client_clock": 42, "encrypted_records_only": True}
    first = {"collection": f"private.{app}.v1.records.sample", "id": "first", "ciphertext": "first-identity",
             "nonce": "identity-nonce", "packed_ciphertext": "first-packed", "packed_nonce": "packed-nonce",
             "updated_at": "2026-01-01T00:00:00Z"}
    second = dict(first, id="second", ciphertext="second-identity", packed_ciphertext="second-packed")

    def signed_envelope(sequence, payload, route, method="POST", target=app):
        expiry = int(time.time()) + 60
        digest = hashlib.sha256(payload).hexdigest()
        tx_id = f"records-tx-{sequence}"
        nonce = f"records-nonce-{sequence}"
        message = "\n".join(("daochi-tx-v1", "6", tx_id, user, target, device_id, method, route, digest, nonce, str(expiry), ""))
        encoded = base64.b64encode(message.encode()).decode()
        return {"protocol_version": 6, "tx_id": tx_id, "account_id": user, "app_id": target,
                "device_key_id": device_id, "method": method, "path": route, "body_sha256": digest,
                "nonce": nonce, "expires_at": expiry,
                "signature": sign_with_oracle(oracle, "sign", PrivateKey=keys["PrivateKey"], Message=encoded)["Signature"],
                "device_signature": sign_with_oracle(oracle, "device-sign", PrivateKey=device["PrivateKey"], Message=encoded)["Signature"]}

    def proof(sequence, payload, padded=False, route=path, method="POST", target=app):
        transaction = signed_envelope(sequence, payload, route, method, target)
        header = base64.urlsafe_b64encode(json.dumps(transaction).encode()).decode()
        return {**auth, "X-Mesh-User": user.upper(), "X-Mesh-Tx": header if padded else header.rstrip("=")}

    overflow = dict(first, ciphertext="f" * 2000)
    payload = json.dumps(dict(scope, encrypted_records=[overflow])).encode()
    status, error = request("POST", path, payload, proof("full", payload))
    assert status == 413 and error == {"error": "account storage limit reached", "code": "storage_full",
                                        "account_storage": {"used_bytes": 0, "limit_bytes": 1000}}, (status, error)

    cross_scope = dict(first, collection="private.otherapp.v1.records.sample")
    payload = json.dumps(dict(scope, encrypted_records=[cross_scope])).encode()
    status, error = request("POST", path, payload, proof("scope", payload))
    assert status == 400 and error["error"] == "encrypted record collection belongs to another app", (status, error)

    payload = json.dumps(dict(scope, encrypted_records=[first, second], record_limit=1)).encode()
    headers = proof("upload", payload)
    assert request("POST", path, payload + b" ", headers) == (401, {"error": "signed transaction body mismatch"})
    status, page = request("POST", path, gzip.compress(payload), headers | {"Content-Encoding": "gzip", "Accept-Encoding": "gzip"})
    assert status == 200 and page["status"] == "ok" and page["server_version"] == 2, (status, page)
    if notification:
        _, stream = notification
        assert read_sync_event(stream) == {"type": "sync_changed", "user_id_hash": user, "server_version": 2}
    assert page["applied"]["encrypted_records"] == 2 and not page["changes_complete"]
    assert page["changes"]["encrypted_records"][0]["ciphertext"] == "first-identity"
    assert "package_sequence" not in page
    assert request("POST", path, payload, headers) == (409, {"error": "signed transaction replay"})

    packages = Path(f"{database}.packages")
    root = packages / app
    root.mkdir(parents=True)
    latest = root / "latest-v2.json"
    release = json.dumps({"app_id": app, "sequence": 23}).encode()
    latest.write_bytes(release)
    payload = json.dumps(dict(scope, record_cursor=page["record_cursor"], record_encoding="gzip-v1", include_metadata=True)).encode()
    status, last = request("POST", path, payload, proof("next", payload, padded=True))
    assert status == 200 and last["changes_complete"] and "record_cursor" not in last, (status, last)
    assert last["changes"]["encrypted_records"][0]["ciphertext"] == "second-packed"
    assert last["changes"]["social_cache"][0]["json"] == {"friends": []}
    assert last["package_sequence"] == 23
    with sqlite3.connect(database) as store:
        assert store.execute("SELECT COUNT(*) FROM server_signed_transactions WHERE account_id=?", (user,)).fetchone() == (2,)
        rows = store.execute("SELECT client_id,app_id,client_clock,server_version,applied_json FROM server_sync_audit WHERE user_id_hash=? ORDER BY id",
                             (user,)).fetchall()
        assert [(row[:4], json.loads(row[4]).get("encrypted_records", 0)) for row in rows] == [
            ((client_id, app, 42, 2), 2), ((client_id, app, 42, 2), 0)]

    def check_no_package(case):
        code, result = request("POST", path, payload, proof(case, payload))
        assert code == 200 and "package_sequence" not in result, (case, code, result)

    for case, document in (
        ("wrong-app", b'{"app_id":"different","sequence":24}'),
        ("non-integer", b'{"app_id":"inbe","sequence":"24"}'),
        ("oversize-metadata", release + b" " * 65536),
    ):
        latest.write_bytes(document)
        check_no_package(case)
    latest.unlink()
    check_no_package("missing-package")
    outside = packages / "outside"
    outside.mkdir()
    outside_release = outside / latest.name
    outside_release.write_bytes(release)
    latest.symlink_to(outside_release)
    check_no_package("linked-release")
    latest.unlink()
    root.rmdir()
    root.symlink_to(outside, target_is_directory=True)
    check_no_package("linked-app-directory")

    if notification:
        shared = dict(first, id="shared", collection="shared.inbe.v1.records.sample")
        update = json.dumps(dict(scope, since_server_version=2, encrypted_records=[shared])).encode()
        status, sync = request("POST", path, update, proof("shared-upload", update))
        assert status == 200 and sync["server_version"] == 3, (status, sync)
        assert read_sync_event(notification[1]) == {"type": "sync_changed", "user_id_hash": user, "server_version": 3}
        enrollment = json.dumps(device_change("enroll-entity", target="entity")).encode()
        status, _ = request("POST", devices_path, enrollment, auth)
        assert status == 200
        check_app_grants(request, database, user, auth, signed_envelope, proof, shared)

    if notification:
        signed_op = {"op_id": "signed-typed-op", "client_id": client_id, "seq": 1, "entity_type": "extension",
                     "entity_id": "sample", "op_type": "upsert", "payload": {"sample": True}}
        typed = json.dumps(dict(scope, encrypted_records_only=False, client_clock=0, ops=[signed_op])).encode()
        assert request("POST", path, typed, auth | {"X-Mesh-User": user}) == (401, {"error": "signed transaction required"})
        signed_headers = proof("typed", typed)
        with sqlite3.connect(database) as store:
            store.execute("CREATE TRIGGER test_signed_typed_rollback BEFORE INSERT ON server_sync_ops BEGIN SELECT RAISE(ABORT,'test rollback'); END")
        assert request("POST", path, typed, signed_headers) == (500, {"error": "sync failed"})
        with sqlite3.connect(database) as store:
            store.execute("DROP TRIGGER test_signed_typed_rollback")
        status, typed_sync = request("POST", path, typed, signed_headers)
        assert status == 200 and typed_sync["accepted_ops"] == ["signed-typed-op"] and typed_sync["server_version"] == 4, (status, typed_sync)
        assert request("POST", path, typed, signed_headers) == (409, {"error": "signed transaction replay"})

    if notification:
        check_blobs(request, database, user, auth, proof)

    revocation = json.dumps(device_change("revocation", revoke=True)).encode()
    assert request("DELETE", devices_path, revocation, auth) == (200, {"status": "revoked"})
    assert request("GET", devices_path, headers=device_auth) == (401, {"error": "invalid bearer token"})
    if notification:
        assert notification[1].read(1) == b""
    assert request("DELETE", devices_path, revocation, auth) == (409, {"error": "device revocation replay"})
    assert request("POST", devices_path, json.dumps(device_change("enrollment-after-revoke")).encode(), auth) == (409, {"error": "device key revoked"})
    status, listed = request("GET", devices_path, headers=auth)
    assert status == 200
    assert next(device for device in listed["devices"] if device["app_id"] == app)["revoked_at"]
    assert request("POST", path, payload, proof("revoked", payload)) == (401, {"error": "device key not registered"})


def check_blobs(request, database, user, auth, proof):
    base = "/api/v1/blobs/"
    folder = Path(f"{database}.blobs") / user
    with sqlite3.connect(database) as store:
        row = store.execute("SELECT used_bytes FROM server_storage_usage WHERE user_id_hash=?", (user,)).fetchone()
    baseline = row[0] if row else 0
    size = 1000 - baseline - 4
    assert size > 4
    first, second = b"a" * size, b"b" * size

    def path(data, app="inbe"):
        return base + app + "/" + hashlib.sha256(data).hexdigest()

    def signed(method, route, sequence, data=b"", **options):
        return request(method, route, data, proof(f"blob-{sequence}", data, route=route, method=method), **options)

    primary, alternate = path(first), path(second)
    primary_file = folder / "inbe" / primary.rsplit("/", 1)[1]
    assert request("GET", primary) == (401, {"error": "bearer token required"})
    assert request("GET", primary, headers=auth) == (401, {"error": "signed transaction required"})
    assert request("GET", base + "inbe/short", headers=auth) == (400, {"error": "invalid blob address"})
    assert request("POST", primary, headers=auth) == (405, {"error": "unsupported blob method"})
    assert signed("PUT", primary, "wrong-hash", second) == (400, {"error": "blob hash mismatch"})
    assert signed("PUT", path(b""), "empty") == (413, {"error": "blob exceeds limit or is empty"})
    large = b"large" * 1700
    status, limit = signed("PUT", path(large), "large", large)
    assert status == 413 and limit["code"] == "storage_full"
    protocol_limit = b"x" * (16777216 + 1)
    assert request("PUT", primary, protocol_limit, auth) == (413, {"error": "blob exceeds limit or is empty"})

    with sqlite3.connect(database) as store:
        store.execute("CREATE TRIGGER test_blob_rollback BEFORE INSERT ON server_blobs "
                      "BEGIN SELECT RAISE(ABORT,'test blob rollback'); END")
    headers = proof("blob-upload", first, route=primary, method="PUT")
    assert request("PUT", primary, first, headers) == (507, {"error": "could not store blob"})
    assert not primary_file.exists()
    with sqlite3.connect(database) as store:
        store.execute("DROP TRIGGER test_blob_rollback")
    assert request("PUT", primary, first, headers) == (204, None)
    assert request("PUT", primary, first, headers) == (409, {"error": "signed transaction replay"})
    assert signed("PUT", primary, "duplicate", first) == (204, None)
    assert primary_file.read_bytes() == first
    status, reply, data = signed("GET", primary, "download", raw=True)
    assert status == 200 and reply.getheader("Cache-Control") == "private, no-store" and data == first
    status, reply, data = signed("HEAD", primary, "head", raw=True)
    assert status == 200 and int(reply.getheader("Content-Length")) == len(first) and data == b""
    assert signed("GET", path(first, "entity"), "scoped") == (404, {"error": "blob not found"})
    primary_file.write_bytes(second)
    assert signed("GET", primary, "corrupt") == (503, {"error": "blob integrity failure"})
    assert signed("PUT", primary, "repair", first) == (204, None)
    status, limited = signed("PUT", alternate, "over-quota", second)
    assert (status, limited) == (413, {"error": "account storage limit reached", "code": "storage_full",
                                      "account_storage": {"used_bytes": baseline + size, "limit_bytes": 1000}})
    assert signed("POST", primary + "/trash", "trash") == (204, None)
    assert signed("GET", primary, "download-trash", raw=True)[2] == first
    assert signed("PUT", alternate, "second", second) == (204, None)
    assert signed("POST", primary + "/restore", "restore")[0] == 413
    assert signed("DELETE", alternate, "delete-second") == (204, None)
    assert signed("POST", primary + "/restore", "restore") == (204, None)

    with sqlite3.connect(database) as store:
        store.execute("CREATE TRIGGER test_blob_delete_rollback BEFORE DELETE ON server_blobs "
                      "BEGIN SELECT RAISE(ABORT,'test blob delete rollback'); END")
    assert signed("DELETE", primary, "delete-first") == (500, {"error": "blob storage unavailable"})
    assert primary_file.is_file()
    with sqlite3.connect(database) as store:
        store.execute("DROP TRIGGER test_blob_delete_rollback")
    assert signed("DELETE", primary, "delete-first") == (204, None)
    assert signed("DELETE", primary, "delete-missing") == (204, None)
    assert not primary_file.exists()
    entity = path(b"shared account ciphertext", "entity")
    assert signed("PUT", entity, "entity-write", b"shared account ciphertext") == (204, None)
    assert signed("GET", entity, "entity-read", raw=True)[2] == b"shared account ciphertext"
    assert signed("DELETE", entity, "entity-delete") == (204, None)
    with sqlite3.connect(database) as store:
        row = store.execute("SELECT used_bytes FROM server_storage_usage WHERE user_id_hash=?", (user,)).fetchone()
        assert row == (baseline,)
        assert store.execute("SELECT COUNT(*) FROM server_blobs WHERE user_id_hash=?", (user,)).fetchone() == (0,)
    assert not list(folder.rglob(".upload-*")) and not list(folder.rglob(".delete-*"))


def check_app_grants(request, database, user, auth, sign, proof, shared):
    path = "/api/v1/account/app-grants"
    assert request("GET", path) == (401, {"error": "bearer token required"})
    assert request("GET", path, headers=auth) == (200, {"grants": []})
    grant = {"source_app_id": "inbe", "target_app_id": "entity", "collection_prefix": "shared.inbe.v1.*"}
    records = "/api/v1/account/app-records"
    query = f"{records}?{urlencode(grant)}"
    read_proof = proof("granted-records", b"", route=records, method="GET", target="entity")
    assert request("GET", query, headers=auth) == (401, {"error": "signed transaction required"})
    assert request("GET", query, headers=read_proof) == (403, {"error": "app grant required"})
    private = dict(grant, collection_prefix="private.inbe.v1.*")
    assert request("POST", path, json.dumps(private).encode(), auth) == (
        400, {"error": "private collections cannot be granted across apps"})
    absent = dict(grant, target_app_id="missing")
    assert request("POST", path, json.dumps(absent).encode(), auth) == (404, {"error": "app not found"})
    status, created = request("POST", path, gzip.compress(json.dumps(grant).encode()), auth | {"Content-Encoding": "gzip"})
    assert status == 201, (status, created)
    expected = dict(grant, permission="read", user_id_hash=user, status="active")
    assert {name: created[name] for name in expected} == expected
    assert request("GET", path, headers=auth) == (200, {"grants": [created]})
    status, granted = request("GET", query, headers=read_proof)
    assert status == 200 and len(granted["records"]) == 1, (status, granted)
    record, = granted["records"]
    assert record["id"] == shared["id"] and record["ciphertext"] == shared["ciphertext"]
    assert request("GET", query, headers=read_proof) == (409, {"error": "signed transaction replay"})
    assert request("DELETE", f"{path}/{created['id']}", headers=auth) == (200, {"status": "revoked"})
    revoked = proof("revoked-records", b"", route=records, method="GET", target="entity")
    assert request("GET", query, headers=revoked) == (403, {"error": "app grant required"})
    assert request("DELETE", f"{path}/{created['id']}", headers=auth) == (404, {"error": "app grant not found"})

    signed_path = f"{path}/signed"
    signed_grant = dict(grant, target_app_id="inbe", permission="read")
    canonical = json.dumps(signed_grant, separators=(",", ":")).encode()
    tx = sign("grant", canonical, signed_path)
    body = json.dumps({"tx": dict(tx, body_sha256="0" * 64), "grant": signed_grant}).encode()
    assert request("POST", signed_path, body, auth) == (400, {"error": "signed transaction body hash must cover grant payload"})
    tx.pop("body_sha256")  # The Go contract also accepts an omitted hash.
    body = json.dumps({"tx": tx, "grant": signed_grant}).encode()
    with sqlite3.connect(database) as store:
        store.execute("CREATE TRIGGER test_grant_rollback BEFORE INSERT ON server_app_grant_audit BEGIN SELECT RAISE(ABORT,'test rollback'); END")
    assert request("POST", signed_path, body, auth) == (500, {"error": "app grant failed"})
    with sqlite3.connect(database) as store:
        assert store.execute("SELECT id,status FROM server_app_grants WHERE user_id_hash=?", (user,)).fetchall() == [(created["id"], "revoked")]
        assert store.execute("SELECT 1 FROM server_signed_transactions WHERE tx_id=?", (tx["tx_id"],)).fetchone() is None
        store.execute("DROP TRIGGER test_grant_rollback")
    status, signed = request("POST", signed_path, body, auth)
    assert status == 201 and signed["status"] == "active", (status, signed)
    assert request("POST", signed_path, body, auth) == (409, {"error": "signed transaction replay"})
    assert request("DELETE", f"{path}/{signed['id']}", headers=auth) == (200, {"status": "revoked"})
    with sqlite3.connect(database) as store:
        rows = store.execute("SELECT grant_id,action,payload_json FROM server_app_grant_audit WHERE user_id_hash=? ORDER BY id", (user,)).fetchall()
        assert [(identifier, action, json.loads(payload)) for identifier, action, payload in rows] == [
            (created["id"], "create", dict(grant, permission="read")), (created["id"], "revoke", {}),
            (signed["id"], "create", signed_grant), (signed["id"], "revoke", {})]


def check_sync_diagnostics(request, database, user, other, auth):
    path = "/api/v1/sync/diagnostics"
    assert request("GET", path) == (401, {"error": "bearer token required"})
    assert request("GET", path, headers=auth | {"X-Mesh-User": other}) == (401, {"error": "token user mismatch"})
    assert request("POST", path, headers=auth) == (405, None)
    assert request("HEAD", path, headers=auth) == (200, None)
    with sqlite3.connect(database) as store:
        store.execute("UPDATE server_users SET created_at='2020-01-01T00:00:00.000000000Z' WHERE user_id_hash=?", (other,))
        store.executemany("INSERT INTO server_habits(user_id_hash,id,name,color_r,updated_at) VALUES(?,?,?,?,?)", (
            (user, "h", "Méditer", 45, "sample"), (other, "h", "excluded", 9, "sample"),
        ))
        store.execute("INSERT INTO server_habit_days(user_id_hash,habit_id,local_date,completed,count,updated_at) VALUES(?,?,?,?,?,?)",
                      (user, "h", 20260102, 1, 4, "sample"))
        store.executemany("INSERT INTO server_sessions(user_id_hash,id,started_at,topic,updated_at) VALUES(?,?,?,?,?)", (
            (user, "s2", "start", "second", "end"), (user, "s1", "start", "first", "end"),
        ))
        store.executemany("INSERT INTO server_session_rounds VALUES(?,?,?,?,?)", (
            (user, "s2", 3, 5, 60), (user, "s1", 1, 20, 10), (user, "s1", 0, 32, 30),
        ))
        store.execute("INSERT INTO server_meditation_logs(id,user_id_hash,session_id,duration_seconds,completed_at) VALUES(?,?,?,?,?)",
                      ("m", user, "s1", 50, "end"))
        store.execute("INSERT INTO server_sync_compaction(user_id_hash,compacted_through_version) VALUES(?,?)", (user, 1))
        store.executemany("INSERT INTO server_encrypted_payloads(user_id_hash,client_id,payload_json,server_version) VALUES(?,?,?,?)", (
            (user, "older", '{"payload_index":%d}' % i, i + 2) for i in range(6)
        ))
        store.execute("INSERT INTO server_encrypted_payloads(user_id_hash,payload_json) VALUES(?,?)", (other, '"private"'))
        store.executemany("INSERT INTO server_sync_audit(user_id_hash,protocol_version,applied_json,server_version) VALUES(?,?,?,?)", (
            (user, 2, '{"HABITS":3,"encrypted_records":4,"future":55}', i) for i in range(12)
        ))
        store.execute("INSERT INTO server_sync_audit(user_id_hash,protocol_version) VALUES(?,99)", (other,))
        store.execute("UPDATE server_clients SET protocol_version=6 WHERE user_id_hash=? AND client_id='record-client'", (user,))
        version = store.execute("SELECT server_version FROM server_sync_state WHERE user_id_hash=?", (user,)).fetchone()[0]
        record_rows = store.execute("SELECT collection,id,key_id,nonce,ciphertext,updated_at,deleted_at,content_hash,schema_version,parent_id "
                                    "FROM server_encrypted_records WHERE user_id_hash=? ORDER BY collection,id", (user,)).fetchall()
    legacy = make_bearer(f"v1\n{other}\n{int(time.time()) + 60}\n".encode("ascii"))
    assert request("HEAD", path, headers={"Authorization": legacy}) == (200, None)
    expected = [
        ("habit", "h", "Méditer", 45, 0, 0, 0, 0, 0, 0, 0, "sample"),
        ("habit_day", "h", 20260102, 1, 4, "sample"),
        ("session", "s1", "start", 0, "first", 0, "", "", 0, 0, 0, 0, "", "", 0, "end"),
        ("session", "s2", "start", 0, "second", 0, "", "", 0, 0, 0, 0, "", "", 0, "end"),
        ("round", "s1", 0, 32, 30), ("round", "s1", 1, 20, 10), ("round", "s2", 3, 5, 60),
        ("meditation_log", "m", "s1", 50, "end"),
        ("social_cache", "blob", '{"key": 1}', "sample"),
        ("social_cache", "invalid-utf8", b"\xff", "sample"),
        ("social_cache", "text", '{"key": 1}', "sample"),
    ] + [("encrypted_record", *row) for row in record_rows]
    canonical = b"\n".join(b"\t".join(value if isinstance(value, bytes) else str(value).encode() for value in row)
                           for row in expected) + b"\n"
    status, result = request("GET", path, headers=auth | {"Accept-Encoding": "gzip"})
    assert status == 200, (status, result)
    assert result["status"] == "ok" and result["user_id_hash"] == user and result["server_version"] == version
    assert result["state_hash"] == hashlib.sha256(canonical).hexdigest(), result
    assert result["active_websocket_supported"] is True and result["compacted_through_version"] == 1
    assert result["table_counts"]["server_habits"] == 1 and result["table_counts"]["server_session_rounds"] == 3
    assert result["table_counts"]["server_encrypted_payloads"] == 6 and result["encrypted_payload_bytes"] == 19 * 6
    assert result["legacy_clients"] == ["native-login"]
    assert [item["payload"] for item in result["recent_encrypted_payloads"]] == [{"payload_index": i} for i in range(5, 0, -1)]
    recent = result["recent_sync_audit"]
    assert [row["server_version"] for row in recent] == list(range(11, 1, -1))
    assert recent[0]["applied"] == {"meditation_logs": 0, "habits": 3, "habit_days": 0,
                                    "sessions": 0, "social_cache": 0, "encrypted_records": 4}


def check_typed_sync(request, database, user, other):
    path = "/api/v1/sync"
    token = make_bearer(f"v1\n{user}\n{int(time.time()) + 60}\n".encode("ascii"))
    headers = {"Authorization": token, "X-Ksync-User": user.upper()}
    updated = "2026-02-01T11:12:13+01:00"
    habit = {"id": "reading", "name": "Reading", "updated_at": updated}
    day = {"habit_id": "reading", "local_date": 20260201, "completed": True, "count": 0, "updated_at": updated}
    session = {"id": "first-session", "updated_at": updated, "rounds": [{"round_index": 1, "breaths": 5, "hold_seconds": 20}]}
    meditation = {"id": "morning", "session_id": "first-session", "duration": 60, "timestamp": updated}
    operation = {"op_id": "first-op", "client_id": "typed-client", "seq": 1, "entity_type": "habit",
                 "op_type": "upsert", "payload": {"id": "op-habit", "name": "From op", "updated_at": updated, "extra": [1, "two"]}}

    def send(expected=200, version=2, **changes):
        payload = json.dumps({"client_id": "typed-client", "protocol_version": version, **changes}).encode()
        status, result = request("POST", path, gzip.compress(payload), headers | {"Content-Encoding": "gzip", "Accept-Encoding": "gzip"})
        assert status == expected, (status, expected, result, changes)
        return result

    assert send(400, user_id_hash=other)["error"].endswith("does not match user_id_hash")
    assert send(400, app_id="unregistered-app")["error"] == "unknown app_id"
    assert send(400, social_cache=[{}])["error"] == "social_cache is server-owned"
    foreign = {"collection": "private.entity.v1.records.sample", "id": "first", "nonce": "n", "ciphertext": "c", "updated_at": updated}
    assert send(400, encrypted_records=[foreign])["error"] == "entity records need a signed request"
    seeded = send(habits=[habit], habit_days=[day], sessions=[session], meditation_logs=[meditation], ops=[operation])
    assert seeded["applied"] == {"habits": 2, "habit_days": 1, "sessions": 1, "meditation_logs": 1, "social_cache": 0}, seeded
    assert seeded["accepted_ops"] == ["first-op"] and seeded["legacy_write_required"]
    version, digest = seeded["server_version"], seeded["server_state_hash"]
    viewed = seeded["changes"]
    canonical = next(row["id"] for row in viewed["habits"] if row["name"] == "Reading")
    assert canonical != habit["id"] and viewed["habit_days"][0]["habit_id"] == canonical
    assert viewed["habit_days"][0]["completed"] is True and viewed["habit_days"][0]["count"] == 1
    assert viewed["meditation_logs"][0]["completed_at"] == "2026-02-01T10:12:13.000000000Z"
    assert viewed["meditation_logs"][0]["duration_seconds"] == 60 and viewed["sessions"][0]["rounds"] == session["rounds"]
    remote = seeded["ops"][0]
    assert remote["entity_id"] == remote["payload"]["id"] and remote["entity_id"] != "op-habit", remote
    assert remote["payload"]["extra"] == [1, "two"]
    repeated = send(since_server_version=version, last_server_state_hash=digest.upper(), ops=[operation])
    assert repeated["server_version"] == version and repeated["applied"]["habits"] == 0 and repeated["accepted_ops"] == ["first-op"]
    assert repeated["changes"]["habits"] == [] and repeated["base_state_hash"] == digest
    with sqlite3.connect(database) as store:
        store.execute("CREATE TRIGGER test_typed_rollback BEFORE INSERT ON server_sync_ops BEGIN SELECT RAISE(ABORT,'test rollback'); END")
    send(500, sessions=[dict(session, id="rolled-back")], ops=[dict(operation, op_id="second-op", seq=2)])
    with sqlite3.connect(database) as store:
        store.execute("DROP TRIGGER test_typed_rollback")
        assert store.execute("SELECT COUNT(*) FROM server_sessions WHERE id='rolled-back'").fetchone() == (0,)
        assert store.execute("SELECT server_version FROM server_sync_state WHERE user_id_hash=?", (user,)).fetchone() == (version,)
    changed = send(sessions=[dict(session, topic="updated")])
    bridged = send(since_server_version=version, last_server_state_hash=digest)
    assert bridged["changes_complete"] is True and bridged["full_snapshot_required"] is False, bridged
    assert bridged["changes"]["sessions"][0]["topic"] == "updated"
    deleted = send(version=3, habits=[dict(habit, deleted_at=1)], habit_days=[day],
                   ops=[dict(operation, op_id="delete-op", seq=2, op_type="delete")])
    assert deleted["applied"]["habits"] == 3 and deleted["applied"]["habit_days"] == 0, deleted
    assert deleted["data"]["habit_days"] == [] and deleted["changes"]["habits"] == deleted["data"]["habits"]
    assert deleted["data"]["friends"] == {"friends": []}
    assert {entry["kind"] for entry in deleted["logs"]} == {"op"} and deleted["deletes"][0]["kind"] == "delete"
    with sqlite3.connect(database) as store:
        assert store.execute("SELECT COUNT(*) FROM server_state_hashes WHERE user_id_hash=? AND server_version=?",
                             (user, changed["server_version"])).fetchone() == (0,)
    stale = send(since_server_version=1, last_server_state_hash="0" * 64)
    assert stale["full_snapshot_required"] is True and stale["changes_complete"] is False
    assert stale["diagnostics"]["snapshot_reason"] == "state_hash_mismatch"
    compacted = send()
    assert compacted["diagnostics"]["snapshot_reason"] == "sync_ops_compacted" and "ops" not in compacted
    with sqlite3.connect(database) as store:
        assert store.execute("SELECT COUNT(*) FROM server_sync_ops WHERE user_id_hash=?", (user,)).fetchone() == (0,)
        assert store.execute("SELECT name FROM server_habits WHERE user_id_hash=? AND id='h'", (other,)).fetchone() == ("Méditer",)
    record = dict(foreign, collection="inbe.habits")
    write_clock = compacted["server_version"]
    dual = send(version=4, app_id="inbe", client_clock=write_clock, habits=[habit], encrypted_records=[record])
    assert dual["transition_mode"] == "dual_write" and dual["changes"]["encrypted_records"][0]["ciphertext"] == "c"
    reduced = send(version=5, client_clock=write_clock)
    assert reduced["transition_mode"] == "encrypted_primary" and reduced["data"]["habits"] == []
    shown = send(version=5, client_clock=write_clock, include_legacy_data=True)
    assert any(row["name"] == "Reading" for row in shown["data"]["habits"])
    blocked = send(413, habits=[dict(habit, id="blocked")], encrypted_records=[dict(record, collection="inbe.sessions", ciphertext="x" * 1200)])
    assert blocked["code"] == "storage_full"
    replaced = send(version=3, full_sync_requested=True, sessions=[session])
    assert replaced["data"]["habit_days"] == [] and len(replaced["data"]["sessions"]) == 1
    with sqlite3.connect(database) as store:
        assert store.execute("SELECT COUNT(*) FROM server_habits WHERE user_id_hash=?", (user,)).fetchone() == (0,)
        assert store.execute("SELECT COUNT(*) FROM server_encrypted_records WHERE user_id_hash=?", (user,)).fetchone() == (0,)


def check_encrypted_sync(request, host, port, database, user, auth):
    path = "/api/v1/sync"
    envelope = {"v": 2, "nonce": "opaque-nonce", "ciphertext": "opaque-data"}
    body = json.dumps(envelope).encode()
    headers = auth | {"X-Ksync-User": user.upper(), "X-Ksync-Client": "mailbox-client"}
    assert request("POST", path, body, auth) == (400, {"error": "missing X-Mesh-User"})
    assert request("POST", path, body, headers | {"X-Mesh-User": "bad"}) == (400, {"error": "invalid X-Mesh-User"})
    invalid_limit = request("POST", path, body, headers | {"X-Mesh-Limit": "0"})
    assert invalid_limit == (400, {"error": "invalid X-Mesh-Limit"}), invalid_limit
    assert request("POST", path, body, headers | {"X-Mesh-Since-Version": "9223372036854775808"}) == (
        400, {"error": "invalid X-Mesh-Since-Version"})
    too_large = json.dumps(dict(envelope, ciphertext="x" * 500)).encode()
    assert request("POST", path, gzip.compress(too_large), headers | {"Content-Encoding": "gzip"}) == (
        413, {"error": "encrypted payload quota exceeded"})

    with sqlite3.connect(database) as store:
        latest = store.execute("SELECT server_version FROM server_sync_state WHERE user_id_hash=?", (user,)).fetchone()[0]
        store.execute("INSERT INTO server_encrypted_payloads(user_id_hash,payload_json,created_at,server_version) VALUES(?,?,?,?)",
                      (user, '"expired"', "2001-01-01T00:00:00.000000000Z", latest))
        store.execute("CREATE TRIGGER reject_payload BEFORE INSERT ON server_encrypted_payloads BEGIN SELECT RAISE(ABORT,'test'); END")
    assert request("POST", path, body, headers) == (500, {"error": "encrypted sync failed"})
    with sqlite3.connect(database) as store:
        assert store.execute("SELECT server_version FROM server_sync_state WHERE user_id_hash=?", (user,)).fetchone() == (latest,)
        store.execute("DROP TRIGGER reject_payload")

    with ExitStack() as resources:
        _, stream = open_sync_socket(resources, host, port, auth["Authorization"].removeprefix("Bearer "))
        assert read_sync_event(stream)["type"] == "sync_ready"
        status, page = request("POST", path, gzip.compress(body), headers | {"Content-Encoding": "gzip", "X-Ksync-Limit": "1"})
        assert status == 200, (status, page)
        assert page["transition_mode"] == "encrypted_payload" and page["server_version"] == latest + 1
        assert page["encrypted_payloads_truncated"] and page["encrypted_payloads_next_since_version"] == 2
        assert [item["payload"] for item in page["encrypted_payloads"]] == [{"payload_index": 0}]
        assert page["account_alias"] == "RestoredName" and page["profile_icon"] == 2 and page["changes_complete"] is True
        assert read_sync_event(stream) == {"type": "sync_changed", "user_id_hash": user, "server_version": latest + 1}

    status, page = request("POST", path, body, headers | {"X-Ksync-Since-Version": f"+{latest + 1}", "X-Mesh-Limit": "9"})
    assert status == 200, (status, page)
    assert page["server_version"] == latest + 2 and page["encrypted_payloads"][0]["server_version"] > latest + 1
    # Legacy fixtures can have versions ahead of the account. The requested
    # limit is clamped to two, and the new envelope is ordered with those rows.
    assert len(page["encrypted_payloads"]) <= 2
    with sqlite3.connect(database) as store:
        assert store.execute("SELECT COUNT(*) FROM server_encrypted_payloads WHERE user_id_hash=? AND payload_json='\"expired\"'",
                             (user,)).fetchone() == (0,)
        assert store.execute("SELECT COUNT(*) FROM server_encrypted_payloads WHERE user_id_hash=? AND payload_json=?",
                             (user, body.decode())).fetchone() == (2,)
        assert store.execute("SELECT protocol_version,last_sync_at IS NOT NULL FROM server_clients WHERE user_id_hash=? AND client_id='mailbox-client'",
                             (user,)).fetchone() == (6, 1)
        assert store.execute("SELECT encrypted_payload,encrypted_payload_bytes FROM server_sync_audit WHERE user_id_hash=? ORDER BY id DESC LIMIT 1",
                             (user,)).fetchone() == (1, len(body))


def check_passkeys(request, user, auth):
    path = "/api/v1/account/passkeys"
    opening = "/api/v1/account-backup/passkey/open"
    proof = hashlib.sha256(b"passkey recovery test proof").digest()
    credential = hashlib.sha256(b"passkey recovery test credential").hexdigest()
    verifier = hashlib.sha256(proof).hexdigest()

    def copy(credential_id, label="Security key", size=1):
        wrapper = ("waozi-account-passkey-v1\nalgorithm=ML-DSA-44\n"
                   "kdf=WebAuthn-PRF-SHA-256\ncredential=" + credential_id + "\n" + "x" * size)
        return {"credential": credential_id, "label": label, "verifier": verifier, "wrapper": wrapper}

    def put(item, compress=False):
        payload = json.dumps(item).encode()
        headers = auth
        if compress:
            payload = gzip.compress(payload)
            headers = auth | {"Content-Encoding": "gzip"}
        return request("POST", path, payload, headers)

    assert request("GET", path) == (401, {"error": "bearer token required"})
    assert request("POST", path, b"{}") == (401, {"error": "bearer token required"})
    assert request("GET", path, headers=auth) == (200, {"passkeys": []})
    status, response, contents = request("HEAD", path, headers=auth, raw=True)
    assert status == 200 and not contents and response.getheader("Cache-Control") == "private, no-store"
    status, response, contents = request("PUT", path, headers=auth, raw=True)
    assert status == 405 and not contents and response.getheader("Allow") == "GET, HEAD, POST"
    original = copy(credential, size=12000)
    assert put(original | {"label": "invalid!"}) == (400, {"error": "invalid passkey copy"})
    assert put(original | {"wrapper": copy("b" * 64)["wrapper"]}) == (400, {"error": "invalid passkey copy"})
    assert put(copy(credential, size=18000), compress=True) == (400, {"error": "invalid passkey copy"})
    assert put(original) == (200, {"stored": True})
    status, listed = request("GET", path, headers=auth)
    assert status == 200 and listed["passkeys"] == [
        {"credential": credential, "label": "Security key", "created_at": listed["passkeys"][0]["created_at"]},
    ]
    created_at = listed["passkeys"][0]["created_at"]
    assert abs(created_at - int(time.time())) <= 5
    for item in range(7):
        assert put(copy(f"{item:064x}")) == (200, {"stored": True})
    assert put(copy("e" * 64)) == (409, {"error": "too many passkeys"})
    latest = original | {"label": None}
    assert put(latest, compress=True) == (200, {"stored": True})
    status, listed = request("GET", path, headers=auth)
    assert status == 200 and len(listed["passkeys"]) == 8
    assert {row["credential"]: row for row in listed["passkeys"]}[credential] == {
        "credential": credential, "label": "", "created_at": created_at,
    }
    valid_open = {"account": user, "credential": credential, "proof": proof.hex()}
    status, response, contents = request("POST", opening, json.dumps(valid_open), raw=True)
    assert status == 200 and response.getheader("Cache-Control") == "private, no-store"
    assert json.loads(contents) == {"account_id": user, "wrapper": latest["wrapper"]}
    for item in [valid_open | {"proof": "0" * 64}, valid_open | {"account": "f" * 64},
                 valid_open | {"credential": "e" * 64}]:
        assert request("POST", opening, json.dumps(item)) == (403, {"error": "passkey not recognized"})
    duplicate = json.dumps(valid_open)[:-1] + r', "pr\u006fof": null}'
    assert request("POST", opening, duplicate) == (400, {"error": "invalid passkey request"})
    assert request("POST", opening, json.dumps(valid_open | {"extra": None})) == (400, {"error": "invalid passkey request"})
    remove_body = json.dumps({"credential": credential})
    assert request("POST", path + "/remove", remove_body) == (401, {"error": "bearer token required"})
    assert request("POST", path + "/remove", remove_body, auth) == (200, {"deleted": True})
    assert request("POST", path + "/remove", remove_body, auth) == (200, {"deleted": False})
    for _ in range(14):
        assert request("POST", opening, json.dumps(valid_open)) == (403, {"error": "passkey not recognized"})
    status, response, contents = request("POST", opening, json.dumps(valid_open), raw=True)
    assert status == 429 and response.getheader("Retry-After") == "60"
    assert response.getheader("Cache-Control") == "private, no-store"
    assert json.loads(contents) == {"error": "too many attempts", "retry_after": 60}


def check_password_backup(request, database, user, auth):
    path = "/api/v1/account/backup"
    opening = "/api/v1/account-backup/open"
    salt_route = "/api/v1/account-backup/salt"
    wrong = (403, {"error": "wrong username or password"})
    proof = hashlib.sha256(b"password recovery test proof").digest()
    verifier = hashlib.sha256(proof).hexdigest()
    salt = "0123456789abcdef" * 2
    backup = ("waozi-account-backup-v3\nalgorithm=ML-DSA-44\nkdf=Argon2id\n"
              f"memory_kib=65536\npasses=3\nlanes=1\nsalt={salt}\nnonce={'ab' * 12}\n"
              f"ciphertext={'fe' * 6000}")
    item = {"backup": backup, "salt": salt.upper(), "verifier": verifier.upper()}

    def account_salt(name):
        status, response, contents = request("POST", salt_route, json.dumps({"account": name}), raw=True)
        assert status == 200 and response.getheader("Cache-Control") == "private, no-store"
        return json.loads(contents)["salt"]

    def opened(name, guess):
        return request("POST", opening, json.dumps({"account": name, "proof": guess}))

    def put(entry):
        return request("POST", path, gzip.compress(json.dumps(entry).encode()), auth | {"Content-Encoding": "gzip"})

    def failed_attempts():
        with sqlite3.connect(database) as store:
            return store.execute("SELECT failed_attempts,locked_until FROM server_account_backups WHERE user_id_hash=?", (user,)).fetchone()

    assert request("GET", path) == (401, {"error": "bearer token required"})
    assert request("GET", path, headers=auth) == (200, {"stored": False, "updated_at": 0})
    decoy = account_salt(user)
    assert account_salt(" @RESTOREDNAME ") == decoy and account_salt("@@RestoredName") == decoy
    unknown = account_salt("Unknown_Account")
    assert account_salt("unknown_account") == unknown and unknown != decoy
    with sqlite3.connect(database) as store:
        secret = store.execute("SELECT secret FROM server_account_backup_secret WHERE id=1").fetchone()[0]
    expected = hmac.digest(secret, f"account-backup-salt\naccount:{user}".encode(), "sha256")[:16].hex()
    assert decoy == expected
    assert request("POST", salt_route, b'{"account":"a","accou\\u006et":null}') == (400, {"error": "invalid account"})
    assert opened(user, "not-hex") == (400, {"error": "invalid proof"})
    for invalid in (backup.replace("passes=3", "passes=1"), backup[:-1], backup + "h0"):
        assert put(item | {"backup": invalid}) == (400, {"error": "invalid account backup"})
    status, written = put(item)
    assert status == 200 and written["stored"] and abs(written["updated_at"] - int(time.time())) <= 5
    assert request("GET", path, headers=auth) == (200, written)
    assert account_salt(user) == salt and account_salt("restoredname") == salt
    assert opened("RESTOREDNAME", proof.hex().upper()) == (200, {"account_id": user, "backup": backup})
    assert failed_attempts() == (0, 0)
    with sqlite3.connect(database) as store:
        store.execute("CREATE TRIGGER test_backup_rollback BEFORE UPDATE OF locked_until ON server_account_backups "
                      "WHEN NEW.failed_attempts=0 BEGIN SELECT RAISE(ABORT, 'test rollback'); END")
    assert opened(user, proof.hex()) == (500, {"error": "account backup unavailable"})
    assert failed_attempts() == (0, 0)
    with sqlite3.connect(database) as store:
        store.execute("DROP TRIGGER test_backup_rollback")
    for _ in range(5):
        assert opened("RestoredName", "0" * 64) == wrong
        assert opened("unknown_account", proof.hex()) == wrong
    for name in (user, "unknown_account"):
        status, response, contents = request("POST", opening, json.dumps({"account": name, "proof": proof.hex()}), raw=True)
        assert status == 429 and response.getheader("Cache-Control") == "private, no-store"
        body = json.loads(contents)
        assert body["error"] == "too many attempts" and 1 <= body["retry_after"] <= 60
        assert int(response.getheader("Retry-After")) == body["retry_after"]
    failures, locked = failed_attempts()
    assert failures == 5 and 0 < locked - int(time.time()) <= 60
    with sqlite3.connect(database) as store:
        store.execute("UPDATE server_account_backups SET locked_until=0 WHERE user_id_hash=?", (user,))
    assert opened(user, "0" * 64) == wrong
    failures, locked = failed_attempts()
    assert failures == 6 and 60 < locked - int(time.time()) <= 120
    assert request("DELETE", path, headers=auth) == (200, {"deleted": True})
    assert request("DELETE", path, headers=auth) == (200, {"deleted": False})
    assert account_salt(user) == decoy
    status, locked = opened("restoredname", proof.hex())
    assert status == 429 and 60 < locked["retry_after"] <= 120
    assert put(item)[0] == 200
    assert opened(user, proof.hex()) == (200, {"account_id": user, "backup": backup})


def check_signed_login(host, port, database, oracle, server_mode=False):
    path = "/api/v1/sync/login"
    sign_out = "/api/v1/sync/logout"
    keys = sign_with_oracle(oracle, "keypair")
    user = hashlib.sha256(base64.b64decode(keys["PublicKey"])).hexdigest()
    challenge = f"/api/v1/sync/challenge?user_id={user.upper()}"
    connection = HTTPConnection(host, port, timeout=3)

    def request(method, url, body=None, headers=None, raw=False):
        connection.request(method, url, body, headers or {})
        response = connection.getresponse()
        content = response.read()
        if response.getheader("Content-Encoding") == "gzip":
            content = gzip.decompress(content)
        if raw:
            return response.status, response, content
        return response.status, json.loads(content) if content else None

    def proof(payload, signed_path=path, method="POST"):
        status, issued = request("GET", challenge)
        assert status == 200 and issued["user_id_hash"] == user
        assert issued["expires_in_seconds"] == 60 and len(bytes.fromhex(issued["nonce"])) == 32
        digest = hashlib.sha256(payload).hexdigest()
        message = f"ksync-sync-v1\n{method}\n{signed_path}\n{digest}\n{issued['nonce']}\n".encode()
        signed = sign_with_oracle(oracle, "sign", PrivateKey=keys["PrivateKey"],
                                  Message=base64.b64encode(message).decode())
        return {"X-Ksync-User": user.upper(), "X-Ksync-Signature": signed["Signature"]}

    try:
        assert request("HEAD", challenge) == (200, None)
        assert request("GET", "/api/v1/sync/challenge?user_id=bad") == (400, {"error": "invalid user_id"})
        assert request("POST", path, b"{") == (400, {"error": "invalid json"})
        assert request("POST", path, b"{}") == (400, {"error": "missing X-Mesh-User"})
        assert request("POST", path, b"{}", {"X-Mesh-User": user}) == (400, {"error": "invalid client_id"})
        assert request("POST", path, b" " * 8193) == (400, {"error": "request body too large"})
        assert request("POST", path, b"{}", {"Content-Encoding": "wrong"}) == (400, {"error": "unsupported content encoding"})
        payload = json.dumps({"USER_ID_HASH": user.upper(), "client_id": "native-login", "public_key": keys["PublicKey"]}).encode()
        headers = proof(payload)
        assert request("POST", path, payload + b" ", headers) == (401, {"error": "signature rejected"})
        status, session = request("POST", path, gzip.compress(payload), headers | {"Content-Encoding": "gzip", "Accept-Encoding": "gzip"})
        assert status == 200 and session["status"] == "ok" and session["expires_in_seconds"] == 3600
        assert abs(session["server_time"] - int(time.time())) <= 5
        status, failure = request("POST", path, payload, headers)
        assert status == 401 and failure == {"error": "signature rejected"}
        auth = {"Authorization": f"Bearer {session['auth_token']}"}
        device_payload = json.dumps({"client_id": "record-client", "public_key": keys["PublicKey"]}).encode()
        status, device_session = request("POST", path, device_payload, proof(device_payload))
        assert status == 200
        device_auth = {"Authorization": f"Bearer {device_session['auth_token']}"}
        with ExitStack() as notifications:
            notification = None
            if server_mode:
                assert request("GET", "/api/v1/sync/ws?token=invalid", headers=device_auth) == (401, {"error": "websocket query tokens are not accepted"})
                notification = open_sync_socket(notifications, host, port, device_session["auth_token"])
                peer, stream = notification
                assert read_sync_event(stream) == {"type": "sync_ready", "user_id_hash": user, "server_version": 0}
                peer.sendall(masked_frame(b"heartbeat", opcode=9))
                assert read_server_frame(stream) == (10, b"heartbeat")
            check_signed_records(request, database, oracle, keys, user, auth, device_auth, notification)
        if server_mode:
            check_passkeys(request, user, auth)

        # The listener fixture restores a legacy hold. The standalone server
        # creates a hold through the original deletion endpoint first.
        held_dir = Path(f"{database}.held")
        held_file = held_dir / f"{user}.db"
        profile = "/api/v1/account/profile-icon"
        profile_headers = auth | {"X-Ksync-User": user.upper()}
        assert request("GET", profile) == (405, None)
        assert request("POST", profile, b"{}", device_auth) == (400, {"error": "missing X-Mesh-User"})
        mismatched = json.dumps({"user_id_hash": "0" * 64, "profile_icon": 129}).encode()
        assert request("POST", profile, mismatched, profile_headers) == (400, {"error": "X-Ksync-User does not match user_id_hash"})
        assert request("POST", profile, b'{"profile_icon": "41"}', profile_headers) == (400, {"error": "invalid json"})
        assert request("POST", profile, b'{"profile_icon": 66}', profile_headers) == (400, {"error": "invalid profile_icon"})
        assert request("POST", profile, b'{"profile_icon": 129}', device_auth | {"X-Mesh-User": user}) == (401, {"error": "invalid bearer token"})
        body = json.dumps({"USER_ID_HASH": user.upper(), "PROFILE_ICON": 129}).encode()
        assert request("POST", profile, gzip.compress(body), profile_headers | {"Content-Encoding": "gzip"}) == (200, {"status": "ok", "profile_icon": 129})
        with sqlite3.connect(database) as store:
            assert store.execute("SELECT profile_icon FROM server_users WHERE user_id_hash=?", (user,)).fetchone() == (129,)
            assert store.execute("SELECT client_id FROM server_clients WHERE user_id_hash=? ORDER BY client_id", (user,)).fetchall() == [("native-login",), ("record-client",)]
            store.execute("UPDATE server_users SET alias='RestoredName' WHERE user_id_hash=?", (user,))
        assert request("POST", profile, b'{"profile_icon": 2}', profile_headers) == (200, {"status": "ok", "profile_icon": 2})
        if server_mode:
            check_password_backup(request, database, user, auth)
        export = "/api/v1/account/export"
        assert request("GET", export) == (401, {"error": "bearer token required"})
        assert request("GET", export, headers=device_auth) == (401, {"error": "invalid bearer token"})
        other = hashlib.sha256(b"foreign export fixture").hexdigest()
        assert request("GET", export, headers=auth | {"X-Mesh-User": other}) == (401, {"error": "token user mismatch"})
        with sqlite3.connect(database) as store:
            store.execute("INSERT INTO server_users(user_id_hash, public_key) VALUES(?, ?)", (other, b"fixture key"))
            store.execute("INSERT INTO server_profile_stats(user_id_hash,app,practice,metric,value) VALUES(?,?,?,?,?)", (user, "inbe", "fixture", "test", 1.25))
            store.executemany("INSERT INTO server_social_snapshots(user_id_hash,kind,json,updated_at) VALUES(?,?,?,?)", (
                (user, "text", '{"key": 1}', "sample"),
                (user, "blob", b'{"key": 1}', "sample"),
                (user, "invalid-utf8", b'\xff', "sample"),
                (other, "excluded", '{"private": true}', "sample"),
            ))
        assert request("HEAD", export, headers=auth) == (200, None)
        status, exported = request("GET", export, headers=auth | {"Accept-Encoding": "gzip"})
        assert status == 200 and exported["user_id_hash"] == user and exported["status"] == "ok", (status, exported)
        assert exported["account_alias"] == "RestoredName" and exported["profile_icon"] == 2
        tables = exported["tables"]
        assert len(tables["users"]) == 1 and tables["users"][0]["user_id_hash"] == user
        assert "public_key" not in tables["users"][0] and "auth_sessions" not in tables
        assert "account_passkeys" not in tables and "passkeys" not in tables
        assert "account_backups" not in tables and "backups" not in tables
        assert len(tables["clients"]) == 2 and tables["habits"] == [] and "game_completions" not in tables
        assert tables["profile_stats"][0]["value"] == 1.25
        assert {row["kind"]: row["json"] for row in tables["social_snapshots"]} == {
            "text": '{"key": 1}', "blob": {"key": 1}, "invalid-utf8": "\ufffd",
        }
        if server_mode:
            check_sync_diagnostics(request, database, user, other, auth)
            check_encrypted_sync(request, host, port, database, user, auth)
            check_typed_sync(request, database, other, user)
        with sqlite3.connect(database) as store:
            store.execute("PRAGMA foreign_keys=ON")
            store.execute("DELETE FROM server_users WHERE user_id_hash=?", (other,))
        assert request("POST", sign_out, headers=auth) == (200, {"status": "ok"})
        assert request("POST", sign_out, headers=auth) == (401, {"error": "invalid bearer token"})
        live_blob = Path(f"{database}.blobs") / user / "fixture"
        if server_mode:
            live_blob.parent.mkdir(parents=True, exist_ok=True)
            live_blob.write_bytes(b"blob on hold")
            delete = "/api/v1/account/delete"
            body = json.dumps({"user_id_hash": user}).encode()
            with sqlite3.connect(database) as store:
                store.execute("CREATE TRIGGER test_delete_rollback BEFORE DELETE ON server_users BEGIN SELECT RAISE(ABORT,'test rollback'); END")
            assert request("POST", delete, body, proof(body, delete)) == (500, {"error": "delete failed"})
            assert live_blob.read_bytes() == b"blob on hold" and not held_file.exists()
            with sqlite3.connect(database) as store:
                assert store.execute("SELECT COUNT(*) FROM server_users WHERE user_id_hash=?", (user,)).fetchone() == (1,)
                store.execute("DROP TRIGGER test_delete_rollback")
            delete_headers = proof(body, delete)
            assert request("POST", delete, body + b" ", delete_headers) == (401, {"error": "signature rejected"})
            status, deleted = request("POST", delete, gzip.compress(body), delete_headers | {"Content-Encoding": "gzip"})
            assert status == 200 and deleted == {"status": "deleted"}, (status, deleted)
            assert not live_blob.exists() and (held_dir / f"{user}.blobs" / "fixture").read_bytes() == b"blob on hold"
            assert held_file.is_file()
            with sqlite3.connect(database) as store:
                assert store.execute("SELECT held_at,purge_after FROM server_account_holds WHERE user_id_hash=?", (user,)).fetchone()[1] > int(time.time())
                assert store.execute("SELECT COUNT(*) FROM server_users WHERE user_id_hash=?", (user,)).fetchone() == (0,)
                assert store.execute("SELECT COUNT(*) FROM server_account_passkeys WHERE user_id_hash=?", (user,)).fetchone() == (0,)
                assert store.execute("SELECT COUNT(*) FROM server_account_backups WHERE user_id_hash=?", (user,)).fetchone() == (0,)
        else:
            held_dir.mkdir()
            with sqlite3.connect(database) as store:
                store.execute("PRAGMA foreign_keys=ON")
                store.execute("ATTACH DATABASE ? AS held", (str(held_file),))
                store.execute("CREATE TABLE held.server_users AS SELECT * FROM main.server_users WHERE user_id_hash=?", (user,))
                store.execute("CREATE TABLE held.server_clients AS SELECT * FROM main.server_clients WHERE user_id_hash=?", (user,))
                store.execute("DELETE FROM server_users WHERE user_id_hash=?", (user,))
                store.execute("INSERT INTO server_account_holds VALUES(?,100,200)", (user,))
                store.execute("INSERT INTO server_account_tombstones(user_id_hash) VALUES(?)", (user,))
                store.commit()
                store.execute("DETACH DATABASE held")
        payload = json.dumps({"client_id": "restored-login", "public_key": keys["PublicKey"], "restore": True}).encode()
        status, restored = request("POST", path, payload, proof(payload))
        assert status == 200 and restored["account_alias"] == "RestoredName"
        assert restored["profile_icon"] == 2 and "held_until" not in restored and not held_file.exists()
        if server_mode:
            assert live_blob.read_bytes() == b"blob on hold"
        with sqlite3.connect(database) as store:
            devices = store.execute("SELECT client_id FROM server_clients WHERE user_id_hash=? ORDER BY client_id", (user,)).fetchall()
            expected = [("native-login",), ("record-client",), ("restored-login",)]
            if server_mode:
                expected.insert(0, ("mailbox-client",))
            assert devices == expected, devices

        if server_mode:
            legacy_delete = "/api/v1/account"
            body = json.dumps({"user_id_hash": user}).encode()
            assert request("GET", legacy_delete) == (404, {"error": "not found"})
            status, response, contents = request("POST", legacy_delete, raw=True)
            assert (status, contents, response.getheader("Allow")) == (405, b"Method Not Allowed\n", "DELETE, GET, HEAD")
            assert request("DELETE", legacy_delete, body, proof(body, "/api/v1/account/delete", "DELETE")) == (401, {"error": "signature rejected"})
            assert request("DELETE", legacy_delete, body, proof(body, legacy_delete, "DELETE")) == (200, {"status": "deleted"})
            assert held_file.is_file()
            status, restored = request("POST", path, payload, proof(payload))
            assert status == 200 and restored["profile_icon"] == 2 and not held_file.exists()
            delete_with_key = "/api/v1/account/delete-with-key"
            wrong_key = sign_with_oracle(oracle, "keypair")["PrivateKey"]

            def key_file(private_key):
                return json.dumps({"user_id_hash": user.upper(), "exported_key": "\r\n".join((
                    "account-key-v1", "algorithm=ML-DSA-44", f"public_id={user.upper()}", f"private_key={private_key}"))}).encode()

            assert request("POST", delete_with_key, key_file(wrong_key)) == (401, {"error": "exported key does not match sync account"})
            assert request("POST", delete_with_key, key_file(keys["PrivateKey"])) == (200, {"status": "deleted"})
            status, restored = request("POST", path, payload, proof(payload))
            assert status == 200 and restored["account_alias"] == "RestoredName"
            encrypted_key = "\n".join((
                "mesh-account-key-encrypted-v1", "algorithm=ML-DSA-44", f"public_id={user}",
                "kdf=Argon2id", "memory_kib=65536", "passes=3", "lanes=1", "salt=" + "12" * 16,
                "nonce=" + "34" * 24, "ciphertext=" + "ab" * 2576, ""))
            encrypted_body = json.dumps({"user_id_hash": user, "exported_key": encrypted_key}).encode()
            signed_delete = proof(encrypted_body, delete_with_key)
            assert request("POST", delete_with_key, encrypted_body, signed_delete) == (200, {"status": "deleted"})
            assert held_file.is_file()
            status, restored = request("POST", path, payload, proof(payload))
            assert status == 200 and restored["account_alias"] == "RestoredName"
            status, response = request("POST", delete_with_key, encrypted_body, signed_delete)
            assert (status, response["error"]) in ((400, "missing or expired challenge"), (401, "signature rejected"))
            assert live_blob.read_bytes() == b"blob on hold" and not held_file.exists()

        # A separate connecting client gets its own bucket for the same user.
        for index in range(20):
            status, _ = request("GET", challenge, headers={"X-Forwarded-For": f"192.0.2.{index + 1}, 198.51.100.3"})
            assert status == 200
        assert request("GET", challenge, headers={"X-Forwarded-For": "192.0.2.99, 198.51.100.3"}) == (429, {"error": "rate limit exceeded"})
        assert request("GET", challenge, headers={"X-Forwarded-For": "198.51.100.4"})[0] == 200
        return restored["auth_token"]
    finally:
        connection.close()


def check_upgrade_response(response, payload):
    headers, body = response.split(b"\r\n\r\n", 1)
    assert headers.startswith(b"HTTP/1.1 101 "), headers
    assert b"Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=" in headers, headers
    assert b"Sec-WebSocket-Protocol: mesh-sync-v1" in headers, headers
    expected = b"\x81" + frame_length(len(payload)) + payload if payload else b""
    assert body == expected, body


def open_sync_socket(resources, host, port, bearer):
    peer = resources.enter_context(socket.create_connection((host, port), timeout=3))
    stream = resources.enter_context(peer.makefile("rb"))
    request = UPGRADE.replace(b"GET /socket ", b"GET /api/v1/sync/ws ")
    request = request.replace(b"inbe-sync-v1, mesh-sync-v1", b"mesh-sync-v1, bearer." + bearer.encode())
    peer.sendall(request)
    response = stream.readline(512)
    assert response.startswith(b"HTTP/1.1 101 "), response
    headers = {}
    for _ in range(32):
        line = stream.readline(512)
        if line == b"\r\n":
            break
        name, value = line.rstrip().split(b":", 1)
        headers[name.lower()] = value.strip()
    assert headers[b"sec-websocket-protocol"] == b"mesh-sync-v1"
    assert headers[b"sec-websocket-accept"] == b"s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
    return peer, stream


def read_server_frame(stream):
    prefix = stream.read(2)
    assert len(prefix) == 2 and prefix[0] & 0x80 and prefix[1] < 127, prefix
    opcode = prefix[0] & 0x0f
    length = prefix[1] if prefix[1] < 126 else int.from_bytes(stream.read(2), "big")
    assert length <= 1024, length
    payload = stream.read(length)
    assert len(payload) == length
    return opcode, payload


def read_sync_event(stream):
    opcode, payload = read_server_frame(stream)
    assert opcode == 1, opcode
    return json.loads(payload)


def check_websocket(host, port):
    address = (host, port)
    with socket.create_connection(address, timeout=3) as peer:
        peer.sendall(UPGRADE + masked_frame(b"buffered"))
        check_upgrade_response(read_response(peer), b"buffered")

    with socket.create_connection(address, timeout=3) as peer:
        payload = bytes(range(128))
        frame = masked_frame(payload)
        peer.sendall(UPGRADE + frame[:1])
        time.sleep(0.03)
        peer.sendall(frame[1:9])
        time.sleep(0.03)
        peer.sendall(frame[9:])
        check_upgrade_response(read_response(peer), payload)

    with socket.create_connection(address, timeout=3) as peer:
        peer.sendall(UPGRADE + b"\x81\x01x")
        check_upgrade_response(read_response(peer), b"")

    with socket.create_connection(address, timeout=3) as peer:
        peer.sendall(UPGRADE + b"\x81")
        check_upgrade_response(read_response(peer), b"")

    with socket.create_connection(address, timeout=3) as peer:
        peer.sendall(UPGRADE.replace(b"13\r\n", b"12\r\n").replace(b"Connection: Upgrade", b"Connection: Upgrade, close"))
        response = read_response(peer)
        assert response.startswith(b"HTTP/1.1 400 "), response
        assert response.endswith(b'{"error":"invalid websocket handshake"}\n'), response


def check_server_health(host, port):
    with socket.create_connection((host, port), timeout=3) as peer:
        peer.sendall(b"GET /readyz HTTP/1.1\r\nHost: mesh\r\nConnection: close\r\n\r\n")
        response = read_response(peer)
        assert response.startswith(b"HTTP/1.1 200 "), response
        payload = response.split(b"\r\n\r\n", 1)[1]
        assert json.loads(payload)["checks"] == {"database": "ok", "token_issuer": "read_only",
                                                 "token_secret": "ok", "verifier": "ok"}


def check_token_routes(host, port, public_key):
    with closing(HTTPConnection(host, port, timeout=3)) as connection:
        connection.request("GET", "/api/v1/tokens/issuer")
        response = connection.getresponse()
        issuer = json.loads(response.read())
        assert response.status == 200 and issuer["public_key"] == public_key
        assert issuer["algorithm"] == "Ed25519" and issuer["status"] == "read_only"

        connection.request("GET", "/api/v1/tokens/products")
        response = connection.getresponse()
        assert response.status == 200 and json.loads(response.read()) == {"products": [
            {"product_id": "first", "token_units": 20}, {"product_id": "second", "token_units": 30}]}

        connection.request("GET", "/api/v1/tokens/balance")
        response = connection.getresponse()
        assert response.status == 401
        response.read()


def seed_holds(database):
    expired = "e" * 64
    retained = "f" * 64
    directory = Path(f"{database}.held")
    directory.mkdir()
    for account in (expired, retained):
        (directory / f"{account}.db").write_bytes(b"hold database fixture")
        blobs = directory / f"{account}.blobs"
        blobs.mkdir()
        (blobs / "fixture").write_bytes(b"hold blob fixture")
    with sqlite3.connect(database) as store:
        store.execute("CREATE TABLE server_account_holds(user_id_hash TEXT PRIMARY KEY, held_at INTEGER NOT NULL, purge_after INTEGER NOT NULL)")
        store.executemany("INSERT INTO server_account_holds VALUES(?,1,?)", ((expired, 2), (retained, int(time.time()) + 3600)))
        store.execute("CREATE TABLE server_account_tombstones(user_id_hash TEXT PRIMARY KEY, deleted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        store.execute("INSERT INTO server_account_tombstones(user_id_hash) VALUES(?)", (expired,))
    return expired, retained


def check_hold_retention(database, expired, retained):
    directory = Path(f"{database}.held")
    assert not (directory / f"{expired}.db").exists()
    assert not (directory / f"{expired}.blobs").exists()
    assert (directory / f"{retained}.db").is_file()
    assert (directory / f"{retained}.blobs" / "fixture").is_file()
    with sqlite3.connect(database) as store:
        assert store.execute("SELECT user_id_hash FROM server_account_holds").fetchall() == [(retained,)]
        assert store.execute("SELECT user_id_hash FROM server_account_tombstones").fetchall() == [(expired,)]


def seed_trashed_blobs(database):
    account = "d" * 64
    directory = Path(f"{database}.blobs") / account / "inbe"
    directory.mkdir(parents=True)
    with sqlite3.connect(database) as store:
        store.execute("CREATE TABLE server_users(user_id_hash TEXT PRIMARY KEY, public_key BLOB NOT NULL, "
                      "alias TEXT UNIQUE, profile_icon INTEGER NOT NULL DEFAULT 0, "
                      "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        store.execute("INSERT INTO server_users(user_id_hash,public_key) VALUES(?,?)", (account, b"retention fixture"))
        store.execute("CREATE TABLE server_blobs(user_id_hash TEXT NOT NULL REFERENCES server_users(user_id_hash) ON DELETE CASCADE, "
                      "app_id TEXT NOT NULL, hash TEXT NOT NULL, size INTEGER NOT NULL, "
                      "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, trashed_at INTEGER NOT NULL DEFAULT 0, "
                      "PRIMARY KEY(user_id_hash,app_id,hash))")
        data = []
        for label, trashed in (("expired", 1), ("recent", int(time.time())), ("live", 0)):
            contents = label.encode()
            name = hashlib.sha256(contents).hexdigest()
            (directory / name).write_bytes(contents)
            store.execute("INSERT INTO server_blobs(user_id_hash,app_id,hash,size,trashed_at) VALUES(?,?,?,?,?)",
                          (account, "inbe", name, len(contents), trashed))
            data.append(name)
    return directory, data


def check_blob_retention(database, directory, names, enabled):
    remaining = names[1:] if enabled else names
    assert sorted(entry.name for entry in directory.iterdir()) == sorted(remaining)
    with sqlite3.connect(database) as store:
        assert store.execute("SELECT hash FROM server_blobs ORDER BY hash").fetchall() == [(name,) for name in sorted(remaining)]


def check_app_registry(host, port, database):
    connection = HTTPConnection(host, port, timeout=3)

    def response(path, status=200, method="GET", body=None, headers=None):
        connection.request(method, path, body, headers or {})
        reply = connection.getresponse()
        body = reply.read()
        assert reply.status == status, (path, reply.status, body[:160])
        return json.loads(body) if body else None

    path = "/api/v1/apps"
    try:
        apps = response(path)["apps"]
        assert [app["app_id"] for app in apps] == ["entity", "inbe"]
        inbe = response(f"{path}/inbe")
        assert inbe == apps[1] and inbe["display_name"] == "Inner Breeze"
        assert "encrypted-records" in inbe["capabilities"] and len(inbe["token_policies"]) == 2
        assert response(f"{path}/inbe/collections")["collections"] == inbe["collections"]
        assert response(f"{path}/inbe", method="HEAD") is None
        assert response(f"{path}/missing", 404) == {"error": "app not found"}
        assert response(f"{path}/missing/collections", 404) == {"error": "app not found"}

        app = "manifesttest"
        metadata = {"app_schema_version": 2, "current_client_version": "2.1.0",
                    "features": [{"id": "release.feature", "requires_signed_tx": True}],
                    "legacy_protocols": [{"name": "released", "status": "compatibility", "valid_until": "2027-01-01"}]}
        with sqlite3.connect(database) as store:
            store.execute("INSERT INTO server_apps(app_id,display_name,app_schema_version) VALUES(?,?,1)", (app, "Manifest test"))
            store.execute("INSERT INTO server_app_manifests(app_id,manifest_version,manifest_json,manifest_hash,"
                          "manifest_signature,approval_signature,expires_at) VALUES(?,2,?,?,?,?,?)",
                          (app, json.dumps(metadata), "fixture-hash", "manifest-proof", "approval-proof", 2147483647))
            store.execute("INSERT INTO server_app_keys(app_id,key_id,algorithm,public_key) VALUES(?,?,?,?)",
                          (app, "fixture-key", "ed25519", "fixture-public-key"))
        detail = response(f"{path}/{app}")
        assert {name: detail[name] for name in metadata} == metadata
        assert detail["manifest_version"] == 2 and detail["approval_signature"] == "approval-proof"
        assert detail["keys"][0]["key_id"] == "fixture-key"
        assert response(f"{path}/{app}/collections") == {"collections": []}

        app_id = "registry-fixture"
        detail_path = f"{path}/{app_id}"
        authorization = {"X-Mesh-Admin": "mesh-test-registry-administration"}
        prefix = f"private.{app_id}.v1.*"
        feature = {"id": "sync", "collections": [prefix], "requires_signed_tx": True}
        legacy = {"name": "released", "version": 1, "status": "compatibility", "valid_until": "2028-02-29"}
        definition = {"App_ID": f" {app_id} ", "display_name": " Registry fixture ", "app_schema_version": 4,
                      "collections": [{"collection_prefix": prefix, "visibility": "private", "schema_version": 2}],
                      "capabilities": [" records ", "records"], "features": [feature], "legacy_protocols": [legacy],
                      "token_policies": [{"asset_id": "waozi:token", "permission": "spend"}]}
        payload = json.dumps(definition).encode()
        assert response(path, 401, "POST", payload) == {"error": "admin token required"}
        assert response(path, 401, "POST", payload, {"X-Mesh-Admin": "wrong", "X-Ksync-Admin": authorization["X-Mesh-Admin"]}) == {
            "error": "admin token required",
        }
        assert response(path, 400, "POST", b"{}", authorization) == {"error": "invalid app_id"}
        assert response(f"{path}/different", 400, "PUT", payload, authorization) == {"error": "app_id path mismatch"}
        compressed = {"X-Ksync-Admin": authorization["X-Mesh-Admin"], "Content-Encoding": "gzip"}
        registered = response(path, 200, "POST", gzip.compress(payload), compressed)
        assert registered == response(detail_path)
        assert registered["app_id"] == app_id and registered["display_name"] == "Registry fixture"
        assert registered["app_schema_version"] == 4 and registered["status"] == "active"
        assert registered["features"] == [feature] and registered["legacy_protocols"] == [legacy]
        assert registered["capabilities"] == ["records"]
        assert registered["collections"][0]["collection_prefix"] == prefix
        assert registered["token_policies"] == [{"asset_id": "waozi:token", "permission": "spend", "status": "active"}]
        duplicate = definition | {"display_name": "Must roll back", "collections": definition["collections"] * 2}
        assert response(detail_path, 500, "PUT", json.dumps(duplicate), authorization) == {"error": "app registration failed"}
        assert response(detail_path) == registered
        missing_asset = definition | {"token_policies": [{"asset_id": "missing:asset", "permission": "spend"}]}
        assert response(detail_path, 500, "PUT", json.dumps(missing_asset), authorization) == {"error": "app registration failed"}
        assert response(detail_path) == registered
        foreign = definition | {"collections": [{"collection_prefix": "private.inbe.v1.*", "visibility": "private"}]}
        assert response(path, 400, "POST", json.dumps(foreign), authorization) == {"error": "invalid app collection"}
        replacement = {"app_id": app_id, "display_name": "Suspended fixture", "status": "suspended"}
        changed = response(detail_path, 200, "PUT", json.dumps(replacement), authorization)
        assert changed["status"] == "suspended" and changed["display_name"] == "Suspended fixture"
        assert all(field not in changed for field in ("collections", "capabilities", "features", "legacy_protocols", "token_policies"))
    finally:
        connection.close()


def check_signed_app_registration(host, port, database, oracle, authority, lan_only):
    node_key_file = Path(f"{database}.node-key")
    node_key = bytes.fromhex(node_key_file.read_text().strip())
    assert len(node_key) == 64 and node_key_file.stat().st_mode & 0o777 == 0o600
    node_id = hashlib.sha256(node_key[32:]).hexdigest()
    app = "verified-fixture"
    client = sign_with_oracle(oracle, "device-keypair")
    public = base64.b64decode(client["PublicKey"]).hex()
    collection = f"private.{app}.v1.*"
    canonical = {"manifest_version": 1, "manifest_sequence": 3, "app_id": app,
                 "display_name": "Verified é😀 <&>\u2028release", "description": "Preview\x1f with \\u003c",
                 "status": "active", "expires_at": int(time.time()) + 3600}
    if lan_only:
        canonical.update(lan_only=True, lan_networks=["127.0.0.0/8"], registration_node_id=node_id)
    canonical.update(app_schema_version=6, current_client_version="1.1.0",
                     keys=[{"key_id": "release-key", "algorithm": "Ed25519", "public_key": public}],
                     collections=[{"app_id": app, "collection_prefix": collection, "visibility": "private",
                                   "schema_version": 2, "description": "Private files", "created_at": "fixture"}],
                     capabilities=["records"],
                     features=[{"id": "records", "collections": [collection], "requires_signed_tx": True}],
                     legacy_protocols=[{"name": "records", "version": 1, "status": "compatibility", "valid_until": "2028-02-29"}],
                     token_policies=[{"asset_id": "waozi:token", "permission": "spend", "status": "active"}])

    def signed(value):
        raw = json.dumps(value, ensure_ascii=False).encode()
        encoded = sign_with_oracle(oracle, "app-manifest-sign", PrivateKey=client["PrivateKey"], Message=base64.b64encode(raw).decode())
        manifest = base64.b64decode(encoded["Message"])
        digest = hashlib.sha256(manifest).hexdigest()
        message = f"daochi-app-approval-v1\n{app}\n{digest}\n".encode()
        approved = sign_with_oracle(oracle, "device-sign", PrivateKey=authority["PrivateKey"], Message=base64.b64encode(message).decode())
        return {"manifest": value, "manifest_signature": encoded["Signature"],
                "approval_signature": base64.b64decode(approved["Signature"]).hex()}, manifest, digest

    path = "/api/v1/apps/register-signed"
    with closing(HTTPConnection(host, port, timeout=5)) as connection:
        def request(value, status, gzip_body=False):
            body = json.dumps(value).encode()
            headers = {"Content-Encoding": "gzip"} if gzip_body else {}
            connection.request("POST", path, gzip.compress(body) if gzip_body else body, headers)
            reply = connection.getresponse()
            result = json.loads(reply.read())
            assert reply.status == status, (reply.status, status, result.get("error"))
            return result

        valid, normalized, digest = signed(canonical)
        raw = dict(reversed(list(canonical.items()))) | {"display_name": " " + canonical["display_name"] + " ",
                                                        "app_id": " " + app, "status": "",
                                                        "collections": [canonical["collections"][0] | {"app_id": "ignored"}],
                                                        "token_policies": [{"asset_id": "waozi:token", "permission": "spend"}]}
        malformed = request(valid | {"manifest_signature": "bad"}, 400)
        assert malformed == {"error": "invalid manifest signature"}, malformed
        assert request(valid | {"approval_signature": "00" * 64}, 401) == {"error": "node approval rejected"}
        assert request(valid | {"manifest": canonical | {"description": "tampered"}}, 401) == {"error": "manifest signature rejected"}
        expired = canonical | {"expires_at": int(time.time()) - 2}
        assert request(valid | {"manifest": expired}, 400) == {"error": "manifest expired"}
        invalid_network = canonical | {"lan_only": True, "lan_networks": ["127.0.0.1/32"], "registration_node_id": node_id}
        assert request(valid | {"manifest": invalid_network}, 403) == {"error": "app manifest is restricted to its local node and LAN"}
        if lan_only:
            wrong_node = canonical | {"registration_node_id": "0" * 64}
            assert request(valid | {"manifest": wrong_node}, 403) == {"error": "app manifest is restricted to its local node and LAN"}
        response = request(valid | {"manifest": raw}, 200, gzip_body=True)
        assert response["manifest_hash"] == digest and response["public_key"] == public
        assert response["manifest_version"] == 1 and response["approval_signature"] == valid["approval_signature"]
        assert response["keys"][0]["purpose"] == "signing" and response["keys"][0]["status"] == "active"
        assert response["features"] == canonical["features"]

        older, _, _ = signed(canonical | {"manifest_sequence": 2})
        assert request(older, 409) == {"error": "app manifest is older than the installed manifest"}
        duplicate, _, _ = signed(canonical | {"manifest_sequence": 4, "keys": canonical["keys"] * 2})
        assert request(duplicate, 500) == {"error": "app registration failed"}
        with sqlite3.connect(database) as store:
            row = store.execute("SELECT manifest_json,manifest_hash FROM server_app_manifests WHERE app_id=?", (app,)).fetchone()
            assert row == (normalized.decode(), digest)
            assert store.execute("SELECT count(*) FROM server_app_keys WHERE app_id=?", (app,)).fetchone() == (1,)


def read_trusted_peers(host, port, headers=None):
    with closing(HTTPConnection(host, port, timeout=5)) as connection:
        connection.request("GET", "/api/v1/node/peers", headers=headers or {})
        reply = connection.getresponse()
        return reply.status, json.loads(reply.read())


@contextmanager
def pairing_node(binary, database, *, arguments=(), **settings):
    env = {**os.environ, "MESH_ADDR": "127.0.0.1:0", "MESH_DB": str(database),
           "MESH_TOKEN_SECRET_HEX": "00" * 32, "MESH_ADMIN_TOKEN": "pairing-test",
           "MESH_NODE_IDENTITY_KEY_FILE": f"{database}.node-key", "MESH_NODE_IDENTITY_PRIVATE_KEY_HEX": "",
           "MESH_NODE_IDENTITY_PRIVATE_KEY_HEX_FILE": "", "MESH_NODE_REGISTRY_PUBLIC_KEY_HEX": "",
           "MESH_NODE_REGISTRY_PUBLIC_KEY_HEX_FILE": "", "MESH_TOKEN_ISSUER_PUBLIC_KEY_HEX": "",
           "MESH_TOKEN_ISSUER_PUBLIC_KEY_HEX_FILE": "", "MESH_LAN_ONLY": "1", "MESH_LAN_NETWORKS": "127.0.0.1/32",
           "MESH_BASE_URL": "http://127.0.0.1", "MESH_NODE_SYNC_INTERVAL_SECONDS": "0",
           "MESH_KNOWN_NODES": "", "MESH_NODE_SYNC_TOKEN": "", **settings}
    with subprocess.Popen([binary, *arguments], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as server:
        try:
            ready, _, _ = select.select([server.stdout], [], [], 5)
            assert ready, "pairing node did not report its bound port"
            started = server.stdout.readline()
            assert started, server.stderr.read(1000)
            yield int(started)
        finally:
            if server.poll() is None:
                server.terminate()
            try:
                _, errors = server.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.communicate()
                raise
            assert server.returncode == 0, errors


def check_legacy_environment(binary, directory):
    """Deployed units still set DAOCHI_* names; MESH_* names take precedence."""
    database = directory / "legacy-names.db"
    env = {key: value for key, value in os.environ.items() if not key.startswith(("MESH_", "DAOCHI_"))}
    env.update({"DAOCHI_ADDR": "127.0.0.1:0", "DAOCHI_DB": str(database), "DAOCHI_TOKEN_SECRET_HEX": "00" * 32,
                "DAOCHI_NODE_IDENTITY_KEY_FILE": f"{database}.node-key", "DAOCHI_NODE_NAME": "Legacy node",
                "DAOCHI_BASE_URL": "http://legacy.invalid", "MESH_BASE_URL": "http://127.0.0.1",
                "DAOCHI_LAN_ONLY": "1", "DAOCHI_LAN_NETWORKS": "127.0.0.1/32",
                "DAOCHI_NODE_SYNC_INTERVAL_SECONDS": "0"})
    with subprocess.Popen([binary], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as server:
        try:
            ready, _, _ = select.select([server.stdout], [], [], 5)
            assert ready, "legacy-named node did not report its bound port"
            started = server.stdout.readline()
            assert started, server.stderr.read(1000)
            status = node_status("127.0.0.1", int(started))
            assert status["node_name"] == "Legacy node" and status["base_url"] == "http://127.0.0.1", status
            assert database.is_file() and Path(f"{database}.node-key").is_file()
        finally:
            server.terminate()
            _, errors = server.communicate(timeout=5)
    assert server.returncode == 0, errors


def check_node_pairing(binary, directory, oracle):
    left_db, right_db = directory / "pairing-left.db", directory / "pairing-right.db"
    host = "127.0.0.1"
    admin = {"X-Mesh-Admin": "pairing-test"}
    prefix = "/api/v1/node/pairing/"

    def call(port, route, value, status, headers=admin, compressed=False):
        body = json.dumps(value).encode()
        if compressed:
            body = gzip.compress(body)
            headers = headers | {"Content-Encoding": "gzip"}
        with closing(HTTPConnection(host, port, timeout=5)) as connection:
            connection.request("POST", prefix + route, body, headers)
            response = connection.getresponse()
            parsed = json.loads(response.read())
            assert response.status == status, (route, response.status, parsed)
            return parsed

    def signature(value, private, invite_signature=None):
        addresses = ",".join(sorted(value["addresses"]))
        lines = ["mesh-pairing-invite-v1", "1", value["invite_id"], value["node_id"], value["public_key"],
                 value["display_name"], addresses]
        if invite_signature is None:
            policy_names = ("direction", "apps", "accounts", "collections", "spaces", "data")
            policy = {key: value["policy"][key] for key in policy_names if value["policy"].get(key)}
            encoded = json.dumps(policy, ensure_ascii=False, separators=(",", ":"))
            for character in "<>&\u2028\u2029":
                encoded = encoded.replace(character, f"\\u{ord(character):04x}")
            lines += [value.get("space_id", ""), str(value["expires_at"]), value["nonce"],
                      hashlib.sha256(encoded.encode()).hexdigest()]
        else:
            lines[0] += "-acceptance"
            lines += [str(value["accepted_at"]), value["nonce"], hashlib.sha256(invite_signature.encode()).hexdigest()]
        message = base64.b64encode(("\n".join(lines) + "\n").encode()).decode()
        raw = sign_with_oracle(oracle, "device-sign", PrivateKey=private, Message=message)
        return base64.urlsafe_b64encode(base64.b64decode(raw["Signature"])).decode().rstrip("=")

    def acceptance(invite, **changes):
        key = sign_with_oracle(oracle, "device-keypair")
        public = base64.b64decode(key["PublicKey"])
        value = {"version": 1, "invite_id": invite["invite_id"], "node_id": hashlib.sha256(public).hexdigest(),
                 "public_key": public.hex(), "display_name": "Independent Go signer", "addresses": ["http://127.0.0.1"],
                 "accepted_at": int(time.time()), "nonce": "test-acceptance", **changes}
        return value | {"signature": signature(value, key["PrivateKey"], invite["signature"])}

    def peer_count(database):
        with closing(sqlite3.connect(database)) as store:
            return store.execute("SELECT COUNT(*) FROM trusted_node_peers").fetchone()[0]

    with pairing_node(binary, left_db, MESH_NODE_NAME="Inviter") as left, \
            pairing_node(binary, right_db, MESH_NODE_NAME="Acceptor") as right, \
            socket.socket(socket.AF_INET) as forbidden:
        forbidden.bind((host, 0))
        forbidden.listen()
        # Linux can route an unspecified destination to the local listener,
        # but it is neither a loopback address nor in the approved subnet.
        outsider = f"http://0.0.0.0:{forbidden.getsockname()[1]}"
        local = f"http://{host}:{left}"
        policy = {"direction": "Receive", "apps": ["pair<&>é😀\u2028"], "data": ["encrypted_records"]}
        request = {"addresses": [outsider, local], "space_id": " test space ", "policy": policy}
        assert call(left, "invites", request, 401, {}) == {"error": "admin token required"}
        assert call(left, "invites", request | {"policy": {}}, 400) == {"error": "explicit pairing policy required"}
        assert call(left, "invites", request | {"expires_in_seconds": 86401}, 400) == {
            "error": "pairing invite expiry exceeds 24 hours"}
        assert call(left, "invites", request | {"addresses": ["file:///etc/passwd"]}, 400) == {
            "error": "invalid pairing address"}
        invite = call(left, "invites", request, 200, compressed=True)
        assert invite["addresses"] == [outsider, local] and invite["space_id"] == "test space", invite
        assert invite["policy"] == policy | {"direction": "pull"}
        file_key = bytes.fromhex(Path(f"{left_db}.node-key").read_text())
        assert invite["signature"] == signature(invite, base64.b64encode(file_key).decode()), invite
        assert call(right, "accept", invite, 401, {}) == {"error": "admin token required"}
        assert call(left, "accept", invite, 400) == {"error": "cannot pair a node with itself"}
        assert call(right, "accept", invite | {"space_id": "tampered"}, 400) == {"error": "invalid pairing signature"}
        assert call(right, "complete", {"invite": invite, "acceptance": {}}, 400, {}) == {
            "error": "pairing invite belongs to another node"}
        assert call(right, "accept", invite, 200) == {"status": "paired", "node_id": invite["node_id"]}
        assert not select.select([forbidden], [], [], 0)[0], "LAN pairing opened an out-of-scope connection"
        left_status, left_peers = read_trusted_peers(host, left, admin)
        right_status, right_peers = read_trusted_peers(host, right, admin)
        first = left_peers["peers"][0]
        assert (left_status, right_status) == (200, 200)
        assert first["display_name"] == "Acceptor" and first["policy"] == policy | {"direction": "push"}
        assert right_peers["peers"][0]["node_id"] == invite["node_id"]
        assert right_peers["peers"][0]["policy"] == invite["policy"]
        assert call(right, "accept", invite, 409) == {"error": "pairing invite already consumed"}
        replay = {"invite": invite, "acceptance": acceptance(invite)}
        assert call(left, "complete", replay, 409, {}) == {"error": "pairing invite already completed by another node"}
        assert peer_count(left_db) == peer_count(right_db) == 1

        issued = call(left, "invites", request, 200)
        valid = acceptance(issued)
        assert call(left, "complete", {"invite": issued, "acceptance": valid | {"signature": "bad"}}, 400, {}) == {
            "error": "invalid pairing acceptance signature"}
        assert call(left, "complete", {"invite": issued, "acceptance": acceptance(issued, accepted_at=1)}, 400, {}) == {
            "error": "pairing acceptance time is outside the allowed window"}
        with closing(sqlite3.connect(left_db)) as store:
            store.execute("CREATE TRIGGER reject_peer BEFORE INSERT ON trusted_node_peers BEGIN "
                          "SELECT RAISE(ABORT, 'fixture failure'); END")
            store.commit()
        proof = {"invite": issued, "acceptance": valid}
        assert call(left, "complete", proof, 409, {}) == {"error": "could not record pairing"}
        with closing(sqlite3.connect(left_db)) as store:
            assert store.execute("SELECT completed_node_id FROM issued_pairing_invites WHERE invite_id=?",
                                 (issued["invite_id"],)).fetchone() == ("",)
            store.execute("DROP TRIGGER reject_peer")
            store.commit()
        assert call(left, "complete", proof, 200, {}) == {"status": "paired", "node_id": valid["node_id"]}
        assert call(left, "complete", proof, 200, {}) == {"status": "paired", "node_id": valid["node_id"]}
        assert peer_count(left_db) == 2


def check_trusted_peers(host, port, database, admin_token):
    assert read_trusted_peers(host, port) == (401, {"error": "admin token required"})
    header = {"X-Ksync-Admin": admin_token}
    assert read_trusted_peers(host, port, header) == (200, {"peers": []})
    nodes = []
    for byte, label, revoked in ((1, "Zulu", ""), (2, "Alpha", ""), (3, "Hidden", "2026-01-02")):
        key = bytes([byte]) * 32
        node_id = hashlib.sha256(key).hexdigest()
        addresses = [f"http://127.0.0.1:{8080 + byte}"]
        policy = {"direction": "pull", "apps": ["inbe"], "data": ["encrypted_records"]}
        stamp = "2026-01-01 00:00:00"
        with sqlite3.connect(database) as store:
            store.execute("INSERT INTO trusted_node_peers(node_id,public_key,display_name,addresses_json,space_id,"
                          "policy_json,trusted_at,revoked_at) VALUES(?,?,?,?,?,?,?,?)",
                          (node_id, key, label, json.dumps(addresses), "", json.dumps(policy), stamp, revoked))
        if not revoked:
            nodes.append({"node_id": node_id, "public_key": key.hex(), "display_name": label,
                          "addresses": addresses, "policy": policy, "trusted_at": stamp})
    assert read_trusted_peers(host, port, header) == (200, {"peers": list(reversed(nodes))})
    assert read_trusted_peers(host, port, header | {"X-Mesh-Admin": "wrong"}) == (401, {"error": "admin token required"})
    with sqlite3.connect(database) as store:
        store.execute("UPDATE trusted_node_peers SET policy_json='[]' WHERE node_id=?", (nodes[0]["node_id"],))
    assert read_trusted_peers(host, port, header) == (500, {"error": "peer list failed"})
    with sqlite3.connect(database) as store:
        store.execute("UPDATE trusted_node_peers SET policy_json=? WHERE node_id=?",
                      (json.dumps(nodes[0]["policy"]), nodes[0]["node_id"]))


def check_namespace(host, port, database, oracle, admin_token):
    prefix = "/api/v1/namespaces"
    admin = {"X-Mesh-Admin": admin_token}

    def call(method, route, status, value=None, headers=None):
        with closing(HTTPConnection(host, port, timeout=5)) as connection:
            body = None if value is None else json.dumps(value).encode()
            connection.request(method, prefix + route, body, headers or {})
            response = connection.getresponse()
            raw = response.read()
            assert response.status == status, (method, route, response.status, raw)
            if method == "HEAD":
                assert not raw
                return None
            return json.loads(raw)

    def sign(key, value, services):
        digest = hashlib.sha256(services).hexdigest()
        parts = ["mesh-name-claim-v1", "1", value["space_id"], value["name"], value["node_id"],
                 str(value["sequence"]), str(value["expires_at"]), digest, ""]
        signed = sign_with_oracle(oracle, "device-sign", PrivateKey=base64.b64encode(key).decode(),
                                  Message=base64.b64encode("\n".join(parts).encode()).decode())
        return base64.urlsafe_b64encode(base64.b64decode(signed["Signature"])).decode().rstrip("=")

    assert call("POST", "", 401, {"display_name": "Local Mesh"}) == {"error": "admin token required"}
    assert call("POST", "", 400, {"display_name": " "}, admin) == {"error": "invalid trust space"}
    space = call("POST", "", 201, {"display_name": " Local Mesh "}, admin)
    space_id = space["space_id"]
    assert space["display_name"] == "Local Mesh"
    query = "/resolve?" + urlencode({"space_id": f" {space_id} ", "name": " HOME-ONE "})
    assert call("GET", query, 404) == {"error": "name not found"}
    assert call("GET", "/resolve?space_id=bad&name=home", 400) == {"error": "invalid space or name"}
    service = {"service": "Web", "endpoints": ["https://mesh.example.test/path"],
               "capabilities": ["<telemetry>&é\u2028"]}
    request = {"space_id": space_id, "name": " Home-One ", "node_id": space_id, "services": [service],
               "sequence": 123, "signature": "replaced-by-node", "expires_at": int(time.time()) + 600}
    assert call("POST", "/claims", 400, request | {"expires_at": 1}, admin) == {"error": "invalid name expiry"}
    assert call("POST", "/claims", 400, request | {"services": [service | {"endpoints": []}]}, admin) == {
        "error": "invalid service endpoint"}
    assert call("POST", "/claims", 400, request | {"services": [service | {"service": "-invalid"}]}, admin) == {
        "error": "invalid service name"}
    claim = call("POST", "/claims", 200, request, admin)
    assert claim["sequence"] == 1 and claim["version"] == 1 and claim["services"] == [service]
    assert claim["name"] == "home-one" and claim["node_id"] == space_id
    encoded = (
        '[{"service":"Web","endpoints":["https://mesh.example.test/path"],'
        '"capabilities":["\\u003ctelemetry\\u003e\\u0026é\\u2028"]}]'
    ).encode()
    with closing(sqlite3.connect(database)) as store:
        public, key = store.execute("SELECT authority_public_key,authority_private_key FROM trust_spaces WHERE space_id=?",
                                    (space_id,)).fetchone()
        assert len(key) == 64 and hashlib.sha256(public).hexdigest() == space_id
        assert store.execute("SELECT services_json FROM name_claims WHERE space_id=?", (space_id,)).fetchone() == (encoded.decode(),)
        assert claim["signature"] == sign(key, claim, encoded)
        store.execute("UPDATE name_claims SET signature='tampered' WHERE space_id=?", (space_id,))
        store.commit()
        assert call("GET", query, 500) == {"error": "name resolution failed"}
        store.execute("UPDATE name_claims SET expires_at=1 WHERE space_id=?", (space_id,))
        store.commit()
        assert call("GET", query, 404) == {"error": "name not found"}
        store.execute("CREATE TRIGGER reject_name BEFORE UPDATE ON name_claims BEGIN SELECT RAISE(ABORT, 'fixture'); END")
        store.commit()
        assert call("POST", "/claims", 400, request, admin) == {"error": "name claim could not be stored"}
        assert store.execute("SELECT sequence FROM name_claims WHERE space_id=?", (space_id,)).fetchone() == (1,)
        store.execute("DROP TRIGGER reject_name")
        store.commit()
    with ThreadPoolExecutor(max_workers=3) as workers:
        claims = list(workers.map(lambda _: call("POST", "/claims", 200, request | {"services": []}, admin), range(3)))
    assert sorted(entry["sequence"] for entry in claims) == [2, 3, 4]
    last = max(claims, key=lambda entry: entry["sequence"])
    assert "services" not in last and last["signature"] == sign(key, last, b"[]")
    resolved = call("GET", query, 200)
    assert resolved["claim"] == last and 0 < resolved["ttl_seconds"] <= 600
    assert resolved["uri"] == f"mesh://{space_id}/home-one"
    call("HEAD", query, 200)


def check_peer_exchange(host, port, database, oracle):
    path = "/api/v1/node/mesh/export"
    import_path = "/api/v1/node/mesh/import"
    key = sign_with_oracle(oracle, "device-keypair")
    public = base64.b64decode(key["PublicKey"])
    node = hashlib.sha256(public).hexdigest()
    account, other_account = node, "38" * 32
    app = "verified-fixture"
    prefix = f"private.{app}.v1."
    collection = prefix + "items"
    now = int(time.time())
    with closing(sqlite3.connect(database)) as store, store:
        space = store.execute("SELECT space_id FROM trust_spaces WHERE display_name='Local Mesh'").fetchone()[0]
        policy = {"accounts": [account], "apps": [app], "collections": [prefix + "*"], "spaces": [space],
                  "data": ["encrypted_records", "app_registry", "names"]}
        store.execute("INSERT INTO trusted_node_peers(node_id,public_key,policy_json,trusted_at) VALUES (?,?,?,?)",
                      (node, public, json.dumps(policy | {"direction": "push"}), str(now)))
        store.executemany("INSERT INTO server_users(user_id_hash,public_key) VALUES (?,?)",
                          [(account, public), (other_account, public)])
        store.executemany(
            "INSERT INTO server_encrypted_records(user_id_hash,collection,id,ciphertext,updated_at) VALUES (?,?,?,?,?)",
            [(account, collection, "keep", "sealed", "2026-01-01T00:00:00.000000000Z"),
             (other_account, collection, "unshared-account", "other", "2026-01-01T00:00:00.000000000Z"),
             (account, "private.other.v1.items", "unshared-collection", "other", "2026-01-01T00:00:00.000000000Z"),
             (account, collection, "removed", "gone", "2026-01-01T00:00:00.000000000Z")])
        store.execute("DELETE FROM server_encrypted_records WHERE user_id_hash=? AND collection=? AND id='removed'",
                      (account, collection))

    def sign(body, nonce, timestamp=now, legacy=False, endpoint=path):
        message = "\n".join(("daochi-node-request-v1", node, str(timestamp), nonce, "POST", endpoint,
                             hashlib.sha256(body).hexdigest(), "")).encode()
        signed = sign_with_oracle(oracle, "device-sign", PrivateKey=key["PrivateKey"],
                                  Message=base64.b64encode(message).decode())
        signature = base64.urlsafe_b64encode(base64.b64decode(signed["Signature"])).decode().rstrip("=")
        prefix = "X-Daochi" if legacy else "X-Mesh"
        return {f"{prefix}-Node-{field}": value for field, value in
                {"ID": node, "Time": str(timestamp), "Nonce": nonce, "Signature": signature}.items()}

    def post(body, status, headers=None, endpoint=path):
        with closing(HTTPConnection(host, port, timeout=5)) as connection:
            connection.request("POST", endpoint, body, headers or {})
            response = connection.getresponse()
            data = response.read()
            assert response.status == status, (response.status, data)
            return json.loads(data)

    body = json.dumps({"limit": 1, "policy": policy}).encode()
    post(body, 401)
    post(body + b" ", 401, sign(body, "modified"))
    post(body, 401, sign(body, "expired", now - 600))
    missing = b"{}"
    post(missing, 400, sign(missing, "missing-policy"))
    broad = json.dumps({"policy": {k: v for k, v in policy.items() if k != "accounts"}}).encode()
    post(broad, 403, sign(broad, "broadened-policy"))
    headers = sign(body, "export-page-1", legacy=True) | {"Content-Encoding": "gzip"}
    first = post(gzip.compress(body), 200, headers)
    assert first["status"] == "ok" and first["truncated"]
    assert [(r["user_id_hash"], r["Record"]["id"]) for r in first["records"]] == [(account, "keep")]
    assert first["records"][0]["Record"]["ciphertext"] == "sealed"
    assert first["apps"][0]["manifest"]["app_id"] == app
    assert [entry["space_id"] for entry in first["spaces"]] == [space]
    assert [(entry["name"], entry["services"]) for entry in first["names"]] == [("home-one", [])]
    cursor = first["next_cursor"]
    decoded = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    assert decoded.pop("seq") > 0 and decoded == dict.fromkeys(("updated_at", "user_id_hash", "collection", "id"), "")
    replay = post(gzip.compress(body), 401, headers)
    assert replay["error"] == "node request replayed"
    body = json.dumps({"policy": policy, "cursor": cursor}).encode()
    second = post(body, 200, sign(body, "export-page-2"))
    assert second["records"] == [] and "truncated" not in second
    assert [(r["user_id_hash"], r["collection"], r["id"]) for r in second["deletions"]] == [
        (account, collection, "removed")]

    def import_batch(value, nonce, status=200):
        body = json.dumps(value).encode()
        return post(body, status, sign(body, nonce, endpoint=import_path), import_path)

    assert import_batch({"policy": policy}, "push-only", 403)["error"] == "requested mesh policy exceeds paired scope"
    authority = sign_with_oracle(oracle, "device-keypair")
    realm_key = base64.b64decode(authority["PublicKey"])
    realm = hashlib.sha256(realm_key).hexdigest()
    incoming = policy | {"spaces": [space, realm]}
    with closing(sqlite3.connect(database)) as store, store:
        store.execute("UPDATE trusted_node_peers SET policy_json=? WHERE node_id=?",
                      (json.dumps(incoming | {"direction": "bidirectional"}), node))

    def name_claim(sequence, label="remote", target=node):
        value = {"version": 1, "space_id": realm, "name": label, "node_id": target,
                 "sequence": sequence, "expires_at": now + 600, "services": []}
        digest = hashlib.sha256(b"[]").hexdigest()
        message = "\n".join(("daochi-name-claim-v1", "1", realm, label, target, str(sequence),
                             str(value["expires_at"]), digest, "")).encode()
        signed = sign_with_oracle(oracle, "device-sign", PrivateKey=authority["PrivateKey"],
                                  Message=base64.b64encode(message).decode())
        return value | {"signature": base64.urlsafe_b64encode(base64.b64decode(signed["Signature"])).decode().rstrip("=")}

    record = first["records"][0] | {"mesh_version": 3}
    record["Record"] = record["Record"] | {"ciphertext": "second"}
    deletion = {"user_id_hash": account, "collection": collection, "id": "keep", "mesh_version": 2,
                "deleted_at": record["Record"]["updated_at"]}
    claim = name_claim(5)
    remote = {"space_id": realm, "display_name": "Remote Mesh", "authority_public_key": realm_key.hex()}
    batch = {"policy": incoming, "apps": first["apps"], "records": [record], "deletions": [deletion],
             "spaces": [remote], "names": [claim]}
    assert import_batch(batch, "accept-batch") == {"status": "ok", "records": 4, "applied": 3}
    unchanged = {"policy": incoming, "apps": first["apps"], "records": [record], "names": [claim, name_claim(1)]}
    assert import_batch(unchanged, "unchanged-batch") == {"status": "ok", "records": 4, "applied": 0}
    next_record = record | {"Record": record["Record"] | {"ciphertext": "third"}}
    fork = {"policy": incoming, "records": [next_record], "names": [name_claim(5, target=space)]}
    assert import_batch(fork, "forked-name", 400)["error"] == "namespace history fork detected"
    bad_proof = claim | {"signature": "tampered"}
    assert import_batch({"policy": incoming, "names": [bad_proof]}, "invalid-name", 400)["error"] == (
        "invalid mesh name claim signature")
    large_record = record | {"Record": record["Record"] | {"id": "oversized", "ciphertext": "x" * 1200}}
    rejected = import_batch({"policy": incoming, "records": [large_record]}, "quota", 413)
    assert rejected["account_storage"]["limit_bytes"] == 1000
    resolve = "/api/v1/namespaces/resolve?" + urlencode({"space_id": realm, "name": "remote"})
    with closing(HTTPConnection(host, port, timeout=5)) as connection:
        connection.request("GET", resolve)
        response = connection.getresponse()
        assert response.status == 200 and json.loads(response.read())["claim"]["signature"] == claim["signature"]

    with closing(sqlite3.connect(database)) as store, store:
        assert store.execute(
            "SELECT id,ciphertext FROM server_encrypted_records WHERE user_id_hash=? AND collection=?",
            (account, collection)).fetchall() == [("keep", "second")]
        assert store.execute("SELECT length(authority_private_key) FROM trust_spaces WHERE space_id=?", (realm,)).fetchone() == (0,)
        store.execute("DELETE FROM name_claims WHERE space_id=?", (realm,))
        store.execute("DELETE FROM trust_spaces WHERE space_id=?", (realm,))
        store.execute("DELETE FROM trusted_node_peers WHERE node_id=?", (node,))
        store.execute("DELETE FROM server_encrypted_records WHERE user_id_hash IN (?,?)", (account, other_account))
        store.execute("DELETE FROM server_users WHERE user_id_hash IN (?,?)", (account, other_account))
        store.execute("DELETE FROM server_sync_state WHERE user_id_hash IN (?,?)", (account, other_account))
        store.execute("DELETE FROM server_mesh_changes WHERE user_id_hash IN (?,?)", (account, other_account))


def check_portable_bundle(host, port, database, oracle):
    app, key = "manifesttest", "package-key"
    device = sign_with_oracle(oracle, "device-keypair")
    public = base64.b64decode(device["PublicKey"]).hex()
    path = f"/api/v1/packages/{app}/"
    directory = Path(f"{database}.packages") / app
    directory.mkdir(parents=True)
    latest = directory / "latest.json"
    with sqlite3.connect(database) as store:
        store.execute("INSERT INTO server_app_keys(app_id,key_id,algorithm,public_key) VALUES(?,?,?,?)",
                      (app, key, "ed25519", public))

    def release(sequence, bundle):
        digest = hashlib.sha256(bundle).hexdigest()
        version, runtime = f"1.0.{sequence}", "kryon-desktop-v1"
        message = f"daochi-zib-release-v1\n{app}\n{sequence}\n{version}\n{runtime}\n{digest}\n{len(bundle)}\n{key}\n"
        signature = sign_with_oracle(oracle, "device-sign", PrivateKey=device["PrivateKey"],
                                     Message=base64.b64encode(message.encode()).decode())
        return {"app_id": app, "sequence": sequence, "version": version, "runtime": runtime,
                "sha256": digest, "size": len(bundle), "key_id": key,
                "signature": base64.b64decode(signature["Signature"]).hex()}

    def request(asset, status=200, method="GET"):
        with ExitStack() as stack:
            connection = HTTPConnection(host, port, timeout=3)
            stack.callback(connection.close)
            connection.request(method, path + asset)
            reply = connection.getresponse()
            body = reply.read()
            assert reply.status == status, (asset, reply.status, body[:256])
            return reply, body

    request("latest", 404)
    bundle = bytes(range(256)) * 512 + b"last block"
    current = release(2, bundle)
    previous_bundle = b"older portable package"
    previous = release(1, previous_bundle)
    asset = current["sha256"] + ".zib"
    archived_asset = previous["sha256"] + ".zib"
    latest.write_text(json.dumps(current | {"ignored": True}))
    (directory / (archived_asset + ".json")).write_text(json.dumps(previous))
    (directory / asset).write_bytes(bundle)
    (directory / archived_asset).write_bytes(previous_bundle)
    reply, body = request("latest")
    assert reply.getheader("Cache-Control") == "no-store" and json.loads(body) == current
    reply, body = request(asset)
    assert reply.getheader("Cache-Control") == "public, max-age=31536000, immutable"
    assert reply.getheader("ETag") == f'"{current["sha256"]}"' and body == bundle
    reply, body = request(asset, method="HEAD")
    assert body == b"" and int(reply.getheader("Content-Length")) == len(bundle)
    assert request(archived_asset)[1] == previous_bundle
    gate = directory / (asset + ".product")
    gate.write_text("sample-product")
    assert json.loads(request(asset, 403)[1]) == {"error": "package grant required"}
    gate.unlink()
    gate.symlink_to("product-does-not-exist")
    request(asset, 403)
    gate.unlink()
    (directory / asset).write_bytes(bundle[:-1] + b"!")
    request(asset, 503)
    (directory / asset).unlink()
    (directory / asset).symlink_to(archived_asset)
    request(asset, 503)
    latest.write_text(json.dumps(current | {"sequence": 3}))
    assert json.loads(request("latest", 503)[1]) == {"error": "untrusted portable release"}
    latest.write_text(json.dumps(current))
    with sqlite3.connect(database) as store:
        store.execute("UPDATE server_app_keys SET expires_at=1 WHERE app_id=? AND key_id=?", (app, key))
    request("latest", 503)


def check_portable_release(host, port, database, oracle, issuer):
    app, key = "manifesttest", "release-v2"
    device = sign_with_oracle(oracle, "device-keypair")
    public = base64.b64decode(device["PublicKey"]).hex()
    directory = Path(f"{database}.packages") / app
    latest, history = directory / "latest-v2.json", directory / "history-v2.json"
    with sqlite3.connect(database) as store:
        store.execute("INSERT INTO server_app_keys(app_id,key_id,algorithm,public_key) VALUES(?,?,?,?)",
                      (app, key, "ed25519", public))

    def sign(message, signer):
        result = sign_with_oracle(oracle, "device-sign", PrivateKey=signer["PrivateKey"],
                                  Message=base64.b64encode(message).decode())
        return base64.b64decode(result["Signature"])

    def release(sequence, host_api, assets, channel=None, dependencies=None):
        version, runtime = f"2.0.{sequence}", "kryon-app-v1"
        artifacts = [{"variant": variant, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
                     for variant, _, data in assets]
        dependencies = dependencies or []
        fields = ["daochi-zib-release-v2", app, sequence, version, runtime, host_api, 1, 0, key, len(artifacts)]
        for artifact in artifacts:
            fields.extend(artifact.values())
        fields.append(len(dependencies))
        for dependency in dependencies:
            fields.extend(dependency.values())
        message = "\n".join(map(str, fields)) + "\n"
        value = {"app_id": app, "sequence": sequence, "version": version, "runtime": runtime,
                 "host_api": host_api, "module_api": 1, "data_schema": 0, "key_id": key,
                 "artifacts": artifacts, "dependencies": dependencies,
                 "signature": sign(message.encode(), device).hex()}
        if channel is not None:
            value["channel"] = channel
        for artifact, (_, format, data) in zip(artifacts, assets):
            name = artifact["sha256"] + format
            (directory / name).write_bytes(data)
            (directory / (name + ".release-v2.json")).write_text(json.dumps(value))
        return value

    def request(asset, status=200, method="GET"):
        connection = HTTPConnection(host, port, timeout=3)
        try:
            connection.request(method, f"/api/v2/packages/{app}/{asset}")
            reply = connection.getresponse()
            body = reply.read()
            assert reply.status == status, (asset.split("/")[0], reply.status, body[:256])
            return reply, body
        finally:
            connection.close()

    def metadata(asset, status=200):
        return json.loads(request(asset, status)[1])

    def zx(revision, payload):
        return b"ZX\0\0" + revision.to_bytes(4, "little") + payload

    request("latest", 404)
    legacy = release(1, 1, [("module", ".zib", b"ZIB\0legacy module"),
                            ("standalone", ".zib", b"ZIB\0legacy standalone")],
                     dependencies=[{"app_id": "entity", "version": "1.0.0", "sha256": "a" * 64, "size": 64}])
    previous = release(3, 2, [("module", ".zx", zx(27, b"stable executable"))])
    module = zx(28, b"new executable")
    current = release(1000, 3, [("module", ".zx", module)], channel="beta")
    wasm_bytes = b"\x00asm\x01\x00\x00\x00"
    wasm = release(1000, 3, [("module", ".wasm", wasm_bytes)], channel="beta")
    (directory / "release-1000.wasm.json").write_text(json.dumps(wasm))
    latest.write_text(json.dumps(current | {"ignored": "node metadata"}))
    history.write_text(json.dumps({"versions": [current, previous, legacy]}))
    assert metadata("latest?format=zx&host_api=3&zx=28") == current
    expected_history = metadata("history?format=zx")
    assert expected_history == {"versions": [current, previous]}, [item["sequence"] for item in expected_history["versions"]]
    assert metadata("latest?format=zx&host_api=3") == previous
    assert metadata("latest?format=zx&host_api=2") == previous
    request("latest?format=zx&host_api=2&oldest_host_api=3", 400)
    request("latest?format=zx&module_api=2", 404)
    request("latest?zx=28", 400)
    assert metadata("latest") == legacy
    assert metadata("history") == {"versions": [legacy]}
    rollout = directory / "rollout.json"
    rollout.write_text(json.dumps({"sequence": 1000, "percent": 25, "until": int(time.time()) + 60}))
    assert metadata("latest?format=zx&bucket=24") == current
    assert metadata("latest?format=zx&bucket=25") == previous
    assert metadata("history?format=zx&bucket=50") == {"versions": [previous]}
    rollout.unlink()
    assert metadata("latest?format=wasm&host_api=3&zx=28") == wasm
    web = wasm["artifacts"][0]["sha256"] + ".wasm"
    reply, body = request(web)
    assert reply.getheader("Content-Type") == "application/wasm" and body == wasm_bytes
    reply, body = request(web, method="HEAD")
    assert int(reply.getheader("Content-Length")) == len(wasm_bytes) and body == b""
    history.write_text(json.dumps({"versions": [current, previous | {"version": "2.0.99"}, legacy]}))
    assert metadata("history?format=zx") == {"versions": [current]}
    history.write_text(json.dumps({"versions": [current, current]}))
    request("history", 503)
    history.unlink()
    assert metadata("history?format=zx") == {"versions": [current]}
    archived = release(999, 2, [("module", ".zx", zx(27, b"archived executable"))])
    (directory / "release-999.json").write_text(json.dumps(archived))
    assert metadata("latest?format=zx&host_api=2") == archived

    name = current["artifacts"][0]["sha256"] + ".zx"
    product, now = "example-pack", int(time.time())
    (directory / (name + ".product")).write_text(product + "\n")

    def grant(named, expires, signer=issuer):
        payload = f"daochi-pack-grant-v1\n{named}\n{expires}".encode()
        return ".".join(base64.urlsafe_b64encode(field).rstrip(b"=").decode()
                        for field in (payload, sign(payload, signer)))

    request(name, 403)
    request(name + "/" + grant(product, now - 1), 403)
    request(name + "/" + grant("wrong-product", now + 60), 403)
    request(name + "/" + grant(product, now + 60, device), 403)
    valid_grant = grant(product, now + 60)
    request(name + "/" + valid_grant + "=", 403)
    reply, body = request(name + "/" + valid_grant)
    assert body == module and reply.getheader("Cache-Control") == "private, no-store"
    assert reply.getheader("ETag") == f'"{name[:64]}"'
    (directory / name).write_bytes(module + b"extra")
    request(name + "/" + valid_grant, 503)
    latest.write_text(json.dumps(current | {"host_api": 2}))
    request("latest?format=zx", 503)


def check_device_link(host, port):
    def request(action, payload, headers=None):
        connection = HTTPConnection(host, port, timeout=3)
        try:
            connection.request("POST", f"/api/v1/link/{action}", payload, headers or {})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    link = {"id": "0123456789abcdef" * 2}
    request_slot = json.dumps(link | {"slot": "request"}).encode()
    response_slot = json.dumps(link | {"slot": "response"}).encode()
    assert request("get", request_slot) == (404, {"error": "device link waiting"})
    assert request("put", b'{"id":"123","slot":"request","data":"bad"}') == (400, {"error": "invalid device link"})
    duplicate = ('{"id":"%s","\\u0069d":"%s","slot":"request","data":"duplicate"}' % (link["id"], link["id"])).encode()
    assert request("put", duplicate) == (400, {"error": "invalid device link"})
    assert request("get", json.dumps(link | {"slot": "other"})) == (400, {"error": "invalid device link slot"})
    assert request("put", json.dumps(link | {"slot": "request", "data": "x" * 16385})) == (400, {"error": "invalid device link message"})
    assert request("put", b" " * 17409) == (400, {"error": "invalid device link"})
    sealed_request = "sealed request " * 800
    packet = json.dumps(link | {"slot": "request", "data": sealed_request}).encode()
    with ThreadPoolExecutor(max_workers=2) as requests:
        first = requests.submit(request, "put", packet)
        second = requests.submit(request, "put", gzip.compress(packet), {"Content-Encoding": "gzip"})
        assert sorted((first.result(), second.result())) == [
            (200, {"ok": True}), (409, {"error": "device link request already sent"}),
        ]
    assert request("get", request_slot) == (200, {"data": sealed_request})
    assert request("get", response_slot) == (404, {"error": "device link waiting"})
    assert request("put", json.dumps(link | {"slot": "response", "data": "sealed response"})) == (200, {"ok": True})
    assert request("get", response_slot) == (200, {"data": "sealed response"})
    assert request("close", json.dumps(link)) == (200, {"ok": True})
    assert request("get", request_slot) == (404, {"error": "device link waiting"})
    assert request("put", packet) == (409, {"error": "device link closed"})
    source = {"CF-Connecting-IP": "192.0.2.10"}
    for _ in range(300):
        assert request("get", request_slot, source)[0] == 404
    assert request("get", request_slot, source) == (429, {"error": "device link rate limit exceeded"})
    assert request("get", request_slot, {"CF-Connecting-IP": "192.0.2.11"})[0] == 404


def recovery_request(host, port, action, name, proof=None):
    data = {"account": name}
    if proof is not None:
        data["proof"] = proof
    with ExitStack() as resources:
        client = HTTPConnection(host, port, timeout=3)
        resources.callback(client.close)
        client.request("POST", "/api/v1/account-backup/" + action, json.dumps(data),
                       {"CF-Connecting-IP": "192.0.2.110"})
        response = client.getresponse()
        return response.status, json.loads(response.read())


def prepare_backup_restart(host, port, database):
    account = hashlib.sha256(b"persistent lock fixture").hexdigest()
    proof = bytes.fromhex("42" * 32)
    with sqlite3.connect(database) as store:
        store.execute("INSERT INTO server_users(user_id_hash,public_key) VALUES(?,?)", (account, b"fixture key"))
        store.execute("INSERT INTO server_account_backups(user_id_hash,backup,salt,verifier,updated_at) VALUES(?,?,?,?,?)",
                      (account, "existing encrypted backup", "ab" * 16, hashlib.sha256(proof).hexdigest(), int(time.time())))
    status, decoy = recovery_request(host, port, "salt", "restart_decoy")
    assert status == 200
    for _ in range(5):
        assert recovery_request(host, port, "open", account, "0" * 64) == (403, {"error": "wrong username or password"})
    return account, proof.hex(), decoy


def check_backup_restart(binary, env, host, state):
    account, proof, decoy = state
    with subprocess.Popen([binary], env=env | {"MESH_ADMIN_TOKEN": ""}, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as server:
        try:
            readable, _, _ = select.select([server.stdout], [], [], 5)
            assert readable, "native server did not report a port on restart"
            port = int(server.stdout.readline())
            assert recovery_request(host, port, "salt", "restart_decoy") == (200, decoy)
            status, body = recovery_request(host, port, "open", account, proof)
            assert status == 429 and 1 <= body["retry_after"] <= 60
            status, peers = read_trusted_peers(host, port)
            assert status == 200 and len(peers["peers"]) == 2
            assert read_trusted_peers(host, port, {"X-Forwarded-For": "127.0.0.1"}) == (
                403, {"error": "administration requires loopback access or MESH_ADMIN_TOKEN"},
            )
        finally:
            if server.poll() is None:
                server.terminate()
            try:
                _, errors = server.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.communicate()
                raise
        assert server.returncode == 0, errors


def node_status(host, port, method="GET"):
    with closing(HTTPConnection(host, port, timeout=5)) as connection:
        connection.request(method, "/api/v1/node", headers={"Accept-Encoding": "gzip"})
        response = connection.getresponse()
        body = response.read()
        assert response.status == 200, (response.status, body)
        if method == "HEAD":
            assert body == b""
            return None
        if response.getheader("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        return json.loads(body)


def check_node_status(binary, database):
    settings = {
        "MESH_NODE_NAME": "Fixture node",
        "MESH_KNOWN_NODES": (
            "mirror|http://127.0.0.1:43201/;mode=RECEIVE;collection=private.stats.*;data=encrypted_records,"
            "duplicate=http://127.0.0.1:43201;mode=push,http://127.0.0.1:43202;enabled=no"
        ),
    }
    with pairing_node(binary, database, **settings) as port:
        current, stale = "01" * 32, "02" * 32
        records = ["private.stats.v1.items", "private.stats.v1.other", "private.other.v1.items"]
        with closing(sqlite3.connect(database)) as store, store:
            for user in (current, stale):
                store.execute("INSERT INTO server_users(user_id_hash,public_key) VALUES (?,?)",
                              (user, bytes.fromhex(user)))
                store.execute("INSERT INTO server_clients(user_id_hash,client_id) VALUES (?,?)", (user, "fixture"))
            store.execute("UPDATE server_users SET last_seen_at='2000-01-01' WHERE user_id_hash=?", (stale,))
            store.execute("UPDATE server_clients SET last_seen_at='2000-01-01' WHERE user_id_hash=?", (stale,))
            for app, prefix in (("wide", "private.stats.*"), ("exact", records[0]), ("empty", "private.empty.*")):
                store.execute("INSERT INTO server_apps(app_id,display_name) VALUES (?,?)", (app, app))
                store.execute("INSERT INTO server_app_collections(app_id,collection_prefix) VALUES (?,?)", (app, prefix))
            for collection in records:
                store.execute(
                    "INSERT INTO server_encrypted_records(user_id_hash,collection,id,ciphertext,updated_at) "
                    "VALUES (?,?,?,?,?)", (current, collection, "row", "seal", "2026-01-01T00:00:00.000000000Z"))
            store.execute("INSERT INTO server_encrypted_payloads(user_id_hash,client_id,payload_json) VALUES (?,?,?)",
                          (current, "fixture", "{}"))

        status = node_status("127.0.0.1", port)
        public = bytes.fromhex(Path(f"{database}.node-key").read_text())[-32:]
        assert status["node_id"] == hashlib.sha256(public).hexdigest() and status["node_public_key"] == public.hex()
        assert status["status"] == "ok" and status["node_name"] == "Fixture node"
        assert status["protocol"] == {"min_supported": 1, "latest": 6} and status["build_version"]
        # The exact Go order: node and sync responses share it, then configured extras.
        assert status["capabilities"] == NODE_CAPABILITIES + ["http-zstd-v1"], status["capabilities"]
        assert status["known_nodes"] == [
            {"name": "mirror", "url": "http://127.0.0.1:43201", "sync": {
                "direction": "pull", "collections": ["private.stats.*"], "data": ["encrypted_records"]}},
            {"url": "http://127.0.0.1:43202", "sync": {"direction": "none"}},
        ]
        usage = status["usage"]
        assert (usage["registered_users"], usage["active_users_30d"]) == (2, 1)
        assert (usage["registered_clients"], usage["active_clients_30d"]) == (2, 1)
        assert (usage["connected_users"], usage["connected_websocket_clients"]) == (0, 0)
        storage = status["storage"]
        actual = {app["app_id"]: [(row["collection"], row.get("collection_prefix"), row["record_bytes"])
                                  for row in app["collections"]] for app in storage["apps"]}
        assert actual == {"empty": [], "entity": [], "inbe": [],
                          "exact": [(records[0], records[0], len(records[0]) + 7)],
                          "wide": [(records[1], "private.stats.*", len(records[1]) + 7)],
                          "unregistered": [(records[2], None, len(records[2]) + 7)]}, actual
        assert storage["database_total_bytes"] >= storage["database_file_bytes"] > 0
        assert storage["sqlite_page_bytes"] > 0
        assert storage["unassigned_encrypted_payloads"] == {"count": 1, "logical_bytes": 9}
        assert storage["encrypted_record_bytes"] == sum(len(collection) + 7 for collection in records)
        assert storage["logical_bytes"] == storage["encrypted_record_bytes"] + 9
        assert storage["unassigned_bytes"] == len(records[2]) + 7 + 9
        node_status("127.0.0.1", port, "HEAD")

        with closing(HTTPConnection("127.0.0.1", port, timeout=5)) as connection:
            connection.request("GET", "/metrics")
            response = connection.getresponse()
            assert response.status == 401 and json.loads(response.read()) == {"error": "admin token required"}
            connection.request("GET", "/healthz")
            response = connection.getresponse()
            assert response.status == 200
            response.read()
            connection.request("GET", "/private-fixture-path")
            response = connection.getresponse()
            assert response.status == 404 and json.loads(response.read()) == {"error": "not found"}
            challenge = f"/api/v1/sync/challenge?user_id={current}"
            for expected in [200] * 20 + [429] * 2:
                connection.request("GET", challenge)
                response = connection.getresponse()
                assert response.status == expected, (response.status, response.read())
                response.read()
            connection.request("GET", "/metrics", headers={"X-Mesh-Admin": "pairing-test"})
            response = connection.getresponse()
            assert response.status == 200 and response.getheader("Content-Type") == "text/plain; version=0.0.4; charset=utf-8"
            body = response.read().decode()
        samples = set(body.splitlines())
        assert "mesh_registered_users 2" in samples
        assert "mesh_active_clients_30d 1" in samples
        assert 'mesh_storage_collection_logical_bytes{app_id="exact",collection="private.stats.v1.items"} 29' in samples
        assert 'mesh_http_requests_total{method="GET",route="/healthz",status="200"} 1' in samples, body
        assert '# TYPE mesh_http_request_duration_milliseconds_total counter' in samples
        prefix = 'mesh_http_request_duration_milliseconds_total{method="GET",route="/healthz",status="200"} '
        assert int(next(sample[len(prefix):] for sample in samples if sample.startswith(prefix))) >= 0
        assert 'mesh_http_requests_total{method="GET",route="unmatched",status="404"} 1' in samples
        assert 'ksync_http_requests_total{method="GET",route="/metrics",status="401"} 1' in samples
        assert "# TYPE mesh_rate_limited_requests_total counter" in samples
        assert "mesh_rate_limited_requests_total 2" in samples
        assert "ksync_rate_limited_requests_total 2" in samples
        # Every Go family name reaches native, with the same type and labels.
        families = {
            parts[2]: parts[3]
            for line in samples if line.startswith("# TYPE ")
            for parts in [line.split(" ")]
        }
        for name in GO_METRIC_FAMILIES:
            assert name in families, (name, sorted(families))
        assert families["mesh_websocket_active"] == "gauge", families["mesh_websocket_active"]
        assert families["ksync_websocket_active"] == "gauge"
        assert families["mesh_rate_limited_requests_total"] == "counter"
        assert families["mesh_storage_app_logical_bytes"] == "gauge"
        assert "/private-fixture-path" not in body


def check_payment_wiring(binary, directory):
    credentials = {name + suffix: "" for name in (
        "MESH_GOOGLE_SERVICE_ACCOUNT_JSON", "MESH_GOOGLE_OAUTH_CLIENT_JSON", "MESH_GOOGLE_OAUTH_REFRESH_TOKEN",
        "MESH_TOKEN_ISSUER_PRIVATE_KEY_HEX", "MESH_LUMI_WEBHOOK_SECRET", "MESH_LUMI_BOT_TOKEN",
    ) for suffix in ("", "_FILE")}
    credentials.update(MESH_TELEGRAM_ACCOUNTS_ENABLED="false", MONERO_WALLET_RPC_URL="")
    route = "/api/v1/tokens/purchases/google/verify"
    capability = "google-play-purchases"
    database = directory / "payments-disabled.db"
    with pairing_node(binary, database, **credentials) as port:
        assert capability not in node_status("127.0.0.1", port)["capabilities"]
        with closing(sqlite3.connect(database)) as store:
            assert store.execute("SELECT COUNT(linked_at) FROM server_lumi_telegram").fetchone() == (0,)
        with closing(HTTPConnection("127.0.0.1", port, timeout=5)) as connection:
            connection.request("GET", route)
            reply = connection.getresponse()
            assert (reply.status, reply.getheader("Allow"), reply.read()) == (405, "POST", b"")
            connection.request("POST", route, body="{}", headers={"Content-Type": "application/json"})
            reply = connection.getresponse()
            assert reply.status == 401
            reply.read()
            connection.request("GET", "/api/v1/tokens/purchases/telegram/catalog?app_id=inbe")
            reply = connection.getresponse()
            assert reply.status == 401
            reply.read()
            reconcile = "/api/v1/admin/tokens/purchases/telegram/reconcile"
            admin = {"X-Mesh-Admin": "pairing-test", "Content-Type": "application/json"}
            connection.request("POST", reconcile, body="{}", headers=admin)
            reply = connection.getresponse()
            assert reply.status == 503
            reply.read()
    with pairing_node(binary, directory / "play-configured.db",
                      **(credentials | {"MESH_GOOGLE_SERVICE_ACCOUNT_JSON": "{}"})) as port:
        assert capability in node_status("127.0.0.1", port)["capabilities"]


def check_peer_pulls(binary, directory, oracle):
    source = directory / "pull-source.db"
    receiver = directory / "pull-receiver.db"
    configured = directory / "pull-configured.db"
    collection = "private.pull-fixture.v1.items"
    pull = {"direction": "pull", "collections": ["private.pull-fixture.v1.*"], "data": ["encrypted_records"]}
    push = pull | {"direction": "push"}
    key = sign_with_oracle(oracle, "device-keypair")
    public = base64.b64decode(key["PublicKey"])
    account = hashlib.sha256(public).hexdigest()

    def node_key(path):
        public_key = bytes.fromhex(Path(f"{path}.node-key").read_text())[-32:]
        return hashlib.sha256(public_key).hexdigest(), public_key

    def trust(store, path, policy, addresses):
        node, public_key = node_key(path)
        store.execute(
            "INSERT INTO trusted_node_peers(node_id,public_key,addresses_json,policy_json,trusted_at) "
            "VALUES (?,?,?,?,?)", (node, public_key, json.dumps(addresses), json.dumps(policy), str(int(time.time()))))

    def put(store, record):
        store.execute(
            "INSERT INTO server_encrypted_records(user_id_hash,collection,id,ciphertext,updated_at) "
            "VALUES (?,?,?,?,?)", (account, collection, record, "sealed", "2026-01-01T00:00:00.000000000Z"))

    def read(path, query, params=()):
        with closing(sqlite3.connect(path)) as store:
            return store.execute(query, params).fetchall()

    def await_rows(path, query, wanted, params=()):
        deadline = time.monotonic() + 7
        while True:
            rows = read(path, query, params)
            if rows == wanted:
                return
            assert time.monotonic() < deadline, (query, rows, wanted)
            time.sleep(0.05)

    with pairing_node(binary, receiver):
        pass
    with pairing_node(binary, configured):
        pass
    token = f"{node_key(configured)[0]}:test-peer-token"
    with pairing_node(binary, source, MESH_LAN_ONLY="0", MESH_NODE_SYNC_TOKEN=token) as port:
        address = f"http://127.0.0.1:{port}"
        with closing(sqlite3.connect(source)) as store, store:
            trust(store, receiver, push, [])
            trust(store, configured, push, [])
            store.execute("INSERT INTO server_users(user_id_hash,public_key) VALUES (?,?)", (account, public))
            put(store, "first")
            put(store, "second")
        with closing(sqlite3.connect(receiver)) as store, store:
            trust(store, source, pull, [address])
            store.execute("CREATE TRIGGER reject_peer_cursor BEFORE INSERT ON node_sync_cursors "
                          "BEGIN SELECT RAISE(ABORT, 'fixture'); END")

        records = "SELECT id FROM server_encrypted_records ORDER BY id"
        cursor = "SELECT peer_key,cursor FROM node_sync_cursors"
        cursor_key = hashlib.sha256(address.encode() + b"\0" + json.dumps(pull, separators=(",", ":")).encode()).hexdigest()

        def saved(sequence):
            raw = json.dumps({"seq": sequence, "updated_at": "", "user_id_hash": "", "collection": "", "id": ""},
                             separators=(",", ":")).encode()
            return [(cursor_key, base64.urlsafe_b64encode(raw).decode().rstrip("="))]

        options = {"MESH_NODE_SYNC_INTERVAL_SECONDS": "1", "MESH_NODE_SYNC_BATCH_LIMIT": "1"}
        with pairing_node(binary, receiver, **options):
            await_rows(source, "SELECT COUNT(*) >= 2 FROM node_request_nonces WHERE node_id=?",
                       [(1,)], (node_key(receiver)[0],))
            with closing(sqlite3.connect(receiver)) as store, store:
                store.execute("BEGIN IMMEDIATE")
                assert store.execute(records).fetchall() == []
                assert store.execute(cursor).fetchall() == []
                store.execute("DROP TRIGGER reject_peer_cursor")
            await_rows(receiver, records, [("first",), ("second",)])
            assert read(receiver, cursor) == saved(2)
        with closing(sqlite3.connect(source)) as store, store:
            put(store, "third")
        with pairing_node(binary, receiver, **options):
            await_rows(receiver, records, [("first",), ("second",), ("third",)])
            assert read(receiver, cursor) == saved(3)
        entry = f"fixture={address};mode=pull;collection=private.pull-fixture.v1.*;data=encrypted_records"
        with pairing_node(binary, configured, MESH_LAN_ONLY="0", MESH_KNOWN_NODES=entry,
                          MESH_NODE_SYNC_TOKEN=token, **options):
            await_rows(configured, records, [("first",), ("second",), ("third",)])
            assert read(configured, cursor) == saved(3)

    with socket.socket(socket.AF_INET) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(5)
        address = f"http://127.0.0.1:{listener.getsockname()[1]}"
        entry = f"fixture={address};mode=pull;collection=private.pull-fixture.v1.*;data=encrypted_records"
        with ExitStack() as connections:
            with pairing_node(binary, configured, MESH_LAN_ONLY="0", MESH_KNOWN_NODES=entry,
                              MESH_NODE_SYNC_TOKEN=token, **options):
                connection, _ = listener.accept()
                connections.enter_context(connection)
                connection.settimeout(5)
                request = connection.makefile("rb")
                connections.enter_context(request)
                assert request.readline() == b"POST /api/v1/node/mesh/export HTTP/1.1\r\n"
                # Shutdown must interrupt the transfer even though the peer is still connected.


def check_pull_headers(binary, directory, oracle):
    """A pre-rename peer accepts only the X-Daochi-Node-* signing headers.

    Peers installed before the Mesh rename read the legacy names, so the pull
    client must send both spellings. The fixture answers one export request and
    checks ID/Time/Nonce/Signature exactly as node_auth_native does, using the
    pulling node's paired key.
    """
    database = directory / "pull-headers.db"
    policy = {"direction": "pull", "collections": ["private.pull-fixture.v1.*"], "data": ["encrypted_records"]}

    with pairing_node(binary, database):
        pass
    public_key = bytes.fromhex(Path(f"{database}.node-key").read_text())[-32:]
    node = hashlib.sha256(public_key).hexdigest()

    received = []

    def serve(listener):
        listener.settimeout(20)
        try:
            connection, _ = listener.accept()
        except socket.timeout:
            raise AssertionError("pull client did not contact the legacy-only peer") from None
        with closing(connection), closing(connection.makefile("rb")) as stream:
            connection.settimeout(20)
            request_line = stream.readline()
            headers = {}
            while True:
                line = stream.readline()
                if line in (b"\r\n", b""):
                    break
                name, _, value = line.decode("latin-1").partition(":")
                headers[name.strip().lower()] = value.strip()
            length = int(headers.get("content-length", "0"))
            body = stream.read(length)
            received.append({"line": request_line, "headers": headers, "body": body})
            reply = json.dumps({"status": "ok", "records": [], "deletions": []}).encode()
            connection.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(reply)}\r\nConnection: close\r\n\r\n".encode()
                + reply)

    with socket.socket(socket.AF_INET) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        address = f"http://127.0.0.1:{listener.getsockname()[1]}"
        with closing(sqlite3.connect(database)) as store, store:
            store.execute(
                "INSERT INTO trusted_node_peers(node_id,public_key,addresses_json,policy_json,trusted_at) "
                "VALUES (?,?,?,?,?)",
                ("ab" * 32, bytes(32), json.dumps([address]), json.dumps(policy), str(int(time.time()))))
        worker = threading.Thread(target=serve, args=(listener,))
        worker.start()
        try:
            with pairing_node(binary, database, MESH_NODE_SYNC_INTERVAL_SECONDS="1"):
                deadline = time.monotonic() + 20
                while not received and time.monotonic() < deadline:
                    time.sleep(0.05)
        finally:
            worker.join(timeout=20)
    assert received, "pull client sent no request to the legacy-only peer"
    exchange = received[0]
    assert exchange["line"] == b"POST /api/v1/node/mesh/export HTTP/1.1\r\n", exchange["line"]
    headers = exchange["headers"]
    for field in ("ID", "Time", "Nonce", "Signature"):
        assert f"x-daochi-node-{field.lower()}" in headers, (field, sorted(headers))
    assert headers["x-daochi-node-id"] == node
    timestamp, nonce = headers["x-daochi-node-time"], headers["x-daochi-node-nonce"]
    assert timestamp.isdigit() and abs(int(timestamp) - time.time()) <= 300
    assert 1 <= len(nonce) <= 128
    signature = base64.urlsafe_b64decode(headers["x-daochi-node-signature"] + "=" * 4)
    # The header names are legacy; the signed domain is the current one, as in
    # node_auth_native.Signed before its daochi fallback.
    message = "\n".join(("mesh-node-request-v1", node, timestamp, nonce, "POST",
                         "/api/v1/node/mesh/export", hashlib.sha256(exchange["body"]).hexdigest(), "")).encode()
    verified = sign_with_oracle(oracle, "device-verify", PublicKey=base64.b64encode(public_key).decode(),
                                Message=base64.b64encode(message).decode(),
                                Signature=base64.b64encode(signature).decode())
    assert verified["Verified"], "Daochi-named node request signature did not verify"
    assert json.loads(exchange["body"])["policy"] == policy


def run_listener(binary, host, database, oracle, server_mode=False):
    env = {**os.environ, "MESH_TEST_HTTP_DATABASE": str(database),
           "MESH_TEST_HTTP_IPV6": "1" if host == "::1" else "0"}
    if server_mode:
        expired, retained = seed_holds(database)
        blob_directory, blob_names = seed_trashed_blobs(database)
        purge_days = 30 if host == "127.0.0.1" else 0
        issuer = sign_with_oracle(oracle, "device-keypair")
        issuer_path = database.with_suffix(".issuer-public")
        issuer_path.write_text(base64.b64decode(issuer["PublicKey"]).hex() + "\n")
        registry_authority = sign_with_oracle(oracle, "device-keypair")
        registry_path = database.with_suffix(".registry-public")
        registry_path.write_text(base64.b64decode(registry_authority["PublicKey"]).hex() + "\n")
        address = f"[{host}]" if ":" in host else host
        env.update(MESH_TOKEN_ISSUER_PUBLIC_KEY_HEX="", MESH_TOKEN_ISSUER_PUBLIC_KEY_HEX_FILE=str(issuer_path),
                   MESH_TOKEN_ISSUER_PRIVATE_KEY_HEX="", MESH_TOKEN_ISSUER_PRIVATE_KEY_HEX_FILE="",
                   MESH_TOKEN_PRODUCTS="second:30:5000,first:20", MESH_TOKEN_DIRECT_PURCHASES_ENABLED="0",
                   MESH_NODE_REGISTRY_PUBLIC_KEY_HEX="", MESH_NODE_REGISTRY_PUBLIC_KEY_HEX_FILE=str(registry_path),
                   MESH_NODE_IDENTITY_KEY_FILE=f"{database}.node-key", MESH_NODE_IDENTITY_PRIVATE_KEY_HEX="",
                   MESH_NODE_IDENTITY_PRIVATE_KEY_HEX_FILE="",
                   MESH_ADDR=f"{address}:0", MESH_DB=str(database), MESH_TOKEN_SECRET_HEX="00" * 32,
                   MESH_LAN_ONLY="1" if host == "127.0.0.1" else "0", MESH_LAN_NETWORKS="127.0.0.0/8",
                   MESH_TRASH_PURGE_DAYS=str(purge_days),
                   MESH_MAX_BODY_BYTES="8192", MESH_ACCOUNT_STORAGE_LIMIT_BYTES="1000",
                   MESH_ADMIN_TOKEN="mesh-test-registry-administration", MESH_NODE_SYNC_TOKEN="",
                   MESH_NODE_SYNC_INTERVAL_SECONDS="0",
                   MESH_ENCRYPTED_PAYLOAD_MAX_RETURN="2", MESH_ENCRYPTED_PAYLOAD_MAX_ACCOUNT_BYTES="400",
                   MESH_ENCRYPTED_PAYLOAD_RETENTION_DAYS="90",
                   MESH_CHALLENGE_TTL_SECONDS="60", MESH_TOKEN_TTL_SECONDS="3600",
                   MESH_STORAGE_PLANS_ENABLED="0", MESH_HTTP_ZSTD_REQUESTS="1", MESH_HTTP_ZSTD_RESPONSES="1")
    with subprocess.Popen([binary], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE) as server, ExitStack() as resources:
        closing_stream = None
        try:
            readable, _, _ = select.select([server.stdout], [], [], 5)
            if not readable:
                raise RuntimeError("native listener did not report a bound port")
            port = int(server.stdout.readline())
            if server_mode:
                check_server_health(host, port)
                check_token_routes(host, port, issuer_path.read_text().strip())
                check_hold_retention(database, expired, retained)
                check_blob_retention(database, blob_directory, blob_names, purge_days > 0)
                check_app_registry(host, port, database)
                check_signed_app_registration(host, port, database, oracle, registry_authority, host == "127.0.0.1")
                check_trusted_peers(host, port, database, env["MESH_ADMIN_TOKEN"])
                check_namespace(host, port, database, oracle, env["MESH_ADMIN_TOKEN"])
                check_peer_exchange(host, port, database, oracle)
                check_portable_bundle(host, port, database, oracle)
                check_portable_release(host, port, database, oracle, issuer)
                check_device_link(host, port)
            else:
                check_http(host, port)
                check_client_address(host, port)
                check_request_target(host, port)
                check_authentication(host, port, database)
                check_websocket(host, port)
            bearer = check_signed_login(host, port, database, oracle, server_mode)
            if server_mode:
                backup_restart = prepare_backup_restart(host, port, database)
                _, closing_stream = open_sync_socket(resources, host, port, bearer)
                assert read_sync_event(closing_stream)["type"] == "sync_ready"
                open_sync_socket(resources, host, port, bearer)
                usage = node_status(host, port)["usage"]
                assert usage["connected_users"] == 1 and usage["connected_websocket_clients"] == 2, usage
                with closing(HTTPConnection(host, port, timeout=5)) as connection:
                    connection.request("GET", "/metrics", headers={"X-Mesh-Admin": env["MESH_ADMIN_TOKEN"]})
                    response = connection.getresponse()
                    assert response.status == 200
                    samples = set(response.read().decode().splitlines())
                assert {"mesh_connected_users 1", "mesh_websocket_active 2", "ksync_websocket_active 2"} <= samples
        finally:
            try:
                if server_mode and server.poll() is None:
                    server.terminate()
                _, errors = server.communicate(input=None if server_mode else b"\n", timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.communicate()
                raise
        assert server.returncode == 0, errors
        if closing_stream:
            assert closing_stream.read(1) == b""

    if server_mode:
        key_path = Path(env["MESH_NODE_IDENTITY_KEY_FILE"])
        established_key = key_path.read_bytes()
        check_backup_restart(binary, env, host, backup_restart)
        assert key_path.read_bytes() == established_key
        if host == "127.0.0.1":
            check_lan_startup(binary, env)
    return port


def check_lan_startup(binary, env):
    def rejects(settings, message):
        result = subprocess.run([binary], env=env | settings, capture_output=True, timeout=5)
        assert result.returncode == 1 and not result.stdout and message in result.stderr, result

    local_required = b"LAN-only nodes require explicit private LAN networks and a local bind address"
    rejects({"MESH_ADDR": "0.0.0.0:0"}, local_required)
    rejects({"MESH_LAN_NETWORKS": "8.8.8.0/24"}, local_required)
    rejects({"MESH_LAN_NETWORKS": "127.0.0.0/8,"}, local_required)
    rejects({"MESH_LAN_NETWORKS": "fc00::/7"}, b"LAN network is not within a connected local subnet")
    corrupt_key = Path(env["MESH_NODE_IDENTITY_KEY_FILE"]).with_suffix(".corrupt-key")
    written = "00" * 64 + "\n"
    corrupt_key.write_text(written)
    rejects({"MESH_NODE_IDENTITY_KEY_FILE": str(corrupt_key)}, b"could not load the Mesh node identity")
    assert corrupt_key.read_text() == written
    fallback = corrupt_key.with_suffix(".fallback-key")
    credential = corrupt_key.with_suffix(".credential")
    bad_identity = b"could not load the Mesh node identity"
    configured = {"MESH_NODE_IDENTITY_KEY_FILE": str(fallback),
                  "MESH_NODE_IDENTITY_PRIVATE_KEY_HEX_FILE": str(credential)}
    rejects(configured, bad_identity)
    credential.write_text("\n")
    rejects(configured, bad_identity)
    credential.write_text("11" * 32 + "\n")
    rejects(configured | {"MESH_NODE_IDENTITY_PRIVATE_KEY_HEX": "invalid-fixture-key"}, bad_identity)
    rejects(configured | {"MESH_NODE_IDENTITY_PRIVATE_KEY_HEX": "00" * 64}, bad_identity)
    assert not fallback.exists()
    # RFC 8032 test vector 1. The seed and expanded forms identify the same node.
    seed = "9d61b19deffd5a60ba844af492ec2cc4" "4449c5697b326919703bac031cae7f60"
    public = "d75a980182b10ab7d54bfed3c964073a" "0ee172f3daa62325af021a68f707511a"
    credential.write_text(seed + "\n")
    for inline in ("", seed + public):
        settings = configured | {"MESH_NODE_IDENTITY_PRIVATE_KEY_HEX": inline}
        with pairing_node(binary, fallback.with_suffix(".db"), **settings) as port:
            assert node_status("127.0.0.1", port)["node_public_key"] == public
    assert not fallback.exists()
    rejects({"MESH_LAN_ONLY": "0"}, b"stored app manifest forbids this node network scope")


def check_feedback_token_settings(binary, directory):
    token_file = directory / "operator.token"
    token_file.write_text("file-operator\n")
    database = directory / "feedback-token.db"
    common = {"MESH_ADMIN_TOKEN": "", "MESH_ADMIN_TOKEN_FILE": str(token_file)}
    for settings, allowed, denied in (
        (common | {"MESH_FEEDBACK_TOKEN": "", "MESH_FEEDBACK_TOKEN_FILE": ""}, "file-operator", "pairing-test"),
        (common | {"MESH_FEEDBACK_TOKEN": "direct-operator", "MESH_FEEDBACK_TOKEN_FILE": str(token_file)},
         "direct-operator", "file-operator"),
    ):
        with pairing_node(binary, database, **settings) as port, closing(HTTPConnection("127.0.0.1", port, timeout=5)) as conn:
            for token, status in ((denied, 401), (allowed, 200)):
                conn.request("GET", "/api/v1/admin/feedback", headers={"X-Mesh-Admin": token})
                response = conn.getresponse()
                assert response.status == status
                response.read()


def check_server_cli(binary, database):
    version = os.environ.get("MESH_TEST_BUILD_VERSION", "dev")
    env = os.environ | {"MESH_DB": str(database), "MESH_TOKEN_SECRET_HEX": "",
                        "MESH_ALLOW_EPHEMERAL_TOKEN_SECRET": "0"}

    def run(*arguments):
        return subprocess.run([binary, *arguments], env=env, capture_output=True, timeout=5)

    for option in ("--help", "-h", "help"):
        result = run(option)
        assert result.returncode == 0 and not result.stderr and result.stdout.startswith(b"usage: mesh "), result
    for option in ("--version", "version"):
        result = run(option)
        assert (result.returncode, result.stdout, result.stderr) == (0, f"mesh {version}\n".encode(), b""), result
    result = run("unknown-command")
    assert result.returncode == 1 and not result.stdout and b"unknown Mesh command" in result.stderr, result
    result = run("serve", "extra")
    assert result.returncode == 1 and not result.stdout and result.stderr == b"usage: mesh serve\n", result
    result = run("inspect", "--help")
    assert result.returncode == 0 and not result.stderr and b"usage: mesh inspect" in result.stdout, result
    result = run("inspect", "summary")
    assert result.returncode == 1 and b"cannot open database read-only" in result.stderr, result
    assert not database.exists()

    with pairing_node(binary, database, arguments=("serve",)) as port:
        assert node_status("127.0.0.1", port)["build_version"] == version
        with closing(HTTPConnection("127.0.0.1", port, timeout=5)) as connection:
            connection.request("GET", "/metrics", headers={"X-Mesh-Admin": "pairing-test"})
            response = connection.getresponse()
            assert response.status == 200
            label = version.replace("\\", "\\\\").replace('"', '\\"')
            assert f'mesh_build_info{{version="{label}"}} 1' in response.read().decode().splitlines()

    stat = database.stat()
    result = run("inspect", "summary")
    assert result.returncode == 0 and not result.stderr and result.stdout.startswith(b"Mesh data summary\n"), result
    updated = database.stat()
    assert (stat.st_size, stat.st_mtime_ns) == (updated.st_size, updated.st_mtime_ns)


def main():
    binary = Path(sys.argv[1]).resolve()
    oracle = Path(sys.argv[2]).resolve()
    server_mode = sys.argv[3:] == ["--server"]
    if server_mode:
        # Native rates refresh by default. Only explicit loopback providers are
        # reachable in this suite, including in its subprocess fixtures.
        os.environ.update(MESH_XMR_RATE_REFRESH_ENABLED="false",
                          https_proxy="http://127.0.0.1:1", HTTPS_PROXY="http://127.0.0.1:1",
                          no_proxy="127.0.0.1,localhost,::1", NO_PROXY="127.0.0.1,localhost,::1")
    with TemporaryDirectory(dir=binary.parent) as directory:
        for host, family in (("127.0.0.1", socket.AF_INET), ("::1", socket.AF_INET6)):
            database = Path(directory) / f"mesh-{family}.db"
            port = run_listener(binary, host, database, oracle, server_mode)
            with socket.socket(family) as listener:
                if family == socket.AF_INET6:
                    listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind((host, port))
                listener.listen()
        if server_mode:
            check_legacy_environment(binary, Path(directory))
            check_node_pairing(binary, Path(directory), oracle)
            check_peer_pulls(binary, Path(directory), oracle)
            check_pull_headers(binary, Path(directory), oracle)
            check_node_status(binary, Path(directory) / "node-status.db")
            check_payment_wiring(binary, Path(directory))
            check_feedback_token_settings(binary, Path(directory))
            check_server_cli(binary, Path(directory) / "cli.db")
    if server_mode:
        fixture_dir = Path(__file__).resolve().parent
        checks = Path(os.environ["MESH_NATIVE_CHECKS_DIR"])
        for name in ("check_docs_http", "check_legacy_headers", "check_server_policy"):
            subprocess.run([checks / name, binary], check=True)
        subprocess.run([sys.executable, "-B", fixture_dir / "check_exchange_rate_http.py", binary], check=True)
        for fixture in ("check_feedback_http.py", "check_publications_http.py", "check_social_http.py",
                        "check_chat_http.py", "check_google_account_http.py", "check_lists_http.py",
                        "check_social_socket.py", "check_lumi_developer_http.py", "check_token_http.py",
                        "check_entitlements_http.py", "check_token_rewards_http.py", "check_alias_http.py",
                        "check_game_evidence_http.py", "check_monero_http.py", "check_monero_reconcile.py",
                        "check_referral_http.py"):
            subprocess.run([sys.executable, "-B", fixture_dir / fixture, binary, oracle], check=True)
        for fixture in ("check_delegated_http.py", "check_delegated_ws.py",
                        "check_lumi_telegram_http.py", "check_telegram_account_http.py",
                        "check_telegram_stars_http.py"):
            subprocess.run([sys.executable, "-B", fixture_dir / fixture, binary, oracle, "--server"], check=True)

if __name__ == "__main__":
    main()
