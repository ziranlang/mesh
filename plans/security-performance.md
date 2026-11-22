# Mesh security and performance plan

Keep legacy data readable through the planning horizon of 2027-11-22. Reassess compatibility with deployed clients and restored backups before changing that policy. This plan schedules no data migration or deletion. The existing verifier cutoff for legacy Daochi request signatures remains in effect. Preserve the existing database format, accounts, keys and durable cryptographic domains.

## Replay reservation and delegated request cleanup

1. Enforce peer request expiry at nonce reservation in `src/node/node_nonce.zi` and `src/node/node_auth_native.zi`. Recheck after acquiring SQLite's write transaction. Preserve atomic cleanup and replay reservation, and keep the existing signed request fields and expiry boundary.
2. Add an expiry index for `server_authorization_nonces` in `src/storage/schema_sql.zi`. Both backends create it during schema initialization. Keep request nonce uniqueness, transaction boundaries and existing rows intact.
3. Run the Go node authentication, nonce and storage schema tests plus the native server suite. Exercise existing signed peer replay, expiry, partition and reconnection tests. Verify that upgrading a store preserves replay claims and that SQLite uses the expiry index. Measure cleanup with live nonce populations and report latency and limits separately from encrypted sync loopback measurements.

The nonce deadline and additive index are implemented. The full Go application and the native SQLite and server suites passed. The saved-IR application suite passed with a local overlay adjustment for duplicate generated filenames from the Shovey override. The SQLite benchmark includes deleting one expired row and inserting its replacement in a WAL transaction. With 32,768 active nonce rows, median time across three trials fell from 1.402 ms to 0.033 ms. Native delegated sync returned eight synthetic opaque 4 KiB records per request to four loopback workers. Median p95 across three local trials fell from 6.192 ms to 3.044 ms. Production traffic and nonce-table size remain unmeasured. The rest of the database and wire format are compatible with existing clients and servers.

## Persistent identity creation

Both backends stage a private file in the identity directory before publishing it with a hard link that cannot replace the destination. A concurrent starter reads and validates the file already published. The existing seed and expanded key encodings remain supported. First startup requires hard-link support in the identity directory's filesystem. An explicitly configured identity bypasses this file-creation step.

## Follow-on work

1. Use independent random content roots for a versioned client format. Define key IDs, authenticated recipient key wraps, restore behavior and old-client handling before writing the new format. Keep existing ciphertext readable under its original derivation. Coordinate client changes with the package owner first.
2. Specify owner or authorized delegate provenance for opt-in replica collections. Bind the record, operation, predecessor and ciphertext digest, then persist validation and replay state with the record. Keep explicitly trusted legacy collections compatible. Verify tombstones, expiry, unauthorized and malformed imports, duplicates, conflicting writes, offline catch-up and rollback before promotion.
3. Profile the native encrypted sync paths against deterministic high-entropy fixtures and release-sized databases. Attribute SQLite lock time, database checkpoint stalls, encoding and compression before optimizing them. Keep durability and authorization unchanged across measurements. Coordinate any SQLite engine or package change separately.
