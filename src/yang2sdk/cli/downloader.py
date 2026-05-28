import os
import logging
from pathlib import Path
from dotenv import load_dotenv
from lxml import etree
from ncclient import manager

# Configure logging using standard pathways
log_file = Path.cwd() / "temp" / "yang_downloader.log"
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


class YangDownloader:
    """Download YANG models directly from a running network node."""

    def __init__(self, host, port, user, password, output_dir):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def get_schema_list(self, netconf_manager) -> list:
        """Retrieve list of schemas supported by target node."""
        filter_exp = """
        <netconf-state xmlns="urn:ietf:params:xml:ns:yang:ietf-netconf-monitoring">
            <schemas/>
        </netconf-state>
        """
        response = netconf_manager.get(filter=("subtree", filter_exp))
        root = etree.fromstring(response.xml.encode())
        namespaces = {"mon": "urn:ietf:params:xml:ns:yang:ietf-netconf-monitoring"}
        return root.xpath("//mon:schema", namespaces=namespaces)

    def download_all(self) -> None:
        """Iterate schemas and execute get-schema operations."""
        try:
            with manager.connect(
                host=self.host,
                port=self.port,
                username=self.user,
                password=self.password,
                hostkey_verify=False,
            ) as m:
                schemas = self.get_schema_list(m)
                print(
                    f"[*] Found {len(schemas)} schemas on node. Commencing extraction..."
                )

                for schema in schemas:
                    name_el = schema.find(
                        "{urn:ietf:params:xml:ns:yang:ietf-netconf-monitoring}identifier"
                    )
                    ver_el = schema.find(
                        "{urn:ietf:params:xml:ns:yang:ietf-netconf-monitoring}version"
                    )

                    if name_el is None:
                        continue

                    name = name_el.text
                    version = ver_el.text if ver_el is not None else None

                    filename = f"{name}@{version}.yang" if version else f"{name}.yang"
                    filepath = self.output_dir / filename

                    try:
                        content = m.get_schema(identifier=name, version=version).data
                        filepath.write_text(content, encoding="utf-8")
                        print(f"[+] Saved: {filepath}")
                    except Exception as e:
                        logger.error(f"Failed to fetch {name}: {e}")
                        print(f"[!] Failed to fetch {name}: {e}")

        except Exception as e:
            logger.critical(f"System extraction error: {e}", exc_info=True)
            print(f"CRITICAL SYSTEM ERROR: {e}")


def main():
    try:
        ip = os.environ["DEVICE_IP"]
        port = os.environ["NETCONF_PORT"]
        username = os.environ["DEVICE_USER"]
        password = os.environ["DEVICE_PASS"]
        device_name = os.environ["DEVICE_NAME"]
    except KeyError as e:
        print(f"Environment configuration missing key: {e}")
        return

    output_path = Path.cwd() / "temp" / "yang_modules" / device_name
    extractor = YangDownloader(
        host=ip,
        port=int(port),
        user=username,
        password=password,
        output_dir=output_path,
    )
    extractor.download_all()


if __name__ == "__main__":
    main()
