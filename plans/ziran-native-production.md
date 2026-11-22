# Ziran native production

Owner, 2026-11-22: "and can we rwerite more of it in ziran". The application
source is Ziran. Production still runs the zi2go output through the Go
toolchain, the CI and deploy tooling is Python and shell, and the regression
suite under `tests/_go` is Go.

## 1. Native server in production

`make test-ziran-native-server` passes locally against the native C build of
`src/server/server_native.zi`.

1. Close the gaps between the Go and native servers, each with its regression
   test. Done: `/readyz` direct purchases, chat-usage retention, the Telegram
   Stars and Google Play suites, the shared capability list, `DAOCHI_*`
   setting aliases and the four referral routes. Remaining:
   - Referral settlement. The native worker settles the outbox every 10 s
     with Go's commissions, holds, capped reversals and debt offsets, and
     native allocation has Go's referral lots, reversal eligibility and
     offsets. Remaining: a native test of the debt-offset path, the
     historical Google origin backfill (`BackfillGoogleOrigins`) and the
     Monero monitor parity noted in `referral_monero.zi:108-110`.
   - `rescale-dew` and `dew-ratio` stay on the Go binary until ported.
2. The generated C in the native build folder includes only libc and compiler
   headers. `ziran_exe_main.c` is the executable entry. CI builds and tests the
   native server, then adds that generated C to the deploy artifact. The origin
   compiles it with `cc` and links its system libraries. The Ziran toolchain
   stays off the origin.
3. The origin (Debian 13) lacks libmicrohttpd and links SQLite from the system.
   Add the libmicrohttpd runtime package and confirm the system SQLite against
   the native SQLite suite.
4. The origin installs the native binary beside the Go binary. One command
   switches `mesh.service` back to the Go binary. Keep the Go build until native
   has run in production without problems.
5. Verify the public `build_version`, `/readyz` and authenticated account and
   sync routes on the native binary.

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

`mesh.service` drop-ins set node sync tokens with `Environment=`. systemd shows
those values to unprivileged local users through `systemctl show`. Move them
into the existing `0600` `/etc/mesh/mesh.env`, with the owner's approval for
the host change.
