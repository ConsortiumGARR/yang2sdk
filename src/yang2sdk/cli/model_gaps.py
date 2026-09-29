"""Model-gap report: does the generated model match the device's feature set?

Zero codegen change. The tool compiles the *same* YANG roots twice, in
separate subprocesses (an in-process pyang run cannot be repeated: the
global optparse registry raises ``OptionConflictError`` on the second run,
see ``tests/test_sdk_generate.py``), and diffs the two *emitted* surfaces:

* **default** — pyang's default: every feature supported.
* **device** — pyang ``--features`` fed from a features file written by
  ``yang-downloader`` (``cli/features.py``).

Surfaces are read back by *importing* the generated packages and
introspecting them, never by scraping text (``-f tree`` is not parsed
anywhere in this repo):

* ``nav_nodes`` — the set of data nodes the generated navigator ``Data``
  graph can address (containers and lists).
* ``model_nodes`` — every data node reachable from ``Data``, leaves
  included, because ``extra="forbid"`` fails on a leaf the model does not
  know.
* ``model_classes`` — flat class inventory, catches dropped/renamed models.

Two directions, both actionable:

* ``missing_from_default`` — the device has it, the default model does not.
  **Read risk**: ``extra="forbid"`` raises on the first such element.
* ``absent_on_device`` — the model has it, the device does not.
  **Write risk**: a merge is rejected or silently ignored.

Usage::

    uv run python -m yang2sdk.cli.model_gaps srl_nokia-system ... \\
        --yang-dir temp/yang_modules/srl-live \\
        --features-file temp/yang_modules/srl-live/features.json \\
        --device srl --json temp/srl-model-gaps.json

Exit codes: ``0`` no read risk, ``1`` ``missing_from_default`` is non-empty,
``2`` operational failure (compile/introspect error).
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from yang2sdk.cli.features import read_features_file, to_pyang_args

#: Introspection runs in its own interpreter: importing a generated package
#: rebuilds every pydantic model, which must not happen in the process that
#: is about to run pyang.
INSPECT_SOURCE = r'''
import importlib, json, pkgutil, sys, types
from pathlib import Path
from typing import Annotated, Union, get_args, get_origin

out_dir = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(out_dir.parent))
pkg = out_dir.name
max_depth = int(sys.argv[2]) if len(sys.argv) > 2 else 64
max_paths = int(sys.argv[3]) if len(sys.argv) > 3 else 200000

nav_pkg = importlib.import_module(pkg + ".data_navigators")
models_pkg = importlib.import_module(pkg + ".data_models")
base = importlib.import_module(pkg + ".data_models._base")
rebuild = base.rebuild_model_and_dependencies

model_classes = set()
ns_to_module = {}
for info in pkgutil.iter_modules(list(models_pkg.__path__)):
    mod = importlib.import_module(pkg + ".data_models." + info.name)
    for name, obj in vars(mod).items():
        if not isinstance(obj, type) or not issubclass(obj, base.NetconfXmlModel):
            continue
        if obj.__module__ != mod.__name__:
            continue
        model_classes.add(info.name + "." + name)
        nsmap = getattr(obj, "__xml_nsmap__", None) or {}
        default_ns = nsmap.get("")
        if default_ns and default_ns not in ns_to_module:
            ns_to_module[default_ns] = info.name

nav_paths = set()
model_paths = set()
config_paths = set()
unresolved = []
broken = []
visited = set()
truncated = False


def class_default_ns(model_cls):
    return (getattr(model_cls, "__xml_nsmap__", None) or {}).get("", "")


def child_model(annot):
    """Unwrap Optional/list/Annotated aliases (same rule as _base._strip_nonconfig_tree)."""
    if annot is None:
        return None
    origin = get_origin(annot)
    if origin in (Union, types.UnionType, list, tuple, set, dict, Annotated):
        for arg in get_args(annot):
            found = child_model(arg)
            if found is not None:
                return found
        return None
    if isinstance(annot, type) and issubclass(annot, base.NetconfXmlModel):
        return annot
    return None


def model_for(navigator):
    """<stem>.<X>Node -> data_models.<stem>.<X> (generator convention)."""
    cls = type(navigator)
    name = cls.__name__
    if not name.endswith("Node"):
        return None
    stem = cls.__module__.rsplit(".", 1)[-1]
    try:
        mod = importlib.import_module(pkg + ".data_models." + stem)
    except Exception:
        unresolved.append(stem + "." + name)
        return None
    model = getattr(mod, name[:-4], None)
    if model is None:
        unresolved.append(stem + "." + name)
    return model


def walk_model(model_cls, path, depth):
    global truncated
    if model_cls is None or depth > max_depth or truncated:
        return
    key = (model_cls.__name__, path)
    if key in visited:
        return
    visited.add(key)
    try:
        rebuild(model_cls)
        fields = model_cls.model_fields
    except Exception:
        unresolved.append("/".join(n for _ns, n in path) + " -> " + model_cls.__name__)
        return
    for field_info in fields.values():
        extra = field_info.json_schema_extra or {}
        tag = extra.get("tag")
        if not tag:
            continue  # attribute fields (nc_operation) carry no element tag
        ns = extra.get("ns") or class_default_ns(model_cls)
        child_path = path + ((ns, tag),)
        model_paths.add(child_path)
        if extra.get("is_config", True):
            config_paths.add(child_path)
        if len(model_paths) > max_paths:
            truncated = True
            return
        nested = child_model(field_info.annotation)
        if nested is not None and nested is not model_cls:
            walk_model(nested, child_path, depth + 1)


def walk(navigator, model_cls, path, depth):
    global truncated
    if depth > max_depth or truncated:
        return
    props = [
        (n, p) for n, p in sorted(vars(type(navigator)).items()) if isinstance(p, property)
    ]
    if not props:
        # ListNode: the item navigator carries the same path, and the item
        # model is what holds the keys and leaves.
        item_cls = getattr(navigator, "_item_cls", None)
        if item_cls is None:
            return
        try:
            item = item_cls(None, list(getattr(navigator, "_path", path)))
        except Exception as exc:
            unresolved.append(
                type(navigator).__name__ + " item -> " + type(exc).__name__ + ": " + str(exc)
            )
            return
        item_path = tuple((ns, tag) for ns, tag, _k in getattr(item, "_path", ())) or path
        item_model = model_for(item)
        walk_model(item_model, item_path, depth)
        walk(item, item_model, item_path, depth + 1)
        return
    for prop_name, prop in props:
        try:
            child = prop.fget(navigator)
        except Exception as exc:
            # A generated property that cannot be constructed (e.g. a
            # class-name collision in the aggregate Data class) is a real
            # finding: record it, keep walking the rest of the surface.
            broken.append(
                f"{type(navigator).__name__}.{prop_name} -> {type(exc).__name__}: {exc}"
            )
            continue
        child_path = tuple((ns, tag) for ns, tag, _k in getattr(child, "_path", ()))
        if not child_path:
            continue
        nav_paths.add(child_path)
        if len(nav_paths) > max_paths:
            truncated = True
            return
        # A ListNode carries no model class of its own (the item model holds
        # the keys/leaves and is resolved in the _item_cls branch below), so
        # asking for one would report a bogus miss on every list.
        child_cls = None if getattr(child, "_item_cls", None) is not None else model_for(child)
        walk_model(child_cls, child_path, depth + 1)
        walk(child, child_cls, child_path, depth + 1)


root = nav_pkg.Data(None, [])
walk(root, None, (), 0)

print(json.dumps({
    "nav_paths": [[[ns, tag] for ns, tag in path] for path in sorted(nav_paths)],
    "model_paths": [[[ns, tag] for ns, tag in path] for path in sorted(model_paths)],
    "config_paths": [[[ns, tag] for ns, tag in path] for path in sorted(config_paths)],
    "model_classes": sorted(model_classes),
    "ns_to_module": ns_to_module,
    "unresolved": sorted(set(unresolved)),
    "broken": sorted(set(broken)),
    "truncated": truncated,
}))
'''


#: A data-node path: ((namespace, node-name), ...) from the datastore root.
NodePath = tuple[tuple[str, str], ...]


def _as_path(raw: list[list[str]]) -> NodePath:
    return tuple((seg[0], seg[1]) for seg in raw)


class ModelGapsError(RuntimeError):
    """Operational failure while compiling or introspecting a variant."""


@dataclass
class Surface:
    """One generated SDK, as seen by importing and introspecting it."""

    nav_paths: set[NodePath] = field(default_factory=set)
    model_paths: set[NodePath] = field(default_factory=set)
    config_paths: set[NodePath] = field(default_factory=set)
    model_classes: set[str] = field(default_factory=set)
    ns_to_module: dict[str, str] = field(default_factory=dict)
    unresolved: list[str] = field(default_factory=list)
    broken: list[str] = field(default_factory=list)
    truncated: bool = False


def _run(
    cmd: Sequence[str], *, what: str, timeout: int
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        list(cmd), capture_output=True, text=True, check=False, timeout=timeout
    )
    if proc.returncode != 0:
        raise ModelGapsError(
            f"{what} failed (exit {proc.returncode}):\n{proc.stderr[-2000:]}"
        )
    return proc


def compile_variant(
    *,
    roots: Sequence[str],
    yang_dir: Path,
    output_dir: Path,
    device: str,
    device_version: str = "",
    config_only: bool = False,
    features_args: Sequence[str] = (),
    ignore_errors: Sequence[str] = (),
    timeout: int = 3600,
) -> None:
    """Compile one variant in a fresh interpreter (pyang is single-run per process)."""
    payload = json.dumps(
        {
            "format_type": "netconf",
            "yang_modules": [str(r) for r in roots],
            "yang_dir": str(yang_dir),
            "output_dir": str(output_dir),
            "device": device,
            "device_version": device_version,
            "config_only": config_only,
            "pyang_features": list(features_args),
            "ignore_errors": list(ignore_errors),
        }
    )
    code = (
        "import json, sys; from yang2sdk.cli.compiler import compile_in_process; "
        "compile_in_process(**json.loads(sys.argv[1]))"
    )
    _run(
        [sys.executable, "-c", code, payload],
        what=f"compile ({output_dir.name})",
        timeout=timeout,
    )


def introspect(
    out_dir: Path, *, max_depth: int = 64, max_paths: int = 200000
) -> Surface:
    """Import the generated package in a subprocess and return its surface."""
    proc = _run(
        [
            sys.executable,
            "-c",
            INSPECT_SOURCE,
            str(out_dir),
            str(max_depth),
            str(max_paths),
        ],
        what=f"introspect ({out_dir.name})",
        timeout=900,
    )
    stdout = proc.stdout.strip()
    data = json.loads(stdout.splitlines()[-1] if stdout else "{}")
    return Surface(
        nav_paths={_as_path(p) for p in data.get("nav_paths", [])},
        model_paths={_as_path(p) for p in data.get("model_paths", [])},
        config_paths={_as_path(p) for p in data.get("config_paths", [])},
        model_classes=set(data.get("model_classes", [])),
        ns_to_module=dict(data.get("ns_to_module", {})),
        unresolved=list(data.get("unresolved", [])),
        broken=list(data.get("broken", [])),
        truncated=bool(data.get("truncated", False)),
    )


def _path_entry(path: NodePath, ns_to_module: dict[str, str]) -> dict[str, object]:
    """One JSON row: namespace-qualified segments plus the rendered path."""
    return {
        "path": _render(path, ns_to_module),
        "segments": [{"ns": ns, "node": tag} for ns, tag in path],
    }


def _sorted(paths: set[NodePath]) -> list[NodePath]:
    return sorted(paths, key=lambda p: (len(p), tuple(name for _ns, name in p)))


def _render(path: NodePath, ns_to_module: dict[str, str]) -> str:
    """Root segment bare; prefix a segment only where the namespace changes."""
    parts: list[str] = []
    prev = path[0][0] if path else ""
    for ns, tag in path:
        module = ns_to_module.get(ns)
        parts.append(f"{module}:{tag}" if ns != prev and module else tag)
        prev = ns
    return "/".join(parts)


def _render_group(
    title: str,
    paths: set[NodePath],
    ns_to_module: dict[str, str],
    max_list: int,
) -> list[str]:
    """List the shallowest paths only when a group is large: a container gap
    subsumes every node under it, so the full list is noise.
    """
    ordered = _sorted(paths)
    lines = [f"{title} [{len(ordered)}]:"]
    if not ordered:
        return lines + ["  (none)"]
    if len(ordered) <= max_list:
        return lines + [f"  {_render(p, ns_to_module)}" for p in ordered]
    shown: list[NodePath] = []
    for path in ordered:
        if not any(
            len(other) < len(path) and path[: len(other)] == other for other in shown
        ):
            shown.append(path)
        if len(shown) >= max_list:
            break
    lines.append(f"  (showing {len(shown)} shallowest of {len(ordered)})")
    lines.extend(f"  {_render(p, ns_to_module)}" for p in shown)
    return lines


@dataclass
class GapReport:
    """Diff of the two surfaces, keyed on ``(namespace, node-name)``."""

    missing_from_default: set[NodePath] = field(default_factory=set)
    absent_on_device: set[NodePath] = field(default_factory=set)
    missing_classes: set[str] = field(default_factory=set)
    absent_classes: set[str] = field(default_factory=set)
    missing_nav: set[NodePath] = field(default_factory=set)
    default_config_paths: set[NodePath] = field(default_factory=set)
    device_config_paths: set[NodePath] = field(default_factory=set)
    ns_to_module: dict[str, str] = field(default_factory=dict)
    truncated: bool = False
    unresolved: list[str] = field(default_factory=list)
    broken: list[str] = field(default_factory=list)
    features: dict[str, list[str]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        """JSON form: full path tuples (namespace kept) plus rendered names."""
        return {
            "summary": {
                "missing_from_default": len(self.missing_from_default),
                "absent_on_device": len(self.absent_on_device),
                "missing_from_default_config": len(
                    self.missing_from_default & self.device_config_paths
                ),
                "absent_on_device_config": len(
                    self.absent_on_device & self.default_config_paths
                ),
                "missing_model_classes": len(self.missing_classes),
                "absent_model_classes": len(self.absent_classes),
                "missing_nav_paths": len(self.missing_nav),
                "unwalkable_navigators": len(self.broken),
                "truncated": self.truncated,
            },
            "features": {m: sorted(fs) for m, fs in self.features.items()},
            "missing_from_default": [
                _path_entry(p, self.ns_to_module)
                for p in _sorted(self.missing_from_default)
            ],
            "absent_on_device": [
                _path_entry(p, self.ns_to_module)
                for p in _sorted(self.absent_on_device)
            ],
            "missing_model_classes": sorted(self.missing_classes),
            "absent_model_classes": sorted(self.absent_classes),
            "missing_nav_paths": [
                _path_entry(p, self.ns_to_module) for p in _sorted(self.missing_nav)
            ],
            "unresolved_navigators": self.unresolved,
            "unwalkable_navigators": self.broken,
        }

    def render(self, *, max_list: int = 40) -> str:
        read_risk = len(self.missing_from_default)
        read_risk_config = len(self.missing_from_default & self.device_config_paths)
        write_risk = len(self.absent_on_device)
        write_risk_config = len(self.absent_on_device & self.default_config_paths)
        out = [
            "model gap report (pyang default features vs device features)",
            f"  read risk  - missing_from_default : {read_risk} (config: {read_risk_config})",
            f"  write risk - absent_on_device    : {write_risk} (config: {write_risk_config})",
            (
                f"  model classes  - missing: {len(self.missing_classes)}"
                f" / absent: {len(self.absent_classes)}"
            ),
            f"  missing navigator paths: {len(self.missing_nav)}",
        ]
        if self.features:
            total = sum(len(fs) for fs in self.features.values())
            out.append(
                "  device features: "
                + ", ".join(
                    f"{m}({len(fs)})" for m, fs in sorted(self.features.items())
                )
                + f" [{total} total]"
            )
        out.append("")
        out.extend(
            _render_group(
                "missing_from_default (read risk)",
                self.missing_from_default,
                self.ns_to_module,
                max_list,
            )
        )
        out.extend(
            _render_group(
                "absent_on_device (write risk)",
                self.absent_on_device,
                self.ns_to_module,
                max_list,
            )
        )
        for title, values in (
            ("missing model classes", self.missing_classes),
            ("absent model classes", self.absent_classes),
        ):
            if values:
                out.append("")
                out.append(f"{title} [{len(values)}]:")
                out.extend(f"  {v}" for v in sorted(values)[:max_list])
        if self.unresolved:
            out.append("")
            out.append(
                f"unresolved navigator->model mappings [{len(self.unresolved)}]:"
            )
            out.extend(f"  {v}" for v in self.unresolved[:max_list])
        if self.truncated:
            out.append("")
            out.append(
                "WARNING: surface truncated (raise --max-paths); counts are partial"
            )
        return "\n".join(out)


def gap_report(
    default: Surface, device: Surface, features: dict[str, list[str]]
) -> GapReport:
    """Diff the two surfaces. `missing_from_default` is the read-risk direction."""
    ns_to_module = {**device.ns_to_module, **default.ns_to_module}
    return GapReport(
        missing_from_default=device.model_paths - default.model_paths,
        absent_on_device=default.model_paths - device.model_paths,
        missing_classes=device.model_classes - default.model_classes,
        absent_classes=default.model_classes - device.model_classes,
        missing_nav=device.nav_paths - default.nav_paths,
        default_config_paths=default.config_paths,
        device_config_paths=device.config_paths,
        ns_to_module=ns_to_module,
        truncated=default.truncated or device.truncated,
        unresolved=sorted(set(default.unresolved) | set(device.unresolved)),
        broken=sorted(set(default.broken) | set(device.broken)),
        features=features,
    )


def run_gap_check(
    *,
    roots: Sequence[str],
    yang_dir: Path,
    features_file: Path,
    device: str,
    device_version: str = "",
    config_only: bool = False,
    ignore_errors: Sequence[str] = (),
    work_dir: Path | None = None,
    keep_work: bool = False,
    max_depth: int = 64,
    max_paths: int = 200000,
    timeout: int = 3600,
) -> GapReport:
    """Compile default + device variants, diff them, clean up unless `keep_work`."""
    features = read_features_file(features_file)
    work = Path(work_dir) if work_dir else Path.cwd() / "temp" / "model_gaps" / device
    default_dir = work / "default"
    device_dir = work / "device"
    for out in (default_dir, device_dir):
        if out.exists():
            shutil.rmtree(out)
    try:
        compile_variant(
            roots=roots,
            yang_dir=yang_dir,
            output_dir=default_dir,
            device=device,
            device_version=device_version,
            config_only=config_only,
            ignore_errors=ignore_errors,
            timeout=timeout,
        )
        compile_variant(
            roots=roots,
            yang_dir=yang_dir,
            output_dir=device_dir,
            device=device,
            device_version=device_version,
            config_only=config_only,
            features_args=to_pyang_args(features),
            ignore_errors=ignore_errors,
            timeout=timeout,
        )
        default_surface = introspect(
            default_dir, max_depth=max_depth, max_paths=max_paths
        )
        device_surface = introspect(
            device_dir, max_depth=max_depth, max_paths=max_paths
        )
    finally:
        if not keep_work:
            for out in (default_dir, device_dir):
                shutil.rmtree(out, ignore_errors=True)
    return gap_report(default_surface, device_surface, features)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Diff a default-feature compile against a device-feature compile.",
    )
    parser.add_argument("yang_modules", nargs="+", help="Root YANG module paths.")
    parser.add_argument("--yang-dir", required=True, help="YANG search dir.")
    parser.add_argument(
        "--features-file",
        required=True,
        help="features.json from yang-downloader (see cli/features.py).",
    )
    parser.add_argument("--device", default="device", help="Device name for packaging.")
    parser.add_argument("--device-version", default="", help="Device OS version.")
    parser.add_argument("--config-only", action="store_true", help="Drop config false.")
    parser.add_argument(
        "--ignore-error",
        dest="ignore_errors",
        action="append",
        default=[],
        metavar="ERROR_TAG",
        help="Downgrade a pyang error tag for both compiles (repeatable).",
    )
    parser.add_argument(
        "--json", dest="json_out", default="", help="Write the full report here."
    )
    parser.add_argument(
        "--work-dir",
        default="",
        help="Compile target (default temp/model_gaps/<device>).",
    )
    parser.add_argument(
        "--keep-work", action="store_true", help="Keep both generated SDKs."
    )
    parser.add_argument(
        "--max-list", type=int, default=40, help="Paths listed per group."
    )
    parser.add_argument("--max-depth", type=int, default=64, help="Walk depth cap.")
    parser.add_argument(
        "--max-paths", type=int, default=200000, help="Node cap per surface."
    )
    parser.add_argument(
        "--timeout", type=int, default=3600, help="Per-compile timeout (s)."
    )
    args = parser.parse_args(argv)

    try:
        report = run_gap_check(
            roots=args.yang_modules,
            yang_dir=Path(args.yang_dir),
            features_file=Path(args.features_file),
            device=args.device,
            device_version=args.device_version,
            config_only=args.config_only,
            ignore_errors=args.ignore_errors,
            work_dir=Path(args.work_dir) if args.work_dir else None,
            keep_work=args.keep_work,
            max_depth=args.max_depth,
            max_paths=args.max_paths,
            timeout=args.timeout,
        )
    except (ModelGapsError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"model-gap check failed: {exc}", file=sys.stderr)
        return 2

    print(report.render(max_list=args.max_list))
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
        print(f"\nfull report: {out}")
    return 1 if report.missing_from_default else 0


if __name__ == "__main__":
    sys.exit(main())
