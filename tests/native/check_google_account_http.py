"""Exercise Google account recovery with loopback certificates and disposable keys."""

import base64
import gzip
import hashlib
import hmac
import json
import os
import select
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import closing, contextmanager
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from check_http_listener import sign_with_oracle


METHODS = "/api/v1/account-backup/methods"
LOOKUP = "/api/v1/account-backup/google"
LINK = "/api/v1/account/google"
UNLINK = LINK + "/remove"
CLIENT = "fixture.apps.googleusercontent.com"
SECOND = "fixture-secondary.apps.googleusercontent.com"
BAD_TOKEN = {"error": "google token not accepted"}
NO_LINK = {"error": "no account linked"}
DISABLED = {"error": "sign in with google is not set up on this node"}
UNAVAILABLE = {"error": "account google unavailable"}


class CertificateProvider:
    def __init__(self, directory):
        self.key, cert = self.make_key(directory, "trusted")
        self.rogue_key, _ = self.make_key(directory, "unknown")
        self.certificates = {"trusted-key": cert.read_text()}
        self.hits = 0
        self.status = 200
        self.seen = []

    @staticmethod
    def make_key(directory, name):
        key = directory / f"{name}.key.pem"
        cert = directory / f"{name}.cert.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-subj", "/CN=mesh-local-oidc-fixture", "-keyout", str(key), "-out", str(cert)],
                       capture_output=True, timeout=15, check=True)
        return key, cert

    @contextmanager
    def serve(self):
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                provider.hits += 1
                provider.seen.append((self.path, self.headers.get("Authorization")))
                self.send_response(provider.status)
                if provider.status == 302:
                    self.send_header("Location", "/certificate-redirect")
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                if provider.status == 200:
                    self.wfile.write(json.dumps(provider.certificates).encode())

            def log_message(self, *args):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as service:
            worker = threading.Thread(target=service.serve_forever)
            worker.start()
            try:
                yield f"http://127.0.0.1:{service.server_port}/certificates"
            finally:
                service.shutdown()
                worker.join()

    def token(self, subject="provider-subject", nonce="", *, header=None, untrusted=False, **changes):
        now = int(time.time())
        claims = {"iss": "https://accounts.google.com", "aud": CLIENT, "sub": subject,
                  "iat": now, "exp": now + 3600, "nonce": nonce,
                  "email": "fixture@example.invalid", "email_verified": True, **changes}

        def encode(value):
            return base64.urlsafe_b64encode(value).rstrip(b"=")

        if header is None:
            header = {"alg": "RS256", "kid": "trusted-key"}
        encoded_header = header if isinstance(header, bytes) else json.dumps(header, separators=(",", ":")).encode()
        payload = json.dumps(claims, separators=(",", ":")).encode()
        message = encode(encoded_header) + b"." + encode(payload)
        key = self.rogue_key if untrusted else self.key
        result = subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(key)], input=message,
                                capture_output=True, timeout=10, check=True)
        return (message + b"." + encode(result.stdout)).decode()


@contextmanager
def server(binary, database, certificates, client_ids):
    env = {key: value for key, value in os.environ.items() if not key.startswith("MESH_")}
    env.update(MESH_ADDR="127.0.0.1:0", MESH_DB=str(database), MESH_TOKEN_SECRET_HEX="00" * 32,
               MESH_GOOGLE_SIGNIN_CLIENT_IDS=client_ids, MESH_GOOGLE_SIGNIN_TEST_CERTIFICATES_URL=certificates,
               MESH_NODE_SYNC_INTERVAL_SECONDS="0")
    with subprocess.Popen([binary], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        try:
            assert select.select([process.stdout], [], [], 10)[0], "native server did not report its port"
            port = process.stdout.readline()
            assert port.strip().isdigit(), f"invalid native server port: {port!r}"
            with closing(HTTPConnection("127.0.0.1", int(port), timeout=10)) as connection:
                yield connection
        finally:
            process.terminate()
            try:
                _, errors = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise
            assert process.returncode == 0, errors


def request(connection, method, path, status, *, body=None, bearer=None, headers=None):
    headers = dict(headers or {})
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"
    if isinstance(body, dict):
        body = json.dumps(body).encode()
    connection.request(method, path, body, headers)
    response = connection.getresponse()
    data = response.read()
    assert response.status == status, (method, path, response.status, data[:250], status)
    if path in (LOOKUP, LINK, UNLINK) and status != 405:
        assert response.getheader("Cache-Control") == "private, no-store", response.headers
    return response, json.loads(data) if data else None


def login(connection, oracle):
    keys = sign_with_oracle(oracle, "keypair")
    user = hashlib.sha256(base64.b64decode(keys["PublicKey"])).hexdigest()
    path = "/api/v1/sync/login"
    _, challenge = request(connection, "GET", f"/api/v1/sync/challenge?user_id={user}", 200)
    body = json.dumps({"client_id": "oidc-fixture", "public_key": keys["PublicKey"]}).encode()
    message = f"ksync-sync-v1\nPOST\n{path}\n{hashlib.sha256(body).hexdigest()}\n{challenge['nonce']}\n".encode()
    signed = sign_with_oracle(oracle, "sign", PrivateKey=keys["PrivateKey"], Message=base64.b64encode(message).decode())
    _, response = request(connection, "POST", path, 200, body=body,
                          headers={"X-Ksync-User": user, "X-Ksync-Signature": signed["Signature"]})
    return user, response["auth_token"]


def account_nonce(account):
    return hashlib.sha256(f"waozi-google-link-v1\n{account}".encode()).hexdigest()


def lookup(connection, google_token, status=200, **kwargs):
    return request(connection, "POST", LOOKUP, status, body={"id_token": google_token}, **kwargs)[1]


def link(connection, bearer, google_token, status=200):
    return request(connection, "POST", LINK, status, bearer=bearer, body={"id_token": google_token})[1]


def check_disabled(binary, oracle, path, certificates, provider):
    with server(binary, path, certificates, " , ") as connection:
        assert request(connection, "GET", METHODS, 200)[1] == {}
        assert request(connection, "HEAD", METHODS, 200)[1] is None
        assert lookup(connection, "a.b.c", 404) == DISABLED
        assert link(connection, None, "a.b.c", 404) == DISABLED
        _, bearer = login(connection, oracle)
        assert request(connection, "GET", LINK, 200, bearer=bearer)[1] == {"linked": False}
        assert request(connection, "POST", UNLINK, 200, bearer=bearer)[1] == {"linked": False}
        assert provider.hits == 0


def check_requests(connection, provider, first, second):
    account, bearer = first
    other, other_bearer = second
    assert request(connection, "GET", METHODS, 200)[1] == {"google_client_id": CLIENT}
    assert request(connection, "GET", LINK, 401)[1] == {"error": "bearer token required"}
    assert request(connection, "POST", UNLINK, 401)[1] == {"error": "bearer token required"}
    reply, _ = request(connection, "GET", LOOKUP, 405)
    assert reply.getheader("Allow") == "POST"
    first_proof = provider.token(nonce=account_nonce(account))
    assert link(connection, None, first_proof, 401) == {"error": "bearer token required"}
    assert link(connection, "invalid", first_proof, 401) == {"error": "invalid bearer token"}
    for body in (b"{", b"[]", b'{"id_token":7}', b'{"id_token":"a","extra":"b"}',
                 b'{"id_token":"a","id_token":"b"}', b" " * 4097):
        assert request(connection, "POST", LINK, 400, bearer=bearer, body=body)[1] == {"error": "invalid google token"}
    assert provider.hits == 0
    assert lookup(connection, provider.token(nonce="browser-random-nonce"), 404) == NO_LINK
    assert provider.seen == [("/certificates", None)]
    assert link(connection, other_bearer, first_proof, 401) == BAD_TOKEN
    assert link(connection, bearer, provider.token(), 401) == BAD_TOKEN
    assert link(connection, bearer, first_proof) == {"linked": True}
    assert link(connection, bearer, first_proof) == {"linked": True}
    assert link(connection, other_bearer, provider.token(nonce=account_nonce(other)), 409) == {
        "error": "google account linked to another account"}
    assert request(connection, "GET", LINK, 200, bearer=bearer)[1] == {"linked": True}
    assert request(connection, "HEAD", LINK, 200, bearer=bearer)[1] is None
    assert request(connection, "GET", LINK, 200, bearer=other_bearer)[1] == {"linked": False}
    assert request(connection, "GET", LINK, 401, bearer=bearer,
                   headers={"X-Mesh-User": other})[1] == {"error": "token user mismatch"}
    assert lookup(connection, provider.token(nonce="unrelated", iss="accounts.google.com", aud=SECOND)) == {"account_id": account}
    assert lookup(connection, provider.token("different-subject"), 404) == NO_LINK
    for proof in (
        "a.b", provider.token(untrusted=True), provider.token(aud="different-client"),
        provider.token(aud=[CLIENT]), provider.token(iss="https://example.invalid"),
        provider.token(subject=""), provider.token(exp=int(time.time()) - 120),
        provider.token(iat=int(time.time()) + 120), provider.token(exp="4102444800"),
        provider.token(header={"alg": "none", "kid": "trusted-key"}),
        provider.token(header={"alg": "RS256", "kid": "unknown-key"}),
        provider.token(header=b'{"alg":"RS256","kid":"trusted-key","kid":"trusted-key"}'),
    ):
        assert lookup(connection, proof, 401) == BAD_TOKEN
    assert provider.hits == 1


def check_database(connection, database, provider, account, bearer):
    def links():
        return database.execute("SELECT typeof(subject_hash),CAST(subject_hash AS BLOB),user_id_hash "
                                "FROM server_account_google").fetchall()

    secret, = database.execute("SELECT secret FROM server_account_backup_secret WHERE id=1").fetchone()
    expected = hmac.digest(secret, b"account-google-subject\nprovider-subject", "sha256")
    assert links() == [("text", expected, account)]
    replacement = provider.token("replacement", nonce=account_nonce(account))
    database.execute("CREATE TRIGGER fail_link AFTER UPDATE ON server_account_google "
                     "BEGIN SELECT RAISE(FAIL,'fixture failure'); END")
    assert link(connection, bearer, replacement, 500) == UNAVAILABLE
    assert links() == [("text", expected, account)]
    database.execute("DROP TRIGGER fail_link")
    database.execute("CREATE TRIGGER fail_unlink AFTER DELETE ON server_account_google "
                     "BEGIN SELECT RAISE(FAIL,'fixture failure'); END")
    assert request(connection, "POST", UNLINK, 500, bearer=bearer)[1] == UNAVAILABLE
    assert links() == [("text", expected, account)]
    database.execute("DROP TRIGGER fail_unlink")
    compressed = gzip.compress(json.dumps({"id_token": replacement}).encode())
    assert request(connection, "POST", LINK, 200, bearer=bearer, body=compressed,
                   headers={"Content-Encoding": "gzip"})[1] == {"linked": True}
    assert lookup(connection, provider.token(), 404) == NO_LINK
    assert lookup(connection, provider.token("replacement")) == {"account_id": account}
    provider.status = 503
    assert request(connection, "POST", UNLINK, 200, bearer=bearer)[1] == {"linked": False}
    assert request(connection, "POST", UNLINK, 200, bearer=bearer)[1] == {"linked": False}
    assert links() == []
    assert lookup(connection, provider.token("replacement"), 404) == NO_LINK
    assert provider.hits == 1


def check_outage_and_rate(binary, path, certificates, provider):
    provider.status = 302
    before = provider.hits
    proof = provider.token()
    with server(binary, path, certificates, CLIENT) as connection:
        address = {"X-Forwarded-For": "192.0.2.31"}
        for _ in range(20):
            assert lookup(connection, proof, 401, headers=address) == BAD_TOKEN
        response, reply = request(connection, "POST", LOOKUP, 429, body={"id_token": proof}, headers=address)
        assert response.getheader("Retry-After") == "60" and reply == {"error": "too many attempts"}
        assert lookup(connection, proof, 401, headers={"X-Forwarded-For": "192.0.2.32"}) == BAD_TOKEN
    assert provider.hits == before + 1 and provider.seen[-1] == ("/certificates", None)


def main(binary, oracle):
    binary, oracle = str(Path(binary).resolve()), str(Path(oracle).resolve())
    with tempfile.TemporaryDirectory(prefix="google-account-", dir=Path(binary).parent) as temporary:
        directory = Path(temporary)
        provider = CertificateProvider(directory)
        with provider.serve() as certificates:
            check_disabled(binary, oracle, directory / "disabled.sqlite", certificates, provider)
            path = directory / "mesh.sqlite"
            with server(binary, path, certificates, f" ,{CLIENT}, {SECOND} ,") as connection:
                first, second = login(connection, oracle), login(connection, oracle)
                check_requests(connection, provider, first, second)
                with closing(sqlite3.connect(path, isolation_level=None)) as database:
                    check_database(connection, database, provider, *first)
            check_outage_and_rate(binary, directory / "outage.sqlite", certificates, provider)
    print("native Google account HTTP tests passed")


if __name__ == "__main__":
    main(*sys.argv[1:])
