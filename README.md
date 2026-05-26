# YANG2SDK: Generate a Pydantic-based IDE-friendly RESTCONF SDK for your network devices directly from YANG modules

This pipeline extracts the YANG modules directly from your network devices and transforms them into a type-safe RESTCONF SDK interface to your device.

Then you can do stuff like this to update the description of a port:

```python
from device_name import RestconfClient as DeviceNameClient

client = DeviceNameClient(
    management_ip="192.168.123.42",
    port=443,
    username="user",
    password="pass",
    verify=True
)

    # IDE will suggest possibilities and autocomplete as soon as you type `client.`
uri = client.data.ne.shelf(1).slot(3).card.port(1)

    # Retrieve current config
port131 = uri.retrieve(content='config', depth=2)

    # The retrieved config (JSON) is loaded into the corresponding Pydantic model that you can modify.
    # As soon as you type `port1.` the IDE will show you all possible fields.
port131.service_label = "test123"

port131.admin_status = "daun" 
    # Here the code fails immediately, raising the following error:
    # pydantic_core._pydantic_core.ValidationError: 1 validation error for PortItem
    # admin_status
    #   Input should be 'up' or 'down'  [type=enum, input_value='daun', input_type=str]

    # Update the device config
uri.update(port131)
```

---

## Quick Start

### Setup

You need [`uv`](https://github.com/astral-sh/uv).

```bash
git clone https://thisrepo
cd pyangdantic
uv sync --locked
```

save a file named `.env` in project's root with the following variables:
```.env
DEVICE_NAME = 'device_name'
DEVICE_IP = 'ip'
DEVICE_USER = 'username'
DEVICE_PASS = 'password'
RESTCONF_PORT = 443 # default 443 or vendor specific
NETCONF_PORT = 830  # default 830 or vendor specific
```

### Model Extraction

Get the YANG models from the vendor or use the following to pull what the device is running.

```bash
uv run utils/yang_downloader.py
```

### YANG Tree inspection and modules identification

Identify the root modules you want to convert. This can help:

```bash
uv run pyang -p temp/yang_modules/device_name/ -f tree temp/yang_modules/device_name/* > temp/yang_tree/device_name.txt
```

### Schema Transformation

Convert all the modules of interest. For example, if root modules are in file1.yang and file2.yang:

```bash
uv run pyang -V \
    --plugindir utils/pyang_plugins/ \
    -f pydantic \
    --pydantic-output-dir  temp/restconf_clients/device_name/ \
    --path temp/yang_modules/device_name/ \
    temp/yang_modules/device_name/file1.yang temp/yang_modules/device_name/file2.yang
```

### Acquisition of one instance of the model and models validation

Fetch the actual read-write configuration in JSON with RESTCONF using the generated client and load it into Pydantic models.

> [!WARNING]  
> **DO NOT REQUEST THE ROOT PATH (`restconf/data/`) ON PRODUCTION.**
> A large config can hit 100% CPU and trigger a watchdog reboot or OOM kill. Use lab equipment.

```bash
uv run utils/try_client.py
```

## Comparison with Alternatives

The primary alternative is [pydantify](https://github.com/pydantify/pydantify). While both tools aim to bridge the gap between YANG and Pydantic, they are different.

### Pydantify
Pydantify is a sophisticated, multi-stage pipeline:
`YANG Abstract Syntax Tree (AST)` -> `Internal Object-Oriented AST` -> `Dynamic In-Memory Pydantic Models` -> `JSON Schema` -> `datamodel-code-generator` -> `Pydantic Models`.

**Pros:** modular, well-architected codebase, intermediate formats (JSON Schema).

### Pyangdantic
This project instead ignores the "clean code" manual in favor of direct results. It walks the raw `pyang` AST and uses direct string concatenation to generate code. By skipping intermediate JSON Schemas, it avoids the quirks and limitations of third-party code generators and possible metadata loss.

**Pros:** This is an opinionated SDK generator. It builds the Pydantic v2 models and the boilerplate for URI navigation, CRUD operations, and configuration templating. If you want to change the generated code, you have to change the code that generates it.
