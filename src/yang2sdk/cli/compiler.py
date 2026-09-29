import argparse
import importlib.resources as pkg_resources
import os
import sys
from collections.abc import Generator, Iterable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pyang.scripts.pyang_tool import run

import yang2sdk.plugin as entry_pkg
from yang2sdk.cli.features import (
    FEATURES_FILENAME,
    read_features_file,
    to_pyang_args,
)


@dataclass(frozen=True)
class FeatureSet:
    """Effective pyang feature whitelist plus where it came from.

    ``source`` is recorded in MANIFEST.yang-revisions.json so a generated
    package always states which feature set shaped it.
    """

    modules: dict[str, list[str]]
    source: str
    features_file: str = ""


def resolve_features(
    *,
    yang_dir: Path | None,
    manual: Iterable[str],
    features_file: str = "",
    use_device_features: bool = True,
) -> FeatureSet:
    """Combine the device-advertised feature set with manual ``mod:feature``.

    The device set comes from ``features.json`` written by ``yang-downloader``
    (see ``cli/features.py``). Manual features **union** with it: an operator
    may add features, never narrow them. Narrowing is exactly what produces a
    silently wrong model — pyang prunes ``if-feature "not X"`` subtrees the
    device actually has, and the strict models then reject real device data.

    No features file and no ``--feature`` yields an empty set, so nothing is
    forwarded to pyang and codegen stays byte-identical to previous releases.
    """
    manual_map: dict[str, list[str]] = {}
    for entry in manual:
        module, _, names = entry.partition(":")
        bucket = manual_map.setdefault(module, [])
        bucket.extend(n for n in names.split(",") if n)

    path = Path(features_file) if features_file else None
    if path is None and use_device_features and yang_dir is not None:
        candidate = Path(yang_dir) / FEATURES_FILENAME
        path = candidate if candidate.is_file() else None
    device_map: dict[str, list[str]] = (
        read_features_file(path) if path is not None else {}
    )

    effective: dict[str, list[str]] = {
        module: sorted(set(names)) for module, names in device_map.items()
    }
    for module, names in manual_map.items():
        effective[module] = sorted(set(effective.get(module, [])) | set(names))

    if device_map and manual_map:
        source = "device+manual"
    elif device_map:
        source = "device"
    elif manual_map:
        source = "manual"
    else:
        source = "none"
    return FeatureSet(
        modules=effective,
        source=source,
        features_file=str(path) if path is not None else "",
    )


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
        pyang_features: list[str] | None = None,
        features_source: str = "none",
        ignore_errors: list[str] | None = None,
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
        self.features_source: str = features_source
        # Native pyang whitelists: "<modname>:<feature>,<feature>". One argument
        # per module — pyang_tool.parse_features_string splits on the first ":"
        # and repeating "--features mod:feat" would be read as a file name.
        self.pyang_features: list[str] = list(pyang_features or [])
        # pyang error tags to downgrade, e.g. XPATH_SYNTAX_ERROR for a vendor
        # model that ships an illegal `when` expression. Generic pass-through:
        # the decision (and its documentation) stays with the operator/adapter,
        # it is never baked in for a specific vendor.
        self.ignore_errors: list[str] = list(ignore_errors or [])

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
        for feat in self.pyang_features:
            injected_args.extend(["--features", feat])
        if self.features_source:
            injected_args.extend(["--sdk-features-source", self.features_source])
        for tag in self.ignore_errors:
            injected_args.extend(["--ignore-error", tag])

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
        help=(
            "Additional enabled feature as mod:feature (repeatable). Unions with "
            "the device feature set; it can add features but never narrow them."
        ),
    )
    parser.add_argument(
        "--features-file",
        dest="features_file",
        default="",
        metavar="FEATURES_JSON",
        help=(
            "Device feature set from yang-downloader. Defaults to "
            "<yang-dir>/features.json when that file exists."
        ),
    )
    parser.add_argument(
        "--no-device-features",
        dest="use_device_features",
        action="store_false",
        help="Ignore <yang-dir>/features.json and compile with pyang defaults.",
    )
    parser.add_argument(
        "--ignore-error",
        dest="ignore_errors",
        action="append",
        default=[],
        metavar="ERROR_TAG",
        help=(
            "Downgrade a pyang error tag (repeatable), e.g. XPATH_SYNTAX_ERROR "
            "for a vendor model that ships an illegal XPath. Use only with a "
            "documented reason; the model is still generated."
        ),
    )
    parser.add_argument(
        "--check-model-gaps",
        dest="check_model_gaps",
        default="",
        metavar="FEATURES_JSON",
        help=(
            "Opt-in: after compiling, diff this model against a compile using the "
            "device feature set from features.json (see cli/model_gaps.py). Exits "
            "non-zero when the device has data nodes the model lacks. NETCONF only."
        ),
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

    yang_dir = yangs_dir(parsed_args)
    feature_set = resolve_features(
        yang_dir=yang_dir,
        manual=parsed_args.features,
        features_file=parsed_args.features_file,
        use_device_features=parsed_args.use_device_features,
    )

    compile_in_process(
        format_type=format_type,
        yang_modules=parsed_args.yang_modules,
        device=parsed_args.device,
        yang_dir=str(yang_dir),
        output_dir=parsed_args.output_dir,
        config_only=parsed_args.config_only,
        device_version=parsed_args.device_version,
        package_version=parsed_args.package_version,
        deviation_modules=parsed_args.deviation_modules,
        features=[
            f"{module}:{name}"
            for module, names in sorted(feature_set.modules.items())
            for name in names
        ],
        pyang_features=to_pyang_args(feature_set.modules),
        features_source=feature_set.source,
        ignore_errors=parsed_args.ignore_errors,
    )

    if parsed_args.check_model_gaps:
        _run_model_gap_check(parsed_args, yang_dir, format_type)


def yangs_dir(parsed_args: argparse.Namespace) -> Path:
    """YANG search dir for this invocation (workspace default when unset)."""
    return (
        Path(parsed_args.yang_dir)
        if parsed_args.yang_dir
        else Path.cwd() / "temp" / "yang_modules" / parsed_args.device
    )


def compile_in_process(
    *,
    format_type: Literal["restconf", "netconf"],
    yang_modules: Sequence[str],
    device: str,
    yang_dir: str | None = None,
    output_dir: str | None = None,
    config_only: bool = False,
    device_version: str = "",
    package_version: str = "",
    deviation_modules: Sequence[str] = (),
    features: Sequence[str] = (),
    pyang_features: Sequence[str] = (),
    features_source: str = "none",
    ignore_errors: Sequence[str] = (),
) -> None:
    """Programmatic compile (no argparse) — the single seam both the CLI and
    ``cli/model_gaps.py`` compile through. ``pyang_features`` forwards native
    pyang ``<mod>:<feature>,...`` whitelists; empty means pyang's default
    (every feature supported), i.e. byte-identical to previous releases.
    ``features_source`` is recorded in MANIFEST.yang-revisions.json.
    """
    try:
        plugin_dir = Path(str(pkg_resources.files(entry_pkg))).resolve()
    except Exception as e:  # noqa: BLE001 -- resource resolution varies by installer; fail fast with message
        print(
            f"Error: Failed to resolve core compiler plugin location: {e}",
            file=sys.stderr,
        )
        sys.exit(1)

    yangs = (
        Path(yang_dir) if yang_dir else Path.cwd() / "temp" / "yang_modules" / device
    )
    out = (
        Path(output_dir)
        if output_dir
        else Path.cwd() / "temp" / f"{format_type}_clients" / device
    )

    Compiler(
        format_type=format_type,
        output_dir=out,
        plugin_dir=plugin_dir,
        yangs_dir=yangs,
        yang_modules=[Path(m) for m in yang_modules],
        config_only=config_only,
        device=device,
        device_version=device_version,
        package_version=package_version,
        deviation_modules=[Path(m) for m in deviation_modules],
        features=list(features),
        pyang_features=list(pyang_features),
        features_source=features_source,
        ignore_errors=list(ignore_errors),
    ).compile()


def _run_model_gap_check(
    parsed_args: argparse.Namespace, yangs_dir: Path, format_type: str
) -> None:
    """Opt-in post-compile gap report. Measurement only: never touches codegen."""
    if format_type != "netconf":
        print(
            "Error: --check-model-gaps is implemented for the netconf target only; "
            "re-run without it for restconf.",
            file=sys.stderr,
        )
        sys.exit(2)
    from yang2sdk.cli.model_gaps import ModelGapsError, run_gap_check

    try:
        report = run_gap_check(
            roots=[str(m) for m in parsed_args.yang_modules],
            yang_dir=yangs_dir,
            features_file=Path(parsed_args.check_model_gaps),
            device=parsed_args.device,
            device_version=parsed_args.device_version,
            config_only=parsed_args.config_only,
            ignore_errors=parsed_args.ignore_errors,
        )
    except (ModelGapsError, OSError, ValueError) as e:
        print(f"Error: model-gap check failed: {e}", file=sys.stderr)
        sys.exit(2)
    print(report.render())
    if report.missing_from_default:
        print(
            f"Error: {len(report.missing_from_default)} data node(s) exist on the device "
            'but not in the generated model (read risk with extra="forbid"). '
            "Recompile forwarding the device feature set (see --feature / "
            "cli/features.py).",
            file=sys.stderr,
        )
        sys.exit(1)


def restconf() -> None:
    """CLI Entrypoint for RESTCONF compiler targets."""
    run_compiler("restconf", sys.argv[1:])


def netconf() -> None:
    """CLI Entrypoint for NETCONF compiler targets."""
    run_compiler("netconf", sys.argv[1:])
