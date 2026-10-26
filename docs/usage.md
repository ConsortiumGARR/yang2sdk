# Usage

Generating clients, authenticating, writing safely, calling RPCs, and packaging the output.

`temp/` is an untracked workspace: the compiler, downloader, and `sdk-verify` create their own output dirs on demand (`temp/yang_modules/<device>/`, `temp/{restconf,netconf}_clients/<device>/`, `temp/verify/`). A missing YANG search dir is a hard error (run `yang-downloader` first); output dirs are created automatically.

## Compile

Both targets share the same navigator intent and differ only in transport; the per-class surface is asymmetrical by protocol (RESTCONF plural lists expose `create`, not `update`; NETCONF plural lists expose `update`, not `delete` — see `AGENTS.md` parity table).

```bash
uv run yang2sdk --protocol netconf   --device <device>   # interactive: pick the roots
uv run yang2sdk --protocol restconf --device <device>
uv run yang2sdk --protocol netconf --device <device> --roots ietf-interfaces,ietf-system
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
| `--ignore-error TAG` | downgrade a pyang error tag (repeatable), e.g. `XPATH_SYNTAX_ERROR`, `PATTERN_ERROR` |
| `--check-model-gaps` | report device data nodes the model lacks (NETCONF only; exits non-zero on gaps) |

`PATTERN_ERROR` is worth knowing by name. A YANG `pattern` is an XML Schema regular expression (RFC 7950 §9.4.5), so the XSD engine pyang validates with rejects Python-only constructs such as a negative lookahead (`(?!...)`) — which a vendor model may still contain. `--ignore-error PATTERN_ERROR` accepts the model and **keeps** the constraint: the pattern is read from the `pattern` statement itself, not from pyang's aborted type spec, so the emitted validator still carries it. A pattern Python's `re` cannot express either (`\p{IsBasicLatin}`, `\i`, `\c`) is refused instead of emitted, because the validator runs when a value is assigned, not when the module is imported; replace it through a deviation module (RFC 7950 §7.20.3).

Without a flag, the compiler picks up `<yang-dir>/features.json` automatically (written by `yang-downloader` from the `<hello>` capability set, RFC 6241 §8.3, newest revision per module per RFC 6022 §3.1.2). Keep that default: pyang otherwise assumes every `if-feature` is on and prunes subtrees the device implements, and the strict models (`extra="forbid"`) then reject real device data. `--feature` unions on purpose — narrowing produces a silently wrong model.

The emitter compiles every generated `.py` before reporting success, so a client that cannot be imported is never handed to you.

## Credentials and connection security

No credentials are ever baked into generated code. Both clients resolve them at runtime **by source**, not field by field:

1. an explicit `password` is used and the environment key pair is ignored;
2. an explicit `key_filename` is used and `DEVICE_PASS` is ignored; its passphrase comes only from the explicit `passphrase` argument (or from `DEVICE_SSH_KEY_PASSPHRASE` when the key itself came from `DEVICE_SSH_KEY`);
3. otherwise the environment is used: `DEVICE_USER` plus `DEVICE_PASS`, or — for NETCONF — `DEVICE_SSH_KEY` / `DEVICE_SSH_KEY_PASSPHRASE`. If both password and key are set there, the key wins with a logged warning, never silently.

Supplying both an explicit password and an explicit key is rejected by generated clients and `yang-downloader`; `sdk-verify` warns and uses the key for NETCONF, so `--protocol both` can pair a RESTCONF password with a NETCONF key.

A username is always required. NETCONF then accepts **either** a password **or** an SSH key; RESTCONF accepts a password only (SSH transport belongs to NETCONF). If no credential is available, the constructor raises `ValueError` before any transport work — there is no unauthenticated client that fails later with an opaque error. The names are not aliased: `DEVICE_USERNAME` / `DEVICE_PASSWORD` are retired and ignored, and the same `.env` that `yang-downloader` and `sdk-verify` read also works for a generated client. The NETCONF username is forwarded to the SSH layer unchanged, so it must consist of XML `Char` code points (RFC 6242 §3, RFC 6241 §2.2); a name containing e.g. a NUL is rejected before connecting.

```python
from g30_1_4_0 import NetconfClient

# Password
client = NetconfClient(management_ip="192.0.2.10")

# SSH key (or `...-cert.pub` for an OpenSSH user certificate)
client = NetconfClient(
    management_ip="192.0.2.10",
    key_filename="~/.ssh/id_ed25519",
    passphrase=None,  # only for an encrypted key
)
```

`key_filename` takes one path or a sequence. OpenSSH certificates use the conventional `<private-key>-cert.pub` filename: paramiko strips that suffix to load the private key and then attaches the certificate, so passing either member of the pair works. The authentication method is the RFC 4252 §7 `publickey` method; the certificate *encoding* is the OpenSSH vendor extension specified by the IETF Internet-Draft `draft-ietf-sshm-cert`, not by an RFC. The adjacent private key must be present, because an explicit path disables the SSH agent and `~/.ssh` key discovery. Paths are validated before connecting, so a typo — or a valid but unauthorized key — fails closed instead of silently falling through to another key. The **account password** is never placed in ncclient's single SSH `password` slot when a key is used: that slot carries the key passphrase (or nothing for an unencrypted key), because paramiko rejects an unencrypted key when a password is supplied. ncclient will, however, try an encrypted key's passphrase as a password if public-key authentication fails (`SSHSession._auth`); the client logs a warning naming that fallback. Use an unencrypted key if that behaviour is unacceptable. A passphrase without a key is rejected as misconfiguration rather than being tried as an account password.

`yang-downloader` reads the same two key names, and `sdk-verify` exposes them as `--ssh-key` / `--ssh-key-passphrase` (or the environment) for `--protocol netconf`.

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

Each compile emits a self-contained, installable project per device *and* OS version: client, models, navigators, own `pyproject.toml`, README (device, OS version, source YANG revisions, protocol), deviations/features provenance (`MANIFEST.yang-revisions.json`), and `py.typed`. No `sdk-verify`, no `.env`, no logs, no secrets.

`--output-dir` is a **container** holding exactly one project; the project is
the directory named after the package, and the importable package sits under
`src/` (the layout this repo uses for itself):

```text
temp/netconf_clients/<device>/          # --output-dir (container)
└── <device>_<os-version>/              # project: pyproject.toml, README, MANIFEST
    └── src/<device>_<os-version>/      # package: import <device>_<os_version>
```

There is one copy of the code, on disk and in the sdist. A container that
already holds a *different* package (a previous compile for another OS
version) is refused rather than mixed into, and so is a directory from the
pre-`src/` layout: both would put two importable trees in one place.

```bash
uv add path/to/temp/netconf_clients/<device>/<device>_<os-version>   # or --editable
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
        └── src/
            └── <device>_<os-version>/
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
