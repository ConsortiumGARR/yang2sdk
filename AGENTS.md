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
  → yang2sdk root picker (proposes roots; human confirms)
  → yang2sdk --protocol netconf|restconf (pyang plugin: AST → IR → Jinja)
  → sdk-verify (full-tree read + RPC round-trip + gated CRUD, LAB ONLY)
  → publish generated client as versioned package
```

| Stage | Entry point | Notes |
|---|---|---|
| Download YANG | `src/yang2sdk/cli/downloader.py` (`yang-downloader`) | `ietf-netconf-monitoring/get-schema` via `ncclient`. Output: `temp/yang_modules/<device>/`. |
| Pick roots | `src/yang2sdk/cli/rootpicker.py` (`yang2sdk` with no `--roots`) | Proposes modules that declare top-level data **or** operations; human confirms with Enter, or filters. Excludes `ietf-netconf*` transport RPCs (`get-config`/`edit-config`); ranks `ietf-yang-library`/monitoring/deviations last. Measured: 31 of 357 modules on SR Linux (both ci-srl roots among them), 8 of 130 on G30 including `coriant-rpc`/`coriant-cli`, whose entire surface is operations. `pyang -f tree` over the same tree costs the same (~16s) but yields 18k lines to read by hand. |
| Compile | `src/yang2sdk/cli/main.py` (`yang2sdk --protocol netconf\|restconf`) | In-process pyang invocation. Defaults: `--yang-dir temp/yang_modules/<device>`, `--output-dir temp/{restconf,netconf}_clients/<device>`. Flags: `--device`, `--yang-dir`, `--output-dir`, `--config-only`, `--device-version`, `--package-version`, `--deviation-module`, `--feature`, `--features-file`, `--no-device-features`, `--ignore-error`, `--check-model-gaps` (NETCONF only). |
| IR | `src/yang2sdk/plugin/src/ir.py` (`IRBuilder`) | AST → `IRModule/IRModel/IREnum/IRNavNode/IRField` dataclasses. Load-bearing logic, see §5. |
| Emit | `src/yang2sdk/plugin/src/core.py` (`Yang2Restconf`, `Yang2Netconf`) | IR → Jinja templates in `src/yang2sdk/plugin/src/templates/{restconf,netconf}/`. |
| Validate | `src/yang2sdk/cli/sdk_verify.py` (`sdk-verify`) | Walks the generated navigator tree and calls its own methods: `read` (full tree, bounded depth), `rpc` (serialise every Input, no send), `crud` (idempotent merge + proven restore). Both protocols. LAB ONLY. |
| Debug helper | `src/yang2sdk/cli/canonicalize_ast.py` | AST canonicalizer for diffing generated code. |

### Generated SDK layout (both protocols)

```
temp/<proto>_clients/<device>/          # --output-dir: a container, one project
└── <device>_<os-version>/              # project: pyproject.toml, README.md, MANIFEST...
    └── src/<device>_<os-version>/      # package: import <device>_<os_version>
        __init__.py                   # RestconfClient / NetconfClient
        session_manager.py            # transport only
        data_models/{models.py, _base.py, __init__.py}
        data_navigators/{navigators.py, _base.py, __init__.py}
```

- **One copy of the code**, defined once in `src/yang2sdk/layout.py` and consumed by the emitter, `sdk-verify`, `model_gaps` and the tests. The previous shape emitted the same modules twice (flat at `output_dir` for the lab import path, again at `output_dir/<package_name>` for pip): two trees that could drift, an sdist that needed a hand-written exclude, and a generated README whose `uv add path/to/<package_name>` pointed at a directory with no `pyproject.toml` (measured: `uv build` refused it). `src/` is not decoration — hatchling packages a project-root layout as `./__init__.py` + `./data_models/`, i.e. straight into `site-packages`.
- **The container is refused if it holds another package** (a compile for a different OS version) or a pre-`src/` flat copy: either would leave two importable trees and an ambiguous "find the package" step. Nothing is deleted automatically.
- Models are strict: `extra="forbid"`, `validate_assignment=True`, `defer_build=True`.
- **Deferred models are built per payload, never per graph.** `defer_build=True` means a pydantic schema is generated on first use, and that is expensive (measured on the G30 tree: 1355 classes / ~30k fields / 17–34s cold, against a 0.1s device reply). pydantic-xml asserts a field's model serializer exists before walking a parent's fields (`ModelDeserializer.deserialize` / `ModelSerializer.serialize` in `pydantic_xml/serializers/factories/model.py`) even when the element/value is absent, so the target model **and its direct children** must be built; nothing deeper may be, unless the payload/instance actually reaches it. `ensure_xml_models_built` / `ensure_instance_models_built` implement that rule. `rebuild_model_and_dependencies` still exists as the explicit whole-graph build (`--check-model-gaps`) — do not put it back on the read or write path, and keep the pruning helpers (`prune_xml_tree_to_depth`, `prune_xml_to_content`) as pure metadata walks. Pinned by `tests/test_matrix.py::test_from_xml_builds_only_the_models_the_payload_instantiates` and `::test_payload_driven_build_matches_the_whole_graph_build`.
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
- **Provenance scoping (normative):** `$DEVICE_NAME` and `$DEVICE_VERSION` are **one pair describing one lab device** (`.env.example` exports them beside `DEVICE_IP`/`DEVICE_USER`/`DEVICE_PASS`), so `--device` inherits `$DEVICE_NAME` and `--device-version` inherits `$DEVICE_VERSION` **only when the target device is `$DEVICE_NAME`**. An explicit `--device-version` always wins, and an explicit `""` means "no version", never "fall back". The fallback used to be unconditional, so any compile in a `.env`-loaded shell stamped that one device's OS version onto *every* generated client — and it lands in the package name, the package version and the generated README, all of which ship. Measured: a notconf-cisco-nx 10.4-4 image produced `notconf_cisco_nx_10_4_4_4_8_0`, version `4.8.0`, README "version `4.8.0`" — for a device that is 10.4-4. Models and navigators never see the value, so this is provenance rather than correctness; it is still a lab-identity leak into a publishable artifact, the same class as the no-secrets rule below. Corollary: **never pass a version-bearing string as `--device`** — `core.py` builds the package name as `_sanitize_pkg(f"{device}_{device_version}")`, so a slug that already contains the tag double-encodes (`notconf_cisco_nx_10_4_4` + `_0_0_0`). `tests/conftest.py::_compile_all` therefore derives the directory slug, `--device` and `--device-version` from the one matrix entry. Pinned by `tests/test_matrix.py::test_device_version_env_fallback_is_scoped_to_its_own_device` (precedence), `::test_compile_all_names_the_installable_package_from_the_matrix_slug` (compiles; asserts the package at `src/<slug>` plus `version = "10.4.4"` — this is the one that catches the wiring), and `::test_generated_package_name_matches_the_matrix_slug` (naming scheme across all 11 tags; it holds by construction and catches scheme drift, not the `--device` wiring).
- **Installability:** self-contained (own `pyproject.toml`, no `temp/` or repo-absolute imports), in the `src/` layout: the project is `<container>/<device>_<os-version>/`, the package `<project>/src/<device>_<os-version>/`. Installable via:
  - `uv add path/to/<container>/<device>_<os-version>`
  - `uv add --editable path/to/<container>/<device>_<os-version>`
  - (and equivalents: git URL / registry once published).
- **No secrets:** never embed usernames, passwords, tokens, IPs, or private CA bundles in generated code, templates, tests, or examples. Auth comes from caller args or environment at runtime only.
- **Credential contract (both protocols, normative):** credentials are resolved **by source**, never field by field. An explicit `password` suppresses both environment key names; an explicit `key_filename` suppresses `DEVICE_PASS` and takes its passphrase only from an explicit `passphrase` (or from `DEVICE_SSH_KEY_PASSPHRASE` when the key itself came from `DEVICE_SSH_KEY`). The generated client and `yang-downloader` reject both explicit values as ambiguous; `sdk-verify` warns and uses the key for NETCONF (so `--protocol both` can pair a RESTCONF password with a NETCONF key). With neither explicit, the client reads **`DEVICE_USER`** plus **`DEVICE_PASS`** (password) or, for NETCONF, **`DEVICE_SSH_KEY`** / **`DEVICE_SSH_KEY_PASSPHRASE`** (SSH public-key authentication); if both environment credentials are set, the key wins with a logged warning, never silently. A username is always required; NETCONF then needs *either* a password or a key, RESTCONF needs a password (its transport is HTTP/TLS, so SSH keys do not apply). The username must match the XML `Char` production (RFC 6242 §3, RFC 6241 §2.2); both the generated client and `yang-downloader` reject non-`Char` code points before transport work instead of relying on the server to drop the session. There are no aliases. `DEVICE_USERNAME`/`DEVICE_PASSWORD` are **retired, not aliased** — a generated client must ignore them even when they are the only ones exported. Rationale: every other consumer in this repo (`.env.example`, `cli/downloader.py`, `cli/sdk_verify.py`, the lab matrix in `tests/conftest.py`) has always used the short form, so the long form existed only in these templates and made the `.env` the project tells you to copy fail against a generated client. A two-name alias was tried and was worse: the two templates resolved the pairs in opposite order, so a host exporting both names authenticated as two different identities depending on transport. Do not reintroduce an alias. If no credential is available, the constructor raises `ValueError` (never a `Warning` subclass used as an exception) *before* any transport work: both protocols fail closed, so no client can exist that would attempt an unauthenticated session. Key paths are validated at the same point, and an explicit key disables ncclient's SSH-agent and `~/.ssh` fallbacks (`allow_agent=False`, `look_for_keys=False`): ncclient otherwise tries every agent/default key after an explicit key fails, so a typo -- or a valid but unauthorized key -- would silently authenticate as another identity. A certificate path additionally requires its adjacent `<key>` private key, because agent-held keys are not consulted when a path is explicit. Covered by `tests/test_matrix.py::test_both_protocols_fail_closed_without_credentials` (args beat env, env fallback works, both protocols fail closed), `::test_retired_long_form_credential_names_are_ignored` (the retired names are inert), `::test_netconf_ssh_key_authenticates_without_a_password`, `::test_explicit_password_wins_over_environment_key` (an explicit password is not overridden by an environment key), `::test_explicit_key_ignores_environment_password_and_passphrase`, `::test_explicit_password_and_key_ambiguity_is_not_silent`, `::test_environment_password_and_key_warns_and_key_wins`, `::test_password_auth_keeps_ncclient_agent_and_default_key_discovery`, `::test_netconf_ssh_certificate_and_passphrase_reach_ncclient` (including the passphrase-fallback warning), `::test_paramiko_loads_real_key_encrypted_key_and_openssh_certificate` (real ssh-keygen material, not a fake string), `::test_ncclient_tries_the_key_passphrase_as_a_password_after_key_failure` (pins the dependency behaviour), `::test_netconf_ssh_key_environment_fallback`, `::test_netconf_missing_key_path_fails_closed`, `::test_certificate_without_its_private_key_is_refused`, `::test_downloader_explicit_key_disables_agent_and_default_key_fallbacks`, `::test_downloader_and_sdk_verify_accept_key_credentials`, `::test_downloader_environment_password_and_key_warns_and_uses_key`, `::test_downloader_warns_when_key_passphrase_is_supplied`, `::test_netconf_username_must_match_the_xml_char_production`, and `::test_downloader_rejects_a_non_xml_username`. Tests `monkeypatch.delenv` every credential name (including the key pair): `tests/conftest.py` loads an untracked local `.env` into `os.environ`, so any test touching this path without clearing them passes locally and fails in CI. **Certificate semantics:** an OpenSSH user certificate is passed as `key_filename` using the conventional `<private-key>-cert.pub` name — paramiko's `PKey.from_path` strips the suffix to load the private key and attaches the certificate. The adjacent private key must exist; an explicit path never falls back to an agent-held key. ncclient exposes no separate passphrase parameter, so a key passphrase occupies its single `password` slot (`SSHSession._auth`). When `key_filename` is configured that slot must carry the passphrase or **nothing**, never the account password: paramiko 5 rejects an *unencrypted* key when given a password (`TypeError: Password was given but private key is not encrypted`), so an account password in that slot makes key auth fail before it is tried and silently falls back to password auth. The **account password** is therefore not placed in that slot. ncclient will, however, try an encrypted key's passphrase as a password if public-key authentication fails (`SSHSession._auth`); every construction with a passphrase logs a warning naming that behaviour, and the dependency behaviour is pinned by `tests/test_matrix.py::test_ncclient_tries_the_key_passphrase_as_a_password_after_key_failure`. Use an unencrypted key if that fallback is unacceptable. Real key, encrypted-key and CA-signed certificate loading is pinned by `::test_paramiko_loads_real_key_encrypted_key_and_openssh_certificate`. **Do not call the certificate format an RFC standard:** the authentication *method* is RFC 4252 §7 `publickey`, and §7 permits a certificate in the public-key blob, but the OpenSSH certificate encoding is the vendor extension documented by the IETF Internet-Draft `draft-ietf-sshm-cert` (successor to `draft-miller-ssh-cert`), not an RFC. Cite the draft for the format and RFC 4252 §7 for the method.
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
mkdir -p temp/yang_tree
uv run pyang -p temp/yang_modules/<device>/ -f tree temp/yang_modules/<device>/*.yang > temp/yang_tree/<device>.txt
uv run yang2sdk --protocol netconf   [--device <device>]   # picks roots interactively
uv run yang2sdk --protocol restconf --device <device> --roots <mod1>,<mod2>
uv run sdk-verify --device <d>  # LAB ONLY, see §11
```

### Lint — `ruff` (blocking)

```bash
uv run ruff check .
uv run ruff format --check .
```

`ruff` is the only formatter/linter. Do not introduce black/isort/flake8 configs.

### Type-check — `ty` + `pyrefly` (both blocking)

```bash
uv run ty check
uv run pyrefly check
```

- **Run the gates with `uv run`, never `uvx`.** `ruff`, `ty` and `pyrefly` are pinned
  *exactly* in the `dev` dependency group and `CI` runs `uv sync --locked`, so CI
  installs what `uv.lock` says. `uvx <tool>` resolves the newest release per
  invocation and can redden `main` with no commit to explain it — measured, not
  hypothetical: `ty` 0.0.60 passed, 0.0.86 flags a deprecated `@contextmanager`
  overload. Bump with `uv lock --upgrade-package <tool>` (procedure in
  `docs/dev.md`).
- Both must pass on `src/`. Astral `ty` and Meta `pyrefly` are complementary; a pass in one does not excuse a failure in the other.
- Tool scoping lives in `pyproject.toml`: `[tool.ruff]` pins `target-version = "py312"` and excludes ephemeral `temp/` output; `[tool.pyrefly]` excludes `temp/` and adds `scripts` to `search-path`; `[tool.ty.environment]` adds the same `scripts` root via `extra-paths`. `ty` used to need no section because it respected `.gitignore`, which covered `temp/` — that stopped being true once `scripts/release.py` imported `scripts/changelog.py`, and the fix was a section rather than a suppression. There is no `[tool.pyright]` — pyright is gone, do not reintroduce it.
- `scripts/` is a source root for both checkers and for pytest (via `tests/conftest.py`), but it is **not** in `[tool.hatch.build.targets.wheel]`, which stays `["src/yang2sdk"]`. Release tooling must never become downstream install weight.
- Generated code under `temp/` is excluded from blocking type-checks (it is ephemeral output), but templates that *produce* it are not — template edits must still type-check at the template level and via a generated sample.

## Testing and CI

There is a `pytest` suite plus CI. `cli/sdk_verify.py` (`sdk-verify`) is the
full-SDK validation harness. It is **no longer outside CI**:
`tests/test_sdk_verify_ci.py` drives its verifier from pytest against the
notconf containers the smoke matrix already starts, so it is a **blocking**
gate per image (`read,rpc`), and `ci-srl.yaml` runs it with `crud` against the
full SR Linux tree. That closed the gap that let three shipped defects survive:
the RESTCONF ancestor-envelope unwrap (`7bdb8cd`), the NETCONF augment-closure
gap, and the `ietf-notification-capabilities` model hole. Agents must not
describe it as unenforced coverage.

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
- Simulated backend: `https://github.com/notconf/notconf` (admin/admin, lab-only). CI: `.github/workflows/ci-pr.yaml` (lint → typecheck → offline → smoke shards) and `ci-nightly.yaml` (full 11-tag matrix + `workflow_dispatch`). `ci-pr` ends in a `ci-gate` job that `needs` all other jobs; the `main` branch ruleset requires **only** that one stable check name (GITHUB_TOKEN cannot write rulesets on the free plan, so the list is maintained by hand — add new `ci-pr` jobs to `ci-gate`'s `needs` in the same PR).
- **Vendor-model-tree coverage comes from SR Linux, not the simulators — but it is NOT NMDA coverage.** An earlier revision of this file claimed SR Linux supplied NMDA coverage. It does not: measured from a raw `<hello>` (370 capabilities) on 25.10.1, its base set is `candidate`, `confirmed-commit`, `rollback-on-error`, `startup`, `url`, `validate`, `with-defaults`, `with-operational-defaults`, `yang-library` — **no `:nmda:1.0`**. It ships the `ietf-netconf-nmda` *module* without advertising the capability, which is exactly the false positive `test_nmda_is_detected_from_the_capability_uri_not_a_module_name` pins. Do not reintroduce that claim; the `has_nmda` branch's only live coverage remains offline, and that gap is real. What SR Linux *does* supply is a full commercial vendor model tree (hundreds of modules, deep augment closure, identityref leaves) through the generated client. `.github/workflows/ci-srl.yaml` deploys it via containerlab, pulls the node's own models over `get-schema`, compiles `temp/netconf_clients/srl`, then runs `tests/test_srl_netconf.py`. Three rules govern it:
  - **Severity is split on purpose.** Deploy / readiness / `get-schema` / generate are **blocking** — zero coverage must never render as a green run. The pytest step is `continue-on-error: true`, so a generator regression is reported without gating a merge. Do not collapse these into one all-or-nothing step.
  - **It must stay on a standard runner.** `ubuntu-24.04` (4 vCPU / 16 GB) is free for public repos; larger runners are billed even for public repos. SR Linux needs 2 vCPU / 4 GB, so the standard runner is sufficient.
  - **Versions are pinned in `tests/srl/PINNED.md`** (image tag *and* digest, plus the containerlab version). Update tag and digest together. An unpinned containerlab release can change deploy behaviour overnight and redden the pipeline with no code change.
- Golden snapshots under `tests/fixtures/golden/` are run-local and gitignored; promoting them to checked-in, diff-compared fixtures (plus `canonicalize_ast.py` snapshot diffs) is still open. They are also `pytest.mark.integration`, so they are invisible to the offline gate as well as to CI — two independent reasons nothing in that directory can fail a merge. What is stored is a **sorted list of the payload's top-level keys**, not a payload dump: the comparison consumes only key names, and the old write truncated `json.dumps(...)` at 20000 characters, producing unparseable JSON that raised `JSONDecodeError` on every subsequent local run for any payload over that size.
- Agents must not claim coverage beyond what the suite asserts. Current reality: there is no `xfail`
in the suite; RESTCONF and NETCONF CRUD round-trips make hard assertions (a `if-feature`
mismatch or a missing `ietf-interfaces` navigator skips them); NMDA discrimination may skip on the
documented factory-default race; `tests/test_srl_netconf.py` runs in CI but is **non-blocking**, so it
reports rather than gates (and it carries one skip, `test_hostname_mutate_restore`, for two reasons
documented in that file — neither of which is a generator defect); and
`tests/test_lab_device_netconf.py` is in no CI workflow, so anything it asserts is not gating anything. `tests/test_sdk_verify_ci.py` **is** blocking, per image, for every `sdk-verify` `read`/`rpc` finding.
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
- `temp/` is ephemeral and gitignored (untracked; every writer creates its own output dir on demand). Never import from `temp/` in shipped code; never commit generated clients, logs (`*.log`), or YANG dumps.
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

## Releasing (normative)

Two commands and a review. Never hand-type a tag.

```bash
uv run python scripts/release.py --bump --dry-run   # prints the version it would use
git switch -c release/2026.11.1
uv run python scripts/release.py --bump             # notes, version, lock, commit
#   ... PR, merge ...
git switch main && git pull
uv run python scripts/release.py                   # derive the tag, push it
```

- **Neither the version nor the tag is typed.** CalVer makes the version the date, so the only free variable is the same-day sequence number, and PyPI already records which ones are used: `next_version()` returns `2026.11.1`, or `2026.11.1.2` after two releases that day, and reports every candidate it skipped with the reason. A *tag* collision is reported distinctly from a PyPI one because it is recoverable — the operator may prefer to move the stale tag instead of burning the number, and that is their call, not the script's. `--bump <version>` remains for a deliberate jump.
- **The tag is derived, never typed.** `release.yaml` asserts `tag == pyproject version`; `scripts/release.py` computes the tag from the same single source of truth. It fired on 2026-10-16 precisely because a human typed `v2026.10.16.1` one commit before the bump landed. A guard that can only say no has moved the failure to a human, not removed it.
- **The bump is a code change and goes through review.** The ruleset requires a PR for `main`; the bump is the commit the tag will name, so it is the one that deserves the review. The tag is the only step taken from a clean `main`.
- **A version cannot ship undocumented.** `scripts/changelog.py` refuses unless `CHANGELOG.md` has a non-empty `## [<version>]` section, `## [Unreleased]` is empty, and the heading date agrees with the CalVer version. Enforced again in `release.yaml`'s build job, so a tag pushed around the script is still refused. Humans write the prose; the tool never invents it and never publishes without it.
- **A version cannot be reused.** PyPI filenames are permanent (a re-upload is `400 File already exists`; yanking does not free the name), so the script checks PyPI before tagging and **fails closed** if it cannot reach it. Same-day re-releases use `YYYY.M.D.N`.
- **Never move a published tag.** `release.py` refuses if the tag exists on origin and prints the commands to move it, because rewriting a public ref is a human decision.
- `release.yaml` also runs `twine check --strict`, publishes via Trusted Publishing with PEP 740 attestations, installs the published wheel **from PyPI** to verify it (`verify-published`), and creates the GitHub Release from the changelog section. The full stage table is in `docs/dev.md`; the gates are pinned by `tests/test_changelog.py` and `tests/test_release_script.py`, both of which include a negative control.

## Contribution guardrails

- Minimal diffs; no new runtime dependencies without justification in the PR (each dep is downstream install weight for every generated package consumer).
- Keep `ir.py` ↔ templates in sync: new IR fields require template rendering + a generated sample proving the output imports.
- Preserve strictness: `extra="forbid"`, `validate_assignment`, alias/tag/ns plumbing, `is_config` filtering, choice-exclusion validator, 64-bit serializers.
- Commit messages: concise, match existing style (`git log --oneline -10` first). Never commit secrets, `temp/`, or `.venv/`. Stage only intended files (`git status` + `git diff` before commit).
- **Behaviour change ⇒ `## [Unreleased]` entry, same commit**, under the matching `### Added|Changed|Fixed|Removed`; say what broke, not what the diff did. Tests/docs/CI-only churn folds into the entry it supports. `scripts/changelog.py` still gates release.
- PRs touching protocols must cite RFC sections; PRs touching safety defaults must call out the warning behavior explicitly.

## Trust protocol

Agents are hyper-skeptic (Trust 0): every assertion and every decision is a claim
requiring proof — authoritative evidence or an empirical demonstration. The source
code is the only source of truth; prior conversation, documentation and recollection
are not evidence.

Three consequences, not a procedure:

- **Nothing about unread code.** A claim about a file nobody has opened is a
  guess, however confident it sounds.
- **Demonstrate, don't deduce.** Where behaviour can be observed by running it —
  a reproduction, a negative control, an offline test — run it and show the
  output. Reasoning about what a template emits is not verification.
- **Unproven claims are hypotheses.** They may shape a question; they may not
  decide one, enter a commit message, or appear in a PR description. When proof is
  unreachable here, name what is missing instead of proceeding as if it exists.

## Evidence rule

Every summary of work must state: files changed, commands run (`ruff`, `ty`, `pyrefly`, generators), and what was verified vs what remains WIP (tests/CI, live-device validation). No "already verified" hand-waving.
