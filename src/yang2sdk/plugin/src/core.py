"""
YANG to Pydantic v2 converter plugin (Refactored to IR + Jinja2)

Converts YANG modules to Pydantic v2 Python classes with proper handling of:
- Data nodes (container, list, leaf, leaf-list, choice/case)
- Groupings and uses statements
- RPCs and notifications
- Type mappings with validation
- RFC 7951 JSON encoding compliance
"""

import json
import optparse
import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from pyang import plugin

from yang2sdk.plugin.src.ir import IRBuilder

TEMPLATES_DIR = Path(__file__).parent / "templates"


def _sdk_options() -> list:
    return [
        optparse.make_option(
            "--sdk-output-dir",
            dest="sdk_output_dir",
            default="./generated_sdk",
            help="Output directory",
        ),
        optparse.make_option(
            "--sdk-config-only",
            dest="sdk_config_only",
            action="store_true",
            help="Only config true nodes",
        ),
        optparse.make_option(
            "--sdk-device",
            dest="sdk_device",
            default="",
            help="Device name for package provenance (PEP440 pkg <device>_<version>).",
        ),
        optparse.make_option(
            "--sdk-device-version",
            dest="sdk_device_version",
            default="",
            help="Device OS version for package provenance.",
        ),
        optparse.make_option(
            "--sdk-package-version",
            dest="sdk_package_version",
            default="",
            help="PEP440 package version (defaults to device version).",
        ),
        optparse.make_option(
            "--sdk-deviation-module",
            dest="sdk_deviation_module",
            action="append",
            default=[],
            help="Deviation module file applied (repeatable, recorded in MANIFEST).",
        ),
        optparse.make_option(
            "--sdk-feature",
            dest="sdk_feature",
            action="append",
            default=[],
            help="Enabled feature mod:feature (repeatable, recorded in MANIFEST).",
        ),
        optparse.make_option(
            "--sdk-features-source",
            dest="sdk_features_source",
            default="none",
            help="Where the effective feature set came from: "
            "device|manual|device+manual|none (recorded in MANIFEST).",
        ),
    ]


def _add_sdk_options_once(optparser) -> None:
    # Both restconf/netconf plugins register in one pyang process; adding the
    # same option twice raises OptionConflictError (breaks in-process compile
    # used by tests). Add only missing options.
    try:
        existing = set()
        for grp in getattr(optparser, "option_groups", []):
            for opt in getattr(grp, "option_list", []):
                existing.update(getattr(opt, "_long_opts", []))
        for opt in getattr(optparser, "option_list", []):
            existing.update(getattr(opt, "_long_opts", []))
    except Exception:  # noqa: BLE001 -- optparse internals vary; fall back to try/add
        existing = set()
    missing = [o for o in _sdk_options() if o._long_opts[0] not in existing]
    if not missing:
        return
    g = optparser.add_option_group("Pydantic output specific options")
    g.add_options(missing)


def _sanitize_pkg(name: str) -> str:
    pkg = re.sub(r"[^A-Za-z0-9_]", "_", name)
    if not pkg or not (pkg[0].isalpha() or pkg[0] == "_"):
        pkg = f"pkg_{pkg}"
    return pkg


def _pep440_version(raw: str) -> str:
    # Best-effort OS-version → PEP440 (23.4R1 → 23.4.1, 10.4-4 → 10.4.4).
    v = re.sub(r"[^0-9A-Za-z_.+-]", ".", raw.strip())
    v = re.sub(r"[Rr](\d+)", r".\1", v)
    v = v.replace("-", ".").replace("_", ".")
    v = re.sub(r"\.+", ".", v).strip(".")
    return v or "0.0.0"


def _package_context(ctx, modules, ir_modules, protocol: str) -> dict:
    device = getattr(ctx.opts, "sdk_device", "") or "device"
    device_version = getattr(ctx.opts, "sdk_device_version", "") or "0.0.0"
    pkg_version_raw = getattr(ctx.opts, "sdk_package_version", "") or device_version
    deviations = list(getattr(ctx.opts, "sdk_deviation_module", None) or [])
    features = list(getattr(ctx.opts, "sdk_feature", None) or [])
    try:
        from yang2sdk import __version__ as gen_version
    except Exception:  # noqa: BLE001 -- installed metadata may be missing
        gen_version = "unknown"
    pkg_name = _sanitize_pkg(
        f"{device}_{device_version}".replace(".", "_").replace("-", "_")
    )
    pkg_version = _pep440_version(pkg_version_raw)
    mods = []
    for m, ir in zip(modules, ir_modules):
        rev = ""
        try:
            r = m.search_one("revision")
            rev = r.arg if r else ""
        except Exception:  # noqa: BLE001 -- pyang AST variance
            rev = getattr(ir, "revision", "")
        mods.append(
            {
                "name": m.arg,
                "revision": rev or getattr(ir, "revision", ""),
                "namespace": getattr(ir, "namespace", ""),
            }
        )
    # Reproducibility: a wall-clock stamp makes two compiles of identical YANG
    # differ, so `diff -r` can never be clean and no generated output can ever
    # be committed as a golden fixture. Honour SOURCE_DATE_EPOCH (the
    # reproducible-builds convention) and omit the field entirely otherwise,
    # so the default build is byte-identical run to run.
    created_utc = ""
    source_date_epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if source_date_epoch:
        try:
            created_utc = (
                datetime.fromtimestamp(int(source_date_epoch), tz=UTC)
                .isoformat()
                .replace("+00:00", "Z")
            )
        except (ValueError, OverflowError, OSError):
            created_utc = ""
    return {
        "package_name": pkg_name,
        "package_version": pkg_version,
        "device": device,
        "device_version": device_version,
        "protocol": protocol,
        "generator_version": gen_version,
        "modules": mods,
        "deviations": deviations,
        "features": features,
        "features_source": getattr(ctx.opts, "sdk_features_source", "none") or "none",
        "created_utc": created_utc,
    }


def pyang_plugin_init():
    """Register the plugin"""
    plugin.register_plugin(Yang2Restconf())
    plugin.register_plugin(Yang2Netconf())


class Yang2Restconf(plugin.PyangPlugin):
    """Main plugin class for YANG to Pydantic conversion"""

    def __init__(self):
        plugin.PyangPlugin.__init__(self, "yang2restconf")
        self.multiple_modules = True

    def add_output_format(self, fmts):
        fmts["restconf"] = self

    def add_opts(self, optparser):
        _add_sdk_options_once(optparser)

    def setup_fmt(self, ctx):
        ctx.implicit_errors = False

    def emit(self, ctx, modules, fd):
        """Main emission function orchestration."""
        output_dir = ctx.opts.sdk_output_dir
        config_only = ctx.opts.sdk_config_only

        models_dir = os.path.join(output_dir, "data_models")
        navigators_dir = os.path.join(output_dir, "data_navigators")

        for d in [models_dir, navigators_dir]:
            os.makedirs(d, exist_ok=True)

        env = Environment(
            loader=FileSystemLoader(TEMPLATES_DIR), trim_blocks=True, lstrip_blocks=True
        )
        ir_modules = []

        # 1. Parse AST to IR
        for module in modules:
            builder = IRBuilder(ctx, module, config_only)
            ir_module = builder.build()
            ir_modules.append(ir_module)

        # 2. Render IR with Jinja2
        for ir_mod in ir_modules:
            model_out = env.get_template("restconf/data_models/models.py.jinja").render(
                module=ir_mod
            )
            with open(os.path.join(models_dir, f"{ir_mod.py_name}.py"), "w") as f:
                f.write(model_out)

            if ir_mod.nav_nodes:
                nav_out = env.get_template(
                    "restconf/data_navigators/navigators.py.jinja"
                ).render(module=ir_mod)
                with open(
                    os.path.join(navigators_dir, f"{ir_mod.py_name}.py"), "w"
                ) as f:
                    f.write(nav_out)

        # Global rendering
        all_data_props = []
        all_rpc_props = []
        module_names = []
        for mod in ir_modules:
            module_names.append(mod.py_name)
            all_data_props.extend(mod.root_data_props)
            all_rpc_props.extend(mod.root_rpc_props)

        with open(os.path.join(models_dir, "__init__.py"), "w") as f:
            f.write(
                env.get_template("restconf/data_models/__init__.py.jinja").render(
                    module_names=module_names,
                    data_props=all_data_props,
                    rpc_props=all_rpc_props,
                )
            )

        with open(os.path.join(navigators_dir, "__init__.py"), "w") as f:
            f.write(
                env.get_template("restconf/data_navigators/__init__.py.jinja").render(
                    module_names=module_names,
                    data_props=all_data_props,
                    rpc_props=all_rpc_props,
                )
            )

        # Static Scaffold files
        self._write_static_files(env, output_dir)
        _write_package_files(env, output_dir, ctx, modules, ir_modules, "restconf")
        fd.write(f"Generated SDK in: {output_dir}\n")

    def _write_static_files(self, env: Environment, out_dir: str):
        """Render and write the static scaffolding files from their templates."""
        static_files = {
            "__init__.py": "restconf/__init__.py.jinja",
            "session_manager.py": "restconf/session_manager.py.jinja",
            "data_models/_base.py": "restconf/data_models/_base.py.jinja",
            "data_navigators/_base.py": "restconf/data_navigators/_base.py.jinja",
        }

        for target_path, template_path in static_files.items():
            full_path = os.path.join(out_dir, target_path)
            with open(full_path, "w") as f:
                f.write(env.get_template(template_path).render())


class Yang2Netconf(plugin.PyangPlugin):
    """Main plugin class for YANG to NETCONF Pydantic-XML conversion"""

    def __init__(self):
        plugin.PyangPlugin.__init__(self, "yang2netconf")
        self.multiple_modules = True

    def add_output_format(self, fmts):
        fmts["netconf"] = self

    def add_opts(self, optparser):
        _add_sdk_options_once(optparser)

    def setup_fmt(self, ctx):
        ctx.implicit_errors = False

    def emit(self, ctx, modules, fd):
        output_dir = ctx.opts.sdk_output_dir
        config_only = ctx.opts.sdk_config_only

        models_dir = os.path.join(output_dir, "data_models")
        navigators_dir = os.path.join(output_dir, "data_navigators")

        for d in [models_dir, navigators_dir]:
            os.makedirs(d, exist_ok=True)

        env = Environment(
            loader=FileSystemLoader(TEMPLATES_DIR), trim_blocks=True, lstrip_blocks=True
        )
        ir_modules = []

        for module in modules:
            builder = IRBuilder(ctx, module, config_only, target_format="netconf")
            ir_modules.append(builder.build())

        for ir_mod in ir_modules:
            model_out = env.get_template("netconf/data_models/models.py.jinja").render(
                module=ir_mod
            )
            with open(os.path.join(models_dir, f"{ir_mod.py_name}.py"), "w") as f:
                f.write(model_out)

            if ir_mod.nav_nodes:
                nav_out = env.get_template(
                    "netconf/data_navigators/navigators.py.jinja"
                ).render(module=ir_mod)
                with open(
                    os.path.join(navigators_dir, f"{ir_mod.py_name}.py"), "w"
                ) as f:
                    f.write(nav_out)

        all_data_props, all_rpc_props, module_names = [], [], []
        for mod in ir_modules:
            module_names.append(mod.py_name)
            all_data_props.extend(mod.root_data_props)
            all_rpc_props.extend(mod.root_rpc_props)

        with open(os.path.join(models_dir, "__init__.py"), "w") as f:
            f.write(
                env.get_template("netconf/data_models/__init__.py.jinja").render(
                    module_names=module_names,
                    data_props=all_data_props,
                    rpc_props=all_rpc_props,
                )
            )

        with open(os.path.join(navigators_dir, "__init__.py"), "w") as f:
            f.write(
                env.get_template("netconf/data_navigators/__init__.py.jinja").render(
                    module_names=module_names,
                    data_props=all_data_props,
                    rpc_props=all_rpc_props,
                )
            )

        static_files = {
            "__init__.py": "netconf/__init__.py.jinja",
            "session_manager.py": "netconf/session_manager.py.jinja",
            "data_models/_base.py": "netconf/data_models/_base.py.jinja",
            "data_navigators/_base.py": "netconf/data_navigators/_base.py.jinja",
        }

        for target_path, template_path in static_files.items():
            full_path = os.path.join(output_dir, target_path)
            with open(full_path, "w") as f:
                f.write(env.get_template(template_path).render())

        _write_package_files(env, output_dir, ctx, modules, ir_modules, "netconf")
        fd.write(f"Generated NETCONF SDK in: {output_dir}\n")


def _validate_generated(output_dir: str) -> None:
    """Fail the build if any emitted .py file does not parse.

    A code generator that reports success while shipping a file Python cannot
    import is worse than one that crashes: the failure surfaces in the
    *consumer's* project as an opaque SyntaxError pointing at generated code.
    A YANG `description` containing a triple quote or a trailing backslash used
    to do exactly that. Validating here turns any future escaping bug into a
    build-time error with the offending file and line.
    """
    errors: list[str] = []
    for path in sorted(Path(output_dir).rglob("*.py")):
        try:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
        except SyntaxError as e:
            errors.append(f"  {path}:{e.lineno}: {e.msg}")
        except (OSError, UnicodeDecodeError) as e:  # pragma: no cover - IO guard
            errors.append(f"  {path}: {e}")
    if errors:
        raise SyntaxError(
            "Generated Python does not parse; refusing to ship a client that "
            "cannot be imported:\n" + "\n".join(errors)
        )


def _write_package_files(
    env: Environment, out_dir: str, ctx, modules, ir_modules, protocol: str
) -> None:
    """Emit registry-ready packaging (pyproject/README/MANIFEST/py.typed).

    Flat files at out_dir preserve the lab import path
    (temp.<proto>_clients.<device>); the namespaced copy at
    out_dir/<package_name>/ is what pip installs (import <package_name>).
    No secrets, no temp/ absolute imports, secure defaults documented.
    """
    pkg = _package_context(ctx, modules, ir_modules, protocol)
    pkg_name, pkg_version = pkg["package_name"], pkg["package_version"]
    out = Path(out_dir)
    # pyproject.toml (project root = out_dir)
    if protocol == "restconf":
        deps = ['"pydantic>=2.12.5"', '"requests>=2.32.5"']
    else:
        deps = [
            '"pydantic>=2.12.5"',
            '"pydantic-xml>=2.21.0"',
            '"lxml>=4.9.0"',
            '"ncclient>=0.7.0"',
        ]
    deps_str = ",\n    ".join(deps)
    (out / "pyproject.toml").write_text(
        "[build-system]\n"
        'requires = ["hatchling"]\n'
        'build-backend = "hatchling.build"\n\n'
        "[project]\n"
        f'name = "{pkg_name}"\n'
        f'version = "{pkg_version}"\n'
        f'description = "Generated {protocol.upper()} SDK for {pkg["device"]} {pkg["device_version"]}."\n'
        'readme = "README.md"\n'
        'requires-python = ">=3.12"\n'
        f"dependencies = [\n    {deps_str},\n]\n\n"
        "[tool.hatch.build.targets.wheel]\n"
        f'packages = ["{pkg_name}"]\n',
        encoding="utf-8",
    )
    mod_rows = "\n".join(
        f"| `{m['name']}` | `{m['revision'] or '-'}` | `{m['namespace']}` |"
        for m in pkg["modules"]
    )
    dev_rows = "\n".join(f"- `{d}`" for d in pkg["deviations"]) or "- none"
    feat_rows = "\n".join(f"- `{f}`" for f in pkg["features"]) or "- none"
    (out / "README.md").write_text(
        f"# {pkg_name}\n\n"
        f"Generated {protocol.upper()} SDK for `{pkg['device']}` "
        f"version `{pkg['device_version']}`.\n\n"
        f"- Protocol: `{protocol}`\n"
        f"- Generator: `yang2sdk {pkg['generator_version']}`\n"
        f"- Features: source `{pkg['features_source']}` "
        f"({len(pkg['features'])} enabled)\n"
        + (f"- Created (UTC): `{pkg['created_utc']}`\n" if pkg["created_utc"] else "")
        + "- Secure defaults: `verify=True` (explicit `verify=False` lab-only "
        "with warning); RESTCONF `scheme=https` default.\n"
        "- No credentials, IPs, or CA bundles are embedded; pass auth at runtime.\n\n"
        "## Install\n\n"
        "```bash\n"
        f"uv add path/to/{pkg_name}  # or --editable\n"
        "```\n\n"
        "## Modules\n\n"
        "| module | revision | namespace |\n"
        "|---|---|---|\n"
        f"{mod_rows}\n\n"
        "## Deviations applied\n\n"
        f"{dev_rows}\n\n"
        "## Features enabled\n\n"
        f"{feat_rows}\n\n"
        "## Use\n\n"
        "```python\n"
        + (
            f"from {pkg_name} import RestconfClient\n"
            "client = RestconfClient(management_ip=..., username=..., password=..., verify=True)\n"
            if protocol == "restconf"
            else f"from {pkg_name} import NetconfClient\n"
            "client = NetconfClient(management_ip=..., username=..., password=..., verify=True)\n"
        )
        + "```\n",
        encoding="utf-8",
    )
    (out / "MANIFEST.yang-revisions.json").write_text(
        json.dumps(pkg, indent=2), encoding="utf-8"
    )
    (out / "py.typed").write_text("", encoding="utf-8")
    # Namespaced installable copy: out/<pkg_name>/...
    pkg_dir = out / pkg_name
    pkg_dir.mkdir(exist_ok=True)
    for item in ("__init__.py", "session_manager.py", "py.typed"):
        src = out / item
        if src.exists():
            shutil.copy2(src, pkg_dir / item)
    for sub in ("data_models", "data_navigators"):
        src_d = out / sub
        dst_d = pkg_dir / sub
        if src_d.is_dir():
            if dst_d.exists():
                shutil.rmtree(dst_d)
            shutil.copytree(src_d, dst_d)
    # py.typed marker inside package (PEP 561)
    (pkg_dir / "py.typed").write_text("", encoding="utf-8")

    # Last step, covering both the flat lab copy and the namespaced copy: a
    # client that cannot be imported is not a deliverable.
    _validate_generated(str(out))
