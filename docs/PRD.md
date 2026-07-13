# Knowledge Base Auditor

## Product Requirements Document

**Document status:** Current  
**Product stage:** Local-first prototype  
**Last updated:** 2026-07-10

## 1. Purpose

Knowledge Base Auditor helps organizations determine which internal documentation can be
trusted, which pages require human review, and which pages appear obsolete or superseded.

Internal search systems frequently return multiple versions of documentation without clearly
indicating which version should be used. This can cause engineers to implement against obsolete
APIs, product managers to base decisions on outdated behavior, and AI systems to retrieve
unreliable guidance.

The product analyzes a selected set of documentation pages, produces an explainable trust
classification for each page, identifies likely replacements, and provides a workflow for
reviewing and resolving findings.

## 2. Product Vision

Knowledge Base Auditor will become a trust and governance layer for internal documentation.
Before a person, search system, or AI assistant relies on a page, the page's trust state should
be visible and actionable.

The long-term product should:

- detect obsolete and unreliable documentation continuously
- identify the strongest available replacement for superseded content
- help documentation owners resolve findings through an operational workflow
- expose trust information to search, Q&A, retrieval-augmented generation, and agent systems
- preserve clear evidence explaining every classification

## 3. Current Product Position

The current product is an early, local-first prototype intended for:

- portfolio demonstration
- local evaluation
- architecture and product validation
- design-partner conversations
- testing deterministic document-trust heuristics

It is not currently a hosted, multi-tenant SaaS product. It does not yet provide hosted
authentication, organization administration, enterprise permission synchronization, scheduled
scans, managed deployment, or direct integration with external search and AI retrieval systems.

## 4. Goals

### 4.1 Current Goals

- Scan selected documentation from supported sources.
- Classify every analyzed page as `current`, `needs_review`, `stale`, or `unknown`.
- Explain each classification with lifecycle, applicability scope, positive evidence, review risks,
  missing evidence, confidence, and a recommended action.
- Detect version relationships, duplicates, broken links, structured internal links, and unresolved
  references.
- Recommend replacement pages when evidence supports a specific alternative.
- Keep reference resolution and relationship analysis within the pages included in the scan.
- Give users a review queue for results that require human audit, with workflow states for
  acknowledgment, assignment, deferral, dismissal, accepted risk, fixing, and reopening.
- Preserve scan history and workflow state locally.
- Provide a credential-free demonstration of the complete product workflow.

### 4.2 Long-Term Goals

- Run scans automatically on a configurable schedule.
- Notify owners when new or overdue findings require action.
- Provide policy-based trust rules by team, source, page type, and risk tolerance.
- Integrate trust results into enterprise search and AI retrieval workflows.
- Support enterprise identity, permissions, audit logs, security controls, and deployment models.
- Expand source support beyond Notion and Confluence Cloud.

## 5. Non-Goals For The Current Product

The current prototype is not intended to:

- edit or delete source documentation
- replace human review or declare factual correctness
- crawl every page available to an integration when a narrower scan target is selected
- use opaque machine-learning output as the sole basis for classification
- provide full enterprise content management
- operate as a hosted multi-tenant service
- enforce source-system permissions independently of the configured integration
- automatically block pages from search or AI retrieval

## 6. Target Users

### 6.1 Documentation Consumer

Examples include engineers, product managers, support staff, and other stakeholders searching for
reliable internal guidance.

Needs:

- know whether a page is safe to rely on
- understand why it received its classification
- find a current replacement for obsolete content

### 6.2 Documentation Owner

Examples include engineering teams, product teams, technical writers, and service owners.

Needs:

- see which owned pages require attention
- understand the evidence behind each finding
- record ownership, due dates, notes, and disposition
- confirm when a problem has been resolved

### 6.3 Engineering Or Knowledge Manager

Needs:

- understand documentation health across a selected area
- prioritize risk and review work
- monitor whether findings improve or recur over time
- establish documentation governance practices

### 6.4 AI/Search Platform Owner

This is primarily a future product user.

Needs:

- prevent unreliable pages from being treated as authoritative
- obtain trust metadata for ranking, filtering, and retrieval decisions
- retain an auditable explanation for automated decisions

## 7. Core User Journeys

### 7.1 Evaluate The Product Without Credentials

1. The user installs the project locally.
2. The user starts the CLI or web demo.
3. The product scans ten built-in demo pages.
4. The user sees all four trust classifications and six actionable findings.
5. The user inspects evidence, suggested replacements, and the review queue.
6. The user performs at least one workflow action.

### 7.2 Scan A Documentation Area

1. The user configures Notion or Confluence Cloud credentials.
2. The user chooses a scan mode and target.
3. The product retrieves the selected pages.
4. The product analyzes relationships and evidence among those pages.
5. The user receives classifications and explanations.

### 7.3 Find The Correct Version

1. A scan includes multiple related versions of a page.
2. The product detects the version family.
3. Older versions are marked stale when sufficient supersession evidence exists.
4. The latest supported page is identified as current when sufficient trust evidence exists.
5. Older pages display the latest page as a suggested replacement.

### 7.4 Review And Resolve A Finding

1. The user opens the review queue.
2. The user filters or sorts actionable findings.
3. The user inspects the page, evidence, and suggested replacement.
4. The user acknowledges, assigns, snoozes, dismisses, accepts the risk, fixes, or reopens the
   finding.
5. The workflow state persists across scans.
6. If the underlying evidence disappears on a later scan, the finding can automatically resolve.

## 8. Functional Requirements

Requirement status values:

- **Implemented:** Present in the current product.
- **Next:** A near-term requirement for commercial readiness.
- **Future:** Part of the longer-term product direction.

### 8.1 Source Integrations

| ID | Requirement | Status |
|---|---|---|
| SRC-01 | Scan all Notion pages shared with the configured integration. | Implemented |
| SRC-02 | Scan a selected Notion page tree recursively. | Implemented |
| SRC-03 | Scan entries in a selected Notion database. | Implemented |
| SRC-04 | Search Notion by page title or page URL. | Implemented |
| SRC-05 | Scan a Confluence Cloud space. | Implemented |
| SRC-06 | Scan a Confluence Cloud page tree. | Implemented |
| SRC-07 | Scan Confluence Cloud pages selected by CQL. | Implemented |
| SRC-08 | Preserve source URLs and available source metadata. | Implemented |
| SRC-09 | Respect the access available to the configured source integration. | Implemented |
| SRC-10 | Support additional enterprise sources such as SharePoint, Google Drive, GitHub, Slack, and Zendesk. | Future |

### 8.2 Scan Scope

| ID | Requirement | Status |
|---|---|---|
| SCP-01 | Classification must use only pages included in the completed scan. | Implemented |
| SCP-02 | Internal reference resolution must not search unrelated pages outside the scan results. | Implemented |
| SCP-03 | Notion page-tree and database scans must remain limited to the selected hierarchy or database. | Implemented |
| SCP-04 | Notion title and URL searches may include accessible title variants needed to compare related versions. | Implemented |
| SCP-05 | Confluence scans must remain within the selected space, page tree, or CQL result set. | Implemented |
| SCP-06 | The UI must explain scan modes and targets in source-neutral, user-friendly language. | Implemented |

### 8.3 Analysis

| ID | Requirement | Status |
|---|---|---|
| ANL-01 | Detect page age using configurable warning and critical thresholds. | Implemented |
| ANL-02 | Detect exact duplicate pages. | Implemented |
| ANL-03 | Detect near-duplicate pages using content similarity. | Implemented |
| ANL-04 | Detect related version chains such as v1, v2, and v3. | Implemented |
| ANL-05 | Detect references to configured obsolete software versions. | Implemented |
| ANL-06 | Detect external links that return a failure response. | Implemented |
| ANL-07 | Detect page references that resolve within the scan. | Implemented |
| ANL-08 | Detect unresolved and ambiguous page references within the scan. | Implemented |
| ANL-09 | Extract supported trust metadata from page content. | Implemented |
| ANL-10 | Detect explicit supersession, legacy, deprecated, retired, archived, and replacement evidence. | Implemented |
| ANL-11 | Extract structured internal links from source adapters and resolve them within the scan. | Implemented |
| ANL-12 | Detect replacement links, successor backlinks, broken internal links, and ambiguous internal links. | Implemented |
| ANL-13 | Detect document lifecycle values separately from the four trust classifications. | Implemented |
| ANL-14 | Detect structured applicability scope for product, version, audience, environment, region, plan, feature state, and feature flag. | Implemented |
| ANL-15 | Allow trust policies and thresholds to vary by team, source, or page type. | Next |

### 8.4 Trust Classification

| ID | Requirement | Status |
|---|---|---|
| CLS-01 | Classify each page as `current`, `needs_review`, `stale`, or `unknown`. | Implemented |
| CLS-02 | Require positive trust evidence before classifying a page as current. | Implemented |
| CLS-03 | Never treat absence of negative evidence as proof that a page is current. | Implemented |
| CLS-04 | Classify strong obsolescence or supersession evidence as stale. | Implemented |
| CLS-05 | Classify active maintenance risks as needs review. | Implemented |
| CLS-06 | Classify insufficient evidence as unknown. | Implemented |
| CLS-07 | Treat contradictory stale and authoritative evidence as requiring human review. | Implemented |
| CLS-08 | Produce a confidence value and human-readable reason. | Implemented |
| CLS-09 | Produce structured positive evidence, review risks, missing evidence, and a recommended action. | Implemented |
| CLS-10 | Keep classification deterministic for identical pages, configuration, and dates. | Implemented |
| CLS-11 | Treat `draft` and `experimental` lifecycle as review-required without classifying them stale solely for that reason. | Implemented |
| CLS-12 | Allow multiple supported/current scoped versions to coexist when explicit applicability scope differs. | Implemented |
| CLS-13 | Distinguish classification status from whether a human audit workflow finding is required. | Implemented |
| CLS-14 | Allow administrators to configure classification policies without changing source code. | Next |

### 8.5 Replacement Recommendations

| ID | Requirement | Status |
|---|---|---|
| REP-01 | Suggest a replacement when an older version has a clearly identified newer version. | Implemented |
| REP-02 | Suggest a replacement when a near-duplicate page has a stronger authoritative alternative. | Implemented |
| REP-03 | Display the replacement title in result details. | Implemented |
| REP-04 | Link to the replacement when the source provides a usable URL. | Implemented |
| REP-05 | Never expose nonfunctional demo URLs as user navigation. | Implemented |

### 8.6 Review Workflow

| ID | Requirement | Status |
|---|---|---|
| WRK-01 | Create workflow findings only for results whose actionability metadata says human audit is required. | Implemented |
| WRK-02 | Exclude current pages from the actionable queue. | Implemented |
| WRK-03 | Support `open`, `acknowledged`, `dismissed`, `fixed`, `snoozed`, and `accepted_risk` states. | Implemented |
| WRK-04 | Store owner, due date, note, snooze date, and dismissal or acceptance reason. | Implemented |
| WRK-05 | Preserve workflow state across scans using stable finding identities. | Implemented |
| WRK-06 | Reopen a finding when materially different evidence appears. | Implemented |
| WRK-07 | Automatically resolve eligible findings when their evidence disappears. | Implemented |
| WRK-08 | Exclude terminal and future-snoozed findings from the default actionable queue. | Implemented |
| WRK-09 | Create or synchronize work in Jira, Linear, or another task system. | Future |
| WRK-10 | Notify owners and escalate overdue findings. | Future |

### 8.7 CLI

| ID | Requirement | Status |
|---|---|---|
| CLI-01 | Run source scans from the command line. | Implemented |
| CLI-02 | Produce human-readable table output. | Implemented |
| CLI-03 | Produce JSON output to standard output or a file. | Implemented |
| CLI-04 | Run without database persistence when requested. | Implemented |
| CLI-05 | Display scan history. | Implemented |
| CLI-06 | List and filter review findings. | Implemented |
| CLI-07 | Update workflow state using a finding-key prefix. | Implemented |
| CLI-08 | Run the credential-free demo with an isolated database. | Implemented |

### 8.8 Web Application

| ID | Requirement | Status |
|---|---|---|
| WEB-01 | Start scans using source-appropriate scan modes and targets. | Implemented |
| WEB-02 | Display summary totals for all four classifications. | Implemented |
| WEB-03 | Filter and sort results. | Implemented |
| WEB-04 | Display evidence, confidence, metadata, recommended actions, and suggested replacements. | Implemented |
| WEB-05 | Display scan history and prior results. | Implemented |
| WEB-06 | Provide an actionable review queue. | Implemented |
| WEB-07 | Support workflow actions, list-row action menus, and detailed workflow management. | Implemented |
| WEB-08 | Generate downloadable JSON and text review reports. | Implemented |
| WEB-09 | Provide a credential-free demo workspace. | Implemented |
| WEB-10 | Remain usable at desktop and mobile viewport widths without horizontal overflow. | Implemented |
| WEB-11 | Provide hosted authentication and organization administration. | Future |

### 8.9 Persistence And History

| ID | Requirement | Status |
|---|---|---|
| DAT-01 | Persist scans, documents, results, and workflow state in SQLite. | Implemented |
| DAT-02 | Prevent concurrent scans from corrupting shared scan state. | Implemented |
| DAT-03 | Store classification changes across scans. | Implemented |
| DAT-04 | Keep demo data separate from the default connected-source database. | Implemented |
| DAT-05 | Maintain a backend-neutral persistence conformance suite for future storage backends. | Implemented |
| DAT-06 | Keep PostgreSQL support optional until it satisfies the storage contract. | Implemented |
| DAT-07 | Provide PostgreSQL storage support for the full storage-method surface. | Implemented |
| DAT-08 | Provide an opt-in live PostgreSQL conformance harness. | Implemented |
| DAT-09 | Provide manual Alembic migration management for the PostgreSQL backend. | Implemented |
| DAT-10 | Wire PostgreSQL into `create_storage()` for explicit Postgres URLs. | Implemented |
| DAT-11 | Provide PostgreSQL readiness checks and an opt-in local Postgres demo/test path. | Implemented |
| DAT-12 | Certify live PostgreSQL conformance in CI and harden for production use. | Next |
| DAT-13 | Support a production database suitable for hosted multi-user operation. | Future |
| DAT-14 | Provide configurable retention, backup, and recovery policies. | Future |

### 8.10 Demo Mode

| ID | Requirement | Status |
|---|---|---|
| DEM-01 | Run without external credentials or knowledge-base access. | Implemented |
| DEM-02 | Use ten realistic built-in pages. | Implemented |
| DEM-03 | Produce three current, three stale, three needs-review, and one unknown result. | Implemented |
| DEM-04 | Demonstrate version succession and suggested replacements. | Implemented |
| DEM-05 | Demonstrate explicit legacy content, overdue review, unresolved references, near duplicates, and missing evidence. | Implemented |
| DEM-06 | Provide both CLI and web entry points. | Implemented |
| DEM-07 | Allow review workflow actions against demo findings. | Implemented |

## 9. Trust Evidence Model

### 9.1 Positive Evidence

Examples include:

- `Status: Current`
- `Canonical: true`
- sufficient incoming references from other pages in the scan
- recent review date
- a designated owner
- resolved outgoing references
- being the latest supported page in a related version family

Supporting evidence may increase confidence, but weaker indicators alone must not automatically
make a page current.

### 9.2 Stale Evidence

Examples include:

- explicit legacy, deprecated, retired, obsolete, archived, superseded, or end-of-life status
- `Replaced by` or `Superseded by` metadata
- explicit body text stating that the page should no longer be used
- structured internal links whose text or context says the page is replaced by another page
- an older version when a newer related page exists
- an exact duplicate that is older than another copy
- archived source metadata

### 9.3 Review Risks

Examples include:

- broken external links
- unresolved or ambiguous page references
- broken or ambiguous structured internal links
- overdue review cadence
- old last-reviewed date
- critical document age
- near-duplicate content
- conflicting authoritative and stale evidence

### 9.4 Lifecycle And Scope

Lifecycle is stored separately from classification status. Supported lifecycle values are `current`,
`supported`, `deprecated`, `superseded`, `experimental`, `draft`, `archived`, and `unknown`.
Negative lifecycle evidence can drive stale or review outcomes; draft and experimental lifecycle
states require review but are not stale solely because they are draft or experimental.

Structured applicability scope may include product, version, audience, environment, region, plan,
feature state, and feature flag. Scope is used to avoid false supersession when multiple versions or
variants are intentionally supported for different contexts.

### 9.5 Missing Evidence

Examples include:

- no status field
- no owner or DRI
- no last-reviewed date
- no review cadence
- no incoming references

Missing evidence should explain uncertainty. It should not, by itself, prove that a page is stale.

### 9.6 Human-Audit Actionability

Classification status and workflow actionability are separate. Current pages do not require human
audit. Stale pages, high-risk lifecycle states, hard review risks, and overdue review cadence require
human audit. Lower-importance `unknown` or soft-risk `needs_review` results remain visible in scan
results but may be suppressed from the actionable queue when importance signals are below threshold.

## 10. User Experience Requirements

- Status labels and colors must remain consistent throughout the application.
- Explanations must use plain language suitable for non-specialist users.
- Source-specific terminology must appear only when relevant to the selected source.
- The product must not expose fake demo links as navigable links.
- Suggested replacements must be clearly labeled and distinguishable from explanatory text.
- The default review queue must match the number of actionable findings.
- Current pages must not appear in the actionable review queue.
- Destructive or terminal workflow actions must require clear user intent.
- Loading, empty, success, and failure states must be visible.
- Scan controls must prevent accidental duplicate submissions.
- The interface must remain usable on desktop and mobile screen widths.

## 11. Non-Functional Requirements

### 11.1 Explainability

- Every classification must have a reason.
- Evidence must be available in structured form.
- The product must distinguish positive evidence, active risks, and missing evidence.
- A user must be able to understand why a replacement was suggested.

### 11.2 Determinism

- Identical input pages, configuration, and reference dates must produce identical results.
- Tests using fixed dates must not depend on the actual current date.

### 11.3 Reliability

- A failed scan must not be reported as successful.
- Concurrent scan attempts must not overwrite an active scan.
- Workflow updates must remain consistent across repeated requests and rescans.
- Source clients and database connections must be closed after use.

### 11.4 Security

- API tokens must be supplied through environment variables or local configuration excluded from
  source control.
- Error messages and logs must not expose credentials.
- External links opened by the web UI must use appropriate browser isolation attributes.
- The current product must bind locally by default.

### 11.5 Performance

- External link checks should run concurrently with bounded worker counts and timeouts.
- Source pagination must support documentation sets larger than one API response.
- Duplicate and relationship analysis should remain practical for the intended prototype scale.
- Commercial scale targets must be established before hosted deployment.

### 11.6 Accessibility And Responsiveness

- Dialogs and drawers must expose appropriate labels and keyboard behavior.
- Hidden demo controls must not remain keyboard-focusable.
- Text and controls must not overlap or create horizontal page overflow.

### 11.7 Compatibility

- The project requires Python 3.11 or newer.
- The local web application must support a current Chromium-based browser.
- Notion and Confluence behavior must be tested without requiring live credentials.

## 12. Architecture

The current architecture uses a deterministic processing pipeline:

```text
Document Source
    -> Evidence Analyzers
    -> Trust Classifier
    -> Scan Orchestrator
    -> Reporters and SQLite
    -> CLI and FastAPI Web UI
```

Primary components:

- `sources`: retrieve and normalize pages from Notion, Confluence Cloud, or demo data
- `analyzers`: produce timestamp, similarity, version, external-link, structured-internal-link, and
  reference signals
- `trust.py`: convert signals and document metadata into trust verdicts
- `auditor.py`: coordinate fetching, analysis, classification, replacement selection, and storage
- `db.py`: preserve the existing `Database` persistence facade for compatibility callers
- `storage`: provide the `create_storage()` construction boundary, persistence contracts, the SQLite
  backend, schema, serialization helpers, and factory-wired opt-in PostgreSQL backend plus optional
  live conformance tests, manual Alembic migrations, and PostgreSQL readiness checks
- `docker-compose.postgres.yml`: provide an opt-in local PostgreSQL instance for development and
  live-backend testing
- `reporters`: produce console and JSON output
- `web`: expose the local API and browser interface

SQLite is appropriate for the current single-user local product. A hosted commercial version will
require a completed production database backend and tenant-aware service architecture.

## 13. Reporting Requirements

Each scan result should include:

- page identity and source
- title and source URL when available
- classification
- confidence
- confidence reason
- positive evidence
- review risks
- missing evidence
- recommended action
- suggested replacement when available
- lifecycle and applicability metadata when available
- human-audit requirement, priority, and actionability reason
- workflow state when actionable

Scan-level reporting should include:

- total pages analyzed
- counts by classification
- human-audit/actionable finding count
- comparison with a prior scan when available
- downloadable machine-readable and human-readable output

## 14. Success Metrics

The current prototype should be evaluated using:

- percentage of demo and golden-scenario pages classified as expected
- percentage of classifications with complete structured explanations
- percentage of known superseded pages with correct replacement suggestions
- number of false `current` classifications in curated scenarios
- successful completion of unit, integration, API, CLI, workflow, and browser tests
- time required for a new evaluator to run the demo and understand the result set

Future commercial metrics should include:

- reduction in stale pages used by employees or AI systems
- median time from finding creation to acknowledgment
- median time from acknowledgment to resolution
- percentage of findings resolved before their due date
- recurrence rate for previously resolved findings
- percentage of retrieved pages carrying a verified trust state
- adoption by documentation owners and search/AI platform teams

## 15. Current Acceptance Criteria

The current prototype is acceptable when:

1. A user can install and run the credential-free demo.
2. The demo produces exactly ten results: three current, three stale, three needs review, and one
   unknown.
3. The demo produces six actionable review findings.
4. The three expected replacement relationships are correctly generated and displayed.
5. A user can scan a supported Notion or Confluence Cloud target.
6. Every result includes a classification, confidence, and structured explanation.
7. Review workflow actions persist and behave consistently across scans.
8. Current pages are excluded from the default actionable review queue.
9. Reports can be viewed and downloaded from the web application.
10. Unit, integration, API, CLI, workflow, and browser tests pass.
11. Ruff and mypy complete without errors.
12. No credentials are required to evaluate the primary demo workflow.

## 16. Known Limitations

- Scans are manually initiated.
- The application is local-first and single-organization.
- Source permissions are limited to what the configured integration can access.
- External link results can vary because of authentication, rate limiting, network policy, or
  remote server behavior.
- Deterministic heuristics identify trust evidence and risk; they do not verify factual truth.
- Some source context metadata is stored only for explainability and is not yet used as
  classification evidence.
- The product does not yet write corrections back to source systems.
- The product does not yet influence external search or AI retrieval ranking.
- There is no hosted identity, tenant, policy, notification, or audit-log layer.

## 17. Roadmap

### 17.1 Near-Term Product Quality

- improve and calibrate trust heuristics using representative documentation sets
- add configurable trust policies without requiring source changes
- add a separate edge-case test fixture for contradictory metadata, ambiguous references, missing
  dates, exact duplicates, and broken URLs
- improve onboarding and publication materials, including screenshots
- validate installation and demo use on a clean machine

### 17.2 Operational Product

- scheduled scans
- owner notifications
- overdue-review escalation
- Jira, Linear, Slack, and email integrations
- richer trend and documentation-health reporting
- configurable retention and export

### 17.3 Enterprise Product

- hosted authentication and organization management
- tenant isolation
- permission-aware scanning and reporting
- policy administration
- audit logs and compliance reporting
- managed deployment and security controls
- additional knowledge-source connectors

### 17.4 Search And AI Trust Layer

- expose trust state through an API
- filter or rerank search and retrieval results
- prevent stale pages from being treated as authoritative by AI assistants
- provide replacement pages and evidence to downstream retrieval systems
- collect feedback when users or reviewers disagree with a classification

## 18. Open Product Questions

- Which team should own trust policy: documentation, engineering productivity, enterprise search,
  or AI platform engineering?
- Should `needs_review` always block AI retrieval, or should behavior depend on policy and risk?
- Which evidence should be configurable, and which rules should remain fixed for safety?
- How should permissions be preserved when results are exposed outside the source system?
- What scan size and completion-time targets are required for enterprise adoption?
- Should source-system write-back be supported, and what approval model would govern it?
- Which integrations provide the strongest commercial entry point: notifications, task systems,
  search, or AI retrieval?

## 19. Terminology

- **Scan:** One analysis run over a selected set of pages.
- **Scan scope:** The source mode and target that determine which pages are included.
- **Finding:** An actionable result requiring review or disposition.
- **Trust evidence:** Information supporting the use of a page as authoritative.
- **Review risk:** Evidence that a page may be unreliable or require maintenance.
- **Lifecycle:** A document lifecycle label such as supported, deprecated, experimental, draft, or
  archived, stored separately from trust classification.
- **Applicability scope:** Structured metadata describing where a document applies, such as product,
  version, environment, audience, or plan.
- **Human-audit actionability:** The explicit decision that a result should or should not appear in
  the review workflow queue.
- **Suggested replacement:** A page identified as a stronger alternative to a stale or duplicate
  page.
- **Current:** Recommended as authoritative based on available evidence.
- **Needs review:** Contains active risks requiring human judgment.
- **Stale:** Strong evidence indicates that the page is obsolete or superseded.
- **Unknown:** Insufficient evidence exists to classify the page confidently.
