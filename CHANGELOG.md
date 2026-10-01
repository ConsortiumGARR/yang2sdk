# Changelog

All notable changes to `yang2sdk` are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is
`0.x` pre-1.0 (breaking changes allowed, documented).

## [Unreleased]

### Added

- **`sdk-verify`: full-SDK validation against a lab device.** The old `tester`
  script issued one to four `GET`s: it was RESTCONF-only, read only the
  top-level `Data` properties at `depth=2`, never touched a write method, and
  never touched `client.operations`, so every RPC and every nested node in a real
  client was untested. It also had two vacuous passes (an empty list reported
  `[OK] Parsed 0 model(s)`, and a client with no data properties reported
  `[OK] every navigator validated`).
  `sdk-verify` walks the generated navigator tree and calls the client's own
  methods, recording one row per `(node, method)`:
  - `read` — recursive walk of the entire data tree, bounded depth, automatic.
  - `rpc` — builds every RPC `Input` and serialises it (`model_dump(by_alias=)`,
    `to_xml_payload()`) **without sending**, so envelope bugs surface across the
    whole RPC surface instead of at call time on a device. `--rpc-allowlist`
    dispatches named RPCs, echoing each first.
  - `crud` — `retrieve → update → read back` per container with an idempotent
    merge, a pre-write snapshot, a held NETCONF lock, and a final whole-tree
    digest that reports `RESTORE NOT PROVEN` loudly. Gated behind `--write` plus
    a second acknowledgement: `--allow-running-writes` (no `:candidate`, so
    edits land in running) or `--allow-restconf-writes` (RESTCONF writes are
    always live — there is no candidate and no `<discard-changes>`).
  Device rejections (HTTP 400/404/405, NETCONF `invalid-value`/`access-denied`)
  are recorded as skips citing the RFC section, not as client failures. Every
  skip carries its reason, and a node covering nothing is never a pass.
  `create`/`delete` are deliberately not automated: no generic synthesiser can
  satisfy arbitrary `must`/`when`/leafref/mandatory constraints, so claiming
  full CRUD coverage would be false.
  `tester` is removed; `sdk-verify` replaces it.

### Fixed

- **`retrieve(depth=N)` on a RESTCONF client returned data its own strict model
  rejected.** RFC 8040 §4.8.2 makes `depth` a server-side ceiling: a compliant
  server returns nodes up to the requested depth, and containers sitting
  exactly on that boundary come back *empty* (children truncated). The payload
  the client prunes starts below the URI target node, so those boundary
  containers sit at recursion level `depth + 1` — but the pruner targeted
  `min(requested, observed)`, one level too shallow. A real G30 made it
  concrete: a `depth=2` read of `ne:ne/system/networking/interface` came back
  with `ethernet: {}` on every entry, and the strict model failed on the
  mandatory `eth-resource-ref` behind the empty container; the same read of
  `routing-protocol` failed on `ospf.router-id`. The off-by-one went the other
  way too: a complete tree read with a deep request had its legitimately empty
  deepest containers *deleted* instead of kept.
  *Fix:* the pruner now removes empty containers only at level `depth + 1`, and
  only when the response actually reaches that level (`observed == depth + 1`,
  the truncation signature); a shallower tree was not truncated, and a server
  that returns deeper than asked ignored `depth` — in both cases nothing is
  deleted. This also removes the dead `_prune_at_max_depth` duplicate. Found by
  running `sdk-verify` against the lab G30;
  `tests/test_matrix.py::test_read_pruning_is_bounded_by_the_requested_depth`
  is the tripwire, with the G30's exact truncated-list shape.

- **`sdk-verify` ignored the `.env` the README points at.** A fresh
  `uv sync --locked --extra lab && uv run sdk-verify --device <d>` died with
  `credentials missing` even though the repo `.env` — the file the workflow
  configures — sat in the working directory: `yang-downloader` calls
  `load_dotenv()`, `sdk-verify` read `os.environ` raw. Both lab tools now load
  it; exported variables still win (`load_dotenv` never overrides).

- **`sdk-verify --protocol both` validated the RESTCONF client twice.** Both
  generated clients are packages named after the device
  (`temp/restconf_clients/g30`, `temp/netconf_clients/g30`), so the second
  `importlib.import_module(client_dir.name)` returned the first protocol's
  module from `sys.modules`, and the run reported `exposes no netconf client
  class`. Clients are now imported under a per-protocol module name.

- **`sdk-verify --json-out` with `--protocol both` wrote one report over the
  other.** With more than one protocol, the report is now written per protocol
  (`<stem>_<protocol><suffix>`), and each path is printed.

- **A NACM `access-denied` on a *write* was a red `FAIL`.** The device's
  refusal policy is per-node: the lab G30's NACM denies `edit-config` to the
  test user under `/ne/system` (security, ssh, console, ipsec) while allowing
  the rest, so the crud tier reported nine failures that are policy answers,
  not client defects. The update and read-back paths now classify device
  rejections like the read path does (skip, with the RFC 8341 NACM citation).

- **A YANG node named after a Python builtin could produce an unimportable
  client.** `container property` generated `@property def property(...)`, and
  because the class body had bound the name `property` to the property object,
  every *later* decorator in that body resolved to it:
  `TypeError: 'property' object is not callable` at import. A leaf named `str`,
  `int` or `bool` failed the same way one line later
  (`FieldInfo | None`), because `models.py.jinja` emits no
  `from __future__ import annotations` and the annotation is evaluated eagerly; a
  leaf-list named `list` broke NETCONF only, whose annotations are a bare
  `list[...]`. The emitter's `_validate_generated` cannot catch this — the output
  is valid Python, so it parsed and shipped.
  *Fix:* `_to_field_name` now also escapes the builtins the emitter writes bare
  inside a class body (`_generated_code_builtins`). Deliberately not all of
  `builtins`: measured across the 1470 vendor YANG files in `temp/yang_modules/`,
  `id`/`type`/`input`/`filter` appear three to four orders of magnitude more
  often than `property`, and escaping them would rename ordinary leaves for no
  benefit.
  `tests/test_matrix.py::test_builtin_named_nodes_do_not_break_the_generated_import`
  is the tripwire for widening the emitter's vocabulary.

- **RESTCONF lists hid their keys, making list items unaddressable.** `is_key`
  was emitted only on the NETCONF branch, and the RESTCONF list `__call__` was
  the shared positional `ListNode.__call__(*keys)`. A RESTCONF client therefore
  told a caller nothing about which fields keyed a list — and keys are a YANG
  `key` statement, not part of an instance payload, so they cannot be recovered
  by inspecting device data. Arity was unchecked too: the wrong number of keys
  built a wrong URL and failed only at the device.
  *Fix:* RESTCONF models now carry `is_key`, and RESTCONF list navigators emit a
  named `__call__(self, id, kind)` mirroring NETCONF. Both surfaces are now
  symmetrical.

- **Dead code after `return` shadowed the NETCONF XML parsers.** The NETCONF
  `data_models/_base.py.jinja` template ended with three stacked
  `from_xml`/`from_xml_tree` definitions; the middle one had
  `return super().from_xml_tree(...)` followed by `return super().from_xml(source, ...)`,
  and the last definition silently won. That last one did not call
  `_drop_unbound_attributes`, so the undeclared-attribute stripping the G30
  workaround depends on was bypassed for any payload parsed through
  `from_xml_tree`. Removed the unreachable code.

- **NETCONF `<edit-config>` never requested `rollback-on-error`.** The capability
  was discovered (`has_rollback_on_error`) and then ignored, so a failure part
  way through an edit on a device without `:candidate` could leave partial config
  in `running`. The option is now emitted — as the
  `<error-option>rollback-on-error</error-option>` **element** (RFC 6241 §7.2) —
  only when `target == "running"` and the capability is advertised. Three
  deliberate restrictions: not on `candidate`, where `<discard-changes>` is the
  right primitive and §8.5.1 warns this option can revert another session's
  staged work; not on the NMDA `<edit-data>` branch, whose error behaviour already
  corresponds to rollback-on-error (RFC 8526 §3.1.2); and §8.5.1 makes a held lock
  a precondition, so `sdk-verify` refuses to write running without one.

- **A generated client could not authenticate with the `.env` the project tells
  you to copy, and the two protocols failed differently when it had none.**
  *Symptom:* `cp .env.example .env` gives you `DEVICE_USER` / `DEVICE_PASS`.
  `yang-downloader` and `sdk-verify` read those names; a generated client read
  `DEVICE_USERNAME` / `DEVICE_PASSWORD`, so the documented setup left every
  client unauthenticated. With no credentials at all the two protocols also
  failed in two different, both-opaque ways: RESTCONF did
  `raise UserWarning(...)`, and a `Warning` subclass used as an exception is
  invisible to `except ValueError`, while NETCONF raised nothing at all and
  handed `None` straight to `manager.connect()`.
  *Root cause:* the templates were the only place in the project that ever used
  the long form, and RESTCONF was the only one of the two that attempted a
  check. The failure was invisible because the credentials path had no test.
  *Fix:* both templates now resolve `username` / `password` from the constructor
  args, else `DEVICE_USER` / `DEVICE_PASS` — the names `.env.example`,
  `cli/downloader.py`, `cli/sdk_verify.py` and the lab matrix in
  `tests/conftest.py` have always used — and raise `ValueError` when a value is
  missing, before any transport work. One `.env` now serves every consumer.
  *Removed:* `DEVICE_USERNAME` / `DEVICE_PASSWORD` are **retired, not aliased**.
  A two-name alias was tried first and was strictly worse: the two templates
  resolved the pairs in *opposite* order, so a host exporting both names
  authenticated as two different identities depending on which transport was
  used. One name makes that hazard unrepresentable.
  *Tests:* `test_both_protocols_fail_closed_without_credentials` (args beat env,
  env fallback works, both protocols fail closed) and
  `test_retired_long_form_credential_names_are_ignored` (the long form is inert,
  and the current names win when both are exported). Both `monkeypatch.delenv`
  all four names — `tests/conftest.py` loads an untracked local `.env` into
  `os.environ`, so a test that did not clear them passed on a developer machine
  and failed in CI.

The RPC/action path had **no test coverage at all** — the only rpc-related
test asserted the degenerate rpc-*free* case. Verifying it against the primary
RFC text (`rfc-editor.org`, not the `RFCs/*.md` summaries) exposed seven
independent defects. Each is listed with the symptom, the root cause, the
normative citation, the fix, and the offline test that now pins it.

- **RESTCONF RPC/action request bodies were not module-qualified.**
  *Symptom:* `ops.my_rpc({...})` sent `{"input": {...}}`; every strict server
  rejects it, and the client read its own node back as unknown data.
  *Root cause:* the RPC envelope's `input` field was emitted without an
  alias, so `model_dump(by_alias=True)` produced a bare `input` key. The
  data-write path already carried the correct `_envelope_name` mechanism; the
  RPC branch simply never used it.
  *Fix:* `restconf/data_models/models.py.jinja` now emits
  `input: X | None = Field(default=None, alias="<module>:input")` and the
  navigator dumps with `exclude_none=True`, sending **no body at all** when
  there is nothing to send. The URI (`POST .../operations/<module>:<rpc>`) and
  the reply unwrapping (`<module>:output`) were already correct and are
  unchanged.
  *Normative:* RFC 8040 §3.6, §3.6.1, §3.6.2; RFC 7951 §4. The member name is
  the fixed `input`/`output` qualified by the defining module — the rpc
  identifier appears **only** in the request URI. `RFCs/8040.md` did not
  document the operation body at all, which is why this survived; it now does.
  *Tests:* `test_restconf_rpc_request_body_is_module_qualified`,
  `test_restconf_rpc_without_input_sends_no_body`,
  `test_restconf_rpc_output_is_unwrapped_from_the_module`,
  `test_restconf_action_uri_and_body`.

- **Every NETCONF RPC/action input parameter was silently deleted from the
  write payload.** *Symptom:* the device received an RPC with no parameters
  (`<reboot xmlns="..."/>`) regardless of what the caller passed.
  *Root cause:* pyang's `_keywords_with_no_explicit_config` is
  `['action','rpc','notification']`, so for a node inside an rpc/action
  `i_config` **exists but is `None`** — "config-ness is not defined here".
  `_build_field` read it with a `True` default, which silently yields `None`,
  and `_strip_nonconfig_tree` tested `extra.get("is_config", True)` for
  truth. `None` is falsy, so the node was classified `config false` and
  removed from the serialized payload.
  *Fix:* normalise the undefined case to `True` in the IR (an rpc input is
  writable data), and harden `_strip_nonconfig_tree` and the RESTCONF
  `_prune_content` so that only an **explicit** `False` means state data.
  *Tests:* `test_netconf_rpc_input_is_not_stripped_and_output_parses` (asserts
  `is_config is True` on every rpc-input field *and* that the parameter
  survives serialization).

- **NETCONF RPC `Input`/`Output` models had the root tag `"None"`.**
  *Symptom:* `ParsingError: root element not found (actual: output, expected:
  {urn:...}None)` for every reply.
  *Root cause:* `_build_rpc` resolved `input`/`output` with
  `search_one()`, which returns the **un-expanded** substatement whose `.arg`
  is `None`. pyang copies the node into `i_children` during the expand phase
  and sets `arg` to the keyword there; the model was built from the wrong
  node.
  *Fix:* new `IRBuilder._expanded_io()` prefers pyang's expanded copy from
  `i_children` and only falls back to `search_one`. This also matters for
  nested YANG 1.1 `action`s, whose `input` has no `i_children` at all on the
  un-expanded statement.
  *Tests:* `test_netconf_rpc_input_is_not_stripped_and_output_parses` asserts
  `__xml_tag__ == "input"` / `"output"`.

- **YANG 1.1 `action` statements never produced their models.** *Symptom:*
  invoking any action raised `ImportError: cannot import name 'ResetInput'`
  before a single byte was sent, on **both** protocols.
  *Root cause:* `IRBuilder.build()` only called `_build_rpc` for `rpc`
  statements that were direct children of the module, while
  `_build_nav_nodes` happily emitted a navigator for every nested `action`
  that imported `<Action>`, `<Action>Input` and `<Action>Output`.
  *Fix:* a new `_build_actions()` pass walks the tree and builds the
  Input/Output envelope models for every `action`. Actions are deliberately
  **not** added to `root_rpc_props`: RFC 8040 §3.6 keeps them out of
  `{+restconf}/operations` because they are invoked through the data tree.
  *Tests:* `test_restconf_action_uri_and_body`,
  `test_netconf_action_encodes_the_datastore_hierarchy`.

- **NETCONF action replies could not be parsed.** *Root cause:* the generated
  code wrapped the `<rpc-reply>` children in an **unqualified** `output`
  element (`etree.Element("output", nsmap=...)` declares a default namespace
  but does *not* put the element in it), while the model's root element is
  namespaced (`{ns}output`).
  *Fix:* build the wrapper as `etree.Element("{" + ns + "}output")`.
  *Normative:* RFC 7950 §7.15.2 — output parameters are encoded as child
  elements of `<rpc-reply>` with no wrapper; the wrapper exists only to
  satisfy pydantic-xml's root-element check, so it must be namespaced.
  *Tests:* `test_netconf_rpc_input_is_not_stripped_and_output_parses`.

- **NETCONF rejected dicts keyed by the YANG wire name, RESTCONF accepted
  them** (protocol-parity break). *Symptom:* the identical dict
  `{"delay-seconds": 5}` worked on RESTCONF and raised `extra_forbidden` on
  NETCONF, so every NETCONF `update(data=dict)`, `create([dict])` and
  `rpc(dict)` call was affected for any hyphenated leaf.
  *Root cause:* pydantic-xml binds an element to a field by its `tag`, not by
  the Python attribute name, so the NETCONF models carry no `alias`; RESTCONF
  models carry `Field(alias=...)` and do accept the wire name.
  *Fix:* a `mode="before"` model validator on `NetconfBaseModel` maps the
  recorded `tag` back to the field name (and `operation` → `nc_operation`).
  `extra="forbid"` still rejects genuinely unknown nodes.
  *Tests:* `test_netconf_models_accept_yang_wire_names_in_dicts`.

- **An rpc-only module generated an unimportable client.** *Symptom:*
  `IndentationError: expected an indented block after class definition` — a
  module with RPCs but no top-level data nodes emitted an empty `Data` class
  body.
  *Root cause:* the aggregate `Data` navigator had no `pass` fallback (the
  existing regression test only covered the rpc-*free* direction, where the
  `Operations` class was the one at risk). RESTCONF was saved by an incidental
  docstring; NETCONF was not.
  *Fix:* `pass` fallback in both protocols' `data_navigators/__init__.py.jinja`.
  *Tests:* `test_rpc_only_module_compiles_on_both_protocols`.

- **RESTCONF URIs were not module-qualified for cross-module nodes.**
  *Symptom:* an augmented container was addressed at a path segment that does
  not exist on the device.
  *Root cause:* RFC 8040 §3.5.3 requires a module prefix when a node comes from
  a module other than its parent, but every nested navigator used the bare
  YANG name. Only the top-level `Data` properties were qualified.
  *Fix:* `IRNavProperty.path_segment` is computed in the IR from the defining
  and parent module names, and the RESTCONF template uses it — one rule in one
  place rather than a name convention duplicated in the template.
  *Tests:* `test_restconf_uri_qualifies_cross_module_path_segments` (asserts
  top-level qualified, same-module nested bare, augmented nested qualified).

- **`type empty` was modelled as `bool`.** *Symptom:* RESTCONF put
  `true`/`false` on the wire for a leaf that carries no value, and NETCONF
  serialized `<flag>false</flag>`, which a conformant server rejects.
  *Root cause:* the IR mapped `empty` to `bool` in the same branch as
  `boolean`.
  *Fix:* a dedicated `Empty` alias per protocol — RESTCONF
  `list[None]` serialized to `[null]` (RFC 7951 §6.9), NETCONF a `str` that
  renders as an empty element (RFC 7950 §9.11). Both accept `None`/`True` on
  input for ergonomics.
  *Tests:* `test_type_empty_is_not_a_boolean`.

- **A YANG `description` could produce an unimportable client.** *Symptom:* the
  generator printed "Generated SDK in: …" and shipped a file Python could not
  import.
  *Root cause:* two independent gaps. `_escape_docstring` replaced a triple
  quote with an **identical** triple quote (the replacement was a raw string
  equal to the search text), so the escaping was a no-op; and nothing ever
  compiled the output, so the defect was invisible until a consumer imported
  the package.
  *Fix:* split escaping by context — `_docstring()` escapes backslashes and
  quote runs for `"""…"""` interpolation, `_clean_text()` leaves the text
  unescaped for the Pydantic `description=` field, which is emitted through
  `repr()` and was being double-escaped. Plus a new `core._validate_generated()`
  gate that compiles every emitted `.py` and fails the build with the offending
  file and line, so no future escaping bug can ship.
  *Tests:* `test_hostile_yang_description_still_yields_importable_python`,
  `test_generator_refuses_to_ship_unparseable_python`.

- **Disjoint YANG ranges silently lost a bound.** *Symptom:* `range "1..5|7"`
  generated `ge=1` with no `le`, so the model accepted values the device
  rejects.
  *Root cause:* only `parts[0]` and `parts[-1]` were inspected, and a bare
  single value in either position has no `..` to split on. `0|3..5` lost `ge=0`
  the same way.
  *Fix:* `_get_range_constraints` now scans every alternative and takes the
  smallest lower and largest upper bound, per RFC 7950 §9.2.2.
  *Tests:* `test_disjoint_yang_ranges_keep_both_bounds`.

- **`replace()` silently deleted configuration.** *Symptom:*
  `nav.replace({"description": "x"})` issued a PUT (or
  `nc:operation="replace"`) whose body contained only that one field, wiping
  every other leaf on the node.
  *Root cause:* write payloads are dumped with `exclude_unset=True`, and a
  replace was then sent as-is. A PUT body must represent the *complete*
  resource, so absence means deletion — the two combine into silent data loss.
  *Fix:* `_require_complete_for_replace()` refuses and names the fields that
  would be deleted, pointing at `update()` or a full-depth retrieve. Pass
  `allow_partial=True` to accept it deliberately. A retrieve→modify→replace
  round-trip is unaffected because every returned field is set, and a
  depth-truncated read now fails loudly instead of truncating the device.
  *Normative:* RFC 8040 §4.5; RFC 6241 §8.2.1.
  *Tests:* `test_replace_refuses_to_silently_delete_unset_fields` (both
  protocols, including that nothing is sent when the guard trips).

- **RESTCONF read pruning ignored the requested depth.** *Symptom:* the depth a
  caller asked for had no effect; pruning depended only on the shape of the
  response, and a legitimately empty container at the deepest returned level
  was deleted from the caller's data.
  *Root cause:* the pruner was passed `_get_max_depth(payload)` — the observed
  depth — instead of the requested one.
  *Fix:* prune at `min(requested, observed)`, and prune nothing at all for
  `depth="unbounded"` (the RFC 8040 §4.8.2 default), which now genuinely means
  unbounded.
  *Tests:* `test_read_pruning_is_bounded_by_the_requested_depth`.

- **`_redact` missed `passphrase`, and the raised `HTTPError` carried the whole
  response body.** *Symptom:* NTP/TACACS/RADIUS authenticator passphrases
  reached the log in cleartext under `log_bodies=True`, and every HTTP error
  surfaced the full device payload inside the exception message that callers
  log.
  *Root cause:* `"password" in "passphrase"` is False, and so is
  `"passwd" in "passphrase"`; and the hygiene rule was applied to the log line
  but not to the exception.
  *Fix:* widened `_SENSITIVE_KEYS` (adding `passphrase`, `psk`, `pre-shared`,
  `credential`, `md5`, `username`, …) in both templates, and the raised error
  now carries the status plus the YANG `error-tag` (RFC 8040 §7) with the body
  kept behind the `log_bodies` opt-in.
  *Tests:* `test_logging_redacts_passphrase_and_never_leaks_the_error_body`.

- **The NETCONF client never released its session.** *Symptom:* every
  `NetconfClient()` opened an SSH transport and a NETCONF session that only
  garbage collection closed, so a sweep across many devices leaks sessions
  until the device refuses new ones.
  *Root cause:* no `close()` and no `__del__`; the context manager unlocked but
  never closed, and its unguarded `unlock()` could mask the original exception.
  *Fix:* `close()` calling `close_session()`, idempotent and safe to call from
  `__del__`; the context manager closes on the way out and guards every cleanup
  step so a failure there can never hide the real error; a `_mc` accessor turns
  use-after-close into an explicit `RuntimeError`.
  *Tests:* `test_netconf_releases_its_session`.

- **`auto_commit` defaulted to True, and there was no `validate()`.**
  *Symptom:* every edit committed the candidate immediately, so a multi-step
  change could not be reviewed, validated or rolled back — and the safe
  validate-then-commit sequence described in `AGENTS.md` could not be
  expressed at all, because the client had no `<validate>`.
  *Root cause:* `auto_commit: bool = True`; `<validate>`/`<validate-source>`
  were never implemented.
  *Fix:* `auto_commit` is now opt-in and warns when enabled; added
  `has_validate()` and `validate()` implementing RFC 6241 §8.3.5.1 `<validate>`
  and §8.3.5.2 `<validate-source>`, refusing cleanly when the capability is
  absent. **Behaviour change:** code that relied on the implicit commit must
  now call `commit()`.
  *Tests:* `test_netconf_auto_commit_is_opt_in`,
  `test_netconf_exposes_the_validate_workflow`.

- **Codegen was not byte-reproducible.** *Symptom:* `diff -r` between two
  compiles of the same YANG was never clean, so no generated output could ever
  be committed as a golden fixture.
  *Root cause:* a wall-clock `created_utc` was written into `README.md` and
  `MANIFEST.yang-revisions.json`.
  *Fix:* the stamp is now opt-in via `SOURCE_DATE_EPOCH` and omitted
  otherwise, so the default build is byte-identical run to run.
  *Tests:* `test_generated_output_is_byte_reproducible`.

The following five were found only by running the generated SDKs against the
real lab targets (SR Linux 25.10.1, Groove G30) rather than the simulator, and
none of them were reachable from the existing suite.

- **NMDA was detected by a substring match, which broke 100% of reads on SR
  Linux.** *Symptom:* every datastore read failed with
  `unknown-element` / `Error [InvalidArgument]: Unknown element`.
  *Root cause:* `has_nmda = any("ietf-netconf-nmda" in cap ...)`. SR Linux
  advertises `.../yang:ietf-netconf-nmda?module=ietf-netconf-nmda&...` as a
  **module** capability but does **not** advertise
  `urn:ietf:params:netconf:capability:nmda:1.0`. The substring matched, so the
  client routed every read to a `<get-data>` the device does not implement.
  Any device that ships the module without implementing NMDA is affected, and
  the simulator matrix cannot see it because notconf does advertise the real
  capability.
  *Fix:* `_has_capability()` compares the capability base URI (before `?`)
  exactly, and is now used for `:candidate`, `:writable-running`, `:nmda` and
  `:validate`. `has_confirmed_commit` and `has_rollback_on_error` were added
  while there. The resolved path is logged at INFO so a wrong route is visible
  in a log, not only in a stack trace.
  *Normative:* RFC 8526 §3.1.1; RFC 6241 §8.1.
  *Tests:* `test_nmda_is_detected_from_the_capability_uri_not_a_module_name`
  (uses SR Linux's real capability list as the negative case).

- **A YANG leaf named after a Pydantic attribute was unreachable.**
  *Symptom:* `ietf-netconf-monitoring`'s `schema` leaf produced a field pydantic
  warned "shadows an attribute in parent", which resolved to the inherited
  `BaseModel.schema()` **method** — reading it raised
  `TypeError: object of type 'method' has no len()` on a live device.
  *Root cause:* `_to_field_name` guarded Python keywords but not
  `BaseModel` / `BaseXmlModel` attribute names. `json`, `dict`, `copy`,
  `construct` and `validate` are affected identically, and they are ordinary
  YANG leaf names.
  *Fix:* the reserved set is computed from the real classes at import (so it
  cannot drift with the pinned Pydantic version) and a colliding field is
  suffixed with `_`. The wire name is untouched — NETCONF keeps
  `element(tag="schema")` and RESTCONF `Field(alias="schema")` — and the
  NETCONF wire-name validator maps the tag back, so dict input keeps working.
  *Tests:* `test_fields_never_shadow_a_pydantic_attribute`.

- **The NETCONF error path destroyed the device's error message.** *Symptom:*
  any RPC the device rejected surfaced
  `AttributeError: 'RPCError' object has no attribute '_tag'` instead of the
  reason.
  *Root cause:* ncclient 0.7 exposes `RPCError.tag`/`type`/`severity` as
  properties over `_tag`/`_type`/`_severity`, which are only populated when
  ncclient parsed structured `<rpc-error>` elements. It also raises `RPCError`
  with a **plain-string** errlist for a libyang `InvalidArgument`, and then
  `_tag` does not exist — so the handler crashed while formatting. A real SRL
  rejection ("Must match the pattern …") was reduced to an `AttributeError`.
  *Fix:* `_rpcerror_attr()` / `_rpcerror_text()` read whatever is available and
  never raise; the message falls back to `errlist` and then to the raw
  `<rpc-error>` XML. Transport-level `OSError`/`ValueError` are re-raised
  unchanged rather than being swallowed.
  *Tests:* covered by the live SR Linux write round-trip; a mocked case is
  worth adding.

- **`<validate-source>` does not exist in RFC 6241.** *Symptom:* the
  `validate()` added for the safe transaction sequence answered
  `unknown-element` on every device.
  *Root cause:* the implementation invented a `<validate-source>` RPC. RFC 6241
  §8.6.4.1 defines `<validate>` with an **optional `<source>` wrapper**:
  `<validate><source><candidate/></source></validate>`. The `:validate:1.1`
  capability instead extends `<edit-config>` to validate an inline `<config>`
  (§8.6.5.1).
  *Fix:* corrected to the RFC encoding.
  *Tests:* `test_netconf_exposes_the_validate_workflow` (asserts the exact
  element shape and that `validate-source` never appears).

- **An undeclared XML attribute on a data node made every G30 read fail.**
  *Symptom:* `ValidationError: @cli-name: Extra inputs are not permitted` on
  every subtree read.
  *Root cause:* the G30 emits `cli-name="…"` on every data element with no YANG
  declaration anywhere (it is declared as an *extension* in
  `coriant-cli-extensions` but never used). pydantic-xml reports unbound
  attributes through the same `extra="forbid"` hook as unknown elements —
  prefixing their location with `@` — so an undeclared attribute was
  indistinguishable from an unmodelled data node.
  *Fix:* unknown **attributes** are dropped before parsing, because RFC 7952
  §4.1 states an attribute "MUST NOT be used to represent a YANG leaf value" —
  they carry metadata, so dropping one cannot lose data. Unknown **elements**
  stay fatal, which is the model-gap safety net `--check-model-gaps` exists for.
  Gated by `NetconfXmlModel.ignore_unbound_attributes` (a `ClassVar`, default
  `True`) so a strict adapter can set it `False`. This is a property of YANG
  XML encoding, not a vendor quirk baked into the generic path: the only
  attribute a generated model ever binds is `nc:operation`, and the helper says
  so.
  *Normative:* RFC 7952 §4.1.
  *Tests:* the live G30 read suite; a mocked case is worth adding.

### Added

- Offline regression coverage for the RPC/action path, which previously had
  none. `tests/test_matrix.py` now compiles a module with an `rpc` that has
  input+output, an `rpc` with no input, and a nested YANG 1.1 `action`, then
  asserts the exact wire shape on both transports against a recording
  stand-in client — no device and no network required.

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

- RESTCONF write shapes corrected to strict RFC 8040 + RFC 7951 (primary
  sources, not tests): item PUT/PATCH single-element arrays per §4.5 jukebox
  album example + RFC 7951 §5.4, create POST to parent per §4.4.1 + App. B.2.1
  (exactly one instance). Removed false deviation claims citing non-existent
  §4.6.2, §5.2-object, and §6.3-list; rousette `tests/restconf-plain-patch.cpp`
  (204) confirms the array form. Whole-list `replace` now PUTs the list
  resource itself (prior parent-target list-keyed body was malformed, hence
  LY_EVALID). Regression tests in `tests/test_matrix.py`
  (`test_restconf_write_bodies_are_module_qualified`,
  `test_no_nonexistent_rfc_sections_cited`, `test_rfc8040_jukebox_array_shape`).
- RESTCONF write envelopes: nested list/container nodes now send
  module-qualified top-level body members (RFC 7951 §4) via a navigator
  `_envelope_name` (write key) separate from the response key; response
  matching keeps the simple name. Regression test in `tests/test_matrix.py`
  (`test_restconf_write_bodies_are_module_qualified`).
- RESTCONF `create` sends one POST per new entry; the lab backend rejects
  multi-entry create bodies ("MUST contain exactly one instance",
  RFC 8040 §4.4.1).
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
- NETCONF secure default: generated `NetconfClient(verify=True)` now verifies
  host keys (RFC 6242); `verify=False` is an explicit lab-only opt-out with a
  logged warning (was silent `False` default). Deprecated `hostkey_verify`
  alias kept with `DeprecationWarning`. Both clients share swappable core
  kwargs (`loopback_ip/management_ip/port/username/password/verify/log_bodies/
  timeout` + `**kwargs`) and unified `DEVICE_USER(NAME)`/`DEVICE_PASS(WORD)`
  env fallback. Regression test `test_netconf_template_keeps_hostkey_default`
  + `test_clients_swappable_signature`.
- Logging hygiene: generated RESTCONF/NETCONF clients log method/URL/status
  at INFO only; bodies go to DEBUG behind structured `log_bodies=False`
  opt-in with redaction + truncation (`_redact`), never credentials. Enabling
  `log_bodies=True` logs a warning. Contract tests
  `test_restconf_logging_hygiene_contract` + `test_logging_hygiene_generated_client`.
- Registry-ready packaging: generated clients now emit `pyproject.toml`
  (hatchling, protocol deps), `README.md` (device/OS/protocol/generator/
  module revisions), `MANIFEST.yang-revisions.json` (revisions, deviations,
  features, UTC), `py.typed`, plus namespaced installable copy
  `<out>/<package>/` (`import <package>`). Verified with `uv build` +
  clean install + import for both protocols. New CLI flags
  `--device-version/--package-version/--deviation-module/--feature`
  (pyang `--deviation-module` passthrough, recorded in MANIFEST).
  IR captures `revision`; `core.py` sanitizes `<device>_<os>` names and
  PEP440 versions. Tests `test_generated_package_is_registry_ready` +
  `test_package_manifest_records_deviations_and_features`.

### Changed

- Lab tooling quarantined behind the `lab` extra
  (`pip install yang2sdk[lab]`, dev: `uv sync --locked --extra lab`):
  `lxml`, `ncclient`, `python-dotenv` moved out of default dependencies.
  `yang2sdk.cli.compiler` (production path) no longer loads `.env`
  (`$DEVICE_NAME` fallback still works via plain environment).
  `downloader`/`sdk-verify` fail fast with a `[lab]` hint when the extra is
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

### Fixed (round 2 — found by running the generated SDK against the notconf matrix)

The NETCONF leg of `tests/test_sdk_generate.py` had never actually executed
successfully. Bringing it up surfaced five real defects: three in the
generator/client, and three test-harness bugs that had been masking them. Each
is listed with symptom, root cause, citation, fix, and the test that pins it.

- **A YANG list key named after a Python keyword made the whole client
  unimportable.**
  *Symptom:* generation of Cisco NX-OS (`notconf-cisco-nx:10.4-4`) aborted with
  `data_navigators/Cisco_NX_OS_device.py:94971: invalid syntax`. The file is
  25 MB of generated navigators, so this was a total generation failure for
  that device, not a single bad symbol.
  *Root cause:* the NETCONF navigator template emitted each list key as a
  Python parameter of `__call__` with only `k.replace('-', '_')`. NX-OS
  ships a list keyed on the leaf `if`, yielding
  `def __call__(self, if: str | int, addr: str | int)` — a `SyntaxError`.
  `IRBuilder._to_field_name` already handled keywords for *model* fields, but
  the navigator's key signature bypassed it entirely.
  *Fix:* added `IRNavNode.key_params`, a list of `(python_name, yang_name)`
  pairs built through `_to_field_name`, and switched the template to it. The
  Python parameter is now `if_` while the wire key stays `if`. RESTCONF was
  never affected because it addresses keys positionally via
  `__call__(*keys)`; the test asserts that asymmetry explicitly.
  *Also fixed here:* the same `k.replace('-', '_')` would have mangled a legal
  YANG name that contains a literal underscore, so both protocols' identifier
  derivation now go through the one keyword-safe implementation.
  *Pinned by:* `test_list_key_named_like_python_keyword` (both protocols).

- **The `replace()` guard counted `config false` state leaves as deletions and
  blocked every legitimate partial-data replace.**
  *Symptom:* 10 live `ietf-interfaces` round-trips failed with
  `ValueError: replace() on InterfacesInterfaceItem would DELETE 13 field(s)
  that are not set: admin_status, if_index, oper_status, ...` — every one of
  the listed fields is operational **state**, not configuration.
  *Root cause:* the guard tested only `name not in model_fields_set`. A
  partially-read interface has most of its *state* leaves unset, and state
  never appears in a config body and cannot be deleted by an edit, so the
  guard fired on data that was never at risk.
  *Fix:* the guard now skips fields whose
  `json_schema_extra["is_config"]` is `False` (and, for NETCONF,
  `nc_operation`, which is an attribute rather than a data node). Config
  nodes are still refused loudly, which is the behaviour that matters.
  *Pinned by:* `test_replace_guard_ignores_state_fields`.

- **The generated NETCONF client could not be constructed by its own test
  suite, so the NETCONF SDK layer was never exercised in CI.**
  *Symptom:* all 3 NETCONF tests failed on every image with
  `SSHUnknownHostError: Unknown host key [5b:d7:...] for [127.0.0.1]`, i.e.
  the transport never opened.
  *Root cause:* the tests build the client through the generated
  `NetconfClient(...)` with no `verify` argument, so the secure default
  `verify=True` applied and paramiko rejected the simulator's per-container
  random host key. `tests/test_notconf_protocol.py` already opted out for its
  raw-ncclient path; the generated-client path did not.
  *Fix:* the 4 generated-client constructions now pass `verify=False`
  explicitly, with a comment recording that a real deployment must keep
  `verify=True`. No product default was weakened — this only fixed the
  harness.

- **NETCONF write tests asserted against a datastore nothing was written to.**
  *Symptom:* `test_netconf_sdk_crud_roundtrip` failed at `created entry not
  visible after edit-config` and `test_netconf_sdk_update_roundtrip` at
  `hostname still unset after baseline write`, on every candidate-capable
  image.
  *Root cause:* the NETCONF client defaults `default_target` to `candidate`
  whenever the device advertises it, and `auto_commit` now defaults to
  `False` (the deliberate safety fix — see the `auto_commit` entry above).
  The tests were written when `auto_commit` defaulted to `True`, so every
  write auto-committed; after the safety fix, the edit landed in `candidate`
  and the read of `running` correctly found nothing. The tests had been
  passing by accident, and the candidate datastore was never exercised
  explicitly.
  *Fix:* added `_commit_candidate()`, which runs the sequence `AGENTS.md`
  mandates — edit → `validate` → `commit` (RFC 6241 §8.6.4.1) — and skips
  `validate` when the device does not advertise `:validate`, because
  `<validate>` is optional (RFC 6241 §8.6.4.1) and the client already warns
  and returns `False` in that case.

- **The "every top-level node merged cleanly" test could not fail usefully, and
  then failed for the wrong reason.**
  *Symptom:* two successive shapes of this test: (a) swallowed every node
  error, so the suite stayed green while the device rejected all writes;
  (b) after the first fix, 7 hard failures of the form
  `top-level nodes failed: ietf_interfaces_interfaces_state: HTTP 404`.
  *Root cause:* (a) ignored the return value of the merge. (b) conflated two
  unrelated things: a node the backend cannot *read* at all — a documented
  notconf quirk, since state containers answer 404/400 — with a node the
  backend read and then *rejected a write to*. The first is a skip; only the
  second is a defect.
  *Fix:* split the tracking into `_NODE_READ_SKIPS` (tolerated, reported) and
  `_NODE_WRITE_FAILURES` (asserted empty), mirroring the assertion the NETCONF
  twin already made. An image that exposes no readable config at all
  (`notconf:latest`, the minimal base image) now `pytest.skip`s explicitly,
  because claiming "all nodes merged" over zero nodes is a vacuous pass.

- **The NETCONF CRUD test replaced a fresh minimal payload, which the guard
  correctly refused.**
  *Symptom:* `replace()` on `InterfacesInterfaceItem` reported it "would
  DELETE 4 field(s) that are not set: bind_ni_name, ipv4, ipv6,
  link_up_down_trap_enable".
  *Root cause:* the test rebuilt a minimal payload for the replace step. That
  payload is genuinely incomplete, and per RFC 6241 §8.2.1 an
  `operation="replace"` edit does overwrite the whole node, so omitting those
  subtrees really would delete them. The simulator additionally never returns
  those four leaves, so a complete body is unconstructable there.
  *Fix:* the live test now asserts the property that actually has value — a
  partial replace is refused **loudly** rather than silently deleting the
  subtrees — and then exercises the wire path via the explicit
  `allow_partial=True` opt-in. The guard's config-vs-state split is pinned
  offline.

- **The RESTCONF CRUD test repeated the same stale partial `replace()`.**
  *Symptom:* `test_restconf_sdk_crud_roundtrip` failed with
  `ValueError: replace() on InterfacesInterfaceItem would DELETE 4 field(s)
  that are not set: bind_ni_name, ipv4, ipv6, link_up_down_trap_enable`.
  *Root cause:* identical to the NETCONF twin above — the test built a fresh
  minimal payload for the replace step, which is genuinely incomplete and,
  per RFC 8040 §4.5, a PUT body is the complete resource.
  *Fix:* same treatment in both protocol twins: assert the loud refusal, then
  exercise the wire path via the explicit `allow_partial=True` opt-in. Both
  twins now assert the same safety property, which is what makes RESTCONF and
  NETCONF behaviourally symmetric here.

- **`decimal64` did not enforce `fraction-digits`, so an invalid value only
  failed at the device.**
  *Symptom:* `C.coarse` typed as `decimal64 { fraction-digits 2; }` accepted
  `Decimal("1.234567")` client-side, and the write was rejected by the device.
  *Correction to an earlier claim:* I had recorded that the RESTCONF
  `decimal64` serializer "may be lossy". That was wrong and is retracted —
  `format_at_least_two_places` only ever *pads* the scale (it never truncates),
  and every value round-trips exactly, including `1.5 -> "1.50"`,
  `100 -> "100.00"`, and high-precision values. Verified directly before
  changing anything.
  *Root cause:* the real gap was one level up, in validation:
  `_get_range_constraints` extracts `ge`/`le` but nothing read
  `fraction-digits`, so the scale constraint (RFC 7950 §9.3.2) was never
  modelled.
  *Fix:* the IR now carries `decimal_places` in the constraint map, which both
  protocol templates already render generically, so RESTCONF emits
  `Field(..., decimal_places=2)` and NETCONF `element(..., decimal_places=2)`.
  A value that violates the scale is now rejected locally with pydantic's
  `decimal_max_places` instead of round-tripping to the device.
  *Pinned by:* `test_decimal64_fraction_digits_enforced_locally` (both
  protocols, including that a `fraction-digits 6` sibling still accepts the
  value its stricter sibling rejects).

- **The generator used the OLDEST revision of every multi-revision module —
  the largest fidelity bug found in this audit.**
  *Symptom:* `test_restconf_all_config_nodes_retrieve_update` on
  `notconf-cisco-xr:2531` failed with
  `ValidationError: 2 validation errors for Cef: platform, platform-load-balance
  — Extra inputs are not permitted`. The device served `cef/platform/...`; the
  model described `cef/load-balance/...` with no `platform` at all, so every
  read of that node failed locally.
  *Root cause:* RFC 6022 §3.1.2 lets a server advertise several revisions of
  one module, and it encodes/returns data using the *newest* one unless the
  client asks for another. The downloader faithfully saved every advertised
  revision into one directory (`<name>@<rev>.yang`) and left the choice to
  pyang, which resolved the ambiguity by taking the oldest. Cisco IOS-XR
  advertises multiple revisions for **1199 of its modules**; after this fix,
  cleaning a real device's library removed 1800 stale schema files, so the
  majority of the generated model had described schemas the device was not
  using. Because the models are strict (`extra="forbid"`), this surfaced as a
  hard read failure rather than a silent mismatch — which is the one saving
  grace of `extra="forbid"`.
  *Fix:* the downloader now groups `<schemas>` entries by module identifier,
  keeps only the newest revision of each, says how many modules were affected,
  and fetches only those. Ordering uses a new `_revision_key` that parses the
  `YYYY-MM-DD[:HH:MM]` form from RFC 7950 §4.1, sorts an unparseable revision
  below every real one, and is total and deterministic.
  *Verified:* with the stale revisions removed, `notconf-cisco-xr:2531` goes
  from 1 failed to `5 passed, 2 skipped`.
  *Pinned by:* `test_revision_ordering_picks_newest_yang_revision`.

- **The SDK test suite exercised a *copy* of the downloader, not the
  downloader.**
  *Symptom:* after fixing the revision selection in `cli/downloader.py`,
  `notconf-cisco-xr:2531` still failed with the same `Cef: platform` error.
  The production fix was correct and simply was not being run.
  *Root cause:* `tests/test_sdk_generate.py::_download_yangs` was a
  "mirror of `YangDownloader.download_all`" pasted into the test file, and it
  had diverged — it deduped by *first-seen* identifier, keeping the oldest
  revision, while the shipped downloader kept all of them. Either way the test
  asserted against a code path no user ever runs, and a regression in the real
  downloader would have been invisible.
  *Fix:* the test now imports and calls the shipped `YangDownloader`, and
  asserts that no schema fetch failed. It also clears `temp/yang_modules/notconf`
  first, so a stale revision from an earlier run cannot be resolved by pyang and
  mask a revision-selection regression. Net effect: the integration suite
  exercises the download path users actually invoke.
  *Verified:* `notconf-cisco-xr:2531` is now `5 passed, 2 skipped`, and the
  downloaded library contains `0` modules with more than one revision.

- **`replace()` crashed with an opaque `AttributeError` when given a dict.**
  *Symptom:* `client.data.<list>(key).replace({"name": "lo100"})` raised
  `AttributeError: 'dict' object has no attribute 'model_fields'` from inside
  the guard. Found by re-verifying SR Linux after the guard was added.
  *Root cause:* the NETCONF navigator calls `_require_complete_for_replace(data,
  allow_partial)` and only *afterwards* delegates to `update()`, which is where
  a dict/XML/JSON string is coerced into a model. The guard therefore inspected
  the raw input: a bad error, and — more importantly — a silent bypass of a
  safety check for the most convenient input form. RESTCONF was unaffected
  because it already coerces before calling the guard.
  *Fix:* the guard takes the model class and coerces dict/XML/JSON input with
  the same rules `update()` uses before inspecting fields, so the check always
  sees a model. Both templates pass the class at every call site.
  *Pinned by:* `test_replace_guard_accepts_dict_input` (NETCONF).

- **`has_validate` was a method while its five sibling capability flags were
  attributes.**
  *Symptom:* `c.has_nmda()` raised `TypeError: 'bool' object is not callable`,
  and the converse trap existed too: `if client.has_validate:` is **always
  true** because a bound method is truthy, so validation would be silently
  skipped on a device that does not implement it.
  *Root cause:* `has_validate` was defined as a method that recomputed the
  capability on demand, while `has_candidate`, `has_writable_running`,
  `has_nmda`, `has_confirmed_commit` and `has_rollback_on_error` are plain
  booleans assigned in `__init__`.
  *Fix:* all six are now plain `bool` attributes, consistent with each other and
  with `auto_commit`. Recorded in `AGENTS.md` so a future change does not
  reintroduce a lone method among attributes.

- **`RestconfClient` had no `close()` at all — a protocol-parity break.**
  *Symptom:* `AttributeError: 'RestconfClient' object has no attribute 'close'`
  while `NetconfClient.close()` existed. Found by the G30 re-verification.
  *Root cause:* the NETCONF session manager gained explicit teardown this
  release; the RESTCONF one never did, so a generated client holding a
  `requests.Session` had no way to release its keep-alive sockets and TLS
  session, and a use-after-close produced `AttributeError` on `None` rather
  than a clear error.
  *Fix:* `close()` (idempotent), `__enter__`/`__exit__`, `__del__`, and a
  `RuntimeError("RESTCONF session is closed; construct a new RestconfClient.")`
  guard in `_request`, matching the NETCONF client exactly. AGENTS.md requires
  the public API to stay symmetrical, and a lifecycle method missing from one
  protocol is exactly that kind of divergence.
  *Pinned by:* `test_restconf_client_close_is_symmetric_with_netconf`.

Two further observations are recorded as *not* product defects, with evidence:

- Raw `<get>`/`<get-config>` on `notconf-ietf` returns an empty payload for
  every filter, including no filter at all, while `edit-config` returns
  `<ok/>`. The write is accepted and the read-back is empty, so the round-trip
  failure is a backend limitation, not a client fault. Confirmed with raw
  ncclient independently of the generated client.
- An xpath-filtered `<get>` times out on the simulator
  (`SHM event "oper get" ... processing timed out`). The generated client uses
  a **subtree** filter (RFC 6241 §6.4.3), which the simulator accepts, so the
  SDK is not on the wrong path.

### Planned (not in this change)

- Flatten `yang2sdk.plugin.src.*` → `yang2sdk.plugin.*` (cosmetic; deferred
  behind a regeneration regression run — touches pyang `--plugindir`
  resolution).
- Dependency pins (`uv lock`) for generated clients; field-level `deviated`
  flags (MANIFEST-level provenance shipped).
