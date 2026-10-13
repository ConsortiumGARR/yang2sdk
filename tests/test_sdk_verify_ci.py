"""Run `sdk-verify` against every notconf image — LAB ONLY, CI gate.

`src/yang2sdk/cli/sdk_verify.py` was a manual lab harness in no workflow, so
the defects it finds were invisible: three of them shipped (the RESTCONF
ancestor-envelope unwrap, the NETCONF augment-closure gap, and the
`ietf-notification-capabilities` model hole). This module drives the same
verifier from pytest so it gates a merge.

It reuses the container `live_endpoint` already started and the clients
`generated_sdks` already compiled, so it costs no extra container startup.

Tiers are `read,rpc` only. `crud` writes to the device and needs a second
acknowledgement (`--allow-restconf-writes`, or `--allow-running-writes` on
NETCONF); SR Linux covers the CRUD round-trip under its own job instead.
`sdk-verify` deliberately never automates create/delete -- see `AGENTS.md`.

A device rejection is a skip, not a failure; a model/parse failure is a
failure. `AGENTS.md`: "A skip is not a pass", so a run that checks nothing
must not be reported as green.
"""

from __future__ import annotations

import pytest

from tests.conftest import CREDS, TEMP_CLIENTS

pytestmark = pytest.mark.integration

from yang2sdk.cli.sdk_verify import FAIL, PASS, build_parser, run_protocol


def _run(protocol: str, sdks: dict, monkeypatch: pytest.MonkeyPatch):
    """Run sdk-verify's verifier for one protocol against the live container.

    `run_protocol` reads the endpoint from DEVICE_IP / RESTCONF_PORT /
    NETCONF_PORT (see sdk_verify.py ~L993), so the container's ephemeral ports
    are injected rather than passed.
    """
    ep = sdks["ep"]
    monkeypatch.setenv("DEVICE_IP", "127.0.0.1")
    if protocol == "restconf":
        # The fixture publishes `restconf_base` as a URL, not a bare port.
        base = ep["restconf_base"].split("://", 1)[1]
        monkeypatch.setenv("RESTCONF_PORT", base.rsplit(":", 1)[1])
    else:
        monkeypatch.setenv("NETCONF_PORT", str(ep["netconf_port"]))

    client_dir = TEMP_CLIENTS / f"{sdks['slug']}_{protocol}"
    argv = [
        "--device",
        sdks["slug"],
        "--protocol",
        protocol,
        "--client-dir",
        str(client_dir),
        "--tiers",
        "read,rpc",
        "--username",
        CREDS[0],
        "--password",
        CREDS[1],
        # The simulator presents a self-signed cert; this is lab gear only and
        # never a production default (see AGENTS.md, secure transport defaults).
        "--no-verify-tls",
    ]
    if protocol == "restconf":
        argv += ["--restconf-scheme", "http"]
    args = build_parser().parse_args(argv)
    return run_protocol(protocol, args, {"read", "rpc"}, CREDS[0], CREDS[1])


def _fails(report) -> list[str]:
    return [
        f"{r.node} [{r.method}]: {r.detail}" for r in report.results if r.status == FAIL
    ]


@pytest.mark.parametrize("protocol", ["restconf", "netconf"])
def test_sdk_verify_reads_and_rpcs_every_node(
    live_endpoint, generated_sdks, monkeypatch: pytest.MonkeyPatch, protocol
):
    """Walk every reachable node and serialise every RPC input on each image."""
    image, _ep = live_endpoint
    report = _run(protocol, generated_sdks, monkeypatch)
    counts = report.counts()

    assert not _fails(report), (
        f"sdk-verify FAIL on {image['package']}:{image['tag']} ({protocol})\n"
        + "\n".join(_fails(report))
    )
    # "A skip is not a pass": an image where nothing was actually checked must
    # not pass vacuously.
    assert counts[PASS] > 0, (
        f"sdk-verify checked nothing on {image['package']}:{image['tag']} "
        f"({protocol}) — {counts}"
    )
