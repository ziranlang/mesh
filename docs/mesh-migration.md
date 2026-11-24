# Daochi to Mesh

Mesh's canonical source is https://git.waozi.xyz/ziranlang/mesh and website is
https://mesh.ziran-lang.org. The production API remains https://api.waozi.xyz.
The package name is Mesh, executable `mesh`, and Go module `mesh`.

New clients use `X-Mesh-*`, `mesh-sync-v1`, `mesh.delegate.*`,
`mesh.session.*`, and `mesh://` namespace links. Installed `X-Daochi-*`,
`daochi-sync-v1`, `daochi.delegate.*` and `daochi.session.*` clients remain
accepted. Ksync/Inbe compatibility remains available where it was before.
Mesh headers sign the original message bytes. In particular the Mesh login
signature context remains `daochi-sync-v1`; changing a header or product name
never changes a signed message.

## Durable protocol identifiers

All existing `daochi-*-v1`/`daochi-*-v2` signing domains and record/encryption
profiles remain byte-for-byte unchanged, including `daochi-entity-v1`,
`daochi-record-v1`, `daochi-record-provenance-v1`, account/device/authorization,
node pairing/request/name claims, pack grants and signed package releases.
The `ksync-sync-v1` and `inbe-sync-v1` contexts also remain unchanged. These
strings are permanent protocol identifiers, not product branding. Persisted
Monero `daochi-` and `daochi-account-` labels and `_daochi._tcp.local` discovery
remain stable so existing wallets and LAN clients still find their identities.
The private R2 bucket remains `daochi-packages`; the Worker is `mesh-packages`.

## Configuration and deployment

`MESH_*` replaces `DAOCHI_*`. The server falls back to the old name only when
the corresponding new variable is unset. An explicitly empty Mesh variable
wins. `scripts/migrate-legacy-env.py FILE --apply --canonical-only` converts old keys without changing values, saves a private
backup, and rejects conflicting old/new values rather than switching secrets
or databases. Existing Ksync settings can also be migrated. Non-product
`MONERO_*` settings keep their names.

The default database filename remains `daochi.db` to preserve local stores
that never set a database path. Set `MESH_DB` explicitly for new installations.
The node identity still defaults to the same `<database>.node-key` file.
Never rename a database, key, blob directory, encrypted payload, signed field,
app identifier or wallet label as part of this product rename.

`deploy/install.sh` verifies build provenance before touching services. For
an existing Daochi installation it prepares `/opt/mesh`, `/etc/mesh/mesh.env`
and `mesh.service`, retaining the original executable, configuration and unit.
It preserves file ownership and permissions, all configured paths, and the
original service working directory. Existing `/var/lib/daochi` state remains
at its physical location; `/var/lib/mesh/backups` holds new upgrade snapshots.
It stops the former service before taking a SQLite backup, resolves the original
database path, and starts Mesh only after configuration migration succeeds.
There is one running server. A failed upgrade restores its database snapshot,
binary, environment and credential override; an initial rename failure also
restores the former service's enabled/running state. The old installation is
retained for rollback. Never run old and new services against the same database.
Existing backup maintenance is renamed only if already enabled.
Symlinked installation roots or a database inside the configuration directory
are refused before services stop. For those layouts, provision independent Mesh
configuration and a unit that keeps the original state/key paths, then stop the
old service before upgrading. No database directory is copied into `/opt/mesh`.

A fresh host must provision `/etc/mesh/mesh.env`, the stable signing/issuer/node
keys and `mesh.service` before using the upgrade installer. It must not create
new credentials to replace an existing installation.

Docker Compose keeps the original `daochi_daochi-data` volume name by default
and `/data/daochi.db` within it. If the previous Compose project used a custom
name, set `MESH_DATA_VOLUME` to that exact existing volume before starting Mesh.
The volume is external: a misspelled or absent volume fails instead of silently
starting with empty state. Stop the old container first; never run two writers
on one volume. For a genuinely fresh installation, explicitly create a volume
with `docker volume create mesh-data` and set `MESH_DATA_VOLUME=mesh-data`.

Gitea Actions builds and verifies production deployments from `ziranlang/mesh`
`master` on Build's native `build-native` host runner; there is no Docker image
job. Enable `MESH_DEPLOY_READY` after the origin's root-owned
`deploy/gitea-origin.py` is installed as the new SSH key's forced command.
The key may request deployment of an artifact from the active canonical Gitea
run. The origin checks required job conclusions and the revision independently,
verifies the native build inputs and resulting binary, and checks local and
public readiness after install. The deploy job supplies a short-lived Gitea
job token with Actions and contents read access. It uses the pinned origin host
key and leaves the existing node credentials in place.

GitHub no longer deploys the API. Pages uses project
`mesh` and provisions `mesh.ziran-lang.org` with `CLOUDFLARE_DNS_API_TOKEN`
(falling back to `CLOUDFLARE_API_TOKEN`). This provisioning token needs Pages
Edit, Zone Read and DNS Edit for ziran-lang.org. Site uploads continue using
the separate `CLOUDFLARE_API_TOKEN` deployment credential.
A conflicting DNS record is retained and reported as an error. The former
Pages project/domain is retained until the new site is verified live.
The site sends `Cache-Control: no-transform` so the CDN serves the published
bytes without injecting scripts. Deployment verification hashes every public
page and the branding assets against the working tree over canonical HTTPS.
