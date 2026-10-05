# SR Linux lab (LAB ONLY)

Single-node containerlab lab for testing the generated NETCONF SDK
against real SR Linux instead of the `notconf` simulator.

- Topology: `srl.clab.yml` (pinned `ghcr.io/nokia/srlinux:25.10.1`,
  `ixr-d2l`, `127.0.0.1:1830 -> 830`). Pinned digests and the
  containerlab version are recorded in [PINNED.md](PINNED.md).
- `srl-startup.cli` adds **only** the AAA `yang2sdk-test` NETCONF role.
  SSH (`mgmt-netconf:830`) + `netconf-server mgmt` come from containerlab
  defaults (`nodes/srl/version_configs/netconf.cfg`), so they are not
  duplicated here.
- Credentials: `admin / NokiaSrl1!` (published containerlab defaults).
  They live in `.env.srl.example` — committed, because they are documentation,
  not secrets. `lab.py` parses that file and is the **only** place the values
  are read from; never hardcode them again. Override the endpoint or the
  credentials with `SRL_DEVICE_IP` / `SRL_DEVICE_NETCONF_PORT` /
  `SRL_DEVICE_USER` / `SRL_DEVICE_PASS` (see `../../.env.srl-lab.example`) to
  point the same code at a remote node.
- YANG source: **the node's own library**, pulled over `get-schema` by
  `yang-downloader`, then compiled with `yang2netconf` over the augment
  closure from `srl_nokia-system` + `srl_nokia-interfaces`. Offline
  `nokia/srlinux-yang-models` (tag matching the image) is the manual fallback
  when there is no node to ask; CI does not use it.
- Safety: depth-bounded reads only, throwaway entries, `auto_commit=False`
  + explicit lock/commit/discard, no `copy-config`/`delete-config` in v1.

## Running it

```bash
curl -sL https://containerlab.dev/setup | sudo -E bash -s install-containerlab
sudo -E containerlab deploy -t srl.clab.yml     # from this directory
uv run python -m tests.srl.lab                   # block until NETCONF answers
uv run yang-downloader                           # -> temp/yang_modules/srl
mapfile -t F < <(uv run python -m yang2sdk.cli.closure \
  srl_nokia-system srl_nokia-interfaces --yang-dir temp/yang_modules/srl --format files)
uv run yang2netconf "${F[@]}" --device srl \
  --yang-dir temp/yang_modules/srl --output-dir temp/netconf_clients/srl
NOTCONF_RUN_INTEGRATION=1 uv run pytest tests/test_srl_netconf.py -v
sudo -E containerlab destroy -t srl.clab.yml --cleanup
```

`python -m tests.srl.lab` is also the readiness probe CI uses; it exits
non-zero on timeout so a node that never boots fails loudly instead of
silently skipping every test.

## In CI

`.github/workflows/ci-srl.yaml` runs this lab on every PR, non-blocking. SR
Linux is the only device in the repo with a real NMDA implementation, so it
covers the `has_nmda` → `<get-data>`/`<edit-data>` path that no `notconf`
image can. Deploy/readiness/generate are blocking; pytest is not. See the
README's "SR Linux job" section for the severity table.

Host requirement: SR Linux's XDP datapath needs **SSSE3**, and containerlab
aborts without it. Emulated CPU models under QEMU/Proxmox often lack it.
