"""Exercise the native Lumi owner and developer relay on a disposable loopback node."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import gzip
import json
from pathlib import Path
import socket
import sqlite3
import sys
from tempfile import TemporaryDirectory

from check_feedback_http import login, node, request, send

ACCOUNT = "/api/v1/lumi/developer"
RELAY = "/api/v1/admin/lumi/developer"
TOKEN = "fixture-lumi-developer-token-primary"
ROTATED = "fixture-lumi-developer-token-rotated"
OPERATOR = {"X-Mesh-Admin": TOKEN}


def key(number):
    return f"{number:032x}"


def account(port, auth, number, *, expected=200, **fields):
    return request(port, "POST", ACCOUNT, headers=auth, body={"id": key(number), **fields}, expected=expected)


def finish(port, owner, number, reply, *, headers=OPERATOR, expected=200):
    payload = {"id": key(number), "account_id": owner, "reply": reply}
    return request(port, "POST", RELAY, headers=headers, body=payload, expected=expected)


def check_access(port, database, owner, owner_auth, other, other_auth):
    payload = {"id": key(0)}
    assert request(port, "GET", ACCOUNT, expected=405) is None
    assert request(port, "DELETE", RELAY, expected=405) is None
    assert request(port, "POST", ACCOUNT, body=payload, expected=401) == {"error": "bearer token required"}
    assert account(port, other_auth | {"X-Mesh-User": owner}, 0, expected=401) == {"error": "token user mismatch"}
    assert account(port, other_auth, 0, text="other", expected=403) == {"error": "owner account required"}
    assert account(port, owner_auth, 0, account_id=other, expected=400) == {"error": "invalid developer message"}
    assert account(port, owner_auth, 0, reply="spoof", expected=400) == {"error": "invalid developer message"}
    for credential in ({}, {"X-Mesh-Admin": "fixture-admin"}, {"X-Mesh-Admin": "fixture-feedback"}, owner_auth):
        assert request(port, "GET", RELAY, headers=credential, expected=401) == {"error": "admin token required"}
    for header in ("X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "X-Real-IP", "Forwarded",
                   "CF-Connecting-IP", "True-Client-IP", "Via"):
        assert request(port, "GET", RELAY, headers=OPERATOR | {header: "127.0.0.1"}, expected=403) == {
            "error": "developer relay requires loopback",
        }
    for header in ({"Host": "example.test"}, {"Origin": "https://example.test"},
                   {"Origin": "http://localhost/"}, {"Origin": "http://localhost@127.0.0.1"},
                   {"Origin": "http://localhost,https://example.test"}):
        assert request(port, "GET", RELAY, headers=OPERATOR | header, expected=403) == {
            "error": "developer relay requires loopback",
        }
    empty = {"id": "", "account_id": owner}
    assert request(port, "GET", RELAY, headers={"X-Ksync-Admin": TOKEN, "Host": "LOCALHOST:1234",
                                                "Origin": "http://[::1]:1234"}) == empty
    assert request(port, "HEAD", RELAY, headers=OPERATOR) is None
    with closing(sqlite3.connect(database)) as storage, storage:
        storage.execute("INSERT INTO server_lumi_developer_turns(account_id,id,text) VALUES(?,?,?)",
                        (other, key(0), "another account's current turn"))
    assert account(port, owner_auth, 0, expected=404) == {"error": "unknown developer turn"}
    assert request(port, "GET", RELAY, headers=OPERATOR) == empty
    assert finish(port, owner, 0, "wrong queue", expected=409) == {"error": "developer turn unavailable"}


def check_limits(port, owner, owner_auth):
    invalid = {"error": "invalid developer turn"}
    for body in (b"", b"null", b"[]", b'{"id":1}', b'{"id":"z"}',
                 {"id": "A" * 32}, {"id": key(0), "text": []}):
        assert request(port, "POST", ACCOUNT, headers=owner_auth, body=body, expected=400) == invalid
    assert account(port, owner_auth, 0, text=" \u2003\t", expected=400) == {"error": "invalid developer message"}
    assert account(port, owner_auth, 0, text="é" * 4097, expected=400) == {"error": "invalid developer message"}
    raw = json.dumps({"id": key(0), "text": "é" * 4096}, ensure_ascii=False).encode().ljust(65536)
    compressed = owner_auth | {"Content-Encoding": "gzip"}
    assert request(port, "POST", ACCOUNT, headers=owner_auth, body=raw + b" ", expected=400) == invalid
    assert request(port, "POST", ACCOUNT, headers=compressed, body=gzip.compress(raw + b" "), expected=400) == invalid
    assert request(port, "POST", ACCOUNT, headers=compressed, body=gzip.compress(raw)) == {"id": key(0)}
    assert request(port, "POST", ACCOUNT, headers=owner_auth, body=raw) == {"id": key(0)}
    assert finish(port, owner, 0, "é" * 4097, expected=400) == {"error": "invalid developer reply"}
    assert finish(port, owner, 0, "\u2003", expected=400) == {"error": "invalid developer reply"}
    assert finish(port, owner, 0, "é" * 4096) == {"id": key(0)}
    assert account(port, owner_auth, 0) == {"id": key(0), "reply": "é" * 4096}


def check_queue(port, database, owner, owner_auth, other):
    for number in range(1, 8):
        assert account(port, owner_auth, number, text=f"message {number}") == {"id": key(number)}

    def submit(number):
        return number, send(port, "POST", ACCOUNT, headers=owner_auth,
                            body={"id": key(number), "text": f"message {number}"})

    with ThreadPoolExecutor(max_workers=2) as pool:
        slots = sorted(pool.map(submit, (8, 9)), key=lambda entry: entry[1][0])
    winner, success = slots[0]
    rejected, failure = slots[1]
    assert success == (200, {"id": key(winner)})
    assert failure == (429, {"error": "developer chat queue full"})
    assert account(port, owner_auth, rejected, expected=404) == {"error": "unknown developer turn"}
    assert account(port, owner_auth, winner, text=f"message {winner}") == {"id": key(winner)}
    assert account(port, owner_auth, winner, text="different", expected=409) == {"error": "developer turn already exists"}
    assert finish(port, other, 1, "wrong account", expected=400) == {"error": "invalid developer reply"}
    assert finish(port, owner, rejected, "missing", expected=409) == {"error": "developer turn unavailable"}

    next_turn = {"id": key(1), "text": "message 1", "account_id": owner}
    assert request(port, "GET", RELAY, headers=OPERATOR) == next_turn
    assert request(port, "GET", RELAY, headers=OPERATOR) == next_turn

    def worker(reply):
        payload = {"id": key(1), "reply": reply, "account_id": owner}
        return reply, send(port, "POST", RELAY, headers=OPERATOR, body=payload)

    with ThreadPoolExecutor(max_workers=2) as pool:
        completions = sorted(pool.map(worker, ("fixture reply one", "fixture reply two")), key=lambda entry: entry[1][0])
    reply, success = completions[0]
    assert success == (200, {"id": key(1)})
    assert completions[1][1] == (409, {"error": "developer turn unavailable"})
    assert finish(port, owner, 1, reply) == {"id": key(1)}
    assert account(port, owner_auth, 1, text="message 1") == {"id": key(1), "reply": reply}
    assert account(port, owner_auth, rejected, text=f"message {rejected}") == {"id": key(rejected)}

    body = json.dumps({"id": key(90), "text": "abandoned upload"}).encode()
    with closing(socket.create_connection(("127.0.0.1", port), timeout=5)) as client:
        headers = {"Host": "127.0.0.1", "Content-Length": str(len(body) + 1024), **owner_auth}
        request_head = f"POST {ACCOUNT} HTTP/1.1\r\n" + "".join(f"{name}: {value}\r\n" for name, value in headers.items())
        client.sendall(request_head.encode() + b"\r\n" + body)
    assert account(port, owner_auth, 90, expected=404) == {"error": "unknown developer turn"}

    with closing(sqlite3.connect(database)) as storage, storage:
        storage.execute("UPDATE server_lumi_developer_turns SET created_at=datetime('now','-8 days') WHERE account_id=? AND id=?",
                        (owner, key(2)))
        storage.execute("INSERT INTO server_lumi_developer_turns(account_id,id,text,created_at) VALUES(?,?,?,datetime('now','-8 days'))",
                        (other, key(2), "other account"))
        storage.execute("CREATE TRIGGER fixture_reject_turn BEFORE INSERT ON server_lumi_developer_turns "
                        "WHEN NEW.text='fixture reject' BEGIN SELECT RAISE(ABORT,'fixture reject'); END")
    assert request(port, "GET", RELAY, headers=OPERATOR) == {"id": key(3), "text": "message 3", "account_id": owner}
    assert account(port, owner_auth, 2) == {"id": key(2)}
    assert account(port, owner_auth, 10, text="fixture reject", expected=503) == {"error": "developer chat unavailable"}
    assert account(port, owner_auth, 2) == {"id": key(2)}
    assert account(port, owner_auth, 10, text="accepted after rollback") == {"id": key(10)}
    assert account(port, owner_auth, 2, expected=404) == {"error": "unknown developer turn"}
    with closing(sqlite3.connect(database)) as storage:
        assert storage.execute("SELECT text FROM server_lumi_developer_turns WHERE account_id=? AND id=?",
                               (other, key(2))).fetchone() == ("other account",)
        assert storage.execute("SELECT reply FROM server_lumi_developer_turns WHERE account_id=? AND id=?",
                               (other, key(0))).fetchone() == ("",)
    return reply


def main():
    binary, oracle = map(str, map(Path.resolve, map(Path, sys.argv[1:])))
    with TemporaryDirectory(prefix="lumi-developer-", dir=Path(binary).parent) as temporary:
        directory = Path(temporary)
        with node(binary, directory, MESH_ADMIN_TOKEN=TOKEN) as (port, _):
            owner, owner_auth = login(port, oracle, "lumi-owner")
            other, other_auth = login(port, oracle, "lumi-other")
            assert account(port, owner_auth, 0, expected=503) == {"error": "developer chat unavailable"}
            assert request(port, "GET", RELAY, headers=OPERATOR, expected=503) == {"error": "developer chat unavailable"}
        with node(binary, directory, MESH_LUMI_OWNER_ID=owner.upper(), MESH_LUMI_DEVELOPER_TOKEN=TOKEN) as (port, _):
            assert account(port, owner_auth, 0, expected=503) == {"error": "developer chat unavailable"}
        with node(binary, directory, MESH_LUMI_OWNER_ID=owner, MESH_LUMI_DEVELOPER_TOKEN="short") as (port, _):
            assert account(port, owner_auth, 0, expected=503) == {"error": "developer chat unavailable"}
        token_file = directory / "relay.token"
        token_file.write_text(TOKEN + "\n")
        settings = {"MESH_LUMI_OWNER_ID": owner, "MESH_LUMI_DEVELOPER_TOKEN_FILE": str(token_file),
                    "MESH_ADMIN_TOKEN": "fixture-admin", "MESH_FEEDBACK_TOKEN": "fixture-feedback"}
        with node(binary, directory, **settings) as (port, database):
            check_access(port, database, owner, owner_auth, other, other_auth)
            check_limits(port, owner, owner_auth)
            reply = check_queue(port, database, owner, owner_auth, other)
        with node(binary, directory, **settings, MESH_LUMI_DEVELOPER_TOKEN=ROTATED) as (port, _):
            assert request(port, "GET", RELAY, headers=OPERATOR, expected=401) == {"error": "admin token required"}
            rotated = {"X-Mesh-Admin": ROTATED}
            assert request(port, "GET", RELAY, headers=rotated) == {"id": key(3), "text": "message 3", "account_id": owner}
            assert account(port, owner_auth, 1) == {"id": key(1), "reply": reply}
            assert finish(port, owner, 1, reply, headers=rotated) == {"id": key(1)}
    print("Native Lumi developer HTTP: owner and relay access, limits, cancellation, queue and reply retries, rollback, retention and restart passed.")


if __name__ == "__main__":
    main()
