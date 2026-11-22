"""Verify post-commit social hints alongside native versioned WebSocket events."""

from contextlib import ExitStack, closing
from http.client import HTTPConnection
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory

from check_http_listener import open_sync_socket, pairing_node, read_sync_event
from check_lists_http import command, post
from check_social_http import FRIENDS, PROFILE_STATS, REQUESTS, call, login


class Client:
    def __init__(self, resources, port, account, auth, sockets=1):
        self.account = account
        self.auth = auth
        self.streams = []
        bearer = auth["Authorization"].removeprefix("Bearer ")
        for _ in range(sockets):
            _, stream = open_sync_socket(resources, "127.0.0.1", port, bearer)
            assert read_sync_event(stream) == self.event(0, "sync_ready")
            self.streams.append(stream)

    def event(self, version, kind="sync_changed"):
        return {"type": kind, "user_id_hash": self.account, "server_version": version}

    def expect(self, *versions):
        expected = [self.event(version) for version in versions]
        for stream in self.streams:
            events = [read_sync_event(stream) for _ in versions]
            assert events == expected, (events, expected)

    def advance(self, connection):
        # A real update marks the end of a sequence without a timing-based
        # silence assertion. Any unintended zero-version hint arrives first.
        reply = call(connection, "POST", "/api/v1/sync", 200, auth=self.auth,
                     headers={"X-Mesh-User": self.account, "X-Mesh-Client": "socket-fixture"},
                     body={"v": 2, "nonce": "fixture", "ciphertext": "opaque"})
        return reply["server_version"]

    def expect_no_hint(self, connection):
        self.expect(self.advance(connection))


def check_contacts(connection, alice, bob, outsider):
    before = alice.advance(connection)
    result = call(connection, "POST", REQUESTS, 201, auth=alice.auth, body={"target": bob.account})
    after = alice.advance(connection)
    assert after == before + 1
    alice.expect(before, 0, after)
    bob.expect(0)
    accept = f"{REQUESTS}/{result['request']['id']}/accept"
    call(connection, "POST", accept, 403, auth=outsider.auth)
    outsider.expect_no_hint(connection)
    call(connection, "POST", accept, 200, auth=bob.auth)
    alice.expect(0)
    bob.expect(0)
    call(connection, "POST", accept, 409, auth=bob.auth)
    alice.expect_no_hint(connection)
    bob.expect_no_hint(connection)

    # Go sends a hint only when the reading account's cache changes.
    call(connection, "GET", FRIENDS, 200, auth=alice.auth)
    alice.expect(0)
    call(connection, "GET", FRIENDS, 200, auth=alice.auth)
    alice.expect_no_hint(connection)
    bob.expect_no_hint(connection)


def check_profile(connection, database, alice, bob, outsider):
    score = {"practice": "game", "metric": "score", "value": 9, "local_date": 20260218}
    with database:
        database.execute("CREATE TRIGGER socket_block_profile BEFORE INSERT ON server_profile_stats "
                         "WHEN new.metric='blocked' BEGIN SELECT RAISE(ABORT,'fixture rollback'); END")
    call(connection, "PUT", PROFILE_STATS, 500, auth=alice.auth,
         body={"app": "socket-app", "metrics": [score, {**score, "metric": "blocked"}]})
    assert database.execute("SELECT count(*) FROM server_profile_stats").fetchone() == (0,)
    alice.expect_no_hint(connection)
    bob.expect_no_hint(connection)
    call(connection, "PUT", PROFILE_STATS, 200, auth=alice.auth, body={"app": "socket-app", "metrics": [score]})
    alice.expect(0)
    bob.expect(0)
    outsider.expect_no_hint(connection)


def check_lists(connection, database, owner, editor, pending, outsider):
    with database:
        database.execute("INSERT INTO server_friendships VALUES(?,?,CURRENT_TIMESTAMP)",
                         sorted((owner.account, pending.account)))
    draft = {"id": "socket-list", "title": "Shared list", "items": []}
    first = command("socket-invite", "invite", list=draft, target=editor.account)
    post(connection, owner.auth, first)
    owner.expect(0)
    editor.expect(0)
    post(connection, owner.auth, first)
    owner.expect_no_hint(connection)
    editor.expect_no_hint(connection)
    post(connection, editor.auth, command("socket-accept", "accept", invitation_id=first["request_id"]))
    owner.expect(0)
    editor.expect(0)

    second = command("socket-pending", "invite", list=draft, target=pending.account)
    post(connection, owner.auth, second)
    owner.expect(0)
    editor.expect(0)
    pending.expect(0)
    edited = {**draft, "revision": 1, "items": [{"id": "private-item", "title": "Private until accepted"}]}
    save = command("socket-save", "save", list=edited)
    with database:
        database.execute("CREATE TRIGGER socket_block_list_receipt BEFORE INSERT ON server_list_commands "
                         "WHEN new.request_id='socket-save' BEGIN SELECT RAISE(ABORT,'fixture rollback'); END")
    post(connection, editor.auth, save, 500)
    assert database.execute("SELECT revision FROM server_shared_lists WHERE id=?", (draft["id"],)).fetchone() == (1,)
    owner.expect_no_hint(connection)
    editor.expect_no_hint(connection)
    with database:
        database.execute("DROP TRIGGER socket_block_list_receipt")
    post(connection, editor.auth, save)
    owner.expect(0)
    editor.expect(0)
    pending.expect_no_hint(connection)
    renamed = {**edited, "revision": 2, "title": "Renamed list"}
    post(connection, owner.auth, command("socket-rename", "save", list=renamed))
    owner.expect(0)
    editor.expect(0)
    pending.expect(0)

    call(connection, "DELETE", f"{FRIENDS}/{editor.account}", 200, auth=owner.auth)
    owner.expect(0)
    editor.expect(0)
    assert database.execute("SELECT status FROM server_list_invitations WHERE id=?", (first["request_id"],)).fetchone() == (
        "declined",)
    post(connection, owner.auth, command("socket-delete", "delete", list={"id": draft["id"]}))
    owner.expect(0)
    pending.expect(0)
    editor.expect_no_hint(connection)
    outsider.expect_no_hint(connection)


def main(binary, oracle):
    binary = str(Path(binary).resolve())
    with TemporaryDirectory(prefix="social-socket-", dir=Path(binary).parent) as directory:
        path = Path(directory) / "mesh.db"
        with pairing_node(binary, path) as port, ExitStack() as resources:
            connection = resources.enter_context(closing(HTTPConnection("127.0.0.1", port, timeout=5)))
            database = resources.enter_context(closing(sqlite3.connect(path)))
            alice, bob, pending, outsider = [
                Client(resources, port, *login(connection, oracle, f"socket-{name}"), sockets=2 if name == "alice" else 1)
                for name in ("alice", "bob", "pending", "outsider")
            ]
            check_contacts(connection, alice, bob, outsider)
            check_profile(connection, database, alice, bob, outsider)
            check_lists(connection, database, alice, bob, pending, outsider)
            call(connection, "POST", "/api/v1/sync/logout", 200, auth=bob.auth)
            for stream in bob.streams:
                assert stream.read(1) == b"", "revoked session remained subscribed"
    print("native social WebSocket hints passed")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(f"usage: {sys.argv[0]} SERVER SIGNING_ORACLE")
    main(*sys.argv[1:])
