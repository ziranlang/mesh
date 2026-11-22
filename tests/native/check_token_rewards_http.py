#!/usr/bin/env python3
"""Exercise native Dew gifts, game rewards, and offline issuance."""
import gzip
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.client import HTTPConnection
from pathlib import Path

from check_token_http import (
    ADMIN_HEADERS, ASSET, KEY, PUBLIC, SEED, check_checkpoint, check_receipt,
    credit, get, login, request, seed_receipt, server, tables_snapshot,
)

GIFT = "/api/v1/tokens/gifts"
REWARD = "/api/v1/rewards"
CAMPAIGN = "shovey.campaign-complete"
CHEST = "shovey.secret-chest"
DAILY = "wurm.daily-05"


def call(connection, path, bearer, payload, status=200, **kwargs):
    response, body = request(connection, "POST", path, status, token=bearer, body=payload, **kwargs)
    assert response.getheader("Cache-Control") == "private, no-store"
    return body


def gift_body(recipient, units, key):
    return {"recipient": recipient, "units": units, "request_id": key}


def parallel(connection, commands):
    def send(command):
        with closing(HTTPConnection(connection.host, connection.port, timeout=15)) as client:
            return call(client, *command)
    with ThreadPoolExecutor(max_workers=len(commands)) as workers:
        return list(workers.map(send, commands))


def check_gifts(binary, oracle, root):
    with server(binary, root / "rewards-disabled.sqlite", MESH_TOKEN_ISSUER_PRIVATE_KEY_HEX=SEED) as (c, _):
        _, bearer, _ = login(c, oracle)
        assert call(c, GIFT, bearer, b"malformed", 403) == {"error": "gifts are paused"}
    with server(binary, root / "rewards-readonly.sqlite", MESH_TOKEN_ISSUER_PUBLIC_KEY_HEX=PUBLIC,
                MESH_DEW_GIFTS_ENABLED="true") as (c, _):
        _, bearer, _ = login(c, oracle)
        assert call(c, GIFT, bearer, gift_body("7" * 64, 1, "readonly-gift"), 503) == {"error": "gifts unavailable"}
    with server(binary, root / "gifts.sqlite", MESH_TOKEN_ISSUER_PRIVATE_KEY_HEX=SEED,
                MESH_DEW_GIFTS_ENABLED="true", MESH_ADMIN_TOKEN=KEY) as (c, db):
        sender, bearer, _ = login(c, oracle)
        recipient, other, _ = login(c, oracle)
        stranger, _, _ = login(c, oracle)
        valid = gift_body(recipient, 25, "first-gift")
        assert request(c, "GET", GIFT, 405)[0].getheader("Allow") == "POST"
        request(c, "POST", GIFT, 401, body=valid)
        wrong = [dict(valid, recipient=sender), dict(valid, units=0), dict(valid, units=1.5),
                 dict(valid, units=2**63), dict(valid, request_id="short"), dict(valid, optional=None),
                 b'{"units":1,"\\u0075nits":2}', b'{"Recipient":"' + recipient.encode() + b'"}',
                 b" " * 1025]
        for malformed in wrong:
            assert call(c, GIFT, bearer, malformed, 400) == {"error": "invalid gift"}
        assert call(c, GIFT, bearer, valid, 402) == {"error": "not enough Dew"}
        initial = credit(c, sender, 60, "fund-gift", app="")["receipt"]
        snapshot = tables_snapshot(db)
        assert call(c, GIFT, bearer, valid, 403) == {"error": "friendship required"}
        assert tables_snapshot(db) == snapshot
        db.execute("INSERT INTO server_friendships(user_id_a,user_id_b) VALUES(?,?)", sorted((sender, recipient)))
        db.execute("CREATE TRIGGER refuse_gift BEFORE INSERT ON token_processed_payments "
                   "WHEN NEW.provider='gift-v1' BEGIN SELECT RAISE(FAIL,'gift credit failure'); END")
        assert call(c, GIFT, bearer, valid, 503) == {"error": "gift unavailable"}
        assert tables_snapshot(db) == snapshot
        db.execute("DROP TRIGGER refuse_gift")
        first, again = parallel(c, [(GIFT, bearer, valid), (GIFT, bearer, valid)])
        assert first == again == dict(valid, balance=35, receipt_id=first["receipt_id"])
        debit = get(c, "/receipts/" + first["receipt_id"])
        credit_id, = db.execute("SELECT receipt_id FROM token_processed_payments WHERE provider='gift-v1' "
                                "AND provider_payment_id=?", (sender + ":first-gift",)).fetchone()
        received = get(c, "/receipts/" + credit_id)
        check_receipt(root, debit, 2, initial["event_hash"])
        check_receipt(root, received, 3, debit["event_hash"])
        assert (debit["account_id"], debit["amount_delta"], debit["app_id"], debit["source_type"], debit["source_ref"]) == (
            sender, -25, "inbe", "gift", recipient)
        assert (received["account_id"], received["amount_delta"], received["app_id"], received["source_type"], received["source_ref"]) == (
            recipient, 25, "inbe", "gift", sender)
        assert get(c, "/balance", token=other)["balance"] == 25
        db.execute("DELETE FROM server_friendships")
        db.execute("UPDATE token_spend_nonces SET request_hash='' WHERE idempotency_key='gift:first-gift'")
        snapshot = tables_snapshot(db)
        assert call(c, GIFT, bearer, valid) == first
        for conflict in (dict(valid, units=1), dict(valid, recipient=stranger)):
            assert call(c, GIFT, bearer, conflict, 409) == {"error": "gift request id reused"}
        assert tables_snapshot(db) == snapshot
        db.execute("INSERT INTO server_friendships(user_id_a,user_id_b) VALUES(?,?)", sorted((sender, recipient)))
        scoped = seed_receipt(db, sender, 4, 100, "inbe", "google_play", "inbe-title-only")
        db.execute("INSERT INTO token_allocations(receipt_id,account_id,asset_id,app_id,source_receipt_id,source_type,"
                   "provider_payment_id,issued,remaining,ledger_seq) VALUES(?,?,?,'inbe',?,'google_play',?,100,100,4)",
                   (scoped, sender, ASSET, scoped, "inbe-title-only"))
        snapshot = tables_snapshot(db)
        assert call(c, GIFT, bearer, gift_body(recipient, 36, "scoped-gift"), 402) == {"error": "not enough Dew"}
        assert tables_snapshot(db) == snapshot


def complete_archive(db, account, levels, complete=1, moves=10):
    rows = [(level, f"{level:064x}") for level in range(1, levels + 1)]
    db.executemany("INSERT OR IGNORE INTO game_maps VALUES('shovey.classic',?,?,'{}',0)", rows)
    db.executemany("INSERT INTO game_completions VALUES(?,?,'shovey.classic',?,?,?,'','',?,'{}',0) "
                   "ON CONFLICT(account_id,id) DO UPDATE SET moves=excluded.moves,evidence_complete=excluded.evidence_complete",
                   [(account, digest, level, digest, moves, complete) for level, digest in rows])


def check_rewards(binary, oracle, root):
    with server(binary, root / "game-rewards.sqlite", MESH_TOKEN_ISSUER_PRIVATE_KEY_HEX=SEED,
                MESH_ADMIN_TOKEN=KEY) as (c, db):
        account, bearer, _ = login(c, oracle)
        other, second, _ = login(c, oracle)
        request(c, "GET", REWARD, 401)
        response, payload = request(c, "GET", REWARD, token=bearer)
        assert response.getheader("Cache-Control") == "private, no-store"
        offered = {entry["reward"]: entry["units"] for entry in payload["rewards"]}
        assert offered == {CHEST: 2000000, CAMPAIGN: 100000000, DAILY: 1000000, "wurm.daily-10": 2000000,
                           "wurm.daily-15": 2000000, "wurm.daily-20": 2000000, "wurm.daily-25": 3000000}
        response, data = request(c, "HEAD", REWARD, token=bearer)
        assert response.getheader("Cache-Control") == "private, no-store" and data is None
        for invalid in ({"Reward": CHEST}, {"reward": CHEST, "units": 100}, {"reward": None},
                        b'{"reward":"wurm.daily-05","\\u0072eward":"wurm.daily-10"}'):
            assert call(c, REWARD, bearer, invalid, 400) == {"error": "invalid reward"}
        assert call(c, REWARD, bearer, {"reward": "wurm.unlisted"}, 404) == {"error": "unknown reward"}
        assert call(c, REWARD, bearer, gzip.compress(b'{"reward":"wurm.daily-05"}'), 403,
                    headers={"Content-Encoding": "gzip"})["error"].startswith("check in on more")
        today, = db.execute("SELECT day FROM reward_checkins WHERE account_id=?", (account,)).fetchone()
        assert abs(today - int(time.time()) // 86400) <= 1
        db.executemany("INSERT INTO reward_checkins VALUES(?,'wurm',?,0)",
                       [(account, today - previous) for previous in (1, 2, 3, 5, 6)])
        assert call(c, REWARD, bearer, {"reward": DAILY}, 403)["error"].startswith("check in on more")
        db.execute("INSERT INTO reward_checkins VALUES(?,'wurm',?,0)", (account, today - 4))
        daily = call(c, REWARD, bearer, {"reward": DAILY})
        assert daily == {"reward": DAILY, "units": 1000000, "credited": True, "balance": 1000000}
        assert call(c, REWARD, bearer, {"reward": DAILY}) == dict(daily, credited=False)
        complete_archive(db, account, 175, complete=0)
        assert call(c, REWARD, bearer, {"reward": CHEST}, 403)["error"].startswith("sync complete evidence")
        complete_archive(db, account, 175, moves=9)
        assert call(c, REWARD, bearer, {"reward": CHEST}, 403)["error"].startswith("sync complete evidence")
        complete_archive(db, account, 175)
        complete_archive(db, other, 175)
        snapshot = tables_snapshot(db)
        db.execute("CREATE TRIGGER refuse_reward BEFORE INSERT ON token_processed_payments "
                   "WHEN NEW.provider='reward-v1' BEGIN SELECT RAISE(FAIL,'reward record failure'); END")
        assert call(c, REWARD, bearer, {"reward": CHEST}, 503) == {"error": "rewards unavailable"}
        assert tables_snapshot(db) == snapshot
        db.execute("DROP TRIGGER refuse_reward")
        chest = call(c, REWARD, bearer, {"reward": CHEST})
        assert (chest["units"], chest["balance"]) == (2000000, 3000000)
        campaign = {"reward": CAMPAIGN}
        first, second_result = parallel(c, [(REWARD, bearer, campaign), (REWARD, second, campaign)])
        assert {first["place"], second_result["place"]} == {1, 2}
        for claimed, token in ((first, bearer), (second_result, second)):
            assert claimed["units"] == 100000000 and claimed["credited"] is True
            assert call(c, REWARD, token, campaign) == dict(claimed, credited=False)
        receipts = db.execute("SELECT receipt_id FROM token_ledger ORDER BY ledger_seq").fetchall()
        previous = ""
        hashes = []
        for sequence, (receipt_id,) in enumerate(receipts, 1):
            receipt = get(c, "/receipts/" + receipt_id)
            assert receipt["source_type"] == "reward" and receipt["event_type"] == "credit"
            check_receipt(root, receipt, sequence, previous)
            previous = receipt["event_hash"]
            hashes.append(previous)
        assert len(hashes) == 4
        checkpoint = request(c, "POST", "/api/v1/admin/tokens/checkpoint", headers=ADMIN_HEADERS)[1]
        check_checkpoint(root, checkpoint, 4, hashes)


def check_cli(binary, oracle, root):
    path = root / "reward-cli.sqlite"
    with server(binary, path, MESH_TOKEN_ISSUER_PRIVATE_KEY_HEX=SEED) as (c, db):
        account, _, _ = login(c, oracle)
        db.execute("UPDATE server_users SET alias='gift_clifixture' WHERE user_id_hash=?", (account,))
        db.execute("CREATE TRIGGER refuse_checkpoint BEFORE INSERT ON token_checkpoints "
                   "BEGIN SELECT RAISE(FAIL,'injected checkpoint failure'); END")
    env = {key: value for key, value in os.environ.items() if not key.startswith("MESH_")}
    env.update(MESH_ADDR="127.0.0.1:0", MESH_DB=str(path), MESH_TOKEN_ISSUER_PRIVATE_KEY_HEX=SEED,
               MESH_NODE_SYNC_INTERVAL_SECONDS="0")

    def issue(*arguments):
        return subprocess.run([binary, "reward-dew", *arguments], env=env, capture_output=True, text=True, timeout=15)

    with closing(sqlite3.connect(path, isolation_level=None)) as db:
        before = tables_snapshot(db)
        assert issue(CAMPAIGN, "@GIFT_CLIFIXTURE", "5").returncode != 0
        assert tables_snapshot(db) == before
        db.execute("DROP TRIGGER refuse_checkpoint")
        assert issue(CAMPAIGN, "missing-person", "5").returncode != 0
        first = issue(CAMPAIGN, "@GIFT_CLIFIXTURE", "5")
        assert first.returncode == 0 and account in first.stdout, first
        replay = issue(CAMPAIGN, account, "5")
        assert replay.returncode == 0 and "already credited" in replay.stdout, replay
        assert issue(CAMPAIGN, account, "6").returncode != 0
        assert db.execute("SELECT COUNT(*) FROM token_ledger").fetchone() == (1,)
        db.execute("UPDATE token_processed_payments SET created_at='2026-10-27 01:02:03'")
    with server(binary, path, MESH_TOKEN_ISSUER_PRIVATE_KEY_HEX=SEED, MESH_ADMIN_TOKEN=KEY) as (c, db):
        original, topup = db.execute("SELECT receipt_id FROM token_ledger ORDER BY ledger_seq").fetchall()
        original, topup = (get(c, "/receipts/" + row[0]) for row in (original, topup))
        check_checkpoint(root, get(c, "/checkpoints/latest"), 1, [original["event_hash"]])
        check_receipt(root, topup, 2, original["event_hash"])
        assert topup["amount_delta"] == 95000000 and topup["source_ref"] == CAMPAIGN
        assert db.execute("SELECT COUNT(*) FROM token_processed_payments WHERE provider_payment_id=?",
                          (CAMPAIGN + ".place:" + account,)).fetchone() == (1,)
    with server(binary, path, MESH_TOKEN_ISSUER_PRIVATE_KEY_HEX=SEED) as (_, db):
        assert db.execute("SELECT COUNT(*) FROM token_ledger").fetchone() == (2,)


def main(binary, oracle):
    binary, oracle = str(Path(binary).resolve()), str(Path(oracle).resolve())
    with tempfile.TemporaryDirectory(prefix="token-rewards-", dir=Path(binary).parent) as temporary:
        root = Path(temporary)
        check_gifts(binary, oracle, root)
        print("Native Dew gift tests passed")
        check_rewards(binary, oracle, root)
        print("Native game reward tests passed")
        check_cli(binary, oracle, root)
        print("Native reward-dew and campaign settlement tests passed")


if __name__ == "__main__":
    main(*sys.argv[1:])
