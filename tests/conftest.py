"""Shared fixtures for notconf simulator tests and real lab devices.

Offline by default. Integration tests (docker + live simulators) run only
when NOTCONF_RUN_INTEGRATION=1 or --integration is passed. Real devices are
shaped differently — pre-existing endpoint, no image, no docker, credentials
from the environment — so the matrix carries a second ``devices`` list and
``lab_device`` resolves it without ever logging a credential or host value.
"""

import json
import os
import socket
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


def _env_value(entry, key, default=None):
    """Resolve a device setting from the environment.

    The legacy ``DEVICE_*`` names (used by ``yang-downloader`` and ``tester``)
    stay the fallback so an existing .env keeps working unchanged, and the
    port also accepts the bare ``NETCONF_PORT`` those tools read. Values are
    returned, never logged: a device is referenced by its matrix id.
    """
    prefix = entry.get("env", "")
    names = [f"{prefix}_{key}"] if prefix else []
    names += [f"DEVICE_{key}"]
    if key == "NETCONF_PORT":
        names.append("NETCONF_PORT")
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


def _port_open(host, port, timeout=2.0):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


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
    if "lab_device" in metafunc.fixturenames:
        wanted = os.environ.get("LAB_DEVICE_ID")
        devices = [
            pytest.param(entry, id=entry["id"])
            for entry in load_matrix().get("devices", [])
            if wanted is None or entry["id"] == wanted
        ]
        assert devices, "lab device filter matched nothing"
        metafunc.parametrize("lab_device", devices, indirect=True)


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


# --- real lab devices (matrix "devices" list) -------------------------------


def _load_dotenv():
    """Load .env into the environment without printing anything from it."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    env_file = REPO_ROOT / ".env"
    if env_file.is_file():
        load_dotenv(env_file, override=False)


_load_dotenv()


def load_devices():
    return load_matrix().get("devices", [])


def _known_hosts_path(entry):
    """Optional pinned host key, so verify=True can actually pass.

    A real device with a self-signed host key fails closed (that is the point
    of the default). Pinning it is a lab-only opt-in: point LAB_DEVICE_KNOWN_HOSTS
    at a file in known_hosts format and the secure default works end to end.
    """
    return _env_value(entry, "KNOWN_HOSTS") or None


@pytest.fixture(scope="session")
def lab_device(request):
    """Resolve one real device from the matrix, or skip with a clear reason."""
    entry = getattr(request, "param", None)
    if entry is None:
        pytest.skip("lab_device fixture requires a matrix entry (use lab_device_id)")
    host = _env_value(entry, "IP")
    port = _env_value(entry, "NETCONF_PORT")
    user = _env_value(entry, "USER")
    password = _env_value(entry, "PASS")
    if not (host and port and user and password):
        prefix = entry.get("env", "DEVICE")
        pytest.skip(
            f"lab env for {entry['id']} is not configured "
            f"(set {prefix}_IP/{prefix}_NETCONF_PORT/{prefix}_USER/{prefix}_PASS, "
            "or the legacy DEVICE_* names); see .env.example"
        )
    if not _port_open(host, port):
        pytest.skip(f"{entry['id']}: nothing is listening on the NETCONF port")
    known_hosts = _known_hosts_path(entry)
    if known_hosts and not Path(known_hosts).is_file():
        pytest.skip(f"{entry['id']}: KNOWN_HOSTS file does not exist")
    return {
        "id": entry["id"],
        "host": host,
        "port": int(port),
        "username": user,
        "password": password,
        # verify=True is the secure default. It is kept unless the operator
        # explicitly pinned a host key or opted out.
        "verify": known_hosts is not None
        and os.environ.get(f"{entry.get('env', 'DEVICE')}_ALLOW_UNVERIFIED") != "1",
        "known_hosts": known_hosts,
        "ssh_config": entry.get("ssh_config"),
        "roots": entry.get("roots", []),
        "yang_dir": entry.get("yang_dir", ""),
        "read_subtree": entry.get("read_subtree", ""),
        "read_path": entry.get("read_path", []),
        "ignore_errors": entry.get("ignore_errors", []),
        "write_subtree": entry.get("write_subtree", ""),
        "write_leaf": entry.get("write_leaf", ""),
        # Without this the write gate in tests/test_lab_device_netconf.py
        # could never pass, so the repo's only commit/rollback test always
        # skipped no matter what the matrix said.
        "write_value": entry.get("write_value", ""),
    }


@pytest.fixture(scope="session")
def lab_device_client(lab_device):
    """A generated SDK client for the lab device, or skip.

    The SDK package must already be generated (see the module docstring of
    tests/test_lab_device_netconf.py); the client is never logged.
    """
    import importlib
    import sys

    slug = lab_device["id"]
    clients = REPO_ROOT / "temp" / "netconf_clients"
    if not (clients / slug / "__init__.py").is_file():
        pytest.skip(f"generated client for {slug} not found in temp/netconf_clients")
    if str(clients) not in sys.path:
        sys.path.insert(0, str(clients))
    module = importlib.import_module(slug)
    kwargs = {
        "management_ip": lab_device["host"],
        "port": lab_device["port"],
        "username": lab_device["username"],
        "password": lab_device["password"],
        "verify": lab_device["verify"],
        "timeout": 60,
        # A shared device: never auto-commit.
        "auto_commit": False,
    }
    if lab_device["verify"] and lab_device["known_hosts"]:
        cfg = Path(lab_device["known_hosts"]).with_suffix(".sshconfig")
        cfg.write_text(
            f"Host {lab_device['host']}\n"
            f"    UserKnownHostsFile {lab_device['known_hosts']}\n"
        )
        cfg.chmod(0o600)
        kwargs["ssh_config"] = str(cfg)
    client = module.NetconfClient(**kwargs)
    try:
        yield client
    finally:
        try:
            client._manager.close_session()
        except Exception:  # noqa: BLE001, S110 - teardown best effort
            pass


def pytest_report_header(config):
    """Say which real devices are configured, by name only."""
    _load_dotenv()
    configured = [
        entry["id"]
        for entry in load_devices()
        if _env_value(entry, "IP") and _env_value(entry, "USER")
    ]
    if configured:
        return f"lab devices configured (values never printed): {', '.join(configured)}"
    return None


def _unused(*_args):  # pragma: no cover - keeps subprocess import honest
    return subprocess
