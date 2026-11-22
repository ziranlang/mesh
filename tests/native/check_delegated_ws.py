"""Compare delegated WebSocket events and authentication to the Go protocol."""

import argparse
import base64
from contextlib import ExitStack, closing
from http.client import HTTPConnection
from pathlib import Path
import secrets
import select
import socket
import sqlite3
import tempfile

from check_delegated_http import BOT_TOKEN, COLLECTION, SYNC, encode, fixture, verify
from check_http_listener import masked_frame, open_sync_socket, pairing_node, read_sync_event


WS = "/api/v1/delegated/ws"
QUERY = f"collection={COLLECTION}"
UPGRADE = {
    "Upgrade": "websocket",
    "Connection": "Upgrade",
    "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
    "Sec-WebSocket-Version": "13",
    "Sec-WebSocket-Protocol": "daochi-sync-v1",
}


def browser(headers, *, prefix="mesh"):
    proof = base64.urlsafe_b64encode(headers["X-Mesh-Delegate"].encode()).rstrip(b"=").decode()
    credential = headers["Authorization"].removeprefix("Delegate ds1.")
    return {"Sec-WebSocket-Protocol": f"daochi-sync-v1, {prefix}.delegate.{proof}, {prefix}.session.{credential}"}


def denied(port, target, headers, status, body, *, method="GET"):
    with closing(HTTPConnection("127.0.0.1", port, timeout=3)) as connection:
        connection.request(method, target, headers=UPGRADE | headers)
        response = connection.getresponse()
        assert response.status == status, (response.status, target)
        assert body in response.read(), target
        if status == 405:
            assert response.getheader("Allow") == "GET, HEAD"


def connect(resources, port, headers):
    peer = resources.enter_context(socket.create_connection(("127.0.0.1", port), timeout=3))
    stream = resources.enter_context(peer.makefile("rb"))
    fields = UPGRADE | headers
    request = f"GET {WS}?{QUERY} HTTP/1.1\r\nHost: localhost\r\n"
    request += "".join(f"{name}: {value}\r\n" for name, value in fields.items()) + "\r\n"
    peer.sendall(request.encode("ascii"))
    status = stream.readline(512)
    assert status.startswith(b"HTTP/1.1 101 "), status
    response_headers = {}
    for _ in range(24):
        line = stream.readline(512)
        if line == b"\r\n":
            break
        name, value = line.rstrip().split(b":", 1)
        response_headers[name.lower()] = value.strip()
    assert response_headers[b"sec-websocket-protocol"] == b"daochi-sync-v1"
    assert response_headers[b"sec-websocket-accept"] == b"s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
    return peer, stream


def event(stream, version):
    assert read_sync_event(stream) == {"type": "collection_changed", "collection": COLLECTION, "version": version}


def quiet(peer):
    assert not select.select([peer], [], [], 0.8)[0], "delegate socket exposed an unrelated event or an ack"


def check_socket(port, database, call, proof, issue_session, credential, owner_call, grant_id, bearer, account, read, record):
    def signed(*, nonce=None, query=QUERY, path=WS, using=None):
        return proof(b"", nonce=nonce, query=query, path=path, method="GET", using=using)

    target = f"{WS}?{QUERY}"
    denied(port, target, {}, 405, b"Method Not Allowed", method="POST")
    denied(port, target, {}, 401, b"delegate socket proof required")
    headers = signed()
    denied(port, target, browser(headers) | headers, 401, b"delegate socket proof required")
    for changed in (target.replace("delegated", "%64elegated"), target.replace("private", "%70rivate"), target + "&other=1"):
        denied(port, changed, browser(headers), 401, b"invalid delegate request proof")
    denied(port, target, browser(signed(path=SYNC)), 401, b"invalid delegate request proof")

    nonce = secrets.token_hex(16)
    other = "collection=private.inbe.v1.cells"
    denied(port, f"{WS}?{other}", browser(signed(nonce=nonce, query=other)), 403, b"delegate socket scope mismatch")
    accepted = browser(signed(nonce=nonce), prefix="daochi")
    with closing(sqlite3.connect(database)) as db, db:
        initial, = db.execute("SELECT MAX(server_version) FROM server_encrypted_records "
                              "WHERE user_id_hash=? AND collection=? AND key_id=?", (account, COLLECTION, record["key_id"])).fetchone()

    with ExitStack() as resources:
        peer, stream = connect(resources, port, accepted)
        event(stream, initial)
        denied(port, target, accepted, 409, b"delegate request replayed")
        _, ordinary = open_sync_socket(resources, "127.0.0.1", port, bearer.removeprefix("Bearer "))
        ordinary_event = read_sync_event(ordinary)
        assert ordinary_event["type"] == "sync_ready" and ordinary_event["user_id_hash"] == account, ordinary_event

        socket_record = dict(record, id="lumi.socket.denied")
        mutation = dict(read, sync=dict(read["sync"], encrypted_records=[socket_record]))
        peer.sendall(masked_frame(encode(mutation)))
        with closing(sqlite3.connect(database)) as db, db:
            assert db.execute("SELECT COUNT(*) FROM server_encrypted_records WHERE id=?", (socket_record["id"],)).fetchone() == (0,)
            for collection, key, identifier in (("private.inbe.v1.cells", record["key_id"], "outside.collection"),
                                                (COLLECTION, "rotated-owner-key", "outside.key")):
                db.execute("INSERT INTO server_encrypted_records (user_id_hash,collection,id,key_id,updated_at,server_version) VALUES(?,?,?,?,?,?)",
                           (account, collection, identifier, key, record["updated_at"], initial + 1000))
        quiet(peer)

        rejected = encode(dict(mutation, collection="private.inbe.v1.cells"))
        nonce = secrets.token_hex(16)
        call(SYNC, rejected, 403, proof(rejected, nonce=nonce))
        payload = encode(mutation)
        result = call(SYNC, payload, 200, proof(payload, nonce=nonce))
        assert result["applied"] == 1, result
        with closing(sqlite3.connect(database)) as db, db:
            version, = db.execute("SELECT server_version FROM server_encrypted_records WHERE id=?", (socket_record["id"],)).fetchone()
        assert initial < version < initial + 1000
        event(stream, version)
        with closing(sqlite3.connect(database)) as db, db:
            db.execute("UPDATE server_authorization_sessions SET expires_at=0 WHERE session_id=?", (credential["session_id"],))
        assert stream.read(1) == b"", "expired socket remained open"

    next_credential = issue_session()
    with ExitStack() as resources:
        peer, stream = connect(resources, port, signed(using=next_credential))
        event(stream, version)
        assert owner_call("revoke", dict(grant_id, app_id="inbe")) is True
        assert stream.read(1) == b"", "revoked socket remained open"
    denied(port, target, browser(signed(using=next_credential)), 401, b"authorization inactive on this node")
    return next_credential


def rate_limit(port):
    for _ in range(121):
        with closing(HTTPConnection("127.0.0.1", port, timeout=3)) as connection:
            connection.request("GET", f"{WS}?{QUERY}")
            response = connection.getresponse()
            body = response.read()
            if response.status == 429:
                assert b"authorization rate limit exceeded" in body
                return
            assert response.status == 401, (response.status, body)
    raise AssertionError("delegate socket guard did not limit requests")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("binary")
    parser.add_argument("oracle")
    parser.add_argument("--server", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as directory:
        database = Path(directory) / "delegation.db"
        listener = pairing_node(args.binary, database, MESH_LUMI_BOT_TOKEN=BOT_TOKEN) if args.server else fixture(args.binary, database)
        with listener as port:
            verify(port, database, args.oracle, check_socket=check_socket)
            rate_limit(port)
    print("Native delegated WebSocket sync loopback passed")


if __name__ == "__main__":
    main()
