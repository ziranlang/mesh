"""Check native alias ownership, paid claims, and legacy migration over loopback."""

import json
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.client import HTTPConnection
from pathlib import Path
from threading import Barrier
from urllib.parse import quote as escape

from check_token_http import KEY, PUBLIC, SEED, TOKENS, check_receipt, credit, login, post, request, server


ALIAS = "/api/v1/account/alias"
PURCHASE = ALIAS + "/purchase"
DEW = 1_000_000


def quote(connection, bearer, alias, status=200):
    response, result = request(connection, "GET", ALIAS + "?alias=" + escape(alias), status, token=bearer)
    assert response.getheader("Cache-Control") == "private, no-store"
    return result


def assign(connection, account, bearer, alias, status=200):
    return post(connection, ALIAS, status, token=bearer, body={"alias": alias, "user_id_hash": account},
                headers={"X-Mesh-User": account})


def claim(connection, bearer, alias, price, key, status=200):
    response, result = request(connection, "POST", PURCHASE, status, token=bearer,
                               body={"alias": alias, "price_units": price, "request_id": key})
    assert response.getheader("Cache-Control") == "private, no-store"
    return result


def stored_alias(database, account):
    return database.execute("SELECT alias FROM server_users WHERE user_id_hash=?", (account,)).fetchone()[0]


def balance(connection, bearer):
    return request(connection, "GET", TOKENS + "/balance", token=bearer)[1]["balance"]


def check_disabled_and_migration(binary, oracle, root):
    path = root / "legacy.sqlite"
    with server(binary, path) as (connection, database):
        first, bearer, _ = login(connection, oracle)
        second, other, _ = login(connection, oracle)
        assert quote(connection, None, "google", 401) == {"error": "bearer token required"}
        assert claim(connection, None, "google", 0, "needs-bearer", 401) == {"error": "bearer token required"}
        assert post(connection, ALIAS, 400, body={"alias": "google"}, token=bearer) == {"error": "missing X-Mesh-User"}
        assert post(connection, ALIAS, 400, body={"alias": "google", "user_id_hash": first}, token=bearer,
                    headers={"X-Ksync-User": second}) == {"error": "X-Ksync-User does not match user_id_hash"}
        assert assign(connection, first, other, "google", 401) == {"error": "token user mismatch"}
        assert quote(connection, bearer, " @GoOgLe ") == {
            "alias": "google", "enabled": False, "available": True, "owned": False, "premium": False,
            "category": "standard", "price_units": 0, "units_per_dew": DEW, "minimum_length": 4, "maximum_length": 32,
        }
        assert post(connection, ALIAS, token=bearer, headers={"X-Inbe-User": first.upper()},
                    body={"ALIAS": " @GoOgLe ", "USER_ID_HASH": first.upper(), "ignored": True}) == {
                        "status": "ok", "alias": "google"}
        assert assign(connection, first, bearer, " @GOOGle ") == {"status": "ok", "alias": "GOOGle"}
        assert assign(connection, second, other, "Google", 409) == {"error": "alias unavailable"}
        assert claim(connection, other, "abcd", 0, "disabled-free") == {"alias": "abcd", "price_units": 0}
        assert database.execute("SELECT COUNT(*) FROM token_ledger").fetchone() == (0,)
        database.execute("DROP INDEX server_users_alias_case_unique")
        database.execute("UPDATE server_users SET alias='OldMixedCase', created_at='2024-01-01T00:00:00Z' "
                         "WHERE user_id_hash=?", (first,))
        database.execute("UPDATE server_users SET alias='oldmixedcase', created_at='2025-01-01T00:00:00Z' "
                         "WHERE user_id_hash=?", (second,))
    with server(binary, path) as (connection, database):
        assert stored_alias(database, first) == "OldMixedCase"
        assert stored_alias(database, second) is None
        assert database.execute("SELECT account_id,previous_alias,normalized_alias,retained_account_id "
                                "FROM server_alias_case_collisions").fetchall() == [(second, "oldmixedcase", "oldmixedcase", first)]
        assert quote(connection, bearer, "OLDMIXEDCASE")["owned"]
        assert not quote(connection, other, "oldmixedcase")["available"]
        assert assign(connection, second, other, "oldmixedcase", 409) == {"error": "alias unavailable"}
        database.execute("UPDATE server_users SET alias='Abc' WHERE user_id_hash=?", (first,))
        assert quote(connection, bearer, "ABC")["owned"]
        assert assign(connection, first, bearer, "aBc") == {"status": "ok", "alias": "aBc"}


def check_claims(binary, oracle, root):
    with server(binary, root / "paid.sqlite", MESH_PREMIUM_ALIASES_ENABLED="true",
                MESH_TOKEN_ISSUER_PRIVATE_KEY_HEX=SEED, MESH_ADMIN_TOKEN=KEY) as (connection, database):
        first, bearer, _ = login(connection, oracle)
        second, other, _ = login(connection, oracle)
        for raw, alias, category, units in ((" @GoOgLe ", "google", "brand", 1000), ("JAMES", "james", "common", 250),
                                            ("12_a", "12_a", "four_character", 100), ("google_fan", "google_fan", "standard", 0)):
            assert quote(connection, bearer, raw) == {
                "alias": alias, "enabled": True, "available": True, "owned": False, "premium": bool(units),
                "category": category, "price_units": units * DEW, "units_per_dew": DEW,
                "minimum_length": 4, "maximum_length": 32,
            }
        for value in ("", "abc", "Kryon", "İnbe", "\u00a0google", "abc/def"):
            assert quote(connection, bearer, value, 400) == {"error": "alias quote unavailable"}
            assert assign(connection, first, bearer, value, 400) == {"error": "invalid alias"}
        for body in (b'{"alias":"google","alias":"ziran","request_id":"invalid-key"}',
                     b'{"alias":"google","price_units":1.0,"request_id":"invalid-key"}',
                     b'{"alias":"google","price_units":-1,"request_id":"invalid-key"}',
                     b'{"alias":"google","request_id":"invalid-key","extra":0}',
                     b'{"alias":"google","request_id":"short"}', b"{}" + b" " * 2047):
            assert post(connection, PURCHASE, 400, body=body, token=bearer) == {"error": "invalid alias purchase"}
        assert assign(connection, first, bearer, "google", 402) == {
            "error": "premium alias requires explicit purchase at the quoted price"}
        assert claim(connection, bearer, "google", DEW, "stale-quote", 409) == {
            "error": "alias price changed; request a new quote"}
        assert claim(connection, bearer, "google", 1000 * DEW, "no-tokens", 402) == {"error": "insufficient balance"}
        assert stored_alias(database, first) is None
        previous = credit(connection, first, 1500 * DEW, "first-funds")["receipt"]
        paid = claim(connection, bearer, " @GOOGle ", 1000 * DEW, "google-claim")
        assert paid == {"alias": "google", "price_units": 1000 * DEW, "receipt_id": paid["receipt_id"]}
        receipt = request(connection, "GET", TOKENS + "/receipts/" + paid["receipt_id"])[1]
        assert (receipt["account_id"], receipt["app_id"], receipt["source_type"], receipt["source_ref"],
                receipt["amount_delta"]) == (first, "entity", "username", "google", -1000 * DEW)
        check_receipt(root, receipt, 2, previous["event_hash"])
        assert balance(connection, bearer) == 500 * DEW
        assert claim(connection, bearer, "GOOGLE", 1000 * DEW, "google-claim") == paid
        assert claim(connection, bearer, "ziran", 1000 * DEW, "google-claim", 409) == {
            "error": "request_id reused for a different claim"}
        assert claim(connection, bearer, "google", DEW, "google-claim", 409) == {
            "error": "request_id reused for a different claim"}
        assert assign(connection, first, bearer, "Google") == {"status": "ok", "alias": "Google"}
        assert claim(connection, bearer, "google", 0, "retained-name") == {"alias": "google", "price_units": 0}
        owned = quote(connection, bearer, "google")
        assert owned["owned"] and not owned["available"] and owned["premium"] and owned["price_units"] == 0
        taken = quote(connection, other, "google")
        assert not taken["owned"] and not taken["available"] and taken["price_units"] == 1000 * DEW
        assert claim(connection, other, "GOOGLE", 1000 * DEW, "contested-name", 409) == {"error": "alias unavailable"}
        assert assign(connection, first, bearer, "fresh_alias") == {"status": "ok", "alias": "fresh_alias"}
        assert claim(connection, bearer, "google", 1000 * DEW, "google-claim", 409) == {
            "error": "claim already completed; alias changed"}
        assert balance(connection, bearer) == 500 * DEW
        check_atomic_retry(connection, database, first, bearer)
        credit(connection, second, 400 * DEW, "second-funds")
        check_concurrent_claim(connection.port, database, ((first, bearer), (second, other)))


def check_atomic_retry(connection, database, account, bearer):
    database.execute("CREATE TRIGGER fail_alias_claim AFTER INSERT ON token_spend_nonces "
                     "WHEN NEW.idempotency_key='username:rollback-claim' "
                     "BEGIN SELECT RAISE(FAIL,'reject idempotency record'); END")
    assert claim(connection, bearer, "james", 250 * DEW, "rollback-claim", 503) == {"error": "alias purchase unavailable"}
    assert stored_alias(database, account) == "fresh_alias"
    assert balance(connection, bearer) == 500 * DEW
    assert database.execute("SELECT COUNT(*) FROM token_spend_nonces WHERE idempotency_key='username:rollback-claim'").fetchone() == (0,)
    assert database.execute("SELECT COUNT(*) FROM token_ledger WHERE source_type='username' AND source_ref='james'").fetchone() == (0,)
    database.execute("DROP TRIGGER fail_alias_claim")
    assert claim(connection, bearer, "james", 250 * DEW, "rollback-claim")["price_units"] == 250 * DEW
    assert stored_alias(database, account) == "james"
    assert balance(connection, bearer) == 250 * DEW


def check_concurrent_claim(port, database, accounts):
    ready = Barrier(2, timeout=5)

    def compete(pair):
        account, bearer = pair
        body = json.dumps({"alias": "zzzz", "price_units": 100 * DEW, "request_id": "race-claim-" + account[:10]})
        with closing(HTTPConnection("127.0.0.1", port, timeout=15)) as connection:
            ready.wait()
            connection.request("POST", PURCHASE, body=body, headers={"Authorization": "Bearer " + bearer})
            response = connection.getresponse()
            return response.status, json.loads(response.read()), account

    with ThreadPoolExecutor(max_workers=2) as executor:
        winner, loser = sorted(executor.map(compete, accounts), key=lambda result: result[0])
    assert winner[0] == 200 and winner[1]["price_units"] == 100 * DEW, winner
    assert loser[:2] == (409, {"error": "alias unavailable"}), loser
    assert stored_alias(database, winner[2]) == "zzzz" and stored_alias(database, loser[2]) != "zzzz"
    assert database.execute("SELECT account_id,amount_delta FROM token_ledger WHERE source_type='username' AND source_ref='zzzz'").fetchall() == [
        (winner[2], -100 * DEW)]


def check_read_only(binary, oracle, root):
    with server(binary, root / "read-only.sqlite", MESH_PREMIUM_ALIASES_ENABLED="true",
                MESH_TOKEN_ISSUER_PUBLIC_KEY_HEX=PUBLIC) as (connection, database):
        account, bearer, _ = login(connection, oracle)
        assert quote(connection, bearer, "google")["price_units"] == 1000 * DEW
        assert claim(connection, bearer, "google", 1000 * DEW, "read-only-claim", 503) == {"error": "alias purchase unavailable"}
        assert stored_alias(database, account) is None
        assert claim(connection, bearer, "free_alias", 0, "read-only-free") == {"alias": "free_alias", "price_units": 0}


def main(binary, oracle):
    binary, oracle = str(Path(binary).resolve()), str(Path(oracle).resolve())
    with tempfile.TemporaryDirectory(prefix="aliases-", dir=Path(binary).parent) as temporary:
        root = Path(temporary)
        check_disabled_and_migration(binary, oracle, root)
        check_claims(binary, oracle, root)
        check_read_only(binary, oracle, root)
    print("native alias quotes, assignment, paid replay, rollback, and concurrent claims passed")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(f"usage: {sys.argv[0]} SERVER SIGNING_ORACLE")
    main(*sys.argv[1:])
