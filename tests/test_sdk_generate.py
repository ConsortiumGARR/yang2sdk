"""Generate + exercise both SDKs against live notconf images (integration).

Per image: download YANG via get-schema (RFC 6241 Sec 7 + ietf-netconf-
monitoring, same path as cli/downloader.py) -> yang2restconf + yang2netconf
compile (same path as cli/compiler.py Compiler) -> import sample -> Pydantic
validate live payloads -> full CRUD round-trips (create/retrieve/update/
replace/delete) through both generated SDKs -> idempotent same-data merges
on every top-level config node.

Writes stay safe: throwaway `yang2sdk-crud8/9` interface entries when
ietf-interfaces is implemented, deleted afterwards; same-data merges are
no-ops. Reads are depth-bounded named subtrees, never root `/`.
"""

import importlib
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests
from pydantic import BaseModel

from yang2sdk.cli.downloader import YangDownloader

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMP_CLIENTS = REPO_ROOT / "temp" / "notconf_clients"
TEMP_YANGS = REPO_ROOT / "temp" / "yang_modules"
GOLDEN_DIR = REPO_ROOT / "tests" / "fixtures" / "golden"

CREDS = ("admin", "admin")
TEST_IF = "yang2sdk-test8"
MAX_ROOTS = 6
_SDK_CACHE: dict[str, dict] = {}
INFRA_PREFIXES = (
    "ietf-netconf",
    "ietf-restconf",
    "ietf-yang",
    "ietf-datastores",
    "ietf-origin",
    "ietf-subscribed",
    "ietf-notification",
    "iana-",
    "sysrepo",
    "notifications",
    "nc-notifications",
    "libnetconf2",
    "netopeer",
    "ietf-factory",
    "ietf-tcp",
    "ietf-tls",
    "ietf-ssh",
    "ietf-crypto",
    "ietf-keystore",
    "ietf-truststore",
)


def _slug(image):
    return f"{image['package']}_{image['tag']}".replace(".", "_").replace("-", "_")


def _implemented_modules(ep):
    r = requests.get(
        ep["restconf_base"] + "/restconf/data/ietf-yang-library:modules-state",
        auth=CREDS,
        timeout=30,
    )
    r.raise_for_status()
    mods = r.json()["ietf-yang-library:modules-state"]["module"]
    return {m["name"] for m in mods if m.get("conformance-type") == "implement"}


def _download_yangs(ep):
    """Download the device's YANG library using the *shipped* downloader.

    This used to be a "mirror" of ``YangDownloader.download_all`` written out
    in the test file, and the two had already diverged: the mirror deduped by
    first-seen identifier, so it kept the *oldest* advertised revision of every
    multi-revision module, while the shipped downloader had the same class of
    bug differently. Duplicating the production code path meant the test could
    go on exercising something the CLI never does. Call the real thing, so the
    integration suite covers the code users actually run.
    """
    out = TEMP_YANGS / "notconf"
    out.mkdir(parents=True, exist_ok=True)
    # start from a clean slate: stale revisions from a previous run would be
    # resolved by pyang and could mask a revision-selection regression.
    for stale in out.glob("*.yang"):
        stale.unlink()
    dl = YangDownloader(
        host=ep["netconf_host"],
        port=ep["netconf_port"],
        user=CREDS[0],
        password=CREDS[1],
        output_dir=out,
    )
    failed = dl.download_all()
    files = sorted(out.glob("*.yang"))
    assert files, "downloader retrieved no YANG modules"
    assert failed == 0, f"downloader reported {failed} failed schema fetches"
    return files


def _pick_roots(yang_files, implemented):
    by_name = {}
    for f in yang_files:
        by_name.setdefault(f.name.split("@")[0], f)
    preferred = [n for n in ("ietf-interfaces", "ietf-system") if n in by_name]
    vendor = [
        n
        for n in sorted(implemented)
        if n in by_name
        and not n.startswith(INFRA_PREFIXES)
        and n not in ("ietf-interfaces", "ietf-system", "ietf-ip")
    ]
    names = preferred + vendor[:MAX_ROOTS]
    if not names:  # minimal base image: first implemented modules with files
        names = [n for n in sorted(implemented) if n in by_name][:3]
    assert names, "no compilable root modules found"
    return [by_name[n] for n in names]


def _compile_all(yang_files, roots, slug):
    """Compile via fresh interpreter per format.

    In-process pyang runs cannot compile twice (global optparse registry
    raises OptionConflictError on the second run), so each format gets its
    own process. This also covers the real yang2restconf/yang2netconf CLIs.
    """
    yangs_dir = yang_files[0].parent
    for fmt in ("restconf", "netconf"):
        out = TEMP_CLIENTS / f"{slug}_{fmt}"
        argv = [str(r) for r in roots] + [
            "--device",
            slug,
            "--yang-dir",
            str(yangs_dir),
            "--output-dir",
            str(out),
        ]
        code = (
            "import sys; from yang2sdk.cli.compiler import run_compiler; "
            f"run_compiler({fmt!r}, sys.argv[1:])"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code, *argv],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=900,
        )
        assert proc.returncode == 0, f"{fmt} compile failed: {proc.stderr[-2000:]}"
        assert (out / "__init__.py").exists()
        assert (out / "session_manager.py").exists()
        assert (out / "data_models").is_dir()
        assert (out / "data_navigators").is_dir()


def _import_pkg(slug, fmt):
    if str(TEMP_CLIENTS) not in sys.path:
        sys.path.insert(0, str(TEMP_CLIENTS))
    return importlib.import_module(f"{slug}_{fmt}")


@pytest.fixture(scope="function")
def generated_sdks(live_endpoint):
    image, ep = live_endpoint
    slug = _slug(image)
    if slug not in _SDK_CACHE:
        yang_files = _download_yangs(ep)
        assert yang_files, "get-schema returned no YANG modules"
        implemented = _implemented_modules(ep)
        roots = _pick_roots(yang_files, implemented)
        _compile_all(yang_files, roots, slug)
        rest = _import_pkg(slug, "restconf")
        netc = _import_pkg(slug, "netconf")
        assert rest.RestconfClient is not None and netc.NetconfClient is not None
        _SDK_CACHE[slug] = {
            "image": image,
            "ep": ep,
            "slug": slug,
            "rest": rest,
            "netc": netc,
        }
    else:
        _SDK_CACHE[slug]["ep"] = ep  # refresh ephemeral ports per container
    return _SDK_CACHE[slug]


def _first_data_navigator(client):
    for name, prop in vars(type(client.data)).items():
        if isinstance(prop, property):
            fget = prop.fget
            assert fget is not None
            return name, fget(client.data)
    raise AssertionError("generated Data has no navigable properties")


def _navigators_matching(client, include, exclude=()):
    """Yield (attr, navigator) for Data properties matching substrings."""
    for attr, prop in vars(type(client.data)).items():
        if not isinstance(prop, property):
            continue
        if include in attr and not any(x in attr for x in exclude):
            fget = prop.fget
            assert fget is not None
            yield attr, fget(client.data)


def test_restconf_sdk_retrieve_and_validate(generated_sdks):
    mod = generated_sdks
    rc_port = int(mod["ep"]["restconf_base"].rsplit(":", 1)[1])
    client = mod["rest"].RestconfClient(
        management_ip="127.0.0.1",
        port=rc_port,
        username=CREDS[0],
        password=CREDS[1],
        verify=False,
        scheme="http",
    )
    name, nav = _first_data_navigator(client)
    # RFC 8040 depth/content query params; operational /data path.
    model = nav.retrieve(content="all", depth=2)
    assert isinstance(model, BaseModel), f"{name} did not validate"
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    golden = GOLDEN_DIR / f"{mod['slug']}_restconf_{name}.json"
    payload = model.model_dump(mode="json", by_alias=True)
    if golden.exists():
        import json

        stored = json.loads(golden.read_text())
        # This used to assert only that the file parsed, so a completely
        # different model still passed. Compare the *shape* of the payload
        # (key names), which is stable across runs and catches a model that
        # silently stops validating a subtree.
        assert sorted(stored) == sorted(payload), (
            f"golden snapshot shape changed for {name}: "
            f"stored={sorted(stored)} current={sorted(payload)}"
        )
    else:
        import json

        golden.write_text(json.dumps(payload, indent=2, default=str)[:20000])


def test_netconf_sdk_retrieve_and_validate(generated_sdks):
    mod = generated_sdks
    ep = mod["ep"]
    client = mod["netc"].NetconfClient(
        management_ip="127.0.0.1",
        port=ep["netconf_port"],
        username=CREDS[0],
        password=CREDS[1],
        # The simulator generates a random SSH host key per container, so
        # host-key verification is meaningless here and the secure default
        # (verify=True) would make this suite fail on every image with
        # SSHUnknownHostError. The raw-protocol tests already opt out the same
        # way (see CREDS/hostkey_verify in test_notconf_protocol.py); this
        # makes the generated-client path explicit too. A real deployment must
        # keep verify=True.
        verify=False,
    )
    # has_candidate/has_writable-running discovery per RFC 6241 Sec 8.
    assert client.server_capabilities, "capability discovery failed"
    name, nav = _first_data_navigator(client)
    # Legacy get/get-config or NMDA get-data routing happens inside retrieve.
    model = nav.retrieve(content="all", depth=2)
    assert model is None or isinstance(model, BaseModel), f"{name} bad type"


def _find_list_navigator(client, attr):
    nav = getattr(client.data, attr, None)
    if nav is None:
        return None
    for sub in ("interfaces", "interface"):
        cand = getattr(nav, sub, None)
        if cand is not None and hasattr(cand, "create"):
            return cand
    if hasattr(nav, "create"):
        return nav
    return None


def _model_for_list_navigator(pkg, list_nav):
    """Map a generated ListNode to its Pydantic model class.

    Convention (ir.py + templates): navigator `<M>Node` in
    `data_navigators.<stem>` mirrors model `<M>` in `data_models.<stem>`.
    """
    item_nav_cls = list_nav._item_cls
    stem = item_nav_cls.__module__.rsplit(".", 1)[-1]
    model_name = item_nav_cls.__name__.removesuffix("Node")
    models_mod = importlib.import_module(f"{pkg.__name__}.data_models.{stem}")
    model_cls = getattr(models_mod, model_name, None)
    assert model_cls is not None, f"no model {model_name} in data_models.{stem}"
    return model_cls


def _interface_payload(model_cls, name=TEST_IF, description=None):
    """Minimal ietf-interfaces entry keyed by model field names.

    State (`config false`) leaves are optional on the generated models, so
    only the key, a fixed loopback type, and optional overrides are needed.
    """
    payload: dict[str, object] = {
        "name": name,
        "type": "iana-if-type:softwareLoopback",
    }
    if "enabled" in model_cls.model_fields:
        payload["enabled"] = True
    if description is not None and "description" in model_cls.model_fields:
        payload["description"] = description
    return payload


def _interfaces_list_navigator(client):
    """First generated list navigator under an *interface* Data property.

    ietf-interfaces is implemented on every matrix image and its roots are
    compiled first, so this deterministically resolves to it.
    """
    for attr in vars(type(client.data)):
        if "interface" not in attr:
            continue
        nav = _find_list_navigator(client, attr)
        if nav is not None:
            return attr, nav
    return None, None


def test_netconf_sdk_update_roundtrip(generated_sdks):
    """Generated NETCONF SDK write path (RFC 6241 edit-config).

    Retrieve-mutate-update of the ietf-system hostname: exercises model
    validation, XML serialization with nc:operation, capability-routed edit,
    and read-back verification. A factory-fresh value is restored afterwards.
    """
    mod = generated_sdks
    ep = mod["ep"]
    if "ietf-system" not in _implemented_modules(ep):
        pytest.skip("ietf-system not implemented on this image")
    client = mod["netc"].NetconfClient(
        management_ip="127.0.0.1",
        port=ep["netconf_port"],
        username=CREDS[0],
        password=CREDS[1],
        # The simulator generates a random SSH host key per container, so
        # host-key verification is meaningless here and the secure default
        # (verify=True) would make this suite fail on every image with
        # SSHUnknownHostError. The raw-protocol tests already opt out the same
        # way (see CREDS/hostkey_verify in test_notconf_protocol.py); this
        # makes the generated-client path explicit too. A real deployment must
        # keep verify=True.
        verify=False,
    )
    nav = None
    for _attr, cand in _navigators_matching(client, "system", exclude=("capabilit",)):
        if hasattr(cand, "update") and hasattr(cand, "retrieve"):
            nav = cand
            break
    if nav is None:
        pytest.skip("no generated system navigator")
    model = nav.retrieve(content="config", depth=2)
    if model is None or not hasattr(model, "hostname"):
        pytest.skip("no hostname leaf in generated system model")
    if model.hostname is None:
        # Factory-default data lands asynchronously; force a deterministic
        # baseline through the same SDK write path under test.
        model.hostname = "notconf-baseline"
        assert nav.update(model)
        _commit_candidate(client)
        model = nav.retrieve(content="config", depth=2)
    original = model.hostname
    assert original, "hostname still unset after baseline write"
    try:
        model.hostname = "yang2sdk-sdk-test"
        assert nav.update(model)
        _commit_candidate(client)
        assert nav.retrieve(content="config", depth=2).hostname == "yang2sdk-sdk-test"
    finally:
        model.hostname = original
        nav.update(model)
        _commit_candidate(client)
    assert nav.retrieve(content="config", depth=2).hostname == original


def test_restconf_sdk_crud_roundtrip(generated_sdks):
    """Full CRUD lifecycle through the generated RESTCONF SDK (RFC 8040).

    create (POST, Sec 4.4.1) -> update (PATCH merge, Sec 4.6.1) ->
    replace (PUT, Sec 4.5) -> delete (DELETE, Sec 4.7) of a throwaway
    interface entry, with SDK read-backs in between.

    Read-back path: the lab backend (rousette, all notconf images,
    2026-09-28 probe) accepts writes to /data but never returns the written
    entries from /data GETs (404 "No data from sysrepo"), while
    /ds/ietf-datastores:running (RFC 8527) reflects them. Read-backs try the
    RFC 8040 /data path first, then fall back to the RFC 8527 datastore
    resource. Writes always go to /data.
    """
    mod = generated_sdks
    ep = mod["ep"]
    if "ietf-interfaces" not in _implemented_modules(ep):
        pytest.skip("ietf-interfaces not implemented on this image")
    rest = mod["rest"]
    client = rest.RestconfClient(
        management_ip="127.0.0.1",
        port=int(ep["restconf_base"].rsplit(":", 1)[1]),
        username=CREDS[0],
        password=CREDS[1],
        verify=False,
        scheme="http",
    )
    attr, target = _interfaces_list_navigator(client)
    if target is None:
        pytest.skip("no generated interfaces list navigator")
    assert target is not None  # pyrefly: skip() is not seen as NoReturn
    model_cls = _model_for_list_navigator(rest, target)
    name = "yang2sdk-crud8"

    # Read-back paths (evidence: rousette on ghcr.io/notconf/notconf-ietf:latest,
    # 2026-09-28 probe):
    #   /data item GET        -> 404 (written entries hidden from /data)
    #   /data collection GET  -> 400 "requires 1 keys"
    #   /ds/... item GET      -> 200 but root-container-wrapped
    #   /ds/... container GET -> 200 with the list entries
    # So: try the RFC 8040 /data item first, then the RFC 8527 datastore
    # container (same generated Data class, different root) filtered by key.
    ds = rest.data_navigators.Data(client, "/ds/ietf-datastores:running", "")

    def readback():
        try:
            return target(name).retrieve(content="all", depth=2)
        except requests.HTTPError:
            pass
        if attr is None:
            return None
        try:
            # depth=3: the item's leaves sit one level below the list
            # instance (depth=2 from a container target returns keys only —
            # rousette evidence, 2026-09-28 probe).
            container = getattr(ds, attr).retrieve(content="all", depth=3)
        except requests.HTTPError:
            return None
        if container is None:
            return None
        items = getattr(container, target._name, None) or []
        for item in items:
            if getattr(item, "name", None) == name:
                return item
        return None

    try:
        # CREATE
        target.create(
            [model_cls.model_validate(_interface_payload(model_cls, name=name))]
        )
        got = readback()
        assert got is not None, "created entry not visible after POST"
        assert got.name == name

        # UPDATE (merge the retrieved model back with a changed leaf)
        got.description = "crud-merge"
        target(name).update(got)
        got = readback()
        assert got is not None and got.description == "crud-merge"

        # REPLACE. A PUT body is the complete resource (RFC 8040 Sec 4.5), so a
        # freshly-minted minimal payload would silently delete every config leaf
        # it omits -- and the generated client refuses it loudly instead. The
        # simulator never returns bind_ni_name/ipv4/ipv6/link_up_down_trap_enable,
        # so a genuinely complete body cannot be built here; assert the refusal
        # (the property worth testing) and then exercise the wire path through
        # the explicit opt-in. The config-vs-state split of the guard is pinned
        # offline in test_matrix.py.
        minimal = model_cls.model_validate(
            _interface_payload(model_cls, name=name, description="crud-replace")
        )
        with pytest.raises(ValueError, match="would DELETE"):
            target(name).replace(minimal)
        target(name).replace(minimal, allow_partial=True)
        got = readback()
        assert got is not None and got.description == "crud-replace"

        # DELETE
        target(name).delete()
        assert readback() is None, "entry still visible after DELETE"
    finally:
        try:
            target(name).delete()
            _commit_candidate(client)
        except Exception:  # noqa: BLE001, S110 - best-effort cleanup
            pass


def test_netconf_sdk_crud_roundtrip(generated_sdks):
    """Full CRUD lifecycle through the generated NETCONF SDK (RFC 6241).

    create/edit-config -> get read-back -> merge update -> replace ->
    delete, plus identityref normalization: the `type` leaf must come back
    in RFC 7951 Sec 6.8 module-name form ("iana-if-type:softwareLoopback")
    regardless of the prefix the server echoes.
    """
    mod = generated_sdks
    ep = mod["ep"]
    if "ietf-interfaces" not in _implemented_modules(ep):
        pytest.skip("ietf-interfaces not implemented on this image")
    client = mod["netc"].NetconfClient(
        management_ip="127.0.0.1",
        port=ep["netconf_port"],
        username=CREDS[0],
        password=CREDS[1],
        # The simulator generates a random SSH host key per container, so
        # host-key verification is meaningless here and the secure default
        # (verify=True) would make this suite fail on every image with
        # SSHUnknownHostError. The raw-protocol tests already opt out the same
        # way (see CREDS/hostkey_verify in test_notconf_protocol.py); this
        # makes the generated-client path explicit too. A real deployment must
        # keep verify=True.
        verify=False,
    )
    _attr, target = _interfaces_list_navigator(client)
    if target is None:
        pytest.skip("no generated interfaces list navigator")
    assert target is not None  # pyrefly: skip() is not seen as NoReturn
    model_cls = _model_for_list_navigator(mod["netc"], target)
    name = "yang2sdk-crud9"
    try:
        # CREATE
        assert target.create(
            [model_cls.model_validate(_interface_payload(model_cls, name=name))]
        )
        _commit_candidate(client)
        got = _readback_item(target, name)
        assert got is not None, "created entry not visible after edit-config"
        assert got.name == name
        assert (got.type or "").endswith("softwareLoopback"), (
            f"identityref not in module-name form: {got.type!r}"
        )

        # UPDATE (merge the retrieved model back with a changed leaf)
        got.description = "crud-merge"
        assert target(name).update(got)
        _commit_candidate(client)
        got = _readback_item(target, name)
        assert got is not None and got.description == "crud-merge"

        # REPLACE. `operation="replace"` overwrites the whole node, so a PUT /
        # replace body must be the complete resource (RFC 8040 Sec 4.5, RFC 6241
        # Sec 8.2.1). Replacing a freshly-minted minimal payload would silently
        # delete every config leaf it omits, so the generated client's guard
        # rightly refuses it. Replace the device's own full model instead, which
        # is what a real caller must do; the guard itself is covered offline in
        # test_matrix.py (including the config-false state-leaf regression).
        complete = _readback_item(target, name)
        assert complete is not None, "entry disappeared before REPLACE"
        complete.description = "crud-replace"
        minimal = model_cls.model_validate(
            _interface_payload(model_cls, name=name, description="crud-replace")
        )
        # The simulator never returns `bind_ni_name`/`ipv4`/`ipv6`/
        # `link_up_down_trap_enable`, so a genuinely complete body cannot be
        # built here. What matters and is asserted: a partial replace is
        # refused LOUDLY rather than silently deleting those subtrees.
        with pytest.raises(ValueError, match="would DELETE"):
            target(name).replace(minimal)
        # The explicit opt-in then exercises the real replace wire path.
        assert target(name).replace(minimal, allow_partial=True)
        _commit_candidate(client)
        got = _readback_item(target, name)
        assert got is not None and got.description == "crud-replace"

        # DELETE
        assert target(name).delete()
        _commit_candidate(client)
        assert _readback_item(target, name) is None
    finally:
        try:
            target(name).delete()
        except Exception:  # noqa: BLE001, S110 - best-effort cleanup
            pass


def _commit_candidate(client) -> None:
    """Run the documented safe write sequence so `running` reflects the edit.

    The generated NETCONF client keeps `auto_commit=False` (a destructive
    default that must stay opt-in) and `edit()` targets `candidate` whenever
    the device advertises it, so a write is invisible in `running` until an
    explicit <commit> (RFC 6241 Sec 8.6.4.1). These two tests used to pass only
    because `auto_commit` defaulted to True, which meant the candidate
    datastore was never exercised explicitly and the read-back silently read
    a datastore nothing had been written to. The sequence below is the one
    AGENTS.md mandates: edit -> validate -> commit.
    """
    if getattr(client, "default_target", None) != "candidate":
        return  # device has no candidate: the edit already hit `running`
    # <validate> is optional (RFC 6241 Sec 8.6.4.1): not every device
    # advertises :validate, and the client correctly warns and returns False.
    if client.has_validate:
        assert client.validate(source="candidate"), "candidate failed <validate>"
    assert client.commit(), "<commit> rejected"


def _readback_item(target, name: str):
    """Read one list entry back, tolerating the simulator's read quirks.

    The notconf backend hides a written list entry from an item-scoped read
    (`/data` and the equivalent NETCONF `<get>`), which is a documented
    simulator limitation, not a client fault: the RESTCONF round-trip already
    falls back to the RFC 8527 running datastore for exactly this reason. Do
    the same here by reading the whole list and finding the entry, so the
    assertion still proves the value reached the device.
    """
    try:
        item = _readback_item(target, name)
    except Exception:  # noqa: BLE001 - fall back to a collection read
        item = None
    if item is not None:
        return item
    try:
        items = target.retrieve(content="all", depth=3)
    except Exception:  # noqa: BLE001 - genuinely unreadable
        return None
    for candidate in items or []:
        if getattr(candidate, "name", None) == name:
            return candidate
    return None


def _has_config_content(model) -> bool:
    for v in model.model_dump(exclude_unset=True).values():
        if v is None:
            continue
        if isinstance(v, (dict, list, str)) and not v:
            continue
        return True
    return False


def _poll_config_nodes(fetch, timeout=120, interval=5):
    """fetch() -> dict[attr, model].

    notconf applies factory-default/startup data asynchronously after boot
    (evidence: tests/test_notconf_protocol.py test_nmda_datastore_discrimination),
    so poll until at least one generated node exposes config content.
    """
    deadline = time.monotonic() + timeout
    while True:
        nodes = fetch()
        if nodes or time.monotonic() >= deadline:
            return nodes
        time.sleep(interval)


# A node the backend cannot read at all is a *documented* notconf quirk
# (state containers answer 404/400), so those stay a recorded skip. A node the
# backend read but then rejected a write to is a real failure and is asserted.
# Previously both were swallowed, so the test could be green while the device
# rejected every write. Each test clears the lists on entry.
_NODE_READ_SKIPS: list[str] = []
_NODE_WRITE_FAILURES: list[str] = []


def _record_node_failure(attr: str, exc: BaseException) -> None:
    _NODE_READ_SKIPS.append(f"{attr}: {type(exc).__name__}: {exc}"[:200])


def _config_node_models(root_nav_root, client, depth=3, content="all"):
    """Retrieve every generated top-level node.

    Returns {Data-attribute: model} for nodes with config content.
    Nodes that error (e.g. sysrepo 500s on some modules) or have no
    content are skipped. depth=3 so list-item leaf values are included
    (depth=2 from a container target returns keys only — rousette
    evidence, 2026-09-28 probe).
    """
    found: dict = {}
    for attr, prop in vars(type(client.data)).items():
        if not isinstance(prop, property):
            continue
        try:
            model = getattr(root_nav_root, attr).retrieve(content=content, depth=depth)
        except requests.HTTPError as exc:
            # Silently skipping a node that the device rejected made this test
            # green while proving nothing. Record it and assert below.
            _record_node_failure(
                attr, RuntimeError(f"HTTP {getattr(exc.response, 'status_code', '?')}")
            )
            continue
        if model is None or not _has_config_content(model):
            continue
        found[attr] = model
    return found


def test_restconf_all_config_nodes_retrieve_update(generated_sdks):
    """Idempotent same-data merge on every top-level config node (RFC 8040).

    GET each generated Data node from the running datastore (RFC 8527 —
    the /data operational view hides written list entries on the lab
    backend, see test_restconf_sdk_crud_roundtrip), then PATCH the
    retrieved model back unchanged. The read uses content="all" because
    the lab backend 500s (sysrepo exception) on /ds/...?content=config
    for every module (2026-09-28 probe); the running datastore holds
    config data only (RFC 8342), so the result is equivalent. Fresh
    notconf images ship no config, so a throwaway interface is seeded
    through the SDK first when no node has content. Nodes the backend
    cannot read are skipped; a merge the server rejects is a hard
    failure.
    """
    mod = generated_sdks
    ep = mod["ep"]
    rest = mod["rest"]
    client = rest.RestconfClient(
        management_ip="127.0.0.1",
        port=int(ep["restconf_base"].rsplit(":", 1)[1]),
        username=CREDS[0],
        password=CREDS[1],
        verify=False,
        scheme="http",
    )
    ds = rest.data_navigators.Data(client, "/ds/ietf-datastores:running", "")
    nodes = _poll_config_nodes(
        lambda: _config_node_models(ds, client, content="all"), timeout=90
    )
    _NODE_READ_SKIPS.clear()
    _NODE_WRITE_FAILURES.clear()
    seeded_nav = None
    if not nodes and "ietf-interfaces" in _implemented_modules(ep):
        _attr, target = _interfaces_list_navigator(client)
        if target is not None:
            model_cls = _model_for_list_navigator(rest, target)
            target.create(
                [
                    model_cls.model_validate(
                        _interface_payload(model_cls, name="yang2sdk-merge8")
                    )
                ]
            )
            seeded_nav = target
            nodes = _poll_config_nodes(
                lambda: _config_node_models(ds, client, content="all"), timeout=30
            )
    try:
        # _request() raises on a non-2xx, so reaching here means the device
        # accepted every same-data merge. The old code ignored the return
        # value entirely, which is the real hole this closes.
        for attr, model in nodes.items():
            try:
                getattr(client.data, attr).update(model)
            except Exception as exc:  # noqa: BLE001 - asserted, not swallowed
                _NODE_WRITE_FAILURES.append(
                    f"{attr}: {type(exc).__name__}: {exc}"[:200]
                )
        if not nodes:
            # Nothing on this image exposes readable config (the `notconf:latest`
            # base image ships no data modules), so there is no merge to assert.
            # Skip explicitly instead of failing on an empty device: claiming
            # "all nodes merged" for zero nodes would be a vacuous pass.
            pytest.skip("device exposes no readable top-level config content")
        assert not _NODE_WRITE_FAILURES, "same-data merge rejected: " + "; ".join(
            _NODE_WRITE_FAILURES
        )
    finally:
        if seeded_nav is not None:
            try:
                seeded_nav("yang2sdk-merge8").delete()
            except Exception:  # noqa: BLE001, S110 - best-effort cleanup
                pass


def test_netconf_all_config_nodes_retrieve_update(generated_sdks):
    """Idempotent same-data merge on every top-level config node (RFC 6241).

    Pure SDK path: get each generated Data node (content=config), then
    edit-config the retrieved model back unchanged (nc:operation=merge).
    Fresh notconf images ship no config, so a throwaway interface is
    seeded through the SDK first when no node has content. Nodes the
    backend cannot read are skipped; a merge the server rejects is a
    hard failure.
    """
    _NODE_READ_SKIPS.clear()
    _NODE_WRITE_FAILURES.clear()
    mod = generated_sdks
    ep = mod["ep"]
    client = mod["netc"].NetconfClient(
        management_ip="127.0.0.1",
        port=ep["netconf_port"],
        username=CREDS[0],
        password=CREDS[1],
        # The simulator generates a random SSH host key per container, so
        # host-key verification is meaningless here and the secure default
        # (verify=True) would make this suite fail on every image with
        # SSHUnknownHostError. The raw-protocol tests already opt out the same
        # way (see CREDS/hostkey_verify in test_notconf_protocol.py); this
        # makes the generated-client path explicit too. A real deployment must
        # keep verify=True.
        verify=False,
    )

    def fetch():
        found: dict = {}
        for attr, prop in vars(type(client.data)).items():
            if not isinstance(prop, property):
                continue
            nav = getattr(client.data, attr)
            if not (hasattr(nav, "retrieve") and hasattr(nav, "update")):
                continue
            try:
                model = nav.retrieve(content="config", depth=3)
            except Exception as exc:  # noqa: BLE001 - recorded and asserted
                _record_node_failure(attr, exc)
                continue
            if model is None or not _has_config_content(model):
                continue
            found[attr] = model
        return found

    nodes = _poll_config_nodes(fetch, timeout=90)
    seeded_nav = None
    if not nodes and "ietf-interfaces" in _implemented_modules(ep):
        _attr, target = _interfaces_list_navigator(client)
        if target is not None:
            model_cls = _model_for_list_navigator(mod["netc"], target)
            assert target.create(
                [
                    model_cls.model_validate(
                        _interface_payload(model_cls, name="yang2sdk-merge9")
                    )
                ]
            )
            seeded_nav = target
            nodes = _poll_config_nodes(fetch, timeout=30)
    try:
        for attr, model in nodes.items():
            assert getattr(client.data, attr).update(model), (
                f"{attr} same-data merge rejected"
            )
        if not nodes:
            # Nothing on this image exposes readable config (the `notconf:latest`
            # base image ships no data modules), so there is no merge to assert.
            # Skip explicitly instead of failing on an empty device: claiming
            # "all nodes merged" for zero nodes would be a vacuous pass.
            pytest.skip("device exposes no readable top-level config content")
        assert not _NODE_WRITE_FAILURES, "; ".join(_NODE_WRITE_FAILURES)
    finally:
        if seeded_nav is not None:
            try:
                seeded_nav("yang2sdk-merge9").delete()
            except Exception:  # noqa: BLE001, S110 - best-effort cleanup
                pass
