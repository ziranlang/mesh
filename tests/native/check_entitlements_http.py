"""Verify the native storage plan and unlock routes on a disposable loopback node."""

import base64
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import gzip
import json
import os
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import time

from check_token_http import (
    ADMIN_HEADERS, ASSET, KEY, PUBLIC, SEED, credit, login, post, request,
    seed_receipt, server, verify_signature,
)


PLAN = "/api/v1/storage/plan"
UNLOCKS = "/api/v1/unlocks"
GRANT = UNLOCKS + "/grant"
PRODUCTS = UNLOCKS + "/products"
TAI_CHI = "inbe.practice.taichi"
ISLAND = "shovey.island"
PRIVATE = "private, no-store"
BASE_BYTES = 1000
DEW = 1_000_000


def get(connection, path, *, token=None, cache=PRIVATE):
    response, value = request(connection, "GET", path, token=token)
    assert response.getheader("Cache-Control") == cache
    return value


def count(database, table, owner, field="account_id"):
    return database.execute(f"SELECT COUNT(*) FROM {table} WHERE {field}=?", (owner,)).fetchone()[0]


def configured(**extra):
    defaults = dict(MESH_STORAGE_PLANS_ENABLED="true", MESH_TOKEN_DIRECT_PURCHASES_ENABLED="true",
                    MONERO_RATE_ATOMIC_AMOUNT="1000000000000", MONERO_RATE_TOKEN_UNITS="10000000000",
                    MONERO_WALLET_RPC_URL="http://127.0.0.1:1", MONERO_NETWORK="testnet",
                    MESH_TOKEN_ISSUER_PRIVATE_KEY_HEX=SEED, MESH_ADMIN_TOKEN=KEY,
                    MESH_ACCOUNT_STORAGE_LIMIT_BYTES=str(BASE_BYTES))
    return {**defaults, **extra}


def check_configuration(binary, oracle, root):
    rate = configured(MESH_STORAGE_PLANS_ENABLED=" yes ", MONERO_RATE_ATOMIC_AMOUNT="7", MONERO_RATE_TOKEN_UNITS="3")
    with server(binary, root / "quote.db", **rate) as (connection, _):
        _, token, _ = login(connection, oracle)
        status = get(connection, PLAN, token=token)
        assert status["monthly_xmr_atomic_units"] == 46_666_667
        assert status["yearly_xmr_atomic_units"] == 466_666_667
    environment = {key: value for key, value in os.environ.items() if not key.startswith("MESH_")}
    environment.update(MESH_ADDR="127.0.0.1:0", MESH_DB=str(root / "unconfigured.db"), MESH_TOKEN_SECRET_HEX="00" * 32)
    for overrides, error in (
        ({"MESH_TOKEN_ISSUER_PRIVATE_KEY_HEX": ""}, "storage billing requires the Dew issuer private key"),
        ({"MONERO_RATE_ATOMIC_AMOUNT": "0"}, "storage billing requires valid fixed XMR prices"),
        ({"MONERO_WALLET_RPC_URL": ""}, "storage billing requires Monero top-ups"),
        ({"MONERO_NETWORK": "wrong"}, "storage billing requires an explicit Monero network"),
    ):
        process = subprocess.run([binary], env={**environment, **configured(**overrides)},
                                 capture_output=True, timeout=5)
        assert process.returncode != 0 and error.encode() in process.stderr, process.stderr[:300]


def check_disabled(binary, oracle, root):
    database = root / "disabled.db"
    with server(binary, database, MESH_TOKEN_ISSUER_PUBLIC_KEY_HEX=PUBLIC,
                MONERO_RATE_ATOMIC_AMOUNT="0", MONERO_RATE_TOKEN_UNITS="0") as (connection, _):
        prices = get(connection, PRODUCTS, cache="no-store")
        assert prices == {"products": [
            {"product": name, "price_units": regular, "regular_price_units": regular, "trial_until": 0}
            for name, regular in ((TAI_CHI, 100 * DEW), (ISLAND, 85 * DEW))
        ]}
        for path, method in ((PLAN, "GET"), (PLAN, "POST"), (UNLOCKS, "GET"), (UNLOCKS, "POST"), (GRANT, "POST")):
            _, error = request(connection, method, path, 401, headers=ADMIN_HEADERS)
            assert error == {"error": "bearer token required"}
        owner, token, _ = login(connection, oracle)
        status = get(connection, PLAN, token=token)
        assert status["account_id"] == owner and status["asset_id"] == ASSET and status["enabled"] is False
        assert status["plan"] == {"size": "", "period": "", "expires_at": 0, "limit_bytes": 0, "balance": 0}
        assert status["free_bytes"] == status["used_bytes"] == 0
        assert status["monthly_units"] == 20 * DEW and status["yearly_units"] == 200 * DEW
        assert status["monthly_xmr_atomic_units"] == status["yearly_xmr_atomic_units"] == 0
        assert get(connection, UNLOCKS, token=token) == {"unlocks": [], **prices}
        response, empty = request(connection, "HEAD", PRODUCTS)
        assert empty is None and response.getheader("Cache-Control") == "no-store"
        response, _ = request(connection, "GET", GRANT, 405)
        assert response.getheader("Allow") == "POST"
        assert post(connection, PLAN, 503, body={}, token=token) == {"error": "storage upgrades unavailable"}
        assert post(connection, GRANT, 403, body={"product": TAI_CHI}, token=token) == {"error": "product not unlocked"}


def debit_audit(database, owner, source):
    return database.execute("SELECT app_id,amount_delta,source_ref,receipt_id FROM token_ledger "
                            "WHERE account_id=? AND source_type=? AND event_type='debit' ORDER BY ledger_seq",
                            (owner, source)).fetchall()


def check_storage(connection, database, owner, token, oracle):
    upgrade = {"period": "monthly", "request_id": "fixture-month"}
    for invalid in (b'{"period":"monthly","PERIOD":"yearly","request_id":"x"}',
                    b'{"period":"monthly","period":"monthly","request_id":"x"}',
                    b'{"period":"monthly","size":"2gb","request_id":"x"}',
                    b'{"period":true,"request_id":"x"}', b'{"period":"monthly","request_id":"../x"}',
                    b" " * 1025):
        assert post(connection, PLAN, 400, body=invalid, token=token) == {"error": "invalid storage upgrade"}
    assert post(connection, PLAN, 402, body=upgrade, token=token) == {"error": "not enough Dew"}
    initial = get(connection, PLAN, token=token)
    assert initial["enabled"] is True and initial["free_bytes"] == BASE_BYTES and initial["units_per_dew"] == DEW
    assert initial["monthly_xmr_atomic_units"] == 2_000_000_000
    assert initial["plans"] == [
        dict(size=f"{gib}gb", limit_bytes=gib * 2**30, monthly_units=units * DEW, yearly_units=units * 10 * DEW,
             monthly_xmr_atomic_units=units * 100_000_000, yearly_xmr_atomic_units=units * 1_000_000_000)
        for gib, units in ((1, 20), (5, 60))
    ]
    scoped_owner, scoped_token, _ = login(connection, oracle)
    seq = database.execute("SELECT COALESCE(MAX(ledger_seq),0) + 1 FROM token_ledger").fetchone()[0]
    scoped = seed_receipt(database, scoped_owner, seq, 100 * DEW, "inbe", "google_play", "scoped-only")
    database.execute("INSERT INTO token_allocations(receipt_id,account_id,asset_id,app_id,source_receipt_id,source_type,"
                     "provider_payment_id,issued,remaining,ledger_seq) VALUES(?,?,?,'inbe',?,'google_play','scoped-only',?,?,?)",
                     (scoped, scoped_owner, ASSET, scoped, 100 * DEW, 100 * DEW, seq))
    assert post(connection, PLAN, 402, body=upgrade, token=scoped_token) == {"error": "not enough Dew"}
    assert debit_audit(database, scoped_owner, "storage") == []
    credit(connection, owner, 400 * DEW, "storage-top-up")
    loaded = post(connection, PLAN, body=gzip.compress(json.dumps(upgrade).encode()), token=token,
                  headers={"Content-Encoding": "gzip"})
    plan = loaded["plan"]
    assert plan["size"] == "1gb" and plan["period"] == "monthly" and plan["limit_bytes"] == 2**30
    assert plan["balance"] == 380 * DEW and plan["expires_at"] > int(time.time())
    assert post(connection, PLAN, body=upgrade, token=token) == loaded
    assert post(connection, PLAN, 409, body={**upgrade, "size": "5gb"}, token=token) == {"error": "storage request id reused"}
    debits = debit_audit(database, owner, "storage")
    assert len(debits) == 1 and debits[0][:3] == ("inbe", -20 * DEW, upgrade["request_id"])
    assert database.execute("SELECT receipt_id FROM server_storage_purchases WHERE user_id=?", (owner,)).fetchone() == (debits[0][3],)

    run = "UPDATE server_storage_plans SET expires_at=? WHERE user_id=?"
    database.execute(run, (int(datetime(2096, 1, 31, 10, tzinfo=timezone.utc).timestamp()), owner))
    renewal = {"period": "monthly", "request_id": "leap-renewal"}
    updated = post(connection, PLAN, body=renewal, token=token)["plan"]
    assert updated["expires_at"] == int(datetime(2096, 2, 29, 10, tzinfo=timezone.utc).timestamp())

    database.execute("CREATE TRIGGER fail_plan AFTER INSERT ON server_storage_purchases "
                     "BEGIN SELECT RAISE(FAIL,'fixture purchase failure'); END")
    attempted = {"size": "5gb", "period": "monthly", "request_id": "rollback"}
    assert post(connection, PLAN, 503, body=attempted, token=token) == {"error": "storage upgrade unavailable"}
    assert get(connection, PLAN, token=token)["plan"] == updated
    assert count(database, "server_storage_purchases", owner, "user_id") == 2
    assert len(debit_audit(database, owner, "storage")) == 2
    assert database.execute("SELECT COUNT(*) FROM token_spend_nonces WHERE account_id=? AND idempotency_key='rollback'",
                            (owner,)).fetchone() == (0,)
    database.execute("DROP TRIGGER fail_plan")
    switched = post(connection, PLAN, body=attempted, token=token)["plan"]
    assert switched["size"] == "5gb" and switched["balance"] == 300 * DEW and switched["limit_bytes"] == 5 * 2**30
    assert switched["expires_at"] < updated["expires_at"]
    database.execute(run, (1, owner))
    assert get(connection, PLAN, token=token)["plan"]["limit_bytes"] == BASE_BYTES


def check_unlocks(connection, database, owner, token, trial_end, root, oracle):
    other, outsider, _ = login(connection, oracle)
    quote = get(connection, PRODUCTS, cache="no-store")["products"]
    assert [(row["product"], row["price_units"], row["trial_until"]) for row in quote] == [
        (TAI_CHI, 60 * DEW, trial_end), (ISLAND, 51 * DEW, 0),
    ]
    assert post(connection, UNLOCKS, 402, body={"product": TAI_CHI}, token=outsider) == {"error": "insufficient balance"}
    assert post(connection, UNLOCKS, 404, body={"product": "not.sold"}, token=token) == {"error": "unknown product"}
    assert post(connection, UNLOCKS, 409, body={"product": ISLAND, "expected_units": 85 * DEW}, token=token) == {
        "error": "price changed; confirm a fresh quote"}
    for body in (b'{"product":"inbe.practice.taichi","expected_units":1.5}',
                 b'{"product":"inbe.practice.taichi","price_units":1}'):
        assert post(connection, UNLOCKS, 400, body=body, token=token) == {"error": "invalid unlock"}

    database.execute("CREATE TRIGGER fail_unlock AFTER INSERT ON account_unlocks "
                     "BEGIN SELECT RAISE(FAIL,'fixture unlock failure'); END")
    assert post(connection, UNLOCKS, 503, body={"product": TAI_CHI}, token=token) == {"error": "unlock unavailable"}
    assert debit_audit(database, owner, "unlock") == []
    assert count(database, "account_unlocks", owner) == 0
    database.execute("DROP TRIGGER fail_unlock")
    tai_chi = post(connection, UNLOCKS, body={"product": TAI_CHI}, token=token)
    assert tai_chi["product"] == TAI_CHI and tai_chi["unlocked"] is True and tai_chi["price_units"] == 60 * DEW
    assert tai_chi["balance"] == 240 * DEW
    assert tai_chi == post(connection, UNLOCKS, body={"product": TAI_CHI}, token=token)
    database.execute("DELETE FROM account_unlocks WHERE account_id=? AND product=?", (owner, TAI_CHI))
    assert tai_chi == post(connection, UNLOCKS, body={"product": TAI_CHI}, token=token)
    island = post(connection, UNLOCKS, body={"product": ISLAND, "expected_units": 51 * DEW}, token=token)
    assert island["price_units"] == 51 * DEW and island["balance"] == 189 * DEW
    assert island == post(connection, UNLOCKS, body={"product": ISLAND}, token=token)
    assert debit_audit(database, owner, "unlock") == [
        ("inbe", -60 * DEW, TAI_CHI, tai_chi["receipt_id"]), ("shovey", -51 * DEW, ISLAND, island["receipt_id"]),
    ]
    listed = get(connection, UNLOCKS, token=token)
    assert listed["products"] == quote
    assert {row["product"]: row["receipt_id"] for row in listed["unlocks"]} == {
        TAI_CHI: tai_chi["receipt_id"], ISLAND: island["receipt_id"],
    }
    assert get(connection, UNLOCKS, token=outsider)["unlocks"] == []
    assert count(database, "account_unlocks", other) == 0
    database.execute("INSERT INTO token_spend_nonces(account_id,app_id,idempotency_key,receipt_id,request_hash) "
                     "VALUES(?, 'entity', 'unlock:shovey.island', ?, '')", (other, island["receipt_id"]))
    assert post(connection, UNLOCKS, 503, body={"product": ISLAND}, token=outsider) == {"error": "unlock unavailable"}
    assert count(database, "account_unlocks", other) == 0
    assert post(connection, GRANT, 403, body={"product": TAI_CHI}, token=outsider) == {"error": "product not unlocked"}
    grant = post(connection, GRANT, body={"product": TAI_CHI}, token=token)
    encoded, signature = grant["grant"].split(".")
    assert "=" not in grant["grant"] and abs(grant["expires_at"] - int(time.time()) - 3600) < 5
    payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    assert payload == f"daochi-pack-grant-v1\n{TAI_CHI}\n{grant['expires_at']}".encode()
    verify_signature(root, payload, base64.urlsafe_b64decode(signature + "==").hex())


def check_paid(binary, oracle, root):
    database = root / "paid.db"
    now = int(time.time())
    settings = configured(MESH_UNLOCK_TRIAL_UNTIL=str(now + 3600), MESH_SHOVEY_ISLAND_LAUNCH_AT=str(now - 60))
    with server(binary, database, **settings) as (connection, db):
        owner, token, _ = login(connection, oracle)
        check_storage(connection, db, owner, token, oracle)
        check_unlocks(connection, db, owner, token, now + 3600, root, oracle)

    # Recreate a pre-size schema with populated Go-compatible plan rows.
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("DELETE FROM server_storage_purchases WHERE user_id=?", (owner,))
        db.execute("ALTER TABLE server_storage_plans DROP COLUMN size")
        db.execute("ALTER TABLE server_storage_purchases DROP COLUMN size")
        db.execute("UPDATE server_storage_plans SET expires_at=4102444800 WHERE user_id=?", (owner,))
        db.execute("INSERT INTO server_storage_purchases(user_id,request_id,period,expires_at,receipt_id) "
                   "VALUES(?, 'legacy-purchase', 'monthly', 4102444800, 'old-receipt')", (owner,))
    with server(binary, database, **settings) as (connection, db):
        plan = get(connection, PLAN, token=token)["plan"]
        assert plan["size"] == "1gb" and plan["limit_bytes"] == 2**30 and plan["balance"] == 189 * DEW
        assert db.execute("SELECT size FROM server_storage_purchases WHERE request_id='legacy-purchase'").fetchone() == ("1gb",)
        assert post(connection, PLAN, body={"period": "monthly", "request_id": "legacy-purchase"}, token=token)["plan"] == plan
        assert len(debit_audit(db, owner, "storage")) == 3


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(f"usage: {sys.argv[0]} SERVER SIGNING_ORACLE")
    binary, oracle = (Path(name).resolve() for name in sys.argv[1:])
    with TemporaryDirectory(prefix="entitlements-", dir=binary.parent) as directory:
        root = Path(directory)
        check_disabled(binary, oracle, root)
        check_configuration(binary, oracle, root)
        check_paid(binary, oracle, root)
    print("native storage plan and unlock HTTP routes passed")
