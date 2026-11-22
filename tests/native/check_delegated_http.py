import argparse
import base64
import gzip
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import select
import sqlite3
import subprocess
import tempfile
import time
from contextlib import closing, contextmanager
from http.client import HTTPConnection
from urllib.parse import urlencode

from check_http_listener import make_bearer, pairing_node, sign_with_oracle


PREFIX = "/api/v1/authorization/"
SYNC = "/api/v1/delegated/sync"
AUDIENCE = "http://127.0.0.1"
COLLECTION = "private.inbe.v2.lumi"
BOT_TOKEN = "42:fixture"


def encode(value):
    return json.dumps(value, separators=(",", ":")).encode()


def lines(*values):
    return ("\n".join(map(str, values)) + "\n").encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


@contextmanager
def fixture(binary, database):
    env = {**os.environ, "MESH_TEST_HTTP_DATABASE": str(database)}
    with subprocess.Popen([binary], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE) as process:
        try:
            ready, _, _ = select.select([process.stdout], [], [], 5)
            assert ready, "fixture did not report its bound port"
            yield int(process.stdout.readline())
        finally:
            _, errors = process.communicate(input=b"\n", timeout=5)
            assert process.returncode == 0, errors


def verify(port, database, oracle, check_socket=None):
    owner = sign_with_oracle(oracle, "keypair")
    public = base64.b64decode(owner["PublicKey"])
    account = digest(public)
    device = sign_with_oracle(oracle, "device-keypair")
    delegate = sign_with_oracle(oracle, "device-keypair")
    device_id = "owner-device"
    client = "c" * 64
    signing_key = base64.b64decode(delegate["PublicKey"]).hex()
    encryption_key = "e" * 2368
    now = int(time.time())
    session_id = secrets.token_hex(32)
    bearer = make_bearer(lines("v2", account, now + 3600, now, session_id))
    token_hash = digest(bearer.removeprefix("Bearer ").encode())

    def sign(key, message, operation):
        return sign_with_oracle(oracle, operation, PrivateKey=key["PrivateKey"],
                                Message=base64.b64encode(message).decode())["Signature"]

    def delegate_sign(message):
        return base64.b64decode(sign(delegate, message, "device-sign")).hex()

    with closing(sqlite3.connect(database)) as writer, writer:
        writer.execute("INSERT INTO server_users(user_id_hash,public_key) VALUES (?,?)", (account, public))
        writer.execute("INSERT INTO server_auth_sessions(token_hash,user_id_hash,session_id,issued_at,expires_at,created_at) "
                       "VALUES (?,?,?,?,?,CURRENT_TIMESTAMP)", (token_hash, account, session_id, now, now + 3600))
        writer.execute("INSERT INTO server_device_keys(account_id,app_id,device_key_id,client_id,public_key) VALUES (?,?,?,?,?)",
                       (account, "inbe", device_id, "owner-client", base64.b64decode(device["PublicKey"]).hex()))

    def call(route, value, status, headers=None, *, compress=False, method="POST"):
        payload = value if isinstance(value, bytes) else encode(value)
        headers = {"Content-Type": "application/json", **(headers or {})}
        if compress:
            headers["Content-Encoding"] = "gzip"
        with closing(HTTPConnection("127.0.0.1", port, timeout=5)) as connection:
            connection.request(method, route, gzip.compress(payload) if compress else payload, headers)
            reply = connection.getresponse()
            body = reply.read()
            assert reply.status == status, (route, reply.status, body[:512])
            if status == 405:
                assert reply.getheader("Allow") == "POST"
                return None
            assert reply.getheader("Content-Type") == "application/json", (route, reply.headers)
            return json.loads(body)

    def owner_call(action, value, status=200):
        route = PREFIX + action
        payload = encode(value)
        expiry = int(time.time()) + 60
        identifier = secrets.token_hex(16)
        nonce = secrets.token_hex(16)
        message = lines("daochi-tx-v1", 6, identifier, account, "inbe", device_id, "POST",
                        route, digest(payload), nonce, expiry)
        transaction = {"protocol_version": 6, "tx_id": identifier, "account_id": account,
                       "app_id": "inbe", "device_key_id": device_id, "method": "POST", "path": route,
                       "body_sha256": digest(payload), "nonce": nonce, "expires_at": expiry,
                       "signature": sign(owner, message, "sign"), "device_signature": sign(device, message, "device-sign")}
        proof = base64.urlsafe_b64encode(encode(transaction)).decode().rstrip("=")
        return call(route, payload, status, {"Authorization": bearer, "X-Mesh-Tx": proof})

    request = {"app_id": "inbe", "scopes": [{"collection": COLLECTION, "read": True, "write": True}]}
    call(PREFIX + "session", {}, 405, method="GET")
    call(PREFIX + "challenge", b'{"grant_id":"' + b"1" * 32 + b'","grant_id":"' + b"2" * 32 + b'"}', 400)
    call(PREFIX + "requests", request, 401, {"Authorization": bearer})
    owner_call("requests", {"app_id": "inbe", "scopes": [{"collection": "private.inbe.v1.cells", "read": True}]}, 400)
    cancelled = owner_call("requests", request)
    assert owner_call("cancel", {"request_id": cancelled["request_id"], "app_id": "inbe"})["status"] == "cancelled"
    pending = owner_call("requests", request)
    pending_id = {"request_id": pending["request_id"], "app_id": "inbe"}
    redacted = call(PREFIX + "rendezvous", pending_id, 200)
    assert set(redacted) == {"request_id", "grant_id", "app_id", "node_id", "audience", "expires_at", "challenge", "status"}
    assert redacted["status"] == "pending" and len(redacted["node_id"]) == 64
    assert pending["audience"] == AUDIENCE

    def claim_for(row, init_data="", init_digest=""):
        message = lines("daochi-rendezvous-claim-v1", row["request_id"], "inbe", row["node_id"], row["audience"],
                        row["challenge"], row["expires_at"], client, signing_key, encryption_key, init_digest)
        return {"request_id": row["request_id"], "client_id": client, "signing_key": signing_key,
                "encryption_key": encryption_key, "proof": delegate_sign(message), "init_data": init_data}

    claim = claim_for(pending)
    assert call(PREFIX + "claim", dict(claim, proof="0" * 128), 401)["error"] == "delegate claim proof rejected"
    claimed = call(PREFIX + "claim", claim, 200, compress=True)
    assert claimed == {"request_id": pending["request_id"], "bot_id": 0, "telegram_id": 0,
                       "init_digest": "", "node_id": pending["node_id"], "audience": AUDIENCE}
    call(PREFIX + "claim", claim, 409)

    scope = dict(request["scopes"][0], visibility="private", key_id="lumi-key-v2", key_envelope="wrapped-key")
    grant = {"version": 1, "account_id": account, "grant_id": secrets.token_hex(16), "request_id": pending["request_id"],
             "app_id": "inbe", "client_id": client, "node_id": pending["node_id"], "audience": AUDIENCE,
             "signing_key": signing_key, "encryption_key": encryption_key, "issued_at": now,
             "not_before": now, "expires_at": now + 120, "nonce": secrets.token_hex(16),
             "bot_id": 0, "telegram_id": 0, "scopes": [scope]}

    def approve(value, status):
        fields = ("account_id", "grant_id", "request_id", "app_id", "client_id", "node_id", "audience", "signing_key",
                  "encryption_key", "issued_at", "not_before", "expires_at", "nonce", "bot_id", "telegram_id")
        message = lines("daochi-authorization-v1", 1, *(value[k] for k in fields), len(value["scopes"]))
        for item in value["scopes"]:
            message += lines(item["collection"], item["visibility"], "read" if item["read"] else "",
                             "write" if item["write"] else "", item["key_id"], digest(item["key_envelope"].encode()))
        value["signature"] = base64.b64decode(sign(owner, message, "sign")).hex()
        return call(PREFIX + "approve", value, status, {"Authorization": bearer})

    assert approve(dict(grant, scopes=[dict(scope, write=False)]), 403)["error"] == "approval scope mismatch"
    assert approve(grant, 200) is True
    call(PREFIX + "approve", dict(grant, signature="0" * 4840), 401, {"Authorization": bearer})
    assert call(PREFIX + "rendezvous", pending_id, 200)["grant_id"] == grant["grant_id"]

    grant_id = {"grant_id": grant["grant_id"]}

    def issue_session(check_replays=False):
        challenge = call(PREFIX + "challenge", grant_id, 200)
        message = lines("daochi-session-v1", grant["grant_id"], challenge["challenge"], challenge["node_id"],
                        challenge["audience"], challenge["expires_at"])
        issue = dict(grant_id, challenge=challenge["challenge"], signature=delegate_sign(message))
        if check_replays:
            call(PREFIX + "session", dict(issue, signature="0" * 128), 401)
        answer = call(PREFIX + "session", issue, 200)
        if check_replays:
            call(PREFIX + "session", issue, 409)
        assert answer["grant"] == grant and answer["owner_public_key"] == public.hex()
        assert answer["credential"]["expires_at"] == grant["expires_at"]
        return answer["credential"]

    credential = issue_session(check_replays=True)

    def proof(payload, nonce=None, query="", path=SYNC, *, method="POST", using=None):
        chosen = using if using is not None else credential
        value = {"version": 1, "grant_id": grant["grant_id"], "session_id": chosen["session_id"],
                 "account_id": account, "app_id": "inbe", "client_id": client, "node_id": grant["node_id"],
                 "audience": AUDIENCE, "challenge": chosen["challenge"], "method": method, "path": path,
                 "query": query, "body_sha256": digest(payload), "expires_at": int(time.time()) + 30,
                 "nonce": nonce or secrets.token_hex(16)}
        value["signature"] = delegate_sign(lines("daochi-delegate-request-v1", *value.values()))
        return {"Authorization": "Delegate ds1." + chosen["session_id"], "X-Mesh-Delegate": encode(value).decode()}

    record = {"collection": COLLECTION, "id": "lumi.message.1", "key_id": "lumi-key-v2",
              "nonce": "nonce", "ciphertext": "opaque-ciphertext", "updated_at": "2026-10-08T00:00:00Z"}
    sync = {"protocol_version": 6, "app_id": "inbe", "user_id_hash": account, "client_id": client}
    read = {"collection": COLLECTION, "limit": 1, "read": True, "since_version": 0, "sync": sync}
    other_scope = encode(dict(read, collection="private.inbe.v1.cells"))
    nonce = secrets.token_hex(16)
    call(SYNC, other_scope, 403, proof(other_scope, nonce=nonce))
    write = encode(dict(read, sync=dict(sync, encrypted_records=[record, dict(record, id="lumi.message.2")])))
    header = proof(write, nonce=nonce)
    result = call(SYNC, write, 200, header, compress=True)
    assert result["applied"] == 2 and result["truncated"] and len(result["records"]) == 1, result
    assert result["records"][0] == dict(record, updated_at="2026-10-08T00:00:00.000000000Z")
    call(SYNC, write, 409, header)

    next_page = encode(dict(read, since_version=result["next_version"]))
    alias = proof(next_page)
    alias["X-Daochi-Delegate"] = alias.pop("X-Mesh-Delegate")
    tail = call(SYNC, next_page, 200, alias)
    assert tail["applied"] == 0 and not tail["truncated"] and tail["records"][0]["id"] == "lumi.message.2", tail
    call(SYNC + "?extra=x", next_page, 401, proof(next_page, query="extra=x"))
    call(SYNC.replace("sync", "%73ync"), next_page, 401, proof(next_page))
    call(SYNC, next_page + b" ", 401, proof(next_page), compress=True)
    call(SYNC, next_page, 401, proof(next_page) | {"X-Mesh-User": "f" * 64})
    call(SYNC, encode(dict(read, sync=dict(sync, full_sync_requested=True))), 403)
    call(SYNC, dict(read, sync=dict(sync, full_sync_requested=True, encrypted_records=[{"id": 7}])), 400)
    call(SYNC, dict(read, sync=dict(sync, client_capabilities=[7])), 400)
    call(SYNC, dict(read, sync=dict(sync, client_capabilities=["content-free-deletions"])), 403)
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("UPDATE server_encrypted_records SET key_id='rotated-owner-key' WHERE id=?", (record["id"],))
    problem = call(SYNC, write, 403, proof(write))
    assert problem["error"] == "record key generation is outside grant"
    readable = encode(read)
    result = call(SYNC, readable, 200, proof(readable))
    assert [item["id"] for item in result["records"]] == ["lumi.message.2"]

    if check_socket is not None:
        credential = check_socket(port, database, call, proof, issue_session, credential, owner_call,
                                  grant_id, bearer, account, read, record)

    assert owner_call("revoke", dict(grant_id, app_id="inbe")) is True
    assert owner_call("revoke", dict(grant_id, app_id="inbe")) is True
    assert call(SYNC, readable, 401, proof(readable))["error"] == "authorization inactive on this node"
    call(PREFIX + "challenge", grant_id, 401)

    # Existing Telegram bindings remain subject to both Telegram verification
    # and the delegate's proof, with a shared replay reservation.
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS server_lumi_telegram (account_id TEXT PRIMARY KEY, telegram_id INTEGER NOT NULL UNIQUE, linked_at INTEGER NOT NULL)")
        db.execute("INSERT INTO server_lumi_telegram(account_id,telegram_id,linked_at) VALUES (?,?,?)", (account, 123456, now))
    telegram_request = dict(request, telegram=True)
    linked = owner_call("requests", telegram_request)
    assert linked["bot_id"] == 42 and linked["telegram_id"] == 123456
    init = {"auth_date": str(int(time.time())), "user": '{"id":123456,"first_name":"Fixture"}'}
    payload = "\n".join(f"{key}={value}" for key, value in sorted(init.items())).encode()
    tag = hmac.digest(hmac.digest(b"WebAppData", BOT_TOKEN.encode(), "sha256"), payload, "sha256").hex()
    bad_init = urlencode(dict(init, hash="0" * 64))
    call(PREFIX + "claim", claim_for(linked, bad_init), 401)
    init_data = urlencode(dict(init, hash=tag))
    replay_digest = digest(b"42\n" + payload)
    assert call(PREFIX + "claim", claim_for(linked, init_data, replay_digest), 200)["init_digest"] == replay_digest
    another = owner_call("requests", telegram_request)
    assert call(PREFIX + "claim", claim_for(another, init_data, replay_digest), 409)["error"] == "Telegram initData already claimed"


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
            verify(port, database, args.oracle)
    print("Native delegated authorization and sync loopback passed")


if __name__ == "__main__":
    main()
