"""INTERMEDIATE REPRESENTATION (IR) DATACLASSES AND BUILDER"""

import hashlib
import keyword
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from pyang import statements


@dataclass
class IRField:
    name: str
    type_str: str
    assignment: str
    uses: list[str] = field(default_factory=list)


@dataclass
class IRModel:
    name: str
    description: str
    fields: list[IRField] = field(default_factory=list)
    is_rpc_envelope: bool = False
    rpc_input_cls: Optional[str] = None
    rpc_output_cls: Optional[str] = None


@dataclass
class IREnumValue:
    py_name: str
    value: str
    description: Optional[str] = None


@dataclass
class IREnum:
    name: str
    description: str
    values: list[IREnumValue] = field(default_factory=list)


@dataclass
class IRNavProperty:
    name: str
    type_hint: str
    nav_cls: str
    path_name: str
    yang_name: str
    item_cls: Optional[str] = None


@dataclass
class IRNavNode:
    node_type: str  # 'container', 'list', 'rpc'
    class_name: str
    item_class_name: Optional[str] = None
    list_class_name: Optional[str] = None
    path_name: str = ""
    yang_name: str = ""
    pydantic_module: str = ""
    module_yang_name: str = ""  # MUST BE ADDED FOR RPC NAMESPACES
    properties: list[IRNavProperty] = field(default_factory=list)
    has_input: bool = False
    has_output: bool = False


@dataclass
class IRParentProperty:
    # Used for global roots (__init__.py)
    module: str
    cls: str
    alias: str
    field: str
    node_type: str = "container"


@dataclass
class IRModule:
    name: str
    py_name: str
    models: list[IRModel] = field(default_factory=list)
    enums: list[IREnum] = field(default_factory=list)
    nav_nodes: list[IRNavNode] = field(default_factory=list)
    root_data_props: list[IRParentProperty] = field(default_factory=list)
    root_rpc_props: list[IRParentProperty] = field(default_factory=list)


class IRBuilder:
    """Walks the YANG AST and populates the pristine IR dataclasses."""

    def __init__(self, ctx, module, config_only):
        self.ctx = ctx
        self.module = module
        self.config_only = config_only

        self.groupings = {}
        self.enum_registry = {}
        self.uses_refs = {}

        self.ir = IRModule(name=module.arg, py_name=module.arg.replace("-", "_"))

    def _models_are_equivalent(self, m1: IRModel, m2: IRModel) -> bool:
        """Determines if two models are semantically equivalent."""
        if m1.is_rpc_envelope != m2.is_rpc_envelope:
            return False
        if m1.rpc_input_cls != m2.rpc_input_cls:
            return False
        if m1.rpc_output_cls != m2.rpc_output_cls:
            return False

        # Compare fields
        if len(m1.fields) != len(m2.fields):
            return False

        for f1, f2 in zip(m1.fields, m2.fields):
            if f1.name != f2.name:
                return False
            if f1.type_str != f2.type_str:
                return False
            if f1.assignment != f2.assignment:
                return False

        return True

    def _register_model(self, model: IRModel) -> str:
        """
        Registers a model in the module's IR list. If a model with the same name
        already exists, compares them for semantic equivalence:
        - If equivalent: Reuses the existing model's name.
        - If different: Generates a unique name by appending an incrementing suffix.
        """
        base_name = model.name
        candidate_name = base_name
        counter = 0

        while True:
            existing = next(
                (m for m in self.ir.models if m.name == candidate_name), None
            )
            if not existing:
                model.name = candidate_name
                self.ir.models.append(model)
                return candidate_name

            if self._models_are_equivalent(existing, model):
                return existing.name

            counter += 1
            candidate_name = f"{base_name}_{counter}"

    def build(self) -> IRModule:
        self._resolve_names(self.module)
        self._collect_groupings(self.module)

        for grouping in self.groupings.values():
            if self._get_module_name(grouping) == self.module.arg:
                cls_name = getattr(
                    grouping, "_pydantic_class_name", self._to_class_name(grouping.arg)
                )
                model = self._build_model(grouping, cls_name)
                if model:
                    final_name = self._register_model(model)
                    grouping._pydantic_class_name = final_name
                    self.uses_refs[grouping.arg] = final_name

        data_children = [
            ch
            for ch in self.module.i_children
            if ch.keyword in statements.data_definition_keywords
        ]
        for child in data_children:
            if child.keyword in ["container", "list"]:
                cls_name = getattr(
                    child, "_pydantic_class_name", self._to_class_name(child.arg)
                )
                if child.keyword == "list" and not cls_name.endswith("Item"):
                    cls_name += "Item"

                model = self._build_model(child, cls_name)
                if model:
                    final_name = self._register_model(model)
                    child._pydantic_class_name = final_name
                    cls_name = final_name

                self.ir.root_data_props.append(
                    IRParentProperty(
                        module=self.ir.py_name,
                        cls=cls_name,
                        alias=f"{self.module.arg}:{child.arg}",
                        field=self._to_field_name(child.arg),
                        node_type=child.keyword,
                    )
                )

        if data_children:
            root_model = self._build_model(
                self.module, f"{self._to_class_name(self.module.arg)}Data"
            )
            if root_model:
                self._register_model(root_model)

        rpcs = [ch for ch in self.module.i_children if ch.keyword == "rpc"]
        for rpc in rpcs:
            self._build_rpc(rpc)
            self.ir.root_rpc_props.append(
                IRParentProperty(
                    module=self.ir.py_name,
                    cls=getattr(
                        rpc, "_pydantic_class_name", self._to_class_name(rpc.arg)
                    ),
                    alias=f"{self.module.arg}:{rpc.arg}",
                    field=self._to_field_name(rpc.arg),
                    node_type="rpc",
                )
            )

        notifs = [ch for ch in self.module.i_children if ch.keyword == "notification"]
        for notif in notifs:
            n_name = getattr(
                notif, "_pydantic_class_name", self._to_class_name(notif.arg)
            )
            n_name = (
                n_name if n_name.endswith("Notification") else f"{n_name}Notification"
            )
            model = self._build_model(notif, n_name, bypass_config_check=True)
            if model:
                final_name = self._register_model(model)
                notif._pydantic_class_name = final_name

        # Build Navigator IR
        self._build_nav_nodes(self.module)

        return self.ir

    # --- NAVIGATOR IR BUILDER ---

    def _build_nav_nodes(self, stmt):
        if hasattr(stmt, "i_children"):
            for child in stmt.i_children:
                if child.keyword in ["container", "list", "rpc"]:
                    node = self._build_nav_node(child)
                    if node and not any(
                        n.class_name == node.class_name for n in self.ir.nav_nodes
                    ):
                        self.ir.nav_nodes.append(node)
                # Recurse regardless of whether the current node is a container/list/rpc
                self._build_nav_nodes(child)

    def _build_nav_node(self, stmt) -> Optional[IRNavNode]:
        cls_name = getattr(stmt, "_pydantic_class_name", self._to_class_name(stmt.arg))

        node = IRNavNode(
            node_type=stmt.keyword,
            class_name=cls_name,
            path_name=stmt.arg,
            yang_name=stmt.arg,
            pydantic_module=self.ir.py_name,
            module_yang_name=self.module.arg,  # Pass raw YANG namespace for JSON keys
            has_input=stmt.search_one("input") is not None
            if stmt.keyword == "rpc"
            else False,
            has_output=stmt.search_one("output") is not None
            if stmt.keyword == "rpc"
            else False,
        )

        if stmt.keyword == "list":
            node.item_class_name = (
                cls_name if cls_name.endswith("Item") else f"{cls_name}Item"
            )
            node.list_class_name = (
                node.item_class_name[:-4] + "List"
                if node.item_class_name.endswith("Item")
                else node.item_class_name + "List"
            )

        if hasattr(stmt, "i_children"):
            for child in stmt.i_children:
                if child.keyword in ["container", "list"]:
                    child_cls = getattr(
                        child, "_pydantic_class_name", self._to_class_name(child.arg)
                    )
                    prop = IRNavProperty(
                        name=self._to_field_name(child.arg),
                        type_hint=f"{child_cls}Node"
                        if child.keyword == "container"
                        else f"{child_cls[:-4] if child_cls.endswith('Item') else child_cls}ListNode",
                        nav_cls=f"{child_cls}Node"
                        if child.keyword == "container"
                        else f"{child_cls[:-4] if child_cls.endswith('Item') else child_cls}ListNode",
                        path_name=child.arg,
                        yang_name=child.arg,
                        item_cls=f"{child_cls if child_cls.endswith('Item') else f'{child_cls}Item'}Node"
                        if child.keyword == "list"
                        else None,
                    )
                    node.properties.append(prop)
        return node

    # --- MODEL IR BUILDER ---

    def _build_model(
        self, stmt, class_name, bypass_config_check=False
    ) -> Optional[IRModel]:
        if (
            not bypass_config_check
            and self.config_only
            and hasattr(stmt, "i_config")
            and not stmt.i_config
        ):
            return None

        model = IRModel(
            name=class_name,
            description=self._escape_docstring(stmt.search_one("description").arg)
            if stmt.search_one("description")
            else f"{stmt.keyword.capitalize()}: {stmt.arg}",
        )

        if hasattr(stmt, "i_children"):
            model.fields = self._build_fields(
                stmt.i_children, stmt, class_name, bypass_config_check
            )

        return model

    def _build_rpc(self, rpc):
        base_name = getattr(rpc, "_pydantic_class_name", self._to_class_name(rpc.arg))
        envelope = IRModel(
            name=base_name, description=f"RPC: {rpc.arg}", is_rpc_envelope=True
        )

        inp = rpc.search_one("input")
        if inp and hasattr(inp, "i_children") and inp.i_children:
            cls_name = base_name if base_name.endswith("Input") else f"{base_name}Input"
            model = self._build_model(inp, cls_name, bypass_config_check=True)
            if model:
                final_name = self._register_model(model)
                inp._pydantic_class_name = final_name
                envelope.rpc_input_cls = final_name

        outp = rpc.search_one("output")
        if outp and hasattr(outp, "i_children") and outp.i_children:
            cls_name = (
                base_name if base_name.endswith("Output") else f"{base_name}Output"
            )
            model = self._build_model(outp, cls_name, bypass_config_check=True)
            if model:
                final_name = self._register_model(model)
                outp._pydantic_class_name = final_name
                envelope.rpc_output_cls = final_name

        final_envelope_name = self._register_model(envelope)
        rpc._pydantic_class_name = final_envelope_name

    def _build_fields(
        self,
        children,
        parent_stmt,
        parent_class_name,
        bypass_config_check,
        active_choices=None,
    ) -> list[IRField]:
        active_choices = active_choices or {}
        fields = []
        for child in children:
            if (
                not bypass_config_check
                and self.config_only
                and hasattr(child, "i_config")
                and not child.i_config
            ):
                continue

            if child.keyword == "uses":
                grouping_name = child.arg
                if grouping_name in self.uses_refs:
                    grouping = self.groupings.get(grouping_name)
                    if grouping and hasattr(grouping, "i_children"):
                        inner = self._build_fields(
                            grouping.i_children,
                            parent_stmt,
                            parent_class_name,
                            bypass_config_check,
                            active_choices,
                        )
                        for f in inner:
                            f.uses.append(grouping_name)
                        fields.extend(inner)
                continue

            if child.keyword == "choice":
                for case in child.i_children:
                    if case.keyword == "case" and hasattr(case, "i_children"):
                        new_choices = active_choices.copy()
                        new_choices[child.arg] = case.arg
                        case_fields = self._build_fields(
                            case.i_children,
                            parent_stmt,
                            parent_class_name,
                            bypass_config_check,
                            new_choices,
                        )
                        for cf in case_fields:
                            # Force choice items optional
                            if not cf.type_str.endswith(" | None"):
                                cf.type_str += " | None"
                        fields.extend(case_fields)
                continue

            if child.keyword == "case":
                continue

            f = self._build_field(
                child,
                parent_stmt,
                parent_class_name,
                bypass_config_check,
                active_choices,
            )
            if f:
                fields.append(f)
        return fields

    def _build_field(
        self,
        stmt,
        parent_stmt,
        parent_class_name,
        bypass_config_check,
        active_choices=None,
    ) -> Optional[IRField]:
        field_name = self._to_field_name(stmt.arg)
        constraints = {}
        type_str = "Any"
        is_optional = False

        is_config = getattr(stmt, "i_config", True)
        extra_dict = {"is_config": is_config}
        if active_choices:
            extra_dict["choice_mapping"] = active_choices

        field_params = [f"json_schema_extra={repr(extra_dict)}"]

        if stmt.keyword == "container":
            type_str = getattr(
                stmt, "_pydantic_class_name", self._to_class_name(stmt.arg)
            )
            nested = self._build_model(stmt, type_str, bypass_config_check)
            if nested:
                final_name = self._register_model(nested)
                stmt._pydantic_class_name = final_name
                type_str = final_name
            is_optional = not self._is_mandatory(stmt)

        elif stmt.keyword == "list":
            item_type = getattr(
                stmt, "_pydantic_class_name", self._to_class_name(stmt.arg)
            )
            if not item_type.endswith("Item"):
                item_type += "Item"
            nested = self._build_model(stmt, item_type, bypass_config_check)
            if nested:
                final_name = self._register_model(nested)
                stmt._pydantic_class_name = final_name
                item_type = final_name
            type_str = f"RestconfList[{item_type}]"
            is_optional = not self._is_mandatory(stmt)

            min_elements = stmt.search_one("min-elements")
            if min_elements:
                field_params.append(f"min_length={min_elements.arg}")
            max_elements = stmt.search_one("max-elements")
            if max_elements:
                field_params.append(f"max_length={max_elements.arg}")

        elif stmt.keyword == "leaf":
            type_str, constraints = self._get_leaf_type(stmt)
            if "_patterns" in constraints:
                validators = [
                    f"AfterValidator(lambda v: check_pattern({repr(f'^(?:{self._convert_yang_regex(p)})$')}, v))"
                    for p in constraints.pop("_patterns")
                ]
                type_str = f"Annotated[{type_str}, {', '.join(validators)}]"
            is_optional = not self._is_mandatory(stmt) and not hasattr(stmt, "i_is_key")

        elif stmt.keyword == "leaf-list":
            item_type, constraints = self._get_leaf_type(stmt)
            inner = [
                f"{k}={constraints.pop(k)}"
                for k in ["ge", "le", "gt", "lt", "min_length", "max_length"]
                if k in constraints
            ]
            if inner:
                item_type = f"Annotated[{item_type}, Field({', '.join(inner)})]"
            if "_patterns" in constraints:
                validators = [
                    f"AfterValidator(lambda v: check_pattern({repr(f'^(?:{self._convert_yang_regex(p)})$')}, v))"
                    for p in constraints.pop("_patterns")
                ]
                item_type = f"Annotated[{item_type}, {', '.join(validators)}]"
            type_str = f"RestconfList[{item_type}]"
            is_optional = not self._is_mandatory(stmt)

            min_elements = stmt.search_one("min-elements")
            if min_elements:
                field_params.append(f"min_length={min_elements.arg}")
            max_elements = stmt.search_one("max-elements")
            if max_elements:
                field_params.append(f"max_length={max_elements.arg}")

        elif stmt.keyword in ["anydata", "anyxml"]:
            is_optional = True
        else:
            return None

        field_def = f"{type_str} | None" if is_optional else type_str
        default_val = self._get_default_value(stmt)

        desc = self._build_field_description(stmt)
        if desc:
            field_params.append(f"description={repr(desc)}")
        for k, v in constraints.items():
            field_params.append(f"{k}={v}")
        if default_val is not None:
            field_params.append(
                f"default={default_val if default_val != 'None' or not is_optional else 'None'}"
            )
        elif is_optional:
            field_params.append("default=None")

        mod_name, parent_mod = (
            self._get_module_name(stmt),
            self._get_module_name(parent_stmt),
        )
        if parent_stmt.keyword in ("module", "submodule") or mod_name != parent_mod:
            field_params.append(f'alias="{mod_name}:{stmt.arg}"')
        elif field_name != stmt.arg:
            field_params.append(f'alias="{stmt.arg}"')

        assign = (
            f"Field({', '.join(field_params)})"
            if field_params
            else (default_val if default_val else "")
        )

        return IRField(name=field_name, type_str=field_def, assignment=assign)

    def _convert_yang_regex(self, pattern: str) -> str:
        """
        Convert YANG (XSD) regex to Python re syntax.
        Handles common incompatibilities like \\p{N}.
        """
        translation_map = {
            # https://www.w3.org/TR/2004/REC-xmlschema-2-20041028/#nt-charProp
            # copied from pydantify, credits to them!
            r"\p{L}": r"\w",  # All Letters
            r"\P{L}": r"\W",  # All Letters
            r"\p{Lu}": r"[A-Z]",  # uppercase
            r"\P{Lu}": r"[^A-Z]",  # uppercase
            r"\p{Ll}": r"[a-z]",  # uppercase
            r"\P{Ll}": r"[^a-z]",  # uppercase
            r"\p{N}": r"\d",  # All Numbers
            r"\P{N}": r"\D",  # All Numbers
            r"\p{Nd}": r"\d",  # decimal digit
            r"\P{Nd}": r"\D",  # decimal digit
            r"\p{C}": r"[\x00-\x1F\x7F-\x9F]",  # invisible control characters and unused code points
            r"\P{C}": r"[^\x00-\x1F\x7F-\x9F]",  # invisible control characters and unused code points
            r"\p{P}": r"[!\"'#$%&\"()*+,\-./:;<=>?@[\\\]^_`{|}~]",  # punctuation
            r"\P{P}": r"[^!\"'#$%&\"()*+,\-./:;<=>?@[\\\]^_`{|}~]",  # punctuation
        }

        for search, replace in translation_map.items():
            pattern = pattern.replace(search, replace)

        return pattern

    def _get_range_constraints(self, type_stmt) -> dict[str, Any]:
        """Extract ge/le from YANG range statement"""
        constraints = {}
        range_stmt = type_stmt.search_one("range")
        if not range_stmt:
            return constraints

        parts = range_stmt.arg.split("|")

        lower_part = parts[0].strip()
        if ".." in lower_part:
            low = lower_part.split("..")[0].strip()
            if low != "min":
                try:
                    constraints["ge"] = float(low) if "." in low else int(low)
                except ValueError:
                    pass

        upper_part = parts[-1].strip()
        if ".." in upper_part:
            high = upper_part.split("..")[1].strip()
            if high != "max":
                try:
                    constraints["le"] = float(high) if "." in high else int(high)
                except ValueError:
                    pass

        return constraints

    def _get_leaf_type(self, stmt) -> tuple[str, dict]:
        type_stmt = stmt.search_one("type")
        if not type_stmt:
            return "str", {}
        return self._resolve_type_stmt(type_stmt, stmt)

    def _resolve_type_stmt(self, type_stmt, context_stmt) -> tuple[str, dict]:
        """Recursively parses type statements with contextual fallback rules."""
        yt = type_stmt.arg

        if yt in ["int8", "int16", "int32"]:
            return "int", self._get_range_constraints(type_stmt)
        elif yt == "int64":
            c = self._get_range_constraints(type_stmt)
            c.setdefault("ge", -9223372036854775808)
            c.setdefault("le", 9223372036854775807)
            return "Int64", c
        elif yt in ["uint8", "uint16", "uint32"]:
            c = self._get_range_constraints(type_stmt)
            c.setdefault("ge", 0)
            return "int", c
        elif yt == "uint64":
            c = self._get_range_constraints(type_stmt)
            c.setdefault("ge", 0)
            c.setdefault("le", 18446744073709551615)
            return "Uint64", c
        elif yt == "decimal64":
            return "Decimal64", self._get_range_constraints(type_stmt)
        elif yt in ["boolean", "empty"]:
            return "bool", {}
        elif yt in ["binary", "bits", "identityref", "instance-identifier"]:
            return "str", {}
        elif yt == "string":
            c = {}
            length = type_stmt.search_one("length")
            if length:
                match = re.search(r"(\d+)\.\.(\d+)", length.arg)
                if match:
                    c["min_length"] = int(match.group(1))
                    c["max_length"] = int(match.group(2))
                elif length.arg.isdigit():
                    c["min_length"] = int(length.arg)
                    c["max_length"] = int(length.arg)

            if pt := type_stmt.search("pattern"):
                c["_patterns"] = [p.arg for p in pt]
            return "str", c
        elif yt == "leafref":
            if hasattr(type_stmt, "i_type_spec") and getattr(
                type_stmt.i_type_spec, "i_target_node", None
            ):
                return self._get_leaf_type(type_stmt.i_type_spec.i_target_node)
            return "str", {}
        elif yt == "enumeration":
            enums = type_stmt.search("enum")

            # Inline fallback to Literals for low cardinality options
            if len(enums) <= 3:
                literal_vals = [f'"{e.arg}"' for e in enums]
                return f"Literal[{', '.join(literal_vals)}]", {}

            e_name = self._to_class_name(context_stmt.arg) + "Enum"

            data_parts = []
            for e in sorted(enums, key=lambda x: x.arg):
                val_stmt = e.search_one("value")
                val = val_stmt.arg if val_stmt else ""
                data_parts.append(f"{e.arg}:{val}")

            fp = hashlib.md5("|".join(data_parts).encode()).hexdigest()

            if fp not in self.enum_registry:
                actual = e_name
                counter = 0
                while actual in self.enum_registry.values():
                    counter += 1
                    actual = f"{e_name}_{counter}"
                self.enum_registry[fp] = actual

                ir_enum = IREnum(
                    name=actual, description=f"Enumeration for {context_stmt.arg}"
                )
                for e in enums:
                    d = e.search_one("description")
                    ir_enum.values.append(
                        IREnumValue(
                            py_name=self._to_enum_name(e.arg),
                            value=e.arg,
                            description=self._escape_docstring(d.arg) if d else None,
                        )
                    )
                self.ir.enums.append(ir_enum)

            return self.enum_registry[fp], {}
        elif yt == "union":
            types = []
            for t in type_stmt.search("type"):
                t_str, _ = self._resolve_type_stmt(t, context_stmt)
                types.append(t_str)
            return f"{' | '.join(types)}" if types else "str", {}
        elif hasattr(type_stmt, "i_typedef") and type_stmt.i_typedef:
            typedef_type_stmt = type_stmt.i_typedef.search_one("type")
            if typedef_type_stmt:
                return self._resolve_type_stmt(typedef_type_stmt, type_stmt.i_typedef)
        return "str", {}

    def _get_default_value(self, stmt) -> str:
        default = stmt.search_one("default")
        if not default:
            return None

        type_stmt = stmt.search_one("type")
        if type_stmt:
            base_type_stmt = type_stmt
            while (
                base_type_stmt.arg != "enumeration"
                and hasattr(base_type_stmt, "i_typedef")
                and base_type_stmt.i_typedef
            ):
                base_type_stmt = base_type_stmt.i_typedef.search_one("type")

            yt = base_type_stmt.arg

            if yt == "boolean":
                return default.arg.title()
            elif yt in [
                "int8",
                "int16",
                "int32",
                "int64",
                "uint8",
                "uint16",
                "uint32",
                "uint64",
                "decimal64",
            ]:
                return default.arg
            elif yt == "enumeration":
                enums = base_type_stmt.search("enum")
                if len(enums) <= 3:
                    return repr(default.arg)

                # Reproduce fingerprint to locate the exact Python enum name
                data_parts = []
                for e in sorted(enums, key=lambda x: x.arg):
                    val_stmt = e.search_one("value")
                    data_parts.append(f"{e.arg}:{val_stmt.arg if val_stmt else ''}")

                import hashlib

                fp = hashlib.md5("|".join(data_parts).encode()).hexdigest()
                actual_enum = self.enum_registry.get(fp)
                if actual_enum:
                    py_enum_member = self._to_enum_name(default.arg)
                    return f"{actual_enum}.{py_enum_member}"

        return repr(default.arg)

    def _is_mandatory(self, stmt) -> bool:
        if stmt.search_one("when") or stmt.search("must"):
            return False
        if stmt.keyword in ["leaf", "choice", "anydata", "anyxml"]:
            m = stmt.search_one("mandatory")
            return m and m.arg == "true"
        elif stmt.keyword in ["list", "leaf-list"]:
            min_els = stmt.search_one("min-elements")
            return min_els and int(min_els.arg) > 0
        return False

    def _build_field_description(self, stmt) -> str:
        parts = []
        desc = stmt.search_one("description")
        if desc:
            parts.append(desc.arg.strip())

        when_stmt = stmt.search_one("when")
        if when_stmt:
            parts.append(f"\nCondition (when): {when_stmt.arg}")

        must_stmts = stmt.search("must")
        if must_stmts:
            constraints = []
            for m in must_stmts:
                constraint = f"- {m.arg}"
                err = m.search_one("error-message")
                if err:
                    constraint += f" (Error: {err.arg})"
                constraints.append(constraint)
            parts.append("\nValidation Constraints (must):\n" + "\n".join(constraints))

        return self._escape_docstring("\n".join(parts).strip())

    def _get_original_node(self, stmt):
        """Restore exact Pyang AST backtracking."""
        if not hasattr(stmt, "i_uses") or not stmt.i_uses:
            return None
        uses_stmt = (
            stmt.i_uses[-1] if isinstance(stmt.i_uses, (list, tuple)) else stmt.i_uses
        )
        grouping = getattr(uses_stmt, "i_grouping", None)
        if not grouping:
            return None

        path = []
        curr = stmt
        while curr is not None and curr != getattr(uses_stmt, "parent", None):
            path.append((curr.keyword, curr.arg))
            curr = getattr(curr, "parent", None)
        if curr is None:
            return None
        path.reverse()

        curr_orig = grouping
        for kw, arg in path:
            found = False
            for child in getattr(curr_orig, "i_children", []):
                if child.keyword == kw and child.arg == arg:
                    curr_orig = child
                    found = True
                    break
            if not found:
                for child in getattr(curr_orig, "substmts", []):
                    if child.keyword == kw and child.arg == arg:
                        curr_orig = child
                        found = True
                        break
            if not found:
                return None
        return curr_orig

    def _resolve_names(self, module):
        """Restore the iterative O(N) collision resolver."""
        nodes_map = []

        def collect_nodes(stmt):
            orig = self._get_original_node(stmt)
            if orig and len(getattr(stmt, "i_children", [])) == len(
                getattr(orig, "i_children", [])
            ):
                return
            if stmt.keyword in ["container", "rpc", "grouping"]:
                nodes_map.append({"stmt": stmt, "suffix": "", "depth": 0})
            elif stmt.keyword == "list":
                nodes_map.append({"stmt": stmt, "suffix": "Item", "depth": 0})
            elif stmt.keyword == "notification":
                nodes_map.append({"stmt": stmt, "suffix": "Notification", "depth": 0})

            if hasattr(stmt, "i_children"):
                for child in stmt.i_children:
                    collect_nodes(child)
            if hasattr(stmt, "substmts"):
                for sub in stmt.substmts:
                    if sub.keyword == "grouping":
                        collect_nodes(sub)

        collect_nodes(module)

        for _ in range(30):
            name_registry = {}
            for entry in nodes_map:
                stmt, suffix, depth = entry["stmt"], entry["suffix"], entry["depth"]
                parts = [self._to_class_name(stmt.arg)]
                curr = stmt
                for _ in range(depth):
                    parent = getattr(curr, "parent", None)
                    if parent and parent.keyword not in ("module", "submodule"):
                        parts.insert(0, self._to_class_name(parent.arg))
                        curr = parent
                    else:
                        break
                full_name = "".join(parts) + suffix
                entry["current_name"] = full_name
                name_registry.setdefault(full_name, []).append(entry)

            has_collision = False
            for name, entries in name_registry.items():
                if len(entries) > 1:
                    has_collision = True
                    for entry in entries:
                        entry["depth"] += 1

            if not has_collision:
                break

        for entry in nodes_map:
            entry["stmt"]._pydantic_class_name = entry["current_name"]

        def propagate_names(stmt):
            orig = self._get_original_node(stmt)
            if orig:
                if not getattr(stmt, "_pydantic_class_name", None):
                    orig_name = getattr(orig, "_pydantic_class_name", None)
                    if orig_name:
                        stmt._pydantic_class_name = orig_name
            if hasattr(stmt, "i_children"):
                for child in stmt.i_children:
                    propagate_names(child)

        propagate_names(module)

    def _collect_groupings(self, stmt):
        """Recursively collect all groupings"""
        if stmt.keyword == "grouping":
            key = self._get_qualified_name(stmt)
            self.groupings[key] = stmt

        if hasattr(stmt, "i_children"):
            for child in stmt.i_children:
                self._collect_groupings(child)

    def _get_qualified_name(self, stmt) -> str:
        """Get fully qualified name for a statement"""
        module_name = self._get_module_name(stmt)
        return f"{module_name}:{stmt.arg}"

    def _get_module_name(self, stmt) -> str:
        """Get module name for a statement"""
        if stmt.keyword in ["module", "submodule"]:
            return stmt.arg
        if hasattr(stmt, "i_module"):
            return stmt.i_module.arg
        return "unknown"

    def _to_class_name(self, name: str) -> str:
        """Convert YANG name to Python class name (PascalCase)"""
        name = re.sub(r"[^a-zA-Z0-9]", "_", name)

        parts = re.split(r"[_]", name)

        res = "".join(word.capitalize() for word in parts)
        if keyword.iskeyword(res):
            return res + "_"
        return res

    def _to_field_name(self, name: str) -> str:
        """Convert YANG name to Python field name (snake_case)"""
        res = re.sub(r"[^a-zA-Z0-9]", "_", name)

        if keyword.iskeyword(res):
            res = f"{res}_"
        return res

    def _to_enum_name(self, name: str) -> str:
        """Convert enum value to Python enum name (UPPER_CASE)"""
        res = name.replace("+", "_PLUS_")

        res = re.sub(r"[^a-zA-Z0-9]", "_", res)

        res = res.upper()

        res = re.sub(r"_+", "_", res).strip("_")

        if res and res[0].isdigit():
            res = "_" + res
        return res or "VAL_UNKNOWN"

    def _escape_docstring(self, text: str) -> str:
        """Escape text for use in docstring"""

        return text.replace('"""', r"\"\"\"").replace("\n", " ").strip()
