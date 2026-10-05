# Usage

Generating clients, authenticating, writing safely, calling RPCs, and packaging the output.

## Compile

Both targets share the same navigator intent and differ only in transport; the per-class surface is asymmetrical by protocol (RESTCONF plural lists expose `create`, not `update`; NETCONF plural lists expose `update`, not `delete` — see `AGENTS.md` parity table).

```bash
uv run yang2restconf temp/yang_modules/<device>/file1.yang [file2.yang ...] --device <device>
uv run yang2netconf  temp/yang_modules/<device>/file1.yang [file2.yang ...] --device <device>
```

| Flag | Effect |
| --- | --- |
| `--device` | target device name; defaults to `$DEVICE_NAME` |
| `--yang-dir` | YANG search path; defaults to `temp/yang_modules/<device>` |
| `--output-dir` | defaults to `temp/{restconf,netconf}_clients/<device>` |
| `--config-only` | drop `config false` (state) nodes from the generated models |
| `--device-version` | OS version for provenance and the package name; defaults to `$DEVICE_VERSION` |
| `--package-version` | PEP 440 package version; defaults to the device version |
| `--deviation-module` | apply a pyang deviation (repeatable, recorded in the MANIFEST) |
| `--feature mod:feat` | enable a feature (repeatable); unions with the device set, never narrows it |
| `--features-file` | feature set JSON; defaults to `<yang-dir>/features.json` when present |
| `--no-device-features` | ignore the device feature set and use pyang defaults |
| `--ignore-error TAG` | downgrade a pyang error tag (repeatable), e.g. `XPATH_SYNTAX_ERROR` |
| `--check-model-gaps` | report device data nodes the model lacks (NETCONF only; exits non-zero on gaps) |

Without a flag, the compiler picks up `<yang-dir>/features.json` automatically (written by `yang-downloader` from the `<hello>` capability set, RFC 6241 §8.3, newest revision per module per RFC 6022 §3.1.2). Keep that default: pyang otherwise assumes every `if-feature` is on and prunes subtrees the device implements, and the strict models (`extra="forbid"`) then reject real device data. `--feature` unions on purpose — narrowing produces a silently wrong model.

The emitter compiles every generated `.py` before reporting success, so a client that cannot be imported is never handed to you.

## Credentials and connection security

No credentials are ever baked into generated code. Both clients resolve them at runtime in the same order:

1. the `username` / `password` constructor arguments, if given;
2. otherwise `DEVICE_USER` and `DEVICE_PASS` from the environment.

If either value is still missing, the constructor raises `ValueError` before any transport work — there is no unauthenticated client that fails later with an opaque error. Exactly two names, both protocols, no aliases: the same `.env` that `yang-downloader` and `sdk-verify` read also works for a generated client. `DEVICE_USERNAME` / `DEVICE_PASSWORD` are retired and ignored.

| Transport | Default | Opt-out (explicit + logged warning) |
| --- | --- | --- |
| RESTCONF | `verify=True`, `scheme="https"`, `port=443` | `verify=False` (self-signed lab gear) or `scheme="http"` (plaintext simulator) |
| NETCONF | host keys verified (`verify=True`), `port=830` | `verify=False` (lab-only) |

`loopback_ip` is tried before `management_ip`, so a local management interface wins when both are given. `timeout` is a `(connect, read)` pair for RESTCONF and a single value in seconds for NETCONF. `verify=False` is for lab equipment with self-signed certificates — never production. Both clients expose `close()` and work as context managers to release the HTTP pool / SSH session deterministically.

## NETCONF write safety

`NetconfClient` defaults to `auto_commit=False`, and that default is the point: with it off, an `edit()` to `candidate` is invisible in `running` until you commit, so a multi-step change can be reviewed and rolled back. The safe sequence is edit → validate → commit (RFC 6241 §8.6.4.1, §8.3.4.1):

```python
from g30_1_4_0 import NetconfClient

client = NetconfClient(management_ip="192.0.2.10")
nav = client.data.ietf_interfaces_interface("ethernet-1/1")
nav.update(port, target="candidate")

if client.has_validate:  # :validate is optional (RFC 6241 §8.6.4.1)
    client.validate(source="candidate")
client.commit()
```

`commit()`, `validate()`, `lock()`, `unlock()` and `discard_changes()` are client methods; `discard_changes()` throws the candidate away (RFC 6241 §8.3.4.2), leaving `running` untouched. `auto_commit=True` commits after *every* edit, which is why it is opt-in and warns. As a context manager the client locks on entry and, on exception, discards the candidate and releases the lock.

Reads and writes route through NMDA (`<get-data>` / `<edit-data>`, RFC 8526) when the exact `:nmda:1.0` capability URI is present, else legacy `<get>` / `<get-config>` / `<edit-config>` (RFC 6241). `default_target` is `candidate` when the device offers `:candidate`, else `running`. The six capability flags are plain booleans, so `if client.has_validate:` is the correct test: `has_candidate`, `has_writable_running`, `has_nmda`, `has_validate`, `has_confirmed_commit`, `has_rollback_on_error`.

`edit()` emits `<error-option>rollback-on-error</error-option>` (RFC 6241 §7.2) only when `target == "running"` and the device advertises `:rollback-on-error` — not on `candidate` (use `<discard-changes>`), not on the NMDA `<edit-data>` branch (RFC 8526 §3.1.2 already behaves that way). Callers writing `running` are expected to hold a lock first (§8.5.1).

`replace()` on a container or keyed item (PUT / `operation="replace"`) refuses a model with unset config fields (`allow_partial=True` overrides): a replace body is the complete resource, so a partial model would silently delete the rest. Whole-list `replace()` takes a bare list with no guard — the list body *is* the complete resource. State (`config false`) leaves never count toward the guard.

## RPCs and actions

RPCs live under `client.operations` with the same `<module>_<rpc>` naming:

```python
result = client.operations.ietf_system_system_reset(delay_seconds=5)
```

YANG 1.1 actions are invoked through the data tree, not under `/operations` (RFC 8040 §3.6):

```python
client.data.ietf_interfaces_interface("ethernet-1/1").reset(delay=3)
```

## Generated package

Each compile emits a self-contained, installable package: client, models, navigators, own `pyproject.toml`, README (device, OS version, source YANG revisions, protocol), deviations/features provenance (`MANIFEST.yang-revisions.json`), and `py.typed`. No `sdk-verify`, no `.env`, no logs, no secrets.

```bash
uv add path/to/temp/netconf_clients/<device>_<os-version>   # or --editable
```

### Scaling to production (HAL)

For multiple vendors / OS versions, keep `yang2sdk` vendor-agnostic and put quirks in downstream adapters (commented with YANG module + revision + observed evidence), not in the generator:

```txt
multi_vendor_automation_project/
├── pyproject.toml
├── main.py
├── hal/                      # vendor-agnostic contracts
│   ├── __init__.py
│   ├── node.py               # e.g. Protocols and match-case routing to the adapter
│   ├── port.py
│   ├── l2services.py
│   ├── l3services.py
│   └── adapters/
│       ├── __init__.py
│       └── <device>_<os-version>/
│           ├── __init__.py
│           ├── node.py
│           ├── port.py
│           ├── l2services.py
│           └── l3services.py
└── clients/                  # generated packages (uv dependencies)
    └── <device>_<os-version>/
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

One package per device *and* OS version.
