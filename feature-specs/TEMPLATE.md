# Feature Name

For a long single-file spec, add a linked table of contents below this introduction. A large coupled
feature may split ledger, rebase, and test detail into sibling documents as described in
[feature-specs/README.md](README.md); keep these required headings in `index.md` and link each
companion from its owning section.

## Fork Metadata

- **Status:** Fork-only | Partially upstreamed | Upstreamed | Retired
- **Tracking:** Link the issue or design record.
- **First implementation:** Link the pull request and immutable commit.
- **Implementation base:** Link the immutable upstream commit on which the feature was first applied.
- **Latest upstream assessment:** Record the immutable upstream commit, assessment date, and
  conclusion.

## Summary

State the user-facing purpose and the smallest complete contract for the feature.

## Why This Fork Carries It

Explain the problem that upstream behavior does not solve and why this feature is the chosen
boundary.

### Goals

- List intended outcomes and link their requirement IDs.

### Non-Goals

- List adjacent behavior deliberately owned elsewhere.

## Behavior

Describe observable behavior, terminology, defaults, ordering, errors, compatibility, and security
boundaries. Define each normative outcome once in this section or in the ledger, then reference its
requirement ID elsewhere. Use third-level headings for lifecycle phases, public surfaces, or coupled
subfeatures.

Detailed algorithms and internal state names are non-exclusive reference designs unless explicitly
declared public or persisted compatibility contracts. Alternative designs conform when they prove
the same observable outcomes, migrations, and direct tests.

### Subfeature Name

Summarize its role and reference the applicable [req:example-outcome](#req-example-outcome) and
[delta:example-core](#delta-example-core) rows. Do not repeat normative prose or internal paths.

## Entry Points

List only public API, CLI/tool/UI, and user or developer documentation surfaces. Internal code paths
belong only in the Delta Inventory. Link with document-relative destinations and
repository-relative labels; adjust `..` for this spec's location.

- [website/docs/developer-guide/plugins/index.md](../website/docs/developer-guide/plugins/index.md)
- Public import: `package.PublicType`

For a Retired spec, use commit-pinned links to former public surfaces and current upstream or
replacement surfaces where applicable. If removal was deliberate and has no replacement, say so
and link the removal evidence.

For a changed public API, state how existing callers remain valid through optional or additive
evolution, or name the explicit versioned migration. A newly required argument is not
backward-compatible.

## Requirement Status Ledger

This is the canonical identity and status record. Define every `req:`, `gap:`, and `missing:` anchor
exactly once here. Other sections link these IDs. Implementation defects and missing direct coverage
are independent, even when their suffixes match. IDs are unique across the catalog; prefix a suffix
with the feature ID when needed. When this ledger is a companion file, references from `index.md`
include its document-relative path.

| Requirement ID                                         | Normative contract/reference                                                     | Implementation status/evidence                                                                                               | Upstream disposition/evidence                          | Coverage (direct/missing)                                      |
| ------------------------------------------------------ | -------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------ | -------------------------------------------------------------- |
| <a id="req-example-outcome"></a> `req:example-outcome` | State the sole canonical contract here, or link its sole definition in Behavior. | Conforming — cite runtime evidence and [delta:example-core](#delta-example-core).                                            | Retain — cite the assessed upstream commit and reason. | `tests/path/test_feature.py:test_outcome`                      |
| <a id="req-example-failure"></a> `req:example-failure` | Link the sole normative definition.                                              | <a id="gap-example-failure"></a> `gap:example-failure` — cite defect evidence and [delta:example-core](#delta-example-core). | Adapt — cite evidence.                                 | <a id="missing-example-failure"></a> `missing:example-failure` |

The table has exactly five columns and one row per requirement:

1. Column 1 defines exactly one canonical, anchored `req:` ID.
2. Column 2 contains the sole normative contract or links its sole definition in Behavior.
3. Column 3 is exactly `Conforming — <evidence>; <linked delta IDs>` or
   `<canonical anchored gap definitions> — <evidence>; <linked delta IDs>`. It contains no raw
   internal path.
4. Column 4 starts with exactly one of `Retain`, `Adapt`, `Upstream`, `Deliberately removed`, or
   `Not applicable`, followed by evidence.
5. Column 5 contains one or both of: direct targets in the formats documented in
   [feature-specs/README.md](README.md), and canonical, anchored `missing:` definitions owned by that
   same row.

Do not use `Implemented`, `Partial`, `Fork-only`, or free-form substitutes in ledger cells. A Retired
row instead links immutable equivalence/removal evidence and explains inapplicable implementation or
coverage status. Supporting sections link ledger and inventory IDs; they never redefine an ID,
contract, status, or internal path map.

## Persisted State And Compatibility

Reference the requirements governing database fields, files, serialized values, defaults for old
data, identity/ownership, migration/rebuild locations, downgrade behavior, and cleanup. Put internal
locations only in Delta Inventory rows and link their IDs here. Write `Not applicable` with a reason
when the feature has no persisted state.

## Invariants

- Link the ledger requirement that must survive a refactor or rebase; do not restate its contract.
- Identify any explicitly public or persisted algorithm, state name, or ordering constraint.

## Known Implementation Gaps

For each defined gap, link its ledger ID, affected requirement, and responsible Delta Inventory row.
Record the verified evidence, unsafe or incompatible current outcome, and intended fix boundary
without weakening or repeating the normative contract. Do not repeat raw internal paths here. Use
`None known` only after checking every ledger row.

- [gap:example-failure](#gap-example-failure) affects
  [req:example-failure](#req-example-failure) in
  [delta:example-core](#delta-example-core); link evidence and state the fix boundary.

For a Retired spec, write `Not applicable — retired` and link any residual risk to the retirement
evidence.

## Rebase Assessment

### Delta Inventory

This table is the sole canonical map of internal implementation paths and responsibilities. Give
every row one stable `delta:` anchor; reference row IDs everywhere else. Prefer stable symbols and
responsibilities over line numbers.

| Delta ID                                             | Fork entry points                         | Upstream/replacement                                                              | Responsibility or evidence                       |
| ---------------------------------------------------- | ----------------------------------------- | --------------------------------------------------------------------------------- | ------------------------------------------------ |
| <a id="delta-example-core"></a> `delta:example-core` | [tools/registry.py](../tools/registry.py) | Link an immutable upstream path or record deliberate removal without replacement. | State the responsibility that must be preserved. |

For a Retired spec, use commit-pinned historical entry points and current upstream or replacement
locations where applicable. For deliberate removal without replacement, say so and cite removal and
lifecycle/migration/cleanup evidence.

### Conflict-Resolution Rules

- Explain how to adapt inventory rows if upstream moves or replaces an integration point.
- Reference requirement IDs for ordering, ownership, migration, and fail-open/fail-closed outcomes.
- Permit any internal design that proves equivalent requirements, migration, and direct coverage.

### Upstream Equivalence Review

Review every requirement's ledger disposition and evidence. Do not repeat requirements in a generic
checklist. Confirm that configuration, success and failure paths, persisted state, cleanup, public
surfaces, and coverage are represented by ledger or inventory IDs.

### Retirement Criteria

State the evidence required to move this spec to `feature-specs/retired/`. Similar names or APIs are
not proof; every applicable ledger and inventory row must be upstream-equivalent or deliberately
removed. For a Retired spec, link the retirement commit and replacement evidence where applicable,
or explicit evidence for deliberate removal without replacement.

## Test Coverage

### Direct Coverage

The ledger's coverage column is canonical. Add only supporting test-design or fixture notes here,
keyed to requirement IDs; do not repeat targets or coverage status. A direct target must exercise
the feature and assert its requirement. Python paths stay under `tests/`, use a `test_*.py` filename,
and select a collectable `test_` function or a `test_` method on a collectable `Test*` class, never a
helper or source function:

```text
tests/path/test_file.py:test_fn
tests/path/test_file.py:TestClass::test_method
```

JavaScript and TypeScript selectors name the literal Vitest title in a repository-local
`*.{test,spec}.{js,jsx,ts,tsx,mjs,cjs}` file. An optional `suite >` prefix disambiguates nested
suites, and the final component must occur in a literal `test(...)` or `it(...)` call:

```text
web/src/lib/example.test.ts:test title
web/src/components/Example.spec.tsx:suite > test title
```

For a Retired spec, put applicable replacement targets and immutable historical tests in the
ledger; use this section only for explanatory evidence. If behavior was deliberately removed and no
current test applies, the ledger records `Not applicable — retired` and links the removal evidence.

### Required Coverage Backlog

The ledger is also canonical for missing coverage. Add only implementation notes keyed to linked
`missing:` and requirement IDs; do not redefine the ID or repeat its status.

- [missing:example-failure](#missing-example-failure) for
  [req:example-failure](#req-example-failure): note the fixture or test boundary to build.

### Verification Commands

List only feature-specific commands that exercise the current implementation, replacement, or
removal. CI runs the lightweight catalog checker unconditionally for every change; global catalog
and diff validation does not belong in an individual spec.

```bash
scripts/run_tests.sh tests/path/test_feature.py
```

### Test Generation Notes

Reference requirement or gap IDs when noting positive, negative, edge, concurrency, migration,
restart, and regression cases that future tests should consider.

## History

- Date — issue/PR/immutable commit — contract, status, or upstream-assessment change; reference
  affected requirement and inventory IDs.
