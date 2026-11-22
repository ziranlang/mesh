#!/usr/bin/env python3
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import gzip
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import sqlite3
from tempfile import TemporaryDirectory
from threading import Thread
import time

from check_delegated_http import digest, encode, lines
from check_feedback_http import node, request
from check_lumi_telegram_http import BASE as LUMI, BOT, PRIVATE, SECRET, WEBHOOK
from check_telegram_account_http import AUTHORIZATION, BOT_ID, Keys, signed_init


LAUNCH = "/api/v1/telegram/launch"
STARS = "/api/v1/tokens/purchases/telegram"
INVOICES = STARS + "/invoices"
CATALOG = STARS + "/catalog?app_id=inbe"
BALANCE = STARS + "/balance?app_id=inbe"
RECONCILE = "/api/v1/admin/tokens/purchases/telegram/reconcile"
ADMIN = {"X-Mesh-Admin": "fixture-operator-token"}
OFFER = {"product_id": "dew_10", "title": "10 Dew", "amount": 23, "units": 10_000_000}
LINK = "https://t.me/$fixture_purchase"


class Provider:
    def __init__(self):
        self.calls = []
        self.history = []
        self.fail_link = False
        self.fail_answer = False

    def call(self, operation, body):
        self.calls.append((operation, body))
        if operation == "createInvoiceLink":
            if self.fail_link:
                self.fail_link = False
                return {"ok": True, "result": "https://other.example/invoice"}
            return {"ok": True, "result": LINK}
        if operation == "answerPreCheckoutQuery":
            if self.fail_answer:
                self.fail_answer = False
                return {"ok": False}
            return {"ok": True, "result": True}
        if operation == "getStarTransactions":
            return {"ok": True, "result": {"transactions": self.history}}
        return {"ok": False}

    def requests(self, operation):
        return [body for method, body in self.calls if method == operation]


@contextmanager
def fake_provider():
    fake = Provider()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            prefix = "/bot" + BOT + "/"
            if not self.path.startswith(prefix):
                self.send_error(404)
                return
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            data = encode(fake.call(self.path.removeprefix(prefix), payload))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            yield server.server_port, fake
        finally:
            server.shutdown()
            thread.join()


def seed(database, keys):
    with closing(sqlite3.connect(database)) as db, db:
        for app in ("inbe", "second"):
            db.execute("INSERT OR IGNORE INTO server_apps(app_id,display_name) VALUES (?,?)", (app, app))
        db.executemany("INSERT INTO server_lumi_telegram(account_id,telegram_id) VALUES (?,?)",
                       ((keys.owner[0], PRIVATE), (keys.other[0], PRIVATE + 1)))


def launch(init=None, app="inbe"):
    return {"app_id": app, "init_data": (init or signed_init())[0]}


def invoice_body(identifier, product=OFFER["product_id"]):
    return dict(launch(), product_id=product, request_id=identifier)


def target(invoice):
    return INVOICES + "/" + invoice["invoice_id"]


def payload(invoice):
    return "entity_stars." + invoice["invoice_id"]


def checkout(port, fake, invoice, checkout_id, allowed, *, buyer=PRIVATE, amount=OFFER["amount"], expected=200):
    details = {"id": checkout_id, "from": {"id": buyer}, "currency": "XTR", "total_amount": amount,
               "invoice_payload": payload(invoice)}
    assert request(port, "POST", LUMI + "webhook", headers=WEBHOOK,
                   body={"pre_checkout_query": details}, expected=expected) == (
                       True if expected == 200 else {"error": "Telegram checkout reply temporarily unavailable"})
    answer = {"pre_checkout_query_id": checkout_id, "ok": allowed}
    if not allowed:
        answer["error_message"] = "This offer is unavailable. Return to the app and try again."
    assert fake.requests("answerPreCheckoutQuery")[-1] == answer


def payment(invoice, charge, *, refund=False, buyer=PRIVATE, amount=OFFER["amount"]):
    return {"message": {"chat": {"id": buyer, "type": "private"}, "from": {"id": BOT_ID if refund else buyer, "is_bot": refund},
                        "refunded_payment" if refund else "successful_payment": {
                            "currency": "XTR", "total_amount": amount, "invoice_payload": payload(invoice),
                            "telegram_payment_charge_id": charge}}}


def purchase_checks(port, database, fake, owner, other):
    def create(name, expected=201, product=OFFER["product_id"]):
        return request(port, "POST", INVOICES, headers=owner, body=invoice_body(name, product), expected=expected)

    def webhook(value, expected=200, headers=WEBHOOK):
        return request(port, "POST", LUMI + "webhook", headers=headers, body=value, expected=expected)

    def wallet(credits, refunds, balance):
        assert request(port, "GET", BALANCE, headers=owner)["balance"] == balance
        assert request(port, "GET", BALANCE, headers=other)["balance"] == 0
        with closing(sqlite3.connect(database)) as db:
            rows = dict(db.execute("SELECT event_type,COUNT(*) FROM token_ledger WHERE source_type='telegram_stars' GROUP BY event_type"))
            assert rows == {event: count for event, count in (("credit", credits), ("debit", refunds)) if count}, rows
            assert db.execute("SELECT COUNT(*) FROM token_processed_payments WHERE provider='telegram_stars'").fetchone() == (credits,)

    fake.fail_link = True
    create("purchase-retry", 503)
    invoice = create("purchase-retry")
    assert invoice == {"invoice_id": invoice["invoice_id"], "invoice_url": LINK, "product_id": OFFER["product_id"],
                       "currency": "XTR", "amount": OFFER["amount"], "units": OFFER["units"], "status": "pending"}
    calls = fake.requests("createInvoiceLink")
    assert calls == [{"title": OFFER["title"], "description": OFFER["title"], "payload": payload(invoice),
                      "provider_token": "", "currency": "XTR", "prices": [{"label": OFFER["title"], "amount": OFFER["amount"]}]}] * 2
    assert create("purchase-retry", 200) == invoice
    create("purchase-retry", 409, "changed.product")
    assert len(fake.requests("createInvoiceLink")) == 2
    assert request(port, "GET", target(invoice), headers=owner) == invoice
    request(port, "GET", target(invoice), headers=other, expected=404)
    request(port, "POST", target(invoice) + "/cancel", headers=other, expected=404)
    cancelled = request(port, "POST", target(invoice) + "/cancel", headers=owner)
    assert cancelled["status"] == "cancelled" and "invoice_url" not in cancelled
    checkout(port, fake, invoice, "cancelled", False)
    webhook(payment(invoice, "cancelled-charge"), 409)

    invoice = create("purchase-late")
    settled = payment(invoice, "charge-late")
    webhook(settled, 401, {})
    webhook(settled, 409)
    checkout(port, fake, invoice, "wrong-buyer", False, buyer=PRIVATE + 1)
    checkout(port, fake, invoice, "wrong-amount", False, amount=1)
    fake.fail_answer = True
    checkout(port, fake, invoice, "approved", True, expected=503)
    checkout(port, fake, invoice, "approved", True)
    checkout(port, fake, invoice, "another-checkout", False)
    request(port, "POST", target(invoice) + "/cancel", headers=owner, expected=409)
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("UPDATE telegram_stars_invoices SET expires_at=1 WHERE invoice_id=?", (invoice["invoice_id"],))
        db.execute("CREATE TRIGGER reject_stars_credit BEFORE UPDATE OF state ON telegram_stars_invoices "
                   "WHEN NEW.state='credited' BEGIN SELECT RAISE(ABORT,'fixture invoice failure'); END")
    assert request(port, "GET", target(invoice), headers=owner)["status"] == "expired"
    webhook(settled, 503)
    wallet(0, 0, 0)
    with closing(sqlite3.connect(database)) as db, db:
        assert db.execute("SELECT charge_id FROM telegram_stars_invoices WHERE invoice_id=?", (invoice["invoice_id"],)).fetchone() == ("",)
        db.execute("DROP TRIGGER reject_stars_credit")
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(webhook, [settled] * 4)) == [True] * 4
    wallet(1, 0, OFFER["units"])
    assert request(port, "GET", target(invoice), headers=owner)["status"] == "credited"
    webhook(payment(invoice, "different-charge", refund=True), 409)
    assert webhook(payment(invoice, "charge-late", refund=True)) is True
    assert webhook(settled) is True
    assert webhook(payment(invoice, "charge-late", refund=True)) is True
    wallet(1, 1, 0)

    invoice = create("purchase-history")
    checkout(port, fake, invoice, "history-checkout", True)
    fake.history = [{"id": "history-charge", "amount": -OFFER["amount"],
                     "receiver": {"type": "user", "user": {"id": PRIVATE}}}]
    for _ in range(2):
        assert request(port, "POST", RECONCILE, headers=ADMIN, body={"offset": 5}) == {"seen": 1, "matched": 0, "next_offset": 6}
    assert fake.requests("getStarTransactions") == [{"limit": 32, "offset": 5}] * 2
    wallet(1, 1, 0)
    assert webhook(payment(invoice, "history-charge")) is True
    assert request(port, "GET", target(invoice), headers=owner)["status"] == "refunded"
    wallet(2, 2, 0)
    source = {"type": "user", "transaction_type": "invoice_payment", "user": {"id": PRIVATE}, "invoice_payload": payload(invoice)}
    fake.history.append({"id": "history-charge", "amount": OFFER["amount"], "source": source})
    assert request(port, "POST", RECONCILE, headers=ADMIN, body={"offset": 0}) == {"seen": 2, "matched": 2, "next_offset": 2}
    fake.history.append({"id": "history-charge", "amount": OFFER["amount"], "source": source, "nanostar_amount": "0"})
    request(port, "POST", RECONCILE, headers=ADMIN, body={}, expected=503)
    wallet(2, 2, 0)


def delegate_headers(port, keys, permitted):
    pending = keys.owner_request(port, AUTHORIZATION + "requests", {"app_id": "inbe", "telegram": True,
                                 "scopes": [{"collection": "private.inbe.v2.lumi", "read": True}]})
    init, init_digest = signed_init()
    claim = lines("daochi-rendezvous-claim-v1", pending["request_id"], "inbe", pending["node_id"], pending["audience"],
                  pending["challenge"], pending["expires_at"], keys.client, keys.signing, keys.encryption, init_digest)
    request(port, "POST", AUTHORIZATION + "claim", body={"request_id": pending["request_id"], "client_id": keys.client,
            "signing_key": keys.signing, "encryption_key": keys.encryption, "init_data": init, "proof": keys.delegate_sign(claim)})
    account, bearer, owner_key, _ = keys.owner
    now = int(time.time())
    grant = {"version": 1, "account_id": account, "grant_id": secrets.token_hex(16), "request_id": pending["request_id"],
             "app_id": "inbe", "client_id": keys.client, "node_id": pending["node_id"], "audience": pending["audience"],
             "signing_key": keys.signing, "encryption_key": keys.encryption, "issued_at": now, "not_before": now,
             "expires_at": now + 120, "nonce": secrets.token_hex(16), "bot_id": BOT_ID, "telegram_id": PRIVATE}
    scope = {"collection": "private.inbe.v2.lumi", "visibility": "private", "read": True, "write": False,
             "key_id": "stars-fixture-key", "key_envelope": "synthetic-envelope"}
    signed = lines("daochi-authorization-v1", *grant.values(), 1, scope["collection"], scope["visibility"],
                   "read", "", scope["key_id"], digest(scope["key_envelope"].encode()))
    grant["scopes"] = [scope]
    grant["signature"] = base64.b64decode(keys.sign(owner_key, signed, "sign")).hex()
    if permitted:
        grant["telegram_stars"] = True
        request(port, "POST", AUTHORIZATION + "approve", headers={"Authorization": bearer}, body=grant, expected=401)
        grant["signature"] = base64.b64decode(keys.sign(owner_key, signed + b"telegram-stars-purchases\n", "sign")).hex()
    assert request(port, "POST", AUTHORIZATION + "approve", headers={"Authorization": bearer}, body=grant) is True
    identifier = {"grant_id": grant["grant_id"]}
    challenge = request(port, "POST", AUTHORIZATION + "challenge", body=identifier)
    proof = lines("daochi-session-v1", grant["grant_id"], challenge["challenge"], challenge["node_id"],
                  challenge["audience"], challenge["expires_at"])
    issued = request(port, "POST", AUTHORIZATION + "session", body=dict(identifier, challenge=challenge["challenge"],
                     signature=keys.delegate_sign(proof)))["credential"]

    def headers(verb, route, body=b""):
        path, _, query = route.partition("?")
        data = {"version": 1, "grant_id": grant["grant_id"], "session_id": issued["session_id"], "account_id": account,
                "app_id": "inbe", "client_id": keys.client, "node_id": grant["node_id"], "audience": grant["audience"],
                "challenge": issued["challenge"], "method": verb, "path": path, "query": query, "body_sha256": digest(body),
                "expires_at": int(time.time()) + 30, "nonce": secrets.token_hex(16)}
        data["signature"] = keys.delegate_sign(lines("daochi-delegate-request-v1", *data.values()))
        return {"Authorization": "Delegate ds1." + issued["session_id"], "X-Mesh-Delegate": encode(data).decode()}

    return headers


def exercise(port, database, oracle, fake):
    keys = Keys(oracle, database)
    seed(database, keys)
    owner = {"Authorization": keys.owner[1]}
    other = {"Authorization": keys.other[1]}
    request(port, "GET", CATALOG, expected=401)
    request(port, "GET", CATALOG, headers=owner | {"X-Mesh-User": keys.other[0]}, expected=401)
    assert request(port, "GET", CATALOG, headers=owner) == {"currency": "XTR", "products": [OFFER] if fake else []}
    assert request(port, "HEAD", CATALOG, headers=owner) is None
    request(port, "GET", CATALOG, headers=owner, body=b" ", expected=400)
    with closing(HTTPConnection("127.0.0.1", port, timeout=5)) as connection:
        connection.request("GET", INVOICES)
        reply = connection.getresponse()
        assert (reply.status, reply.getheader("Allow"), reply.read()) == (405, "POST", b"Method Not Allowed\n")
    assert request(port, "GET", BALANCE, headers=owner) == {"account_id": keys.owner[0], "app_id": "inbe", "asset_id": "waozi:token", "balance": 0}
    request(port, "POST", RECONCILE, headers=owner, body={}, expected=401)
    request(port, "POST", RECONCILE, headers=ADMIN, body={"offset": 1000001}, expected=400)
    request(port, "POST", LAUNCH, headers=owner, body=b'{"app_id":"first","app_id":"second"}', expected=400)
    init = signed_init(start_param="fixture_123", chat_type="sender")
    body = launch(init)
    assert request(port, "POST", LAUNCH, headers=owner, body=body) == {
        "app_id": "inbe", "stars_available": fake is not None, "start_param": "fixture_123", "chat_type": "sender"}
    request(port, "POST", LAUNCH, headers=other, body=body, expected=401)
    request(port, "POST", LAUNCH, headers=owner, body=launch(init, "second"), expected=409)
    request(port, "GET", INVOICES + "/bad-id", headers=owner, expected=404)
    request(port, "GET", INVOICES + "/" + "a" * 32, headers=owner, expected=404)
    if fake is None:
        request(port, "POST", INVOICES, headers=owner, body=invoice_body("disabled-purchase"), expected=404)
        request(port, "POST", RECONCILE, headers=ADMIN, body={}, expected=503)
        return
    purchase_checks(port, database, fake, owner, other)
    for permitted in (False, True):
        headers = delegate_headers(port, keys, permitted)
        body = encode(launch())
        signed = headers("POST", LAUNCH, body)
        for coding, encoded in (("", body), ("identity", body), ("gzip", gzip.compress(body))):
            assert request(port, "POST", LAUNCH, headers=signed | {"Content-Encoding": coding}, body=encoded, expected=400)["error"] == (
                "Telegram delegate requests require unencoded JSON")
        assert request(port, "POST", LAUNCH, headers=signed, body=body)["stars_available"] == permitted
        signed = headers("GET", BALANCE)
        request(port, "GET", BALANCE, headers=signed, body=b" ", expected=400)
        request(port, "GET", BALANCE, headers=signed, expected=200 if permitted else 403)
        if permitted:
            request(port, "GET", BALANCE, headers=signed, expected=409)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("binary", type=lambda value: str(Path(value).resolve()))
    parser.add_argument("oracle", type=lambda value: str(Path(value).resolve()))
    parser.add_argument("--server", action="store_true")
    args = parser.parse_args()
    settings = {"MESH_LUMI_BOT_TOKEN": BOT, "MESH_ADMIN_TOKEN": ADMIN["X-Mesh-Admin"], "MESH_TELEGRAM_ACCOUNTS_ENABLED": "1",
                "MESH_TOKEN_PRODUCTS": "dew_10:10000000,partial:1100000,ambiguous:1000000",
                "MESH_TELEGRAM_STARS_APPS": "inbe", "MESH_TELEGRAM_STARS_PRICES": "dew_10:23,partial:10,ambiguous:1,ambiguous:2"}
    with TemporaryDirectory(prefix="telegram-stars-", dir=Path(args.binary).parent) as path:
        if args.server:
            with node(args.binary, Path(path), **settings, MESH_LUMI_WEBHOOK_SECRET="", MESH_TOKEN_ISSUER_PRIVATE_KEY_HEX="") as (port, database):
                exercise(port, database, args.oracle, None)
        else:
            with fake_provider() as (provider_port, provider):
                with node(args.binary, Path(path), **settings, MESH_LUMI_WEBHOOK_SECRET=SECRET,
                          MESH_LUMI_FIXTURE_TELEGRAM_PORT=str(provider_port)) as (port, database):
                    exercise(port, database, args.oracle, provider)


if __name__ == "__main__":
    main()
