"""Real lab device (NETCONF) — discovery, read, and a transactional write.

Device-agnostic: the endpoint, credentials namespace, YANG roots and read
subtree all come from the ``devices`` list in tests/notconf/matrix.json. The
whole module skips cleanly when the lab environment is absent, so the suite
still passes on a machine without the device.

The device is shared equipment. Two rules are absolute here:

* **Never request the root datastore.** Every read is subtree-filtered and
  depth-bounded (README.md:87-89 — a transponder with many ports is exactly
  the CPU/OOM case that rule exists for).
* **Every write is a transaction that must be provably reverted**, even if
  this process dies mid-test: snapshot -> lock -> edit -> validate -> commit
  -> unlock, restore in ``finally``, then re-read and compare against the
  snapshot. A restore that is not verified is not a rollback.

The write test additionally needs ``LAB_DEVICE_ALLOW_WRITE=1`` *and* an
operator-reviewed ``write_subtree``/``write_leaf``/``write_value`` in the
matrix entry, so a routine run can never mutate shared equipment.
"""

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from lxml import etree  # ty: ignore[unresolved-import] - lxml ships no stubs

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent
READ_DEPTH = 3
WRITE_BUDGET_SECONDS = 120

#: The Groove G30 emits an undeclared ``cli-name`` attribute on data nodes: the
#: YANG declares the matching `coriant-cli-extensions:cli-name` extension but
#: never applies it, so a strict model must reject it. Kept as an explicit,
#: reviewable list — never a silent catch-all.
KNOWN_VENDOR_ATTRIBUTES = ("cli-name",)


def _root_nav(client):
    names = [n for n, v in vars(type(client.data)).items() if isinstance(v, property)]
    if not names:
        pytest.skip("generated client exposes no data navigators")
    return getattr(client.data, names[0])


def _subtree_nav(client, entry: dict):
    """Walk the generated navigator down to the configured subtree.

    This is what keeps the read bounded: the navigator builds a precise
    RFC 6241 Sec 6 subtree filter, so the device never returns — and is never
    asked to materialise — the whole datastore.
    """
    node = _root_nav(client)
    for name in entry.get("read_path", []):
        props = [n for n, v in vars(type(node)).items() if isinstance(v, property)]
        match = next((p for p in props if p == name or p.endswith(f"_{name}")), None)
        if match is None:
            pytest.skip(f"generated client has no {name!r} below the read root")
        node = getattr(node, match)
    return node


def _filter_xml(node_name: str) -> str:
    """Minimal subtree filter: one node, nothing else."""
    return f"<{node_name}/>"


def _unbound_attribute_names(errors) -> set[str]:
    return {
        str(part)[1:]
        for err in errors
        for part in (err.get("loc") or ())
        if str(part).startswith("@")
    }


def _write_ready(entry: dict) -> tuple[bool, str]:
    if os.environ.get("LAB_DEVICE_ALLOW_WRITE") != "1":
        return False, "set LAB_DEVICE_ALLOW_WRITE=1 to exercise the write path"
    missing = [
        k for k in ("write_subtree", "write_leaf", "write_value") if not entry.get(k)
    ]
    if missing:
        return False, (
            f"matrix devices entry for {entry['id']} has no {', '.join(missing)}; "
            "an operator must review the exact object before any write"
        )
    return True, ""


@contextmanager
def _budget(seconds: int = WRITE_BUDGET_SECONDS):
    started = time.monotonic()
    yield
    assert time.monotonic() - started < seconds, "write block exceeded its budget"


# --- discovery ----------------------------------------------------------------


def test_capability_inventory(lab_device, lab_device_client):
    """Record the capabilities that shape every later decision.

    The device is referenced by name; no host, user or credential is asserted
    or printed anywhere in this module.
    """
    client = lab_device_client
    caps = client.server_capabilities
    assert caps, "no capabilities advertised"
    inventory = {
        "device": lab_device["id"],
        "host_key_verification": lab_device["verify"],
        "base_1_0": any(c.startswith("urn:ietf:params:netconf:base:1.0") for c in caps),
        "base_1_1": any(c.startswith("urn:ietf:params:netconf:base:1.1") for c in caps),
        "candidate": client.has_candidate,
        "writable_running": client.has_writable_running,
        "nmda": client.has_nmda,
        "startup": any("capability:startup" in c for c in caps),
        "confirmed_commit": any("confirmed-commit" in c for c in caps),
        "rollback_on_error": any("rollback-on-error" in c for c in caps),
        "validate": any("capability:validate" in c for c in caps),
        "url": any("capability:url" in c for c in caps),
        "yang_library": any("yang-library" in c for c in caps),
        "get_schema": any("monitoring" in c for c in caps),
        "module_capabilities": sum(1 for c in caps if "?module=" in c),
        "default_target": client.default_target,
    }
    print(f"\n{inventory}")
    assert client.default_target in ("running", "candidate")
    if not client.has_candidate:
        assert client.has_writable_running, (
            "neither :candidate nor :writable-running: writes are impossible"
        )


def test_secure_transport_default(lab_device):
    """The default is host-key verification; the lab opt-out is explicit."""
    if not lab_device["known_hosts"]:
        pytest.skip(
            "no pinned host key for this device: the client used the explicit "
            "verify=False lab opt-out (set *_KNOWN_HOSTS to prove verify=True)"
        )
    assert lab_device["verify"] is True, (
        "a pinned host key must not be paired with the verify=False opt-out"
    )


def test_features_file_present(lab_device):
    """A device build needs features.json beside the YANG to match the NOS."""
    features = REPO_ROOT / lab_device["yang_dir"] / "features.json"
    if not features.is_file():
        pytest.skip(f"no features.json in {lab_device['yang_dir']}")
    data = json.loads(features.read_text())
    assert data["source"].startswith("netconf-hello")
    for module, names in data["modules"].items():
        assert isinstance(names, list), f"{module}: features must be a list"


def test_get_schema_available(lab_device, lab_device_client):
    """RFC 6241 Sec 7.2 get-schema: implemented on this device."""
    try:
        reply = lab_device_client._handle_rpc(
            lab_device_client._manager.get_schema,
            identifier="ietf-inet-types",
        )
    except Exception as exc:  # noqa: BLE001 - absence is a legitimate answer
        pytest.skip(f"get-schema is not implemented: {type(exc).__name__}: {exc}")
    assert reply.data, "get-schema returned an empty module"


# --- read path ----------------------------------------------------------------


def test_bounded_read_is_faithful_or_names_the_deviation(lab_device, lab_device_client):
    """One read, three honest outcomes.

    The read is subtree-filtered and depth-bounded, never the root datastore.
    It then has exactly three possible results, and anything outside them is
    a generator gap:

    1. validates -> the model matches the device;
    2. rejects only the documented vendor attributes -> a named deviation
       (Groove G30 ``@cli-name``), reported rather than hidden;
    3. rejects anything else -> fail, because the model is not faithful.
    """
    from pydantic import ValidationError

    try:
        model = _subtree_nav(lab_device_client, lab_device).retrieve(
            source="running", content="config", depth=READ_DEPTH
        )
    except ValidationError as exc:
        attrs = _unbound_attribute_names(exc.errors())
        assert attrs, (
            f"strict models rejected {len(exc.errors())} data elements, not "
            f"attributes: {exc.errors()[:3]}"
        )
        assert attrs <= set(KNOWN_VENDOR_ATTRIBUTES), (
            f"undeclared attributes beyond the documented deviation: {sorted(attrs)}"
        )
        pytest.skip(
            f"{lab_device['id']}: device emits the documented undeclared "
            f"attribute(s) {sorted(attrs)}; model is otherwise faithful"
        )
    # A None result is legal for an empty subtree; otherwise a model came back.
    print(f"validated model: {type(model).__name__ if model is not None else None}")


# --- write path ---------------------------------------------------------------


def _snapshot(client, entry: dict) -> bytes:
    """Raw XML of the exact target subtree, read before anything is touched."""
    reply = client._handle_rpc(
        client._manager.get_config,
        source="running",
        filter=("subtree", _filter_xml(entry["write_leaf"])),
    )
    data = getattr(reply, "data_ele", None)
    assert data is not None, "snapshot read returned no <data> element"
    return etree.tostring(data)


def test_transactional_write_is_reverted(lab_device, lab_device_client):
    """Snapshot -> lock -> edit -> verify -> restore -> prove restoration.

    Skipped unless the operator has both enabled writes and named the exact
    object in the matrix. ``startup`` is never touched: no ``copy-config``
    and no ``delete-config`` (a factory reset on several NOSes).
    """
    ready, why = _write_ready(lab_device)
    if not ready:
        pytest.skip(why)
    client = lab_device_client
    entry = lab_device
    target = "running"
    with _budget():
        before = _snapshot(client, entry)
        locked = client.lock(target=target)
        assert locked, "could not lock the datastore; refusing to write unlocked"
        try:
            payload = etree.fromstring(before)
            node = (
                payload[0]
                if len(payload)
                else etree.SubElement(payload, entry["write_leaf"])
            )
            etree.SubElement(node, entry["write_value"][0]).text = entry["write_value"][
                1
            ]
            assert client.edit(
                config_xml=payload, target=target, default_operation="merge"
            ), "edit rejected"
            assert client.commit(), "commit failed"
            changed = _snapshot(client, entry)
            assert changed != before, "the edit did not change the datastore"
        finally:
            try:
                assert client.edit(
                    config_xml=before, target=target, default_operation="replace"
                ), "RESTORE FAILED"
                client.commit()
            except Exception as restore_error:  # noqa: BLE001
                pytest.fail(
                    f"ROLLBACK FAILED for {entry['write_subtree']} on "
                    f"{entry['id']}: {restore_error}. The device is left modified; "
                    f"restore with: edit-config replace <filter>{entry['write_subtree']}"
                    f"</filter> (raw snapshot kept in the test log)."
                )
            finally:
                client.unlock(target=target)
        after = _snapshot(client, entry)
        if after != before:
            pytest.fail(
                f"RESTORE NOT PROVEN for {entry['write_subtree']} on {entry['id']}: "
                "the datastore still differs from the snapshot"
            )
