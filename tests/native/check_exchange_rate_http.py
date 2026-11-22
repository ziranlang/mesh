"""Verify native rate aggregation and lifecycle using only loopback providers."""

from contextlib import closing, contextmanager
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import select
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event, Lock, Thread
import time


PATH = "/api/v1/rates"
NAMES = ("coingecko", "kraken", "coinpaprika")
MISSING = dict(xmr_usd_cents=0, updated_at=0, sources=[], xmr_atomic_units=7, token_units=3)


class Market:
    def __init__(self):
        self.lock = Lock()
        self.entered = Event()
        self.release = Event()
        self.responses = {}

    def set(self, **responses):
        with self.lock:
            self.responses = responses
        self.entered.clear()

    def get(self, name):
        with self.lock:
            return self.responses.get(name, (503, b""))


@contextmanager
def upstream(market):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            status, payload = market.get(self.path.removeprefix("/"))
            if status == 0:
                market.entered.set()
                market.release.wait(12)
                return
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            yield server.server_port
        finally:
            market.release.set()
            server.shutdown()
            thread.join()


@contextmanager
def node(binary, database, market_port, **overrides):
    environment = {key: value for key, value in os.environ.items() if not key.startswith(("MESH_", "MONERO_"))}
    environment.update(MESH_ADDR="127.0.0.1:0", MESH_DB=str(database), MESH_LAN_DISCOVERY="false",
                       MESH_XMR_RATE_REFRESH_INTERVAL_SECONDS="1", MESH_TOKEN_SECRET_HEX="01" * 32,
                       MONERO_RATE_ATOMIC_AMOUNT="7", MONERO_RATE_TOKEN_UNITS="3",
                       no_proxy="127.0.0.1,localhost,::1", NO_PROXY="127.0.0.1,localhost,::1")
    for name in NAMES:
        environment[f"MESH_XMR_{name.upper()}_URL"] = f"http://127.0.0.1:{market_port}/{name}"
    with subprocess.Popen([binary], env=environment | overrides, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True) as process:
        try:
            assert select.select([process.stdout], [], [], 8)[0], "listener startup timed out"
            port = process.stdout.readline().strip()
            assert port.isdecimal(), f"listener did not announce its port: {port!r}"
            with closing(HTTPConnection("127.0.0.1", int(port), timeout=5)) as connection:
                yield connection
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                _, errors = process.communicate(timeout=4)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise AssertionError("rate refresh prevented timely shutdown") from None
            assert process.returncode == 0, (process.returncode, errors[-3000:])


def rate(connection):
    connection.request("GET", PATH)
    reply = connection.getresponse()
    assert reply.status == 200
    assert reply.getheader("Cache-Control") == "public, max-age=300"
    assert reply.getheader("Content-Type") == "application/json"
    return json.loads(reply.read())


def expect_rate(connection, expected):
    limit = time.monotonic() + 8
    while True:
        value = rate(connection)
        if value["xmr_usd_cents"] == expected:
            return value
        if time.monotonic() >= limit:
            raise AssertionError((expected, value))
        time.sleep(0.05)


def saved(database):
    with closing(sqlite3.connect(database)) as connection:
        return connection.execute("SELECT usd_cents, updated_at, sources FROM exchange_rates WHERE pair='XMR/USD'").fetchone()


def check(binary, database, market, port):
    market.set(coingecko=(200, b'{"monero":{"usd":100.125}}'),
               kraken=(200, b'{"error":[],"result":{"XXMRZUSD":{"c":["101.125","unused"]}}}'),
               coinpaprika=(200, b'{"quotes":{"USD":{"price":500}}}'))
    with node(binary, database, port) as connection:
        value = expect_rate(connection, 10063)
        names = ["coingecko", "kraken"]
        assert value == MISSING | dict(xmr_usd_cents=10063, updated_at=value["updated_at"], sources=names)
        assert abs(value["updated_at"] - int(time.time())) <= 8
        assert saved(database) == (10063, value["updated_at"], ",".join(names))

        connection.request("HEAD", PATH)
        reply = connection.getresponse()
        assert (reply.status, reply.getheader("Cache-Control"), reply.read()) == (200, "public, max-age=300", b"")
        connection.request("POST", PATH)
        reply = connection.getresponse()
        assert (reply.status, reply.getheader("Allow"), reply.read()) == (405, "GET, HEAD", b"")

        market.set(coingecko=(200, b'{"monero":{"usd":200}}'), kraken=(503, b""),
                   coinpaprika=(200, b'{"quotes":{"USD":{"price":202}}}'))
        kept = expect_rate(connection, 20100)
        assert kept["sources"] == ["coingecko", "coinpaprika"]
        assert saved(database) == (20100, kept["updated_at"], "coingecko,coinpaprika")

        market.set(coingecko=(200, b'{"monero":{"usd":2000}}'), kraken=(200, b"a" * 65537),
                   coinpaprika=(200, b'{"quotes":{"USD":{"price":1e30}}}'))
        time.sleep(1.3)
        assert rate(connection) == kept

    market.set(coingecko=(0, b""))
    with node(binary, database, port) as connection:
        assert market.entered.wait(4), "expected an in-flight rate request"
        assert rate(connection) == kept
    assert saved(database) == (20100, kept["updated_at"], "coingecko,coinpaprika")
    market.release.set()

    market.set()
    for offset in (-7201, 330):
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("UPDATE exchange_rates SET updated_at=?", (int(time.time()) + offset,))
        with node(binary, database, port) as connection:
            assert rate(connection) == MISSING

    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("UPDATE exchange_rates SET updated_at=?", (int(time.time()),))
    market.set(coingecko=(0, b""))
    with node(binary, database, port, MESH_XMR_RATE_REFRESH_ENABLED="false", MONERO_RATE_TOKEN_UNITS="0") as connection:
        assert rate(connection) == MISSING | dict(xmr_atomic_units=0, token_units=0)
        assert not market.entered.wait(0.3)


def main():
    binary = Path(sys.argv[1]).resolve()
    market = Market()
    with TemporaryDirectory(dir=binary.parent) as directory, upstream(market) as port:
        check(binary, Path(directory) / "rates.db", market, port)
    print("Native exchange rates, restart, and refresh cancellation passed")


if __name__ == "__main__":
    main()
