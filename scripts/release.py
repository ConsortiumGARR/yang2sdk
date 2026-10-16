#!/usr/bin/env python3
"""Create the release tag, derived from `pyproject.toml` rather than typed.

Why this exists: `release.yaml` asserts `tag == pyproject version`, which is
the right guard and the wrong interface. It fired on 2026-10-16 because the
tag had been typed by hand one commit before the version bump landed:

    tag v2026.10.16.1 implies 2026.10.16.1 but pyproject.toml says 2026.10.16

A guard that can only say no has not removed the failure, it has moved it to a
human. So the tag is computed from the single source of truth here, and every
way this can go wrong is checked BEFORE anything is pushed:

  * the working tree is dirty, or the branch is not main, or main is behind
    origin -- refuse;
  * `uv.lock` disagrees with `pyproject.toml` -- refuse, because `ci-pr` runs
    `uv sync --locked` and would go red on the commit we just tagged;
  * `CHANGELOG.md` does not document this version -- refuse. This is the gap
    that actually bit: eight commits landed on `main` after `v2026.10.16`, two
    of them breaking, while `## [Unreleased]` sat empty. A version number is a
    claim about what changed, and an undocumented one cannot be checked. The
    rules live in `scripts/changelog.py` and are enforced again in CI, so a tag
    pushed around this script is still refused;
  * the tag already exists -- refuse, so a re-run cannot silently rewrite a
    public ref;
  * the version is already on PyPI -- refuse, because PyPI filenames are
    permanent and a re-upload fails with 400 File already exists. Yanking does
    not free the name. This is the check that would have caught the original
    "republish 2026.10.16" request.

Usage — two phases, because the version bump is a code change and this repo's
ruleset requires a pull request for `main`::

    uv run python scripts/release.py --bump 2026.11.1   # on a branch: notes, bump, commit
    #   ... open the PR, merge it ...
    uv run python scripts/release.py                   # on main: derive the tag, push it
    uv run python scripts/release.py --dry-run         # check only, change nothing

That is one command and a review for the tag, which is the whole point: the
failure this script exists to prevent was a tag typed one commit ahead of the
version it named. The bump goes through review because the bump is what the tag
will name; the tag itself needs none, since it is derived and every check on it
has already run.

Not shipped in the wheel (it lives outside `src/`), so it costs a consumer
nothing.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

# Importable as a sibling script. `python scripts/release.py` already puts
# `scripts/` on sys.path[0], but leaning on that makes the module untestable from
# anywhere else, so it is stated rather than assumed.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import changelog

REPO = Path(__file__).resolve().parent.parent
PYPI_URL = "https://pypi.org/pypi/yang2sdk/{version}/json"


class Failure(Exception):
    """A pre-flight check failed. Nothing has been pushed."""


def run(*argv: str, capture: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        cwd=REPO,
        capture_output=capture,
        text=True,
        check=False,
    )


def declared_version() -> str:
    """The one source of truth: `[project] version` in pyproject.toml."""
    return str(
        tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["version"]
    )


def locked_version() -> str | None:
    """`uv.lock` records the editable root's version, and CI enforces it."""
    text = (REPO / "uv.lock").read_text()
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == 'name = "yang2sdk"':
            for follow in lines[i : i + 4]:
                if follow.strip().startswith("version"):
                    return follow.split("=", 1)[1].strip().strip('"')
    return None


def published(version: str) -> bool:
    """Is this version already on PyPI? Filenames there are permanent."""
    try:
        with urllib.request.urlopen(PYPI_URL.format(version=version), timeout=15) as r:
            return r.status == 200
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise Failure(f"cannot reach PyPI to check {version}: HTTP {exc.code}") from exc
    except OSError as exc:
        raise Failure(f"cannot reach PyPI to check {version}: {exc}") from exc


def check_clean_tree() -> None:
    dirty = run("git", "status", "--porcelain").stdout.strip()
    if dirty:
        raise Failure(f"working tree is dirty:\n{dirty[:800]}")


def check_on_main() -> None:
    branch = run("git", "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if branch != "main":
        raise Failure(
            f"releases are tagged on main, not {branch!r}. "
            "Merge the branch first: the tag must point at a commit CI has gated."
        )


def check_up_to_date() -> None:
    run("git", "fetch", "--quiet", "origin")
    behind = run("git", "rev-list", "--count", "HEAD..origin/main").stdout.strip()
    if behind != "0":
        raise Failure(
            f"local main is {behind} commit(s) behind origin/main; pull first"
        )
    unpushed = run("git", "rev-list", "--count", "origin/main..HEAD").stdout.strip()
    if unpushed != "0":
        raise Failure(f"{unpushed} commit(s) not on origin/main; push before tagging")


def check_lock_matches(version: str) -> None:
    locked = locked_version()
    if locked != version:
        raise Failure(
            f"uv.lock says {locked!r} but pyproject.toml says {version!r}. "
            "Run `uv lock` and commit it -- `ci-pr` runs `uv sync --locked`."
        )
    result = run("uv", "lock", "--check")
    if result.returncode != 0:
        raise Failure(f"`uv lock --check` failed:\n{result.stderr[-500:]}")


def check_tag_absent(tag: str) -> bool:
    exists = (
        run("git", "ls-remote", "--tags", "origin", f"refs/tags/{tag}").stdout.strip()
        != ""
    )
    return not exists


def bump(version: str) -> None:
    """Write the version into pyproject.toml, re-lock, commit.

    `CHANGELOG.md` is committed in the same commit when it was promoted here, so
    the tree is clean again by the time anything is tagged. Splitting them would
    put a half-finished release on main.
    """
    path = REPO / "pyproject.toml"
    text = path.read_text()
    old = declared_version()
    updated = text.replace(f'version = "{old}"', f'version = "{version}"', 1)
    if updated == text:
        raise Failure(f'could not find version = "{old}" in pyproject.toml')
    path.write_text(updated)

    lock = run("uv", "lock")
    if lock.returncode != 0:
        raise Failure(f"`uv lock` failed:\n{lock.stderr[-500:]}")

    run("git", "add", "pyproject.toml", "uv.lock")
    committed = run(
        "git",
        "commit",
        "-m",
        f"release: {old} -> {version}\n\nVersion bump for the\n"
        f"release tag. `uv.lock` records the editable root's version and\n"
        f"`ci-pr` runs `uv sync --locked`, so the two must move together.",
    )
    if committed.returncode != 0:
        raise Failure(
            f"git commit failed:\n{committed.stdout[-400:]}{committed.stderr[-400:]}"
        )
    print(f"committed version {old} -> {version}")


def prepare_notes(version: str, may_promote: bool, write: bool) -> None:
    """Make `CHANGELOG.md` releasable for `version`, or explain why it is not.

    With `--bump`, an unwritten `[Unreleased]` is promoted into the version's
    section -- the heading is mechanical, the prose is the author's. Without
    `--bump` nothing is promoted: a bare tag run only checks, so it can never
    rewrite the changelog of a release someone else wrote by hand.

    `write=False` is the dry run. It reports the promotion it *would* make and
    validates the result in memory, because a dry run that refuses for something
    the real run would fix is lying about the release.
    """
    text = changelog.CHANGELOG.read_text()
    if changelog.find(text, version) is None and may_promote:
        try:
            promoted = changelog.promote(text, version)
        except changelog.ChangelogError as exc:
            raise Failure(str(exc)) from exc
        text = promoted
        if write:
            changelog.CHANGELOG.write_text(promoted)
            run("git", "add", "CHANGELOG.md")
        print(
            f"promote    [Unreleased] -> ## [{version}]"
            f"{'' if write else ' (dry run: not written)'}"
        )
    try:
        changelog.require_release_notes(text, version)
    except changelog.ChangelogError as exc:
        raise Failure(str(exc)) from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Tag a release from pyproject.toml's version.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage::")[-1],
    )
    parser.add_argument(
        "--bump",
        metavar="VERSION",
        help="Write VERSION into pyproject.toml, re-lock, commit, then tag.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run every check and print the tag; change nothing.",
    )
    args = parser.parse_args(argv)

    try:
        check_clean_tree()

        # `--bump` is a *code change* and belongs on a branch, because this repo's
        # ruleset requires a pull request for `main` (it is not ceremony: the
        # bump is what the release tag names, so it is exactly the commit that
        # deserves review). The tag is the only step taken from a clean main.
        # Doing both in one run -- as the first version of this script did --
        # could therefore never succeed here, and the two-phase shape is what
        # makes "push a tag" one command instead of five.
        if args.bump:
            return _bump_phase(args)
        return _tag_phase(dry_run=bool(args.dry_run))

    except Failure as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def _bump_phase(args: argparse.Namespace) -> int:
    """Write the version and the notes, commit, and stop before the tag."""
    target = str(args.bump)
    if published(target):
        raise Failure(
            f"{target} is already published on PyPI. PyPI filenames are "
            "permanent -- a re-upload fails with 400 File already exists, and "
            "yanking does not free the name. Pick a new version "
            "(e.g. the next date under CalVer) and pass --bump."
        )

    if args.dry_run:
        prepare_notes(target, may_promote=True, write=False)
        # The bump has not run, so `uv.lock` cannot yet carry the new version --
        # `bump()` is what re-locks it. Checking the lock against `target` here
        # would refuse every `--bump --dry-run`, which makes the rehearsal
        # useless precisely when it is wanted most: before committing a
        # release. Check what the *current* commit must satisfy instead --
        # `ci-pr` runs `uv sync --locked` on it.
        check_lock_matches(declared_version())
        print(f"version    {target} (would bump from {declared_version()})")
        print(f"tag        v{target} (after the merge, not now)")
        print(f"lock       matches pyproject (will re-lock for {target})")
        print("changelog  documents this version")
        print(f"pypi       {target} not published")
        print("\ndry run: nothing changed")
        return 0

    prepare_notes(target, may_promote=True, write=True)
    bump(target)
    if declared_version() != target:
        raise Failure(f"bump did not take: pyproject.toml says {declared_version()!r}")
    check_lock_matches(target)

    print(f"version    {target}")
    print("lock       matches pyproject")
    print("changelog  documents this version")
    print(
        "\nNow open a PR for this branch and merge it, then on main:\n"
        "    uv run python scripts/release.py"
    )
    return 0


def _tag_phase(*, dry_run: bool) -> int:
    """Verify main, derive the tag from the version, and push it. Writes nothing."""
    check_on_main()
    check_up_to_date()

    target = declared_version()
    if published(target):
        raise Failure(
            f"{target} is already published on PyPI. PyPI filenames are "
            "permanent -- a re-upload fails with 400 File already exists, and "
            "yanking does not free the name. Pick a new version "
            "(e.g. the next date under CalVer) and pass --bump."
        )

    tag = f"v{target}"
    if not check_tag_absent(tag):
        raise Failure(
            f"{tag} already exists on origin. It may have failed to publish; "
            "moving a public tag rewrites it, so do that deliberately:\n"
            f"    git tag -f -a {tag} -m '{target}' && git push -f origin {tag}"
        )

    check_lock_matches(target)
    prepare_notes(target, may_promote=False, write=False)

    print(f"version    {target}")
    print(f"tag        {tag}")
    print("lock       matches pyproject")
    print("changelog  documents this version")
    print(f"pypi       {target} not published")
    print("tag        not on origin yet")
    if dry_run:
        # Reached by `scripts/release.py --dry-run`, which is how an operator
        # rehearses the tag before committing to it. The phase split introduced
        # this path and initially dropped the flag on the floor, so a dry run
        # pushed the tag it was supposed to be previewing.
        print("\ndry run: nothing changed")
        return 0

    annotated = run("git", "tag", "-a", tag, "-m", target)
    if annotated.returncode != 0:
        raise Failure(f"git tag failed:\n{annotated.stderr[-400:]}")
    pushed = run("git", "push", "origin", tag)
    if pushed.returncode != 0:
        raise Failure(
            f"git push failed:\n{pushed.stderr[-400:]}\n"
            f"The local tag {tag} exists but was not pushed."
        )
    print(f"\npushed {tag} -> release.yaml will run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
