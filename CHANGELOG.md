# Changelog

All notable changes to `yang2sdk` are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is
`0.x` pre-1.0 (breaking changes allowed, documented).

## [Unreleased]

### Added

- Integration coverage for CRUD through the generated SDKs
  (`tests/test_sdk_generate.py`): `test_restconf_sdk_crud_roundtrip` and
  `test_netconf_sdk_crud_roundtrip` drive create → retrieve → update
  (merge) → replace → delete of a throwaway ietf-interfaces entry with
  read-back assertions (RESTCONF read-backs fall back to the RFC 8527
  running datastore because the lab backend hides `/data` writes; NETCONF
  read-backs also assert identityref values come back in RFC 7951 §6.8
  module-name form), plus per-protocol
  `test_*_all_config_nodes_retrieve_update` idempotent same-data merges of
  every top-level config node (a throwaway entry is seeded when a fresh
  image ships no config). Hard assertions — the previous
  best-effort/xfail RESTCONF write test is gone.

### Fixed

- RESTCONF write envelopes: nested list/container nodes now send
  module-qualified top-level body members (RFC 7951 §4) via a navigator
  `_envelope_name` (write key) separate from the response key; response
  matching keeps the simple name. Regression test in `tests/test_matrix.py`
  (`test_restconf_write_bodies_are_module_qualified`).
- RESTCONF `create` sends one POST per new entry; the lab backend rejects
  multi-entry create bodies ("MUST contain exactly one instance",
  2026-09-28 probe).
- RESTCONF models accept Python field names on input
  (`populate_by_name=True`), matching the NETCONF models; previously
  `Model(some_leaf=...)` / `model_validate({"some_leaf": ...})` failed with
  "field required" because only wire aliases were accepted. This was the
  actual cause of the long-suspected "RESTCONF write rejected (LY_EVALID)"
  xfail — the failure was client-side validation, not a server rejection.
- State (`config false`) leaves, leaf-lists, and lists are now optional on
  generated models; previously a mandatory state leaf (e.g. ietf-interfaces
  `if-index`/`oper-status`) made read-back of freshly created running
  entries fail validation.
- NETCONF identityref support: values stay RFC 7951 §6.8 module-name
  strings; the write path binds the prefix from the server's advertised
  capabilities (RFC 7950 §9.10.3) and the read path normalizes server
  prefixes back to the module name. Previously `create`/`replace` of
  ietf-interfaces failed with "unable to map prefix to YANG schema".
  Regression test in `tests/test_matrix.py`
  (`test_netconf_identityref_binding_and_normalization`).
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
