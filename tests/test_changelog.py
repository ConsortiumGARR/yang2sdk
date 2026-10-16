"""The release gates: a version cannot ship undocumented.

`scripts/changelog.py` is the single implementation of "this version is
releasable", used by `scripts/release.py` before tagging and by `release.yaml`
in the build job. Both call sites must agree, so these tests exercise the module
directly *and* the workflow step that shells out to it.

Why any of this exists: eight commits landed on `main` after `v2026.10.16` --
two of them breaking, `yang2netconf`/`yang2restconf` removed outright -- while
`## [Unreleased]` sat empty. A version number is a claim about what changed, and
an unbacked one cannot be checked by the person installing the package.

Offline: no network, no docker, no device.
"""

# `scripts/` is a source root, so `import changelog` resolves as a real import in
# both type-checkers (see `[tool.pyrefly] search-path`) instead of needing a
# `sys.path` hack and a suppression on every use.

from __future__ import annotations

import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

import changelog

RELEASE_WF = REPO_ROOT / ".github" / "workflows" / "release.yaml"

GOOD = """\
# Changelog

## [Unreleased]

## [2026.1.2] - 2026-01-02

### Added

- a thing
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "CHANGELOG.md"
    path.write_text(text)
    return path


# --- the four rules, each with its own failure -------------------------------


def test_a_version_with_notes_passes() -> None:
    section = changelog.require_release_notes(GOOD, "2026.1.2")
    assert section.date == "2026-01-02"


def test_a_version_with_no_section_is_refused() -> None:
    with pytest.raises(changelog.ChangelogError, match="no '## \\[2026.1.3\\]"):
        changelog.require_release_notes(GOOD, "2026.1.3")


def test_a_section_of_only_headings_is_refused() -> None:
    """The vacuous pass.

    `### Added` on its own promises an entry and delivers none. The first version
    of this check accepted it -- found by running the check, not by reading it --
    which is the same shape of bug AGENTS.md records three times: something that
    reports success while covering nothing.
    """
    hollow = GOOD.replace("- a thing\n", "")
    with pytest.raises(changelog.ChangelogError, match="documents nothing"):
        changelog.require_release_notes(hollow, "2026.1.2")


def test_an_entirely_empty_section_is_refused() -> None:
    empty = "## [Unreleased]\n\n## [2026.1.2] - 2026-01-02\n"
    with pytest.raises(changelog.ChangelogError, match="documents nothing"):
        changelog.require_release_notes(empty, "2026.1.2")


def test_unpromoted_unreleased_notes_are_refused() -> None:
    """They would ship with the NEXT release, silently."""
    stale = GOOD.replace(
        "## [Unreleased]\n", "## [Unreleased]\n\n### Fixed\n\n- forgot to promote\n"
    )
    with pytest.raises(changelog.ChangelogError, match=r"\[Unreleased\]"):
        changelog.require_release_notes(stale, "2026.1.2")


def test_a_heading_date_that_contradicts_calver_is_refused() -> None:
    """Under CalVer the version *is* the release date; one of the two is untrue."""
    wrong = GOOD.replace("2026-01-02", "2026-02-02")
    with pytest.raises(changelog.ChangelogError, match="disagrees with the version"):
        changelog.require_release_notes(wrong, "2026.1.2")


def test_a_micro_version_still_checks_the_date() -> None:
    """`2026.1.2.3` is a same-day re-release, not a different date."""
    micro = GOOD.replace("2026.1.2]", "2026.1.2.3]").replace("- a thing", "- a thing")
    assert changelog.require_release_notes(micro, "2026.1.2.3").date == "2026-01-02"


# --- parsing -----------------------------------------------------------------


def test_subsection_headings_are_not_mistaken_for_releases() -> None:
    """The real CHANGELOG has 8 `###` headings under one `##`."""
    sections = changelog.parse(GOOD)
    assert [s.version for s in sections] == ["Unreleased", "2026.1.2"]


def test_a_body_stops_at_the_next_release_not_at_the_next_heading() -> None:
    text = (
        "## [Unreleased]\n\n## [2.0.0] - 2026-01-02\n\n"
        "### Added\n\n- mine\n\n### Fixed\n\n- also mine\n\n"
        "## [1.0.0] - 2026-01-01\n\n- older\n"
    )
    section = changelog.find(text, "2.0.0")
    assert section is not None
    assert section.notes(text).endswith("also mine")


def test_expected_date_needs_no_zero_padding() -> None:
    """PEP 440 orders `YYYY.M.D` numerically, so `2026.1.2` is legitimate."""
    assert changelog.expected_date("2026.1.2") == "2026-01-02"
    assert changelog.expected_date("2026.10.16.1") == "2026-10-16"
    assert changelog.expected_date("0.1.0") is None


# --- promotion ---------------------------------------------------------------


def test_promotion_moves_the_notes_and_leaves_a_clean_unreleased() -> None:
    """Rehearses the exact call the release script makes.

    Promotion is the one release step that rewrites a file, so the shape of its
    output is pinned here rather than discovered during a release. The first
    version of `promote()` emitted the body twice -- `tail` overlapped what had
    already been written from `body` -- and only running it against a real
    changelog showed that. Duplicated release notes are the kind of defect that
    reaches users as "this project has no idea what it shipped".
    """
    out = changelog.promote(
        GOOD.replace("## [Unreleased]\n", "## [Unreleased]\n\n- new work\n"),
        "2026.3.4",
        on=date(2026, 3, 4),
    )
    assert "## [Unreleased]\n\n## [2026.3.4] - 2026-03-04" in out
    pending = changelog.unreleased(out)
    assert pending is not None
    assert pending.notes(out) == ""
    # every entry survives, exactly once
    assert out.count("- new work") == 1
    assert out.count("- a thing") == 1
    # and the result passes the gate it was built for
    changelog.require_release_notes(out, "2026.3.4")


def test_promotion_leaves_earlier_releases_byte_identical() -> None:
    original = GOOD.replace("## [Unreleased]\n", "## [Unreleased]\n\n- new work\n")
    promoted = changelog.promote(original, "2026.3.4", on=date(2026, 3, 4))
    marker = "## [2026.1.2]"
    assert promoted[promoted.index(marker) :] == original[original.index(marker) :]


def test_promotion_refuses_when_there_is_nothing_to_promote() -> None:
    with pytest.raises(changelog.ChangelogError, match="nothing to promote"):
        changelog.promote(GOOD, "2026.3.4")


def test_promotion_refuses_rather_than_duplicate_a_section() -> None:
    with pytest.raises(changelog.ChangelogError, match="already has"):
        changelog.promote(GOOD, "2026.1.2")


# --- the CLI the workflow calls ---------------------------------------------


def test_cli_check_exits_nonzero_with_an_actionable_message(tmp_path: Path) -> None:
    path = write(tmp_path, GOOD)
    proc = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "changelog.py"),
            "--check",
            "2026.1.2",
            "--file",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    missing = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "changelog.py"),
            "--check",
            "2026.9.9",
            "--file",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert missing.returncode == 1
    assert "CHANGELOG.md" in missing.stderr


def test_cli_notes_prints_only_the_body(tmp_path: Path) -> None:
    """`gh release create --notes-file` reads this, so headings must not leak in."""
    path = write(tmp_path, GOOD)
    proc = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "changelog.py"),
            "--notes",
            "2026.1.2",
            "--file",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "### Added\n\n- a thing"


def test_cli_promote_rewrites_the_file_in_place(tmp_path: Path) -> None:
    path = write(
        tmp_path, GOOD.replace("## [Unreleased]\n", "## [Unreleased]\n\n- work\n")
    )
    proc = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "changelog.py"),
            "--promote",
            "2026.3.4",
            "--date",
            "2026-03-04",
            "--file",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert changelog.find(path.read_text(), "2026.3.4") is not None
    # in-place means exactly one rewrite: the tree must be clean afterwards
    assert not proc.stderr.strip()


# --- the workflow step, run the way GitHub runs it --------------------------


def _changelog_check_step() -> str:
    doc = yaml.safe_load(RELEASE_WF.read_text())
    build = doc["jobs"]["build"]
    step = next(
        s
        for s in build["steps"]
        if s.get("name") == "Fail if CHANGELOG.md does not document this version"
    )
    return str(step["run"])


def test_release_workflow_gates_on_the_changelog() -> None:
    """A tag pushed by hand, around scripts/release.py, is still refused.

    Parsed from the workflow, never copied: a copy would let the workflow drift
    from the rule these tests execute, which is the failure this pin exists to
    prevent -- the same reasoning as the tag-guard tests.

    `${{ steps.version.outputs.value }}` is substituted into the script text
    before bash ever sees it, exactly as GitHub does it. Left in place, bash
    fails on `bad substitution` and this test would judge the wrong thing --
    the same mistake the tag-guard tests document, where the guard compared
    against "" and so failed for every tag including the correct ones.
    """
    run = _changelog_check_step().replace(
        "${{ steps.version.outputs.value }}", _pyproject_version()
    )
    assert "scripts/changelog.py" in run and "--check" in run
    assert "${{" not in run, "version expression was not substituted"

    # Run against the repository's own CHANGELOG.md and its own declared
    # version: this asserts the invariant that makes the gate meaningful, namely
    # that `main` is always in a releasable state for the version it declares.
    # A release PR that forgets the notes fails here, on the PR, not on the tag.
    version = _pyproject_version()
    proc = subprocess.run(
        ["bash", "-e", "-c", run],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"CHANGELOG.md does not document {version}, so this branch could not "
        f"be released: {proc.stderr[-2000:]}"
    )


def test_the_workflow_gate_refuses_an_undocumented_version() -> None:
    """Negative control, so a green run above means a real check.

    Without this, `test_release_workflow_gates_on_the_changelog` would also pass
    if the step had become a no-op -- the failure mode this whole suite exists
    to prevent.
    """
    run = _changelog_check_step().replace(
        "${{ steps.version.outputs.value }}", "1999.1.1"
    )
    proc = subprocess.run(
        ["bash", "-e", "-c", run],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert proc.returncode == 1, proc.stdout
    assert "1999.1.1" in proc.stderr


def _pyproject_version() -> str:
    import tomllib

    return str(
        tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]["version"]
    )
