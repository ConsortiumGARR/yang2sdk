"""Shared fixtures for notconf simulator tests.

Offline by default. Integration tests (docker + live simulators) run only
when NOTCONF_RUN_INTEGRATION=1 or --integration is passed.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MATRIX_PATH = REPO_ROOT / "tests" / "notconf" / "matrix.json"

RUN_INTEGRATION = os.environ.get("NOTCONF_RUN_INTEGRATION") == "1"


def pytest_addoption(parser):
    parser.addoption(
        "--integration",
        action="store_true",
        default=False,
        help="run integration tests against live notconf containers",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--integration") or RUN_INTEGRATION:
        return
    skip = pytest.mark.skip(reason="needs --integration / NOTCONF_RUN_INTEGRATION=1")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


def load_matrix():
    with open(MATRIX_PATH) as f:
        return json.load(f)


def pytest_generate_tests(metafunc):
    if "notconf_image" in metafunc.fixturenames:
        matrix = load_matrix()
        images = matrix["images"]
        only_smoke = os.environ.get("NOTCONF_SMOKE_ONLY") == "1" or (
            "smoke" in (metafunc.definition.keywords or {})
        )
        only_pkg = os.environ.get("NOTCONF_IMAGE_PACKAGE")
        only_tag = os.environ.get("NOTCONF_IMAGE_TAG")
        params = [
            pytest.param(img, id=f"{img['package']}:{img['tag']}")
            for img in images
            if (img.get("smoke", False) if only_smoke else True)
            and (only_pkg is None or img["package"] == only_pkg)
            and (only_tag is None or str(img["tag"]) == str(only_tag))
        ]
        assert params, "image filter matched nothing"
        metafunc.parametrize("notconf_image", params)


@pytest.fixture(scope="session")
def docker_available():
    try:
        subprocess.run(["docker", "info"], capture_output=True, check=True, timeout=30)
        return True
    except Exception:  # noqa: BLE001 - docker may be absent
        return False


@pytest.fixture(scope="session")
def notconf_container_factory(docker_available):
    """Start one notconf container per image on ephemeral ports.

    Yields a dict {image_id: {"netconf_host/port": ..., "restconf_base": ...}}.
    Containers are removed on teardown. Ephemeral host ports avoid clashes
    with kind (host :80/:443) and parallel jobs.
    """
    if not docker_available:
        pytest.skip("docker not available")
    started = {}

    def _free_port():
        import socket

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def start(image):
        ref = (
            f"{image['registry']}/{image['package']}:{image['tag']}"
            if "registry" in image
            else f"ghcr.io/notconf/{image['package']}:{image['tag']}"
        )
        iid = f"{image['package']}:{image['tag']}"
        if iid in started:
            return started[iid]
        nc_port = _free_port()
        rc_port = _free_port()
        name = f"yang2sdk-{image['package']}-{image['tag']}".replace(".", "-").replace(
            ":", "-"
        )
        subprocess.run(
            ["docker", "rm", "-f", name], capture_output=True, check=False, timeout=30
        )
        subprocess.run(
            ["docker", "pull", ref], check=False, capture_output=True, timeout=600
        )
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "-p",
                f"127.0.0.1:{nc_port}:830",
                "-p",
                f"127.0.0.1:{rc_port}:80",
                ref,
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "wait_healthy", REPO_ROOT / "tests" / "notconf" / "wait_healthy.py"
        )
        assert spec is not None and spec.loader is not None
        wh = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(wh)
        wait_netconf, wait_restconf = wh.wait_netconf, wh.wait_restconf

        wait_netconf("127.0.0.1", nc_port, timeout=600)
        wait_restconf(f"http://127.0.0.1:{rc_port}", timeout=600)
        info = {
            "ref": ref,
            "name": name,
            "netconf_host": "127.0.0.1",
            "netconf_port": nc_port,
            "restconf_base": f"http://127.0.0.1:{rc_port}",
            "username": "admin",
            "password": "admin",
        }
        started[iid] = info
        return info

    yield start

    for info in started.values():
        subprocess.run(
            ["docker", "rm", "-f", info["name"]],
            capture_output=True,
            check=False,
            timeout=60,
        )


@pytest.fixture(scope="function")
def live_endpoint(notconf_image, notconf_container_factory, request):
    matrix = load_matrix()
    entry = dict(notconf_image)
    entry.setdefault("registry", matrix["registry"])
    if "smoke" in (request.keywords or {}):
        assert entry.get("smoke", False), "smoke job got non-smoke image"
    return entry, notconf_container_factory(entry)
