"""Verify Mesh DNS-SD packets and lifecycle inside a private Linux network."""

import argparse
from contextlib import closing, contextmanager
from http.client import HTTPConnection
import ipaddress
import json
import os
from pathlib import Path
import select
import socket
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory
import time


INTERFACE = "mesh-fixture"
HOST = "mesh-discovery-fixture"
IPV4 = "10.99.0.1"
IPV6 = "fd99::1"
MDNS4 = "224.0.0.251"
MDNS6 = "ff02::fb"
MDNS_PORT = 5353
PORT = 38080
NODE = "abcdef0123456789" * 4
SERVICE = (b"_daochi", b"_tcp", b"local")
TXID = 0x4D53


def read_name(packet, offset):
    labels = []
    following = None
    seen = set()
    while True:
        assert offset not in seen, "DNS compression loop"
        seen.add(offset)
        length = packet[offset]
        if length == 0:
            return tuple(labels), following or offset + 1
        if length & 0xC0 == 0xC0:
            if following is None:
                following = offset + 2
            offset = ((length & 0x3F) << 8) | packet[offset + 1]
        else:
            assert 0 < length < 64
            start = offset + 1
            offset = start + length
            labels.append(packet[start:offset])


def records(packet, txid=None):
    identifier, flags, questions, answers, authority, additional = struct.unpack_from("!6H", packet)
    if not flags & 0x8000 or (txid is not None and txid != identifier):
        return
    offset = 12
    for _ in range(questions):
        _, offset = read_name(packet, offset)
        offset += 4
    for _ in range(answers + authority + additional):
        name, offset = read_name(packet, offset)
        kind, rrclass, ttl, size = struct.unpack_from("!HHIH", packet, offset)
        offset += 10
        begin = offset
        offset += size
        assert offset <= len(packet)
        if rrclass & 0x7FFF == 1:
            value = packet[begin:offset]
            if kind == 12:
                value, end = read_name(packet, begin)
                assert end == offset
            elif kind == 33:
                priority, weight, port = struct.unpack_from("!3H", packet, begin)
                target, end = read_name(packet, begin + 6)
                assert end == offset
                value = (priority, weight, port, target)
            elif kind == 16:
                fields = []
                for index in txt_indices(value):
                    fields.append(value[index + 1:index + 1 + value[index]])
                value = frozenset(fields)
            yield name, kind, ttl, value


def txt_indices(data):
    offset = 0
    while offset < len(data):
        yield offset
        offset += 1 + data[offset]
    assert offset == len(data)


def query(name=SERVICE, identifier=TXID):
    return (struct.pack("!6H", identifier, 0, 1, 0, 0, 0) +
            b"".join(bytes([len(label)]) + label for label in name) + b"\0\0\x0c\0\x01")


def expected(instance, port=PORT, node_id=NODE):
    target = (HOST.encode(), b"local")
    name = (instance.encode(),) + SERVICE
    return {
        (SERVICE, 12, name),
        (name, 33, (0, 0, port, target)),
        (name, 16, frozenset({f"id={node_id}".encode(), b"protocol=6", b"trust=pairing-required", b"path=/api/v1/node"})),
    }


def wait_records(connection, wanted, *, ttl_zero=False, txid=None, timeout=6):
    remaining = set(wanted)
    deadline = time.monotonic() + timeout
    while remaining:
        time_left = deadline - time.monotonic()
        assert time_left > 0, ("missing mDNS records", remaining)
        ready, _, _ = select.select([connection], [], [], time_left)
        assert ready, ("missing mDNS records", remaining)
        for name, kind, ttl, value in records(connection.recv(9000), txid):
            if (ttl == 0) == ttl_zero:
                remaining.discard((name, kind, value))


def assert_no_service(listener):
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        ready, _, _ = select.select([listener], [], [], max(0, deadline - time.monotonic()))
        if not ready:
            break
        # The responder can repeat a withdrawal after the first goodbye.
        assert not any(name == SERVICE and ttl > 0 for name, _, ttl, _ in records(listener.recv(9000)))


def player(family, listen=True):
    connection = socket.socket(family, socket.SOCK_DGRAM)
    try:
        if family == socket.AF_INET:
            address = socket.inet_aton(IPV4)
            connection.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, address)
            connection.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
            if listen:
                connection.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                connection.bind(("", MDNS_PORT))
                connection.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(MDNS4) + address)
            else:
                connection.bind((IPV4, 0))
        else:
            index = socket.if_nametoindex(INTERFACE)
            connection.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            connection.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_IF, index)
            connection.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_HOPS, 255)
            if listen:
                connection.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                connection.bind(("::", MDNS_PORT))
                request = socket.inet_pton(family, MDNS6) + struct.pack("@I", index)
                connection.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_JOIN_GROUP, request)
            else:
                connection.bind((IPV6, 0))
    except OSError:
        connection.close()
        raise
    return connection


def setup_network():
    commands = [
        ["hostname", HOST],
        ["ip", "link", "set", "lo", "up"],
        ["ip", "link", "add", INTERFACE, "type", "dummy"],
        ["ip", "addr", "add", f"{IPV4}/24", "dev", INTERFACE],
        ["ip", "-6", "addr", "add", f"{IPV6}/64", "dev", INTERFACE, "nodad"],
        ["ip", "link", "set", INTERFACE, "up", "multicast", "on"],
        ["ip", "-6", "route", "replace", "ff00::/8", "dev", INTERFACE],
    ]
    for command in commands:
        subprocess.run(command, check=True)


def read(process):
    ready, _, _ = select.select([process.stdout], [], [], 5)
    assert ready, "discovery test process did not report its state"
    return process.stdout.readline().decode().strip()


def send(process, command):
    process.stdin.write(command)


@contextmanager
def worker(binary, *, name="Home", node=NODE, enabled=True, port=PORT):
    env = dict(os.environ, MESH_NODE_NAME=name, MESH_DISCOVERY_FIXTURE_ID=node,
               MESH_LAN_DISCOVERY="1" if enabled else "0", MESH_DISCOVERY_FIXTURE_PORT=str(port))
    with subprocess.Popen([binary], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          bufsize=0) as process:
        try:
            yield process
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise
            assert process.returncode == 0, process.stderr.read(4096)


def check_advertisement(binary):
    with closing(player(socket.AF_INET)) as ipv4, closing(player(socket.AF_INET6)) as ipv6:
        with worker(binary, name=" \t\u2003Café.Home\n") as process:
            assert read(process) == "ready"
            wanted = expected(f"Café.Home {NODE[:12]}")
            wait_records(ipv4, wanted)
            wait_records(ipv6, wanted)
            target = (HOST.encode(), b"local")
            with closing(player(socket.AF_INET, listen=False)) as browser:
                browser.sendto(query(), (MDNS4, MDNS_PORT))
                wait_records(browser, wanted | {(target, 1, ipaddress.ip_address(IPV4).packed)}, txid=TXID)

            send(process, b"r\n")
            assert read(process) == "stopped"
            # The PTR goodbye removes the instance from active browsers.
            goodbye = {entry for entry in wanted if entry[1] == 12}
            wait_records(ipv4, goodbye, ttl_zero=True)
            wait_records(ipv6, goodbye, ttl_zero=True)
            assert read(process) == "ready"
            wait_records(ipv4, wanted)
            send(process, b"q\n")
            assert read(process) == "stopped"
            wait_records(ipv4, goodbye, ttl_zero=True)
            assert_no_service(ipv4)

        with worker(binary, name="\u2003 ", port=PORT + 1) as process:
            assert read(process) == "ready"
            wait_records(ipv4, expected(f"Mesh Node {NODE[:12]}", PORT + 1))
        # Drain any goodbye from the previous valid instance before checking the disabled setting.
        wait_records(ipv4, {(SERVICE, 12, (f"Mesh Node {NODE[:12]}".encode(),) + SERVICE)}, ttl_zero=True)
        with worker(binary, enabled=False) as process:
            assert read(process) == "ready"
            ipv4.sendto(query(identifier=0), (MDNS4, MDNS_PORT))
            assert_no_service(ipv4)


def check_errors(binary):
    for settings, expected_error in (
        ({"port": 0}, "requires a fixed TCP port"),
        ({"node": "bad"}, "requires a valid node identity"),
        ({"name": "é" * 26}, "invalid LAN discovery instance name"),
    ):
        with worker(binary, **settings) as process:
            assert expected_error in read(process)
    with closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as exclusive4, \
            closing(socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)) as exclusive6:
        exclusive4.bind(("", MDNS_PORT))
        exclusive6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        exclusive6.bind(("::", MDNS_PORT))
        with worker(binary) as process:
            assert "could not open multicast DNS sockets" in read(process)


@contextmanager
def server_node(binary, database, setting=None):
    settings = {key: value for key, value in os.environ.items() if not key.startswith("MESH_")}
    settings.update(MESH_ADDR=f"{IPV4}:{PORT}", MESH_DB=str(database), MESH_NODE_NAME="Discovery Node",
                    MESH_TOKEN_SECRET_HEX="00" * 32)
    if setting is not None:
        settings["MESH_LAN_DISCOVERY"] = setting
    with subprocess.Popen([binary], env=settings, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as server:
        try:
            assert read(server) == str(PORT), "Mesh did not report the configured listener port"
            with closing(HTTPConnection(IPV4, PORT, timeout=5)) as connection:
                connection.request("GET", "/api/v1/node")
                response = connection.getresponse()
                assert response.status == 200, response.status
                node = json.load(response)
                assert node["node_name"] == settings["MESH_NODE_NAME"], node["node_name"]
                yield node["node_id"]
        finally:
            server.terminate()
            try:
                _, errors = server.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.communicate()
                raise
            assert server.returncode == 0, errors[:4096]


def check_server(binary):
    with TemporaryDirectory(prefix="discovery-", dir=Path(binary).parent) as directory, \
            closing(player(socket.AF_INET)) as listener:
        database = Path(directory) / "mesh.db"
        with server_node(binary, database) as node_id:
            wanted = expected(f"Discovery Node {node_id[:12]}", node_id=node_id)
            wait_records(listener, wanted)
        goodbye = {entry for entry in wanted if entry[1] == 12}
        wait_records(listener, goodbye, ttl_zero=True)
        assert_no_service(listener)
        for setting in (" on ", "YES"):
            with server_node(binary, database, setting) as restarted_id:
                assert restarted_id == node_id
                wait_records(listener, wanted)
            wait_records(listener, goodbye, ttl_zero=True)
            assert_no_service(listener)
        for setting in ("false", "off", "NO", "unrecognized"):
            with server_node(binary, database, setting) as restarted_id:
                assert restarted_id == node_id
                listener.sendto(query(identifier=0), (MDNS4, MDNS_PORT))
                assert_no_service(listener)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture")
    parser.add_argument("--server", help="also check the canonical native server")
    parser.add_argument("--outside-namespaces", nargs=2, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.outside_namespaces is None:
        ids = [str(os.stat(f"/proc/self/ns/{kind}").st_ino) for kind in ("net", "uts")]
        subprocess.run(["unshare", "-Urnu", sys.executable, "-B", __file__, *sys.argv[1:], "--outside-namespaces", *ids],
                       check=True)
        return
    for kind, outside in zip(("net", "uts"), args.outside_namespaces):
        if os.stat(f"/proc/self/ns/{kind}").st_ino == int(outside):
            raise SystemExit(f"private {kind} namespace required")
    binary = str(Path(args.fixture).resolve())
    setup_network()
    check_advertisement(binary)
    check_errors(binary)
    print("native private-network mDNS discovery passed")
    if args.server:
        check_server(str(Path(args.server).resolve()))
        print("canonical native server mDNS discovery passed")


if __name__ == "__main__":
    main()
