# Knowledge Base Auditor

## Technical Design Document

**Document status:** Current implementation  
**Product stage:** Local-first prototype  
**Last updated:** 2026-06-25

## 1. Purpose

This document describes the implemented technical design of Knowledge Base Auditor. It covers the
runtime architecture, component responsibilities, data flow, persistence model, APIs, concurrency
controls, security boundaries, testing strategy, and known technical constraints.

The related product documents are:

- `docs/Product Brief.md`: original problem statement and product intent
- `docs/Product Requirements Document.md`: product requirements and roadmap
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
| Persistence | SQLite with WAL mode |
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
  web/
    app.py            FastAPI application and web runtime
    templates/
      index.html      Browser UI
  auditor.py          Scan orchestration
  cli.py              CLI entry point
  config.py           Configuration loading
  db.py               SQLite schema and persistence
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
| `content_hash` | string | SHA-256 hash calculated from content |

The content hash is used for incremental scan behavior. It does not currently include title,
source metadata, URL, or modification time.

### 6.2 StalenessSignal

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

### 6.3 AuditResult

`AuditResult` combines a document, analyzer signals, and the trust verdict.

Important fields include:

- `status`
- `confidence`
- `confidence_reason`
- `trust_metadata`
- `trust_evidence`
- `suggested_replacement`

### 6.4 Finding Identity

Actionable results use a deterministic finding key:

```text
SHA-256(source_type + ":" + document_id + ":" + status)[0:24]
```

The key remains stable while source, document identity, and classification remain unchanged.
A classification change produces a different key.

### 6.5 Evidence Identity

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
recursively to a configured internal maximum depth. Extracted page metadata includes:

- created time and creator
- last editor
- parent information
- archived state
- extracted links

Title and URL search behavior can retrieve related title variants so pages such as v1, v2, and v3
can be analyzed together. Page-tree and database modes remain bound to their selected targets.

### 7.2 ConfluenceSource

Authentication uses HTTP Basic authentication with Atlassian account email and API token.

Supported modes:

- space
- recursive page tree
- CQL query

The adapter uses the Confluence Cloud REST API, retrieves storage-format HTML, and converts it to
plain text with an `HTMLParser`-based extractor. It retains:

- source URL
- modification time
- creator and last editor
- creation date
- version number
- page status
- space key
- ancestor titles
- extracted links

Modification dates and links feed current analyzers. Most remaining Confluence metadata is stored
as context but is not currently used by the trust classifier.

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

It parses structured body fields:

- `Status`
- `Owner`, `Maintained by`, or `DRI`
- `Last reviewed`
- `Replaced by` or `Superseded by`
- `Deprecated as of`
- `Canonical`
- `Review cadence`
- `Applies to`

### 10.1 Classification Precedence

```text
1. Explicit stale or supersession evidence
2. Hard review risks
3. Positive trust evidence with no active risks
4. Soft review risks
5. Insufficient evidence
```

Mapped outcomes:

- stale evidence -> `stale`
- stale evidence plus contradictory trust evidence -> `needs_review`
- hard risk -> `needs_review`
- positive trust evidence without risk -> `current`
- soft risk -> `needs_review`
- otherwise -> `unknown`

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
- title-based year, version, or stale-suffix supersession within the scan

### 10.4 Review Risks

Hard risks include:

- unresolved references
- ambiguous references
- broken links
- near duplicates
- critical modification age

Soft risks include:

- last reviewed more than one year ago
- overdue recognized review cadence

Current classifier behavior promotes any active hard or soft risk to `needs_review`.

### 10.5 Confidence

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
  -> classify changed documents
  -> select replacements
  -> apply cross-document post-processing
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

### 11.4 Failure Behavior

Any non-lease exception before completion:

- marks the scan failed
- records a sanitized error
- removes or prevents use of partial results as a completed scan
- prevents reporters from running

A worker that loses its lease aborts without overwriting the replacement worker's state.

## 12. Persistence Design

### 12.1 SQLite Configuration

The database connection enables write-ahead logging:

```sql
PRAGMA journal_mode=WAL;
```

Database URLs such as `sqlite:///kbaudit.db` are normalized to filesystem paths.

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

### 12.3 Schema Evolution

The schema is created with `CREATE TABLE IF NOT EXISTS`. A migration list applies additive
`ALTER TABLE` statements for databases created by earlier project versions.

There is no external migration framework in the current prototype.

### 12.4 Retention

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

For actionable results:

- new findings are inserted
- existing findings are updated
- changed evidence may reopen prior terminal findings
- stable evidence preserves existing workflow disposition

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

### 15.3 Web Review Reports

The web API exposes scan reports in JSON and text formats. The browser renders a review report and
supports downloads.

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
- a detail drawer with evidence and workflow controls
- explicit suggested-replacement display
- non-navigable demo titles

### 18.3 Review Queue

The review queue displays actionable findings and supports:

- acknowledgment
- full editing
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

- tenant IDs
- source connection records
- encrypted credential references
- migrations
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
