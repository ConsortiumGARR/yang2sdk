"""Augment-closure resolver. LAB/DEV helper — no new runtime deps (stdlib only).

Given requested root modules, find every module that augments them
(transitively: augmenters of augmenters), so ``yang2netconf`` compiles the
full closure and pyang expands augments into parents. Without this, the
device returns fully-augmented subtrees while generated models only carry
base children, and strict ``extra="forbid"`` validation rejects replies.

Usage:
    uv run python -m yang2sdk.cli.closure srl_nokia-system srl_nokia-interfaces \\
        --yang-dir temp/yang_modules/srl-live --yang-dir /tmp/srl-yang-offline/... \\
        [--format files|modules]

Provenance: file list should be recorded in MANIFEST alongside revisions.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

# Identifiers may be quoted (RFC 7950 Sec 6.2.1: an unquoted identifier
# cannot start with a digit, so vendors quote machine-generated names). The
# Groove G30 models are written entirely in quoted form
# (`module "ne" {`, `import "shelf" { prefix "shelf"; }`), so every pattern
# must tolerate the quotes or the whole index comes out empty.
_MODULE_RE = re.compile(r'(?m)^\s*module\s+"?([\w-]+)"?\s*\{')
_SELF_PREFIX_RE = re.compile(r'(?m)^\s*prefix\s+"?([\w-]+)"?\s*;')
_IMPORT_RE = re.compile(
    r'import\s+"?([\w-]+)"?\s*\{[^}]*?prefix\s+"?([\w-]+)"?\s*;', re.DOTALL
)
_AUGMENT_RE = re.compile(r'augment\s+"([^"]+)"')
# Cross-module `uses "prefix:grouping";`. The Groove G30 reaches nearly all of
# its data tree this way (one `ne:ne` container + `uses` of groupings exported
# by ~60 modules), so an augment-only closure silently produced a model with
# an empty container.
_USES_RE = re.compile(r'uses\s+"([\w-]+:[\w-]+)"')
_TARGET_PREFIX_RE = re.compile(r"([\w-]+):")


def _parse_yang(path: Path) -> tuple[str | None, dict[str, str], list[str], list[str]]:
    """Return (module, prefix->module map, augment targets, cross-module uses).

    The two reference kinds pull the closure in *opposite* directions, so
    they are kept apart rather than merged:

    * ``uses "p:grouping"`` — the referencing module needs the definition, so
      including it must also include the module it uses (Groove G30: ``ne``
      pulls in ~60 modules this way).
    * ``augment "/p:path"`` — the *augmenting* module is optional; it must be
      included when the module that owns the augmented node is included, so
      pyang expands it into the parent (SR Linux).
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, {}, [], []
    m = _MODULE_RE.search(text)
    module = m.group(1) if m else None
    prefixes: dict[str, str] = {}
    self_p = _SELF_PREFIX_RE.search(text)
    if m and self_p:
        prefixes[self_p.group(1)] = m.group(1)
    for imp_mod, imp_pfx in _IMPORT_RE.findall(text):
        prefixes[imp_pfx] = imp_mod
    return module, prefixes, _AUGMENT_RE.findall(text), _USES_RE.findall(text)


def _index_yang_dirs(
    yang_dirs: list[Path],
) -> dict[str, tuple[Path, dict[str, str], list[str], list[str]]]:
    """Map module name -> (file, prefixes, augment targets, uses). First dir wins."""
    index: dict[str, tuple[Path, dict[str, str], list[str], list[str]]] = {}
    seen_modules: set[str] = set()
    for d in yang_dirs:
        if not d.is_dir():
            continue
        for path in sorted(d.rglob("*.yang")):
            module, prefixes, augments, uses = _parse_yang(path)
            if module is None or module in seen_modules:
                continue
            seen_modules.add(module)
            index[module] = (path, prefixes, augments, uses)
    return index


def resolve_closure(
    root_modules: list[str], yang_dirs: list[Path]
) -> tuple[list[Path], dict[str, list[str]]]:
    """Resolve the transitive augment **and uses** closure over root modules.

    Returns (ordered file list, provenance {included_module: [why-modules]}).
    Fixpoint over two edges:

    * ``augment "/a:b/c"`` — SR Linux: B may augment
      ``/srl-system:system/srl-ssh:ssh-server`` where ``srl-ssh`` itself augments.
    * ``uses "p:grouping"`` — Groove G30: one ``ne:ne`` container pulls in ~60
      modules through groupings.

    ``if-feature``/deviations are recorded, not filtered here; the device
    feature set is applied by pyang at compile time (see ``cli/features.py``).
    """
    # module -> (file, prefixes, augment targets, cross-module uses)
    flat = _index_yang_dirs(yang_dirs)
    included: set[str] = set()
    for root in root_modules:
        if root in flat:
            included.add(root)
    provenance: dict[str, list[str]] = {}
    changed = True
    while changed:
        changed = False
        for mod, (path, prefixes, augments, uses) in flat.items():
            # Edge 1 (uses): including `mod` requires every module whose
            # grouping it instantiates, otherwise pyang cannot resolve the
            # reference and the container comes out empty.
            if mod in included:
                for target in uses:
                    for pfx in _TARGET_PREFIX_RE.findall(target):
                        resolved = prefixes.get(pfx)
                        if resolved and resolved in flat and resolved not in included:
                            included.add(resolved)
                            provenance.setdefault(resolved, []).append(mod)
                            changed = True
                continue
            # Edge 2 (augment): including a module requires every module that
            # augments a node inside it, so pyang expands the augments into
            # the parent (SR Linux).
            hit: list[str] = []
            for target in augments:
                for pfx in _TARGET_PREFIX_RE.findall(target):
                    resolved = prefixes.get(pfx)
                    if resolved in included:
                        hit.append(resolved)
            if hit:
                included.add(mod)
                provenance[mod] = sorted(set(hit))
                changed = True
    ordered = sorted(included)
    files = [flat[mod][0] for mod in ordered if mod in flat]
    missing = [r for r in root_modules if r not in flat]
    if missing:
        raise FileNotFoundError(f"root modules not found in yang dirs: {missing}")
    return files, provenance


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Resolve transitive augment closure for YANG roots."
    )
    parser.add_argument("roots", nargs="+", help="Root module names.")
    parser.add_argument(
        "--yang-dir",
        dest="yang_dirs",
        action="append",
        required=True,
        help="YANG search dir (repeatable, first wins).",
    )
    parser.add_argument(
        "--format",
        choices=["files", "modules"],
        default="files",
        help="Print file paths or module names.",
    )
    args = parser.parse_args(argv)
    files, _prov = resolve_closure(args.roots, [Path(d) for d in args.yang_dirs])
    if args.format == "modules":
        for f in files:
            module, _, _, _ = _parse_yang(f)
            print(module)
    else:
        for f in files:
            print(f)


if __name__ == "__main__":
    main()
