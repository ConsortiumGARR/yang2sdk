import argparse
import importlib.resources as pkg_resources
import os
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from pyang.scripts.pyang_tool import run

import yang2sdk.plugin as entry_pkg


@contextmanager
def patch_sys_argv(new_argv: list[str]) -> Generator[None, None, None]:
    """Safely patch sys.argv temporarily to isolate internal tool executions.

    This ensures that in-process execution of tools reading from sys.argv (like pyang)
    do not pollute the global runtime context of parent applications or test runners.
    """
    original_argv = sys.argv[:]
    sys.argv = new_argv
    try:
        yield
    finally:
        sys.argv = original_argv


class Compiler:
    """Orchestrates in-process compilation of YANG modules to target Python SDK formats."""

    def __init__(
        self,
        format_type: Literal["restconf", "netconf"],
        output_dir: Path,
        plugin_dir: Path,
        yangs_dir: Path,
        yang_modules: list[Path],
        config_only: bool = False,
        device: str = "",
        device_version: str = "",
        package_version: str = "",
        deviation_modules: list[Path] | None = None,
        features: list[str] | None = None,
    ) -> None:
        self.format: Literal["restconf", "netconf"] = format_type
        self.output_dir: Path = Path(output_dir)
        self.plugin_dir: Path = Path(plugin_dir)
        self.yangs_dir: Path = Path(yangs_dir)
        self.yang_modules: list[Path] = [Path(m) for m in yang_modules]
        self.config_only: bool = config_only
        self.device: str = device
        self.device_version: str = device_version
        self.package_version: str = package_version
        self.deviation_modules: list[Path] = [
            Path(m) for m in (deviation_modules or [])
        ]
        self.features: list[str] = list(features or [])

    def compile(self) -> None:
        """Executes the compiler inside a safely isolated sys.argv context block."""
        injected_args = [
            "pyang",
            "-V",
            "--plugindir",
            str(self.plugin_dir),
            "-f",
            self.format,
            "--sdk-output-dir",
            str(self.output_dir),
            "--path",
            str(self.yangs_dir),
        ]

        if self.config_only:
            injected_args.append("--sdk-config-only")
        if self.device:
            injected_args.extend(["--sdk-device", self.device])
        if self.device_version:
            injected_args.extend(["--sdk-device-version", self.device_version])
        if self.package_version:
            injected_args.extend(["--sdk-package-version", self.package_version])
        for dev in self.deviation_modules:
            # Native pyang deviation application + provenance recording.
            injected_args.extend(["--deviation-module", str(dev)])
            injected_args.extend(["--sdk-deviation-module", str(dev)])
        for feat in self.features:
            injected_args.extend(["--sdk-feature", feat])

        injected_args.extend(str(module_path) for module_path in self.yang_modules)

        try:
            with patch_sys_argv(injected_args):
                run()
        except SystemExit as e:
            if e.code != 0:
                print(
                    f"Error: pyang exited with error status code {e.code}",
                    file=sys.stderr,
                )
                sys.exit(e.code)
        except Exception as e:  # noqa: BLE001 -- pyang run() raises arbitrary errors; CLI crash guard must not escape
            print(f"Compilation engine crashed unexpectedly: {e}", file=sys.stderr)
            sys.exit(1)


def run_compiler(format_type: Literal["restconf", "netconf"], argv: list[str]) -> None:
    """Unified configuration resolver and driver execution block.

    Validates program parameters, coordinates environment defaults, and passes
    sanitized instructions down to the compilation engine.
    """
    parser = argparse.ArgumentParser(
        description=f"Generate a Pydantic-based {format_type.upper()} SDK from target YANG modules.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--device",
        default=os.environ.get("DEVICE_NAME"),
        help="Target device name. Falls back to $DEVICE_NAME environment variable.",
    )
    parser.add_argument(
        "--yang-dir",
        help="Directory containing YANG source modules. Defaults to 'temp/yang_modules/<device_name>'.",
    )
    parser.add_argument(
        "--output-dir",
        help="Target directory for generated output. Defaults to 'temp/<format>_clients/<device_name>'.",
    )
    parser.add_argument(
        "--config-only",
        action="store_true",
        help="Configure output schemas to serialize and validate config-only nodes.",
    )
    parser.add_argument(
        "--device-version",
        default=os.environ.get("DEVICE_VERSION", ""),
        help="Device OS version for package provenance (MANIFEST + package name).",
    )
    parser.add_argument(
        "--package-version",
        default="",
        help="PEP440 package version (defaults to device version).",
    )
    parser.add_argument(
        "--deviation-module",
        dest="deviation_modules",
        action="append",
        default=[],
        help="Deviation module applied by pyang (repeatable, recorded in MANIFEST).",
    )
    parser.add_argument(
        "--feature",
        dest="features",
        action="append",
        default=[],
        help="Enabled feature as mod:feature (repeatable, recorded in MANIFEST).",
    )
    parser.add_argument(
        "yang_modules",
        nargs="+",
        help="Target YANG file paths to parse and convert.",
    )

    parsed_args = parser.parse_args(argv)

    if not parsed_args.device:
        print(
            "Error: Target device name must be specified. Use '--device <name>' or "
            "set the 'DEVICE_NAME' environment variable.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Clean default paths mapping back to the standard workspace layout
    yangs_dir = (
        Path(parsed_args.yang_dir)
        if parsed_args.yang_dir
        else Path.cwd() / "temp" / "yang_modules" / parsed_args.device
    )

    output_dir = (
        Path(parsed_args.output_dir)
        if parsed_args.output_dir
        else Path.cwd() / "temp" / f"{format_type}_clients" / parsed_args.device
    )

    try:
        plugin_dir = Path(str(pkg_resources.files(entry_pkg))).resolve()
    except Exception as e:  # noqa: BLE001 -- resource resolution varies by installer; fail fast with message
        print(
            f"Error: Failed to resolve core compiler plugin location: {e}",
            file=sys.stderr,
        )
        sys.exit(1)

    compiler = Compiler(
        format_type=format_type,
        output_dir=output_dir,
        plugin_dir=plugin_dir,
        yangs_dir=yangs_dir,
        yang_modules=[Path(m) for m in parsed_args.yang_modules],
        config_only=parsed_args.config_only,
        device=parsed_args.device,
        device_version=parsed_args.device_version,
        package_version=parsed_args.package_version,
        deviation_modules=[Path(m) for m in parsed_args.deviation_modules],
        features=list(parsed_args.features),
    )
    compiler.compile()


def restconf() -> None:
    """CLI Entrypoint for RESTCONF compiler targets."""
    run_compiler("restconf", sys.argv[1:])


def netconf() -> None:
    """CLI Entrypoint for NETCONF compiler targets."""
    run_compiler("netconf", sys.argv[1:])
