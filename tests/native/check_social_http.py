"""Exercise the native social API against a disposable loopback server and database."""

import base64
from contextlib import closing
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
from http.client import HTTPConnection
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from urllib.parse import urlencode

from check_http_listener import pairing_node, sign_with_oracle


FRIENDS = "/api/v1/friends"
REQUESTS = f"{FRIENDS}/requests"
PROFILE_STATS = "/api/v1/profile/stats"
LOGIN = "/api/v1/sync/login"


def call(connection, method, path, expected, *, auth=None, body=None, headers=None):
    request_headers = {**(auth or {}), **(headers or {})}
    if body is not None and not isinstance(body, bytes):
        request_headers["Content-Type"] = "application/json"
        body = json.dumps(body).encode()
    connection.request(method, path, body, request_headers)
    response = connection.getresponse()
    payload = response.read()
    if response.getheader("Content-Encoding") == "gzip":
        payload = gzip.decompress(payload)
    assert response.status == expected, (method, path, expected, response.status, payload)
    return json.loads(payload) if payload else None


def login(connection, oracle, client):
    keys = sign_with_oracle(oracle, "keypair")
    account = hashlib.sha256(base64.b64decode(keys["PublicKey"])).hexdigest()
    challenge = call(connection, "GET", f"/api/v1/sync/challenge?user_id={account}", 200)
    payload = json.dumps({"client_id": client, "public_key": keys["PublicKey"]}).encode()
    digest = hashlib.sha256(payload).hexdigest()
    message = f"ksync-sync-v1\nPOST\n{LOGIN}\n{digest}\n{challenge['nonce']}\n".encode()
    signed = sign_with_oracle(oracle, "sign", PrivateKey=keys["PrivateKey"],
                              Message=base64.b64encode(message).decode())
    session = call(connection, "POST", LOGIN, 200, body=payload,
                   headers={"X-Ksync-User": account.upper(), "X-Ksync-Signature": signed["Signature"]})
    return account, {"Authorization": f"Bearer {session['auth_token']}"}


def create(connection, auth, target):
    body = gzip.compress(json.dumps({"TARGET": target}).encode())
    result = call(connection, "POST", REQUESTS, 201, auth=auth, body=body,
                  headers={"Content-Encoding": "gzip"})
    assert result["status"] == "ok"
    item = result["request"]
    assert len(bytes.fromhex(item["id"])) == 16
    assert item["status"] == "pending"
    for key in ("created_at", "updated_at"):
        assert datetime.fromisoformat(item[key]).tzinfo == timezone.utc
    return item


def check_requests(connection, alice, alice_auth, bob, bob_auth, outsider_auth):
    assert call(connection, "GET", FRIENDS, 401) == {"error": "bearer token required"}
    assert call(connection, "GET", REQUESTS, 401, auth=alice_auth,
                headers={"X-Mesh-User": bob}) == {"error": "token user mismatch"}
    assert call(connection, "GET", FRIENDS, 200, auth=alice_auth,
                headers={"X-Ksync-User": alice.upper()}) == {"friends": []}
    assert call(connection, "HEAD", FRIENDS, 200, auth=alice_auth) is None
    assert call(connection, "GET", REQUESTS, 200, auth=alice_auth) == {"incoming": [], "outgoing": []}
    for body, status, message in [({"target": False}, 400, "invalid json"),
                                  (b" " * 8193, 400, "request body too large"),
                                  ({"target": "  "}, 400, "target required"),
                                  ({"target": "@missing"}, 404, "friend target not found"),
                                  ({"target": alice}, 409, "cannot friend self")]:
        assert call(connection, "POST", REQUESTS, status, auth=alice_auth, body=body) == {"error": message}
    assert call(connection, "GET", PROFILE_STATS, 405, auth=alice_auth) is None
    assert call(connection, "POST", f"{REQUESTS}/missing/accept", 404, auth=bob_auth) == {
        "error": "friend request not found"}
    item = create(connection, alice_auth, "  @BOB_USER  ")
    assert item == {"id": item["id"], "requester_user_id_hash": alice, "requester_alias": "alice_user",
                    "target_user_id_hash": bob, "target_alias": "bob_user", "status": "pending",
                    "created_at": item["created_at"], "updated_at": item["updated_at"]}
    assert create(connection, alice_auth, bob.upper()) == item
    assert call(connection, "GET", REQUESTS, 200, auth=alice_auth) == {"incoming": [], "outgoing": [item]}
    assert call(connection, "GET", REQUESTS, 200, auth=bob_auth) == {"incoming": [item], "outgoing": []}
    assert call(connection, "GET", REQUESTS, 200, auth=outsider_auth) == {"incoming": [], "outgoing": []}
    path = f"{REQUESTS}/{item['id']}"
    for identity, action in [(alice_auth, "accept"), (outsider_auth, "accept"), (outsider_auth, "decline")]:
        assert call(connection, "POST", f"{path}/{action}", 403, auth=identity) == {
            "error": "friend request not owned by user"}
    declined = call(connection, "POST", f"{path}/decline", 200, auth=alice_auth)
    assert declined["status"] == "declined" and declined["request"]["status"] == "declined"
    assert call(connection, "POST", f"{path}/accept", 409, auth=bob_auth) == {"error": "request not pending"}
    reopened = create(connection, alice_auth, bob)
    assert reopened["id"] == item["id"] and reopened["created_at"] == item["created_at"]
    reverse = create(connection, bob_auth, alice)
    accepted = call(connection, "POST", f"{path}/accept", 200, auth=bob_auth, body={"user_id_hash": alice})
    assert accepted["status"] == "accepted" and accepted["request"]["status"] == "accepted"
    assert call(connection, "POST", f"{path}/accept", 409, auth=bob_auth) == {"error": "request not pending"}
    assert call(connection, "POST", REQUESTS, 409, auth=alice_auth, body={"target": bob}) == {"error": "already friends"}
    assert call(connection, "GET", FRIENDS, 200, auth=alice_auth) == {
        "friends": [{"user_id_hash": bob, "alias": "bob_user", "profile_icon": 41,
                     "created_at": accepted["request"]["updated_at"]}]}
    assert call(connection, "GET", REQUESTS, 200, auth=bob_auth) == {"incoming": [], "outgoing": [reverse]}


def board(connection, auth, app, practice, metric):
    query = urlencode({"app": app, "practice": practice, "metric": metric})
    return call(connection, "GET", f"{FRIENDS}/stats?{query}", 200, auth=auth)["rows"]


def check_stats(connection, database, alice, alice_auth, bob, bob_auth, outsider_auth):
    def write(auth, metrics, applied):
        assert call(connection, "PUT", PROFILE_STATS, 200, auth=auth,
                    body={"APP": " example ", "METRICS": metrics}) == {"status": "ok", "applied": applied}

    def snapshot_version():
        return database.execute("SELECT server_version FROM server_social_snapshots "
                                "WHERE user_id_hash=? AND kind='leaderboard.example.game.score'", (alice,)).fetchone()[0]

    assert call(connection, "GET", f"{FRIENDS}/stats?app=example&practice=whm&metric=avg_time", 400,
                auth=alice_auth) == {"error": "invalid stats query"}
    assert call(connection, "GET", f"{FRIENDS}/stats", 401) == {"error": "bearer token required"}
    assert call(connection, "PUT", PROFILE_STATS, 400, auth=alice_auth, body={"app": ".."}) == {"error": "invalid app"}
    sample = {"PRACTICE": " game ", "metric": "score", "value": 8.5, "label": " untrusted label "}
    assert call(connection, "PUT", PROFILE_STATS, 400, auth=alice_auth,
                body={"app": "example", "metrics": [sample, {"practice": "game", "metric": "bad/metric"}]}) == {
        "error": "invalid metric"}
    assert database.execute("SELECT count(*) FROM server_profile_stats").fetchone() == (0,)
    assert call(connection, "PUT", PROFILE_STATS, 400, auth=alice_auth,
                body={"app": "example", "metrics": [None] * 101}) == {"error": "too many metrics"}
    assert call(connection, "PUT", PROFILE_STATS, 400, auth=alice_auth,
                body={"app": "example", "metrics": [{**sample, "value": "8.5"}]}) == {"error": "invalid json"}
    assert call(connection, "PUT", PROFILE_STATS, 400, auth=alice_auth,
                body={"app": "..", "metrics": [False]}) == {"error": "invalid json"}
    assert call(connection, "PUT", PROFILE_STATS, 200, auth=alice_auth,
                body=b'{"app":"example","metrics":[{"practice":"game","metric":"score"}],"metrics":null}') == {
        "status": "ok", "applied": 0}
    with database:
        database.execute("CREATE TRIGGER deny_appearance BEFORE INSERT ON server_profile_stats "
                         "WHEN NEW.metric='appearance' BEGIN SELECT RAISE(ABORT,'fixture rollback'); END")
    appearance = {"practice": "game", "metric": "appearance", "value": 12}
    assert call(connection, "PUT", PROFILE_STATS, 500, auth=alice_auth,
                body={"app": "example", "metrics": [sample, appearance]}) == {"error": "profile stats failed"}
    assert database.execute("SELECT count(*) FROM server_profile_stats").fetchone() == (0,)
    with database:
        database.execute("DROP TRIGGER deny_appearance")
    write(alice_auth, [sample, appearance], 2)
    write(alice_auth, [sample], 0)
    write(bob_auth, [{**sample, "value": 15}], 1)
    write(outsider_auth, [{**sample, "value": 1000}], 1)
    rows = board(connection, alice_auth, " example ", "game", "score")
    assert rows == [
        {"user_id_hash": bob, "alias": "bob_user", "profile_icon": 41, "app": "example", "practice": "game",
         "metric": "score", "value": 15, "label": "15", "updated_at": ""},
        {"user_id_hash": alice, "alias": "alice_user", "game_icon": 12, "app": "example", "practice": "game",
         "metric": "score", "value": 8.5, "label": "8", "updated_at": ""},
    ]
    version = snapshot_version()
    assert board(connection, alice_auth, "example", "game", "score") == rows and snapshot_version() == version
    assert database.execute("SELECT label FROM server_profile_stats WHERE user_id_hash=? AND metric='score'",
                            (alice,)).fetchone() == ("untrusted label",)
    now = datetime.now(timezone.utc)
    today = int(now.strftime("%Y%m%d"))
    timestamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    with database:
        database.execute("INSERT INTO server_sessions(user_id_hash,id,started_at,local_date,updated_at) VALUES(?,?,?,?,?)",
                         (bob, "hold-session", timestamp, today, timestamp))
        database.executemany("INSERT INTO server_session_rounds(user_id_hash,session_id,round_index,hold_seconds) VALUES(?,?,?,?)",
                             [(bob, "hold-session", 0, 10), (bob, "hold-session", 1, 11)])
    assert [(row["user_id_hash"], row["value"]) for row in board(connection, alice_auth, "example", "whm", "streak")] == [
        (bob, 1), (alice, 0)]
    hold = {"practice": "whm", "metric": "avg_hold", "value": 30.5}
    write(alice_auth, [hold], 1)
    average = board(connection, alice_auth, "example", "whm", "avg_hold")
    assert [(row["value"], row["label"]) for row in average] == [(30.5, "30"), (10.5, "10")]
    with database:
        stale = (now - timedelta(days=31)).strftime("%Y-%m-%dT%H:%M:%SZ")
        database.execute("UPDATE server_profile_stats SET updated_at=? WHERE user_id_hash=? AND metric='avg_hold'", (stale, alice))
    assert [row["value"] for row in board(connection, alice_auth, "example", "whm", "avg_hold")] == [10.5, 0]


def check_remove(connection, database, alice, alice_auth, bob, bob_auth, outsider):
    with database:
        database.executemany("INSERT INTO server_shared_lists(id,owner,title,items,revision) VALUES(?,?,'fixture','[]',1)",
                             [("alice-list", alice), ("bob-list", bob), ("other-list", outsider)])
        database.executemany("INSERT INTO server_list_invitations(id,list_id,target,status) VALUES(?,?,?,?)",
                             [("alice-invite", "alice-list", bob, "accepted"),
                              ("bob-invite", "bob-list", alice, "pending"),
                              ("other-invite", "other-list", alice, "accepted")])
        database.execute("CREATE TRIGGER deny_invite_change BEFORE UPDATE ON server_list_invitations "
                         "BEGIN SELECT RAISE(ABORT,'fixture rollback'); END")
    path = f"{FRIENDS}/{bob.upper()}"
    assert call(connection, "DELETE", path, 500, auth=alice_auth) == {"error": "friend remove failed"}
    assert database.execute("SELECT count(*) FROM server_friendships").fetchone() == (1,)
    assert database.execute("SELECT count(*) FROM server_friend_requests").fetchone() == (2,)
    with database:
        database.execute("DROP TRIGGER deny_invite_change")
    assert call(connection, "DELETE", path, 200, auth=alice_auth) == {"status": "removed"}
    assert call(connection, "DELETE", path, 200, auth=alice_auth) == {"status": "removed"}
    assert call(connection, "GET", FRIENDS, 200, auth=bob_auth) == {"friends": []}
    assert call(connection, "GET", REQUESTS, 200, auth=bob_auth) == {"incoming": [], "outgoing": []}
    assert [row["user_id_hash"] for row in board(connection, alice_auth, "example", "game", "score")] == [alice]
    assert database.execute("SELECT id,status FROM server_list_invitations ORDER BY id").fetchall() == [
        ("alice-invite", "declined"), ("bob-invite", "declined"), ("other-invite", "accepted")]
    assert database.execute("SELECT count(*) FROM server_friend_requests").fetchone() == (0,)


def main(binary, oracle):
    with TemporaryDirectory(prefix="social-", dir=Path(binary).resolve().parent) as directory:
        db_path = Path(directory) / "mesh.db"
        with pairing_node(binary, db_path, MESH_MAX_BODY_BYTES="8192") as port, \
                closing(HTTPConnection("127.0.0.1", port, timeout=5)) as connection, \
                closing(sqlite3.connect(db_path)) as database:
            alice, alice_auth = login(connection, oracle, "social-alice")
            bob, bob_auth = login(connection, oracle, "social-bob")
            outsider, outsider_auth = login(connection, oracle, "social-outsider")
            with database:
                database.executemany("UPDATE server_users SET alias=?,profile_icon=? WHERE user_id_hash=?",
                                     [("alice_user", 0, alice), ("bob_user", 41, bob)])
            check_requests(connection, alice, alice_auth, bob, bob_auth, outsider_auth)
            check_stats(connection, database, alice, alice_auth, bob, bob_auth, outsider_auth)
            check_remove(connection, database, alice, alice_auth, bob, bob_auth, outsider)
    print("native social HTTP passed")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(f"usage: {sys.argv[0]} SERVER SIGNING_ORACLE")
    main(*sys.argv[1:])
