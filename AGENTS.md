# AGENTS.md — yang2sdk

## Long-term vision: production-grade tool

`yang2sdk` generates a Pydantic-v2, IDE-friendly SDK for network devices **directly from the YANG modules the device actually runs**.

North star:

```
YANG off the box → versioned, publishable Python SDK → HAL + adapters for multi-vendor / multi-version automation
```

Concretely, production-grade means:

1. **Fidelity:** the generated models mean what the YANG means (types, constraints, namespaces, `config true/false`, choices).
2. **Interchangeability:** RESTCONF and NETCONF clients expose the same navigator surface (`retrieve/update/replace/create/delete`, RPC `__call__`); only transport/encoding differs (JSON vs XML).
3. **Publishability:** every generated client is a self-contained Python package a downstream project can depend on — no credentials, no lab paths, secure defaults.
4. **Multi-vendor scale:** downstream projects build a Hardware Abstraction Layer (HAL) with `adapters/<device>_<os-version>/` on top of generated clients (see `README.md` HAL sketch). `yang2sdk` itself stays vendor-agnostic.
5. **Safety:** it is impossible to use the tool "correctly" and still reboot, OOM, or lock out a production device.
6. **Verifiability:** every change is linted, type-checked, and (once available) exercised against a simulator, not against production.

## Architecture (do not break)

### Pipeline

```
yang-downloader (NETCONF get-schema)
  → pyang -f tree inspection (human picks root modules)
  → yang2restconf / yang2netconf (pyang plugin: AST → IR → Jinja)
  → sdk-verify (full-tree read + RPC round-trip + gated CRUD, LAB ONLY)
  → publish generated client as versioned package
```

| Stage | Entry point | Notes |
|---|---|---|
| Download YANG | `src/yang2sdk/cli/downloader.py` (`yang-downloader`) | `ietf-netconf-monitoring/get-schema` via `ncclient`. Output: `temp/yang_modules/<device>/`. |
| Inspect tree | `uv run pyang -p temp/yang_modules/<device>/ -f tree …` | Human identifies *root* modules. No automation here yet. |
| Compile | `src/yang2sdk/cli/compiler.py` (`yang2restconf`, `yang2netconf`) | In-process pyang invocation. Defaults: `--yang-dir temp/yang_modules/<device>`, `--output-dir temp/{restconf,netconf}_clients/<device>`. Flags: `--device`, `--yang-dir`, `--output-dir`, `--config-only`, `--device-version`, `--package-version`, `--deviation-module`, `--feature`, `--features-file`, `--no-device-features`, `--ignore-error`, `--check-model-gaps` (NETCONF only). |
| IR | `src/yang2sdk/plugin/src/ir.py` (`IRBuilder`) | AST → `IRModule/IRModel/IREnum/IRNavNode/IRField` dataclasses. Load-bearing logic, see §5. |
| Emit | `src/yang2sdk/plugin/src/core.py` (`Yang2Restconf`, `Yang2Netconf`) | IR → Jinja templates in `src/yang2sdk/plugin/src/templates/{restconf,netconf}/`. |
| Validate | `src/yang2sdk/cli/sdk_verify.py` (`sdk-verify`) | Walks the generated navigator tree and calls its own methods: `read` (full tree, bounded depth), `rpc` (serialise every Input, no send), `crud` (idempotent merge + proven restore). Both protocols. LAB ONLY. |
| Debug helper | `src/yang2sdk/cli/canonicalize_ast.py` | AST canonicalizer for diffing generated code. |

### Generated SDK layout (both protocols)

```
<device>_<os-version>/
  __init__.py                  # RestconfClient / NetconfClient
  session_manager.py           # transport only
  data_models/{models.py, _base.py, __init__.py}
  data_navigators/{navigators.py, _base.py, __init__.py}
```

- Models are strict: `extra="forbid"`, `validate_assignment=True`, `defer_build=True`.
- RESTCONF models use `Field(alias="module:leaf")` + `RestconfList[T]`; NETCONF models use `pydantic-xml` `element(tag, ns)`.
- Navigators are path builders: `client.data.ne.shelf(1).slot(3)…`, `ListNode.__call__(*keys)` with URL-quoting (RESTCONF) or key-tuples (NETCONF).
- `model_dump(content="config"|"all"|"nonconfig")` filtering via `json_schema_extra["is_config"]` is load-bearing — preserve it.

## RFC compliance (normative)

Agents must be compliant with the RFCs. Canonical agent-facing form is **`RFCs/*.md` normalized summaries**.

- Normalized summaries (all of `RFCs/` is `.md`; no raw `.txt`/`.json` remain):
  - YANG 1.1 language: RFC 7950 (`RFCs/7950.md`).
  - JSON encoding: RFC 7951 (`RFCs/7951.md`).
  - NETCONF base: RFC 6241 (`RFCs/6241.md`); SSH transport: RFC 6242 (`RFCs/6242.md`).
  - Datastores/NMDA: RFC 8342 (`RFCs/8342.md`, architecture), RFC 8526 (`RFCs/8526.md`, `get-data`/`edit-data`).
  - RESTCONF base: RFC 8040 (`RFCs/8040.md`: `data`/`operations` resources, `depth`, `content`, `fields`, `with-defaults`); NMDA RESTCONF extensions: RFC 8527 (`RFCs/8527.md`, `/ds/` resources, `with-origin`).
  - Access control: RFC 8341 (`RFCs/8341.md`) is **NACM, not RESTCONF** — cite it only for authorization/`access-denied` semantics. (RFC 8340, YANG tree diagrams, is out of scope.)
- Rules:
  - Never invent encoding, filtering, or error semantics. Cite `RFC <n> §<x>` or the `RFCs/*.md` summary for every protocol claim.
  - 64-bit integers follow RFC 7951 (JSON string encoding). Depth/content/`with-defaults` follow RFC 8040. NMDA `get-data`/`edit-data` follow RFC 8526; legacy `get`/`get-config`/`edit-config` follow RFC 6241.
  - If device behavior contradicts the RFC, document the deviation in code comments + PR description; do not silently bake the quirk into the generic path (isolate per-adapter workarounds downstream).

## YANG fidelity contract (normative)

The generator must handle, at minimum:

- Data nodes: `container`, `list` (+ `key`, composite keys, `min/max-elements`), `leaf`, `leaf-list`, `choice/case` (flattened, optional, runtime mutual-exclusion check via `choice_mapping`), `anydata`/`anyxml` → `str`.
- Reuse: `grouping`/`uses`, cross-module `augment`, `typedef` chains.
- **Augment nodes keep the AUGMENTING module's namespace on the wire.** RFC 7950 §7.17 adds the nodes to the target tree but each node still belongs to its defining module, so the NETCONF model root for an augmented node must declare the augmenting namespace -- not the file's module default. Emitting the default makes every read of the node fail with "root element not found" even though the device answered correctly (measured on SR Linux 25.10.1: 77 nodes under `/system/{aaa,tls,ssh-server,…}` and `/qos`, all validating once fixed; same defect also fixed G30 `openroadm-augment` port models). Pinned offline by `test_augment_model_root_uses_augmenting_namespace`; the vendor-tree sweep is `sdk-verify --tiers read,rpc` (a parse/validation failure there is a FAIL, never a device skip).
- RPCs: `rpc` + `action` (both map to `rpc` navigator nodes with `Input`/`Output` envelopes), `notification` (model-only).
- Types: `int8/16/32`, `int64`/`uint64` (RFC 7951 string form), `uint8/16/32`, `decimal64`, `boolean`/`empty` → `bool`, `string` (+ `length`/`pattern`, XSD→Python regex map), `enumeration` (`Literal` if ≤3 values else `Enum` with MD5-fingerprint dedup), `union`, `leafref` (resolve to target type, fallback `str`), `bits`/`binary`/`instance-identifier` → `str`.
- **`identityref` → `str`, deliberately, and do not "fix" it.** RFC 7950 §9.10.2 permits *either* an element
  named after the identity or a string; we emit RFC 7951 §6.8 `module:identity` (bare when the identity is
  defined in the leaf's own module). Both are conformant. Measured on SR Linux 25.10.1: the device reads
  the bare form back verbatim, and on write accepts it for a *new* entry while rejecting every prefixed
  form and the element form. Consequence, verified by bisection: **the device cannot round-trip its own
  retrieved subtree** — its `/system` config merges back only after all 19 identityref leaves are removed.
  That is a device deviation: document it and work around it downstream, never bend the generic path.
  Pinned by `test_matrix.py::test_identityref_stays_a_string_on_the_wire`, which asserts the *emitted
  shape only* — it contacts no device and deliberately claims nothing about device behaviour.
- Metadata: `mandatory`, `config true/false` (`--config-only` drops `config false`), `default` (type-aware), `when`/`must` (emitted into descriptions; `_is_mandatory` returns `False` when present — intentional until constraints become executable), `description` (escaped docstrings).
- Naming: `YANG-name → PascalCase` classes / `snake_case` fields; iterative depth-based collision resolver + `_pydantic_class_name` propagation in `ir.py` are load-bearing. Do not "simplify" them without a regression corpus. Corollary: a list navigator's item class is referenced as `<resolved>Node` verbatim on BOTH the definition and the reference side -- re-appending `Item` on the reference side breaks every collision-renamed (`…_1`) list with an ImportError on first touch (measured on SR Linux ACL/system navigators). Pinned by `test_all_navigator_references_resolve`.
- **Reserved Python names (three classes, all in `_to_field_name`):** keywords (`class`, `import`), `BaseModel`/`BaseXmlModel` attribute names (`schema`, `json`, `copy`, `dict` → `schema_`, `json_`, …), and the builtins the emitter itself writes bare inside a generated class body (`property`, `str`, `int`, `bool`, `list` → suffixed). The third class is what stops a node named `property` from generating an *unimportable* module (`TypeError: 'property' object is not callable`), which `_validate_generated` cannot catch because the output still parses. Keep that list to names the emitter actually emits: escaping all of `builtins` would rename ordinary YANG leaves `id`/`type`/`input`/`filter`/`range` for no benefit. `tests/test_matrix.py::test_builtin_named_nodes_do_not_break_the_generated_import` is the tripwire — widen the emitter's vocabulary without widening the set and it fails loudly.
- `is_key` and the key-aware `ListNode.__call__` must be emitted on **both** transports. They were NETCONF-only, which left RESTCONF callers unable to address a list instance at all (the YANG `key` statement is not in an instance payload, so it cannot be recovered from device data). This is what makes the navigator surfaces symmetrical.

When adding a YANG feature: extend `IRBuilder` first, then templates. Never emit unvalidated Python by string-concatenation outside Jinja.

## RESTCONF ↔ NETCONF parity (normative)

- Public navigator API is `retrieve(depth, content, fields/with-defaults)`, `update` (PATCH/merge), `replace` (PUT), `create` (POST), `delete`, RPC dispatch — **symmetrical in intent, not in every method on every node class.** The surface is
  *asymmetrical by protocol*, and that is deliberate. Measured on generated clients (not inferred):

  | node | verbs present |
  | --- | --- |
  | NETCONF `ListNode` (plural) | `create` `replace` `retrieve` `update` — **no `delete`** |
  | NETCONF `ItemNode` (keyed) | `delete` `replace` `retrieve` `update` — no `create` |
  | RESTCONF `ListNode` (plural) | `create` `delete` `replace` `retrieve` — **no `update`** |
  | RESTCONF `ItemNode` (keyed) | `delete` `replace` `retrieve` `update` — no `create` |

  The divergence is protocol-shaped, not accidental (RFC 8040 §4.4–§4.7 for
  POST/PUT/PATCH/DELETE; RFC 6241 §7.2 for `operation="create"`):
  - **No RESTCONF plural `update`.** Collection `PATCH` (RFC 8040 §4.6.1 merge)
    is legal but withheld: `create()` (POST, RFC 8040 §4.4.1) adds entries and
    item `update()` merges them, so a collection-wide merge has no mapping the
    item verbs do not already cover. NETCONF keeps plural `update` because
    `operation="merge"` (§7.2) addresses the subtree directly.
  - **No NETCONF list `delete`.** Deleting a NETCONF list entry requires its
    keys, which only the keyed `ItemNode` has. RESTCONF `DELETE` on the list
    *resource* is meaningful (RFC 8040 §4.7), so only it has the plural `delete`.

  Do not "fix" this by adding verbs whose semantics do not map across
  transports. A HAL adapter must branch on capability, not assume the union.
  If a verb is added, this table and `tests/test_matrix.py` move with it.
- Transports intentionally differ:
  - RESTCONF: `requests` + TCP keepalive, `loopback_ip/management_ip` failover, `application/yang-data+json`.
  - NETCONF: `ncclient`, capability discovery by **exact base capability URI** (`:candidate` / `:writable-running` / `:nmda:1.0` — never a module name, or a device that ships `ietf-netconf-nmda` without implementing NMDA routes every read to an unsupported `<get-data>`), NMDA vs legacy RPC routing, `lock`/`unlock`, opt-in `auto_commit` plus `validate()` + `commit`/`discard_changes`, `RPCError → RuntimeError` (attribute access must never raise from the error path).
- Any deliberate divergence (e.g. client-side depth pruning, `{}`-for-`[]` RESTCONF quirk handling, `pydantic-xml` deferred-rebuild) must live in the protocol's `_base` template with a comment citing the RFC section or device evidence.

## Generated SDK packaging (normative)

Generated clients are **securely publishable Python packages**:

- **Naming:** `<device>_<os-version>` (e.g. `g30_1_4_0`). One package per device + OS version. Never reuse a package name across different YANG revisions.
- **Installability:** self-contained (own `pyproject.toml`, no `temp/` or repo-absolute imports). Installable via:
  - `uv add path/to/<device>_<os-version>`
  - `uv add --editable path/to/<device>_<os-version>`
  - (and equivalents: git URL / registry once published).
- **No secrets:** never embed usernames, passwords, tokens, IPs, or private CA bundles in generated code, templates, tests, or examples. Auth comes from caller args or environment at runtime only.
- **Credential contract (both protocols, normative):** `username`/`password` args win; otherwise the client reads **`DEVICE_USER` and `DEVICE_PASS`** from the environment. Exactly two names, never more. `DEVICE_USERNAME`/`DEVICE_PASSWORD` are **retired, not aliased** — a generated client must ignore them even when they are the only ones exported. Rationale: every other consumer in this repo (`.env.example`, `cli/downloader.py`, `cli/sdk_verify.py`, the lab matrix in `tests/conftest.py`) has always used the short form, so the long form existed only in these templates and made the `.env` the project tells you to copy fail against a generated client. A two-name alias was tried and was worse: the two templates resolved the pairs in opposite order, so a host exporting both names authenticated as two different identities depending on transport. Do not reintroduce an alias — with one name that hazard cannot exist. If a value is missing, the constructor raises `ValueError` (never a `Warning` subclass used as an exception) *before* any transport work: RESTCONF and NETCONF both fail closed, so no client can exist that would attempt an unauthenticated session. Covered by `tests/test_matrix.py::test_both_protocols_fail_closed_without_credentials` (args beat env, env fallback works, both protocols fail closed) and `::test_retired_long_form_credential_names_are_ignored` (the retired names are inert). Both `monkeypatch.delenv` all four names: `tests/conftest.py` loads an untracked local `.env` into `os.environ`, so any test touching this path without clearing them passes locally and fails in CI.
- **Secure transport defaults (both protocols):**
  - RESTCONF: `verify=True` by default. `verify=False` is allowed **only** as an explicit opt-out with a logged warning (lab/self-signed use).
  - NETCONF: verify host keys by default (`verify=True`; `hostkey_verify` kept only as a deprecated alias). Opt-out allowed **only** explicitly with a logged warning.
  - Template changes must keep the secure default; reviewers must reject PRs that flip the default or silence the warning.
- **Contents:** generated package includes client, models, navigators, and minimal README (device, OS version, source YANG revisions, protocol). No `sdk-verify`, no `.env`, no log files.

## Multi-vendor / multi-version (HAL vision)

`yang2sdk` stays a vendor-agnostic generator. Scale lives downstream (see `README.md` sketch):

```
automation_project/
  hal/                        # vendor-agnostic protocols (node.py, port.py, l2/l3services.py)
  hal/adapters/<device>_<os-version>/  # HAL impls on top of generated clients
  clients/<device>_<os-version>/       # generated packages (uv dependencies)
```

Agents must not hardcode vendor quirks into the generic IR/templates. Device-specific workarounds belong in downstream adapters, with a comment linking the YANG module + revision + observed evidence.

## Dev workflows (normative)

Requires Python `>=3.12` (see `pyproject.toml`, `.python-version`).

```bash
uv sync --locked --extra lab   # lab extra: downloader/sdk-verify + live-sim tests
cp .env.example .env            # never commit .env
uv run yang-downloader
uv run pyang -p temp/yang_modules/<device>/ -f tree temp/yang_modules/<device>/*.yang > temp/yang_tree/<device>.txt
uv run yang2restconf <root1.yang> [<root2.yang> ...] [--device <device>] [--config-only]
uv run yang2netconf  <root1.yang> [<root2.yang> ...] [--device <device>] [--config-only]
uv run sdk-verify --device <d>  # LAB ONLY, see §11
```

### Lint — `ruff` (blocking)

```bash
uvx ruff check .
uvx ruff format --check .
```

`ruff` is the only formatter/linter. Do not introduce black/isort/flake8 configs.

### Type-check — `ty` + `pyrefly` (both blocking)

```bash
uvx ty check
uvx pyrefly check
```

- Both must pass on `src/`. Astral `ty` and Meta `pyrefly` are complementary; a pass in one does not excuse a failure in the other.
- Tool scoping lives in `pyproject.toml`: `[tool.ruff]` pins `target-version = "py312"` and excludes ephemeral `temp/` output; `[tool.pyrefly]` excludes `temp/`; `ty` needs no section (it respects `.gitignore`, which already covers `temp/`). There is no `[tool.pyright]` — pyright is gone, do not reintroduce it.
- Generated code under `temp/` is excluded from blocking type-checks (it is ephemeral output), but templates that *produce* it are not — template edits must still type-check at the template level and via a generated sample.

## Testing and CI

There is a `pytest` suite plus CI. `cli/sdk_verify.py` (`sdk-verify`) is the
lab harness for full-SDK validation and is **not** a test gate; it was never in
CI and must not be claimed as coverage.

- `sdk-verify` tiers (LAB ONLY, both protocols, replaces the old `tester` script):
  - `read` — recursive walk of the entire navigator tree at bounded depth,
    validating each payload. Automatic; no flags.
  - `rpc` — build every RPC `Input` and serialise it (`model_dump(by_alias=)`,
    `to_xml_payload()`) **without sending**. Automatic. This is where envelope
    and wire-name bugs surface across the whole RPC surface instead of on a
    device at call time.
  - `rpc` + `--rpc-allowlist` — actually dispatch, echoing each RPC first.
  - `crud` — `retrieve → update → read back` per container, then a proven
    restore. Requires `--write` **and** a second acknowledgement:
    `--allow-running-writes` (no `:candidate`, so edits land in running) or
    `--allow-restconf-writes` (RESTCONF writes are always live).
  - Non-negotiable properties, all covered by its own code and comments: bounded
    depth on every read (never a root read); snapshot to
    `temp/verify/<device>-<protocol>-snapshot.json` before any write, and refuse
    to write if that fails; hold the NETCONF lock and **abort** if `lock()` fails
    (RFC 6241 §8.5.1 makes the lock a precondition for writing running); treat
    a missing `:validate` as a skip, never a pass; prove restoration with a
    whole-tree digest and report `RESTORE NOT PROVEN` loudly naming the snapshot.
  - **A device rejection is not a client failure.** HTTP 400/404/405 and NETCONF
    `invalid-value`/`access-denied` are recorded as skips with the RFC section.
  - `create`/`delete` are deliberately **not** automated: no generic synthesiser
    can satisfy `must`/`when`/leafref/mandatory, so claiming CRUD coverage would
    be false. Report the coverage the tool has.

- Layout: `tests/test_matrix.py` (offline, no docker: matrix coverage, template secure defaults, navigator parity, rpc-free generation regression), `tests/test_notconf_protocol.py` + `tests/test_sdk_generate.py` (integration, gated on `NOTCONF_RUN_INTEGRATION=1` or `--integration`), `tests/notconf/matrix.json` (all 11 pre-built images, `smoke` flags latest-per-family), `tests/notconf/compose.yaml` (local lab), `tests/notconf/wait_healthy.py` (readiness probe, credentials parameterised per backend), `tests/srl/` (containerlab lab: topology, startup-config, pinned versions, and `lab.py` — the single source of truth for the committed defaults, used by both the test suite and the CI job).
- Simulated backend: `https://github.com/notconf/notconf` (admin/admin, lab-only). CI: `.github/workflows/ci-pr.yaml` (lint → typecheck → offline → smoke shards) and `ci-nightly.yaml` (full 11-tag matrix + `workflow_dispatch`).
- **Vendor-model-tree coverage comes from SR Linux, not the simulators — but it is NOT NMDA coverage.** An earlier revision of this file claimed SR Linux supplied NMDA coverage. It does not: measured from a raw `<hello>` (370 capabilities) on 25.10.1, its base set is `candidate`, `confirmed-commit`, `rollback-on-error`, `startup`, `url`, `validate`, `with-defaults`, `with-operational-defaults`, `yang-library` — **no `:nmda:1.0`**. It ships the `ietf-netconf-nmda` *module* without advertising the capability, which is exactly the false positive `test_nmda_is_detected_from_the_capability_uri_not_a_module_name` pins. Do not reintroduce that claim; the `has_nmda` branch's only live coverage remains offline, and that gap is real. What SR Linux *does* supply is a full commercial vendor model tree (hundreds of modules, deep augment closure, identityref leaves) through the generated client. `.github/workflows/ci-srl.yaml` deploys it via containerlab, pulls the node's own models over `get-schema`, compiles `temp/netconf_clients/srl`, then runs `tests/test_srl_netconf.py`. Three rules govern it:
  - **Severity is split on purpose.** Deploy / readiness / `get-schema` / generate are **blocking** — zero coverage must never render as a green run. The pytest step is `continue-on-error: true`, so a generator regression is reported without gating a merge. Do not collapse these into one all-or-nothing step.
  - **It must stay on a standard runner.** `ubuntu-24.04` (4 vCPU / 16 GB) is free for public repos; larger runners are billed even for public repos. SR Linux needs 2 vCPU / 4 GB, so the standard runner is sufficient.
  - **Versions are pinned in `tests/srl/PINNED.md`** (image tag *and* digest, plus the containerlab version). Update tag and digest together. An unpinned containerlab release can change deploy behaviour overnight and redden the pipeline with no code change.
- Golden snapshots under `tests/fixtures/golden/` are run-local and gitignored; promoting them to checked-in, diff-compared fixtures (plus `canonicalize_ast.py` snapshot diffs) is still open.
- Agents must not claim coverage beyond what the suite asserts. Current reality: there is no `xfail`
in the suite; RESTCONF and NETCONF CRUD round-trips make hard assertions (a `if-feature`
mismatch or a missing `ietf-interfaces` navigator skips them); NMDA discrimination may skip on the
documented factory-default race; `tests/test_srl_netconf.py` runs in CI but is **non-blocking**, so it
reports rather than gates (and it carries one skip, `test_hostname_mutate_restore`, for two reasons
documented in that file — neither of which is a generator defect); and
`tests/test_lab_device_netconf.py` is in no CI workflow, so anything it asserts is not gating anything.
- **A `skip` is not a pass.** The SDK suite skips deliberately and must say why: an image that
implements no config modules, a node the simulator cannot read, a device without `:validate`. When a
test covers zero nodes it must `pytest.skip`, never "succeed" vacuously.
- **A generated client must be exercised through its own public API.** The NETCONF leg of
`tests/test_sdk_generate.py` was previously unable to run at all, because it built the client
without opting out of host-key verification and so the secure default refused the simulator's random
key. Tests that construct a generated client against a simulator must pass `verify=False` explicitly
and say why; never weaken the client default to make a test pass.
- **Tests must not depend on a destructive default.** With `auto_commit=False`, an `edit()` to
`candidate` is invisible in `running` until an explicit `<commit>`. Any test asserting a read-back
of a NETCONF write must run edit → `validate` → `commit`, and must skip `validate` when `:validate`
is not advertised (RFC 6241 §8.6.4.1 makes it optional).
- Every capability flag (`has_validate`, `has_candidate`, `has_nmda`,
  `has_writable_running`, `has_confirmed_commit`, `has_rollback_on_error`) is a
  plain **bool attribute**, never a method or property. `getattr(client,
  "has_validate", False)` therefore returns the real flag, and
  `if client.has_validate:` is a correct test. One of these was briefly a method
  while its five siblings were attributes, which made `if client.has_validate:`
  always true and silently skipped validation — keep the six consistent.
- Do not add tests that require a live production device.
- Until golden fixtures land: verify template/IR changes by (a) generating a sample client, (b) running `ruff` + `ty` + `pyrefly`, (c) importing the sample and validating a live-simulator or lab-captured payload. State exactly what was and was not executed in the PR/summary.

## Safety and security (normative, non-negotiable)

- **Never request root `restconf/data/` on production.** Large configs can spike to 100% CPU and trigger watchdog reboot / OOM kill. Lab equipment only (per `README.md` warning).
- `sdk-verify` and `yang-downloader` run against lab devices only. Confirm `DEVICE_IP` in `.env` is a lab address before running.
- `temp/` is ephemeral and gitignored (only `.gitkeep` scaffolding is committed). Never import from `temp/` in shipped code; never commit generated clients, logs (`*.log`), or YANG dumps.
- `*.env` / `.env` never committed. `DEVICE_PASS` in cleartext on disk is already a compromise — do not print, log, or propagate it. Full response bodies must not be committed to logs at INFO in production paths.
- Timeouts, failover URL order, and `verify`/host-key defaults are safety features, not tuning knobs. Changing them requires explicit justification.
- `auto_commit` defaults to **False** on the generated NETCONF client. It is a destructive
  default and must stay opt-in; the safe sequence is `edit(target="candidate")` →
  `validate(source="candidate")` → `commit()` (RFC 6241 §8.6.4.1, §8.3.4.1).
- The NETCONF `edit()` emits `<error-option>rollback-on-error</error-option>` (RFC 6241 §7.2)
  **only** when `target == "running"` and the device advertises `:rollback-on-error`. Three
  deliberate restrictions, each with its own test: not on `candidate` (where `<discard-changes>`
  is the right primitive, and §8.5.1 warns this option can revert another session's staged work);
  not on the NMDA `<edit-data>` branch, whose error behaviour already corresponds to
  rollback-on-error (RFC 8526 §3.1.2); and callers writing `running` are expected to hold a lock
  first (§8.5.1). Do not "simplify" the gate to `if self.has_rollback_on_error`.
- `replace()` refuses a model with unset fields (`allow_partial=True` overrides). A replace
  body IS the complete resource (RFC 8040 §4.5; RFC 6241 §8.2.1), so a partial model silently
  deletes the rest. Do not remove this guard without an equivalent loud failure.
- The emitter compiles every generated `.py` before reporting success (`core._validate_generated`).
  Never bypass it: a client that cannot be imported is not a deliverable.

## Contribution guardrails

- Minimal diffs; no new runtime dependencies without justification in the PR (each dep is downstream install weight for every generated package consumer).
- Keep `ir.py` ↔ templates in sync: new IR fields require template rendering + a generated sample proving the output imports.
- Preserve strictness: `extra="forbid"`, `validate_assignment`, alias/tag/ns plumbing, `is_config` filtering, choice-exclusion validator, 64-bit serializers.
- Commit messages: concise, match existing style (`git log --oneline -10` first). Never commit secrets, `temp/`, or `.venv/`. Stage only intended files (`git status` + `git diff` before commit).
- PRs touching protocols must cite RFC sections; PRs touching safety defaults must call out the warning behavior explicitly.

## Evidence rule

Every summary of work must state: files changed, commands run (`ruff`, `ty`, `pyrefly`, generators), and what was verified vs what remains WIP (tests/CI, live-device validation). No "already verified" hand-waving.
