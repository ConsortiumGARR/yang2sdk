# yang2sdk

Generate a Pydantic-based IDE-friendly SDK for your network devices directly from YANG modules.

## Overview

This pipeline extracts the YANG modules directly from your network devices and transforms them into a type-safe RESTCONF or NETCONF SDK interface to your device.

Then you can do stuff like this to update the description of a port:

```python
from device_name import RestconfClient as DeviceNameClient

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

# `replace()` is a PUT / nc:operation="replace", i.e. the body IS the whole
# resource (RFC 8040 Sec 4.5). It refuses a model with unset fields so a
# hand-built or depth-truncated model cannot silently delete the rest.
interfaces.replace(port1)
```

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

You need [`uv`](https://github.com/astral-sh/uv).

```bash
git clone https://github.com/ConsortiumGARR/yang2sdk.git
cd yang2sdk
uv sync --locked --extra lab   # lab extra: downloader/tester tooling
cp .env.example .env
```

Modify and save `.env` with your device's information.

### Model Extraction

Get the YANG models from the vendor or use the following to try pulling what the network device is running.

```bash
uv run yang-downloader
```

### YANG Tree inspection and modules identification

Identify the *root* modules you want to convert. This can help:

```bash
uv run pyang -p temp/yang_modules/device_name/ -f tree temp/yang_modules/device_name/* > temp/yang_tree/device_name.txt
```

### Compile to SDK

Convert all the modules of interest. For example, if the root modules are in file1.yang and file2.yang:

```bash
uv run yang2restconf temp/yang_modules/device_name/file1.yang temp/yang_modules/device_name/file2.yang 
```

### Acquisition of one instance of the model and models validation

Fetch the actual read-write configuration in JSON with RESTCONF using the generated client and load it into Pydantic models.

> [!WARNING]  
> **DO NOT REQUEST THE ROOT PATH (`restconf/data/`) ON PRODUCTION.**
> A large config can hit 100% CPU and trigger a watchdog reboot or OOM kill. Use lab equipment.

```bash
uv run tester
```

### Usage

Copy-paste the entire directory `temp/netconf_clients/device_name` into your own project and start automating!

#### Scaling to Production (Multi-Vendor / Multi-Version)

When managing real networks, it is inevitable to deal with multiple device models, vendors, and OS versions. An option is to structure the automation around a **Hardware Abstraction Layer (HAL)** and concrete **adapters**. 
- **HAL:** Exposes generic, vendor-agnostic entities and functions (e.g., `update_port_description(port, description)`).
- **adapters:** Implements the HAL interfaces using the specific `yang2sdk` clients for a given device and OS version.

For example:
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
|   └── adapters/
│       ├── __init__.py
|       └── device_name/
|           └── release_version/
│               ├── __init__.py
│               ├── node.py
│               ├── port.py
│               ├── l2services.py
│               └── l3services.py
└── clients/                   <-- generated clients can live inside your project or added as a package
    ├── __init__.py             
    └── device_name/
        └── release_version/
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

Both generated SDKs are exercised against all 11 pre-built [`notconf`](https://github.com/notconf/notconf) simulator images (Cisco IOS XR `762/771/2411/2531`, IOS NX `10.4-4`, Junos `21.1R1/23.4R1`, Nokia SROS `21.10/22.2`, IETF, base) via the `pytest` suite in `tests/`: protocol checks, per-image SDK generation from the simulator's own YANG, Pydantic validation of live payloads, full CRUD round-trips (create → retrieve → update → replace → delete) through both clients, and idempotent same-data merges on every top-level config node. `uv run pytest tests/` runs the offline gate; `NOTCONF_RUN_INTEGRATION=1` runs the live matrix (see `tests/notconf/matrix.json`, `.github/workflows/`).

Known limits (each pinned to a test): write shapes follow strict RFC 8040 + RFC 7951 — `create` POSTs to the parent with a single-element array body (RFC 8040 §4.4.1 + App. B.2.1, exactly one instance) and item `update`/`replace` send single-element array bodies (RFC 8040 §4.5 jukebox album example + RFC 7951 §5.4 list as name/array; PATCH list-instance follows the same JSON encoding, cf. rousette `tests/restconf-plain-patch.cpp` 204); key-mismatch and `requires N keys` errors enforce RFC 8040 §3.5.3/§4.5. Whole-list `replace` PUTs the list resource itself (`tests/test_matrix.py`). Simulator read quirks (`/data` GETs hide written list entries; collection GETs 400; `/ds/...?content=config` 500s; `/ds` item GETs are root-wrapped) are isolated to test read-backs via the RFC 8527 running-datastore container, not baked into the SDK. Details in `tests/test_sdk_generate.py`.
