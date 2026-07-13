# Knowledge Base Auditor

## Technical Design Document

**Document status:** Current implementation  
**Product stage:** Local-first prototype  
**Last updated:** 2026-07-10

## 1. Purpose

This document describes the implemented technical design of Knowledge Base Auditor. It covers the
runtime architecture, component responsibilities, data flow, persistence model, APIs, concurrency
controls, security boundaries, testing strategy, and known technical constraints.

The related product documents are:

- `docs/Product Brief.md`: original problem statement and product intent
- `docs/PRD.md`: product requirements and roadmap
- `README.md`: installation, usage, and contributor-facing overview

This is a description of the current system. Future architecture is identified separately and is
not presented as already implemented.

## 2. System Context

Knowledge Base Auditor retrieves documentation pages from a supported source, normalizes them into
a source-independent model, analyzes evidence across the pages in a scan, assigns a trust
classification, persists the results, and exposes them through a CLI and local web application.

```text
Notion API ---------\
Confluence API ------> Document Sources
Built-in Demo ------/        |
                             v
                     Normalized Documents
                             |
                             v
                       Evidence Analyzers
                             |
                             v
                       Trust Classifier
                             |
                             v
                       Scan Orchestrator
                       /       |       \
                      v        v        v
               Reporters    SQLite    Workflow
                  |            |          |
                  v            v          v
                 CLI       FastAPI API   Web UI
```

The current deployment model is a single local Python process with a local SQLite database.
The web application serves a single HTML document containing the user interface, CSS, and
JavaScript.

## 3. Design Principles

### 3.1 Deterministic And Explainable

Classification is based on explicit rules and evidence. Each result includes:

- classification
- confidence
- human-readable reason
- positive evidence
- review risks
- missing evidence
- recommended action

The system does not use an external language model to classify pages.

### 3.2 Conservative Trust

A page is not considered current merely because no stale signal was found. Current status requires
positive trust evidence and no active review risk.

### 3.3 Source Independence

Notion, Confluence Cloud, and demo pages are converted to the same `Document` model before
analysis. Analyzers and classification logic do not depend on a specific source client.

### 3.4 Scan-Local Relationships

Reference resolution, version comparison, duplicate detection, and replacement selection operate
on the pages included in the scan. The analyzer layer does not independently search an entire
workspace for matching titles.

The source layer determines which pages enter the scan. Notion title and URL search modes may
retrieve related accessible title variants before analysis.

### 3.5 Local-First Operation

The prototype stores data locally, binds the web server to `127.0.0.1` by default, and can be
evaluated without credentials using demo mode.

## 4. Technology Stack

| Area | Technology |
|---|---|
| Language | Python 3.11+ |
| CLI | Click |
| HTTP clients | HTTPX |
| Web API | FastAPI |
| Web server | Uvicorn |
| Frontend | Server-delivered HTML, CSS, and vanilla JavaScript |
| Persistence | SQLite with WAL mode; factory-wired PostgreSQL backend with opt-in readiness and live conformance harnesses |
| Configuration | YAML, environment variables, and `.env` |
| Similarity | RapidFuzz |
| Console output | Rich |
| Testing | Pytest, RESPX, FastAPI TestClient, Playwright |
| Static analysis | Ruff and mypy |
| Packaging | Hatchling |

## 5. Repository Structure

```text
src/kb_audit/
  analyzers/          Evidence analyzers
  reporters/          Console and JSON output
  sources/            Notion, Confluence, and demo sources
  storage/            Persistence factory, contracts, SQLite backend, PostgreSQL backend, readiness checks, schemas, and serialization helpers
  web/
    app.py            FastAPI application and web runtime
    templates/
      index.html      Browser UI
  auditor.py          Scan orchestration
  cli.py              CLI entry point
  config.py           Configuration loading
  db.py               Backward-compatible persistence facade exposing Database
  models.py           Shared domain models
  titles.py           Shared title normalization
  trust.py            Trust classification

tests/
  analyzers/          Analyzer unit tests
  reporters/          Reporter tests
  sources/            Source adapter tests
  test_browser.py     Playwright end-to-end UI tests
  test_demo_pipeline.py
  golden-scenario tests
  test_trust.py
  test_storage_serialization.py
  test_web.py
  test_workflow.py
  ...                 Additional unit and integration tests
```

## 6. Core Domain Model

### 6.1 Document

`Document` is the normalized representation shared by all sources.

| Field | Type | Description |
|---|---|---|
| `id` | string | Source-provided stable page identifier |
| `title` | string | Human-readable page title |
| `content` | string | Flattened plain-text page content |
| `source_type` | string | `notion`, `confluence`, or `demo` |
| `url` | optional string | Source page URL |
| `last_modified` | optional datetime | Source modification timestamp |
| `metadata` | dictionary | Source-specific context and extracted links |
| `links` | list of `DocumentLink` | Structured source links resolved by the internal-link analyzer |
| `content_hash` | string | SHA-256 hash calculated from content |

The content hash is used for incremental scan behavior. It does not currently include title,
source metadata, URL, or modification time.

### 6.2 DocumentLink

`DocumentLink` captures source-aware links extracted by adapters before content is flattened.

| Field | Type | Description |
|---|---|---|
| `url` | optional string | Link URL when the source provides one |
| `target_id` | optional string | Source-specific target document ID when available |
| `target_title` | optional string | Target title or page mention text when available |
| `text` | optional string | Anchor or rich-text label |
| `context` | optional string | Nearby source text used to interpret the relationship |
| `source` | string | Source adapter that produced the link |

`url` is optional because some source links, mentions, or structured references can be resolved by
ID or title without a URL.

### 6.3 StalenessSignal

An analyzer produces zero or more `StalenessSignal` objects for a page.

| Field | Description |
|---|---|
| `signal_type` | Machine-readable evidence type |
| `severity` | `info`, `warning`, or `critical` |
| `message` | Human-readable signal description |
| `details` | Structured analyzer-specific data |

Examples include:

- `age`
- `duplicate`
- `near_duplicate`
- `version_marker`
- `version_ref`
- `broken_link`
- `resolved_reference`
- `unresolved_reference`
- `ambiguous_reference`
- `resolved_internal_link`
- `broken_internal_link`
- `ambiguous_internal_link`
- `replacement_link`
- `backlink_from_successor`

### 6.4 AuditResult

`AuditResult` combines a document, analyzer signals, and the trust verdict.

Important fields include:

- `status`
- `confidence`
- `confidence_reason`
- `trust_metadata`
- `trust_evidence`
- `suggested_replacement`

`trust_metadata` stores parsed document metadata and derived heuristic metadata, including lifecycle,
applicability scope, human-audit actionability, audit priority, importance score, and actionability
reason.

### 6.5 Finding Identity

Actionable results use a deterministic finding key:

```text
SHA-256(source_type + ":" + document_id + ":" + status)[0:24]
```

The key remains stable while source, document identity, and classification remain unchanged.
A classification change produces a different key.

### 6.6 Evidence Identity

Each result also has an evidence hash derived from:

- status
- sorted signal types, severities, messages, and stable details
- evidence summary
- positive evidence
- review risks
- missing evidence
- recommended action

The workflow uses this hash to detect materially changed evidence and determine whether a
previously resolved or dismissed finding should reopen.

## 7. Source Adapter Design

All sources implement:

```python
class DocumentSource(ABC):
    def fetch_documents(self) -> Iterator[Document]: ...
    @classmethod
    def source_type(cls) -> str: ...
```

The source iterator allows pages to be consumed incrementally, although the orchestrator collects
changed documents in memory before cross-document analysis.

### 7.1 NotionSource

Authentication uses a Notion integration token.

Supported modes:

- all pages accessible to the integration
- recursive page tree
- database entries
- title search
- Notion page URL search

Notion content is retrieved from block children and flattened to text. Nested blocks are traversed
recursively to a configured internal maximum depth. Rich-text links, page mentions, bookmarks,
embeds, and link previews are also converted to `DocumentLink` objects when enough source data is
available. Extracted page metadata includes:

- created time and creator
- last editor
- parent information
- archived state
- extracted URL strings in `metadata["links"]`
- structured links in `Document.links`

Title and URL search behavior can retrieve related title variants so pages such as v1, v2, and v3
can be analyzed together. Page-tree and database modes remain bound to their selected targets.

### 7.2 ConfluenceSource

Authentication uses HTTP Basic authentication with Atlassian account email and API token.

Supported modes:

- space
- recursive page tree
- CQL query

The adapter uses the Confluence Cloud REST API, retrieves storage-format HTML, and converts it to
plain text with an `HTMLParser`-based extractor. Anchor tags are also converted to structured
`DocumentLink` objects with URL, anchor text, and containing-block context. It retains:

- source URL
- modification time
- creator and last editor
- creation date
- version number
- page status
- space key
- ancestor titles
- extracted URL strings in `metadata["links"]`
- structured links in `Document.links`

Modification dates, URL links, and structured links feed current analyzers. Most remaining
Confluence metadata is stored as context but is not currently used by the trust classifier.

Confluence API calls paginate in groups of 25 and retry HTTP 429 responses using `Retry-After`.

### 7.3 DemoSource

`DemoSource` creates ten deterministic page scenarios using dates relative to the current time.
It requires no credentials, files, or network access.

The page set exercises:

- current guidance
- explicit legacy content
- version succession
- replacement recommendations
- overdue review cadence
- unresolved references
- near-duplicate content
- missing trust evidence

The expected result distribution is:

```text
3 current
3 stale
3 needs review
1 unknown
```

## 8. Analyzer Design

All analyzers implement:

```python
class Analyzer(ABC):
    def analyze(
        self, documents: list[Document]
    ) -> dict[str, list[StalenessSignal]]: ...
```

Analyzers return signals grouped by document ID. They do not directly assign trust status.

### 8.1 TimestampAnalyzer

Inputs:

- `Document.last_modified`
- configurable warning and critical day thresholds

Behavior:

- missing modification date produces a warning
- age at or above the critical threshold produces a critical signal
- age at or above the warning threshold produces a warning

Default thresholds are 90 and 180 days.

### 8.2 SimilarityAnalyzer

Analysis occurs in three phases:

1. Exact duplicates are grouped by content hash.
2. Versioned pages are grouped by normalized base title and ordered by numeric version.
3. Remaining page pairs are compared with RapidFuzz token similarity.

For long content, first, middle, and final text windows are compared. The older page is flagged
for exact and near-duplicate relationships.

Similarity signals carry replacement target identifiers used later by the orchestrator.

### 8.3 VersionRefsAnalyzer

This analyzer detects references to configured obsolete software versions.

Configuration supplies:

- labels and current versions
- regular-expression patterns used to identify version text

Historical context such as "migrate from v1" is excluded to reduce false positives.

### 8.4 BrokenLinkAnalyzer

URLs are extracted from structured source metadata and plain text.

Behavior:

- internal Notion links are skipped
- unique URLs are checked once and mapped back to all containing pages
- checks run in a bounded thread pool
- HTTP `HEAD` is attempted first
- HTTP 405 falls back to `GET`
- status codes of 400 or higher are broken
- connection failures are broken
- timeouts are not currently classified as broken

External network behavior can make results environment-dependent.

### 8.5 ReferenceAnalyzer

The analyzer recognizes references introduced by phrases such as:

- `See`
- `See:`
- `Refer to`
- `For more information, see`

Resolution order:

1. case-insensitive exact title
2. normalized base-title match

The source page itself is excluded as a target.

Outcomes:

- one target: resolved reference
- multiple targets: ambiguous reference
- no target: unresolved reference

Resolution occurs only against the pages passed to the analyzer.

### 8.6 InternalLinkAnalyzer

`InternalLinkAnalyzer` resolves source-aware `Document.links` against documents included in the
current scan.

Resolution order:

1. exact `target_id`
2. exact normalized URL
3. exact target title
4. normalized base-title match

The source page itself is excluded as a target.

Outcomes:

- one target with replacement language in link text or context: `replacement_link`
- one target with successor/backlink language: `backlink_from_successor`
- one target without relationship language: `resolved_internal_link`
- multiple targets: `ambiguous_internal_link`
- internal-looking link with no target: `broken_internal_link`

`replacement_link` is stale/supersession evidence for the source document. Broken and ambiguous
internal links are hard review risks. Resolved internal links contribute to incoming-reference
counts.

## 9. Title Normalization

`titles.py` provides shared normalization used by reference analysis, similarity analysis, and
trust classification.

Normalization removes supported trailing forms such as:

- years: `Guide 2024`
- versions: `Guide v3`, `Guide version 3.1`
- stale markers: `(old)`, `(deprecated)`, `(archived)`, `(legacy)`, `(draft)`, and related suffixes

The normalized base title is lowercase. A structured normalization function also returns the
detected year/version and stale suffix for comparison.

## 10. Trust Classifier

The classifier in `trust.py` receives:

- one normalized document
- all signals for that document
- incoming resolved-reference count
- titles of pages in the scan
- structured applicability scopes for pages in the scan

It parses structured body fields:

- `Status`
- `Owner`, `Maintained by`, or `DRI`
- `Last reviewed`
- `Replaced by` or `Superseded by`
- `Deprecated as of`
- `Canonical`
- `Review cadence`
- `Applies to`
- structured scope fields: `Product`, `Version`, `Audience`, `Environment`, `Region`, `Plan`,
  `Feature state`, and `Feature flag`
- compact `Scope:` metadata when it uses recognized scope dimensions

### 10.1 Classification Precedence

```text
1. Collect trust, stale, lifecycle, and scope evidence
2. Apply lifecycle policy to stale/review inputs
3. Explicit stale or supersession evidence
4. Hard review risks
5. Positive trust evidence with no active risks
6. Soft review risks
7. Insufficient evidence
```

Mapped outcomes:

- stale evidence -> `stale`
- stale evidence plus contradictory trust evidence -> `needs_review`
- hard risk -> `needs_review`
- positive trust evidence without risk -> `current`
- soft risk -> `needs_review`
- otherwise -> `unknown`

Lifecycle is stored separately from status. Supported lifecycle values are:

- `current`
- `supported`
- `deprecated`
- `superseded`
- `experimental`
- `draft`
- `archived`
- `unknown`

Lifecycle policy is centralized in the classifier:

- deprecated, superseded, and archived lifecycle preserve negative stale evidence
- experimental and draft lifecycle create review risk without making a document stale solely for
  that reason
- supported lifecycle suppresses weak scan-local version supersession because supported older
  versions may coexist
- current lifecycle suppresses weak version inference, but only by converting the conflict to a
  review risk when no stronger stale evidence exists
- hard negative title suffixes and explicit replacement/deprecation evidence are not suppressed by
  supported/current lifecycle

### 10.2 Positive Trust Evidence

Strong evidence includes:

- at least two incoming references
- `Status: Current`
- `Canonical: true`

Supporting evidence includes:

- supporting statuses such as `Supported` or `Approved`
- all outgoing references resolving
- recent last-reviewed date
- designated owner

Supporting evidence cannot produce current status without at least one strong indicator.

### 10.3 Stale Evidence

Stale evidence includes:

- exact duplicate signal
- older version signal
- configured obsolete-version reference
- stale status keywords
- supersession phrases in page content
- replacement metadata
- deprecation metadata
- archived source metadata
- structured `replacement_link` signals
- title-based year, version, or stale-suffix supersession within the scan

### 10.4 Review Risks

Hard risks include:

- unresolved references
- ambiguous references
- broken links
- broken internal links
- ambiguous internal links
- near duplicates
- critical modification age

Soft risks include:

- last reviewed more than one year ago
- overdue recognized review cadence

Current classifier behavior promotes any active hard or soft risk to `needs_review`.

### 10.5 Applicability Scope

Applicability scope is parsed into `trust_metadata["applicability_scope"]`. Scope fields are used to
avoid false stale classifications when related titles differ by year or version but explicitly apply
to different products, versions, audiences, environments, regions, plans, feature states, or feature
flags.

Scope is a suppression signal for weak scan-local supersession only. It does not create positive
trust evidence by itself and does not override explicit stale evidence such as `Replaced by`,
deprecated status, archived source metadata, or replacement-link signals.

### 10.6 Confidence

Confidence is rule-based and bounded between zero and one. Separate scoring functions handle:

- stale verdicts
- risk verdicts
- trusted verdicts
- unknown verdicts

Confidence represents strength of available evidence, not a statistical probability of factual
correctness.

## 11. Scan Orchestration

`Auditor.run()` coordinates the complete pipeline.

### 11.1 Normal Scan Flow

```text
Acquire scan lease
  -> create scan row
  -> load hashes from previous completed scan
  -> fetch and persist source documents
  -> carry unchanged prior results forward
  -> run analyzers on changed documents
  -> compute incoming-reference context
  -> parse scan-level applicability scopes
  -> classify changed documents
  -> select replacements
  -> apply cross-document post-processing
  -> compute human-audit actionability
  -> persist changed results
  -> combine changed and carried results
  -> synchronize workflow findings
  -> mark scan completed
  -> prune old scans
  -> run reporters
```

### 11.2 Incremental Processing

Documents whose content hash matches the previous scan are skipped during analysis. Their previous
audit results are copied into the new scan.

Changed or new documents are reanalyzed.

This reduces repeated work, especially external link checks. The current content hash only covers
page content. Changes limited to title, URL, source metadata, or modification timestamp do not
invalidate the hash.

Cross-document analysis for a mixed changed/unchanged scan is performed on the changed document
set, while prior results for unchanged pages are carried forward. This is an intentional current
prototype optimization and a known constraint for future relationship-sensitive incremental
analysis.

### 11.3 Cross-Document Post-Processing

After initial classification, the orchestrator:

- downgrades pages whose current status relies only on references from stale pages
- boosts a latest version when older related versions point to it
- promotes an otherwise unknown latest version when supersession evidence is strong and no active
  risk exists
- selects the first valid duplicate or similarity target as the suggested replacement

### 11.4 Human-Audit Actionability

After statuses and replacement context are final, the orchestrator calls
`compute_audit_actionability()` for every changed result and stores the returned fields inside
`trust_metadata`.

Actionability fields include:

- `requires_human_audit`
- `audit_priority`
- `importance_score`
- `importance_reasons`
- `actionability_reason`

Actionability rules:

- `current` results never require human audit
- `stale` results always require human audit with high priority
- deprecated, superseded, archived, experimental, and draft lifecycle values always require human
  audit
- hard risks such as broken links, broken internal links, unresolved references, ambiguous
  references, near duplicates, duplicates, and critical document age always require human audit
- overdue recognized review cadence always requires human audit
- lower-importance `unknown` and soft-risk `needs_review` results are visible in results but do not
  create workflow findings unless their importance score reaches the configured threshold

Importance uses incoming references, owner/status metadata, lifecycle, canonical/applicability
metadata, version-family membership, and whether a document is the suggested replacement for stale
siblings.

### 11.5 Failure Behavior

Any non-lease exception before completion:

- marks the scan failed
- records a sanitized error
- removes or prevents use of partial results as a completed scan
- prevents reporters from running

A worker that loses its lease aborts without overwriting the replacement worker's state.

## 12. Persistence Design

### 12.1 SQLite Backend

`storage/sqlite.py` contains the SQLite implementation as `SqliteStorage`. It owns connection
lifecycle, lease management, scan lifecycle, document/result persistence, workflow synchronization,
history queries, and maintenance operations.

`storage/factory.py` exposes `create_storage(database_url)`, the application construction boundary
used by CLI and FastAPI runtime paths. The factory returns an unconnected `AuditStorage` instance
— `SqliteStorage` for SQLite and bare-path inputs, `PostgresStorage` for `postgres://` and
`postgresql://` URLs. Unsupported schemes raise `ValueError`. Callers continue to own connection
lifecycle by calling `connect()` and `close()`.

`db.py` is now a backward-compatible facade. Existing callers can continue importing
`Database`, `LeaseLostError`, `ScanLeaseContext`, `_UNSET`, and `_sanitize_error` from
`kb_audit.db`. `Database` subclasses `SqliteStorage` and preserves the historical public API.

The SQLite connection enables write-ahead logging:

```sql
PRAGMA journal_mode=WAL;
```

Database URLs such as `sqlite:///kbaudit.db` are normalized to filesystem paths by `SqliteStorage`.

### 12.2 Primary Tables

#### `scans`

Stores scan lifecycle and summary information:

- scan ID
- start and completion timestamps
- status
- document counts by classification
- owner token
- error

#### `documents`

Stores normalized source documents for each scan:

- document ID
- scan ID
- title
- content
- source type
- URL
- modification timestamp
- content hash
- source metadata

#### `audit_results`

Stores one audit result per document and scan:

- document ID and scan ID
- overall status
- serialized signals
- suggested replacement ID
- confidence
- confidence reason
- serialized trust metadata and evidence

#### `finding_workflow`

Stores durable review workflow data:

- finding key
- document and source identity
- workflow state
- owner
- due date
- note
- snooze date
- dismissal or acceptance reason
- evidence hash
- first and last seen scan IDs
- last checked scan ID
- timestamps

#### `scan_state`

Stores the singleton scan lease:

- whether a scan is active
- owner token
- lease expiry
- last completed scan ID
- last scan error

### 12.3 Schema Initialization And Evolution

`storage/schema.py` owns SQLite schema initialization. It contains the current `CREATE TABLE IF NOT
EXISTS` statements, additive migration SQL, and the `initialize_schema()` helper used by
`Database.connect()`.

The schema initializer creates the base tables, creates workflow and scan-state tables, inserts the
singleton scan-state row, and applies additive `ALTER TABLE` statements for databases created by
earlier project versions.

SQLite schema evolution remains internal to `storage/schema.py`. PostgreSQL schema evolution now has
a manual Alembic migration path under `migrations/`; Alembic is not run automatically during normal
application startup.

### 12.4 Storage Contracts

`storage/contracts.py` defines runtime-checkable `Protocol` contracts for the current persistence
surface:

- connection lifecycle
- scan lease and concurrency state
- scan lifecycle and history
- document and audit-result persistence
- workflow finding synchronization and updates
- maintenance operations

`AuditStorage` combines these sub-protocols as the intended storage boundary. The current
`SqliteStorage` implementation and the compatibility `Database` facade remain structurally
compatible with the protocols. Application orchestration code types against `AuditStorage` where
practical, while legacy tests and callers may still construct `Database` directly.

`tests/test_storage_conformance.py` exercises the storage boundary through `AuditStorage` and
`create_storage()` rather than through `Database` or SQLite internals. It documents backend-neutral
expectations for connection lifecycle, scan lifecycle, document/result persistence, carry-forward,
workflow synchronization, workflow reopening, and maintenance behavior. Future storage backends
should satisfy this suite before being wired into runtime construction.

`PostgresStorage` is the implemented relational backend and is factory-wired for explicit
`postgres://` and `postgresql://` URLs (Step 10). SQLite remains the default backend for
bare paths and `sqlite://` inputs. Unsupported URL schemes still raise `ValueError`.

Any further relational or document-store backends must satisfy the same `AuditStorage` contract. A
relational backend can preserve the contract with tables, transactions, row locks or advisory locks,
and JSON-compatible columns. A document-store backend can preserve it with collections, embedded
documents, and backend-supported transactions or equivalent atomic update mechanisms at scan and
workflow boundaries.

`storage/postgres_support.py` provides `psycopg_available()` and `require_psycopg()` as an
optional driver probe for the PostgreSQL backend. psycopg v3 (`psycopg[binary]>=3.1`) is
declared as an optional project dependency under `[project.optional-dependencies] postgres`.
`create_storage()` returns `PostgresStorage` for `postgres://` and `postgresql://` URLs (Step 10).
The factory uses a lazy local import so that psycopg remains optional for SQLite-only users.
`PostgresStorage` has an opt-in live conformance harness in `tests/test_storage_postgres_live.py`.
Live conformance tests are skipped by default unless `KB_AUDIT_POSTGRES_TEST_URL` points to a
dedicated test database and psycopg is installed.

`storage/schema_postgres.py` contains the PostgreSQL DDL design for all five tables (`scans`,
`documents`, `audit_results`, `finding_workflow`, `scan_state`) and eight index statements. It
uses PostgreSQL-appropriate types: `BIGINT GENERATED BY DEFAULT AS IDENTITY` for auto-increment
primary keys, `TIMESTAMPTZ` for all timestamp columns, `JSONB` for signals/trust data/metadata,
and `BOOLEAN` for `scan_state.in_progress`. The module does not import psycopg and does not open connections directly.
`iter_postgres_schema_statements()` returns statements in dependency order for
`PostgresStorage.connect()` to execute.

Alembic migration management exists for PostgreSQL under `alembic.ini` and `migrations/`.
`migrations/versions/0001_initial_schema.py` delegates to
`iter_postgres_schema_statements()` so the initial migration and direct schema initializer share the
same DDL source. Migrations are applied manually with `KB_AUDIT_POSTGRES_URL=<url> alembic upgrade
head`; they are not invoked from `PostgresStorage.connect()` or normal application startup. The
`alembic_live` tests are opt-in and skip unless Alembic, psycopg, and
`KB_AUDIT_POSTGRES_TEST_URL` are available.

`storage/postgres.py` contains `PostgresStorage` with connection lifecycle, scan
lease/lifecycle behaviour, document/result persistence, workflow persistence, scan history/diff,
and maintenance operations. Connection
lifecycle: `connect()`, `close()`, `conn` (property), `is_connected` (property).  `connect()` calls `require_psycopg()`, imports
psycopg inside the method (never at module level), opens the connection, applies the full
schema via `iter_postgres_schema_statements()` in a cursor block, commits, and closes the
connection on error before re-raising.

Scan lease methods: `try_start_scan()`, `renew_lease()`, `owns_live_lease()`, `end_scan()`,
`reset_scan_state()`, `get_scan_state()`.  Scan lifecycle methods: `start_scan()`,
`finish_scan()`, `fail_scan()`.  `try_start_scan()` uses `SELECT … FOR UPDATE` on the
singleton `scan_state` row (id=1) instead of SQLite's `BEGIN IMMEDIATE`.  `start_scan()`
uses `INSERT … RETURNING id` to obtain the new scan ID.

Document/result persistence methods: `store_document()`, `store_result()`, `get_previous_hashes()`,
`carry_forward_results()`, `load_audit_results()`, and `get_scan_results()`. These methods reuse
`storage/serialization.py`, use PostgreSQL `INSERT … ON CONFLICT` upserts, pass JSON payloads as
serialized strings with explicit `::jsonb` casts, and cast JSONB values back to text for existing
deserializers. Mutating methods verify the active lease when an owner token is supplied and verify
that the target scan is still running before writing.

Workflow persistence methods: `complete_scan_with_findings()`, `sync_findings()`,
`update_workflow()`, `get_findings()`, `get_finding()`, and `get_workflow_summary()`. These methods
preserve SQLite finding identity and evidence-hash semantics, create findings only for actionable
results, reopen terminal findings when evidence changes, auto-fix findings that disappear from a
scanned document, and use PostgreSQL-compatible parameterization and timestamp handling.

History and maintenance methods: `get_scan_history()`, `get_scan_diff()`, `clear_all()`,
`clear_all_if_idle()`, and `prune_scans()`. These methods mirror SQLite scan-history shapes,
normalize Postgres timestamp values to API-compatible ISO strings, use `SELECT … FOR UPDATE` for
idle-clear lease checks, and avoid pruning running scans.

`PostgresStorage` does not inherit from `AuditStorage` but satisfies the runtime-checkable
`AuditStorage` protocol in focused tests. It is registered in `create_storage()` as of Step 10.
The optional `postgres_live` test marker covers live-backend behavior when a dedicated PostgreSQL
test database is supplied. Live tests and Alembic migration management are manual and opt-in;
production readiness is not yet claimed.

`storage/pg_readiness.py` provides import-safe PostgreSQL readiness checks. `check_readiness()`
reports whether a URL was supplied, whether it is a PostgreSQL URL, whether psycopg and Alembic are
importable, and whether migration files exist on disk. It does not import psycopg, Alembic, or
SQLAlchemy at module import time and does not open a connection. `connect_check()` is an explicit
live check that imports psycopg only when called, runs a lightweight `SELECT 1`, closes the
connection, does not mutate data, and does not run migrations.

The CLI exposes `kb-audit postgres-check` for the optional PostgreSQL path. URL resolution order is
`--url` > `KB_AUDIT_POSTGRES_TEST_URL` > configured `DATABASE_URL`. Empty or whitespace-only
resolved URLs are treated as no URL. Offline checks run by default; `--connect` must be supplied for
a live connection check. The command never runs Alembic and does not change the default SQLite
runtime path.

`docker-compose.postgres.yml` provides an opt-in local PostgreSQL container for development and
live-backend testing. It creates an ephemeral `kbaudit_dev` database and a dedicated
`kbaudit_test` database. It is not part of normal application startup, and production readiness is
not claimed.

### 12.5 Storage Serialization

`storage/serialization.py` centralizes persistence-format JSON helpers used by `db.py`:

- signal serialization and deserialization
- raw signal-record deserialization for API/reporting payloads
- trust metadata/evidence blob packing and unpacking
- document metadata JSON serialization
- stored scan-error sanitization

This package owns storage-format encoding only. Domain identity remains in `models.py`:

- `AuditResult.evidence_hash` remains on `AuditResult`
- `build_finding_key()` remains in the domain model layer

This preserves workflow identity and evidence-change semantics while preparing the persistence layer
for cleaner future boundaries.

### 12.6 Retention

After a successful scan, old scan data is pruned to retain the most recent 20 scans. Pruning is
non-fatal: a pruning failure does not change an already completed scan to failed.

## 13. Scan Lease And Concurrency

The product permits one active scan per database.

### 13.1 Lease Acquisition

The CLI or web API requests an owner token with `try_start_scan()`. If a non-expired lease exists,
the request is rejected.

### 13.2 Lease Ownership

`ScanLeaseContext` maintains and validates the lease. The owner token is passed to database write
operations.

Lease checks occur at orchestration boundaries, including:

- before scan initialization
- before document writes
- before each analyzer
- before result writes
- before completion
- before pruning
- before reporter execution

### 13.3 Lease Loss

If ownership is lost, `LeaseLostError` stops the stale worker. It must not mark or clean up the
replacement worker's scan.

### 13.4 Web Execution

The web API starts scans as FastAPI background tasks. Polling `/api/status` reports progress and
completion to the browser.

This is appropriate for the local prototype. A hosted deployment would require a durable job
queue rather than in-process background execution.

## 14. Workflow Synchronization

Workflow synchronization occurs atomically with successful scan completion.

For actionability-eligible results:

- new findings are inserted
- existing findings are updated
- changed evidence may reopen prior terminal findings
- stable evidence preserves existing workflow disposition

Workflow creation is governed by `trust_metadata["requires_human_audit"]` when present. Legacy rows
without that flag fall back to the earlier status-based rule: `stale`, `needs_review`, and `unknown`
receive findings.

For findings whose issue disappears:

- eligible open, acknowledged, snoozed, dismissed, or accepted-risk findings can auto-resolve to
  fixed
- findings for pages absent from the scan are not automatically fixed
- unchanged pages carried forward are not treated as newly reanalyzed evidence

The default queue includes:

- open findings
- acknowledged findings
- expired snoozed findings

It excludes:

- fixed findings
- dismissed findings
- accepted-risk findings
- future-snoozed findings

## 15. Reporting

Reporters implement:

```python
class Reporter(ABC):
    def report(self, results: list[AuditResult]) -> None: ...
```

### 15.1 ConsoleReporter

Produces a Rich table intended for interactive CLI use.

### 15.2 JsonReporter

Produces structured JSON to standard output or a selected file. JSON-only output avoids unrelated
standard-output text so it can be consumed programmatically.

Each document entry includes `trust_metadata`, which carries lifecycle, applicability scope, and
human-audit actionability metadata.

### 15.3 Web Review Reports

The web API exposes scan reports in JSON and text formats. The browser renders a review report and
supports downloads.

Reports distinguish status-flagged documents from documents that require human audit.

## 16. CLI Design

The `kb-audit` command exposes:

| Command | Responsibility |
|---|---|
| `scan` | Run a Notion or Confluence scan |
| `demo` | Reset and run the credential-free demo |
| `history` | Show recent scans |
| `findings` | List review findings |
| `triage` | Update workflow state |

Source selection:

- explicit `--source` takes precedence
- otherwise Confluence is selected when base URL and API token are configured
- otherwise Notion is selected

A working Confluence connection additionally requires account email.

The CLI can bypass persistence with `--no-db`, but workflow and history are then unavailable.

## 17. Web API

The FastAPI application exposes:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Serve the web application |
| `GET` | `/api/status` | Return configuration and scan state |
| `POST` | `/api/scans` | Start a scan |
| `GET` | `/api/scans` | List scan history |
| `DELETE` | `/api/scans` | Clear scan history when safe |
| `GET` | `/api/scans/{scan_id}` | Return scan results and comparison data |
| `GET` | `/api/scans/{scan_id}/report` | Return JSON or text review report |
| `GET` | `/api/references/summary` | Return reference-resolution summary |
| `GET` | `/api/findings` | List workflow findings |
| `GET` | `/api/findings/summary` | Return workflow counts |
| `GET` | `/api/findings/{finding_key}` | Return one finding |
| `PATCH` | `/api/findings/{finding_key}` | Update workflow data |

### 17.1 Scan Request

The scan request can specify:

- `scope_type`
- Notion query, root page, or database ID
- Confluence space or page ID
- Confluence CQL through the query field

Demo mode ignores source targets and always uses `DemoSource`.

### 17.2 Error Responses

The API returns structured JSON errors for:

- missing source configuration
- invalid page-tree targets
- concurrent scan attempts
- invalid workflow transitions
- missing findings
- unsafe history deletion

## 18. Web Frontend

The browser UI is implemented in one server-delivered HTML file.

It contains:

- CSS
- semantic HTML
- application state
- API client functions
- result rendering
- scan history
- workflow queue
- drawers and modals
- report generation and downloads

Major client-side state includes:

- current source and demo mode
- active scan and scan data
- active result and workflow filters
- selected finding
- in-flight workflow operations
- polling timer

### 18.1 Scan Modes

The UI constructs source-specific controls:

- Notion: configured target, page tree, database, title or URL search
- Confluence: configured target, space, page tree, CQL
- Demo: fixed pages with source controls hidden

Target values are not automatically restored between sessions, reducing accidental scans against
an old target. The selected scan mode may be retained in browser local storage.

### 18.2 Result UI

The results area provides:

- classification totals
- filters and sorting
- status, confidence, title, primary issue, owner, due date, and workflow state
- lifecycle and actionability indicators when present
- a detail drawer with evidence and workflow controls
- explicit suggested-replacement display
- non-navigable demo titles

### 18.3 Review Queue

The review queue displays actionable findings and supports:

- acknowledgment
- row-level More menus for secondary actions
- detailed workflow management
- owner assignment
- due dates
- snoozing
- dismissal
- accepted risk
- fixed and reopened states

Client-side in-flight tracking prevents repeated updates for the same finding.

### 18.4 Accessibility And Responsive Behavior

The implementation includes:

- labeled dialogs and close controls
- focus movement into and back from drawers
- keyboard dismissal behavior
- hidden demo controls removed from the focus order
- responsive layouts verified at desktop and mobile widths

## 19. Configuration

Configuration is loaded in this order:

1. built-in defaults
2. optional YAML configuration
3. environment variables for supported settings
4. command-line target overrides

`python-dotenv` loads local `.env` values.

Important settings include:

- Notion integration token
- Notion root page or database
- Confluence base URL, email, token, space, or page
- SQLite database path
- timestamp thresholds
- similarity threshold
- current software-version mappings and version patterns

Trust actionability currently uses fixed in-code thresholds and policy sets rather than
administrator-configurable policy files.

Secrets should be provided through `.env` or environment variables and excluded from source
control.

## 20. Demo Runtime Design

### 20.1 CLI Demo

`kb-audit demo`:

- uses `DemoSource`
- defaults to `kbaudit-demo.db`
- clears prior demo data when no scan is active
- acquires the standard scan lease
- supports table and JSON output
- supports a custom database path

### 20.2 Web Demo

`kb-audit-web --demo`:

- configures the FastAPI process explicitly for demo mode
- defaults to `kbaudit-demo.db`
- clears the demo database once at server startup
- does not clear data between scans during the same server session
- hides connected-source controls
- retains the normal result, history, report, and workflow experience

Demo URLs use the reserved `demo.example` hostname and are suppressed as navigation in the UI.

## 21. Security Design

### 21.1 Credentials

- source credentials are not stored in SQLite
- credentials are loaded from local environment or configuration
- `.env` is excluded from version control
- error handling is intended not to expose tokens

### 21.2 Network Boundaries

- the web server binds to localhost by default
- source APIs and external links are the only intended outbound network calls
- demo mode performs no source API calls

### 21.3 Browser Links

External links open in a new tab with `rel="noopener"`.
Demo URLs are rendered as plain text.

### 21.4 Current Security Limitations

The local web API has no authentication or authorization layer. It must not be exposed to an
untrusted network in its current form.

There is no tenant isolation, centralized secret management, permission replication, or hosted
audit log.

## 22. Observability And Error Handling

Python logging covers:

- fetched and skipped pages
- analyzer execution
- rate limiting
- workflow synchronization
- scan failures
- lease loss
- pruning

CLI errors are written to standard error where appropriate. JSON output avoids contaminating
standard output with status text.

The web UI receives scan state and the last scan error from `/api/status`.

The current system has no metrics backend, distributed tracing, alerting, or centralized log
collection.

## 23. Testing Strategy

### 23.1 Unit Tests

Unit tests cover:

- data models and hashes
- title normalization
- each analyzer
- trust classification
- storage serialization helpers
- storage conformance through the `AuditStorage` protocol
- configuration
- source conversion helpers
- reporters
- database operations

### 23.2 Integration Tests

Integration tests cover:

- the full auditor pipeline
- golden classification scenarios
- payment-document scenarios
- demo pipeline behavior
- scan history and incremental processing
- workflow synchronization and state transitions
- CLI commands
- FastAPI endpoints

External source APIs are mocked. Live Notion or Confluence credentials are not required.

### 23.3 Browser Tests

Playwright tests run against the real FastAPI UI with either:

- intercepted deterministic API responses
- a real local demo-mode backend

They verify:

- filtering and result rendering
- actionable queue counts
- workflow actions
- scan control behavior
- demo empty and completed states
- suggested replacements
- safe link rendering
- console errors
- desktop and mobile overflow

### 23.4 Static Verification

The development checks are:

```bash
pytest -q -m "not browser" -p no:cacheprovider
playwright install chromium
pytest -q -p no:cacheprovider
ruff check src/ tests/ --no-cache
mypy src/ --cache-dir /private/tmp/kbaudit-mypy-cache
```

## 24. Known Technical Constraints

### 24.1 Incremental Relationship Analysis

Unchanged results are carried forward, while analyzers receive changed documents. A change to one
page can theoretically affect the relationship-based classification of an unchanged page.
The current post-processing handles common version-target promotion cases among reanalyzed pages,
but fully correct incremental graph analysis would require dependency invalidation or reanalysis
of affected neighbors.

### 24.2 Content-Only Change Detection

The content hash excludes:

- title
- URL
- modification timestamp
- source metadata

A metadata-only change may therefore retain the previous result.

### 24.3 In-Memory Pairwise Similarity

Near-duplicate comparison is pairwise and can approach quadratic work as scan size grows. This is
acceptable for the current prototype but will require indexing or candidate generation at larger
scale.

### 24.4 Local SQLite

SQLite and a singleton scan lease support the current local process model. They are not the target
architecture for a multi-user hosted service.

### 24.5 In-Process Background Work

FastAPI background tasks are not durable. A process exit interrupts an active web scan.

### 24.6 External Link Variability

Link checks can differ across networks because of:

- authentication requirements
- firewall policy
- rate limiting
- bot blocking
- transient availability
- servers with unusual HEAD behavior

### 24.7 Frontend Packaging

The single-file frontend is simple to deploy but increasingly costly to maintain as UI behavior
grows. It has no component framework, module bundler, or generated API client.

### 24.8 Fixed Heuristic Policy

Lifecycle mappings, hard-risk signal sets, actionability thresholds, and scope dimensions are
implemented in code. They are deterministic and tested, but not yet configurable by administrators.

## 25. Future Technical Direction

### 25.1 Relationship-Aware Incremental Analysis

Introduce an explicit page relationship graph and invalidate:

- changed pages
- pages referencing changed pages
- pages in the same version family
- duplicate and similarity candidates
- replacement targets

### 25.2 Hosted Job Architecture

Replace in-process background tasks with:

- durable job queue
- worker processes
- retry policy
- cancellation
- progress events
- scheduled execution

### 25.3 Production Persistence

Move hosted deployments to a production relational database with:

- live PostgreSQL conformance/integration tests in CI
- tenant IDs
- source connection records
- encrypted credential references
- row-level authorization
- retention controls
- backups

### 25.4 Policy Engine

Separate classifier policy from implementation so organizations can configure:

- age and review thresholds
- required metadata
- status vocabulary
- risk severity
- current-status requirements
- AI retrieval eligibility

Policies must remain explainable and versioned.

### 25.5 Integration API

Expose stable APIs for:

- trust lookup by source page ID
- search filtering and reranking
- replacement lookup
- findings and workflow updates
- source-system webhooks
- event notifications

### 25.6 Frontend Evolution

If product complexity continues to grow, separate the frontend into modules or a typed application
with:

- reusable components
- generated API types
- isolated state management
- accessibility primitives
- frontend unit tests

This should be driven by actual complexity, not performed solely as a framework migration.

## 26. Technical Acceptance Criteria

The current technical implementation is acceptable when:

1. Supported sources normalize pages into valid `Document` objects.
2. Analyzer execution produces deterministic structured signals.
3. The classifier produces one valid status and a structured explanation for every analyzed page.
4. Reference resolution remains limited to pages supplied to the analyzer.
5. Suggested replacements resolve to pages in the scan.
6. Scan persistence reaches one terminal state: completed or failed.
7. Concurrent scans cannot write through the same active database lease.
8. Workflow state persists across scans and responds correctly to evidence changes.
9. Demo mode runs without credentials or source API calls.
10. CLI JSON output remains machine-readable.
11. The web UI exposes no navigable demo URLs.
12. Unit, integration, API, CLI, workflow, and browser tests pass.
13. Ruff and mypy report no errors.

## 27. Design Decisions Summary

| Decision | Rationale | Current Tradeoff |
|---|---|---|
| Deterministic rules instead of an LLM classifier | Explainability and repeatability | Heuristics require manual calibration |
| Shared normalized `Document` model | Keeps analyzers source-independent | Some source richness is stored but unused |
| SQLite for persistence | Minimal local setup | Not suitable for hosted multi-user scale |
| Stable finding and evidence hashes | Durable workflow across rescans | Identity changes when classification changes |
| Content-hash carry-forward | Avoids repeated analysis and network checks | Metadata-only and neighbor effects may be missed |
| Vanilla single-file frontend | Simple packaging and local deployment | Maintainability declines as UI complexity grows |
| FastAPI background tasks | Simple local asynchronous scans | Jobs are not durable |
| Bounded concurrent link checks | Reduces scan time | Results still depend on external networks |
| Credential-free demo source | Makes evaluation immediate and repeatable | Demo scenarios are curated, not exhaustive |
