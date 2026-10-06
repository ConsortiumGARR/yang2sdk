"""Download YANG modules from a lab device. LAB ONLY — never production."""

import logging
import os
import sys
from pathlib import Path

from yang2sdk.cli.features import (
    FEATURES_FILENAME,
    parse_capability_features,
    write_features_file,
)

try:
    from dotenv import load_dotenv
    from lxml import etree  # ty: ignore[unresolved-import] - lxml ships no stubs
    from ncclient import manager
except ImportError as e:
    raise ImportError(
        "yang-downloader needs the lab extra: pip install 'yang2sdk[lab]' "
        "(or `uv sync --extra lab` for development)"
    ) from e

logger = logging.getLogger(__name__)

_configured = False


def _configure_logging() -> None:
    """Configure logging lazily (no import-time side effects).

    Importing this module must not create files: the previous top-level
    ``basicConfig(FileHandler(temp/yang_downloader.log))`` at DEBUG level
    grew a 118MB log as a side effect of ``--help`` or any test import.
    Default is INFO to stderr; an opt-in rotating file is enabled only via
    ``YANG_DOWNLOADER_LOG_FILE`` (5MB x 3 backups). ``YANG_DOWNLOADER_DEBUG=1``
    selects DEBUG.
    """
    global _configured
    if _configured:
        return
    level = (
        logging.DEBUG
        if os.environ.get("YANG_DOWNLOADER_DEBUG") == "1"
        else logging.INFO
    )
    log_file = os.environ.get("YANG_DOWNLOADER_LOG_FILE", "")
    handlers: list[logging.Handler]
    if log_file:
        from logging.handlers import RotatingFileHandler

        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers = [
            RotatingFileHandler(path, maxBytes=5_000_000, backupCount=3),
            logging.StreamHandler(),
        ]
    else:
        handlers = [logging.StreamHandler()]
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=handlers,
        force=True,
    )
    _configured = True


def _revision_key(version: str) -> tuple:
    """Order a YANG revision date for comparison.

    RFC 7950 Sec 4.1: the `revision` argument is `YYYY-MM-DD`, optionally with
    a `:HH:MM` clock part. A plain string compare would order `2025-02-04`
    above `2025-2-4`-style values wrongly and, more importantly, cannot rank
    a dated revision against a malformed one. Unparseable parts sort first so
    a well-formed revision always wins, and the raw string is the final
    tie-breaker so the result is total and deterministic.
    """
    head, _, clock = version.partition(":")
    try:
        date = tuple(int(part) for part in head.split("-"))
    except ValueError:
        return (0, (), 0, version)
    if len(date) != 3:
        return (0, (), 0, version)
    try:
        time = tuple(int(part) for part in clock.split("-")) if clock else (0, 0, 0)
    except ValueError:
        time = (0, 0, 0)
    return (1, date, time, version)


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

    def download_all(self) -> int:
        """Iterate schemas and execute get-schema operations.

        Returns the number of schemas that could not be fetched, so the CLI
        can exit non-zero. It previously returned None and printed
        "CRITICAL SYSTEM ERROR", so a total extraction failure still exited 0
        and looked like success to any script or CI step.
        """
        _configure_logging()
        logger.warning(
            "hostkey_verify=False is a lab-only opt-out; "
            "never use the downloader against production"
        )
        try:
            with manager.connect(  # type: ignore[union-attr] - ncclient stubs
                host=self.host,
                port=self.port,
                username=self.user,
                password=self.password,
                hostkey_verify=False,
            ) as m:
                schemas = self.get_schema_list(m)
                saved = 0
                failed: list[str] = []
                # RFC 6241 Sec 8.3: the <hello> capabilities carry the exact
                # per-module feature set the device implements. Persist it so a
                # later compile can reproduce the device's model instead of
                # pyang's "all features supported" default (see cli/features.py).
                features = parse_capability_features(m.server_capabilities)
                write_features_file(
                    self.output_dir / FEATURES_FILENAME,
                    features,
                    source="netconf-hello",
                )
                print(
                    f"[*] Found {len(schemas)} schemas on node. "
                    f"Advertised features for {len(features)} module(s). "
                    "Commencing extraction..."
                )

                # RFC 6022 Sec 3.1.2: a server may advertise several
                # revisions of one module, and it encodes/returns data using
                # the *newest* revision it supports unless a client asks for
                # another. Saving every advertised revision into one directory
                # left the choice to pyang, which took the oldest -- so on
                # Cisco IOS-XR (which ships 3 revisions of many modules) the
                # generated model described an older schema than the device
                # actually served. Because the models are strict
                # (extra="forbid"), every read of an affected node then failed
                # validation locally. Keep only the newest revision per module
                # and say which older ones were dropped.
                newest: dict[str, tuple[str, str]] = {}
                undated: list[tuple[str, str]] = []
                for schema in schemas:
                    name_el = schema.find(
                        "{urn:ietf:params:xml:ns:yang:ietf-netconf-monitoring}identifier"
                    )
                    ver_el = schema.find(
                        "{urn:ietf:params:xml:ns:yang:ietf-netconf-monitoring}version"
                    )
                    if name_el is None or not name_el.text:
                        continue
                    name = name_el.text
                    version = ver_el.text if ver_el is not None else None
                    if version is None:
                        undated.append((name, name))
                        continue
                    current = newest.get(name)
                    if current is None or _revision_key(version) > _revision_key(
                        current[0]
                    ):
                        newest[name] = (version, name)

                duplicates = 0
                for name, (version, _) in newest.items():
                    advertised = sum(
                        1
                        for other in schemas
                        if (
                            other.findtext(
                                "{urn:ietf:params:xml:ns:yang:ietf-netconf-monitoring}"
                                "identifier"
                            )
                            == name
                        )
                    )
                    if advertised > 1:
                        duplicates += 1
                if duplicates:
                    print(
                        f"[*] {duplicates} module(s) advertise multiple revisions; "
                        "keeping only the newest, which is the one the device "
                        "encodes data with (RFC 6022 Sec 3.1.2)."
                    )

                wanted: list[tuple[str, str | None, str]] = [
                    (n, v, f"{n}@{v}.yang") for n, (v, _) in newest.items()
                ]
                wanted += [(n, None, f"{n}.yang") for n, _ in undated]

                for name, version, filename in wanted:
                    filepath = self.output_dir / filename

                    try:
                        content = m.get_schema(identifier=name, version=version).data
                        filepath.write_text(content, encoding="utf-8")
                        print(f"[+] Saved: {filepath}")
                        saved += 1
                    except Exception:
                        # Broad: one broken schema must not abort the whole
                        # extraction. logger.exception records the traceback,
                        # which also satisfies BLE001 (no blind swallow).
                        logger.exception(f"Failed to fetch {name}")
                        print(f"[!] Failed to fetch {name}")
                        failed.append(name)

            print(f"[*] Extraction finished: {saved} saved, {len(failed)} failed")
            if saved == 0 and failed:
                print("[!] No schema could be extracted at all.", file=sys.stderr)
                return len(failed)
            return len(failed)

        except Exception as e:
            logger.critical(f"System extraction error: {e}", exc_info=True)
            print(f"CRITICAL SYSTEM ERROR: {e}", file=sys.stderr)
            return 1


def main():
    _configure_logging()
    load_dotenv()
    try:
        ip = os.environ["DEVICE_IP"]
        port = os.environ["NETCONF_PORT"]
        username = os.environ["DEVICE_USER"]
        password = os.environ["DEVICE_PASS"]
        device_name = os.environ["DEVICE_NAME"]
    except KeyError as e:
        print(f"Environment configuration missing key: {e}", file=sys.stderr)
        sys.exit(2)

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
