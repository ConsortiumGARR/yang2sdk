"""
YANG to Pydantic v2 converter plugin (Refactored to IR + Jinja2)

Converts YANG modules to Pydantic v2 Python classes with proper handling of:
- Data nodes (container, list, leaf, leaf-list, choice/case)
- Groupings and uses statements
- RPCs and notifications
- Type mappings with validation
- RFC 7951 JSON encoding compliance
"""

import optparse
import os
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from pyang import plugin

from yang2sdk.plugin.src.ir import IRBuilder

TEMPLATES_DIR = Path(__file__).parent / "templates"


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
        optlist = [
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
        ]
        g = optparser.add_option_group("Pydantic output specific options")
        g.add_options(optlist)

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
        # Covered by RESTCONF parsing options mapping transparently
        pass

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
            model_out = env.get_template("netconf/data_models/models.py.jinja").render(module=ir_mod)
            with open(os.path.join(models_dir, f"{ir_mod.py_name}.py"), "w") as f:
                f.write(model_out)

            if ir_mod.nav_nodes:
                nav_out = env.get_template("netconf/data_navigators/navigators.py.jinja").render(module=ir_mod)
                with open(os.path.join(navigators_dir, f"{ir_mod.py_name}.py"), "w") as f:
                    f.write(nav_out)

        all_data_props, all_rpc_props, module_names = [], [], []
        for mod in ir_modules:
            module_names.append(mod.py_name)
            all_data_props.extend(mod.root_data_props)
            all_rpc_props.extend(mod.root_rpc_props)

        with open(os.path.join(models_dir, "__init__.py"), "w") as f:
            f.write(env.get_template("netconf/data_models/__init__.py.jinja").render(
                module_names=module_names, data_props=all_data_props, rpc_props=all_rpc_props
            ))

        with open(os.path.join(navigators_dir, "__init__.py"), "w") as f:
            f.write(env.get_template("netconf/data_navigators/__init__.py.jinja").render(
                module_names=module_names, data_props=all_data_props, rpc_props=all_rpc_props
            ))

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

        fd.write(f"Generated NETCONF SDK in: {output_dir}\n")