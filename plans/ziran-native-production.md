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
in native code, and native peer pulls send the `X-Daochi-Node-` headers too.

Traffic comparison on 2026-11-23 (07:10-07:40 UTC): nginx mirrored 2,168 live
requests to native `60602a0` on a database copy (loopback only, no outbound
network, wallet on a closed port), with sign-in state refreshed from live.
2,069 matched Go's status, including 127 Android syncs. The other 99 were 56
logins, which the copy cannot complete because the challenge lives in the
live database, and 43 requests on the three routes apps call right after a
login, using a token the copy had not yet seen. No other route differed.
`check_legacy_headers.zi` proves the native login itself: an installed app's
X-Daochi-User and X-Daochi-Signature login over `daochi-sync-v1`, then a sync
with its token.

Native goes live through `deploy/runtime=native`. A watchdog compares live
statuses with the three hours of Go traffic before the switch and rolls back
to Go when a route Go served answers an error status Go never gave it.


Rollback to Go: remove `/etc/systemd/system/mesh.service.d/30-runtime.conf`,
run `systemctl daemon-reload` and `systemctl restart mesh.service`.

## 2. Tooling in Ziran

Port the remaining Python under `scripts/` and `tests/native/` (27 native
checkers, most driven by `check_http_listener.py`) into small Ziran tools and
checks on `tests/native/lib/native_check.zi`. Retire duplicate checks so the
check count goes down. The origin-side scripts (`verify_deployment_inputs.py`,
`migrate-legacy-env.py`, `deploy/gitea-origin.py`) wait until the origin
builds Ziran tools from shipped C, as it builds the native server.

## 3. Tests in Ziran

Port `tests/_go` (198 files) into table-driven Ziran tests. Drop cases the
native suites already cover.

## 4. Remove the Go host

Port `rescale-dew` and `dew-ratio` to the native binary first: they run only
on the Go binary, which the deploy keeps installed for them
(`deploy/apply-dew-ratio.sh`). Remove `embedded_host.go`, `native/*.go` and
the Go-only oracles once nothing uses them. Remove the Go build path after the native server has run in
production without problems.

## Service hardening

After native runs in production without problems, run `mesh.service` as a
dedicated non-root `mesh` user that owns the data directory, with
`NoNewPrivileges`, `ProtectSystem=strict` plus `ReadWritePaths` and
`PrivateTmp`, then verify readiness and sync.
