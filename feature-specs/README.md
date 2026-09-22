# Feature Specs

Feature specs are the source of truth for behavior that this fork adds on top of
[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent). They are maintenance
documents for implementation review, regression analysis, and upstream rebases. They complement
user and developer documentation; they do not replace it.

The feature ID comes from the filename. For example, `kanban-workspace-provider.md` defines
`kanban-workspace-provider`. Use one kebab-case Markdown file per independently assessable
fork feature. Keep subfeatures in the same file when they share one lifecycle or cannot be rebased
safely in isolation.

## When A Spec Is Required

Add or update a spec when a change:

- adds behavior that is not present upstream;
- changes a fork-owned contract, default, persisted field, lifecycle, or failure mode;
- moves an implementation or test entry point named by an existing spec;
- changes which parts of the feature remain necessary after an upstream sync; or
- fixes a regression where the intended fork behavior would otherwise be ambiguous.

The implementation and its spec should change in the same pull request. A spec records behavior and
invariants, not the current patch shape: during a rebase, adapt the implementation to upstream rather
than blindly replaying old hunks.

## Required Contents

Start from [`TEMPLATE.md`](TEMPLATE.md). Every spec must record:

- provenance: tracking issue, first implementation, implementation base, and latest upstream
  assessment;
- why the fork carries the feature, including goals and non-goals;
- observable behavior, defaults, state transitions, errors, and compatibility behavior;
- concrete implementation and documentation entry points;
- persisted state, ownership boundaries, and cleanup rules when applicable;
- invariants that must survive refactors and rebases;
- known places where current code does not yet satisfy the stated contract;
- a complete rebase surface and an upstream-equivalence checklist;
- exact test targets for existing coverage and explicit backlog for missing coverage; and
- the conditions under which the fork delta can be reduced or retired.

Use these status values:

- **Fork-only** — upstream has no equivalent behavior.
- **Partially upstreamed** — upstream covers part of the contract; the remaining delta is stated.
- **Upstreamed** — upstream covers the full contract, but removal of the fork implementation has not
  yet been verified and merged.
- **Retired** — no fork implementation remains. Retired specs may be removed in a later cleanup once
  history and replacement links are clear.

## Test Targets And Coverage Gaps

List test targets as plain repository-relative text so paths and function names remain searchable:

```text
tests/hermes_cli/test_example.py:test_behavior
```

Do not link test targets. If required behavior lacks direct coverage, end the item with
`missing:<stable-kebab-id>`. The ID should describe the behavior and remain stable until a
test replaces it:

```text
- A missing configured provider fails closed before spawn:
  missing:missing-provider-fails-closed
```

Generic tests may be useful supporting evidence, but a spec must not claim direct coverage unless the
named test exercises the fork feature and asserts the stated invariant.

## Upstream Rebase Workflow

For every upstream sync:

1. Record the upstream commit being assessed and compare it with the spec, not only with the old
   fork diff.
2. Search upstream for a native equivalent. Classify the feature as retain, adapt, partially retire,
   or retire.
3. Resolve every entry in the spec's rebase surface, including schema rebuild/migration lists,
   registration tables, cleanup paths, user surfaces, docs, and tests.
4. Re-check every invariant and explicit coverage gap. New upstream behavior may satisfy a gap or
   introduce a new terminal/error path.
5. Update the upstream assessment and delta inventory in the same sync change.
6. Run the spec's verification commands and inspect the final fork-vs-upstream diff for accidental
   loss or duplicated behavior.

An auto-merge is not evidence of semantic compatibility. Conversely, a textual conflict is not a
reason to preserve the old file shape when upstream now provides a better integration point.

## Feature Index

- [kanban-workspace-provider](kanban-workspace-provider.md)
