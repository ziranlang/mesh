# Development

Mesh's server application is written in Ziran. The server build uses Ziran's
Go backend and links the Go runtime. It generates `.zi.go` and `.zi_test.go`
files in the root Go package as ignored build outputs.

| Path | Contents |
| --- | --- |
| `src/server` | Entry point, startup, configuration, routing and operations |
| `src/http` | HTTP middleware, body limits, compression and WebSockets |
| `src/node` | Peer discovery, trust, replication and the embedded node |
| `src/sync` | Client sync, cursors, history and record transfer |
| `src/storage` | SQLite, schema, retention and storage accounting |
| `src/protocol` | Wire types, signing, encryption and serialization |
| `src/accounts`, `src/apps`, `src/chat`, `src/payments`, `src/token` | Application features |
| `tests/ziran` | Ziran tests run by the Go backend |
| `tests/native` | Shared Ziran code tested with the native C backend |
| `tests/_go` | Go regression tests and independent behavior baselines |
| `testdata` | Shared protocol fixtures and standalone reference programs |
| `native`, `embedded_host.go` | Go network and file-lock adapters, and C entry points |
| `bench/compression` | Ziran compression benchmarks and native codec comparisons |

## Requirements and build

Use Go 1.26, a C compiler, CMake and the Ziran launcher. The package lock selects
the exact Ziran toolchain. The Makefile builds the required liboqs implementation
and sets the cgo flags for Go, liboqs and SQLite.

```sh
make generate
make build
make test-ziran
```

Generation checks canonical `.zi` sources and creates the native executable
entry through the compiler. `make build` regenerates when source or toolchain
inputs change. CI generates from the locked toolchain before testing and
deployment. `ZIRAN` overrides the launcher when validating a local toolchain.

For development against the canonical local Ziran checkout, an ignored
`ziran.local.toml` can select it:

```toml
[overrides]
ziran = "../../ziranlang/ziran"
```

General compiler and standard-library changes belong in Ziran upstream. Publish
them there first, then update this project's toolchain through Ziran's package
commands. Do not hand-edit lockfiles or downstream dependency source copies.

## Tests

`make test-ziran` runs the application suite against generated Go from source
and saved `.zir`. It also runs `make test-ziran-native`, which builds and runs
tests for binary fields, token signing and payloads, account and habit identifiers, path
names, HTTP compression negotiation, collection scopes, encrypted record
validation, IP address classification, WebSocket handshakes and framing, and
readiness decisions and JSON with Ziran's C backend. The WebSocket handshake
uses the same locked Crypto package to derive its accept key in both backends.
The native tests also compile the SQLite schema and inspection SQL shared by
the storage and CLI modules and check inspection report formatting.
`make test-ziran-native-sqlite` links the native SQLite build and checks
database readiness, inspection reports, bearer-session migration and revocation,
and statement cleanup. Its schema tests cover legacy social rows, plans,
foreign-key cascades, WAL visibility, concurrent writers, alias collisions,
reward snapshots, packing invalidation, accounting backfills, built-in app seeds,
and atomic migration of legacy timestamps and habit histories.
Build SQLite with its `make native` target first. Set `SQLITE_NATIVE_DIR` if its
library is outside the adjacent package checkout.

`make test-ziran-native-http` also links GNU libmicrohttpd on Linux and exercises
HTTP and WebSocket traffic on disposable IPv4 and IPv6 loopback sockets. Its
fixtures cover bearer bootstrap, proxy address precedence, signed login over
gzip, device enrollment and revocation, and signed record sync with quotas,
replay detection, pagination and audit logs. They use independent Go
ML-DSA-44 and Ed25519 signing fixtures. Native Ed25519 verification links
OpenSSL 3's `libcrypto`.
Set `MICROHTTPD_NATIVE_DIR` if the library is outside the system linker path.
`make test-ziran-native-server` also builds the Linux Ziran server entry point
and runs the account and sync flows through its routes. It checks readiness,
WebSocket notifications after committed writes, graceful shutdown with active
connections, profile icons and account exports, signed account deletion and
recovery, startup retention of held accounts, and QR device linking. Account
recovery tests cover sealed password and passkey backups, proof validation,
rollback, and password lockouts that survive server restarts. Sync
fixtures cover typed changes, migrated snapshots, operation replay and compaction,
state hash history, and signed v6 rollback. Encrypted envelope tests exercise
pagination, retention, quotas, and after-commit notification. App registry tests
cover admin and signed registration, manifest ordering, rollback, and LAN and
node restrictions. The suite checks stable node identities and signed pairing
between two native nodes, including invite replay, database rollback and LAN
outbound address restrictions. The native server links `libcurl.so.4` for peer
HTTP requests. Namespace fixtures cover authority creation, signed claims,
concurrent sequence assignment, rollback, and verified name resolution. Peer
replication fixtures exercise signed export and import with scoped policies,
replay protection, pagination, quotas, and rollback of an entire import batch.
They also cover paired and configured pulls, atomic cursor writes, resume
after restart, and shutdown during a peer request. The server serves `/metrics`
in Prometheus text format and requires admin authentication when
`MESH_ADMIN_TOKEN` is set. The native endpoint reports usage and storage gauges,
active WebSockets, rate-limit denials, and HTTP request counts and durations by
method, bounded route label, and queued response status. It retains the existing
`ksync_` aliases for the HTTP families, limiter counter, and active WebSocket
gauge. The Go-specific sync, authentication, WebSocket accept and reject, and
payment counters still need native instrumentation. The suite also tests
package update hints, signed grant creation and revocation, and grant-scoped
record reads.
The native server suite exercises the landing page and OpenAPI document,
feedback reports and replies, publication visibility and quotas, friend and
profile-stat routes, CORS preflights, chat quotas, Google sign-in, and shared
lists. Social and list writes send WebSocket hints to affected accounts. The
Lumi developer relay uses `MESH_LUMI_DEVELOPER_TOKEN` or its file setting. The
token read routes use the configured issuer and products, and `/readyz`
reports the issuer's actual signing or read-only status. The feedback inbox
uses `MESH_FEEDBACK_TOKEN` or `MESH_FEEDBACK_TOKEN_FILE` before falling back to
`MESH_ADMIN_TOKEN` or `MESH_ADMIN_TOKEN_FILE`. Without an operator token, the
inbox and reply routes are disabled.
Package tests cover signed v1 archives, v2 release
history and rollouts, ZIB, ZX and WebAssembly selection, and expiring download
grants. Private blob tests cover upload limits, signed downloads, repair, quota,
trash, restoration, and startup retention. The server verifies package and blob
hashes in bounded chunks, then streams from the verified descriptor through
libmicrohttpd. Both routes use the rooted file APIs in the adjacent Ziran
standard library. Set
`ZIRAN_STD="$PWD/../ziran/std"` if the installed toolchain predates those APIs.
`make build` continues to use the Go backend while the remaining routes are
being ported. Use `make build-ziran-native-server` to build the native server at
`build/native-tests/server/server_native`. Run it without arguments or with
`serve` to start the service. Use `inspect` for a read-only database report.
`reward-dew REWARD ACCOUNT DEW` issues an offline reward against `MESH_DB`
using the configured Dew issuer private key. Replaying the same award returns
the existing credit.
`--version`, node info, and metrics report the build's `VERSION` override or
source revision. Set `SOURCE_COMMIT` when building from a source archive.
Native LAN discovery loads `libavahi-core.so.7` at runtime when
`MESH_LAN_DISCOVERY` is enabled. Include Avahi Core in the Linux runtime when
packaging the native server. The service registers only a configured TCP port.
`GET /api/v1/rates` serves the latest stored Monero USD quote and the configured
fixed Dew ratio. The native refresh worker polls CoinGecko, Kraken, and
CoinPaprika every 600 seconds, requires at least two agreeing quotes, and
serves a quote for up to two hours. `MESH_XMR_RATE_REFRESH_ENABLED=false`
disables USD quotes. Native fixtures use the `MESH_XMR_COINGECKO_URL`,
`MESH_XMR_KRAKEN_URL`, and `MESH_XMR_COINPAPRIKA_URL` overrides with loopback
providers and set `MESH_XMR_RATE_REFRESH_INTERVAL_SECONDS` to shorten the cycle.
When a Monero wallet RPC URL is configured, the native server reconciles
invoices and deposits every 60 seconds. `MESH_MONERO_RECONCILE_SECONDS` sets
the interval. Deposit audit records capture the current USD quote, while
credit uses the configured atomic-unit to Dew ratio.
The native node advertises `google-play-purchases` when a Google service
account or an OAuth client and refresh token are configured. The Play
fixture covers verification, consume recovery, and refunds against a
loopback provider.
`make test-ziran-native-http-coding` adds zlib and libzstd and checks wire and
decoded size limits, concatenated members and frames, negotiation, and
interoperability with Python gzip and the `zstd` command. During development it
uses the adjacent Ziran and Compression checkouts through `ziran.local.toml`.
`make test-ziran-native-ml-dsa44` links Mesh's liboqs build and verifies
native key generation, signing, and the same Wycheproof cases used by the
application tests.
`make test-ziran-native-account-auth` exercises signed login with SQLite and
liboqs, including concurrent challenge use, session rollback and revocation.

The test runner adds `tests/_go` to the server package with a Go overlay.
Independent fixtures cover native types, signatures, SQL behavior, protocol
bytes, cancellation, replay prevention and resource cleanup.

```sh
make test-ziran GOFLAGS='-mod=mod -race -count=1'
make test TEST_FLAGS='-tags=mesh_embedded -run TestEmbeddedNode'
```

`make build-ziran-native-embedded` builds the opt-in Linux C ABI library at
`build/native-tests/embedded/libmesh_embedded.so`. Link with
`-lmesh_embedded` and include `native/mesh_embedded.h`. The library exports
`NodeStart`, `NodeStop`, `NodeSuspend`, `NodeResume` and `NodeRequest`.
`make test-ziran-native-embedded` links a C host and exercises disposable
profiles, peer listener suspension, in-process requests and durable state.

The native library and server use the Linux C target, libmicrohttpd, SQLite,
liboqs, OpenSSL, libcurl, zstd, zlib and PCRE2. The embedded library uses
POSIX threads, Linux file metadata and Unix socket pairs. The Go build and
the `mesh_embedded` Go host are still the defaults for their existing entry
points.

Keep test concurrency within the managed machine budget. Tests do not use the
owner's display. Visual or window-management tests require a private display
and isolated processes. `make compression-bench` builds the Ziran codec, HTTP,
and retained-memory benchmarks. Their Go backend dependencies are pinned in a
separate module. See the [benchmark instructions](../bench/compression/README.md).

## Ownership and boundaries

- Mesh owns node and server policy, authenticated protocol handling, storage,
  quotas, replication and operational endpoints.
- Mesh Client is the independent Ziran protocol library. Applications supply
  key storage, merge policy and platform transport; the server does not receive
  client private keys.
- Reusable language, codec, I/O and native binding capabilities belong in Ziran.
  Widgets and application UI belong in their corresponding libraries and apps.
- Native Go tests and independent fixtures verify generated behavior. Keep
  regression coverage when changing the implementation language or backend.

The [HTTP compression guide](compression.md) describes encoder ownership,
negotiation, bounded decoding and the benchmark. The
[encrypted record profile](encrypted-record-profile-v1.md) defines private
record envelopes and compression before encryption.

## Deployment verification

Use the existing GitHub deployment workflow and its backup, readiness and
rollback path. Builds stamp `version.zi` through the native Go string variable;
an unstamped development build reports `dev`. Confirm the actual installed
`build_version` through `/api/v1/node` and verify readiness and negotiated
responses separately from GitHub CI. A build or a pushed commit does not prove
the installed service changed.
