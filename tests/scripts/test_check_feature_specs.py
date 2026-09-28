"""Behavior tests for the feature-spec metadata checker."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_feature_specs.py"
SPEC = importlib.util.spec_from_file_location("check_feature_specs", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise ImportError("failed to load check_feature_specs.py")
CHECKER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CHECKER
SPEC.loader.exec_module(CHECKER)


REQUIRED_SECTIONS = """\
## Summary

Summary.

## Why This Fork Carries It

The fork needs this behavior.

## Behavior

The feature behaves predictably.

## Entry Points

- [Guide](../docs/guide.md#details)

## Requirement Status Ledger

| Requirement ID | Contract | Implementation | Upstream | Coverage |
| --- | --- | --- | --- | --- |
| <a id="req-main-behavior"></a> req:main-behavior | Predictable behavior. | Conforming — runtime evidence in [delta:example-core](#delta-example-core). | Retain — upstream evidence. | tests/test_feature.py:test_behavior |

## Persisted State And Compatibility

Not applicable because this fixture has no state.

## Invariants

- Behavior remains predictable.

## Known Implementation Gaps

None known.

## Rebase Assessment

Retain until upstream is equivalent.

### Delta Inventory

| Delta ID | Entry point | Responsibility |
| --- | --- | --- |
| <a id="delta-example-core"></a> delta:example-core | [Guide](../docs/guide.md) | Preserve behavior. |

## Test Coverage

- tests/test_feature.py:test_behavior

## History

- 2026-09-28 — created.
"""


def _write_repository(tmp_path: Path) -> Path:
    feature_dir = tmp_path / "feature-specs"
    feature_dir.mkdir()
    (feature_dir / "README.md").write_text(
        "# Feature Specs\n\n## Feature Index\n\n- [Example](example.md)\n",
        encoding="utf-8",
    )
    (feature_dir / "TEMPLATE.md").write_text("# Template\n", encoding="utf-8")
    (feature_dir / "example.md").write_text(
        "# Example\n\n## Fork Metadata\n\n- **Status:** Fork-only\n\n"
        + REQUIRED_SECTIONS,
        encoding="utf-8",
    )
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text(
        "# Guide\n\n## Details\n\nDetails.\n", encoding="utf-8"
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_feature.py").write_text(
        "def test_behavior():\n    assert True\n",
        encoding="utf-8",
    )
    return tmp_path


def _messages(root: Path) -> list[str]:
    return [issue.message for issue in CHECKER.check_repository(root)]


def _add_main_gap(root: Path, known_gaps: str = "") -> Path:
    spec = root / "feature-specs" / "example.md"
    text = spec.read_text(encoding="utf-8").replace(
        "Conforming — runtime evidence in [delta:example-core](#delta-example-core).",
        '<a id="gap-main"></a> gap:main — runtime evidence in '
        "[delta:example-core](#delta-example-core).",
        1,
    )
    if known_gaps:
        text = text.replace("None known.", known_gaps, 1)
    spec.write_text(text, encoding="utf-8")
    return spec


def _add_feature(
    root: Path,
    feature_id: str,
    *,
    retired: bool = False,
    status: str = "Fork-only",
) -> Path:
    feature_dir = root / "feature-specs"
    source = (feature_dir / "example.md").read_text(encoding="utf-8")
    text = (
        source.replace("# Example", f"# {feature_id}", 1)
        .replace("Fork-only", status, 1)
        .replace("main-behavior", f"{feature_id}-behavior")
        .replace("example-core", f"{feature_id}-core")
    )
    parent = feature_dir / "retired" if retired else feature_dir
    parent.mkdir(exist_ok=True)
    path = parent / f"{feature_id}.md"
    if retired:
        text = text.replace("../docs/guide.md", "../../docs/guide.md")
    path.write_text(text, encoding="utf-8")
    readme = feature_dir / "README.md"
    target = f"retired/{feature_id}.md" if retired else f"{feature_id}.md"
    readme.write_text(
        readme.read_text(encoding="utf-8") + f"- [{feature_id}]({target})\n",
        encoding="utf-8",
    )
    return path


def test_good_catalog_and_spec_pass(tmp_path):
    root = _write_repository(tmp_path)

    assert CHECKER.check_repository(root) == []


def test_directory_spec_companions_and_delta_ids_pass(tmp_path):
    root = _write_repository(tmp_path)
    feature_dir = root / "feature-specs"
    directory = feature_dir / "example"
    directory.mkdir()
    spec = feature_dir / "example.md"
    text = spec.read_text(encoding="utf-8").replace(
        "../docs/guide.md", "../../docs/guide.md"
    )
    (directory / "index.md").write_text(text, encoding="utf-8")
    spec.unlink()
    (feature_dir / "README.md").write_text(
        "# Feature Specs\n\n## Feature Index\n\n- [Example](example/index.md)\n",
        encoding="utf-8",
    )

    assert CHECKER.check_repository(root) == []


@pytest.mark.parametrize(
    ("readme", "expected"),
    [
        (
            "# Feature Specs\n\n## Feature Index\n\n- [Missing](missing.md)\n",
            "Feature Index target does not exist",
        ),
        (
            "# Feature Specs\n\n## Feature Index\n\n",
            "live spec is not indexed",
        ),
    ],
    ids=("missing-index-target", "unindexed-spec"),
)
def test_catalog_bijection_reports_missing_targets_and_specs(
    tmp_path, readme, expected
):
    root = _write_repository(tmp_path)
    (root / "feature-specs" / "README.md").write_text(readme, encoding="utf-8")

    assert any(expected in message for message in _messages(root))


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("Fork-only", "Experimental", "invalid status"),
        (
            "## Behavior",
            "## Observable Contract",
            "missing required heading `## Behavior`",
        ),
    ],
    ids=("bad-status", "missing-heading"),
)
def test_structure_rejects_bad_status_and_missing_heading(tmp_path, old, new, expected):
    root = _write_repository(tmp_path)
    spec = root / "feature-specs" / "example.md"
    spec.write_text(
        spec.read_text(encoding="utf-8").replace(old, new), encoding="utf-8"
    )

    assert any(expected in message for message in _messages(root))


def test_duplicate_gap_and_missing_definitions_fail_independently(tmp_path):
    root = _write_repository(tmp_path)
    spec = root / "feature-specs" / "example.md"
    text = spec.read_text(encoding="utf-8")
    row = (
        '| <a id="gap-race"></a> gap:race | [req:main-behavior](#req-main-behavior) '
        '| Open. | Fork-only. | <a id="missing-race"></a> missing:race |\n'
    )
    text = text.replace(
        "## Persisted State And Compatibility",
        row + row + "\n## Persisted State And Compatibility",
    )
    spec.write_text(text, encoding="utf-8")

    messages = _messages(root)
    assert any("duplicate gap definition `gap:race`" in message for message in messages)
    assert any(
        "duplicate missing definition `missing:race`" in message for message in messages
    )


def test_ids_are_unique_across_the_catalog(tmp_path):
    root = _write_repository(tmp_path)
    feature_dir = root / "feature-specs"
    original = (feature_dir / "example.md").read_text(encoding="utf-8")
    (feature_dir / "second.md").write_text(
        original.replace("# Example", "# Second", 1), encoding="utf-8"
    )
    readme = feature_dir / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8") + "- [Second](second.md)\n",
        encoding="utf-8",
    )

    assert any(
        "duplicate req definition `req:main-behavior`" in message
        for message in _messages(root)
    )


def test_requirement_row_requires_direct_or_missing_coverage(tmp_path):
    root = _write_repository(tmp_path)
    spec = root / "feature-specs" / "example.md"
    spec.write_text(
        spec.read_text(encoding="utf-8").replace(
            "tests/test_feature.py:test_behavior", "No direct coverage.", 1
        ),
        encoding="utf-8",
    )

    assert any(
        "`req:main-behavior` has no coverage disposition" in message
        for message in _messages(root)
    )


def test_linked_test_label_does_not_count_as_direct_coverage(tmp_path):
    root = _write_repository(tmp_path)
    spec = root / "feature-specs" / "example.md"
    spec.write_text(
        spec.read_text(encoding="utf-8").replace(
            "tests/test_feature.py:test_behavior",
            "[tests/missing/test_absent.py:test_absent](../docs/guide.md)",
            1,
        ),
        encoding="utf-8",
    )

    assert any(
        "`req:main-behavior` has no coverage disposition" in message
        for message in _messages(root)
    )


def test_directory_spec_must_link_every_companion(tmp_path):
    root = _write_repository(tmp_path)
    feature_dir = root / "feature-specs"
    directory = feature_dir / "example"
    directory.mkdir()
    spec = feature_dir / "example.md"
    (directory / "index.md").write_text(
        spec.read_text(encoding="utf-8").replace(
            "../docs/guide.md#details", "../../docs/guide.md#details"
        ),
        encoding="utf-8",
    )
    (directory / "notes.md").write_text("# Notes\n", encoding="utf-8")
    spec.unlink()
    (feature_dir / "README.md").write_text(
        "# Feature Specs\n\n## Feature Index\n\n- [Example](example/index.md)\n",
        encoding="utf-8",
    )

    assert any(
        "does not link companion `notes.md`" in message for message in _messages(root)
    )


def test_broken_relative_link_reports_the_source_document(tmp_path):
    root = _write_repository(tmp_path)
    spec = root / "feature-specs" / "example.md"
    spec.write_text(
        spec.read_text(encoding="utf-8").replace(
            "The feature behaves predictably.",
            "The feature behaves predictably. [Missing](../docs/not-there.md)",
        ),
        encoding="utf-8",
    )

    issues = CHECKER.check_repository(root)
    assert any(
        issue.path == "feature-specs/example.md"
        and "relative link target does not exist" in issue.message
        for issue in issues
    )


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("tests/no_such_file.py:test_behavior", "listed test file does not exist"),
        ("tests/test_feature.py:test_not_there", "listed test function does not exist"),
    ],
    ids=("missing-file", "missing-function"),
)
def test_missing_test_targets_are_rejected(tmp_path, target, expected):
    root = _write_repository(tmp_path)
    spec = root / "feature-specs" / "example.md"
    spec.write_text(
        spec.read_text(encoding="utf-8").replace(
            "tests/test_feature.py:test_behavior",
            target,
        ),
        encoding="utf-8",
    )

    assert any(expected in message for message in _messages(root))


def test_plain_test_target_requires_a_top_level_function(tmp_path):
    root = _write_repository(tmp_path)
    (root / "tests" / "test_feature.py").write_text(
        "def helper():\n    def test_nested():\n        assert True\n",
        encoding="utf-8",
    )
    spec = root / "feature-specs" / "example.md"
    spec.write_text(
        spec.read_text(encoding="utf-8").replace("test_behavior", "test_nested"),
        encoding="utf-8",
    )

    assert any(
        "listed test function does not exist" in message for message in _messages(root)
    )


def test_test_path_in_verification_command_is_checked_but_external_history_is_not(
    tmp_path,
):
    root = _write_repository(tmp_path)
    spec = root / "feature-specs" / "example.md"
    text = spec.read_text(encoding="utf-8")
    text += (
        "\n[tests/removed/test_old.py]"
        "(https://github.com/example/project/blob/0123456789/tests/removed/test_old.py)\n"
        "\n```bash\nscripts/run_tests.sh tests/missing/test_command.py\n```\n"
    )
    spec.write_text(text, encoding="utf-8")

    messages = _messages(root)
    assert any("tests/missing/test_command.py" in message for message in messages)
    assert not any("tests/removed/test_old.py" in message for message in messages)


@pytest.mark.parametrize(
    ("arrange", "expected"),
    [
        (
            lambda root: (root / "feature-specs" / "example.md").write_text(
                (root / "feature-specs" / "example.md")
                .read_text(encoding="utf-8")
                .replace("test_behavior", "helper"),
                encoding="utf-8",
            ),
            "Python selector is not collectable",
        ),
        (
            lambda root: (root / "feature-specs" / "example.md").write_text(
                (root / "feature-specs" / "example.md")
                .read_text(encoding="utf-8")
                .replace(
                    "tests/test_feature.py:test_behavior",
                    "src/test_feature.py:test_behavior",
                ),
                encoding="utf-8",
            ),
            "must be under `tests/`",
        ),
        (
            lambda root: (root / "feature-specs" / "example.md").write_text(
                (root / "feature-specs" / "example.md")
                .read_text(encoding="utf-8")
                .replace(
                    "tests/test_feature.py:test_behavior",
                    "tests/../../escape/test_feature.py:test_behavior",
                ),
                encoding="utf-8",
            ),
            "uses traversal",
        ),
        (
            lambda root: (root / "feature-specs" / "example.md").write_text(
                (root / "feature-specs" / "example.md")
                .read_text(encoding="utf-8")
                .replace(
                    "tests/test_feature.py:test_behavior",
                    "tests/helper.py:test_behavior",
                ),
                encoding="utf-8",
            ),
            "not a collectable `test_*.py` file",
        ),
        (
            lambda root: (root / "feature-specs" / "example.md").write_text(
                (root / "feature-specs" / "example.md")
                .read_text(encoding="utf-8")
                .replace(
                    "tests/test_feature.py:test_behavior",
                    "web/example.ts:rejects invalid input",
                ),
                encoding="utf-8",
            ),
            "not a `*.test.*` or `*.spec.*` file",
        ),
        (
            lambda root: (root / "feature-specs" / "example.md").write_text(
                (root / "feature-specs" / "example.md")
                .read_text(encoding="utf-8")
                .replace(
                    "tests/test_feature.py:test_behavior",
                    "web/example.test.ts:not the literal title",
                ),
                encoding="utf-8",
            ),
            "JavaScript test title does not exist",
        ),
    ],
    ids=(
        "python-helper",
        "python-outside-tests",
        "path-traversal",
        "python-helper-file",
        "javascript-implementation-file",
        "javascript-missing-title",
    ),
)
def test_direct_targets_reject_non_collectable_or_escaping_evidence(
    tmp_path, arrange, expected
):
    root = _write_repository(tmp_path)
    (root / "src").mkdir()
    (root / "src" / "test_feature.py").write_text(
        "def test_behavior():\n    assert True\n", encoding="utf-8"
    )
    (root / "tests" / "helper.py").write_text(
        "def test_behavior():\n    assert True\n", encoding="utf-8"
    )
    (root / "web").mkdir()
    (root / "web" / "example.ts").write_text(
        "test('rejects invalid input', () => {})\n", encoding="utf-8"
    )
    (root / "web" / "example.test.ts").write_text(
        "test('rejects invalid input', () => {})\n", encoding="utf-8"
    )
    arrange(root)

    assert any(expected in message for message in _messages(root))


@pytest.mark.parametrize("kind", ["function", "class-method", "javascript"])
def test_documented_python_and_javascript_target_forms_pass(tmp_path, kind):
    root = _write_repository(tmp_path)
    target = "tests/test_feature.py:test_behavior"
    if kind == "class-method":
        (root / "tests" / "test_feature.py").write_text(
            "class TestFeature:\n    def test_behavior(self):\n        assert True\n",
            encoding="utf-8",
        )
        target = "tests/test_feature.py:TestFeature::test_behavior"
    elif kind == "javascript":
        web = root / "web"
        web.mkdir()
        (web / "example.test.ts").write_text(
            "describe('validation', () => {\n"
            "  it('rejects invalid input', () => {})\n"
            "})\n",
            encoding="utf-8",
        )
        target = "web/example.test.ts:validation > rejects invalid input"
    spec = root / "feature-specs" / "example.md"
    spec.write_text(
        spec.read_text(encoding="utf-8").replace(
            "tests/test_feature.py:test_behavior", target
        ),
        encoding="utf-8",
    )

    assert CHECKER.check_repository(root) == []


@pytest.mark.parametrize(
    "source",
    [
        "test.skip('disabled test', () => {})\n",
        "it.todo('disabled test')\n",
        "// test('disabled test', () => {})\n",
        "/*\ntest('disabled test', () => {})\n*/\n",
        (
            "describe.skip('disabled suite', () => {\n"
            "  test('disabled test', () => {})\n"
            "})\n"
        ),
    ],
    ids=("skip", "todo", "line-comment", "block-comment", "disabled-suite"),
)
def test_javascript_target_rejects_disabled_or_commented_tests(tmp_path, source):
    root = _write_repository(tmp_path)
    web = root / "web"
    web.mkdir()
    (web / "example.test.ts").write_text(source, encoding="utf-8")
    spec = root / "feature-specs" / "example.md"
    spec.write_text(
        spec.read_text(encoding="utf-8").replace(
            "tests/test_feature.py:test_behavior",
            "web/example.test.ts:disabled test",
        ),
        encoding="utf-8",
    )

    assert any(
        "listed JavaScript test title does not exist" in message
        for message in _messages(root)
    )


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (
            "__test__ = False\n\ndef test_behavior():\n    assert True\n",
            "tests/test_feature.py:test_behavior",
        ),
        (
            "def test_behavior():\n    assert True\n\n"
            "test_behavior.__test__ = False\n",
            "tests/test_feature.py:test_behavior",
        ),
        (
            "class TestFeature:\n"
            "    __test__ = False\n\n"
            "    def test_behavior(self):\n"
            "        assert True\n",
            "tests/test_feature.py:TestFeature::test_behavior",
        ),
        (
            "class TestFeature:\n"
            "    def test_behavior(self):\n"
            "        assert True\n\n"
            "TestFeature.test_behavior.__test__ = False\n",
            "tests/test_feature.py:TestFeature::test_behavior",
        ),
    ],
    ids=("module", "function", "class", "method"),
)
def test_python_target_rejects_static_test_collection_opt_out(
    tmp_path, source, target
):
    root = _write_repository(tmp_path)
    (root / "tests" / "test_feature.py").write_text(source, encoding="utf-8")
    spec = root / "feature-specs" / "example.md"
    spec.write_text(
        spec.read_text(encoding="utf-8").replace(
            "tests/test_feature.py:test_behavior", target
        ),
        encoding="utf-8",
    )

    assert any("disabled by `__test__ = False`" in message for message in _messages(root))


def test_html_comments_and_markdown_links_are_not_test_evidence(tmp_path):
    root = _write_repository(tmp_path)
    spec = root / "feature-specs" / "example.md"
    spec.write_text(
        spec.read_text(encoding="utf-8")
        + "\n<!-- tests/missing/test_hidden.py:test_hidden -->\n"
        + "[old test](../tests/missing/test_linked.py)\n",
        encoding="utf-8",
    )

    messages = _messages(root)
    assert not any("test_hidden" in message for message in messages)
    assert not any(
        "listed test" in message and "test_linked" in message for message in messages
    )


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("live-retired", "live feature spec status must not be `Retired`"),
        ("retired-live", "retired tombstone status must be `Retired`"),
        ("outside-metadata", "expected exactly one `**Status:**` metadata value"),
        ("duplicate-live-forms", "duplicate feature ID `example`"),
        ("duplicate-live-retired", "duplicate feature ID `example`"),
        ("bad-feature-id", "invalid feature ID `Bad_Name`"),
    ],
)
def test_feature_identity_and_status_follow_catalog_location(
    tmp_path, mutation, expected
):
    root = _write_repository(tmp_path)
    feature_dir = root / "feature-specs"
    spec = feature_dir / "example.md"
    if mutation == "live-retired":
        spec.write_text(
            spec.read_text(encoding="utf-8").replace("Fork-only", "Retired", 1),
            encoding="utf-8",
        )
    elif mutation == "retired-live":
        _add_feature(root, "old-feature", retired=True, status="Fork-only")
    elif mutation == "outside-metadata":
        spec.write_text(
            spec.read_text(encoding="utf-8")
            .replace("- **Status:** Fork-only", "- **Tracking:** fixture", 1)
            .replace("## Summary", "## Summary\n\n- **Status:** Fork-only", 1),
            encoding="utf-8",
        )
    elif mutation == "duplicate-live-forms":
        directory = feature_dir / "example"
        directory.mkdir()
        (directory / "index.md").write_text(
            spec.read_text(encoding="utf-8").replace(
                "../docs/guide.md", "../../docs/guide.md"
            ),
            encoding="utf-8",
        )
        readme = feature_dir / "README.md"
        readme.write_text(
            readme.read_text(encoding="utf-8")
            + "- [Example directory](example/index.md)\n",
            encoding="utf-8",
        )
    elif mutation == "duplicate-live-retired":
        _add_feature(root, "example", retired=True, status="Retired")
    else:
        bad = feature_dir / "Bad_Name.md"
        spec.rename(bad)
        readme = feature_dir / "README.md"
        readme.write_text(
            readme.read_text(encoding="utf-8").replace("example.md", "Bad_Name.md"),
            encoding="utf-8",
        )

    assert any(expected in message for message in _messages(root))


def test_retired_directory_uses_parent_as_feature_id(tmp_path):
    root = _write_repository(tmp_path)
    flat = _add_feature(root, "old-feature", retired=True, status="Retired")
    directory = flat.parent / "old-feature"
    directory.mkdir()
    index = directory / "index.md"
    index.write_text(
        flat.read_text(encoding="utf-8").replace(
            "../../docs/guide.md", "../../../docs/guide.md"
        ),
        encoding="utf-8",
    )
    flat.unlink()
    readme = root / "feature-specs" / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8").replace(
            "retired/old-feature.md", "retired/old-feature/index.md"
        ),
        encoding="utf-8",
    )

    assert CHECKER.check_repository(root) == []


def test_github_heading_slug_collisions_are_resolved_globally(tmp_path):
    root = _write_repository(tmp_path)
    spec = root / "feature-specs" / "example.md"
    spec.write_text(
        spec.read_text(encoding="utf-8")
        + "\n## Repeat\n\n## Repeat-1\n\n## Repeat\n\n"
        + "[third collision](#repeat-2)\n",
        encoding="utf-8",
    )

    assert CHECKER.check_repository(root) == []


def test_anchor_cache_is_shared_across_link_checks(tmp_path, monkeypatch):
    root = _write_repository(tmp_path)
    spec = root / "feature-specs" / "example.md"
    spec.write_text(
        spec.read_text(encoding="utf-8")
        .replace("Summary.", "[Guide again](../docs/guide.md#details).", 1),
        encoding="utf-8",
    )
    original = CHECKER._markdown_anchors
    calls = []

    def counted(path):
        calls.append(path)
        return original(path)

    monkeypatch.setattr(CHECKER, "_markdown_anchors", counted)
    assert CHECKER.check_repository(root) == []
    assert calls.count(root / "docs" / "guide.md") == 1


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        (
            "| <a id=\"req-main-behavior\"></a> req:main-behavior | Predictable behavior. | Conforming — runtime evidence in [delta:example-core](#delta-example-core). | Retain — upstream evidence. | tests/test_feature.py:test_behavior |",
            "| <a id=\"req-main-behavior\"></a> req:main-behavior | Predictable behavior. | Conforming. | Fork-only. |",
            "requirement ledger row has 4 columns; expected 5",
        ),
        ("Conforming — runtime evidence", "Implemented — runtime evidence", "implementation column must start"),
        ("Retain — upstream evidence.", "Fork-only — no upstream.", "invalid upstream disposition"),
        ("Retain — upstream evidence.", "Retain", "invalid upstream disposition"),
        ("Retain — upstream evidence.", "Retain; upstream evidence.", "invalid upstream disposition"),
        ("Retain — upstream evidence.", "Retain — !!!", "invalid upstream disposition"),
        ("[delta:example-core](#delta-example-core)", "runtime code", "must reference at least one `delta:` ID"),
    ],
    ids=(
        "column-count",
        "implementation-status",
        "upstream-status",
        "upstream-evidence-required",
        "upstream-semicolon-rejected",
        "upstream-punctuation-rejected",
        "delta-required",
    ),
)
def test_requirement_ledger_enforces_column_contracts(tmp_path, old, new, expected):
    root = _write_repository(tmp_path)
    spec = root / "feature-specs" / "example.md"
    spec.write_text(
        spec.read_text(encoding="utf-8").replace(old, new, 1), encoding="utf-8"
    )

    assert any(expected in message for message in _messages(root))


def test_requirement_ledger_parses_escaped_and_code_span_pipes(tmp_path):
    root = _write_repository(tmp_path)
    spec = root / "feature-specs" / "example.md"
    spec.write_text(
        spec.read_text(encoding="utf-8").replace(
            "Predictable behavior.", r"Predictable `left|right` behavior with an escaped \| pipe.", 1
        ),
        encoding="utf-8",
    )

    assert CHECKER.check_repository(root) == []


@pytest.mark.parametrize(
    ("kind", "column", "expected_column"),
    [("gap", 4, 3), ("missing", 3, 5)],
)
def test_canonical_gap_and_missing_definitions_are_cell_scoped(
    tmp_path, kind, column, expected_column
):
    root = _write_repository(tmp_path)
    spec = root / "feature-specs" / "example.md"
    text = spec.read_text(encoding="utf-8")
    row = next(line for line in text.splitlines() if "req-main-behavior" in line)
    cells = list(CHECKER._markdown_table_cells(row))
    cells[column] += f' <a id="{kind}-wrong-cell"></a> {kind}:wrong-cell'
    replacement = "| " + " | ".join(cells) + " |"
    spec.write_text(text.replace(row, replacement), encoding="utf-8")

    assert any(
        f"canonical `{kind}:` definitions belong only in column {expected_column}"
        in message
        for message in _messages(root)
    )


def test_requirement_delta_must_be_declared_by_same_feature(tmp_path):
    root = _write_repository(tmp_path)
    _add_feature(root, "second")
    spec = root / "feature-specs" / "example.md"
    spec.write_text(
        spec.read_text(encoding="utf-8").replace(
            "[delta:example-core](#delta-example-core)",
            "[delta:second-core](second.md#delta-second-core)",
            1,
        ),
        encoding="utf-8",
    )

    assert any(
        "implementation cell has no declared delta from the same feature" in message
        for message in _messages(root)
    )


def test_gap_implementation_status_must_begin_with_canonical_definition(tmp_path):
    root = _write_repository(tmp_path)
    spec = _add_main_gap(
        root,
        "- [gap:main](#gap-main) affects "
        "[req:main-behavior](#req-main-behavior) in "
        "[delta:example-core](#delta-example-core).",
    )
    spec.write_text(
        spec.read_text(encoding="utf-8").replace(
            '<a id="gap-main"></a> gap:main — runtime evidence',
            'Implemented garbage <a id="gap-main"></a> gap:main — runtime evidence',
            1,
        ),
        encoding="utf-8",
    )

    assert any(
        "implementation gap status must begin with its canonical `gap:` definition"
        in message
        for message in _messages(root)
    )


def test_canonical_gap_with_one_owned_known_gap_item_passes(tmp_path):
    root = _write_repository(tmp_path)
    _add_main_gap(
        root,
        "- [gap:main](#gap-main) affects "
        "[req:main-behavior](#req-main-behavior) in "
        "[delta:example-core](#delta-example-core).",
    )

    assert CHECKER.check_repository(root) == []


@pytest.mark.parametrize(
    ("known_gaps", "history", "expected"),
    [
        (
            "",
            "[gap:main](#gap-main) was recorded historically.",
            "must appear exactly once as a Known Implementation Gaps item; found 0",
        ),
        (
            "- [delta:example-core](#delta-example-core) has an issue.",
            "",
            "must link exactly one canonical `gap:`",
        ),
        (
            "- [gap:main](#gap-main) is in "
            "[delta:example-core](#delta-example-core).",
            "",
            "must link owning `req:main-behavior`",
        ),
        (
            "- [gap:main](#gap-main) affects "
            "[req:main-behavior](#req-main-behavior) in "
            "[delta:example-core](#delta-example-core).\n"
            "- [gap:main](#gap-main) is duplicated for "
            "[req:main-behavior](#req-main-behavior) in "
            "[delta:example-core](#delta-example-core).",
            "",
            "must appear exactly once as a Known Implementation Gaps item; found 2",
        ),
    ],
    ids=("history-only", "delta-only", "missing-owner", "duplicate-item"),
)
def test_every_gap_requires_one_owned_known_gap_item(
    tmp_path, known_gaps, history, expected
):
    root = _write_repository(tmp_path)
    spec = _add_main_gap(root, known_gaps)
    if history:
        spec.write_text(
            spec.read_text(encoding="utf-8").replace(
                "- 2026-09-28 — created.",
                f"- 2026-09-28 — created. {history}",
            ),
            encoding="utf-8",
        )

    assert any(expected in message for message in _messages(root))


def test_known_gap_items_require_a_same_feature_delta(tmp_path):
    root = _write_repository(tmp_path)
    spec = _add_main_gap(
        root,
        "- [gap:main](#gap-main) affects [req:main-behavior](#req-main-behavior).",
    )

    assert any(
        "Known Implementation Gaps item must reference at least one `delta:` ID"
        in message
        for message in _messages(root)
    )


def test_canonical_gap_and_missing_ids_must_be_referenced(tmp_path):
    root = _write_repository(tmp_path)
    spec = root / "feature-specs" / "example.md"
    text = spec.read_text(encoding="utf-8")
    text = text.replace(
        "Conforming — runtime evidence in [delta:example-core](#delta-example-core).",
        '<a id="gap-main"></a> gap:main — evidence in [delta:example-core](#delta-example-core).',
        1,
    ).replace(
        "tests/test_feature.py:test_behavior",
        '<a id="missing-main"></a> missing:main',
        1,
    )
    spec.write_text(text, encoding="utf-8")

    messages = _messages(root)
    assert any("canonical `gap:main` is never referenced" in message for message in messages)
    assert any(
        "canonical `missing:main` is never referenced" in message
        for message in messages
    )
