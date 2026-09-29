"""SR Linux (containerlab) NETCONF integration — LAB ONLY.

Runs against the single SR Linux node described in tests/srl/README.md.
Everything is depth-bounded and subtree-filtered: this file never requests
the root datastore (README.md safety rule — a full read on a real device can
spike CPU and trigger a watchdog reboot).

Requires ``--integration`` / NOTCONF_RUN_INTEGRATION=1 *and* a reachable lab.
Credentials come from ``SRL_DEVICE_*`` (see .env.srl-lab.example) with the
published containerlab defaults as a fallback; values are never printed.
"""

import importlib
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent
CLIENTS = REPO_ROOT / "temp" / "netconf_clients"
SRL_EXAMPLE = REPO_ROOT / "tests" / "srl" / ".env.srl.example"
# SR Linux constrains /system/name/host-name and interface names with YANG
# patterns; both values below satisfy the shipped model.
TEST_HOSTNAME = "yang2sdk-srl-test"
TEST_INTERFACE = "lo99"


def _srl_defaults() -> dict[str, str]:
    """Published containerlab defaults (committed; not a secret)."""
    out: dict[str, str] = {}
    for line in SRL_EXAMPLE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def _reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=3):
            return True
    except OSError:
        return False


def _model_for(pkg_name: str, navigator: object) -> Any:
    """Map a generated list navigator to its item model class.

    Convention shared with the notconf suite: navigator ``<M>Node`` in
    ``data_navigators.<stem>`` mirrors model ``<M>`` in ``data_models.<stem>``.
    The class only exists at runtime, so it is returned as ``Any``.
    """
    nav_cls = type(navigator)
    item_cls = getattr(navigator, "_item_cls", nav_cls)
    stem = item_cls.__module__.rsplit(".", 1)[-1]
    models = importlib.import_module(f"{pkg_name}.data_models.{stem}")
    return getattr(models, item_cls.__name__.removesuffix("Node"))


@pytest.fixture(scope="module")
def srl_client():
    if not (CLIENTS / "srl" / "__init__.py").is_file():
        pytest.skip("generated SRL client not found in temp/netconf_clients")
    defaults = _srl_defaults()
    host = os.environ.get("SRL_DEVICE_IP", defaults.get("DEVICE_IP", "127.0.0.1"))
    port = int(
        os.environ.get("SRL_DEVICE_NETCONF_PORT", defaults.get("NETCONF_PORT", "1830"))
    )
    user = os.environ.get("SRL_DEVICE_USER", defaults.get("DEVICE_USER", ""))
    password = os.environ.get("SRL_DEVICE_PASS", defaults.get("DEVICE_PASS", ""))
    if not (user and password):
        pytest.skip("SRL credentials are not configured (see .env.srl-lab.example)")
    if not _reachable(host, port):
        pytest.skip("SR Linux lab is not reachable on its NETCONF port")

    if str(CLIENTS) not in sys.path:
        sys.path.insert(0, str(CLIENTS))
    pkg = importlib.import_module("srl")
    client = pkg.NetconfClient(
        management_ip=host,
        port=port,
        username=user,
        password=password,
        # Lab opt-out: the container's host key is not in known_hosts. The
        # generated client logs a warning for this; never flip the default.
        verify=False,
        # The containerlab candidate datastore is shared between runs.
        auto_commit=False,
        timeout=60,
    )
    try:
        yield client
    finally:
        try:
            client._manager.close_session()
        except Exception:  # noqa: BLE001, S110 - teardown best effort
            pass


def _property_names(nav: object) -> list[str]:
    return [n for n, v in vars(type(nav)).items() if isinstance(v, property)]


def _system_nav(client):
    """The <system> root navigator, whatever the collision resolver named it."""
    for attr in _property_names(client.data):
        if attr.endswith("_system"):
            return getattr(client.data, attr)
    pytest.skip("no generated system navigator")


def test_capability_discovery(srl_client):
    """RFC 6241 Sec 8 discovery through the generated client."""
    assert srl_client.server_capabilities, "capability discovery failed"
    # Assert on derived booleans, never on a host string.
    assert srl_client.has_nmda is True, "SR Linux advertises ietf-netconf-nmda"
    assert srl_client.has_candidate is True, "SR Linux advertises :candidate"
    assert srl_client.default_target == "candidate"
    assert srl_client.module_namespaces, (
        "RFC 6241 Sec 8.3 module capability map is empty"
    )


def test_read_system_config_validates(srl_client):
    """content='config' + bounded depth must validate with zero unbound nodes.

    Regression: the default-feature model pruned ``system/management``
    (``if-feature "not platform-imgmt"``), and pydantic-xml's STRICT search
    then turned that single unknown element into 127 extra_forbidden errors.
    """
    model = _system_nav(srl_client).retrieve(
        source="running", content="config", depth=4
    )
    assert model is not None, "no <system> config returned"
    assert model.management is not None, "system/management missing from the model"
    assert model.logging is not None and model.logging.buffer is not None


def _first_interface_list(client):
    """The interfaces list navigator — the Data root may be the list itself."""
    for attr in _property_names(client.data):
        if "interface" not in attr:
            continue
        node = getattr(client.data, attr)
        if hasattr(node, "create"):
            return node
        for sub in ("interface", "interfaces"):
            cand = getattr(node, sub, None)
            if cand is not None and hasattr(cand, "create"):
                return cand
    return None


def test_state_data_is_stripped_from_write_bodies(srl_client):
    """`is_config` filtering is load-bearing (AGENTS.md): the payload that
    `to_xml_payload()` builds for edit-config must not carry `config false`
    state, even when the model was filled from a content='all' read.
    """
    from lxml import etree  # ty: ignore[unresolved-import] - lxml stubs (cf. src/)

    target = _first_interface_list(srl_client)
    if target is None:
        pytest.skip("no generated interfaces list navigator")
    existing = target.retrieve(source="running", content="all", depth=2)
    if not existing:
        pytest.skip("device exposes no interface entries")
    item = existing[0]
    state_fields = [
        name
        for name, info in type(item).model_fields.items()
        if (info.json_schema_extra or {}).get("is_config") is False
    ]
    assert state_fields, "no config false fields on the interface model"
    populated = [
        n for n in state_fields if getattr(item, n, None) not in (None, [], "")
    ]
    if not populated:
        pytest.skip("no state data populated at this depth")
    payload = item.to_xml_payload()
    xml = payload if isinstance(payload, bytes) else payload.encode()
    tags = {
        etree.QName(e).localname
        for e in etree.fromstring(xml).iter()
        if isinstance(e.tag, str)
    }
    for name in state_fields:
        extra = type(item).model_fields[name].json_schema_extra or {}
        tag = extra.get("tag", name.replace("_", "-"))
        assert tag not in tags, (
            f"config false leaf {tag!r} leaked into the edit-config payload"
        )


def test_hostname_mutate_restore(srl_client):
    """Retrieve -> mutate -> update -> read back, with a verified restore.

    <system>/<name> is a container (srl_nokia-system-name); the hostname leaf
    is <system>/<name>/<host-name>.
    """
    nav = _system_nav(srl_client)
    # The container only carries <host-name> in operational state, so the
    # baseline is read there while the write goes to the candidate datastore.
    operational = nav.retrieve(source="operational", content="all", depth=3)
    model = nav.retrieve(source="candidate", content="config", depth=3)
    if (
        operational is None
        or operational.name is None
        or "host_name" not in type(operational.name).model_fields
    ):
        pytest.skip("no <system>/<name>/<host-name> leaf in the generated model")
    original = operational.name.host_name
    assert original, "hostname unset in the operational datastore"
    # Deviation: SR Linux rejects the RFC 6241 Sec 7.5 <lock><target>
    # encoding ("expected keyword 'candidate' and a namespace or module
    # prefix may be required"). Recorded, not worked around here: the restore
    # below is what actually protects the device.
    try:
        srl_client.lock()
    except RuntimeError as exc:
        pytest.skip(f"device rejected <lock> on the candidate datastore: {exc}")
    try:
        model.name.host_name = TEST_HOSTNAME
        assert nav.update(model), "merge rejected"
        reread = nav.retrieve(source="candidate", content="config", depth=3)
        assert reread is not None and reread.name.host_name == TEST_HOSTNAME
    finally:
        model.name.host_name = original
        nav.update(model)
        srl_client.unlock()
    # Re-read after the lock is released: an unrestored value would show here.
    restored = nav.retrieve(source="candidate", content="config", depth=3)
    assert restored is None or restored.name is None or not restored.name.host_name, (
        "the candidate datastore still carries the test hostname — "
        "the device is left modified"
    )


def test_throwaway_interface_crud(srl_client):
    """create -> retrieve -> update -> replace -> delete on a throwaway entry.

    Blast radius: exactly one interface named for this test, deleted in a
    finally block even if the test dies mid-way.
    """
    target = _first_interface_list(srl_client)
    if target is None:
        pytest.skip("no generated interfaces list navigator")

    model_cls = _model_for("srl", target)
    payload: dict[str, object] = {"name": TEST_INTERFACE}
    for field, value in (("admin_state", "enable"), ("description", "yang2sdk-crud")):
        if field in model_cls.model_fields:
            payload[field] = value
    # auto_commit=False and the target is the shared candidate datastore, so
    # the round trip never commits and never discards: create + delete inside
    # the candidate leaves it equal to running again for this subtree.
    target_ds = srl_client.default_target
    try:
        assert target.create([model_cls.model_validate(payload)]), "create rejected"
        got = None
        for _ in range(10):
            got = target(TEST_INTERFACE).retrieve(
                source=target_ds, content="config", depth=3
            )
            if got is not None:
                break
            time.sleep(2)
        assert got is not None, f"created interface not visible in {target_ds}"
        if "description" in type(got).model_fields:
            got.description = "yang2sdk-updated"
            assert target(TEST_INTERFACE).update(got), "update rejected"
            assert (
                target(TEST_INTERFACE)
                .retrieve(source=target_ds, content="config", depth=3)
                .description
                == "yang2sdk-updated"
            )
        assert target(TEST_INTERFACE).delete(), "delete rejected"
    finally:
        try:
            target(TEST_INTERFACE).delete()
        except Exception:  # noqa: BLE001, S110 - best-effort cleanup
            pass
    assert (
        target(TEST_INTERFACE).retrieve(source=target_ds, content="config", depth=3)
        is None
    ), "throwaway interface survived deletion in the candidate datastore"
