# Lab validation

The offline suite proves the *wire shape* is right. It cannot prove *your* device speaks it. `sdk-verify` closes that gap by walking the generated client's own navigator tree and recording one row per (node, method).

> [!WARNING]
> **Lab equipment only, never production.** Every read is depth-bounded, but a large config can still hit 100% CPU and trigger a watchdog reboot or OOM kill. The write tiers edit the device.

```bash
# Read every node and round-trip every RPC model. Nothing is modified.
uv run sdk-verify --device <device> --protocol both --tiers read,rpc

# Report as JSON, for CI or a spreadsheet.
uv run sdk-verify --device <device> --json-out temp/verify/<device>.json
```

It needs the `lab` extra, reads `DEVICE_IP` / `DEVICE_USER` / `DEVICE_PASS` (same contract as a generated client: args win, then the environment), and exits non-zero if any endpoint fails.

## Tiers

| Tier | What it does | Gate |
| --- | --- | --- |
| `read` | Recursive walk of the entire data tree at bounded depth (`--depth`, `--max-depth`), validating each payload. Automatic. | none |
| `rpc` | Builds every RPC `Input` and serialises it (`model_dump(by_alias=)`, `to_xml_payload()`) **without sending**. Catches envelope bugs across the whole RPC surface. | none |
| `rpc` + `--rpc-allowlist` | Actually dispatches the named RPCs, echoing each one first. | `--rpc-allowlist` |
| `crud` | `retrieve → update → read back` per container (`--containers` to narrow), then a proven restore. Idempotent merge, so an interrupted run cannot leave a changed value. | `--write`, plus a second flag (below) |

Every skip carries its reason. A node that covers nothing is never reported as a pass, and a device rejection (HTTP 400/404/405, `invalid-value` / `access-denied` incl. NACM RFC 8341) is recorded as a skip with the RFC section, not as a client failure. A run in which **every** row is a skip exits non-zero (`no endpoint was exercised`).

## Write gates

`--write` is opt-in, and writing needs a *second* acknowledgement because the two transports are not equally reversible:

| Situation | Extra flag | Why |
| --- | --- | --- |
| NETCONF with `:candidate` | — | edits stage in `candidate` and are dropped with `<discard-changes>` (RFC 6241 §8.3.4.2) |
| NETCONF without `:candidate` | `--allow-running-writes` | edits land in **running** immediately (RFC 6241 §8.2); only the snapshot can undo them |
| RESTCONF (any device) | `--allow-restconf-writes` | `PATCH`/`PUT`/`POST`/`DELETE` are live at once; no candidate, no `<discard-changes>` |

Before any write the tool snapshots every node it will touch (`--snapshot`, default `temp/verify/<device>-<protocol>-snapshot.json`, created on demand) and **refuses to proceed if that fails**. It holds a NETCONF lock for the duration — a lock failure aborts rather than warns (RFC 6241 §8.5.1 makes the lock a precondition for writing running). Afterwards it compares a whole-tree digest and reports `RESTORE NOT PROVEN` loudly, naming the snapshot.

`create` / `delete` are **not** in the automated tier: no generic synthesiser can satisfy arbitrary `must` / `when` / leafref / mandatory constraints, so testing every list would produce false failures and could write junk to live gear.

## Environment files

| File | Used by |
| --- | --- |
| `.env.example` | `yang-downloader`, `sdk-verify`, local compiles against real gear (`DEVICE_*`) |
| `.env.notconf.example` | `notconf` simulator matrix (`NOTCONF_USER` / `NOTCONF_PASS` / `NOTCONF_RUN_INTEGRATION`) |
| `.env.srl-lab.example` | SR Linux containerlab lab remote-override shim (`SRL_DEVICE_*`) |
| `tests/srl/.env.srl.example` | committed containerlab defaults sourced by CI/`lab.py` (`DEVICE_*`, `NETCONF_PORT=1830`, `RESTCONF_PORT=443`) |
| `.env.lab-device.example` | real-lab-device NETCONF harness (`LAB_DEVICE_*`) |

`.env` files are gitignored and must never be committed. Do not print, log, or propagate `DEVICE_PASS` — cleartext on disk is already a compromise.

## TLS verification

`sdk-verify` verifies TLS/host keys by default (`--verify-tls`, matching the generated clients' `verify=True`); use `--no-verify-tls` for lab self-signed gear only — it logs a warning and must never target production. `--restconf-scheme http` is plaintext, for simulators only.

## Simulator lab

`tests/notconf/` drives pre-built [`notconf`](https://github.com/notconf/notconf) images (`compose.yaml`, `matrix.json`, `wait_healthy.py`): Cisco IOS XR `762/771/2411/2531`, IOS NX `10.4-4`, Junos `21.1R1/23.4R1`, Nokia SROS `21.10/22.2`, IETF, and base. The image also serves plaintext RESTCONF on port 80 — that is what the tests' `scheme="http"` opt-out is for.

## SR Linux job (`ci-srl.yaml`)

Simulators cover a handful of IETF/vendor models; SR Linux supplies a **full commercial vendor model tree** (hundreds of modules, deep augment closure, identityref leaves) through the generated client. It is **not** NMDA coverage: its `<hello>` (370 capabilities) advertises `candidate`, `confirmed-commit`, `rollback-on-error`, `startup`, `url`, `validate`, `with-defaults`, `with-operational-defaults`, `yang-library` — no `:nmda:1.0`. It ships the `ietf-netconf-nmda` *module* without the capability, the false positive `test_nmda_is_detected_from_the_capability_uri_not_a_module_name` pins.

Severity is split on purpose:

| Tier | Steps | On failure |
| --- | --- | --- |
| blocking | deploy, readiness, `get-schema`, generate | red X — the run covered nothing |
| non-blocking | `pytest tests/test_srl_netconf.py` | reported, does not gate a merge |

Blocking steps run first, so a broken lab is never reported as "tests passed". The job pulls the node's own models over `get-schema` and compiles `temp/netconf_clients/srl` — the same `yang-downloader` → `yang2netconf` path a user runs. Expect one skip (`test_hostname_mutate_restore` — documented in `tests/test_srl_netconf.py`). Pinned image/containerlab versions: `tests/srl/PINNED.md`. Stays on `ubuntu-24.04` (standard runners are free for public repos; `-large` labels are billed).
