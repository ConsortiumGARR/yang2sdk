"""SR Linux containerlab lab helpers — LAB ONLY.

Single source of truth for the lab's committed defaults, shared by
``tests/test_srl_netconf.py`` and the CI job in ``ci-srl.yaml``.

The values come from ``tests/srl/.env.srl.example``, which holds the
*published* containerlab defaults (``admin`` / ``NokiaSrl1!``). They are not
secrets — they are in containerlab's own documentation — but they are still
credentials, so this module resolves and returns them without ever logging or
printing a value. Never hardcode them a second time; add a key to the example
file and read it from here.

Run standalone to block until the node answers NETCONF, then exit non-zero on
timeout::

    uv run python -m tests.srl.lab

Exits 0 when ready, 1 on timeout, 2 when the example file is unreadable. The
CI job uses the exit code, so a lab that never boots fails the deploy step
loudly instead of silently skipping every test.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from tests.notconf.wait_healthy import wait_netconf

#: The committed defaults file. Committed on purpose (see module docstring).
ENV_EXAMPLE = Path(__file__).resolve().parent / ".env.srl.example"

#: SR Linux needs several minutes to reach NETCONF-ready; containerlab's own
#: lab examples budget 4-5 minutes for deploy alone. Generous on purpose: a
#: slow boot must not be reported as a broken device.
DEFAULT_READY_TIMEOUT = 900


def lab_defaults(env_example: Path = ENV_EXAMPLE) -> dict[str, str]:
    """Parse ``KEY=value`` pairs from the committed example env file.

    Lines that are blank, comments, or lack ``=`` are skipped. Values are
    returned to the caller and never logged -- only the originating file is
    safe to name in output.
    """
    out: dict[str, str] = {}
    for line in env_example.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def lab_connection(
    defaults: dict[str, str] | None = None,
) -> tuple[str, int, str, str]:
    """Resolve ``(host, port, username, password)`` for the lab's NETCONF server.

    All four values are overridable from the environment so a developer can
    point the same helpers at a remote node instead of the local container:
    ``SRL_DEVICE_IP``, ``SRL_DEVICE_NETCONF_PORT``, ``SRL_DEVICE_USER``,
    ``SRL_DEVICE_PASS``.

    Returned as one tuple on purpose. Resolving the endpoint in one place and
    the credentials in another invites the half-override case -- a remote host
    silently paired with the local container's credentials.
    """
    cfg = lab_defaults() if defaults is None else defaults
    host = os.environ.get("SRL_DEVICE_IP") or cfg.get("DEVICE_IP", "127.0.0.1")
    raw_port = os.environ.get("SRL_DEVICE_NETCONF_PORT") or cfg.get(
        "NETCONF_PORT", "1830"
    )
    username = os.environ.get("SRL_DEVICE_USER") or cfg.get("DEVICE_USER", "")
    password = os.environ.get("SRL_DEVICE_PASS") or cfg.get("DEVICE_PASS", "")
    return host, int(raw_port), username, password


def wait_until_ready(timeout: int = DEFAULT_READY_TIMEOUT) -> None:
    """Block until the lab's NETCONF server completes the RFC 6241 Sec 8 hello."""
    host, port, username, password = lab_connection()
    if not (username and password):
        raise ValueError(f"{ENV_EXAMPLE.name} must define DEVICE_USER and DEVICE_PASS")
    # Only the endpoint is ever printed; the password never is.
    print(f"[srl-lab] waiting for NETCONF {host}:{port} (timeout {timeout}s)")
    wait_netconf(
        host,
        port,
        timeout=timeout,
        username=username,
        password=password,
    )
    print(f"[srl-lab] NETCONF {host}:{port} ready")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        wait_until_ready(timeout=int(args[0]) if args else DEFAULT_READY_TIMEOUT)
    except TimeoutError as exc:
        # The exception text carries ncclient's message, never the password.
        print(f"[srl-lab] NOT ready: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"[srl-lab] cannot resolve the lab: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
