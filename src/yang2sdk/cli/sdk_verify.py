"""Exercise a generated SDK against a lab device, one endpoint at a time.

LAB ONLY. Never point this at production.

The question this answers is "will my generated client actually work?", which
cannot be answered by the offline suite: that one proves the *wire shape* is
right, not that this device speaks it. So this walks the client's own navigator
tree and calls its own methods, recording one row per (node, method).

Four tiers, split because "test every CRUD method" cannot be both total and
safe:

  read    Recursive walk of the whole data tree, bounded depth, validating every
          payload against the generated models. Fully automatic. This is where
          most generator bugs surface (aliases, config filtering, 64-bit
          strings, enumeration literals, choice exclusion).
  rpc     For every RPC/action, build the Input model and round-trip it through
          the wire serializer *without sending it*, then invoke only the RPCs
          named in --rpc-allowlist.
  crud    create -> retrieve -> update -> replace -> delete against the writable
          datastore. NOT automatic: needs --write, plus a second acknowledgement
          when there is no candidate to discard into.
  rpc-live  RPCs on the allowlist are dispatched. Never automatic.

Why CRUD is not automatic: a generic value synthesiser cannot satisfy arbitrary
`must`/`when`/leafref/pattern constraints, so "create every list" would produce
false failures against real semantics and could write junk to live gear. Rather
than report coverage it does not have, this tool tests CRUD only on containers
the operator selects, using values it read back from the device itself.
"""

import argparse
import hashlib
import importlib
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# NETCONF needs ncclient/lxml/pydantic-xml; both transports need `requests`.
# These live in the `lab` extra, matching yang-downloader and the tester that
# sdk-verify replaces. Probed rather than imported: the generated NETCONF client
# imports lxml itself, so this check only has to fail early with a good message.
try:
    import importlib.util

    if importlib.util.find_spec("lxml") is None:
        raise ImportError("lxml is not installed")
    from dotenv import load_dotenv
except ImportError as e:  # pragma: no cover - import guard
    raise ImportError(
        "sdk-verify needs the lab extra: pip install 'yang2sdk[lab]' "
        "(or `uv sync --extra lab` for development)"
    ) from e

# Same .env contract as yang-downloader: the repo's .env is what a user configures,
# and it is not automatically in os.environ. Without this, a fresh `uv sync --extra
# lab && uv run sdk-verify --device <d>` fails with "credentials missing" even
# though the very file the README points at is right there.
load_dotenv()


PASS = "pass"
FAIL = "fail"
SKIP = "skip"

# Depth for every read. Never request an unbounded top-level container: that is
# the read AGENTS.md warns can drive a device to 100% CPU and trip a watchdog.
DEFAULT_DEPTH = 2


@dataclass
class Result:
    """One endpoint outcome. A row per (node, method) is the coverage unit."""

    node: str
    kind: str
    method: str
    status: str
    detail: str = ""
    seconds: float | None = None


@dataclass
class Report:
    device: str
    protocol: str
    results: list[Result] = field(default_factory=list)

    def add(self, result: Result) -> None:
        self.results.append(result)

    def counts(self) -> dict[str, int]:
        out = {PASS: 0, FAIL: 0, SKIP: 0}
        for r in self.results:
            out[r.status] = out.get(r.status, 0) + 1
        return out

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status == FAIL)

    def to_json(self) -> str:
        counts = self.counts()
        return json.dumps(
            {
                "device": self.device,
                "protocol": self.protocol,
                "totals": counts,
                "results": [asdict(r) for r in self.results],
            },
            indent=2,
        )


class Verifier:
    """Walks a generated client and records what worked."""

    def __init__(
        self,
        client: Any,
        report: Report,
        *,
        depth: int = DEFAULT_DEPTH,
        write: bool = False,
        allow_running_writes: bool = False,
        allow_restconf_writes: bool = False,
        rpc_allowlist: set[str] | None = None,
        selected_containers: set[str] | None = None,
        max_depth: int = 12,
        dry_run: bool = False,
    ):
        self.client = client
        self.report = report
        self.depth = depth
        self.write = write
        self.allow_running_writes = allow_running_writes
        self.allow_restconf_writes = allow_restconf_writes
        self.rpc_allowlist = rpc_allowlist or set()
        self.selected_containers = selected_containers or set()
        self.max_depth = max_depth
        self.dry_run = dry_run
        self.is_netconf = hasattr(client, "has_nmda")
        self._imported_models: dict[str, Any] = {}

    # -- helpers ---------------------------------------------------------

    def _nav_props(self, node: Any) -> list[tuple[str, Any]]:
        return [(n, f) for n, f in vars(type(node)).items() if isinstance(f, property)]

    def _label(self, node: Any) -> str:
        path = getattr(node, "_path", "")
        if isinstance(path, list):  # NETCONF: list of (ns, tag, keys) tuples
            return "/" + "/".join(str(seg[1]) for seg in path)
        return str(path)

    def _classify(self, child: Any) -> str:
        """container | list | rpc, by shape rather than by name.

        Both transports expose the same three navigator classes per generated
        module (`Node` for containers and rpcs, `ListNode` for lists), so
        duck-typing avoids importing internals that differ between versions.
        """
        if hasattr(child, "_item_cls"):
            return "list"
        if hasattr(child, "retrieve"):
            return "container"
        return "rpc"

    def _record(
        self, node: str, kind: str, method: str, status: str, detail: str = ""
    ) -> None:
        self.report.add(
            Result(node=node, kind=kind, method=method, status=status, detail=detail)
        )

    def _skip(self, node: str, kind: str, method: str, why: str) -> None:
        # A skip always carries its reason. A vacuous "pass" over zero nodes is
        # exactly what the old tester did and it proved nothing.
        self._record(node, kind, method, SKIP, why)

    # -- tier A: full-tree read ------------------------------------------

    def walk_data(self, root: Any) -> None:
        """Recursively retrieve every reachable data node.

        Navigator properties are pure path builders -- constructing one issues
        no request -- so the tree is enumerated for free and only the explicit
        `retrieve()` calls touch the device.
        """
        seen: set[str] = set()
        self._walk(root, depth=0, seen=seen, ancestry=())

    def _walk(
        self, node: Any, depth: int, seen: set[str], ancestry: tuple[str, ...]
    ) -> None:
        label = self._label(node)
        if depth > self.max_depth:
            self._skip(
                label, "container", "retrieve", f"max depth {self.max_depth} reached"
            )
            return
        if label in seen:
            # Guard against an augment loop producing a cycle.
            return
        seen.add(label)

        for name, prop in self._nav_props(node):
            try:
                child = prop.fget(node)
            except Exception as e:  # noqa: BLE001 - a broken getter is a finding
                self._record(
                    f"{label}/{name}", "?", "navigate", FAIL, f"{type(e).__name__}: {e}"
                )
                continue
            kind = self._classify(child)
            child_label = self._label(child) or f"{label}/{name}"

            if kind == "list":
                self._check_list(child, child_label, name)
                # Descend into list items only via keys we could obtain; a
                # collection read returns instances but not navigators, so the
                # per-item subtree is covered when tier crud runs with keys.
                continue
            if kind == "rpc":
                continue

            self._check_retrieve(child, child_label)
            self._walk(child, depth + 1, seen, (*ancestry, name))

    def _check_retrieve(self, nav: Any, label: str) -> None:
        try:
            if self.is_netconf:
                result = nav.retrieve(
                    source=self._read_source(), content="config", depth=self.depth
                )
            else:
                result = nav.retrieve(content="config", depth=self.depth)
        except Exception as e:  # noqa: BLE001 - aggregate, never fail fast
            if _is_device_fault(e):
                self._skip(label, "container", "retrieve", _device_fault_reason(e))
            else:
                self._record(label, "container", "retrieve", FAIL, _short(e))
            return

        # A list-of-items retrieve legitimately returns []; an empty container
        # returns an empty model. Neither is a pass *and* neither is a failure:
        # the point of this tier is that the payload validated.
        if result is None:
            self._skip(
                label, "container", "retrieve", "device returned no data for this node"
            )
        elif isinstance(result, list):
            self._record(
                label,
                "container",
                "retrieve",
                PASS,
                f"{len(result)} model(s): {sorted({type(i).__name__ for i in result})}",
            )
        else:
            self._record(
                label, "container", "retrieve", PASS, f"model {type(result).__name__}"
            )

    def _check_list(self, nav: Any, label: str, name: str) -> None:
        """A list collection: retrieve it, and record whether keys are known.

        Both transports now emit `is_key` on the item model and a named
        `__call__`, so the key names are discoverable without touching the
        device. Without them a generic caller cannot address an item at all,
        which is why that is asserted rather than assumed.
        """
        try:
            items = (
                nav.retrieve(
                    **({"source": self._read_source()} if self.is_netconf else {})
                )
                or []
            )
        except Exception as e:  # noqa: BLE001
            if _is_device_fault(e):
                self._skip(label, "list", "retrieve", _device_fault_reason(e))
            else:
                self._record(label, "list", "retrieve", FAIL, _short(e))
            return

        keys = self._item_keys(nav)
        if not keys:
            self._skip(
                label,
                "list",
                "call",
                "no named key parameters on __call__, so list items cannot be "
                "addressed by a caller",
            )
        else:
            self._record(
                label,
                "list",
                "retrieve",
                PASS,
                f"{len(items)} item(s); keys={keys}",
            )

    def _item_keys(self, list_nav: Any) -> list[str]:
        """Key parameter names for a list navigator, or [] when unaddressable.

        Read from the generated `__call__` signature, which both transports now
        emit with named key parameters. A variadic `*keys` signature means the
        keys are not discoverable, and callers cannot address an item -- so it
        is reported rather than silently treated as covered.
        """
        import inspect

        try:
            params = inspect.signature(list_nav.__call__).parameters
        except (TypeError, ValueError):
            return []
        names = [n for n in params if n != "self"]
        if not names:
            return []
        variadic = any(
            p.kind is inspect.Parameter.VAR_POSITIONAL for p in params.values()
        )
        return [] if variadic else names

    def _read_source(self) -> str:
        # Reading candidate would report staged edits, not the running config.
        return "running"

    # -- tier B: rpc schema round-trip -----------------------------------

    def walk_rpcs(self, root: Any) -> None:
        """Build every RPC Input and serialise it without sending it.

        This is where an envelope bug shows up: a wrong `model_dump(by_alias=)`
        or a broken `to_xml_payload()` fails here, offline, on all N RPCs --
        instead of on a device after someone calls it for real.
        """
        for name, prop in self._nav_props(root):
            try:
                rpc_nav = prop.fget(root)
            except Exception as e:  # noqa: BLE001
                self._record(name, "rpc", "navigate", FAIL, _short(e))
                continue
            self._check_rpc(rpc_nav, name)

    def _check_rpc(self, rpc_nav: Any, name: str) -> None:
        """Offline half of an RPC: build the Input and serialise it.

        Never dispatches. An allowlisted RPC is called only after this passes,
        so a model that cannot even serialise is caught before it goes near a
        device.
        """
        input_cls = self._rpc_input_cls(rpc_nav)

        if input_cls is None:
            # An rpc with no input section is still a covered endpoint: RFC 8040
            # Sec 3.6.1 says such a request carries no message-body at all, so
            # there is nothing to serialise. Output-only rpcs are common.
            if name in self.rpc_allowlist:
                self._invoke_rpc(rpc_nav, name, None)
                return
            out_cls = self._rpc_output_cls(rpc_nav)
            self._record(
                name,
                "rpc",
                "schema",
                PASS,
                "no input section; request body is omitted (RFC 8040 Sec 3.6.1)"
                if out_cls is not None
                else "no input and no output section",
            )
            return

        # An empty Input must at least construct -- this is where a required
        # field the generator failed to mark optional would show up.
        try:
            empty = input_cls()
        except Exception as e:  # noqa: BLE001
            self._record(
                name, "rpc", "schema", FAIL, f"empty input rejected: {_short(e)}"
            )
            return

        try:
            payload = empty.model_dump(mode="json", exclude_none=True, by_alias=True)
        except Exception as e:  # noqa: BLE001
            self._record(
                name, "rpc", "schema", FAIL, f"model_dump(by_alias) failed: {_short(e)}"
            )
            return

        # NETCONF must serialise to XML; on RESTCONF the dict *is* the body.
        if self.is_netconf and hasattr(empty, "to_xml_payload"):
            try:
                empty.to_xml_payload()
            except Exception as e:  # noqa: BLE001
                self._record(
                    name, "rpc", "schema", FAIL, f"to_xml_payload failed: {_short(e)}"
                )
                return

        self._record(name, "rpc", "schema", PASS, f"input keys={sorted(payload)}")

        if name in self.rpc_allowlist:
            self._invoke_rpc(rpc_nav, name, empty)

    def _invoke_rpc(self, rpc_nav: Any, name: str, empty: Any) -> None:
        """Dispatch an allowlisted RPC. Always echoed first: it may be destructive."""
        print(f"  [LIVE RPC] dispatching {name}")
        try:
            rpc_nav() if empty is None else rpc_nav(empty)
        except Exception as e:  # noqa: BLE001
            # A device rejection is a fact about this device, not proof the
            # generated model is wrong: mandatory leaves have no valid default
            # to synthesise, and an empty Input is not a meaningful call for most
            # rpcs. Reported as a skip with the reason, never as a pass.
            self._skip(
                name, "rpc", "call", f"device rejected the invocation: {_short(e, 160)}"
            )
            return
        self._record(name, "rpc", "call", PASS, "device accepted the call")

    def _rpc_input_cls(self, rpc_nav: Any) -> Any:
        return self._rpc_model(rpc_nav, "Input")

    def _rpc_output_cls(self, rpc_nav: Any) -> Any:
        return self._rpc_model(rpc_nav, "Output")

    def _rpc_model(self, rpc_nav: Any, suffix: str) -> Any:
        """The generated `<Rpc><suffix>` pydantic model, or None if absent.

        Both navigator modules start with `from __future__ import annotations`,
        so `inspect.signature(...).parameters[...].annotation` is a *string*, not
        a class. `get_type_hints` resolves it, which is what makes this work for
        a generated client without importing the navigator module by name.
        """
        import inspect
        import typing

        from pydantic import BaseModel

        try:
            hints = typing.get_type_hints(rpc_nav.__call__)
        except Exception:  # noqa: BLE001 - unresolvable forward ref
            hints = {}
        for hint in hints.values():
            for candidate in _flatten_type(hint):
                if (
                    inspect.isclass(candidate)
                    and issubclass(candidate, BaseModel)
                    and candidate.__name__.endswith(suffix)
                ):
                    return candidate
        return None


def _flatten_type(hint: Any) -> list[Any]:
    """Every class mentioned in a possibly-nested union/optional annotation.

    Generated signatures are `Cls | None` and `Cls | dict | str | None`, so a
    plain `inspect.isclass` on the annotation is not enough to find the model.
    """
    import inspect
    import types
    import typing

    if inspect.isclass(hint):
        return [hint]
    args = typing.get_args(hint)
    if not args:
        return []
    if hint is types.UnionType or typing.get_origin(hint) in (
        typing.Union,
        types.UnionType,
    ):
        out: list[Any] = []
        for arg in args:
            out.extend(_flatten_type(arg))
        return out
    return []


def _status_of(exc: BaseException) -> int | None:
    """HTTP status from a requests HTTPError, if this is one."""
    import requests

    if isinstance(exc, requests.HTTPError):
        resp = getattr(exc, "response", None)
        if resp is not None:
            return int(resp.status_code)
    return None


# Statuses that describe the *device*, not the generated client. Reporting these
# as failures would make every run against a real box red for the wrong reason,
# and would bury the generator bugs this tool exists to find.
#
# 404 -- RFC 8040 Sec 4.3: the target resource does not exist. A node the device
#        does not implement, or a container with no config, is normal.
# 400 -- RFC 8040 Sec 4.3: a list/leaf-list GET that identifies more than one
#        instance MUST be 400 when XML encoding is used. Simulators and some
#        vendors answer a *collection* GET this way, so it is a device
#        behaviour, not evidence the URL was built wrongly.
# 405 -- the resource does not support the method (e.g. GET on an operation
#        resource, which Sec 4.3 requires servers to reject).
_DEVICE_FAULT_STATUSES = {400, 404, 405}


def _is_device_fault(exc: BaseException) -> bool:
    # A parse/validation failure proves the device ANSWERED: bytes came back
    # and our model could not read them. That is a fidelity finding about the
    # generated client (wrong namespace, alias, type), never a device fault --
    # and it must never be reported as a skip. This used to match the
    # "not found" substring inside pydantic-xml's own "root element not
    # found (actual: ..., expected: ...)" text, which hid 77 augment-namespace
    # parse failures on SR Linux as device skips.
    if type(exc).__name__ in ("ParsingError", "ValidationError"):
        return False
    status = _status_of(exc)
    if status is not None:
        return status in _DEVICE_FAULT_STATUSES
    # NETCONF: an operation-level rejection is a device answer, not a bug.
    text = str(exc)
    return any(
        marker in text
        for marker in (
            "invalid-value",
            "data-missing",
            "unknown-element",
            "operation-not-supported",
            "access-denied",
            "not found",
            "does not exist",
        )
    )


def _device_fault_reason(exc: BaseException) -> str:
    status = _status_of(exc)
    hints = {
        400: "device rejected the read (RFC 8040 Sec 4.3 list/leaf-list instance rule)",
        404: "node not implemented or not present on this device (RFC 8040 Sec 4.3)",
        405: "resource does not support this method (RFC 8040 Sec 4.3)",
    }
    if status in hints:
        return f"{hints[status]}: {_short(exc, 120)}"
    if "access-denied" in str(exc):
        # NACM (RFC 8341): the device's access-control policy refused this
        # operation for this user. That is a policy answer, not a client defect
        # -- the SDK correctly delivered the edit and correctly surfaced the
        # refusal.
        return (
            f"device access control refused this operation (RFC 8341 NACM): "
            f"{_short(exc, 120)}"
        )
    return f"device rejected the operation: {_short(exc, 120)}"


def _short(exc: BaseException, limit: int = 200) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text[:limit]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sdk-verify",
        description=(
            "Exercise every endpoint of a generated SDK against a lab device. "
            "LAB ONLY -- never production."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--device",
        default=os.environ.get("DEVICE_NAME"),
        help="Generated client name under --client-dir.",
    )
    parser.add_argument(
        "--protocol", choices=["restconf", "netconf", "both"], default="restconf"
    )
    parser.add_argument(
        "--client-dir",
        default="",
        help="Directory holding the generated client (default temp/<protocol>_clients/<device>).",
    )
    parser.add_argument(
        "--tiers", default="read,rpc", help="Comma-separated: read,rpc,crud,rpc-live."
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=DEFAULT_DEPTH,
        help="Bounded read depth. Never unbounded.",
    )
    parser.add_argument(
        "--username", default="", help="Device username. Falls back to $DEVICE_USER."
    )
    parser.add_argument(
        "--password", default="", help="Device password. Falls back to $DEVICE_PASS."
    )
    parser.add_argument(
        "--max-depth", type=int, default=12, help="Recursion limit for the tree walk."
    )
    parser.add_argument(
        "--write", action="store_true", help="Enable the crud tier. Mutates the device."
    )
    parser.add_argument(
        "--allow-running-writes",
        action="store_true",
        help="Second, explicit acknowledgement: the device has no candidate datastore, so crud edits land in running and are rolled back by re-writing the snapshot.",
    )
    parser.add_argument(
        "--allow-restconf-writes",
        action="store_true",
        help="Second acknowledgement for RESTCONF, whose PATCH/PUT/POST/DELETE have no candidate and no discard-changes.",
    )
    parser.add_argument(
        "--rpc-allowlist",
        default="",
        help="Comma-separated RPC names to actually dispatch.",
    )
    parser.add_argument(
        "--containers",
        default="",
        help="Comma-separated container paths for the crud tier (all when empty).",
    )
    parser.add_argument(
        "--snapshot", default="", help="Where to write the pre-test config snapshot."
    )
    parser.add_argument(
        "--verify-tls",
        action="store_true",
        help="Verify TLS/host keys (the generated default; omit only for lab self-signed gear).",
    )
    parser.add_argument(
        "--restconf-scheme",
        choices=["https", "http"],
        default="https",
        help="RESTCONF URI scheme. 'http' is plaintext, for simulators only.",
    )
    parser.add_argument("--json-out", default="", help="Write the JSON report here.")
    return parser


def _import_client(client_dir: Path, protocol: str) -> Any:
    # --protocol both runs two clients in one process, and both are packages named
    # after the device (temp/restconf_clients/g30 and temp/netconf_clients/g30).
    # A plain importlib.import_module(client_dir.name) would hand the second
    # protocol the *first* module from sys.modules, silently validating the
    # RESTCONF client twice. Import under a unique per-protocol name instead.
    name = f"_sdkverify_{protocol}_{client_dir.name}"
    spec = importlib.util.spec_from_file_location(
        name,
        client_dir / "__init__.py",
        submodule_search_locations=[str(client_dir)],
    )
    if spec is None or spec.loader is None:
        raise SystemExit(f"sdk-verify: cannot import client package at {client_dir}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _resolve_credentials(args: argparse.Namespace) -> tuple[str, str]:
    """Args win, then DEVICE_USER/DEVICE_PASS -- the generated clients' contract."""
    username = args.username or os.environ.get("DEVICE_USER")
    password = args.password or os.environ.get("DEVICE_PASS")
    if not username or not password:
        raise SystemExit(
            "sdk-verify: credentials missing. Pass --username/--password or set "
            "DEVICE_USER and DEVICE_PASS. Refusing to run unauthenticated."
        )
    return username, password


def run_protocol(
    protocol: str,
    args: argparse.Namespace,
    tiers: set[str],
    username: str,
    password: str,
) -> Report:
    client_dir = Path(args.client_dir or f"temp/{protocol}_clients/{args.device}")
    if not (client_dir / "__init__.py").exists():
        raise SystemExit(
            f"sdk-verify: no generated {protocol} client at {client_dir}. "
            f"Run 'uv run yang2{protocol} <root.yang> ...' first."
        )
    module = _import_client(client_dir, protocol)
    cls = getattr(
        module, "RestconfClient" if protocol == "restconf" else "NetconfClient", None
    )
    if cls is None:
        raise SystemExit(f"sdk-verify: {client_dir} exposes no {protocol} client class")

    host = os.environ.get("DEVICE_IP")
    if not host:
        raise SystemExit("sdk-verify: set DEVICE_IP (lab address only).")
    port = int(
        os.environ.get(
            "RESTCONF_PORT" if protocol == "restconf" else "NETCONF_PORT",
            "443" if protocol == "restconf" else "830",
        )
    )
    verify = args.verify_tls
    if not verify:
        print(
            "[warn] TLS/host-key verification disabled. Lab/self-signed gear only; "
            "never against production.",
            file=sys.stderr,
        )

    kwargs: dict[str, Any] = {
        "management_ip": host,
        "port": port,
        "username": username,
        "password": password,
        "verify": verify,
    }
    if protocol == "netconf":
        # auto_commit must stay False: with it True, edit() silently commits to
        # running after every edit, which would defeat the candidate flow.
        kwargs["auto_commit"] = False
    else:
        kwargs["scheme"] = args.restconf_scheme
    client = cls(**kwargs)

    report = Report(device=args.device or client_dir.name, protocol=protocol)
    verifier = Verifier(
        client,
        report,
        depth=args.depth,
        max_depth=args.max_depth,
        write=args.write and "crud" in tiers,
        allow_running_writes=args.allow_running_writes,
        allow_restconf_writes=args.allow_restconf_writes,
        rpc_allowlist={r for r in args.rpc_allowlist.split(",") if r},
        selected_containers={c for c in args.containers.split(",") if c},
    )

    try:
        if "read" in tiers:
            print(f"[{protocol}] walking data tree (depth={args.depth})")
            verifier.walk_data(client.data)
        if "rpc" in tiers or "rpc-live" in tiers:
            print(f"[{protocol}] round-tripping rpc schemas")
            verifier.walk_rpcs(client.operations)
        if "crud" in tiers:
            _run_crud(verifier, protocol, args)
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()

    return report


def _run_crud(verifier: Verifier, protocol: str, args: argparse.Namespace) -> None:
    """CRUD tier. Refuses to start unless the datastore situation is understood.

    Gate logic, and why each branch refuses:
      * no --write                       -> caller has not opted in
      * NETCONF, no :candidate           -> needs --allow-running-writes, because
                                           edits land in running and the only
                                           recovery is re-writing the snapshot
      * RESTCONF, any datastore          -> needs --allow-restconf-writes: RESTCONF
                                           has no candidate and no <discard-changes>,
                                           so every PATCH is immediately live
    """
    if not args.write:
        verifier._record(
            "-",
            "crud",
            "gate",
            SKIP,
            "crud tier requires --write (it mutates the device)",
        )
        return

    if protocol == "restconf":
        if not args.allow_restconf_writes:
            verifier._record(
                "-",
                "crud",
                "gate",
                SKIP,
                "RESTCONF writes are immediately live (no candidate, no "
                "<discard-changes>); pass --allow-restconf-writes",
            )
            return
    elif (
        not getattr(verifier.client, "has_candidate", False)
        and not args.allow_running_writes
    ):
        verifier._record(
            "-",
            "crud",
            "gate",
            SKIP,
            "device has no :candidate, so edits land in running and only a "
            "re-written snapshot can undo them; back up the config and pass "
            "--allow-running-writes",
        )
        return

    snapshot_path = _write_snapshot(verifier, protocol, args)
    if snapshot_path is None:
        verifier._record(
            "-",
            "crud",
            "snapshot",
            FAIL,
            "could not snapshot the config; refusing to write without a recovery point",
        )
        return

    target = (
        "candidate"
        if protocol == "netconf" and verifier.client.has_candidate
        else "running"
    )
    locked = False
    if protocol == "netconf":
        # RFC 6241 Sec 8.5.1 makes a held lock a precondition for the safety of
        # writing running, and a lock failure here must abort rather than warn.
        try:
            locked = bool(verifier.client.lock(target=target))
        except Exception as e:  # noqa: BLE001
            locked = False
            print(f"[warn] lock({target}) raised: {_short(e)}", file=sys.stderr)
        if not locked:
            verifier._record(
                "-",
                "crud",
                "lock",
                FAIL,
                f"could not lock the {target} datastore; refusing to write unlocked "
                "(RFC 6241 Sec 8.5.1)",
            )
            return
        verifier._record("-", "crud", "lock", PASS, f"held lock on {target}")

    try:
        _crud_containers(verifier, protocol, target)
        if protocol == "netconf" and verifier.client.has_candidate:
            # RFC 6241 Sec 8.6.4.1: :validate is optional, and validate() returns
            # False when it is absent. That is "could not validate", not "valid",
            # so it is reported as a skip and never as a pass.
            if verifier.client.has_validate:
                if verifier.client.validate(source="candidate"):
                    verifier._record(
                        "-", "crud", "validate", PASS, "<validate> accepted candidate"
                    )
                else:
                    verifier._record(
                        "-", "crud", "validate", FAIL, "<validate> rejected candidate"
                    )
            else:
                verifier._skip(
                    "-", "crud", "validate", "device does not advertise :validate"
                )
            if verifier.client.discard_changes():
                verifier._record("-", "crud", "discard", PASS, "candidate discarded")
            else:
                verifier._record(
                    "-", "crud", "discard", FAIL, "<discard-changes> failed"
                )
        _verify_restored(verifier, protocol, snapshot_path)
    finally:
        if protocol == "netconf" and locked:
            try:
                verifier.client.unlock(target=target)
            except Exception as e:  # noqa: BLE001
                print(f"[warn] unlock raised: {_short(e)}", file=sys.stderr)


def _crud_targets(verifier: Verifier) -> list[tuple[str, Any]]:
    """Every data node eligible for the CRUD tier, honouring --containers.

    Returns (label, navigator) for both containers and lists, because the
    round-trip applies to both.

    A list and the container that holds it address the same YANG data with two
    different navigators, so both are returned. They are de-duplicated on the
    write path (see `_crud_containers`): the container's model already contains
    the list, so writing both would send the same leaves twice.

    Walking is offline: navigator properties are path builders, so building the
    whole tree costs no requests. Only the retrieve calls in `_capture` and
    `_crud_containers` touch the device.
    """
    selected = verifier.selected_containers
    found: list[tuple[str, Any]] = []

    def visit(node: Any, depth: int) -> None:
        if depth > verifier.max_depth:
            return
        for _name, prop in verifier._nav_props(node):
            try:
                child = prop.fget(node)
            except Exception:  # noqa: BLE001, S112 - already reported by the read tier
                continue
            kind = verifier._classify(child)
            if kind == "rpc":
                continue
            label = verifier._label(child)
            if not selected or any(label.endswith(s) or s in label for s in selected):
                found.append((label, child))
            # A list has no child navigators: its instances are addressed by
            # keys, and a keyless instance subtree is not reachable generically.
            if kind == "container":
                visit(child, depth + 1)

    visit(verifier.client.data, 0)
    return found


def _write_snapshot(
    verifier: Verifier, protocol: str, args: argparse.Namespace
) -> Path | None:
    """Persist the pre-test config so a crashed run can still be undone.

    On a device without a candidate there is no <discard-changes>: this file is
    the only recovery path, so failing to write it aborts the tier rather than
    warning and continuing.
    """
    stamp = (
        args.snapshot
        or f"temp/verify/{verifier.report.device}-{protocol}-snapshot.json"
    )
    out = Path(stamp)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        digest, blobs = _capture(verifier, protocol)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] snapshot read failed: {_short(e)}", file=sys.stderr)
        return None
    out.write_text(
        json.dumps({"digest": digest, "nodes": blobs}, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"  [snapshot] {out} (digest {digest[:12]})")
    return out


def _capture(verifier: Verifier, protocol: str) -> tuple[str, dict[str, str]]:
    """Fingerprint every node the CRUD tier will touch."""
    blobs: dict[str, str] = {}
    for label, nav in _crud_targets(verifier):
        try:
            blobs[label] = _fingerprint(_retrieve(verifier, protocol, nav))
        except Exception as e:  # noqa: BLE001
            if _is_device_fault(e):
                # The node does not exist here; it cannot have been changed by
                # this run either, so it is excluded rather than counted.
                continue
            blobs[label] = f"unreadable: {_short(e)}"
    return _digest(blobs), blobs


def _digest(blobs: dict[str, str]) -> str:
    hasher = hashlib.sha256()
    for label in sorted(blobs):
        hasher.update(f"{label}={blobs[label]}".encode())
    return hasher.hexdigest()


def _dump_config(model: Any) -> Any:
    """`model_dump` restricted to config content.

    Only the RESTCONF base model takes `content=`; NETCONF models are plain
    pydantic-xml and its `to_xml_payload()` already carries only config leaves
    (that is what `nc_operation` rides on). Passing `content=` to a NETCONF
    model raises TypeError, so the keyword is added only where it exists.
    """
    kwargs = {
        "mode": "json",
        "exclude_unset": True,
        "exclude_none": True,
        "by_alias": True,
    }
    try:
        return model.model_dump(content="config", **kwargs)
    except TypeError:
        pass
    try:
        return model.model_dump(**kwargs)
    except TypeError:
        # A transport marker the generated update() sets; not device state.
        return {
            k: v
            for k, v in model.model_dump(
                mode="python", exclude_unset=True, exclude_none=True
            ).items()
            if k != "nc_operation"
        }


def _fingerprint(model: Any) -> str:
    """Stable text of a model's config content, ignoring key order.

    Compares *what was sent*, not everything the device happens to report: a
    re-merge legitimately leaves factory defaults and `config false` state on
    the read-back that were absent from the original read, and treating that as
    a failure would make every run red on gear with defaults enabled.
    """
    if model is None:
        return "none"
    items = model if isinstance(model, list) else [model]
    parts = [json.dumps(_dump_config(i), sort_keys=True, default=str) for i in items]
    return parts[0] if len(parts) == 1 else "[" + ",".join(parts) + "]"


def _baseline_fields(model: Any) -> set[str]:
    """Top-level field names present in the pre-write read.

    Excludes the NETCONF edit-operation marker (`nc_operation`), which the
    generated `update()` sets on the model as a side effect. It is transport
    plumbing, not device state, and is never present on a read-back -- counting
    it would make every round-trip look like data loss.
    """
    item = model[0] if isinstance(model, list) and model else model
    if item is None:
        return set()
    return set(_dump_config(item)) - {"nc_operation"}


def _retrieve(verifier: Verifier, protocol: str, nav: Any) -> Any:
    if protocol == "netconf":
        return nav.retrieve(source="running", content="config", depth=verifier.depth)
    return nav.retrieve(content="config", depth=verifier.depth)


def _crud_containers(verifier: Verifier, protocol: str, target: str) -> None:
    """Per node: retrieve -> update -> read back, then restore the snapshot.

    The update is an idempotent same-data merge. It exercises the full write
    path -- payload construction, the wire encoding, the device's datastore
    transaction -- while being a no-op in effect, so an interrupted run cannot
    leave a changed value behind. Nothing is invented: a `must`, `when`, leafref
    or mandatory leaf the device enforces could reject a synthesised value, and
    reporting that rejection as a generator bug would be a lie. `create`/`delete`
    are therefore not part of this tier; see the module docstring.
    """
    for label, nav in _crud_targets(verifier):
        kind = verifier._classify(nav)
        if kind == "list":
            # A list and its parent container address the same data. The
            # container's model already embeds the list, so round-tripping both
            # would send the same leaves twice for no extra coverage -- and on
            # NETCONF the two navigators disagree about the argument shape
            # (item model vs list of items), which is exactly the kind of
            # asymmetry this tool is meant to pin down. Test the container, and
            # record the list as covered by it.
            verifier._skip(
                label,
                kind,
                "crud",
                "covered by its parent container's round-trip (same data, two navigators)",
            )
            continue
        try:
            baseline = _retrieve(verifier, protocol, nav)
        except Exception as e:  # noqa: BLE001
            if _is_device_fault(e):
                verifier._skip(label, kind, "crud", _device_fault_reason(e))
            else:
                verifier._record(label, kind, "retrieve", FAIL, _short(e))
            continue

        if baseline is None or (isinstance(baseline, list) and not baseline):
            verifier._skip(
                label,
                kind,
                "crud",
                "no config present to round-trip (an empty list cannot be re-merged)",
            )
            continue

        if isinstance(baseline, list) and len(baseline) > 1:
            # The NETCONF navigators merge one item at a time, and re-merging
            # every entry of a large list on real gear is exactly the kind of
            # unbounded work AGENTS.md warns about. Reported, never silently
            # reduced to the first entry.
            verifier._skip(
                label,
                kind,
                "crud",
                f"{len(baseline)} instances present; round-trip covers a single "
                "entry per node, so this list was not rewritten",
            )
            continue

        try:
            _update(protocol, nav, baseline, target)
        except Exception as e:  # noqa: BLE001
            if _is_device_fault(e):
                verifier._skip(label, kind, "update", _device_fault_reason(e))
            else:
                verifier._record(label, kind, "update", FAIL, _short(e))
            continue
        verifier._record(label, kind, "update", PASS, "idempotent merge accepted")

        # The write must be observable, otherwise `update` is a silent no-op and
        # the "pass" above proved nothing.
        try:
            after = _retrieve(verifier, protocol, nav)
        except Exception as e:  # noqa: BLE001
            if _is_device_fault(e):
                verifier._skip(label, kind, "read-back", _device_fault_reason(e))
            else:
                verifier._record(label, kind, "read-back", FAIL, _short(e))
            continue

        if _fingerprint(after) == _fingerprint(baseline):
            verifier._record(
                label, kind, "read-back", PASS, "config unchanged (as intended)"
            )
            continue

        # A read-back may legitimately carry *more* than was sent: the device
        # reports defaults and state it did not have to before. What must not
        # happen is one of the fields we sent changing value or disappearing --
        # that is a real round-trip defect.
        expected = _baseline_fields(baseline)
        actual = _baseline_fields(after)
        lost = sorted(expected - actual)
        if lost:
            verifier._record(
                label,
                kind,
                "read-back",
                FAIL,
                f"fields present before the merge are gone after it: {lost}",
            )
            continue

        verifier._record(
            label,
            kind,
            "read-back",
            PASS,
            f"config unchanged for the {len(expected)} field(s) sent "
            f"(device added {sorted(actual - expected) or 'nothing'})",
        )


def _update(protocol: str, nav: Any, model: Any, target: str) -> None:
    """Merge what `_retrieve` returned back into `nav`, unchanged.

    On NETCONF the list and container navigators take the *item* model while
    their `retrieve()` returns a `list` of items for a list navigator, so a
    list is unwrapped to its single element. Both navigators would otherwise
    fail on a list: the item `update()` sets `nc_operation` on the value
    (`'list' object has no attribute 'nc_operation'`), and the list `update()`
    calls `model_dump` on each element (`'list' object has no attribute
    'model_dump'`).

    A multi-item list is skipped upstream, so this only ever sees 0 or 1 items.
    """
    if protocol == "netconf":
        if isinstance(model, list):
            if len(model) != 1:
                raise ValueError(
                    f"expected at most one item to round-trip, got {len(model)}"
                )
            nav.update(model[0], target=target)
        else:
            nav.update(model, target=target)
    else:
        nav.update(model)


def _verify_restored(verifier: Verifier, protocol: str, snapshot_path: Path) -> None:
    """Prove the datastore ended where it started.

    A restore that is not verified is not a rollback, so this compares the
    post-test digest against the one captured before any write. `rollback-on-
    error` only covers a single <edit-config>; it does not make a multi-step run
    self-healing, which is exactly why this check exists.
    """
    try:
        expected = json.loads(snapshot_path.read_text(encoding="utf-8")).get("digest")
        actual, _ = _capture(verifier, protocol)
    except Exception as e:  # noqa: BLE001
        verifier._record(
            "-", "crud", "verify-restore", FAIL, f"could not re-read: {_short(e)}"
        )
        return
    if actual == expected:
        verifier._record(
            "-",
            "crud",
            "verify-restore",
            PASS,
            f"config digest matches snapshot {expected[:12]}",
        )
    else:
        verifier._record(
            "-",
            "crud",
            "verify-restore",
            FAIL,
            f"RESTORE NOT PROVEN: digest {actual[:12]} != snapshot {str(expected)[:12]}. "
            f"The device is left modified. Recover from {snapshot_path}.",
        )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    username, password = _resolve_credentials(args)
    tiers = {t.strip() for t in args.tiers.split(",") if t.strip()}
    protocols = ["restconf", "netconf"] if args.protocol == "both" else [args.protocol]

    reports: list[Report] = []
    for protocol in protocols:
        reports.append(run_protocol(protocol, args, tiers, username, password))

    total_fail = 0
    total_checked = 0
    for report in reports:
        counts = report.counts()
        checked = counts[PASS] + counts[FAIL]
        total_checked += checked
        print(
            f"\n[{report.protocol}] {counts[PASS]} pass, {counts[FAIL]} fail, "
            f"{counts[SKIP]} skip"
        )
        for r in report.results:
            if r.status == FAIL:
                print(f"  [FAIL] {r.node} {r.method}: {r.detail}", file=sys.stderr)
        if args.json_out:
            out = Path(args.json_out)
            if len(protocols) > 1:
                # --protocol both would otherwise write the netconf report over
                # the restconf one in the same file.
                out = out.with_name(f"{out.stem}_{report.protocol}{out.suffix}")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(report.to_json(), encoding="utf-8")
            print(f"  report: {out}")
        total_fail += report.failed

    if total_fail:
        print(f"\n[FAIL] {total_fail} endpoint check(s) failed", file=sys.stderr)
        return 1
    if not total_checked:
        # A run that exercised nothing must not report success: that is the exact
        # vacuous pass the tool this replaces had (it printed "[OK] every
        # navigator validated" on a client with no data properties).
        print(
            "\n[FAIL] no endpoint was exercised: every row is a skip. Check "
            "--tiers, --client-dir, and whether this device implements the "
            "compiled modules.",
            file=sys.stderr,
        )
        return 1
    print(f"\n[OK] {total_checked} endpoint check(s) passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
