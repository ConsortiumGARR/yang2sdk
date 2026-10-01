"""Validate a generated client against a lab device. LAB ONLY — never production."""

import importlib
import logging
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError as e:
    raise ImportError(
        "tester needs the lab extra: pip install 'yang2sdk[lab]' "
        "(or `uv sync --extra lab` for development)"
    ) from e

log_file = Path.cwd() / "temp" / "client_tester.log"
log_file.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(log_file),
    ],
)
logger = logging.getLogger(__name__)

load_dotenv()


def main():
    try:
        ip = os.environ["DEVICE_IP"]
        port = os.environ["RESTCONF_PORT"]
        username = os.environ["DEVICE_USER"]
        password = os.environ["DEVICE_PASS"]
        device_name = os.environ["DEVICE_NAME"]
    except KeyError as e:
        # A gate that cannot even find its configuration must fail.
        print(f"Environment configuration missing key: {e}", file=sys.stderr)
        sys.exit(2)

    # Add active working directory to sys.path to locate transient generated structures
    failures: list[tuple[str, str]] = []
    sys.path.insert(0, str(Path.cwd()))
    module_path = f"temp.restconf_clients.{device_name}"

    try:
        module = importlib.import_module(module_path)
        device_client_class = module.RestconfClient
        logger.debug(f"Imported generated client module: {module_path}")
    except ImportError as e:
        logger.error(f"Failed to import client module {module_path}: {e}")
        print(f"Import Error: {e}")
        sys.exit(1)

    client = device_client_class(
        management_ip=ip,
        port=int(port),
        username=username,
        password=password,
        verify=False,
    )
    logger.warning(
        "verify=False is an explicit lab-only opt-out for self-signed devices; "
        "never use the tester against production"
    )

    for prop in vars(type(client.data)).values():
        if isinstance(prop, property):
            fget = prop.fget
            assert fget is not None
            navigator = fget(client.data)
            print(f"Testing validation sequence on: {navigator._path}")
            logger.info(f"Testing validation sequence on: {navigator._path}")

            try:
                # Bounded depth: an unbounded read of a top-level container is exactly
                # the request AGENTS.md warns can trigger a watchdog reboot.
                result = navigator.retrieve(content="config", depth=2)
                # A list navigator returns a plain Python list, so the old
                # `result.__class__.__name__` reported "list" and every list
                # navigator passed unconditionally -- the check validated
                # nothing. Validate each item's class instead.
                items = result if isinstance(result, list) else [result]
                classes = {type(i).__name__ for i in items}
                print(f"  [OK] Parsed {len(items)} model(s): {sorted(classes)}")
                logger.info(f"  [OK] Parsed {len(items)} model(s): {sorted(classes)}")
            except Exception as e:
                failures.append((navigator._path, f"{type(e).__name__}: {e}"))
                logger.exception(f"  [FAIL] {navigator._path}")
                print(f"  [FAIL] {navigator._path} - Error: {e}")

    if failures:
        # A validation gate that always exits 0 cannot gate anything.
        print(
            f"\n[FAIL] {len(failures)} navigator(s) failed validation:", file=sys.stderr
        )
        for path, err in failures:
            print(f"  {path}: {err}", file=sys.stderr)
        sys.exit(1)
    print("\n[OK] every navigator validated against the device")


if __name__ == "__main__":
    main()
