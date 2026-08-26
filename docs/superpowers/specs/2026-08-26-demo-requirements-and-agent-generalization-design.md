# Demo Requirement Refresh and Agent Generalization

Date: 2026-08-26
Status: Approved in chat; pending written-spec review

## Objective

Remove duplicate custom-alias/link-expiry requirements from the Governance SQLite
backlog, replace them with two distinct URL-shortener demo requirements, and make
the SDLC workflow produce truthful, requirement-specific implementation and test
results for all supported demos.

The canonical `REQ-002` record remains unchanged. Only duplicate records
`REQ-004` and `REQ-005` are removed.

## Current Problems

1. `REQ-004` and `REQ-005` duplicate the canonical expiry requirement and add
   no useful variety to the dashboard.
2. The brownfield architect emits alias/expiry design output regardless of the
   actual requirement.
3. In mock mode, the implementer records a success rationale without changing
   the target application.
4. The tester writes fixed pass counts and does not execute pytest, allowing a
   run to claim acceptance coverage that does not exist.
5. The current generated-code path fails on a clean SQLite database because it
   flushes a link with a non-nullable code before assigning the generated code.

## Chosen Approach

Use a hybrid profile architecture:

- Known demo requirements use deterministic profiles so the full demonstration
  works offline and produces repeatable artifacts.
- Live mode may use the configured LLM for an unknown requirement.
- Mock/replay mode must stop clearly when no deterministic profile matches. It
  must never substitute an unrelated profile or fabricate success.

This is preferred over a live-LLM-only design because the documented demo must
remain reliable without network access or API keys.

## Replacement Requirements

Both records begin as `draft`, `not_started`, and `not_requested` for analysis.
This lets a presenter demonstrate the complete dashboard lifecycle rather than
starting from an artificially advanced state. SQLite autoincrement assigns new
IDs; deleted IDs are not reused.

### Password-Protected Short Links

Type: `brownfield`

Intent: Allow a link creator to require a password before a short link reveals
or redirects to its destination, without changing existing unprotected links.

Acceptance criteria:

- `POST /shorten` accepts an optional `password`.
- Passwords are salted and hashed; plaintext passwords are never persisted or
  returned by the API.
- `GET /{code}` redirects an unprotected link exactly as it does today.
- `GET /{code}` returns `401 Unauthorized` for a protected link when the
  `X-Link-Password` header is missing or incorrect.
- `GET /{code}` redirects a protected link when the header is correct.
- Existing links and requests that omit `password` remain backward compatible.

Constraints:

- Use standard-library password derivation (`hashlib.pbkdf2_hmac`) and
  constant-time comparison; do not add a paid service.
- Never log or include the supplied password in artifacts.
- Keep SQLite persistence.

### Bulk URL Shortening with Idempotent Retries

Type: `brownfield`

Intent: Allow clients to shorten several URLs in one request and safely retry a
timed-out request without creating duplicate links.

Acceptance criteria:

- `POST /shorten/batch` accepts between 1 and 100 items.
- Each item supports the same URL, custom-alias, expiry, and password fields as
  `POST /shorten`.
- The endpoint requires an `Idempotency-Key` header.
- Retrying the same key with the same payload returns the original response and
  creates no additional links.
- Reusing the same key with a different payload returns `409 Conflict`.
- Item-level validation or alias conflicts are reported without discarding
  successful items, using a stable ordered result for every input item.
- The existing single-item `POST /shorten` API remains unchanged.

Constraints:

- Store only a digest of the idempotency key and a canonical request digest.
- Persist the completed response so retries remain stable after restart.
- Keep batch size bounded at 100.

## Profile Model

Add a small profile registry owned by the agents package. A profile contains:

- a stable profile name;
- deterministic matching rules based on normalized title, intent, acceptance
  criteria, and requirement type;
- requirement-specific architecture/impact data;
- expected implementation capabilities;
- the pytest acceptance-test selectors that prove those capabilities.

Initial profiles are:

- core shortener;
- custom alias and expiry;
- password-protected links;
- bulk shortening with idempotency;
- ambiguous reliability, which retains the interpretation gate.

Matching must produce exactly one profile. Zero matches in mock/replay mode is a
safe-stop with a useful reason. Multiple matches are an ambiguity failure rather
than an arbitrary choice. Live mode may ask the LLM to design an unknown
requirement, but still requires executable tests before the workflow passes.

## Agent Behavior

### Requirements and Planner

The requirements analysis remains the source of normalized acceptance criteria.
The selected profile is added to run context and lineage. Existing ambiguity
handling remains intact.

### Architect

The architect reads the selected profile and emits profile-specific impacted
modules, schema changes, API changes, security considerations, and regression
risks. Human approval is requested when the resulting tags include schema or
security-sensitive changes. Alias/expiry details are no longer the universal
brownfield output.

### Implementer

For deterministic demo profiles, mock/replay mode materializes a versioned,
known-good target application template that supports all deterministic profiles.
Using one cumulative template prevents a later demo run from deleting features
implemented by an earlier run. The implementation artifact records:

- selected profile;
- template version or live-generation provenance;
- files changed;
- capabilities provided;
- a digest of the resulting target-app source.

Live mode may generate the target app from the approved design. Generated code
must compile before it replaces the existing file; replacement uses a temporary
file followed by an atomic move. Unusable output leaves the existing file intact
and fails the implementation node rather than reporting success.

### Tester

The tester runs pytest as a subprocess using profile-specific test selectors. It
captures the command, return code, passed/failed counts, and concise failure
output in the testing artifact. `exit_ok` is true only when pytest exits zero and
every acceptance criterion is mapped to a passing test. Fixed pass counts are
removed.

Tests use a temporary SQLite database selected through an application database
path environment variable. Test execution must not mutate `target-app/urls.db`.

## Target Application Changes

### Link Persistence

Extend `links` with nullable password salt and password hash columns. Existing
rows remain unprotected. SQLite startup migration adds missing columns without
rewriting existing link data.

Fix generated-code allocation so clean databases do not insert a null code. A
generated link receives a provisional unique code before its first flush, then
receives its final base62 code inside the transaction. Collision checks cover
both generated codes and custom aliases.

### Password Verification

Password creation uses a cryptographically random salt and PBKDF2-HMAC-SHA256.
Verification uses `hmac.compare_digest`. Missing and incorrect passwords have
the same response and do not reveal which case occurred. Click counts increment
only after password and expiry checks pass.

### Idempotency Persistence

Add an `idempotency_requests` table containing:

- idempotency-key digest (unique);
- canonical request digest;
- serialized completed response;
- HTTP status;
- creation timestamp.

The batch operation and idempotency record are committed consistently. A retry
with the same key and payload returns the stored response. A different payload
for the same key returns `409`.

Batch results preserve input order. Each item reports success with its code and
short URL, or failure with an HTTP-style status and detail. The overall endpoint
returns `200` when every item succeeds and `207 Multi-Status` when results are
mixed.

## Governance Database Cleanup

Before mutation, use SQLite's online backup API to create a timestamped backup
under `workspace/backups/`. This safely includes committed WAL data.

Within one immediate transaction:

1. Re-read IDs 4 and 5.
2. Verify both titles normalize to the custom-alias/link-expiry duplicate and
   neither is canonical ID 2.
3. Delete only IDs 4 and 5.
4. Insert the two replacement requirements with fresh autoincrement IDs.
5. Verify canonical IDs 1 through 3 are unchanged and titles are unique.
6. Commit; otherwise roll back the entire operation.

Historical directories under `workspace/runs/` remain untouched. They are audit
records outside the SQLite requirements table and may still be inspected by run
ID. No current database table has a foreign key to requirements.

## Testing Strategy

### Database cleanup

- A test database containing REQ-001 through REQ-005 removes only duplicates 4
  and 5.
- Canonical REQ-002 is unchanged.
- Replacement IDs are new and both begin in the expected lifecycle states.
- A title mismatch aborts cleanup without partial mutation.
- The backup can be opened and contains the pre-cleanup records.

### Profiles and agents

- Each seeded requirement matches exactly one profile.
- Unknown and multiply matched mock requirements safe-stop.
- Architect artifacts contain only the selected profile's changes.
- Implementer failures do not overwrite the existing app.
- Tester node status reflects the real pytest exit code.

### Target application

- Existing shorten, redirect, stats, aliases, and expiry tests remain green.
- Clean-database ordinary shortening succeeds.
- Password hashing, successful authorization, missing/incorrect password, expiry
  ordering, and click-count behavior are covered.
- Batch bounds, mixed results, ordering, same-payload retry, changed-payload
  conflict, and restart persistence are covered.

### End-to-end

- Run each replacement requirement from analysis through the release approval
  gate in mock mode.
- Confirm artifacts name the correct profile and actual tests.
- Confirm no expiry-specific architecture leaks into password or batch runs.

## Safety and Rollback

- Preserve unrelated working-tree changes.
- Never delete run directories as part of database cleanup.
- Keep the timestamped pre-cleanup database backup.
- Database mutation is transactional.
- Target-app replacement is atomic and occurs only after syntax validation.
- A failed implementation or test node remains visibly failed/stopped and cannot
  reach release readiness.

## Completion Criteria

The work is complete when:

1. REQ-004 and REQ-005 no longer exist in the active Governance database.
2. Canonical REQ-002 is unchanged.
3. The two replacement requirements appear as draft backlog items.
4. Each replacement can run through accurate profile-specific architecture,
   implementation, and real tests in offline mock mode.
5. Existing URL-shortener behavior remains compatible.
6. All repository tests and both end-to-end demo workflows pass.
