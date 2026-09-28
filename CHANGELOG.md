# Changelog

All notable changes to `yang2sdk` are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is
`0.x` pre-1.0 (breaking changes allowed, documented).

## [Unreleased]

### Fixed

- Generated RESTCONF models: `model_dump(content="config"|"nonconfig")`
  now prunes `config false`/`config true` nodes at every depth
  (RFC 8040 §4.5.2). Previously only top-level fields were excluded, so
  nested state (state leaves inside config containers/list items,
  state leaf-lists) leaked into config dumps — and therefore into
  PATCH/PUT bodies, which serialize with `content="config"`. The walk is
  bottom-up: mismatching leaves/leaf-lists always drop; mismatching
  containers/lists keep an ancestor shell only when a matchable
  descendant remains. Regression test in `tests/test_matrix.py`
  (`test_content_filter_prunes_nested_state`); validated live against the
  notconf simulator.
- Packaging: added `py.typed` so downstream type-checkers see the generated
  SDK types; added missing direct `lxml` dependency (previously only
  transitive via `ncclient`). Verified with `uv build` + clean-venv install
  + import test. Note: the hatch `packages = ["src/yang2sdk"]` config was
  accused of shipping a `src.yang2sdk` top-level package — disproven by
  inspecting the built wheel; config left as-is.
- Version is single-sourced: `pyproject.toml` is the authority,
  `yang2sdk.__version__` resolves it via `importlib.metadata` (stdlib-only,
  `"unknown"` fallback on uninstalled source checkouts). No `hatch-vcs`:
  repo tags are not semver and git-coupling every build is not worth it.

### Changed

- Lab tooling quarantined behind the `lab` extra
  (`pip install yang2sdk[lab]`, dev: `uv sync --locked --extra lab`):
  `lxml`, `ncclient`, `python-dotenv` moved out of default dependencies.
  `yang2sdk.cli.compiler` (production path) no longer loads `.env`
  (`$DEVICE_NAME` fallback still works via plain environment).
  `downloader`/`tester` fail fast with a `[lab]` hint when the extra is
  missing and log a warning for their lab-only insecure defaults
  (`hostkey_verify=False`, `verify=False`).
- `gitingest` moved from runtime dependencies to the `dev` group (zero
  usage in `src/`; pure install weight for downstream consumers).
- `.pytest_cache/` is now gitignored.
- Lint is green: all 41 `ruff check` violations fixed (modernized typing
  to `X | Y` / `list[...]` / `collections.abc`, sorted imports, removed
  dead imports, `removesuffix`, `logger.exception` with tracebacks instead
  of swallowed errors) and the 5 pre-existing `ruff format` drifts
  reformatted. Broad `except Exception` guards at CLI boundaries kept with
  targeted `noqa: BLE001` justifications. Codegen output proven
  byte-identical before/after (`diff -r` on restconf+netconf samples
  exercising enums, patterns, list keys, RPC envelopes).

### Planned (not in this change)

- Flatten `yang2sdk.plugin.src.*` → `yang2sdk.plugin.*` (cosmetic; deferred
  behind a regeneration regression run — touches pyang `--plugindir`
  resolution).
- Emit `pyproject.toml` + dependency pins for generated clients (the
  §7 publishability story is still open).
