"""Device feature-set plumbing (pure, stdlib only — no lab extras, no I/O side effects).

NETCONF ``<hello>`` advertises per-module YANG capabilities as (RFC 6241 Sec 8.3)::

    <namespace>?module=<name>&revision=<rev>&features=<f1,f2>,...

This module turns those capabilities into the *pyang* ``--features`` argument
that makes a generated model match what the device actually implements.

Why a module that advertises no ``features=`` must be left out
----------------------------------------------------------------
pyang treats ``ctx.features`` as a **per-module whitelist**
(``pyang/statements.py``: an ``if-feature`` resolves to False unless the
feature is listed for the *defining* module). A module that advertises no
``features=`` is *unknown*, not *empty*; emitting ``mod:`` (an empty list)
would disable **every** feature of that module. pyang's own hello loader
(``pyang/hello.py`` ``get_features``) has the same trap, which is why
``--hello`` is not reused here: it also discards the operator's root-module
selection and the augment closure.

Feature sets are keyed by the **defining** module, never by the consuming
one: a central ``srl_nokia-features``-style module with 279 features covers
every ``srl_nokia-feat:*`` consumer with a single entry.

The functions live here, not in ``cli/downloader.py``, so that they stay
importable (and unit-testable) without the lab extra — importing the
downloader requires lxml/ncclient/dotenv *and* triggers ``load_dotenv()``
plus the downloader log file. ``downloader`` imports and re-exports them.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from urllib.parse import parse_qs

FEATURES_FILENAME = "features.json"
FEATURES_FORMAT = "yang2sdk-features-v1"


def parse_capability_features(capabilities: Iterable[str]) -> dict[str, list[str]]:
    """Map module name -> advertised features from NETCONF ``<hello>`` capabilities.

    Only capabilities that actually carry a ``features=`` parameter are
    included (see module docstring). A bare ``features=`` — a parameter
    present with an empty value — is the device explicitly saying "no features
    supported" and maps to an empty list, exactly as pyang's own hello loader
    does (``pyang/hello.py`` ``get_features``). A module advertised more than
    once keeps the first capability that carries features; order is otherwise
    preserved so the emitted ``--features`` arguments are deterministic.
    """
    out: dict[str, list[str]] = {}
    for cap in capabilities:
        if "?" not in cap:
            continue
        _base, _, query = cap.partition("?")
        params = parse_qs(query, keep_blank_values=True)
        names = params.get("module")
        raw = params.get("features")
        if not names or not names[0] or raw is None:
            continue
        module = names[0]
        if module in out:
            # First capability carrying features wins; deterministic and
            # avoids merging across revisions of the same module.
            continue
        out[module] = [f for f in raw[0].split(",") if f]
    return out


def to_pyang_args(features: Mapping[str, Iterable[str]]) -> list[str]:
    """Render ``--features`` arguments: one ``<mod>:<f1,f2>`` arg per module.

    pyang's ``-F/--features`` is ``action="append"`` and each occurrence is
    parsed by ``pyang_tool.parse_features_string`` as ``<modname>:[<feature>,]*``
    — repeating ``--features mod:feat`` is a hard error, so every module needs
    exactly one argument with its features comma-joined.
    """
    return [
        f"{module}:{','.join(sorted(fs))}" for module, fs in sorted(features.items())
    ]


def write_features_file(
    path: Path,
    features: Mapping[str, Iterable[str]],
    *,
    source: str = "netconf-hello",
) -> Path:
    """Write the device feature set next to the downloaded YANG modules."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "format": FEATURES_FORMAT,
        "source": source,
        "modules": {m: list(fs) for m, fs in sorted(features.items())},
    }
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


def read_features_file(path: Path) -> dict[str, list[str]]:
    """Read a features file written by :func:`write_features_file`.

    A bare ``{module: [features]}`` mapping is also accepted so hand-written
    fixtures stay trivial.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    modules = data.get("modules", data)
    try:
        return {str(m): [str(f) for f in fs] for m, fs in modules.items()}
    except (AttributeError, TypeError) as e:
        raise ValueError(f"{path}: expected a module->features mapping") from e
