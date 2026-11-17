# Mesh Repository Rules

Mesh is a local-first sync and trusted-node network. Keep the protocol secure,
the public-node path convenient, and the implementation easy to audit.

## Clean Naming Rule

Use direct names inside the Mesh codebase. Do not prefix internal types and
functions with `Mesh` when package or protocol context already makes their
meaning clear. Keep `Mesh` only where it prevents wire-level ambiguity, such
as `X-Mesh-*` headers and `mesh://` URIs. Preserve the durable Daochi
signature/encryption domains and installed-client aliases documented in
`docs/mesh-migration.md`; never rename persisted cryptographic labels. Do not add new `Ksync` or `Inbe`
compatibility names; migrate maintained callers to the clean surface.

## Readability Rule

Write conventional, fully readable code. Never compress multiple statements,
branches, declarations, or error checks onto one line. Avoid dense one-line
handlers and clever control flow. Use descriptive names, focused helpers, and
explicit validation and error paths.

Run `gofmt` on changed Go files, run `git diff --check`, and inspect the final
diff before considering a change complete.

## Mesh Safety Rule

Discovery does not imply trust. New peer and replication paths must authenticate
node identity, reject replays, enforce explicit data scopes, and preserve the
zero-configuration public-node path. Add tests for unauthorized, malformed,
expired, and partition/reconnection behavior with every mesh protocol change.

## Bend

When using Bend:
- run `bend guide` to learn it
- use `LAWS.bend` to keep important rules
- run `bend PROOF.bend` before committing
- parallelize the code whenever possible
