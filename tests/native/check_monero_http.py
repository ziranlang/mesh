"""Exercise native Monero routes using a loopback wallet and disposable SQLite data."""

import base64
import hashlib
import json
import re
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from check_token_http import SEED, TOKENS, check_receipt, enroll_device, login, post, request, server, sign


BASE = TOKENS + "/purchases/monero"
INVOICES = BASE + "/invoices"
ADDRESS = BASE + "/address"
DEPOSITS = BASE + "/deposits"
PRODUCT = {"app_id": "inbe", "product_id": "small"}
PRICE = 700
UNITS = 1_234_567
USER = "fixture"
PASSWORD = "fixture-wallet-secret"


class Wallet:
    def __init__(self):
        self.lock = threading.Lock()
        self.allocations = []
        self.transfers = {"in": [], "pool": [], "pending": []}
        self.height = 500
        self.mode = "digest"
        self.fail_create = False
        self.digest_requests = 0
        self.redirect_requests = 0
        self.block_method = None
        self.probe = threading.Event()
        self.release = threading.Event()

    def set(self, **transfers):
        with self.lock:
            self.transfers = {kind: transfers.get(kind, []) for kind in self.transfers}

    def authorized(self, header, path):
        if self.mode == "basic":
            return header == "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
        if self.mode != "digest" or not header.startswith("Digest "):
            return False
        fields = {name: quoted if quoted else plain for name, quoted, plain in re.findall(
            r'(\w+)=(?:"([^"]*)"|([^, ]+))', header)}
        sha = lambda value: hashlib.sha256(value.encode()).hexdigest()
        first = sha(f"{USER}:fixture:{PASSWORD}")
        second = sha(f"POST:{path}")
        expected = sha(":".join((first, "test-nonce", "00000001", fields.get("cnonce", ""), "auth", second)))
        return (fields.get("username"), fields.get("uri"), fields.get("algorithm"), fields.get("qop"),
                fields.get("response")) == (USER, path, "SHA-256", "auth", expected)

    def rpc(self, method, params):
        if method == "create_address":
            assert params["account_index"] == 0
            if self.fail_create:
                return {"error": {"code": -1, "message": "fixture RPC error"}}
            index = len(self.allocations) + 1
            self.allocations.append(params["label"])
            return {"result": {"address": f"fake-address-{index}", "address_index": index}}
        if method == "get_height":
            return {"result": {"height": self.height}}
        assert method == "get_transfers", method
        indices = params.get("subaddr_indices")
        result = {}
        for kind, rows in self.transfers.items():
            if not params.get(kind):
                continue
            result[kind] = [row for row in rows if (indices is None or row["subaddr_index"]["minor"] in indices) and
                            (not params.get("filter_by_height") or row["height"] >= params["min_height"])]
        return {"result": result}


@contextmanager
def mock_wallet():
    wallet = Wallet()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/never":
                wallet.redirect_requests += 1
                self.send_error(500)
                return
            assert self.path == "/wallet/json_rpc", self.path
            assert payload["jsonrpc"] == "2.0" and payload["id"] == "0"
            with wallet.lock:
                if wallet.mode == "redirect":
                    self.send_response(307)
                    self.send_header("Location", "/never")
                    self.end_headers()
                    return
                if not wallet.authorized(self.headers.get("Authorization", ""), self.path):
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Digest realm="weak", nonce="weak", algorithm=MD5, qop="auth"')
                    if wallet.mode != "weak":
                        self.send_header("WWW-Authenticate", 'Digest realm="fixture", nonce="test-nonce", algorithm=SHA-256, qop="auth"')
                    self.end_headers()
                    return
                wallet.digest_requests += (wallet.mode == "digest")
                blocked = payload["method"] == wallet.block_method
                reply = wallet.rpc(payload["method"], payload["params"])
            if blocked:
                wallet.probe.set()
                wallet.release.wait(timeout=12)
                return
            raw = json.dumps({"jsonrpc": "2.0", "id": "0", **reply}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as mock:
        worker = threading.Thread(target=mock.serve_forever, daemon=True)
        worker.start()
        try:
            yield wallet, f"http://127.0.0.1:{mock.server_port}/wallet"
        finally:
            wallet.release.set()
            mock.shutdown()
            worker.join(timeout=5)


def configuration(url, **extra):
    return {**dict(MESH_TOKEN_DIRECT_PURCHASES_ENABLED="true", MESH_TOKEN_ISSUER_PRIVATE_KEY_HEX=SEED,
                   MESH_XMR_RATE_REFRESH_ENABLED="false",
                   MESH_TOKEN_PRODUCTS=f"small:{UNITS}:{PRICE},non_xmr:17",
                   MONERO_WALLET_RPC_URL=url, MONERO_WALLET_RPC_USER=USER, MONERO_WALLET_RPC_PASSWORD=PASSWORD,
                   MONERO_RATE_ATOMIC_AMOUNT="9", MONERO_RATE_TOKEN_UNITS="4", MONERO_NETWORK="testnet",
                   MONERO_CONFIRMATIONS_REQUIRED="3", MONERO_MINIMUM_ATOMIC_AMOUNT="11"), **extra}


def transfer(tx, index, amount, confirmations=3, **extra):
    return {**dict(txid=tx, amount=amount, height=480, confirmations=confirmations,
                   subaddr_index={"major": 0, "minor": index}, unlock_time=0, locked=False, double_spend_seen=False), **extra}


def get(connection, path, status=200, **kwargs):
    return request(connection, "GET", path, status, **kwargs)[1]


def direct_purchase_readiness(connection):
    connection.request("GET", "/readyz")
    reply = connection.getresponse()
    return json.loads(reply.read())["checks"].get("token_direct_purchases")


def check_disabled(binary, oracle, root):
    with server(binary, root / "disabled.sqlite", **configuration("", MESH_TOKEN_DIRECT_PURCHASES_ENABLED="false")) as (connection, _):
        assert direct_purchase_readiness(connection) is None
        assert get(connection, ADDRESS, 503) == {"error": "monero purchases disabled"}
        assert get(connection, DEPOSITS, 401) == {"error": "bearer token required"}
        _, bearer, _ = login(connection, oracle)
        assert get(connection, DEPOSITS, token=bearer) == {"deposits": []}
        assert post(connection, INVOICES, 503, body=PRODUCT, token=bearer) == {"error": "direct token purchases disabled"}
        assert post(connection, INVOICES, 400, body={"app_id": "inbe"}, token=bearer) == {"error": "product_id required"}


def signed_invoice(oracle, owner, keys, device, body):
    key_id, device_keys = device
    expiry = int(time.time()) + 120
    digest = hashlib.sha256(body).hexdigest()
    tx_id, nonce = "monero-fixture-request", "monero-fixture-nonce"
    proof = "\n".join(("daochi-tx-v1", "6", tx_id, owner, "inbe", key_id, "POST", INVOICES, digest, nonce, str(expiry), "")).encode()
    return {"X-Mesh-Tx": json.dumps(dict(protocol_version=6, tx_id=tx_id, account_id=owner, app_id="inbe",
                                        device_key_id=key_id, method="POST", path=INVOICES, body_sha256=digest,
                                        nonce=nonce, expires_at=expiry, signature=sign(oracle, keys, proof),
                                        device_signature=sign(oracle, device_keys, proof, "device-sign")))}


def check_address(connection, oracle, wallet, owner, bearer):
    assert get(connection, ADDRESS, 401) == {"error": "bearer token required"}
    assert get(connection, ADDRESS + "/missing", 404) == {"error": "recipient not found"}
    post(connection, "/api/v1/account/alias", body={"alias": "recipient"}, token=bearer, headers={"X-Mesh-User": owner})

    def allocate(route):
        with closing(HTTPConnection("127.0.0.1", connection.port, timeout=15)) as other:
            response, result = request(other, "GET", route, token=bearer if route == ADDRESS else None)
            assert response.getheader("Cache-Control") == "private, no-store"
            return result

    with ThreadPoolExecutor(max_workers=2) as workers:
        responses = list(workers.map(allocate, (ADDRESS, ADDRESS + "/%40RECIPIENT")))
    assert responses[0] == responses[1] == {
        "account_id": owner, "alias": "recipient", "asset_id": "waozi:token", "address": "fake-address-1",
        "uri": "monero:fake-address-1", "network": "testnet", "confirmations_required": 3,
        "minimum_atomic_amount": 11, "rate": {"xmr_atomic_units": 9, "token_units": 4},
    }
    assert len(wallet.allocations) == 1 and wallet.allocations[0].startswith("daochi-account-")
    assert get(connection, ADDRESS + "/" + owner) == responses[0]


def check_invoices(connection, database, wallet, oracle, root, owner, bearer, keys):
    assert post(connection, INVOICES, 401, body=PRODUCT) == {"error": "bearer token required"}
    assert post(connection, INVOICES, 400, body=dict(PRODUCT, product_id="non_xmr"), token=bearer) == {"error": "unknown monero product_id"}
    assert post(connection, INVOICES, 400, body=dict(PRODUCT, app_id="missing"), token=bearer) == {"error": "unknown app_id"}
    assert get(connection, INVOICES + "/bad", 400, token=bearer) == {"error": "invalid invoice id"}
    device = enroll_device(connection, oracle, owner, bearer, keys)
    body = json.dumps(PRODUCT).encode()
    headers = signed_invoice(oracle, owner, keys, device, body)
    wallet.fail_create = True
    assert post(connection, INVOICES, 500, body=body, token=bearer, headers=headers) == {"error": "monero invoice failed"}
    assert database.execute("SELECT COUNT(*) FROM server_signed_transactions").fetchone() == (0,)
    wallet.fail_create = False
    invoice = post(connection, INVOICES, 201, body=body, token=bearer, headers=headers)
    assert post(connection, INVOICES, 409, body=body, token=bearer, headers=headers) == {"error": "signed transaction replay"}
    assert invoice == {"id": invoice["id"], "app_id": "inbe", "status": "pending", "product_id": "small",
                       "asset_id": "waozi:token", "token_units": UNITS, "atomic_amount": PRICE,
                       "address": "fake-address-2", "address_index": 2, "expires_at": invoice["expires_at"]}
    assert wallet.allocations[1] == "daochi-" + invoice["id"]
    route = INVOICES + "/" + invoice["id"]
    _, outsider, _ = login(connection, oracle)
    assert get(connection, route, 404, token=outsider) == {"error": "invoice not found"}
    assert get(connection, route, token=bearer) == invoice
    first = transfer("tx-b", 2, 400)
    second = transfer("tx-a", 2, 300)
    wallet.set(**{"in": [first, transfer("other-account", 2, 900, subaddr_index={"major": 1, "minor": 2})],
                  "pool": [transfer("tx-a", 2, 300, 0)]})
    assert get(connection, route, token=bearer)["status"] == "pending"
    wallet.set(**{"in": [first, second, second]})
    database.execute("CREATE TRIGGER refuse_invoice_receipt BEFORE UPDATE OF receipt_id ON token_payment_intents "
                     "WHEN NEW.provider='monero' BEGIN SELECT RAISE(FAIL,'fixture write failure'); END")
    assert get(connection, route, token=bearer)["status"] == "pending"
    assert database.execute("SELECT COUNT(*) FROM token_ledger").fetchone() == (0,)
    assert database.execute("SELECT COUNT(*) FROM token_processed_payments").fetchone() == (0,)
    database.execute("DROP TRIGGER refuse_invoice_receipt")
    paid = get(connection, route, token=bearer)
    assert paid["status"] == "paid" and paid["payment_id"] == "tx-a+tx-b:0:2"
    receipt = paid["receipt"]
    assert (receipt["account_id"], receipt["app_id"], receipt["source_type"], receipt["source_ref"],
            receipt["amount_delta"]) == (owner, "inbe", "monero", "tx-a+tx-b:0:2", UNITS)
    check_receipt(root, receipt, 1, "")
    assert get(connection, route, token=bearer) == paid
    assert database.execute("SELECT COUNT(*) FROM token_ledger").fetchone() == (1,)
    assert get(connection, DEPOSITS, token=bearer) == {"deposits": []}


def check_rpc_failures(connection, wallet, bearer):
    for mode in ("weak", "redirect"):
        wallet.mode = mode
        assert post(connection, INVOICES, 500, body=PRODUCT, token=bearer) == {"error": "monero invoice failed"}
    assert wallet.redirect_requests == 0
    wallet.mode = "basic"
    assert post(connection, INVOICES, 201, body=PRODUCT, token=bearer)["status"] == "pending"


def main(binary, oracle):
    binary, oracle = (str(Path(name).resolve()) for name in (binary, oracle))
    with tempfile.TemporaryDirectory(prefix="monero-", dir=Path(binary).parent) as temporary:
        root = Path(temporary)
        check_disabled(binary, oracle, root)
        with mock_wallet() as (wallet, url), server(binary, root / "ready.sqlite", **configuration(url)) as (connection, database):
            assert direct_purchase_readiness(connection) == "ok"
            owner, bearer, keys = login(connection, oracle)
            check_address(connection, oracle, wallet, owner, bearer)
            check_invoices(connection, database, wallet, oracle, root, owner, bearer, keys)
            check_rpc_failures(connection, wallet, bearer)
            assert wallet.digest_requests >= 5
    print("native Monero invoice, address, and deposit HTTP routes passed")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(f"usage: {sys.argv[0]} SERVER SIGNING_ORACLE")
    main(*sys.argv[1:])
