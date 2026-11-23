"""Keep the native /metrics output equal to the Go server's.

The Go server (src/server/metrics.zi) and the native server
(src/server/metrics_native.zi with src/http/http_metrics_native.zi) are two
implementations of one endpoint. Operators and dashboards read the same
family names either way, so a family that exists on only one side, or that
changes type or labels on one side, is a regression.

Run: python3 -B scripts/test_metrics_parity.py
"""

import re
from pathlib import Path
import unittest


REPOSITORY = Path(__file__).resolve().parents[1]
GO = REPOSITORY / "src/server/metrics.zi"
NATIVE = REPOSITORY / "src/server/metrics_native.zi"
NATIVE_HTTP = REPOSITORY / "src/http/http_metrics_native.zi"
SERVER = REPOSITORY / "src/server/server.zi"

# Go writes these outside WriteMaps; the native Apps() and Report() do the same.
EXTRA = {
    "mesh_build_info": ("gauge", ()),
    "mesh_storage_app_logical_bytes": ("gauge", ("app_id",)),
    "mesh_storage_collection_logical_bytes": ("gauge", ("app_id", "collection")),
}

EVENT_SUFFIXES = (
    "sync_requests_total",
    "sync_failures_total",
    "sync_full_snapshots_total",
    "sync_encrypted_records_applied_total",
    "sync_encrypted_payloads_applied_total",
    "auth_failures_total",
    "legacy_client_hints_total",
    "websocket_accepted_total",
    "websocket_rejected_total",
)


def go_families():
    source = GO.read_text()
    families = dict(EXTRA)
    for match in re.finditer(r'WriteScalar\(writer, "([a-z0-9_]+)", "([a-z]+)"', source):
        name, kind = match.group(1), match.group(2)
        assert name not in families, f"{name} written twice by the Go server"
        families[name] = (kind, ())
    for match in re.finditer(r'WriteLabelMap\(writer, "([a-z0-9_]+)", "([a-z]+)", "([a-z,]+)"', source):
        name, kind, labels = match.group(1), match.group(2), tuple(match.group(3).split(","))
        assert name not in families, f"{name} written twice by the Go server"
        families[name] = (kind, labels)
    for match in re.finditer(r'WriteSingleLabelMap\(writer, "([a-z0-9_]+)", "([a-z]+)", "([a-z]+)"', source):
        name, kind, label = match.group(1), match.group(2), match.group(3)
        assert name not in families, f"{name} written twice by the Go server"
        families[name] = (kind, (label,))
    return families


def native_families():
    metrics = NATIVE.read_text()
    http = NATIVE_HTTP.read_text()
    families = dict(EXTRA)
    for match in re.finditer(r'Scalar\(output, "([a-z0-9_]+)"((?:, [^;]*?)?)\)', metrics):
        name, rest = match.group(1), match.group(2)
        kind = "gauge"
        if rest:
            trailing = re.search(r'"([a-z]+)"$', rest)
            if trailing:
                kind = trailing.group(1)
        assert name not in families, f"{name} written twice by the native server"
        families[name] = (kind, ())
    for match in re.finditer(r'Family\(state, output, "([a-z0-9_]+)", (true|false)\)', http):
        name = match.group(1)
        assert name not in families, f"{name} written twice by the native server"
        families[name] = ("counter", ("method", "route", "status"))
    for prefix in ("mesh_", "ksync_"):
        for suffix in EVENT_SUFFIXES:
            families[prefix + suffix] = ("counter", ())
        families[prefix + "auth_failures_by_reason_total"] = ("counter", ("status", "reason"))
        families[prefix + "sync_full_snapshots_by_reason_total"] = ("counter", ("reason",))
        families[prefix + "websocket_rejected_by_reason_total"] = ("counter", ("reason",))
    families["mesh_monero_stuck_invoices_total"] = ("counter", ())
    return families


def go_routes():
    """The series-route labels Go's Route() can label a request with."""
    source = SERVER.read_text()
    registered = sorted({match.group(2) for match in re.finditer(r'"([A-Z]+) (/\S*)"', source)})

    def normalized(path):
        if path.startswith("/api/v1/friends/requests/"):
            return "/api/v1/friends/requests/{id}"
        if path.startswith("/api/v1/friends/") and path not in ("/api/v1/friends/requests", "/api/v1/friends/stats"):
            return "/api/v1/friends/{user_id_hash}"
        if path.startswith("/api/v1/apps/"):
            return "/api/v1/apps/{app_id}/collections" if path.endswith("/collections") else "/api/v1/apps/{app_id}"
        if path.startswith("/api/v1/account/app-grants/"):
            return "/api/v1/account/app-grants/{id}"
        if path.startswith("/api/v1/tokens/purchases/monero/invoices/"):
            return "/api/v1/tokens/purchases/monero/invoices/{id}"
        if path.startswith("/api/v1/tokens/purchases/monero/address/"):
            return "/api/v1/tokens/purchases/monero/address/{recipient}"
        if path.startswith("/api/v1/tokens/receipts/"):
            return "/api/v1/tokens/receipts/{receipt_id}"
        return path

    labels = {normalized(path) for path in registered} | {"unmatched"}
    # Prefixes that only a request path (never a registration) produces.
    labels |= {
        "/api/v1/apps/{app_id}/collections",
        "/api/v1/friends/requests/{id}",
        "/api/v1/friends/{user_id_hash}",
    }
    return labels


def native_routes():
    source = NATIVE_HTTP.read_text()
    table = re.search(r"Routes: \[(\d+)\]string = \.\[(.*?)\];", source, re.S)
    assert table is not None, "native route table not found"
    declared, body = int(table.group(1)), table.group(2)
    routes = re.findall(r'"([^"]+)"', body)
    return declared, routes


class MetricsParityTest(unittest.TestCase):
    def test_native_reports_every_go_family_with_the_same_type_and_labels(self):
        go = go_families()
        native = native_families()
        self.assertEqual(sorted(set(go) - set(native)), [], "families missing from the native server")
        self.assertEqual(sorted(set(native) - set(go)), [], "families the Go server never reports")
        mismatched = {name: (go[name], native[name]) for name in sorted(go) if go[name] != native[name]}
        self.assertEqual(mismatched, {}, "type or label mismatch between Go and native")

    def test_native_route_label_table_matches_go(self):
        go_labels = go_routes()
        declared, routes = native_routes()
        self.assertEqual(declared, len(routes), "declared route table size does not match its entries")
        self.assertEqual(len(routes), len(set(routes)), "duplicate route labels in the native table")
        self.assertEqual(sorted(go_labels - set(routes)), [], "Go route labels the native table cannot emit")
        # Every native row must be reachable on Go as well, or a dashboard breaks
        # when the same request is served by the other runtime.
        self.assertEqual(sorted(set(routes) - go_labels), [], "native route labels the Go server never emits")


if __name__ == "__main__":
    unittest.main()
