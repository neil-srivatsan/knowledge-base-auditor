# Persistence Boundary Inventory

_Last updated: 2026-07-12 — PostgresStorage is fully implemented and factory-wired (Step 10); `create_storage()` returns `PostgresStorage` for `postgres://` and `postgresql://` URLs._

---

## 1. Refactor Status

The persistence-boundary refactor is complete.  The project is now structured
so that a second storage backend (Postgres, MongoDB, or another future engine)
can be added by:

1. Implementing the `AuditStorage` protocol defined in
   `src/kb_audit/storage/contracts.py`.
2. Registering it in `src/kb_audit/storage/factory.py`.
3. Running `tests/test_storage_conformance.py` against the new backend.

`PostgresStorage` is now implemented and factory-wired (Step 10). SQLite
remains the default backend for bare-path and `sqlite://` inputs; PostgreSQL is
available as an explicit opt-in backend for `postgres://` and `postgresql://`
URLs.

`create_storage()` routes by URL scheme:

- `postgres://` and `postgresql://` return a `PostgresStorage` instance without
  connecting (lazy import keeps psycopg optional for SQLite-only users).
- `mysql://`, `mongodb://`, and any other unrecognised `scheme://` raise
  `ValueError` with a generic "unsupported" message.

The psycopg v3 driver (`psycopg[binary]>=3.1`) has been added as an optional
project dependency under the `postgres` extras group.  It is not installed in
the default environment and is not imported by any runtime path.
`src/kb_audit/storage/postgres_support.py` provides two probe helpers —
`psycopg_available()` and `require_psycopg()` — that are called only by the
factory-wired, opt-in `PostgresStorage` implementation without polluting unrelated modules.

`src/kb_audit/storage/schema_postgres.py` contains the full PostgreSQL DDL —
five `CREATE TABLE` statements, eight `CREATE INDEX` statements, and the
singleton `scan_state` seed row.  The module itself does not import psycopg and
does not open a connection; it is used by `PostgresStorage.connect()` (which
iterates `iter_postgres_schema_statements()` to apply the DDL) and by the
initial Alembic migration (`migrations/versions/0001_initial_schema.py`), which
delegates to the same function.  It remains backend-specific and does not affect
SQLite or default application startup.  Alembic is a manual PostgreSQL path and
is not invoked automatically by `create_storage()`, `connect()`, or application
startup.

`src/kb_audit/storage/postgres.py` contains `PostgresStorage`.  It implements
the full `AuditStorage` surface:

- **Connection lifecycle**: `connect()`, `close()`, `conn` property,
  `is_connected` property
- **Lease methods**: `try_start_scan()`, `renew_lease()`, `owns_live_lease()`,
  `end_scan()`, `reset_scan_state()`, `get_scan_state()`
- **Scan lifecycle**: `start_scan()`, `finish_scan()`, `fail_scan()`
- **Document/result persistence**: `store_document()`, `store_result()`,
  `get_previous_hashes()`, `carry_forward_results()`, `load_audit_results()`,
  `get_scan_results()`
- **Workflow persistence**: `complete_scan_with_findings()`, `sync_findings()`,
  `update_workflow()`, `get_findings()`, `get_finding()`, `get_workflow_summary()`
- **History and maintenance**: `get_scan_history()`, `get_scan_diff()`,
  `clear_all()`, `clear_all_if_idle()`, `prune_scans()`

`connect()` calls `require_psycopg()`, imports psycopg only inside the method,
opens the connection, applies every statement from
`iter_postgres_schema_statements()` in order, commits, and rolls back on error.
`try_start_scan()` uses `SELECT … FOR UPDATE` on the singleton `scan_state` row
(id=1) to prevent concurrent lease acquisition.  `start_scan()` uses
`INSERT … RETURNING id` to obtain the new scan ID.

`PostgresStorage` does **not** inherit from `AuditStorage` but satisfies the
runtime-checkable `AuditStorage` protocol in focused tests.  It is registered
in `create_storage()` as of Step 10.  Alembic migrations remain manual and are
not invoked automatically by `connect()` or application startup.  Production
readiness is not yet claimed.

---

## 2. Current Module Structure

```
src/kb_audit/
├── db.py                        # Thin backward-compat facade: Database(SqliteStorage)
└── storage/
    ├── __init__.py              # Re-exports: create_storage, AuditStorage, SqliteStorage,
    │                            #   initialize_schema, all serialization helpers
    ├── contracts.py             # @runtime_checkable Protocol classes
    │   ├── ConnectionLifecycle  # connect() / close()
    │   ├── ScanLeaseStore       # try_start_scan / renew_lease / owns_live_lease /
    │   │                        #   end_scan / reset_scan_state / get_scan_state
    │   ├── ScanStore            # start_scan / finish_scan / fail_scan /
    │   │                        #   complete_scan_with_findings / get_scan_history /
    │   │                        #   get_scan_diff / get_scan_results
    │   ├── DocumentResultStore  # store_document / store_result / get_previous_hashes /
    │   │                        #   carry_forward_results / load_audit_results
    │   ├── WorkflowStore        # update_workflow / get_findings / get_finding /
    │   │                        #   get_workflow_summary
    │   ├── MaintenanceStore     # clear_all_if_idle / prune_scans
    │   └── AuditStorage         # Combined protocol (all six above)
    ├── factory.py               # create_storage(url | Path) -> AuditStorage
    ├── postgres.py              # PostgresStorage — full AuditStorage implementation; factory-wired
    ├── postgres_support.py      # psycopg_available() / require_psycopg() probe (psycopg optional)
    ├── schema.py                # initialize_schema(conn) — SQLite DDL + migration runner
    ├── schema_postgres.py       # PostgreSQL DDL design artifact — no connections, no psycopg import
    ├── serialization.py         # JSON encode/decode helpers for domain objects
    └── sqlite.py                # SqliteStorage — full SQLite implementation
```

### Key design decisions

- `Database` in `db.py` inherits `SqliteStorage` and adds nothing.  It exists
  solely for backward compatibility with import sites that predate the refactor.
  New code should use `create_storage()`.

- `AuditStorage` is a `@runtime_checkable Protocol`.  `SqliteStorage` does not
  explicitly inherit it; structural compatibility is verified by
  `isinstance(store, AuditStorage)` in the conformance suite.

- `create_storage()` never calls `.connect()`.  Callers own the connection
  lifecycle (connect → use → close).

- `AuditResult.evidence_hash` and `build_finding_key()` remain in `models.py`.
  They are domain-identity contracts that storage _consumes_ but does not own.
  Moving them would risk reopening or orphaning historical workflow findings
  across live databases.

---

## 3. Backend Readiness Contract

Any future backend must satisfy every assertion in
`tests/test_storage_conformance.py`.  The contract covers:

### Connection lifecycle
- `connect()` initialises the backing store (creates tables, runs migrations).
- `close()` releases all resources cleanly; subsequent calls are safe.
- Data committed before `close()` survives a `close()` / `connect()` cycle.

### Scan lease semantics
- `try_start_scan()` returns a non-empty owner token, or `None` if a live
  lease is already held.
- Only one live lease can exist at a time.
- `renew_lease(owner_token)` extends the lease expiry; returns `False` when
  the token does not match or the lease has expired.
- `end_scan(owner_token, …)` releases the lease and returns `True`; returns
  `False` for an unrecognised token.

### Scan state
- `get_scan_state()` returns `{in_progress: bool, last_scan_id: int | None, …}`.
- `in_progress` is `False` before any scan and after `end_scan`.
- `in_progress` is `True` between `try_start_scan` and `end_scan`.
- Expired leases are treated as idle (in_progress=False).

### Atomic scan completion
- `complete_scan_with_findings()` atomically persists findings and marks the
  scan completed in a single transaction.  A partial failure must not leave the
  scan in a running state with no committed findings.

### History and status
- `get_scan_history(limit)` returns completed scans newest-first.
- Each entry includes `scan_id`, `document_count`, `stale_count`,
  `needs_review_count`, `unknown_count`.

### Document and result persistence shape
- `get_scan_results(scan_id)` returns a list of dicts with at minimum:
  `id`, `title`, `source_type`, `url`, `overall_status`, `confidence`,
  `signals` (list of dicts), `trust_metadata` (dict), `trust_evidence` (dict).

### Trust metadata and evidence persistence
- `trust_metadata` and `trust_evidence` dicts round-trip through `store_result`
  / `get_scan_results` without data loss.

### Carry-forward
- `carry_forward_results(scan_id, doc_ids)` copies audit results from the most
  recent completed scan into the current scan for unchanged documents.
- `load_audit_results(scan_id, doc_ids)` reconstructs full `AuditResult` objects
  including signals, trust_metadata, and trust_evidence.

### Finding key stability
- A finding created for `(source_type, document_id, status)` is retrievable by
  the same `finding_key` across scans.

### Evidence hash reopening
- A finding in a terminal state (`dismissed`, `fixed`, `accepted_risk`) is
  reopened to `open` when `complete_scan_with_findings` is called with a result
  whose `evidence_hash` differs from the stored value.

### Workflow state transitions
- `update_workflow(key, state=…, note=…, …)` returns `True` on success,
  `False` when the key does not exist.
- `get_findings(include_all=True)` returns all findings regardless of state.
- `get_finding(key)` returns a single finding dict or `None`.
- `get_workflow_summary(include_all=True)` returns `{state: count}` aggregates.

### Snooze and terminal filtering
- By default `get_findings()` excludes terminal states and future-snoozed
  findings.  `include_all=True` overrides this.

### Clear / retention
- `clear_all_if_idle()` returns `True` and removes all data when no live lease
  is held.  Returns `False` (no change) when a live lease is active.

### Error sanitisation
- Errors passed to `fail_scan` and `end_scan` must be sanitised before
  persistence (credentials, bearer tokens, URL passwords stripped).

---

## 4. Relational Backend Notes (Postgres)

The following translation table records the design choices made when implementing
`PostgresStorage`. It may also serve as a reference for adding further relational
backends in the future.

| Concern | SQLite approach | Postgres equivalent |
|---------|-----------------|---------------------|
| Exclusive write lock | `BEGIN IMMEDIATE` | `BEGIN` with `SELECT … FOR UPDATE` |
| Upsert | `INSERT OR REPLACE INTO` | `INSERT … ON CONFLICT DO UPDATE` |
| Auto-increment reset | `DELETE FROM sqlite_sequence` | `ALTER SEQUENCE … RESTART` |
| JSON storage | Single JSON column (`TEXT`) | `JSONB` column |
| ISO timestamp comparison | String `>` comparison | Native `TIMESTAMPTZ` |
| WAL multi-process safety | `PRAGMA journal_mode=WAL` | Default Postgres MVCC |

Tables map 1:1: `scans`, `documents`, `audit_results`, `finding_workflow`,
`scan_state`.  The PostgreSQL DDL lives in `storage/schema_postgres.py` and is
shared between `PostgresStorage.connect()` and the Alembic initial migration
(`migrations/versions/0001_initial_schema.py`).  Alembic migrations are applied
manually with `alembic upgrade head`; they are not invoked by `connect()` or
normal application startup.

All serialisation helpers in `storage/serialization.py` are backend-neutral and
can be reused as-is.

---

## 5. Document-Store Backend Notes (MongoDB)

When adding a MongoDB backend:

| SQL concept | MongoDB equivalent |
|-------------|-------------------|
| `scans` table | `scans` collection |
| `documents` table | `documents` collection |
| `audit_results` table | `audit_results` collection |
| `finding_workflow` table | `findings` collection |
| `scan_state` table | `scan_state` collection (single doc) |
| `BEGIN IMMEDIATE` lease | Atomic `findOneAndUpdate` with `$setOnInsert` |
| INSERT OR REPLACE | `replaceOne(…, upsert=True)` |
| signals JSON column | Embedded array field |
| trust_data JSON column | Embedded document field |

Atomicity requirement: `complete_scan_with_findings` must use a MongoDB
multi-document transaction (requires replica set).  Single-document atomicity is
not sufficient for the scan + findings update.

The `AuditStorage` protocol and conformance suite are the same regardless of
backing store.  The document-store backend supplies a different fixture; the
22 conformance tests run unchanged.

---

## 6. Domain Identity Contracts — Do Not Move to Storage

`AuditResult.evidence_hash` and `build_finding_key()` live in `models.py` and
must remain there unless a deliberate domain-model refactor moves them.

- **`AuditResult.evidence_hash`** — deterministic fingerprint of the material
  evidence behind an audit result.  Storage reads it to detect whether terminal
  workflow findings should be reopened.  Changing the algorithm, field inclusion,
  or sort order will reopen or orphan historical findings in every live database.

- **`build_finding_key()`** — stable primary key for a workflow finding derived
  from `(source_type, document_id, status)`.  Changing it orphans all existing
  finding rows.

These are domain contracts that storage *consumes*; they are not serialisation
helpers that storage *owns*.
