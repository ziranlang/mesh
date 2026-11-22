# Ziran native production

Owner, 2026-11-22: "and can we rwerite more of it in ziran". The application
source is Ziran. Production still runs the zi2go output through the Go
toolchain, the CI and deploy tooling is Python and shell, and the regression
suite under `tests/_go` is Go.

## 1. Native server in production

`make test-ziran-native-server` passes locally against the native C build of
`src/server/server_native.zi`.

Blocked on unpushed dependencies. The native server added in Mesh `82940cd`
compiles only with uncommitted work in other checkouts, so it does not build
from the locked toolchain and packages:

- ziranlang/ziran `std/file_tree_linux.zi`: `OpenDirectoryHandle`,
  `OpenChildRegularFile`, `OpenChildDirectory`, `OpenOrCreateChildDirectory`,
  `OpenNewChildFile`, `RemoveChildFile`, `RenameChild` (edited by an active
  Ziran lane on 2026-11-22).
- ziranlang/ziran `std/gzip_linux.zi`: `GzipDecompressMembers` (uncommitted
  since 2026-11-20).
- ziranlang/compression `src/zstd_native.zi`: the `ZstdEncoder` pool
  (uncommitted since 2026-11-20).
- ziranlang/sqlite `src/SQLite.zi` (uncommitted since 2026-11-20).

When their owners push them, bump the `ziran.toml` pins, run `ziran lock`
without `ziran.local.toml`, run the Go and native suites, and restore the CI
steps: the native server suite in `checks` (`make sqlite-native
microhttpd-native`, then `make test-ziran-native-server` with those
directories) and `make native-release` plus packaging `build/native-release`
`*.c`/`*.h` in `deploy`.

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
   headers. `ziran_exe_main.c` is the executable entry. CI already runs the
   native suite against `make sqlite-native` (SQLite 3.53.4 from the locked
   source) and `make microhttpd-native` (Debian 1.0.1-4, pinned by sha256).
   Add `native-release`: copy the generated `*.c`/`*.h` and the SQLite
   amalgamation into `build/native-release`, which the deploy job adds to the
   artifact. Add `build-native` for the origin: per-file `cc -O3 -Wno-psabi
   -pthread` objects plus static SQLite and liboqs, linked with
   `-l:libmicrohttpd.so.12 -l:libcrypto.so.3 -l:libcurl.so.4 -l:libzstd.so.1
   -l:libz.so.1 -l:libpcre2-8.so.0`. The Ziran toolchain stays off the origin.
3. The origin (Debian 13) needs the `libmicrohttpd12t64` 1.0.1-4 runtime
   package installed. That is the only new host package.
4. `install.sh` keeps the Go binary at `/opt/mesh/mesh` (its `dew-ratio` and
   `rescale-dew` commands and `apply-dew-ratio.sh` use it) and installs
   `/opt/mesh/mesh-native` with its manifest. The drop-in
   `mesh.service.d/30-runtime.conf` (`ExecStart=` then
   `ExecStart=/opt/mesh/mesh-native`) selects native. `rollback.sh` restores
   the previous drop-in state. Manual rollback to Go: remove that drop-in,
   `systemctl daemon-reload`, `systemctl restart mesh.service`. The origin
   gateway (`deploy/gitea-origin.py`, installed at
   `/usr/local/libexec/mesh-gitea-deploy`) also runs `make build-native` and
   verifies its manifest.
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
