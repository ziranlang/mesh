# Telegram Mini Apps and Stars purchases

Mesh can verify a Mini App launch for an existing Telegram-linked account and
sell canonical Dew packs for Telegram Stars. Both use the account's current
issuer, app registration and the bot already used for Telegram account entry.

## Enable an issuer

- Enable Telegram accounts with `MESH_TELEGRAM_ACCOUNTS_ENABLED=true`. Set the
  current merchant bot credential and webhook secret through the existing
  `MESH_LUMI_BOT_TOKEN_FILE` and `MESH_LUMI_WEBHOOK_SECRET_FILE` configuration.
- Set `MESH_TELEGRAM_STARS_APPS` to the comma-separated IDs of registered apps
  allowed to sell. For Inner Breeze the app ID is `inbe`.
- Set `MESH_TELEGRAM_STARS_PRICES` to comma-separated `product_id:whole_stars`
  entries. Each ID must identify an active product in the existing Dew catalog
  (`MESH_TOKEN_PRODUCTS`). Choose each integer XTR price explicitly. Packs
  with missing, ambiguous or invalid prices are unavailable. One Dew is
  1,000,000 wallet units.
- Register the Mini App URL for the same bot and use the existing
  `/api/v1/lumi/telegram/webhook`. Keep the webhook secret enabled and include
  `pre_checkout_query` and `message` in `allowed_updates`, alongside the
  existing chat update types. Private `/paysupport` directs buyers to the bot's
  existing feedback channel. The operator handles support requests and refunds.

The issuer must have the existing active Dew signing key. Leave the apps or
prices unset to keep invoice creation disabled. Telegram requires `XTR` for
digital goods. Developer rewards are subject to Telegram's terms and have no
fixed exchange rate. [Stars payments](https://core.telegram.org/bots/payments-stars) · [Developer terms](https://telegram.org/tos/bot-developers) · [Webhook settings](https://core.telegram.org/bots/api#setwebhook)

## Application contract

Keep `WebApp.initData` in memory for authenticated requests. The node verifies
the configured bot and a five-minute freshness window. It checks the actual
linked Telegram buyer and binds the signed digest to the owner, app and current
issuer. Decoded duplicate fields fail validation. The response never contains
the raw Telegram data or Telegram user ID. Handle `start_param` using the
application's routing rules. [Telegram launch verification](https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app)

| Request | Result |
| --- | --- |
| `POST /api/v1/telegram/launch` with `{app_id, init_data}` | `{app_id, start_param?, chat_type?, stars_available}` |
| `GET /api/v1/tokens/purchases/telegram/catalog?app_id=APP` | `{currency: "XTR", products: [{product_id, title, amount, units}]}` |
| `POST /api/v1/tokens/purchases/telegram/invoices` with `{app_id, product_id, request_id, init_data}` | Invoice record. New link creation returns HTTP 201. |
| `GET /api/v1/tokens/purchases/telegram/invoices/ID` | Invoice record for the authenticated purchaser. |
| `POST /api/v1/tokens/purchases/telegram/invoices/ID/cancel` with an empty body | Cancel an unapproved invoice. An approved pending checkout returns HTTP 409. |
| `GET /api/v1/tokens/purchases/telegram/balance?app_id=APP` | `{account_id, app_id, asset_id: "waozi:token", balance}` in wallet units. |

Use the existing `Bearer` owner session or the hosted `Delegate ds1.<session_id>`
session with a valid `X-Mesh-Delegate` proof. For the two GET query routes, the
proof signs the exact query `app_id=APP` with an empty body digest. Sign the
invoice path and method and hash the exact body sent over HTTP. The signed
Telegram-linked owner grant needs `telegram_stars: true` for hosted purchasing.
Append the literal ASCII line `telegram-stars-purchases` followed by one LF to
the existing grant canonical message after all collection scopes, before owner
signing. Existing grants without this field keep their prior encoding and may
verify launch, with `stars_available: false` for a Telegram-linked delegate.
The purchase capability grants read-only Dew balance and own-invoice access.
General wallet or account mutations retain their existing authorization rules.

An invoice record contains `invoice_id`, `product_id`, `currency`, `amount`,
`units`, `status`, and a pending `invoice_url` if available. Generate one random
request ID per intended purchase. Retry transient creation errors using the
same request ID, product and still-fresh launch data. A changed product for
that request ID returns HTTP 409. An unpaid quote expires after 15 minutes.
Read `invoice_url` from the authenticated response and pass it to Telegram's
`openInvoice`. Refresh issuer status after Telegram's `invoiceClosed` callback.
Refresh the wallet on `credited` or `refunded`. Cancel only on a deliberate
user request. A provider/link failure may leave a quote pending and retryable.
[Mini App invoice events](https://core.telegram.org/bots/webapps#events-available-for-mini-apps)

## Settlement and recovery

Pre-checkout validates the stored payload, bot, buyer, XTR amount, current
account/app binding and quote expiry before answering Telegram. After approval,
an authenticated `successful_payment` settles even if the dialog has closed
or the quote has expired. The charge record and signed canonical wallet credit
commit in the same SQLite transaction. A verified `refunded_payment` reverses
the original lot through the wallet ledger. Repeated or reordered events
converge on the same credit and reversal. A refund of consumed Dew can leave
wallet debt. Telegram's browser callback has no settlement authority.
[Checkout deadline](https://core.telegram.org/bots/api#answerprecheckoutquery) · [Refund API](https://core.telegram.org/bots/api#refundstarpayment)

For a merchant-initiated refund call Telegram's `refundStarPayment` as the bot
with the matched buyer and Telegram charge ID. If updates are missed, an
operator can `POST /api/v1/admin/tokens/purchases/telegram/reconcile` with
`{"offset":0}`. Use `X-Mesh-Admin` when configured, or call over a direct
loopback connection. This reads one provider history page of up to 32 entries
and returns `{seen, matched, next_offset}`. Scan further pages as needed. It
matches authentic history against the same approved stored invoice and charge
rules. Preserve the issuer database containing these records during backups
and deployments. [Stars transaction history](https://core.telegram.org/bots/api#getstartransactions)

## Local verification

Run `make -j1 generate` and `make -j1 test TEST_FLAGS='-run TestTelegramStars -count=1'`.
The test uses signed synthetic launches, a disposable SQLite issuer, and a
fake provider at loopback. It exercises checkout, replay, cancellation, refunds
and reconciliation without moving real Stars. Telegram's separate test
environment uses its own account, bot, signing key and `/test/` API path. The
deployed Mesh configuration here targets the production Telegram network.
[Telegram test environment](https://core.telegram.org/bots/webapps#using-bots-in-the-test-environment)
