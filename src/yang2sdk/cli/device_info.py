"""Device identity recorded next to the downloaded YANG modules.

`yang-downloader` knows which device and OS release it pulled the modules
from; the compiler, run later and often in another shell, did not. Without a
release the package is named `<device>_0_0_0`, so every compile had to repeat
`--release` by hand. The downloader therefore writes `<yang-dir>/device.json`
and the compiler reads it as a fallback, scoped to the device it names (the
same scoping rule as `$DEVICE_NAME`/`$DEVICE_VERSION`, see
`compiler._resolve_device_version`).

Also home to the small interactive-prompt helper both CLIs use.
"""

from __future__ import annotations

import getpass
import json
import sys
from pathlib import Path

DEVICE_INFO_FILENAME = "device.json"
DEVICE_INFO_FORMAT = 1


def write_device_info(yang_dir: Path, *, device: str, release: str) -> Path:
    path = Path(yang_dir) / DEVICE_INFO_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"format": DEVICE_INFO_FORMAT, "device": device, "release": release}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def read_device_info(yang_dir: Path) -> dict[str, str]:
    """Return `{"device": ..., "release": ...}`, or `{}` if absent/unreadable."""
    path = Path(yang_dir) / DEVICE_INFO_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: data[k] for k in ("device", "release") if isinstance(data.get(k), str)}


def interactive() -> bool:
    """True when a human can answer a prompt (never block CI on a read)."""
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def ask(label: str, default: str | None = None, *, secret: bool = False) -> str:
    """Prompt on stderr; an empty answer returns `default` (or "")."""
    suffix = f" [{default}]" if default else ""
    prompt = f"{label}{suffix}: "
    if secret:
        answer = getpass.getpass(prompt)
    else:
        print(prompt, end="", file=sys.stderr, flush=True)
        answer = sys.stdin.readline().strip()
    return answer or (default or "")
