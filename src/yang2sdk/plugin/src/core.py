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
import sys
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


def _refuse_on_error(ctx, modules) -> bool:
    """Exit before writing anything if YANG errors survived --ignore-error.

    pyang prints errors and then calls the emitter anyway, exiting non-zero
    only afterwards, so without this both happen: a complete client on disk
    AND a failure for the same run. Measured 2026-10-16 with a `confirmation`
    statement (which belongs to ietf-yang-metadata, not the core grammar): the
    models were byte-identical to a clean compile and the exit was still 1.
    Two ways that misleads -- a CI job reddens on a good artifact, and a human
    trusting "Generated SDK in: <dir>" publishes a model built from rejected
    YANG.
    """
    unsuppressed = _unsuppressed_errors(ctx, modules)
    if not unsuppressed:
        return False
    tags = sorted({tag for _, tag, _ in unsuppressed})
    print(
        f"Error: refusing to generate: {len(unsuppressed)} YANG error(s) "
        f"({', '.join(tags)}). No client written. Fix the model, or pass "
        "--ignore-error <TAG> for the errors you accept.",
        file=sys.stderr,
    )
    sys.exit(1)


def _unsuppressed_errors(ctx, modules) -> list[tuple]:
    """Errors pyang would actually REPORT, minus anything downgraded.

    pyang filters three times before printing, and this guard has to apply the
    same filters or it becomes stricter than the tool it wraps:

    * the blanket ``--ignore-errors`` empties ``ctx.errors`` outright;
    * a per-tag ``--ignore-error`` skips printing but leaves the entry there;
    * and, crucially, ``setup_fmt`` sets ``ctx.implicit_errors = False``, which
      makes pyang skip errors belonging to modules pulled in by ``import`` rather
      than named on the command line (pyang_tool.py:502-509). Those entries stay
      in ``ctx.errors``.

    Without that third filter the guard refused to generate on the SR Linux
    tree: 58 errors, all UNUSED_IMPORT / XPATH_NODE_NOT_FOUND1 from implicitly
    imported modules, none of which pyang prints. The guard must be exactly as
    strict as the reporting, or a green blocking job turns red.
    """
    from pyang import error as pyang_error

    opts = ctx.opts
    if getattr(opts, "ignore_errors", False):
        return []
    ignored = set(getattr(opts, "ignore_error_tags", None) or [])
    # pyang's own rule (pyang_tool.py:512-522): a warning-level tag is only a
    # failure when the operator promoted it with `--errors`. Without this the
    # guard refused the SR Linux tree on 43 UNUSED_IMPORT / XPATH_NODE_NOT_FOUND1
    # entries that pyang prints as `warning:` and does not fail on.
    promoted = set(getattr(opts, "errors", None) or [])
    modulenames = {m.arg for m in modules}
    # pyang's `filenames` is a local in run() (pyang_tool.py:230), not an
    # option, so it cannot be read off ctx.opts. The command-line files become
    # modules carrying that same path as `.ref`, which is where it comes from.
    filenames = {getattr(m, "ref", None) for m in modules}

    out: list[tuple] = []
    for entry in getattr(ctx, "errors", []):
        epos, etag, _eargs = entry
        if etag in ignored:
            continue
        level = pyang_error.err_level(etag)
        if pyang_error.is_warning(level) and etag not in promoted:
            continue
        top = getattr(epos, "top", None)
        if (
            getattr(ctx, "implicit_errors", True) is False
            and top is not None
            and top.arg not in modulenames
            and (
                not hasattr(top, "i_modulename") or top.i_modulename not in modulenames
            )
            and epos.ref not in filenames
        ):
            continue
        out.append(entry)
    return out


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

        # A YANG error means the model is not trustworthy, so nothing is
        # written and nothing is announced. pyang prints errors and then calls
        # this emitter anyway, exiting non-zero only afterwards -- which had us
        # writing a complete, working client AND reporting failure for the same
        # run. Two ways that misleads: a CI job goes red on a good artifact, and
        # a human who trusts "Generated SDK in: <dir>" publishes a model built
        # from YANG pyang rejected.
        #
        # `--ignore-error <TAG>` is the escape hatch and stays authoritative:
        # pyang keeps a downgraded tag in ctx.errors, so the operator's
        # downgrades are re-checked here rather than assumed (and the blanket
        # --ignore-errors clears ctx.errors outright, so nothing survives).
        # Measured 2026-10-16: a `confirmation` statement (which belongs to
        # ietf-yang-metadata, not to the core grammar) produced byte-identical
        # models and exit 1 at the same time.
        if _refuse_on_error(ctx, modules):
            return
        # `temp/` is untracked and created on demand: ensure the output root
        # exists before any write (makedirs below would also create it, but
        # the package/static writers must not depend on that ordering).
        os.makedirs(output_dir, exist_ok=True)
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

        if _refuse_on_error(ctx, modules):
            return

        # `temp/` is untracked and created on demand: ensure the output root
        # exists before any write (see the restconf emit above).
        os.makedirs(output_dir, exist_ok=True)
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

        # Every module pyang loaded, name -> namespace, merged across the roots.
        # An identityref value is "module-name:identity" (RFC 7951 Sec 6.8), an
        # XML QName whose prefix MUST be declared (RFC 7950 Sec 9.10.3). Handing
        # this to the client makes the write path namespace-complete without
        # depending on the peer advertising the module in its <hello>.
        merged_module_namespaces: dict[str, str] = {}
        for ir_module in ir_modules:
            merged_module_namespaces.update(getattr(ir_module, "module_namespaces", {}))

        for target_path, template_path in static_files.items():
            full_path = os.path.join(output_dir, target_path)
            with open(full_path, "w") as f:
                f.write(
                    env.get_template(template_path).render(
                        module_namespaces_literal=repr(merged_module_namespaces),
                    )
                )

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
    # Defensive: `emit()` already created this, but `_write_package_files`
    # must not depend on call ordering (model_gaps introspection, tests).
    out.mkdir(parents=True, exist_ok=True)
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
        f'packages = ["{pkg_name}"]\n\n'
        # The package is emitted twice on purpose: flat at out_dir so the lab
        # harness can `import temp.<proto>_clients.<device>`, and namespaced at
        # out_dir/<package_name>/ so pip installs `import <package_name>`. Only
        # the namespaced copy belongs in a published artifact. The wheel already
        # carried just it; an sdist includes the whole project tree and shipped
        # both (measured: a 25-entry tarball with two data_models/, two
        # data_navigators/, two __init__.py, two session_manager.py, two
        # py.typed). Excluding the flat copy is safe because the wheel target
        # references the namespaced path, so a wheel built from the extracted
        # sdist is unaffected.
        "[tool.hatch.build.targets.sdist]\n"
        'exclude = ["/__init__.py", "/data_models", "/data_navigators",'
        ' "/session_manager.py", "/py.typed"]\n',
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
    pkg_dir.mkdir(parents=True, exist_ok=True)
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
