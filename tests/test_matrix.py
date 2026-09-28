"""Offline checks: matrix completeness + template contract. No docker needed."""

import compileall
import json
import subprocess
import sys
from pathlib import Path

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
