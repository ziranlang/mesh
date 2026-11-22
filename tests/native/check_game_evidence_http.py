#!/usr/bin/env python3
"""Exercise approved game maps, exact replay, and frozen progress on a local node."""
import base64
import ctypes
import ctypes.util
import gzip
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlencode

from check_token_http import ADMIN_HEADERS, KEY, SEED, get, login, request, server

MAPS = "/api/v1/admin/game-maps"
COMPLETIONS = "/api/v1/game-completions"
REWARDS = "/api/v1/rewards"
MIGRATION = REWARDS + "/migration"
GAME = "shovey.classic"
COLLECTION = "private.entity.v1.game-progress"
RECORD = "fd65a3d7ac3d5ceb2d4d96cc5224a740"
CHEST = "shovey.secret-chest"


def compact(value):
    return json.dumps(value, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def board():
    tiles = bytearray([12] * 240 + [0] * 480)
    tiles[13], tiles[14], tiles[25] = 10, 7, 0
    return tiles


def revision(tiles, level):
    cells = tiles.hex()
    return {"level": level, "map_hash": digest(f"{GAME}.map.v1\n{level}\n{cells}".encode()), "map": cells}


def registration(*maps, source="release-1"):
    return {"game": GAME, "source_version": source, "maps": list(maps)}


def completion(map_revision, *, long=True, complete=True):
    return {"game": GAME, "level": map_revision["level"], "map_hash": map_revision["map_hash"],
            "moves": 11 if long else 1, "actions": "31313131312" if long else "2",
            "trace": "0d190d190d190d190d190d0e" if long else "0d0e", "evidence_complete": complete}


def private(connection, method, route, bearer, status=200, **kwargs):
    response, payload = request(connection, method, route, status, token=bearer, **kwargs)
    assert response.getheader("Cache-Control") == "private, no-store"
    return payload


def test_game(binary, oracle, root):
    board_v1 = board()
    initial = revision(board_v1, 1)
    register = registration(initial)
    with server(binary, root / "game-disabled.sqlite") as (client, _):
        assert request(client, "POST", MAPS, 403, body=register)[1] == {"error": "admin disabled"}
    with server(binary, root / "game.sqlite", MESH_TOKEN_ISSUER_PRIVATE_KEY_HEX=SEED,
                MESH_ADMIN_TOKEN=KEY) as (client, db):
        account, bearer, _ = login(client, oracle)
        _, other, _ = login(client, oracle)
        request(client, "POST", MAPS, 401, token=bearer, body=register)
        request(client, "POST", COMPLETIONS, 401, body=completion(initial))
        assert request(client, "GET", MAPS, 405)[0].getheader("Allow") == "POST"
        assert request(client, "PUT", COMPLETIONS, 405)[0].getheader("Allow") == "GET, HEAD, POST"
        private(client, "POST", COMPLETIONS, bearer, 400, body=completion(initial))

        invalid = (registration(dict(initial, map_hash="0" * 64)), registration(),
                   registration(*([initial] * 513)), registration(initial, source=""),
                   registration(revision(bytearray(720), 1)), b'{"maps":[],"\\u006daps":[]}',
                   dict(register, maps=[dict(initial, extra=None)]))
        for payload in invalid:
            assert request(client, "POST", MAPS, 400, headers=ADMIN_HEADERS, body=payload)[1] == {
                "error": "invalid map registration"}
        assert db.execute("SELECT COUNT(*) FROM game_maps").fetchone() == (0,)
        db.execute("CREATE TRIGGER refuse_map BEFORE INSERT ON game_map_sources WHEN NEW.level=2 "
                   "BEGIN SELECT RAISE(FAIL,'source failure'); END")
        request(client, "POST", MAPS, 503, headers=ADMIN_HEADERS, body=registration(initial, revision(board_v1, 2)))
        assert db.execute("SELECT COUNT(*) FROM game_maps").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM game_map_sources").fetchone() == (0,)
        db.execute("DROP TRIGGER refuse_map")
        maps = [revision(board_v1, level) for level in range(1, 29)]
        all_maps = registration(*maps)
        assert request(client, "POST", MAPS, headers=ADMIN_HEADERS, body=all_maps)[1] == all_maps
        assert request(client, "POST", MAPS, headers={"X-Ksync-Admin": KEY}, body=register)[1] == register
        board_v2 = board()
        board_v2[480 + 13] = 57
        version_2 = revision(board_v2, 1)
        request(client, "POST", MAPS, headers=ADMIN_HEADERS, body=registration(version_2, source="release-2"))
        request(client, "POST", MAPS, headers=ADMIN_HEADERS, body=registration(initial, source="release-2"))
        assert db.execute("SELECT COUNT(*) FROM game_maps").fetchone() == (29,)
        assert db.execute("SELECT COUNT(*) FROM game_map_sources").fetchone() == (30,)

        first = completion(initial)
        for bad in (dict(first, actions="x"), dict(first, actions="2"), dict(first, trace="0d000e"),
                    dict(first, trace="0d250e"), dict(first, moves=1000), dict(first, actions="1" * 60001),
                    dict(first, evidence_complete="true"), dict(first, unknown=None),
                    b'{"moves":11,"\\u006doves":12}', b" " * (1048576 + 1)):
            private(client, "POST", COMPLETIONS, bearer, 400, body=bad)
        db.execute("CREATE TRIGGER refuse_completion AFTER INSERT ON game_completions "
                   "BEGIN SELECT RAISE(FAIL,'completion write failure'); END")
        private(client, "POST", COMPLETIONS, bearer, 503, body=first)
        assert db.execute("SELECT COUNT(*) FROM game_completions").fetchone() == (0,)
        db.execute("DROP TRIGGER refuse_completion")

        receipt = digest(compact(first))
        assert private(client, "POST", COMPLETIONS, bearer, body=first) == {"id": receipt, "created": True}
        reordered = dict(reversed(list(first.items())))
        assert private(client, "POST", COMPLETIONS, bearer, body=gzip.compress(compact(reordered)),
                       headers={"Content-Encoding": "gzip"}) == {"id": receipt, "created": False}
        assert db.execute("SELECT id,payload FROM game_completions WHERE account_id=?", (account,)).fetchone() == (
            receipt, compact(first).decode())
        private(client, "POST", COMPLETIONS, bearer, body=completion(initial, long=False))

        query = "?" + urlencode({"game": GAME, "level": "+1", "map_hash": initial["map_hash"]})
        history = COMPLETIONS + query
        assert private(client, "GET", history, other) == {"completions": [], "map": "", "next_offset": 0}
        request(client, "GET", history, 401, token=other, headers={"X-Mesh-User": account})
        assert private(client, "HEAD", history, bearer) is None
        for offset in ("-1", "2147483648", "bad"):
            private(client, "GET", history + "&offset=" + offset, bearer, 400)
        saved = private(client, "GET", history, bearer)
        assert saved == {"completions": [completion(initial, long=False), first],
                         "map": initial["map"], "next_offset": 2}

        for moves in range(12, 29):
            legacy = dict(first, moves=moves, evidence_complete=False)
            private(client, "POST", COMPLETIONS, bearer, body=legacy)
        page_one = private(client, "GET", history, bearer)
        page_two = private(client, "GET", history + "&offset=16", bearer)
        assert page_one["next_offset"] == 16 and page_two["next_offset"] == 19
        assert [entry["moves"] for entry in page_one["completions"] + page_two["completions"]] == [1, *range(11, 29)]
        assert page_one["map"] == page_two["map"] == initial["map"]

        for map_revision in maps[1:-1]:
            private(client, "POST", COMPLETIONS, bearer, body=completion(map_revision))
        private(client, "POST", COMPLETIONS, bearer, body=completion(maps[-1], complete=False))
        private(client, "POST", REWARDS, bearer, 403, body={"reward": CHEST})
        private(client, "POST", COMPLETIONS, bearer, body=completion(maps[-1], long=False))
        private(client, "POST", REWARDS, bearer, 403, body={"reward": CHEST})
        private(client, "POST", COMPLETIONS, bearer, body=completion(maps[-1]))
        assert private(client, "POST", REWARDS, bearer, body={"reward": CHEST})["credited"]
        private(client, "POST", REWARDS, other, 403, body={"reward": CHEST})

        portals = board()
        portals[14], portals[25], portals[200], portals[199] = 0, 8, 8, 7
        paired = revision(portals, 29)
        portals[40] = 8
        ambiguous = revision(portals, 29)
        request(client, "POST", MAPS, headers=ADMIN_HEADERS, body=registration(paired, ambiguous))
        attempt = dict(completion(paired), moves=2, actions="30", trace="0d19c8c7")
        private(client, "POST", COMPLETIONS, bearer, 400, body=dict(attempt, map_hash=ambiguous["map_hash"]))
        assert private(client, "POST", COMPLETIONS, bearer, body=attempt)["created"]


def raw_url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def sodium_fixture():
    # Independently generate actual released XChaCha ciphertext and its
    # nonce-specific opening subkey with the local libsodium runtime.
    library = ctypes.util.find_library("sodium")
    assert library, "local libsodium runtime is required for the migration fixture"
    sodium = ctypes.CDLL(library)
    assert sodium.sodium_init() >= 0
    pointer = ctypes.c_void_p
    length = ctypes.c_ulonglong
    sodium.crypto_aead_xchacha20poly1305_ietf_encrypt.argtypes = [
        pointer, ctypes.POINTER(length), pointer, length, pointer, length, pointer, pointer, pointer]
    sodium.crypto_core_hchacha20.argtypes = [pointer, pointer, pointer, pointer]
    return sodium


def legacy_record(sodium, account, byte, unlocked, completed, aliases=False):
    key = b"q" * 32
    nonce = bytes([byte]) * 24
    aad = {"account_id": account, "app_id": "entity", "collection": COLLECTION, "id": RECORD,
           "schema": "entity.game-progress.v1"}
    bound = compact(aad)
    progress = {"game": GAME, "unlocked": unlocked, "completed": completed,
                "state": "only legacy earned progress is persisted"}
    if aliases:
        progress = {key.upper(): value for key, value in progress.items()}
        progress["unlocked"] = None
    plaintext = compact(progress)
    ciphertext = ctypes.create_string_buffer(len(plaintext) + 16)
    count = ctypes.c_ulonglong()
    assert sodium.crypto_aead_xchacha20poly1305_ietf_encrypt(
        ciphertext, ctypes.byref(count), plaintext, len(plaintext), bound, len(bound), None, nonce, key) == 0
    subkey = ctypes.create_string_buffer(32)
    assert sodium.crypto_core_hchacha20(subkey, nonce[:16], key, None) == 0
    envelope = raw_url(compact({"aad": aad, "alg": "xchacha20poly1305", "key_id": "entity-v1",
                               "profile": "daochi-record-v1", "nonce": raw_url(nonce),
                               "ciphertext": raw_url(ciphertext.raw[:count.value])}))
    return envelope, nonce, {"snapshot": digest(envelope.encode()), "key": subkey.raw.hex()}


def save_record(db, account, record, *, collection=COLLECTION, record_id=RECORD):
    envelope, nonce, _ = record
    db.execute("INSERT INTO server_encrypted_records(user_id_hash,collection,id,key_id,nonce,ciphertext,updated_at,server_version) "
               "VALUES(?,?,?,'entity-v1',?,?,'2026-10-25T00:00:00Z',1) "
               "ON CONFLICT(user_id_hash,collection,id) DO UPDATE SET nonce=excluded.nonce,ciphertext=excluded.ciphertext,"
               "server_version=server_version+1", (account, collection, record_id, raw_url(nonce), envelope))


def test_migration(binary, oracle, root):
    sodium = sodium_fixture()
    database = root / "migration.sqlite"
    settings = {"MESH_TOKEN_ISSUER_PRIVATE_KEY_HEX": SEED}
    with server(binary, database, **settings) as (client, db):
        account, bearer, _ = login(client, oracle)
        other_account, other, _ = login(client, oracle)
        bad_account, bad_bearer, _ = login(client, oracle)
        frozen = legacy_record(sodium, account, 0x31, 74, "1" * 73, aliases=True)
        tampered = legacy_record(sodium, bad_account, 0x34, 4097, "yes")
        save_record(db, account, frozen)
        save_record(db, bad_account, tampered)
        save_record(db, other_account, legacy_record(sodium, other_account, 0x32, 176, "1" * 175), record_id="9" * 32)
        assert private(client, "GET", MIGRATION, bearer) == {"status": "missing"}
        private(client, "POST", MIGRATION, bearer, 403, body=frozen[2])
        # Recreate the marker state from the released database before the upgrade.
        db.execute("DELETE FROM game_legacy_migration")
    with server(binary, database, **settings) as (client, db):
        inflated = legacy_record(sodium, account, 0x33, 176, "1" * 175)
        save_record(db, account, inflated)
        assert private(client, "GET", MIGRATION, bearer) == {
            "status": "available", "ciphertext": frozen[0], "snapshot": frozen[2]["snapshot"], "nonce_hex": frozen[1].hex()}
        assert private(client, "GET", MIGRATION, other) == {"status": "missing"}
        assert db.execute("SELECT COUNT(*) FROM game_legacy_snapshots").fetchone() == (2,)
        request(client, "POST", MIGRATION, 401, body=frozen[2])
        private(client, "POST", MIGRATION, other, 403, body=frozen[2])
        private(client, "POST", MIGRATION, bearer, 403, body=inflated[2])
        private(client, "POST", MIGRATION, bearer, 403, body=dict(frozen[2], key="0" * 64))
        private(client, "POST", MIGRATION, bad_bearer, 403, body=tampered[2])
        for bad in (dict(frozen[2], extra=1), b'{"key":"x","\\u006bey":"y"}', b" " * 1025):
            private(client, "POST", MIGRATION, bearer, 400, body=bad)
        assert db.execute("SELECT COUNT(*) FROM game_legacy_progress").fetchone() == (0,)
        db.execute("CREATE TRIGGER refuse_progress AFTER INSERT ON game_legacy_progress "
                   "BEGIN SELECT RAISE(FAIL,'progress write failure'); END")
        private(client, "POST", MIGRATION, bearer, 403, body=frozen[2])
        assert private(client, "GET", MIGRATION, bearer)["status"] == "available"
        db.execute("DROP TRIGGER refuse_progress")
        assert private(client, "POST", MIGRATION, bearer, body=gzip.compress(compact(frozen[2])),
                       headers={"Content-Encoding": "gzip"}) == {"status": "done"}
        assert private(client, "POST", MIGRATION, bearer, body=frozen[2]) == {"status": "done"}
        assert private(client, "GET", MIGRATION, bearer) == {"status": "done"}
        assert db.execute("SELECT unlocked,completed,snapshot FROM game_legacy_progress").fetchall() == [
            (74, "1" * 73, frozen[2]["snapshot"])]
        assert db.execute("SELECT COUNT(*) FROM game_completions").fetchone() == (0,)
        assert get(client, "/balance", token=bearer)["balance"] == 0
        private(client, "POST", REWARDS, bearer, 403, body={"reward": CHEST})
    with server(binary, database, **settings) as (client, db):
        assert private(client, "GET", MIGRATION, bearer) == {"status": "done"}
        assert db.execute("SELECT ciphertext FROM game_legacy_snapshots WHERE account_id=?", (account,)).fetchone() == (frozen[0],)


if __name__ == "__main__":
    server_binary, signing_oracle = (str(Path(argument).resolve()) for argument in sys.argv[1:])
    with tempfile.TemporaryDirectory(prefix="mesh-native-game-") as folder:
        test_game(server_binary, signing_oracle, Path(folder))
        print("Native game evidence tests passed")
        test_migration(server_binary, signing_oracle, Path(folder))
        print("Native reward migration tests passed")
