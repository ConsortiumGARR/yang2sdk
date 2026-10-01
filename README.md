# yang2sdk

Generate a Pydantic-based IDE-friendly SDK for your network devices directly from YANG modules.

## Overview

This pipeline extracts the YANG modules directly from your network devices and transforms them into a type-safe RESTCONF or NETCONF SDK interface to your device.

Then you can do stuff like this to update the description of a port:

```python
from device_name_1_4_0 import RestconfClient as DeviceNameClient

client = DeviceNameClient(
    management_ip="192.168.137.42",
    port=443,
    username="user",
    password="pass",
    verify=True,
)

# Top-level data properties are named "<yang_module>_<node>", so an
# `ietf-interfaces:interfaces/interface` becomes `ietf_interfaces_interface`.
# Nested navigators then follow the YANG structure, and a list is called with
# its key(s) -- a composite key takes one argument per leaf, in `key` order.
interfaces = client.data.ietf_interfaces_interface("ethernet-1/1")

# Retrieve current config
port1 = interfaces.retrieve(content="config", depth=2)

# The retrieved config (JSON) is loaded into the corresponding Pydantic model
# that you can modify. As soon as you type `port1.` the IDE shows every field.
port1.service_label = "test137"

port1.admin_status = "dowm"
# Here the code fails immediately, raising the following error:
# pydantic_core._pydantic_core.ValidationError: 1 validation error for InterfaceItem
# admin_status
#   Input should be 'up' or 'down'  [type=enum, input_value='dowm', input_type=str]

# Merge the change (RESTCONF PATCH / NETCONF nc:operation="merge")
interfaces.update(port1)
```

`update()` is the merge verb and the one you normally want. `replace()` is a
PUT / `nc:operation="replace"`, which means the body **is** the whole resource
(RFC 8040 §4.5, RFC 6241 §8.2.1): anything missing from it is deleted on the
device. Because the example above reads at `depth=2`, the returned model has
config fields the device never sent, so a `replace()` with it is refused:

```python
interfaces.replace(port1)
# ValueError: replace() on InterfaceItem would DELETE 4 field(s) that are not
# set, because a PUT body must represent the complete resource
# (RFC 8040 Sec 4.5): description, hold-time, ...
```

That guard is deliberate — a depth-truncated model used to silently wipe the
rest of the interface. Read at full depth first, use `update()` to merge, or
pass `allow_partial=True` when deleting those fields is what you actually mean.

```python
full = interfaces.retrieve(content="config", depth="unbounded")
full.service_label = "test137"
interfaces.replace(full)  # every returned field counts as set
```

### Credentials and connection security

No credentials are ever baked into generated code. Both clients resolve them at
runtime in the same order:

1. the `username` / `password` constructor arguments, if given;
2. otherwise `DEVICE_USER` and `DEVICE_PASS` from the environment.

If either value is still missing, the constructor raises `ValueError` before any
transport work happens, so there is no such thing as an unauthenticated client
that fails later with an opaque error. Two names, both protocols, no aliases —
the same `.env` that `yang-downloader` and `tester` read also works for a
generated client.

Transport security defaults are also identical, and both are secure:

| Transport | Default | Opt-out |
|---|---|---|
| RESTCONF | `verify=True`, `scheme="https"` | `verify=False` (self-signed lab gear) or `scheme="http"` (plaintext simulator) |
| NETCONF | host keys verified (`verify=True`) | `verify=False` (lab-only) |

Every opt-out logs a warning at construction. `verify=False` is for lab equipment
with self-signed certificates — never for production. `loopback_ip` is tried
before `management_ip`, so a local management interface wins when both are
given, and `timeout` is a `(connect, read)` pair for RESTCONF and a single
value in seconds for NETCONF.

### NETCONF write safety

`NetconfClient` defaults to `auto_commit=False`, and that default is the point:
with it off, an `edit()` to `candidate` is invisible in `running` until you
commit, so a multi-step change can be reviewed and rolled back. The safe
sequence is edit, validate, commit:

```python
from device_name_1_4_0 import NetconfClient

client = NetconfClient(management_ip="192.168.137.42", port=830, verify=True)

navigator = client.data.ietf_interfaces_interface("ethernet-1/1")
navigator.update(port1, target="candidate")

if client.has_validate:  # :validate is optional (RFC 6241 §8.6.4.1)
    client.validate(source="candidate")
client.commit()
```

`commit()`, `validate()`, `lock()`, `unlock()` and `discard_changes()` are client
methods; `discard_changes()` throws the candidate datastore away (RFC 6241
§8.6.5), leaving `running` untouched. `auto_commit=True` commits after *every*
edit, which is why it is opt-in and warns when enabled. Used as a context manager
the client locks on entry and, on an exception, discards the candidate and
releases the lock:

```python
with NetconfClient(management_ip="192.168.137.42", verify=True) as client:
    client.data.ietf_interfaces_interface("ethernet-1/1").update(port1)
    client.validate(source="candidate")
    client.commit()
```

Which datastore a write lands in depends on what the device advertises: reads
and writes route through NMDA (`<get-data>` / `<edit-data>`, RFC 8526) when the
exact `:nmda:1.0` capability URI is present, and through legacy
`<get>` / `<get-config>` / `<edit-config>` (RFC 6241) otherwise. `default_target`
is `candidate` when the device offers `:candidate`, else `running`. The six
capability flags are plain booleans, so `if client.has_validate:` is the correct
test:

`has_candidate`, `has_writable_running`, `has_nmda`, `has_validate`,
`has_confirmed_commit`, `has_rollback_on_error`.

Both clients also expose `close()` and work as context managers, which is how you
release the HTTP connection pool or the SSH session deterministically rather than
waiting for garbage collection.

### Calling RPCs and actions

RPCs live under `client.operations`; the same `<module>_<rpc>` naming applies.

```python
# Top-level RPC: POST /restconf/operations/<module>:<rpc>
# (NETCONF sends the equivalent <rpc><rpc-name> element)
result = client.operations.ietf_system_system_reset(delay_seconds=5)

# YANG 1.1 actions are invoked through the data tree, not under /operations
# (RFC 8040 Sec 3.6)
client.data.ietf_interfaces_interface("ethernet-1/1").reset(delay=3)
```

> [!WARNING]
> **Do not request the whole `restconf/data/` resource, and do not read with
> `depth="unbounded"`.** On a large production config that is the classic way
> to hit 100% CPU and trigger a watchdog reboot or an OOM kill. Read a subtree
> with an explicit `depth`; the generated navigators default to `depth=2`.

---

## Quick Start

### Setup

You need [`uv`](https://github.com/astral-sh/uv) and Python >= 3.12.

```bash
git clone https://github.com/ConsortiumGARR/yang2sdk.git
cd yang2sdk
uv sync --locked --extra lab   # lab extra: downloader/tester tooling
cp .env.example .env
```

Modify and save `.env` with your device's information. `.env` is gitignored and
must never be committed; the `DEVICE_*` names in it are what `yang-downloader`
and `tester` read, and they are also the default source for the generated
clients' credential lookup. `DEVICE_NAME` names the device and therefore the
output directories; `DEVICE_VERSION` feeds `--device-version` and the generated
package name.

Three more example files cover the other environments, all with placeholder
values and no secrets:

| File | Used by |
|---|---|
| `.env.example` | `yang-downloader`, `tester`, local compiles against real gear |
| `.env.notconf.example` | the `notconf` simulator matrix (`NOTCONF_USER` / `NOTCONF_PASS` / `NOTCONF_RUN_INTEGRATION`) |
| `.env.srl-lab.example` | the SR Linux containerlab lab (`SRL_DEVICE_*`) |
| `.env.lab-device.example` | the real-lab-device NETCONF harness (`LAB_DEVICE_*`) |

### Model Extraction

Get the YANG models from the vendor or use the following to try pulling what the network device is running.

```bash
uv run yang-downloader
```

Besides the YANG files, this writes `temp/yang_modules/<device>/features.json`
from the `<hello>` capability set (RFC 6241 §8.3) and saves only the newest
revision of each module (RFC 6022 §3.1.2). Both matter for the next step.

### YANG Tree inspection and modules identification

Identify the *root* modules you want to convert. This can help:

```bash
uv run pyang -p temp/yang_modules/device_name/ -f tree temp/yang_modules/device_name/* > temp/yang_tree/device_name.txt
```

### Compile to SDK

Both targets take the same arguments and produce the same navigator surface;
only the transport differs.

```bash
uv run yang2restconf temp/yang_modules/device_name/file1.yang temp/yang_modules/device_name/file2.yang
uv run yang2netconf  temp/yang_modules/device_name/file1.yang temp/yang_modules/device_name/file2.yang
```

| Flag | Effect |
|---|---|
| `--device` | target device name; defaults to `$DEVICE_NAME` |
| `--yang-dir` | YANG search path; defaults to `temp/yang_modules/<device>` |
| `--output-dir` | defaults to `temp/{restconf,netconf}_clients/<device>` |
| `--config-only` | drop `config false` (state) nodes from the generated models |
| `--device-version` | OS version for provenance and the package name; defaults to `$DEVICE_VERSION` |
| `--package-version` | PEP 440 package version; defaults to the device version |
| `--deviation-module` | apply a pyang deviation (repeatable, recorded in the MANIFEST) |
| `--feature mod:feat` | enable a feature (repeatable); adds to the device set, never narrows it |
| `--features-file` | feature set JSON; defaults to `<yang-dir>/features.json` when present |
| `--no-device-features` | ignore the device feature set and use pyang's defaults |
| `--ignore-error TAG` | downgrade a pyang error tag (repeatable), e.g. `XPATH_SYNTAX_ERROR` |
| `--check-model-gaps` | after compiling, report device data nodes the model lacks (NETCONF only; exits non-zero on gaps) |

Without a flag, the compiler picks up `<yang-dir>/features.json` automatically.
That is the default worth keeping: pyang otherwise assumes *every* `if-feature`
is on and prunes subtrees the device actually implements, and the strict models
(`extra="forbid"`) then reject real device data. `--feature` unions with the
device set on purpose, because narrowing is what produces a silently wrong
model.

### Acquisition of one instance of the model and models validation

Fetch the actual read-write configuration in JSON with RESTCONF using the generated client and load it into Pydantic models.

> [!WARNING]  
> **DO NOT REQUEST THE ROOT PATH (`restconf/data/`) ON PRODUCTION.**
> A large config can hit 100% CPU and trigger a watchdog reboot or OOM kill. Use lab equipment.

```bash
uv run tester
```

`tester` walks every top-level navigator of a RESTCONF client, reads each at
`content="config", depth=2`, and validates the payload against the generated
models. It is RESTCONF-only, needs the `lab` extra, and exits non-zero if any
navigator fails. It connects with `verify=False`, so point it at lab gear.

### Usage

Each compile emits a self-contained, installable package at
`temp/<protocol>_clients/<device>_<os-version>/`: the client, models, navigators,
its own `pyproject.toml`, a README naming the source YANG revisions, the
deviations and features applied, a `MANIFEST.yang-revisions.json`, and `py.typed`.
Add it to your project:

```bash
uv add path/to/temp/netconf_clients/device_name_1_4_0          # or --editable
```

```python
from device_name_1_4_0 import NetconfClient  # or RestconfClient
```

The emitter compiles every generated `.py` before reporting success, so a client
that cannot be imported is never handed to you.

#### Scaling to Production (Multi-Vendor / Multi-Version)

When managing real networks, it is inevitable to deal with multiple device models, vendors, and OS versions. An option is to structure the automation around a **Hardware Abstraction Layer (HAL)** and concrete **adapters**. 
- **HAL:** Exposes generic, vendor-agnostic entities and functions (e.g., `update_port_description(port, description)`).
- **adapters:** Implements the HAL interfaces using the specific `yang2sdk` clients for a given device and OS version.

One package per device *and* OS version, since different YANG revisions produce
different models:

```
multi_vendor_automation_project/
├── pyproject.toml
├── main.py
├── hal/                      <-- Hardware Abstraction Layer (the abstract contracts)
│   ├── __init__.py
│   ├── node.py               <-- e.g. Protocols and match-case routing to load the correct adapter
│   ├── port.py
│   ├── l2services.py
│   ├── l3services.py
│   └── adapters/
│       ├── __init__.py
│       └── device_name_1_4_0/
│           ├── __init__.py
│           ├── node.py
│           ├── port.py
│           ├── l2services.py
│           └── l3services.py
└── clients/                   <-- generated clients, added as `uv add path/to/...`
    └── device_name_1_4_0/
        ├── pyproject.toml
        ├── README.md
        ├── MANIFEST.yang-revisions.json
        ├── py.typed
        ├── __init__.py
        ├── session_manager.py
        ├── data_models/
        │   ├── __init__.py
        │   ├── _base.py
        │   └── models.py
        └── data_navigators/
            ├── __init__.py
            ├── _base.py
            └── navigators.py
```

`yang2sdk` itself stays vendor-agnostic. Device-specific workarounds belong in
the adapter, with a comment naming the YANG module, its revision, and the
observed behaviour — not in the generated models or the generator templates.

## Development

Requires Python >= 3.12 and [`uv`](https://github.com/astral-sh/uv).

```bash
uv sync --locked --extra lab          # or: uv sync --locked --group dev --extra lab
```

Four blocking gates, all run on `src/` (the ephemeral `temp/` output is excluded):

```bash
uvx ruff check .
uvx ruff format --check .
uvx ty check
uvx pyrefly check
```

`ruff` is the only linter and formatter. `ty` and `pyrefly` are both required; a
pass in one does not excuse a failure in the other.

### Tests

```bash
uv run pytest tests/test_matrix.py      # the offline gate: no docker, no device
```

Everything else in `tests/` is marked `integration` and skips unless you opt in:

```bash
uv run pytest tests/ --integration                        # notconf simulator matrix
NOTCONF_RUN_INTEGRATION=1 uv run pytest tests/             # same, via the environment
NOTCONF_SMOKE_ONLY=1 uv run pytest tests/ --integration   # one image per family
```

| File | Needs | In CI |
|---|---|---|
| `tests/test_matrix.py` | nothing | yes, every PR |
| `tests/test_notconf_protocol.py` | docker + `notconf` images | yes, smoke shards |
| `tests/test_sdk_generate.py` | docker + `notconf` images | yes, smoke shards |
| `tests/test_srl_netconf.py` | SR Linux containerlab lab | no, lab only |
| `tests/test_lab_device_netconf.py` | a real lab device | no, lab only |

CI lives in `.github/workflows/`: `ci-pr.yaml` runs lint → typecheck → the
offline gate → one smoke image per family on every PR, and `ci-nightly.yaml` runs
the full 11-image matrix. The last two rows above are in no workflow, so nothing
they assert gates a merge — treat them as lab verification you run yourself.

### Simulator lab

`tests/notconf/` drives pre-built [`notconf`](https://github.com/notconf/notconf)
images (`compose.yaml`, `matrix.json`, `wait_healthy.py`) covering Cisco IOS XR
`762/771/2411/2531`, IOS NX `10.4-4`, Junos `21.1R1/23.4R1`, Nokia SROS
`21.10/22.2`, IETF, and base. The `notconf` image also serves plain HTTP
RESTCONF on port 80, which is what the tests' `scheme="http"` opt-out is for.

### Contributing

`AGENTS.md` is the contract for changes to the generator: the IR in
`src/yang2sdk/plugin/src/ir.py` and the Jinja templates must stay in sync, the
RFC citations must be real, the secure defaults (`verify=True`, no embedded
credentials, `auto_commit=False`, the `replace()` completeness guard) must not be
weakened, and `CHANGELOG.md` records what changed.

## Comparison with Alternatives

The primary alternatives are [pydantify](https://github.com/pydantify/pydantify) and [pyangbind](https://github.com/robshakir/pyangbind). They both address the data modeling but do not facilitate the actual network operations.

**yang2sdk** directly targets the real-world needs of network automation engineers by generating the Pydantic v2 models as well as the code for actual network operations.
The goal is to make the development of network automation faster, easier, and safer leveraging IDE autocomplete, type hinting, static type checking and Pydantic's runtime validation.
The core of this project is the [pyang](https://github.com/mbj4668/pyang) plugin that walks the raw `pyang` Abstract Syntax Tree (AST), builds an Intermediate Representation and uses Jinja to generate Python code.

**pydantify** converts YANG modules into Pydantic models using a more sophisticated pipeline:
`YANG Abstract Syntax Tree (AST)` -> `Internal Object-Oriented AST` -> `Dynamic In-Memory Pydantic Models` -> `JSON Schema` -> `datamodel-code-generator` -> `Pydantic Models`.
It does not provide the code for network operations.

**pyangbind** dynamically generates Python classes at runtime and does not provide the code for network operations.

## Status

This is a public prototype. Both RESTCONF and NETCONF clients generate, with an interchangeable navigator surface
(`retrieve`/`update`/`replace`/`create`/`delete`, and RPC/action `__call__`) so developer code written against one
only needs the imported client swapped for the other. The RPC and action wire encodings are pinned offline by
`tests/test_matrix.py` against RFC 8040 Sec 3.6 and RFC 7950 Sec 7.15.2, and re-verified live against SR Linux
(`get-schema`, `lock`/`commit`/`unlock`, create→validate→commit→delete) and the Groove G30 (`no-op`, `ping`)
over both transports.

Both generated SDKs are exercised against all 11 pre-built `notconf` simulator images via the `pytest` suite in `tests/`: protocol checks, per-image SDK generation from the simulator's own YANG, Pydantic validation of live payloads, full CRUD round-trips (create → retrieve → update → replace → delete) through both clients, and idempotent same-data merges on every top-level config node.

The offline gate is `uv run pytest tests/test_matrix.py`, which runs on every PR with no device and no docker; it covers matrix coverage, the template secure defaults, navigator parity, and the RPC/action wire shapes. The live matrix is opt-in and splits across CI as described under [Development](#development): smoke shards per family on PRs, the full 11 images nightly. A test that covers zero nodes skips rather than passing vacuously — the image ships no config modules, the simulator cannot read the node, or the device has no `:validate`.

Live runs against SR Linux (`get-schema`, `lock`/`commit`/`unlock`, create → validate → commit → delete) and the Groove G30 (`no-op`, `ping`) happen in `tests/test_srl_netconf.py` and `tests/test_lab_device_netconf.py`. **Neither is in any CI workflow**, so those results come from lab runs, not from a gate.

Known limits (each pinned to a test): write shapes follow strict RFC 8040 + RFC 7951 — `create` POSTs to the parent with a single-element array body (RFC 8040 §4.4.1 + App. B.2.1, exactly one instance) and item `update`/`replace` send single-element array bodies (RFC 8040 §4.5 jukebox album example + RFC 7951 §5.4 list as name/array; PATCH list-instance follows the same JSON encoding, cf. rousette `tests/restconf-plain-patch.cpp` 204); key-mismatch and `requires N keys` errors enforce RFC 8040 §3.5.3/§4.5. Whole-list `replace` PUTs the list resource itself (`tests/test_matrix.py`). Simulator read quirks (`/data` GETs hide written list entries; collection GETs 400; `/ds/...?content=config` 500s; `/ds` item GETs are root-wrapped) are isolated to test read-backs via the RFC 8527 running-datastore container, not baked into the SDK. Details in `tests/test_sdk_generate.py`.
