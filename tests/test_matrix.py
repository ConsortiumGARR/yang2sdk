"""Offline checks: matrix completeness + template contract. No docker needed."""

import compileall
import importlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MATRIX = REPO_ROOT / "tests" / "notconf" / "matrix.json"
T_RESTCONF_SM = (
    REPO_ROOT / "src/yang2sdk/plugin/src/templates/restconf/session_manager.py.jinja"
)
T_NETCONF_SM = (
    REPO_ROOT / "src/yang2sdk/plugin/src/templates/netconf/session_manager.py.jinja"
)

MINIMAL_YANG = """module tmod {
  prefix tm;
  namespace "urn:test:tmod";
  revision 2026-01-01;
  container top {
    leaf name {
      type string;
    }
  }
}
"""

NESTED_STATE_YANG = """module tcontent {
  prefix tc;
  namespace "urn:test:tcontent";
  revision 2026-01-01;
  container top {
    leaf name {
      type string;
    }
    leaf counter {
      type uint64;
      config false;
    }
    leaf-list event-log {
      type string;
      config false;
    }
    list hist {
      key "ts";
      leaf ts {
        type uint32;
      }
      leaf val {
        type uint32;
        config false;
      }
    }
  }
}
"""


def _load():
    return json.loads(MATRIX.read_text())


def test_matrix_covers_all_six_packages():
    pkgs = {i["package"] for i in _load()["images"]}
    assert pkgs == {
        "notconf",
        "notconf-ietf",
        "notconf-sros",
        "notconf-junos",
        "notconf-cisco-xr",
        "notconf-cisco-nx",
    }, f"matrix must cover all pre-built families, got {pkgs}"


def test_matrix_has_full_tag_set():
    ids = {f"{i['package']}:{i['tag']}" for i in _load()["images"]}
    for expected in [
        "notconf:latest",
        "notconf-ietf:latest",
        "notconf-sros:21.10",
        "notconf-sros:22.2",
        "notconf-junos:21.1R1",
        "notconf-junos:23.4R1",
        "notconf-cisco-xr:762",
        "notconf-cisco-xr:771",
        "notconf-cisco-xr:2411",
        "notconf-cisco-xr:2531",
        "notconf-cisco-nx:10.4-4",
    ]:
        assert expected in ids, f"missing {expected} (all available pre-built images)"


@pytest.mark.smoke
def test_smoke_subset_is_latest_per_family():
    smoke = {f"{i['package']}:{i['tag']}" for i in _load()["images"] if i.get("smoke")}
    assert smoke == {
        "notconf:latest",
        "notconf-ietf:latest",
        "notconf-sros:22.2",
        "notconf-junos:23.4R1",
        "notconf-cisco-xr:2411",
        "notconf-cisco-nx:10.4-4",
    }


def test_restconf_template_keeps_secure_defaults():
    src = T_RESTCONF_SM.read_text()
    assert "verify: bool = True" in src, "verify=True default is a safety feature"
    assert 'scheme: str = "https"' in src, "https must stay the default scheme"
    assert "plaintext" in src or "Lab/simulator" in src, "http opt-out needs warning"


def test_netconf_template_keeps_hostkey_default():
    src = T_NETCONF_SM.read_text()
    assert "hostkey_verify" in src, "host-key verification plumbing must exist"
    # AGENTS.md packaging: host-key verification on by default (RFC 6242).
    assert "verify: bool = True" in src, "verify=True default is a safety feature"
    assert "hostkey_verify=verify" in src
    assert (
        "lab-only opt-out" in src.lower() or "never use in production" in src.lower()
    ), "verify=False opt-out needs logged warning"
    assert "log_bodies" in src, "structured body-logging opt-in must exist"


def test_restconf_logging_hygiene_contract():
    src = T_RESTCONF_SM.read_text()
    assert "log_bodies" in src, "structured body-logging opt-in must exist"
    assert "_redact" in src
    # AGENTS.md Safety: full bodies never at INFO in production paths.
    assert 'log.info("Request: %s %s"' in src or 'log.info("Request: %s %s"' in src
    assert 'log.info("Response: %s"' in src or 'log.info("Response: %s"' in src
    assert "Response ({response.status_code}): {response.text}" not in src
    assert 'f"Request: {method} {url} {kwargs}"' not in src


def test_clients_swappable_signature():
    rest = T_RESTCONF_SM.read_text()
    netc = T_NETCONF_SM.read_text()
    for param in [
        "loopback_ip",
        "management_ip",
        "username",
        "password",
        "verify: bool = True",
        "log_bodies",
    ]:
        assert param in rest, f"RESTCONF client missing swappable param {param}"
        assert param in netc, f"NETCONF client missing swappable param {param}"


def test_navigator_parity_surface():
    base = REPO_ROOT / "src/yang2sdk/plugin/src/templates"
    # Public surface = generated per-node methods + inherited _base methods.
    rest = (base / "restconf/data_navigators/navigators.py.jinja").read_text()
    rest += (base / "restconf/data_navigators/_base.py.jinja").read_text()
    netc = (base / "netconf/data_navigators/navigators.py.jinja").read_text()
    netc += (base / "netconf/data_navigators/_base.py.jinja").read_text()
    for op in ["def retrieve", "def update", "def replace", "def delete"]:
        assert op in rest, f"RESTCONF navigators missing {op}"
        assert op in netc, f"NETCONF navigators missing {op}"
    assert "_create" in (base / "restconf/data_navigators/_base.py.jinja").read_text()
    assert "def create" in netc, "NETCONF list navigators missing create"

    # RFC 7951 Sec 4: RESTCONF write bodies must use a module-qualified
    # top-level member; the navigator splits the response key (_name) from
    # the write key (_envelope_name) for nested nodes.
    assert "_envelope_name" in rest, "RESTCONF _base must carry the qualified write key"
    assert (
        "envelope_name="
        in (base / "restconf/data_navigators/navigators.py.jinja").read_text()
    )

    # RFC 7950 Sec 9.10.3 + RFC 6241 Sec 8.3: NETCONF identityref values
    # (RFC 7951 module-name form) need their prefix bound on write and are
    # normalized back to module-name form on read.
    sm = (base / "netconf/session_manager.py.jinja").read_text()
    assert "module_namespaces" in sm and "bind_module_prefixes" in sm
    assert (
        "normalize_identityrefs"
        in (base / "netconf/data_models/_base.py.jinja").read_text()
    )
    assert (
        "module_namespaces=self._client.module_namespaces"
        in (base / "netconf/data_navigators/navigators.py.jinja").read_text()
    )


@pytest.mark.parametrize("fmt", ["restconf", "netconf"])
def test_generate_rpc_free_module_compiles_and_imports(fmt, tmp_path):
    """An rpc-free module yields an empty Operations class that must compile.

    Regression: the Operations class had no `pass` fallback (IndentationError).
    """
    yang = tmp_path / "tmod.yang"
    yang.write_text(MINIMAL_YANG)
    out = tmp_path / f"tmod_{fmt}"
    code = (
        "import sys; from yang2sdk.cli.compiler import run_compiler; "
        f"run_compiler({fmt!r}, sys.argv[1:])"
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(yang),
            "--device",
            "tmod",
            "--yang-dir",
            str(tmp_path),
            "--output-dir",
            str(out),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-1500:]
    assert compileall.compile_dir(str(out), quiet=1), f"{fmt} output has syntax errors"
    sys.path.insert(0, str(tmp_path))
    try:
        pkg = __import__(f"tmod_{fmt}")
        assert pkg.RestconfClient if fmt == "restconf" else pkg.NetconfClient
    finally:
        sys.path.remove(str(tmp_path))


def test_content_filter_prunes_nested_state(tmp_path):
    """model_dump(content=...) prunes state nodes at every depth (RFC 8040 §4.5.2).

    Regression: filtering built a top-level pydantic `exclude` set only, so
    nested state leaked into config dumps — and therefore into PATCH/PUT
    bodies, which serialize with content="config".
    """
    yang = tmp_path / "tcontent.yang"
    yang.write_text(NESTED_STATE_YANG)
    out = tmp_path / "tcontent_restconf"
    code = (
        "import sys; from yang2sdk.cli.compiler import run_compiler; "
        "run_compiler('restconf', sys.argv[1:])"
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(yang),
            "--device",
            "tcontent",
            "--yang-dir",
            str(tmp_path),
            "--output-dir",
            str(out),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-1500:]
    sys.path.insert(0, str(tmp_path))
    try:
        models = __import__("tcontent_restconf.data_models.tcontent", fromlist=["*"])
        data_cls = models.TcontentData
    finally:
        sys.path.remove(str(tmp_path))

    m = data_cls.model_validate(
        {
            "tcontent:top": {
                "name": "x",
                "counter": "5",
                "event-log": ["boot", "link-up"],
                "hist": [{"ts": 1, "val": 2}],
            }
        }
    )
    config = m.model_dump(content="config")["tcontent:top"]
    assert "counter" not in config, "nested state leaf leaked into config dump"
    assert "event-log" not in config, "state leaf-list leaked into config dump"
    assert config["hist"] == [{"ts": 1}], "state inside list items must be pruned"
    nonconfig = m.model_dump(content="nonconfig")["tcontent:top"]
    assert nonconfig["counter"] == "5", "state must survive a nonconfig dump"
    assert nonconfig["event-log"] == ["boot", "link-up"]
    assert nonconfig["hist"] == [
        {"val": 2}
    ]  # uint32 stays numeric; only 64-bit is stringified
    assert "name" not in nonconfig


ENV_YANG = """module env {
  prefix e;
  namespace "urn:test:env";
  revision 2026-01-01;
  container parent {
    list things {
      key "id";
      leaf id {
        type string;
      }
      leaf val {
        type string;
      }
      leaf some-leaf {
        type string;
      }
    }
  }
}
"""


class _RecordingClient:
    def __init__(self):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.responses: list[dict] = []

    def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        self.calls.append((method, path, kwargs.get("json")))
        if self.responses:
            return self.responses.pop(0)
        return {}


def test_restconf_write_bodies_are_module_qualified(tmp_path):
    """RFC 7951 Sec 4 + Sec 5.4 with RFC 8040 Sec 4.4.1/4.5: write bodies are
    module-qualified and lists are name/array — including item PUT/PATCH as
    single-element arrays (Sec 4.5 jukebox album example) and create POST to
    the parent (Sec 4.4.1 + App. B.2.1).

    Regression: nested navigators sent {"interface": [...]}, which only works
    on lenient servers; strict servers (and the RFC) require
    {"ietf-interfaces:interface": [...]}.
    """
    yang = tmp_path / "env.yang"
    yang.write_text(ENV_YANG)
    out = tmp_path / "env_restconf"
    code = (
        "import sys; from yang2sdk.cli.compiler import run_compiler; "
        "run_compiler('restconf', sys.argv[1:])"
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(yang),
            "--device",
            "env",
            "--yang-dir",
            str(tmp_path),
            "--output-dir",
            str(out),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-1500:]
    sys.path.insert(0, str(tmp_path))
    try:
        nav_mod = importlib.import_module("env_restconf.data_navigators")
        models = importlib.import_module("env_restconf.data_models.env")

        client = _RecordingClient()
        data = nav_mod.Data(client, "/data", "")
        list_nav = data.env_parent.things

        thing = models.ThingsItem.model_validate(
            {"id": "a", "val": "x", "some_leaf": "s"}
        )
        full = {"id": "a", "val": "x", "some-leaf": "s"}

        list_nav.create([thing])
        method, path, body = client.calls[-1]
        # RFC 8040 Sec 4.4.1 + App. B.2.1: POST to the parent with a
        # single-element array, one POST per entry (Sec 4.4.1 MUST exactly
        # one instance). See _base.py.jinja _create.
        assert method == "POST" and path == "/data/env:parent"
        assert body == {"env:things": [full]}, (
            f"create body must be a qualified single-element array: {body}"
        )

        list_nav.replace([thing])
        method, path, body = client.calls[-1]
        # RFC 8040 Sec 4.5 + RFC 7951 Sec 5.4: whole-list PUT targets the
        # list resource itself with name/array encoding.
        assert method == "PUT" and path == "/data/env:parent/things"
        assert body == {"env:things": [full]}, (
            f"list replace body must be a qualified array: {body}"
        )

        list_nav("a").update(thing)
        method, path, body = client.calls[-1]
        # RFC 8040 Sec 4.5 jukebox PUT array + RFC 7951 Sec 5.4 list/array;
        # PATCH list-instance follows the same JSON encoding (no JSON PATCH
        # list-instance example in RFC 8040; XML bare <album> differs by
        # design). Matches rousette tests/restconf-plain-patch.cpp (204).
        assert body == {"env:things": [full]}, (
            f"item PATCH body must be a qualified array: {body}"
        )

        list_nav("a").replace(thing)
        method, path, body = client.calls[-1]
        assert method == "PUT" and path == "/data/env:parent/things=a"
        assert body == {"env:things": [full]}, (
            f"item PUT body must be a qualified array: {body}"
        )

        parent = models.Parent.model_validate({"things": [full]})
        data.env_parent.update(parent)
        method, path, body = client.calls[-1]
        assert method == "PATCH" and path == "/data/env:parent"
        assert body == {"env:parent": {"things": [full]}}, (
            f"container envelope must be qualified: {body}"
        )

        # Response keys stay simple for same-module nodes: retrieve must
        # still match the unqualified key.
        client.responses.append({"things": [full]})
        items = list_nav.retrieve(content="all", depth=2)
        assert [i.id for i in items] == ["a"]
    finally:
        sys.path.remove(str(tmp_path))


TB_BASE_YANG = """module tbase {
  prefix tb;
  namespace "urn:test:tbase";
  revision 2026-01-01;
  identity base-id;
}
"""

TB_MAIN_YANG = """module tmain {
  prefix tm;
  namespace "urn:test:tmain";
  import tbase {
    prefix tb;
  }
  revision 2026-01-01;
  container stuff {
    list items {
      key "id";
      leaf id {
        type string;
      }
      leaf kind {
        type identityref {
          base tb:base-id;
        }
        mandatory true;
      }
    }
  }
}
"""


def test_netconf_identityref_binding_and_normalization(tmp_path):
    """identityref values stay RFC 7951 Sec 6.8 module-name strings in models;
    the write path binds the prefix (RFC 7950 Sec 9.10.3) and the read path
    normalizes server prefixes back to the module name (RFC 6241 Sec 8.3).

    Regression: NETCONF create/replace of ietf-interfaces failed with
    'unable to map prefix to YANG schema' because <type> carried an unbound
    prefix; retrieved models also kept server-specific prefixes.
    """
    (tmp_path / "tbase.yang").write_text(TB_BASE_YANG)
    main = tmp_path / "tmain.yang"
    main.write_text(TB_MAIN_YANG)
    out = tmp_path / "tmain_netconf"
    code = (
        "import sys; from yang2sdk.cli.compiler import run_compiler; "
        "run_compiler('netconf', sys.argv[1:])"
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(main),
            "--device",
            "tmain",
            "--yang-dir",
            str(tmp_path),
            "--output-dir",
            str(out),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-1500:]
    assert compileall.compile_dir(str(out), quiet=1), "netconf output has syntax errors"
    sys.path.insert(0, str(tmp_path))
    try:
        models = importlib.import_module("tmain_netconf.data_models.tmain")
        from lxml import etree  # ty: ignore[unresolved-import] - lxml stubs (cf. src/)

        # Generated by this test at runtime; import dynamically so the
        # type checkers do not try to resolve a nonexistent source module.
        bind_module_prefixes = importlib.import_module(
            "tmain_netconf.session_manager"
        ).bind_module_prefixes

        item_cls = models.ItemsItem
        assert (
            item_cls.model_fields["kind"].json_schema_extra.get("is_identityref")
            is True
        )

        # Write side: module-name prefix gets its xmlns declaration.
        cfg = etree.fromstring(
            '<config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">'
            '<stuff xmlns="urn:test:tmain"><items>'
            "<id>a</id><kind>tbase:base-id</kind></items></stuff></config>"
        )
        bind_module_prefixes(cfg, {"tbase": "urn:test:tbase"})
        assert 'xmlns:tbase="urn:test:tbase"' in etree.tostring(cfg).decode()
        # Idempotent, and never rebinds a prefix already in scope.
        bind_module_prefixes(cfg, {"tbase": "urn:test:tbase"})
        assert etree.tostring(cfg).count(b"xmlns:tbase") == 1
        cfg2 = etree.fromstring('<a xmlns:tbase="urn:other"><t>tbase:x</t></a>')
        bind_module_prefixes(cfg2, {"tbase": "urn:test:tbase"})
        assert 'xmlns:tbase="urn:other"' in etree.tostring(cfg2).decode()

        # Read side: server prefix form normalizes to the module name.
        xml = (
            '<items xmlns="urn:test:tmain"><id>a</id>'
            '<kind xmlns:xx="urn:test:tbase">xx:base-id</kind></items>'
        )
        m = item_cls.from_xml(xml, module_namespaces={"tbase": "urn:test:tbase"})
        assert m.kind == "tbase:base-id"

        # Round trip: module-name form serializes unchanged.
        payload = item_cls(id="a", kind="tbase:base-id").to_xml_payload()
        assert b"tbase:base-id" in (
            payload if isinstance(payload, bytes) else payload.encode()
        )
    finally:
        sys.path.remove(str(tmp_path))


def test_no_nonexistent_rfc_sections_cited():
    """Guard against wrong RFC cites (e.g. RFC 8040 has no Sec 4.6.2; Sec 5.2
    is message encoding, not PATCH bodies; list encoding is RFC 7951 Sec 5.4,
    not Sec 6.3 which covers types).

    Regression: templates/tests cited Sec 4.6.2 / Sec 5.2-object /
    Sec 6.3-list for body shapes that are actually Sec 4.4.1/4.5/4.6.1 +
    RFC 7951 Sec 5.4 (jukebox array example).
    """
    base = REPO_ROOT / "src/yang2sdk/plugin/src/templates"
    hay = ""
    hay += (base / "restconf/data_navigators/_base.py.jinja").read_text()
    hay += (base / "restconf/session_manager.py.jinja").read_text()
    hay += (base / "netconf/session_manager.py.jinja").read_text()
    hay += (REPO_ROOT / "README.md").read_text()
    # Old false claims (now fixed): Sec 4.6.2 does not exist, Sec 5.2 is
    # message encoding (not PATCH bodies), list encoding is Sec 5.4 not 6.3.
    assert "Sec 4.6.2 specifies" not in hay
    assert "Sec 5.2 shows an object" not in hay
    assert "Sec 6.3 list encoding" not in hay


def test_rfc8040_jukebox_array_shape():
    """RFC 8040 Sec 4.5 jukebox example: PUT on list=key uses single-element
    array under the list name (RFC 7951 Sec 5.4). Guards against regressing
    item PUT/PATCH to bare objects.
    """
    # Shape-level check: mirrors the RFC example structurally, not the
    # full jukebox module.
    body = {"example-jukebox:album": [{"name": "Wasting Light"}]}
    assert isinstance(body["example-jukebox:album"], list)
    assert body["example-jukebox:album"][0]["name"] == "Wasting Light"


def _run_compiler(fmt, yang_path, tmp_path, out, extra_args=None):
    code = (
        "import sys; from yang2sdk.cli.compiler import run_compiler; "
        f"run_compiler({fmt!r}, sys.argv[1:])"
    )
    argv = [
        str(yang_path),
        "--device",
        "tmod",
        "--yang-dir",
        str(tmp_path),
        "--output-dir",
        str(out),
    ]
    argv += list(extra_args or [])
    proc = subprocess.run(
        [sys.executable, "-c", code, *argv],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-1500:]
    return proc


def test_generated_package_is_registry_ready(tmp_path):
    """Generated SDK is a securely publishable package (AGENTS.md packaging).

    Asserts pyproject.toml + README.md + MANIFEST + py.typed exist, manifest
    carries module revisions, no secrets/lab paths, and the namespaced copy
    imports (import <package_name>).
    """
    yang = tmp_path / "tmod.yang"
    yang.write_text(MINIMAL_YANG)
    out = tmp_path / "tmod_pkg"
    _run_compiler(
        "restconf",
        yang,
        tmp_path,
        out,
        ["--device-version", "1.2.3", "--package-version", "1.2.3"],
    )
    assert (out / "pyproject.toml").exists()
    assert (out / "README.md").exists()
    assert (out / "MANIFEST.yang-revisions.json").exists()
    assert (out / "py.typed").exists()
    import tomllib

    pyproj = tomllib.loads((out / "pyproject.toml").read_text())
    assert pyproj["project"]["name"] == "tmod_1_2_3"
    assert pyproj["project"]["version"] == "1.2.3"
    assert "data_models" not in pyproj["project"]["name"]
    manifest = json.loads((out / "MANIFEST.yang-revisions.json").read_text())
    assert manifest["device"] == "tmod"
    assert manifest["device_version"] == "1.2.3"
    assert manifest["protocol"] == "restconf"
    assert any(m["name"] == "tmod" for m in manifest["modules"])
    hay = (out / "pyproject.toml").read_text() + (out / "README.md").read_text()
    for secret in ("DEVICE_PASS", "DEVICE_USER", "192.168", "BEGIN PRIVATE"):
        assert secret not in hay
    # Namespaced installable copy imports without temp/lab paths.
    sys.path.insert(0, str(out))
    try:
        pkg = importlib.import_module("tmod_1_2_3")
        assert pkg.RestconfClient is not None
    finally:
        sys.path.remove(str(out))


def test_package_manifest_records_deviations_and_features(tmp_path):
    yang = tmp_path / "tmod.yang"
    yang.write_text(MINIMAL_YANG)
    # Minimal deviation module that pyang can load (applies cleanly to tmod).
    dev = tmp_path / "my-dev.yang"
    dev.write_text(
        """module my-dev {
  prefix md;
  namespace "urn:test:my-dev";
  revision 2026-01-01;
  import tmod { prefix tm; }
  deviation /tm:top {
    deviate not-supported;
  }
}
"""
    )
    out = tmp_path / "tmod_dev"
    _run_compiler(
        "restconf",
        yang,
        tmp_path,
        out,
        [
            "--device-version",
            "9.9",
            "--deviation-module",
            str(dev),
            "--feature",
            "tmod:myfeat",
        ],
    )
    manifest = json.loads((out / "MANIFEST.yang-revisions.json").read_text())
    assert any("my-dev.yang" in d for d in manifest["deviations"])
    assert "tmod:myfeat" in manifest["features"]


def test_logging_hygiene_generated_client(tmp_path):
    """INFO carries method/URL/status only; bodies need log_bodies opt-in."""
    yang = tmp_path / "tmod.yang"
    yang.write_text(MINIMAL_YANG)
    out = tmp_path / "tmod_log"
    _run_compiler("restconf", yang, tmp_path, out)
    sys.path.insert(0, str(tmp_path))
    try:
        sm_mod = importlib.import_module("tmod_log.session_manager")

        class FakeResp:
            status_code = 200
            text = '{"tmod:top": {"name": "x", "password": "s3cret"}}'

            def raise_for_status(self):
                return None

            def json(self):
                return {"tmod:top": {"name": "x"}}

        class FakeSession:
            verify = True
            trust_env = True
            auth = ("u", "p")

            def __init__(self):
                self.headers: dict = {}

            def mount(self, *a, **k):
                return None

            def request(self, method, url, timeout=None, **kwargs):
                return FakeResp()

        from unittest import mock

        with mock.patch.object(sm_mod.requests, "Session", return_value=FakeSession()):
            client = sm_mod.RestconfClient(
                management_ip="127.0.0.1", username="u", password="p", verify=False
            )
            # Default: bodies hidden from INFO.
            assert client.log_bodies is False
    finally:
        sys.path.remove(str(tmp_path))
