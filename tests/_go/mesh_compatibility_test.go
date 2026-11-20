package main

import (
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
)

func TestMeshEnvironmentPreservesInstalledConfiguration(t *testing.T) {
	t.Setenv("DAOCHI_DB", "/var/lib/daochi/daochi.db")
	t.Setenv("DAOCHI_NODE_IDENTITY_KEY_FILE", "/etc/daochi/node.key")
	t.Setenv("MESH_DB", "")
	os.Unsetenv("MESH_DB")
	t.Setenv("MESH_NODE_IDENTITY_KEY_FILE", "")
	os.Unsetenv("MESH_NODE_IDENTITY_KEY_FILE")
	// Config helpers read new names and retain old values without rewriting paths.
	if got := Config_EnvString("MESH_DB", "missing"); got != "/var/lib/daochi/daochi.db" {
		t.Fatal(got)
	}
	if got := Config_EnvString("MESH_NODE_IDENTITY_KEY_FILE", "missing"); got != "/etc/daochi/node.key" {
		t.Fatal(got)
	}
	t.Setenv("MESH_DB", "/existing/selected.db")
	if got := Config_EnvString("MESH_DB", "missing"); got != "/existing/selected.db" {
		t.Fatal(got)
	}
	t.Setenv("MESH_DB", "")
	if got := Config_EnvString("MESH_DB", "fallback"); got != "fallback" {
		t.Fatal("explicit empty Mesh value must not load legacy state", got)
	}
}

func TestMeshAndDaochiHeadersShareSignatureBytes(t *testing.T) {
	for _, prefix := range []string{"X-Mesh", "X-Daochi"} {
		request := httptest.NewRequest(http.MethodPost, "/api/v1/sync/login", nil)
		request.Header.Set(prefix+"-User", strings.Repeat("a", 64))
		request.Header.Set(prefix+"-Signature", "proof")
		if got := HttpAuth_UserHeader(request); got.Value != strings.Repeat("a", 64) {
			t.Fatalf("%s: %#v", prefix, got)
		}
		if got := SyncRequest_SignatureHeader(request); got.Value != "proof" || got.Context != "daochi-sync-v1" {
			t.Fatalf("%s changed signed domain: %#v", prefix, got)
		}
	}
}
