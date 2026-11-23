# Ziran native production

Owner, 2026-11-22: "and can we rwerite more of it in ziran". The application
source is Ziran. Production still runs the zi2go output through the Go
toolchain, the CI and deploy tooling is Python and shell, and the regression
suite under `tests/_go` is Go.

## 1. Native server in production

The native server builds from the locked toolchain and packages: Ziran
`c450e3a3` (rooted child file handles, multi-member gzip), compression
`c42011e` (zstd streaming encoder) and SQLite `b15b524` (busy and progress
handlers). CI runs the native server suite and the deploy job ships the
generated C.

The origin has `libmicrohttpd12t64`, the native-capable gateway and a built
`/opt/mesh/mesh-native` beside Go.

Native ran in production on 2026-11-23 from 05:00:31 to 05:02:25 UTC and was
rolled back to Go: installed apps send `X-Daochi-User`, which Go accepts for
`X-Mesh-User` and native did not. `transport.Header` now accepts the
`X-Daochi-` alias for every `X-Mesh-` header, with a native check.

Every legacy header and signature context the Go sources name is also named
in native code, and the native checks send Daochi, Ksync and Inbe user
headers. Names alone do not prove each route behaves the same, so before
setting `deploy/runtime` back to `native`, get evidence from real traffic,
for example by mirroring live requests to a native instance on a database
copy and comparing statuses.

Smaller differences seen live: unauthenticated `GET /api/v1/account` is 405 on
native and 404 on Go, and native `/metrics` has 22 metric names where Go has 41.

Open gaps between the Go and native servers, each needing its regression test:

- Referral settlement: the historical Google origin backfill
  (`BackfillGoogleOrigins`) and the Monero monitor parity noted in
  `referral_monero.zi:108-110`.
- `rescale-dew` and `dew-ratio` stay on the Go binary until ported.

Rollback to Go: remove `/etc/systemd/system/mesh.service.d/30-runtime.conf`,
run `systemctl daemon-reload` and `systemctl restart mesh.service`.

## 2. Tooling in Ziran

Port `scripts/` (73 Python files plus shell) into small Ziran tools with Ziran
tests. Retire duplicate checks so the check count goes down.

## 3. Tests in Ziran

Port `tests/_go` (198 files) into table-driven Ziran tests. Drop cases the
native suites already cover.

## 4. Remove the Go host

Remove `embedded_host.go`, `native/*.go` and the Go-only oracles once nothing
uses them. Remove the Go build path after the native server has run in
production without problems.

## Operations finding

The `20-daochi-mesh.conf` and `20-mesh-mesh.conf` drop-ins set an older node
sync token with `Environment=`, which `systemctl show` reveals to local users.
`/etc/mesh/mesh.env` overrides it with the token the server uses, so the
drop-ins have no effect. Back them up and remove them in the native deploy
window.
