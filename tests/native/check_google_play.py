import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import gzip
import hashlib
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import select
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs, unquote, urlsplit

from check_http_listener import make_bearer, sign_with_oracle


ROUTE = "/api/v1/tokens/purchases/google/verify"
PACKAGE = "xyz.waozi.inbe"
OTHER_PACKAGE = "dev.fixture.other"


def encode(value):
    return json.dumps(value, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def lines(*values):
    return ("\n".join(map(str, values)) + "\n").encode()


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, value=None):
        body = b"" if value is None else encode(value)
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        state = self.server
        if self.path == "/token":
            body = self.rfile.read(int(self.headers["Content-Length"]))
            fields = parse_qs(body.decode(), strict_parsing=True)
            with state.lock:
                state.forms.append(fields)
                if state.oauth_error:
                    self.reply(400, {"error": "invalid_grant"})
                    return
                access = f"access-{len(state.forms)}"
                state.access.add(access)
            self.reply(200, {"access_token": access, "token_type": "Bearer", "expires_in": 1})
            return
        assert self.path.endswith(":consume"), self.path
        self.purchase(True)

    def do_GET(self):
        self.purchase(False)

    def purchase(self, consume):
        state = self.server
        path = urlsplit(self.path)
        access = self.headers.get("Authorization", "").removeprefix("Bearer ")
        with state.lock:
            assert access in state.access, access
            state.requests.append((self.command, self.path, access))
        parts = path.path.split("/")
        if parts[-1] == "voidedpurchases":
            parameters = parse_qs(path.query)
            assert parameters["includeQuantityBasedPartialRefund"] == ["true"] and parameters["maxResults"] == ["100"]
            assert abs(int(parameters["startTime"][0]) - (int(time.time() * 1000) - 2592000000)) < 10000
            with state.lock:
                documents = list(state.voided.get(unquote(parts[2]), []))
            cursor = "page +?&"
            if len(documents) > 1 and "token" not in parameters:
                self.reply(200, {"voidedPurchases": documents[:1], "tokenPagination": {"nextPageToken": cursor}})
                return
            if "token" in parameters:
                assert parameters["token"] == [cursor]
                documents = documents[1:]
            self.reply(200, {"voidedPurchases": documents} if documents else {})
            return
        assert len(parts) == 8 and parts[1] == "applications" and parts[3:5] == ["purchases", "products"] and parts[6] == "tokens", parts
        token = unquote(parts[7].removesuffix(":consume") if consume else parts[7])
        with state.lock:
            item = state.items.get(token)
            if item is None:
                self.reply(404, {})
                return
            if consume:
                mode = state.consume.get(token, "ok")
                if mode == "error":
                    self.reply(500, {})
                    return
                item["consumptionState"] = 1
                self.reply(503 if mode == "lost" else 204)
                return
            data = dict(item)
            gate = state.gates.get(token)
        if gate is not None:
            gate[0].set()
            assert gate[1].wait(6), "fixture never released provider response"
        self.reply(200, data)


@contextmanager
def provider():
    with ThreadingHTTPServer(("127.0.0.1", 0), Provider) as server:
        server.lock = threading.Lock()
        server.forms = []
        server.requests = []
        server.access = set()
        server.items = {}
        server.consume = {}
        server.gates = {}
        server.voided = {}
        server.oauth_error = False
        server.origin = f"http://127.0.0.1:{server.server_port}"
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield server
        finally:
            server.shutdown()
            worker.join(5)


@contextmanager
def native(binary, database, server, **settings):
    env = {key: value for key, value in os.environ.items() if not key.startswith("MESH_TEST_PLAY_")}
    env.update(MESH_TEST_HTTP_DATABASE=str(database), MESH_TEST_PLAY_ORIGIN=server.origin)
    env.update({"MESH_TEST_PLAY_" + key.upper(): value for key, value in settings.items()})
    with subprocess.Popen([binary], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE) as process:
        try:
            ready, _, _ = select.select([process.stdout], [], [], 5)
            assert ready, "Play fixture did not report its port"
            line = process.stdout.readline()
            assert line.strip().isdigit(), (line, process.poll())
            yield int(line)
        finally:
            _, errors = process.communicate(input=b"\n", timeout=8)
            assert process.returncode == 0, (process.returncode, errors)


def query(database, sql, args=()):
    with closing(sqlite3.connect(database)) as session, session:
        return session.execute(sql, args).fetchall()


def user(database, oracle):
    owner = sign_with_oracle(oracle, "keypair")
    device = sign_with_oracle(oracle, "device-keypair")
    public = base64.b64decode(owner["PublicKey"])
    account = digest(public)
    session = secrets.token_hex(32)
    now = int(time.time())
    bearer = make_bearer(lines("v2", account, now + 3600, now, session))
    token_hash = digest(bearer.removeprefix("Bearer ").encode())
    with closing(sqlite3.connect(database)) as writer, writer:
        writer.execute("INSERT OR IGNORE INTO server_apps(app_id,display_name) VALUES ('inbe','Inbe'),('entity','Entity')")
        writer.execute("INSERT INTO server_users(user_id_hash,public_key) VALUES (?,?)", (account, public))
        writer.execute("INSERT INTO server_auth_sessions(token_hash,user_id_hash,session_id,issued_at,expires_at,created_at) VALUES (?,?,?,?,?,CURRENT_TIMESTAMP)",
                       (token_hash, account, session, now, now + 3600))
        writer.execute("INSERT INTO server_device_keys(account_id,app_id,device_key_id,client_id,public_key) VALUES (?,?,?,?,?)",
                       (account, "inbe", "fixture-key", "fixture-client", base64.b64decode(device["PublicKey"]).hex()))
    return account, bearer, owner, device


def signed(oracle, actor, body):
    account, bearer, owner, device = actor
    version, identifier, nonce, expiry = 6, secrets.token_hex(16), secrets.token_hex(16), int(time.time()) + 300
    message = lines("daochi-tx-v1", version, identifier, account, "inbe", "fixture-key", "POST", ROUTE, digest(body), nonce, expiry)
    transaction = dict(protocol_version=version, tx_id=identifier, account_id=account, app_id="inbe",
                       device_key_id="fixture-key", method="POST", path=ROUTE, body_sha256=digest(body), nonce=nonce, expires_at=expiry)
    for field, operation, key in [("signature", "sign", owner), ("device_signature", "device-sign", device)]:
        transaction[field] = sign_with_oracle(oracle, operation, PrivateKey=key["PrivateKey"],
                                              Message=base64.b64encode(message).decode())["Signature"]
    return {"Authorization": bearer, "X-Mesh-Tx": base64.urlsafe_b64encode(encode(transaction)).rstrip(b"=").decode()}


def purchase(token, app="inbe", package=PACKAGE, sku="sku"):
    return dict(app_id=app, package_name=package, product_id=sku, purchase_token=token)


def call(port, body, status, headers=None, *, method="POST", compress=False):
    payload = body if isinstance(body, bytes) else encode(body)
    headers = {"Content-Type": "application/json", **(headers or {})}
    if compress:
        headers["Content-Encoding"] = "gzip"
        payload = gzip.compress(payload)
    with closing(HTTPConnection("127.0.0.1", port, timeout=9)) as connection:
        connection.request(method, ROUTE, payload, headers)
        response = connection.getresponse()
        actual = response.read()
        assert response.status == status, (body, response.status, actual[:800])
        if status == 405:
            assert response.getheader("Allow") == "POST"
            return
        assert response.getheader("Content-Type") == "application/json", response.headers
        return json.loads(actual)


def item(server, token, account=None, **fields):
    payload = dict(purchaseState=0, consumptionState=0, purchaseToken=token, orderId="GPA." + token)
    if account is not None:
        payload["obfuscatedExternalAccountId"] = digest(account.encode())
    with server.lock:
        server.items[token] = {**payload, **fields}


def state(database, token):
    return query(database, "SELECT account_id,app_id,state,receipt_id FROM google_play_purchase_lifecycle WHERE purchase_token=?", (token,))


def untouched(database, token):
    assert not state(database, token)
    for table, column in [("token_processed_payments", "provider_payment_id"), ("token_google_origins", "payment_id"),
                          ("google_play_purchase_proofs", "purchase_token"), ("token_ledger", "source_ref")]:
        assert not query(database, f"SELECT 1 FROM {table} WHERE {column}=?", (token,)), table


def core(port, database, server, oracle):
    primary = user(database, oracle)
    secondary = user(database, oracle)
    account, bearer, *_ = primary
    headers = {"Authorization": bearer}
    call(port, b"{", 405, method="GET")
    call(port, b"{", 401)
    assert call(port, b"{", 400, headers)["error"] == "invalid json"
    assert call(port, purchase("unused", app="entity"), 400, headers)["error"] == "package app mismatch"
    assert call(port, purchase("unused", sku="missing"), 400, headers)["error"] == "unknown product_id"
    assert not server.requests

    token = "strict/a?b+#%"
    item(server, token, account)
    wrong = {"Authorization": secondary[1]}
    assert call(port, purchase(token), 500, wrong)["error"] == "token credit failed"
    untouched(database, token)
    with ThreadPoolExecutor(max_workers=4) as concurrency:
        credits = list(concurrency.map(lambda _: call(port, purchase(token), 200, headers, compress=True), range(4)))
    receipt = credits[0]["receipt"]
    assert all(credit == credits[0] for credit in credits), credits
    assert credits[0]["balance"] == 17 and receipt["amount_delta"] == 17 and receipt["source_ref"] == token
    assert receipt["app_id"] == "inbe" and receipt["account_id"] == account and receipt["source_type"] == "google_play"
    assert state(database, token) == [(account, "inbe", "consumed", receipt["receipt_id"])]
    assert query(database, "SELECT app_id,issued,remaining FROM token_allocations WHERE receipt_id=?", (receipt["receipt_id"],)) == [("inbe", 17, 17)]
    assert any("strict%2Fa%3Fb+%23%25" in request[1] for request in server.requests)
    call(port, purchase(token), 500, wrong)
    call(port, purchase(token, sku="other"), 500, headers)
    call(port, purchase(token, app="entity", package=OTHER_PACKAGE), 500, headers)

    waiting = "missing-binding"
    item(server, waiting)
    assert call(port, purchase(waiting), 409, headers)["error"] == "google purchase account binding required"
    assert state(database, waiting) == [("", "", "binding_missing", "")]
    item(server, waiting, account)
    call(port, purchase(waiting), 500, wrong)
    assert call(port, purchase(waiting), 200, headers)["balance"] == 34

    item(server, "multi", account, quantity=3, refundableQuantity=2)
    multi = call(port, purchase("multi"), 200, headers)
    assert multi["balance"] == 68 and multi["receipt"]["amount_delta"] == 51
    assert query(database, "SELECT amount_delta FROM token_ledger WHERE source_ref='multi' ORDER BY ledger_seq") == [(51,), (-17,)]
    item(server, "multi", account, quantity=3, refundableQuantity=1, consumptionState=1)
    assert call(port, purchase("multi"), 200, headers)["balance"] == 51
    assert call(port, purchase("multi"), 200, headers)["balance"] == 51
    assert query(database, "SELECT amount_delta FROM token_ledger WHERE source_ref='multi' ORDER BY ledger_seq") == [(51,), (-17,), (-17,)]

    for bad, fields, status in [("quantity", {"quantity": 2}, 400), ("overflow", {"quantity": 2, "refundableQuantity": 2}, 400),
                                ("consumed", {"consumptionState": 1}, 500), ("pending", {"purchaseState": 2}, 500),
                                ("token-mismatch", {"purchaseToken": "another"}, 400)]:
        item(server, bad, account, **fields)
        call(port, purchase(bad, sku="overflow" if bad == "overflow" else "sku"), status, headers)
        untouched(database, bad)

    query(database, "UPDATE token_app_permissions SET legacy_unsigned_until=0 WHERE app_id='inbe' AND asset_id='waozi:token' AND permission='purchase'")
    item(server, "storage-fault", account)
    payload = encode(purchase("storage-fault"))
    auth = signed(oracle, primary, payload)
    assert call(port, payload, 401, headers)["error"] == "signed transaction required"
    query(database, "CREATE TRIGGER abort_play_fixture BEFORE INSERT ON google_play_purchase_lifecycle WHEN NEW.purchase_token='storage-fault' BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
    assert call(port, payload, 500, auth)["error"] == "token credit failed"
    untouched(database, "storage-fault")
    query(database, "DROP TRIGGER abort_play_fixture")
    call(port, payload, 200, auth)
    assert call(port, payload, 409, auth)["error"] == "signed transaction replay"

    item(server, "signed-claim")
    payload = encode(purchase("signed-claim"))
    auth = signed(oracle, primary, payload)
    call(port, payload, 409, auth)
    item(server, "signed-claim", account)
    call(port, payload, 200, auth)
    query(database, "DELETE FROM token_app_permissions WHERE app_id='inbe'")

    item(server, "cancel", account)
    entered, release = threading.Event(), threading.Event()
    server.gates["cancel"] = (entered, release)
    payload = encode(purchase("cancel"))
    with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
        client.sendall((f"POST {ROUTE} HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: {bearer}\r\n"
                        f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n\r\n").encode() + payload)
        assert entered.wait(4), "Play request never entered the provider"
        client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    release.set()
    del server.gates["cancel"]
    time.sleep(0.15)
    untouched(database, "cancel")
    call(port, purchase("cancel"), 200, headers)

    server.oauth_error = True
    item(server, "oauth-error", account)
    assert call(port, purchase("oauth-error"), 503, headers)["error"] == "payment verifier unavailable"
    untouched(database, "oauth-error")
    server.oauth_error = False
    call(port, purchase("oauth-error"), 200, headers)
    assert all(form == {"client_id": ["mock client"], "client_secret": ["s+e&cret"], "grant_type": ["refresh_token"],
                        "refresh_token": ["t+k&n"]} for form in server.forms)
    api_access = [access for method, path, access in server.requests if "voidedpurchases" not in path]
    assert len(api_access) == len(set(api_access)), api_access

    legacy_reference = f"{PACKAGE}:sku:GPA.prior"
    item(server, legacy_reference, account, orderId="GPA.prior")
    original = call(port, purchase(legacy_reference), 200, headers)
    item(server, "legacy-token", orderId="GPA.prior", consumptionState=1)
    assert call(port, purchase("legacy-token"), 200, headers) == original
    assert query(database, "SELECT provider_payment_id,receipt_id FROM token_processed_payments WHERE provider_payment_id IN (?,?) ORDER BY provider_payment_id",
                 (legacy_reference, "legacy-token")) == [("legacy-token", original["receipt"]["receipt_id"]), (legacy_reference, original["receipt"]["receipt_id"])]

    void_time = int(time.time() * 1000) - 1000
    full_void = dict(purchaseToken="oauth-error", orderId="GPA.oauth-error", voidedTimeMillis=str(void_time))
    server.voided[PACKAGE] = [dict(full_void, purchaseToken="unknown"), full_void]
    call(port, purchase("oauth-error"), 500, headers)
    call(port, purchase("oauth-error"), 500, headers)
    assert state(database, "oauth-error")[0][2] == "refunded"
    assert query(database, "SELECT amount_delta FROM token_ledger WHERE source_ref='oauth-error' ORDER BY ledger_seq") == [(17,), (-17,)]
    assert query(database, "SELECT voided_millis,voided_quantity,LENGTH(receipt_id) FROM google_play_refund_events WHERE purchase_token='oauth-error'") == [(void_time, 0, 32)]
    server.voided.clear()

    item(server, "watched", account, quantity=3, refundableQuantity=3)
    call(port, purchase("watched"), 200, headers)
    server.voided[PACKAGE] = [dict(purchaseToken="watched", orderId="GPA.watched", voidedTimeMillis=str(void_time), voidedQuantity=1)]

    item(server, "durable", account)
    server.consume["durable"] = "error"
    call(port, purchase("durable"), 200, headers)
    assert state(database, "durable")[0][2] == "consume_pending"
    server.consume["durable"] = "lost"
    return primary


def eventually(predicate):
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    assert predicate(), "Play worker did not complete durable work"


def recovery(database, server, actor):
    eventually(lambda: state(database, "durable")[0][2] == "consumed")
    eventually(lambda: query(database, "SELECT 1 FROM google_play_refund_events WHERE purchase_token='watched'"))
    assert query(database, "SELECT count(*) FROM google_play_refund_events WHERE purchase_token='watched'") == [(1,)]
    assert query(database, "SELECT refunded_quantity FROM google_play_purchase_quantities WHERE purchase_token='watched'") == [(0,)]
    with server.lock:
        server.voided.clear()
    item(server, "watched", actor[0], quantity=3, refundableQuantity=2, consumptionState=1)
    eventually(lambda: query(database, "SELECT refunded_quantity FROM google_play_purchase_quantities WHERE purchase_token='watched'") == [(1,)])
    item(server, "watched", actor[0], quantity=3, refundableQuantity=0, consumptionState=1, purchaseState=1)
    eventually(lambda: state(database, "watched")[0][2] == "refunded")
    assert query(database, "SELECT amount_delta FROM token_ledger WHERE source_ref='watched' ORDER BY ledger_seq") == [(51,), (-17,), (-34,)]


def jwt_signature(directory, fields, server, public_key):
    assert fields["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"], fields.keys()
    token, = fields["assertion"]
    head, claims, signature = token.split(".")
    decode = lambda value: base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    assert json.loads(decode(head)) == {"alg": "RS256", "typ": "JWT"}
    payload = json.loads(decode(claims))
    assert payload["aud"] == server.origin + "/token" and payload["iss"] == "fixture@example.invalid"
    assert payload["scope"] == "https://www.googleapis.com/auth/androidpublisher"
    assert abs(payload["iat"] - time.time()) < 15 and payload["exp"] - payload["iat"] == 3600
    signature_file = directory / "assertion.sig"
    signature_file.write_bytes(decode(signature))
    signed = subprocess.run(["openssl", "dgst", "-sha256", "-verify", public_key, "-signature", signature_file],
                            input=f"{head}.{claims}".encode(), capture_output=True, check=True)
    assert signed.stdout.strip() == b"Verified OK"


def main(binary, oracle):
    with tempfile.TemporaryDirectory(prefix="mesh-play-") as path, provider() as server:
        directory = Path(path)
        database = directory / "mesh.db"
        oauth = encode({"installed": {"client_id": "mock client", "client_secret": "s+e&cret", "token_uri": server.origin + "/token"}}).decode()
        with native(binary, database, server, oauth=oauth, refresh=" t+k&n ", apps=OTHER_PACKAGE + "=entity") as port:
            primary = core(port, database, server, oracle)
        with native(binary, database, server, oauth=oauth, refresh="t+k&n", retry="1"):
            recovery(database, server, primary)
        with native(binary, database, server) as port:
            assert call(port, purchase("disabled"), 503, {"Authorization": primary[1]})["error"] == "payment verifier unavailable"
            untouched(database, "disabled")

        private = directory / "play-private.pem"
        public = directory / "play-public.pem"
        subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", private], capture_output=True, check=True)
        subprocess.run(["openssl", "pkey", "-in", private, "-pubout", "-out", public], capture_output=True, check=True)
        service = encode(dict(client_email="fixture@example.invalid", private_key=private.read_text(), token_uri=server.origin + "/token")).decode()
        start = len(server.forms)
        item(server, "signed-jwt", primary[0])
        with native(binary, database, server, service=service, oauth=oauth, refresh="unused", packages=PACKAGE) as port:
            assert call(port, purchase("blocked", package="net.waozi.inbe"), 400, {"Authorization": primary[1]})["error"] == "package not allowed"
            call(port, purchase("signed-jwt"), 200, {"Authorization": primary[1]})
        assert len(server.forms) >= start + 3
        for form in server.forms[start:]:
            jwt_signature(directory, form, server, public)
    print("Native Google Play verification, OAuth, transaction rollback, consume recovery and refund loopback passed")


if __name__ == "__main__":
    main(*sys.argv[1:])
