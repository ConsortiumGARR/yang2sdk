"""Readiness probe for notconf simulators (mirrors upstream wait-healthy).

Polls NETCONF :830 hello (RFC 6241 Sec 8) and RESTCONF host-meta
(RFC 8040 Sec 3.1) until both answer or the timeout expires.
Usage: python wait_healthy.py <netconf_host> <netconf_port> <restconf_base_url>
"""

import socket
import sys
import time
import urllib.request

from ncclient import manager


def wait_netconf(host: str, port: int, timeout: int = 300) -> None:
    deadline = time.time() + timeout
    last: Exception | None = None
    while time.time() < deadline:
        try:
            m = manager.connect(
                host=host,
                port=port,
                username="admin",
                password="admin",
                hostkey_verify=False,
                timeout=15,
            )
            assert m is not None  # narrow ncclient stubs for type-checkers
            with m:
                if m.server_capabilities:
                    return
        except Exception as e:  # noqa: BLE001 - readiness polling
            last = e
            time.sleep(5)
    raise TimeoutError(f"NETCONF {host}:{port} not ready: {last}")


def wait_restconf(base_url: str, timeout: int = 300) -> None:
    deadline = time.time() + timeout
    last: Exception | None = None
    host = base_url.split("://", 1)[1].split("/", 1)[0]
    hostname = host.rsplit(":", 1)[0]
    port = int(host.rsplit(":", 1)[1]) if ":" in host else 80
    while time.time() < deadline:
        try:
            with socket.create_connection((hostname, port), timeout=5):
                pass
            req = urllib.request.Request(base_url + "/.well-known/host-meta")
            with urllib.request.urlopen(req, timeout=10) as r:
                if b"restconf" in r.read():
                    return
        except Exception as e:  # noqa: BLE001 - readiness polling
            last = e
            time.sleep(5)
    raise TimeoutError(f"RESTCONF {base_url} not ready: {last}")


if __name__ == "__main__":
    nc_host, nc_port, rc_base = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    wait_netconf(nc_host, nc_port)
    print(f"NETCONF {nc_host}:{nc_port} ready")
    wait_restconf(rc_base)
    print(f"RESTCONF {rc_base} ready")
