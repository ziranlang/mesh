"""Exercise all native feedback operations on a disposable loopback node."""

import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import gzip
import hashlib
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import select
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory

from check_http_listener import sign_with_oracle


REPORTS = "/api/v1/feedback"
INBOX = "/api/v1/admin/feedback"
REPLY = "/api/v1/admin/feedback/reply"
ADMIN = "fixture-base-admin"
OPERATOR = "fixture-feedback-operator"


@contextmanager
def node(binary, directory, **settings):
    database = directory / "mesh.sqlite"
    env = {key: value for key, value in os.environ.items() if not key.startswith("MESH_")}
    env.update({
        "MESH_ADDR": "127.0.0.1:0",
        "MESH_DB": str(database),
        "MESH_NODE_IDENTITY_KEY_FILE": str(directory / "node.key"),
        "MESH_TOKEN_SECRET_HEX": "00" * 32,
        "MESH_LAN_ONLY": "1",
        "MESH_LAN_NETWORKS": "127.0.0.1/8",
        "MESH_BASE_URL": "http://127.0.0.1",
        "MESH_NODE_SYNC_INTERVAL_SECONDS": "0",
        "MESH_MAX_BODY_BYTES": "8192",
        **settings,
    })
    with subprocess.Popen([binary], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as server:
        try:
            ready, _, _ = select.select([server.stdout], [], [], 5)
            assert ready, "feedback node did not report its bound port"
            started = server.stdout.readline()
            assert started, server.stderr.read(1000)
            yield int(started), database
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


def send(port, method, route, *, headers=None, body=None):
    if isinstance(body, dict):
        body = json.dumps(body, ensure_ascii=False).encode()
    with closing(HTTPConnection("127.0.0.1", port, timeout=5)) as connection:
        connection.request(method, route, body, {"Content-Type": "application/json", **(headers or {})})
        response = connection.getresponse()
        content = response.read()
        if response.getheader("Content-Encoding") == "gzip":
            content = gzip.decompress(content)
        content = json.loads(content) if content else None
        return response.status, content


def request(port, method, route, *, headers=None, body=None, expected=200):
    status, content = send(port, method, route, headers=headers, body=body)
    assert status == expected, (method, route, status, expected, str(content)[:300])
    return content


def login(port, oracle, client):
    keys = sign_with_oracle(oracle, "keypair")
    user = hashlib.sha256(base64.b64decode(keys["PublicKey"])).hexdigest()
    challenge = request(port, "GET", "/api/v1/sync/challenge?user_id=" + user)
    route = "/api/v1/sync/login"
    payload = json.dumps({"client_id": client, "public_key": keys["PublicKey"]}).encode()
    digest = hashlib.sha256(payload).hexdigest()
    message = f"daochi-sync-v1\nPOST\n{route}\n{digest}\n{challenge['nonce']}\n".encode()
    signed = sign_with_oracle(oracle, "sign", PrivateKey=keys["PrivateKey"],
                              Message=base64.b64encode(message).decode())
    session = request(port, "POST", route, headers={"X-Mesh-User": user, "X-Mesh-Signature": signed["Signature"]},
                      body=payload)
    return user, {"Authorization": f"Bearer {session['auth_token']}", "X-Mesh-User": user}


def report_id(number):
    return f"{number:064x}"


def check_reports(port, database, owner, owner_auth, other, other_auth, operator):
    assert request(port, "GET", REPORTS, headers=owner_auth) == {"feedback": None}
    assert request(port, "GET", INBOX, headers=operator) == {"feedback": None}
    assert request(port, "GET", REPORTS, expected=401) == {"error": "bearer token required"}
    assert request(port, "GET", REPORTS, headers=owner_auth | {"X-Mesh-User": other}, expected=401) == {
        "error": "token user mismatch",
    }
    assert request(port, "GET", INBOX, headers=owner_auth, expected=401) == {"error": "admin token required"}
    assert request(port, "GET", INBOX, headers={"X-Mesh-Admin": ADMIN}, expected=401) == {"error": "admin token required"}
    assert request(port, "HEAD", REPORTS, headers=owner_auth) is None
    assert request(port, "PUT", REPORTS, headers=owner_auth, expected=405) is None
    multilingual = "日本語 / Español / العربية 🙂\n" * 350
    maximum = multilingual + "x" * (32768 - len(multilingual.encode()))
    first = {"id": report_id(1), "app_id": "inbe", "title": "Expanded report", "message": maximum, "context": "fixture"}
    escaped = first | {"id": report_id(2), "title": "\x01" * 160,
                       "message": "\x00" + "\x01" * 32767, "context": "\x01" * 2048}
    assert request(port, "POST", REPORTS, body=first, expected=401) == {"error": "bearer token required"}
    for report in (first, escaped):
        assert request(port, "POST", REPORTS, headers=owner_auth, body=report) == report["id"]
        changed = report | {"message": "attempted replacement", "account_id": other}
        assert request(port, "POST", REPORTS, headers=owner_auth, body=changed) == report["id"]
    body = json.dumps(first, ensure_ascii=False).encode().ljust(262144, b" ")
    assert request(port, "POST", REPORTS, headers=owner_auth, body=body) == first["id"]
    assert request(port, "POST", REPORTS, headers=owner_auth, body=body + b" ", expected=400) == {"error": "invalid feedback"}
    gzip_headers = owner_auth | {"Content-Encoding": "gzip"}
    assert request(port, "POST", REPORTS, headers=gzip_headers, body=gzip.compress(body)) == first["id"]
    assert request(port, "POST", REPORTS, headers=gzip_headers, body=gzip.compress(body + b" "), expected=400) == {
        "error": "invalid feedback",
    }
    for invalid in (first | {"title": "\u2003"}, first | {"message": maximum + "界"},
                    first | {"context": "x" * 2049}, first | {"app_id": "other"}, first | {"reply": False}, b"{"):
        assert request(port, "POST", REPORTS, headers=owner_auth, body=invalid, expected=400) == {"error": "invalid feedback"}

    assert request(port, "GET", REPORTS, headers=other_auth) == {"feedback": None}
    same_id = first | {"title": "Other account", "message": "Other account report", "account_id": owner}
    assert request(port, "POST", REPORTS, headers=other_auth, body=same_id) == first["id"]
    other_report, = request(port, "GET", REPORTS, headers=other_auth)["feedback"]
    assert other_report["message"] == same_id["message"] and "account_id" not in other_report
    owner_reports = request(port, "GET", REPORTS, headers=owner_auth | {"Accept-Encoding": "gzip"})["feedback"]
    assert len(owner_reports) == 2
    for item in owner_reports:
        report = {first["id"]: first, escaped["id"]: escaped}[item["id"]]
        assert set(item) == {*report, "reply", "created_at"}
        assert all(item[key] == value for key, value in report.items())
        assert item["reply"] == "" and item["created_at"]
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("UPDATE server_users SET alias=? WHERE user_id_hash=?", ("Feedback fixture", owner))
        assert connection.execute("SELECT message FROM server_app_feedback WHERE account_id=? AND id=?",
                                  (owner, escaped["id"])).fetchone() == (escaped["message"],)
    inbox = request(port, "GET", INBOX, headers=operator)["feedback"]
    assert len(inbox) == 3
    assert {item["account_id"] for item in inbox} == {owner, other}
    assert all(item["account_alias"] == "Feedback fixture" for item in inbox if item["account_id"] == owner)

    for number in range(3, 20):
        report = first | {"id": report_id(number), "message": "quota fixture"}
        assert request(port, "POST", REPORTS, headers=owner_auth, body=report) == report["id"]
    # Verify both results without assuming which request claims the last slot.
    def submit_for_last_slot(number):
        report = first | {"id": report_id(number), "message": "quota race"}
        return send(port, "POST", REPORTS, headers=owner_auth, body=report)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit_for_last_slot, (20, 21)))
    assert sorted(status for status, _ in results) == [200, 429], results
    for number, (status, value) in zip((20, 21), results):
        assert value == (report_id(number) if status == 200 else {"error": "daily feedback allowance reached"})
    assert request(port, "POST", REPORTS, headers=owner_auth, body=first) == first["id"]
    assert len(request(port, "GET", REPORTS, headers=owner_auth)["feedback"]) == 20
    return first


def check_replies(port, database, first, owner, owner_auth, other, other_auth, operator):
    legacy = {"id": first["id"], "account_id": owner, "reply": "First response"}
    assert request(port, "POST", REPLY, headers=owner_auth, body=legacy, expected=401) == {"error": "admin token required"}
    assert request(port, "POST", REPLY, headers=operator, body=legacy) == first["id"]
    assert request(port, "POST", REPLY, headers=operator, body=legacy | {"id": report_id(999)}, expected=404) == {
        "error": "feedback not found",
    }
    page = REPORTS + "?replies=1"
    assert request(port, "GET", page, headers=other_auth) == {"feedback": None}
    legacy["reply"] = "Corrected first response"
    body = json.dumps(legacy).encode().ljust(8192, b" ")
    assert request(port, "POST", REPLY, headers=operator, body=body) == first["id"]
    assert request(port, "POST", REPLY, headers=operator, body=body + b" ", expected=400) == {"error": "invalid feedback reply"}
    later = [legacy | {"reply_id": report_id(number), "reply": text}
             for number, text in ((801, "follow-up"), (802, "é" * 2048))]
    for reply in later:
        assert request(port, "POST", REPLY, headers=operator, body=reply) == first["id"]
        assert request(port, "POST", REPLY, headers=operator, body=reply) == first["id"]
    for change in ({"reply": "replacement"}, {"id": report_id(2)}):
        assert request(port, "POST", REPLY, headers=operator, body=later[0] | change, expected=409) == {
            "error": "reply ID already used",
        }
    for invalid in (later[0] | {"reply_id": "A" * 64}, later[1] | {"reply": later[1]["reply"] + "!"},
                    legacy | {"reply": "\u00a0"}):
        assert request(port, "POST", REPLY, headers=operator, body=invalid, expected=400) == {"error": "invalid feedback reply"}
    assert request(port, "POST", REPLY, headers=operator, body=later[0] | {"account_id": other}) == first["id"]
    for offset in ("-1", "1234567", "1a", "%20"):
        assert request(port, "GET", page + "&offset=" + offset, headers=owner_auth, expected=400) == {
            "error": "invalid feedback offset",
        }
    stored = request(port, "GET", page, headers=owner_auth)["feedback"]
    assert [item["id"] for item in stored] == [first["id"], later[0]["reply_id"]]
    assert [item["reply"] for item in stored] == [legacy["reply"], later[0]["reply"]]
    last, = request(port, "GET", page + "&offset=000002", headers=owner_auth)["feedback"]
    assert last["id"] == later[1]["reply_id"] and last["reply"] == later[1]["reply"]
    for item in [*stored, last]:
        assert item["title"] == first["title"] and item["app_id"] == "inbe"
        assert item["message"] == item["context"] == ""
        assert "account_id" not in item and "reply_id" not in item
    assert request(port, "GET", page + "&offset=3", headers=owner_auth) == {"feedback": None}
    other_reply, = request(port, "GET", page, headers=other_auth)["feedback"]
    assert other_reply["title"] == "Other account" and other_reply["id"] == later[0]["reply_id"]
    latest, = [item for item in request(port, "GET", REPORTS, headers=owner_auth)["feedback"] if item["id"] == first["id"]]
    assert latest["reply"] == later[1]["reply"]
    with closing(sqlite3.connect(database)) as connection:
        count, = connection.execute("SELECT COUNT(*) FROM server_app_feedback_replies WHERE account_id=?", (owner,)).fetchone()
        assert count == 2


def main():
    binary, oracle = (str(Path(path).resolve()) for path in sys.argv[1:])
    with TemporaryDirectory(prefix="feedback-", dir=Path(binary).parent) as scratch:
        directory = Path(scratch)
        active = directory / "active"
        active.mkdir()
        token_file = active / "feedback.token"
        token_file.write_text(OPERATOR + "\n")
        with node(binary, active, MESH_ADMIN_TOKEN=ADMIN, MESH_FEEDBACK_TOKEN_FILE=str(token_file)) as (port, database):
            owner, owner_auth = login(port, oracle, "feedback-owner")
            other, other_auth = login(port, oracle, "feedback-other")
            operator = {"X-Ksync-Admin": OPERATOR}
            first = check_reports(port, database, owner, owner_auth, other, other_auth, operator)
            check_replies(port, database, first, owner, owner_auth, other, other_auth, operator)
            request(port, "POST", "/api/v1/sync/logout", headers=owner_auth)
            assert request(port, "GET", REPORTS, headers=owner_auth, expected=401) == {"error": "invalid bearer token"}
        with node(binary, active, MESH_ADMIN_TOKEN=ADMIN) as (port, _):
            assert len(request(port, "GET", INBOX, headers={"X-Mesh-Admin": ADMIN})["feedback"]) == 21
        with node(binary, active) as (port, _):
            assert request(port, "GET", INBOX, expected=403) == {"error": "admin disabled"}
            assert request(port, "POST", REPLY, body={}, expected=403) == {"error": "admin disabled"}
    print("Native feedback HTTP: account and operator access, report quota, exact body limits, SQLite retries, reply pagination and token revocation verified.")


if __name__ == "__main__":
    main()
