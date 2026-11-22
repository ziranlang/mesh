"""Check native request and response coding against independent gzip and zstd tools."""

import gzip
import os
import select
import socket
import subprocess
import sys
from http.client import HTTPConnection


def zstd(data, *, decode=False):
    flags = ["-d"] if decode else []
    return subprocess.run(
        ["zstd", "-q", "-c", *flags], input=data, capture_output=True, timeout=3, check=True
    ).stdout


def exchange(connection, body=b"", *, headers=(), method="POST", path="/echo"):
    connection.putrequest(method, path, skip_accept_encoding=True)
    connection.putheader("Content-Type", "application/json")
    connection.putheader("Content-Length", str(len(body)))
    for name, value in headers:
        connection.putheader(name, value)
    connection.endheaders(body)
    response = connection.getresponse()
    return response.status, dict(response.getheaders()), response.read()


def check_enabled(connection):
    first = b'{"text":"' + b"mesh " * 400
    second = b'"}\n'
    plain = first + second
    gzip_members = gzip.compress(first) + gzip.compress(second) + gzip.compress(b"")
    zstd_frames = zstd(first) + bytes.fromhex("502a4d1803000000") + b"pad" + zstd(second)
    cases = [
        ("identity", plain, "identity", None),
        ("GZIP", gzip_members, "gzip", "gzip"),
        ("zstd", zstd_frames, "gzip, zstd", "zstd"),
        ("identity", plain, "gzip;q=1, zstd;q=0.7", "gzip"),
    ]
    for incoming, wire, accept, outgoing in cases:
        status, headers, encoded = exchange(connection, wire, headers=(
            ("Content-Encoding", incoming), ("Accept-Encoding", accept)
        ))
        assert status == 200, (incoming, status)
        assert headers["Vary"] == "Accept-Encoding", headers
        assert headers.get("Content-Encoding") == outgoing, headers
        decoded = {"gzip": gzip.decompress, "zstd": lambda data: zstd(data, decode=True)}
        assert (decoded[outgoing](encoded) if outgoing else encoded) == plain

    status, headers, encoded = exchange(connection, plain, headers=(
        ("Accept-Encoding", "gzip;q=0, zstd"), ("accept-encoding", "zstd;q=0, *;q=1")
    ))
    assert (status, headers.get("Content-Encoding"), encoded) == (200, None, plain)
    assert exchange(connection, plain, headers=(("Accept-Encoding", "*;q=0"),))[0] == 406
    assert exchange(connection, plain, headers=(("Accept-Encoding", "x" * 4097),))[0] == 406
    assert exchange(connection, plain, headers=(("Accept-Encoding", "*;q=0"),), method="HEAD")[2] == b""
    assert exchange(connection, plain, headers=(("Accept-Encoding", "zstd"),), path="/partial")[2] == plain
    assert exchange(connection, plain, headers=(("Accept-Encoding", "zstd,identity;q=0"),), path="/partial")[0] == 406

    for wire, headers, status in (
        (plain, (("Content-Encoding", "br"),), 415),
        (gzip_members, (("Content-Encoding", "gzip"), ("Content-Encoding", "")), 415),
        (gzip_members[:-1], (("Content-Encoding", "gzip"),), 400),
        (zstd_frames[:-1], (("Content-Encoding", "zstd"),), 400),
        (gzip.compress(b"x" * 4097), (("Content-Encoding", "gzip"),), 400),
        (zstd(b"x" * 4097), (("Content-Encoding", "zstd"),), 400),
        (b"x" * 4097, (), 413),
    ):
        assert exchange(connection, wire, headers=headers)[0] == status, (headers, status)
    assert exchange(connection, b"x" * 2048, path="/small")[0] == 200
    assert exchange(connection, b"x" * 2049, path="/small")[0] == 400
    exact = b'"' + b"x" * 4094 + b'"'
    assert exchange(connection, gzip.compress(exact), headers=(("Content-Encoding", "gzip"),))[2] == exact
    assert exchange(connection, zstd(exact), headers=(("Content-Encoding", "zstd"),))[2] == exact


def check_disabled(connection):
    plain = b'{"status":"ok"}'
    assert exchange(connection, zstd(plain), headers=(("Content-Encoding", "zstd"),))[0] == 415
    status, headers, encoded = exchange(connection, plain, headers=(("Accept-Encoding", "zstd,gzip"),))
    assert (status, headers.get("Content-Encoding"), gzip.decompress(encoded)) == (200, "gzip", plain)


def check_server(executable, enabled):
    environment = os.environ | {"MESH_TEST_NATIVE_ZSTD": str(int(enabled))}
    with subprocess.Popen([executable], env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE) as server:
        try:
            readable, _, _ = select.select([server.stdout], [], [], 5)
            if not readable:
                raise RuntimeError("native coding fixture did not report a bound port")
            port = int(server.stdout.readline())
            # Abort an upload, then verify normal requests and shutdown still work.
            with socket.create_connection(("127.0.0.1", port), timeout=3) as interrupted:
                interrupted.sendall(b"POST /echo HTTP/1.1\r\nHost: localhost\r\nContent-Length: 100\r\n\r\na")
            connection = HTTPConnection("127.0.0.1", port, timeout=3)
            try:
                (check_enabled if enabled else check_disabled)(connection)
            finally:
                connection.close()
        finally:
            try:
                server.communicate(input=b"\n", timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.communicate()
                raise
        assert server.returncode == 0, server.returncode


if __name__ == "__main__":
    check_server(sys.argv[1], enabled=True)
    check_server(sys.argv[1], enabled=False)
