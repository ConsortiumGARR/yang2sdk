"""Interactive selection of root YANG modules.

`run_compiler` needs *root* modules -- the ones you name on the command line --
and pyang expands an `augment` only when the augmenting module is also named
(RFC 7950 Sec 7.17). Picking those roots was the last manual step in the
pipeline, and it was a bad one: a wrong choice emits a model that parses,
imports, and then rejects the device's own replies because `extra="forbid"`
refuses the augmented nodes it never learned about.

So this proposes them instead of asking for them. A module is a *candidate* when
no other module in the directory imports it and nothing augments into it --
the modules that stand at the top of the dependency graph.

A candidate list is a suggestion, not an answer. Vendors ship models that are
perfectly good roots precisely because something else imports them, so the
prompt accepts anything the operator types and never silently narrows.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .closure import _index_yang_dirs

#: How many candidates to show before collapsing the rest behind a filter hint.
PAGE_SIZE = 24


@dataclass(frozen=True)
class Candidate:
    """One proposed root module."""

    module: str
    path: Path
    revision: str = ""
    top_level: tuple[str, ...] = ()
    #: Top-level rpc/action names. A module whose whole surface is operations
    #: declares no data nodes at all, so counting only data would drop it.
    rpcs: tuple[str, ...] = ()
    #: Modules that import this one.
    imported_by: tuple[str, ...] = ()
    #: Modules that augment into this one.
    augmented_by: tuple[str, ...] = field(default=())

    @property
    def declares_surface(self) -> bool:
        """Does this module contribute anything a caller can navigate?

        Data nodes OR rpcs. Data alone is not enough: on the G30 tree,
        `coriant-rpc` (41 operations) and `coriant-cli` (37) declare no
        top-level data nodes whatsoever, so a data-only test dropped every
        vendor operation from the model. Operations are the device's API.
        """
        return bool(self.top_level or self.rpcs)

    @property
    def declares_data(self) -> bool:
        """Does this module contribute its own top-level data nodes?

        Measured on the 397-module SR Linux tree: this is the rule that
        produces a usable list. "Nothing imports it" produced 146 candidates
        of which 144 were leaf/type modules that only augment into other
        modules -- and it DISQUALIFIED the most useful root on the device,
        because `srl_nokia-system` is imported by 88 modules while still being
        one of the two roots ci-srl compiles. Being imported or augmented says
        the module is *shared*, not that it is a leaf. What actually
        distinguishes a root is that it carries navigable top-level data;
        that is 31 of 357 modules, and it contains the roots the CI job uses.
        """
        return bool(self.top_level)


def _revision(path: Path) -> str:
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if stripped.startswith("revision"):
                parts = stripped.split()
                if len(parts) >= 2:
                    return parts[1].strip("\"';")
    except OSError:
        pass
    return ""


_DATA_KEYWORDS = frozenset(
    {"container", "list", "leaf", "leaf-list", "anydata", "anyxml"}
)
_OPERATION_KEYWORDS = frozenset({"rpc", "action"})


def _surface_of(module) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(top-level data node names, top-level rpc/action names)."""
    data: list[str] = []
    ops: list[str] = []
    for child in getattr(module, "i_children", ()):
        if child.keyword in _DATA_KEYWORDS:
            data.append(child.arg or "")
        elif child.keyword in _OPERATION_KEYWORDS:
            ops.append(child.arg or "")
    return tuple(data), tuple(ops)


def _load_batch(
    paths: Sequence[Path], search_dir: Path
) -> dict[str, tuple[tuple[str, ...], tuple[str, ...]]]:
    """Validate `paths` in ONE pyang context and return their top-level nodes.

    A fresh context per module is the trap: measured on the 397-module SR Linux
    tree it took minutes (146 candidates, each re-resolving its imports from
    disk), while a single shared context does them all in well under a second --
    which is what the compiler already does when it hands pyang 95 modules at
    once.
    """
    from pyang import context, repository

    repo = repository.FileRepository(f"yang:{search_dir}")
    ctx = context.Context(repo)
    ctx.implicit_errors = False
    mods = []
    for p in paths:
        try:
            m = ctx.add_module(str(p), p.read_text(encoding="utf-8", errors="replace"))
        except Exception:  # noqa: BLE001 - a probe must not break a compile
            m = None
        if m is not None:
            mods.append(m)
    ctx.validate()
    return {m.arg: _surface_of(m) for m in mods}


def _top_level_nodes_batch(
    paths: Sequence[Path], search_dir: Path
) -> dict[str, tuple[tuple[str, ...], tuple[str, ...]]]:
    """Batched first; stragglers probed individually if one poisons the batch."""
    if not paths:
        return {}
    out: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
    try:
        out.update(_load_batch(list(paths), search_dir))
    except Exception:  # noqa: BLE001 - one bad module can poison the batch
        out.clear()
    missing = [p for p in paths if p.name.split("@")[0] not in out]
    for p in missing:
        try:
            out.update(_load_batch([p], search_dir))
        except Exception:  # noqa: BLE001, S112 - unreadable module, skipped
            continue
    return out


def with_top_level(cands: Sequence[Candidate], yang_dir: Path) -> list[Candidate]:
    """Attach each candidate's top-level data nodes, in one pyang pass.

    Every module is validated, not just an already-known subset: which modules
    declare top-level data IS the question, so pre-filtering would be circular.
    Measured on the 397-module SR Linux tree: 18.4s for all 357 modules in a
    single pyang context -- the same work the compiler does when it hands pyang
    95 modules, and far cheaper than the `pyang -f tree` session this replaces.
    """
    surfaces = _top_level_nodes_batch([c.path for c in cands], yang_dir)
    return [
        Candidate(
            module=c.module,
            path=c.path,
            revision=c.revision,
            imported_by=c.imported_by,
            augmented_by=c.augmented_by,
            top_level=surfaces.get(c.module, ((), ()))[0],
            rpcs=surfaces.get(c.module, ((), ()))[1],
        )
        for c in cands
    ]


def scan(yang_dir: Path) -> list[Candidate]:
    """Every module in `yang_dir`, flagged as candidate or not.

    Implemented on `closure._index_yang_dirs`, the same parse the augment
    closure already uses, so the two cannot disagree about what a module imports.
    """
    flat = _index_yang_dirs([yang_dir])
    imported_by: dict[str, set[str]] = {}
    augmented_by: dict[str, set[str]] = {}
    for mod, (_path, prefixes, augments, _uses) in flat.items():
        for pfx, target in prefixes.items():
            if target == mod or target not in flat:
                continue
            imported_by.setdefault(target, set()).add(mod)
        for aug in augments:
            for pfx in _part_or_self(prefixes, aug):
                if pfx in flat:
                    augmented_by.setdefault(pfx, set()).add(mod)

    out: list[Candidate] = []
    for mod, (path, _prefixes, _augments, _uses) in flat.items():
        out.append(
            Candidate(
                module=mod,
                path=path,
                revision=_revision(path),
                imported_by=tuple(sorted(imported_by.get(mod, ()))),
                augmented_by=tuple(sorted(augmented_by.get(mod, ()))),
            )
        )
    out.sort(key=lambda c: c.module)
    return out


#: Modules that declare data but are never a useful root. Ranked last, not
#: hidden: an operator may still want one, and `all` must reach them.
_INFRASTRUCTURE = frozenset(
    {
        "ietf-yang-library",
        "ietf-inet-types",
        "ietf-yang-metadata",
        "ietf-datastores",
        "ietf-netconf-monitoring",
        "ietf-netconf-acm",
        "ietf-netconf-with-defaults",
        "ietf-netconf-yang-library",
        "ietf-netconf-nmda",
    }
)


def resolve_modules(
    names: Iterable[str], yang_dir: Path
) -> tuple[list[Path], list[str]]:
    """Map module names to files. Returns (found, missing).

    `--roots ietf-interfaces` has to become a path, because the compiler's
    positional argument is a file. The lookup is `closure._index_yang_dirs`, so
    a name the picker cannot see is a name the compiler cannot find either.
    """
    flat = _index_yang_dirs([yang_dir])
    found: list[Path] = []
    missing: list[str] = []
    for name in names:
        if name in flat:
            found.append(flat[name][0])
        else:
            missing.append(name)
    return found, missing


def _is_protocol_only(cand: Candidate) -> bool:
    """Transport operations, not device operations -- never suggested.

    `ietf-netconf` declares get-config / edit-config / copy-config /
    delete-config and `ietf-netconf-nmda` declares get-data / edit-data. These
    are the operations the client itself performs; no caller ever wants them
    navigable on a device model, and compiling them is how the G30 tree ended
    up with 18 of 19 top-level navigators being protocol plumbing.

    The vendor rpc-only modules are the opposite case and ARE suggested:
    `coriant-cli` (37 operations), `coriant-rpc` (41) and `openroadm-augment`
    (11) are the device's API.

    An `ietf-netconf*` module that also carries data stays a candidate, because
    that data is the device's: ietf-netconf-monitoring's netconf-state is a
    real read.
    """
    return cand.module.startswith("ietf-netconf") and not cand.top_level


def _is_infrastructure(cand: Candidate) -> bool:
    """Suggested, but ranked last: real modules that are rarely a root.

    Kept in the list rather than hidden so `all` can still reach them --
    ietf-yang-library and netconf-monitoring are legitimate reads for anyone
    who asks for them.
    """
    return cand.module in _INFRASTRUCTURE or cand.module.endswith("-deviations")


def suggested_roots(cands: Iterable[Candidate]) -> list[Candidate]:
    """The modules worth proposing, best first."""
    real = [c for c in cands if c.declares_surface and not _is_protocol_only(c)]
    return sorted(real, key=lambda c: (_is_infrastructure(c), c.module))


def _part_or_self(prefixes: dict[str, str], target: str) -> Iterable[str]:
    """Resolve every prefix in an `augment "/pfx:a/pfx:b"` target."""
    seen = False
    for chunk in target.replace('"', "").split("/"):
        if ":" not in chunk:
            continue
        seen = True
        pfx = chunk.split(":", 1)[0]
        if pfx in prefixes:
            yield prefixes[pfx]
    if not seen:
        # An augment target with no prefix at all ("/top") refers to the
        # augmenting module's own prefix, which `_parse_yang` already mapped.
        for pfx, mod in prefixes.items():
            yield mod


def render(cands: Sequence[Candidate], yang_dir: Path, stream) -> None:
    """Print the numbered candidate list."""
    roots = suggested_roots(cands)
    others = len(cands) - len(roots)
    print(f"\nyang modules in {yang_dir}  ({len(cands)} modules)", file=stream)
    if not roots:
        print(
            "\nNo module here declares top-level data or operations -- every module "
            "is reached through another. Name roots explicitly:\n"
            "  uv run yang2sdk --protocol netconf <module>.yang ...\n"
            "  uv run yang2sdk --protocol netconf --roots <modules> ...",
            file=stream,
        )
        return
    print(
        f"\nSuggested roots -- {len(roots)} module(s) that declare top-level data "
        f"or operations"
        + (f"  ({others} other modules are reached through them)" if others else ""),
        file=stream,
    )
    for i, c in enumerate(roots, 1):
        if c.top_level:
            tops = ", ".join(c.top_level[:4])
            if len(c.top_level) > 4:
                tops += f", +{len(c.top_level) - 4} more"
            kind = ""
        else:
            tops = ", ".join(c.rpcs[:3])
            if len(c.rpcs) > 3:
                tops += f", +{len(c.rpcs) - 3} more"
            kind = f"[{len(c.rpcs)} rpc] "
        print(f"  {i:>3}  {c.module:<44} {kind}{tops}", file=stream)
    if len(roots) > PAGE_SIZE:
        print(
            f"      ... {len(roots) - PAGE_SIZE} more candidate(s); type a filter to "
            f"narrow, or `all` to take every candidate.",
            file=stream,
        )


def prompt(
    cands: Sequence[Candidate],
    yang_dir: Path,
    *,
    stdin=None,
    stdout=None,
) -> list[Candidate] | None:
    """Ask which candidates to compile. None means the operator aborted.

    Reads from an injectable stream so the loop is testable without a pty.
    """
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    roots = suggested_roots(cands)
    if not roots:
        render(cands, yang_dir, stdout)
        return None

    while True:
        render(cands, yang_dir, stdout)
        print(
            "\nPick roots: numbers/ranges (1,3-5), `all`, or `q` to abort.\n > ",
            end="",
            file=stdout,
        )
        stdout.flush()
        line = stdin.readline()
        if not line:
            # EOF (piped input exhausted): never loop forever.
            print(file=stdout)
            return None
        choice = line.strip().lower()
        if not choice:
            # Enter accepts the suggestion -- that is the whole point of
            # suggesting, and the list was printed for exactly this.
            return roots
        if choice in ("q", "quit", "n", "no"):
            return None
        if choice in ("all", "a", "*"):
            return roots

        if " " in choice and not _is_selection(choice):
            # A bare word is treated as a substring filter over module names.
            hits = [c for c in roots if choice in c.module.lower()]
            if not hits:
                print(f"no candidate module matches {choice!r}", file=stdout)
                continue
            print(f"{len(hits)} match(es):", file=stdout)
            # Narrow the list to the matches and ask again about that subset.
            cands = list(hits)
            roots = cands
            continue

        picked = _parse_selection(choice, roots)
        if picked is None:
            print(
                f"could not read {choice!r} -- use numbers (1,3-5), `all`, or `q`",
                file=stdout,
            )
            continue
        return picked


def _is_selection(text: str) -> bool:
    return bool(text) and all(
        part and part.replace("-", "").isdigit() for part in text.split(",")
    )


def _parse_selection(text: str, roots: Sequence[Candidate]) -> list[Candidate] | None:
    picked: list[Candidate] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, _, hi = part.partition("-")
            if not (lo.strip().isdigit() and hi.strip().isdigit()):
                return None
            lo_i, hi_i = int(lo), int(hi)
            if lo_i > hi_i:
                lo_i, hi_i = hi_i, lo_i
            if lo_i < 1 or hi_i > len(roots):
                return None
            picked.extend(roots[lo_i - 1 : hi_i])
        else:
            if not part.isdigit():
                return None
            idx = int(part)
            if idx < 1 or idx > len(roots):
                return None
            picked.append(roots[idx - 1])
    return picked or None
