"""Offline checks: matrix completeness + template contract. No docker needed."""

import ast
import compileall
import importlib
import importlib.util
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any, ClassVar, cast, get_args

import pytest

from yang2sdk import layout
from yang2sdk.cli import model_gaps
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
RELEASE_WF = REPO_ROOT / ".github" / "workflows" / "release.yaml"
T_RESTCONF_SM = (
    REPO_ROOT / "src/yang2sdk/plugin/src/templates/restconf/session_manager.py.jinja"
)
T_NETCONF_SM = (
    REPO_ROOT / "src/yang2sdk/plugin/src/templates/netconf/session_manager.py.jinja"
)


def _release_guard_scripts() -> tuple[str, str]:
    """Return the (version-read, tag-guard) `run` scripts from release.yaml.

    Parsed, never copied: a copy would let the workflow drift away from what
    these tests execute, which is the failure this pin exists to prevent.
    """
    import yaml

    doc = yaml.safe_load(RELEASE_WF.read_text())
    build = doc["jobs"]["build"]
    read_step = next(s for s in build["steps"] if s.get("id") == "version")
    guard_step = next(
        s
        for s in build["steps"]
        if s.get("name") == "Fail if the tag does not match pyproject"
    )
    return str(read_step["run"]), str(guard_step["run"])


def _run_release_guard(
    tmp_path: Path, ref_name: str, pyproject_version: str | None
) -> subprocess.CompletedProcess[str]:
    """Run both release.yaml steps the way GitHub runs them.

    Two GitHub semantics are emulated because ignoring either hides the bug:

    1. A `run` step's outputs come from `name=value` lines written to
       `$GITHUB_OUTPUT`. A bare value on stdout is log output, not an output.
    2. `${{ steps.version.outputs.value }}` is substituted into the guard's
       script text before bash ever sees it.
    """
    read_script, guard_script = _release_guard_scripts()
    body = '[project]\nname = "yang2sdk"\n'
    if pyproject_version is not None:
        body += f'version = "{pyproject_version}"\n'
    (tmp_path / "pyproject.toml").write_text(body)
    outputs = tmp_path / "github_output"
    outputs.write_text("")

    env = {
        **os.environ,
        "GITHUB_REF_NAME": ref_name,
        "GITHUB_OUTPUT": str(outputs),
    }
    read = subprocess.run(
        ["bash", "-e", "-c", read_script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if read.returncode != 0:
        return read

    step_value = ""
    for line in outputs.read_text().splitlines():
        if line.startswith("value="):
            step_value = line.split("=", 1)[1]
            break
    return subprocess.run(
        [
            "bash",
            "-e",
            "-c",
            guard_script.replace("${{ steps.version.outputs.value }}", step_value),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def test_every_tracked_file_with_a_shebang_is_executable() -> None:
    """Ruff's `EXE001`, asserted where the answer is a fact and not a view.

    A shebang in a file that is not executable is a violation of an enabled
    rule, and this one took down every `ci-pr` run: `scripts/changelog.py`
    carried `#!/usr/bin/env python3` at mode 644 from the commit that added it,
    so `uv run ruff check .` failed on `main` — and therefore on every branch
    and every dependabot PR, since they all inherit `main`. The boundary is
    exact: `575f798` passes, `bdd3978` fails, and that is the commit that added
    the file.

    It was invisible locally, which is the reason this test exists rather than a
    note to run ruff. Ruff's answer to "is this file executable" depends on the
    environment and not only on the file: the *same binary*, on the same
    directory, at the same mode, reports `EXE001` inside a container and reports
    nothing on the developer host — with `os.access(path, X_OK)` false and
    `stat` reporting 644 in both. A rule whose verdict depends on the runner
    cannot be the only thing enforcing it, so the invariant is read from the git
    index, which is exactly what a checkout reproduces.
    """
    listed = subprocess.run(
        ["git", "ls-files", "-s", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if listed.returncode != 0:
        # Git refusing is not the same as there being no index, and the
        # difference matters to whoever reads this: the common cause is running
        # as a user that does not own the checkout ("dubious ownership"), where
        # the invariant is real but cannot be read from here. Quoting git's own
        # words is what makes the skip diagnosable instead of mysterious.
        detail = listed.stderr.strip().splitlines()
        pytest.skip(
            "`git ls-files` failed, so the index cannot be read and the "
            f"invariant cannot be checked here: {detail[-1] if detail else '?'}"
        )

    offenders: list[str] = []
    for entry in listed.stdout.split("\0"):
        if not entry:
            continue
        meta, _, path = entry.partition("\t")
        mode = meta.split()[0]
        if mode != "100644":  # already executable, or a symlink: nothing to check
            continue
        target = REPO_ROOT / path
        if not target.is_file():
            continue
        with target.open("rb") as handle:
            if handle.read(2) != b"#!":
                continue
        offenders.append(path)

    assert not offenders, (
        "these tracked files start with a shebang but are not executable, which "
        f"`ruff check .` rejects as EXE001: {offenders}. "
        "Fix with `chmod +x <path>` and commit the mode change — the mode is "
        "tracked by git, so the fix is a commit and not a local chmod."
    )


def test_release_tag_guard_passes_on_a_matching_tag(tmp_path: Path) -> None:
    """A tag equal to `pyproject.toml`'s version must let the release build.

    Regression: the version step used to `print()` a bare value, which GitHub
    does not record as a step output, so `steps.version.outputs.value` was
    always empty and the guard failed for *every* tag. Run 38891426993 failed
    with `pyproject.toml says ` and nothing after it; run 38893814299 failed
    identically after the tag was corrected to `v2026.10.16` and the version
    bumped to match. The guard was comparing against "", never a version.
    """
    version = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"][
        "version"
    ]
    proc = _run_release_guard(tmp_path, f"v{version}", version)
    assert proc.returncode == 0, (
        f"guard rejected tag v{version} against matching version {version}: "
        f"{proc.stderr[-2000:]}"
    )


def test_release_tag_guard_rejects_a_mismatched_tag(tmp_path: Path) -> None:
    """Still fails when tag and version genuinely disagree.

    The fix must not turn the guard into a no-op that lets a drifted tag
    publish. Here the output IS read; the comparison is what rejects.
    """
    proc = _run_release_guard(tmp_path, "v0.1.0", "2026.10.16")
    assert proc.returncode == 1, proc.stderr[-2000:]
    assert "0.1.0" in proc.stderr and "2026.10.16" in proc.stderr, proc.stderr[-2000:]


def test_release_tag_guard_rejects_a_missing_version(tmp_path: Path) -> None:
    """An unreadable version fails loudly instead of comparing as "".

    Without the explicit check, a `pyproject.toml` missing
    `[project].version` left the output empty and the guard failed naming no
    version at all -- the same unreadable log line that hid the real bug
    across two runs.
    """
    proc = _run_release_guard(tmp_path, "v2026.10.16", None)
    assert proc.returncode == 1, proc.stderr[-2000:]
    assert "could not read" in proc.stderr, proc.stderr[-2000:]


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

UNWRAP_YANG = """module tunwrap {
  prefix tu;
  namespace "urn:test:unwrap";
  revision 2026-01-01;
  container top {
    container clock {
      leaf timezone-name {
        type string;
      }
    }
    container ntp {
      container server {
        leaf addr {
          type string;
        }
      }
      leaf weight {
        type uint32;
      }
    }
    container dns {
      container server {
        leaf addr {
          type string;
        }
      }
      leaf port {
        type uint16;
      }
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
        re.DOTALL,
    )
    assert rest_lists, "no RESTCONF ListNode classes found in template"
    for block in rest_lists:
        assert "def create" in block, "RESTCONF ListNode missing create"
        assert "def replace" in block and "def retrieve" in block
        assert "def update" not in block, "RESTCONF ListNode must not expose update"
    netc_lists = re.findall(
        r"ListNode\[.*?\](.*?)\n(?=class |\{% |\Z)",
        (base / "netconf/data_navigators/navigators.py.jinja").read_text(),
        re.DOTALL,
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
    # Import the package the README names, from `src/` -- not the container,
    # which is not a package (it would import as an empty namespace package and
    # fail with a confusing AttributeError).
    pkg = _import(out, "")
    assert pkg.RestconfClient if fmt == "restconf" else pkg.NetconfClient


def _gen_restconf(yang_text: str, name: str, tmp_path):
    """Generate a RESTCONF client and return the package root on sys.path."""
    yang = tmp_path / f"{name}.yang"
    yang.write_text(yang_text)
    out = tmp_path / f"{name}_restconf"
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
    assert proc.returncode == 0, proc.stderr[-1500:]
    # Don't hardcode the version directory: the default device_version differs
    # between the console script and run_compiler(). The manifest is the
    # authoritative statement of what was emitted.
    project = _project(out)
    manifest = json.loads((project / "MANIFEST.yang-revisions.json").read_text())
    pkg_root = project / "src" / manifest["package_name"]
    assert pkg_root.is_dir(), f"expected package at {pkg_root}"
    sys.path.insert(0, str(pkg_root.parent))
    return str(pkg_root.parent), pkg_root.name


def _gen_netconf(
    yang_text: str,
    name: str,
    tmp_path,
    extra: dict[str, str] | None = None,
) -> Path:
    """Generate a NETCONF client into ``tmp_path/<name>_netconf``; return it.

    ``extra`` writes additional modules beside the root and passes them as
    further roots, which is how a cross-module `augment` gets compiled in.
    """
    yang = tmp_path / f"{name}.yang"
    yang.write_text(yang_text)
    roots = [str(yang)]
    for filename, text in (extra or {}).items():
        path = tmp_path / filename
        path.write_text(text)
        roots.append(str(path))
    out = tmp_path / f"{name}_netconf"
    code = (
        "import sys; from yang2sdk.cli.compiler import run_compiler; "
        "run_compiler('netconf', sys.argv[1:])"
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            *roots,
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
    assert proc.returncode == 0, proc.stderr[-1500:]
    return out


def test_restconf_unwraps_a_reply_nested_under_the_ancestor(tmp_path):
    """A reply keyed by an ANCESTOR's name must still unwrap (RFC 8040 §4.2).

    Regression, measured on notconf-ietf 25.x: `GET /ietf-system:system/clock`
    answers `{"ietf-system:system": {"clock": {}}}`. The node's own name
    (`clock`) is not a top-level key, so the old lookup fell through to "assume
    the whole response is my node" and handed the strict `SystemClock` model an
    envelope — which `extra="forbid"` then correctly rejected, surfacing as
    "Extra inputs are not permitted" at a node that is perfectly conformant.

    RFC 8040 §4.2 identifies a resource by its URI, so the path the navigator
    requested is the specification for reading the reply.
    """
    sys_root, pkg = _gen_restconf(UNWRAP_YANG, "tunwrap", tmp_path)
    try:
        nav = __import__(f"{pkg}.data_navigators.tunwrap", fromlist=["*"])

        class _Stub:
            """Returns one canned reply; records the paths actually requested."""

            def __init__(self, payload):
                self.payload = payload
                self.seen = []

            def _request(self, method, path, params=None, json=None):
                self.seen.append(path)
                return self.payload

        payload = {
            "tunwrap:top": {
                "clock": {"timezone-name": "UTC"},
                "ntp": {"server": {"addr": "NTP-VALUE"}, "weight": 1},
                "dns": {"server": {"addr": "DNS-VALUE"}, "port": 53},
            }
        }
        client = _Stub(payload)
        top = nav.TopNode(client, "/data/tunwrap:top", "tunwrap:top")

        assert top.clock.retrieve().timezone_name == "UTC"
        assert "/data/tunwrap:top/clock" in client.seen
    finally:
        sys.path.remove(sys_root)


def test_restconf_unwrap_never_returns_a_same_named_sibling(tmp_path):
    """Walking the path must not land on a same-named leaf elsewhere (RFC 8040 §4.2).

    This is the hazard that rules out the obvious fix. `tunwrap:top` has two
    children named `server` (`ntp/server` and `dns/server`) — mirroring
    ietf-system, which has both `ntp/server` and `dns-resolver/server`. A
    recursive search for the leaf name resolves the ancestor-wrapped shape but
    silently returns whichever `server` it meets first, i.e. the NTP servers
    when DNS servers were asked for. That turns a loud failure into a wrong
    answer, which is worse than the bug it fixes.

    Only the requested path is reachable by walking segments, so a same-named
    sibling is structurally out of reach.
    """
    sys_root, pkg = _gen_restconf(UNWRAP_YANG, "tunwrap", tmp_path)
    try:
        nav = __import__(f"{pkg}.data_navigators.tunwrap", fromlist=["*"])

        class _Stub:
            def __init__(self, payload):
                self.payload = payload

            def _request(self, method, path, params=None, json=None):
                return self.payload

        payload = {
            "tunwrap:top": {
                "clock": {"timezone-name": "UTC"},
                # ntp sorts before dns, so a name search finds NTP first.
                "ntp": {"server": {"addr": "NTP-VALUE"}, "weight": 1},
                "dns": {"server": {"addr": "DNS-VALUE"}, "port": 53},
            }
        }
        client = _Stub(payload)
        top = nav.TopNode(client, "/data/tunwrap:top", "tunwrap:top")

        assert top.dns.server.retrieve().addr == "DNS-VALUE"
        assert top.ntp.server.retrieve().addr == "NTP-VALUE"
    finally:
        sys.path.remove(sys_root)


def test_restconf_unwrap_falls_back_when_the_reply_has_no_path(tmp_path):
    """Already-unwrapped replies must keep working — no regression (RFC 8040 §4.2).

    A server may answer with the resource itself rather than nesting it, and a
    list instance's URI carries a predicate (`interface[name=eth0]`) that has no
    JSON counterpart. Neither can be path-walked, so the historical name lookup
    stays as the fallback. This test pins that the fallback still runs.
    """
    sys_root, pkg = _gen_restconf(UNWRAP_YANG, "tunwrap", tmp_path)
    try:
        base = __import__(f"{pkg}.data_navigators._base", fromlist=["*"])

        # Already unwrapped: no path structure to walk.
        assert (
            base._unwrap_by_path({"addr": "1.1.1.1"}, "/data/t/top/dns/server") is None
        )
        assert base._unwrap_by_name({"addr": "1.1.1.1"}, "server") == {
            "addr": "1.1.1.1"
        }

        # URI predicate: not a JSON member name, so the walk declines.
        assert (
            base._unwrap_by_path(
                {"tunwrap:server": [{"addr": "x"}]}, "/data/t/top/server[name=x]"
            )
            is None
        )

        # A top-level resource still walks, and matches the old behaviour.
        wrapped = {"tunwrap:top": {"clock": {}}}
        assert base._unwrap_by_path(wrapped, "/data/tunwrap:top") == {"clock": {}}
        assert base._unwrap_by_name(wrapped, "tunwrap:top") == {"clock": {}}

        # Bare vs qualified segment equivalence (RFC 7951 §4).
        assert base._member_matches("clock", "tunwrap:clock")
        assert base._member_matches("tunwrap:clock", "clock")
        assert not base._member_matches("ntp", "dns")

        # A reply already keyed by the TARGET cannot be path-walked (the parent
        # segment is absent), so the path layer declines and the name layer
        # handles it — the layering working as designed.
        assert (
            base._unwrap_by_path({"tunwrap:clock": {}}, "/data/tunwrap:top/clock")
            is None
        )
        assert base._unwrap_by_name({"tunwrap:clock": {}}, "clock") == {}
        assert base._unwrap_by_name({"clock": {}}, "clock") == {}
    finally:
        sys.path.remove(sys_root)


AUGMENTED_YANG = """module tbase {
  prefix tb;
  namespace "urn:test:tbase";
  revision 2026-01-01;
  container sysinfo {
    leaf hostname {
      type string;
    }
  }
}
"""

AUGMENTING_YANG = """module tadd {
  prefix ta;
  namespace "urn:test:tadd";
  import tbase { prefix tb; }
  revision 2026-01-01;
  augment "/tb:sysinfo" {
    leaf load {
      type uint32;
    }
  }
}
"""


def test_augment_closure_is_expanded_by_default(tmp_path):
    """A root module alone emits a model missing the augment's children.

    RFC 7950 Sec 7.17: pyang expands an `augment` into its target only when
    the augmenting module is on the command line. Naming `tbase` alone
    produced a model with no `load` field, so a device answering with it --
    which is conformant -- was rejected by `extra="forbid"`.

    Measured on notconf-ietf: compiling `ietf-system` +
    `ietf-system-capabilities` left `subscription-capabilities` entirely out of
    the model, and reading `/system-capabilities/datastore-capabilities` failed
    with six "Extra inputs are not permitted" errors.
    """
    (tmp_path / "tbase.yang").write_text(AUGMENTED_YANG)
    (tmp_path / "tadd.yang").write_text(AUGMENTING_YANG)
    from yang2sdk.cli.compiler import expand_augment_closure

    roots = [tmp_path / "tbase.yang"]
    expanded, provenance = expand_augment_closure(roots, tmp_path)

    names = sorted(p.name for p in expanded)
    assert names == ["tadd.yang", "tbase.yang"], names
    assert provenance.get("tadd") == ["tbase"]


def test_augment_closure_opt_out_keeps_the_requested_roots(tmp_path):
    """--no-augment-closure must leave the caller's input untouched."""
    (tmp_path / "tbase.yang").write_text(AUGMENTED_YANG)
    (tmp_path / "tadd.yang").write_text(AUGMENTING_YANG)
    from yang2sdk.cli.compiler import expand_augment_closure

    roots = [tmp_path / "tbase.yang", tmp_path / "tadd.yang"]
    expanded, _prov = expand_augment_closure(roots, tmp_path)
    # Already complete: nothing to add, and both roots survive.
    assert sorted(p.name for p in expanded) == ["tadd.yang", "tbase.yang"]


def test_augment_closure_survives_an_unresolvable_root(tmp_path):
    """An unreadable/foreign root degrades to a warning, never a failed compile."""
    from yang2sdk.cli.compiler import expand_augment_closure

    bogus = tmp_path / "not-a-module.yang"
    bogus.write_text("this is not YANG\n")
    roots = [bogus]
    expanded, prov = expand_augment_closure(roots, tmp_path)
    assert expanded == roots
    assert prov == {}


@pytest.mark.parametrize("no_closure", [False, True])
def test_cli_applies_augment_closure_end_to_end(tmp_path, no_closure):
    """The *compiler command* must pull the augment in, not just the helper.

    The unit tests above pin `expand_augment_closure`; this pins that
    `run_compiler` actually calls it. Without this, the wiring could be
    reverted while every helper test still passed.
    """
    (tmp_path / "tbase.yang").write_text(AUGMENTED_YANG)
    (tmp_path / "tadd.yang").write_text(AUGMENTING_YANG)
    out = tmp_path / "tbase_restconf"
    code = (
        "import sys; from yang2sdk.cli.compiler import run_compiler; "
        "run_compiler('restconf', sys.argv[1:])"
    )
    cmd = [
        sys.executable,
        "-c",
        code,
        str(tmp_path / "tbase.yang"),
        "--device",
        "tbase",
        "--yang-dir",
        str(tmp_path),
        "--output-dir",
        str(out),
    ]
    if no_closure:
        cmd.append("--no-augment-closure")
    proc = subprocess.run(
        cmd, cwd=REPO_ROOT, capture_output=True, text=True, check=False, timeout=300
    )
    assert proc.returncode == 0, proc.stderr[-1500:]

    models = _pkg_file(out, "data_models", "tbase.py")
    assert models.is_file(), f"no generated model for tbase under {out}"
    text = models.read_text()
    has_augmented_field = "load" in text
    # Default: the augment IS resolved into the model.
    # --no-augment-closure: base only, which is exactly the defect being fixed.
    assert has_augmented_field is not no_closure, (
        f"no_closure={no_closure}: augmented field present={has_augmented_field}\n{text[:600]}"
    )


def test_generated_package_name_matches_the_matrix_slug():
    """`--device` + `--device-version` must yield exactly `_slug(image)`.

    `core.py` builds the package name as
    `_sanitize_pkg(f"{device}_{device_version}")`, while `conftest._slug`
    names the output directory `f"{package}_{tag}"`. If the two drift, the
    installable package (`out/<package_name>/`) is named differently from the
    directory the tests import, and the generated README advertises an import
    name that does not match what `uv add` installs.

    Measured before this was pinned: notconf-cisco-nx 10.4-4 produced
    `notconf_cisco_nx_10_4_4_0_0_0` where the slug is `notconf_cisco_nx_10_4_4`
    -- the version appeared twice because the version-bearing slug was passed
    as `--device` AND the version was appended again.

    Runs offline over every matrix entry, so the awkward tags are covered
    (`21.1R1`, `10.4-4`, `762`, `latest`) without paying for a compile.

    NOTE: this is a naming-scheme cross-check only. It holds by construction
    and therefore does NOT catch the `--device` wiring regression; that is
    what `test_compile_all_names_the_installable_package_from_the_matrix_slug`
    pins, by actually compiling.
    """
    from tests.conftest import _slug, load_matrix
    from yang2sdk.layout import sanitize_package_name

    images = load_matrix()["images"]
    assert images, "matrix declares no images"
    for image in images:
        want = _slug(image)
        got = sanitize_package_name(f"{image['package']}_{image['tag']}")
        assert got == want, (
            f"{image['package']}:{image['tag']}: package name {got!r} != "
            f"directory slug {want!r}; the installable copy and the imported "
            f"directory would disagree"
        )


def test_compile_all_names_the_installable_package_from_the_matrix_slug(
    tmp_path, monkeypatch
):
    """`_compile_all` must make the installable package name == the slug.

    The installable copy is `out/<package_name>/` (core.py "Namespaced
    installable copy") while the tests import the flat copy at `out/`. If
    those two names drift, `uv add` installs a package under one name and the
    generated README tells the consumer to import another.

    Measured before this was fixed: notconf-cisco-nx 10.4-4 produced
    `notconf_cisco_nx_10_4_4_0_0_0` where the slug is `notconf_cisco_nx_10_4_4`
    -- the version appeared twice, because the version-bearing slug was passed
    as `--device` and then the version was appended again. `4.8.0` from an
    unrelated `$DEVICE_VERSION` made it worse.

    This compiles a real (tiny) module through the real `_compile_all`, so it
    fails if the `--device` / `--device-version` wiring is reverted. It is
    offline and takes seconds; the version-prefix invariant itself is checked
    for every matrix tag by the sibling test above.
    """
    from tests import conftest

    (tmp_path / "tmod.yang").write_text(MINIMAL_YANG)
    monkeypatch.setattr(conftest, "TEMP_CLIENTS", tmp_path / "clients")
    image = {"package": "notconf-cisco-nx", "tag": "10.4-4"}

    conftest._compile_all([tmp_path / "tmod.yang"], [tmp_path / "tmod.yang"], image)

    slug = conftest._slug(image)
    out = tmp_path / "clients" / f"{slug}_restconf"
    # One copy of the code, in an installable project the generated README can
    # name: `uv add <container>/<package>`.
    project = _project(out)
    package = _package(out)
    assert package.name == slug, (
        f"installable package is not named {slug}; found "
        f"{sorted(p.name for p in out.iterdir())}"
    )
    assert package.parent.name == "src", package
    assert not (out / f"{slug}_0_0_0").exists(), "device version double-encoded"
    pyproject = (project / "pyproject.toml").read_text()
    assert f'name = "{slug}"' in pyproject, pyproject[:400]
    assert f'packages = ["src/{slug}"]' in pyproject, pyproject[:400]
    # PEP 440: 10.4-4 -> 10.4.4
    assert 'version = "10.4.4"' in pyproject, pyproject[:400]


def test_golden_shape_failure_names_only_the_differing_keys():
    """The golden mismatch message must name the difference, not both lists.

    The original assertion was `f"...stored={stored} current={shape}"`, which
    dumps both lists whole. The cisco-nx System container has 225 top-level
    keys, so a real failure produced ~4500 characters with the one changed key
    at the very end -- the assertion's entire purpose arriving last. This
    becomes the primary diagnostic for a generator regression once the golden
    fixtures are promoted from run-local to checked-in and blocking.

    Runs offline; the golden test itself is `pytest.mark.integration`.
    """
    from tests.test_sdk_generate import _check_golden_shape

    shape = ["a", "b", "c"]
    golden = Path("x.json")

    # No change: silent pass.
    _check_golden_shape(["a", "b", "c"], shape, "n", golden)
    # Order is not a difference (the golden is stored sorted).
    _check_golden_shape(["c", "b", "a"], shape, "n", golden)

    with pytest.raises(AssertionError) as exc:
        _check_golden_shape(["a", "b"], shape, "n", golden)
    msg = str(exc.value)
    assert "missing=['c']" in msg, msg
    assert "added=[]" in msg, msg
    # The whole point: the full lists must NOT be dumped.
    assert "stored=['a', 'b', 'c']" not in msg, msg

    with pytest.raises(AssertionError) as exc:
        _check_golden_shape(["a", "b", "c", "d"], shape, "n", golden)
    assert "added=['d']" in str(exc.value), str(exc.value)

    # A pre-fix snapshot (payload dump, not a key list) gets an actionable
    # message, not a phantom shape diff.
    with pytest.raises(AssertionError) as exc:
        _check_golden_shape({"interface": []}, shape, "n", golden)
    assert "stale golden" in str(exc.value), str(exc.value)
    assert "Delete tests/fixtures/golden" in str(exc.value), str(exc.value)


def test_device_version_env_fallback_is_scoped_to_its_own_device(monkeypatch):
    """`$DEVICE_VERSION` must only apply to the device `$DEVICE_NAME` names.

    The two are one pair describing ONE lab device -- `.env.example` exports
    them beside DEVICE_IP / DEVICE_USER / DEVICE_PASS -- and `--device`
    inherits `$DEVICE_NAME`. Before this, any compile in a `.env`-loaded shell
    stamped that single device's OS version onto EVERY client: a
    notconf-cisco-nx 10.4-4 image produced package `notconf_cisco_nx_10_4_4_4_8_0`,
    version `4.8.0`, and a README claiming "version 4.8.0".

    The wrong value lands in the package name, the package version and the
    generated README -- publishable metadata -- so this is a provenance bug,
    not a cosmetic one. Models and navigators never saw the value.
    """
    from yang2sdk.cli.compiler import _resolve_device_version

    # conftest loads an untracked local .env into os.environ, so both names must
    # be cleared before each case or this passes locally and fails in CI.
    monkeypatch.delenv("DEVICE_NAME", raising=False)
    monkeypatch.delenv("DEVICE_VERSION", raising=False)

    # Nothing in the environment: never a version, whatever the device.
    assert _resolve_device_version(None, "cisco-nx") == ""

    monkeypatch.setenv("DEVICE_NAME", "g30lab")
    monkeypatch.setenv("DEVICE_VERSION", "1.4.0")

    # The device those variables actually describe: documented behaviour, kept.
    assert _resolve_device_version(None, "g30lab") == "1.4.0"
    # Any other device must not inherit it. This is the regression.
    assert _resolve_device_version(None, "cisco-nx") == ""
    # An explicit flag always wins, for any device.
    assert _resolve_device_version("9.9.9", "cisco-nx") == "9.9.9"
    assert _resolve_device_version("9.9.9", "g30lab") == "9.9.9"
    # An explicit "" is a deliberate "no version", not a request to fall back:
    # otherwise `--device-version ""` would silently resurrect the env value.
    assert _resolve_device_version("", "g30lab") == ""


def test_cli_does_not_stamp_a_foreign_device_version(tmp_path):
    """Wiring: `run_compiler` must actually apply the scoping above.

    The unit test pins `_resolve_device_version`; this pins that the CLI calls
    it. Without this the scoping could be reverted while every helper test
    still passed -- the same failure mode `test_cli_applies_augment_closure_
    end_to_end` guards against for the augment closure.
    """
    (tmp_path / "tmod.yang").write_text(MINIMAL_YANG)
    out = tmp_path / "out"
    env = {**os.environ, "DEVICE_NAME": "g30lab", "DEVICE_VERSION": "9.9.9"}
    code = (
        "import sys; from yang2sdk.cli.compiler import run_compiler; "
        "run_compiler('restconf', sys.argv[1:])"
    )
    cmd = [
        sys.executable,
        "-c",
        code,
        str(tmp_path / "tmod.yang"),
        "--device",
        "someotherdevice",
        "--yang-dir",
        str(tmp_path),
        "--output-dir",
        str(out),
    ]
    proc = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr[-1500:]
    project = _project(out)
    pyproject = (project / "pyproject.toml").read_text()
    assert "9.9.9" not in pyproject, (
        f"foreign DEVICE_VERSION leaked into the package metadata:\n{pyproject}"
    )
    readme = (project / "README.md").read_text()
    assert "9.9.9" not in readme, (
        f"foreign DEVICE_VERSION leaked into the generated README:\n{readme[:600]}"
    )


USED_ONLY_YANG = """module tused {
  prefix td;
  namespace "urn:test:tused";
  import tgrp { prefix tg; }
  revision 2026-01-01;
  container holder {
    uses tg:shared;
  }
}
"""

USED_ONLY_REFINED_YANG = """module tusedref {
  prefix tur;
  namespace "urn:test:tusedref";
  import tgrp { prefix tg; }
  revision 2026-01-01;
  container holder {
    uses tg:shared {
      refine tag {
        default "refined";
      }
    }
  }
}
"""

GROUPING_YANG = """module tgrp {
  prefix tg;
  namespace "urn:test:tgrp";
  revision 2026-01-01;
  grouping shared {
    leaf tag {
      type string;
    }
  }
  container unrelated {
    leaf other {
      type string;
    }
  }
}
"""


def test_augment_closure_excludes_uses_only_modules(tmp_path):
    """A module reached only via `uses` must NOT become a compile root.

    pyang resolves `uses`/`import` from the search path on its own, so listing
    those modules on the command line is not needed to resolve them -- it only
    promotes their own top-level nodes into the generated model. Measured on
    the G30: walking the `uses` edge widened a 2-module compile to 108 and left
    the emitted `ne` model byte-identical, i.e. all extra surface, no extra
    correctness. `cli/closure.py` still offers the full closure via
    `include_uses=True` for the cases that do need it.
    """
    (tmp_path / "tused.yang").write_text(USED_ONLY_YANG)
    (tmp_path / "tgrp.yang").write_text(GROUPING_YANG)
    from yang2sdk.cli.compiler import expand_augment_closure

    roots = [tmp_path / "tused.yang"]
    expanded, _prov = expand_augment_closure(roots, tmp_path)
    assert [p.name for p in expanded] == ["tused.yang"], (
        "a uses-only module was promoted to a root"
    )

    # The resolver still offers the wider closure when a caller asks for it.
    from yang2sdk.cli.closure import resolve_closure

    wide, _p = resolve_closure(["tused"], [tmp_path], include_uses=True)
    assert sorted(p.name for p in wide) == ["tgrp.yang", "tused.yang"]


def test_uses_regex_accepts_quoted_unquoted_and_refined(tmp_path):
    """`_USES_RE` must see every legal `uses` form, and only cross-module ones.

    Three real gaps, each found by running the pattern against the vendor trees
    rather than by reading it:

    * Unquoted `uses p:grouping;` (RFC 7950 Sec 6.2.1 permits it) matched
      nothing. The old pattern hard-required quotes, so SR Linux and the notconf
      images scored **0** uses references — the uses edge was dead for both.
    * `uses p:grouping { refine ...; }` (RFC 7950 Sec 7.12) terminates with `{`,
      not `;`. Requiring `;` dropped 60 references from the G30.
    * `refuses "a:b";` must not match, hence the word boundary.
    """
    from yang2sdk.cli.closure import _USES_RE

    def uses(text):
        return _USES_RE.findall(text)

    # All three terminator/quoting styles are cross-module references.
    assert uses('uses "tg:shared";') == ["tg:shared"]
    assert uses("uses tg:shared;") == ["tg:shared"]
    assert uses('uses "tg:shared" {') == ["tg:shared"]
    assert uses('uses\t"a:b";') == ["a:b"]

    # Unprefixed: a grouping in the module's own namespace. Not an edge.
    assert uses('uses "capabilities";') == []
    assert uses('uses "fault-management" {') == []

    # A longer identifier ending in the same text is captured whole.
    assert uses('uses "tg:shared-extra";') == ["tg:shared-extra"]

    # Keyword-embedded text is not a statement.
    assert uses('refuses "a:b";') == []

    # KNOWN LIMITATION, pinned deliberately: none of the patterns in
    # closure.py strip comments or string literals, so a `description` that
    # quotes YANG verbatim is read as a real reference. That is over-inclusive,
    # never under-inclusive -- pyang still resolves the module either way, the
    # worst case is one unnecessary module on the command line. Fixing it
    # properly means stripping comments for every pattern here, which is a
    # separate change; this assertion exists so the behaviour is recorded
    # rather than discovered later.
    assert uses('description "uses a:b;";') == ["a:b"]


def test_uses_edge_pulls_in_unquoted_references(tmp_path):
    """End-to-end: an unquoted `uses` must reach the resolver's fixpoint."""
    (tmp_path / "tused.yang").write_text(USED_ONLY_YANG)
    (tmp_path / "tgrp.yang").write_text(GROUPING_YANG)
    from yang2sdk.cli.closure import resolve_closure

    wide, prov = resolve_closure(["tused"], [tmp_path], include_uses=True)
    assert sorted(p.name for p in wide) == ["tgrp.yang", "tused.yang"]
    assert prov.get("tgrp") == ["tused"]


def test_uses_edge_pulls_in_a_refined_uses(tmp_path):
    """`uses "p:g" { refine ... }` terminates with `{`, not `;` (RFC 7950 §7.12)."""
    (tmp_path / "tusedref.yang").write_text(USED_ONLY_REFINED_YANG)
    (tmp_path / "tgrp.yang").write_text(GROUPING_YANG)
    from yang2sdk.cli.closure import resolve_closure

    wide, prov = resolve_closure(["tusedref"], [tmp_path], include_uses=True)
    assert sorted(p.name for p in wide) == ["tgrp.yang", "tusedref.yang"]
    assert prov.get("tgrp") == ["tusedref"]


CASE_COLLIDE_BASE_YANG = """module tcollide {
  prefix tc;
  namespace "urn:test:case-collide";
  revision 2026-01-01;
  grouping shared {
    leaf a {
      type string;
    }
  }
  container parent {
    container CamelCase-items {
      uses tc:shared;
    }
    container camelcase-items {
      uses tc:shared;
    }
  }
}
"""


def test_case_differing_siblings_are_not_merged_into_one_model(tmp_path):
    """Two YANG nodes differing only in case must NOT share a model class.

    Regression, measured on Cisco NX-OS 10.4-4 (2026-10-13). That model declares
    BOTH `container gTimers-items` and `container gtimers-items`. YANG
    identifiers are case-sensitive (RFC 7950 Sec 6.2), as are XML element
    names, but both fold to the Python class name `GtimersItems`. The
    equivalence check compared fields only, judged them equivalent, and
    `_register_model` reused the first -- so the second node was silently
    dropped and every read of it failed:

        ParsingError: root element not found
        (actual: ...gtimers-items, expected: ...gTimers-items)

    Both models must survive, each with its own wire tag.
    """
    (tmp_path / "tcollide.yang").write_text(CASE_COLLIDE_BASE_YANG)
    out = tmp_path / "tcollide_restconf"
    code = (
        "import sys; from yang2sdk.cli.compiler import run_compiler; "
        "run_compiler('netconf', sys.argv[1:])"
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(tmp_path / "tcollide.yang"),
            "--device",
            "tcollide",
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
    model = _pkg_file(out, "data_models", "tcollide.py")
    text = model.read_text()

    classes = re.findall(r"^class (\w+)\(.*?tag=\"([^\"]+)\"", text, re.MULTILINE)
    tags = [t for _, t in classes]
    assert "camelcase-items" in tags, f"lowercase node lost its model: {classes}"
    assert "CamelCase-items" in tags, f"camelCase node lost its model: {classes}"
    # Distinct classes, not one reused twice.
    assert len({t for t in tags}) == len(tags)


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
    models = _import(out, "data_models.tcontent")
    data_cls = models.TcontentData

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
    nav_mod = _import(out, "data_navigators")
    models = _import(out, "data_models.env")

    client = _RecordingClient()
    data = nav_mod.Data(client, "/data", "")
    list_nav = data.env_parent.things

    thing = models.ThingsItem.model_validate({"id": "a", "val": "x", "some_leaf": "s"})
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
    models = _import(out, "data_models.tmain")
    from lxml import etree  # ty: ignore[unresolved-import] - lxml stubs (cf. src/)

    # Generated by this test at runtime; import dynamically so the
    # type checkers do not try to resolve a nonexistent source module.
    bind_module_prefixes = _import(out, "session_manager").bind_module_prefixes

    item_cls = models.ItemsItem
    assert item_cls.model_fields["kind"].json_schema_extra.get("is_identityref") is True

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


NSMAP_YANG = """module tnsmap {
  prefix tn;
  namespace "urn:test:tnsmap";
  import ietf-netconf {
    prefix nc;
  }
  revision 2026-01-01;
  container top {
    leaf name {
      type string;
    }
  }
  rpc do-thing {
    input {
      leaf arg {
        type string;
      }
    }
    output {
      leaf result {
        type string;
      }
    }
  }
}
"""


def test_netconf_declares_the_module_nsmap_once_and_models_inherit_it(tmp_path):
    """The per-module nsmap is emitted once, on an intermediate base class.

    It used to be re-declared on every model class -- identical for all of them,
    because `namespace` and `imports_nsmap` are per-module (measured on the G30
    tree: the same 25-entry map on 1358 classes, 1.9 MiB of `data_models/`).
    pydantic-xml merges a subclass nsmap into the parent's
    (`BaseXmlModel.__init_subclass__`: `dict(parent_nsmap, **nsmap)`), so one
    declaration is enough -- but the emitter must keep emitting it, or the
    models lose `nc` / the default namespace.

    Two things this pins that the earlier template got wrong:
    * a module that imports ietf-netconf (prefix `nc`) emitted `"nc"` twice in
      the same dict literal;
    * the RPC envelopes derive from `NetconfXmlModel`, not `NetconfBaseModel`,
      so they need their own base and cannot share the data-model one.
    """
    out = _gen_netconf(NSMAP_YANG, "tnsmap", tmp_path)
    source = (_package(out) / "data_models" / "tnsmap.py").read_text()

    declarations = [
        line
        for line in source.splitlines()
        if line.startswith("class ") and "nsmap={" in line
    ]
    assert [line.split("(")[0] for line in declarations] == [
        "class _ModuleBase",
        "class _ModuleRpcBase",
    ], declarations
    for line in declarations:
        assert line.count('"nc":') == 1, f"duplicate nc entry: {line}"
        assert '"nc": "urn:ietf:params:xml:ns:netconf:base:1.0"' in line
        assert '"": "urn:test:tnsmap"' in line
        assert '"tn": "urn:test:tnsmap"' in line
    assert "nsmap={" not in "".join(
        line
        for line in source.splitlines()
        if line.startswith("class ") and not line.startswith("class _")
    ), "a model class re-declares the nsmap"

    models = _import(out, "data_models.tnsmap")
    top = models.Top
    assert top.__xml_nsmap__ == {
        "nc": "urn:ietf:params:xml:ns:netconf:base:1.0",
        "": "urn:test:tnsmap",
        "tn": "urn:test:tnsmap",
    }, top.__xml_nsmap__
    # The inherited map has to reach the serializer, not just the class:
    # `nc_operation` is an attribute resolved through the model nsmap
    # (RFC 6241 Sec 7.1), and the element carries the module's default ns.
    inst = top(name="x")
    inst.nc_operation = "merge"
    xml = inst.to_xml()
    xml = xml if isinstance(xml, bytes) else xml.encode()
    assert b'nc:operation="merge"' in xml, xml
    assert b'xmlns="urn:test:tnsmap"' in xml, xml
    # The RPC envelope comes from the other base and still gets the same map.
    assert models.DoThing.__xml_nsmap__ == top.__xml_nsmap__
    assert "nc_operation" not in models.DoThing.model_fields

    # model_gaps inventories model classes by walking module attributes; the
    # nsmap bases are plumbing and must not be counted as models.
    surface = model_gaps.introspect(out, max_depth=8, max_paths=5000)
    assert "tnsmap.Top" in surface.model_classes, surface.model_classes
    assert not [
        name for name in surface.model_classes if name.split(".", 1)[1].startswith("_")
    ], surface.model_classes


INSTANCE_ID_YANG = """module tid {
  yang-version 1.1;
  prefix ti;
  namespace "urn:test:tid";
  revision 2026-01-01;

  container system {
    list user {
      key "name";
      leaf name {
        type string;
      }
      leaf enabled {
        type boolean;
      }
    }
    list server {
      key "ip port";
      leaf ip {
        type string;
      }
      leaf port {
        type uint16;
      }
    }
    list flag {
      key "on";
      leaf on {
        type boolean;
      }
    }
    list marker {
      key "tag";
      leaf tag {
        type empty;
      }
    }
  }

  rpc do-thing {
    input {
      leaf arg {
        type string;
      }
    }
  }
}
"""

INSTANCE_ID_AUGMENT_YANG = """module tidadd {
  yang-version 1.1;
  prefix ta;
  namespace "urn:test:tidadd";
  import tid { prefix ti; }
  revision 2026-01-01;

  augment "/ti:system/ti:user" {
    container prefs {
      list addr {
        key "ip";
        leaf ip {
          type string;
        }
      }
    }
  }
}
"""


def test_instance_identifier_names_each_node_by_its_defining_module(tmp_path):
    """`instance_identifier()` renders RFC 7950 Sec 9.13 / RFC 7951 Sec 6.11.

    RFC 7951 Sec 6.11 gives the exact shape:

        /ietf-interfaces:interfaces/interface[name='eth0']/ietf-ip:ipv4/ip

    -- the leftmost node always qualified, a later node qualified only when its
    module differs from its parent's, and the same rule for key names. RFC 7950
    Sec 9.13 adds that every key of a list entry needs a predicate, and Sec 14
    defines `quoted-string` with no escape mechanism.

    The qualification is a SCHEMA fact, so it must come from the compile-time
    module map, not from `client.namespace_modules` (which the device's <hello>
    overlays). The navigators are built with `client=None` here precisely to
    pin that no transport is needed to name a path.
    """
    out = _gen_netconf(
        INSTANCE_ID_YANG,
        "tid",
        tmp_path,
        extra={"tidadd.yang": INSTANCE_ID_AUGMENT_YANG},
    )
    nav = _import(out, "data_navigators")
    base = _import(out, "data_navigators._base")
    session = _import(out, "session_manager")

    # One literal, inverted once: the navigator map is derived from the
    # session manager's compile-time map rather than re-declared.
    assert session.MODULE_NAMESPACES == {
        "tid": "urn:test:tid",
        "tidadd": "urn:test:tidadd",
    }
    assert base.NAMESPACE_MODULES == {
        "urn:test:tid": "tid",
        "urn:test:tidadd": "tidadd",
    }

    data = nav.Data(None, [])
    system = data.tid_system

    # Same module all the way down: only the top-level node is qualified.
    assert system.instance_identifier() == "/tid:system"
    assert (
        system.user("alice").instance_identifier() == "/tid:system/user[name='alice']"
    )

    # RFC 7950 Sec 9.13: one predicate per key, in key order.
    assert (
        system.server("192.0.2.1", 80).instance_identifier()
        == "/tid:system/server[ip='192.0.2.1'][port='80']"
    )

    # RFC 7950 Sec 9.7.2: a boolean key's lexical form is true/false, not
    # Python's True/False.
    assert system.flag(True).instance_identifier() == "/tid:system/flag[on='true']"

    # RFC 7950 Sec 9.13: a key of type empty has no value and is a zero-length
    # string; None is refused rather than rendered as "None".
    assert system.marker("").instance_identifier() == "/tid:system/marker[tag='']"
    with pytest.raises(ValueError, match="cannot be None"):
        system.user(None).instance_identifier()

    # RFC 7950 Sec 7.17: an augmented node keeps its own module, so the path
    # qualifies at the module boundary and drops the prefix again after it.
    assert (
        system.user("alice").prefs.instance_identifier()
        == "/tid:system/user[name='alice']/tidadd:prefs"
    )
    assert (
        system.user("alice").prefs.addr("10.0.0.1").instance_identifier()
        == "/tid:system/user[name='alice']/tidadd:prefs/addr[ip='10.0.0.1']"
    )

    # RFC 7950 Sec 14: either quote style, whichever the value allows.
    assert (
        system.user("o'brien").instance_identifier()
        == '/tid:system/user[name="o\'brien"]'
    )

    # A value holding both quote characters has no representable form.
    with pytest.raises(ValueError, match="both quote characters"):
        system.user("both'\"quotes").instance_identifier()

    # Not instance nodes: the datastore root, a list collection, an rpc.
    with pytest.raises(ValueError, match="datastore root"):
        data.instance_identifier()
    with pytest.raises(ValueError, match="list collection"):
        system.user.instance_identifier()
    with pytest.raises(ValueError, match="not in the data tree"):
        nav.Operations(None, []).tid_do_thing.instance_identifier()

    # An unknown namespace must fail closed: a bare name would silently mean
    # "same module as the parent" (RFC 7951 Sec 6.11), i.e. a different path.
    with pytest.raises(ValueError, match="no module name is known"):
        base.Node(None, [("urn:test:unknown", "x", {})]).instance_identifier()


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
    carries module revisions, no secrets/lab paths, and the package imports as
    `<package_name>` with nothing but the project on `sys.path`.
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
    project = _project(out)
    package = _package(out)
    assert (project / "pyproject.toml").exists()
    assert (project / "README.md").exists()
    assert (project / "MANIFEST.yang-revisions.json").exists()
    assert (package / "py.typed").exists()
    import tomllib

    pyproj = tomllib.loads((project / "pyproject.toml").read_text())
    assert pyproj["project"]["name"] == "tmod_1_2_3"
    assert pyproj["project"]["version"] == "1.2.3"
    assert "data_models" not in pyproj["project"]["name"]
    assert pyproj["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/tmod_1_2_3"
    ]
    manifest = json.loads((project / "MANIFEST.yang-revisions.json").read_text())
    assert manifest["device"] == "tmod"
    assert manifest["device_version"] == "1.2.3"
    assert manifest["protocol"] == "restconf"
    assert any(m["name"] == "tmod" for m in manifest["modules"])
    hay = (project / "pyproject.toml").read_text() + (project / "README.md").read_text()
    for secret in ("DEVICE_PASS", "DEVICE_USER", "192.168", "BEGIN PRIVATE"):
        assert secret not in hay
    # The installable package imports with only `src/` on the path: no temp/,
    # no lab path, no second copy.
    assert package.parent.name == "src"
    sys.path.insert(0, str(package.parent))
    try:
        pkg = importlib.import_module("tmod_1_2_3")
        assert pkg.RestconfClient is not None
    finally:
        sys.path.remove(str(package.parent))


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
    manifest = json.loads((_project(out) / "MANIFEST.yang-revisions.json").read_text())
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
    sm_mod = _import(out, "session_manager")

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
    auto_manifest = json.loads(
        (_project(auto) / "MANIFEST.yang-revisions.json").read_text()
    )
    assert auto_manifest["features_source"] == "device"
    assert auto_manifest["features"] == ["tfeat:other"]
    # `g` is `if-feature "not f"` and f is unsupported, so the auto-detected
    # device set must keep it in the model.
    assert "G" in (_package(auto) / "data_models" / "tfeat.py").read_text()

    opted_out = tmp_path / "opted_out"
    _run_compiler("restconf", yang, tmp_path, opted_out, ["--no-device-features"])
    out_manifest = json.loads(
        (_project(opted_out) / "MANIFEST.yang-revisions.json").read_text()
    )
    assert out_manifest["features_source"] == "none"
    assert out_manifest["features"] == []
    assert (
        "class G(" not in (_package(opted_out) / "data_models" / "tfeat.py").read_text()
    )


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


def _project(out) -> Path:
    """The single installable project inside an ``--output-dir`` container.

    The layout is defined in `yang2sdk.layout`; the assert makes a container
    holding two projects (a stale compile for another device version) loud
    here rather than in whichever consumer globbed first.
    """
    projects = [
        p for p in sorted(Path(out).iterdir()) if (p / "pyproject.toml").is_file()
    ]
    assert len(projects) == 1, (
        f"expected one project in {out}: {[p.name for p in projects]}"
    )
    return projects[0]


def _package(out) -> Path:
    """The importable package inside a container/project: ``<project>/src/<pkg>``."""
    return layout.resolve_package(Path(out))


def _pkg_file(out, *parts) -> Path:
    """A path inside the generated package: ``<project>/src/<pkg>/<parts...>``."""
    return _package(out).joinpath(*parts)


_IMPORTED_FROM: dict[str, str] = {}


def _purge_package(name: str) -> None:
    """Drop a package and its submodules from `sys.modules`."""
    prefix = f"{name}."
    for stale in [n for n in sys.modules if n == name or n.startswith(prefix)]:
        del sys.modules[stale]


def _import(out, dotted, alias=None):
    """Import ``<package>.<dotted>`` from a generated container or project.

    The generated package is named after the *device*, so several tests here
    compile the same device name from different YANG into different tmp dirs.
    Python caches the first one, and a later test then silently asserts against
    an earlier test's model -- tests that pass alone and fail in a full run.
    Purge only when the package is being imported from a *different* directory,
    so two imports inside one test still see the same module objects (a
    navigator's lazy `from .x import Y` must resolve to the same classes the
    test holds).

    ``alias`` loads the package under a private top-level name instead, which
    is what a caller needs when two *protocols* for one device are alive in the
    same process: they share one distribution name (`<device>_<version>`, no
    protocol in it), so they cannot both answer to it -- and they could not be
    installed side by side either. `sdk-verify --protocol both` loads them the
    same way for the same reason.
    """
    package = _package(out)
    parent = str(package.parent)
    if alias:
        spec = importlib.util.spec_from_file_location(
            alias, package / "__init__.py", submodule_search_locations=[str(package)]
        )
        assert spec is not None and spec.loader is not None, package
        module = importlib.util.module_from_spec(spec)
        sys.modules[alias] = module
        spec.loader.exec_module(module)
        target = alias if not dotted else f"{alias}.{dotted}"
        return importlib.import_module(target)
    if _IMPORTED_FROM.get(package.name, parent) != parent:
        _purge_package(package.name)
    _IMPORTED_FROM[package.name] = parent
    sys.path.insert(0, parent)
    try:
        target = package.name if not dotted else f"{package.name}.{dotted}"
        return importlib.import_module(target)
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


# --- NETCONF depth: filter, skeleton, and D pruner -------------------------

DEPTH_YANG = """module tdepth {
  prefix td;
  namespace "urn:test:tdepth";
  revision 2026-01-01;
  container root {
    leaf name { type string; }
    leaf-list tags { type string; }
    container child {
      leaf value { type string; }
      container grand { leaf deep { type string; } }
    }
    container wrapper {
      container inner { leaf deep { type string; } }
    }
    container strict {
      leaf must-have { type string; mandatory true; }
    }
    list items {
      key "id";
      leaf id { type string; }
      leaf label { type string; }
      container detail { leaf code { type string; } }
    }
    // YANG 1.1 permits a keyless list when config is false; pyang otherwise
    // refuses the model with NEED_KEY. This is the shape the depth filter must
    // treat as a boundary keyless list.
    list keyless {
      config false;
      leaf value { type string; }
    }
  }
}
"""

_DEPTH_XML = (
    '<root xmlns="urn:test:tdepth">'
    "<name>n</name><tags>t</tags>"
    "<child><value>v</value><grand><deep>d</deep></grand></child>"
    "<wrapper><inner><deep>d</deep></inner></wrapper>"
    "<strict><must-have>m</must-have></strict>"
    "<items><id>a</id><label>l</label><detail><code>c</code></detail></items>"
    "<keyless><value>kv</value></keyless>"
    "</root>"
)


def _depth_filter_tags(element):
    from lxml import etree as _et  # ty: ignore[unresolved-import] - lxml ships no stubs

    return {_et.QName(child).localname for child in element}


def _assert_filter_is_bounded(base, element, model_cls):
    """Every complex element must retain a selection node (RFC 6241 Sec 6.2)."""
    from lxml import etree as _et  # ty: ignore[unresolved-import] - lxml ships no stubs

    for info in model_cls.model_fields.values():
        extra = info.json_schema_extra or {}
        tag = extra.get("tag")
        if not tag:
            continue
        child_cls, _is_list = base._child_model_and_kind(info.annotation)
        if child_cls is None:
            continue
        for child in element:
            if _et.QName(child).localname != tag:
                continue
            if extra.get("ns") and _et.QName(child).namespace != extra.get("ns"):
                continue
            assert not base._is_whole_subtree_trigger(child), (
                f"depth filter has a whole-subtree trigger at {tag}: "
                f"{_et.tostring(child, encoding='unicode')}"
            )
            _assert_filter_is_bounded(base, child, child_cls)


def test_netconf_depth_filter_never_selects_a_whole_subtree(tmp_path):
    """RFC 6241 has no server-side depth, so the generated filter is the
    ceiling. An empty complex element is a selection node and would fetch the
    whole subtree; a content-match-only sibling set would too. The builder
    must drop boundary containers/keyless lists, keep boundary keys, and
    collapse to `None` (skeleton) when the target itself is a trigger.
    """
    from lxml import etree as _et  # ty: ignore[unresolved-import] - lxml ships no stubs

    out, _ = _gen(tmp_path, "netconf", DEPTH_YANG, "tdepth")
    base = _import(out, "data_navigators._base")
    models = _import(out, "data_models.tdepth")

    root_path = [("urn:test:tdepth", "root", {})]
    f2 = base.build_depth_filter(root_path, models.Root, 2)
    assert f2 is not None
    _assert_filter_is_bounded(base, f2, models.Root)
    assert all("operation" not in _et.QName(node).localname for node in f2.iter()), (
        "nc:operation is an attribute, not a data node in a read filter"
    )
    assert _depth_filter_tags(f2) == {"name", "tags", "items"}, (
        f"depth 2 must drop boundary containers and the keyless list: "
        f"{_et.tostring(f2, encoding='unicode')}"
    )
    items2 = next(c for c in f2 if _et.QName(c).localname == "items")
    assert _depth_filter_tags(items2) == {"id"}, (
        "a keyed list on the boundary must keep keys only"
    )

    f3 = base.build_depth_filter(root_path, models.Root, 3)
    assert f3 is not None
    _assert_filter_is_bounded(base, f3, models.Root)
    assert _depth_filter_tags(f3) == {
        "name",
        "tags",
        "child",
        "strict",
        "items",
        "keyless",
    }, (
        "wrapper's only child is on the boundary, so the emptied wrapper must "
        f"be removed: {_et.tostring(f3, encoding='unicode')}"
    )
    child3 = next(c for c in f3 if _et.QName(c).localname == "child")
    assert _depth_filter_tags(child3) == {"value"}
    items3 = next(c for c in f3 if _et.QName(c).localname == "items")
    assert _depth_filter_tags(items3) == {"id", "label"}

    f4 = base.build_depth_filter(root_path, models.Root, 4)
    assert f4 is not None
    _assert_filter_is_bounded(base, f4, models.Root)
    assert _depth_filter_tags(f4) == {
        "name",
        "tags",
        "child",
        "wrapper",
        "strict",
        "items",
        "keyless",
    }
    child4 = next(c for c in f4 if _et.QName(c).localname == "child")
    assert _depth_filter_tags(child4) == {"value", "grand"}
    wrapper4 = next(c for c in f4 if _et.QName(c).localname == "wrapper")
    inner4 = next(c for c in wrapper4 if _et.QName(c).localname == "inner")
    assert _depth_filter_tags(inner4) == {"deep"}
    items4 = next(c for c in f4 if _et.QName(c).localname == "items")
    assert _depth_filter_tags(items4) == {"id", "label", "detail"}

    # depth=1 on a container cannot be expressed without selecting its whole
    # subtree: the caller must get a skeleton instead.
    assert base.build_depth_filter(root_path, models.Root, 1) is None

    # A keyed list collection at depth=1 is expressible as keys-only.
    items_path = [("urn:test:tdepth", "root", {}), ("urn:test:tdepth", "items", {})]
    f_items1 = base.build_depth_filter(items_path, models.ItemsItem, 1)
    assert f_items1 is not None
    _assert_filter_is_bounded(base, f_items1, models.Root)
    items1 = f_items1[0]
    assert _depth_filter_tags(items1) == {"id"}
    assert all(child.text is None for child in items1), (
        "collection keys must be selection nodes, not content matches"
    )

    # A keyless list at depth=1 has no addressable key and no kept child.
    keyless_path = [
        ("urn:test:tdepth", "root", {}),
        ("urn:test:tdepth", "keyless", {}),
    ]
    assert base.build_depth_filter(keyless_path, models.KeylessItem, 1) is None

    # A specific list item at depth=1 has only its path keys (content matches).
    item_path = [
        ("urn:test:tdepth", "root", {}),
        ("urn:test:tdepth", "items", {"id": "a"}),
    ]
    assert base.build_depth_filter(item_path, models.ItemsItem, 1) is None
    f_item2 = base.build_depth_filter(item_path, models.ItemsItem, 2)
    assert f_item2 is not None
    _assert_filter_is_bounded(base, f_item2, models.Root)
    items2 = f_item2[0]
    assert _depth_filter_tags(items2) == {"id", "label"}
    assert items2[0].text == "a", "the path key must stay a content match"
    assert items2[1].text is None, "the selected leaf must stay a selection node"

    # A container target whose only child is on the boundary collapses.
    wrapper_path = [
        ("urn:test:tdepth", "root", {}),
        ("urn:test:tdepth", "wrapper", {}),
    ]
    assert base.build_depth_filter(wrapper_path, models.Wrapper, 2) is None
    f_wrapper3 = base.build_depth_filter(wrapper_path, models.Wrapper, 3)
    assert f_wrapper3 is not None
    _assert_filter_is_bounded(base, f_wrapper3, models.Root)

    # `fields` may not smuggle a complex field in empty; a leaf is fine.
    assert base.build_depth_filter(root_path, models.Root, 2, fields=["child"]) is None
    f_field = base.build_depth_filter(root_path, models.Root, 2, fields=["name"])
    assert f_field is not None
    assert _depth_filter_tags(f_field) == {"name"}
    f_field3 = base.build_depth_filter(root_path, models.Root, 3, fields=["child"])
    assert f_field3 is not None
    _assert_filter_is_bounded(base, f_field3, models.Root)
    assert _depth_filter_tags(f_field3) == {"child"}
    assert _depth_filter_tags(f_field3[0]) == {"value"}


def test_netconf_depth_pruner_matches_restconf_boundary(tmp_path):
    """The local safety net must use the same level convention as RESTCONF:
    target level 1, boundary containers/keyless lists removed, boundary keys
    kept. It must trim a server that ignores the filter.
    """
    from lxml import etree as _et  # ty: ignore[unresolved-import] - lxml ships no stubs

    out, _ = _gen(tmp_path, "netconf", DEPTH_YANG, "tdepth")
    base = _import(out, "data_navigators._base")
    models = _import(out, "data_models.tdepth")

    root1 = _et.fromstring(_DEPTH_XML)
    base.prune_xml_tree_to_depth(root1, models.Root, 1)
    assert _depth_filter_tags(root1) == set(), "depth 1 is the target alone"

    root2 = _et.fromstring(_DEPTH_XML)
    base.prune_xml_tree_to_depth(root2, models.Root, 2)
    assert _depth_filter_tags(root2) == {"name", "tags", "items"}
    items2 = next(c for c in root2 if _et.QName(c).localname == "items")
    assert _depth_filter_tags(items2) == {"id"}

    root3 = _et.fromstring(_DEPTH_XML)
    base.prune_xml_tree_to_depth(root3, models.Root, 3)
    assert _depth_filter_tags(root3) == {
        "name",
        "tags",
        "child",
        "wrapper",
        "strict",
        "items",
        "keyless",
    }
    child3 = next(c for c in root3 if _et.QName(c).localname == "child")
    assert _depth_filter_tags(child3) == {"value"}
    wrapper3 = next(c for c in root3 if _et.QName(c).localname == "wrapper")
    assert _depth_filter_tags(wrapper3) == set(), (
        "a non-boundary container emptied by pruning stays as an empty shell "
        "(RESTCONF _maybe_prune only drops empties exactly at the boundary)"
    )
    items3 = next(c for c in root3 if _et.QName(c).localname == "items")
    assert _depth_filter_tags(items3) == {"id", "label"}

    entry1 = _et.fromstring(
        '<items xmlns="urn:test:tdepth"><id>a</id><label>l</label>'
        "<detail><code>c</code></detail></items>"
    )
    base.prune_xml_tree_to_depth(entry1, models.ItemsItem, 1)
    assert _depth_filter_tags(entry1) == {"id"}


def _netconf_depth_client(replies=None, forbid=False):
    from lxml import etree as _et  # ty: ignore[unresolved-import] - lxml ships no stubs

    class _Client:
        module_namespaces: ClassVar[dict[str, str]] = {}
        has_nmda = False

        def __init__(self):
            self.calls = []
            self.replies = list(replies or [])
            self.forbid = forbid

        def retrieve(self, **kwargs):
            if self.forbid:
                raise AssertionError("skeleton path must not hit the wire")
            self.calls.append(kwargs)
            return self.replies.pop(0) if self.replies else None

    return _Client(), _et


def test_netconf_depth_skeleton_does_not_hit_the_wire(tmp_path, caplog):
    """A collapsed finite-depth filter is not sent. The caller gets the
    RESTCONF-shaped skeleton (container empty model, keyed item with path
    keys, keyless list []) and a warning; no RPC is issued.
    """
    out, _ = _gen(tmp_path, "netconf", DEPTH_YANG, "tdepth")
    nav = _import(out, "data_navigators")

    client, _ = _netconf_depth_client(forbid=True)
    data = nav.Data(client, [])

    with caplog.at_level("WARNING"):
        root = data.tdepth_root.retrieve(depth=1)
    assert root is not None and client.calls == []
    assert getattr(root, "_yang2sdk_skeleton", False) is True
    assert "whole subtree" in caplog.text
    assert "root" in caplog.text

    caplog.clear()
    with caplog.at_level("WARNING"):
        item = data.tdepth_root.items("a").retrieve(depth=1)
    assert item is not None and item.id == "a"
    assert getattr(item, "_yang2sdk_skeleton", False) is True
    assert client.calls == []
    assert "root/items" in caplog.text
    assert "'a'" not in caplog.text, "the warning must not log instance key values"

    keyless = data.tdepth_root.keyless.retrieve(depth=1)
    assert keyless == [] and client.calls == []
    assert getattr(keyless, "_yang2sdk_skeleton", False) is True

    # Mandatory fields cannot validate an empty container; the skeleton path
    # deliberately falls back to model_construct instead of raising.
    caplog.clear()
    with caplog.at_level("WARNING"):
        strict = data.tdepth_root.strict.retrieve(depth=1)
    assert strict is not None and client.calls == []
    assert getattr(strict, "_yang2sdk_skeleton", False) is True
    assert "cannot satisfy mandatory fields" in caplog.text


def test_netconf_skeleton_shape_matches_restconf_depth_one(tmp_path):
    """The skeleton mirrors the RESTCONF visible shape for the expressible
    depth-1 cases: empty container, empty keyless list, keyed item with keys.
    """
    rout, _ = _gen(tmp_path, "restconf", DEPTH_YANG, "tdepth")
    rbase = _import(rout, "data_navigators._base")

    assert rbase._maybe_prune({}, 1) == {}, "container depth 1 is empty"
    assert rbase._maybe_prune([{}], 1) == [], "keyless list depth 1 is []"
    assert rbase._maybe_prune([{"id": "a"}], 1) == [{"id": "a"}], (
        "keyed list item depth 1 keeps its keys"
    )
    assert rbase._maybe_prune({"id": "a"}, 1) == {"id": "a"}


def test_netconf_depth_uses_the_filter_and_prunes_a_lying_server(tmp_path):
    """The wire path sends a bounded filter; a reply deeper than asked is
    still trimmed locally (the client cannot undo received bytes, but the
    model stays bounded).
    """
    from lxml import etree as _et  # ty: ignore[unresolved-import] - lxml ships no stubs

    out, _ = _gen(tmp_path, "netconf", DEPTH_YANG, "tdepth")
    nav = _import(out, "data_navigators")

    # Keyed collection at depth 1: keys-only filter, keys-only entries.
    reply = _et.fromstring(
        '<data xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">'
        '<root xmlns="urn:test:tdepth">'
        "<items><id>a</id></items><items><id>b</id></items>"
        "</root></data>"
    )
    client, _et2 = _netconf_depth_client(replies=[reply])
    data = nav.Data(client, [])
    items = data.tdepth_root.items.retrieve(depth=1)
    assert [item.id for item in items] == ["a", "b"]
    assert getattr(items, "_yang2sdk_skeleton", False) is False
    sent = client.calls[0]["filter_xml"]
    items_filter = next(c for c in sent if _et2.QName(c).localname == "items")
    assert _depth_filter_tags(items_filter) == {"id"}
    assert all(child.text is None for child in items_filter)

    # Specific item at depth 2: path key stays a content match, label is a
    # selection node, and the deeper detail container is trimmed.
    reply = _et.fromstring(
        '<data xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">'
        '<root xmlns="urn:test:tdepth"><items>'
        "<id>a</id><label>l</label><detail><code>c</code></detail>"
        "</items></root></data>"
    )
    client, _et2 = _netconf_depth_client(replies=[reply])
    data = nav.Data(client, [])
    item = data.tdepth_root.items("a").retrieve(depth=2)
    assert item.id == "a" and item.label == "l" and item.detail is None
    sent = client.calls[0]["filter_xml"]
    items_filter = next(c for c in sent if _et2.QName(c).localname == "items")
    assert _depth_filter_tags(items_filter) == {"id", "label"}
    assert items_filter[0].text == "a"
    assert items_filter[1].text is None

    # A server that ignores the filter and returns everything is trimmed.
    reply = _et.fromstring(
        '<data xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">'
        + _DEPTH_XML
        + "</data>"
    )
    client, _et2 = _netconf_depth_client(replies=[reply])
    data = nav.Data(client, [])
    model = data.tdepth_root.retrieve(depth=2)
    assert model.name == "n"
    assert model.child is None, "boundary container survived the pruner"
    assert model.keyless is None, "boundary keyless list survived the pruner"
    assert [entry.id for entry in model.items] == ["a"]
    assert model.items[0].label is None, "boundary leaf survived the pruner"


def test_depth_validation_rejects_only_inexpressible_values(tmp_path):
    """`depth=1` is valid; reject bool/0/negative/non-numeric/>65535/None
    before any transport work, on both protocols.
    """
    out, _ = _gen(tmp_path, "netconf", DEPTH_YANG, "tdepth")
    base = _import(out, "data_navigators._base")
    nav = _import(out, "data_navigators")

    assert base.validate_depth(1) == 1
    assert base.validate_depth("2") == 2
    assert base.validate_depth(65535) == 65535
    assert base.validate_depth("unbounded") is None
    assert base.validate_depth("UNBOUNDED") is None
    for bad in (0, -1, True, "foo", 70000, None, 1.5, "", " 2 "):
        with pytest.raises(ValueError, match="depth must be"):
            base.validate_depth(bad)

    client, _et = _netconf_depth_client(forbid=True)
    data = nav.Data(client, [])
    for bad in (0, -1, True, "foo", 70000, None):
        with pytest.raises(ValueError, match="depth must be"):
            data.tdepth_root.retrieve(depth=bad)
    assert client.calls == []

    # depth=1 is accepted (the skeleton path), not rejected.
    assert data.tdepth_root.retrieve(depth=1) is not None

    # RESTCONF uses the same validator before its HTTP request.
    rout, _ = _gen(tmp_path / "rc", "restconf", DEPTH_YANG, "tdepth")
    rsm = _import(rout, "session_manager")
    rnav = _import(rout, "data_navigators")
    assert rsm.validate_depth(1) == 1
    assert rsm.validate_depth("unbounded") is None
    for bad in (0, -1, True, "foo", 70000, None):
        with pytest.raises(ValueError, match="depth must be"):
            rsm.validate_depth(bad)
    rclient = _Recorder(reply={"tdepth:root": {}})
    rdata = rnav.Data(rclient, "/data", "")
    for bad in (0, -1, True, "foo", 70000, None):
        with pytest.raises(ValueError, match="depth must be"):
            rdata.tdepth_root.retrieve(depth=bad)
    assert rclient.calls == []


KEYS_YANG = """module tkeys {
  prefix tk;
  namespace "urn:test:tkeys";
  revision 2026-01-01;
  container root {
    list items {
      key "id";
      leaf id { type string; }
      leaf cfg { type string; }
      leaf state { type string; config false; }
    }
  }
}
"""


def test_netconf_content_pruning_keeps_list_keys(tmp_path):
    """A `content="nonconfig"` read of a config list keeps its state leaves
    and its keys; dropping the keys made every entry unaddressable. The list
    shell is still dropped when only keys remain and the list itself does not
    match the requested content.
    """
    from lxml import etree as _et  # ty: ignore[unresolved-import] - lxml ships no stubs

    out, _ = _gen(tmp_path, "netconf", KEYS_YANG, "tkeys")
    base = _import(out, "data_navigators._base")
    models = _import(out, "data_models.tkeys")
    nav = _import(out, "data_navigators")

    xml = (
        '<root xmlns="urn:test:tkeys"><items>'
        "<id>a</id><cfg>C</cfg><state>S</state>"
        "</items></root>"
    )
    root = _et.fromstring(xml)
    base.prune_xml_to_content(root, models.Root, "nonconfig")
    items = root[0]
    assert _depth_filter_tags(items) == {"id", "state"}, (
        f"keys must survive a nonconfig read: {_et.tostring(items)}"
    )

    root = _et.fromstring(xml)
    base.prune_xml_to_content(root, models.Root, "config")
    items = root[0]
    assert _depth_filter_tags(items) == {"id", "cfg"}

    # End-to-end through the navigator: a legacy <get> reply is unfiltered,
    # and the navigator's prune is what makes content= mean the same thing.
    reply = _et.fromstring(
        '<data xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">' + xml + "</data>"
    )
    client, _et2 = _netconf_depth_client(replies=[reply])
    data = nav.Data(client, [])
    model = data.tkeys_root.retrieve(content="nonconfig", depth="unbounded")
    assert model is not None
    assert model.items[0].id == "a"
    assert model.items[0].state == "S"
    assert model.items[0].cfg is None


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


def test_depth_filter_and_pruner_keep_augmenting_namespaces(tmp_path):
    """RFC 7950 Sec 7.17: augment adds nodes to the target tree, but each
    node stays in the AUGMENTING module's namespace. The depth filter and the
    pruner must use `json_schema_extra["ns"]`, not the parent's namespace, or
    the device answers correctly and the client rejects it.
    """
    from lxml import etree  # ty: ignore[unresolved-import] - lxml ships no stubs

    out, _ = _gen_roots(
        tmp_path,
        "netconf",
        {"taug": AUGMENT_BASE_YANG, "taugext": AUGMENT_EXT_YANG},
        ["taug", "taugext"],
        "augm",
    )
    base = _import(out, "data_navigators._base")
    models = _import(out, "data_models.taug")

    path = [("urn:test:taug", "system", {})]
    f2 = base.build_depth_filter(path, models.System, 2)
    assert f2 is None, (
        "system has only boundary containers, so the filter must collapse"
    )
    f3 = base.build_depth_filter(path, models.System, 3)
    assert f3 is not None
    aaa = next(c for c in f3 if etree.QName(c).localname == "aaa")
    assert etree.QName(aaa).namespace == "urn:test:taugext"
    mode = next(c for c in aaa if etree.QName(c).localname == "mode")
    assert etree.QName(mode).namespace == "urn:test:taugext"

    xml = (
        '<system xmlns="urn:test:taug">'
        "<name><host-name>h</host-name></name>"
        '<aaa xmlns="urn:test:taugext"><mode>strict</mode></aaa>'
        "</system>"
    )
    root2 = etree.fromstring(xml)
    base.prune_xml_tree_to_depth(root2, models.System, 2)
    assert _depth_filter_tags(root2) == set(), "both children are boundary containers"
    root3 = etree.fromstring(xml)
    base.prune_xml_tree_to_depth(root3, models.System, 3)
    aaa3 = next(c for c in root3 if etree.QName(c).localname == "aaa")
    assert etree.QName(aaa3).namespace == "urn:test:taugext"
    assert _depth_filter_tags(aaa3) == {"mode"}


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


MODULE_NAME_COLLISION_YANG = """module {name} {{
  prefix m;
  namespace "urn:test:{name}";
  revision 2026-01-01;
  container root {{
    leaf value {{ type string; }}
  }}
  rpc do-it {{
    input {{ leaf value {{ type string; }} }}
    output {{ leaf ok {{ type boolean; }} }}
  }}
}}"""


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


@pytest.mark.parametrize("fmt", ["restconf", "netconf"])
@pytest.mark.parametrize(
    "module_name",
    [
        "license",  # builtin: resolves to the builtin _Printer at runtime
        "element",  # NETCONF template global: from pydantic_xml import element
        "Field",  # RESTCONF template global: from pydantic import Field
    ],
)
def test_module_names_colliding_with_runtime_names_still_import(
    tmp_path, fmt, module_name
):
    """A YANG module stem must never be used bare in an aggregate annotation.

    Regression: `license@2015-08-25.yang` produced

        license_licenses: 'license.Licenses | None' = element(...)

    in `data_models/__init__.py`. Pydantic evaluates that annotation at class
    creation, and `license` resolves to the builtin `_Printer`; attribute access
    raises `AttributeError`, which pydantic does not catch (it only defers
    `NameError`). The generated client could not be imported at all. `element`
    (NETCONF) and `Field` (RESTCONF) fail the same way because both are bound
    in the aggregate module's globals.

    The alias is an emitter detail: the navigator surface and the per-module
    files must keep their raw names.
    """
    out, compiled = _gen(
        tmp_path,
        fmt,
        MODULE_NAME_COLLISION_YANG.format(name=module_name),
        module_name,
    )
    assert compiled, f"{fmt}/{module_name}: generated code failed to compile"

    # 1. The aggregate package imports, and both the data and rpc fields exist.
    models = _import(out, "data_models")
    assert f"{module_name}_root" in models.Data.model_fields, sorted(
        models.Data.model_fields
    )
    assert f"{module_name}_do_it" in models.Operations.model_fields, sorted(
        models.Operations.model_fields
    )
    assert not any(name.startswith("_m_") for name in models.Data.model_fields), sorted(
        models.Data.model_fields
    )

    # 2. The per-module models still import under their raw module name.
    module_models = _import(out, f"data_models.{module_name}")
    assert hasattr(module_models, "Root")
    assert hasattr(module_models, "DoIt")

    # 3. The public navigator surface is unchanged: raw property names, no
    #    alias leak, raw per-module navigator file.
    navigators = _import(out, "data_navigators")
    assert hasattr(navigators.Data, f"{module_name}_root")
    assert hasattr(navigators.Operations, f"{module_name}_do_it")
    assert not any(name.startswith("_m_") for name in dir(navigators.Data)), [
        name for name in dir(navigators.Data) if name.startswith("_m_")
    ]
    assert _pkg_file(out, "data_models", f"{module_name}.py").is_file()
    assert _pkg_file(out, "data_navigators", f"{module_name}.py").is_file()
    assert not _pkg_file(out, "data_models", f"_m_{module_name}.py").exists()
    assert not _pkg_file(out, "data_navigators", f"_m_{module_name}.py").exists()


@pytest.mark.parametrize(
    ("binding", "annotation", "stem"),
    [
        (
            "from pydantic import BaseModel\n",
            "'license.Thing | None'",
            "license",
        ),
        (
            "from pydantic import BaseModel\nfrom pydantic_xml import element\n",
            "'element.Thing | None'",
            "element",
        ),
        (
            "from pydantic import BaseModel, Field\n",
            "'Field.Thing | None'",
            "Field",
        ),
    ],
)
def test_aggregate_annotation_guard_rejects_runtime_name(
    tmp_path, binding, annotation, stem
):
    """The offline guard must reject the shape that parses but cannot import."""
    from yang2sdk.plugin.src.core import _aggregate_annotation_errors

    path = tmp_path / "data_models" / "__init__.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        "from __future__ import annotations\n"
        "from typing import TYPE_CHECKING\n"
        f"{binding}"
        "if TYPE_CHECKING:\n"
        "    from . import license\n"
        "class Data(BaseModel):\n"
        f"    x: {annotation} = None\n"
    )
    errors = _aggregate_annotation_errors(path)
    assert any(stem in error for error in errors), errors


def test_aggregate_annotation_guard_accepts_aliases_and_builtin_types(tmp_path):
    """The guard must not reject a safe alias or an ordinary builtin type."""
    from yang2sdk.plugin.src.core import _aggregate_annotation_errors

    path = tmp_path / "data_models" / "__init__.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        "from __future__ import annotations\n"
        "from typing import TYPE_CHECKING, Any\n"
        "from pydantic import BaseModel, Field\n"
        "if TYPE_CHECKING:\n"
        "    from . import license as _m_license\n"
        "class Data(BaseModel):\n"
        "    x: _m_license.Thing | None = Field(None, alias='license:x')\n"
        "    y: dict[str, Any] = {}\n"
    )
    assert _aggregate_annotation_errors(path) == []


def test_aggregate_annotation_guard_allows_a_runtime_module_import(tmp_path):
    """A runtime module import shadows a builtin, so the bare stem is safe."""
    from yang2sdk.plugin.src.core import _aggregate_annotation_errors

    path = tmp_path / "data_models" / "__init__.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        "from __future__ import annotations\n"
        "from . import license\n"
        "class Data:\n"
        "    x: license.Thing | None = None\n"
    )
    assert _aggregate_annotation_errors(path) == []


def test_module_annotation_aliases_are_unique_and_never_a_module_stem():
    """The alias must not collide with a real module named like an alias."""
    from types import SimpleNamespace

    from yang2sdk.plugin.src.core import _module_annotation_aliases

    modules = [
        SimpleNamespace(py_name="license"),
        SimpleNamespace(py_name="_m_license"),
        SimpleNamespace(py_name="_m_license_"),
    ]
    aliases = _module_annotation_aliases(modules)
    assert len(set(aliases.values())) == len(modules)
    assert not set(aliases.values()) & {m.py_name for m in modules}
    assert all(alias.startswith("_m_") for alias in aliases.values())


def test_validate_generated_rejects_a_bare_aggregate_module_stem(tmp_path):
    """`_validate_generated` must run the annotation guard, not just compile."""
    from yang2sdk.plugin.src.core import _validate_generated

    out = tmp_path / "out"
    models = out / "data_models"
    models.mkdir(parents=True)
    (models / "__init__.py").write_text(
        "from __future__ import annotations\n"
        "from typing import TYPE_CHECKING\n"
        "from pydantic import BaseModel\n"
        "if TYPE_CHECKING:\n"
        "    from . import license\n"
        "class Data(BaseModel):\n"
        "    x: 'license.Thing | None' = None\n"
    )
    with pytest.raises(SyntaxError, match="license"):
        _validate_generated(str(out))


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
    base_src = _pkg_file(out, "data_models", "_base.py").read_text()
    assert base_src.count("def from_xml(cls") == 1, base_src.count("def from_xml(cls")
    assert base_src.count("def from_xml_tree(cls") == 1, base_src.count(
        "def from_xml_tree(cls"
    )


# --- a read builds only the models its payload reaches ----------------------

LAZY_YANG = """module tlazy {
  prefix tl;
  namespace "urn:test:tlazy";
  revision 2026-01-01;

  container top {
    container mid {
      container deep {
        container deeper {
          leaf value { type string; }
        }
      }
      leaf name { type string; }
    }
    container sibling {
      leaf value { type string; }
    }
  }

  rpc ping {
    input {
      container target {
        leaf address { type string; }
      }
    }
  }
}
"""

LAZY_SHALLOW = b'<top xmlns="urn:test:tlazy"><mid><name>a</name></mid></top>'
LAZY_DEEP = (
    b'<top xmlns="urn:test:tlazy"><mid><deep><deeper><value>7</value>'
    b"</deeper></deep><name>a</name></mid></top>"
)
LAZY_RPC = (
    b'<ping xmlns="urn:test:tlazy"><input><target><address>1.1.1.1</address>'
    b"</target></input></ping>"
)


def _annotated_model(annotation: Any) -> Any:
    """The single model class an Optional[X] field annotation binds to."""
    for arg in get_args(annotation):
        if isinstance(arg, type) and hasattr(arg, "model_fields"):
            return arg
    raise AssertionError(f"no model class in annotation {annotation!r}")


def test_from_xml_builds_only_the_models_the_payload_instantiates(tmp_path):
    """A read must not pay for the whole model graph.

    pydantic-xml asserts that a field's model has a serializer before it walks
    a parent's fields (`serializers/factories/model.py:450`), even when the
    element is absent, so the target model's direct children are mandatory.
    Anything deeper is only reachable through a present element, so it must
    stay unbuilt.

    Regression: `from_xml` called `rebuild_model_and_dependencies` first, which
    builds every reachable class. Measured on the G30 tree that is 1355 model
    classes / ~30k fields / 34s cold -- while the reply holding two leaves
    arrived from the device in 0.1s.
    """
    out, _ = _gen(tmp_path, "netconf", LAZY_YANG, "lazy")
    models = _import(out, "data_models.tlazy")

    top = models.Top
    mid = _annotated_model(top.model_fields["mid"].annotation)
    deep = _annotated_model(mid.model_fields["deep"].annotation)
    deeper = _annotated_model(deep.model_fields["deeper"].annotation)
    sibling = _annotated_model(top.model_fields["sibling"].annotation)

    # `defer_build=True` keeps import cheap; nothing is built yet.
    assert top.__xml_serializer__ is None
    assert deeper.__xml_serializer__ is None

    parsed = top.from_xml(LAZY_SHALLOW)
    assert parsed.mid.name == "a"

    assert top.__xml_serializer__ is not None
    # Direct children are required by pydantic-xml even when absent: `sibling`
    # is not in the payload, `deep` is a child of the present `mid`.
    assert sibling.__xml_serializer__ is not None
    assert deep.__xml_serializer__ is not None
    # But `deeper` sits below an absent element, so nothing can reach it.
    assert deeper.__xml_serializer__ is None, (
        "a model absent from the payload was built; the read path is walking "
        "the whole graph again"
    )

    # A payload that does carry it builds it on demand and parses it.
    parsed = top.from_xml(LAZY_DEEP)
    assert parsed.mid.deep.deeper.value == "7"
    assert deeper.__xml_serializer__ is not None

    # An RPC envelope field is emitted as `element(tag="input")` with no
    # `json_schema_extra`, so the payload walk cannot key off it. Missing that
    # case left the input model unbuilt and every RPC reply failed with
    # "model ... is partially initialized".
    ping = models.Ping
    ping_input = _annotated_model(ping.model_fields["input"].annotation)
    target = _annotated_model(ping_input.model_fields["target"].annotation)
    assert ping_input.__xml_serializer__ is None
    parsed = ping.from_xml(LAZY_RPC)
    assert parsed.input.target.address == "1.1.1.1"
    assert ping_input.__xml_serializer__ is not None
    assert target.__xml_serializer__ is not None

    # Pruning is a pure metadata walk: it must never trigger a schema build.
    nav_base = _pkg_file(out, "data_navigators", "_base.py").read_text()
    assert "rebuild_model_and_dependencies(" not in nav_base


def test_payload_driven_build_matches_the_whole_graph_build(tmp_path):
    """The lazy build must be an optimisation, not a behaviour change."""
    eager_out, _ = _gen(tmp_path / "eager", "netconf", LAZY_YANG, "lazyeager")
    eager_models = _import(eager_out, "data_models.tlazy")
    eager_base = _import(eager_out, "data_models._base")
    eager_base.rebuild_model_and_dependencies(eager_models.Top)
    expected = eager_models.Top.from_xml(LAZY_DEEP).model_dump()

    lazy_out, _ = _gen(tmp_path / "lazy", "netconf", LAZY_YANG, "lazylazy")
    lazy_models = _import(lazy_out, "data_models.tlazy")
    assert lazy_models.Top.__xml_serializer__ is None
    assert lazy_models.Top.from_xml(LAZY_DEEP).model_dump() == expected


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
        nav_file = _pkg_file(out, "data_navigators", "tkw.py")
        if not nav_file.exists():
            nav_file = _pkg_file(out, "data_navigators", "navigators.py")
        src = nav_file.read_text()
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
        model_file = _pkg_file(out, "data_models", "tkw.py")
        if not model_file.exists():
            model_file = _pkg_file(out, "data_models", "models.py")
        models = model_file.read_text()
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
        "DEVICE_SSH_KEY",
        "DEVICE_SSH_KEY_PASSPHRASE",
        "DEVICE_USERNAME",
        "DEVICE_PASSWORD",
    ):
        monkeypatch.delenv(var, raising=False)

    rout, _ = _gen(tmp_path / "rc", "restconf", REPLACE_YANG, "tcred")
    rest = _import(rout, "session_manager", alias="_tcred_restconf")
    with pytest.raises(ValueError, match="credentials"):
        rest.RestconfClient(management_ip="127.0.0.1", port=8181)

    nout, _ = _gen(tmp_path / "nc", "netconf", REPLACE_YANG, "tcred")
    netc = _import(nout, "session_manager", alias="_tcred_netconf")
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

    for var in (
        "DEVICE_USER",
        "DEVICE_PASS",
        "DEVICE_SSH_KEY",
        "DEVICE_SSH_KEY_PASSPHRASE",
        "DEVICE_USERNAME",
        "DEVICE_PASSWORD",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DEVICE_USERNAME", "retired")
    monkeypatch.setenv("DEVICE_PASSWORD", "retiredpass")

    rout, _ = _gen(tmp_path / "rc", "restconf", REPLACE_YANG, "tlong")
    rest = _import(rout, "session_manager", alias="_tlong_restconf")
    with pytest.raises(ValueError, match="credentials"):
        rest.RestconfClient(management_ip="127.0.0.1", port=8181)

    nout, _ = _gen(tmp_path / "nc", "netconf", REPLACE_YANG, "tlong")
    netc = _import(nout, "session_manager", alias="_tlong_netconf")
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


def _key_auth_env(monkeypatch):
    """Clear every credential name so key-auth tests cannot read a local .env."""
    for var in (
        "DEVICE_USER",
        "DEVICE_PASS",
        "DEVICE_SSH_KEY",
        "DEVICE_SSH_KEY_PASSPHRASE",
        "DEVICE_USERNAME",
        "DEVICE_PASSWORD",
    ):
        monkeypatch.delenv(var, raising=False)


def test_netconf_ssh_key_authenticates_without_a_password(tmp_path, monkeypatch):
    """A key is a credential: no password is required when one is named.

    This is the half of the credential contract SSH key auth relaxes. The
    username stays mandatory, and the key path reaches ncclient as
    `key_filename` with no password value. Agent and ~/.ssh discovery are
    disabled with an explicit key, so only that key can authenticate.
    """
    from unittest import mock

    _key_auth_env(monkeypatch)
    monkeypatch.setenv("DEVICE_USER", "keyuser")
    key = tmp_path / "id_ed25519"
    key.write_text("dummy key material; not parsed by the client")

    out, _ = _gen(tmp_path, "netconf", REPLACE_YANG, "tkey")
    sm = _import_fresh(out, "session_manager")
    with mock.patch.object(
        sm.manager, "connect", return_value=_FakeNcclientManager(_BASE_CAPS)
    ) as connect:
        client = sm.NetconfClient(management_ip="127.0.0.1", key_filename=str(key))

    kwargs = connect.call_args.kwargs
    assert kwargs["key_filename"] == [str(key)]
    assert kwargs["password"] is None
    assert kwargs["allow_agent"] is False, (
        "an explicit key must not fall through to the SSH agent"
    )
    assert kwargs["look_for_keys"] is False, (
        "an explicit key must not fall through to ~/.ssh/id_*"
    )
    assert client.key_filenames == [str(key)]
    assert client.username == "keyuser"


def test_password_auth_keeps_ncclient_agent_and_default_key_discovery(
    tmp_path, monkeypatch
):
    """The fallback gate is scoped to an explicit key, not applied globally.

    ncclient's agent/~/.ssh discovery is the documented behaviour when no key
    path is supplied; disabling it for password auth would break callers who
    rely on it. Callers may still opt out explicitly.
    """
    from unittest import mock

    _key_auth_env(monkeypatch)
    out, _ = _gen(tmp_path, "netconf", REPLACE_YANG, "tkeydefaults")
    sm = _import_fresh(out, "session_manager")
    with mock.patch.object(
        sm.manager, "connect", return_value=_FakeNcclientManager(_BASE_CAPS)
    ) as connect:
        sm.NetconfClient(
            management_ip="127.0.0.1", username="u", password="p", verify=False
        )
    kwargs = connect.call_args.kwargs
    assert kwargs["allow_agent"] is True
    assert kwargs["look_for_keys"] is True

    with mock.patch.object(
        sm.manager, "connect", return_value=_FakeNcclientManager(_BASE_CAPS)
    ) as connect:
        sm.NetconfClient(
            management_ip="127.0.0.1",
            username="u",
            password="p",
            verify=False,
            allow_agent=False,
            look_for_keys=False,
        )
    kwargs = connect.call_args.kwargs
    assert kwargs["allow_agent"] is False
    assert kwargs["look_for_keys"] is False


def test_netconf_username_must_match_the_xml_char_production(tmp_path, monkeypatch):
    """RFC 6242 Sec 3 + RFC 6241 Sec 2.2: the NETCONF username is XML text.

    The SSH layer forwards the name unchanged, so a name containing a code
    point outside XML's ``Char`` production cannot be represented. Reject it
    before the transport instead of relying on the server to drop the session.
    """
    from unittest import mock

    _key_auth_env(monkeypatch)
    out, _ = _gen(tmp_path, "netconf", REPLACE_YANG, "tusername")
    sm = _import_fresh(out, "session_manager")

    for bad in ("bad\x00user", "bad\x01user"):
        with pytest.raises(ValueError, match="XML Char"):
            sm.NetconfClient(management_ip="127.0.0.1", username=bad, password="p")

    # Valid XML characters, including non-ASCII, must not be over-rejected.
    with mock.patch.object(
        sm.manager, "connect", return_value=_FakeNcclientManager(_BASE_CAPS)
    ):
        sm.NetconfClient(management_ip="127.0.0.1", username="utente-è", password="p")


def test_explicit_password_wins_over_environment_key(tmp_path, monkeypatch):
    """An explicit password must not be overridden by DEVICE_SSH_KEY.

    Resolving fields independently made ``password=...`` silently become
    key-only auth whenever the host exported DEVICE_SSH_KEY. The source rule is
    the contract: an explicit password suppresses both environment key names.
    """
    import argparse
    from unittest import mock

    from yang2sdk.cli.sdk_verify import _resolve_credentials

    _key_auth_env(monkeypatch)
    key = tmp_path / "id_ed25519"
    key.write_text("dummy key material")
    monkeypatch.setenv("DEVICE_USER", "envuser")
    monkeypatch.setenv("DEVICE_SSH_KEY", str(key))
    monkeypatch.setenv("DEVICE_SSH_KEY_PASSPHRASE", "env-key-pass")

    out, _ = _gen(tmp_path, "netconf", REPLACE_YANG, "tkeyexplicitpw")
    sm = _import_fresh(out, "session_manager")
    with mock.patch.object(
        sm.manager, "connect", return_value=_FakeNcclientManager(_BASE_CAPS)
    ) as connect:
        client = sm.NetconfClient(
            management_ip="127.0.0.1", password="explicit-password"
        )

    kwargs = connect.call_args.kwargs
    assert kwargs["password"] == "explicit-password"
    assert kwargs["key_filename"] is None
    assert kwargs["allow_agent"] is True
    assert kwargs["look_for_keys"] is True
    assert client.password == "explicit-password"
    assert client.key_filenames is None
    assert client.passphrase is None

    args = argparse.Namespace(
        username="", password="explicit-password", ssh_key="", ssh_key_passphrase=""
    )
    assert _resolve_credentials(args, "netconf") == (
        "envuser",
        "explicit-password",
        None,
        None,
    )


def test_explicit_key_ignores_environment_password_and_passphrase(
    tmp_path, monkeypatch
):
    """An explicit key owns its source: no DEVICE_PASS, no env passphrase.

    Inheriting DEVICE_SSH_KEY_PASSPHRASE here would make a valid unencrypted
    key unusable because of unrelated environment state. It belongs only to a
    key that itself came from DEVICE_SSH_KEY.
    """
    import argparse
    from unittest import mock

    from yang2sdk.cli.sdk_verify import _resolve_credentials

    _key_auth_env(monkeypatch)
    key = tmp_path / "id_ed25519"
    key.write_text("dummy key material")
    monkeypatch.setenv("DEVICE_USER", "keyuser")
    monkeypatch.setenv("DEVICE_PASS", "env-account-password")
    monkeypatch.setenv("DEVICE_SSH_KEY_PASSPHRASE", "env-key-pass")

    out, _ = _gen(tmp_path, "netconf", REPLACE_YANG, "tkeyexplicitkey")
    sm = _import_fresh(out, "session_manager")
    with mock.patch.object(
        sm.manager, "connect", return_value=_FakeNcclientManager(_BASE_CAPS)
    ) as connect:
        client = sm.NetconfClient(
            management_ip="127.0.0.1",
            username="keyuser",
            key_filename=str(key),
        )

    kwargs = connect.call_args.kwargs
    assert kwargs["key_filename"] == [str(key)]
    assert kwargs["password"] is None
    assert client.password is None
    assert client.passphrase is None

    args = argparse.Namespace(
        username="keyuser", password="", ssh_key=str(key), ssh_key_passphrase=""
    )
    assert _resolve_credentials(args, "netconf") == (
        "keyuser",
        None,
        str(key),
        None,
    )


def test_explicit_password_and_key_ambiguity_is_not_silent(
    tmp_path, monkeypatch, capsys
):
    """Both explicit credentials are ambiguous; neither is silently discarded.

    The generated client and downloader reject the pair. sdk-verify warns and
    lets the key win, because ``--protocol both`` legitimately uses a password
    for RESTCONF and a key for NETCONF in one invocation.
    """
    import argparse

    from yang2sdk.cli.downloader import YangDownloader
    from yang2sdk.cli.sdk_verify import _resolve_credentials

    _key_auth_env(monkeypatch)
    key = tmp_path / "id_ed25519"
    key.write_text("dummy key material")

    out, _ = _gen(tmp_path, "netconf", REPLACE_YANG, "tkeyboth")
    sm = _import_fresh(out, "session_manager")
    with pytest.raises(ValueError, match="alternatives"):
        sm.NetconfClient(
            management_ip="127.0.0.1",
            username="u",
            password="account-password",
            key_filename=str(key),
        )

    with pytest.raises(ValueError, match="alternatives"):
        YangDownloader(
            host="127.0.0.1",
            port=830,
            user="u",
            password="account-password",
            output_dir=tmp_path,
            key_filename=str(key),
        )

    args = argparse.Namespace(
        username="u",
        password="account-password",
        ssh_key=str(key),
        ssh_key_passphrase="",
    )
    assert _resolve_credentials(args, "netconf") == (
        "u",
        None,
        str(key),
        None,
    )
    assert "both --password and --ssh-key were given" in capsys.readouterr().err


def test_environment_password_and_key_warns_and_key_wins(
    tmp_path, monkeypatch, caplog, capsys
):
    """With no explicit argument, env ambiguity is resolved loudly.

    DEVICE_SSH_KEY wins because a host can pair a password for RESTCONF with a
    key for NETCONF; passing an explicit password still forces password auth.
    """
    import argparse
    import logging
    from unittest import mock

    from yang2sdk.cli.sdk_verify import _resolve_credentials

    _key_auth_env(monkeypatch)
    key = tmp_path / "id_ed25519"
    key.write_text("dummy key material")
    monkeypatch.setenv("DEVICE_USER", "envuser")
    monkeypatch.setenv("DEVICE_PASS", "env-password")
    monkeypatch.setenv("DEVICE_SSH_KEY", str(key))
    monkeypatch.setenv("DEVICE_SSH_KEY_PASSPHRASE", "env-key-pass")

    caplog.set_level(logging.WARNING)
    out, _ = _gen(tmp_path, "netconf", REPLACE_YANG, "tkeyenvboth")
    sm = _import_fresh(out, "session_manager")
    with mock.patch.object(
        sm.manager, "connect", return_value=_FakeNcclientManager(_BASE_CAPS)
    ) as connect:
        client = sm.NetconfClient(management_ip="127.0.0.1")

    kwargs = connect.call_args.kwargs
    assert kwargs["key_filename"] == [str(key)]
    assert kwargs["password"] == "env-key-pass"
    assert client.password is None
    assert "Both DEVICE_PASS and DEVICE_SSH_KEY are set" in caplog.text

    args = argparse.Namespace(
        username="", password="", ssh_key="", ssh_key_passphrase=""
    )
    assert _resolve_credentials(args, "netconf") == (
        "envuser",
        None,
        str(key),
        "env-key-pass",
    )
    assert "both DEVICE_PASS and DEVICE_SSH_KEY are set" in capsys.readouterr().err


def test_netconf_ssh_certificate_and_passphrase_reach_ncclient(
    tmp_path, monkeypatch, caplog
):
    """An OpenSSH certificate is a first-class key_filename, and a key
    passphrase occupies ncclient's single password slot.

    paramiko resolves `<key>-cert.pub` to the adjacent private key and attaches
    the certificate, so either path is valid. ncclient has no separate
    passphrase parameter (`SSHSession._auth` uses `password` to unlock keys),
    which is why asserting the transport kwarg -- not just the attribute -- is
    the only way to know an encrypted key will actually open.
    """
    import logging
    from unittest import mock

    _key_auth_env(monkeypatch)
    key = tmp_path / "id_ed25519"
    cert = tmp_path / "id_ed25519-cert.pub"
    key.write_text("dummy key material")
    cert.write_text("ssh-ed25519-cert-v01@openssh.com AAAA dummy cert")

    caplog.set_level(logging.WARNING)
    out, _ = _gen(tmp_path, "netconf", REPLACE_YANG, "tcert")
    sm = _import_fresh(out, "session_manager")
    with mock.patch.object(
        sm.manager, "connect", return_value=_FakeNcclientManager(_BASE_CAPS)
    ) as connect:
        client = sm.NetconfClient(
            management_ip="127.0.0.1",
            username="u",
            key_filename=str(cert),
            passphrase="key-secret",
            verify=False,
        )

    kwargs = connect.call_args.kwargs
    assert kwargs["key_filename"] == [str(cert)]
    assert kwargs["password"] == "key-secret", (
        "encrypted keys are unlocked with ncclient's password parameter"
    )
    assert kwargs["allow_agent"] is False
    assert kwargs["look_for_keys"] is False
    assert client.password is None, "the key passphrase is not an account password"
    assert client.passphrase == "key-secret"
    assert "try it as a password" in caplog.text, (
        "encrypted-key auth must warn about ncclient's password fallback"
    )


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen is required")
def test_paramiko_loads_real_key_encrypted_key_and_openssh_certificate(tmp_path):
    """Exercise the real key/certificate loading path, not just kwargs.

    The generated client delegates the actual parsing to
    ``paramiko.PKey.from_path``. This creates real key material with
    ssh-keygen, including a CA-signed OpenSSH certificate, and pins the
    behaviours the client relies on: an unencrypted key has no certificate,
    an encrypted key opens only with its passphrase, and the certificate works
    from either member of the ``<key>`` / ``<key>-cert.pub`` pair.
    """
    import paramiko

    def keygen(*args: str) -> None:
        subprocess.run(
            ["ssh-keygen", *args],
            check=True,
            capture_output=True,
            text=True,
        )

    plain = tmp_path / "id_plain"
    keygen("-q", "-t", "ed25519", "-N", "", "-f", str(plain))
    encrypted = tmp_path / "id_encrypted"
    keygen("-q", "-t", "ed25519", "-N", "secret", "-f", str(encrypted))
    ca = tmp_path / "id_ca"
    keygen("-q", "-t", "ed25519", "-N", "", "-f", str(ca))
    signed = tmp_path / "id_signed"
    keygen("-q", "-t", "ed25519", "-N", "", "-f", str(signed))
    keygen("-q", "-s", str(ca), "-I", "test", "-n", "user", f"{signed}.pub")

    assert paramiko.PKey.from_path(str(plain)).public_blob is None
    # paramiko's annotation says str, but cryptography requires bytes here.
    encrypted_key = paramiko.PKey.from_path(str(encrypted), cast(str, b"secret"))
    assert encrypted_key.get_name() == "ssh-ed25519"

    cert = tmp_path / "id_signed-cert.pub"
    assert cert.exists()
    for loaded in (
        paramiko.PKey.from_path(str(signed)),
        paramiko.PKey.from_path(str(cert)),
    ):
        assert loaded.get_name() == "ssh-ed25519"
        assert loaded.public_blob is not None
        assert loaded.public_blob.key_type == "ssh-ed25519-cert-v01@openssh.com"

    with pytest.raises(
        TypeError, match="Password was given but private key is not encrypted"
    ):
        paramiko.PKey.from_path(str(plain), cast(str, b"secret"))
    with pytest.raises(TypeError, match="password-protected"):
        paramiko.PKey.from_path(str(encrypted))


def test_ncclient_tries_the_key_passphrase_as_a_password_after_key_failure(
    monkeypatch,
):
    """Pin the dependency behaviour the documentation must not overclaim.

    ncclient's ``SSHSession._auth`` takes one password slot. With a key it is
    the passphrase; if public-key auth fails, ncclient falls back to
    ``auth_password`` using that slot. This test is deliberately about
    ncclient, not yang2sdk: if the dependency changes, the security wording in
    AGENTS.md / SECURITY.md / docs/usage.md must change with it.
    """
    import logging

    from ncclient.transport import ssh

    class _Transport:
        def __init__(self) -> None:
            self.password_calls: list[tuple[str, str]] = []

        def auth_publickey(self, *_args: object) -> None:
            raise AssertionError("key parsing is expected to fail first")

        def auth_password(self, username: str, password: str) -> None:
            self.password_calls.append((username, password))

    def _reject(*_args: object, **_kwargs: object) -> None:
        raise ValueError("key rejected")

    monkeypatch.setattr(ssh.paramiko.PKey, "from_path", _reject)
    transport = _Transport()
    session: Any = object.__new__(ssh.SSHSession)
    session.logger = logging.getLogger("test-ncclient-auth")
    session._transport = transport

    session._auth("u", "key-passphrase", ["/tmp/not-a-real-key"], False, False)

    assert transport.password_calls == [("u", "key-passphrase")]


def test_netconf_ssh_key_environment_fallback(tmp_path, monkeypatch):
    """DEVICE_SSH_KEY / DEVICE_SSH_KEY_PASSPHRASE work like the password pair."""
    from unittest import mock

    _key_auth_env(monkeypatch)
    # Keep `~` expansion without touching the real home directory.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DEVICE_USER", "envkeyuser")
    monkeypatch.setenv("DEVICE_SSH_KEY", "~/keys/id_ed25519")
    monkeypatch.setenv("DEVICE_SSH_KEY_PASSPHRASE", "env-key-pass")
    expected = os.path.expanduser("~/keys/id_ed25519")
    Path(expected).parent.mkdir(parents=True, exist_ok=True)
    Path(expected).write_text("dummy key material")

    out, _ = _gen(tmp_path, "netconf", REPLACE_YANG, "tkeyenv")
    sm = _import_fresh(out, "session_manager")
    with mock.patch.object(
        sm.manager, "connect", return_value=_FakeNcclientManager(_BASE_CAPS)
    ) as connect:
        client = sm.NetconfClient(management_ip="127.0.0.1")

    kwargs = connect.call_args.kwargs
    assert client.username == "envkeyuser"
    assert kwargs["key_filename"] == [expected]
    assert kwargs["password"] == "env-key-pass"


def test_netconf_missing_key_path_fails_closed(tmp_path, monkeypatch):
    """A typo must fail before the transport, not fall back to another identity.

    ncclient silently falls through to the agent/~/.ssh when an explicit key
    cannot be loaded, so a nonexistent path could otherwise authenticate as
    whoever the host happens to have keys for.
    """
    _key_auth_env(monkeypatch)
    out, _ = _gen(tmp_path, "netconf", REPLACE_YANG, "tbadkey")
    sm = _import_fresh(out, "session_manager")
    missing = tmp_path / "no-such-key"
    with pytest.raises(ValueError, match="not found"):
        sm.NetconfClient(
            management_ip="127.0.0.1",
            username="u",
            key_filename=str(missing),
        )

    # A certificate path is not a credential by itself: paramiko needs the
    # adjacent private key, and explicit paths never fall back to the agent.
    cert = tmp_path / "id_ed25519-cert.pub"
    cert.write_text("ssh-ed25519-cert-v01@openssh.com AAAA dummy cert")
    with pytest.raises(ValueError, match="adjacent private key"):
        sm.NetconfClient(
            management_ip="127.0.0.1",
            username="u",
            key_filename=str(cert),
        )

    # A stray passphrase must not silently replace the account password in
    # ncclient's shared transport slot.
    with pytest.raises(ValueError, match="without key_filename"):
        sm.NetconfClient(
            management_ip="127.0.0.1",
            username="u",
            password="p",
            passphrase="stray",
        )


def test_downloader_and_sdk_verify_accept_key_credentials(tmp_path, monkeypatch):
    """The two CLI consumers resolve the same key names as a generated client.

    `yang-downloader` reads DEVICE_SSH_KEY / DEVICE_SSH_KEY_PASSPHRASE, and
    sdk-verify accepts --ssh-key (or the environment) for NETCONF while still
    refusing a RESTCONF run with no password. Pinned here because the
    generated client is not the only place the credential contract is stated.
    """
    import argparse

    from yang2sdk.cli import downloader as downloader_mod
    from yang2sdk.cli.downloader import main as downloader_main
    from yang2sdk.cli.sdk_verify import _resolve_credentials

    _key_auth_env(monkeypatch)
    key = tmp_path / "id_ed25519"
    key.write_text("dummy key material")
    seen: dict[str, object] = {}
    RealYangDownloader = downloader_mod.YangDownloader

    # Explicit credentials are alternatives. With a key the transport slot is
    # its passphrase or empty, never the account password; without a key it is
    # the password.
    with pytest.raises(ValueError, match="alternatives"):
        RealYangDownloader(
            host="127.0.0.1",
            port=830,
            user="u",
            password="p",
            output_dir=tmp_path,
            key_filename=str(key),
        )
    assert (
        RealYangDownloader(
            host="127.0.0.1",
            port=830,
            user="u",
            output_dir=tmp_path,
            key_filename=str(key),
        ).transport_password
        is None
    )
    assert (
        RealYangDownloader(
            host="127.0.0.1",
            port=830,
            user="u",
            password="p",
            output_dir=tmp_path,
        ).transport_password
        == "p"
    )

    class _FakeDownloader:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def download_all(self):
            return 0

    monkeypatch.setattr(downloader_mod, "YangDownloader", _FakeDownloader)
    monkeypatch.setattr(downloader_mod, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("DEVICE_IP", "127.0.0.1")
    monkeypatch.setenv("NETCONF_PORT", "830")
    monkeypatch.setenv("DEVICE_NAME", "fake")
    monkeypatch.setenv("DEVICE_USER", "envuser")
    monkeypatch.setenv("DEVICE_SSH_KEY", str(key))
    monkeypatch.setenv("DEVICE_SSH_KEY_PASSPHRASE", "env-key-pass")
    downloader_main()
    assert seen["user"] == "envuser"
    assert seen["key_filename"] == str(key)
    assert seen["passphrase"] == "env-key-pass"
    assert seen["password"] is None

    args = argparse.Namespace(
        username="",
        password="",
        ssh_key=str(key),
        ssh_key_passphrase="env-key-pass",
    )
    user, password, key_filename, passphrase = _resolve_credentials(args, "netconf")
    assert (user, password, key_filename, passphrase) == (
        "envuser",
        None,
        str(key),
        "env-key-pass",
    )
    with pytest.raises(SystemExit, match="RESTCONF needs"):
        _resolve_credentials(args, "restconf")


def test_downloader_environment_password_and_key_warns_and_uses_key(
    tmp_path, monkeypatch, caplog
):
    """The downloader mirrors the client's environment-ambiguity rule."""
    import logging

    from yang2sdk.cli import downloader as downloader_mod

    _key_auth_env(monkeypatch)
    key = tmp_path / "id_ed25519"
    key.write_text("dummy key material")
    seen: dict[str, object] = {}

    class _FakeDownloader:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def download_all(self):
            return 0

    monkeypatch.setattr(downloader_mod, "YangDownloader", _FakeDownloader)
    monkeypatch.setattr(downloader_mod, "_configure_logging", lambda: None)
    monkeypatch.setattr(downloader_mod, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("DEVICE_IP", "127.0.0.1")
    monkeypatch.setenv("NETCONF_PORT", "830")
    monkeypatch.setenv("DEVICE_NAME", "fake")
    monkeypatch.setenv("DEVICE_USER", "u")
    monkeypatch.setenv("DEVICE_PASS", "env-password")
    monkeypatch.setenv("DEVICE_SSH_KEY", str(key))
    monkeypatch.setenv("DEVICE_SSH_KEY_PASSPHRASE", "env-key-pass")

    with caplog.at_level(logging.WARNING, logger=downloader_mod.__name__):
        downloader_mod.main()

    assert seen["password"] is None
    assert seen["key_filename"] == str(key)
    assert seen["passphrase"] == "env-key-pass"
    assert "Both DEVICE_PASS and DEVICE_SSH_KEY are set" in caplog.text


def test_downloader_explicit_key_disables_agent_and_default_key_fallbacks(
    tmp_path, monkeypatch
):
    """The downloader must not fall through to another host identity.

    ncclient's SSHSession._auth tries the named key, then the SSH agent, then
    ~/.ssh/id_* before giving up. A valid but unauthorized key would therefore
    still authenticate as whoever the host has keys for. Measured on SR Linux
    in containerlab: an unsigned, unauthorized key connected successfully via
    ~/.ssh/id_ed25519 until these two flags were forced off.
    """
    from unittest import mock

    from yang2sdk.cli import downloader as downloader_mod

    _key_auth_env(monkeypatch)
    key = tmp_path / "id_ed25519"
    key.write_text("dummy key material")

    class _Manager:
        server_capabilities: tuple[str, ...] = ()

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    monkeypatch.setattr(
        downloader_mod.YangDownloader, "get_schema_list", lambda self, manager: []
    )
    with mock.patch.object(
        downloader_mod.manager, "connect", return_value=_Manager()
    ) as connect:
        downloader = downloader_mod.YangDownloader(
            host="127.0.0.1",
            port=830,
            user="u",
            output_dir=tmp_path,
            key_filename=str(key),
        )
        assert downloader.download_all() == 0

    kwargs = connect.call_args.kwargs
    assert kwargs["key_filename"] == str(key)
    assert kwargs["allow_agent"] is False
    assert kwargs["look_for_keys"] is False


def test_downloader_warns_when_key_passphrase_is_supplied(
    tmp_path, monkeypatch, caplog
):
    """The downloader repeats the encrypted-key fallback warning."""
    import logging

    from yang2sdk.cli import downloader as downloader_mod

    _key_auth_env(monkeypatch)
    key = tmp_path / "id_ed25519"
    key.write_text("dummy key material")
    caplog.set_level(logging.WARNING, logger=downloader_mod.__name__)

    downloader = downloader_mod.YangDownloader(
        host="127.0.0.1",
        port=830,
        user="u",
        output_dir=tmp_path,
        key_filename=str(key),
        passphrase="key-secret",
    )

    assert downloader.transport_password == "key-secret"
    assert "try it as a password" in caplog.text


def test_downloader_exits_nonzero_when_extraction_fails(monkeypatch):
    """A failed authentication or a total extraction failure must not exit 0.

    `download_all()` returned a failure count, but `main()` ignored it, so an
    unauthorized key printed CRITICAL SYSTEM ERROR and still looked successful
    to a script or CI step.
    """
    from yang2sdk.cli import downloader as downloader_mod

    _key_auth_env(monkeypatch)

    class _FailingDownloader:
        def __init__(self, **kwargs):
            pass

        def download_all(self):
            return 1

    monkeypatch.setattr(downloader_mod, "YangDownloader", _FailingDownloader)
    monkeypatch.setattr(downloader_mod, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("DEVICE_IP", "127.0.0.1")
    monkeypatch.setenv("NETCONF_PORT", "830")
    monkeypatch.setenv("DEVICE_NAME", "fake")
    monkeypatch.setenv("DEVICE_USER", "u")
    monkeypatch.setenv("DEVICE_PASS", "p")

    with pytest.raises(SystemExit) as exc_info:
        downloader_mod.main()
    assert exc_info.value.code == 1


def test_downloader_rejects_a_non_xml_username(tmp_path, monkeypatch):
    """RFC 6242 Sec 3 / RFC 6241 Sec 2.2 apply to the downloader too."""
    from yang2sdk.cli.downloader import YangDownloader

    _key_auth_env(monkeypatch)
    with pytest.raises(ValueError, match="XML Char"):
        YangDownloader(
            host="127.0.0.1",
            port=830,
            user="bad\x00user",
            password="p",
            output_dir=tmp_path,
        )


def test_a_key_passphrase_without_a_key_is_refused(tmp_path, monkeypatch):
    """A stray passphrase must never displace the account password.

    ncclient's transport has one `password` slot, used first as the key
    passphrase and only then for password auth. Accepting a passphrase with no
    key would replace DEVICE_PASS with a value that cannot authenticate, and
    the failure would look like a wrong password rather than a misconfiguration.
    """
    import argparse

    from yang2sdk.cli.downloader import YangDownloader
    from yang2sdk.cli.sdk_verify import _resolve_credentials

    _key_auth_env(monkeypatch)
    with pytest.raises(ValueError, match="without key_filename"):
        YangDownloader(
            host="127.0.0.1",
            port=830,
            user="u",
            password="p",
            output_dir=tmp_path,
            passphrase="stray",
        )

    args = argparse.Namespace(
        username="u",
        password="p",
        ssh_key="",
        ssh_key_passphrase="stray",
    )
    with pytest.raises(SystemExit, match="without --ssh-key"):
        _resolve_credentials(args, "netconf")

    # The same rule applies when the stray passphrase comes from the
    # environment and the password is the only credential.
    monkeypatch.setenv("DEVICE_PASS", "p")
    monkeypatch.setenv("DEVICE_SSH_KEY_PASSPHRASE", "stray")
    args = argparse.Namespace(
        username="u", password="", ssh_key="", ssh_key_passphrase=""
    )
    with pytest.raises(SystemExit, match="without --ssh-key"):
        _resolve_credentials(args, "netconf")

    out, _ = _gen(tmp_path, "netconf", REPLACE_YANG, "tstrayenv")
    sm = _import_fresh(out, "session_manager")
    with pytest.raises(ValueError, match="without key_filename"):
        sm.NetconfClient(management_ip="127.0.0.1", username="u")


def test_certificate_without_its_private_key_is_refused(tmp_path, monkeypatch):
    """An explicit certificate is not a credential by itself.

    paramiko resolves `<key>-cert.pub` by loading the adjacent private key and
    attaching the certificate. With agent/~/.ssh discovery disabled for
    explicit paths, a cert without that key cannot authenticate, so both CLI
    consumers reject it before touching the transport.
    """
    import argparse

    from yang2sdk.cli.downloader import YangDownloader
    from yang2sdk.cli.sdk_verify import _resolve_credentials

    _key_auth_env(monkeypatch)
    cert = tmp_path / "id_ed25519-cert.pub"
    cert.write_text("ssh-ed25519-cert-v01@openssh.com AAAA dummy cert")

    with pytest.raises(ValueError, match="adjacent private key"):
        YangDownloader(
            host="127.0.0.1",
            port=830,
            user="u",
            output_dir=tmp_path,
            key_filename=str(cert),
        )

    args = argparse.Namespace(
        username="u",
        password="",
        ssh_key=str(cert),
        ssh_key_passphrase="",
    )
    with pytest.raises(SystemExit, match="adjacent private key"):
        _resolve_credentials(args, "netconf")

    # Placing the private key alongside the certificate makes it usable.
    (tmp_path / "id_ed25519").write_text("dummy key material")
    assert YangDownloader(
        host="127.0.0.1",
        port=830,
        user="u",
        output_dir=tmp_path,
        key_filename=str(cert),
    ).key_filename == str(cert)
    assert _resolve_credentials(args, "netconf")[2] == str(cert)


def test_sdk_verify_reports_connection_failure_without_a_traceback(
    tmp_path, monkeypatch
):
    """An unauthorized key is an operator error, not a Python crash.

    The negative control against SR Linux produced a full ncclient traceback
    ending in `AuthenticationError`, which buries the one actionable line.
    """
    import argparse

    from yang2sdk.cli import sdk_verify

    client_dir = tmp_path / "fake-client"
    client_dir.mkdir()
    (client_dir / "__init__.py").write_text("")

    class _RejectingClient:
        def __init__(self, **kwargs):
            raise RuntimeError("Authentication failed.")

    module = type("FakeModule", (), {"NetconfClient": _RejectingClient})
    monkeypatch.setattr(
        sdk_verify, "_import_client", lambda *args, **kwargs: (module, None)
    )
    monkeypatch.setenv("DEVICE_IP", "127.0.0.1")
    monkeypatch.setenv("NETCONF_PORT", "830")
    args = argparse.Namespace(
        client_dir=str(client_dir),
        device="fake",
        verify_tls=False,
        debug=False,
    )

    with pytest.raises(SystemExit, match="connection failed: Authentication failed."):
        sdk_verify.run_protocol(
            "netconf", args, {"read"}, "u", None, str(tmp_path / "id_ed25519")
        )


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
    assert _pkg_file(out, "__init__.py").is_file()
    assert _pkg_file(out, "session_manager.py").is_file()


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


# --- content= parity across NETCONF branches


def _import_fresh(pkg_dir, dotted):
    """Import from a freshly compiled package, ignoring an earlier one.

    Same failure as `_import` guards against, forced: purge unconditionally.
    """
    package = _package(pkg_dir)
    _purge_package(package.name)
    _IMPORTED_FROM.pop(package.name, None)
    return _import(pkg_dir, dotted)


# --- content= parity across NETCONF branches -------------------------------
#
#
# `content=` reaches the wire three different ways, and one of them cannot be
# expressed in RFC 6241. Measured on generated clients by stubbing the
# dispatch (below) rather than inferred:
#
#   content      NMDA (RFC 8526)             legacy (RFC 6241)
#   config       <config-filter>true</>      <get-config>
#   all          no config-filter            <get>
#   nonconfig    <config-filter>false</>     <get>   <- state AND running config
#
# RFC 6241 Sec 7.7 defines <get> as returning both, and there is no state-only
# read, so a legacy `content="nonconfig"` arrives unfiltered. The generated
# navigator prunes to `is_config` metadata (prune_xml_to_content) so the caller
# sees the same thing on every transport.
#
# RESTCONF needs no such handling: the server filters on `?content=`
# (RFC 8040 Sec 4.5.2), pinned by test_content_filter_prunes_nested_state.

_NMDA_NS = "urn:ietf:params:xml:ns:yang:ietf-netconf-nmda"
_NC_NS = "urn:ietf:params:xml:ns:netconf:base:1.0"

# What a legacy <get> returns: every node populated, config and state mixed.
_UNFILTERED_REPLY = (
    f'<data xmlns="{_NC_NS}">'
    '<top xmlns="urn:test:tcontent">'
    "<name>cfg-leaf</name>"
    "<counter>42</counter>"
    "<only_state><reads>7</reads></only_state>"
    "<mixed><cfg>C</cfg><obs>O</obs></mixed>"
    "<only_config><a>A</a><b>B</b></only_config>"
    "</top>"
    "</data>"
).encode()

CONTENT_STATE_YANG = """module tcontent {
  prefix tc;
  namespace "urn:test:tcontent";
  revision 2026-01-01;
  container top {
    leaf name { type string; }
    leaf counter { type string; config false; }

    // A container holding only state. YANG inherits `config true` onto it from
    // its parent (RFC 7950 Sec 7.21.6), so pruning on the container's own
    // is_config would drop it whole and lose the counter inside.
    container only_state {
      leaf reads { type string; config false; }
    }

    // One child of each kind: the state child must survive a nonconfig filter.
    container mixed {
      leaf cfg { type string; }
      leaf obs { type string; config false; }
    }

    // Emptied entirely by a nonconfig filter: must read as None, not as an
    // empty model instance a consumer would treat as present.
    container only_config {
      leaf a { type string; }
      leaf b { type string; }
    }
  }
}
"""


class _Reply:
    """Stands in for an ncclient reply, exposing both accessor styles.

    `retrieve` reads `.data_ele` on the legacy path and `_reply_root` reads
    `.ele`/`.xml` on the NMDA path, so the stub must carry all three.
    """

    def __init__(self, xml: bytes):
        from lxml import etree  # ty: ignore[unresolved-import] - lxml ships no stubs

        self.xml = xml
        self.data_ele = etree.fromstring(xml).find(f"{{{_NC_NS}}}data")


class _ManagerSpy:
    """Records which transport verb the client chose."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _reply(self, name: str, kwargs: dict[str, Any]) -> _Reply:
        self.calls.append((name, kwargs))
        return _Reply(_UNFILTERED_REPLY)

    def get(self, **kwargs):
        return self._reply("<get>", kwargs)

    def get_config(self, **kwargs):
        return self._reply("<get-config>", kwargs)

    def dispatch(self, rpc):
        return self._reply("<get-data>", {"rpc": rpc})


def _stub_netconf_session(sm, has_nmda: bool):
    """A NetconfClient wired to spies, with no socket and no credentials."""
    client = sm.NetconfClient.__new__(sm.NetconfClient)
    client.has_nmda = has_nmda
    client._manager = _ManagerSpy()
    client.module_namespaces = {}
    client._handle_rpc = lambda func, *a, **kw: func(*a, **kw)
    return client


@pytest.mark.parametrize(
    ("content", "want_verb", "want_config_filter"),
    [
        ("config", "<get-data>", "true"),
        ("all", "<get-data>", None),
        ("nonconfig", "<get-data>", "false"),
    ],
)
def test_netconf_nmda_sends_config_filter(
    tmp_path, content, want_verb, want_config_filter
):
    """NMDA expresses all three content values as <config-filter>."""
    out, _ = _gen(tmp_path, "netconf", CONTENT_STATE_YANG, "tcontent")
    sm = _import_fresh(out, "session_manager")

    client = _stub_netconf_session(sm, has_nmda=True)
    client.retrieve(source="running", content=content)

    verb, kwargs = client._manager.calls[0]
    assert verb == want_verb, kwargs
    node = kwargs["rpc"].find(f"{{{_NMDA_NS}}}config-filter")
    got = None if node is None else node.text
    assert got == want_config_filter, f"{content}: config-filter={got!r}"


def test_netconf_nmda_max_depth_is_normalized_and_validated(tmp_path):
    """NMDA carries `max-depth` (RFC 8526), not the legacy depth filter.
    `depth=1` is valid, `"2"` is normalized, and `"unbounded"` omits the
    element. Invalid values fail before the RPC.
    """
    out, _ = _gen(tmp_path, "netconf", CONTENT_STATE_YANG, "tcontent")
    sm = _import_fresh(out, "session_manager")

    for depth, want in ((1, "1"), ("2", "2"), (65535, "65535")):
        client = _stub_netconf_session(sm, has_nmda=True)
        client.retrieve(source="running", depth=depth)
        _verb, kwargs = client._manager.calls[0]
        node = kwargs["rpc"].find(f"{{{_NMDA_NS}}}max-depth")
        assert node is not None and node.text == want

    for depth in ("unbounded", "UNBOUNDED"):
        client = _stub_netconf_session(sm, has_nmda=True)
        client.retrieve(source="running", depth=depth)
        _verb, kwargs = client._manager.calls[0]
        assert kwargs["rpc"].find(f"{{{_NMDA_NS}}}max-depth") is None

    for bad in (0, -1, True, "foo", 70000, None):
        client = _stub_netconf_session(sm, has_nmda=True)
        with pytest.raises(ValueError, match="depth must be"):
            client.retrieve(source="running", depth=bad)
        assert client._manager.calls == []


def test_netconf_legacy_finite_depth_without_a_filter_is_refused(tmp_path):
    """RFC 6241 has no server-side depth. A finite-depth legacy read with no
    subtree filter would be a whole-datastore read; the session manager must
    refuse it instead of silently ignoring the ceiling. `unbounded` remains
    the explicit escape hatch.
    """
    out, _ = _gen(tmp_path, "netconf", CONTENT_STATE_YANG, "tcontent")
    sm = _import_fresh(out, "session_manager")

    client = _stub_netconf_session(sm, has_nmda=False)
    with pytest.raises(ValueError, match="subtree filter"):
        client.retrieve(source="running", depth=2)
    assert client._manager.calls == []

    # Unbounded is allowed without a filter.
    client = _stub_netconf_session(sm, has_nmda=False)
    client.retrieve(source="running", depth="unbounded")
    assert len(client._manager.calls) == 1


@pytest.mark.parametrize(
    ("content", "want_verb"),
    [("config", "<get-config>"), ("all", "<get>"), ("nonconfig", "<get>")],
)
def test_netconf_legacy_maps_content_onto_get_or_get_config(
    tmp_path, content, want_verb
):
    """Legacy maps config to <get-config>; all and nonconfig both become <get>.

    The last row is the whole problem: RFC 6241 has no state-only read, so
    nonconfig and all are indistinguishable on the wire here.
    """
    out, _ = _gen(tmp_path, "netconf", CONTENT_STATE_YANG, "tcontent")
    sm = _import_fresh(out, "session_manager")

    client = _stub_netconf_session(sm, has_nmda=False)
    client.retrieve(content=content)

    verb, _kwargs = client._manager.calls[0]
    assert verb == want_verb, f"content={content!r} used {verb}"


@pytest.mark.parametrize(
    ("content", "want_config", "want_state"),
    [
        ("config", True, False),
        ("all", True, True),
        ("nonconfig", False, True),
    ],
)
def test_netconf_content_parity_when_the_device_does_not_filter(
    tmp_path, content, want_config, want_state
):
    """The caller sees the same thing on both branches, whatever the device did.

    The session is stubbed to return the unfiltered reply a legacy <get>
    produces, so this exercises exactly the case RFC 6241 cannot express. If
    the prune is removed from the navigator, the state/config columns flip and
    this fails.
    """
    from lxml import etree as _et  # ty: ignore[unresolved-import] - lxml ships no stubs

    out, _ = _gen(tmp_path, "netconf", CONTENT_STATE_YANG, "tcontent")
    navs = _import_fresh(out, "data_navigators.tcontent")
    models = _import_fresh(out, "data_models.tcontent")

    class _Client:
        module_namespaces: ClassVar[dict[str, str]] = {}

        def retrieve(self, source=None, filter_xml=None, content=None, **kw):
            # Always the unfiltered reply: the legacy <get> case.
            return _et.fromstring(_UNFILTERED_REPLY)

    node_cls = getattr(navs, f"{models.Top.__name__}Node")
    model = node_cls(_Client(), [("urn:test:tcontent", "top", {})]).retrieve(
        content=content, depth="unbounded"
    )

    assert model is not None, "no <top> returned"
    got_config = model.name is not None
    got_state = model.counter is not None
    assert (got_config, got_state) == (want_config, want_state), (
        f"content={content!r}: got config={got_config} state={got_state}, "
        f"want config={want_config} state={want_state}"
    )


def _top_from_unfiltered_reply(tmp_path):
    """The Top model as a caller sees it when the device did NOT filter."""
    from lxml import etree as _et  # ty: ignore[unresolved-import] - lxml ships no stubs

    out, _ = _gen(tmp_path, "netconf", CONTENT_STATE_YANG, "tcontent")
    navs = _import_fresh(out, "data_navigators.tcontent")
    models = _import_fresh(out, "data_models.tcontent")

    class _Client:
        module_namespaces: ClassVar[dict[str, str]] = {}

        def retrieve(self, source=None, filter_xml=None, content=None, **kw):
            return _et.fromstring(_UNFILTERED_REPLY)

    node_cls = getattr(navs, f"{models.Top.__name__}Node")
    return node_cls(_Client(), [("urn:test:tcontent", "top", {})]).retrieve(
        content="nonconfig", depth="unbounded"
    )


def test_netconf_nonconfig_keeps_state_inside_a_config_container(tmp_path):
    """A container whose children are ALL `config false` must survive.

    Regression, caught by demonstration rather than by a test: YANG inherits
    `config true` onto a container that does not state it (RFC 7950 Sec 7.21.6),
    so `only_state` is is_config=True in the generated model even though its
    only leaf is state. Filtering on the container's own flag deleted it whole
    and the caller lost the data inside:

        top.only_state = None      instead of OnlyState(reads='7')

    Containers and lists are groupings, so the prune recurses and decides on
    the leaves.
    """
    model = _top_from_unfiltered_reply(tmp_path)

    assert model.only_state is not None, (
        "state-only container dropped whole: YANG inherits config true onto it, "
        "so is_config on the container must not drive the filter"
    )
    assert model.only_state.reads == "7", model.only_state


def test_netconf_nonconfig_keeps_the_state_child_of_a_mixed_container(tmp_path):
    """`mixed` holds one config and one state child; only the state one survives."""
    model = _top_from_unfiltered_reply(tmp_path)

    assert model.mixed is not None, "mixed container dropped instead of pruned"
    assert model.mixed.obs == "O", model.mixed
    assert model.mixed.cfg is None, (
        f"config leaf survived a nonconfig read: {model.mixed}"
    )


def test_netconf_nonconfig_renders_an_emptied_container_as_none(tmp_path):
    """A container emptied by the filter reads as absent, not as an empty model.

    Kept-empty deserializes to `OnlyConfig(a=None, b=None)`, and
    `if model.only_config:` is then True -- a consumer would read a device that
    has no such container as having one. None is the honest answer.
    """
    model = _top_from_unfiltered_reply(tmp_path)

    assert model.only_config is None, (
        f"emptied container should be None, got {model.only_config!r}"
    )
    assert not model.only_config, "None must stay falsy for `if node:` checks"


def test_netconf_model_dump_takes_content_and_prunes_like_restconf(tmp_path):
    """`NetconfBaseModel.model_dump(content=...)` mirrors the RESTCONF base.

    Parity regression: NETCONF models were plain pydantic-xml, so a caller had
    to know the transport -- `model_dump(content="config")` raised TypeError
    and `model_dump_json()` serialised the whole schema, every absent node as
    `null` and every YANG default materialised. Both dump verbs now apply the
    same `is_config` rule the XML layer already applies, so a read dump holds
    the content the caller asked for and nothing else.
    """
    import json

    out, _ = _gen(tmp_path, "netconf", CONTENT_STATE_YANG, "tcontent")
    models = _import_fresh(out, "data_models.tcontent")

    model = models.Top.model_validate(
        {
            "name": "cfg-leaf",
            "counter": "42",
            "only_state": {"reads": "7"},
            "mixed": {"cfg": "C", "obs": "O"},
            "only_config": {"a": "A", "b": "B"},
        }
    )

    config = model.model_dump(content="config")
    assert "counter" not in config, f"state leaf leaked: {config}"
    # `only_state` is a config-true container (RFC 7950 Sec 7.21.6 inherits the
    # flag), so it matches the filter and is RETAINED even once emptied -- the
    # same rule the RESTCONF base documents and the XML layer implements.
    assert config["only_state"] == {}, f"emptied matching container: {config}"
    assert config["mixed"] == {"cfg": "C"}, (
        f"shell must survive with its state child pruned: {config['mixed']}"
    )
    assert config["only_config"] == {"a": "A", "b": "B"}
    assert "nc_operation" not in config, (
        "nc:operation is an attribute, never resource contents"
    )
    assert all(
        "nc_operation" not in v for v in config.values() if isinstance(v, dict)
    ), f"nc:operation leaked into a nested dump: {config}"

    nonconfig = model.model_dump(content="nonconfig")
    assert "name" not in nonconfig, f"config leaf leaked: {nonconfig}"
    assert nonconfig["counter"] == "42"
    assert nonconfig["mixed"] == {"obs": "O"}, (
        f"config child of a mixed container must be pruned: {nonconfig['mixed']}"
    )

    # content="all" is the plain pydantic dump: nothing pruned.
    everything = model.model_dump(content="all")
    assert everything["name"] == "cfg-leaf" and everything["counter"] == "42"

    # pydantic's BaseModel.model_dump_json does NOT route through model_dump,
    # so the JSON verb needs its own content handling or it silently serialises
    # the unpruned tree.
    assert json.loads(model.model_dump_json(content="config")) == config
    assert json.loads(model.model_dump_json(content="nonconfig")) == nonconfig
    # ensure_ascii is the JSON encoder's, not model_dump's: it must not be
    # forwarded to the dict dump (which would raise TypeError).
    assert (
        json.loads(model.model_dump_json(content="config", ensure_ascii=True)) == config
    )


def _surviving_paths_netconf(tmp_path, content):
    """Field paths a NETCONF caller can see after `content=` filtering."""
    from lxml import etree as _et  # ty: ignore[unresolved-import] - lxml ships no stubs

    out, _ = _gen(tmp_path, "netconf", CONTENT_STATE_YANG, "tcontent")
    base = _import_fresh(out, "data_navigators._base")
    models = _import_fresh(out, "data_models.tcontent")

    top = _et.fromstring(_UNFILTERED_REPLY).find("{urn:test:tcontent}top")
    base.prune_xml_to_content(top, models.Top, content)
    return {_et.QName(c).localname for c in top}


def _surviving_paths_restconf(tmp_path, content):
    """Field paths a RESTCONF caller can see after `content=` filtering."""
    out, _ = _gen(tmp_path, "restconf", CONTENT_STATE_YANG, "tcontent")
    models = _import_fresh(out, "data_models.tcontent")

    # The payload the navigator hands to model_validate: the unwrapped top
    # container, exactly as restconf/data_navigators/_base.py.jinja:251-258
    # produces it.
    model = models.Top.model_validate(
        {
            "name": "cfg-leaf",
            "counter": "42",
            "only_state": {"reads": "7"},
            "mixed": {"cfg": "C", "obs": "O"},
            "only_config": {"a": "A", "b": "B"},
        }
    )
    return set(model.model_dump(content=content))


@pytest.mark.parametrize("content", ["config", "nonconfig"])
def test_netconf_and_restconf_content_pruning_agree(tmp_path, content):
    """The two transports must prune identically for the same input.

    NETCONF has to prune client-side (RFC 6241 cannot express a state-only
    read) while RESTCONF lets the server filter and prunes in the model layer.
    Two implementations of one rule is only acceptable if they cannot drift,
    so this compares their OUTPUT on the same module and the same data rather
    than asserting each in isolation.

    `mixed` is the discriminating case: it keeps its ancestor shell because a
    matchable descendant remains, while `only_config` is dropped outright.
    """
    netconf = _surviving_paths_netconf(tmp_path / "nc", content)
    restconf = _surviving_paths_restconf(tmp_path / "rc", content)

    assert netconf == restconf, (
        f"content={content!r}: NETCONF kept {sorted(netconf)}, "
        f"RESTCONF kept {sorted(restconf)}"
    )


def test_restconf_keeps_an_emptied_matching_container(tmp_path):
    """The rule both transports now share, asserted on RESTCONF directly.

    A config container whose children are all state is emptied by a
    content="config" filter, and it is RETAINED -- not dropped -- because the
    IR tracks no presence flag, so a dropped container is indistinguishable
    from one the device never had, and an omitted node is a deletion in a PUT
    body (RFC 8040 Sec 4.5).
    """
    out, _ = _gen(tmp_path, "restconf", CONTENT_STATE_YANG, "tcontent")
    models = _import_fresh(out, "data_models.tcontent")

    model = models.Top.model_validate(
        {"name": "n", "counter": "42", "mixed": {"cfg": "C", "obs": "O"}}
    )
    kept = model.model_dump(content="config")

    assert "mixed" in kept, f"emptied matching container dropped: {kept}"
    assert "obs" not in kept["mixed"], f"state leaf leaked into a config dump: {kept}"


def test_generated_sdist_carries_one_copy_of_the_package(tmp_path):
    """One copy of the code, in an installable project the README can name.

    The emitter used to write the client twice: flat at the output dir so the
    lab harness could `import temp.<proto>_clients.<device>`, and namespaced so
    pip could install it. Two trees could drift, the sdist needed a hand-written
    exclude to ship one (measured: a 25-entry tarball with two `data_models/`,
    two `data_navigators/`, two `__init__.py`, two `session_manager.py`, two
    `py.typed`), and the generated README's install line -- `uv add
    path/to/<package_name>` -- pointed at a directory with no `pyproject.toml`,
    so `uv build` refused it outright.

    Asserted on the artifact rather than on the generator's intent, and the
    wheel-from-sdist path is exercised too, because that is what `pip install
    <sdist>` does.
    """
    import tarfile

    out, _ = _gen(tmp_path, "netconf", CONTENT_STATE_YANG, "tsdist")
    project = _project(out)
    package = _package(out)

    # The directory the generated README tells you to `uv add` must be the
    # project: it carries the packaging metadata itself.
    readme = (project / "README.md").read_text()
    assert f"uv add path/to/{package.name}" in readme, readme[:400]
    for meta in ("pyproject.toml", "README.md", "MANIFEST.yang-revisions.json"):
        assert (project / meta).is_file(), f"{meta} missing from {project}"
    # ...and the code lives under src/, once.
    assert package.parent.name == "src"
    assert sorted(p.name for p in out.iterdir()) == [package.name], sorted(
        p.name for p in out.iterdir()
    )

    dist = tmp_path / "dist"
    build = subprocess.run(
        ["uv", "build", "--out-dir", str(dist), str(project)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    assert build.returncode == 0, build.stderr[-2000:]

    sdists = list(dist.glob("*.tar.gz"))
    assert len(sdists) == 1, [p.name for p in sdists]
    with tarfile.open(sdists[0]) as tar:
        names = ["/".join(n.split("/")[1:]) for n in tar.getnames()]

    # Exactly one copy of the package, under src/, and nothing at the root but
    # packaging metadata.
    inits = [n for n in names if n.endswith("/__init__.py") or n == "__init__.py"]
    top_level_inits = [n for n in inits if n.count("/") == 2 and n.startswith("src/")]
    nested = [n for n in inits if n.count("/") > 2 and "/data_" not in n]
    assert len(top_level_inits) == 1, sorted(inits)
    assert not nested, f"package nested inside itself: {sorted(inits)}"
    assert not any(n == "__init__.py" for n in names), (
        f"a flat copy is still in the sdist: {sorted(names)}"
    )
    for member in ("data_models", "data_navigators"):
        tops = [
            n
            for n in names
            if n.startswith(f"src/{package.name}/{member}/") and n.count("/") == 3
        ]
        assert tops, f"src/{package.name}/{member} missing from sdist: {sorted(names)}"

    # `pip install <sdist>` builds a wheel from the extracted tree: prove the
    # src layout survives that round trip.
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    with tarfile.open(sdists[0]) as tar:
        tar.extractall(extracted)  # our own build output, into tmp_path
    src = next(extracted.iterdir())
    wheel_dir = tmp_path / "dist2"
    again = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(wheel_dir), str(src)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    assert again.returncode == 0, again.stderr[-2000:]
    wheels = list(wheel_dir.glob("*.whl"))
    assert wheels, "no wheel built from the extracted sdist"
    # The wheel must install `import <package_name>`, never bare `data_models`.
    import zipfile

    with zipfile.ZipFile(wheels[0]) as wheel:
        wheel_names = wheel.namelist()
    assert f"{package.name}/__init__.py" in wheel_names, wheel_names
    assert not any(
        n.startswith(("data_models/", "session_manager.py")) for n in wheel_names
    ), f"wheel puts package modules at site-packages root: {wheel_names}"


def test_the_container_holds_exactly_one_project(tmp_path):
    """`--output-dir` is a container, so a second compile must not mix in.

    The generated package name embeds the device *and* OS version, so
    recompiling the same device after an upgrade would leave
    `srl_0_0_0/` next to `srl_25_10_1/`. Every consumer then has to pick one,
    and a stale tree on `sys.path` is the wrong device's models. Refusing is
    the documented behaviour (the tree is in gitignored `temp/`, where a
    previous compile is not automatically garbage).
    """
    yang = tmp_path / "tstale.yang"
    yang.write_text(MINIMAL_YANG)

    out, _ = _gen(tmp_path, "netconf", MINIMAL_YANG, "tstale")
    assert (out / "tstale_0_0_0").is_dir(), sorted(p.name for p in out.iterdir())

    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; from yang2sdk.cli.compiler import run_compiler;"
                " run_compiler('netconf', sys.argv[1:])"
            ),
            str(yang),
            "--device",
            "tstale",
            "--device-version",
            "9.9",
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
    assert proc.returncode != 0, "a second project in one container was accepted"
    assert "refusing to generate" in proc.stderr, proc.stderr[-800:]
    assert "tstale_0_0_0/" in proc.stderr, proc.stderr[-800:]
    # Nothing was written for the refused run.
    assert not (out / "tstale_9_9").exists()
    assert sorted(p.name for p in out.iterdir()) == ["tstale_0_0_0"]


def test_a_legacy_flat_output_dir_is_refused(tmp_path):
    """The pre-`src/` layout must be refused, not silently mixed with.

    A container that still holds `__init__.py`/`data_models/` at its root is a
    compile from before the layout change. Writing the new project next to it
    would leave two importable trees again -- the duplication this change
    removed -- and `import <container>` would resolve as an empty namespace
    package, which is worse than an error.
    """
    yang = tmp_path / "tlegacy.yang"
    yang.write_text(MINIMAL_YANG)
    out = tmp_path / "legacy"
    (out / "data_models").mkdir(parents=True)
    (out / "__init__.py").write_text("")

    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; from yang2sdk.cli.compiler import run_compiler;"
                " run_compiler('netconf', sys.argv[1:])"
            ),
            str(yang),
            "--device",
            "tlegacy",
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
    assert proc.returncode != 0, "a legacy flat output dir was accepted"
    assert "legacy flat copy: __init__.py" in proc.stderr, proc.stderr[-800:]
    assert not (out / "tlegacy_0_0_0").exists()


def test_consumers_resolve_container_project_and_package(tmp_path):
    """Every shape an operator has to hand resolves to the one package.

    `sdk-verify --client-dir` and the lab fixtures accept the container (the
    default), the project (what `uv add` takes) and the package itself. Two
    projects in one container is ambiguous, and guessing would verify the wrong
    device.
    """
    out, _ = _gen(tmp_path, "netconf", MINIMAL_YANG, "tresolve")
    project = _project(out)
    package = _package(out)

    for candidate in (out, project, package):
        assert layout.resolve_package(candidate) == package, candidate

    with pytest.raises(LookupError):
        layout.resolve_package(tmp_path / "does-not-exist")

    # A second *package* makes the container ambiguous.
    other = out / "tresolve_9_9"
    (other / "src" / "tresolve_9_9").mkdir(parents=True)
    (other / "src" / "tresolve_9_9" / "__init__.py").write_text("")
    (other / "pyproject.toml").write_text('[project]\nname = "tresolve_9_9"\n')
    with pytest.raises(LookupError) as exc:
        layout.resolve_package(out)
    assert "exactly one" in str(exc.value), str(exc.value)


def test_both_protocols_import_as_distinct_modules(tmp_path, monkeypatch):
    """The lab fixture's two imports must not collapse into one module.

    Both protocols generate `<device>_<version>` -- each is its own
    installable project -- so the RESTCONF and NETCONF trees in
    `temp/notconf_clients` carry the SAME import name. A plain
    `importlib.import_module(package.name)` returns the first one out of
    `sys.modules` for the second protocol too, and every NETCONF test then
    holds the RESTCONF client: measured as five red smoke shards, all dying
    on `AttributeError: module '...' has no attribute 'NetconfClient'`.
    `_import_pkg` therefore imports under a unique per-protocol name, the same
    rule `sdk_verify._import_client` follows for `--protocol both`.
    """
    from tests import conftest

    for fmt, attr in (("restconf", "RestconfClient"), ("netconf", "NetconfClient")):
        package = tmp_path / f"tdevice_{fmt}" / "tdevice_1_0" / "src" / "tdevice_1_0"
        package.mkdir(parents=True)
        (package.parent.parent / "pyproject.toml").write_text(
            '[project]\nname = "tdevice_1_0"\n'
        )
        (package / "__init__.py").write_text(f"{attr} = object\n")
    monkeypatch.setattr(conftest, "TEMP_CLIENTS", tmp_path)

    try:
        rest = conftest._import_pkg("tdevice", "restconf")
        netc = conftest._import_pkg("tdevice", "netconf")
        assert rest is not netc
        assert rest.RestconfClient is not None
        assert netc.NetconfClient is not None
    finally:
        for fmt in ("restconf", "netconf"):
            sys.modules.pop(f"_sdktest_{fmt}_tdevice_1_0", None)


# --- yang2sdk CLI: root picking ---------------------------------------------
#
# Picking root modules was the last hand-fed step in the pipeline, and it is
# load-bearing: pyang expands an `augment` only when the augmenting module is
# also named (RFC 7950 Sec 7.17), so a wrong root set emits a model that
# imports cleanly and then rejects the device's own replies under
# `extra="forbid"`. These tests pin WHAT gets suggested, measured against the
# real vendor trees.

_PICKER_YANG = {
    "pbase.yang": """module pbase {
  prefix pb;
  namespace "urn:test:pbase";
  revision 2026-01-01;
  container system { leaf hostname { type string; } }
  rpc reboot { input { leaf delay { type uint32; } } }
}
""",
    # Imports base -> not a root. Augments base -> reached via the closure.
    # Statement order is load-bearing: a module body is namespace, prefix,
    # import/include, revision, then data definitions. `import` after `revision`
    # is a parse error, and pyang reports it as 'unexpected keyword "import"'
    # rather than as an ordering complaint. Filenames match module names so
    # pyang does not warn.
    "pimports.yang": """module pimports {
  prefix pi;
  namespace "urn:test:pimports";
  import pbase {
    prefix pb;
  }
  revision 2026-01-01;
  container extra {
    leaf x {
      type string;
    }
  }
}
""",
    "paug.yang": """module paug {
  prefix pa;
  namespace "urn:test:paug";
  import pbase {
    prefix pb;
  }
  revision 2026-01-01;
  augment "/pb:system" {
    leaf patched {
      type string;
    }
  }
}
""",
    # Operations only: no top-level data node at all.
    "pops.yang": """module pops {
  prefix po;
  namespace "urn:test:pops";
  revision 2026-01-01;
  rpc install { input { leaf pkg { type string; } } output { leaf ok { type boolean; } } }
  rpc remove { }
}
""",
    # The transport itself: rpcs that are the client's own operations. Named
    # `ietf-netconf` because the exclusion is name-based on the real IETF
    # module names -- a fixture called "pproto" would never exercise the rule.
    "ietf-netconf.yang": """module ietf-netconf {
  prefix pp;
  namespace "urn:test:pproto";
  revision 2026-01-01;
  rpc get-config { }
  rpc edit-config { }
}
""",
    # Real data, so a real suggestion even though the name looks like plumbing.
    "pmonitor.yang": """module pmonitor {
  prefix pm;
  namespace "urn:test:pmonitor";
  revision 2026-01-01;
  container state { leaf uptime { type uint32; } }
  rpc get-schema { }
}
""",
}


def _picker_dir(tmp_path: Path) -> Path:
    d = tmp_path / "picker_yang"
    d.mkdir(parents=True, exist_ok=True)
    for name, text in _PICKER_YANG.items():
        (d / name).write_text(text)
    return d


def _suggested(d: Path) -> list[str]:
    from yang2sdk.cli import rootpicker

    return [
        c.module
        for c in rootpicker.suggested_roots(
            rootpicker.with_top_level(rootpicker.scan(d), d)
        )
    ]


def test_picker_suggests_data_and_operation_modules(tmp_path):
    """A suggested root carries top-level data OR operations.

    Regression from the G30 tree: the rule counted data nodes only, so
    `coriant-rpc` (41 operations) and `coriant-cli` (37) were dropped -- a
    module whose entire surface is operations declares no data nodes. Measured
    there, vendor RPC modules are the device's API and belong in the model.
    """
    suggested = _suggested(_picker_dir(tmp_path))

    assert "pbase" in suggested, "a data-bearing module must be suggested"
    assert "pops" in suggested, (
        "an operations-only module must be suggested: it declares no top-level "
        "data node, and dropping it loses the device's whole API"
    )


def test_picker_excludes_transport_rpcs(tmp_path):
    """`get-config`/`edit-config` are the client's own operations, not roots.

    Compiling them is how the G30 tree ended up with 18 of 19 top-level
    navigators being protocol plumbing.
    """
    assert "pproto" not in _suggested(_picker_dir(tmp_path))


def test_picker_keeps_a_module_that_has_real_data_despite_its_name(tmp_path):
    """ietf-netconf-monitoring stays a root because netconf-state is a read.

    The transport exclusion is name-based, so it has to stop at the first
    `ietf-netconf*` that carries real data of its own -- otherwise the rule
    would silently drop a legitimate device read.
    """
    assert "pmonitor" in _suggested(_picker_dir(tmp_path))


def test_picker_offers_a_module_that_imports_another(tmp_path):
    """Being imported does not disqualify a module as a root.

    Measured on SR Linux: `srl_nokia-system` is imported by 88 modules and is
    still one of the two roots ci-srl compiles. A rule that excluded modules
    with importers disqualified the single most useful root on the device.
    """
    assert "pimports" in _suggested(_picker_dir(tmp_path))


def test_picker_does_not_offer_an_augment_only_module(tmp_path):
    """Augment-only modules are the closure's job, not the operator's.

    pyang expands an `augment` only when the augmenting module is also named
    (RFC 7950 Sec 7.17), and `expand_augment_closure` already resolves that
    transitively -- measured on SR Linux, naming 2 roots pulls in 93 more.
    Offering them as roots would ask the operator to do by hand what the
    compiler does automatically.
    """
    assert "paug" not in _suggested(_picker_dir(tmp_path))


def _run_cli(argv: list[str], **popen_kwargs) -> subprocess.CompletedProcess:
    """Invoke the `yang2sdk` entry point as a subprocess, like a user does."""
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from yang2sdk.cli.main import main; raise SystemExit(main())",
            *argv,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
        **popen_kwargs,
    )


def test_yang2sdk_without_protocol_teaches_and_does_not_compile():
    """Bare `yang2sdk` prints the pipeline and exits non-zero.

    Exit 0 would let a script that forgot --protocol read "success" from a run
    that compiled nothing.
    """
    proc = _run_cli([])

    assert proc.returncode == 2, proc.stdout[-500:]
    assert "yang-downloader" in proc.stdout + proc.stderr, "help must teach the flow"
    assert "--protocol" in proc.stdout + proc.stderr


def test_yang2sdk_help_teaches_the_four_step_pipeline():
    proc = _run_cli(["--help"])

    assert proc.returncode == 0, proc.stderr[-500:]
    text = proc.stdout
    for expected in ("yang-downloader", "--protocol", "uv add", "sdk-verify"):
        assert expected in text, f"--help must mention {expected!r}"


def test_yang2sdk_refuses_to_prompt_when_stdin_is_not_a_tty(tmp_path):
    """CI has no TTY, so an absent --roots must fail fast, not block on a read.

    This is the property that keeps the blocking ci-srl job safe: a prompt that
    waits for input that will never arrive is a hung pipeline.
    """
    d = _picker_dir(tmp_path)
    proc = _run_cli(
        ["--protocol", "netconf", "--yang-dir", str(d), "--device", "x"],
        stdin=subprocess.DEVNULL,
    )

    assert proc.returncode == 2, proc.stdout[-500:]
    combined = proc.stdout + proc.stderr
    assert "not a terminal" in combined, combined[-500:]
    assert "--roots" in combined, "the error must say how to proceed"


def test_yang2sdk_roots_resolves_names_to_files(tmp_path):
    """`--roots a,b` becomes real paths, and an unknown name fails loudly."""
    d = _picker_dir(tmp_path)
    proc = _run_cli(
        [
            "--protocol",
            "netconf",
            "--yang-dir",
            str(d),
            "--device",
            "pickerok",
            "--device-version",
            "1.0",
            "--output-dir",
            str(tmp_path / "out"),
            "--roots",
            "pbase",
        ],
        stdin=subprocess.DEVNULL,
    )

    assert proc.returncode == 0, proc.stderr[-2000:]
    out = tmp_path / "out"
    assert (out / "pickerok_1_0" / "pyproject.toml").is_file(), sorted(
        p.name for p in out.iterdir()
    )
    assert (out / "pickerok_1_0" / "src" / "pickerok_1_0" / "data_models").is_dir()


def test_yang2sdk_rejects_an_unknown_root_name(tmp_path):
    d = _picker_dir(tmp_path)
    proc = _run_cli(
        ["--protocol", "netconf", "--yang-dir", str(d), "--roots", "nosuchmodule"],
        stdin=subprocess.DEVNULL,
    )

    assert proc.returncode == 2, proc.stdout[-500:]
    assert "nosuchmodule" in proc.stderr, proc.stderr[-500:]


BROKEN_YANG = """module tbroken {
  prefix tb;
  namespace "urn:test:tbroken";
  revision 2026-01-01;
  container top { leaf ok { type string; } }
  rpc broken {
    // `confirmation` belongs to ietf-yang-metadata, not the core grammar.
    // YANG comments are // or /* */ -- a # is itself an illegal keyword.
    confirmation "not a core statement";
  }
}
"""


def test_a_yang_error_writes_no_client(tmp_path):
    """A model pyang rejects must leave nothing behind.

    pyang prints errors and calls the emitter anyway, exiting non-zero only
    afterwards, so the run both wrote a complete client AND reported failure.
    Measured 2026-10-16: the emitted models were byte-identical to a clean
    compile of the same YANG while the exit was 1. That misleads twice -- CI
    reddens on a good artifact, and a human who trusts the "Generated SDK in:
    <dir>" line publishes a model built from rejected YANG.
    """
    (tmp_path / "tbroken.yang").write_text(BROKEN_YANG)
    out = tmp_path / "broken_client"
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; from yang2sdk.cli.compiler import run_compiler;"
                " run_compiler('netconf', sys.argv[1:])"
            ),
            str(tmp_path / "tbroken.yang"),
            "--device",
            "tbroken",
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

    assert "unexpected keyword" in proc.stderr, (
        f"the model must actually fail to parse, or this test is vacuous: {proc.stderr[-500:]}"
    )
    assert proc.returncode == 1, proc.stdout[-1000:]
    assert "Generated" not in proc.stdout, "a refused compile must not announce output"
    assert not out.exists(), f"a refused compile wrote {sorted(out.rglob('*'))[:5]}"


def test_ignore_error_re_enables_generation(tmp_path):
    """`--ignore-error` stays the working escape hatch.

    pyang keeps a downgraded tag in `ctx.errors` and only skips printing it, so
    the generator's own guard has to re-apply the same filter. Without that the
    documented opt-out for a vendor model's illegal XPath would be dead.
    """
    yang = tmp_path / "tbroken.yang"
    yang.write_text(BROKEN_YANG)
    out = tmp_path / "downgraded_client"
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; from yang2sdk.cli.compiler import run_compiler;"
                " run_compiler('netconf', sys.argv[1:])"
            ),
            str(yang),
            "--device",
            "tbroken",
            "--yang-dir",
            str(tmp_path),
            "--output-dir",
            str(out),
            "--ignore-error",
            "UNEXPECTED_KEYWORD",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )

    assert proc.returncode == 0, proc.stderr[-1000:]
    assert _pkg_file(out, "__init__.py").is_file()
    assert _pkg_file(out, "data_models").is_dir()


# G30's `coriant-rpc` carries this construct verbatim on the `san` leaf of
# `cert-gen/input`: a negative lookahead. `pattern` is an XML Schema regular
# expression (RFC 7950 Sec 9.4.5), and the XSD grammar has no lookaround, so
# pyang's libxml2 engine rejects the whole model. Python's `re` accepts it, which
# is why the generated client can still carry the constraint -- but only if the
# operator opts in with `--ignore-error PATTERN_ERROR`.
LOOKAHEAD_PATTERN_YANG = """module tpat {
  prefix tp;
  namespace "urn:test:tpat";
  revision 2026-01-01;
  container top {
    leaf san {
      type string {
        // Single quotes: a double-quoted YANG string would additionally earn an
        // ILLEGAL_ESCAPE_WARN for "\\-" and "\\." (RFC 7950 Sec 6.1.3).
        pattern '(IP:|DNS:)(?!x)[A-Za-z0-9\\-\\.:]+';
      }
    }
  }
}
"""

# `\\p{IsBasicLatin}` is valid XML Schema and invalid Python: `re` has no
# `\\p{..}` class, and `_convert_yang_regex` maps only the general categories
# (\\p{L}, \\p{N}, ...). pyang/libxml2 accept this model.
XML_SCHEMA_ONLY_PATTERN_YANG = """module txpat {
  prefix tx;
  namespace "urn:test:txpat";
  revision 2026-01-01;
  container top {
    leaf block {
      type string {
        pattern '\\p{IsBasicLatin}+';
      }
    }
  }
}
"""


def _compile_one_module(
    tmp_path, module: str, source: str, protocol: str = "netconf", *extra: str
):
    """Compile a one-module tree through the same CLI seam as the tests above."""
    yang = tmp_path / f"{module}.yang"
    yang.write_text(source)
    out = tmp_path / f"{module}_{protocol}_client"
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; from yang2sdk.cli.compiler import run_compiler;"
                " run_compiler(sys.argv[1], sys.argv[2:])"
            ),
            protocol,
            str(yang),
            "--device",
            module,
            "--yang-dir",
            str(tmp_path),
            "--output-dir",
            str(out),
            *extra,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    return proc, out


def test_pattern_error_explains_the_xsd_grammar_and_the_escape_hatch(tmp_path):
    """A lookahead pattern must be refused with a reason, not with libxml2's.

    The vendor's own error names a facet on a synthetic schema at "line 6" and
    says nothing about which construct is illegal or what to do. The refusal has
    to carry the RFC, the reason (`(?!...)` has no XSD equivalent), and the exact
    flag -- measured on G30's `coriant-rpc@2021-01-20.yang:533`.
    """
    proc, out = _compile_one_module(tmp_path, "tpat", LOOKAHEAD_PATTERN_YANG)

    assert proc.returncode == 1, proc.stdout[-1000:]
    assert "PATTERN_ERROR" in proc.stderr
    assert "RFC 7950 Sec 9.4.5" in proc.stderr, "the refusal must cite the RFC"
    assert "hint:" in proc.stderr
    assert "--ignore-error PATTERN_ERROR" in proc.stderr
    assert "Generated" not in proc.stdout, "a refused compile must not announce output"
    assert not out.exists(), f"a refused compile wrote {sorted(out.rglob('*'))[:5]}"


def test_pattern_error_escape_hatch_keeps_the_constraint(tmp_path):
    """`--ignore-error PATTERN_ERROR` must not silently drop the pattern.

    pyang only skips *printing* the error; it never builds a pattern spec, so a
    client that read the pattern from `i_type_spec` would lose the constraint.
    The IR reads the `pattern` statement instead, so the emitted validator must
    still contain the vendor's expression -- and be compilable by `re` (the
    emitter now proves that before writing, so this also pins the anchor wrapper
    and the quoting of the emitted literal).
    """
    proc, out = _compile_one_module(
        tmp_path,
        "tpat",
        LOOKAHEAD_PATTERN_YANG,
        "netconf",
        "--ignore-error",
        "PATTERN_ERROR",
    )

    assert proc.returncode == 0, proc.stderr[-1000:]
    assert "Generated" in proc.stdout
    model = _pkg_file(out, "data_models", "tpat.py")
    assert model.is_file(), sorted(out.rglob("*.py"))
    tree = ast.parse(model.read_text(encoding="utf-8"))
    emitted = [
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "check_pattern"
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ]
    assert len(emitted) == 1, emitted
    assert "(?!" in emitted[0], f"the vendor constraint was dropped: {emitted}"
    re.compile(emitted[0])  # raises re.error if the anchor wrapper is wrong


@pytest.mark.parametrize("protocol", ["netconf", "restconf"])
def test_untranslatable_pattern_is_refused_instead_of_shipping_a_late_crash(
    tmp_path, protocol
):
    """A pattern `re` cannot compile must fail here, not in the caller's project.

    Measured before this guard: this exact module generated successfully, the
    package imported cleanly, and `Top(block="abc")` raised
    `re.error: bad escape \\p at position 4` from generated code -- because
    `check_pattern` compiles the regex when a value is assigned, not when the
    module is imported. pyang accepts the model (`\\p{IsBasicLatin}` is valid XML
    Schema), so the PATTERN_ERROR path never sees it.

    Both protocols are checked: each `emit()` builds its own IR list, so a guard
    wired into only one of them would leave the other shipping the crash.
    """
    proc, out = _compile_one_module(
        tmp_path, "txpat", XML_SCHEMA_ONLY_PATTERN_YANG, protocol
    )

    assert "syntax error in pattern" not in proc.stderr, (
        "pyang must accept this pattern, or the test proves nothing about the "
        f"emitter's own guard: {proc.stderr[-800:]}"
    )
    assert proc.returncode == 1, proc.stdout[-1000:]
    assert "cannot be expressed as a Python regular expression" in proc.stderr
    assert "txpat:block" in proc.stderr, "the refusal must name the leaf"
    assert "bad escape" in proc.stderr
    assert "Generated" not in proc.stdout
    assert not out.exists(), f"a refused compile wrote {sorted(out.rglob('*'))[:5]}"


DEVIATION_FOR_XML_SCHEMA_ONLY_PATTERN_YANG = """module txpatdev {
  prefix txd;
  namespace "urn:test:txpatdev";
  import txpat { prefix txpat; }
  revision 2026-01-01;
  deviation "/txpat:top/txpat:block" {
    deviate replace {
      type string {
        pattern '[A-Za-z]+';
      }
    }
  }
}
"""


def test_a_deviation_module_is_the_documented_way_out_of_a_pattern(tmp_path):
    """The refusal's advice must actually work, not just sound plausible.

    `deviate replace` rewrites the `type` statement and the IR reads the
    deviated statement, so the client generates with the replacement pattern --
    which is why the refusal can name `--deviation-module` instead of only
    telling the operator to edit a vendor model they do not own.
    """
    deviation = tmp_path / "txpatdev.yang"
    deviation.write_text(DEVIATION_FOR_XML_SCHEMA_ONLY_PATTERN_YANG)
    proc, out = _compile_one_module(
        tmp_path,
        "txpat",
        XML_SCHEMA_ONLY_PATTERN_YANG,
        "netconf",
        "--deviation-module",
        str(deviation),
    )

    assert proc.returncode == 0, proc.stderr[-1000:]
    assert "Generated" in proc.stdout
    model = _pkg_file(out, "data_models", "txpat.py").read_text(encoding="utf-8")
    assert "check_pattern('^(?:[A-Za-z]+)$'" in model, model[:1000]
    assert "IsBasicLatin" not in model
