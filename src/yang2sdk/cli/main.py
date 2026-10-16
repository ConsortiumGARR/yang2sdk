"""The `yang2sdk` entry point: one command, `--protocol` picks the transport.

Replaces the `yang2netconf` / `yang2restconf` pair. Both were the same program
with a hardcoded transport, and a third transport would have meant a third
copy of twelve flags. Everything except `--protocol` is forwarded verbatim to
`cli.compiler.run_compiler`, so the flag set cannot drift between the two
entry points.

Root modules are chosen here rather than demanded up front. `run_compiler`
needs *root* modules -- the ones pyang is told about directly, because pyang
expands an `augment` only when the augmenting module is also named
(RFC 7950 Sec 7.17) -- and picking them was the last hand-fed step in the
pipeline. Name them and nothing is asked; omit them and `cli.rootpicker`
proposes the modules that actually declare top-level data (measured: 38 of 357
on an SR Linux tree, including both roots ci-srl compiles).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import rootpicker
from .compiler import run_compiler

GUIDE = """\
yang2sdk -- generate a RESTCONF or NETCONF SDK from YANG modules.

The pipeline, end to end:

  1. Get the modules the device actually runs
       uv run yang-downloader                      # NETCONF get-schema (lab only)
     or point --yang-dir at a directory of .yang files you already have.

  2. Pick the root modules. Ask for them:
       uv run yang2sdk --protocol netconf --device srl
     ...and the candidates that declare top-level data are listed, so you
     choose instead of guessing. Or name them yourself:
       uv run yang2sdk --protocol netconf --roots ietf-interfaces,ietf-system
     Naming a .yang file positionally works too, and skips the picker.

  3. Read the generated package (a self-contained, installable Python package)
       uv add ./temp/netconf_clients/<device>_<os-version>
       from <device>_<os_version> import NetconfClient

  4. Optional, lab only: validate the whole generated client against a device
       uv run sdk-verify --device <device> --tiers read,rpc
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="yang2sdk",
        description=GUIDE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  yang2sdk --protocol netconf --device srl\n"
            "      pick roots interactively from the modules in the YANG dir\n"
            "  yang2sdk --protocol restconf --device srl --roots ietf-interfaces\n"
            "      non-interactive: name the roots, nothing is asked\n"
            "  yang2sdk --protocol netconf --device srl --list-roots\n"
            "      print the candidate roots and exit without compiling\n"
            "  yang2sdk --protocol netconf mod1.yang mod2.yang\n"
            "      name root files positionally; skips the picker entirely\n"
            "  yang2sdk --protocol netconf --device srl --help\n"
            "      every other flag is forwarded unchanged to the compiler\n"
        ),
    )
    parser.add_argument(
        "--protocol",
        choices=("netconf", "restconf"),
        help="Transport to generate. Everything else is forwarded to the compiler.",
    )
    parser.add_argument(
        "--roots",
        metavar="MODULES",
        help=(
            "Comma-separated root module names to compile, bypassing the "
            "picker. Use this in scripts and CI, where there is no TTY to ask."
        ),
    )
    parser.add_argument(
        "--list-roots",
        dest="list_roots",
        action="store_true",
        help="Print the candidate root modules and exit without compiling.",
    )
    return parser


def _yang_dir(forwarded: list[str]) -> Path:
    """Where the modules live, mirroring compiler.yangs_dir exactly.

    Kept in step with the compiler's own default (`temp/yang_modules/<device>`)
    rather than duplicated from memory.
    """
    probe = argparse.ArgumentParser(add_help=False)
    probe.add_argument("--device", default=None)
    probe.add_argument("--yang-dir", default=None)
    known, _ = probe.parse_known_args(forwarded)
    if known.yang_dir:
        return Path(known.yang_dir)
    return Path.cwd() / "temp" / "yang_modules" / (known.device or "")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    # parse_known_args, not parse_args: everything that is not one of the four
    # flags below belongs to the compiler and is forwarded untouched. Filtering
    # argv by hand instead (what this did first) drops `--roots a,b` and leaves
    # `a,b` behind as a stray positional, which the compiler would then treat as
    # a root file.
    args, forwarded = parser.parse_known_args(argv)

    if args.protocol is None:
        # Bare `yang2sdk`, or --help without a transport: teach, do not compile.
        # Exit 2 so a script that forgot --protocol cannot mistake this for a
        # successful build.
        parser.print_help(
            sys.stderr if not argv or argv[0] not in ("-h", "--help") else sys.stdout
        )
        return 0 if argv and argv[0] in ("-h", "--help") else 2

    # Anything that looks like a YANG file is a root the caller already chose.
    named = [t for t in forwarded if t.endswith((".yang", ".yin"))]
    roots: list[str] = []
    if named:
        pass  # forwarded verbatim below; nothing to add
    elif args.roots:
        names = [n.strip() for n in args.roots.split(",") if n.strip()]
        yang_dir = _yang_dir(forwarded)
        found, missing = rootpicker.resolve_modules(names, yang_dir)
        if missing:
            print(
                f"Error: unknown module(s) in --roots: {', '.join(missing)}\n"
                f"Not found in {yang_dir}. Run with --list-roots to see what is there.",
                file=sys.stderr,
            )
            return 2
        roots = [str(p) for p in found]
    else:
        picked = _choose_roots(args, forwarded)
        if picked is None:
            return 2
        if picked:
            roots = [str(c.path) for c in picked]

    if args.list_roots:
        return 0

    run_compiler(args.protocol, [*roots, *forwarded])
    return 0


def _choose_roots(
    args: argparse.Namespace, _forwarded_flags: list[str]
) -> list[rootpicker.Candidate] | None:
    """Run the picker, or explain why it cannot run."""
    yang_dir = _yang_dir(_forwarded_flags)
    if not yang_dir.is_dir():
        print(
            f"Error: YANG directory not found: {yang_dir}\n"
            "Pass --yang-dir, or run 'yang-downloader' first.",
            file=sys.stderr,
        )
        return None

    cands = rootpicker.with_top_level(rootpicker.scan(yang_dir), yang_dir)

    if args.list_roots:
        # Ask for the list and nothing else: no TTY needed, no compile.
        rootpicker.render(cands, yang_dir, sys.stdout)
        return []

    if not sys.stdin.isatty():
        # CI has no TTY. Never block on a read that will never come.
        rootpicker.render(cands, yang_dir, sys.stderr)
        print(
            "\nError: no root modules given and stdin is not a terminal.\n"
            "Pass --roots <modules> (or name .yang files) for a non-interactive run.",
            file=sys.stderr,
        )
        return None

    return rootpicker.prompt(cands, yang_dir)


if __name__ == "__main__":
    sys.exit(main())
