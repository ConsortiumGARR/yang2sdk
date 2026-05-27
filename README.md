# yang2sdk

Generate a Pydantic-based IDE-friendly SDK for your network devices directly from YANG modules.

## Overview

This pipeline extracts the YANG modules directly from your network devices and transforms them into a type-safe RESTCONF SDK interface to your device.

Then you can do stuff like this to update the description of a port:

```python
from device_name import RestconfClient as DeviceNameClient

client = DeviceNameClient(
    management_ip="192.168.137.42",
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
port131.service_label = "test137"

port131.admin_status = "dowm" 
    # Here the code fails immediately, raising the following error:
    # pydantic_core._pydantic_core.ValidationError: 1 validation error for PortItem
    # admin_status
    #   Input should be 'up' or 'down'  [type=enum, input_value='dowm', input_type=str]

    # Update the device config
uri.update(port131)
```

---

## Quick Start

### Setup

You need [`uv`](https://github.com/astral-sh/uv).

```bash
git clone https://github.com/ConsortiumGARR/yang2sdk.git
cd yang2sdk
uv sync --locked
cp .env.example .env
```

Modify and save `.env` with your device's information.

### Model Extraction

Get the YANG models from the vendor or use the following to try pulling what the network device is running.

```bash
uv run utils/yang_downloader.py
```

### YANG Tree inspection and modules identification

Identify the *root* modules you want to convert. This can help:

```bash
uv run pyang -p temp/yang_modules/device_name/ -f tree temp/yang_modules/device_name/* > temp/yang_tree/device_name.txt
```

### Compile to SDK

Convert all the modules of interest. For example, if the root modules are in file1.yang and file2.yang:

```bash
uv run pyang -V \
    --plugindir utils/pyang_plugins/ \
    -f restconf \
    --sdk-output-dir  temp/restconf_clients/device_name/ \
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

The primary alternatives are [pydantify](https://github.com/pydantify/pydantify) and [pyangbind](https://github.com/robshakir/pyangbind). They both address the data modeling but do not facilitate the actual network operations.

**yang2sdk** directly targets the real-world needs of network automation engineers by generating the Pydantic v2 models as well as the code for actual network operations.
The goal is to make the development of network automation faster, easier, and safer leveraging IDE autocomplete, type hinting, static type checking and Pydantic's runtime validation.
The core of this project is the [pyang](https://github.com/mbj4668/pyang) plugin that walks the raw `pyang` Abstract Syntax Tree (AST) and uses direct string concatenation to generate Python code.

**pydantify** converts YANG modules into Pydantic models using a more sophisticated pipeline:
`YANG Abstract Syntax Tree (AST)` -> `Internal Object-Oriented AST` -> `Dynamic In-Memory Pydantic Models` -> `JSON Schema` -> `datamodel-code-generator` -> `Pydantic Models`.
It does not provide the code for network operations.

**pyangbind** dynamically generates Python classes at runtime and does not provide the code for network operations.
