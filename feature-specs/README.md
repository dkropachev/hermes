# Feature Specs

Feature specs are the source of truth for behavior that this fork adds on top of
[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent). They support
implementation review, regression analysis, and upstream rebases. They complement user and
developer documentation; they do not replace it.

## Catalog Layout

A feature ID is stable, unique, and kebab-case. A normal spec is
`feature-specs/<feature-id>.md`. A large, tightly coupled feature may instead use
`feature-specs/<feature-id>/index.md`, with material split as needed into `ledger.md`, `rebase.md`,
and `tests.md`. The directory is one feature: its `index.md` links every companion, and the Feature
Index contains one entry for the feature, pointing to `index.md`.

Keep coupled subfeatures together when they share a lifecycle or cannot be rebased safely in
isolation. Subfeatures reference requirement and Delta Inventory IDs; they do not repeat contracts
or internal paths. A single-file spec longer than 500 lines must include a linked table of contents.

Retired specs move to `feature-specs/retired/<feature-id>.md` or
`feature-specs/retired/<feature-id>/index.md` and remain in the Feature Index as one-feature
tombstones. Directory companions move with their index. Do not delete provenance or retirement
evidence.

## When A Spec Is Required

Add or update a spec when a change:

- adds behavior that is not present upstream;
- changes a fork-owned contract, default, persisted field, lifecycle, or failure mode;
- moves an implementation or test entry point named by an existing spec;
- changes which parts of the feature remain necessary after an upstream sync; or
- fixes a regression where the intended fork behavior would otherwise be ambiguous.

The implementation and its spec change in the same pull request. The one bootstrap exception is a
docs-only backfill for a pre-existing, unspecced fork feature. A backfill must cite immutable first
implementation and base commits and assess the current implementation. Once cataloged, that feature
follows the same-pull-request rule.

## Contract And Status Model

Specs define observable outcomes and invariants, not one mandatory implementation. Detailed
algorithms, internal state names, and sequencing are non-exclusive reference designs unless the spec
explicitly identifies them as public or persisted compatibility contracts. An alternative conforms
when it proves the same outcomes, migrations, and direct tests.

Each spec has one Requirement Status Ledger. It keeps three independent facts separate:

- `<a id="req-example"></a> req:example` defines one stable normative requirement identity and its
  sole canonical contract or reference.
- `<a id="gap-example"></a> gap:example` identifies a verified defect in the live implementation.
  It is not a test status.
- `<a id="missing-example"></a> missing:example` identifies missing direct test coverage. It does
  not imply that implementation is defective.

Canonical definitions use those anchors exactly once, in the ledger. Elsewhere, link the ID (for
example, `[req:example](#req-example)`) instead of restating its contract. A gap and a missing test
may share a suffix, but they remain independent records and are resolved independently. Each ID is
catalog-unique; prefix its suffix with the feature ID when a shorter name could collide. In a split
spec, references include the companion path, such as `[req:example](ledger.md#req-example)`.

The ledger is a five-column table with one row per requirement:

1. **Requirement ID** defines exactly one canonical, anchored `req:` ID.
2. **Normative contract/reference** contains the sole contract or links its sole definition in
   Behavior.
3. **Implementation status/evidence** is exactly `Conforming — <evidence>; <linked delta IDs>` or
   `<canonical anchored gap definitions> — <evidence>; <linked delta IDs>`. It never copies an
   internal path.
4. **Upstream disposition/evidence** starts with exactly one of `Retain`, `Adapt`, `Upstream`,
   `Deliberately removed`, or `Not applicable`, followed by evidence.
5. **Coverage (direct/missing)** contains one or both of: direct test targets and canonical,
   anchored `missing:` definitions owned by that same requirement row.

Do not substitute statuses such as `Implemented`, `Partial`, or `Fork-only` in ledger cells. The
feature's overall status follows the ledger:

- **Fork-only** — upstream has no equivalent behavior.
- **Partially upstreamed** — upstream covers part of the contract; the remaining delta is stated.
- **Upstreamed** — upstream covers the full contract, but removal of the fork implementation has not
  yet been verified and merged.
- **Retired** — no fork implementation remains.

## Required Contents

Start from [feature-specs/TEMPLATE.md](TEMPLATE.md). Every live spec records:

- immutable provenance and the latest upstream assessment;
- goals, non-goals, observable behavior, compatibility contracts, and stable requirements;
- a requirement ledger with separate implementation, upstream, and coverage status;
- public API and user-facing entry points;
- persisted state, ownership, migration, and cleanup rules where applicable;
- one canonical Delta Inventory of internal paths and responsibilities;
- verified implementation gaps, rebase rules, retirement criteria, and feature-specific tests.

The Delta Inventory is the sole internal implementation path and responsibility map. Give every row
a stable `delta:<kebab-id>` anchor. Entry Points lists only public API, CLI/tool/UI, and user or
developer documentation surfaces. Known Implementation Gaps links the owning `gap:`, `req:`, and
`delta:` IDs without raw internal paths. All supporting sections link canonical ledger and inventory
IDs; they never redefine IDs, contracts, statuses, or internal path maps.

When a feature evolves a public API, specify how the change remains optional or additive for
existing callers, or document an explicit versioned migration. Do not describe a newly required
argument as backward-compatible.

For a **Retired** spec, replace live paths with commit-pinned historical links and cite the
retirement/removal commit. Record current upstream or replacement locations where applicable. A
deliberate removal with no replacement is valid when the tombstone says so explicitly and links the
evidence that every applicable invariant, lifecycle path, migration, and cleanup obligation was
resolved. Mark inapplicable live-gap, delta, and coverage material as retired with a reason.

## Links And Test Targets

Local links use document-relative destinations and repository-relative labels. Adjust the number of
`..` segments to the document's location:

```markdown
[tools/example.py](../tools/example.py)
```

List direct test targets as plain repository-relative text so paths and selectors remain searchable.
Python targets use one of these forms:

```text
tests/hermes_cli/test_example.py:test_behavior
tests/hermes_cli/test_example.py:TestExample::test_failure
```

The path must remain under `tests/` and its filename must begin with `test_`. The selector must name
a collectable `test_` function or a `test_` method on a collectable `Test*` class. A helper or source
function is not a test target.

JavaScript and TypeScript targets use a repository-local `*.test.*` or `*.spec.*` filename with an
extension of `js`, `jsx`, `ts`, `tsx`, `mjs`, or `cjs`, followed by the literal Vitest test title:

```text
web/src/lib/example.test.ts:rejects an invalid value
web/src/components/Example.spec.tsx:validation > rejects an invalid value
```

The optional `suite >` prefix disambiguates nested suites; the final title component must occur in
a literal `test(...)` or `it(...)` call. Do not use a helper name or source symbol as the selector.
Do not link test targets. Generic tests may be supporting evidence, but direct coverage requires a
collectable, named test that exercises the fork feature and asserts the stated requirement. Define a
canonical `missing:` ID in the same ledger row when direct coverage is absent.

## Upstream Rebase Workflow

For every upstream sync:

1. Record the immutable upstream commit being assessed and compare it with the requirements, not
   only with the old fork diff.
2. Classify every ledger row as retain, adapt, upstream, deliberately removed, or not applicable,
   with evidence.
3. Resolve every Delta Inventory row, including internal registrations, schema and migrations,
   cleanup paths, and responsibilities.
4. Reconcile public Entry Points, implementation gaps, and ledger coverage/test targets
   independently.
5. Update the assessment, ledger, and inventory in the same sync change.
6. Run the feature-specific verification commands and the catalog checker:

   ```bash
   python3 scripts/check_feature_specs.py
   ```

An auto-merge is not evidence of semantic compatibility. A textual conflict is not a reason to
preserve an old internal design when another design proves the same contract.

CI runs this lightweight checker unconditionally for every change, not only when documentation or
feature specs change. The checker enforces the catalog, required headings, five-column ledger,
allowed feature statuses and dispositions, canonical IDs and references, local links, and direct
test targets.

## Feature Index

- [kanban-workspace-provider](kanban-workspace-provider.md)
