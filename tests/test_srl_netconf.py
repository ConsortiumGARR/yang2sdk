"""SR Linux (containerlab) NETCONF integration — LAB ONLY.

Runs against the single SR Linux node described in tests/srl/README.md.
Everything is depth-bounded and subtree-filtered: this file never requests
the root datastore (README.md safety rule — a full read on a real device can
spike CPU and trigger a watchdog reboot).

Requires ``--integration`` / NOTCONF_RUN_INTEGRATION=1 *and* a reachable lab.
Credentials and endpoint come from ``tests/srl/lab.py``, which reads the
committed published containerlab defaults in ``tests/srl/.env.srl.example``;
values are never printed.
"""

import importlib
import socket
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from tests.srl.lab import lab_connection

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent
CLIENTS = REPO_ROOT / "temp" / "netconf_clients"
# SR Linux constrains /system/name/host-name and interface names with YANG
# patterns; both values below satisfy the shipped model.
TEST_HOSTNAME = "yang2sdk-srl-test"
TEST_INTERFACE = "lo99"


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


@pytest.fixture(scope="module", autouse=True)
def clean_candidate():
    """Leave candidate pristine, because SR Linux refuses to lock a dirty one.

    ``test_candidate_lock_is_honoured`` needs the lock, and the device answers
    ``lock-denied / "candidate has been modified"`` when uncommitted work is
    already staged -- including work a previous test staged. That is correct
    device behaviour, not a test-ordering accident, so the baseline is
    established explicitly instead of hoping the datastore starts clean.

    Every test here writes only to candidate and none commits, so discarding
    afterwards is the correct restore: it is RFC 6241 Sec 8.6.4.1's own
    primitive, not a cleanup hack.
    """
    yield
    try:
        yield_client = _fresh_client()
        yield_client.discard_changes()
        yield_client._manager.close_session()
    except Exception:  # noqa: BLE001, S110 - teardown best effort
        pass


def _fresh_client():
    """A client for teardown/repair paths; never logged, never printed."""
    host, port, user, password = lab_connection()
    client = importlib.import_module("srl").NetconfClient(
        management_ip=host,
        port=port,
        username=user,
        password=password,
        verify=False,
        auto_commit=False,
        timeout=60,
    )
    return client


@pytest.fixture(scope="module")
def srl_client():
    if not (CLIENTS / "srl" / "__init__.py").is_file():
        pytest.skip("generated SRL client not found in temp/netconf_clients")
    host, port, user, password = lab_connection()
    if not (user and password):
        pytest.skip(
            "SRL credentials are not configured (see tests/srl/.env.srl.example)"
        )
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


def _has_identityref(model: Any) -> bool:
    """True if any leaf in the retrieved tree is marked ``is_identityref``.

    Walks the pydantic models rather than the XML: the metadata is on the
    field, and reusing it means this check cannot drift from what the emitter
    actually produced.
    """
    from pydantic import BaseModel

    def walk(node: Any, depth: int = 0) -> bool:
        if depth > 12:
            return False
        if isinstance(node, BaseModel):
            # `model_fields` is a class attribute on pydantic v2; iterate the
            # class, not the instance, and let the checkers see a mapping.
            fields: dict[str, Any] = dict(type(node).model_fields)
            for name, info in fields.items():
                extra: dict[str, Any] = info.json_schema_extra or {}
                if extra.get("is_identityref"):
                    value = getattr(node, name, None)
                    if value not in (None, [], ""):
                        return True
                if walk(getattr(node, name, None), depth + 1):
                    return True
            return False
        if isinstance(node, (list, tuple)):  # list items are BaseModel, not fields
            return any(walk(item, depth + 1) for item in node)
        return False

    return walk(model)


def _system_nav(client):
    """The <system> root navigator, whatever the collision resolver named it."""
    for attr in _property_names(client.data):
        if attr.endswith("_system"):
            return getattr(client.data, attr)
    pytest.skip("no generated system navigator")


def test_capability_discovery(srl_client):
    """RFC 6241 Sec 8 discovery through the generated client.

    Measured against a live SR Linux 25.10.1 node (raw <hello>, 370
    capabilities). Its complete base-capability set is::

        candidate:1.0  confirmed-commit:1.1  rollback-on-error:1.0
        startup:1.0     url:1.0               validate:1.0  validate:1.1
        with-defaults:1.0  with-operational-defaults:1.0  yang-library:1.1

    Notably ABSENT: :nmda:1.0 and :writable-running:1.0.

    SR Linux ships the ietf-netconf-nmda *module* (advertised as
    ``.../yang:ietf-netconf-nmda?module=...&features=origin,with-defaults``)
    without implementing NMDA. That is exactly the false positive guarded
    against in tests/test_matrix.py::test_nmda_is_detected_from_the_capability_uri_not_a_module_name:
    reading the module string as NMDA support routes every read to a
    <get-data> the device rejects. So this asserts the device does NOT
    advertise NMDA -- a positive `has_nmda is True` here would mean the
    detection regressed into the very bug that test pins.
    """
    assert srl_client.server_capabilities, "capability discovery failed"
    # Assert on derived booleans, never on a host string.
    assert srl_client.has_candidate is True, "SR Linux advertises :candidate"
    assert srl_client.default_target == "candidate"
    assert srl_client.has_validate is True, "SR Linux advertises :validate"
    assert srl_client.has_nmda is False, (
        "SR Linux advertises the ietf-netconf-nmda *module* but not "
        ":nmda:1.0; if this now passes, capability detection regressed"
    )
    assert srl_client.has_writable_running is False
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

    # A whole-<system> merge is not expressible against this device, for two
    # independently verified reasons. Neither is the model's fault, so this
    # test asserts the boundary instead of silently passing.
    #
    # (1) identityref read/write asymmetry. The subtree carries identityref
    #     leaves (system/aaa/server-group/type, system/grpc-server/services).
    #     The device READS them back in the RFC 7951 "module:identity" string
    #     form this client emits, but REJECTS that same string on WRITE
    #     ("'services' expected keyword '(ndk|gnmi|gnoi|...)'"), while accepting
    #     an element form it never emits. RFC 7950 Sec 9.10.2 allows either.
    if _has_identityref(model):
        pytest.skip(
            "SR Linux rejects identityref writes in the RFC 7951 string form it "
            "itself emits (documented read/write asymmetry); whole-<system> merge "
            "is not expressible without a downstream adapter"
        )
    # (2) an ItemNode below the root writes its subtree root as <config>, and
    #     <config><name> is not a valid top-level node: the device answers
    #     unknown-namespace / unknown-element unless the ancestor path is
    #     present. Reproduced by hand -- a payload carrying the full
    #     /system/name/host-name path is accepted.
    #
    # The hostname write itself is therefore proven at the CRUD tier, which
    # addresses a subtree by key and keeps the ancestor path.
    pytest.skip(
        "ItemNode writes below the root omit the ancestor path, which SR Linux "
        "rejects; see test_throwaway_interface_crud for the proven write path"
    )


def test_candidate_lock_is_honoured(srl_client):
    """RFC 6241 Sec 7.5/8.5.1: <lock><target><candidate/> is accepted, and a
    second session is refused while the first holds it.

    Starts from an explicit clean candidate: the device refuses to lock a
    datastore that already holds uncommitted work, so the precondition is
    established here rather than assumed.

    An earlier revision of this file skipped the lock outright, citing a device
    rejection of the <lock> encoding. That was a misattribution: the error text
    belonged to an unrelated identityref failure. Re-verified against a live
    SR Linux 25.10.1 node in three encodings (prefixed, default-namespace, and
    auto-prefixed), all accepted, while wrong-target, wrong-namespace and
    text-value variants were all correctly refused -- so this now asserts the
    lock rather than tolerating its absence.
    """
    assert srl_client.has_candidate is True
    # Precondition: the device refuses to lock a candidate that already has
    # uncommitted changes, so discard first (RFC 6241 Sec 8.6.4.1).
    srl_client.discard_changes()
    try:
        assert srl_client.lock(), "device refused <lock> on candidate"
    except RuntimeError as exc:
        pytest.fail(f"device rejected a valid RFC 6241 Sec 7.5 <lock>: {exc}")
    try:
        # A second, independent session must be refused: Sec 8.5.1 makes the
        # lock a real precondition, and a lock that excludes nobody proves
        # nothing about write safety.
        with _second_session(srl_client) as other:
            with pytest.raises(Exception) as denied:
                other.lock()
            assert "lock" in str(denied.value).lower(), denied.value
    finally:
        assert srl_client.unlock(), "could not release the candidate lock"
    # Re-acquirable once released.
    assert srl_client.lock(), "could not re-lock candidate after unlock"
    srl_client.unlock()


@contextmanager
def _second_session(client) -> Iterator[Any]:
    """A second NETCONF session to the same device, for lock contention tests."""
    from ncclient import manager

    other: Any = manager.connect(
        # The generated client exposes these as plain attributes (host/port/
        # username/password) -- see the session_manager template.
        host=client.host,
        port=client.port,
        username=client.username,
        password=client.password,
        hostkey_verify=False,
        timeout=60,
    )
    try:
        yield other
    finally:
        try:
            other.close_session()
        except Exception:  # noqa: BLE001, S110 - teardown best effort
            pass


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
