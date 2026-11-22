"""Exercise native token reads and writes with a disposable server and database."""

import base64
import gzip
import hashlib
import json
import os
import select
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing, contextmanager
from http.client import HTTPConnection
from pathlib import Path

from check_http_listener import sign_with_oracle


TOKENS = "/api/v1/tokens"
ADMIN = "/api/v1/admin/tokens"
ASSET = "waozi:token"
CREDIT = ADMIN + "/manual-credit"
SPEND = TOKENS + "/spend"
CHECKPOINT = ADMIN + "/checkpoint"
APP = "inbe"
KEY = "native-token-test"
ADMIN_HEADERS = {"X-Mesh-Admin": KEY}
# Public test vector from RFC 8032, section 7.1.
SEED = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
PUBLIC = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
PRODUCTS = " z:300:+10,b:10,b:+11:9,invalid: 2,bad:4:1:2 "


@contextmanager
def server(binary, database, **settings):
    environment = {key: value for key, value in os.environ.items() if not key.startswith("MESH_")}
    environment.update(MESH_ADDR="127.0.0.1:0", MESH_DB=str(database), MESH_TOKEN_SECRET_HEX="00" * 32,
                       MESH_MAX_BODY_BYTES="4096", MESH_NODE_SYNC_INTERVAL_SECONDS="0", **settings)
    with subprocess.Popen([binary], env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        try:
            assert select.select([process.stdout], [], [], 15)[0], "server did not report a port"
            port = process.stdout.readline()
            assert port.strip().isdigit(), port
            with closing(HTTPConnection("127.0.0.1", int(port), timeout=15)) as connection, \
                    closing(sqlite3.connect(database, isolation_level=None)) as store:
                yield connection, store
        finally:
            process.terminate()
            try:
                _, errors = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise
            assert process.returncode == 0, errors


def request(connection, method, path, status=200, *, body=None, token=None, headers=None):
    headers = dict(headers or {})
    if token:
        headers["Authorization"] = "Bearer " + token
    if isinstance(body, dict):
        body = json.dumps(body, separators=(",", ":")).encode()
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    data = response.read()
    assert response.status == status, (method, path, response.status, data[:300], status)
    return response, json.loads(data) if data else None


def get(connection, route, status=200, **kwargs):
    return request(connection, "GET", TOKENS + route, status, **kwargs)[1]


def post(connection, path, status=200, **kwargs):
    return request(connection, "POST", path, status, **kwargs)[1]


def sign(oracle, keys, message, operation="sign"):
    return sign_with_oracle(oracle, operation, PrivateKey=keys["PrivateKey"],
                            Message=base64.b64encode(message).decode())["Signature"]


def login(connection, oracle):
    keys = sign_with_oracle(oracle, "keypair")
    account = hashlib.sha256(base64.b64decode(keys["PublicKey"])).hexdigest()
    path = "/api/v1/sync/login"
    _, challenge = request(connection, "GET", f"/api/v1/sync/challenge?user_id={account}")
    body = json.dumps({"client_id": "native-token-fixture", "public_key": keys["PublicKey"]}).encode()
    digest = hashlib.sha256(body).hexdigest()
    message = f"ksync-sync-v1\nPOST\n{path}\n{digest}\n{challenge['nonce']}\n"
    payload = post(connection, path, body=body, headers={"X-Ksync-User": account,
                   "X-Ksync-Signature": sign(oracle, keys, message.encode())})
    return account, payload["auth_token"], keys


def seed_receipt(database, account, sequence, amount, app="", source="admin", ref="seed"):
    receipt = f"{sequence:032x}"
    event_hash = f"{sequence:064x}"
    database.execute("INSERT INTO token_ledger (ledger_seq,receipt_id,issuer_id,asset_id,account_id,app_id,event_type,"
                     "amount_delta,source_type,source_ref,previous_hash,event_hash,signature,created_at) "
                     "VALUES (?,?,'waozi',?,?,?,'credit',?,?,?,'',?,'seed-fixture','2025-08-01T00:00:00Z')",
                     (sequence, receipt, ASSET, account, app, amount, source, ref, event_hash))
    return receipt


def check_reads(binary, oracle, root):
    pub_file = root / "issuer.hex"
    pub_file.write_text(PUBLIC + "\n")
    with server(binary, root / "reads.sqlite", MESH_TOKEN_PRODUCTS=PRODUCTS,
                MESH_TOKEN_DIRECT_PURCHASES_ENABLED="1", MESH_TOKEN_ISSUER_PUBLIC_KEY_HEX_FILE=str(pub_file)) as (c, db):
        assert get(c, "/issuer") == {"issuer_id": "waozi", "public_key": PUBLIC,
                                      "algorithm": "Ed25519", "status": "read_only"}
        assert get(c, "/products") == {"products": [{"product_id": "b", "token_units": 11, "monero_atomic_amount": 9},
                                                       {"product_id": "z", "token_units": 300, "monero_atomic_amount": 10}]}
        assert get(c, "/assets") == {"assets": [{"issuer_id": "waozi", "asset_id": ASSET, "display_name": "Dew",
                                                   "decimals": 6, "status": "active"}]}
        db.execute("INSERT INTO token_assets(issuer_id,asset_id,display_name,decimals,status) VALUES('aaa','example:unit','Test',0,'inactive')")
        assert [asset["asset_id"] for asset in get(c, "/assets")["assets"]] == ["example:unit", ASSET]
        assert request(c, "HEAD", TOKENS + "/assets")[1] is None
        assert request(c, "PUT", TOKENS + "/issuer", 405)[0].getheader("Allow") == "GET, HEAD"
        assert get(c, "/checkpoints/latest", 404) == {"error": "checkpoint not found"}
        assert get(c, "/receipts/short/invalid", 400) == {"error": "invalid receipt_id"}
        assert get(c, "/receipts/does-not-exist", 404) == {"error": "receipt not found"}
        for route in ("/balance", "/ledger", "/activity"):
            assert get(c, route, 401) == {"error": "bearer token required"}
        account, bearer, _ = login(c, oracle)
        other, other_bearer, _ = login(c, oracle)
        assert get(c, "/balance", token=bearer) == {"account_id": account, "asset_id": ASSET, "balance": 0}
        assert get(c, "/ledger", token=bearer) == {"events": None}
        assert get(c, "/activity", token=bearer) == {"account_id": account, "asset_id": ASSET, "balance": 0, "events": None}
        assert get(c, "/balance", 401, token=bearer, headers={"X-Mesh-User": other}) == {"error": "token user mismatch"}
        for route in ("/balance", "/ledger"):
            assert get(c, route + "?app_id=%2Finvalid", 400, token=bearer) == {"error": "invalid app_id"}
        for limit in ("0", "101", "x", "9223372036854775808"):
            assert get(c, "/ledger?limit=" + limit, 400, token=bearer) == {"error": "invalid ledger limit"}

        shared = seed_receipt(db, account, 1, 100, APP, "monero_account", "old-payment")
        scoped = seed_receipt(db, account, 3, 40, "wurm", "google_play", "title-payment")
        stranger = seed_receipt(db, other, 2, 999)
        db.execute("INSERT INTO token_allocations(receipt_id,account_id,asset_id,app_id,source_receipt_id,source_type,"
                   "provider_payment_id,issued,remaining,ledger_seq) VALUES(?,?,?,'wurm',?,'google_play','title-payment',40,40,3)",
                   (scoped, account, ASSET, scoped))
        db.execute("INSERT INTO monero_deposits(tx_id,account_index,address_index,account_id,amount_atomic,rate_atomic_amount,"
                   "rate_token_units,usd_cents,usd_updated_at,usd_sources,receipt_id) "
                   "VALUES('fixture-tx',0,0,?,505,100,2,123,42,'fixture-rate',?)", (account, shared))
        profile = get(c, "/balance", token=bearer)
        assert profile == {"account_id": account, "asset_id": ASSET, "balance": 140, "shared_balance": 100,
                           "scoped_balance": 40, "spendable_balance": 100}
        assert get(c, "/balance?app_id=+wurm+", token=bearer) == dict(profile, app_id="wurm", balance=40, spendable_balance=140)
        assert get(c, "/balance?app_id=inbe", token=bearer) == dict(profile, app_id="inbe", balance=100)
        assert [event["receipt_id"] for event in get(c, "/ledger?since=bad", token=bearer)["events"]] == [shared, scoped]
        assert get(c, "/ledger?since=9223372036854775808", token=bearer) == {"events": None}
        event, = get(c, "/ledger?since=1&limit=1&app_id=wurm", token=bearer)["events"]
        assert event["receipt_id"] == scoped and get(c, "/receipts/" + scoped) == event
        assert get(c, "/ledger", token=other_bearer)["events"][0]["receipt_id"] == stranger
        event, = get(c, "/activity?before_seq=3&limit=1", token=bearer)["events"]
        assert event == {"seq": 1, "receipt_id": shared, "app_id": APP, "event_type": "credit", "amount_delta": 100,
                         "source_type": "monero_account", "source_ref": "old-payment", "created_at": "2025-08-01T00:00:00Z",
                         "monero": {"tx_id": "fixture-tx", "amount_atomic": 505, "usd_cents": 123,
                                    "usd_rate_time": 42, "usd_sources": "fixture-rate"}}, event
        events = get(c, "/activity?before_seq=bad&limit=501", token=bearer)["events"]
        assert [event["seq"] for event in events] == [3, 1] and "monero" not in events[0]
        address = {"X-Forwarded-For": "192.0.2.51"}
        for _ in range(60):
            assert get(c, "/receipts/does-not-exist", 404, headers=address) == {"error": "receipt not found"}
        assert get(c, "/receipts/does-not-exist", 429, headers=address) == {"error": "too many receipt requests"}
    with server(binary, root / "disabled.sqlite", MESH_TOKEN_PRODUCTS=PRODUCTS) as (c, _):
        assert get(c, "/issuer") == {"issuer_id": "waozi", "public_key": "", "algorithm": "Ed25519", "status": "disabled"}
        assert get(c, "/products") == {"products": [{"product_id": "b", "token_units": 11},
                                                       {"product_id": "z", "token_units": 300}]}


def go_json(value):
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            .replace("<", r"\u003c").replace(">", r"\u003e").replace("&", r"\u0026")
            .replace("\u2028", r"\u2028").replace("\u2029", r"\u2029")).encode()


def verify_signature(root, message, signature):
    public, data, proof = (root / name for name in ("issuer.der", "message", "signature"))
    public.write_bytes(bytes.fromhex("302a300506032b6570032100" + PUBLIC))
    data.write_bytes(message)
    proof.write_bytes(bytes.fromhex(signature))
    subprocess.run(["openssl", "pkeyutl", "-verify", "-rawin", "-pubin", "-keyform", "DER", "-inkey", str(public),
                    "-in", str(data), "-sigfile", str(proof)], capture_output=True, check=True, timeout=10)


def check_receipt(root, receipt, sequence, previous):
    order = ["receipt_id", "issuer_id", "asset_id", "account_id", "app_id", "event_type", "amount_delta", "ledger_seq",
             "previous_hash", "event_hash", "created_at", "source_type", "source_ref"]
    payload = {key: receipt[key] for key in order if key in receipt}
    signature = receipt["signature"]
    assert receipt["ledger_seq"] == sequence and receipt["previous_hash"] == previous, receipt
    assert len(bytes.fromhex(receipt["receipt_id"])) == 16
    assert receipt["created_at"].endswith("Z") and "." not in receipt["created_at"]
    prefix = b"ksync-token-receipt-v1\n"
    assert hashlib.sha256(prefix + go_json(dict(payload, event_hash=""))).hexdigest() == receipt["event_hash"], receipt
    verify_signature(root, prefix + go_json(payload), signature)


def check_checkpoint(root, checkpoint, sequence, hashes):
    ledger_root = hashlib.sha256("".join(digest + "\n" for digest in hashes).encode()).hexdigest()
    assert checkpoint["ledger_seq"] == sequence and checkpoint["ledger_root"] == ledger_root, checkpoint
    assert checkpoint["issuer_id"] == "waozi" and checkpoint["asset_id"] == ASSET
    verify_signature(root, f"ksync-token-checkpoint-v1\nwaozi\n{ASSET}\n{sequence}\n{ledger_root}\n".encode(), checkpoint["signature"])


def credit(connection, account, units, ref, *, status=200, app=APP):
    return post(connection, CREDIT, status, body={"account_id": account, "amount": units, "source_ref": ref,
                                                  "app_id": app}, headers=ADMIN_HEADERS)


def spend_body(key, amount, app=APP, **settings):
    return dict(app_id=app, amount=amount, action="feature_unlock", idempotency_key=key, **settings)


def tables_snapshot(database):
    return [database.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() for table in (
        "token_ledger", "token_processed_payments", "token_allocations", "token_allocation_receipts",
        "token_allocation_debits", "token_spend_nonces", "token_checkpoints", "server_signed_transactions", "server_device_keys")]


def enroll_device(connection, oracle, account, bearer, keys):
    device = sign_with_oracle(oracle, "device-keypair")
    key_id, client_id, nonce = "token-fixture-device", "token-fixture-client", "token-device-registration"
    expires = int(time.time()) + 120
    public_key = base64.b64decode(device["PublicKey"]).hex()
    message = "\n".join(("daochi-device-registration-v1", account, APP, key_id, client_id, public_key, nonce, str(expires), ""))
    registration = dict(app_id=APP, device_key_id=key_id, client_id=client_id, public_key=public_key, nonce=nonce,
                        expires_at=expires, signature=sign(oracle, keys, message.encode()))
    post(connection, "/api/v1/account/devices", body=registration, token=bearer)
    return key_id, device


def envelope(oracle, account, keys, device, sequence, body):
    key_id, device_keys = device
    expiry = int(time.time()) + 120
    digest = hashlib.sha256(body).hexdigest()
    tx_id, nonce = f"token-tx-{sequence}", f"token-nonce-{sequence}"
    message = "\n".join(("daochi-tx-v1", "6", tx_id, account, APP, key_id, "POST", SPEND, digest, nonce, str(expiry), ""))
    return {"X-Mesh-Tx": json.dumps(dict(protocol_version=6, tx_id=tx_id, account_id=account, app_id=APP,
                                        device_key_id=key_id, method="POST", path=SPEND, body_sha256=digest,
                                        nonce=nonce, expires_at=expiry, signature=sign(oracle, keys, message.encode()),
                                        device_signature=sign(oracle, device_keys, message.encode(), "device-sign")),
                                    separators=(",", ":"))}


def check_writes(binary, oracle, root):
    with server(binary, root / "unsigned.sqlite", MESH_TOKEN_ISSUER_PUBLIC_KEY_HEX=PUBLIC,
                MESH_ADMIN_TOKEN=KEY) as (c, _):
        account, bearer, _ = login(c, oracle)
        assert credit(c, account, 2, "read-only", status=503) == {"error": "token issuer unavailable"}
        assert post(c, SPEND, 503, body=spend_body("read-only", 1), token=bearer) == {"error": "token issuer unavailable"}
        assert post(c, CHECKPOINT, 503, headers=ADMIN_HEADERS) == {"error": "token issuer unavailable"}
    with server(binary, root / "no-admin.sqlite", MESH_TOKEN_ISSUER_PRIVATE_KEY_HEX=SEED) as (c, _):
        assert post(c, CREDIT, 403, headers=ADMIN_HEADERS) == {"error": "admin disabled"}
        assert post(c, CHECKPOINT, 403) == {"error": "admin disabled"}

    with server(binary, root / "writes.sqlite", MESH_TOKEN_ISSUER_PRIVATE_KEY_HEX=SEED, MESH_ADMIN_TOKEN=KEY) as (c, db):
        assert get(c, "/issuer")["public_key"] == PUBLIC and get(c, "/issuer")["status"] == "ok"
        account, bearer, keys = login(c, oracle)
        assert post(c, CREDIT, 401) == {"error": "admin token required"}
        assert post(c, SPEND, 401) == {"error": "bearer token required"}
        assert post(c, CHECKPOINT, 401) == {"error": "admin token required"}
        checkpoint = post(c, CHECKPOINT, headers=ADMIN_HEADERS)
        check_checkpoint(root, checkpoint, 0, [])
        assert get(c, "/checkpoints/latest") == checkpoint
        for payload, error in ((b"{", "invalid json"), (b" " * 4097, "request body too large"),
                               ({"account_id": account, "amount": "4"}, "invalid json"),
                               ({"account_id": "bad", "amount": 4}, "invalid account_id"),
                               ({"account_id": account, "amount": -1}, "credit amount must be positive")):
            assert post(c, CREDIT, 400, body=payload, headers=ADMIN_HEADERS) == {"error": error}
        ref = "fixture <>&\u2028é\\"
        initial = credit(c, " " + account.upper() + " ", 100, " " + ref + " ")
        assert initial["status"] == "ok" and initial["balance"] == 100
        first = initial["receipt"]
        assert first["source_ref"] == ref and first["account_id"] == account
        check_receipt(root, first, 1, "")
        assert credit(c, account, 100, ref, app="other") == initial
        assert credit(c, account, 99, ref, status=400) == {"error": "provider payment id collision"}
        before = tables_snapshot(db)
        db.execute("CREATE TRIGGER fail_credit AFTER INSERT ON token_processed_payments BEGIN SELECT RAISE(FAIL,'failed credit'); END")
        assert post(c, CREDIT, 400, body={"account_id": account, "amount": 10, "source_ref": "rollback"}, headers=ADMIN_HEADERS)
        assert tables_snapshot(db) == before
        db.execute("DROP TRIGGER fail_credit")
        second = credit(c, account, 10, "rollback", app="")["receipt"]
        check_receipt(root, second, 2, first["event_hash"])
        assert "app_id" not in second
        missing = credit(c, account, 1, "", app="")["receipt"]
        assert missing["source_ref"].startswith("manual:") and missing["source_ref"].endswith("Z")
        check_receipt(root, missing, 3, second["event_hash"])

        assert post(c, SPEND, 400, body=spend_body("invalid-app", 1, "unknown"), token=bearer) == {"error": "unknown app_id"}
        assert post(c, SPEND, 400, body=spend_body("invalid-asset", 1, asset_id="invalid"), token=bearer) == {"error": "unsupported asset_id"}
        assert post(c, SPEND, 409, body=spend_body("too-large", 112), token=bearer) == {"error": "insufficient balance"}
        db.execute("UPDATE token_app_permissions SET legacy_unsigned_until=? WHERE app_id=? AND permission='spend'",
                   (int(time.time()) + 300, APP))
        purchase = spend_body("purchase-fixture", 105, metadata=" <>& ")
        output = post(c, SPEND, body=purchase, token=bearer)
        debit = output["receipt"]
        assert output["balance"] == 6 and debit["amount_delta"] == -105
        check_receipt(root, debit, 4, missing["event_hash"])
        assert post(c, SPEND, body=purchase, token=bearer) == output
        assert post(c, SPEND, 409, body=dict(purchase, amount=1), token=bearer) == {"error": "idempotency key reused for different spend"}
        assert get(c, "/receipts/" + debit["receipt_id"]) == debit
        terms = {"AccountID": account, "AppID": APP, "EventType": "debit", "AmountDelta": -105,
                 "SourceType": "spend", "SourceRef": debit["source_ref"]}
        assert db.execute("SELECT request_hash FROM token_spend_nonces WHERE receipt_id=?", (debit["receipt_id"],)).fetchone()[0] == \
            hashlib.sha256(go_json(terms)).hexdigest()
        audit = db.execute("SELECT credit_receipt_id,units FROM token_allocation_debits WHERE debit_receipt_id=? ORDER BY rowid",
                           (debit["receipt_id"],)).fetchall()
        assert audit == [(first["receipt_id"], 100), (second["receipt_id"], 5)], audit

        db.execute("UPDATE token_app_permissions SET status='disabled',legacy_unsigned_until=0 WHERE app_id=? AND permission='spend'", (APP,))
        signed_body = json.dumps(spend_body("signed-fixture", 2), separators=(",", ":")).encode()
        assert post(c, SPEND, 403, token=bearer, body=signed_body) == {"error": "app token permission denied"}
        db.execute("UPDATE token_app_permissions SET status='active' WHERE app_id=? AND permission='spend'", (APP,))
        assert post(c, SPEND, 401, token=bearer, body=signed_body) == {"error": "signed transaction required"}
        device = enroll_device(c, oracle, account, bearer, keys)
        header = envelope(oracle, account, keys, device, "first", signed_body)
        assert post(c, SPEND, 401, token=bearer, body=signed_body + b" ", headers=header) == {"error": "signed transaction body mismatch"}
        before = tables_snapshot(db)
        db.execute("CREATE TRIGGER fail_spend AFTER INSERT ON token_spend_nonces BEGIN SELECT RAISE(FAIL,'failed spend'); END")
        assert post(c, SPEND, 500, token=bearer, body=signed_body, headers=header) == {"error": "token spend failed"}
        assert tables_snapshot(db) == before
        db.execute("DROP TRIGGER fail_spend")
        signed = post(c, SPEND, token=bearer, body=gzip.compress(signed_body), headers=header | {"Content-Encoding": "gzip"})
        assert signed["balance"] == 4
        check_receipt(root, signed["receipt"], 5, debit["event_hash"])
        assert post(c, SPEND, 409, token=bearer, body=signed_body, headers=header) == {"error": "signed transaction replay"}
        renewed = envelope(oracle, account, keys, device, "new", signed_body)
        assert post(c, SPEND, token=bearer, body=signed_body, headers=renewed) == signed

        hashes = [row[0] for row in db.execute("SELECT event_hash FROM token_ledger ORDER BY ledger_seq")]
        before = tables_snapshot(db)
        db.execute("CREATE TRIGGER fail_checkpoint AFTER INSERT ON token_checkpoints BEGIN SELECT RAISE(FAIL,'failed checkpoint'); END")
        assert post(c, CHECKPOINT, 500, headers=ADMIN_HEADERS) == {"error": "token checkpoint failed"}
        assert tables_snapshot(db) == before
        db.execute("DROP TRIGGER fail_checkpoint")
        checkpoint = post(c, CHECKPOINT, headers=ADMIN_HEADERS)
        check_checkpoint(root, checkpoint, 5, hashes)
        assert get(c, "/checkpoints/latest") == checkpoint


def check_existing_ledger(binary, oracle, root):
    with server(binary, root / "historical.sqlite", MESH_TOKEN_ISSUER_PRIVATE_KEY_HEX=SEED + PUBLIC,
                MESH_ADMIN_TOKEN=KEY) as (c, db):
        account, bearer, _ = login(c, oracle)
        db.execute("INSERT OR IGNORE INTO server_apps(app_id,display_name) VALUES('wurm','Wurm')")
        legacy = seed_receipt(db, account, 1, -10000000, "entity", "spend", "wurm.mint:skin-wurm-mint-v2")
        db.execute("UPDATE token_ledger SET event_type='debit' WHERE receipt_id=?", (legacy,))
        db.execute("INSERT INTO token_spend_nonces(account_id,app_id,idempotency_key,receipt_id) "
                   "VALUES(?,'entity','skin-wurm-mint-v2',?)", (account, legacy))
        shared = seed_receipt(db, account, 2, 10000005)
        scoped = seed_receipt(db, account, 3, 30, "wurm", "google_play", "historical-title-credit")
        db.execute("UPDATE token_allocation_migration SET historical_ledger_seq=2 WHERE version=1")
        db.execute("INSERT INTO token_allocations(receipt_id,account_id,asset_id,app_id,source_receipt_id,source_type,"
                   "provider_payment_id,issued,remaining,ledger_seq) "
                   "VALUES(?,?,?,'wurm',?,'google_play','historical-title-credit',30,30,3)", (scoped, account, ASSET, scoped))
        snapshot = tables_snapshot(db)
        shop = dict(app_id="wurm", action="wurm.mint", amount=10000000, idempotency_key="skin-wurm-mint-v2")
        assert post(c, SPEND, token=bearer, body=shop) == {"status": "ok", "balance": 35,
                                                        "receipt": get(c, "/receipts/" + legacy)}
        assert tables_snapshot(db) == snapshot
        assert post(c, SPEND, 400, token=bearer, body=dict(shop, amount=1)) == {"error": "shop product title, price or key mismatch"}
        db.execute("UPDATE token_app_permissions SET legacy_unsigned_until=? WHERE app_id=? AND permission='spend'",
                   (int(time.time()) + 300, APP))
        assert post(c, SPEND, 409, token=bearer, body=spend_body("restricted-fixture", 6)) == {"error": "insufficient balance"}
        applied = post(c, SPEND, token=bearer, body=spend_body("restricted-fixture", 32, app="wurm"))
        receipt = applied["receipt"]
        assert applied["balance"] == 3
        check_receipt(root, receipt, 4, f"{3:064x}")
        assert db.execute("SELECT credit_receipt_id,units FROM token_allocation_debits WHERE debit_receipt_id=? ORDER BY rowid",
                          (receipt["receipt_id"],)).fetchall() == [(scoped, 30), ("", 2)]
        assert get(c, "/receipts/" + shared)["amount_delta"] == 10000005

        raw = (b'{"account_id":"' + account.encode() +
               b'","amount":2,"source_ref":"raw \xff \xe2\x82 \\ud800 \\udc00 \\ud800\\udc00"}')
        normalized = post(c, CREDIT, body=raw, headers=ADMIN_HEADERS)
        source = "raw � �� � � 𐀀"
        assert normalized["receipt"]["source_ref"] == source and normalized["balance"] == 5
        check_receipt(root, normalized["receipt"], 5, receipt["event_hash"])
        assert credit(c, account, 2, source, app="") == normalized


def main(binary, oracle, *arguments):
    assert arguments in ((), ("--reads",)), arguments
    binary, oracle = str(Path(binary).resolve()), str(Path(oracle).resolve())
    with tempfile.TemporaryDirectory(prefix="token-http-", dir=Path(binary).parent) as temporary:
        root = Path(temporary)
        check_reads(binary, oracle, root)
        print("Native token read routes passed")
        if not arguments:
            check_writes(binary, oracle, root)
            check_existing_ledger(binary, oracle, root)
            print("Native token credit, spend and checkpoint routes passed")


if __name__ == "__main__":
    main(*sys.argv[1:])
