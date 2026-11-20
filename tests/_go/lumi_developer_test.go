package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newLumiDeveloperIdentity(t *testing.T, handler http.Handler) testIdentity {
	t.Helper()
	publicKey, privateKey, err := generateMLDSA44Keypair()
	if err != nil {
		t.Fatal(err)
	}
	hash := sha256.Sum256(publicKey)
	userID := hex.EncodeToString(hash[:])
	const loginPath = "/api/v1/sync/login"
	body, err := json.Marshal(LoginRequest{
		UserIDHash: userID,
		PublicKey:  hex.EncodeToString(publicKey),
		ClientID:   "lumi-developer-fixture",
	})
	if err != nil {
		t.Fatal(err)
	}
	nonce := issueChallenge(t, handler, "", userID)
	message := Signing_CanonicalMessageWithContext("ksync-sync-v1", mustDecodeHex(t, nonce), http.MethodPost, loginPath, body)
	signature, err := signWithPrivateKey([]byte(message), privateKey)
	if err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(http.MethodPost, loginPath, bytes.NewReader(body))
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("X-Ksync-User", userID)
	request.Header.Set("X-Ksync-Signature", hex.EncodeToString(signature))
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("signed fixture login returned %d", response.Code)
	}
	var login LoginResponse
	if err := json.Unmarshal(response.Body.Bytes(), &login); err != nil {
		t.Fatal(err)
	}
	if login.AuthToken == "" {
		t.Fatal("signed fixture login returned no session token")
	}
	return testIdentity{UserID: userID, Token: login.AuthToken}
}

func TestLumiDeveloperOwnerRoundTrip(t *testing.T) {
	server, _, _ := testServer(t)
	server.Verifier = testVerifier(OQSVerifier{})
	handler := server.Routes()
	owner := newLumiDeveloperIdentity(t, handler)
	other := newLumiDeveloperIdentity(t, handler)
	const route = "/api/v1/lumi/developer"
	const operatorRoute = "/api/v1/admin/lumi/developer"
	const id = "fedcba9876543210fedcba9876543210"
	const submit = `{"id":"` + id + `","text":"Review the app build"}`
	const poll = `{"id":"` + id + `"}`
	const token = "test-only-lumi-developer-operator-token"
	assertStatus := func(out *httptest.ResponseRecorder, status int) {
		t.Helper()
		if out.Code != status {
			t.Fatalf("got status %d, want %d: %s", out.Code, status, out.Body.String())
		}
	}
	operator := func(method, credential, payload string) *httptest.ResponseRecorder {
		req := httptest.NewRequest(method, operatorRoute, strings.NewReader(payload))
		req.RemoteAddr = "127.0.0.1:31111"
		req.Host = "127.0.0.1:8080"
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("X-Mesh-Admin", credential)
		out := httptest.NewRecorder()
		handler.ServeHTTP(out, req)
		return out
	}
	assertStatus(chatRequest(handler, owner, route, submit), http.StatusServiceUnavailable)
	server.Cfg.LumiOwnerID = owner.UserID
	server.Cfg.LumiDeveloperToken = token
	server.Cfg.FeedbackToken = "separate-feedback-credential"
	assertStatus(chatRequest(handler, other, route, submit), http.StatusForbidden)
	forged := httptest.NewRequest(http.MethodPost, route, strings.NewReader(submit))
	forged.Header.Set("Authorization", "Bearer "+other.Token)
	forged.Header.Set("X-Mesh-User", owner.UserID)
	forged.Header.Set("Content-Type", "application/json")
	out := httptest.NewRecorder()
	handler.ServeHTTP(out, forged)
	if out.Code < 400 {
		t.Fatal("forged owner identity was accepted")
	}
	assertStatus(chatRequest(handler, owner, route, `{"id":"bad","text":"launch"}`), http.StatusBadRequest)
	assertStatus(chatRequest(handler, owner, route, poll), http.StatusNotFound)
	assertStatus(operator(http.MethodGet, server.Cfg.FeedbackToken, ""), http.StatusUnauthorized)
	publicOperator := httptest.NewRequest(http.MethodGet, operatorRoute, nil)
	publicOperator.Header.Set("X-Mesh-Admin", token)
	out = httptest.NewRecorder()
	handler.ServeHTTP(out, publicOperator)
	assertStatus(out, http.StatusForbidden)
	for attempt := 0; attempt < 2; attempt++ {
		assertStatus(chatRequest(handler, owner, route, submit), http.StatusOK)
	}
	assertStatus(chatRequest(handler, owner, route, `{"id":"`+id+`","text":"change the request"}`), http.StatusConflict)
	out = operator(http.MethodGet, token, "")
	assertStatus(out, http.StatusOK)
	var turn map[string]string
	if err := json.Unmarshal(out.Body.Bytes(), &turn); err != nil || turn["text"] != "Review the app build" ||
		turn["account_id"] != owner.UserID || turn["id"] != id {
		t.Fatalf("invalid relay turn: %s", out.Body.String())
	}
	finish := `{"id":"` + id + `","account_id":"` + owner.UserID + `","reply":"The review finished"}`
	assertStatus(operator(http.MethodPost, token, strings.Replace(finish, owner.UserID, other.UserID, 1)), http.StatusBadRequest)
	assertStatus(operator(http.MethodPost, token, finish), http.StatusOK)
	assertStatus(operator(http.MethodPost, token, finish), http.StatusOK)
	assertStatus(operator(http.MethodPost, token, strings.Replace(finish, "finished", "failed", 1)), http.StatusConflict)
	out = operator(http.MethodGet, token, "")
	assertStatus(out, http.StatusOK)
	if strings.Contains(out.Body.String(), id) {
		t.Fatal("finished turn was offered again")
	}
	assertStatus(chatRequest(handler, other, route, poll), http.StatusForbidden)
	out = chatRequest(handler, owner, route, poll)
	assertStatus(out, http.StatusOK)
	if strings.Contains(out.Body.String(), "Review the app build") || !strings.Contains(out.Body.String(), "The review finished") {
		t.Fatalf("invalid owner reply: %s", out.Body.String())
	}
}
