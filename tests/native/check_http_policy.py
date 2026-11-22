"""Check the shared HTTP policy on a disposable native loopback listener."""

import select
import subprocess
import sys
from contextlib import closing
from http.client import HTTPConnection
from pathlib import Path


ALLOWED = (
    "https://mesh-bew.pages.dev",
    "https://mesh.ziran-lang.org",
    "https://inbe.waozi.xyz",
    "https://innerbreeze.org",
    "https://uku.waozi.xyz",
    "https://shovey.waozi.xyz",
    "https://wurm.waozi.xyz",
    "https://dew.waozi.xyz",
    "https://arcade.waozi.xyz",
    "https://entity.waozi.xyz",
    "https://waozi.xyz",
    "http://localhost:8080",
    "http://127.0.0.1",
    "http://0.0.0.0:9000",
    "HTTP://[::1]:8080",
    "http://localhost?#",
    "chrome-extension://" + "a" * 32,
)

REJECTED = (
    "null", "*", "https://unknown.example", "https://mesh.ziran-lang.org.evil",
    "https://mesh.ziran-lang.org:443", "https://mesh.ziran-lang.org/",
    "https://user@mesh.ziran-lang.org", "https://localhost", "http://LOCALHOST",
    "http://localhost.evil", "http://localhost:bad", "http://localhost/path",
    "http://localhost?key=value", "http://localhost#fragment", "http://localhost%00",
    "http://[::ffff:127.0.0.1]", "http://[::1%25lo]", "http://[::1]:bad",
    "http://localhost, https://mesh.ziran-lang.org", "http://localhost:" + "1" * 512,
    "chrome-extension://" + "q" * 32, "chrome-extension://" + "a" * 31,
    "chrome-extension://" + "a" * 32 + ":80",
)

ALLOW_METHODS = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
ALLOW_HEADERS = (
    "Authorization, Content-Type, Content-Encoding, X-Mesh-User, X-Mesh-Signature, X-Mesh-Tx, "
    "X-Mesh-Delegate, X-Mesh-Client, X-Mesh-Since-Version, X-Mesh-Limit, X-Mesh-Admin, "
    "X-Ksync-User, X-Ksync-Signature, X-Ksync-Client, X-Ksync-Since-Version, X-Ksync-Limit, "
    "X-Ksync-Admin, X-Inbe-User, X-Inbe-Signature, X-Daochi-User, X-Daochi-Signature, "
    "X-Daochi-Tx, X-Daochi-Delegate, X-Daochi-Client, X-Daochi-Since-Version, "
    "X-Daochi-Limit, X-Daochi-Admin"
)


def request(connection, method, path, fields=None):
    connection.request(method, path, headers=fields or {})
    response = connection.getresponse()
    return response.status, response.headers, response.read()


def vary_tokens(headers):
    return [token.strip().lower() for field in headers.get_all("Vary", []) for token in field.split(",")]


def check_preflight(connection):
    for origin in (*ALLOWED, *REJECTED, None):
        fields = {
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Authorization, X-Mesh-Signature",
        }
        if origin is not None:
            fields["Origin"] = origin
        status, headers, body = request(connection, "OPTIONS", "/protected", fields)
        assert (status, body) == (204, b""), (origin, status, body)
        assert headers.get_all("Access-Control-Allow-Origin", []) == ([origin] if origin in ALLOWED else [])
        assert headers.get("Access-Control-Allow-Methods") == ALLOW_METHODS
        assert headers.get("Access-Control-Allow-Headers") == ALLOW_HEADERS
        assert headers.get_all("Cache-Control") == ["no-store"]
        assert headers.get_all("X-Content-Type-Options") == ["nosniff"]
        assert "Access-Control-Allow-Credentials" not in headers
        assert "X-Fixture-Downstream" not in headers
        assert vary_tokens(headers) == (["origin"] if origin in ALLOWED else [])

    connection.putrequest("OPTIONS", "/unknown")
    connection.putheader("Origin", ALLOWED[0])
    connection.putheader("Origin", "https://unknown.example")
    connection.endheaders()
    response = connection.getresponse()
    assert (response.status, response.read()) == (204, b"")
    assert "Access-Control-Allow-Origin" not in response.headers


def check_responses(connection):
    origin = ALLOWED[1]
    status, headers, body = request(connection, "GET", "/protected", {"Origin": origin})
    assert (status, body) == (401, b"")
    assert headers.get_all("Access-Control-Allow-Origin") == [origin]
    assert headers["X-Fixture-Downstream"] == "true"

    for path, expected_vary in (
        ("/protected", ["origin", "accept-encoding"]),
        ("/varies-by-origin", ["origin", "accept-encoding"]),
        ("/wildcard", ["*"]),
    ):
        fields = {"Origin": origin, "Authorization": "Bearer fixture"}
        for method, expected_body in (("GET", b"{}\n"), ("HEAD", b"")):
            status, headers, body = request(connection, method, path, fields)
            assert (status, body) == (200, expected_body)
            assert headers.get_all("Access-Control-Allow-Origin") == [origin]
            assert "Access-Control-Allow-Credentials" not in headers
            assert headers["Content-Type"] == "application/vnd.oai.openapi+json"
            assert headers["Content-Length"] == "3"
            assert headers.get_all("Cache-Control") == ["public, max-age=60"]
            assert headers["ETag"] == '"fixture"'
            assert sorted(vary_tokens(headers)) == sorted(expected_vary), headers

    status, headers, body = request(connection, "GET", "/public", {"Origin": REJECTED[2]})
    assert (status, body) == (200, b"{}\n")
    assert "Access-Control-Allow-Origin" not in headers
    assert "Access-Control-Allow-Credentials" not in headers
    assert vary_tokens(headers) == ["accept-encoding"]


def main():
    binary = Path(sys.argv[1]).resolve()
    with subprocess.Popen([binary], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as server:
        try:
            readable, _, _ = select.select([server.stdout], [], [], 5)
            assert readable, "fixture did not announce a loopback port"
            text = server.stdout.readline().strip()
            assert text.isdigit(), text
            with closing(HTTPConnection("127.0.0.1", int(text), timeout=3)) as connection:
                check_preflight(connection)
                check_responses(connection)
        finally:
            server.stdin.close()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
        assert server.returncode == 0, server.stderr.read(4096)
    print("Native HTTP policy loopback passed")


if __name__ == "__main__":
    main()
