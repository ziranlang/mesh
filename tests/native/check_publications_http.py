"""Exercise native publication routes through a fresh loopback Mesh server."""

import base64
import gzip
import hashlib
import json
import os
import re
import select
import sqlite3
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from http.client import HTTPConnection
from itertools import chain, repeat
from pathlib import Path
from urllib.parse import urlencode


PATH = "/api/v1/publications"
LIMIT = 1024
ISSUER_SEED = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
MISSING = {"error": "publication not found"}
INVALID = {"error": "invalid publication"}
THROTTLED = {"error": "publication rate limit exceeded"}


@contextmanager
def server(binary, database):
    env = {key: value for key, value in os.environ.items() if not key.startswith(("MESH_", "MONERO_"))}
    env.update(MESH_ADDR="127.0.0.1:0", MESH_DB=str(database), MESH_TOKEN_SECRET_HEX="00" * 32,
               MESH_ACCOUNT_STORAGE_LIMIT_BYTES=str(LIMIT), MESH_STORAGE_PLANS_ENABLED="1",
               MESH_TOKEN_ISSUER_PRIVATE_KEY_HEX=ISSUER_SEED,
               MESH_TOKEN_DIRECT_PURCHASES_ENABLED="true", MONERO_RATE_ATOMIC_AMOUNT="1000000000000",
               MONERO_RATE_TOKEN_UNITS="10000000000", MONERO_WALLET_RPC_URL="http://127.0.0.1:1", MONERO_NETWORK="testnet",
               MESH_MAX_BODY_BYTES="8192", MESH_NODE_SYNC_INTERVAL_SECONDS="0")
    with subprocess.Popen([binary], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        try:
            assert select.select([process.stdout], [], [], 10)[0], "native server did not report its port"
            port = process.stdout.readline()
            assert port.strip().isdigit(), f"invalid server port: {port!r}"
            connection = HTTPConnection("127.0.0.1", int(port), timeout=10)
            try:
                yield connection
            finally:
                connection.close()
        finally:
            process.terminate()
            try:
                _, errors = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise
            assert process.returncode == 0, errors


def request(connection, method, path, status, body=None, token=None, headers=None):
    sent = dict(headers or {})
    if token is not None:
        sent["Authorization"] = f"Bearer {token}"
    if isinstance(body, dict):
        body = json.dumps(body, separators=(",", ":")).encode()
    connection.request(method, path, body, sent)
    response = connection.getresponse()
    raw = response.read()
    assert response.status == status, (method, path, response.status, raw[:300], status)
    if response.getheader("Content-Encoding") == "gzip" and raw:
        raw = gzip.decompress(raw)
    return response, raw, json.loads(raw) if raw else None


def oracle_call(oracle, operation, **values):
    payload = json.dumps([{"Operation": operation, **values}]).encode()
    process = subprocess.run([oracle], input=payload, capture_output=True, timeout=10, check=True)
    reply, = json.loads(process.stdout)
    assert not reply["Error"], reply["Error"]
    return reply


def login(connection, oracle, database, alias):
    keys = oracle_call(oracle, "keypair")
    account = hashlib.sha256(base64.b64decode(keys["PublicKey"])).hexdigest()
    path = "/api/v1/sync/login"
    _, _, challenge = request(connection, "GET", f"/api/v1/sync/challenge?user_id={account}", 200)
    payload = json.dumps({"client_id": "publications-test", "public_key": keys["PublicKey"]}).encode()
    digest = hashlib.sha256(payload).hexdigest()
    message = f"ksync-sync-v1\nPOST\n{path}\n{digest}\n{challenge['nonce']}\n".encode()
    signed = oracle_call(oracle, "sign", PrivateKey=keys["PrivateKey"],
                         Message=base64.b64encode(message).decode())
    _, _, session = request(connection, "POST", path, 200, payload,
                            headers={"X-Ksync-User": account, "X-Ksync-Signature": signed["Signature"]})
    database.execute("UPDATE server_users SET alias=? WHERE user_id_hash=?", (alias, account))
    return account, session["auth_token"]


def draft(title="Level Pack", visibility="public", **fields):
    return {"format": "pushy.levels", "title": title, "visibility": visibility,
            "document": {"levels": [1, 2]}, **fields}


def publish(connection, token, payload, status=200, **kwargs):
    response, _, reply = request(connection, "POST", PATH, status, payload, token, **kwargs)
    assert response.getheader("Cache-Control") == "private, no-store"
    return reply["publication"] if status == 200 else reply


def fetch(connection, token, publication, status=200, **kwargs):
    response, raw, reply = request(connection, "GET", f"{PATH}/{publication}", status, token=token, **kwargs)
    cache = "public, max-age=15" if token is None else "private, no-store"
    assert response.getheader("Cache-Control") == cache
    assert "authorization" in response.getheader("Vary", "").lower()
    return raw, reply


def list_items(connection, token=None, status=200, **query):
    path = f"{PATH}?{urlencode(query)}" if query else PATH
    response, _, reply = request(connection, "GET", path, status, token=token)
    cache = "public, max-age=15" if token is None else "private, no-store"
    assert response.getheader("Cache-Control") == cache
    return reply["publications"] if status == 200 else reply


def storage(database, account):
    return database.execute("SELECT used_bytes FROM server_storage_usage WHERE user_id_hash=?", (account,)).fetchone()[0]


def check_visibility(connection, database, author, friend, stranger):
    account, owner = author
    friend_id, friend_token = friend
    stranger_id, stranger_token = stranger
    pair = tuple(sorted((account, friend_id)))
    database.execute("INSERT INTO server_friendships(user_id_a,user_id_b) VALUES (?,?)", pair)
    before = storage(database, account)
    document = b'{ "levels" : [1, {"nested": [2,3]}], "escaped": "\\u00e9", "opaque": 1E309 }'
    payload = b'{ "DOCUMENT": ' + document + b', "Format":"pushy.levels", "title":"Classic", "visibility":"public" }'
    created = publish(connection, owner, gzip.compress(payload), headers={"Content-Encoding": "gzip"})
    public_id = created["id"]
    assert re.fullmatch("[0-9a-f]{32}", public_id), created
    assert created == {"id": public_id, "author": {"public_id": account, "alias": "level_author"},
                       "format": "pushy.levels", "title": "Classic", "levels": 2, "visibility": "public",
                       "updated_at": created["updated_at"], "mine": True}, created
    assert abs(created["updated_at"] - int(time.time())) < 60, created
    assert storage(database, account) - before == 32 + len("pushy.levelsClassic") + len(document)
    raw, public = fetch(connection, None, public_id, headers={"Accept-Encoding": "gzip"})
    assert document in raw and public["document"]["escaped"] == "é" and not public["publication"]["mine"]
    friends = publish(connection, owner, draft("For Friends", "friends"))["id"]
    private = publish(connection, owner, draft("Private", "private", document={"levels": 7}))["id"]
    other = publish(connection, owner, draft("Other Format", format="other.format-2"))["id"]
    stranger_public = publish(connection, stranger_token, draft("Stranger's"))["id"]
    for index, identity in enumerate((public_id, friends, private, other, stranger_public), start=100):
        database.execute("UPDATE server_publications SET updated_at=? WHERE id=?", (index, identity))
    for token, visible in ((owner, {public_id, friends, private}), (friend_token, {public_id, friends}),
                           (stranger_token, {public_id}), (None, {public_id})):
        for identity in (public_id, friends, private):
            _, reply = fetch(connection, token, identity, 200 if identity in visible else 404)
            if identity in visible:
                assert reply["publication"]["id"] == identity and reply["publication"]["mine"] == (token == owner)
            else:
                assert reply == MISSING
    assert fetch(connection, owner, private)[1]["publication"]["levels"] == 0
    assert [item["id"] for item in list_items(connection)] == [stranger_public, other, public_id]
    for reference in (" \u2003@LEVEL_AUTHOR ", account, account.upper()):
        assert [item["id"] for item in list_items(connection, friend_token, author=reference)] == [other, friends, public_id]
    assert [item["id"] for item in list_items(connection, owner, author="level_author")] == [other, private, friends, public_id]
    assert [item["id"] for item in list_items(connection, author=account)] == [other, public_id]
    assert [item["id"] for item in list_items(connection, friend_token, author=account, format="\u2003pushy.levels  ")] == [friends, public_id]
    assert list_items(connection, author=stranger_id, format="other.format-2") == []
    assert list_items(connection, author="unknown_account") == []
    assert list_items(connection, status=400, format="Bad_Format") == {"error": "invalid format"}
    database.execute("DELETE FROM server_friendships WHERE user_id_a=? AND user_id_b=?", pair)
    assert fetch(connection, friend_token, friends, 404)[1] == MISSING
    assert [item["id"] for item in list_items(connection, friend_token, author=account)] == [other, public_id]
    for token in (friend_token, stranger_token):
        assert publish(connection, token, draft("Not Yours", id=public_id), 404) == MISSING
        assert request(connection, "DELETE", f"{PATH}/{public_id}", 404, token=token)[2] == MISSING
    assert request(connection, "DELETE", f"{PATH}/{public_id}", 401)[2] == {"error": "bearer token required"}
    assert request(connection, "POST", PATH, 401, draft())[2] == {"error": "bearer token required"}
    assert fetch(connection, "invalid", public_id, 401)[1] == {"error": "invalid bearer token"}
    assert fetch(connection, owner, public_id, 401, headers={"X-Mesh-User": friend_id})[1] == {"error": "token user mismatch"}
    assert fetch(connection, owner, "", 404)[1] == MISSING
    assert fetch(connection, owner, f"{public_id}/extra", 404)[1] == MISSING
    assert fetch(connection, owner, f"A{public_id[1:]}", 404)[1] == MISSING
    response, raw, _ = request(connection, "HEAD", f"{PATH}/{public_id}", 200)
    assert raw == b"" and response.getheader("Cache-Control") == "public, max-age=15"
    response, _, _ = request(connection, "PUT", PATH, 405)
    assert set(response.getheader("Allow").split(", ")) == {"GET", "HEAD", "POST"}
    response, _, _ = request(connection, "POST", f"{PATH}/{public_id}", 405)
    assert set(response.getheader("Allow").split(", ")) == {"GET", "HEAD", "DELETE"}
    original_creation = database.execute("SELECT created_at FROM server_publications WHERE id=?", (public_id,)).fetchone()
    revised = publish(connection, owner, draft("Revised", id=public_id, document={"levels": [42]}))
    assert revised["id"] == public_id and revised["levels"] == 1 and revised["mine"]
    assert database.execute("SELECT created_at FROM server_publications WHERE id=?", (public_id,)).fetchone() == original_creation
    assert request(connection, "DELETE", f"{PATH}/{public_id}", 200, token=owner)[2] == {"deleted": True}
    assert request(connection, "DELETE", f"{PATH}/{public_id}", 404, token=owner)[2] == MISSING
    assert fetch(connection, None, public_id, 404)[1] == MISSING


def check_invalid_and_limits(connection, database, author, friend, stranger):
    account, owner = author
    friend_id, friend_token = friend
    stranger_id, stranger_token = stranger
    for body in (b"", b"{", b"{}{}", b"{}\x00", b'"scalar"', draft(format="Bad_Format"), draft(format="a" * 65),
                 draft("\u2003 \u2003"), draft("é" * 41), draft("line\nfeed"), draft("C1\u0085control"),
                 draft(title=7), draft(visibility="Public"), draft(document=[]), draft(document=None),
                 b'{"format":"valid","title":"\xff","visibility":"public","document":{}}'):
        assert publish(connection, owner, body, 400) == INVALID, repr(body)[:200]
    assert publish(connection, owner, draft(id="f" * 32), 404) == MISSING
    assert publish(connection, owner, draft(id="a" * 33), 404) == MISSING
    assert publish(connection, owner, b"{}", 400, headers={"Content-Encoding": "wrong"}) == INVALID
    assert publish(connection, owner, gzip.compress(b"{}"), 400,
                   headers={"Content-Encoding": "gzip", "content-encoding": "identity"}) == INVALID

    # Quota measures the before/after values updated by the live SQLite triggers.
    small = publish(connection, friend_token, draft("Small"))["id"]
    used = storage(database, friend_id)
    prior = database.execute("SELECT * FROM server_publications WHERE author=?", (friend_id,)).fetchall()
    big = draft("Large", document={"padding": "x" * (LIMIT + 1)})
    for payload in (big, dict(big, id=small)):
        assert publish(connection, friend_token, payload, 413) == {
            "error": "account storage limit reached", "code": "storage_full",
            "account_storage": {"used_bytes": used, "limit_bytes": LIMIT}}
        assert storage(database, friend_id) == used
        assert database.execute("SELECT * FROM server_publications WHERE author=?", (friend_id,)).fetchall() == prior

    headers = {"Accept-Encoding": "identity;q=0"}
    assert publish(connection, friend_token, draft("Unacceptable encoding"), 406, headers=headers) == {
        "error": "no acceptable content encoding"}
    assert database.execute("SELECT * FROM server_publications WHERE author=?", (friend_id,)).fetchall() == prior

    for action, statement in (("UPDATE", "AFTER UPDATE"), ("DELETE", "AFTER DELETE")):
        database.execute(f"CREATE TRIGGER fail_publication {statement} ON server_publications "
                         "BEGIN SELECT RAISE(FAIL,'fixture failure'); END")
        if action == "UPDATE":
            failure = publish(connection, friend_token, draft("Trigger failure", id=small), 500)
        else:
            failure = request(connection, "DELETE", f"{PATH}/{small}", 500, token=friend_token)[2]
        assert failure == {"error": "publications unavailable"}
        assert storage(database, friend_id) == used
        assert database.execute("SELECT * FROM server_publications WHERE author=?", (friend_id,)).fetchall() == prior
        database.execute("DROP TRIGGER fail_publication")
    assert publish(connection, friend_token, draft("Recovered", id=small))["id"] == small

    # The dedicated upload bound accepts documents larger than sync's 8 KiB.
    now = int(time.time())
    database.execute("INSERT INTO server_storage_plans(user_id,period,expires_at,size) VALUES (?,'monthly',?,'1gb')",
                     (friend_id, now + 3600))
    padding = "o" * (4 * 1024 * 1024)
    large = publish(connection, friend_token, draft("é" * 40, "private", id=small, format="a" * 64,
                                                  document={"padding": padding}))
    assert large["title"] == "é" * 40 and large["levels"] == 0
    assert fetch(connection, friend_token, small, headers={"Accept-Encoding": "gzip"})[1]["document"] == {"padding": padding}
    database.execute("UPDATE server_storage_plans SET expires_at=? WHERE user_id=?", (now - 1, friend_id))
    assert publish(connection, friend_token, draft("No Plan", document={"padding": "x"}), 413)["code"] == "storage_full"
    assert publish(connection, friend_token, draft("Shrunk", id=small))["id"] == small
    assert storage(database, friend_id) < LIMIT

    # Two simultaneous growth requests share the same quota. Only one fits.
    def attempt(title):
        with closing(HTTPConnection(connection.host, connection.port, timeout=10)) as client:
            payload = json.dumps(draft(title, document={"padding": "x" * 700})).encode()
            client.request("POST", PATH, payload, {"Authorization": f"Bearer {friend_token}"})
            response = client.getresponse()
            return response.status, json.loads(response.read())

    with ThreadPoolExecutor(max_workers=2) as writers:
        results = list(writers.map(attempt, ("First writer", "Second writer")))
    accepted, refused = sorted(results, key=lambda result: result[0])
    assert accepted[0] == 200 and refused[0] == 413, results
    assert refused[1]["account_storage"]["used_bytes"] == storage(database, friend_id)

    mebibyte = b" " * (1024 * 1024)
    oversized = chain(repeat(mebibyte, 64), (b" ",))
    assert publish(connection, friend_token, oversized, 413, headers={"Content-Length": str(64 * len(mebibyte) + 1)}) == {
        "error": "publication too large"}

    # A pre-existing account may hold more publications than a single page.
    ids = [f"{i:032x}" for i in range(1, 151)]
    database.executemany("INSERT INTO server_publications(id,author,format,title,visibility,levels,document,created_at,updated_at) "
                         "VALUES (?,?,'bulk','Bulk','private',0,'{}',1,1)", ((identity, stranger_id) for identity in ids))
    database.execute("INSERT INTO server_storage_plans(user_id,period,expires_at) VALUES (?,'monthly',?)",
                     (stranger_id, now + 3600))
    recent = publish(connection, stranger_token, draft("More than 150"))["id"]
    listed = list_items(connection, stranger_token, author=stranger_id)
    assert listed[0]["id"] == recent and [item["id"] for item in listed[2:]] == list(reversed(ids[-48:])), listed[:3]
    assert all(item["mine"] for item in listed)
    assert list_items(connection, owner, author=stranger_id, format="bulk") == []
    assert storage(database, account) > 0


def check_rates(connection, database, oracle):
    account, token = login(connection, oracle, database, "rate_writer")
    for _ in range(60):
        assert publish(connection, token, b"{", 400) == INVALID
    assert publish(connection, token, draft(), 429) == THROTTLED
    assert request(connection, "DELETE", f"{PATH}/{'f' * 32}", 429, token=token)[2] == THROTTLED
    for _ in range(120):
        list_items(connection, token, author=account)
    assert list_items(connection, token, status=429) == THROTTLED
    assert fetch(connection, token, "f" * 32, 429)[1] == THROTTLED
    for _ in range(120):
        request(connection, "GET", PATH, 200, headers={"X-Forwarded-For": "192.0.2.4"})
    assert request(connection, "GET", PATH, 429, headers={"X-Forwarded-For": "192.0.2.4"})[2] == THROTTLED
    assert request(connection, "GET", PATH, 200, headers={"X-Forwarded-For": "192.0.2.5"})[2]["publications"]


def main(binary, oracle):
    binary, oracle = str(Path(binary).resolve()), str(Path(oracle).resolve())
    with tempfile.TemporaryDirectory(prefix="publications-", dir=Path(binary).parent) as directory:
        path = Path(directory) / "mesh.db"
        with server(binary, path) as connection:
            with closing(sqlite3.connect(path, isolation_level=None)) as database:
                author = login(connection, oracle, database, "level_author")
                friend = login(connection, oracle, database, "level_friend")
                stranger = login(connection, oracle, database, "level_stranger")
                check_visibility(connection, database, author, friend, stranger)
                check_invalid_and_limits(connection, database, author, friend, stranger)
                check_rates(connection, database, oracle)
    print("native publication HTTP tests passed")


if __name__ == "__main__":
    main(*sys.argv[1:])
