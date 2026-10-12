# Contributing

Thanks for looking at `yang2sdk`. This file is the short version; the
**normative contract lives in [`AGENTS.md`](AGENTS.md)** — read it before
changing the generator, templates, or anything that touches a device.

## Setup

Requires Python >= 3.12 and [`uv`](https://github.com/astral-sh/uv).

```bash
git clone https://github.com/ConsortiumGARR/yang2sdk.git
cd yang2sdk
uv sync --locked --group dev --extra lab
```

## Before you open a PR

```bash
uv run ruff check . && uv run ruff format --check . \
  && uv run ty check && uv run pyrefly check \
  && uv run pytest tests/test_matrix.py
```

That runs all four blocking gates plus the offline suite:

| Gate | What it enforces |
| --- | --- |
| `uv run ruff check .` | lint (the only linter in this repo) |
| `uv run ruff format --check .` | formatting (the only formatter) |
| `uv run ty check` | types |
| `uv run pyrefly check` | types (a pass in one does not excuse a failure in the other) |
| `uv run pytest tests/test_matrix.py` | offline tests — no docker, no device |

To apply ruff's mechanical fixes, run `uv run ruff check --fix .` and
`uv run ruff format .`, then re-run the gates. All four gates are pinned
exactly and run from the venv; **never run them via `uvx`**, and never edit a
pin by hand — see [`docs/dev.md`](docs/dev.md#bumping-a-pinned-gate-tool).

Tests needing docker (`NOTCONF_RUN_INTEGRATION=1 uv run pytest tests/
--integration`) or lab gear (`uv run sdk-verify --device <d>`) are not part of
the gate. `sdk-verify` and the simulator jobs are **lab only**: never point them
at production. A whole-tree read can spike a real device to 100% CPU and trigger
a watchdog reboot.

## House rules

These are summaries. `AGENTS.md` is authoritative and explains *why*.

- **Extend `IRBuilder` (`src/yang2sdk/plugin/src/ir.py`) before templates**, and
  keep `ir.py` ↔ Jinja in sync — a new IR field needs template rendering plus a
  generated sample that imports.
- **Never weaken a secure default**: `verify=True`, `auto_commit=False`, no
  credentials in generated code, and the `replace()` completeness guard all have
  tests that fail loudly if you remove them.
- **Cite real RFC sections** for any protocol claim (see `RFCs/`).
- **A device quirk is not a generic behaviour.** Vendor workarounds belong in
  downstream adapters with a comment linking the module, revision, and evidence.
- **A `skip` is not a pass.** If a test covers zero nodes it must `pytest.skip`
  with a reason. Never claim coverage the suite does not assert.
- Minimal diffs. No new runtime dependencies without justification — every dep
  is install weight for each downstream generated package.
- Commit messages: concise, matching `git log --oneline -10`.

## What a good PR looks like

State, per `AGENTS.md`'s evidence rule:

- files changed,
- commands run (the five gate commands, generators used),
- **what was verified vs. what is still WIP**.

Distinguishing those two is the part reviewers care about most. If you could not
run the simulator matrix or a lab job, say so plainly rather than implying
coverage you did not execute.