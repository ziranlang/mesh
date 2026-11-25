# Changelog

## [Unreleased]

- Allow signed sync requests from Jungle at dejungle.si and jungle.waozi.xyz.

- Prove the native pull client's legacy peer headers with a fixture that accepts only `X-Daochi-Node-*` and verifies the request signature.

- Keep the native `/metrics` families and route labels equal to the Go server, with a parity test that fails on any name, type or label mismatch.

- Run `mesh.service` as a dedicated non-root `mesh` account with `NoNewPrivileges`, `ProtectSystem=strict`, `ReadWritePaths` and `PrivateTmp`, restoring ownership on a failed deploy.

- Keep a single persistent node identity when Go startups share a new identity path.

- Recheck signed peer request expiry when reserving its nonce and speed up expired delegated nonce cleanup.

- Refuse startup when an explicitly configured Ed25519 node identity is invalid or unreadable, preserving the persisted fallback identity.

- Pin a Ziran toolchain that can generate Go from the multiline OpenAPI strings.

- Verify Telegram Mini App launches and settle Telegram Stars Dew purchases and refunds in the existing account wallet.

- Organize server sources and tests by responsibility, move embedded node behavior, filesystem component validation and compression benchmarks into Ziran, and parse HTTP compression preferences in one pass.

- Return linked Lumi replies to the exact originating private Telegram message, with a replay-safe delivery receipt.

- Relay private Lumi developer turns for the configured Mesh owner account.

- Rename Daochi to Mesh at mesh.ziran-lang.org, preserving existing account keys, encrypted records and installed-client access.

- Offer each host only package releases it can run: the ZX revision it names (`zx=R`, read from each stored `.zx` header), revisions before 28 for released hosts that name none, and nothing older than its `oldest_host_api`. Serve a release's signed WebAssembly variant to web hosts that ask with `format=wasm`. Package staging keeps every archived release and blob, and the history keeps the newest release of every format, revision and interface.

- Have node-hosted Lumi read every referenced list item and propose one complete developer report with a single Send/Cancel preview; carry expanded tool arguments without truncation.

- Accept full Lumi developer reports up to 32 KiB of UTF-8 text without truncation, preserving account privacy and retry identity.

- Refuse an account backup upload unless it holds only the nonce and sealed bytes after its header, so a plain key can't be stored as a backup.

- Accept Sign in with Google tokens from the Waozi web client on the production node.

- Answer an empty Monero deposit list as an empty array, so the Dew site no longer says the node is not answering for accounts without deposits.

- Keep a password-sealed account backup the node cannot open, released for a login proof; unknown accounts get the same answer as wrong passwords.

- Store passkey-sealed account copies the node can't open, and find an account from a linked Google account when a node sets `MESH_GOOGLE_SIGNIN_CLIENT_IDS`.

- Add Shovey Island as an account unlock with a confirmed Dew price, a 40% launch offer and purchase restoration across devices.

- Require SHA-256 wallet RPC Digest authentication and canonical filesystem components for blob and package requests.

- Count daily Wurm check-ins by the node's UTC clock and grant each daily Dew reward once per account.

- Revoke signed-in sessions durably on logout and device revocation, including active sync connections.
- Bind Google Play credits to the purchasing account and recover pending consumption and refunds in the background.
- Enforce storage allowances atomically across mesh imports and preserve paid deposits when wallet results are incomplete.
- Verify complete gameplay evidence before granting new rewards; retain imported legacy progress without treating unverified marks as proof.
