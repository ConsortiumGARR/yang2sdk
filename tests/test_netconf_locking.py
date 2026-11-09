"""NETCONF locking is re-entrant per session (RFC 6241 Sec 7.5).

Offline (no device, no docker). A small client is generated into
``tmp_path`` and its real ``session_manager`` imported, so the shipped template
is what is pinned.

The regression: ``with NetconfClient(...)`` locks the candidate on entry, and
``plan()`` / ``reconcile()`` locked it again. Sec 7.5 makes the server refuse a
lock already held by *any* session, the requester included, so every plan run
inside a ``with`` block failed with ``lock-denied`` although nobody else held
the lock. The fake manager below enforces exactly that rule.
"""

import importlib
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from lxml import etree  # ty: ignore[unresolved-import] - lxml ships no stubs
from ncclient.operations import RPCError

from yang2sdk import layout

REPO_ROOT = Path(__file__).resolve().parent.parent

YANG = """\
module tlock {
  yang-version 1.1;
  namespace "urn:tlock";
  prefix tlock;

  container system {
    leaf hostname {
      type string;
    }
  }
}
"""

_LOCK_DENIED = (
    b'<rpc-error xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">'
    b"<error-type>protocol</error-type><error-tag>lock-denied</error-tag>"
    b"<error-severity>error</error-severity>"
    b"<error-message>lock is currently held by another entity</error-message>"
    b"</rpc-error>"
)


def _ok():
    return SimpleNamespace(
        ok=True,
        xml='<rpc-reply xmlns="urn:ietf:params:xml:ns:netconf:base:1.0"><ok/></rpc-reply>',
    )


class FakeManager:
    """ncclient manager stub that denies any lock already held (Sec 7.5)."""

    def __init__(self, deny_all: bool = False) -> None:
        self.locked: set[str] = set()
        self.calls: list[tuple[str, str]] = []
        self.deny_all = deny_all

    def lock(self, target):
        self.calls.append(("lock", target))
        if self.deny_all or target in self.locked:
            raise RPCError(etree.fromstring(_LOCK_DENIED))
        self.locked.add(target)
        return _ok()

    def unlock(self, target):
        self.calls.append(("unlock", target))
        self.locked.discard(target)
        return _ok()

    def close_session(self):
        self.calls.append(("close", ""))

    def ops(self, name: str) -> list[tuple[str, str]]:
        return [c for c in self.calls if c[0] == name]


@pytest.fixture(scope="module")
def sm(tmp_path_factory):
    """Generate a real NETCONF client once and return its session_manager."""
    tmp_path = tmp_path_factory.mktemp("tlock")
    (tmp_path / "tlock.yang").write_text(YANG)
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; from yang2sdk.cli.compiler import run_compiler; "
                "run_compiler('netconf', sys.argv[1:])"
            ),
            str(tmp_path / "tlock.yang"),
            "--device",
            "tlock",
            "--yang-dir",
            str(tmp_path),
            "--output-dir",
            str(tmp_path / "tlock_netconf"),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-1500:]
    package = layout.resolve_package(tmp_path / "tlock_netconf")
    sys.path.insert(0, str(package.parent))
    return importlib.import_module(f"{package.name}.session_manager")


@pytest.fixture
def make_client(sm, monkeypatch):
    """A NetconfClient on a FakeManager with the device I/O stubbed out."""
    monkeypatch.setattr(
        sm,
        "build_config",
        lambda diff, ns: etree.fromstring(b'<system xmlns="urn:tlock"/>'),
    )

    def factory(deny_all: bool = False):
        client = object.__new__(sm.NetconfClient)
        fake = FakeManager(deny_all=deny_all)
        client._manager = fake
        client._held_locks = set()
        client.default_target = "candidate"
        client.module_namespaces = {}
        client.has_candidate = True
        client.has_nmda = False
        client.has_validate = True
        client.has_validate_1_1 = False
        client.auto_commit = False

        # Device I/O other than lock/unlock is out of scope here. Each stub
        # asserts the candidate is locked when it runs: staging unlocked is
        # exactly what the lock exists to prevent.
        def locked(result):
            def stub(*args, **kwargs):
                assert "candidate" in fake.locked, "candidate touched while unlocked"
                return result

            return stub

        client._candidate_is_clean = locked(True)
        client.edit = locked(True)
        client.validate = locked(True)
        client.commit = locked(True)
        client.discard_changes = lambda: True
        return client, fake

    return factory


def test_plan_inside_with_reuses_the_session_lock(make_client) -> None:
    client, fake = make_client()
    with client:
        result = client.plan({})
        assert result.ok is True
        # Still held for the rest of the block: plan() must not release the
        # lock it did not take.
        assert "candidate" in fake.locked
        assert fake.ops("unlock") == []
    assert fake.ops("lock") == [("lock", "candidate")]
    assert fake.ops("unlock") == [("unlock", "candidate")]
    assert fake.locked == set()


def test_reconcile_inside_with_reuses_the_session_lock(make_client) -> None:
    client, fake = make_client()
    with client:
        assert client.reconcile({}, commit=False) is True
        assert "candidate" in fake.locked
        assert fake.ops("unlock") == []
    assert fake.ops("lock") == [("lock", "candidate")]
    assert fake.ops("unlock") == [("unlock", "candidate")]


def test_plan_then_reconcile_in_one_with_block(make_client) -> None:
    client, fake = make_client()
    with client:
        assert client.plan({}).ok is True
        assert client.reconcile({}) is True
    assert len(fake.ops("lock")) == 1
    assert len(fake.ops("unlock")) == 1


def test_bare_client_plan_locks_and_releases_itself(make_client) -> None:
    client, fake = make_client()
    assert client.plan({}).ok is True
    assert fake.ops("lock") == [("lock", "candidate")]
    assert fake.ops("unlock") == [("unlock", "candidate")]
    assert fake.locked == set()
    assert client._held_locks == set()


def test_lock_is_idempotent_and_unlock_of_unheld_is_a_noop(make_client) -> None:
    client, fake = make_client()
    assert client.lock() is True
    assert client.lock() is True
    assert len(fake.ops("lock")) == 1
    assert client.unlock() is True
    assert client.unlock() is True
    assert len(fake.ops("unlock")) == 1


def test_failed_enter_lock_sends_no_unlock_on_exit(make_client) -> None:
    client, fake = make_client(deny_all=True)
    with client:
        pass
    assert fake.ops("unlock") == []


def test_plan_still_refuses_when_lock_is_denied(make_client) -> None:
    """Another entity really holds the lock: plan() must fail, not stage."""
    client, fake = make_client(deny_all=True)
    with pytest.raises(RuntimeError, match="lock-denied"):
        client.plan({})
    assert fake.ops("unlock") == []
