import unittest
from provision_mesh_site import DOMAIN, provision


class FakeAPI:
    def __init__(self, existing=False, conflict=False):
        self.calls = []
        self.existing = existing
        self.conflict = conflict

    def request(self, method, path, body=None, missing=False):
        self.calls.append((method, path, body))
        if method == "POST":
            return {"subdomain": "mesh-bew.pages.dev"} if path.endswith("/projects") else {}
        if path.endswith("/projects/mesh"):
            return {"subdomain": "mesh-bew.pages.dev"} if self.existing else None
        if path.startswith("/zones?"):
            return [{"id": "zone"}]
        if "/dns_records?" in path:
            if self.conflict:
                return [{"type": "A", "content": "192.0.2.1"}]
            return [{"type": "CNAME", "content": "mesh-bew.pages.dev"}] if self.existing else []
        if path.endswith("/domains"):
            return [{"name": DOMAIN}] if self.existing else []
        raise AssertionError(path)


class ProvisionTests(unittest.TestCase):
    def test_creates_only_mesh_project_record_and_domain(self):
        api = FakeAPI()
        provision(api, "account")
        posts = [(path, body) for method, path, body in api.calls if method == "POST"]
        self.assertEqual(len(posts), 3)
        self.assertEqual(posts[0][1]["name"], "mesh")
        self.assertEqual(posts[1][1]["name"], DOMAIN)
        self.assertEqual(posts[1][1]["content"], "mesh-bew.pages.dev")
        self.assertEqual(posts[2][1], {"name": DOMAIN})

    def test_idempotent(self):
        api = FakeAPI(existing=True)
        provision(api, "account")
        self.assertFalse(any(method != "GET" for method, _, _ in api.calls))

    def test_conflicting_dns_is_retained(self):
        api = FakeAPI(existing=True, conflict=True)
        with self.assertRaisesRegex(RuntimeError, "conflicting DNS"):
            provision(api, "account")
        self.assertFalse(any(method != "GET" for method, _, _ in api.calls))


if __name__ == "__main__":
    unittest.main()
