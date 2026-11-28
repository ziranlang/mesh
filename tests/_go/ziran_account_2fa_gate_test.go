package main

// Go-target 2FA login-gate parity check. The native server enforces the same
// gate in account_2fa_native; these cases keep the generated Go login from
// drifting again (statuses, headers, codes, replay guard, recovery codes and
// the 10-per-account rate limit).

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha1"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"golang.org/x/crypto/chacha20poly1305"
	"golang.org/x/crypto/hkdf"
)

func gateIdentity(t *testing.T, seed byte) (string, string, string) {
	t.Helper()
	publicKey := bytes.Repeat([]byte{seed}, mlDSA44PublicKeySize)
	sum := sha256.Sum256(publicKey)
	userID := hex.EncodeToString(sum[:])
	signature := hex.EncodeToString(bytes.Repeat([]byte{seed + 0x11}, mlDSA44SignatureSize))
	return userID, hex.EncodeToString(publicKey), signature
}

// gateSealSecret mirrors account_2fa_verify's storage format: HKDF-SHA256 with salt
// "mesh-2fa-totp-key-v1" and info=user, then XChaCha20-Poly1305 with AAD
// "mesh-2fa-totp-v1\n"+user. The nonce is fixed for test determinism.
func gateSealSecret(t *testing.T, serverSecret []byte, userID string, raw []byte) string {
	t.Helper()
	reader := hkdf.New(sha256.New, serverSecret, []byte("mesh-2fa-totp-key-v1"), []byte(userID))
	key := make([]byte, 32)
	if _, err := io.ReadFull(reader, key); err != nil {
		t.Fatal(err)
	}
	aad := append([]byte("mesh-2fa-totp-v1\n"), userID...)
	aead, err := chacha20poly1305.NewX(key)
	if err != nil {
		t.Fatal(err)
	}
	nonce := bytes.Repeat([]byte{0x77}, 24)
	sealed := aead.Seal(nil, nonce, raw, aad)
	if len(sealed) != 36 {
		t.Fatalf("sealed length = %d", len(sealed))
	}
	return hex.EncodeToString(append(nonce, sealed...))
}

func gateHOTP(key []byte, step uint64) string {
	counter := make([]byte, 8)
	binary.BigEndian.PutUint64(counter, step)
	mac := hmac.New(sha1.New, key)
	mac.Write(counter)
	sum := mac.Sum(nil)
	offset := sum[19] & 0x0f
	code := (uint32(sum[offset]&0x7f)<<24 | uint32(sum[offset+1])<<16 |
		uint32(sum[offset+2])<<8 | uint32(sum[offset+3])) % 1000000
	return fmt.Sprintf("%06d", code)
}

func gateLogin(t *testing.T, handler http.Handler, userID, publicKeyHex, signature, headerCode, bodyExtra string) *httptest.ResponseRecorder {
	t.Helper()
	challengeReq := httptest.NewRequest(http.MethodGet, "/api/v1/sync/challenge?user_id="+userID, nil)
	challengeRes := httptest.NewRecorder()
	handler.ServeHTTP(challengeRes, challengeReq)
	if challengeRes.Code != http.StatusOK {
		t.Fatalf("challenge status = %d body=%s", challengeRes.Code, challengeRes.Body.String())
	}
	body := []byte(fmt.Sprintf(`{"user_id_hash":"%s","client_id":"test-client-1","public_key":"%s"%s}`, userID, publicKeyHex, bodyExtra))
	req := httptest.NewRequest(http.MethodPost, "/api/v1/sync/login", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Mesh-User", userID)
	req.Header.Set("X-Mesh-Signature", signature)
	if headerCode != "" {
		req.Header.Set("X-Mesh-2FA", headerCode)
	}
	res := httptest.NewRecorder()
	handler.ServeHTTP(res, req)
	return res
}

func gateEnableTotp(t *testing.T, store *Store, userID string, raw []byte) {
	t.Helper()
	enc := gateSealSecret(t, bytes.Repeat([]byte{0x99}, 32), userID, raw)
	if _, err := store.Database.Exec("INSERT INTO server_account_2fa(user_id_hash, enabled, totp_secret_enc) VALUES(?1, 1, ?2)", userID, enc); err != nil {
		t.Fatal(err)
	}
}

func gateResetStep(t *testing.T, store *Store, userID string) {
	t.Helper()
	if _, err := store.Database.Exec("UPDATE server_account_2fa SET last_used_step=-1 WHERE user_id_hash=?1", userID); err != nil {
		t.Fatal(err)
	}
}

func TestZiranAccountTwoFactorLoginGate(t *testing.T) {
	server, store, _ := testServer(t)
	handler := server.Routes()
	raw := bytes.Repeat([]byte{0x42}, 20)
	step := uint64(time.Now().Unix() / 30)

	mainUser, mainKey, mainSig := gateIdentity(t, 0x31)
	if token, _ := loginWithKey(t, handler, "", mainUser, mainKey, mainSig); token == "" {
		t.Fatal("initial login failed")
	}
	gateEnableTotp(t, store, mainUser, raw)

	// Missing code: 401 2fa_required with methods list and no-store.
	var sessionsBefore int
	if err := store.Database.QueryRow("SELECT COUNT(*) FROM server_auth_sessions WHERE user_id_hash=?1", mainUser).Scan(&sessionsBefore); err != nil {
		t.Fatal(err)
	}
	res := gateLogin(t, handler, mainUser, mainKey, mainSig, "", "")
	want := `{"error":"2fa_required","methods":["totp","recovery_code"]}` + "\n"
	if res.Code != http.StatusUnauthorized || res.Body.String() != want {
		t.Fatalf("missing-code login status = %d body=%q", res.Code, res.Body.String())
	}
	if got := res.Header().Get("Cache-Control"); got != "private, no-store" {
		t.Fatalf("missing-code cache-control = %q", got)
	}
	var sessions int
	if err := store.Database.QueryRow("SELECT COUNT(*) FROM server_auth_sessions WHERE user_id_hash=?1", mainUser).Scan(&sessions); err != nil {
		t.Fatal(err)
	}
	if sessions != sessionsBefore {
		t.Fatalf("session recorded despite 2fa_required: before=%d after=%d", sessionsBefore, sessions)
	}

	// The signature check runs before the 2FA gate.
	res = gateLogin(t, handler, mainUser, mainKey, mainSig[:len(mainSig)-2], "", "")
	if res.Code == http.StatusOK || strings.Contains(res.Body.String(), "2fa_required") {
		t.Fatalf("bad-signature login status = %d body=%s", res.Code, res.Body.String())
	}

	// Invalid 6-digit code.
	res = gateLogin(t, handler, mainUser, mainKey, mainSig, "", `,"totp_code":"999999"`)
	if res.Code != http.StatusUnauthorized || !strings.Contains(res.Body.String(), "invalid 2fa code") {
		t.Fatalf("invalid-code login status = %d body=%s", res.Code, res.Body.String())
	}

	// A valid TOTP code issues a session token.
	code := gateHOTP(raw, step)
	res = gateLogin(t, handler, mainUser, mainKey, mainSig, "", `,"totp_code":"`+code+`"`)
	if res.Code != http.StatusOK {
		t.Fatalf("totp login status = %d body=%s", res.Code, res.Body.String())
	}
	var payload LoginResponse
	if err := json.Unmarshal(res.Body.Bytes(), &payload); err != nil {
		t.Fatal(err)
	}
	if payload.AuthToken == "" {
		t.Fatal("totp login missing token")
	}

	// Replaying the same code is rejected because the used step advanced.
	res = gateLogin(t, handler, mainUser, mainKey, mainSig, "", `,"totp_code":"`+code+`"`)
	if res.Code != http.StatusUnauthorized || !strings.Contains(res.Body.String(), "invalid 2fa code") {
		t.Fatalf("replay login status = %d body=%s", res.Code, res.Body.String())
	}

	// Header code path.
	gateResetStep(t, store, mainUser)
	res = gateLogin(t, handler, mainUser, mainKey, mainSig, code, "")
	if res.Code != http.StatusOK {
		t.Fatalf("header-code login status = %d body=%s", res.Code, res.Body.String())
	}

	// Legacy two_factor_code body field.
	gateResetStep(t, store, mainUser)
	res = gateLogin(t, handler, mainUser, mainKey, mainSig, "", `,"two_factor_code":"`+code+`"`)
	if res.Code != http.StatusOK {
		t.Fatalf("two_factor_code login status = %d body=%s", res.Code, res.Body.String())
	}

	// A formatted lowercase recovery code is normalized, then consumed once.
	canonical := "A1B2C3D4E5F60718"
	digest := sha256.Sum256([]byte("mesh-2fa-recovery-v1\n" + mainUser + "\n" + canonical))
	if _, err := store.Database.Exec("INSERT INTO server_account_2fa_recovery(user_id_hash, code_hash, used_at) VALUES(?1, ?2, 0)", mainUser, hex.EncodeToString(digest[:])); err != nil {
		t.Fatal(err)
	}
	res = gateLogin(t, handler, mainUser, mainKey, mainSig, "", `,"recovery_code":"a1b2-c3d4 e5f6-0718"`)
	if res.Code != http.StatusOK {
		t.Fatalf("recovery login status = %d body=%s", res.Code, res.Body.String())
	}
	res = gateLogin(t, handler, mainUser, mainKey, mainSig, "", `,"recovery_code":"`+canonical+`"`)
	if res.Code != http.StatusUnauthorized || !strings.Contains(res.Body.String(), "invalid 2fa code") {
		t.Fatalf("recovery-reuse login status = %d body=%s", res.Code, res.Body.String())
	}

	// Account rate limit: 10 invalid attempts pass, the 11th gets 429.
	limitUser, limitKey, limitSig := gateIdentity(t, 0x32)
	loginWithKey(t, handler, "", limitUser, limitKey, limitSig)
	gateEnableTotp(t, store, limitUser, raw)
	for i := 0; i < 10; i++ {
		attempt := gateLogin(t, handler, limitUser, limitKey, limitSig, "", `,"totp_code":"999999"`)
		if attempt.Code != http.StatusUnauthorized {
			t.Fatalf("rate-limit attempt %d status = %d body=%s", i+1, attempt.Code, attempt.Body.String())
		}
	}
	res = gateLogin(t, handler, limitUser, limitKey, limitSig, "", `,"totp_code":"999999"`)
	if res.Code != http.StatusTooManyRequests || !strings.Contains(res.Body.String(), "2fa rate limit exceeded") {
		t.Fatalf("rate-limited login status = %d body=%s", res.Code, res.Body.String())
	}
	if got := res.Header().Get("Cache-Control"); got != "private, no-store" {
		t.Fatalf("rate-limited cache-control = %q", got)
	}
}
