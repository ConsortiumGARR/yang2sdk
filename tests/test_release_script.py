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
import hashlib
import importlib.util
import re
import subprocess
import sys
import tomllib
import urllib.error
from collections.abc import Iterable, Iterator
from datetime import UTC, date
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

# The writers, captured before any fixture replaces them, so a test can assert
# the fixture really did. `run()` is included: it is the only one of the three
# that reaches git.
_WRITERS = ("run", "bump", "prepare_notes")
_ORIGINAL = {name: getattr(release, name) for name in _WRITERS}

# Everything a bump is allowed to change. `no_writes` digests these on entry and
# again on teardown, so a writer it does not know about is a failing test rather
# than a dirty tree.
BUMP_WRITES = ("pyproject.toml", "uv.lock", "CHANGELOG.md")


def _digests() -> dict[str, str]:
    return {
        name: hashlib.sha256((REPO_ROOT / name).read_bytes()).hexdigest()
        for name in BUMP_WRITES
    }


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
    # `tag_exists` rather than `check_tag_absent`, which is now only a wrapper:
    # the derivation asks the same question of every candidate version, and a
    # stub on the wrapper would leave `next_version` reaching for the real
    # remote from inside a unit test.
    monkeypatch.setattr(release, "tag_exists", lambda tag: False)


@pytest.fixture
def no_writes(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[str, ...]]]:
    """Record every `run()` argv instead of executing it — and write nothing.

    Both halves were learned the hard way. `bump()` edits `pyproject.toml`, runs
    `uv lock` and commits, so the first version of this fixture — which stubbed
    only `run()` — left the repository's own version bumped by a suite run. Then
    `prepare_notes(write=True)`, reached by `test_a_bump_does_not_tag`, promoted
    the repository's own `CHANGELOG.md` and left it rewritten, visible only as a
    diff nobody made.

    Stubbing a named writer is a promise to remember the next one, and that
    promise is what failed twice. The teardown digest is a check that does not
    depend on remembering: a writer added later fails the test that reaches it,
    and names the file, instead of dirtying the tree for the next run.
    """
    before = _digests()
    seen: list[tuple[str, ...]] = []
    monkeypatch.setattr(release, "run", lambda *a, **k: seen.append(a) or _done())
    monkeypatch.setattr(release, "bump", lambda version: None)
    monkeypatch.setattr(release, "prepare_notes", lambda *a, **k: None)
    yield seen

    after = _digests()
    dirty = [name for name in BUMP_WRITES if before[name] != after[name]]
    assert not dirty, (
        f"a test using `no_writes` modified {', '.join(dirty)}. The fixture "
        "stubs `run()`, `bump()` and `prepare_notes()`; whatever wrote this is a "
        "writer it does not know about, and the next suite run would fail on the "
        "dirt left behind."
    )


# A fixed day, deliberately *not* `date.today()`: a fixture that tracks the wall
# clock is the defect this exists to remove.
FROZEN_DAY = date(2026, 10, 16)


def _freeze_clock(monkeypatch: pytest.MonkeyPatch, module: Any, day: date) -> None:
    """Pin `module.datetime.now()` to `day`; every other call stays real.

    Subclassed, not replaced, so `fromisoformat`/`combine` keep working.
    """
    real = module.datetime

    class Frozen(real):  # type: ignore[misc, valid-type]
        @classmethod
        def now(cls, tz: Any = None) -> Any:
            # Noon, so a caller in a DST zone cannot hit a non-existent wall time.
            return real(day.year, day.month, day.day, 12, 0, tzinfo=tz)

    monkeypatch.setattr(module, "datetime", Frozen)


@pytest.fixture
def on_a_frozen_day(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A `--bump` rehearsal independent of the calendar and of the repository.

    Three uncontrolled inputs on that path:
      * `next_version()` reads `datetime.now(UTC)` — that is how `main()` calls it;
      * `changelog.promote()` stamps the heading date, which the notes gate then
        checks against the version, so `changelog`'s clock must move too;
      * `prepare_notes()` promotes `## [Unreleased]`, empty on main between
        releases.

    Uncontrolled, the derivation test derived *today's* version against the real
    CHANGELOG.md, so it passed only on 2026-10-16 and thereafter failed every run
    with "nothing to promote", reddening `offline-tests` on all open PRs.
    """
    _freeze_clock(monkeypatch, release, FROZEN_DAY)
    _freeze_clock(monkeypatch, changelog, FROZEN_DAY)

    target = tmp_path / "CHANGELOG.md"
    target.write_text(
        "# Changelog\n\n## [Unreleased]\n\n### Added\n\n- something worth releasing.\n"
    )
    monkeypatch.setattr(changelog, "CHANGELOG", target)


@pytest.fixture
def a_releasable_changelog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A changelog that is already releasable, isolated from the repository.

    Content under `## [Unreleased]` is the normal state between releases; the
    release-time gate is supposed to refuse it, but a *dry run of a bump* is
    not a release. Pointing these rehearsals at the live file made them fail
    the moment anyone wrote down a change, which is the opposite of what the
    gate exists for. The temp file keeps their subject -- whether a rehearsal
    writes anything -- independent of release-day state.
    """
    target = tmp_path / "CHANGELOG.md"
    target.write_text(
        "# Changelog\n\n## [Unreleased]\n\n## [2026.10.16.1] - 2026-10-16\n\n- old\n"
    )
    monkeypatch.setattr(changelog, "CHANGELOG", target)
    return target


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


# --- the derived version ------------------------------------------------------


def _stub_taken(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pypi: Iterable[str] = (),
    tags: Iterable[str] = (),
) -> None:
    """Answer both collision questions from fixed sets, with no network."""
    published_here, tagged_here = set(pypi), set(tags)
    monkeypatch.setattr(release, "published", lambda v: v in published_here)
    monkeypatch.setattr(release, "tag_exists", lambda t: t in tagged_here)


def test_the_first_release_of_a_day_is_the_date_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_taken(monkeypatch)
    version, skipped = release.next_version(date(2026, 11, 1))
    assert version == "2026.11.1"
    assert skipped == []


def test_a_second_release_the_same_day_takes_the_next_sequence_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The question this answers: "is it `.1` or `.2` today?" Nobody should have to ask."""
    _stub_taken(monkeypatch, pypi={"2026.10.16"})
    assert release.next_version(date(2026, 10, 16))[0] == "2026.10.16.1"


def test_the_sequence_keeps_counting_past_the_first_re_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_taken(monkeypatch, pypi={"2026.10.16", "2026.10.16.1"})
    assert release.next_version(date(2026, 10, 16))[0] == "2026.10.16.2"


def test_a_tag_alone_blocks_a_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing was published under `2026.10.16.1`, but a tag claims it.

    The tag is movable and the version is not, so this is the case where the
    operator may disagree with the derivation. It must therefore say *why* it
    skipped, not just pick the next number.
    """
    _stub_taken(monkeypatch, pypi={"2026.10.16"}, tags={"v2026.10.16.1"})
    version, skipped = release.next_version(date(2026, 10, 16))
    assert version == "2026.10.16.2"
    assert skipped == [
        "2026.10.16 (already on PyPI)",
        "2026.10.16.1 (tag v2026.10.16.1 exists on origin)",
    ]


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 1, 5), "2026.1.5"),
        (date(2026, 10, 16), "2026.10.16"),
        (date(2026, 12, 31), "2026.12.31"),
    ],
)
def test_the_derived_version_is_not_zero_padded(
    monkeypatch: pytest.MonkeyPatch, day: date, expected: str
) -> None:
    """PEP 440 orders these numerically, so padding would only help a filename sort."""
    _stub_taken(monkeypatch)
    assert release.next_version(day)[0] == expected


def test_the_derived_version_round_trips_through_the_changelog_date_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two implementations of "which date is this version?", asserted against each other.

    `next_version` writes the date into the version; `changelog.expected_date`
    reads it back out and the gate compares it to the heading. Disagree and the
    promotion stamps a heading that the gate it feeds then refuses — so the
    round trip is the property, not either half of it.
    """
    _stub_taken(monkeypatch)
    for day in (date(2026, 1, 5), date(2026, 10, 16), date(2026, 12, 31)):
        version, _ = release.next_version(day)
        assert changelog.expected_date(version) == day.isoformat()


def test_the_default_date_is_taken_in_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    """A release cut at 23:30 must not be named for a different day by zone.

    `changelog.promote` stamps UTC for exactly this reason. If the version were
    derived in local time the two could disagree, and the gate would refuse the
    release the operator had just derived — for a date neither of them chose.
    Asserted by capturing the `tz` the code passes, so it holds at any hour.
    """
    seen: dict[str, Any] = {}
    real = release.datetime

    class Spy(real):  # type: ignore[misc, valid-type]
        @classmethod
        def now(cls, tz: Any = None) -> Any:
            seen["tz"] = tz
            return real.now(tz=tz)

    _stub_taken(monkeypatch)
    monkeypatch.setattr(release, "datetime", Spy)
    release.next_version()
    assert seen["tz"] is UTC


def test_an_exhausted_sequence_says_so_instead_of_looping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refuse with an explanation; a bound is what stops this being a hang."""
    _stub_taken(monkeypatch, pypi={"x"})
    monkeypatch.setattr(release, "published", lambda v: True)
    with pytest.raises(release.Failure, match="every version from 2026.10.16"):
        release.next_version(date(2026, 10, 16))


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


def test_the_real_changelog_is_well_formed() -> None:
    """What holds on `main` at all times, which is less than "releasable".

    The declared version has notes, and every dated heading in the file agrees
    with its own version. The `## [Unreleased]`-must-be-empty rule is *not*
    asserted here, deliberately: it is a release-time rule, and between releases
    unreleased content is the normal state. It is also the state this repository
    spent eight commits failing to record, with `## [Unreleased]` sitting empty
    while `main` changed — which is what the gate exists to catch. An assertion
    here would have gone red the moment anyone wrote down what they had done.
    The release-time rule is pinned in `tests/test_changelog.py` and re-applied
    by `release.yaml`'s build job.
    """
    version = str(
        tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]["version"]
    )
    text = (REPO_ROOT / "CHANGELOG.md").read_text()

    section = changelog.find(text, version)
    assert section is not None, f"no section for the declared version {version}"
    assert section.notes(text)

    for other in changelog.parse(text):
        want = changelog.expected_date(other.version)
        if want is not None:
            assert other.date == want, (
                f"'## [{other.version}] - {other.date}' should be dated {want}"
            )


def test_the_real_changelog_becomes_releasable_once_the_version_is_promoted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The release-time invariant, against the repository's own file.

    Content under `## [Unreleased]` is normal between releases, so "main is
    releasable" is not the invariant and never was -- asserting it here is what
    made the previous version of this test fail the moment a change was written
    down. The invariant is: *main plus the promotion the bump performs* is
    releasable, which is the state the tag is pushed from.

    That promotion only exists when something is pending, and immediately after
    a release nothing is -- `[Unreleased]` is empty and `main` is *already*
    releasable for the version it declares. The test says so and skips rather
    than passing vacuously: the state it cannot rehearse is the state where the
    release already happened, and `test_the_real_changelog_is_well_formed`
    covers that one.
    """
    text = (REPO_ROOT / "CHANGELOG.md").read_text()
    pending = changelog.unreleased(text)
    if pending is None or not pending.notes(text):
        pytest.skip(
            "nothing is pending under '## [Unreleased]', so there is no "
            "promotion to rehearse: the tree already declares a version and "
            "documents it, which is the state "
            "test_the_real_changelog_is_well_formed covers"
        )

    _stub_taken(monkeypatch, pypi={"2026.10.16"}, tags={"v2026.10.16.1"})
    version, _ = release.next_version()

    promoted = changelog.promote(text, version)
    assert changelog.require_release_notes(promoted, version).notes(promoted)

    left_behind = changelog.unreleased(promoted)
    assert left_behind is not None, "promotion must leave a fresh [Unreleased]"
    assert not left_behind.notes(promoted), (
        "promotion must leave [Unreleased] empty, or the next release ships "
        "this one's notes again"
    )


# --- the whole thing, dry ---------------------------------------------------


def test_the_no_writes_fixture_stubs_every_writer(
    no_writes: list[tuple[str, ...]],
) -> None:
    """Pinned by its effect, not by grepping its own source.

    The previous version of this test asserted that the string `setattr(release,
    "bump"` appeared in this file. It did, and it did not help: `prepare_notes`
    is a second writer, it was not stubbed, and `test_a_bump_does_not_tag`
    promoted the repository's own `CHANGELOG.md` while this test stayed green.
    Found by hashing that file before and after a run, not by reading the fixture.
    """
    for name in _WRITERS:
        assert getattr(release, name) is not _ORIGINAL[name], (
            f"`no_writes` must stub `{name}()`; a test that reaches the real one "
            "writes to the repository"
        )


def test_a_dry_run_reports_the_tag_and_writes_nothing(
    published_off: list[str],
    on_a_clean_main: None,
    no_writes: list[tuple[str, ...]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The rehearsal: no tag, no push, no commit, no re-lock.

    `prepare_notes` is stubbed by `no_writes`; its own rules are tested in
    `tests/test_changelog.py`, and the repository's live changelog is not
    releasable between releases by design — content under `## [Unreleased]` is
    the normal state and the gate refuses it on purpose. Running the real one
    here would make this test measure how recently somebody wrote a changelog
    entry instead of whether the tag phase writes anything.
    """
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
    published_off: list[str],
    on_a_clean_main: None,
    a_releasable_changelog: Path,
) -> None:
    """--dry-run previews the promotion; it must not perform it."""
    before = a_releasable_changelog.read_bytes()
    repository_before = (REPO_ROOT / "CHANGELOG.md").read_bytes()
    assert release.main(["--bump", "2026.10.16.1", "--dry-run"]) == 0
    assert a_releasable_changelog.read_bytes() == before
    assert (REPO_ROOT / "CHANGELOG.md").read_bytes() == repository_before


def test_the_dry_run_of_a_bump_is_not_blocked_by_the_bump_it_previews(
    published_off: list[str],
    on_a_clean_main: None,
    a_releasable_changelog: Path,
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


def test_bump_with_no_argument_derives_the_version(
    on_a_clean_main: None,
    on_a_frozen_day: None,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The change in one test: `--bump` takes no version, and still gets one.

    `2026.10.16` is on PyPI, so the derivation must reach `2026.10.16.1`. Both
    halves must hold to exit 0: a derivation returning the base version, or one
    computed then ignored, fails here.

    `on_a_frozen_day` is load-bearing — without it this passed only on 2026-10-16.
    """
    _stub_taken(monkeypatch, pypi={"2026.10.16"})
    assert release.main(["--bump", "--dry-run"]) == 0

    out = capsys.readouterr().out
    assert "version    2026.10.16.1" in out
    assert "skipped    2026.10.16 (already on PyPI)" in out


def test_a_stale_tag_on_an_explicit_version_is_reported_not_refused(
    published_off: list[str],
    on_a_clean_main: None,
    a_releasable_changelog: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the operator knows whether to move the tag or burn the number.

    The derivation never lands here — it skips tag-blocked candidates — so this
    path is reached only when a version was named by hand. Refusing would be
    wrong at this point: the tag is not pushed until the other phase, which is
    where an existing one becomes fatal.
    """
    monkeypatch.setattr(release, "tag_exists", lambda t: t == "v2026.10.16.1")
    assert release.main(["--bump", "2026.10.16.1", "--dry-run"]) == 0
    assert (
        "skipped    2026.10.16.1 (tag v2026.10.16.1 exists on origin)"
        in capsys.readouterr().out
    )


def test_a_dry_run_of_the_derived_version_writes_nothing_either(
    on_a_clean_main: None,
    on_a_frozen_day: None,
    no_writes: list[tuple[str, ...]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dry run that reaches the network does not get to write on the way back.

    `on_a_frozen_day` keeps the wall clock out. This test passes without it *by
    accident*: `no_writes` stubs `prepare_notes`, so the derived version never
    reaches the notes gate — which is how the sibling test's date dependency went
    unnoticed.
    """
    _stub_taken(monkeypatch, pypi={"2026.10.16"})
    assert release.main(["--bump", "--dry-run"]) == 0
    assert not [a for a in no_writes if a[:2] in {("git", "tag"), ("git", "push")}]


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


# --- the post-publish jobs, parsed from the workflow ------------------------


def _workflow() -> Any:
    import yaml

    path = REPO_ROOT / ".github" / "workflows" / "release.yaml"
    return yaml.safe_load(path.read_text())


def _job_shell(name: str) -> str:
    return "\n".join(
        str(s["run"]) for s in _workflow()["jobs"][name]["steps"] if "run" in s
    )


def _job_text(name: str) -> str:
    """Every string a job carries, `env:` values included.

    `needs.build.outputs.version` may legitimately reach the shell through an
    `env:` entry rather than inline, and `_job_shell` cannot see that.
    """
    import yaml

    return yaml.safe_dump(_workflow()["jobs"][name], sort_keys=True)


def test_verify_published_never_checks_out_the_workspace() -> None:
    """It must install from PyPI, or the check is theatre.

    A `checkout` step plus an editable install from the tree would make every
    assertion below pass while testing the source tree — the claim this job
    exists to stop making, since the sdist shipped the package twice (3b22730)
    with every source-tree gate green.
    """
    steps = _workflow()["jobs"]["verify-published"]["steps"]
    assert not [
        s for s in steps if str(s.get("uses", "")).startswith("actions/checkout")
    ]


def test_the_post_publish_jobs_read_the_build_job_output() -> None:
    """Both halves of reading a job output, neither visible to a grep for it.

    `steps.version` is job-local, so `${{ steps.version.outputs.value }}` outside
    `build` is "". And `needs.build` needs `build` in `needs:` — the context holds
    direct dependencies only, so `verify-published` with `needs: publish` again
    got "" and ran `uv pip install "yang2sdk=="` (run 38960272123). Assert the
    wiring, not the text.
    """
    for job in ("verify-published", "github-release"):
        text = _job_text(job)
        assert "steps.version.outputs.value" not in text, job
        assert "needs.build.outputs.version" in text, job
        assert "build" in _needs(job), (
            f"{job} reads `needs.build.outputs.version` but does not list "
            f"`build` in `needs:` (it has {_needs(job)}), so the expression "
            "resolves to the empty string at runtime"
        )


def _needs(name: str) -> list[str]:
    """A job's `needs:` as a list; YAML allows a scalar or a list."""
    raw = _workflow()["jobs"][name].get("needs", [])
    return [raw] if isinstance(raw, str) else list(raw)


def test_needs_is_normalised_for_both_yaml_spellings() -> None:
    """Pin both branches, or half the assertions silently inspect nothing.

    `publish` writes `needs: build` (scalar), the post-publish jobs write lists.
    """
    assert _needs("publish") == ["build"]
    assert _needs("github-release") == ["build", "publish"]


def test_verify_published_installs_the_exact_published_version() -> None:
    """Not `yang2sdk` unpinned: that would install whatever is newest."""
    assert "yang2sdk==${{ needs.build.outputs.version }}" in _job_shell(
        "verify-published"
    )


# Contexts GitHub substitutes inside `${{ ... }}`. A workflow string that names
# one of them with no leading `$` is a typo, not shell.
_GITHUB_CONTEXTS = (
    "needs",
    "steps",
    "github",
    "secrets",
    "matrix",
    "env",
    "inputs",
    "vars",
    "runner",
    "job",
    "strategy",
)


def _workflow_strings(node: Any) -> Iterator[str]:
    """Every string scalar in a parsed workflow; mapping keys are static text."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _workflow_strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _workflow_strings(value)


def test_no_workflow_carries_a_github_expression_missing_its_dollar() -> None:
    """The v2026.10.21 release died on exactly one missing `$`.

    Written inline as `{{ needs.build.outputs.version }}` it was a *shell
    literal*, so GitHub substituted nothing, the `case` pattern never matched,
    and the job announced `expected version {{ needs.build.outputs.version }}`
    after the installed CLI had correctly printed `yang2sdk 2026.10.21`.

    Docker's `--format '{{.Names}}'` is a real shell argument and must not
    trip this, which is why the pattern names a GitHub context explicitly
    instead of matching a bare `{{`.
    """
    import yaml

    expression = re.compile(r"(?<!\$)\{\{\s*(?:" + "|".join(_GITHUB_CONTEXTS) + r")\.")
    offenders = [
        f"{path.name}: {text.strip()}"
        for path in sorted((REPO_ROOT / ".github" / "workflows").glob("*.yaml"))
        for text in _workflow_strings(yaml.safe_load(path.read_text()))
        if expression.search(text)
    ]
    assert not offenders, (
        "these workflow strings hold a GitHub expression without its `$`, so "
        "GitHub will substitute nothing and shell will see the text verbatim: "
        + "; ".join(offenders)
    )


def test_the_version_check_compares_against_env_not_an_inline_expression() -> None:
    """The comparison must consume the substituted value, and refuse an empty one.

    `*""*` matches every string, so an unresolved job output has to be an
    explicit failure or this step passes while proving nothing.
    """
    steps = _workflow()["jobs"]["verify-published"]["steps"]
    step = next(s for s in steps if "yang2sdk --version" in str(s.get("run", "")))
    assert step["env"]["EXPECTED"] == "${{ needs.build.outputs.version }}"
    assert "$EXPECTED" in step["run"]
    assert 'test -n "$EXPECTED"' in step["run"], (
        "an empty EXPECTED would make the `case` pattern match anything"
    )


def test_the_generate_step_actually_generates_and_imports() -> None:
    """Each part is the claim it is meant to prove."""
    shell = _job_shell("verify-published")
    assert "yang2sdk --protocol restconf" in shell
    assert "from verify_1_0_0 import RestconfClient" in shell, (
        "the job must import the generated client; generating without importing "
        "is the failure `_validate_generated` exists to catch"
    )
    assert "compileall" in shell


def test_the_release_notes_come_from_the_changelog_section() -> None:
    """One text read twice, so the page and the repository cannot drift."""
    shell = _job_shell("github-release")
    assert "scripts/changelog.py --notes" in shell
    assert "--notes-file" in shell
    assert "--verify-tag" in shell


def test_no_job_uses_a_uv_flag_that_does_not_exist() -> None:
    """`uv pip install --no-project` was written here and is not a real flag.

    Found by running the step on uv 0.11.7, which rejected it outright. Checking
    the few flags these jobs use is cheaper than finding it during a release,
    and the fix was to delete the flag rather than to pin uv.
    """
    allowed = {("uv", "venv"), ("uv", "pip", "install"), ("uv", "pip", "--python")}
    for line in _job_shell("verify-published").splitlines():
        tokens = line.strip().split()
        if tokens[:1] == ["uv"]:
            assert any(tuple(tokens[: len(k)]) == k for k in allowed), (
                f"unrecognised uv invocation: {line.strip()}"
            )


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
