#!/usr/bin/env python3
"""Measure native delegated reads with and without the nonce expiry index."""

import argparse
import base64
import random
import sqlite3
import statistics
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests" / "native"))
from check_delegated_http import BOT_TOKEN, SYNC, encode, pairing_node, verify


def measure(_port, database, call, proof, _issue_session, credential,
            _owner_call, grant_id, _bearer, account, read, record):
    entropy = random.Random(34352)
    records = [dict(record, id=f"lumi.message.bench.{index}",
                    nonce=base64.b64encode(entropy.randbytes(24)).decode(),
                    ciphertext=base64.b64encode(entropy.randbytes(4096)).decode())
               for index in range(8)]
    with closing(sqlite3.connect(database)) as db, db:
        watermark = db.execute("SELECT max(server_version) FROM server_encrypted_records WHERE user_id_hash=?",
                               (account,)).fetchone()[0]
        db.execute("""
WITH RECURSIVE ids(n) AS (VALUES(0) UNION ALL SELECT n+1 FROM ids WHERE n+1<32768)
INSERT INTO server_authorization_nonces SELECT ?, printf('%032x',n), ? FROM ids
""", (grant_id["grant_id"], int(time.time()) + 90))

    write = encode(dict(read, read=False, sync=dict(read["sync"], encrypted_records=records)))
    assert call(SYNC, write, 200, proof(write))["applied"] == len(records)
    body = encode(dict(read, limit=len(records), since_version=watermark))
    expected = {row["id"]: row["ciphertext"] for row in records}
    samples = {False: [], True: []}

    def read_once(header):
        started = time.perf_counter_ns()
        result = call(SYNC, body, 200, header)
        duration = (time.perf_counter_ns() - started) / 1_000_000
        assert {row["id"]: row["ciphertext"] for row in result["records"]} == expected
        return duration

    for indexed in (False, True, True, False):
        with closing(sqlite3.connect(database)) as db, db:
            if indexed:
                db.execute("CREATE INDEX IF NOT EXISTS server_authorization_nonces_expiry "
                           "ON server_authorization_nonces(expires_at)")
            else:
                db.execute("DROP INDEX IF EXISTS server_authorization_nonces_expiry")
        # Keep setup and all four batches inside the server's request quota.
        headers = [proof(body) for _ in range(18)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(read_once, headers[:4]))
            samples[indexed].extend(pool.map(read_once, headers[4:]))

    for indexed, timings in samples.items():
        ordered = sorted(timings)
        p95 = ordered[(len(ordered) * 95 + 99) // 100 - 1]
        print(f"expiry_index={indexed} samples={len(ordered)} "
              f"p50_ms={statistics.median(ordered):.3f} p95_ms={p95:.3f}")
    return credential


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("server", type=Path)
    parser.add_argument("signing_oracle", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as directory:
        database = Path(directory) / "delegated-benchmark.db"
        with pairing_node(args.server.resolve(), database, MESH_LUMI_BOT_TOKEN=BOT_TOKEN) as port:
            verify(port, database, args.signing_oracle.resolve(), check_socket=measure)


if __name__ == "__main__":
    main()
