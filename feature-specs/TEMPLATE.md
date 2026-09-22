# Feature Name

## Fork Metadata

- **Status:** Fork-only | Partially upstreamed | Upstreamed | Retired
- **Tracking:** Link the issue or design record.
- **First implementation:** Link the pull request and immutable commit.
- **Implementation base:** Link the upstream commit on which the feature was first applied.
- **Latest upstream assessment:** Record the upstream commit, assessment date, and conclusion.

## Summary

State the user-facing purpose and the smallest complete contract for the feature.

## Why This Fork Carries It

Explain the problem that upstream behavior does not solve and why this feature is the chosen
boundary.

### Goals

- List intended outcomes.

### Non-Goals

- List adjacent behavior deliberately owned elsewhere.

## Behavior

Describe observable behavior, terminology, defaults, configuration, state transitions, ordering,
error handling, compatibility, and security boundaries. Use third-level headings for distinct
lifecycle phases or surfaces.

## Entry Points

Link to the main implementation, public surface, and product/developer documentation using
repository-relative links. Replace the placeholder below with a real link.

- `path/to/implementation.py`

## Subfeatures

Keep coupled subfeatures in this document.

### Subfeature Name

#### Entry Points

- `path/to/subfeature.py`

#### Invariants

- State the subfeature-specific behavior that must survive a rebase.

## Persisted State And Compatibility

List database fields, files, serialized values, migration/rebuild locations, defaults for old data,
identity/ownership data, and downgrade or transfer behavior. Write `Not applicable` with a
reason when the feature has no persisted state.

## Invariants

- List concise, testable behavior that must remain true across refactors and upstream rebases.

## Known Implementation Gaps

List verified cases where the current implementation falls short of the required behavior. Name the
responsible symbol/path, the unsafe or incompatible outcome, the intended fix boundary, and a stable
`missing:<id>` test backlog entry. Do not weaken the contract to match a bug. Write
`None known` only after comparing the implementation with every invariant.

## Rebase Assessment

### Delta Inventory

List every file or subsystem currently carrying the feature and its responsibility. Prefer stable
symbols and responsibilities over line numbers.

| Area    | Current entry points | Responsibility                |
| ------- | -------------------- | ----------------------------- |
| Example | `path/to/file.py`    | State what must be preserved. |

### Conflict-Resolution Rules

- Explain how to adapt the feature if upstream moves or replaces an integration point.
- Call out ordering, ownership, data migration, and fail-open/fail-closed decisions.

### Upstream Equivalence Checklist

- [ ] Configuration and default behavior are equivalent.
- [ ] Success, contention, error, restart, and terminal paths are equivalent.
- [ ] Persisted identity and migration behavior are equivalent.
- [ ] User, plugin, CLI/API/tool, and documentation surfaces are equivalent.
- [ ] Existing and missing test cases have been reconciled.

### Retirement Criteria

State the evidence required before fork code can be removed. Similar names or APIs are not enough;
compare every invariant and lifecycle path.

## Test Coverage

### Direct Coverage

- Behavior is covered:
  `tests/path/test_feature.py:test_behavior`

### Required Coverage Backlog

- Untested required behavior:
  `missing:untested-required-behavior`

### Verification Commands

```bash
scripts/run_tests.sh tests/path/test_feature.py
git diff --check HEAD
```

## Test Generation Notes

Describe positive, negative, edge, concurrency, migration, restart, and regression cases that future
tests should consider.

## History

- Date — issue/PR/commit — what changed in the contract or upstream assessment.
