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
        print(f"Environment configuration missing key: {e}")
        return

    # Add active working directory to sys.path to locate transient generated structures
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
                pydantic_instance = navigator.retrieve(
                    content="config", depth="unbounded"
                )
                print(f"  [OK] Parsed model: {pydantic_instance.__class__.__name__}")
                logger.info(
                    f"  [OK] Parsed model: {pydantic_instance.__class__.__name__}"
                )
            except Exception as e:
                logger.exception(f"  [FAIL] {navigator._path}")
                print(f"  [FAIL] {navigator._path} - Error: {e}")


if __name__ == "__main__":
    main()
