"""Check the released docs representations on a disposable native listener."""

import json
import os
import re
import select
import sqlite3
import subprocess
import sys
from contextlib import closing
from http.client import HTTPConnection
from pathlib import Path
from tempfile import TemporaryDirectory


def request(connection, method, path):
    connection.request(method, path, headers={"Accept-Encoding": "zstd, gzip"})
    response = connection.getresponse()
    return response.status, response.headers, response.read()


def check_docs(port, expected_spec, database):
    with closing(HTTPConnection("127.0.0.1", port, timeout=3)) as connection:
        routes = {
            "/": "text/html; charset=utf-8",
            "/openapi.json": "application/vnd.oai.openapi+json",
        }
        for path, content_type in routes.items():
            status, headers, body = request(connection, "GET", path + "?source=test")
            assert status == 200, (path, status)
            assert headers["Content-Type"] == content_type, (path, headers)
            assert "Content-Encoding" not in headers, (path, headers)
            assert headers["Cache-Control"] == "no-store", (path, headers)
            assert headers["X-Content-Type-Options"] == "nosniff", (path, headers)

            status, head_headers, head_body = request(connection, "HEAD", path)
            assert (status, head_body) == (200, b""), (path, status, head_body)
            assert head_headers["Content-Type"] == content_type, (path, head_headers)
            assert "Content-Encoding" not in head_headers, (path, head_headers)
            assert int(head_headers["Content-Length"]) == len(body), (path, head_headers)

            status, error_headers, error = request(connection, "POST", path)
            assert (status, error) == (405, b"Method Not Allowed\n"), (path, status, error)
            assert error_headers["Allow"] == "GET, HEAD", (path, error_headers)
            assert error_headers["Content-Type"] == "text/plain; charset=utf-8", (path, error_headers)

            status, _, options_body = request(connection, "OPTIONS", path)
            assert (status, options_body) == (204, b""), (path, status, options_body)

            if path == "/":
                assert body.startswith(b'<!doctype html>\n<html lang="en">\n')
                assert b'<title>Mesh</title>' in body
                assert b'href="/openapi.json"' in body
                assert b'linear-gradient(180deg,#f7ede2 0%,#f2e2d0 60%,#f7ede2 100%)' in body
                footer = rb'<footer class="status" aria-label="Users: 0; Storage: 1/[1-9][0-9]* GB">'
                assert re.search(footer, body)
                assert body.endswith(b"</main>\n</body>\n</html>")
            else:
                assert body == expected_spec, "OpenAPI response differs from the released representation"
                spec = json.loads(body)
                assert spec["openapi"] == "3.1.0"
                assert spec["info"]["title"] == "Mesh API"
                assert "post" in spec["paths"]["/api/v1/sync/login"]
                assert "post" in spec["paths"]["/api/v1/referrals/claim"]
                assert "SyncRequest" in spec["components"]["schemas"]
                assert request(connection, "GET", path)[2] == body

        assert request(connection, "GET", "/openapi.json/extra")[0] == 404
        with closing(sqlite3.connect(database)) as db, db:
            db.execute("INSERT INTO server_users(user_id_hash, public_key) VALUES (?, ?)", ("a" * 64, b"test"))
        status, _, updated_page = request(connection, "GET", "/")
        assert status == 200
        assert b'<span><strong>Users</strong> 1</span>' in updated_page


def main():
    binary = Path(sys.argv[1]).resolve()
    repo = Path(__file__).resolve().parents[2]
    source = (repo / "src/server/docs_openapi.zi").read_text(encoding="utf-8")
    expected_spec = "".join(
        json.loads(line.strip().removesuffix(","))
        for line in source.splitlines() if line.startswith('        "')
    ).encode()
    with TemporaryDirectory(dir=binary.parent) as directory:
        fixture = Path(directory)
        database = fixture / "mesh.db"
        env = {name: os.environ[name] for name in ("LD_LIBRARY_PATH",) if name in os.environ}
        env.update(
            MESH_ADDR="127.0.0.1:0",
            MESH_DB=str(database),
            MESH_NODE_IDENTITY_KEY_FILE=str(fixture / "mesh.node-key"),
            MESH_TOKEN_SECRET_HEX="00" * 32,
            MESH_NODE_SYNC_INTERVAL_SECONDS="0",
            MESH_HTTP_ZSTD_RESPONSES="1",
        )
        with subprocess.Popen([binary], cwd=fixture, env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) as server:
            try:
                readable, _, _ = select.select([server.stdout], [], [], 5)
                assert readable, "native server did not announce a loopback port"
                port = server.stdout.readline().strip()
                if not port.isdigit():
                    error = server.stderr.read(4096) if server.poll() is not None else b""
                    raise AssertionError(f"invalid loopback port {port!r}: {error!r}")
                check_docs(int(port), expected_spec, database)
            finally:
                if server.poll() is None:
                    server.terminate()
                    try:
                        server.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait()
    print("Native docs and OpenAPI HTTP compatibility passed")


if __name__ == "__main__":
    main()
