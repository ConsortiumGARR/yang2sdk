"""NETCONF wire logging: every request and every response, redacted.

Offline (no device, no docker). A small NETCONF client is generated once into
``tmp_path`` and its real ``session_manager`` is imported, so these tests pin
the shipped template rather than a copy of it. Transports are stubs: what is
asserted is what gets logged, not what any device answers.

Two invariants carry the safety case, and both are pinned here rather than
trusted to review eyes:

* a password (or passphrase, token, community, private key, ...) never reaches
  a log record in cleartext -- the old per-method ``log_bodies`` lines could
  not promise this, because ``_redact`` truncates XML bytes but redacts only
  dict keys;
* logging is allocation-free when disabled -- serializing multi-megabyte
  replies for nobody to read would tax every production RPC.
"""

import importlib
import inspect
import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from lxml import etree  # ty: ignore[unresolved-import] - lxml ships no stubs
from ncclient.operations import RPCError

from yang2sdk.cli.sdk_verify import (
    Report,
    Verifier,
    _enable_debug_logging,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = (
    REPO_ROOT
    / "src"
    / "yang2sdk"
    / "plugin"
    / "src"
    / "templates"
    / "netconf"
    / "session_manager.py.jinja"
)
LOGGER = "tlog_netconf.session_manager"

YANG = """\
module tlog {
  yang-version 1.1;
  namespace "urn:tlog";
  prefix tlog;

  container system {
    leaf hostname {
      type string;
    }
    leaf admin-password {
      type string;
    }
  }

  list server {
    key "name";
    leaf name {
      type string;
    }
    leaf address {
      type string;
    }
  }

  rpc reboot {
    input {
      leaf delay {
        type uint32;
      }
    }
  }
}
"""


def _ok_reply(inner: str = "<ok/>") -> SimpleNamespace:
    return SimpleNamespace(
        ok=True,
        xml=(
            '<rpc-reply xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">'
            f"{inner}</rpc-reply>"
        ),
    )


@pytest.fixture(scope="module")
def sm(tmp_path_factory):
    """Generate a real NETCONF client once and return its session_manager."""
    tmp_path = tmp_path_factory.mktemp("tlog")
    (tmp_path / "tlog.yang").write_text(YANG)
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; from yang2sdk.cli.compiler import run_compiler; "
                "run_compiler('netconf', sys.argv[1:])"
            ),
            str(tmp_path / "tlog.yang"),
            "--device",
            "tlog",
            "--yang-dir",
            str(tmp_path),
            "--output-dir",
            str(tmp_path / "tlog_netconf"),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-1500:]
    sys.path.insert(0, str(tmp_path))
    return importlib.import_module("tlog_netconf.session_manager")


def _elem() -> etree._Element:
    return etree.fromstring(
        b'<system xmlns="urn:tlog"><hostname>lab-1</hostname>'
        b"<admin-password>s3cr3t!</admin-password>"
        b"<server><name>web</name><address>10.0.0.1</address></server></system>"
    )


def test_redact_xml_redacts_sensitive_leaves(sm) -> None:
    out = sm._redact_xml(_elem())
    assert "s3cr3t!" not in out
    assert "<admin-password>***redacted***</admin-password>" in out
    assert "<hostname>lab-1</hostname>" in out
    assert "<name>web</name>" in out


def test_redact_xml_drops_nested_secret_subtrees(sm) -> None:
    el = etree.fromstring(
        b'<auth xmlns="urn:tlog"><private-key><modulus>AAAA</modulus>'
        b"</private-key></auth>"
    )
    out = sm._redact_xml(el)
    assert "AAAA" not in out and "***redacted***" in out


def test_redact_xml_does_not_mutate_its_input(sm) -> None:
    el = _elem()
    before = etree.tostring(el)
    sm._redact_xml(el)
    assert etree.tostring(el) == before


def test_redact_xml_truncates_huge_bodies(sm) -> None:
    el = etree.fromstring(
        b'<system xmlns="urn:tlog"><banner>' + b"x" * 100_000 + b"</banner></system>"
    )
    out = sm._redact_xml(el)
    assert "...[truncated " in out and "chars]" in out
    assert len(out) < sm._MAX_RPC_LOG_CHARS + 100


def test_handle_rpc_logs_one_request_and_one_reply(sm, caplog) -> None:
    def dispatch(rpc):
        assert rpc.tag.endswith("get-config")
        return _ok_reply(
            '<data><system xmlns="urn:tlog">'
            "<admin-password>s3cr3t!</admin-password>"
            "</system></data>"
        )

    rpc = etree.fromstring(
        b'<get-config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">'
        b"<source><running/></source></get-config>"
    )
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        reply = sm.NetconfClient._handle_rpc(object(), dispatch, rpc)
    assert reply.ok
    debug = [
        r.getMessage()
        for r in caplog.records
        if r.name == LOGGER and r.levelno == logging.DEBUG
    ]
    assert sum(m.startswith("NETCONF request dispatch:") for m in debug) == 1
    replies = [m for m in debug if m.startswith("NETCONF reply dispatch")]
    assert len(replies) == 1 and "bytes" in replies[0]
    info = [
        r.getMessage()
        for r in caplog.records
        if r.name == LOGGER and r.levelno == logging.INFO
    ]
    assert any(m.startswith("NETCONF request:") for m in info)
    assert any(m.startswith("NETCONF response:") for m in info)
    assert "s3cr3t!" not in caplog.text


def test_handle_rpc_names_ncclient_built_envelopes(sm, caplog) -> None:
    """`lock`/`get-config` envelopes are built inside ncclient: log name +
    redacted kwargs (the request semantics) rather than fake envelope bytes."""

    def lock(target=None):
        return _ok_reply()

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        sm.NetconfClient._handle_rpc(object(), lock, target="running")
    debug = [
        r.getMessage()
        for r in caplog.records
        if r.name == LOGGER and r.levelno == logging.DEBUG
    ]
    assert any("lock" in m and "running" in m for m in debug)


def test_handle_rpc_error_keeps_contract_and_request_log(sm, caplog) -> None:
    err = etree.fromstring(
        b'<rpc-error xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">'
        b"<error-tag>invalid-value</error-tag>"
        b"<error-message>bad value</error-message></rpc-error>"
    )

    def commit():
        raise RPCError(err)

    with (
        caplog.at_level(logging.DEBUG, logger=LOGGER),
        pytest.raises(RuntimeError, match="NETCONF RPC Error.*invalid-value"),
    ):
        sm.NetconfClient._handle_rpc(object(), commit)
    debug = [
        r.getMessage()
        for r in caplog.records
        if r.name == LOGGER and r.levelno == logging.DEBUG
    ]
    assert any(m.startswith("NETCONF request commit") for m in debug)
    assert not any(m.startswith("NETCONF reply") for m in debug)


def test_handle_rpc_serializes_nothing_when_debug_off(sm, caplog, monkeypatch) -> None:
    calls: list[int] = []
    real = etree.tostring

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(sm.etree, "tostring", counting)

    def dispatch(rpc):
        return _ok_reply()

    rpc = etree.fromstring(b"<get/>")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        reply = sm.NetconfClient._handle_rpc(object(), dispatch, rpc)
    assert reply.ok and not calls


def test_edit_emits_one_pair_not_two(sm, caplog) -> None:
    """The deleted per-method lines must not survive as duplicates: one
    request + one reply record for the whole edit, whatever the datastore."""

    def dispatch(rpc):
        return _ok_reply()

    self_ns = SimpleNamespace(
        has_nmda=False,
        module_namespaces={},
        default_target="running",
        _mc=SimpleNamespace(dispatch=dispatch),
        auto_commit=False,
        has_rollback_on_error=False,
    )
    # Plain functions stored on a namespace do not bind: close over self.
    self_ns._handle_rpc = lambda func, *a, **k: sm.NetconfClient._handle_rpc(
        self_ns, func, *a, **k
    )
    config = etree.fromstring(
        b'<system xmlns="urn:tlog"><hostname>lab-1</hostname></system>'
    )
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        assert sm.NetconfClient.edit(self_ns, config) is True
    debug = [
        r.getMessage()
        for r in caplog.records
        if r.name == LOGGER and r.levelno == logging.DEBUG
    ]
    assert sum("NETCONF request dispatch:" in m for m in debug) == 1
    assert sum(m.startswith("NETCONF reply dispatch") for m in debug) == 1
    assert "NETCONF edit-config:" not in caplog.text
    assert "NETCONF edit-data:" not in caplog.text


def test_template_keeps_no_per_method_body_logs() -> None:
    """Pin the deletion: request/reply logging lives in `_handle_rpc` only."""
    text = TEMPLATE.read_text()
    for legacy in (
        "NETCONF edit-data:",
        "NETCONF edit-config:",
        "NETCONF validate:",
        "NETCONF rpc:",
    ):
        assert legacy not in text


def test_log_bodies_stays_accepted(sm) -> None:
    """Deprecated alias, not a removed kwarg: older call sites keep working."""
    assert "log_bodies" in inspect.signature(sm.NetconfClient.__init__).parameters


def test_enable_debug_logging_is_idempotent() -> None:
    """One stderr handler however often it runs (--protocol both builds two
    stubs, and reruns must not double-emit). Other loggers are untouched."""
    before = logging.getLogger("tlog unrelated").level
    _enable_debug_logging("tlog-unrelated-stub")
    _enable_debug_logging("tlog-unrelated-stub")
    logger = logging.getLogger("tlog-unrelated-stub")
    assert logger.level == logging.DEBUG
    assert sum(isinstance(h, logging.StreamHandler) for h in logger.handlers) == 1
    assert logging.getLogger("tlog unrelated").level == before


def test_debug_model_preview_redacts_secrets(capsys) -> None:
    """The parsed-model preview shows values (that is its job) but never a
    password in cleartext -- same names as the template redactor."""

    class Model:
        def __init__(self, payload):
            self.payload = payload

        def model_dump(self, **kwargs):
            if "content" in kwargs:
                raise TypeError("RESTCONF-only kwarg")
            return self.payload

    verifier = Verifier(object(), Report(device="d", protocol="netconf"), debug=True)
    verifier._debug_model(Model({"hostname": "lab-1", "admin-password": "s3cr3t!"}))
    out = capsys.readouterr().err
    assert "lab-1" in out
    assert "s3cr3t!" not in out and "***redacted***" in out
    assert "...[truncated " not in out


def test_debug_model_preview_truncates_huge_models(capsys) -> None:
    class Model:
        def model_dump(self, **kwargs):
            if "content" in kwargs:
                raise TypeError("RESTCONF-only kwarg")
            return {"banner": "x" * 5000}

    verifier = Verifier(object(), Report(device="d", protocol="netconf"), debug=True)
    verifier._debug_model(Model())
    out = capsys.readouterr().err
    assert "...[truncated " in out and len(out) < 1500 + 200
