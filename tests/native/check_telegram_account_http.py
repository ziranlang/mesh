#!/usr/bin/env python3
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hmac
from http.client import HTTPConnection
from pathlib import Path
import secrets
import sqlite3
from tempfile import TemporaryDirectory
import time
from urllib.parse import urlencode

from check_delegated_http import digest, encode, lines
from check_feedback_http import node, request, send
from check_http_listener import make_bearer, sign_with_oracle
from check_lumi_telegram_http import BOT, CANVAS, PRIVATE, SECRET, bot_calls, fake_provider, message, webhook


ROOT = "/api/v1/telegram/"
ENTRY = ROOT + "entry"
BIND = ROOT + "account/bind"
AUTHORIZATION = "/api/v1/authorization/"
AUDIENCE = "http://127.0.0.1"
NODE = "a" * 64
BOT_ID = int(BOT.split(":")[0])
INFO_KEYS = ("entry_id", "mode", "app_id", "node_id", "audience", "bot_id", "challenge", "expires_at")
SCOPE = {"app_id": "inbe", "telegram": True,
         "scopes": [{"collection": "private.inbe.v2.lumi", "read": True}]}


def signed_init(sender=PRIVATE, auth_date=None, **extra):
    auth_date = int(time.time()) if auth_date is None else auth_date
    fields = {"auth_date": str(auth_date), "user": encode({"id": sender}).decode(), "query_id": secrets.token_hex(8), **extra}
    canonical = "\n".join(f"{key}={value}" for key, value in sorted(fields.items())).encode()
    key = hmac.digest(b"WebAppData", BOT.encode(), "sha256")
    fields["hash"] = hmac.digest(key, canonical, "sha256").hex()
    return urlencode(fields), digest(f"{BOT_ID}\n".encode() + canonical)


class Keys:
    def __init__(self, oracle, database):
        self.oracle = oracle
        self.delegate = sign_with_oracle(oracle, "device-keypair")
        self.client = secrets.token_hex(32)
        self.signing = base64.b64decode(self.delegate["PublicKey"]).hex()
        self.encryption = "e" * 2368
        self.owner = self.make_owner(database)
        self.other = self.make_owner(database)

    def sign(self, key, data, operation):
        return sign_with_oracle(self.oracle, operation, PrivateKey=key["PrivateKey"],
                                Message=base64.b64encode(data).decode())["Signature"]

    def delegate_sign(self, data):
        return base64.b64decode(self.sign(self.delegate, data, "device-sign")).hex()

    def make_owner(self, database):
        key = sign_with_oracle(self.oracle, "keypair")
        device = sign_with_oracle(self.oracle, "device-keypair")
        public = base64.b64decode(key["PublicKey"])
        account = digest(public)
        session = secrets.token_hex(32)
        now = int(time.time())
        bearer = make_bearer(lines("v2", account, now + 3600, now, session))
        with closing(sqlite3.connect(database)) as db, db:
            db.execute("INSERT INTO server_users(user_id_hash,public_key) VALUES (?,?)", (account, public))
            db.execute("INSERT INTO server_auth_sessions(token_hash,user_id_hash,session_id,issued_at,expires_at,created_at) "
                       "VALUES (?,?,?,?,?,CURRENT_TIMESTAMP)",
                       (digest(bearer.removeprefix("Bearer ").encode()), account, session, now, now + 3600))
            db.execute("INSERT INTO server_device_keys(account_id,app_id,device_key_id,client_id,public_key) VALUES (?,?,?,?,?)",
                       (account, "inbe", "owner-device", "owner-client", base64.b64decode(device["PublicKey"]).hex()))
        return account, bearer, key, device

    def verify(self, info, init):
        data, claim = init
        values = [info[name] for name in INFO_KEYS]
        proof = self.delegate_sign(lines("daochi-telegram-entry-verify-v1", *values, self.client, self.signing,
                                         self.encryption, claim))
        return {"entry_id": info["entry_id"], "client_id": self.client, "signing_key": self.signing,
                "encryption_key": self.encryption, "init_data": data, "proof": proof}

    def action(self, info, verb, *, account="", init_digest="", expiry=None, nonce=None):
        expiry = int(time.time()) + 50 if expiry is None else expiry
        nonce = secrets.token_hex(16) if nonce is None else nonce
        fields = [info[name] for name in INFO_KEYS]
        data = lines("daochi-telegram-entry-action-v1", verb, *fields, self.client, self.signing,
                     self.encryption, expiry, nonce)
        if verb == "bind":
            data += lines("", account, init_digest)
        return {"entry_id": info["entry_id"], "client_id": self.client, "signing_key": self.signing,
                "encryption_key": self.encryption, "expires_at": expiry, "nonce": nonce, "proof": self.delegate_sign(data)}

    def owner_request(self, port, route, body, expected=200, *, owner=None, version=6, include_device=True, headers=None):
        account, bearer, key, device = self.owner if owner is None else owner
        payload = encode(body)
        nonce, identifier = secrets.token_hex(16), secrets.token_hex(16)
        expires = int(time.time()) + 60
        transaction = {"protocol_version": version, "tx_id": identifier, "account_id": account, "app_id": "inbe",
                       "device_key_id": "owner-device", "method": "POST", "path": route, "body_sha256": digest(payload),
                       "nonce": nonce, "expires_at": expires}
        message = lines("daochi-tx-v1", version, identifier, account, "inbe", "owner-device", "POST", route,
                        transaction["body_sha256"], nonce, expires)
        transaction["signature"] = self.sign(key, message, "sign")
        if include_device:
            transaction["device_signature"] = self.sign(device, message, "device-sign")
        tx = base64.urlsafe_b64encode(encode(transaction)).decode().rstrip("=")
        return request(port, "POST", route, body=payload, expected=expected,
                       headers={"Authorization": bearer, "X-Mesh-Tx": tx, **(headers or {})})


def post(port, route, body, expected=200):
    return request(port, "POST", route, body=body, expected=expected)


def public_info(port, entry_id):
    result = post(port, ENTRY, {"entry_id": entry_id})
    assert set(result) == set(INFO_KEYS), result
    assert result["expires_at"] - int(time.time()) in range(590, 601), result
    return result


def entries_from_database(database, environment):
    now = int(time.time())
    ids = [secrets.token_hex(16) for _ in range(3)]
    with closing(sqlite3.connect(database)) as db, db:
        for mode, entry_id in zip(("create", "restore", "connect"), ids):
            db.execute("INSERT INTO server_telegram_account_entries "
                       "(entry_id,mode,app_id,node_id,audience,bot_id,telegram_id,challenge,expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
                       (entry_id, mode, "inbe", environment["node_id"], AUDIENCE, BOT_ID, PRIVATE,
                        secrets.token_hex(16), now + 600))
    return ids


def menu_urls(calls):
    sent = bot_calls(calls, "sendMessage")[-1]
    assert sent["chat_id"] == PRIVATE
    assert "private owner key stays there" in sent["text"]
    keyboard = sent["reply_markup"]["inline_keyboard"]
    assert [row[0]["text"] for row in keyboard] == ["Create account", "Restore account", "Connect existing account"]
    prefix = CANVAS + "?r="
    urls = [row[0]["web_app"]["url"] for row in keyboard]
    assert all(url.startswith(prefix) and len(url[len(prefix):]) == 32 for url in urls), urls
    return [url[len(prefix):] for url in urls]


def check_menu(port, database, calls):
    start = {"update_id": 6001, "message": message(120, "/start")["message"]}
    before = len(calls)
    webhook(port, start, headers={}, expected=401)
    assert len(calls) == before
    webhook(port, {"update_id": 6002, "message": message(121, "/start", bot=True)["message"]})
    webhook(port, {"update_id": 6003, "message": message(122, "/start", chat=PRIVATE+1)["message"]})
    assert len(calls) == before
    webhook(port, start)
    ids = menu_urls(calls)
    assert len(set(ids)) == 3
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("SELECT DISTINCT telegram_id FROM server_telegram_account_entries").fetchall() == [(PRIVATE,)]
    return ids


def check_proofs(port, database, keys, infos):
    created, restored, connected = infos
    expired = signed_init(auth_date=int(time.time()) - 301)
    future = signed_init(auth_date=int(time.time()) + 60)
    good_date = int(time.time()) - 12
    valid = signed_init(auth_date=good_date)
    for invalid in (signed_init(PRIVATE+1), expired, future, (valid[0] + "&hash=" + "a" * 64, valid[1]),
                    (valid[0] + "&extra=1", valid[1])):
        assert post(port, ENTRY + "/verify", keys.verify(created, invalid), 401)["error"] == "Telegram sender and delegate proof required"
    assert post(port, ENTRY + "/verify", dict(keys.verify(created, valid), proof="0" * 128), 401)["error"] == "Telegram sender and delegate proof required"
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("SELECT COUNT(*) FROM server_telegram_init_replays").fetchone()[0] == 0
    # Concurrent verifications using equivalent URL encodings consume the digest once.
    raw, claim = valid
    reordered = ("&".join(reversed(raw.split("&"))), claim)
    inputs = (keys.verify(created, valid), keys.verify(restored, reordered))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda body: send(port, "POST", ENTRY + "/verify", body=body), inputs))
    assert sorted(status for status, _ in results) == [200, 409], results
    accepted = next(body for status, body in results if status == 200)
    assert accepted["status"] == "verified" and accepted["account_id"] == ""
    assert accepted["init_digest"] == claim and accepted["telegram_id"] == PRIVATE
    assert next(body for status, body in results if status == 409)["error"] == "Telegram initData already claimed"
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("SELECT expires_at FROM server_telegram_init_replays WHERE digest=?", (claim,)).fetchone() == (good_date + 300,)
    assert post(port, ENTRY + "/verify", keys.verify(accepted["info"], signed_init()), 409)["error"] == "Telegram entry expired or already verified"
    assert post(port, ENTRY + "/verify", keys.verify(connected, signed_init()))["status"] == "verified"
    return accepted["info"], claim


def check_lifecycle(port, database, keys, active, claim, connected, calls):
    account, bearer, _, _ = keys.owner
    valid = keys.action(active, "status")
    assert post(port, ENTRY + "/status", valid)["status"] == "verified"
    assert post(port, ENTRY + "/status", valid, 409)["error"] == "Telegram entry proof already used"
    stale = keys.action(active, "status", expiry=int(time.time()) - 1)
    assert post(port, ENTRY + "/status", stale, 401)["error"] == "verified delegate proof required"
    future = keys.action(active, "status", expiry=int(time.time()) + 90)
    assert post(port, ENTRY + "/status", future, 401)["error"] == "verified delegate proof required"
    mismatched = dict(keys.action(active, "status"), client_id="9" * 64)
    assert post(port, ENTRY + "/status", mismatched, 401)["error"] == "verified delegate proof required"
    assert post(port, ENTRY + "/rendezvous", keys.action(connected, "request"), 202) is True

    bind = keys.action(active, "bind", account=account, init_digest=claim)
    assert post(port, BIND, bind, 401)["error"] == "bearer token required"
    missing = request(port, "POST", BIND, body=bind, expected=401, headers={"Authorization": bearer})
    assert missing["error"] == "signed transaction required"
    for version in (5, 7):
        rejected = keys.owner_request(port, BIND, bind, 400, version=version)
        assert rejected["error"] == "owner protocol v6 required"
    keys.owner_request(port, BIND, bind, 400, include_device=False)
    assert keys.owner_request(port, BIND, bind, 401, owner=keys.other)["error"] == "verified delegate binding proof required"
    # Inject a mid-transaction failure and retry the same delegate nonce with a fresh owner proof.
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("CREATE TRIGGER fixture_reject_binding BEFORE UPDATE OF bound_at ON server_telegram_account_entries "
                   "BEGIN SELECT RAISE(ABORT,'fixture binding write failure'); END")
    keys.owner_request(port, BIND, bind, 503)
    with closing(sqlite3.connect(database)) as db, db:
        assert db.execute("SELECT COUNT(*) FROM server_lumi_telegram").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM server_telegram_entry_nonces WHERE nonce=?", (bind["nonce"],)).fetchone()[0] == 0
        db.execute("DROP TRIGGER fixture_reject_binding")
    bound = keys.owner_request(port, BIND, bind)
    assert bound["status"] == "bound" and bound["account_id"] == account
    assert keys.owner_request(port, BIND, bind, 409)["error"] == "Telegram entry expired or already bound"

    pending = keys.owner_request(port, AUTHORIZATION + "requests", SCOPE)
    assert pending["telegram_id"] == PRIVATE and pending["bot_id"] == BOT_ID
    if calls is not None:
        start = "/start auth_" + pending["request_id"]
        webhook(port, message(6004, start, sender=PRIVATE+1, chat=PRIVATE+1))
        wrong_sender = bot_calls(calls, "sendMessage")[-1]
        assert wrong_sender["chat_id"] == PRIVATE+1 and "unavailable or expired" in wrong_sender["text"]
        assert "reply_markup" not in wrong_sender
        webhook(port, message(6005, start))
        assert menu_urls(calls)[-1] == pending["request_id"]

    with closing(sqlite3.connect(database)) as db, db:
        db.execute("UPDATE server_authorization_requests SET audience='https://other.example' WHERE request_id=?", (pending["request_id"],))
    discover = keys.action(connected, "request")
    result = post(port, ENTRY + "/rendezvous", discover, 409)
    assert result["error"] == "Owner request belongs to another node"
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("UPDATE server_authorization_requests SET audience=? WHERE request_id=?", (AUDIENCE, pending["request_id"]))
        assert db.execute("SELECT account_id FROM server_telegram_account_entries WHERE entry_id=?",
                          (connected["entry_id"],)).fetchone() == ("",)
    visible = post(port, ENTRY + "/rendezvous", discover)
    assert visible == {name: pending[name] for name in ("request_id", "grant_id", "app_id", "node_id", "audience",
                                                       "expires_at", "challenge", "status")}
    init_digest = post(port, ENTRY + "/status", keys.action(connected, "status"))["init_digest"]
    assert post(port, ENTRY + "/rendezvous", discover, 409)["error"] == "Telegram entry proof already used"

    payload = {"request_id": pending["request_id"], "entry_id": connected["entry_id"], "client_id": keys.client,
               "signing_key": keys.signing, "encryption_key": keys.encryption, "proof": "0" * 128}
    assert post(port, AUTHORIZATION + "claim", payload, 401)["error"] == "delegate claim proof rejected"
    assert post(port, ENTRY + "/status", keys.action(connected, "status"))["status"] == "verified"
    claim_proof = lines("daochi-rendezvous-claim-v1", pending["request_id"], "inbe", pending["node_id"],
                        pending["audience"], pending["challenge"], pending["expires_at"], keys.client, keys.signing,
                        keys.encryption, init_digest)
    claimed = post(port, AUTHORIZATION + "claim", dict(payload, proof=keys.delegate_sign(claim_proof)))
    assert claimed["telegram_id"] == PRIVATE and claimed["init_digest"] == init_digest
    assert post(port, ENTRY + "/status", keys.action(connected, "status"))["status"] == "consumed"
    assert post(port, ENTRY + "/cancel", keys.action(connected, "cancel"), 409)["error"] == "Telegram entry already finished"
    assert post(port, ENTRY + "/cancel", keys.action(active, "cancel")) is True
    assert post(port, ENTRY + "/status", keys.action(active, "status"))["status"] == "cancelled"
    assert post(port, ENTRY, {"entry_id": active["entry_id"]}, 404)["error"] == "Telegram entry unavailable"
    assert post(port, ENTRY + "/rendezvous", keys.action(active, "request"), 401)["error"] == "verified delegate proof required"


def exercise(port, database, oracle, calls):
    env = request(port, "GET", ROOT + "environment")
    assert env == {"app_id": "inbe", "node_id": env["node_id"], "audience": AUDIENCE,
                   "bot_id": BOT_ID, "environment": "production"}
    assert len(env["node_id"]) == 64
    if calls is not None:
        assert env["node_id"] == NODE, "provider tests require the isolated fixture"
    assert request(port, "HEAD", ROOT + "environment") is None
    with closing(HTTPConnection("127.0.0.1", port, timeout=5)) as connection:
        connection.request("GET", ENTRY)
        reply = connection.getresponse()
        assert (reply.status, reply.getheader("Allow"), reply.read()) == (405, "POST", b"Method Not Allowed\n")
    assert post(port, ENTRY, {"unknown": "x"}, 400)["error"] == "invalid Telegram entry"
    duplicate = b'{"entry_id":"' + b"1" * 32 + b'","entry_id":"' + b"2" * 32 + b'"}'
    assert post(port, ENTRY, duplicate, 400)["error"] == "invalid Telegram entry"
    assert post(port, ENTRY, b" " * 1025, 400)["error"] == "invalid Telegram entry"
    assert post(port, ENTRY, {"entry_id": "a" * 32}, 404)["error"] == "Telegram entry unavailable"
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("INSERT OR IGNORE INTO server_apps(app_id,display_name) VALUES ('inbe','Inner Breeze')")
    keys = Keys(oracle, database)
    ids = check_menu(port, database, calls) if calls is not None else entries_from_database(database, env)
    infos = [public_info(port, entry_id) for entry_id in ids]
    assert [info["mode"] for info in infos] == ["create", "restore", "connect"]
    assert all(info["node_id"] == env["node_id"] and info["bot_id"] == BOT_ID for info in infos)
    active, claim = check_proofs(port, database, keys, infos)
    check_lifecycle(port, database, keys, active, claim, infos[-1], calls)
    unused = next(info for info in infos[:2] if info != active)
    identifier = {"entry_id": unused["entry_id"]}
    historical = signed_init()
    with closing(sqlite3.connect(database)) as db, db:
        request_id = db.execute("SELECT request_id FROM server_authorization_requests WHERE status='claimed'").fetchone()[0]
        db.execute("INSERT INTO server_telegram_init_claims(digest,request_id,expires_at) VALUES (?,?,?)",
                   (historical[1], request_id, int(time.time()) + 300))
        db.execute("UPDATE server_telegram_account_entries SET bot_id=bot_id+1 WHERE entry_id=?", (unused["entry_id"],))
    assert post(port, ENTRY, identifier, 404)["error"] == "Telegram entry unavailable"
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("UPDATE server_telegram_account_entries SET bot_id=? WHERE entry_id=?", (BOT_ID, unused["entry_id"]))
    assert post(port, ENTRY + "/verify", keys.verify(unused, historical), 409)["error"] == "Telegram initData already claimed"
    with closing(sqlite3.connect(database)) as db, db:
        assert db.execute("SELECT status FROM server_telegram_account_entries WHERE entry_id=?",
                          (unused["entry_id"],)).fetchone() == ("pending",)
        db.execute("UPDATE server_telegram_account_entries SET expires_at=0 WHERE entry_id=?", (unused["entry_id"],))
    expired = identifier
    assert post(port, ENTRY, expired, 404)["error"] == "Telegram entry unavailable"
    assert post(port, ENTRY + "/verify", keys.verify(unused, signed_init()), 409)["error"] == "Telegram entry expired or already verified"
    return active, keys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("binary", type=lambda value: str(Path(value).resolve()))
    parser.add_argument("oracle", type=lambda value: str(Path(value).resolve()))
    parser.add_argument("--server", action="store_true")
    options = parser.parse_args()
    with TemporaryDirectory(prefix="telegram-account-", dir=Path(options.binary).parent) as path:
        directory = Path(path)
        with node(options.binary, directory) as (port, _):
            assert request(port, "GET", ROOT + "environment", expected=503)["error"] == "Telegram environment unavailable"
        if options.server:
            # With no webhook secret the canonical server cannot dispatch to Telegram.
            with node(options.binary, directory, MESH_LUMI_BOT_TOKEN=BOT) as (port, database):
                exercise(port, database, options.oracle, None)
        else:
            with fake_provider() as (provider, calls):
                settings = {"MESH_LUMI_BOT_TOKEN": BOT, "MESH_LUMI_WEBHOOK_SECRET": SECRET,
                            "MESH_LUMI_CANVAS_URL": CANVAS, "MESH_LUMI_FIXTURE_TELEGRAM_PORT": str(provider),
                            "MESH_TELEGRAM_ACCOUNTS_ENABLED": "1"}
                with node(options.binary, directory, **settings) as (port, database):
                    active, keys = exercise(port, database, options.oracle, calls)
                    before = len(calls)
                unsafe = dict(settings, MESH_LUMI_CANVAS_URL="https://user@example.test/lumi")
                with node(options.binary, directory, **unsafe) as (port, database):
                    result = webhook(port, message(6200, "/history"), expected=503)
                    assert result["error"] == "Telegram account entry unavailable" and len(calls) == before
                    assert post(port, ENTRY + "/status", keys.action(active, "status"))["status"] == "cancelled"
                    with closing(sqlite3.connect(database)) as db:
                        assert db.execute("SELECT status FROM server_telegram_account_entries WHERE entry_id=?",
                                          (active["entry_id"],)).fetchone() == ("cancelled",)
    mode = "canonical entry routes" if options.server else "entry routes and authenticated fake-provider menu"
    print(f"Native Telegram account HTTP: {mode}, replay, binding, cancellation and owner rendezvous passed.")


if __name__ == "__main__":
    main()
