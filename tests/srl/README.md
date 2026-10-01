# SR Linux lab (LAB ONLY)

Single-node containerlab lab for testing the generated NETCONF SDK
against real SR Linux instead of the `notconf` simulator.

- Topology: `srl.clab.yml` (pinned `ghcr.io/nokia/srlinux:25.10.1`,
  `ixr-d2l`, `127.0.0.1:1830 -> 830`).
- `srl-startup.cli` adds **only** the AAA `yang2sdk-test` NETCONF role.
  SSH (`mgmt-netconf:830`) + `netconf-server mgmt` come from containerlab
  defaults (`nodes/srl/version_configs/netconf.cfg`), so they are not
  duplicated here.
- Credentials: `admin / NokiaSrl1!` (defaults). Copy
  `.env.srl.example` to `.env` for local runs; never commit `.env`.
- YANG source for v1: offline `nokia/srlinux-yang-models` tag matching
  the image, plus live `get-schema` diff (see righteous plan).
- Safety: depth-bounded reads only, throwaway entries, `auto_commit=False`
  + explicit lock/commit/discard, no `copy-config`/`delete-config` in v1.
