#!/usr/bin/env python3
"""`CHANGELOG.md` as part of the release contract: a version cannot ship unnoted.

A version number is a claim about what changed. Publishing `2026.10.16.1` with
no notes is publishing a claim nobody can check, and this repo already had that
gap once: eight commits landed on `main` after `v2026.10.16` -- two of them
breaking -- while `## [Unreleased]` sat empty.

So the changelog is checked, not trusted. This module is the single
implementation of that check, used from two places that must agree:

  * `scripts/release.py`, before anything is tagged;
  * `.github/workflows/release.yaml`, in the build job, so a tag pushed by hand
    around the script is still refused.

**stdlib only**, deliberately: the CI step runs a bare `python3` on the runner
with no synced environment, for the same reason the version-read step does.

The rule enforced is [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
discipline at the moment it matters:

  1. `## [<version>] - <YYYY-MM-DD>` exists for the version being released;
  2. that section is not empty;
  3. `## [Unreleased]` is empty -- anything left there has not been written up
     and would silently ship with the *next* release instead of this one;
  4. under CalVer the version *is* the release date, so the heading's date must
     agree with the version (`2026.10.16.1` -> `2026-10-16`).

Humans write the prose. This refuses to publish without it; it never invents it.

Usage::

    python3 scripts/changelog.py --check 2026.10.16.1     # gate (exit 1 on failure)
    python3 scripts/changelog.py --notes 2026.10.16.1     # body, for release notes
    python3 scripts/changelog.py --promote 2026.10.16.1   # [Unreleased] -> section
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CHANGELOG = REPO / "CHANGELOG.md"

UNRELEASED = "Unreleased"

# `## [version] - YYYY-MM-DD`, tolerating a missing date (`## [Unreleased]`) and
# trailing whitespace. Anchored per line so a `###` subsection is never mistaken
# for a release section -- the file has 8 of those under one `##`.
_HEADING = re.compile(
    r"^## \[(?P<version>[^\]]+)\](?: - (?P<date>\d{4}-\d{2}-\d{2}))?[ \t]*$"
)

# CalVer: the first three segments are the release date, so a fourth (a
# same-day re-release, e.g. `2026.10.16.1`) is a micro and not a new date.
_CALVER_DATE = re.compile(r"^(?P<year>\d{4})\.(?P<month>\d{1,2})\.(?P<day>\d{1,2})")


class ChangelogError(Exception):
    """The changelog is not in a releasable state. Nothing has been written."""


@dataclass(frozen=True)
class Section:
    """One `## [x]` block, with line offsets into the original text."""

    version: str
    date: str | None
    heading_line: int
    body_start: int
    body_end: int

    def body(self, text: str) -> str:
        """The section's content, without the heading and without edge blanks."""
        lines = text.splitlines()
        return "\n".join(lines[self.body_start : self.body_end]).strip()

    def notes(self, text: str) -> str:
        """The body with subsection headings removed.

        A section holding only `### Added` / `### Fixed` describes nothing. The
        offline suite has a standing rule against passes that cover nothing, and
        an empty-of-entries release section is the same failure wearing prose:
        the first version of this check accepted one and was caught by running
        it. So "documented" means at least one real line -- a bullet or a
        paragraph -- not merely a heading that promises one.
        """
        return "\n".join(
            ln for ln in self.body(text).splitlines() if not ln.lstrip().startswith("#")
        ).strip()


def parse(text: str) -> list[Section]:
    """Every `## [version]` section, in file order."""
    lines = text.splitlines()
    headings: list[tuple[int, re.Match[str]]] = []
    for i, line in enumerate(lines):
        m = _HEADING.match(line)
        if m:
            headings.append((i, m))

    sections: list[Section] = []
    for n, (i, m) in enumerate(headings):
        end = headings[n + 1][0] if n + 1 < len(headings) else len(lines)
        sections.append(
            Section(
                version=m.group("version"),
                date=m.group("date"),
                heading_line=i,
                body_start=i + 1,
                body_end=end,
            )
        )
    return sections


def find(text: str, version: str) -> Section | None:
    return next((s for s in parse(text) if s.version == version), None)


def unreleased(text: str) -> Section | None:
    return find(text, UNRELEASED)


def expected_date(version: str) -> str | None:
    """`2026.10.16.1` -> `2026-10-16`. None if the version is not date-shaped.

    PEP 440 orders `YYYY.M.D` numerically, so no zero padding is required and
    `2026.1.2` is a legitimate CalVer version.
    """
    m = _CALVER_DATE.match(version)
    if not m:
        return None
    return date(
        int(m.group("year")), int(m.group("month")), int(m.group("day"))
    ).isoformat()


def require_release_notes(text: str, version: str) -> Section:
    """Return the section for `version`, or raise explaining what is missing.

    Every failure names the file and the fix, because this runs at release time
    and the reader is a person about to publish something irreversible.
    """
    section = find(text, version)
    if section is None:
        have = ", ".join(f"[{s.version}]" for s in parse(text)) or "(none)"
        raise ChangelogError(
            f"CHANGELOG.md has no '## [{version}] - <YYYY-MM-DD>' section.\n"
            f"  A release with no notes is a claim nobody can check; write the\n"
            f"  entries under '## [{UNRELEASED}]' and run --promote, or add the\n"
            f"  section by hand. Sections present: {have}"
        )
    if not section.notes(text):
        raise ChangelogError(
            f"CHANGELOG.md section '## [{version}]' documents nothing.\n"
            f"  Subheadings like '### Added' are a promise, not an entry. Write\n"
            f"  what changed under one of them, or release a version that has\n"
            f"  notes."
        )

    pending = unreleased(text)
    if pending is not None and pending.notes(text):
        first = next((ln for ln in pending.notes(text).splitlines() if ln.strip()), "")
        raise ChangelogError(
            f"CHANGELOG.md still has content under '## [{UNRELEASED}]' "
            f"({len(pending.notes(text).splitlines())} line(s), starting "
            f'"{first.strip()[:60]}").\n'
            f"  It would ship with the NEXT release, not {version}. Move it "
            f"under\n"
            f"  '## [{version}]' (or into a later section) before tagging."
        )

    want = expected_date(version)
    if want and section.date != want:
        raise ChangelogError(
            f"CHANGELOG.md heading '## [{version}] - {section.date}' disagrees "
            f"with the version.\n"
            f"  CalVer makes the version the release date, so this heading "
            f"should read\n"
            f"  '## [{version}] - {want}'. If the release really is dated "
            f"{section.date},\n"
            f"  the version is wrong instead -- fix whichever is untrue."
        )
    return section


def promote(text: str, version: str, on: date | None = None) -> str:
    """Turn `## [Unreleased]` into `## [version] - date`, leaving a fresh empty one.

    Refuses rather than guessing: an empty `[Unreleased]` means there is nothing
    to promote, and an existing section for `version` means promoting would
    duplicate it (or silently discard whichever copy was thinner).
    """
    if find(text, version) is not None:
        raise ChangelogError(
            f"CHANGELOG.md already has a '## [{version}]' section; "
            f"promoting would duplicate it."
        )
    section = unreleased(text)
    if section is None:
        raise ChangelogError(
            f"CHANGELOG.md has no '## [{UNRELEASED}]' heading to promote."
        )
    body = section.notes(text)
    if not body:
        raise ChangelogError(
            f"CHANGELOG.md has nothing to promote under '## [{UNRELEASED}]'.\n"
            f"  Subheadings alone are not a release -- write the entries "
            f"first."
        )

    # UTC, not the machine's local zone: this stamps a release date, and a
    # release cut at 23:30 in one zone must not be dated differently by whoever
    # promotes the changelog the next morning.
    stamp = (on or datetime.now(tz=UTC).date()).isoformat()
    lines = text.splitlines()
    head = lines[: section.heading_line]
    # The text written out is the raw body, not `body`: `notes()` strips every
    # `### Added` / `### Fixed` heading on purpose, because "does this section
    # document anything" is a question about entries and a heading is not one.
    # That makes it right for the check above and wrong for the output -- using
    # it here shipped `2026.10.16.1` as 170 flat lines with no categories at all,
    # and the structure was never recovered. The check keeps `notes()`; the
    # output takes the body verbatim.
    verbatim = "\n".join(lines[section.heading_line + 1 : section.body_end]).strip("\n")
    # `body_end` is the *next* `## [` heading, so the tail starts there. Taking
    # `heading_line + 1 : body_end` as well would re-emit the body a second time
    # underneath itself -- it is already written from `verbatim`. Demonstrated,
    # not deduced: the first version printed every promoted entry twice.
    tail = lines[section.body_end :]
    rebuilt = "\n".join(
        [
            *head,
            f"## [{UNRELEASED}]",
            "",
            f"## [{version}] - {stamp}",
            "",
            *verbatim.splitlines(),
            "",
            *tail,
        ]
    )
    return rebuilt.rstrip("\n") + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check, print, or promote a CHANGELOG.md release section.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage::")[-1],
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", metavar="VERSION", help="Refuse unless releasable.")
    group.add_argument(
        "--notes", metavar="VERSION", help="Print the section body (release notes)."
    )
    group.add_argument(
        "--promote",
        metavar="VERSION",
        help="Rewrite [Unreleased] into this version's section, in place.",
    )
    parser.add_argument(
        "--date",
        metavar="YYYY-MM-DD",
        help="Date for --promote (default: today).",
    )
    parser.add_argument(
        "--file",
        default=str(CHANGELOG),
        help="Changelog to operate on (default: the repo's CHANGELOG.md).",
    )
    args = parser.parse_args(argv)

    path = Path(args.file)
    try:
        text = path.read_text()
        if args.check:
            require_release_notes(text, args.check)
            print(f"CHANGELOG.md documents {args.check}")
            return 0
        if args.notes:
            # `body`, not `notes`: the GitHub release page should keep its
            # `### Added` / `### Fixed` grouping, which is the readable part.
            # `notes()` drops headings and exists only to answer "is there
            # anything here at all?" -- using it here flattened every release
            # into an undifferentiated bullet list.
            print(require_release_notes(text, args.notes).body(text))
            return 0

        stamp = date.fromisoformat(args.date) if args.date else None
        promoted = promote(text, args.promote, stamp)
        path.write_text(promoted)
        print(f"promoted [Unreleased] -> ## [{args.promote}]")
        return 0
    except ChangelogError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        # `--notes | head` is a normal thing to do with this. Without this the
        # interpreter prints "Exception ignored ... BrokenPipeError" during
        # shutdown, which looks like a failure of a command that worked.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0


if __name__ == "__main__":
    sys.exit(main())
