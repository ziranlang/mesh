"""Exercise the native Monero reconciliation worker with loopback providers."""

import sys
import tempfile
import time
from pathlib import Path

from check_exchange_rate_http import Market, NAMES, expect_rate, upstream
from check_monero_http import ADDRESS, DEPOSITS, INVOICES, PRICE, PRODUCT, UNITS, configuration, get, mock_wallet, transfer
from check_token_http import KEY, SPEND, check_receipt, credit, login, post, server, spend_body


def await_row(database, query, expected, arguments=()):
    deadline = time.monotonic() + 9
    while True:
        row = database.execute(query, arguments).fetchone()
        if row == expected:
            return
        if time.monotonic() >= deadline:
            raise AssertionError((query, arguments, row, expected))
        time.sleep(0.05)


def deposit_state(database, tx, status):
    await_row(database, "SELECT status FROM monero_deposits WHERE tx_id=?", (status,), (tx,))


def reconcile_settings(url, **overrides):
    return configuration(url, MESH_MONERO_RECONCILE_SECONDS="1", MESH_ADMIN_TOKEN=KEY, **overrides)


def check_deposits(connection, db, wallet, market, oracle, root):
    initial_rate = expect_rate(connection, 10100)
    account, bearer, _ = login(connection, oracle)
    assert get(connection, ADDRESS, token=bearer)["address"] == "fake-address-1"
    bookmark = "SELECT last_height FROM monero_wallet_state WHERE wallet_id='default'"
    awaiting = transfer("deposit", 1, 99, 1, height=498)
    wallet.set(**{"in": [awaiting, transfer("tiny", 1, 10, height=498)],
                  "pool": [dict(awaiting, height=0, confirmations=0)]})
    await_row(db, bookmark, (500,))
    deposit_state(db, "deposit", "confirming")
    deposit_state(db, "tiny", "below_minimum")
    snapshot = "SELECT token_units,rate_atomic_amount,rate_token_units,usd_cents,usd_updated_at,usd_sources FROM monero_deposits WHERE tx_id='deposit'"
    frozen = db.execute(snapshot).fetchone()
    assert frozen[:4] == (44, 9, 4, 10100) and frozen[5] == "coingecko,kraken"
    assert initial_rate["updated_at"] <= frozen[4] <= int(time.time())

    market.set(coingecko=(200, b'{"monero":{"usd":200}}'),
               coinpaprika=(200, b'{"quotes":{"USD":{"price":202}}}'))
    expect_rate(connection, 20100)
    db.execute("CREATE TRIGGER refuse_deposit_receipt BEFORE UPDATE OF receipt_id ON monero_deposits "
               "BEGIN SELECT RAISE(FAIL,'fixture write failure'); END")
    wallet.height = 501
    mined = dict(awaiting, confirmations=3)
    wallet.set(**{"in": [mined, mined]})
    deposit_state(db, "deposit", "confirmed")
    assert db.execute(bookmark).fetchone() == (500,)
    assert db.execute("SELECT COUNT(*) FROM token_ledger").fetchone() == (0,)
    assert db.execute("SELECT COUNT(*) FROM token_processed_payments").fetchone() == (0,)
    db.execute("DROP TRIGGER refuse_deposit_receipt")
    deposit_state(db, "deposit", "credited")
    await_row(db, bookmark, (501,))
    assert db.execute(snapshot).fetchone() == frozen
    entry = next(item for item in get(connection, DEPOSITS, token=bearer)["deposits"] if item["tx_id"] == "deposit")
    receipt = entry["receipt"]
    assert (entry["amount_atomic"], entry["token_units"], entry["usd_cents"], entry["usd_sources"]) == (99, 44, 10100, "coingecko,kraken")
    assert (receipt["account_id"], receipt["source_type"], receipt["source_ref"], receipt["amount_delta"]) == (account, "monero_account", "deposit:0:1", 44)
    check_receipt(root, receipt, 1, "")
    _, outsider, _ = login(connection, oracle)
    assert get(connection, DEPOSITS, token=outsider) == {"deposits": []}

    wallet.set()
    deposit_state(db, "deposit", "conflict_hold")
    assert db.execute("SELECT COUNT(*) FROM token_ledger").fetchone() == (1,)
    db.execute("UPDATE token_app_permissions SET legacy_unsigned_until=? WHERE app_id='inbe' AND permission='spend'", (int(time.time()) + 300,))
    assert post(connection, SPEND, body=spend_body("funds-before-reorg", 44), token=bearer)["balance"] == 0
    wallet.set(**{"in": [dict(mined, double_spend_seen=True)]})
    deposit_state(db, "deposit", "compensation_hold")
    assert db.execute("SELECT COUNT(*) FROM token_ledger WHERE source_type='monero_reorg'").fetchone() == (0,)
    credit(connection, account, 44, "deposit-recovery", app="")
    deposit_state(db, "deposit", "compensated")
    assert db.execute("SELECT amount_delta,source_ref FROM token_ledger WHERE source_type='monero_reorg'").fetchall() == [(-44, "monero_reorg:deposit:0:1")]

    wallet.height = 507
    money = transfer("rewind", 1, 27, 3, height=505)
    wallet.set(**{"in": [money]})
    deposit_state(db, "rewind", "credited")
    await_row(db, bookmark, (507,))
    wallet.height = 490
    wallet.set()
    deposit_state(db, "rewind", "rollback_hold")
    await_row(db, bookmark, (0,))
    assert db.execute("SELECT COUNT(*) FROM token_ledger WHERE source_type='monero_reorg'").fetchone() == (1,)

    wallet.height = 570
    wallet.set(**{"in": [transfer("final", 1, 27, 65, height=505)]})
    deposit_state(db, "final", "finalized")
    wallet.set()
    return bearer


def check_late_invoice(connection, db, wallet, bearer):
    item = post(connection, INVOICES, 201, body=PRODUCT, token=bearer)
    index, invoice_id = item["address_index"], item["id"]
    db.execute("UPDATE token_payment_intents SET expires_at=strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 minute') WHERE id=?", (invoice_id,))
    state = "SELECT status FROM token_payment_intents WHERE id=?"
    await_row(db, state, ("expired",), (invoice_id,))
    partial = transfer("late-small", index, PRICE - 1)
    wallet.set(**{"in": [partial]})
    await_row(db, state, ("expired",), (invoice_id,))
    assert db.execute("SELECT COUNT(*) FROM token_ledger WHERE source_type='monero'").fetchone() == (0,)
    final = transfer("late-difference", index, 1)
    wallet.set(**{"in": [partial, final]})
    await_row(db, state, ("paid",), (invoice_id,))
    row = db.execute("SELECT receipt_id,provider_payment_id FROM token_payment_intents WHERE id=?", (invoice_id,)).fetchone()
    assert row[1] == f"late-difference+late-small:0:{index}"
    referral = "SELECT state FROM referral_monero_invoices WHERE receipt_id=?"
    await_row(db, referral, ("monitoring",), (row[0],))
    wallet.set(**{"in": [dict(partial, double_spend_seen=True), final]})
    await_row(db, referral, ("compensated",), (row[0],))
    assert db.execute("SELECT amount_delta,source_ref FROM token_ledger WHERE source_type='monero' ORDER BY ledger_seq").fetchall() == [(UNITS, row[1]), (-UNITS, row[1])]


def check_cancellation(binary, oracle, root):
    with mock_wallet() as (wallet, url):
        wallet.mode = "basic"
        with server(binary, root / "cancel.sqlite", **reconcile_settings(url)) as (connection, _):
            _, bearer, _ = login(connection, oracle)
            wallet.block_method = "get_height"
            get(connection, ADDRESS, token=bearer)
            assert wallet.probe.wait(7), "Monero worker did not start the blocking wallet request"
            before = time.monotonic()
        assert time.monotonic() - before < 4, "shutdown waited for the wallet to reply"


def main(binary, oracle):
    binary, oracle = (str(Path(name).resolve()) for name in (binary, oracle))
    with tempfile.TemporaryDirectory(prefix="monero-worker-", dir=Path(binary).parent) as temporary:
        root = Path(temporary)
        market = Market()
        market.set(coingecko=(200, b'{"monero":{"usd":100}}'),
                   kraken=(200, b'{"error":[],"result":{"XXMRZUSD":{"c":["102","0"]}}}'))
        with upstream(market) as market_port, mock_wallet() as (wallet, url):
            wallet.mode = "basic"
            settings = {f"MESH_XMR_{name.upper()}_URL": f"http://127.0.0.1:{market_port}/{name}" for name in NAMES}
            settings.update(MESH_XMR_RATE_REFRESH_ENABLED="true", MESH_XMR_RATE_REFRESH_INTERVAL_SECONDS="1")
            with server(binary, root / "worker.sqlite", **reconcile_settings(url, **settings)) as (connection, db):
                bearer = check_deposits(connection, db, wallet, market, oracle, root)
                check_late_invoice(connection, db, wallet, bearer)
        check_cancellation(binary, oracle, root)
    print("native Monero reconciliation, recovery, market snapshot, and cancellation passed")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(f"usage: {sys.argv[0]} SERVER SIGNING_ORACLE")
    main(*sys.argv[1:])
