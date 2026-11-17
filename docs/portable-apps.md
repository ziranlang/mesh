# Signed portable app delivery

Registered applications may publish portable releases in a node-owned package
directory alongside the database: `MESH_DB.packages/APP_ID/`. These are code
artifacts, separate from account records and sync collections. This does not
change app registration, registry approvals, private-data scopes or tokens.

The read-only endpoints are:

- `GET /api/v1/packages/APP_ID/latest`: signed release metadata, without caching.
- `GET /api/v1/packages/APP_ID/SHA256.zib`: immutable bundle bytes. This
  route serves only legacy `.zib` bundles for hosts released before the `.zx`
  format.

Independently versioned applications use release v2:

- `GET /api/v2/packages/APP_ID/latest`: signed metadata with compatibility
  versions, delivery variants and exact dependencies; never cached.
- `GET /api/v2/packages/APP_ID/SHA256.zx`: an immutable signed delivery variant.
- `GET /api/v2/packages/APP_ID/SHA256.wasm`: a release's signed WebAssembly
  module, served as `application/wasm`.

Portable executables are `.zx` files (header `ZX\0\0`, then the executable
revision as a little-endian u32). Hosts that run them add `format=zx` to their
`latest` and `history` requests and are offered only releases stored as
`SHA256.zx`. Hosts released before that format ask without it and are offered
only their `SHA256.zib` releases, through 2027-10-23.

A host offered a release must be able to run it. It names the ZX revision it
runs with `zx=R` and is offered only releases whose stored `.zx` header has
revision R. A host that names `host_api` but no revision was released before
hosts named it and is offered only revisions before 28, even when a newer one
is signed for its interface. `oldest_host_api` excludes releases built for an
interface older than the host still runs. Clients that name no interface, such
as publishing checks, see the newest release.

Web hosts ask with `format=wasm` (and their `zx` revision) for the WebAssembly
module Ziran lowered from a release's `.zx`. It is archived as
`release-SEQUENCE.wasm.json`: the same signed release with that module as its
artifact, beside `SHA256.wasm` and `SHA256.wasm.release-v2.json`. Its `.zx`
must be stored too, and the node checks the variant's signature and that it
signs the same release before offering it.

Every archived release and blob stays on the node. `history-v2.json` (at most
64 entries) lists the newest ten releases and the newest release of every
compatibility group: payload format (`.zx`, `.zib` or the WebAssembly
variant), ZX revision, `host_api` and `module_api`. So hosts released before a
new format, revision or interface keep their last release while newer hosts
move on.

Release v2 uses runtime `kryon-app-v1`. It signs `host_api`, `module_api` and
`data_schema`, an ordered list of `module` and optional `standalone` artifacts,
and a sorted list of exact dependency IDs, numeric versions, hashes and sizes.
Each app retains its own version and increasing sequence. Updating a child does
not change the parent release. Signature, registry status and active publisher
key checks apply to the latest descriptor and to archived artifacts alike.
These APIs serve releases; they do not register publishers or grant approval.

The optional [package delivery cache](../cdn/README.md) keeps large immutable
artifacts in a private R2 bucket and a named edge cache. Every request still
passes the node's existing access and integrity checks before cached bytes are
returned. Metadata and release approval remain on the node, and clients retain
their signed-hash verification. Missing cache entries use direct node delivery.

To stage this format, pass `--format-version 2`, `--host-api N` and
`--module-api N` to `scripts/stage-release.py`. Add `--standalone FILE`
for a separate standalone artifact and `--dependency RELEASE.json` for each
exact module dependency. The importer locks publication per app, rejects
version/sequence rollback and publishes immutable artifacts before updating
`latest-v2.json`. The node verifies the registered publisher when serving them.

Each release has `app_id`, positive monotonically increasing `sequence`,
`version`, `runtime`, lowercase `sha256`, `size`, `key_id` and hexadecimal
`signature`. The signature is Ed25519 over these UTF-8 bytes, with a trailing
newline after every field:

```text
daochi-zib-release-v1
APP_ID
SEQUENCE
VERSION
RUNTIME
SHA256
SIZE
KEY_ID
```

The current runtime contract is `kryon-desktop-v1`. Publishers must verify that
their exported application runs in the reusable Kryon player before staging
a release. A bundle containing unsupported native services is not a portable
release merely because it has a `.zx` extension.

The node verifies the publisher against the application's active registered
Ed25519 key and rejects suspended apps, expired manifests/keys, invalid
signatures and hash/size mismatches. Keep older bundle files and their
`SHA256.zib.json` metadata so a client that read an earlier release can finish
its download while a newer release is published. Revoking a key also prevents
its archived files from being served.

On the node, stage a previously verified bundle using an app-owned release key:

```sh
python3 scripts/stage-release.py --store PATH_TO_DB.packages \
  --app APP_ID --key-id KEY_ID --key PRIVATE_ED25519_PEM --format-version 2 \
  --host-api N --module-api N --sequence SEQUENCE --version VERSION APP.zx
```

The helper reads the signing key directly, never logs it, retains every
previous release and file, and publishes `latest.json` last through atomic renames and
filesystem syncs. The key must already be registered with the node. Staging
does not register an app or grant a new publishing approval. Live package
publication and node deployment are operational actions separate from these
code and fixture checks. Packages are not yet replicated through the mesh.
