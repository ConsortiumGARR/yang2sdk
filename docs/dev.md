# Development

Requires Python >= 3.12 and [`uv`](https://github.com/astral-sh/uv).

```bash
uv sync --locked --extra lab
```

## Gates

Four blocking gates, all on `src/` (ephemeral `temp/` output is excluded via `[tool.ruff]`, `[tool.pyrefly]`, and `.gitignore` for `ty`):

```bash
uvx ruff check .
uvx ruff format --check .
uvx ty check
uvx pyrefly check
```

`ruff` is the only linter/formatter. `ty` and `pyrefly` are both required; a pass in one does not excuse a failure in the other.

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
| `tests/test_srl_netconf.py` | SR Linux containerlab lab | yes, non-blocking (`ci-srl.yaml`) |
| `tests/test_lab_device_netconf.py` | a real lab device | no, lab only |
| `uv run sdk-verify` | a lab device (or simulator) | no, manual lab harness (see `docs/lab.md`) |

CI: `ci-pr.yaml` (lint → typecheck → offline gate → smoke per family), `ci-nightly.yaml` (full 11-image matrix + `workflow_dispatch`), `ci-srl.yaml` (SR Linux; deploy/get-schema/generate block, pytest reports). `test_lab_device_netconf.py` is in no workflow — nothing it asserts gates a merge. A test that covers zero nodes skips with a reason rather than passing vacuously. Tests that build a generated client against a simulator pass `verify=False` explicitly with a comment; the `verify=True` default is never weakened for tests. NETCONF write read-backs run edit → `validate` → `commit`, skipping `validate` when `:validate` is not advertised (RFC 6241 §8.6.4.1 optional).

Golden snapshots under `tests/fixtures/golden/` are run-local and gitignored.

## Contributing

`AGENTS.md` is the contract: extend `IRBuilder` (`src/yang2sdk/plugin/src/ir.py`) before templates, keep `ir.py` ↔ Jinja in sync with a generated sample that imports, cite real RFC sections for protocol changes, and never weaken secure defaults (`verify=True`, no embedded credentials, `auto_commit=False`, the `replace()` completeness guard). Preserve strictness (`extra="forbid"`, `validate_assignment`, alias/tag/ns plumbing, `is_config` filtering, choice-exclusion, 64-bit serializers). Vendor quirks belong in downstream adapters, not the generic path. Minimal diffs, no new runtime deps without justification. Commit messages: concise (`git log --oneline -10` first); never commit secrets, `temp/`, or `.venv/`.

Every summary of work states: files changed, commands run (`ruff`, `ty`, `pyrefly`, generators), and what was verified vs what remains WIP.
