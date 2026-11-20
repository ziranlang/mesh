package main

import (
	"bytes"
	"compress/gzip"
	"crypto/ed25519"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

const (
	starsRoot    = "/api/v1/tokens/purchases/telegram"
	starsWebhook = "/api/v1/lumi/telegram/webhook"
	starsLaunch  = "/api/v1/telegram/launch"
	starsSecret  = "synthetic-webhook-secret"
	starsPrice   = 23
	starsUnits   = 10_000_000
)

type starsFixture struct {
	*authorizationFixture
	providerCalls atomic.Int32
	failLink      atomic.Bool
	checks        chan bool
	history       atomic.Value
}

func newStarsFixture(t *testing.T) *starsFixture {
	t.Helper()
	fixture := &starsFixture{authorizationFixture: entrySetup(t).owner, checks: make(chan bool, 24)}
	fixture.history.Store(`[]`)
	cfg := &fixture.server.Cfg
	cfg.LumiWebhookSecret = starsSecret
	cfg.TelegramAccountsEnabled = true
	cfg.WaoziIssuerPrivateKey = tokenLedgerSigner()
	cfg.TelegramStarsApps = map[string]bool{"inbe": true}
	cfg.TelegramStarsPrices = ConfigValues_StarsPrices("dew_10:23,mismatched:9,duplicate:3,duplicate:5,free:0,overflow:9007199254740992")
	cfg.TokenProducts = map[string]TokenProduct{
		"dew_10":     {ProductID: "dew_10", TokenUnits: starsUnits},
		"dew_100":    {ProductID: "dew_100", TokenUnits: 100_000_000},
		"mismatched": {ProductID: "another_pack", TokenUnits: starsUnits},
		"duplicate":  {ProductID: "duplicate", TokenUnits: starsUnits},
		"free":       {ProductID: "free", TokenUnits: starsUnits},
		"overflow":   {ProductID: "overflow", TokenUnits: starsUnits},
	}
	if _, err := fixture.store.Database.Exec(`INSERT INTO server_lumi_telegram(account_id,telegram_id,linked_at) VALUES(?,1234,1)`, fixture.owner.UserID); err != nil {
		t.Fatal(err)
	}
	fixture.handler = fixture.server.Routes()

	provider := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case strings.HasSuffix(r.URL.Path, "/createInvoiceLink"):
			fixture.providerCalls.Add(1)
			var invoice struct {
				Payload, Currency string
				ProviderToken     *string `json:"provider_token"`
				Prices            []struct{ Amount int64 }
			}
			if json.NewDecoder(r.Body).Decode(&invoice) != nil || invoice.Currency != "XTR" ||
				invoice.ProviderToken == nil || *invoice.ProviderToken != "" || len(invoice.Prices) != 1 ||
				invoice.Prices[0].Amount != starsPrice || !strings.HasPrefix(invoice.Payload, "entity_stars.") || len(invoice.Payload) != 45 {
				t.Error("invalid server-created XTR invoice")
				w.WriteHeader(400)
				return
			}
			if fixture.failLink.Swap(false) {
				w.WriteHeader(502)
				return
			}
			fmt.Fprint(w, `{"ok":true,"result":"https://t.me/$synthetic_invoice"}`)
		case strings.HasSuffix(r.URL.Path, "/answerPreCheckoutQuery"):
			var reply struct {
				OK    bool
				ID    string `json:"pre_checkout_query_id"`
				Error string `json:"error_message"`
			}
			if json.NewDecoder(r.Body).Decode(&reply) != nil || reply.ID == "" || (!reply.OK && reply.Error == "") {
				t.Error("invalid pre-checkout answer")
				w.WriteHeader(400)
				return
			}
			fixture.checks <- reply.OK
			fmt.Fprint(w, `{"ok":true,"result":true}`)
		case strings.HasSuffix(r.URL.Path, "/getStarTransactions"):
			fmt.Fprintf(w, `{"ok":true,"result":{"transactions":%s}}`, fixture.history.Load().(string))
		case strings.HasSuffix(r.URL.Path, "/sendMessage"):
			var message struct{ Text string }
			if json.NewDecoder(r.Body).Decode(&message) != nil || !strings.Contains(message.Text, "/feedback") {
				t.Error("payment support response did not give the existing support route")
				w.WriteHeader(400)
				return
			}
			fmt.Fprint(w, `{"ok":true,"result":true}`)
		default:
			t.Error("unexpected synthetic Telegram API call")
			w.WriteHeader(404)
		}
	}))
	oldBase := TelegramAPIBase
	TelegramAPIBase = provider.URL
	t.Cleanup(func() { TelegramAPIBase = oldBase; provider.Close() })
	return fixture
}

func starsInit(fixture *starsFixture, buyer int, date int64, query string) string {
	return telegramInitFixture(fixture.server.Cfg.LumiBotToken, url.Values{
		"auth_date": {strconv.FormatInt(date, 10)},
		"user":      {fmt.Sprintf(`{"id":%d,"first_name":"Synthetic"}`, buyer)},
		"query_id":  {query}, "start_param": {"join_pack-1"}, "chat_type": {"private"},
	})
}

func starsRequest(handler http.Handler, method, path, token, secret string, input any) *httptest.ResponseRecorder {
	var raw []byte
	if input != nil {
		raw, _ = json.Marshal(input)
	}
	req := httptest.NewRequest(method, path, bytes.NewReader(raw))
	req.Header.Set("Content-Type", "application/json")
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	if secret != "" {
		req.Header.Set("X-Telegram-Bot-Api-Secret-Token", secret)
	}
	out := httptest.NewRecorder()
	handler.ServeHTTP(out, req)
	return out
}

func starsDecode[T any](t *testing.T, out *httptest.ResponseRecorder, status int) T {
	t.Helper()
	var data T
	if out.Code != status || json.Unmarshal(out.Body.Bytes(), &data) != nil {
		t.Fatalf("unexpected HTTP response: status=%d, expected=%d", out.Code, status)
	}
	return data
}

func (f *starsFixture) create(t *testing.T, key string, status int) InvoiceReply {
	t.Helper()
	return starsDecode[InvoiceReply](t, starsRequest(f.handler, "POST", starsRoot+"/invoices", f.owner.Token, "", map[string]string{
		"app_id": "inbe", "product_id": "dew_10", "request_id": key,
		"init_data": starsInit(f, 1234, time.Now().Unix(), "purchase"),
	}), status)
}

func (f *starsFixture) checkout(t *testing.T, invoice, query, currency string, buyer, amount int, allowed bool) {
	t.Helper()
	out := starsRequest(f.handler, "POST", starsWebhook, "", starsSecret, map[string]any{
		"update_id": 10, "pre_checkout_query": map[string]any{
			"id": query, "from": map[string]any{"id": buyer}, "currency": currency,
			"total_amount": amount, "invoice_payload": "entity_stars." + invoice,
		},
	})
	if out.Code != 200 {
		t.Fatalf("checkout status=%d", out.Code)
	}
	select {
	case answer := <-f.checks:
		if answer != allowed {
			t.Fatalf("checkout decision=%t, expected=%t", answer, allowed)
		}
	default:
		t.Fatal("missing checkout answer")
	}
}

func (f *starsFixture) webhook(invoice, charge, kind, currency string, buyer, amount int, secret string) *httptest.ResponseRecorder {
	return starsRequest(f.handler, "POST", starsWebhook, "", secret, map[string]any{
		"update_id": 11, "message": map[string]any{
			"from": map[string]any{"id": buyer, "is_bot": false},
			"chat": map[string]any{"id": buyer, "type": "private"}, kind: map[string]any{
				"currency": currency, "total_amount": amount, "invoice_payload": "entity_stars." + invoice,
				"telegram_payment_charge_id": charge,
			},
		},
	})
}

func (f *starsFixture) wallet(t *testing.T, credits, refunds int, balance int64) {
	t.Helper()
	var actualCredits, actualRefunds, processed int
	if err := f.store.Database.QueryRow(`SELECT COUNT(CASE WHEN event_type='credit' THEN 1 END),COUNT(CASE WHEN event_type='debit' THEN 1 END) FROM token_ledger WHERE source_type='telegram_stars'`).Scan(&actualCredits, &actualRefunds); err != nil {
		t.Fatal(err)
	}
	if err := f.store.Database.QueryRow(`SELECT COUNT(*) FROM token_processed_payments WHERE provider='telegram_stars'`).Scan(&processed); err != nil {
		t.Fatal(err)
	}
	result := starsDecode[TokenBalanceResponse](t, starsRequest(f.handler, "GET", starsRoot+"/balance?app_id=inbe", f.owner.Token, "", nil), 200)
	if actualCredits != credits || processed != credits || actualRefunds != refunds || result.Balance != balance || result.AssetID != "waozi:token" || result.AccountID != f.owner.UserID {
		t.Fatalf("ledger credits=%d processed=%d refunds=%d balance=%d", actualCredits, processed, actualRefunds, result.Balance)
	}
}

func TestTelegramStarsSignedLaunch(t *testing.T) {
	f := newStarsFixture(t)
	now := time.Now().Unix()
	valid := starsInit(f, 1234, now, "launch")
	for _, tc := range []struct {
		name, raw string
		code      int
	}{
		{"stale", starsInit(f, 1234, now-301, "stale"), 401},
		{"duplicate-decoded-key", valid + "&%75ser=x", 401},
		{"tampered", strings.Replace(valid, "join_pack-1", "join_pack-2", 1), 401},
		{"wrong-buyer", starsInit(f, 4321, now, "buyer"), 401},
		{"valid", valid, 200}, {"same-binding-retry", valid, 200},
	} {
		t.Run(tc.name, func(t *testing.T) {
			out := starsRequest(f.handler, "POST", starsLaunch, f.owner.Token, "", map[string]string{"app_id": "inbe", "init_data": tc.raw})
			if tc.code != 200 {
				if out.Code != tc.code {
					t.Fatalf("launch status=%d", out.Code)
				}
				return
			}
			data := starsDecode[map[string]any](t, out, 200)
			if len(data) != 4 || data["app_id"] != "inbe" || data["start_param"] != "join_pack-1" || data["chat_type"] != "private" || data["stars_available"] != true {
				t.Fatal("incorrect safe launch reply")
			}
		})
	}
	if _, err := f.store.Database.Exec(`INSERT INTO server_apps(app_id,display_name,status) VALUES('other','Other','active')`); err != nil {
		t.Fatal(err)
	}
	out := starsRequest(f.handler, "POST", starsLaunch, f.owner.Token, "", map[string]string{"app_id": "other", "init_data": valid})
	if out.Code != 409 {
		t.Fatalf("launch binding reuse status=%d", out.Code)
	}
	for origin, accept := range map[string]bool{"https://innerbreeze.org": true, "https://innerbreeze.org.example": false} {
		req := httptest.NewRequest("OPTIONS", starsLaunch, nil)
		req.Header.Set("Origin", origin)
		req.Header.Set("Access-Control-Request-Method", "POST")
		out := httptest.NewRecorder()
		f.handler.ServeHTTP(out, req)
		if (out.Header().Get("Access-Control-Allow-Origin") == origin) != accept {
			t.Fatal("wrong origin policy")
		}
	}
}

func TestTelegramStarsWebhookSecretAndSupport(t *testing.T) {
	f := newStarsFixture(t)
	req := httptest.NewRequest("POST", starsWebhook, strings.NewReader(`{"update_id":30}`))
	req.Header.Add("X-Telegram-Bot-Api-Secret-Token", starsSecret)
	req.Header.Add("X-Telegram-Bot-Api-Secret-Token", "conflicting-value")
	out := httptest.NewRecorder()
	f.handler.ServeHTTP(out, req)
	if out.Code != 401 {
		t.Fatalf("ambiguous authentication status=%d", out.Code)
	}
	out = starsRequest(f.handler, "POST", starsWebhook, "", starsSecret, map[string]any{
		"update_id": 31, "message": map[string]any{
			"from": map[string]any{"id": 1234}, "chat": map[string]any{"id": 1234, "type": "private"}, "text": "/paysupport"},
	})
	if out.Code != 200 {
		t.Fatalf("payment support status=%d", out.Code)
	}
}

func TestTelegramStarsSettlement(t *testing.T) {
	f := newStarsFixture(t)
	catalog := starsDecode[struct {
		Currency string
		Products []struct {
			Product       string `json:"product_id"`
			Amount, Units int64
		}
	}](t,
		starsRequest(f.handler, "GET", starsRoot+"/catalog?app_id=inbe", f.owner.Token, "", nil), 200)
	if catalog.Currency != "XTR" || len(catalog.Products) != 1 || catalog.Products[0].Product != "dew_10" ||
		catalog.Products[0].Amount != starsPrice || catalog.Products[0].Units != starsUnits {
		t.Fatal("noncanonical catalog")
	}

	invoice := f.create(t, "purchase-first", 201)
	if invoice.ID == "" || invoice.Currency != "XTR" || invoice.Amount != starsPrice || invoice.Units != starsUnits ||
		invoice.Status != "pending" || invoice.Link == "" || f.create(t, "purchase-first", 200) != invoice || f.providerCalls.Load() != 1 {
		t.Fatal("quote or create idempotency changed")
	}
	request := map[string]string{"app_id": "inbe", "product_id": "dew_100", "request_id": "purchase-first", "init_data": starsInit(f, 1234, time.Now().Unix(), "purchase")}
	if out := starsRequest(f.handler, "POST", starsRoot+"/invoices", f.owner.Token, "", request); out.Code != 409 {
		t.Fatal("request ID accepted a changed product")
	}

	const success, refund, charge = "successful_payment", "refunded_payment", "fixture:unique-charge"
	if f.webhook(invoice.ID, charge, success, "XTR", 1234, starsPrice, "wrong-secret").Code != 401 ||
		f.webhook(invoice.ID, charge, success, "XTR", 1234, starsPrice, starsSecret).Code != 409 {
		t.Fatal("untrusted or unapproved payment accepted")
	}
	f.checkout(t, invoice.ID, "buyer", "XTR", 4321, starsPrice, false)
	f.checkout(t, invoice.ID, "price", "XTR", 1234, starsPrice+1, false)
	f.checkout(t, invoice.ID, "currency", "USD", 1234, starsPrice, false)
	f.checkout(t, strings.Repeat("a", 32), "wrong-invoice", "XTR", 1234, starsPrice, false)
	f.checkout(t, invoice.ID, "valid", "XTR", 1234, starsPrice, true)
	f.checkout(t, invoice.ID, "valid", "XTR", 1234, starsPrice, true)
	f.checkout(t, invoice.ID, "new-checkout", "XTR", 1234, starsPrice, false)
	if f.webhook(invoice.ID, charge, success, "XTR", 4321, starsPrice, starsSecret).Code != 409 ||
		f.webhook(invoice.ID, charge, success, "XTR", 1234, starsPrice+1, starsSecret).Code != 409 ||
		f.webhook(invoice.ID, charge, success, "USD", 1234, starsPrice, starsSecret).Code != 400 ||
		f.webhook(invoice.ID, charge, success, "XTR", 1234, starsPrice, "").Code != 401 {
		t.Fatal("wrong payment terms accepted")
	}
	path := starsRoot + "/invoices/" + invoice.ID
	otherOwner := MlDsa44_KeyPair()
	if otherOwner.Error != nil {
		t.Fatal(otherOwner.Error)
	}
	t.Cleanup(func() { clear(otherOwner.PrivateKey) })
	otherAccount := Signing_SHA256Hex(otherOwner.PublicKey)
	if _, err := f.store.Database.Exec(`INSERT INTO server_users(user_id_hash,public_key) VALUES(?,?)`, otherAccount, otherOwner.PublicKey); err != nil {
		t.Fatal(err)
	}
	otherBearer := Token_IssueAuthToken(f.server.Cfg.TokenSecret, otherAccount, time.Now().Add(time.Hour).Unix())
	if otherBearer.Error != "" {
		t.Fatal(otherBearer.Error)
	}
	recordTestBearer(t, f.server, otherBearer.Value)
	if out := starsRequest(f.handler, "GET", path, otherBearer.Value, "", nil); out.Code != 404 {
		t.Fatalf("invoice visible to another account: status=%d", out.Code)
	}
	if starsRequest(f.handler, "POST", path+"/cancel", f.owner.Token, "", nil).Code != 409 {
		t.Fatal("approved checkout could be cancelled")
	}
	f.wallet(t, 0, 0, 0)

	var calls sync.WaitGroup
	for i := 0; i < 8; i++ {
		calls.Go(func() {
			if out := f.webhook(invoice.ID, charge, success, "XTR", 1234, starsPrice, starsSecret); out.Code != 200 {
				t.Errorf("parallel payment status=%d", out.Code)
			}
		})
	}
	calls.Wait()
	settled := starsDecode[InvoiceReply](t, starsRequest(f.handler, "GET", path, f.owner.Token, "", nil), 200)
	if settled.Status != "credited" || settled.Link != "" {
		t.Fatal("settled invoice status or link is incorrect")
	}
	f.wallet(t, 1, 0, starsUnits)
	const spent = 2_000_000
	spend := starsRequest(f.handler, "POST", "/api/v1/tokens/spend", f.owner.Token, "", TokenSpendRequest{
		AppID: "inbe", AssetID: "waozi:token", Amount: spent, Action: "feature_unlock", IdempotencyKey: "stars-fixture-spend",
	})
	if spend.Code != 200 {
		t.Fatalf("canonical spend status=%d", spend.Code)
	}
	other := f.create(t, "purchase-other", 201)
	f.checkout(t, other.ID, "other", "XTR", 1234, starsPrice, true)
	if f.webhook(other.ID, charge, success, "XTR", 1234, starsPrice, starsSecret).Code != 409 ||
		f.webhook(invoice.ID, "different:charge", refund, "XTR", 1234, starsPrice, starsSecret).Code != 409 {
		t.Fatal("charge accepted for different invoice or refund")
	}
	for i := 0; i < 2; i++ {
		if f.webhook(invoice.ID, charge, refund, "XTR", 1234, starsPrice, starsSecret).Code != 200 {
			t.Fatal("verified refund failed")
		}
	}
	if f.webhook(invoice.ID, charge, success, "XTR", 1234, starsPrice, starsSecret).Code != 200 {
		t.Fatal("late duplicate payment failed")
	}
	if starsDecode[InvoiceReply](t, starsRequest(f.handler, "GET", path, f.owner.Token, "", nil), 200).Status != "refunded" {
		t.Fatal("missing refunded state")
	}
	f.wallet(t, 1, 1, -spent)
}

func TestTelegramStarsCancelExpiryAndRefundFirstHistory(t *testing.T) {
	f := newStarsFixture(t)
	f.failLink.Store(true)
	f.create(t, "purchase-retry", 503)
	invoice := f.create(t, "purchase-retry", 201)
	path := starsRoot + "/invoices/"
	cancel := starsDecode[InvoiceReply](t, starsRequest(f.handler, "POST", path+invoice.ID+"/cancel", f.owner.Token, "", nil), 200)
	if cancel.Status != "cancelled" || cancel.Link != "" {
		t.Fatal("incorrect cancel state")
	}
	f.checkout(t, invoice.ID, "cancelled", "XTR", 1234, starsPrice, false)
	if f.webhook(invoice.ID, "cancelled-charge", "successful_payment", "XTR", 1234, starsPrice, starsSecret).Code != 409 {
		t.Fatal("cancelled invoice credited")
	}

	expired := f.create(t, "purchase-expired", 201)
	if _, err := f.store.Database.Exec(`UPDATE telegram_stars_invoices SET expires_at=1 WHERE invoice_id=?`, expired.ID); err != nil {
		t.Fatal(err)
	}
	if got := starsDecode[InvoiceReply](t, starsRequest(f.handler, "GET", path+expired.ID, f.owner.Token, "", nil), 200); got.Status != "expired" || got.Link != "" {
		t.Fatal("incorrect expiry state")
	}
	f.checkout(t, expired.ID, "expired", "XTR", 1234, starsPrice, false)

	approved := f.create(t, "purchase-approved", 201)
	f.checkout(t, approved.ID, "approved", "XTR", 1234, starsPrice, true)
	if _, err := f.store.Database.Exec(`UPDATE telegram_stars_invoices SET expires_at=1 WHERE invoice_id=?`, approved.ID); err != nil {
		t.Fatal(err)
	}
	f.wallet(t, 0, 0, 0)
	f.history.Store(fmt.Sprintf(`[
		{"id":"history-charge","amount":%d,"receiver":{"type":"user","transaction_type":"invoice_payment","user":{"id":1234},"invoice_payload":"entity_stars.%s"}},
		{"id":"history-charge","amount":%d,"source":{"type":"user","transaction_type":"invoice_payment","user":{"id":1234},"invoice_payload":"entity_stars.%s"}},
		{"id":"fake-history","amount":%d,"source":{"type":"user","transaction_type":"invoice_payment","user":{"id":4321},"invoice_payload":"entity_stars.%s"}}
	]`, starsPrice, approved.ID, starsPrice, approved.ID, starsPrice, approved.ID))
	adminPath := "/api/v1/admin/tokens/purchases/telegram/reconcile"
	if out := starsRequest(f.handler, "POST", adminPath, f.owner.Token, "", map[string]int{"offset": 0}); out.Code != 403 {
		t.Fatalf("remote reconciliation status=%d", out.Code)
	}
	for i := 0; i < 2; i++ {
		req := httptest.NewRequest("POST", adminPath, strings.NewReader(`{"offset":0}`))
		req.RemoteAddr = "127.0.0.1:1024"
		req.Host = "127.0.0.1"
		req.Header.Set("Content-Type", "application/json")
		out := httptest.NewRecorder()
		f.handler.ServeHTTP(out, req)
		result := starsDecode[map[string]int](t, out, 200)
		if result["seen"] != 3 || result["matched"] != 2 || result["next_offset"] != 3 {
			t.Fatal("incorrect reconciliation results")
		}
	}
	f.wallet(t, 1, 1, 0)
}

func TestTelegramStarsWithdrawnOffer(t *testing.T) {
	f := newStarsFixture(t)
	approved := f.create(t, "approved-before-disable", 201)
	f.checkout(t, approved.ID, "approved-checkout", "XTR", 1234, starsPrice, true)
	withdrawn := f.create(t, "withdrawn-existing-link", 201)
	f.failLink.Store(true)
	f.create(t, "withdrawn-link-retry", 503)

	f.server.Cfg.TelegramStarsPrices = nil
	f.create(t, "withdrawn-link-retry", 404)
	f.create(t, "withdrawn-existing-link", 404)
	if calls := f.providerCalls.Load(); calls != 3 {
		t.Fatalf("withdrawn offer reached provider: %d calls", calls)
	}
	f.checkout(t, withdrawn.ID, "withdrawn-checkout", "XTR", 1234, starsPrice, false)
	status := starsDecode[InvoiceReply](t, starsRequest(f.handler, "GET", starsRoot+"/invoices/"+withdrawn.ID, f.owner.Token, "", nil), 200)
	if status.Link != "" {
		t.Fatal("withdrawn offer returned a checkout link")
	}
	f.server.Cfg.TelegramStarsPrices = ConfigValues_StarsPrices("dew_10:24")
	f.checkout(t, withdrawn.ID, "changed-quote-checkout", "XTR", 1234, starsPrice, false)
	if got := f.webhook(approved.ID, "already-paid", "successful_payment", "XTR", 1234, starsPrice, starsSecret).Code; got != 200 {
		t.Fatalf("approved payment after offer withdrawal got status %d", got)
	}
	f.wallet(t, 1, 0, starsUnits)
}

func TestTelegramStarsPayloadlessRefundBeforePayment(t *testing.T) {
	f := newStarsFixture(t)
	invoice := f.create(t, "history-without-payload", 201)
	f.checkout(t, invoice.ID, "approved-history", "XTR", 1234, starsPrice, true)
	f.history.Store(fmt.Sprintf(`[
		{"id":"refund-first","amount":%d,"receiver":{"type":"user","transaction_type":"invoice_payment","user":{"id":1234}}}
	]`, starsPrice))
	for i := 0; i < 2; i++ {
		req := httptest.NewRequest("POST", "/api/v1/admin/tokens/purchases/telegram/reconcile", strings.NewReader(`{"offset":0}`))
		req.RemoteAddr = "127.0.0.1:1024"
		req.Host = "127.0.0.1"
		out := httptest.NewRecorder()
		f.handler.ServeHTTP(out, req)
		result := starsDecode[map[string]int](t, out, 200)
		if result["seen"] != 1 || result["matched"] != 0 || result["next_offset"] != 1 {
			t.Fatal("unresolved refund reported as settled")
		}
	}
	f.wallet(t, 0, 0, 0)
	for i := 0; i < 2; i++ {
		if got := f.webhook(invoice.ID, "refund-first", "successful_payment", "XTR", 1234, starsPrice, starsSecret).Code; got != 200 {
			t.Fatalf("payment after reconciled refund got status %d", got)
		}
	}
	status := starsDecode[InvoiceReply](t, starsRequest(f.handler, "GET", starsRoot+"/invoices/"+invoice.ID, f.owner.Token, "", nil), 200)
	if status.Status != "refunded" {
		t.Fatalf("payment after reconciled refund has state %q", status.Status)
	}
	f.wallet(t, 1, 1, 0)
}

func (f *starsFixture) delegateStars(t *testing.T, method, target string, body []byte) *http.Request {
	t.Helper()
	request := httptest.NewRequest(method, target, bytes.NewReader(body))
	f.sequence++
	proof := RequestProof{Version: 1, GrantID: f.grant.GrantID, SessionID: f.credential.SessionID,
		AccountID: f.owner.UserID, AppID: f.grant.AppID, ClientID: f.grant.ClientID, NodeID: f.server.Node.ID,
		Audience: f.server.Cfg.BaseURL, Challenge: f.credential.Challenge, Method: method, Path: request.URL.Path,
		Query: request.URL.RawQuery, BodySHA256: Signing_SHA256Hex(body), ExpiresAt: time.Now().Unix() + 30, Nonce: fmt.Sprintf("%032x", f.sequence)}
	proof.Signature = hex.EncodeToString(ed25519.Sign(f.delegate, []byte(Session_ProofMessage(proof))))
	header, _ := json.Marshal(proof)
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Authorization", "Delegate ds1."+f.credential.SessionID)
	request.Header.Set("X-Mesh-Delegate", string(header))
	return request
}

func TestTelegramStarsDelegatedPurchaseBoundary(t *testing.T) {
	f := newStarsFixture(t)
	ownerSignature := func(msg string) string {
		signed := MlDsa44_Sign([]byte(msg), f.ownerPrivate)
		if signed.Error != nil {
			t.Fatal(signed.Error)
		}
		return hex.EncodeToString(signed.Value)
	}
	for index, permitted := range []bool{false, true} {
		pending := entryPending(t, &entryFixture{owner: f.authorizationFixture})
		init := starsInit(f, 1234, time.Now().Unix(), fmt.Sprintf("claim-%d", index))
		claim := RendezvousClaim{RequestID: pending.RequestID, ClientID: strings.Repeat("b", 64),
			SigningKey: hex.EncodeToString(f.delegate.Public().(ed25519.PublicKey)), EncryptionKey: strings.Repeat("c", 2368), InitData: init}
		digest := TelegramInit_Validate(init, f.server.Cfg.LumiBotToken, time.Now().Unix()).Digest
		claim.Proof = hex.EncodeToString(ed25519.Sign(f.delegate, []byte(AuthorizationHttp_ClaimMessage(pending, claim, digest))))
		claimed := authorizationCall(t, f.handler, "/api/v1/authorization/claim", claim, nil)
		if claimed.Code != 200 {
			t.Fatalf("Telegram delegate claim status=%d", claimed.Code)
		}
		now := time.Now().Unix()
		grant := Grant{Version: 1, AccountID: f.owner.UserID, GrantID: fmt.Sprintf("%032x", 100+index),
			RequestID: pending.RequestID, AppID: "inbe", ClientID: claim.ClientID, NodeID: pending.NodeID,
			Audience: pending.Audience, SigningKey: claim.SigningKey, EncryptionKey: claim.EncryptionKey,
			IssuedAt: now, NotBefore: now, ExpiresAt: now + 3600, Nonce: fmt.Sprintf("%032x", 110+index), BotID: 123456, TelegramID: 1234,
			Scopes: []GrantScope{{Collection: "private.inbe.v2.lumi", Visibility: "private", Read: true, Write: true, KeyID: "lumi-key-v2", KeyEnvelope: "synthetic-envelope"}}}
		headers := map[string]string{"Authorization": "Bearer " + f.owner.Token}
		if permitted {
			baseMessage := Authorization_GrantMessage(grant)
			grant.TelegramStars = true
			if Authorization_GrantMessage(grant) != baseMessage+"telegram-stars-purchases\n" {
				t.Fatal("unexpected signed capability encoding")
			}
			grant.Signature = ownerSignature(baseMessage)
			unsigned := authorizationCall(t, f.handler, "/api/v1/authorization/approve", grant, headers)
			if unsigned.Code != 401 {
				t.Fatalf("unsigned capability status=%d", unsigned.Code)
			}
		}
		grant.Signature = ownerSignature(Authorization_GrantMessage(grant))
		approved := authorizationCall(t, f.handler, "/api/v1/authorization/approve", grant, headers)
		if approved.Code != 200 {
			t.Fatalf("signed capability approval status=%d", approved.Code)
		}
		f.grant = grant
		f.renew(t)
		launchBody, _ := json.Marshal(map[string]string{"app_id": "inbe", "init_data": starsInit(f, 1234, time.Now().Unix(), "delegated-launch")})
		out := httptest.NewRecorder()
		f.handler.ServeHTTP(out, f.delegateStars(t, "POST", starsLaunch, launchBody))
		launch := starsDecode[map[string]any](t, out, 200)
		if launch["stars_available"] != permitted {
			t.Fatal("incorrect launch feature bit")
		}
		path := starsRoot + "/balance?app_id=inbe"
		input := f.delegateStars(t, "GET", path, nil)
		out = httptest.NewRecorder()
		f.handler.ServeHTTP(out, input)
		if !permitted && out.Code != 403 || permitted && out.Code != 200 {
			t.Fatalf("delegated scope status=%d", out.Code)
		}
		if permitted {
			t.Run("bodyless routes reject transmitted bytes", func(t *testing.T) {
				invoice := f.create(t, "delegate-body-boundary", 201)
				for _, route := range []struct{ method, target string }{
					{"GET", path},
					{"GET", starsRoot + "/catalog?app_id=inbe"},
					{"GET", starsRoot + "/invoices/" + invoice.ID},
					{"POST", starsRoot + "/invoices/" + invoice.ID + "/cancel"},
				} {
					req := f.delegateStars(t, route.method, route.target, nil)
					req.Body = httptest.NewRequest(route.method, route.target, strings.NewReader(" ")).Body
					req.ContentLength = -1
					result := httptest.NewRecorder()
					f.handler.ServeHTTP(result, req)
					if result.Code != 400 {
						t.Errorf("%s %s accepted an unsigned body: status=%d", route.method, route.target, result.Code)
					}
				}
			})
			t.Run("compressed launch is not the signed body", func(t *testing.T) {
				var compressed bytes.Buffer
				encoder := gzip.NewWriter(&compressed)
				if _, err := encoder.Write(launchBody); err != nil {
					t.Fatal(err)
				}
				if err := encoder.Close(); err != nil {
					t.Fatal(err)
				}
				req := f.delegateStars(t, "POST", starsLaunch, launchBody)
				req.Body = httptest.NewRequest("POST", starsLaunch, &compressed).Body
				req.ContentLength = int64(compressed.Len())
				req.Header.Set("Content-Encoding", "gzip")
				result := httptest.NewRecorder()
				f.handler.ServeHTTP(result, req)
				if result.Code != 400 {
					t.Fatalf("compressed launch accepted a digest of different bytes: status=%d", result.Code)
				}
			})
			out = httptest.NewRecorder()
			f.handler.ServeHTTP(out, input)
			if out.Code != 409 {
				t.Fatalf("delegated request replay status=%d", out.Code)
			}
		}
		out = httptest.NewRecorder()
		f.handler.ServeHTTP(out, f.delegateStars(t, "POST", "/api/v1/tokens/spend", []byte(`{}`)))
		if out.Code != 401 {
			t.Fatalf("general wallet authorized for delegate: status=%d", out.Code)
		}
	}
}
