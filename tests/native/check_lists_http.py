"""Check native list collaboration on a disposable loopback server and database."""

from contextlib import closing
import gzip
import hashlib
from http.client import HTTPConnection
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory

from check_http_listener import pairing_node
from check_social_http import call, login


ROUTE = "/api/v1/lists"
EMPTY = {"lists": [], "invitations": []}
LIMIT = 262144


def command(request_id, action, **values):
    return {"request_id": request_id, "action": action, **values}


def post(connection, auth, request, expected=200, error="list change failed", encoding=None):
    content = request
    headers = None
    if encoding == "gzip":
        content = gzip.compress(json.dumps(request).encode())
        headers = {"Content-Encoding": "gzip"}
    response = call(connection, "POST", ROUTE, expected, auth=auth, body=content, headers=headers)
    if expected != 200:
        assert response == {"error": error}
    return response


def list_view(connection, auth):
    return call(connection, "GET", ROUTE, 200, auth=auth)


def friends(database, first, second):
    database.execute("INSERT INTO server_friendships VALUES(?,?,CURRENT_TIMESTAMP)", sorted((first, second)))


def legacy_receipt(connection, database, user, auth):
    # This is encoding/json.Marshal's ordered Go Command shape, including
    # escaping and the nil/empty slice distinction used in existing digests.
    legacy_list = {"id": "legacy-list", "owner": "", "alias": "", "title": "Café <&>\u2028",
                   "revision": 0, "deleted": 0, "items": []}
    canonical = command("legacy-receipt", "save", target="", invitation_id="", list=legacy_list)
    encoded = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"))
    for literal, escaped in {"<": r"\u003c", "&": r"\u0026", ">": r"\u003e", "\u2028": r"\u2028"}.items():
        encoded = encoded.replace(literal, escaped)
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    with database:
        database.execute("INSERT INTO server_list_commands VALUES(?,?,?)", (user, canonical["request_id"], digest))
    retry = {"LIST": {"TITLE": legacy_list["title"], "ITEMS": [], "id": "legacy-list"},
             "ACTION": "save", "REQUEST_ID": "legacy-receipt", "ignored": "forwards-compatible"}
    assert post(connection, auth, retry) == EMPTY
    retry["LIST"]["ITEMS"] = None
    post(connection, auth, retry, 409)


def check_protocol(connection, auth, wrong_user):
    assert call(connection, "GET", ROUTE, 401) == {"error": "bearer token required"}
    assert call(connection, "GET", ROUTE, 401, auth=auth, headers={"X-Mesh-User": wrong_user}) == {
        "error": "token user mismatch"}
    assert call(connection, "HEAD", ROUTE, 200, auth=auth) is None
    assert call(connection, "DELETE", ROUTE, 405, auth=auth) is None
    assert list_view(connection, auth) == EMPTY
    for invalid in (b"{", b" " * (LIMIT + 1), {"list": {"revision": False}},
                    {"list": {"items": [{"done": 2147483648}]}}):
        post(connection, auth, invalid, 400, "invalid list command")
    assert call(connection, "POST", ROUTE, 400, auth=auth, body=gzip.compress(b" " * (LIMIT + 1)),
                headers={"Content-Encoding": "gzip"}) == {"error": "invalid list command"}
    post(connection, auth, {"extra": " " * 9000}, 400)


def check_collaboration(connection, database, owner, owner_auth, member, member_auth, other, other_auth):
    draft = {"id": "weekend-list", "title": "Weekend <plans>", "items": [
        {"id": "task-pack", "title": "Pack bag", "comment": "Café & lunch\t\u2029"}]}
    invite = command("invite-viewer", "invite", target=member, access="viewer", list=draft)
    post(connection, owner_auth, {**invite, "request_id": "nonfriend", "target": other}, 403)
    post(connection, owner_auth, {**invite, "request_id": "duplicate-items", "list": {**draft, "items": draft["items"] * 2}}, 403)
    owner_view = post(connection, owner_auth, {**invite, "extra": "x" * 9000}, encoding="gzip")
    assert owner_view["lists"] == [{**draft, "items": [{**draft["items"][0], "done": 0}], "owner": owner,
                                    "alias": "lists_owner", "access": "owner", "revision": 1, "deleted": 0}]
    pending = {"id": "invite-viewer", "list_id": "weekend-list", "title": draft["title"], "owner": owner,
               "alias": "lists_owner", "target": member, "target_alias": "lists_member", "status": "pending",
               "access": "viewer"}
    assert owner_view["invitations"] == [pending]
    assert post(connection, owner_auth, invite) == owner_view
    post(connection, owner_auth, {**invite, "access": "editor"}, 409)
    assert list_view(connection, member_auth) == {"lists": [], "invitations": [pending]}
    assert list_view(connection, other_auth) == EMPTY
    accept = command("accept-viewer", "accept", invitation_id=pending["id"])
    post(connection, owner_auth, accept, 404)
    post(connection, other_auth, accept, 404)
    current = post(connection, member_auth, accept)
    assert current["lists"][0]["access"] == "viewer" and current["invitations"][0]["status"] == "accepted"
    assert post(connection, member_auth, accept) == current
    accessor = command("promote-editor", "access", target=member, access="editor", list={"id": draft["id"]})
    edit = command("edit-list", "save", list={**current["lists"][0], "title": "Changed by editor"})
    post(connection, member_auth, edit, 403)
    post(connection, member_auth, accessor, 403)
    post(connection, owner_auth, accessor)
    with database:
        # Legacy accepted invitations without explicit permissions are editors.
        database.execute("DELETE FROM server_list_permissions WHERE list_id=? AND target=?", (draft["id"], member))
    assert list_view(connection, member_auth)["lists"][0]["access"] == "editor"
    edited = post(connection, member_auth, edit)
    assert edited["lists"][0]["revision"] == 2 and edited["lists"][0]["title"] == "Changed by editor"
    assert post(connection, member_auth, edit) == edited
    post(connection, member_auth, {**edit, "request_id": "stale-list"}, 409)
    oversized = [{"id": f"full-{index}", "title": "Large comment", "comment": "x" * 511} for index in range(6)]
    quota = command("quota-list-edit", "save", list={**edited["lists"][0], "items": oversized})
    post(connection, member_auth, quota, 413)
    assert database.execute("SELECT revision FROM server_shared_lists WHERE id=?", (draft["id"],)).fetchone() == (2,)
    assert database.execute("SELECT count(*) FROM server_list_commands WHERE request_id=?", (quota["request_id"],)).fetchone() == (0,)
    assert post(connection, member_auth, {**quota, "list": edited["lists"][0]})["lists"][0]["revision"] == 3
    with database:
        friends(database, owner, other)
        friends(database, member, other)
    peer = command("editor-invite", "invite", target=other, list=draft)
    post(connection, member_auth, {**peer, "access": "owner"}, 403)
    assert len(post(connection, member_auth, peer)["invitations"]) == 2
    other_pending = list_view(connection, other_auth)
    assert other_pending["lists"] == [] and [row["id"] for row in other_pending["invitations"]] == [peer["request_id"]]
    declined = post(connection, other_auth, command("peer-declines", "decline", invitation_id=peer["request_id"]))
    assert declined == EMPTY
    post(connection, other_auth, command("declined-accept", "accept", invitation_id=peer["request_id"]), 409)
    peer["request_id"] = "editor-reinvite"
    post(connection, member_auth, peer)
    peer_accept = command("peer-accept", "accept", invitation_id=peer["request_id"])
    with database:
        database.executemany("INSERT INTO server_shared_lists VALUES(?,?,'Capacity','[]',1,0)",
                             ((f"capacity-{index}", other) for index in range(32)))
    post(connection, other_auth, peer_accept, 409)
    with database:
        database.execute("DELETE FROM server_shared_lists WHERE owner=? AND id LIKE 'capacity-%'", (other,))
    assert len(post(connection, other_auth, peer_accept)["lists"]) == 1
    remove = command("remove-peer", "remove", target=other, list={"id": draft["id"]})
    post(connection, member_auth, remove, 403)
    post(connection, owner_auth, {**accessor, "request_id": "grant-coowner", "access": "owner"})
    post(connection, member_auth, remove)
    assert list_view(connection, other_auth) == EMPTY
    # Losing the actual friendship must revoke an accepted co-owner as well.
    assert call(connection, "DELETE", f"/api/v1/friends/{member}", 200, auth=owner_auth) == {"status": "removed"}
    assert list_view(connection, member_auth) == EMPTY
    deletion = command("delete-list", "delete", list={"id": draft["id"]})
    post(connection, member_auth, deletion, 404)
    tombstone = post(connection, owner_auth, deletion)
    assert tombstone["invitations"] == [] and tombstone["lists"][0]["deleted"] == 1
    assert tombstone["lists"][0]["items"] == [] and tombstone["lists"][0]["revision"] == 4


def main(binary, oracle):
    binary = str(Path(binary).resolve())
    with TemporaryDirectory(prefix="lists-", dir=Path(binary).parent) as directory:
        path = Path(directory) / "mesh.db"
        with pairing_node(binary, path, MESH_MAX_BODY_BYTES="8192", MESH_ACCOUNT_STORAGE_LIMIT_BYTES="2400",
                          MESH_STORAGE_PLANS_ENABLED="0") as port, \
                closing(HTTPConnection("127.0.0.1", port, timeout=5)) as connection, \
                closing(sqlite3.connect(path)) as database:
            owner, owner_auth = login(connection, oracle, "lists-owner")
            member, member_auth = login(connection, oracle, "lists-member")
            other, other_auth = login(connection, oracle, "lists-other")
            with database:
                friends(database, owner, member)
                database.executemany("UPDATE server_users SET alias=? WHERE user_id_hash=?",
                                     [("lists_owner", owner), ("lists_member", member)])
            check_protocol(connection, owner_auth, member)
            legacy_receipt(connection, database, owner, owner_auth)
            check_collaboration(connection, database, owner, owner_auth, member, member_auth, other, other_auth)
    print("native shared lists HTTP passed")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(f"usage: {sys.argv[0]} SERVER SIGNING_ORACLE")
    main(*sys.argv[1:])
