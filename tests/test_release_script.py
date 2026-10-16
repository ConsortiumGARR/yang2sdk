"""`scripts/release.py`: the checks that run before anything is pushed.

A release is irreversible three separate ways — a public git ref, a permanent
PyPI filename, and a published artifact nobody can replace. Nearly every test
here asserts a *refusal*, because saying no is overwhelmingly the script's job.

The failure that motivated the whole thing is real and recent: on 2026-10-16
`v2026.10.16.1` was typed by hand one commit before the version bump landed, and
the release died with

    tag v2026.10.16.1 implies 2026.10.16.1 but pyproject.toml says 2026.10.16

A guard that can only say no has not removed the failure, it has moved it to a
human. These tests pin the guards that replace the human.

Offline: `published()` is stubbed, no git remote is contacted, nothing is tagged.
"""

from __future__ import annotations

import email.message
import importlib.util
import subprocess
import sys
import tomllib
import urllib.error
from pathlib import Path
from typing import Any

import changelog
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "release.py"


def _load_release() -> Any:
    """Import scripts/release.py by path.

    It is a script rather than a package module, so it is loaded the way `uv run
    python scripts/release.py` loads it. `scripts/` is already on `sys.path`
    (conftest), which is what lets it `import changelog` as a sibling.
    """
    spec = importlib.util.spec_from_file_location("release_script", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


release = _load_release()


def _done(stdout: str = "", stderr: str = "", code: int = 0) -> Any:
    return subprocess.CompletedProcess([], code, stdout=stdout, stderr=stderr)


def _stub_run(monkeypatch: pytest.MonkeyPatch, **counts: str) -> None:
    """Stub `run()` so each `git rev-list --count` returns a chosen number.

    Keyed on the *revision argument* rather than answering from a queue. A queue
    is wrong the moment a guard issues an extra command: `check_up_to_date()`
    fetches before counting, so a positional stub silently shifts every answer by
    one and the test then asserts on the wrong branch. This one failed that way
    — asserting "not on origin/main" while the code reported "behind".
    """

    def go(*argv: str, **kwargs: Any) -> Any:
        if "rev-list" in argv:
            rev = argv[-1]
            return _done(counts.get(rev, "0"))
        return _done()

    monkeypatch.setattr(release, "run", go)


# --- fixtures ----------------------------------------------------------------


@pytest.fixture
def published_off(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """No version is on PyPI; record what was asked about."""
    asked: list[str] = []

    def fake(version: str) -> bool:
        asked.append(version)
        return False

    monkeypatch.setattr(release, "published", fake)
    return asked


@pytest.fixture
def on_a_clean_main(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the decision logic from this machine's actual git state.

    Each environment guard has its own test below. Stubbing the four here is
    what lets the rest exercise the part that is easy to get wrong and hard to
    see — which refusal happens *before* which write — with no remote involved,
    and without a developer who happens to have uncommitted work failing CI.
    """
    monkeypatch.setattr(release, "check_clean_tree", lambda: None)
    monkeypatch.setattr(release, "check_on_main", lambda: None)
    monkeypatch.setattr(release, "check_up_to_date", lambda: None)
    monkeypatch.setattr(release, "check_tag_absent", lambda tag: True)


@pytest.fixture
def no_writes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """Record every `run()` argv instead of executing it.

    Also stubs `bump()`, which is a real writer: it edits `pyproject.toml`, runs
    `uv lock`, and commits. A test that reaches it dirties the working tree of
    whoever is running the suite — which is exactly what happened the first time
    this fixture existed, leaving the repo's own version bumped by a test run.
    """
    seen: list[tuple[str, ...]] = []
    monkeypatch.setattr(release, "run", lambda *a, **k: seen.append(a) or _done())
    monkeypatch.setattr(release, "bump", lambda version: None)
    return seen


# --- the git-state guards ----------------------------------------------------


def test_a_dirty_tree_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(release, "run", lambda *a, **k: _done(" M pyproject.toml\n"))
    with pytest.raises(release.Failure, match="dirty"):
        release.check_clean_tree()


def test_a_release_off_a_feature_branch_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(release, "run", lambda *a, **k: _done("agent/work\n"))
    with pytest.raises(release.Failure, match="tagged on main"):
        release.check_on_main()


def test_a_main_behind_origin_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tagging a commit origin has not seen publishes something CI never gated."""
    _stub_run(monkeypatch, **{"HEAD..origin/main": "3"})
    with pytest.raises(release.Failure, match="behind origin/main"):
        release.check_up_to_date()


def test_an_unpushed_main_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not behind, but ahead: the tag would name a commit CI has never gated."""
    _stub_run(monkeypatch, **{"origin/main..HEAD": "2"})
    with pytest.raises(release.Failure, match="not on origin/main"):
        release.check_up_to_date()


def test_a_main_in_step_with_origin_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Negative control: the guard above is not refusing everything."""
    _stub_run(monkeypatch)
    release.check_up_to_date()


def test_a_lock_that_disagrees_is_refused() -> None:
    """`ci-pr` runs `uv sync --locked`, so this commit would be red for everyone."""
    with pytest.raises(release.Failure, match="uv.lock says"):
        release.check_lock_matches("999.1.1")


def test_the_lock_check_names_both_versions() -> None:
    with pytest.raises(release.Failure) as exc:
        release.check_lock_matches("999.1.1")
    message = str(exc.value)
    assert "999.1.1" in message
    assert release.declared_version() in message


def test_an_existing_tag_is_refused_with_the_commands_to_move_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rewriting a public ref is a decision, not something a re-run may do."""

    # Keyed on the ref being queried: the two calls differ only in the tag, so a
    # stub answering one fixed string would report both tags as present (or both
    # absent) and the test would pass without testing anything.
    def go(*argv: str, **kwargs: Any) -> Any:
        ref = argv[-1]
        found = "deadbeef\trefs/tags/v1\n" if ref.endswith("v1") else ""
        return _done(found)

    monkeypatch.setattr(release, "run", go)
    assert release.check_tag_absent("v1") is False
    assert release.check_tag_absent("v2") is True


# --- the PyPI guard ----------------------------------------------------------


def test_a_published_version_is_refused(
    monkeypatch: pytest.MonkeyPatch, on_a_clean_main: None
) -> None:
    monkeypatch.setattr(release, "published", lambda version: True)
    assert release.main(["--dry-run"]) == 1


def test_the_pypi_message_explains_that_names_are_permanent(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    on_a_clean_main: None,
) -> None:
    """A refusal is useless if it does not say what to do instead."""
    monkeypatch.setattr(release, "published", lambda version: True)
    release.main(["--dry-run"])
    message = capsys.readouterr().err
    assert "permanent" in message
    assert "yanking does not free" in message
    assert "--bump" in message


def test_a_404_from_pypi_means_not_published(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_404(url: str, timeout: int = 15) -> Any:
        raise urllib.error.HTTPError(
            url, 404, "Not Found", email.message.Message(), None
        )

    monkeypatch.setattr(release.urllib.request, "urlopen", raise_404)
    assert release.published("2026.1.1") is False


def test_a_server_error_from_pypi_is_not_read_as_not_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 500 is not a 404. Reading it as "free" would tag a version we cannot see."""

    def raise_500(url: str, timeout: int = 15) -> Any:
        raise urllib.error.HTTPError(url, 500, "boom", email.message.Message(), None)

    monkeypatch.setattr(release.urllib.request, "urlopen", raise_500)
    with pytest.raises(release.Failure, match="cannot reach PyPI"):
        release.published("2026.1.1")


def test_an_unreachable_pypi_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not "assume it is fine". Guessing is the failure this check exists for."""

    def no_route(url: str, timeout: int = 15) -> Any:
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(release.urllib.request, "urlopen", no_route)
    with pytest.raises(release.Failure, match="cannot reach PyPI"):
        release.published("2026.1.1")


def test_published_really_is_asked_about_pypi() -> None:
    """Pin the URL, so a typo cannot quietly make the guard always pass."""
    source = SCRIPT.read_text()
    assert "https://pypi.org/pypi/yang2sdk/{version}/json" in source


# --- notes -------------------------------------------------------------------


def test_notes_are_checked_before_anything_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal must leave the tree exactly as it was found."""
    target = tmp_path / "CHANGELOG.md"
    original = "## [Unreleased]\n\n## [1.0.0] - 2026-01-01\n\n- old\n"
    target.write_text(original)
    monkeypatch.setattr(changelog, "CHANGELOG", target)

    # `may_promote=True` is the interesting case: promotion is the one step that
    # writes, so the refusal has to happen before it, not after.
    with pytest.raises(release.Failure, match="nothing to promote"):
        release.prepare_notes("2026.1.2", may_promote=True, write=True)
    assert target.read_text() == original


def test_a_bare_tag_run_never_rewrites_the_changelog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without --bump the script only checks: someone may have written it by hand."""
    target = tmp_path / "CHANGELOG.md"
    original = "## [Unreleased]\n\n- pending work\n\n## [1.0.0] - 2026-01-01\n\n- old\n"
    target.write_text(original)
    monkeypatch.setattr(changelog, "CHANGELOG", target)

    with pytest.raises(release.Failure, match=r"\[Unreleased\]"):
        release.prepare_notes("1.0.0", may_promote=False, write=True)
    assert target.read_text() == original


def test_the_real_changelog_is_releasable_for_the_declared_version() -> None:
    """`main` is always in a releasable state for the version it declares."""
    version = str(
        tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]["version"]
    )
    text = (REPO_ROOT / "CHANGELOG.md").read_text()
    assert changelog.require_release_notes(text, version).notes(text)


# --- the whole thing, dry ---------------------------------------------------


def test_the_suite_never_writes_to_the_repository() -> None:
    """A test that bumps the version is a test that breaks the next run.

    Found the hard way: an early version of `no_writes` stubbed `run()` but not
    `bump()`, which is itself a writer, so a suite run left `pyproject.toml` and
    `uv.lock` modified and the *next* run failed on the dirt it had caused.
    """
    source = Path(__file__).read_text()
    assert 'monkeypatch.setattr(release, "bump"' in source, (
        "the no_writes fixture must stub bump(); it edits pyproject.toml, "
        "runs `uv lock`, and commits"
    )


def test_a_dry_run_reports_the_tag_and_writes_nothing(
    published_off: list[str],
    on_a_clean_main: None,
    no_writes: list[tuple[str, ...]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The rehearsal: no tag, no push, no commit, no re-lock."""
    assert release.main(["--dry-run"]) == 0
    assert published_off == [release.declared_version()]

    out = capsys.readouterr().out
    assert f"tag        v{release.declared_version()}" in out
    assert "dry run: nothing changed" in out
    # `uv lock --check` is read-only and *should* run; anything that writes to
    # the repo or the network must not have.
    readonly = {("uv", "lock", "--check"), ("git", "fetch")}
    mutating = [
        a
        for a in no_writes
        if a[:1] == ("uv",)
        and a not in (readonly - {("git", "fetch")})
        or a[:2] in {("git", "tag"), ("git", "push"), ("git", "commit"), ("git", "add")}
    ]
    assert not mutating, f"a dry run wrote: {mutating}"


def test_a_dry_run_never_edits_the_changelog_on_disk(
    published_off: list[str], on_a_clean_main: None
) -> None:
    """--dry-run previews the promotion; it must not perform it."""
    before = (REPO_ROOT / "CHANGELOG.md").read_bytes()
    assert release.main(["--bump", "2026.10.16.1", "--dry-run"]) == 0
    assert (REPO_ROOT / "CHANGELOG.md").read_bytes() == before


def test_the_dry_run_of_a_bump_is_not_blocked_by_the_bump_it_previews(
    published_off: list[str], on_a_clean_main: None
) -> None:
    """Regression: it checked the lock against a version only `bump()` creates.

    `bump()` is what re-locks, so `--bump --dry-run` could never satisfy a lock
    check against the *target* — every rehearsal refused, which is exactly when a
    dry run is wanted most. Found by running it against a scratch remote, not by
    reading it.
    """
    assert release.main(["--bump", "2026.10.16.1", "--dry-run"]) == 0
    assert published_off == ["2026.10.16.1"]


# --- the two phases ----------------------------------------------------------


def test_a_bump_does_not_tag(
    published_off: list[str],
    on_a_clean_main: None,
    no_writes: list[tuple[str, ...]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bump is a code change; the ruleset requires it to be reviewed.

    Tagging from the same run could never work here — the bump lands on a branch,
    the ruleset refuses a direct push to `main`, and the tag must name a commit
    CI has gated. Splitting the phases is what turns "push a tag" into one
    command instead of five.
    """
    # `bump()` is stubbed by no_writes, so both the declared version and the
    # post-bump lock check have to be told the bump took; otherwise the script
    # correctly reports that it did not.
    monkeypatch.setattr(release, "declared_version", lambda: "2026.10.16.1")
    monkeypatch.setattr(release, "check_lock_matches", lambda version: None)
    assert release.main(["--bump", "2026.10.16.1"]) == 0
    assert not [a for a in no_writes if a[:2] == ("git", "tag")]
    assert not [a for a in no_writes if a[:2] == ("git", "push")]


def test_a_dry_run_of_the_tag_phase_never_pushes(
    published_off: list[str],
    on_a_clean_main: None,
    no_writes: list[tuple[str, ...]],
) -> None:
    """Regression: the phase split dropped `--dry-run` and the tag got pushed.

    `--dry-run` existed only in the bump phase, so the *rehearsal* of the one
    irreversible step silently performed it. `no_writes` recorded the argv instead
    of running it, which is the only reason the repo survived to have the bug
    found; before that fixture, this would have published a tag from a "dry run".
    """
    assert release.main(["--dry-run"]) == 0
    assert not [a for a in no_writes if a[:2] == ("git", "tag")]
    assert not [a for a in no_writes if a[:2] == ("git", "push")]


def test_a_bump_is_allowed_off_main(
    monkeypatch: pytest.MonkeyPatch, on_a_clean_main: None
) -> None:
    """`--bump` runs on a feature branch, so it must not demand `main`.

    Pins the phase split: `check_on_main()` belongs to the tag phase only, and
    demanding it during a bump would make the documented flow impossible to
    start.
    """
    monkeypatch.setattr(release, "published", lambda v: False)
    monkeypatch.setattr(release, "prepare_notes", lambda *a, **k: None)
    monkeypatch.setattr(release, "bump", lambda v: None)
    monkeypatch.setattr(release, "declared_version", lambda: "2026.10.16.1")
    monkeypatch.setattr(release, "check_lock_matches", lambda v: None)
    monkeypatch.setattr(release, "check_on_main", _forbid("check_on_main"))
    monkeypatch.setattr(release, "check_up_to_date", _forbid("check_up_to_date"))
    assert release.main(["--bump", "2026.10.16.1"]) == 0


def test_the_tag_phase_requires_main(
    monkeypatch: pytest.MonkeyPatch, on_a_clean_main: None
) -> None:
    """The tag must name a commit `main` holds and CI has gated."""
    monkeypatch.setattr(release, "published", lambda v: False)
    monkeypatch.setattr(release, "check_on_main", _refuse("not on main"))
    assert release.main([]) == 1


def _refuse(message: str) -> Any:
    def go(*args: object, **kwargs: object) -> None:
        raise release.Failure(message)

    return go


def _forbid(name: str) -> Any:
    """Fail loudly if a guard runs in a phase that must not need it."""

    def go(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"{name} must not run in this phase")

    return go


def test_the_script_is_never_shipped_in_the_wheel() -> None:
    """Release tooling must not become downstream install weight."""
    targets = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["tool"][
        "hatch"
    ]["build"]["targets"]
    assert targets["wheel"]["packages"] == ["src/yang2sdk"]
    assert "scripts" not in str(targets)


def test_every_refusal_reaches_the_operator_not_a_traceback(
    published_off: list[str],
    on_a_clean_main: None,
    no_writes: list[tuple[str, ...]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A refused release must read as a sentence, never as a stack trace."""

    def boom(version: str) -> None:
        raise release.Failure("boom")

    monkeypatch.setattr(release, "check_lock_matches", boom)
    assert release.main(["--dry-run"]) == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("Error: boom")
    assert "Traceback" not in captured.err
