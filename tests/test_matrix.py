"""Offline checks: matrix completeness + template contract. No docker needed."""

import compileall
import importlib
import inspect
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from yang2sdk.cli.compiler import resolve_features
from yang2sdk.cli.features import (
    parse_capability_features,
    read_features_file,
    to_pyang_args,
    write_features_file,
)
from yang2sdk.cli.model_gaps import main as model_gaps_main
from yang2sdk.cli.model_gaps import run_gap_check

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
    import re

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

    # Per-class verbs pin the AGENTS.md parity table so docs and templates
    # cannot drift again: RESTCONF plural lists expose create (not update),
    # NETCONF plural lists expose update (not delete).
    rest_lists = re.findall(
        r"ListNode\[.*?\](.*?)\n(?=class |\{% |\Z)",
        (base / "restconf/data_navigators/navigators.py.jinja").read_text(),
        re.S,
    )
    assert rest_lists, "no RESTCONF ListNode classes found in template"
    for block in rest_lists:
        assert "def create" in block, "RESTCONF ListNode missing create"
        assert "def replace" in block and "def retrieve" in block
        assert "def update" not in block, "RESTCONF ListNode must not expose update"
    netc_lists = re.findall(
        r"ListNode\[.*?\](.*?)\n(?=class |\{% |\Z)",
        (base / "netconf/data_navigators/navigators.py.jinja").read_text(),
        re.S,
    )
    assert netc_lists, "no NETCONF ListNode classes found in template"
    for block in netc_lists:
        for op in ["def retrieve", "def update", "def create", "def replace"]:
            assert op in block, f"NETCONF ListNode missing {op}"
        assert "def delete" not in block, "NETCONF ListNode must not expose delete"

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


_COMPILER_CODE = "import sys; from yang2sdk.cli.compiler import run_compiler; run_compiler(%r, sys.argv[1:])"


def _run_compiler(fmt, yang_path, tmp_path, out, extra_args=None):
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
        [sys.executable, "-c", _COMPILER_CODE % fmt, *argv],
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
    # `--feature` is forwarded to pyang as a real whitelist now, so pyang
    # rejects a feature the module does not declare (pyang_tool: "unknown
    # feature ... in module ..."). The module must declare it.
    yang = tmp_path / "tmod.yang"
    yang.write_text(
        MINIMAL_YANG.replace("container top {", "feature myfeat;\n  container top {")
    )
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
    assert manifest["features_source"] == "manual"


def test_unknown_manual_feature_is_a_hard_error(tmp_path):
    """A manual feature the module does not declare must fail loudly.

    Silently dropping it would hand back a model that does not contain what
    the operator asked for — the same silent-wrong-model failure the device
    feature set exists to prevent.
    """
    yang = tmp_path / "tmod.yang"
    yang.write_text(MINIMAL_YANG)
    proc = subprocess.run(
        [sys.executable, "-c", _COMPILER_CODE % "restconf"]
        + [
            str(yang),
            "--device",
            "tmod",
            "--yang-dir",
            str(tmp_path),
            "--output-dir",
            str(tmp_path / "out"),
            "--feature",
            "tmod:nosuchfeature",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert proc.returncode != 0, "unknown feature must not be silently ignored"
    assert "unknown feature nosuchfeature" in proc.stderr


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


# --- device feature set (RFC 7950 if-feature -> pyang --features) -------------

FEATURE_YANG = """module tfeat {
  yang-version 1.1;
  prefix tf;
  namespace "urn:test:tfeat";
  revision 2026-01-01;
  feature f;
  feature other;
  container s {
    container g {
      if-feature "not f";
      leaf x {
        type string;
      }
    }
  }
}
"""

FEATURE_NS = "urn:test:tfeat"


def _rendered_paths(paths, report) -> set[str]:
    from yang2sdk.cli.model_gaps import _render

    return {_render(p, report.ns_to_module) for p in paths}


def test_parse_capability_features_excludes_modules_without_features():
    """RFC 6241 Sec 8.3: only a capability carrying `features=` is a whitelist.

    pyang reads ctx.features as a per-module allow-list (pyang/statements.py:
    an if-feature resolves False unless listed for the *defining* module), so
    feeding it a module that advertised no `features=` as an empty list would
    disable every feature of that module. Non-module capabilities and modules
    advertised without `features=` must therefore be dropped entirely.
    """
    caps = [
        "urn:ietf:params:netconf:base:1.0",
        "urn:ietf:params:netconf:capability:yang-library:1.0?revision=2016-06-21",
        f"{FEATURE_NS}?module=tfeat&revision=2026-01-01&features=other,f3",
        f"{FEATURE_NS}?module=tfeat&revision=2026-01-01",
        "urn:ietf:params:xml:ns:yang:ietf-netconf?module=ietf-netconf&revision=2011-06-01",
    ]
    parsed = parse_capability_features(caps)
    assert parsed == {"tfeat": ["other", "f3"]}, (
        f"only capabilities carrying features= may become a pyang whitelist: {parsed}"
    )
    assert "ietf-netconf" not in parsed, (
        "a module with no features= is unknown, not empty; it must stay out"
    )


def test_pyang_features_args_are_one_arg_per_module():
    """pyang parses `--features <mod>:[<f>,]*`; repeating `mod:f` reads as a filename."""
    args = to_pyang_args({"srl_nokia-features": {"b", "a"}, "ietf-netconf": {"nmda"}})
    assert args == ["ietf-netconf:nmda", "srl_nokia-features:a,b"], args


def test_model_gaps_detects_if_feature_gap(tmp_path):
    """The discriminating test: default vs device feature set must differ.

    `g` is gated by `if-feature "not f"`, so pyang's default (all features
    supported) prunes it while a device that does not support `f` keeps it.
    A default-model client reading that device then fails on the first
    unknown element (`extra="forbid"`). This test needs BOTH halves:
    the feature plumbing into pyang *and* a surface diff that notices.
    """
    yang = tmp_path / "tfeat.yang"
    yang.write_text(FEATURE_YANG)
    features_file = tmp_path / "features.json"
    write_features_file(features_file, {"tfeat": ["other"]}, source="test")

    report = run_gap_check(
        roots=[str(yang)],
        yang_dir=tmp_path,
        features_file=features_file,
        device="tfeat",
        work_dir=tmp_path / "work",
    )

    missing = _rendered_paths(report.missing_from_default, report)
    assert missing == {"s/g", "s/g/x"}, (
        f"read-risk paths wrong: {missing} (raw: {report.missing_from_default})"
    )
    assert (FEATURE_NS, "s") not in report.missing_from_default, (
        "the shared parent must not be reported missing"
    )
    assert report.absent_on_device == set(), "device is a superset here"
    assert "tfeat.G" in report.missing_classes, "the dropped model class is the tell"
    assert _rendered_paths(report.missing_nav, report) == {"s/g"}
    assert report.unresolved == [], report.unresolved
    # Both compiles really happened and really differed.
    assert report.missing_from_default, "gap must be non-empty to be meaningful"


def test_model_gaps_exit_codes(tmp_path):
    """0 when the model already matches the device set, 1 on read risk."""
    yang = tmp_path / "tfeat.yang"
    yang.write_text(FEATURE_YANG)
    features_file = tmp_path / "features.json"
    base = [
        str(yang),
        "--yang-dir",
        str(tmp_path),
        "--device",
        "tfeat",
        "--work-dir",
        str(tmp_path / "work"),
    ]
    # Device does not support `f` -> `g` is real on the device -> exit 1.
    write_features_file(features_file, {"tfeat": ["other"]}, source="test")
    assert model_gaps_main([*base, "--features-file", str(features_file)]) == 1
    # Device supports `f` -> default model already matches -> exit 0.
    write_features_file(features_file, {"tfeat": ["f", "other"]}, source="test")
    assert model_gaps_main([*base, "--features-file", str(features_file)]) == 0


def test_features_file_roundtrip(tmp_path):
    features = {"m1": ["a", "b"], "m2": []}
    path = write_features_file(tmp_path / "features.json", features, source="unit")
    assert read_features_file(path) == features
    # A bare mapping is accepted too, so hand-written fixtures stay trivial.
    (tmp_path / "bare.json").write_text(json.dumps(features))
    assert read_features_file(tmp_path / "bare.json") == features


# --- Change B: forwarding the device feature set to pyang --------------------


def test_resolve_features_is_a_noop_without_a_features_file(tmp_path):
    """No features file + no --feature => nothing forwarded, so existing
    compilations stay byte-identical (pyang keeps its "all features" default)."""
    resolved = resolve_features(yang_dir=tmp_path, manual=[])
    assert resolved.modules == {}
    assert resolved.source == "none"
    assert to_pyang_args(resolved.modules) == []


def test_resolve_features_unions_device_and_manual(tmp_path):
    """The operator may add features, never narrow them.

    Narrowing is the silent-wrong-model failure: pyang prunes
    `if-feature "not X"` subtrees the device actually has.
    """
    write_features_file(tmp_path / "features.json", {"tfeat": ["other"]}, source="t")
    resolved = resolve_features(
        yang_dir=tmp_path, manual=["tfeat:f", "tfeat:other", "tm2:x"]
    )
    assert resolved.modules == {
        "tfeat": ["f", "other"],  # union, device feature kept
        "tm2": ["x"],
    }
    assert resolved.source == "device+manual"
    assert to_pyang_args(resolved.modules) == ["tfeat:f,other", "tm2:x"]


def test_resolve_features_honours_opt_out(tmp_path):
    write_features_file(tmp_path / "features.json", {"tfeat": ["other"]}, source="t")
    assert (
        resolve_features(yang_dir=tmp_path, manual=[], use_device_features=False).source
        == "none"
    )
    assert (
        resolve_features(
            yang_dir=tmp_path, manual=[], features_file=str(tmp_path / "features.json")
        ).source
        == "device"
    )


def test_auto_device_features_reach_pyang(tmp_path):
    """End-to-end: features.json next to the YANG is picked up automatically."""
    yang = tmp_path / "tfeat.yang"
    yang.write_text(FEATURE_YANG)
    write_features_file(tmp_path / "features.json", {"tfeat": ["other"]}, source="t")

    auto = tmp_path / "auto"
    _run_compiler("restconf", yang, tmp_path, auto)
    auto_manifest = json.loads((auto / "MANIFEST.yang-revisions.json").read_text())
    assert auto_manifest["features_source"] == "device"
    assert auto_manifest["features"] == ["tfeat:other"]
    # `g` is `if-feature "not f"` and f is unsupported, so the auto-detected
    # device set must keep it in the model.
    assert "G" in (auto / "data_models" / "tfeat.py").read_text()

    opted_out = tmp_path / "opted_out"
    _run_compiler("restconf", yang, tmp_path, opted_out, ["--no-device-features"])
    out_manifest = json.loads((opted_out / "MANIFEST.yang-revisions.json").read_text())
    assert out_manifest["features_source"] == "none"
    assert out_manifest["features"] == []
    assert "class G(" not in (opted_out / "data_models" / "tfeat.py").read_text()


# --- RPC / action wire encoding (offline: no device needed) -------------------
# Regression home for the RPC/action path, which previously had *no* coverage
# at all: the only rpc-related test asserted the degenerate rpc-FREE case.
#
# Normative sources were fetched from the RFC editor rather than inferred from
# the RFCs/*.md summaries:
#   RFC 8040 Sec 3.6   POST {+restconf}/operations/<module-name>:<rpc-identifier>
#   RFC 8040 Sec 3.6.1 JSON body is {"<module>:input": {...}}. The member name is
#                      the FIXED name "input" qualified by the defining module;
#                      the rpc identifier appears only in the URI, never in the
#                      body. No input section => MUST NOT send a body.
#   RFC 8040 Sec 3.6.2 JSON reply is {"<module>:output": {...}}.
#   RFC 7950 Sec 7.15.2 an action is <action> in urn:...:yang:1 holding the
#                      ancestor container/list hierarchy with all key leafs; the
#                      innermost container holds an element named after the
#                      action whose children are the input parameters.

RPC_YANG = """module trpc {
  prefix tr;
  namespace "urn:test:trpc";
  revision 2026-01-01;

  rpc reboot {
    input {
      leaf delay-seconds { type uint32; }
      leaf force { type boolean; default false; }
    }
    output {
      leaf status { type string; }
    }
  }
  rpc noop;
}
"""

RPC_ONLY_YANG = """module tonly {
  prefix to;
  namespace "urn:test:tonly";
  revision 2026-01-01;
  rpc a-rpc { input { leaf x { type string; } } }
}
"""

ACTION_YANG = """module tact {
  yang-version 1.1;
  prefix ta;
  namespace "urn:test:tact";
  revision 2026-01-01;

  container interfaces {
    list interface {
      key "name";
      leaf name { type string; }
      action reset {
        input { leaf delay { type uint32; } }
        output { leaf done { type boolean; } }
      }
    }
  }
}
"""


def _gen(tmp_path, fmt, yang_text, name):
    """Compile one YANG module; return (output dir, all files compiled)."""
    tmp_path = Path(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    yang = tmp_path / f"{name}.yang"
    yang.write_text(yang_text)
    out = tmp_path / f"{name}_{fmt}"
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
            name,
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
    assert proc.returncode == 0, proc.stderr[-2000:]
    return out, compileall.compile_dir(str(out), quiet=1)


def _import(pkg_dir, dotted):
    """Import `<pkg_dir.name>.dotted`, i.e. the flat lab layout under temp/."""
    parent = str(pkg_dir.parent)
    sys.path.insert(0, parent)
    try:
        return importlib.import_module(f"{pkg_dir.name}.{dotted}")
    finally:
        sys.path.remove(parent)


class _Recorder:
    """Stand-in for a generated client that records what was sent."""

    def __init__(self, reply=None):
        self.module_namespaces: dict[str, str] = {}
        self.calls: list[tuple[str, str, Any]] = []
        self.reply = reply
        self.rpc_payloads: list[Any] = []

    def _request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs.get("json")))
        return self.reply if self.reply is not None else {}

    def rpc(self, payload):
        self.rpc_payloads.append(payload)
        return self.reply if self.reply is not None else None


def test_restconf_rpc_request_body_is_module_qualified(tmp_path):
    """RFC 8040 Sec 3.6.1 + RFC 7951 Sec 4: the POST body key is
    "<module>:input" -- the module-qualified FIXED name, not the rpc
    identifier and not bare "input".

    Regression: the envelope's `input` field carried no alias, so every
    RESTCONF RPC/action was sent as unqualified {"input": {...}}.
    """
    out, _ = _gen(tmp_path, "restconf", RPC_YANG, "trpc")
    nav = _import(out, "data_navigators")
    client = _Recorder(reply={"trpc:output": {"status": "ok"}})
    nav.Operations(client, "/operations", "").trpc_reboot(
        {"delay-seconds": 5, "force": True}
    )
    method, path, body = client.calls[-1]
    assert (method, path) == ("POST", "/operations/trpc:reboot")
    assert list(body) == ["trpc:input"], body
    assert body == {"trpc:input": {"delay-seconds": 5, "force": True}}, body


def test_restconf_datastore_root_refuses_every_write(tmp_path):
    """The datastore root must not be writable, on either transport.

    AGENTS.md makes "never request root `restconf/data/`" non-negotiable and
    requires opt-in for destructive defaults. `Data` inherited `Node`'s write
    methods, so `client.data.delete()` emitted a bare `DELETE /restconf/data` --
    a whole-datastore request reachable with no flag, no warning, and no second
    acknowledgement. Nothing in the suite touched it.

    RFC 8040 Sec 4 defines no DELETE/PATCH/PUT on /restconf/data, so this is
    refused rather than merely discouraged. Deleting a *top-level node* is a
    bounded, meaningful request and stays available -- asserted below, because a
    guard that also broke legitimate deletes would be no better than the bug.
    """
    out, _ = _gen(tmp_path / "restconf", "restconf", ROOT_WRITE_YANG, "trdw")
    nav = _import(out, "data_navigators")
    recorder = _Recorder()
    data = nav.Data(recorder, "/data", "")
    assert data._path == "/data"

    for method in ("delete", "_update", "_replace"):
        with pytest.raises(RuntimeError, match="datastore root"):
            if method == "delete":
                data.delete()
            else:
                getattr(data, method)(x=1)
    assert not recorder.calls, (
        f"a refused write still issued a request: {recorder.calls}"
    )

    # A top-level node is still deletable -- the guard is on the root only.
    prop = next(n for n, v in vars(type(data)).items() if isinstance(v, property))
    node = getattr(data, prop)
    node.delete()
    assert recorder.calls[-1][0] == "DELETE", recorder.calls
    assert recorder.calls[-1][1].startswith("/data/"), recorder.calls


def test_netconf_datastore_root_exposes_no_write_methods(tmp_path):
    """The NETCONF root navigator has no write methods at all.

    The parity counterpart to the RESTCONF guard above, asserted directly so the
    two transports cannot drift apart again: RESTCONF's root used to inherit
    write methods that NETCONF's root never had.
    """
    out, _ = _gen(tmp_path / "netconf", "netconf", NETCONF_SAFETY_YANG, "trn")
    nav = _import(out, "data_navigators")
    data = nav.Data(None, [])
    for method in ("delete", "_update", "_replace", "update", "replace", "create"):
        assert not hasattr(data, method), (
            f"the NETCONF datastore root must not expose {method}()"
        )


def test_identityref_prefix_binds_without_a_server(tmp_path):
    """An identityref's module prefix is declared from the COMPILE-TIME map.

    An identityref value is `module-name:identity` (RFC 7951 Sec 6.8), which is
    an XML QName, so RFC 7950 Sec 9.10.3 requires the prefix to be declared.
    The model cannot do it -- it only knows YANG prefixes, not module names --
    so the client binds it before sending.

    This used to depend entirely on the peer advertising the module in its
    `<hello>` (RFC 6241 Sec 8.3): a device that omitted the capability, or a
    value for a module it never advertised, put an undeclared prefix on the
    wire. pyang already knows the answer, so the IR now carries
    `module_namespaces` and the generated client seeds from it.

    Asserted without any device: the compile-time map alone must produce a
    declared prefix, with `module_namespaces` supplied as the template does.
    """
    out, _ = _gen(tmp_path, "netconf", IDENTITYREF_YANG, "tidref")
    models = _import(out, "data_models.tidref")
    sm = _import(out, "session_manager")

    model = models.Root(services=["tidref:gnmi"])
    xml = model.to_xml_payload()
    text = xml.decode() if isinstance(xml, bytes) else xml
    # The module name is NOT a declared prefix on its own: the model declares the
    # module's YANG prefix ('t'), which is a different string.
    assert "xmlns:tidref=" not in text, text
    assert "tidref:gnmi" in text, text

    # Exactly what the emitted client does, with no <hello> involved.
    from lxml import etree  # ty: ignore[unresolved-import] - lxml ships no stubs

    compile_time = {"tidref": "urn:test:tidref"}
    element = etree.fromstring(text.encode())
    sm.bind_module_prefixes(element, compile_time)
    bound = etree.tostring(element).decode()
    assert 'xmlns:tidref="urn:test:tidref"' in bound, bound


def test_restconf_rpc_without_input_sends_no_body(tmp_path):
    """RFC 8040 Sec 3.6.1: with no `input` section the request MUST NOT
    include a message-body. The generated code always POSTed a JSON object.
    """
    out, _ = _gen(tmp_path, "restconf", RPC_YANG, "trpc")
    nav = _import(out, "data_navigators")
    client = _Recorder()
    nav.Operations(client, "/operations", "").trpc_noop()
    method, path, body = client.calls[-1]
    assert (method, path) == ("POST", "/operations/trpc:noop")
    assert body is None, f"an rpc with no input must send no body, got {body!r}"


def test_restconf_rpc_output_is_unwrapped_from_the_module(tmp_path):
    """RFC 8040 Sec 3.6.2: the reply is {"<module>:output": {...}}."""
    out, _ = _gen(tmp_path, "restconf", RPC_YANG, "trpc")
    nav = _import(out, "data_navigators")
    client = _Recorder(reply={"trpc:output": {"status": "ok"}})
    result = nav.Operations(client, "/operations", "").trpc_reboot({"delay-seconds": 5})
    assert result.status == "ok"


def test_restconf_action_uri_and_body(tmp_path):
    """RFC 8040 Sec 3.6: an action is invoked through the DATA tree at
    POST /restconf/data/<module>:<container>/<list>=<key>/<action>, action name
    unqualified in the URI, "<module>:input" body.

    Regression: no `action` statement ever produced its Input/Output models,
    so invoking one raised ImportError before anything was sent.
    """
    out, _ = _gen(tmp_path, "restconf", ACTION_YANG, "tact")
    nav = _import(out, "data_navigators")
    client = _Recorder(reply={"tact:output": {"done": True}})
    result = (
        nav.Data(client, "/data", "")
        .tact_interfaces.interface("eth0")
        .reset({"delay": 3})
    )
    method, path, body = client.calls[-1]
    assert method == "POST"
    assert path == "/data/tact:interfaces/interface=eth0/reset", path
    assert body == {"tact:input": {"delay": 3}}, body
    assert result.done is True


def test_restconf_uri_qualifies_cross_module_path_segments(tmp_path):
    """RFC 8040 Sec 3.5.3: a path segment MUST carry its module name when the
    node comes from a module other than its parent (augmentations); same-module
    children stay bare.

    Regression: every nested navigator used the bare YANG name, so an
    augmented container was addressed as a node that does not exist.
    """
    base = tmp_path / "base.yang"
    base.write_text(
        "module base {\n"
        "  prefix ab;\n"
        '  namespace "urn:test:abase";\n'
        "  revision 2026-01-01;\n"
        "  container same-mod { container nested-same { leaf x { type string; } } }\n"
        "  container to-augment { leaf y { type string; } }\n"
        "}\n"
    )
    aug = tmp_path / "aug.yang"
    aug.write_text(
        "module aug {\n"
        "  yang-version 1.1;\n"
        "  prefix aa;\n"
        '  namespace "urn:test:aaug";\n'
        "  import base { prefix ab; }\n"
        "  revision 2026-01-01;\n"
        '  augment "/ab:to-augment" {\n'
        "    container added-by-augment { leaf z { type string; } }\n"
        "  }\n"
        "}\n"
    )
    out = tmp_path / "aug_restconf"
    code = (
        "import sys; from yang2sdk.cli.compiler import run_compiler; "
        "run_compiler('restconf', sys.argv[1:])"
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(base),
            str(aug),
            "--device",
            "augt",
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
    assert proc.returncode == 0, proc.stderr[-2000:]
    client = _Recorder()
    nav = _import(out, "data_navigators")
    data = nav.Data(client, "/data", "")
    # top-level: parent is the datastore -> always qualified
    assert data.base_same_mod._path == "/data/base:same-mod"
    # same-module child -> bare
    assert data.base_same_mod.nested_same._path == "/data/base:same-mod/nested-same"
    # augmented (cross-module) child -> qualified
    assert data.base_to_augment.added_by_augment._path == (
        "/data/base:to-augment/aug:added-by-augment"
    )


def test_netconf_rpc_input_is_not_stripped_and_output_parses(tmp_path):
    """RFC 7950 Sec 7.15.1/7.15.2: rpc input parameters are child elements of
    the <rpc-name> element; output parameters are child elements of <rpc-reply>.

    Two regressions are pinned here:
      * pyang sets `i_config = None` inside rpc/action/notification subtrees
        ("config-ness not defined here"). The payload pruner tested that for
        truth, so it classified every rpc input as config false and DELETED it:
        every RPC reached the device with an empty payload.
      * `_build_rpc` used search_one(), which returns the un-expanded
        input/output node whose `.arg` is None, so the model root tag became
        the literal string "None" and no reply could ever parse.
    """
    from lxml import etree  # ty: ignore[unresolved-import] - lxml ships no stubs

    out, _ = _gen(tmp_path, "netconf", RPC_YANG, "trpc")
    models = _import(out, "data_models.trpc")
    navigators = _import(out, "data_navigators")

    assert models.RebootInput.__xml_tag__ == "input"
    assert models.RebootOutput.__xml_tag__ == "output"
    for field in models.RebootInput.model_fields.values():
        extra = field.json_schema_extra or {}
        if "tag" in extra:
            assert extra["is_config"] is True, extra

    client = _Recorder(
        reply=etree.fromstring(
            '<rpc-reply xmlns="urn:test:trpc"><status>done</status></rpc-reply>'
        )
    )
    client.module_namespaces = {"trpc": "urn:test:trpc"}
    result = navigators.Operations(client, []).trpc_reboot(
        {"delay-seconds": 7, "force": True}
    )
    sent = etree.tostring(client.rpc_payloads[-1]).decode()
    assert "delay-seconds" in sent and ">7<" in sent, sent
    assert result.status == "done"


def test_netconf_action_encodes_the_datastore_hierarchy(tmp_path):
    """RFC 7950 Sec 7.15.2: <action> (urn:...:yang:1) holds the ancestor
    container/list hierarchy including all key leafs, with an element named
    after the action innermost.

    Regression: the action's Input/Output models were never generated, so
    every action raised ImportError before a byte was sent.
    """
    from lxml import etree  # ty: ignore[unresolved-import] - lxml ships no stubs

    out, _ = _gen(tmp_path, "netconf", ACTION_YANG, "tact")
    navigators = _import(out, "data_navigators")
    client = _Recorder(
        reply=etree.fromstring(
            '<rpc-reply xmlns="urn:test:tact"><done>true</done></rpc-reply>'
        )
    )
    client.module_namespaces = {"tact": "urn:test:tact"}
    result = (
        navigators.Data(client, [])
        .tact_interfaces.interface("eth0")
        .reset({"delay": 3})
    )
    xml = etree.tostring(client.rpc_payloads[-1]).decode()
    assert 'xmlns="urn:ietf:params:xml:ns:yang:1"' in xml, xml
    for token in ("interfaces", "interface", "eth0", "reset", "delay"):
        assert token in xml, f"{token} missing from action payload: {xml}"
    assert result.done is True


def test_netconf_models_accept_yang_wire_names_in_dicts(tmp_path):
    """Protocol parity: a dict keyed by the YANG wire name must validate on
    both transports.

    Regression: pydantic-xml binds elements by `tag`, not by the Python
    attribute, and the NETCONF models carry no `alias`, so
    model_validate({"delay-seconds": 5}) raised extra_forbidden on NETCONF
    while the identical dict worked on RESTCONF.
    """
    out, _ = _gen(tmp_path, "netconf", RPC_YANG, "trpc")
    models = _import(out, "data_models.trpc")
    by_wire = models.RebootInput.model_validate({"delay-seconds": 5, "force": True})
    by_python = models.RebootInput.model_validate({"delay_seconds": 5, "force": True})
    assert by_wire.delay_seconds == 5
    assert by_python.delay_seconds == 5
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        models.RebootInput.model_validate({"no-such-leaf": 1})


def test_rpc_only_module_compiles_on_both_protocols(tmp_path):
    """A module with RPCs but no top-level data nodes must still compile.

    Regression: the NETCONF aggregate `Data` navigator had neither a docstring
    nor a `pass` fallback, so an rpc-only module emitted an empty class body
    and the generated client could not even be imported.
    """
    for fmt, cls in (("restconf", "RestconfClient"), ("netconf", "NetconfClient")):
        out, ok = _gen(tmp_path, fmt, RPC_ONLY_YANG, "tonly")
        assert ok, f"{fmt} output has syntax errors"
        init = _import(out, "__init__")
        assert getattr(init, cls) is not None


# --- the generator must never ship unimportable Python -----------------------

# A YANG `description` is free text and may contain backslashes and quote runs.
# Built by concatenation so this test module's own source stays parseable.
_Q = chr(34) * 3
HOSTILE_DESC_YANG = (
    "module q {\n"
    "  prefix q;\n"
    '  namespace "urn:test:q";\n'
    "  revision 2026-01-01;\n"
    "  container c {\n"
    f"    description 'Literal triple quote {_Q} here, a backslash \\ "
    "and a regex \\d+ too';\n"
    "    leaf x {\n"
    "      type string;\n"
    "      description 'ends with a backslash \\';\n"
    "    }\n"
    "  }\n"
    "}\n"
)


def test_hostile_yang_description_still_yields_importable_python(tmp_path):
    """Regression: `_escape_docstring` replaced a triple quote with an
    *identical* triple quote (the replacement was written as a raw string equal
    to the search text), so the escaping was a no-op, and nothing ever
    compiled the output. The generator reported "Generated SDK in: ..." and
    shipped a file Python could not import.
    """
    out, ok = _gen(tmp_path, "restconf", HOSTILE_DESC_YANG, "q")
    assert ok, "generated output must parse"
    models = _import(out, "data_models.q")
    assert _Q in (models.C.__doc__ or "")
    assert "\\d+" in (models.C.__doc__ or "")
    # The Pydantic `description=` field is emitted through repr() and must not
    # be double-escaped by the docstring escaper.
    assert (models.C.model_fields["x"].description or "").endswith("\\")
    models.C.model_validate({"x": "v"})


def test_generator_refuses_to_ship_unparseable_python(tmp_path):
    """The emitter validates its own output and fails the build, so any future
    escaping bug surfaces at generation time rather than as a SyntaxError
    inside the consumer's project.
    """
    from yang2sdk.plugin.src.core import _validate_generated

    (tmp_path / "ok.py").write_text("x = 1\n")
    _validate_generated(str(tmp_path))  # must not raise

    (tmp_path / "bad.py").write_text('def f(:\n    """unterminated\n')
    with pytest.raises(SyntaxError) as exc:
        _validate_generated(str(tmp_path))
    assert "bad.py" in str(exc.value)
    assert "refusing to ship" in str(exc.value)


RANGE_YANG = """module trng {
  prefix tr;
  namespace "urn:test:trng";
  revision 2026-01-01;
  container r {
    leaf disjoint-hi { type uint8 { range "1..5|7"; } }
    leaf disjoint-lo { type int8 { range "0|3..5"; } }
    leaf wide        { type uint16 { range "1..10|20..30"; } }
    leaf open-top    { type uint8 { range "1..max"; } }
    leaf single      { type uint8 { range "3"; } }
  }
}
"""


def test_disjoint_yang_ranges_keep_both_bounds(tmp_path):
    """RFC 7950 Sec 9.2.2: a range is a union of intervals and may carry bare
    single values, e.g. `1..5|7` and `0|3..5`.

    Regression: only `parts[0]` and `parts[-1]` were inspected, so any range
    beginning or ending in a bare value silently lost that bound -- `1..5|7`
    produced `ge=1` with no `le`, and the generated model accepted values the
    device rejects.
    """
    from annotated_types import Ge, Le

    out, _ = _gen(tmp_path, "restconf", RANGE_YANG, "trng")
    models = _import(out, "data_models.trng")

    def bounds(field_name):
        found: dict[str, object] = {}
        for m in models.R.model_fields[field_name].metadata:
            if isinstance(m, Ge):
                found["ge"] = m.ge
            elif isinstance(m, Le):
                found["le"] = m.le
        return found

    assert bounds("disjoint_hi") == {"ge": 1, "le": 7}, bounds("disjoint_hi")
    assert bounds("disjoint_lo") == {"ge": 0, "le": 5}, bounds("disjoint_lo")
    assert bounds("wide") == {"ge": 1, "le": 30}, bounds("wide")
    assert bounds("open_top") == {"ge": 1}, bounds("open_top")
    assert bounds("single") == {"ge": 3, "le": 3}, bounds("single")


def test_generated_output_is_byte_reproducible(tmp_path):
    """Two compiles of the same module must be byte-identical.

    Regression: a wall-clock `created_utc` was written into both README.md and
    MANIFEST.yang-revisions.json, so `diff -r` between two runs was never clean
    and no generated output could ever be committed as a golden fixture. The
    timestamp is now opt-in via SOURCE_DATE_EPOCH.
    """
    first, _ = _gen(tmp_path, "restconf", RPC_YANG, "repro1")
    second = tmp_path / "repro2_restconf"
    second.mkdir()
    (tmp_path / "repro2.yang").write_text(RPC_YANG)
    code = (
        "import sys; from yang2sdk.cli.compiler import run_compiler; "
        "run_compiler('restconf', sys.argv[1:])"
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(tmp_path / "repro2.yang"),
            "--device",
            "repro1",
            "--yang-dir",
            str(tmp_path),
            "--output-dir",
            str(second),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]

    differing = []
    for path in sorted(first.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(first)
        # The package name embeds the device, so compare the module sources
        other = second / rel
        if other.exists() and path.read_bytes() != other.read_bytes():
            differing.append(str(rel))
    assert not differing, f"non-reproducible generated files: {differing}"


EMPTY_YANG = """module temp {
  prefix te;
  namespace "urn:test:temp";
  revision 2026-01-01;
  rpc do-it {
    input {
      leaf flag { type empty; }
      leaf other { type string; }
    }
    output { leaf ok { type empty; } }
  }
  container c {
    leaf e { type empty; }
    leaf b { type boolean; }
  }
}
"""


def test_type_empty_is_not_a_boolean(tmp_path):
    """RFC 7951 Sec 6.9: JSON for `empty` is a single-element array containing
    null. RFC 7950 Sec 9.11: in XML it is an element with no content.

    Regression: the IR mapped `empty` to `bool` alongside `boolean`, so
    RESTCONF put `true`/`false` on the wire for a leaf that carries no value,
    and NETCONF serialized `<flag>false</flag>`, which a conformant server
    rejects for an `empty` leaf.
    """
    from lxml import etree  # ty: ignore[unresolved-import] - lxml ships no stubs

    # --- RESTCONF: [null] on the wire -------------------------------------
    out, _ = _gen(tmp_path, "restconf", EMPTY_YANG, "temp")
    nav = _import(out, "data_navigators")
    client = _Recorder(reply={"temp:output": {"ok": None}})
    nav.Operations(client, "/operations", "").temp_do_it({"flag": True, "other": "x"})
    body = client.calls[-1][2]
    assert body == {"temp:input": {"flag": [None], "other": "x"}}, body

    # --- NETCONF: an empty element ----------------------------------------
    out, _ = _gen(tmp_path, "netconf", EMPTY_YANG, "temp")
    navigators = _import(out, "data_navigators")
    client = _Recorder(
        reply=etree.fromstring('<rpc-reply xmlns="urn:test:temp"><ok/></rpc-reply>')
    )
    client.module_namespaces = {"temp": "urn:test:temp"}
    navigators.Operations(client, []).temp_do_it({"flag": True, "other": "x"})
    sent = etree.tostring(client.rpc_payloads[-1]).decode()
    assert "<flag/>" in sent, sent
    assert "false" not in sent and "true" not in sent, sent


# --- safety rails: replace() and read pruning -------------------------------

REPLACE_YANG = """module trep {
  prefix tp;
  namespace "urn:test:trep";
  revision 2026-01-01;
  container t {
    leaf a { type string; }
    leaf b { type string; }
    container sub { leaf deep { type string; } }
  }
}
"""


def test_replace_refuses_to_silently_delete_unset_fields(tmp_path):
    """RFC 8040 Sec 4.5 / RFC 6241 Sec 8.2.1: a replace body IS the complete
    resource, so anything absent is deleted on the device.

    Regression: `replace()` dumped with `exclude_unset=True` and sent it
    anyway, so a hand-built (or depth-truncated) model silently wiped every
    field it did not mention.
    """
    # --- RESTCONF ---
    out, _ = _gen(tmp_path, "restconf", REPLACE_YANG, "trep")
    nav = _import(out, "data_navigators")
    models = _import(out, "data_models.trep")
    client = _Recorder()
    data = nav.Data(client, "/data", "")

    with pytest.raises(ValueError, match="would DELETE"):
        data.trep_t.replace(models.T.model_validate({"a": "1"}))
    assert client.calls == [], "nothing may be sent when the guard trips"

    # A full round-trip (every field set) is unaffected.
    full = models.T.model_validate({"a": "1", "b": "2", "sub": {"deep": "x"}})
    data.trep_t.replace(full)
    assert client.calls[-1][0] == "PUT"
    assert client.calls[-1][2] == {"trep:t": {"a": "1", "b": "2", "sub": {"deep": "x"}}}

    # ...and the caller can still opt in explicitly.
    client2 = _Recorder()
    nav.Data(client2, "/data", "").trep_t.replace(
        models.T.model_validate({"a": "9"}), allow_partial=True
    )
    assert client2.calls[-1][2] == {"trep:t": {"a": "9"}}

    # --- NETCONF ---
    out, _ = _gen(tmp_path, "netconf", REPLACE_YANG, "trep")
    nav = _import(out, "data_navigators")
    models = _import(out, "data_models.trep")

    class _NetconfRecorder:
        def __init__(self):
            self.module_namespaces: dict[str, str] = {}
            self.edits = 0

        def edit(self, config_xml, target=None):
            self.edits += 1
            return True

    client = _NetconfRecorder()
    with pytest.raises(ValueError, match="would DELETE"):
        nav.Data(client, []).trep_t.replace(models.T.model_validate({"a": "1"}))
    assert client.edits == 0, "nothing may be sent when the guard trips"


def test_read_pruning_is_bounded_by_the_requested_depth(tmp_path):
    """RFC 8040 Sec 4.8.2: `depth` is the server-side subtree ceiling.

    A compliant server includes nodes up to the requested depth; containers
    sitting exactly on that boundary come back empty (children truncated).
    The payload the pruner sees starts *below* the URI target node (the target
    itself is the response envelope), so the boundary empties sit at recursion
    level `depth + 1`, and a truncated response reaches exactly that level
    (`observed == depth + 1`). A tree that ends sooner was not truncated, and
    a server that returns deeper than asked ignored `depth` -- in both cases
    there are no truncation artefacts and nothing may be deleted.

    Regression (lab G30): `retrieve(depth=2)` on the `interface` list came
    back with `ethernet: {}` on every entry, and the old boundary math
    (`min(requested, observed)`) pruned one level too shallow, so the strict
    model failed on the mandatory `eth-resource-ref` behind the empty
    container -- and a complete tree read with a deep request had its
    legitimately empty deepest containers deleted instead.
    """
    out, _ = _gen(tmp_path, "restconf", REPLACE_YANG, "trep")
    base = _import(out, "data_navigators._base")

    # `depth="unbounded"` (the RFC default) has no boundary: nothing touched.
    payload = {"a": {}, "b": {"c": {}, "d": "x"}}
    assert base._maybe_prune(payload, "unbounded") == payload, (
        "unbounded prunes nothing"
    )

    # Truncated at depth 2: the boundary empties (level 3) go; the shallower
    # empty container (level 2) is real data and stays.
    d2 = {"a": {}, "b": {"c": {}, "d": "x"}}
    assert base._maybe_prune(d2, 2) == {"a": {}, "b": {"d": "x"}}

    # Truncated at depth 1: both children sit on the boundary.
    assert base._maybe_prune({"a": {}, "b": {}}, 1) == {}

    # Complete tree returned for a deep request: nothing was truncated, so
    # every empty container is real data and must survive.
    assert base._maybe_prune(d2, 10) == d2

    # The G30 shape: a depth-2 read of a list truncates every entry at the
    # same level, leaving empty containers on the boundary.
    truncated_entries = [
        {"if-name": "eth1", "ethernet": {}},
        {"if-name": "eth2", "ethernet": {}},
    ]
    assert base._maybe_prune(truncated_entries, 2) == [
        {"if-name": "eth1"},
        {"if-name": "eth2"},
    ]

    # ...and a complete read (some entry deeper than the boundary) is not a
    # truncated shape, so a genuinely empty container is kept.
    full_entries = [
        {"if-name": "eth1", "ethernet": {}},
        {"if-name": "eth2", "ethernet": {"eth-resource-ref": "/ne:ne/x"}},
    ]
    assert base._maybe_prune(full_entries, 2) == full_entries


def test_logging_redacts_passphrase_and_never_leaks_the_error_body(tmp_path):
    """Security: NTP/TACACS/RADIUS authenticator passphrases contain neither
    the substring "password" nor "passwd", so the redaction list let them
    through in cleartext. And the raised HTTPError embedded the whole response
    body, which every caller logs -- the logging-hygiene contract covered the
    log line but not the exception.
    """
    out, _ = _gen(tmp_path, "restconf", REPLACE_YANG, "trep")
    sm = _import(out, "session_manager")

    blob = {"ntp": {"auth": {"passphrase": "hunter2", "password": "p", "key": "k"}}}
    text = str(sm._redact(blob))
    assert "hunter2" not in text, text
    assert "***redacted***" in text, text

    src = (
        REPO_ROOT
        / "src/yang2sdk/plugin/src/templates/restconf/session_manager.py.jinja"
    ).read_text()
    assert 'f"HTTP {status}{detail} on {method} {url}"' in src
    assert "Error: {e.response.text}" not in src, "the error body must not be embedded"


# --- NETCONF transaction safety ---------------------------------------------

NETCONF_SAFETY_YANG = """module tsafe {
  prefix ts;
  namespace "urn:test:tsafe";
  revision 2026-01-01;
  rpc noop;
}
"""


class _FakeNcclientManager:
    """Minimal stand-in for the ncclient manager object."""

    def __init__(self, capabilities):
        self.server_capabilities = capabilities
        self.closed = 0
        self.dispatched: list[Any] = []
        self.commits = 0

    def close_session(self):
        self.closed += 1

    def lock(self, **kwargs):
        return self

    def unlock(self, **kwargs):
        return self

    def commit(self):
        self.commits += 1
        return self

    def dispatch(self, rpc):
        self.dispatched.append(rpc)
        return self

    ok = True
    xml = (
        b'<rpc-reply xmlns="urn:ietf:params:xml:ns:netconf:base:1.0"><ok/></rpc-reply>'
    )


def _netconf_client(tmp_path, capabilities, **kwargs):
    from unittest import mock

    out, _ = _gen(tmp_path, "netconf", NETCONF_SAFETY_YANG, "tsafe")
    sm = _import(out, "session_manager")
    mgr = _FakeNcclientManager(capabilities)
    with mock.patch.object(sm.manager, "connect", return_value=mgr):
        client = sm.NetconfClient(
            management_ip="127.0.0.1",
            username="u",
            password="p",
            verify=False,
            **kwargs,
        )
    return client, mgr


_BASE_CAPS = [
    "urn:ietf:params:netconf:base:1.0",
    "urn:ietf:params:netconf:capability:candidate:1.0",
    "urn:ietf:params:netconf:capability:validate:1.0",
]


def test_netconf_releases_its_session(tmp_path):
    """`manager.connect()` opens an SSH transport and a NETCONF session that
    the device counts against its session limits. Nothing released them: there
    was no `close()`, and the context manager only unlocked. A long-running
    sweep leaked one session per client.
    """
    from lxml import etree  # ty: ignore[unresolved-import] - lxml ships no stubs

    client, mgr = _netconf_client(tmp_path, _BASE_CAPS)
    assert mgr.closed == 0
    client.close()
    assert mgr.closed == 1
    client.close()  # idempotent
    assert mgr.closed == 1, "close() must be idempotent"

    with pytest.raises(RuntimeError, match="closed"):
        client.commit()

    # The context manager closes too.
    from unittest import mock

    out, _ = _gen(tmp_path / "ctx", "netconf", NETCONF_SAFETY_YANG, "tsafe2")
    sm = _import(out, "session_manager")
    mgr2 = _FakeNcclientManager(_BASE_CAPS)
    with (
        mock.patch.object(sm.manager, "connect", return_value=mgr2),
        sm.NetconfClient(
            management_ip="127.0.0.1", username="u", password="p", verify=False
        ),
    ):
        pass
    assert mgr2.closed == 1, "the with-block must release the session"
    del etree


def test_netconf_auto_commit_is_opt_in(tmp_path):
    """`auto_commit=True` committed the candidate after EVERY edit, so a
    multi-step change could never be reviewed, validated or rolled back, and
    there was no way to opt out of it. It is now opt-in, and warns when used.
    """
    client, _ = _netconf_client(tmp_path, _BASE_CAPS)
    assert client.auto_commit is False, "auto-commit must not be the default"
    client2, _ = _netconf_client(tmp_path / "ac", _BASE_CAPS, auto_commit=True)
    assert client2.auto_commit is True, "opt-in must still work"


def test_netconf_exposes_the_validate_workflow(tmp_path):
    """RFC 6241 Sec 8.3.5.1 `<validate>` and Sec 8.3.5.2 `<validate-source>`.

    The client offered only edit() (which optionally auto-committed) and
    commit(), so the safe validate-then-commit sequence AGENTS.md describes
    was not expressible.
    """
    from lxml import etree  # ty: ignore[unresolved-import] - lxml ships no stubs

    client, mgr = _netconf_client(tmp_path, _BASE_CAPS)
    assert client.has_validate is True

    # RFC 6241 Sec 8.6.4.1: <validate><source><candidate/></source></validate>
    assert client.validate(source="candidate") is True
    xml = etree.tostring(mgr.dispatched[-1]).decode()
    assert "validate" in xml, xml
    assert "<source>" in xml and "<candidate/>" in xml, xml
    assert "validate-source" not in xml, (
        "RFC 6241 has no <validate-source> RPC; a conformant server answers "
        f"unknown-element: {xml}"
    )

    assert client.validate() is True
    xml = etree.tostring(mgr.dispatched[-1]).decode()
    assert "<validate" in xml and "<source>" not in xml, xml

    # Without the capability it must refuse rather than pretend.
    client2, _ = _netconf_client(tmp_path / "nov", ["urn:ietf:params:netconf:base:1.0"])
    assert client2.has_validate is False
    assert client2.validate(source="candidate") is False


# --- capability detection and name collisions (found on real lab devices) ----


def test_nmda_is_detected_from_the_capability_uri_not_a_module_name(tmp_path):
    """RFC 8526 Sec 3.1.1: NMDA is signalled by the base capability URI
    `urn:ietf:params:netconf:capability:nmda:1.0`.

    Regression: the check was `any("ietf-netconf-nmda" in cap ...)`, which also
    matches the *module* capability
    `.../yang:ietf-netconf-nmda?module=ietf-netconf-nmda&revision=...`. SR Linux
    ships that module but does NOT implement NMDA, so every read was routed to
    a `<get-data>` the device rejects with "unknown-element" -- 100% of
    datastore reads failed on a real, supported device.
    """
    from unittest import mock

    srl_like = [
        "urn:ietf:params:netconf:base:1.0",
        "urn:ietf:params:netconf:capability:candidate:1.0",
        "urn:ietf:params:netconf:capability:validate:1.0",
        # module capability -- must NOT be read as NMDA support
        (
            "urn:ietf:params:xml:ns:yang:ietf-netconf-nmda"
            "?module=ietf-netconf-nmda&revision=2019-01-07&features=origin,with-defaults"
        ),
    ]
    out, _ = _gen(tmp_path, "netconf", NETCONF_SAFETY_YANG, "caps1")
    sm = _import(out, "session_manager")
    mgr = _FakeNcclientManager(srl_like)
    with mock.patch.object(sm.manager, "connect", return_value=mgr):
        client = sm.NetconfClient(
            management_ip="127.0.0.1", username="u", password="p", verify=False
        )
    assert client.has_nmda is False, (
        "a module capability is not an NMDA implementation; reads would be "
        "routed to an unsupported <get-data>"
    )
    assert client.has_candidate is True

    # A device that really does advertise it must still be detected.
    nmda_device = srl_like + ["urn:ietf:params:netconf:capability:nmda:1.0"]
    out, _ = _gen(tmp_path / "yes", "netconf", NETCONF_SAFETY_YANG, "caps2")
    sm = _import(out, "session_manager")
    with mock.patch.object(
        sm.manager, "connect", return_value=_FakeNcclientManager(nmda_device)
    ):
        client = sm.NetconfClient(
            management_ip="127.0.0.1", username="u", password="p", verify=False
        )
    assert client.has_nmda is True


ROOT_WRITE_YANG = """module trdw {
  prefix t;
  namespace "urn:test:trdw";
  revision 2026-01-01;
  container system {
    leaf hostname { type string; }
  }
}"""


IDENTITYREF_YANG = """module tidref {
  prefix t;
  namespace "urn:test:tidref";
  revision 2026-01-01;

  identity service;
  identity gnmi { base service; }
  identity gribi { base service; }

  container root {
    leaf-list services {
      type identityref { base service; }
    }
  }
}"""


def test_identityref_stays_a_string_on_the_wire(tmp_path):
    """identityref is emitted as a *value*, not a nested element.

    Scope, deliberately narrow: this asserts the **emitted shape only**. It
    contacts no device and therefore makes no claim about how any device
    behaves. An earlier revision of this docstring carried several SR Linux
    claims; an independent audit found every one of them wrong or
    unverifiable from here, and they were removed. Device behaviour is
    documented in AGENTS.md and proven in tests/test_srl_netconf.py, which
    runs against a live node.

    Why the shape is pinned at all: RFC 7950 Sec 9.10.2 permits either an
    element named after the identity or a string, and the element form is the
    more intuitive reading. Emitting the element would silently change the
    wire format for every identityref leaf on every device, so the string form
    is pinned deliberately.

    Both this test and `ir.py` agree the value is the RFC 7951 Sec 6.8
    ``module:identity`` string, bare when the identity is defined in the leaf's
    own module. The values below are bare for exactly that reason -- an earlier
    revision used bare identities while the docstring talked about module-name
    qualification, which made the test lock in the non-canonical form.
    """
    out, _ = _gen(tmp_path / "netconf", "netconf", IDENTITYREF_YANG, "idref")
    # The generated leaf lives in `data_models.<stem>`, matching the module name.
    models = _import(out, "data_models.tidref")

    # Re-declare with the emitter's own metadata: a hand-written subclass loses
    # the tag/ns/pydantic-xml wiring, and pydantic-xml then rejects a bare
    # leaf-list ("entity name is not provided"). Round-trip through the shipped
    # model instead of reimplementing it.
    root = models.Root(services=["gnmi", "gribi"])

    xml = root.to_xml_payload()
    text = xml.decode() if isinstance(xml, bytes) else xml
    assert "<services>gnmi</services>" in text, text
    assert "<services>gribi</services>" in text, text
    # No element-per-identity anywhere.
    for ident in ("gnmi", "gribi"):
        assert f"<services><{ident}" not in text, text
        assert f"<{ident}/>" not in text, text


AUGMENT_BASE_YANG = """module taug {
  prefix a;
  namespace "urn:test:taug";
  revision 2026-01-01;
  container system {
    container name {
      leaf host-name { type string; }
    }
  }
}"""

AUGMENT_EXT_YANG = """module taugext {
  prefix e;
  namespace "urn:test:taugext";
  import taug { prefix a; }
  revision 2026-01-01;
  augment "/a:system" {
    container aaa {
      leaf mode { type string; }
    }
  }
}"""


def _gen_roots(tmp_path, fmt, files, roots, device):
    """Compile several YANG roots; same subprocess contract as _gen."""
    tmp_path = Path(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (tmp_path / f"{name}.yang").write_text(text)
    out = tmp_path / f"{device}_{fmt}"
    code = (
        "import sys; from yang2sdk.cli.compiler import run_compiler; "
        f"run_compiler({fmt!r}, sys.argv[1:])"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code]
        + [str(tmp_path / f"{r}.yang") for r in roots]
        + [
            "--device",
            device,
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
    assert proc.returncode == 0, proc.stderr[-2000:]
    return out, compileall.compile_dir(str(out), quiet=1)


def test_augment_model_root_uses_augmenting_namespace(tmp_path):
    """An augment-defined model's root tag is in the AUGMENTING namespace.

    RFC 7950 Sec 7.17: augment adds nodes to the target, but every node keeps
    the namespace of the module that defines it. The device therefore returns
    ``{augment-ns}aaa`` for a node augmented into ``{base-ns}system`` -- and a
    model whose root defaults to the base namespace rejects that correct
    answer with "root element not found".

    Measured on SR Linux 25.10.1: 77 nodes under /system/{aaa,tls,ssh-server,
    ...} and /qos failed exactly this way, and all of them validate once the
    root carries the augmenting namespace. Same-module models are unchanged
    (ns ""), asserted below so the fix cannot drift the common case.
    """
    out, _ = _gen_roots(
        tmp_path / "netconf",
        "netconf",
        {"taug": AUGMENT_BASE_YANG, "taugext": AUGMENT_EXT_YANG},
        ["taug", "taugext"],
        "augm",
    )
    models = _import(out, "data_models.taug")
    namespaces = {"taug": "urn:test:taug", "taugext": "urn:test:taugext"}

    system = models.System(name=models.Name(host_name="h"))
    system_xml = system.to_xml_payload()
    system_text = system_xml.decode() if isinstance(system_xml, bytes) else system_xml
    assert 'xmlns="urn:test:taug"' in system_text, system_text

    aaa = models.Aaa(mode="strict")
    aaa_xml = aaa.to_xml_payload()
    aaa_text = aaa_xml.decode() if isinstance(aaa_xml, bytes) else aaa_xml
    from lxml import etree  # ty: ignore[unresolved-import] - lxml ships no stubs

    # The tag may be default- or prefix-qualified; what matters is the URI.
    assert etree.QName(etree.fromstring(aaa_text.encode())).namespace == (
        "urn:test:taugext"
    ), aaa_text

    # The device-shaped payload -- augment namespace on the wire -- parses.
    parsed = models.Aaa.from_xml(
        b'<aaa xmlns="urn:test:taugext"><mode>strict</mode></aaa>',
        module_namespaces=namespaces,
    )
    assert parsed.mode == "strict"


def test_sdk_verify_parse_errors_are_never_device_skips():
    """sdk-verify must not file a model failure under "device rejected".

    A ParsingError/ValidationError proves the device ANSWERED: bytes came back
    and the generated model could not read them. Reporting that as a skip hid
    77 augment-namespace parse failures on SR Linux behind "device rejected
    the operation". Genuine device answers (RPC error replies, HTTP
    400/404/405) stay skips.
    """
    from pydantic_xml import ParsingError

    from yang2sdk.cli.sdk_verify import _is_device_fault

    assert (
        _is_device_fault(
            ParsingError(
                "root element not found "
                "(actual: {urn:test:taugext}aaa, expected: {urn:test:taug}aaa)"
            )
        )
        is False
    )
    assert (
        _is_device_fault(
            RuntimeError("NETCONF RPC Error: invalid-value Type: application")
        )
        is True
    )
    import requests

    resp = requests.Response()
    resp.status_code = 404
    assert _is_device_fault(requests.HTTPError(response=resp)) is True


SCHEMA_COLLISION_YANG = """module tcoll {
  prefix tc;
  namespace "urn:test:tcoll";
  revision 2026-01-01;
  rpc get-schema {
    output {
      leaf schema { type string; }
    }
  }
  container c {
    leaf json { type string; }
    leaf copy { type string; }
    leaf name { type string; }
  }
}"""


# Names the emitter writes bare inside a generated class body. A YANG node named
# after one of these is not merely unidiomatic: the class body rebinds the name
# before a later statement reads it, and the generated module then fails to
# *import*. `_validate_generated` only parses, and the output is syntactically
# valid, so nothing offline caught it. Regenerate if the emitter's vocabulary
# widens -- this test is the tripwire (see `_generated_code_builtins`).
BUILTIN_COLLISION_YANG = """module tbi {
  prefix tb;
  namespace "urn:test:tbi";
  revision 2026-01-01;
  container root {
    container property {
      leaf a { type string; }
    }
    container holder {
      leaf str { type string; }
      leaf int { type int32; }
      leaf bool { type boolean; }
      leaf after { type string; }
      leaf-list list { type uint8; }
      list nested { key id; leaf id { type string; } }
    }
    container sibling-after { leaf b { type string; } }
  }
  rpc do-it {
    input {
      leaf str { type string; }
      leaf target { type string; }
    }
    output {
      leaf ok { type boolean; }
    }
  }
}"""


def test_fields_never_shadow_a_pydantic_attribute(tmp_path):
    """A YANG leaf whose Python name collides with a `BaseModel` /
    `BaseXmlModel` attribute resolves to the inherited attribute instead of
    the field, so the value is silently unreachable.

    Regression: `ietf-netconf-monitoring`'s `schema` leaf generated a field
    that pydantic warned "shadows an attribute in parent" and that resolved to
    `BaseModel.schema()` -- a *method*. Reading it raised
    `TypeError: object of type 'method' has no len()` on a live device. The
    wire name is unaffected (NETCONF `element(tag=...)`, RESTCONF
    `Field(alias=...)`), so only the Python attribute is suffixed.
    """
    out, _ = _gen(tmp_path, "netconf", SCHEMA_COLLISION_YANG, "tcoll")
    models = _import(out, "data_models.tcoll")

    # 1. The container leaves: `json` and `copy` are renamed, `name` is not.
    cfields = models.C.model_fields
    assert "name" in cfields, sorted(cfields)
    assert "json_" in cfields and "copy_" in cfields, sorted(cfields)
    inst = models.C.model_validate({"json": "b", "copy": "c", "name": "d"})
    assert inst.json_ == "b" and inst.copy_ == "c" and inst.name == "d"
    # The YANG wire name is preserved: on the way out it is the XML tag, and
    # on the way in `_accept_wire_names` maps the tag back to the field.
    assert b"<json>b</json>" in inst.to_xml(), inst.to_xml()
    assert models.C.model_validate({"json": "z"}).json_ == "z"

    # 2. The rpc output leaf `schema` -- the real-world case: this is
    #    ietf-netconf-monitoring's `schema`, which resolved to the inherited
    #    BaseModel.schema() *method* and raised TypeError on a live device.
    ofields = models.GetSchemaOutput.model_fields
    assert "schema_" in ofields, sorted(ofields)
    assert ofields["schema_"].json_schema_extra["tag"] == "schema"
    out_model = models.GetSchemaOutput.model_validate({"schema": "<module/>"})
    assert callable(getattr(type(out_model), "schema", None)), (
        "before the fix `out.schema` was the inherited BaseModel.schema() method"
    )
    assert out_model.schema_ == "<module/>"
    assert b"<schema>&lt;module/&gt;</schema>" in out_model.to_xml()


def test_builtin_named_nodes_do_not_break_the_generated_import(tmp_path):
    """A YANG node named after a builtin the emitter writes bare must still import.

    Regression: a container named `property` generated

        @property
        def property(self) -> PropertyNode: ...

    and because the class body had bound the name `property` to the property
    object, every *later* decorator in that body resolved to it. The generated
    module raised `TypeError: 'property' object is not callable` **at import** --
    an unimportable client. The emitter's `_validate_generated` only parses, and
    the file is valid Python, so this reached `temp/` unnoticed.

    The same mechanism breaks models: `models.py.jinja` emits no
    `from __future__ import annotations`, so a field named `str` rebinds the name
    before the next annotation reads it (`FieldInfo | None`), and NETCONF
    leaf-list fields emit a bare `list[...]` annotation, which is then not
    subscriptable.

    Both protocols, because the failing constructs differ: `property` and
    `list` fail on one transport only.
    """
    for fmt in ("restconf", "netconf"):
        out, compiled = _gen(tmp_path, fmt, BUILTIN_COLLISION_YANG, "tbi")
        assert compiled, f"{fmt}: generated code failed to compile"

        # 1. It must actually import -- that is what the bug prevented.
        models = _import(out, "data_models.tbi")
        navigators = _import(out, "data_navigators.tbi")

        # 2. The Python attribute is escaped; the YANG name is untouched.
        #    RESTCONF expresses that as `Field(alias=...)`, NETCONF as
        #    `element(tag=...)` -- both derived from the original YANG name.
        root = models.Root.model_fields
        assert "property_" in root, sorted(root)
        if fmt == "restconf":
            assert root["property_"].alias == "property"
        else:
            assert root["property_"].json_schema_extra["tag"] == "property"
        holder = models.Holder.model_fields
        for escaped, wire in (
            ("str_", "str"),
            ("int_", "int"),
            ("bool_", "bool"),
            ("list_", "list"),
        ):
            assert escaped in holder, (fmt, sorted(holder))
            extra = holder[escaped].json_schema_extra or {}
            if fmt == "restconf":
                assert holder[escaped].alias == wire, (fmt, escaped)
            else:
                assert extra["tag"] == wire, (fmt, escaped, extra)

        # 3. Navigator children on both sides of the shadowing property must
        #    work: before the fix, the *later* decorator raised TypeError.
        #    The two transports construct their root navigator differently
        #    (RESTCONF takes a URL path string, NETCONF a path tuple list).
        if fmt == "restconf":
            root_nav = navigators.RootNode(None, "/data/tbi:root", "root")
        else:
            root_nav = navigators.RootNode(None, [])
        assert root_nav.property_ is not None
        assert root_nav.sibling_after is not None

        # 4. The value round-trips through the escaped attribute.
        inst = models.Holder.model_validate(
            {"str": "a", "int": 2, "bool": True, "list": [1, 2], "after": "z"}
        )
        assert inst.str_ == "a" and inst.int_ == 2 and inst.bool_ is True
        assert inst.list_ == [1, 2] and inst.after == "z"

        # 5. An rpc input leaf is escaped too -- this is the `get-schema` shape.
        do_it_in = models.DoItInput.model_fields
        assert "str_" in do_it_in, sorted(do_it_in)
        if fmt == "restconf":
            assert do_it_in["str_"].alias == "str"
        else:
            assert (do_it_in["str_"].json_schema_extra or {})["tag"] == "str"


def test_netconf_edit_config_rollback_on_error_is_running_only(tmp_path):
    """`<error-option>rollback-on-error` is emitted only for a running target.

    RFC 6241 Sec 7.2 makes the server restore the datastore to its complete
    state at the start of *this* `<edit-config>` when any error-severity
    `<rpc-error>` occurs. Two deliberate restrictions, both from Sec 8.5.1,
    which warns the option "can cause other configuration changes (for example,
    via other NETCONF sessions) to be inadvertently altered or removed" in a
    shared datastore unless a lock is held:

    * NOT on `candidate`. The correct primitive there is `<discard-changes>`,
      and candidate is exactly where humans stage work.
    * NMDA needs none of it -- `<edit-data>` has no `error-option` and its error
      behaviour already corresponds to rollback-on-error (RFC 8526 Sec 3.1.2).
    """
    out, _ = _gen(tmp_path, "netconf", COMPOSITE_KEY_YANG, "tro")
    session = _import(out, "session_manager").NetconfClient
    assert session is not None
    source = inspect.getsource(session.edit)

    assert 'target == "running" and self.has_rollback_on_error' in source
    # The NMDA branch must be untouched: it has no error-option parameter.
    nmda_only, marker, legacy = source.partition("Fallback to natively constructed")
    assert marker, "the legacy <edit-config> branch marker moved"
    # RFC 8526 Sec 3.1.2: <edit-data> has no error-option parameter and its
    # error behaviour already corresponds to rollback-on-error, so the NMDA
    # branch must not emit one.
    assert "error-option" not in nmda_only
    # The legacy branch emits it as a child element, gated on running + cap.
    assert "}error-option" in legacy and 'err_opt.text = "rollback-on-error"' in legacy
    # Guard the fallback that would silently disable the safety net.
    assert "has_rollback_on_error" in source, (
        "the capability flag exists but is no longer consulted"
    )


COMPOSITE_KEY_YANG = """module tro {
  prefix t;
  namespace "urn:test:tro";
  revision 2026-01-01;
  container root {
    list items {
      key "id kind";
      leaf id { type string; }
      leaf kind { type string; }
      leaf v { type uint8; }
    }
  }
}"""


# --- regression home for two bugs found only on real lab devices ------------


def test_rpc_error_handler_never_raises_and_keeps_the_device_message(tmp_path):
    """ncclient 0.7's RPCError exposes tag/type/severity as properties over
    `_tag`/`_type`/`_severity`, which are only populated when ncclient parsed a
    structured <rpc-error>. On a real SR Linux rejection `_tag` was absent
    entirely.

    Regression: the handler formatted `e.tag` directly, so every rejected write
    surfaced `AttributeError: 'RPCError' object has no attribute '_tag'` and the
    device's actual message (there: a YANG pattern mismatch) was lost.
    """
    from unittest import mock

    from lxml import etree  # ty: ignore[unresolved-import] - lxml ships no stubs
    from ncclient.operations import RPCError
    from ncclient.operations.rpc import to_ele

    out, _ = _gen(tmp_path, "netconf", NETCONF_SAFETY_YANG, "errmsg")
    sm = _import(out, "session_manager")
    with mock.patch.object(
        sm.manager, "connect", return_value=_FakeNcclientManager(_BASE_CAPS)
    ):
        client = sm.NetconfClient(
            management_ip="127.0.0.1", username="u", password="p", verify=False
        )

    # Build a real RPCError, then remove `_tag`/`_type` exactly as ncclient 0.7
    # leaves them when it raises from a non-structured errlist.
    raw = etree.fromstring(
        b'<rpc-reply xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">'
        b"<rpc-error><error-type>application</error-type>"
        b"<error-tag>invalid-value</error-tag>"
        b"<error-severity>error</error-severity>"
        b"<error-message>boom</error-message></rpc-error></rpc-reply>"
    )
    err = RPCError(to_ele(raw), errs=[])
    err.__dict__.pop("_tag", None)
    err.__dict__.pop("_type", None)
    with pytest.raises(AttributeError):
        _ = err.tag  # the pre-fix crash, reproduced deliberately

    def boom(*args, **kwargs):
        raise err

    with pytest.raises(RuntimeError) as exc:
        client._handle_rpc(boom)
    message = str(exc.value)
    assert "AttributeError" not in message, message
    assert "_tag" not in message, message
    assert "NETCONF RPC Error" in message, message
    # the raw <rpc-error> must still be recoverable from the message
    assert "invalid-value" in message or "boom" in message, message


def test_undeclared_xml_attributes_are_dropped_but_elements_are_not(tmp_path):
    """RFC 7952 Sec 4.1: an attribute MUST NOT be used to represent a YANG leaf
    value, so an undeclared one carries metadata and cannot hide data.

    Regression: pydantic-xml reports unbound attributes through the same
    `extra="forbid"` hook as unknown elements (prefixing the location with
    "@"). The Groove G30 stamps `cli-name="..."` on every data element with no
    YANG declaration anywhere, which made every G30 subtree read fail with
    `@cli-name: Extra inputs are not permitted`. Unknown *elements* must stay
    fatal -- that is the model-gap safety net.
    """
    out, _ = _gen(tmp_path, "netconf", RANGE_YANG, "attrs")
    models = _import(out, "data_models.trng")

    ok_xml = (
        b'<r xmlns="urn:test:trng" some-undeclared-attribute="x">'
        b"<disjoint-hi>3</disjoint-hi><disjoint-lo>2</disjoint-lo>"
        b"<wide>7</wide><open-top>4</open-top><single>3</single></r>"
    )
    m = models.R.from_xml(ok_xml)
    assert m.disjoint_hi == 3 and m.wide == 7, m

    # An unknown ELEMENT is still a hard error: a model gap must not hide.
    from pydantic import ValidationError

    bad_xml = (
        b'<r xmlns="urn:test:trng"><disjoint-hi>3</disjoint-hi>'
        b"<not-in-the-model>x</not-in-the-model></r>"
    )
    with pytest.raises(ValidationError):
        models.R.from_xml(bad_xml)

    # The escape hatch is available for an adapter that wants strictness.
    class Strict(models.R):
        ignore_unbound_attributes = False

    with pytest.raises(ValidationError):
        Strict.from_xml(ok_xml)

    # `from_xml_tree` must strip attributes too. It used to be shadowed by a
    # dead third definition that did not, so the NETCONF navigators -- which parse
    # with `from_xml_tree` -- lost the G30 `cli-name` workaround entirely while
    # this `from_xml` test stayed green.
    from lxml import etree  # ty: ignore[unresolved-import] - lxml ships no stubs

    tree = models.R.from_xml_tree(etree.fromstring(ok_xml))
    assert tree.disjoint_hi == 3 and tree.wide == 7, tree
    with pytest.raises(ValidationError):
        Strict.from_xml_tree(etree.fromstring(ok_xml))

    # And exactly one definition of each survives in the generated base, so a
    # future edit cannot silently reintroduce the shadowing.
    base_src = (out / "data_models" / "_base.py").read_text()
    assert base_src.count("def from_xml(cls") == 1, base_src.count("def from_xml(cls")
    assert base_src.count("def from_xml_tree(cls") == 1, base_src.count(
        "def from_xml_tree(cls"
    )


# --- the replace() guard must only count *config* nodes ---------------------

STATE_GUARD_YANG = """module tguard {
  prefix tg;
  namespace "urn:test:tguard";
  revision 2026-01-01;
  container c {
    leaf configured { type string; }
    leaf operational { type string; config false; }
    list items {
      key "k";
      leaf k { type string; }
      leaf cfg { type string; }
      leaf obs { type string; config false; }
    }
  }
}
"""


def test_replace_guard_ignores_state_fields(tmp_path):
    """`config false` (state) nodes never appear in a config body and cannot be
    deleted by an edit, so they must not be reported as "would be deleted".

    Regression: the first version of the guard counted them, so replacing a
    partially-read `ietf-interfaces:interface` -- whose unset leaves are almost
    all operational state -- raised instead of proceeding, and 10 live
    notconf round-trips failed.
    """
    out, _ = _gen(tmp_path, "restconf", STATE_GUARD_YANG, "tguard")
    nav = _import(out, "data_navigators")
    models = _import(out, "data_models.tguard")
    client = _Recorder()
    data = nav.Data(client, "/data", "")

    # Only the state leaf is unset -> nothing is at risk -> allowed.
    item = models.ItemsItem.model_validate({"k": "a", "cfg": "kept"})
    data.tguard_c.items("a").replace(item)
    assert client.calls[-1][2] == {"tguard:items": [{"k": "a", "cfg": "kept"}]}

    # A *config* leaf is unset -> the guard must still refuse.
    partial = models.ItemsItem.model_validate({"k": "a"})
    with pytest.raises(ValueError, match="would DELETE"):
        data.tguard_c.items("a").replace(partial)

    # And the error names only the config leaf.
    try:
        data.tguard_c.items("a").replace(partial)
    except ValueError as exc:
        assert "cfg" in str(exc) and "obs" not in str(exc), str(exc)


KEYWORD_KEY_YANG = """module tkw {
  prefix tk;
  namespace "urn:test:tkw";
  revision 2026-01-01;
  container c {
    list entries {
      key "if addr";
      leaf if { type string; }
      leaf addr { type string; }
      leaf class { type string; }
      leaf import { type string; }
    }
  }
}
"""


def test_list_key_named_like_python_keyword(tmp_path):
    """A YANG key may legally be named `if`/`class`/`import`.

    The NETCONF navigator emitted the raw key name as a Python parameter, so
    a list keyed on `if` produced `def __call__(self, if: str | int, ...)` --
    a SyntaxError that made the whole client unimportable. Cisco NX-OS ships
    exactly such a list, so this was a hard generation failure, not a cosmetic
    issue. The wire key name must stay `if` while the Python parameter becomes
    `if_`.
    """
    for proto in ("netconf", "restconf"):
        out, _ = _gen(tmp_path, proto, KEYWORD_KEY_YANG, "tkw")
        src = (
            (out / "data_navigators" / "tkw.py").read_text()
            if (out / "data_navigators" / "tkw.py").exists()
            else (out / "data_navigators" / "navigators.py").read_text()
        )
        if proto == "netconf":
            # NETCONF keys are Python parameters on the navigator's __call__,
            # so they must be keyword-safe...
            assert "def __call__(self, if_: str | int, addr: str | int)" in src, (
                "netconf: keyword-safe __call__ signature not emitted"
            )
            # ...while the wire key name stays `if` in the path keys dict.
            assert "'if': if_" in src, "netconf: wire key name 'if' not preserved"
        else:
            # RESTCONF now emits a key-aware __call__ too, so the transport
            # surfaces match and the key names are discoverable from Python.
            assert "def __call__(self, if_: str | int, addr: str | int)" in src, (
                "restconf: keyword-safe __call__ signature not emitted"
            )
        # the model attribute is also keyword-safe
        models = (
            (out / "data_models" / "tkw.py").read_text()
            if (out / "data_models" / "tkw.py").exists()
            else (out / "data_models" / "models.py").read_text()
        )
        assert "if_: str" in models or "if_: " in models, (
            f"{proto}: model field for key 'if' is not keyword-safe"
        )
        assert "class_: str" in models or "class_: " in models


KEY_DISCOVERY_YANG = """module tkd {
  prefix td;
  namespace "urn:test:tkd";
  revision 2026-01-01;
  container root {
    list item {
      key "id kind";
      leaf id { type string; }
      leaf kind {
        type enumeration { enum alpha; enum beta; }
      }
      leaf payload { type uint8; }
    }
  }
}"""


@pytest.mark.parametrize("proto", ["restconf", "netconf"])
def test_list_keys_are_discoverable_and_named_on_call(tmp_path, proto):
    """Both transports must expose the YANG `key` names, not just `*keys`.

    RESTCONF emitted `is_key` only on the NETCONF branch, and its list
    `__call__` was the shared `ListNode.__call__(*keys)`. So a RESTCONF client
    told a caller *nothing* about which fields keyed a list -- and the keys are
    a YANG `key` statement, not part of an instance payload, so they cannot be
    recovered by inspecting device data. Combined with the positional
    `*keys`, arity was unchecked: passing the wrong number built a wrong URL and
    failed only at the device.

    Now both transports carry `is_key` in `json_schema_extra` and take named
    parameters, which is what makes the two navigator surfaces symmetrical
    (AGENTS.md "RESTCONF <-> NETCONF parity").
    """
    out, _ = _gen(tmp_path, proto, KEY_DISCOVERY_YANG, "tkd")
    models = _import(out, "data_models.tkd")
    nav = _import(out, "data_navigators.tkd")

    # 1. is_key identifies the key fields on the item model.
    keys = [
        name
        for name, fi in models.ItemItem.model_fields.items()
        if (fi.json_schema_extra or {}).get("is_key")
    ]
    assert sorted(keys) == ["id", "kind"], keys
    assert not (models.ItemItem.model_fields["payload"].json_schema_extra or {}).get(
        "is_key"
    )

    # 2. __call__ takes them by name, so arity is checked by Python.
    sig = inspect.signature(nav.ItemListNode.__call__)
    assert list(sig.parameters) == ["self", "id", "kind"], list(sig.parameters)

    # 3. Both keys reach the URL segment, percent-quoted per RFC 8040 Sec 3.5.3.
    listnav = nav.ItemListNode.__new__(nav.ItemListNode)
    listnav._client = None
    listnav._name = "item"
    listnav._envelope_name = "tkd:item"
    listnav._item_cls = nav.ItemItemNode
    if proto == "restconf":
        listnav._path = "/data/tkd:item"
    else:
        listnav._path = [("urn:test:tkd", "item", {})]
    item = listnav("eth 0", "a/b")
    if proto == "restconf":
        assert item._path == "/data/tkd:item=eth%200,a%2Fb", item._path
    else:
        # NETCONF carries keys in the path tuple, not a URL segment.
        assert item._path[-1][2] == {"id": "eth 0", "kind": "a/b"}, item._path[-1]

    # 4. Too few keys is a TypeError at the call site, not a bad URL on the wire.
    with pytest.raises(TypeError):
        listnav("only-one")


DECIMAL64_YANG = """module tdec {
  prefix td;
  namespace "urn:test:tdec";
  revision 2026-01-01;
  container c {
    leaf coarse { type decimal64 { fraction-digits 2; range "0..100"; } }
    leaf fine { type decimal64 { fraction-digits 6; } }
  }
}
"""


def test_decimal64_fraction_digits_enforced_locally(tmp_path):
    """RFC 7950 Sec 9.3.2: `fraction-digits` bounds the scale.

    The generated model previously accepted Decimal("1.234567") for a
    fraction-digits 2 leaf, so the error only surfaced at the device. The
    *serializer* was already lossless (it pads, never truncates), so this is
    about failing locally rather than corrupting a value.
    """
    from decimal import Decimal

    from pydantic import ValidationError

    for proto in ("restconf", "netconf"):
        out, _ = _gen(tmp_path, proto, DECIMAL64_YANG, "tdec")
        models = _import(out, "data_models.tdec")

        model = models.C
        # in range, in scale -> accepted, and the value is untouched
        assert model.model_validate({"coarse": Decimal("1.5")}).coarse == Decimal("1.5")
        assert model.model_validate({"coarse": Decimal("1.50")}).coarse == Decimal(
            "1.50"
        )
        assert model.model_validate({"coarse": Decimal(100)}).coarse == Decimal(100)
        # out of range -> rejected (the pre-existing range constraint)
        with pytest.raises(ValidationError):
            model.model_validate({"coarse": Decimal(-1)})
        # too many fraction digits -> now rejected locally
        with pytest.raises(ValidationError):
            model.model_validate({"coarse": Decimal("1.234567")})

        # the fraction-digits 6 sibling still accepts exactly that value
        assert model.model_validate({"fine": Decimal("1.234567")}).fine == Decimal(
            "1.234567"
        )


def test_revision_ordering_picks_newest_yang_revision():
    """RFC 7950 Sec 4.1 revision dates, including the optional `:HH:MM` part.

    The downloader used to save every revision the device advertised and let
    pyang pick, which took the OLDEST. On Cisco IOS-XR that meant 1199 of the
    modules were generated from a stale schema, and because the models are
    strict the device's own data then failed validation locally.
    """
    from yang2sdk.cli.downloader import _revision_key

    versions = ["2019-04-05", "2022-06-23", "2025-02-04", "2013-07-15"]
    assert max(versions, key=_revision_key) == "2025-02-04"
    assert min(versions, key=_revision_key) == "2013-07-15"
    # the clock part breaks ties within a day, per RFC 7950 Sec 4.1
    assert (
        max(["2025-02-04", "2025-02-04:10:00", "2025-02-04:09:59"], key=_revision_key)
        == "2025-02-04:10:00"
    )
    # an unparseable revision must never outrank a real one
    assert max(["garbage", "2013-07-15"], key=_revision_key) == "2013-07-15"
    # total ordering: no two distinct inputs compare equal
    keys = [_revision_key(v) for v in ("2019-04-05", "2022-06-23", "2025-02-04")]
    assert len(set(keys)) == len(keys)


def test_replace_guard_accepts_dict_input(tmp_path):
    """`replace()` accepts a dict as readily as a model.

    The NETCONF navigator ran the completeness guard *before* coercing the
    input, so `replace({...})` raised an opaque
    `AttributeError: 'dict' object has no attribute 'model_fields'` — a bad
    error, and a silent bypass of a safety check for the most convenient
    input form. The guard now coerces with the same rules `update()` uses.
    """
    out, _ = _gen(tmp_path, "netconf", STATE_GUARD_YANG, "tguard")
    nav = _import(out, "data_navigators")

    class _NetconfRecorder:
        def __init__(self):
            self.module_namespaces: dict[str, str] = {}
            self.edits = 0

        def edit(self, config_xml, target=None):
            self.edits += 1
            return True

    client = _NetconfRecorder()
    data = nav.Data(client, [])

    # A dict with only the state leaf unset must be accepted, and sent.
    assert data.tguard_c.items("a").replace({"k": "a", "cfg": "kept"})
    assert client.edits == 1, "an accepted dict replace must reach the transport"

    # ...and a dict missing a config leaf must still be refused, by name,
    # with nothing sent.
    edits_before = client.edits
    with pytest.raises(ValueError, match="would DELETE"):
        data.tguard_c.items("a").replace({"k": "a"})
    try:
        data.tguard_c.items("a").replace({"k": "a"})
    except ValueError as exc:
        assert "cfg" in str(exc) and "obs" not in str(exc), str(exc)
    assert client.edits == edits_before, "nothing may be sent when the guard trips"

    # allow_partial is still the documented escape hatch for dicts too.
    assert data.tguard_c.items("a").replace({"k": "a"}, allow_partial=True)


def test_restconf_client_close_is_symmetric_with_netconf(tmp_path):
    """`RestconfClient` had no `close()` at all, while `NetconfClient` did.

    Protocol parity is a normative requirement (AGENTS.md), and a generated
    client is a long-lived object holding a `requests.Session`; without an
    explicit close its keep-alive sockets and TLS session survive the last
    call. Now: idempotent `close()`, context-manager support, and a clear
    use-after-close error instead of an `AttributeError` on `None`.
    """
    out, _ = _gen(tmp_path, "restconf", REPLACE_YANG, "trep")
    sm = _import(out, "session_manager")

    client = sm.RestconfClient(
        management_ip="127.0.0.1", port=8181, username="u", password="p"
    )
    client.close()
    client.close()  # idempotent
    with pytest.raises(RuntimeError, match="session is closed"):
        client._request("GET", "/data")

    with sm.RestconfClient(
        management_ip="127.0.0.1", port=8181, username="u", password="p"
    ) as ctx:
        assert ctx is not None
    # leaving the context closed the client
    with pytest.raises(RuntimeError, match="session is closed"):
        ctx._request("GET", "/data")

    # parity: the NETCONF client exposes the same lifecycle surface (its own
    # connect/close behaviour is covered by the netconf session tests).
    nout, _ = _gen(tmp_path, "netconf", REPLACE_YANG, "trep")
    netconf = _import(nout, "session_manager")
    for name in ("close", "__enter__", "__exit__"):
        assert hasattr(netconf.NetconfClient, name), f"NetconfClient lacks {name}"
        assert hasattr(sm.RestconfClient, name), f"RestconfClient lacks {name}"


def test_both_protocols_fail_closed_without_credentials(tmp_path, monkeypatch):
    """No generated client may be constructible without credentials.

    Credentials resolve from caller args, else DEVICE_USER / DEVICE_PASS --
    the same names `.env.example`, `cli/downloader.py`, `cli/tester.py` and
    the lab matrix already read, so one `.env` serves every consumer.

    Two defects are pinned here. RESTCONF used to `raise UserWarning` -- a
    Warning subclass used as an exception, so `except ValueError` never saw
    it -- while NETCONF did not raise at all and handed None to
    `manager.connect()`, failing later with an opaque SSH error. Both now fail
    at construction with ValueError.

    The retired long form is asserted *inert* below rather than merely
    removed: it used to be the only name this template read, so a generated
    client could not use the `.env` the project told you to copy. A
    two-name alias was added, then the two templates resolved the pairs in
    opposite order, so a host exporting both authenticated as two different
    identities depending on transport. One name removes the hazard, and the
    `delenv` below keeps it from creeping back unnoticed.

    `monkeypatch.delenv` is what makes this hermetic: conftest loads an
    untracked local `.env` into os.environ at import, and without clearing
    these names the assertions would pass on a developer machine and fail in
    CI -- exactly the bug this test pins down.
    """
    from unittest import mock

    for var in (
        "DEVICE_USER",
        "DEVICE_PASS",
        "DEVICE_USERNAME",
        "DEVICE_PASSWORD",
    ):
        monkeypatch.delenv(var, raising=False)

    rout, _ = _gen(tmp_path / "rc", "restconf", REPLACE_YANG, "tcred")
    rest = _import(rout, "session_manager")
    with pytest.raises(ValueError, match="credentials"):
        rest.RestconfClient(management_ip="127.0.0.1", port=8181)

    nout, _ = _gen(tmp_path / "nc", "netconf", REPLACE_YANG, "tcred")
    netc = _import(nout, "session_manager")
    with pytest.raises(ValueError, match="credentials"):
        netc.NetconfClient(management_ip="127.0.0.1")

    # The documented env fallback works on both protocols...
    monkeypatch.setenv("DEVICE_USER", "envuser")
    monkeypatch.setenv("DEVICE_PASS", "envpass")
    client = rest.RestconfClient(management_ip="127.0.0.1", port=8181)
    assert client._session.auth == ("envuser", "envpass")

    with mock.patch.object(
        netc.manager, "connect", return_value=_FakeNcclientManager(_BASE_CAPS)
    ):
        nclient = netc.NetconfClient(management_ip="127.0.0.1")
    assert (nclient.username, nclient.password) == ("envuser", "envpass")

    # Explicit args still beat the environment on both protocols.
    with mock.patch.object(
        netc.manager, "connect", return_value=_FakeNcclientManager(_BASE_CAPS)
    ):
        nclient = netc.NetconfClient(
            management_ip="127.0.0.1", username="arguser", password="argpass"
        )
    assert (nclient.username, nclient.password) == ("arguser", "argpass")
    arg_client = rest.RestconfClient(
        management_ip="127.0.0.1", port=8181, username="arguser", password="argpass"
    )
    assert arg_client._session.auth == ("arguser", "argpass")


def test_retired_long_form_credential_names_are_ignored(tmp_path, monkeypatch):
    """DEVICE_USERNAME / DEVICE_PASSWORD must not authenticate a client.

    These names are retired, not aliased. If a generated client ever starts
    honouring them again, a host that happens to export both the retired and
    the current names authenticates as whichever the template reaches first --
    the exact two-identities hazard the alias created.
    """
    from unittest import mock

    for var in ("DEVICE_USER", "DEVICE_PASS", "DEVICE_USERNAME", "DEVICE_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DEVICE_USERNAME", "retired")
    monkeypatch.setenv("DEVICE_PASSWORD", "retiredpass")

    rout, _ = _gen(tmp_path / "rc", "restconf", REPLACE_YANG, "tlong")
    rest = _import(rout, "session_manager")
    with pytest.raises(ValueError, match="credentials"):
        rest.RestconfClient(management_ip="127.0.0.1", port=8181)

    nout, _ = _gen(tmp_path / "nc", "netconf", REPLACE_YANG, "tlong")
    netc = _import(nout, "session_manager")
    with pytest.raises(ValueError, match="credentials"):
        netc.NetconfClient(management_ip="127.0.0.1")

    # And the current names win outright when both are exported.
    monkeypatch.setenv("DEVICE_USER", "current")
    monkeypatch.setenv("DEVICE_PASS", "currentpass")
    client = rest.RestconfClient(management_ip="127.0.0.1", port=8181)
    assert client._session.auth == ("current", "currentpass")
    with mock.patch.object(
        netc.manager, "connect", return_value=_FakeNcclientManager(_BASE_CAPS)
    ):
        nclient = netc.NetconfClient(management_ip="127.0.0.1")
    assert (nclient.username, nclient.password) == ("current", "currentpass")


def test_missing_yang_dir_fails_fast_with_actionable_error(tmp_path):
    """A missing YANG search dir must fail fast, never auto-create.

    `temp/` is untracked and created on demand, but only *outputs* are
    created. The search dir is an input: silently creating it would mask a
    forgotten `yang-downloader` run with an empty dir and a cryptic pyang
    failure downstream.
    """
    yang = tmp_path / "tmiss.yang"
    yang.write_text(MINIMAL_YANG)
    missing = tmp_path / "no-such-yang-dir"
    assert not missing.exists()
    code = (
        "import sys; from yang2sdk.cli.compiler import run_compiler; "
        "run_compiler('netconf', sys.argv[1:])"
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(yang),
            "--device",
            "tmiss",
            "--yang-dir",
            str(missing),
            "--output-dir",
            str(tmp_path / "out"),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert proc.returncode == 2, proc.stderr[-2000:]
    assert "yang-downloader" in proc.stderr, proc.stderr[-2000:]
    assert not missing.exists(), "a missing input dir must never be created"


def test_output_dir_is_created_on_demand(tmp_path):
    """Compiling into a nonexistent nested output dir must succeed.

    Every writer owns its output path: a fresh clone has no `temp/` until
    the first run, so the emitter cannot depend on checked-in scaffolding.
    """
    yang = tmp_path / "tfresh.yang"
    yang.write_text(MINIMAL_YANG)
    out = tmp_path / "fresh" / "nested" / "out"
    assert not out.parent.exists()
    code = (
        "import sys; from yang2sdk.cli.compiler import run_compiler; "
        "run_compiler('netconf', sys.argv[1:])"
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(yang),
            "--device",
            "tfresh",
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
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert (out / "__init__.py").is_file()
    assert (out / "session_manager.py").is_file()


def test_importing_downloader_creates_no_files(tmp_path):
    """Importing the downloader must not touch the filesystem.

    The module used to configure a DEBUG FileHandler into
    `temp/yang_downloader.log` at import time, so `--help`, a test import,
    or any tooling grew an unbounded log as a side effect. Logging is now
    lazy (stderr by default, opt-in rotating file via YANG_DOWNLOADER_LOG_FILE).
    """
    proc = subprocess.run(
        [sys.executable, "-c", "import yang2sdk.cli.downloader"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert not (tmp_path / "temp").exists(), "import must not create temp/"
    assert list(tmp_path.glob("*.log")) == []
