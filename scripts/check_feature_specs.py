#!/usr/bin/env python3
"""Validate the fork feature-spec catalog and its machine-checkable evidence.

The checker deliberately uses a small, documented Markdown subset instead of a
Markdown dependency:

* live specs are ``feature-specs/*.md`` (except README/TEMPLATE) and nested
  ``index.md`` files; documents below ``feature-specs/retired`` are tombstones;
* ``req``, ``gap``, and ``missing`` IDs are defined once in the requirement
  ledger; ``delta`` IDs are defined once in the Delta Inventory. Definitions
  use ``<a id="req-example"></a> req:example`` and later uses must be links
  such as ``[req:example](#req-example)``;
* the requirement ledger is a five-column table. Its implementation and
  coverage cells carry canonical ``gap``/``missing`` definitions, while test
  targets name a collectable Python or JavaScript/TypeScript test;
* inline Markdown links are checked outside code spans/fences. HTTP(S), mailto,
  and other URI-scheme links are intentionally external and are not fetched.

Run from anywhere with ``python scripts/check_feature_specs.py``. An optional
repository-root argument exists for fixture-based tests and downstream users.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit


DEFAULT_ROOT = Path(__file__).resolve().parent.parent
FEATURE_DIR = "feature-specs"
REQUIRED_HEADINGS = (
    "Fork Metadata",
    "Summary",
    "Why This Fork Carries It",
    "Behavior",
    "Entry Points",
    "Requirement Status Ledger",
    "Persisted State And Compatibility",
    "Invariants",
    "Known Implementation Gaps",
    "Rebase Assessment",
    "Test Coverage",
    "History",
)
ALLOWED_STATUSES = {"Fork-only", "Partially upstreamed", "Upstreamed", "Retired"}
ALLOWED_UPSTREAM_DISPOSITIONS = (
    "Retain",
    "Adapt",
    "Upstream",
    "Deliberately removed",
    "Not applicable",
)

_H2_RE = re.compile(r"^##\s+(.+?)\s*#*\s*$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_STATUS_RE = re.compile(r"^\s*[-*]\s+\*\*Status:\*\*\s*(.*?)\s*$")
_LINK_RE = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)]+)\)")
_ID_KINDS = "req|gap|missing|delta"
_ID_RAW_RE = re.compile(rf"(?<![A-Za-z0-9_-])({_ID_KINDS}):([^\s|<>]*)")
_ID_LINK_RE = re.compile(
    rf"\[({_ID_KINDS}):([a-z0-9]+(?:-[a-z0-9]+)*)\]\("
    r"([^)]*)\)"
)
_ID_ANCHOR_RE = re.compile(
    rf"<a\s+id=[\"']({_ID_KINDS})-([^\"']+)[\"']\s*></a>",
)
_VALID_SUFFIX_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_VALID_ID_OCCURRENCE_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(req|gap|missing):"
    r"([a-z0-9]+(?:-[a-z0-9]+)*)(?![A-Za-z0-9_-])"
)
_PATH_CHARS = r"A-Za-z0-9_.@+/\\-"
_PY_TEST_TARGET_RE = re.compile(
    rf"(?<![{_PATH_CHARS}])(?P<path>[{_PATH_CHARS}]+\.py):"
    r"(?P<selector>[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)?)"
)
_JS_TEST_TARGET_START_RE = re.compile(
    rf"(?<![{_PATH_CHARS}])(?P<path>[{_PATH_CHARS}]+\.(?:js|jsx|ts|tsx|mjs|cjs)):"
)
_PY_TEST_PATH_RE = re.compile(
    rf"(?<![{_PATH_CHARS}])(?P<path>(?:tests[/\\][{_PATH_CHARS}]*|[{_PATH_CHARS}]*/tests[/\\][{_PATH_CHARS}]*)\.py)"
)
_JS_TEST_PATH_RE = re.compile(
    rf"(?<![{_PATH_CHARS}])(?P<path>[{_PATH_CHARS}]+\.(?:test|spec)\.(?:js|jsx|ts|tsx|mjs|cjs))"
)
_JS_TEST_FILENAME_RE = re.compile(
    r"^.+\.(?:test|spec)\.(?:js|jsx|ts|tsx|mjs|cjs)$", re.IGNORECASE
)
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


@dataclass(frozen=True, order=True)
class Issue:
    """One actionable validation failure."""

    path: str
    line: int
    message: str

    def render(self) -> str:
        location = f"{self.path}:{self.line}" if self.line else self.path
        return f"{location}: {self.message}"


@dataclass(frozen=True)
class Definition:
    kind: str
    suffix: str
    path: Path
    line: int
    row: str

    @property
    def token(self) -> str:
        return f"{self.kind}:{self.suffix}"


@dataclass(frozen=True)
class LedgerRow:
    """One data row in a Requirement Status Ledger table."""

    path: Path
    line: int
    cells: tuple[str, ...]


@dataclass(frozen=True)
class TestTarget:
    """A direct, repository-local test target found in prose or a table cell."""

    path: str
    selector: str
    kind: str


@dataclass
class PythonTestNodes:
    """Collectable and statically disabled Python pytest nodes."""

    functions: set[str]
    methods: dict[str, set[str]]
    disabled_functions: set[str]
    disabled_methods: dict[str, set[str]]
    parse_error: str | None = None


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _strip_html_comments(text: str) -> str:
    """Remove HTML comments while preserving source line numbers."""

    def blank(match: re.Match[str]) -> str:
        return "\n" * match.group(0).count("\n")

    return re.sub(r"<!--.*?(?:-->|$)", blank, text, flags=re.DOTALL)


def _visible_lines(text: str) -> list[tuple[int, str]]:
    """Return non-fenced, non-comment Markdown lines."""

    visible: list[tuple[int, str]] = []
    fence: tuple[str, int] | None = None
    for number, original in enumerate(_strip_html_comments(text).splitlines(), 1):
        stripped = original.lstrip()
        if fence:
            if re.match(rf"^\s*{re.escape(fence[0])}{{{fence[1]},}}\s*$", original):
                fence = None
            continue
        match = re.match(r"^\s*(`{3,}|~{3,})", original)
        if match:
            marker = match.group(1)
            fence = (marker[0], len(marker))
            continue

        visible.append((number, original))
    return visible


def _without_inline_code(line: str) -> str:
    """Blank inline code so pseudo-links in examples are not interpreted."""

    result: list[str] = []
    cursor = 0
    while cursor < len(line):
        if line[cursor] != "`":
            result.append(line[cursor])
            cursor += 1
            continue
        end_marker = cursor
        while end_marker < len(line) and line[end_marker] == "`":
            end_marker += 1
        marker = line[cursor:end_marker]
        end = line.find(marker, end_marker)
        if end < 0:
            result.append(line[cursor])
            cursor += 1
            continue
        result.append(" " * (end + len(marker) - cursor))
        cursor = end + len(marker)
    return "".join(result)


def _without_external_markdown_links(line: str) -> str:
    """Blank external links so historical test labels are not local targets."""

    chars = list(line)
    for match in _LINK_RE.finditer(line):
        destination = _link_destination(match.group(2))
        parsed = urlsplit(destination)
        if parsed.scheme or parsed.netloc or _SCHEME_RE.match(destination):
            chars[match.start() : match.end()] = " " * (match.end() - match.start())
    return "".join(chars)


def _without_markdown_links(line: str) -> str:
    """Blank every Markdown link before scanning for plain test evidence."""

    chars = list(line)
    for match in _LINK_RE.finditer(line):
        chars[match.start() : match.end()] = " " * (match.end() - match.start())
    return "".join(chars)


def _section_bounds(
    lines: list[tuple[int, str]], heading: str
) -> tuple[int, int] | None:
    start: int | None = None
    end = len(lines)
    for index, (_, line) in enumerate(lines):
        match = _H2_RE.match(line)
        if not match:
            continue
        if start is not None:
            end = index
            break
        if match.group(1).strip() == heading:
            start = index + 1
    return None if start is None else (start, end)


def _any_level_section_bounds(
    lines: list[tuple[int, str]], heading: str
) -> tuple[int, int] | None:
    """Find a heading section, ending at the next same-or-higher heading."""

    start: int | None = None
    level: int | None = None
    for index, (_, line) in enumerate(lines):
        match = _HEADING_RE.match(line)
        if not match:
            continue
        current_level = len(match.group(1))
        if start is not None and level is not None and current_level <= level:
            return start, index
        if match.group(2).strip() == heading:
            start = index + 1
            level = current_level
    return None if start is None else (start, len(lines))


def _discover_specs(feature_dir: Path) -> tuple[list[Path], list[Path]]:
    live = [
        path
        for path in feature_dir.glob("*.md")
        if path.name not in {"README.md", "TEMPLATE.md"}
    ]
    live.extend(
        path
        for path in feature_dir.glob("**/index.md")
        if "retired" not in path.relative_to(feature_dir).parts
    )
    retired_dir = feature_dir / "retired"
    retired: list[Path] = []
    if retired_dir.is_dir():
        retired.extend(retired_dir.glob("*.md"))
        retired.extend(retired_dir.glob("**/index.md"))
    return sorted(set(live)), sorted(set(retired))


def _spec_documents(spec: Path) -> list[Path]:
    """Return a spec root and any direct companions in its directory."""

    if spec.name != "index.md":
        return [spec]
    return sorted(spec.parent.glob("*.md"))


def _document_link_targets(path: Path, root: Path) -> set[Path]:
    """Return existing repository-local files linked from a Markdown file."""

    try:
        lines = _visible_lines(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return set()
    targets: set[Path] = set()
    for _, line in lines:
        for match in _LINK_RE.finditer(_without_inline_code(line)):
            local = _local_link_target(path, _link_destination(match.group(2)), root)
            if local is None:
                continue
            target, _ = local
            if _inside(target, root) and target.is_file():
                targets.add(target)
    return targets


def _link_destination(raw: str) -> str:
    """Remove an optional Markdown title from a simple inline destination."""

    raw = raw.strip()
    if raw.startswith("<") and ">" in raw:
        return raw[1 : raw.index(">")]
    # Titles follow whitespace. Repository paths in this project do not use
    # unescaped spaces; angle brackets above are the supported spelling.
    return raw.split(maxsplit=1)[0]


def _local_link_target(
    source: Path, destination: str, root: Path
) -> tuple[Path, str] | None:
    parsed = urlsplit(destination)
    if parsed.scheme or parsed.netloc or _SCHEME_RE.match(destination):
        return None
    path_text = unquote(parsed.path)
    if path_text.startswith("/"):
        target = root / path_text.lstrip("/")
    elif path_text:
        target = source.parent / path_text
    else:
        target = source
    target = target.resolve()
    if target.is_dir():
        target = target / "index.md"
    return target, unquote(parsed.fragment)


def _inside(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory.resolve())
    except ValueError:
        return False
    return True


def _markdown_table_cells(line: str) -> tuple[str, ...] | None:
    """Parse one pipe-table row, preserving pipes in code spans and escapes."""

    stripped = line.strip()
    if "|" not in stripped:
        return None

    cells: list[str] = []
    current: list[str] = []
    code_marker: str | None = None
    cursor = 1 if stripped.startswith("|") else 0
    while cursor < len(stripped):
        char = stripped[cursor]
        if char == "\\" and cursor + 1 < len(stripped):
            current.extend((char, stripped[cursor + 1]))
            cursor += 2
            continue
        if char == "`":
            end = cursor
            while end < len(stripped) and stripped[end] == "`":
                end += 1
            marker = stripped[cursor:end]
            if code_marker is None:
                code_marker = marker
            elif marker == code_marker:
                code_marker = None
            current.append(marker)
            cursor = end
            continue
        if char == "|" and code_marker is None:
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(char)
        cursor += 1
    if current or not stripped.endswith("|"):
        cells.append("".join(current).strip())
    return tuple(cells)


def _plain_markdown(text: str) -> str:
    """Return enough plain text to validate a ledger status token."""

    text = re.sub(r"<a\s+id=[\"'][^\"']+[\"']\s*></a>", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[`*_~]", "", text)
    return " ".join(text.split()).strip()


def _feature_id(spec: Path, feature_dir: Path) -> tuple[str, bool]:
    """Return ``(feature_id, supported_layout)`` for live or retired roots."""

    relative = spec.relative_to(feature_dir)
    parts = relative.parts
    if parts and parts[0] == "retired":
        parts = parts[1:]
    if spec.name == "index.md":
        return spec.parent.name, len(parts) == 2
    return spec.stem, len(parts) == 1


def _check_feature_ids(
    root: Path,
    feature_dir: Path,
    live: list[Path],
    retired: list[Path],
    issues: list[Issue],
) -> dict[Path, str]:
    """Validate stable IDs across flat, directory, live, and retired forms."""

    feature_ids: dict[Path, str] = {}
    owners: defaultdict[str, list[Path]] = defaultdict(list)
    for spec in live + retired:
        feature_id, supported = _feature_id(spec, feature_dir)
        feature_ids[spec] = feature_id
        owners[feature_id].append(spec)
        rel = _relative(spec, root)
        if not supported:
            issues.append(
                Issue(
                    rel,
                    0,
                    "feature spec must use `<feature-id>.md` or `<feature-id>/index.md`",
                )
            )
        if not _VALID_SUFFIX_RE.fullmatch(feature_id):
            issues.append(
                Issue(rel, 0, f"invalid feature ID `{feature_id}`; use lowercase kebab-case")
            )

    for feature_id, specs in sorted(owners.items()):
        if len(specs) <= 1:
            continue
        first = specs[0]
        for duplicate in specs[1:]:
            issues.append(
                Issue(
                    _relative(duplicate, root),
                    0,
                    f"duplicate feature ID `{feature_id}`; first used by `{_relative(first, root)}`",
                )
            )
    return feature_ids


def _slug(text: str) -> str:
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"[`*_~]", "", text).strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return re.sub(r"[ ]+", "-", text)


def _markdown_anchors(path: Path) -> set[str]:
    try:
        lines = _visible_lines(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return set()
    anchors: set[str] = set()
    heading_anchors: set[str] = set()
    slug_counts: Counter[str] = Counter()
    for _, line in lines:
        anchors.update(
            match.group(1)
            for match in re.finditer(r"<a\s+id=[\"']([^\"']+)[\"']", line)
        )
        match = _HEADING_RE.match(line)
        if not match:
            continue
        base = _slug(match.group(2))
        if not base:
            continue
        candidate = base
        while candidate in heading_anchors:
            slug_counts[base] += 1
            candidate = f"{base}-{slug_counts[base]}"
        heading_anchors.add(candidate)
        anchors.add(candidate)
    return anchors


def _check_catalog(
    root: Path, feature_dir: Path, issues: list[Issue]
) -> tuple[list[Path], list[Path]]:
    live, retired = _discover_specs(feature_dir)
    readme = feature_dir / "README.md"
    rel_readme = _relative(readme, root)
    if not readme.is_file():
        issues.append(Issue(rel_readme, 0, "catalog README.md does not exist"))
        return live, retired
    lines = _visible_lines(readme.read_text(encoding="utf-8"))
    bounds = _section_bounds(lines, "Feature Index")
    if bounds is None:
        issues.append(
            Issue(rel_readme, 0, "missing required `## Feature Index` section")
        )
        return live, retired

    indexed: list[tuple[Path, int]] = []
    for number, line in lines[bounds[0] : bounds[1]]:
        for match in _LINK_RE.finditer(_without_inline_code(line)):
            destination = _link_destination(match.group(2))
            local = _local_link_target(readme, destination, root)
            if local is None:
                continue
            target, _ = local
            if not _inside(target, root):
                issues.append(
                    Issue(
                        rel_readme,
                        number,
                        f"Feature Index target escapes the repository: `{destination}`",
                    )
                )
                continue
            if not target.exists():
                issues.append(
                    Issue(
                        rel_readme,
                        number,
                        f"Feature Index target does not exist: `{destination}`",
                    )
                )
                continue
            if (
                target.suffix.lower() != ".md"
                or feature_dir.resolve() not in target.parents
            ):
                issues.append(
                    Issue(
                        rel_readme,
                        number,
                        f"Feature Index target is not a feature spec: `{destination}`",
                    )
                )
                continue
            indexed.append((target, number))

    counts = Counter(path for path, _ in indexed)
    for spec in live + retired:
        count = counts[spec.resolve()]
        if count == 0:
            kind = "retired spec" if spec in retired else "live spec"
            issues.append(
                Issue(
                    rel_readme, 0, f"{kind} is not indexed: `{_relative(spec, root)}`"
                )
            )
        elif count > 1:
            kind = "retired spec" if spec in retired else "live spec"
            issues.append(
                Issue(
                    rel_readme,
                    0,
                    f"{kind} is indexed {count} times: `{_relative(spec, root)}`",
                )
            )
    known = {path.resolve() for path in live + retired}
    for target, number in indexed:
        if target.resolve() not in known:
            issues.append(
                Issue(
                    rel_readme,
                    number,
                    f"indexed Markdown file is not a live spec or retired tombstone: `{_relative(target, root)}`",
                )
            )

    recognized_documents = {
        document.resolve()
        for spec in live + retired
        for document in _spec_documents(spec)
    }
    recognized_documents.update({
        readme.resolve(),
        (feature_dir / "TEMPLATE.md").resolve(),
    })
    for path in sorted(feature_dir.rglob("*.md")):
        if path.resolve() not in recognized_documents:
            issues.append(
                Issue(
                    _relative(path, root),
                    0,
                    "Markdown file is not part of a cataloged spec; add a sibling `index.md` and Feature Index entry",
                )
            )

    for spec in live + retired:
        if spec.name != "index.md":
            continue
        linked = _document_link_targets(spec, root)
        for companion in _spec_documents(spec):
            if companion == spec or companion.resolve() in linked:
                continue
            issues.append(
                Issue(
                    _relative(spec, root),
                    0,
                    f"directory spec does not link companion `{companion.name}`",
                )
            )
    return live, retired


def _check_structure(
    path: Path,
    root: Path,
    lines: list[tuple[int, str]],
    retired: bool,
    issues: list[Issue],
) -> None:
    rel = _relative(path, root)
    headings: defaultdict[str, list[int]] = defaultdict(list)
    for number, line in lines:
        if match := _H2_RE.match(line):
            headings[match.group(1).strip()].append(number)
    for heading in REQUIRED_HEADINGS:
        locations = headings[heading]
        if not locations:
            issues.append(Issue(rel, 0, f"missing required heading `## {heading}`"))
        elif len(locations) > 1:
            issues.append(
                Issue(rel, locations[1], f"duplicate required heading `## {heading}`")
            )

    metadata_bounds = _section_bounds(lines, "Fork Metadata")
    metadata_lines = (
        lines[metadata_bounds[0] : metadata_bounds[1]]
        if metadata_bounds is not None
        else []
    )
    statuses = [
        (number, match.group(1).strip())
        for number, line in metadata_lines
        if (match := _STATUS_RE.match(line))
    ]
    if len(statuses) != 1:
        issues.append(
            Issue(
                rel,
                0,
                f"expected exactly one `**Status:**` metadata value; found {len(statuses)}",
            )
        )
    elif statuses[0][1] not in ALLOWED_STATUSES:
        allowed = ", ".join(sorted(ALLOWED_STATUSES))
        issues.append(
            Issue(
                rel,
                statuses[0][0],
                f"invalid status `{statuses[0][1]}`; allowed: {allowed}",
            )
        )
    elif retired and statuses[0][1] != "Retired":
        issues.append(
            Issue(rel, statuses[0][0], "retired tombstone status must be `Retired`")
        )
    elif not retired and statuses[0][1] == "Retired":
        issues.append(
            Issue(rel, statuses[0][0], "live feature spec status must not be `Retired`")
        )


def _clean_id_suffix(raw: str) -> str:
    return raw.rstrip("`*_.,;:!?'\"})]")


def _is_table_separator(cells: tuple[str, ...]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in cells)


def _requirement_ledger_rows(
    path: Path,
    root: Path,
    lines: list[tuple[int, str]],
    required: bool,
    issues: list[Issue],
) -> list[LedgerRow]:
    """Parse the one documented five-column Requirement Status Ledger."""

    bounds = _section_bounds(lines, "Requirement Status Ledger")
    candidates = lines if path.name == "ledger.md" else (
        lines[bounds[0] : bounds[1]] if bounds is not None else []
    )
    rel = _relative(path, root)
    rows: list[LedgerRow] = []
    in_table = False
    found_header = False
    for number, line in candidates:
        cells = _markdown_table_cells(line)
        first = _plain_markdown(cells[0]).casefold() if cells else ""
        if not in_table:
            if first not in {"requirement", "requirement id"}:
                continue
            in_table = True
            found_header = True
            if len(cells) != 5:
                issues.append(
                    Issue(rel, number, f"requirement ledger header has {len(cells)} columns; expected 5")
                )
            continue
        if cells is None:
            if not line.strip():
                continue
            break
        if _is_table_separator(cells):
            if len(cells) != 5:
                issues.append(
                    Issue(rel, number, f"requirement ledger separator has {len(cells)} columns; expected 5")
                )
            continue
        rows.append(LedgerRow(path, number, cells))
        if len(cells) != 5:
            issues.append(
                Issue(rel, number, f"requirement ledger row has {len(cells)} columns; expected 5")
            )

    if required and not found_header:
        issues.append(Issue(rel, 0, "Requirement Status Ledger has no five-column table"))
    return rows


def _check_ids(
    path: Path,
    root: Path,
    lines: list[tuple[int, str]],
    issues: list[Issue],
) -> tuple[list[Definition], list[tuple[str, str, int, str]]]:
    """Validate ID syntax/spelling and return definitions plus references."""

    rel = _relative(path, root)
    bounds = _section_bounds(lines, "Requirement Status Ledger")
    delta_bounds = _any_level_section_bounds(lines, "Delta Inventory")
    if path.name == "ledger.md":
        ledger_numbers = {number for number, _ in lines}
    else:
        ledger_numbers = (
            {number for number, _ in lines[bounds[0] : bounds[1]]}
            if bounds is not None
            else set()
        )
    delta_numbers = (
        {number for number, _ in lines[delta_bounds[0] : delta_bounds[1]]}
        if delta_bounds is not None
        else set()
    )
    definitions: list[Definition] = []
    references: list[tuple[str, str, int, str]] = []

    for number, line in lines:
        anchors: dict[tuple[str, str], int] = Counter()
        for match in _ID_ANCHOR_RE.finditer(line):
            kind, suffix = match.group(1).lower(), match.group(2)
            if not _VALID_SUFFIX_RE.fullmatch(suffix):
                issues.append(
                    Issue(
                        rel,
                        number,
                        f"invalid {kind} anchor suffix `{suffix}`; use lowercase kebab-case",
                    )
                )
                continue
            anchors[(kind, suffix)] += 1

        linked_spans: list[tuple[int, int]] = []
        for match in _ID_LINK_RE.finditer(line):
            kind, suffix, raw_destination = match.groups()
            destination = _link_destination(raw_destination)
            references.append((kind, suffix, number, destination))
            linked_spans.append(match.span())
            expected_fragment = f"{kind}-{suffix}"
            parsed = urlsplit(destination)
            if unquote(parsed.fragment) != expected_fragment:
                issues.append(
                    Issue(
                        rel,
                        number,
                        f"ID reference `{kind}:{suffix}` must target anchor `#{expected_fragment}`",
                    )
                )

        tokens: list[tuple[str, str, tuple[int, int]]] = []
        for match in _ID_RAW_RE.finditer(line):
            if any(start <= match.start() < end for start, end in linked_spans):
                continue
            kind = match.group(1)
            suffix = _clean_id_suffix(match.group(2))
            if not _VALID_SUFFIX_RE.fullmatch(suffix):
                rendered = f"{kind}:{suffix}" if suffix else f"{kind}:"
                issues.append(
                    Issue(
                        rel,
                        number,
                        f"invalid ID `{rendered}`; use a lowercase kebab-case suffix",
                    )
                )
                continue
            tokens.append((kind, suffix, match.span()))

        consumed_definitions: Counter[tuple[str, str]] = Counter()
        for kind, suffix, span in tokens:
            key = (kind, suffix)
            if anchors[key] > consumed_definitions[key]:
                consumed_definitions[key] += 1
                allowed_numbers = delta_numbers if kind == "delta" else ledger_numbers
                section = (
                    "Delta Inventory"
                    if kind == "delta"
                    else "Requirement Status Ledger"
                )
                if number not in allowed_numbers:
                    issues.append(
                        Issue(
                            rel,
                            number,
                            f"definition `{kind}:{suffix}` must be in `{section}`",
                        )
                    )
                definitions.append(Definition(kind, suffix, path, number, line))
            else:
                issues.append(
                    Issue(
                        rel,
                        number,
                        f'unanchored ID `{kind}:{suffix}`; define it with `<a id="{kind}-{suffix}"></a>` or use a Markdown link reference',
                    )
                )
        for (kind, suffix), count in anchors.items():
            missing_labels = count - consumed_definitions[(kind, suffix)]
            for _ in range(max(0, missing_labels)):
                issues.append(
                    Issue(
                        rel,
                        number,
                        f"anchor `{kind}-{suffix}` has no adjacent `{kind}:{suffix}` definition",
                    )
                )

    return definitions, references


def _check_links(
    path: Path,
    root: Path,
    lines: list[tuple[int, str]],
    anchor_cache: dict[Path, set[str]],
    issues: list[Issue],
) -> None:
    rel = _relative(path, root)
    for number, original in lines:
        line = _without_inline_code(original)
        for match in _LINK_RE.finditer(line):
            destination = _link_destination(match.group(2))
            local = _local_link_target(path, destination, root)
            if local is None:
                continue
            target, anchor = local
            if not _inside(target, root):
                issues.append(
                    Issue(
                        rel,
                        number,
                        f"relative link escapes the repository: `{destination}`",
                    )
                )
                continue
            if not target.exists():
                issues.append(
                    Issue(
                        rel,
                        number,
                        f"relative link target does not exist: `{destination}`",
                    )
                )
                continue
            if anchor and target.suffix.lower() in {".md", ".mdx"}:
                anchors = anchor_cache.get(target)
                if anchors is None:
                    anchors = _markdown_anchors(target)
                    anchor_cache[target] = anchors
                if anchor not in anchors:
                    issues.append(
                        Issue(
                            rel,
                            number,
                            f"relative link anchor does not exist: `{destination}`",
                        )
                    )


def _false_assignment_targets(nodes: list[ast.stmt]) -> list[ast.expr]:
    """Return direct assignment targets whose value is literally ``False``."""

    targets: list[ast.expr] = []
    for node in nodes:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if node.value.value is False:
                targets.extend(node.targets)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.value, ast.Constant)
            and node.value.value is False
        ):
            targets.append(node.target)
    return targets


def _attribute_path(node: ast.expr) -> tuple[str, ...] | None:
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, ast.Attribute):
        parent = _attribute_path(node.value)
        return None if parent is None else (*parent, node.attr)
    return None


def _python_test_nodes(path: Path) -> PythonTestNodes:
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, UnicodeError, SyntaxError) as exc:
        return PythonTestNodes(set(), {}, set(), {}, str(exc))

    all_functions = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    classes = {
        node.name: node for node in tree.body if isinstance(node, ast.ClassDef)
    }
    all_methods = {
        node.name: {
            member.name
            for member in node.body
            if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for node in tree.body
        if isinstance(node, ast.ClassDef)
    }

    module_targets = {
        path
        for target in _false_assignment_targets(tree.body)
        if (path := _attribute_path(target)) is not None
    }
    module_disabled = ("__test__",) in module_targets
    disabled_objects = {
        path[0]
        for path in module_targets
        if len(path) == 2 and path[1] == "__test__"
    }
    disabled_method_paths = {
        (path[0], path[1])
        for path in module_targets
        if len(path) == 3 and path[2] == "__test__"
    }

    disabled_functions = (
        set(all_functions)
        if module_disabled
        else all_functions & disabled_objects
    )
    disabled_methods: dict[str, set[str]] = {}
    for class_name, class_node in classes.items():
        class_targets = {
            path
            for target in _false_assignment_targets(class_node.body)
            if (path := _attribute_path(target)) is not None
        }
        class_disabled = (
            module_disabled
            or class_name in disabled_objects
            or ("__test__",) in class_targets
        )
        local_disabled = {
            path[0]
            for path in class_targets
            if len(path) == 2 and path[1] == "__test__"
        }
        global_disabled = {
            method
            for owner, method in disabled_method_paths
            if owner == class_name
        }
        disabled_methods[class_name] = (
            set(all_methods[class_name])
            if class_disabled
            else (local_disabled | global_disabled) & all_methods[class_name]
        )

    functions = all_functions - disabled_functions
    methods = {
        class_name: names - disabled_methods[class_name]
        for class_name, names in all_methods.items()
    }
    return PythonTestNodes(
        functions,
        methods,
        disabled_functions,
        disabled_methods,
    )


def _strip_javascript_comments(source: str) -> str:
    """Blank JS comments without treating comment markers in strings as code."""

    output: list[str] = []
    cursor = 0
    quote: str | None = None
    while cursor < len(source):
        char = source[cursor]
        if quote is not None:
            output.append(char)
            if char == "\\" and cursor + 1 < len(source):
                output.append(source[cursor + 1])
                cursor += 2
                continue
            if char == quote:
                quote = None
            cursor += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            output.append(char)
            cursor += 1
            continue
        if source.startswith("//", cursor):
            end = source.find("\n", cursor + 2)
            if end < 0:
                output.extend(" " * (len(source) - cursor))
                break
            output.extend(" " * (end - cursor))
            output.append("\n")
            cursor = end + 1
            continue
        if source.startswith("/*", cursor):
            end = source.find("*/", cursor + 2)
            end = len(source) if end < 0 else end + 2
            output.extend("\n" if value == "\n" else " " for value in source[cursor:end])
            cursor = end
            continue
        output.append(char)
        cursor += 1
    return "".join(output)


def _javascript_block_end(source: str, start: int) -> int | None:
    """Find the callback block containing ``start``, ignoring quoted braces."""

    quote: str | None = None
    escaped = False
    depth = 0
    opened = False
    for cursor in range(start, len(source)):
        char = source[cursor]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {"'", '"', "`"}:
            quote = char
        elif char == "{":
            depth += 1
            opened = True
        elif char == "}" and opened:
            depth -= 1
            if depth == 0:
                return cursor + 1
    return None


def _disabled_javascript_ranges(source: str) -> list[tuple[int, int]]:
    disabled_suite = re.compile(
        r"^[ \t]*(?:(?:describe|suite)\.(?:skip|todo)|xdescribe)\s*\(",
        re.MULTILINE,
    )
    ranges: list[tuple[int, int]] = []
    for match in disabled_suite.finditer(source):
        end = _javascript_block_end(source, match.end())
        if end is not None:
            ranges.append((match.start(), end))
    return ranges


def _javascript_test_titles(path: Path) -> tuple[set[str], str | None]:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return set(), str(exc)
    source = _strip_javascript_comments(source)
    disabled_ranges = _disabled_javascript_ranges(source)
    call = re.compile(
        r"^[ \t]*(?:it|test)(?:\.(?:only|concurrent|fails))*\s*\(\s*"
        r'(?:"(?P<double>(?:\\.|[^"\\])*)"|'
        r"'(?P<single>(?:\\.|[^'\\])*)'|"
        r"`(?P<backtick>(?:\\.|[^`\\])*)`)",
        re.MULTILINE,
    )
    titles: set[str] = set()
    for match in call.finditer(source):
        if any(start <= match.start() < end for start, end in disabled_ranges):
            continue
        title = next(value for value in match.groups() if value is not None)
        if "${" not in title:
            titles.add(title)
    return titles, None


def _javascript_selector(line: str, path_start: int, selector_start: int) -> str:
    remainder = line[selector_start:]
    if path_start > 0 and line[path_start - 1] == "`":
        remainder = remainder.split("`", 1)[0]
    else:
        remainder = remainder.split("|", 1)[0]
    return remainder.strip().removesuffix("**").strip()


def _test_targets(line: str) -> list[TestTarget]:
    """Extract the documented Python node IDs and JS/TS literal titles."""

    found = [
        TestTarget(match.group("path"), match.group("selector"), "python")
        for match in _PY_TEST_TARGET_RE.finditer(line)
    ]
    for match in _JS_TEST_TARGET_START_RE.finditer(line):
        selector = _javascript_selector(line, match.start("path"), match.end())
        if selector:
            found.append(TestTarget(match.group("path"), selector, "javascript"))
    return found


def _test_paths(line: str) -> set[str]:
    return {
        match.group("path")
        for pattern in (_PY_TEST_PATH_RE, _JS_TEST_PATH_RE)
        for match in pattern.finditer(line)
    }


def _resolved_repository_path(
    path_text: str,
    root: Path,
    rel: str,
    number: int,
    issues: list[Issue],
) -> Path | None:
    parsed_path = Path(path_text.replace("\\", "/"))
    if parsed_path.is_absolute() or ".." in parsed_path.parts:
        issues.append(
            Issue(
                rel,
                number,
                f"listed test path uses traversal or is not repository-relative: `{path_text}`",
            )
        )
        return None
    target = (root / path_text).resolve()
    if not _inside(target, root):
        issues.append(
            Issue(rel, number, f"listed test path escapes the repository: `{path_text}`")
        )
        return None
    return target


def _validate_test_target(
    target: TestTarget,
    root: Path,
    rel: str,
    number: int,
    issues: list[Issue],
) -> None:
    resolved = _resolved_repository_path(target.path, root, rel, number, issues)
    if resolved is None:
        return
    if target.kind == "python":
        tests_dir = (root / "tests").resolve()
        if not _inside(resolved, tests_dir):
            issues.append(
                Issue(rel, number, f"Python test target must be under `tests/`: `{target.path}`")
            )
            return
        if not (resolved.name.startswith("test_") and resolved.suffix == ".py"):
            issues.append(
                Issue(rel, number, f"Python test target is not a collectable `test_*.py` file: `{target.path}`")
            )
            return
    elif not _JS_TEST_FILENAME_RE.fullmatch(resolved.name):
        issues.append(
            Issue(rel, number, f"JavaScript test target is not a `*.test.*` or `*.spec.*` file: `{target.path}`")
        )
        return

    if not resolved.is_file():
        issues.append(Issue(rel, number, f"listed test file does not exist: `{target.path}`"))
        return

    rendered = f"{target.path}:{target.selector}"
    if target.kind == "python":
        parts = target.selector.split("::")
        valid_selector = (
            (len(parts) == 1 and parts[0].startswith("test_"))
            or (
                len(parts) == 2
                and parts[0].startswith("Test")
                and parts[1].startswith("test_")
            )
        )
        if not valid_selector:
            issues.append(
                Issue(rel, number, f"listed Python selector is not collectable: `{rendered}`")
            )
            return
        nodes = _python_test_nodes(resolved)
        if nodes.parse_error:
            issues.append(
                Issue(
                    rel,
                    number,
                    f"cannot parse listed test file `{target.path}`: {nodes.parse_error}",
                )
            )
        elif len(parts) == 1 and parts[0] in nodes.disabled_functions:
            issues.append(
                Issue(
                    rel,
                    number,
                    f"listed test function is disabled by `__test__ = False`: `{rendered}`",
                )
            )
        elif (
            len(parts) == 2
            and parts[1] in nodes.disabled_methods.get(parts[0], set())
        ):
            issues.append(
                Issue(
                    rel,
                    number,
                    f"listed test method is disabled by `__test__ = False`: `{rendered}`",
                )
            )
        elif len(parts) == 1 and parts[0] not in nodes.functions:
            issues.append(Issue(rel, number, f"listed test function does not exist: `{rendered}`"))
        elif len(parts) == 2 and parts[1] not in nodes.methods.get(parts[0], set()):
            issues.append(Issue(rel, number, f"listed test method does not exist: `{rendered}`"))
        return

    title = target.selector.rsplit(" > ", 1)[-1].strip()
    titles, read_error = _javascript_test_titles(resolved)
    if read_error:
        issues.append(
            Issue(rel, number, f"cannot read listed test file `{target.path}`: {read_error}")
        )
    elif title not in titles:
        issues.append(
            Issue(rel, number, f"listed JavaScript test title does not exist: `{rendered}`")
        )


def _check_tests(path: Path, root: Path, text: str, issues: list[Issue]) -> None:
    rel = _relative(path, root)
    checked_paths: set[str] = set()
    checked_targets: set[TestTarget] = set()
    # Unlike Markdown links, test evidence inside a fenced verification-command
    # block is still evidence and must be validated.
    for number, original in enumerate(_strip_html_comments(text).splitlines(), 1):
        line = _without_markdown_links(original)
        for test_path in _test_paths(line):
            if test_path in checked_paths:
                continue
            checked_paths.add(test_path)
            target = _resolved_repository_path(test_path, root, rel, number, issues)
            if target is not None and not target.is_file():
                issues.append(
                    Issue(
                        rel, number, f"listed test file does not exist: `{test_path}`"
                    )
                )
        for target in _test_targets(line):
            if target in checked_targets:
                continue
            checked_targets.add(target)
            _validate_test_target(target, root, rel, number, issues)


def _cell_definitions(cell: str, definitions: list[Definition]) -> list[Definition]:
    """Return definitions whose anchor and label both occur in ``cell``."""

    found: list[Definition] = []
    for definition in definitions:
        anchor = re.search(
            rf"<a\s+id=[\"']{re.escape(definition.kind)}-{re.escape(definition.suffix)}[\"']\s*></a>",
            cell,
        )
        label = re.search(
            rf"(?<![A-Za-z0-9_-]){re.escape(definition.kind)}:{re.escape(definition.suffix)}(?![A-Za-z0-9_-])",
            cell,
        )
        if anchor and label:
            found.append(definition)
    return found


def _linked_id_suffixes(cell: str, kind: str) -> set[str]:
    return {
        match.group(2)
        for match in _ID_LINK_RE.finditer(cell)
        if match.group(1) == kind
    }


def _starts_with_gap_definition(cell: str, gaps: list[Definition]) -> bool:
    """Whether ``cell`` begins with an anchored canonical gap label."""

    for gap in gaps:
        anchor = re.match(
            rf"^\s*<a\s+id=[\"']gap-{re.escape(gap.suffix)}[\"']\s*></a>",
            cell,
        )
        if anchor is None:
            continue
        remainder = cell[anchor.end() :]
        if re.match(
            rf"^[\s`*_~]*gap:{re.escape(gap.suffix)}(?![A-Za-z0-9_-])",
            remainder,
        ):
            return True
    return False


def _valid_upstream_disposition(cell: str) -> bool:
    plain = _plain_markdown(cell)
    for allowed in ALLOWED_UPSTREAM_DISPOSITIONS:
        prefix = f"{allowed} — "
        if not plain.startswith(prefix):
            continue
        evidence = plain[len(prefix) :].strip()
        return bool(re.search(r"[^\W_]", evidence, re.UNICODE))
    return False


def _known_gap_items(
    path: Path, lines: list[tuple[int, str]]
) -> list[tuple[Path, int, str]]:
    bounds = _any_level_section_bounds(lines, "Known Implementation Gaps")
    if bounds is None:
        return []
    items: list[tuple[Path, int, str]] = []
    current_line: int | None = None
    current_parts: list[str] = []
    for number, line in lines[bounds[0] : bounds[1]]:
        match = re.match(r"^\s*[-*+]\s+(.+)$", line)
        if match:
            if current_line is not None:
                items.append((path, current_line, " ".join(current_parts)))
            current_line = number
            current_parts = [match.group(1).strip()]
        elif current_line is not None and line[:1].isspace() and line.strip():
            current_parts.append(line.strip())
        elif current_line is not None and line.strip():
            items.append((path, current_line, " ".join(current_parts)))
            current_line = None
            current_parts = []
    if current_line is not None:
        items.append((path, current_line, " ".join(current_parts)))
    return items


def _has_same_feature_delta(
    suffixes: set[str],
    owner: Path,
    by_token: dict[tuple[str, str], list[Definition]],
    owner_by_document: dict[Path, Path],
) -> bool:
    return any(
        any(owner_by_document[definition.path] == owner for definition in by_token[("delta", suffix)])
        for suffix in suffixes
    )


def _validate_ledger_rows(
    ledger_rows: list[LedgerRow],
    definitions: list[Definition],
    by_token: dict[tuple[str, str], list[Definition]],
    root: Path,
    owner_by_document: dict[Path, Path],
    retired_specs: set[Path],
    issues: list[Issue],
) -> None:
    definitions_by_line: defaultdict[tuple[Path, int], list[Definition]] = defaultdict(list)
    for definition in definitions:
        if definition.kind in {"req", "gap", "missing"}:
            definitions_by_line[(definition.path, definition.line)].append(definition)
    row_locations = {(row.path, row.line) for row in ledger_rows}

    for row in ledger_rows:
        rel = _relative(row.path, root)
        line_definitions = definitions_by_line[(row.path, row.line)]
        if len(row.cells) != 5:
            continue
        reqs = _cell_definitions(row.cells[0], line_definitions)
        reqs = [definition for definition in reqs if definition.kind == "req"]
        if len(reqs) != 1:
            issues.append(
                Issue(rel, row.line, "requirement ledger row must define exactly one canonical `req:` ID in column 1")
            )
        for definition in line_definitions:
            expected_column = {"req": 0, "gap": 2, "missing": 4}[definition.kind]
            if definition not in _cell_definitions(row.cells[expected_column], line_definitions):
                issues.append(
                    Issue(
                        rel,
                        row.line,
                        f"canonical `{definition.kind}:` definitions belong only in column {expected_column + 1}",
                    )
                )

        gaps = [
            definition
            for definition in _cell_definitions(row.cells[2], line_definitions)
            if definition.kind == "gap"
        ]
        implementation = _plain_markdown(row.cells[2])
        conforming = bool(
            re.match(r"^Conforming\s+—\s+\S", implementation)
        )
        if implementation.startswith("Conforming") and not conforming:
            issues.append(
                Issue(
                    rel,
                    row.line,
                    "`Conforming` implementation status must use `Conforming — <evidence>`",
                )
            )
        if conforming and gaps:
            issues.append(
                Issue(
                    rel,
                    row.line,
                    "implementation column cannot combine `Conforming` with canonical `gap:` statuses",
                )
            )
        elif gaps and not _starts_with_gap_definition(row.cells[2], gaps):
            issues.append(
                Issue(
                    rel,
                    row.line,
                    "implementation gap status must begin with its canonical `gap:` definition",
                )
            )
        elif not conforming and not gaps:
            issues.append(
                Issue(
                    rel,
                    row.line,
                    "implementation column must start with `Conforming` or define a canonical `gap:` status",
                )
            )

        delta_suffixes = _linked_id_suffixes(row.cells[2], "delta")
        owner = owner_by_document[row.path]
        if not delta_suffixes:
            issues.append(
                Issue(rel, row.line, "requirement implementation cell must reference at least one `delta:` ID")
            )
        elif not _has_same_feature_delta(
            delta_suffixes, owner, by_token, owner_by_document
        ):
            issues.append(
                Issue(rel, row.line, "requirement implementation cell has no declared delta from the same feature")
            )

        if not _valid_upstream_disposition(row.cells[3]):
            allowed = ", ".join(ALLOWED_UPSTREAM_DISPOSITIONS)
            issues.append(
                Issue(rel, row.line, f"invalid upstream disposition; allowed: {allowed}")
            )

        missing = [
            definition
            for definition in _cell_definitions(row.cells[4], line_definitions)
            if definition.kind == "missing"
        ]
        direct_targets = _test_targets(_without_markdown_links(row.cells[4]))
        retired_coverage = owner in retired_specs and bool(
            re.search(r"\bNot applicable\b.*\bretired\b", _plain_markdown(row.cells[4]), re.IGNORECASE)
        )
        if not (direct_targets or missing or retired_coverage):
            token = reqs[0].token if len(reqs) == 1 else "requirement row"
            issues.append(
                Issue(
                    rel,
                    row.line,
                    f"`{token}` has no coverage disposition; add a direct test target or canonical `missing:` ID",
                )
            )

    for definition in definitions:
        if definition.kind not in {"req", "gap", "missing"}:
            continue
        if (definition.path, definition.line) not in row_locations:
            issues.append(
                Issue(
                    _relative(definition.path, root),
                    definition.line,
                    f"canonical `{definition.kind}:` definition must be in a five-column requirement ledger row",
                )
            )


def _check_id_relationships(
    definitions: list[Definition],
    references: list[tuple[Path, str, str, int, str]],
    ledger_rows: list[LedgerRow],
    known_gap_items: list[tuple[Path, int, str]],
    root: Path,
    owner_by_document: dict[Path, Path],
    retired_specs: set[Path],
    issues: list[Issue],
) -> None:
    by_token: defaultdict[tuple[str, str], list[Definition]] = defaultdict(list)
    for definition in definitions:
        by_token[(definition.kind, definition.suffix)].append(definition)
    for (kind, suffix), found in sorted(by_token.items()):
        if len(found) <= 1:
            continue
        for duplicate in found[1:]:
            issues.append(
                Issue(
                    _relative(duplicate.path, root),
                    duplicate.line,
                    f"duplicate {kind} definition `{kind}:{suffix}`; first defined at {_relative(found[0].path, root)}:{found[0].line}",
                )
            )

    for path, kind, suffix, number, destination in references:
        found = by_token[(kind, suffix)]
        if not found:
            issues.append(
                Issue(
                    _relative(path, root),
                    number,
                    f"ID reference has no definition: `{kind}:{suffix}`",
                )
            )
            continue
        if len(found) != 1:
            continue
        local = _local_link_target(path, destination, root)
        if local is None:
            issues.append(
                Issue(
                    _relative(path, root),
                    number,
                    f"ID reference `{kind}:{suffix}` must link its local canonical definition",
                )
            )
            continue
        target, _ = local
        if target.exists() and target.resolve() != found[0].path.resolve():
            issues.append(
                Issue(
                    _relative(path, root),
                    number,
                    f"ID reference `{kind}:{suffix}` points to `{_relative(target, root)}`, but its definition is in `{_relative(found[0].path, root)}`",
                )
            )

    _validate_ledger_rows(
        ledger_rows,
        definitions,
        by_token,
        root,
        owner_by_document,
        retired_specs,
        issues,
    )

    definitions_by_line: defaultdict[
        tuple[Path, int], list[Definition]
    ] = defaultdict(list)
    for definition in definitions:
        if definition.kind in {"req", "gap"}:
            definitions_by_line[(definition.path, definition.line)].append(definition)

    owning_requirement: dict[tuple[Path, str], str] = {}
    for row in ledger_rows:
        if len(row.cells) != 5:
            continue
        line_definitions = definitions_by_line[(row.path, row.line)]
        requirements = [
            definition
            for definition in _cell_definitions(row.cells[0], line_definitions)
            if definition.kind == "req"
        ]
        gaps = [
            definition
            for definition in _cell_definitions(row.cells[2], line_definitions)
            if definition.kind == "gap"
        ]
        if len(requirements) == 1:
            owner = owner_by_document[row.path]
            for gap in gaps:
                owning_requirement[(owner, gap.suffix)] = requirements[0].suffix

    gap_item_counts: Counter[tuple[Path, str]] = Counter()
    for path, number, item in known_gap_items:
        owner = owner_by_document[path]
        gap_suffixes = _linked_id_suffixes(item, "gap")
        same_feature_gaps = {
            suffix
            for suffix in gap_suffixes
            if any(
                owner_by_document[definition.path] == owner
                for definition in by_token[("gap", suffix)]
            )
        }
        if len(gap_suffixes) != 1 or len(same_feature_gaps) != 1:
            issues.append(
                Issue(
                    _relative(path, root),
                    number,
                    "Known Implementation Gaps item must link exactly one canonical `gap:` from the same feature",
                )
            )
        else:
            gap_suffix = next(iter(same_feature_gaps))
            gap_item_counts[(owner, gap_suffix)] += 1
            required_suffix = owning_requirement.get((owner, gap_suffix))
            linked_requirements = _linked_id_suffixes(item, "req")
            if required_suffix is not None and required_suffix not in linked_requirements:
                issues.append(
                    Issue(
                        _relative(path, root),
                        number,
                        f"Known Implementation Gaps item for `gap:{gap_suffix}` must link owning `req:{required_suffix}`",
                    )
                )

        delta_suffixes = _linked_id_suffixes(item, "delta")
        if not delta_suffixes:
            issues.append(
                Issue(
                    _relative(path, root),
                    number,
                    "Known Implementation Gaps item must reference at least one `delta:` ID",
                )
            )
        elif not _has_same_feature_delta(
            delta_suffixes, owner, by_token, owner_by_document
        ):
            issues.append(
                Issue(
                    _relative(path, root),
                    number,
                    "Known Implementation Gaps item has no declared delta from the same feature",
                )
            )

    for definition in definitions:
        if definition.kind != "gap":
            continue
        owner = owner_by_document[definition.path]
        count = gap_item_counts[(owner, definition.suffix)]
        if count != 1:
            issues.append(
                Issue(
                    _relative(definition.path, root),
                    definition.line,
                    f"`{definition.token}` must appear exactly once as a Known Implementation Gaps item; found {count}",
                )
            )

    for definition in definitions:
        if definition.kind not in {"gap", "missing"}:
            continue
        owner = owner_by_document[definition.path]
        used = any(
            kind == definition.kind
            and suffix == definition.suffix
            and owner_by_document[path] == owner
            for path, kind, suffix, _, _ in references
        )
        if not used:
            issues.append(
                Issue(
                    _relative(definition.path, root),
                    definition.line,
                    f"canonical `{definition.token}` is never referenced by its feature",
                )
            )



def check_repository(root: Path) -> list[Issue]:
    """Return every feature-spec validation issue below ``root``."""

    root = root.resolve()
    feature_dir = root / FEATURE_DIR
    issues: list[Issue] = []
    if not feature_dir.is_dir():
        return [Issue(FEATURE_DIR, 0, "feature-specs directory does not exist")]

    live, retired = _check_catalog(root, feature_dir, issues)
    _check_feature_ids(root, feature_dir, live, retired, issues)
    roots = live + retired
    documents_by_root = {spec: _spec_documents(spec) for spec in roots}
    documents = sorted({
        document for group in documents_by_root.values() for document in group
    })
    owner_by_document = {
        document: spec
        for spec, group in documents_by_root.items()
        for document in group
    }
    definitions_by_root: defaultdict[Path, list[Definition]] = defaultdict(list)
    all_definitions: list[Definition] = []
    all_references: list[tuple[Path, str, str, int, str]] = []
    all_ledger_rows: list[LedgerRow] = []
    all_known_gap_items: list[tuple[Path, int, str]] = []
    for path in roots:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            issues.append(Issue(_relative(path, root), 0, f"cannot read spec: {exc}"))
            continue
        lines = _visible_lines(text)
        _check_structure(path, root, lines, path in retired, issues)

    anchor_cache: dict[Path, set[str]] = {}
    for path in documents:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            issues.append(
                Issue(_relative(path, root), 0, f"cannot read spec document: {exc}")
            )
            continue
        lines = _visible_lines(text)
        owner = owner_by_document[path]
        owner_documents = documents_by_root[owner]
        has_ledger_companion = any(
            document.name == "ledger.md" for document in owner_documents
        )
        ledger_required = (
            path.name == "ledger.md"
            if has_ledger_companion
            else path == owner
        )
        all_ledger_rows.extend(
            _requirement_ledger_rows(path, root, lines, ledger_required, issues)
        )
        all_known_gap_items.extend(_known_gap_items(path, lines))
        definitions, references = _check_ids(path, root, lines, issues)
        definitions_by_root[owner].extend(definitions)
        full_references = [
            (path, kind, suffix, number, destination)
            for kind, suffix, number, destination in references
        ]
        all_definitions.extend(definitions)
        all_references.extend(full_references)
        _check_links(path, root, lines, anchor_cache, issues)
        _check_tests(path, root, text, issues)
    for spec in documents_by_root:
        definitions = definitions_by_root[spec]
        if not any(definition.kind == "req" for definition in definitions):
            issues.append(
                Issue(
                    _relative(spec, root),
                    0,
                    "Requirement Status Ledger defines no `req:<id>` for this feature",
                )
            )
    _check_id_relationships(
        all_definitions,
        all_references,
        all_ledger_rows,
        all_known_gap_items,
        root,
        owner_by_document,
        set(retired),
        issues,
    )
    return sorted(set(issues))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "root",
        nargs="?",
        type=Path,
        default=DEFAULT_ROOT,
        help="repository root (default: root containing this script)",
    )
    args = parser.parse_args(argv)
    issues = check_repository(args.root)
    if issues:
        print(
            f"feature-spec validation failed with {len(issues)} issue(s):",
            file=sys.stderr,
        )
        for issue in issues:
            print(f"  {issue.render()}", file=sys.stderr)
        return 1
    print("feature-spec validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
