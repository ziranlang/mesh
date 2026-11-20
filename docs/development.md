# Development

Mesh's server is written in Ziran. The build generates `.zi.go` and
`.zi_test.go` files in the root Go package. These are ignored build outputs.

| Path | Contents |
| --- | --- |
| `src/server` | Entry point, startup, configuration, routing and operations |
| `src/http` | HTTP middleware, body limits, compression and websockets |
| `src/node` | Peer discovery, trust, replication and the embedded node |
| `src/sync` | Client sync, cursors, history and record transfer |
| `src/storage` | SQLite, schema, retention and storage accounting |
| `src/protocol` | Wire types, signing, encryption and serialization |
| `src/accounts`, `src/apps`, `src/chat`, `src/payments`, `src/token` | Application features |
| `tests/ziran` | Ziran tests run by the Go backend |
| `tests/_go` | Go regression tests and independent behavior baselines |
| `testdata` | Shared protocol fixtures and standalone reference programs |
| `native`, `embedded_host.go` | Go callback adapter and C entry points |
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

`make test-ziran` runs the application suite against generated Go from source,
then saves checked `.zir`, generates Go from that IR and runs the suite again.
The test runner adds `tests/_go` to the server package with a Go overlay.
Independent fixtures cover native types, signatures, SQL behavior, protocol
bytes, cancellation, replay prevention and resource cleanup.

```sh
make test-ziran GOFLAGS='-mod=mod -race -count=1'
make test TEST_FLAGS='-tags=mesh_embedded -run TestEmbeddedNode'
```

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
