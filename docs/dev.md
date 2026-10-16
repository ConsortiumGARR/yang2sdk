# Development

Requires Python >= 3.12 and [`uv`](https://github.com/astral-sh/uv).

```bash
uv sync --locked --extra lab
```

## Gates

Four blocking gates, all run from the project venv with `uv run` (ephemeral
`temp/` output is excluded via `[tool.ruff]`, `[tool.pyrefly]`, and
`.gitignore` for `ty`):

```bash
uv run ruff check . && uv run ruff format --check . \
  && uv run ty check && uv run pyrefly check \
  && uv run pytest tests/test_matrix.py
```

The four gates are, individually:

```bash
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pyrefly check
```

`ruff` is the only linter/formatter. `ty` and `pyrefly` are both required; a pass
in one does not excuse a failure in the other. To apply ruff's mechanical fixes
in place, run `uv run ruff check --fix .` and `uv run ruff format .`, then
re-run the gates.

Never run a gate as `uvx <tool>`. `uvx` resolves the newest release on every
invocation, so a gate can start failing with no commit and no diff to explain
it — that is how `ty` 0.0.86 turned a green `main` into a red one on 2026-10-06.

### Bumping a pinned gate tool

The gates are pinned **exactly** in the `dev` dependency group (`ruff`,
`ty`, `pyrefly`). There is no bot: bumps are manual and visible by design.

```bash
uv lock --upgrade-package ty
uv sync --locked --group dev --extra lab
# then re-run the gates above
```

`uv sync --locked` makes CI hermetic — a tool release landing upstream cannot
change what CI installs. Breaking a gate is therefore always something you
did deliberately and saw locally, never a background event. Commit
`pyproject.toml` and `uv.lock` together, and say in the commit message what
changed in the tool's output.

`ty` and `pyrefly` are both pre-1.0, so expect their output to keep changing
and expect to repeat this every few weeks. That is the accepted cost of never
being red for a reason nobody can explain.

## Tests

```bash
uv run pytest tests/test_matrix.py      # offline gate: no docker, no device
uv run pytest tests/ --integration                        # notconf simulator matrix
NOTCONF_RUN_INTEGRATION=1 uv run pytest tests/             # same, via environment
NOTCONF_SMOKE_ONLY=1 uv run pytest tests/ --integration   # one image per family
```

| File | Needs | In CI |
| --- | --- | --- |
| `tests/test_matrix.py` | nothing | yes, every PR |
| `tests/test_notconf_protocol.py` | docker + `notconf` images | yes, smoke shards |
| `tests/test_sdk_generate.py` | docker + `notconf` images | yes, smoke shards |
| `tests/test_sdk_verify_ci.py` | docker + `notconf` images (reuses the container above) | yes, **blocking**, smoke shards |
| `tests/test_srl_netconf.py` | SR Linux containerlab lab | yes, non-blocking (`ci-srl.yaml`) |
| `tests/test_lab_device_netconf.py` | a real lab device | no, lab only |
| `uv run sdk-verify` | a lab device (or simulator) | no, manual lab harness (see `docs/lab.md`) |

CI: `ci-pr.yaml` (lint → typecheck → offline gate → smoke per family), `ci-nightly.yaml` (full 11-image matrix + `workflow_dispatch`), `ci-srl.yaml` (SR Linux; deploy/get-schema/generate block, pytest + `sdk-verify --tiers read,rpc,crud` report). `test_lab_device_netconf.py` is in no workflow — nothing it asserts gates a merge.

`sdk-verify` is now a gate. `tests/test_sdk_verify_ci.py` calls the same verifier in-process, so it adds no container startup beyond what the smoke job already pays for; `ci-srl.yaml` runs the CLI itself with `crud`, which is a writer and so is confined to that lab job. It was a manual harness in no workflow until 2026-10-13, which is how the RESTCONF ancestor-envelope unwrap, the augment-closure gap, and the `ietf-notification-capabilities` model hole all shipped. A test that covers zero nodes skips with a reason rather than passing vacuously. Tests that build a generated client against a simulator pass `verify=False` explicitly with a comment; the `verify=True` default is never weakened for tests. NETCONF write read-backs run edit → `validate` → `commit`, skipping `validate` when `:validate` is not advertised (RFC 6241 §8.6.4.1 optional).

Golden snapshots under `tests/fixtures/golden/` are run-local and gitignored.

## Releasing

There was no release process before the first release (`v2026.10.16`): no publish
workflow, no version convention, and 57KB of changelog sitting under
`[Unreleased]`. This is it.

**Versioning: CalVer, the release date.** `version` is `YYYY.M.D` — the day the
release was cut — and the tag is that value with a `v` prefix:
`v2026.10.16`. Adopted 2026-10-16, replacing a SemVer `0.1.0` that was never
published: the first tag was pushed as `v2026.10.16` while `pyproject.toml`
still said `0.1.0`, and the guard caught it. `YYYY.M.D.N` is the same-day
re-release form.

The `release.yaml` build job asserts `tag == pyproject version` exactly and
fails the run on mismatch, so a drifted tag cannot publish. It is shell, not a
declaration, so it is pinned by
`tests/test_matrix.py::test_release_tag_guard_passes_on_a_matching_tag` (with
mismatch and missing-version cases alongside): the test parses the workflow and
runs both steps under GitHub's `$GITHUB_OUTPUT` semantics. That matters — the
guard shipped broken and failed *every* tag, because a step that `print()`s a
bare value records no output, so the comparison was against `""`. Fixed in
`fix/release-version-guard-output`. Four consequences
worth knowing before you tag:

- **The tag must point at the commit carrying the version.** `git tag -f` onto
  an unchanged commit does *not* re-run the workflow — GitHub keys `on.push` on
  the ref value — and "Re-run failed jobs" on the old run re-executes the old
  commit and fails identically.
- **Never push a SemVer tag** (`v0.1.0`, `v1.2.3`): same guard, same failure.
- **No zero padding needed.** PEP 440 orders `YYYY.M.D` numerically, so
  `2026.10.2 < 2026.10.16 < 2026.11.1`. Padding only helps a human sorting
  filenames.
- **A version bump must carry `uv.lock`.** `uv.lock` records the editable root's
  version, and `ci-pr` runs `uv sync --locked`, which fails without it. A
  version change forces a re-resolve, so `uv lock` needs network — it cannot be
  done offline from cache.

Note the distinction from the pre-existing `itnog10` / `tnc26` tags: those are
conference tags, not releases. They do not match a version and nothing
publishes from them (`release.yaml` triggers only on `v*`).

**A release is two commands and a review.** Write the notes, then:

```bash
git switch -c release/2026.11.1
uv run python scripts/release.py --bump 2026.11.1   # notes, version, lock, commit
#   ... open the PR, merge it ...
git switch main && git pull
uv run python scripts/release.py                   # derive the tag, push it
```

`--dry-run` on either phase runs every check and changes nothing.

The split is not ceremony. The bump is a *code change* — it is the commit the
tag will name, so it is exactly the one that deserves review — and the ruleset
requires a pull request for `main`. The tag is the only step taken from a clean
`main`, and every check on it has already run by then. The first version of the
script did both in one command, which could therefore never succeed on a
protected branch; that is the shape that turned "push a tag" into five steps.

Do not hand-type a tag. The guard exists and it will refuse you, which is the
correct outcome learned the expensive way on 2026-10-16, when `v2026.10.16.1` was
pushed one commit before the bump landed and the build died on
`tag v2026.10.16.1 implies 2026.10.16.1 but pyproject.toml says 2026.10.16`.

**The changelog is part of the contract, not documentation.** `scripts/changelog.py`
refuses a release whose version has no section, whose section is empty, whose
`## [Unreleased]` still holds content (it would ship with the *next* release,
silently), or whose heading date contradicts the CalVer version. `release.yaml`
re-checks the same rules in the build job, so a tag pushed around the script is
still refused. Pinned by `tests/test_changelog.py`, which runs the workflow's own
`run` text with GitHub's substitution semantics plus a negative control.

The reason is this repo's own record: eight commits landed on `main` after
`v2026.10.16` — two of them breaking, `yang2netconf`/`yang2restconf` removed
outright — while `## [Unreleased]` sat empty. The `--check` rule counts real
entries, not subheadings: `### Added` alone is a promise, not a release note.
That distinction was found by running the check, not by reading it — the first
version accepted a section of bare headings, which is the same vacuous pass
`AGENTS.md` records three times.

**What CI adds, and why each is there:**

| stage | catches |
| --- | --- |
| `tag == pyproject version` | a tag typed ahead of the bump (it has happened) |
| `changelog.py --check` | a version whose change nobody wrote down |
| `uv build` + `twine check --strict` | metadata PyPI would reject, before the tag is public |
| publish, Trusted Publishing | no long-lived secret; attestations on (PEP 740, already present on `v2026.10.16`) |
| `verify-published` | installs the published wheel **from PyPI**, checks `yang2sdk --version`, generates a client and imports it |
| `github-release` | a release page with notes taken from the changelog section |

`verify-published` exists because "we published" and "the published thing works"
are different claims and only the first was ever tested: the generated sdist
shipped the package twice (`3b22730`) with every gate green, because every gate
ran against the source tree. It deliberately runs no `checkout` step, so an
editable install from the workspace cannot make it vacuous.

Note the distinction from the pre-existing `itnog10` / `tnc26` tags: those are
conference tags, not releases. They do not match a version and nothing
publishes from them (`release.yaml` triggers only on `v*`).

**A version cannot be reused, on PyPI or here.** Filenames on PyPI are
permanent — a re-upload fails `400 File already exists`, and yanking does not
free the name — so `release.py` checks PyPI before tagging and fails closed if
it cannot reach it. For a same-day re-release use `YYYY.M.D.N` (`2026.10.16.1`);
its date still has to match the changelog heading.

**What the workflow does not do:** it cannot create the PyPI-side configuration.
Either enable Trusted Publishing for the repo (recommended; no secret needed) or
add a project-scoped API token as the `PYPI_TOKEN` repository secret, and create
the `pypi` environment so publishing needs an approver. This must be in place
**before** the tag is pushed: `yang2sdk` does not yet exist on PyPI, so the first
upload creates the project, and PyPI rejects the OIDC exchange with a 403 unless
the trusted publisher is already registered against the exact name `yang2sdk`. A
`pypi` environment created implicitly by the first run has no protection rules,
which is the opposite of the approver gate the workflow assumes.

**Verify a release locally before tagging:**

```bash
uv build --out-dir dist && uv run --with twine twine check --strict dist/*
```

Then, in a scratch venv outside the repo, install the wheel and generate a
client from a small module — that proves the *published* artifact works, which
is a different claim from "the gates pass in the source tree".

## Contributing

`AGENTS.md` is the contract: extend `IRBuilder` (`src/yang2sdk/plugin/src/ir.py`) before templates, keep `ir.py` ↔ Jinja in sync with a generated sample that imports, cite real RFC sections for protocol changes, and never weaken secure defaults (`verify=True`, no embedded credentials, `auto_commit=False`, the `replace()` completeness guard). Preserve strictness (`extra="forbid"`, `validate_assignment`, alias/tag/ns plumbing, `is_config` filtering, choice-exclusion, 64-bit serializers). Vendor quirks belong in downstream adapters, not the generic path. Minimal diffs, no new runtime deps without justification. Commit messages: concise (`git log --oneline -10` first); never commit secrets, `temp/`, or `.venv/`.

Every summary of work states: files changed, commands run (`ruff`, `ty`, `pyrefly`, generators), and what was verified vs what remains WIP.
