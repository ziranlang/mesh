//go:build mesh_embedded

package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
)

func startEmbedded(root string) (int64, error) {
	result := EmbeddedRuntime_Start(root)
	return result.Value, result.Error
}

func TestEmbeddedNodePersistsIdentityAndDatabaseAcrossLifecycle(t *testing.T) {
	root := t.TempDir()
	handle, err := startEmbedded(root)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { EmbeddedRuntime_Stop(handle) })
	node := EmbeddedRuntime_Lookup(handle)
	id := node.Core.Server.Node.ID
	url := node.Core.Server.Cfg.BaseURL
	client := &http.Client{}
	response, err := client.Get(url + "/healthz")
	if err != nil || response.StatusCode != 200 {
		t.Fatal("local node unavailable", err)
	}
	response.Body.Close()
	key := []byte{1, 2, 3}
	digest := sha256.Sum256(key)
	account := hex.EncodeToString(digest[:])
	if err := node.Core.Store.RegisterUser(context.Background(), account, key); err != nil {
		t.Fatal(err)
	}
	EmbeddedRuntime_Suspend(handle)
	if _, err := client.Get(url + "/healthz"); err == nil {
		t.Fatal("suspended listener still active")
	}
	if !EmbeddedRuntime_Resume(handle) {
		t.Fatal("resume failed")
	}
	if node.Core.Server.Node.ID != id {
		t.Fatal("suspend replaced identity")
	}
	EmbeddedRuntime_Stop(handle)
	restarted, err := startEmbedded(root)
	if err != nil {
		t.Fatal(err)
	}
	defer EmbeddedRuntime_Stop(restarted)
	next := EmbeddedRuntime_Lookup(restarted)
	if next.Core.Server.Node.ID != id {
		t.Fatal("restart replaced identity")
	}
	var found int
	if err := next.Core.Store.Database.QueryRow("SELECT COUNT(*) FROM server_users WHERE user_id_hash=?", account).Scan(&found); err != nil || found != 1 {
		t.Fatal("durable data lost", found, err)
	}
}

func TestEmbeddedNodeSharesProfileAndRejectsAnotherProcessOwner(t *testing.T) {
	root := t.TempDir()
	first, err := startEmbedded(root)
	if err != nil {
		t.Fatal(err)
	}
	defer EmbeddedRuntime_Stop(first)
	second, err := startEmbedded(root + string(os.PathSeparator))
	if err != nil || first != second {
		t.Fatal("profile not shared", err)
	}
	if !EmbeddedRuntime_Stop(second) || EmbeddedRuntime_Lookup(first) == nil {
		t.Fatal("shared profile prematurely stopped")
	}
	lock, err := os.OpenFile(filepath.Join(root, "node.lock"), os.O_RDWR, 0600)
	if err != nil {
		t.Fatal(err)
	}
	defer lock.Close()
	if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err == nil {
		t.Fatal("second executor acquired profile")
	}
}

func TestEmbeddedNodeOmitsPublicSocialPaymentAndRejectsUnsafeState(t *testing.T) {
	handle, err := startEmbedded(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer EmbeddedRuntime_Stop(handle)
	handler := EmbeddedRuntime_Handler(EmbeddedRuntime_Lookup(handle).Core)
	for _, route := range []string{"/api/v1/friends", "/api/v1/tokens/assets", "/api/v1/account/export", "/metrics", "/"} {
		recorder := httptest.NewRecorder()
		handler.ServeHTTP(recorder, httptest.NewRequest("GET", route, nil))
		if recorder.Code != 404 {
			t.Fatalf("unrelated endpoint enabled: %s", route)
		}
	}
	root := t.TempDir()
	if err := os.Chmod(root, 0755); err != nil {
		t.Fatal(err)
	}
	if h, err := startEmbedded(root); err == nil {
		EmbeddedRuntime_Stop(h)
		t.Fatal("public profile accepted")
	}
	root = t.TempDir()
	keyPath := filepath.Join(root, "node-key")
	if err := os.WriteFile(keyPath, []byte(strings.Repeat("00", 32)), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(keyPath, 0644); err != nil {
		t.Fatal(err)
	}
	if h, err := startEmbedded(root); err == nil {
		EmbeddedRuntime_Stop(h)
		t.Fatal("public identity file accepted")
	}
	root = t.TempDir()
	if err := os.Symlink("/tmp", filepath.Join(root, "node-key")); err != nil {
		t.Fatal(err)
	}
	if h, err := startEmbedded(root); err == nil {
		EmbeddedRuntime_Stop(h)
		t.Fatal("identity symlink accepted")
	}
}

func TestEmbeddedNodeInProcessRequest(t *testing.T) {
	handle, err := startEmbedded(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer EmbeddedRuntime_Stop(handle)
	for _, test := range []struct {
		path   string
		status int
	}{
		{"/healthz", http.StatusOK},
		{"/metrics", http.StatusNotFound},
	} {
		result := EmbeddedRuntime_Request(handle, http.MethodGet, test.path, "")
		if result.Error != nil {
			t.Fatal(result.Error)
		}
		var reply struct {
			Status int             `json:"status"`
			Body   json.RawMessage `json:"body"`
		}
		if err := json.Unmarshal(result.Value, &reply); err != nil || reply.Status != test.status || len(reply.Body) == 0 {
			t.Fatalf("unexpected reply for %s: %s (%v)", test.path, result.Value, err)
		}
	}
	result := EmbeddedRuntime_Request(handle, http.MethodGet, "http://other.invalid/healthz", "")
	if result.Error == nil {
		t.Fatal("in-process request accepted a non-local path")
	}
}
