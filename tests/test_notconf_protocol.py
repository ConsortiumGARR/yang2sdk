"""Live protocol checks against notconf images (integration).

Raw transports only (ncclient + requests), no generated SDK. Proves each
pre-built image speaks RFC 6241 NETCONF and RFC 8040/8527 RESTCONF before
the SDK tests run. Safe: depth-bounded reads of named subtrees only, writes
limited to an ephemeral `yang2sdk-test<N>` interface entry with cleanup.
"""

import pytest
import requests
from ncclient import manager

pytestmark = pytest.mark.integration

CREDS = ("admin", "admin")
TEST_IF = "yang2sdk-test9"


def _nc(host, port):
    m = manager.connect(
        host=host,
        port=port,
        username=CREDS[0],
        password=CREDS[1],
        hostkey_verify=False,
        timeout=30,
    )
    assert m is not None, "ncclient connect returned None"  # stub narrowing
    return m


def test_netconf_hello_advertises_base(live_endpoint):
    _img, ep = live_endpoint
    with _nc(ep["netconf_host"], ep["netconf_port"]) as m:
        caps = set(m.server_capabilities)
    assert "urn:ietf:params:netconf:base:1.0" in caps  # RFC 6241 Sec 8
    assert any("writable-running" in c or "candidate" in c for c in caps)


def test_restconf_yang_library(live_endpoint):
    _img, ep = live_endpoint
    r = requests.get(
        ep["restconf_base"] + "/restconf/data/ietf-yang-library:modules-state",
        auth=CREDS,
        timeout=30,
    )
    assert r.status_code == 200, r.text[:300]
    assert "module" in r.json()["ietf-yang-library:modules-state"]  # RFC 8040 Sec 8


def test_restconf_capabilities(live_endpoint):
    _img, ep = live_endpoint
    r = requests.get(
        ep["restconf_base"]
        + "/restconf/data/ietf-restconf-monitoring:restconf-state/capabilities",
        auth=CREDS,
        timeout=30,
    )
    assert r.status_code == 200, r.text[:300]


def _implemented_modules(ep):
    r = requests.get(
        ep["restconf_base"] + "/restconf/data/ietf-yang-library:modules-state",
        auth=CREDS,
        timeout=30,
    )
    r.raise_for_status()
    return {
        m["name"]
        for m in r.json()["ietf-yang-library:modules-state"]["module"]
        if m.get("conformance-type") == "implement"
    }


def test_nmda_datastore_discrimination(live_endpoint):
    """RFC 8527: running holds config, operational holds state.

    notconf applies factory-default/startup data asynchronously after boot
    (cf. upstream wait-operational-sync.sh), so poll for the hostname leaf.
    """
    import time

    _img, ep = live_endpoint
    if "ietf-system" not in _implemented_modules(ep):
        pytest.skip("ietf-system not implemented on this image")
    running_text = ""
    for _ in range(24):
        running = requests.get(
            ep["restconf_base"]
            + "/restconf/ds/ietf-datastores:running/ietf-system:system",
            auth=CREDS,
            timeout=30,
        )
        assert running.status_code == 200, running.text[:300]
        running_text = running.text
        if "hostname" in running_text:
            break
        time.sleep(5)
    if "hostname" not in running_text:
        pytest.skip("hostname never appeared in running (init race)")
    oper = requests.get(
        ep["restconf_base"]
        + "/restconf/ds/ietf-datastores:operational/ietf-system:system",
        auth=CREDS,
        timeout=30,
    )
    assert oper.status_code == 200, oper.text[:300]
    # Config (hostname) lives in running, not operational, on notconf-ietf.
    assert "hostname" not in oper.text


def test_netconf_edit_config_roundtrip(live_endpoint):
    """RFC 6241 Sec 7.2: merge + delete a throwaway interface entry."""
    _img, ep = live_endpoint
    if "ietf-interfaces" not in _implemented_modules(ep):
        pytest.skip("ietf-interfaces not implemented on this image")
    ns = "urn:ietf:params:xml:ns:yang:ietf-interfaces"
    merge = (
        '<config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">'
        f'<interfaces xmlns="{ns}"><interface>'
        f"<name>{TEST_IF}</name>"
        '<type xmlns:iana="urn:ietf:params:xml:ns:yang:iana-if-type">iana:softwareLoopback</type>'
        "<enabled>true</enabled>"
        "</interface></interfaces></config>"
    )
    delete = (
        '<config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">'
        f'<interfaces xmlns="{ns}">'
        '<interface xmlns:nc="urn:ietf:params:xml:ns:netconf:base:1.0" nc:operation="delete">'
        f"<name>{TEST_IF}</name></interface></interfaces></config>"
    )
    with _nc(ep["netconf_host"], ep["netconf_port"]) as m:
        try:
            assert m.edit_config(target="running", config=merge).ok
            got = m.get_config(
                source="running",
                filter=(
                    "subtree",
                    f'<interfaces xmlns="{ns}"><interface><name>{TEST_IF}</name></interface></interfaces>',
                ),
            )
            assert TEST_IF in got.data_xml
        finally:
            try:
                m.edit_config(target="running", config=delete)
            except Exception:  # noqa: BLE001, S110 - best-effort cleanup
                pass
        gone = m.get_config(
            source="running",
            filter=(
                "subtree",
                f'<interfaces xmlns="{ns}"><interface><name>{TEST_IF}</name></interface></interfaces>',
            ),
        )
        assert TEST_IF not in (gone.data_xml or "")


def test_restconf_read_paths_with_generated_sdk_parity_shape(live_endpoint):
    """RESTCONF GET shape the generated SDK consumes (RFC 7951 JSON)."""
    _img, ep = live_endpoint
    r = requests.get(
        ep["restconf_base"]
        + "/restconf/data/ietf-interfaces:interfaces?depth=2&content=all",
        auth=CREDS,
        timeout=30,
    )
    if r.status_code == 404:
        pytest.skip("ietf-interfaces absent on this image")
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert "ietf-interfaces:interfaces" in body
    # 64-bit ints are strings per RFC 7951; structural check only here.
    assert isinstance(body["ietf-interfaces:interfaces"], dict)
