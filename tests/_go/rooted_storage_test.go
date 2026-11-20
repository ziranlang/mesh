package main

import (
	"bytes"
	"crypto/ed25519"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestRootedBlobStorageKeepsAccountDirectoriesSeparate(t *testing.T) {
	server, user, key := registryHTTPFixture(t)
	server.Verifier = testVerifier(&transactionVerifier{accept: true})
	server.Cfg.DBPath = filepath.Join(t.TempDir(), "node.sqlite")
	store := server.Cfg.DBPath + ".blobs"
	other := strings.Repeat("0", 64)
	if other == user {
		other = strings.Repeat("1", 64)
	}
	otherApp := filepath.Join(store, other, "target")
	if err := os.MkdirAll(otherApp, 0700); err != nil {
		t.Fatal(err)
	}
	data := []byte("fixture ciphertext")
	hash := Signing_SHA256Hex(data)
	stored := filepath.Join(otherApp, hash)
	if err := os.WriteFile(stored, data, 0600); err != nil {
		t.Fatal(err)
	}
	account := filepath.Join(store, user)
	if err := os.Symlink(other, account); err != nil {
		t.Fatal(err)
	}
	path := "/api/v1/blobs/target/" + hash
	for _, method := range []string{http.MethodGet, http.MethodHead, http.MethodPut, http.MethodDelete} {
		t.Run(method, func(t *testing.T) {
			var body []byte
			if method == http.MethodPut {
				body = data
			}
			before, err := os.Stat(stored)
			if err != nil {
				t.Fatal(err)
			}
			response := httptest.NewRecorder()
			request := blobRequest(t, server, user, key, method, path, body, "rooted-blob-"+method)
			Server_HandleBlobHttpRoute(server, response, request)
			if response.Code >= 200 && response.Code < 300 {
				t.Fatalf("%s accepted a linked account directory: %d", method, response.Code)
			}
			after, err := os.Stat(stored)
			if err != nil {
				t.Fatal(err)
			}
			if !os.SameFile(before, after) {
				t.Fatal("another account's file was replaced")
			}
		})
	}
}

func TestRootedPackageStorageRejectsLinkedPathsAndKeepsGrantsClosed(t *testing.T) {
	server, database, _ := testServer(t)
	private := ed25519.NewKeyFromSeed(bytes.Repeat([]byte{0x71}, ed25519.SeedSize))
	const app = "rooted-fixture"
	manifest := AppManifest{ManifestVersion: 1, AppID: app, DisplayName: app,
		Status: "active", Keys: []AppKey{{KeyID: "release", Algorithm: "ed25519",
			PublicKey: hex.EncodeToString(private.Public().(ed25519.PublicKey)), Status: "active"}}}
	marshal := func(value any) []byte {
		t.Helper()
		encoded, err := json.Marshal(value)
		if err != nil {
			t.Fatal(err)
		}
		return encoded
	}
	if err := AppStore_UpsertSignedManifest(database.Database, t.Context(), manifest,
		marshal(manifest), "rooted-fixture-manifest", "fixture", "fixture"); err != nil {
		t.Fatal(err)
	}
	data := []byte("ZIB\x00rooted package fixture")
	hash := Signing_SHA256Hex(data)
	asset := hash + ".zib"
	release := ReleaseV2{AppID: app, Sequence: 1, Version: "1.0.0", Runtime: "kryon-app-v1",
		HostAPI: 1, ModuleAPI: 1, DataSchema: 1, KeyID: "release",
		Artifacts:    []Artifact{{Variant: "module", SHA256: hash, Size: int64(len(data))}},
		Dependencies: []Dependency{}}
	release.Signature = hex.EncodeToString(ed25519.Sign(private, []byte(PackageRelease_SigningMessage(release))))
	store := server.Cfg.DBPath + ".packages"
	root := filepath.Join(store, app)
	if err := os.MkdirAll(root, 0700); err != nil {
		t.Fatal(err)
	}
	for name, content := range map[string][]byte{
		asset: data, asset + ".release-v2.json": marshal(release), "latest-v2.json": marshal(release),
	} {
		if err := os.WriteFile(filepath.Join(root, name), content, 0600); err != nil {
			t.Fatal(err)
		}
	}
	routes := server.Routes()
	get := func(name string) *httptest.ResponseRecorder {
		t.Helper()
		response := httptest.NewRecorder()
		routes.ServeHTTP(response, httptest.NewRequest(http.MethodGet, "/api/v2/packages/"+app+"/"+name, nil))
		return response
	}
	if response := get(asset); response.Code != http.StatusOK || !bytes.Equal(response.Body.Bytes(), data) {
		t.Fatalf("existing package: %d %q", response.Code, response.Body.Bytes())
	}
	sidecar := filepath.Join(root, asset+".product")
	if err := os.Symlink("missing-product", sidecar); err != nil {
		t.Fatal(err)
	}
	if response := get(asset); response.Code != http.StatusForbidden {
		t.Fatalf("linked product sidecar should remain gated: %d", response.Code)
	}
	if err := os.Remove(sidecar); err != nil {
		t.Fatal(err)
	}
	artifactPath := filepath.Join(root, asset)
	alternate := filepath.Join(root, "payload-fixture")
	if err := os.Rename(artifactPath, alternate); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Base(alternate), artifactPath); err != nil {
		t.Fatal(err)
	}
	if response := get(asset); response.Code >= 200 && response.Code < 300 {
		t.Fatalf("accepted a linked artifact: %d", response.Code)
	}
	if err := os.Rename(alternate, artifactPath); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(root, filepath.Join(store, "sibling-fixture")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink("sibling-fixture", root); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"latest", "history", asset} {
		if response := get(name); response.Code >= 200 && response.Code < 300 {
			t.Errorf("%s accepted a linked app directory: %d", name, response.Code)
		}
	}
}
