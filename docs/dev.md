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

## Contributing

`AGENTS.md` is the contract: extend `IRBuilder` (`src/yang2sdk/plugin/src/ir.py`) before templates, keep `ir.py` ↔ Jinja in sync with a generated sample that imports, cite real RFC sections for protocol changes, and never weaken secure defaults (`verify=True`, no embedded credentials, `auto_commit=False`, the `replace()` completeness guard). Preserve strictness (`extra="forbid"`, `validate_assignment`, alias/tag/ns plumbing, `is_config` filtering, choice-exclusion, 64-bit serializers). Vendor quirks belong in downstream adapters, not the generic path. Minimal diffs, no new runtime deps without justification. Commit messages: concise (`git log --oneline -10` first); never commit secrets, `temp/`, or `.venv/`.

Every summary of work states: files changed, commands run (`ruff`, `ty`, `pyrefly`, generators), and what was verified vs what remains WIP.
