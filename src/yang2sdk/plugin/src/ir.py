"""INTERMEDIATE REPRESENTATION (IR) DATACLASSES AND BUILDER"""

import hashlib
import keyword
import re
from dataclasses import dataclass, field
from typing import Any

from pyang import statements
from pydantic import BaseModel
from pydantic_xml import BaseXmlModel

# A docstring-terminating quote run, built at runtime so this module's own
# source never contains one inside a triple-quoted literal.
_TRIPLE = chr(34) * 3


def _reserved_model_attrs() -> frozenset[str]:
    """Attribute names a generated model must not shadow.

    A field whose Python name collides with a `BaseModel` / `BaseXmlModel`
    attribute resolves to the inherited attribute instead of the field, so
    pydantic emits "Field name ... shadows an attribute in parent" and the
    value is silently unreachable at runtime. Collected from the real classes
    rather than hardcoded so it cannot drift with the pinned pydantic version.
    """
    names: set[str] = set()
    for cls in (BaseModel, BaseXmlModel):
        names.update(a for a in dir(cls) if not a.startswith("_"))
    return frozenset(names)


_RESERVED_MODEL_ATTRS = _reserved_model_attrs()


def _generated_code_builtins() -> frozenset[str]:
    """Builtins the emitter writes as bare names *inside a class body*.

    A field or navigator property named after one of these is not merely
    unidiomatic -- it breaks the module at import time, because the class
    body rebinds the name before a later statement reads it:

        class C(Node):
            str: str | None = Field(...)   # binds C.str = FieldInfo
            u: str | None = Field(...)     # annotation reads FieldInfo | None

    `models.py.jinja` emits no `from __future__ import annotations`, so those
    annotations are evaluated eagerly and the second line raises
    `TypeError: unsupported operand type(s) for |: 'FieldInfo' and 'NoneType'`.
    NETCONF leaf-list/list fields emit a bare `list[...]` annotation, so the
    same shadowing yields `'FieldInfo' object is not subscriptable`.
    Navigators additionally decorate child nodes with `@property`, so a
    container/list/leaf-list named `property` makes every *later* decorator in
    that class body resolve to the property object:
    `TypeError: 'property' object is not callable`.

    Deliberately NOT all of `builtins`. Escaping every builtin would rename
    ordinary YANG leaves such as `id`, `type`, `input`, `filter` and `range` to
    `id_`, `type_`, ...: a breaking change to the public API of every generated
    SDK, and one that fixes nothing, because the emitter never writes those
    names bare. (Measured against the 1470 vendor YANG files in
    `temp/yang_modules/`, `id`/`type`/`input`/`filter` appear 3-4 orders of
    magnitude more often than `property`.)

    Each member was confirmed by generating a module that uses it as a node
    name and watching the import fail:

        container property -> TypeError: 'property' object is not callable
        leaf str            -> TypeError: unsupported operand type(s) for |:
                                'FieldInfo' and 'NoneType'
        leaf int / bool     -> same
        leaf-list list      -> TypeError: 'FieldInfo' object is not
                                subscriptable   (NETCONF only: it emits a bare
                                `list[...]` annotation where RESTCONF emits
                                `RestconfList[...]`)

    Keep this list to names the emitter actually emits. `test_builtin_named_
    nodes_do_not_break_the_generated_import` is the tripwire: it generates a
    module using each of these as a node name and asserts it imports, so
    widening the emitter's vocabulary without widening this set fails loudly
    rather than silently shipping an unimportable client.
    """
    return frozenset({"property", "str", "int", "bool", "list"})


_GENERATED_CODE_BUILTINS = _generated_code_builtins()


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
    yang_name: str = ""
    fields: list[IRField] = field(default_factory=list)
    is_rpc_envelope: bool = False
    rpc_input_cls: str | None = None
    rpc_output_cls: str | None = None


@dataclass
class IREnumValue:
    py_name: str
    value: str
    description: str | None = None


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
    ns: str = ""
    item_cls: str | None = None
    module_yang_name: str = ""
    # RFC 8040 Sec 3.5.3: "If a node in the path is defined in a module other
    # than its parent node or its parent is the datastore, then the module name
    # followed by a colon character MUST be prepended to the node name in the
    # resource identifier." Same-module children stay bare; augmented
    # (cross-module) children must be qualified or the URI addresses nothing.
    path_segment: str = ""


@dataclass
class IRNavNode:
    node_type: str  # 'container', 'list', 'rpc'
    class_name: str
    item_class_name: str | None = None
    list_class_name: str | None = None
    path_name: str = ""
    yang_name: str = ""
    pydantic_module: str = ""
    module_yang_name: str = ""
    ns: str = ""
    keys: list[str] = field(default_factory=list)
    # (python_name, yang_name) per list key, for the navigator's __call__
    # signature. The python name must be keyword-safe: a YANG key may legally
    # be named `if`, `class` or `import`, and emitting it raw produces
    # `def __call__(self, if: str | int)` -- a SyntaxError. Cisco NX-OS ships
    # a list keyed on `if`, so this broke the whole NX-OS client.
    key_params: list[tuple[str, str]] = field(default_factory=list)
    properties: list[IRNavProperty] = field(default_factory=list)
    has_input: bool = False
    has_output: bool = False


@dataclass
class IRParentProperty:
    module: str
    cls: str
    alias: str
    field: str
    node_type: str = "container"
    ns: str = ""


@dataclass
class IRModule:
    name: str
    py_name: str
    namespace: str = ""
    revision: str = ""
    imports_nsmap: dict[str, str] = field(default_factory=dict)
    models: list[IRModel] = field(default_factory=list)
    enums: list[IREnum] = field(default_factory=list)
    nav_nodes: list[IRNavNode] = field(default_factory=list)
    root_data_props: list[IRParentProperty] = field(default_factory=list)
    root_rpc_props: list[IRParentProperty] = field(default_factory=list)


class IRBuilder:
    """Walks the YANG AST and populates the pristine IR dataclasses."""

    def __init__(self, ctx, module, config_only, target_format="restconf"):
        self.ctx = ctx
        self.module = module
        self.config_only = config_only
        self.target_format = target_format

        self.groupings = {}
        self.enum_registry = {}
        self.uses_refs = {}
        self._synth_prefixes: dict[str, str] = {}

        ns = module.search_one("namespace")

        imports_nsmap = {}

        own_prefix = module.search_one("prefix")
        if own_prefix and ns:
            imports_nsmap[own_prefix.arg] = ns.arg

        for imp in module.search("import"):
            prefix_stmt = imp.search_one("prefix")
            if prefix_stmt:
                imported_module = self.ctx.get_module(imp.arg)
                if imported_module:
                    ns_stmt = imported_module.search_one("namespace")
                    if ns_stmt:
                        imports_nsmap[prefix_stmt.arg] = ns_stmt.arg
        # ----------------------------------------------------------------------

        rev = module.search_one("revision")
        self.ir = IRModule(
            name=module.arg,
            py_name=module.arg.replace("-", "_"),
            namespace=ns.arg if ns else "urn:unknown",
            revision=rev.arg if rev else "",
            imports_nsmap=imports_nsmap,  # Pass the map to the IR
        )

    def _get_module_namespace(self, stmt) -> str:
        mod = getattr(stmt, "i_module", self.module)
        ns = mod.search_one("namespace")
        return ns.arg if ns else "urn:unknown"

    def _ns_alias(self, uri: str) -> str:
        """Map a namespace URI to its pydantic-xml prefix alias.

        pydantic-xml resolves declaration `ns=` through the model nsmap, so it
        must be a prefix ("" for the module default), not a URI. URIs outside
        the module imports (e.g. augmenting modules) get a deterministic
        synthetic prefix that build() backfills into imports_nsmap.
        """
        if uri == self.ir.namespace:
            return ""
        for prefix, ns_uri in self.ir.imports_nsmap.items():
            if ns_uri == uri:
                return prefix
        if uri in self._synth_prefixes:
            return self._synth_prefixes[uri]
        base = re.sub(r"[^A-Za-z0-9_.-]", "_", uri.rsplit(":", 1)[-1] or "ns")
        if not base or not (base[0].isalpha() or base[0] == "_"):
            base = f"ns_{base}"
        candidate, i = base, 2
        taken = set(self.ir.imports_nsmap) | set(self._synth_prefixes.values())
        while candidate in taken:
            candidate, i = f"{base}_{i}", i + 1
        self._synth_prefixes[uri] = candidate
        return candidate

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
                # `_pydantic_class_name` is the model class name and always
                # ends with "Item" for a list (set by _build_field), so no
                # suffix is appended here: the aggregate Data class imports
                # both the navigator and the model by this name.
                cls_name = getattr(
                    child, "_pydantic_class_name", self._to_class_name(child.arg)
                )
                if child.keyword == "list" and not cls_name.endswith("Item"):
                    cls_name += "Item"  # pragma: no cover - only pre-resolve fallbacks

                model = self._build_model(child, cls_name)
                if model:
                    final_name = self._register_model(model)
                    child._pydantic_class_name = final_name

        if data_children:
            root_model = self._build_model(
                self.module, f"{self._to_class_name(self.module.arg)}Data"
            )
            if root_model:
                self._register_model(root_model)

        # Root data properties are emitted *after* the module root model is
        # built. Building it re-registers every top-level container/list, and
        # the collision resolver can rename it a second time once the nested
        # models are known (e.g. System -> System_1 on SR Linux). Both
        # aggregate `Data` classes (data_models/__init__.py and
        # data_navigators/__init__.py) import `prop.cls` verbatim, so a name
        # captured before that step made `client.data.<root>` raise
        # ImportError at runtime.
        for child in data_children:
            if child.keyword in ["container", "list"]:
                # `cls` is the model class name; the aggregate Data classes
                # import the navigator (`<cls>Node` / `<cls>ListNode`) and the
                # model (`<cls>`) by it. It is never re-suffixed here.
                cls_name = getattr(
                    child, "_pydantic_class_name", self._to_class_name(child.arg)
                )

                self.ir.root_data_props.append(
                    IRParentProperty(
                        module=self.ir.py_name,
                        cls=cls_name,
                        alias=f"{self.module.arg}:{child.arg}",
                        field=self._to_field_name(child.arg),
                        node_type=child.keyword,
                        ns=self._get_module_namespace(child),
                    )
                )

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
                    ns=self._get_module_namespace(rpc),
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

        # YANG 1.1 `action` statements (RFC 7950 Sec 7.15) are RPCs nested
        # under a data node. They need the same Input/Output envelope models as
        # top-level `rpc` statements, because the navigator template emits
        # `<Action>`, `<Action>Input` and `<Action>Output` imports for them.
        # Without this pass the generated client raises ImportError the first
        # time any action is invoked. Actions are deliberately NOT added to
        # root_rpc_props: RFC 8040 Sec 3.6 keeps them out of the
        # {+restconf}/operations resource, they are invoked through the data
        # tree. Must run before _build_nav_nodes so the collision-resolved
        # envelope name is the one the navigator imports.
        self._build_actions(self.module)

        # Build Navigator IR
        self._build_nav_nodes(self.module)

        # Backfill synthetic prefixes for augmenting-module namespaces so every
        # ns= alias used in field declarations exists in the model nsmaps.
        for uri, prefix in self._synth_prefixes.items():
            self.ir.imports_nsmap.setdefault(prefix, uri)

        return self.ir

    def _build_actions(self, stmt) -> None:
        """Build Input/Output models for every nested YANG `action` statement."""
        for child in getattr(stmt, "i_children", []) or []:
            if child.keyword == "action":
                self._build_rpc(child)
            self._build_actions(child)

    # --- NAVIGATOR IR BUILDER ---

    def _build_nav_nodes(self, stmt):
        if hasattr(stmt, "i_children"):
            for child in stmt.i_children:
                if child.keyword in ["container", "list", "rpc", "action"]:
                    node = self._build_nav_node(child)
                    if node and not any(
                        n.class_name == node.class_name for n in self.ir.nav_nodes
                    ):
                        self.ir.nav_nodes.append(node)
                # Recurse regardless of whether the current node is a container/list/rpc
                self._build_nav_nodes(child)

    def _build_nav_node(self, stmt) -> IRNavNode | None:
        cls_name = getattr(stmt, "_pydantic_class_name", self._to_class_name(stmt.arg))
        ns = self._get_module_namespace(stmt)
        keys = (
            stmt.search_one("key").arg.split()
            if stmt.keyword == "list" and stmt.search_one("key")
            else []
        )

        # Map both 'rpc' and 'action' to the abstract 'rpc' node_type for the navigator
        node_type = "rpc" if stmt.keyword in ["rpc", "action"] else stmt.keyword

        node = IRNavNode(
            node_type=node_type,
            class_name=cls_name,
            path_name=stmt.arg,
            yang_name=stmt.arg,
            pydantic_module=self.ir.py_name,
            module_yang_name=self.module.arg,
            ns=ns,
            keys=keys,
            key_params=[(self._to_field_name(k), k) for k in keys],
            has_input=stmt.search_one("input") is not None
            if node_type == "rpc"
            else False,
            has_output=stmt.search_one("output") is not None
            if node_type == "rpc"
            else False,
        )

        if stmt.keyword == "list":
            # `item_class_name` is the *model* class name: the navigator
            # template uses it to import from data_models (the item retrieve
            # already mixed `node.class_name` and `node.item_class_name`).
            # Deriving it by appending "Item" to the collision-resolved name
            # produced `InterfaceItem_1Item` for a model actually named
            # `InterfaceItem_1` (SR Linux), so every list read raised
            # ImportError at runtime. `list_class_name` keeps the same
            # strip-suffix rule the aggregate Data class applies
            # (data_navigators/__init__.py.jinja) so the two agree.
            node.item_class_name = cls_name
            node.list_class_name = (
                node.item_class_name[:-4] + "List"
                if node.item_class_name.endswith("Item")
                else node.item_class_name + "List"
            )

        if hasattr(stmt, "i_children"):
            parent_module = self._get_module_name(stmt)
            for child in stmt.i_children:
                if child.keyword in ["container", "list", "action"]:
                    child_cls = getattr(
                        child, "_pydantic_class_name", self._to_class_name(child.arg)
                    )

                    if child.keyword == "action" or child.keyword == "container":
                        type_hint = f"{child_cls}Node"
                        nav_cls = f"{child_cls}Node"
                    else:
                        type_hint = f"{child_cls.removesuffix('Item')}ListNode"
                        nav_cls = type_hint

                    child_module = self._get_module_name(child)
                    prop = IRNavProperty(
                        name=self._to_field_name(child.arg),
                        type_hint=type_hint,
                        nav_cls=nav_cls,
                        path_name=child.arg,
                        yang_name=child.arg,
                        ns=self._get_module_namespace(child),
                        item_cls=f"{child_cls if child_cls.endswith('Item') else f'{child_cls}Item'}Node"
                        if child.keyword == "list"
                        else None,
                        module_yang_name=child_module,
                        path_segment=(
                            f"{child_module}:{child.arg}"
                            if child_module != parent_module
                            else child.arg
                        ),
                    )
                    node.properties.append(prop)
        return node

    # --- MODEL IR BUILDER ---

    def _build_model(
        self, stmt, class_name, bypass_config_check=False
    ) -> IRModel | None:
        if (
            not bypass_config_check
            and self.config_only
            and hasattr(stmt, "i_config")
            and not stmt.i_config
        ):
            return None

        model = IRModel(
            name=class_name,
            yang_name=stmt.arg,
            description=self._docstring(stmt.search_one("description").arg)
            if stmt.search_one("description")
            else f"{stmt.keyword.capitalize()}: {stmt.arg}",
        )

        if hasattr(stmt, "i_children"):
            model.fields = self._build_fields(
                stmt.i_children, stmt, class_name, bypass_config_check
            )

        return model

    @staticmethod
    def _expanded_io(stmt, keyword: str):
        """Return pyang's *expanded* `input`/`output` node for an rpc/action.

        pyang copies the `input`/`output` substatement into `i_children`
        during the expand_1 phase and sets `arg` to the keyword there. The
        original substatement that `search_one` returns keeps `arg = None`
        and may carry no `i_children` at all, so building a model from it
        yields a Pydantic model whose XML root tag is the string "None" --
        which silently breaks every NETCONF RPC input/output round-trip. The
        expanded copy is also the only node with `i_children` populated for
        a nested YANG 1.1 `action`.
        """
        for child in getattr(stmt, "i_children", []) or []:
            if child.keyword == keyword:
                return child
        return stmt.search_one(keyword)

    def _build_rpc(self, rpc):
        base_name = getattr(rpc, "_pydantic_class_name", self._to_class_name(rpc.arg))
        envelope = IRModel(
            name=base_name,
            yang_name=rpc.arg,
            description=f"RPC: {rpc.arg}",
            is_rpc_envelope=True,
        )

        inp = self._expanded_io(rpc, "input")
        if inp and hasattr(inp, "i_children") and inp.i_children:
            cls_name = base_name if base_name.endswith("Input") else f"{base_name}Input"
            model = self._build_model(inp, cls_name, bypass_config_check=True)
            if model:
                final_name = self._register_model(model)
                inp._pydantic_class_name = final_name
                envelope.rpc_input_cls = final_name

        outp = self._expanded_io(rpc, "output")
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
    ) -> IRField | None:
        field_name = self._to_field_name(stmt.arg)
        constraints = {}
        type_str = "Any"
        is_optional = False
        is_identityref = False

        # pyang's `_keywords_with_no_explicit_config` is ['action','rpc',
        # 'notification']: for nodes inside an rpc/action input or output,
        # `i_config` EXISTS but is None -- "config-ness is not defined here".
        # Reading it with a `True` default silently yields None, and every
        # consumer that tests it for truth (`if not is_config`, `extra.get(
        # "is_config", True)` in both protocol pruners) then treats the node
        # as config=false and DELETES it. That stripped every rpc/action
        # input parameter from the NETCONF write payload. Normalise the
        # undefined case to True: an rpc input is writable data.
        is_config = getattr(stmt, "i_config", True)
        if is_config is None:
            is_config = True
        extra_dict = {"is_config": is_config}
        if active_choices:
            extra_dict["choice_mapping"] = active_choices

        field_params = [f"json_schema_extra={extra_dict!r}"]

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

            type_str = (
                f"list[{item_type}]"
                if self.target_format == "netconf"
                else f"RestconfList[{item_type}]"
            )
            is_optional = not is_config or (not self._is_mandatory(stmt))

            min_elements = stmt.search_one("min-elements")
            if min_elements:
                field_params.append(f"min_length={min_elements.arg}")
            max_elements = stmt.search_one("max-elements")
            if max_elements:
                field_params.append(f"max_length={max_elements.arg}")

        elif stmt.keyword == "leaf":
            type_str, constraints = self._get_leaf_type(stmt)
            is_identityref = constraints.pop("_identityref", False)
            if "_patterns" in constraints:
                validators = [
                    f"AfterValidator(lambda v: check_pattern({f'^(?:{self._convert_yang_regex(p)})$'!r}, v))"
                    for p in constraints.pop("_patterns")
                ]
                type_str = f"Annotated[{type_str}, {', '.join(validators)}]"
            # State (config false) leaves are device-generated; a
            # get/get-config response may legitimately omit them (e.g. an
            # entry with no operational counterpart), so they must never be
            # required on read.
            is_optional = not is_config or (
                not self._is_mandatory(stmt) and not hasattr(stmt, "i_is_key")
            )

        elif stmt.keyword == "leaf-list":
            item_type, constraints = self._get_leaf_type(stmt)
            is_identityref = constraints.pop("_identityref", False)
            inner = [
                f"{k}={constraints.pop(k)}"
                for k in ["ge", "le", "gt", "lt", "min_length", "max_length"]
                if k in constraints
            ]
            if inner:
                item_type = f"Annotated[{item_type}, Field({', '.join(inner)})]"
            if "_patterns" in constraints:
                validators = [
                    f"AfterValidator(lambda v: check_pattern({f'^(?:{self._convert_yang_regex(p)})$'!r}, v))"
                    for p in constraints.pop("_patterns")
                ]
                item_type = f"Annotated[{item_type}, {', '.join(validators)}]"

            type_str = (
                f"list[{item_type}]"
                if self.target_format == "netconf"
                else f"RestconfList[{item_type}]"
            )
            is_optional = not is_config or (not self._is_mandatory(stmt))

            min_elements = stmt.search_one("min-elements")
            if min_elements:
                field_params.append(f"min_length={min_elements.arg}")
            max_elements = stmt.search_one("max-elements")
            if max_elements:
                field_params.append(f"max_length={max_elements.arg}")

        elif stmt.keyword in ["anydata", "anyxml"]:
            is_optional = True
            type_str = "str"
        else:
            return None

        field_def = f"{type_str} | None" if is_optional else type_str
        default_val = self._get_default_value(stmt)

        desc = self._build_field_description(stmt)

        if self.target_format == "netconf":
            field_params = [f'tag="{stmt.arg}"']
            if default_val is not None:
                field_params.append(
                    "default=None"
                    if is_optional and default_val == "None"
                    else f"default={default_val}"
                )
            elif is_optional:
                field_params.append("default=None")
            elif stmt.keyword in ("list", "leaf-list"):
                field_params.append("default_factory=list")

            if desc:
                field_params.append(f"description={desc!r}")
            for k, v in constraints.items():
                field_params.append(f"{k}={v}")

            extra_dict["is_key"] = getattr(stmt, "i_is_key", False)
            extra_dict["tag"] = stmt.arg
            extra_dict["ns"] = self._get_module_namespace(stmt)
            if is_identityref:
                extra_dict["is_identityref"] = True
            field_params.append(f'ns="{self._ns_alias(extra_dict["ns"])}"')
            field_params.append(f"json_schema_extra={extra_dict!r}")
            assign = f"element({', '.join(field_params)})"

        else:
            # RESTCONF. `is_key` was emitted only on the NETCONF branch, so a
            # RESTCONF list item model carried nothing identifying which of its
            # fields are the list keys. That is what `ListNode.__call__` needs
            # to build the `/list=k1,k2` URL segment (RFC 8040 Sec 3.5.3), and a
            # generic caller has no other way to learn the key names -- they are
            # a YANG `key` statement, not part of the instance payload. Emitting
            # it here makes the two protocols symmetrical.
            extra_dict["is_key"] = getattr(stmt, "i_is_key", False)
            field_params = [f"json_schema_extra={extra_dict!r}"]
            if desc:
                field_params.append(f"description={desc!r}")
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
        """Extract ge/le from a YANG range statement (RFC 7950 Sec 9.2.2).

        A range is a union of intervals and may also carry bare single values:
        `1..5|7`, `0|3..5`, `min..10`, `1..max`. The enclosing bounds are the
        *smallest* lower bound and the *largest* upper bound across every
        alternative, so all parts are scanned. Reading only `parts[0]` and
        `parts[-1]` silently dropped a bound whenever an endpoint was a bare
        value: `1..5|7` lost `le=7` and `0|3..5` lost `ge=0`, which made the
        generated model accept values the device rejects.
        """
        constraints: dict[str, Any] = {}
        range_stmt = type_stmt.search_one("range")
        if not range_stmt:
            return constraints

        lower: float | None = None
        upper: float | None = None

        def as_number(token: str) -> float | None:
            try:
                return float(token) if "." in token else int(token)
            except ValueError:
                return None

        for part in range_stmt.arg.split("|"):
            part = part.strip()
            if ".." in part:
                lo, _, hi = part.partition("..")
                lo, hi = lo.strip(), hi.strip()
                if lo not in ("min", "maximum", "-inf"):
                    value = as_number(lo)
                    if value is not None and (lower is None or value < lower):
                        lower = value
                if hi not in ("max", "maximum", "+inf"):
                    value = as_number(hi)
                    if value is not None and (upper is None or value > upper):
                        upper = value
            elif part not in ("min", "max", "maximum"):
                # A bare alternative is a single legal value, so it is both a
                # lower and an upper bound of the union.
                value = as_number(part)
                if value is not None:
                    if lower is None or value < lower:
                        lower = value
                    if upper is None or value > upper:
                        upper = value

        if lower is not None:
            constraints["ge"] = lower
        if upper is not None:
            constraints["le"] = upper
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
            c = self._get_range_constraints(type_stmt)
            # RFC 7950 Sec 9.3.2: `fraction-digits` bounds the scale. Without
            # it the model accepted e.g. Decimal("1.234567") for a
            # fraction-digits 2 leaf, so the value only failed at the device.
            # The *serializer* is lossless (it only ever pads, never
            # truncates), so this is a local-validation gap, not data loss.
            fd = type_stmt.search_one("fraction-digits")
            if fd is not None:
                try:
                    c["decimal_places"] = int(fd.arg)
                except ValueError:  # pragma: no cover - malformed YANG
                    pass
            return "Decimal64", c
        elif yt == "boolean":
            return "bool", {}
        elif yt == "empty":
            # YANG `empty` is not a boolean. RESTCONF renders it as `[null]`
            # (RFC 7951 Sec 6.9) and NETCONF as an empty XML element
            # (RFC 7950 Sec 9.11); each template supplies the right alias for
            # `Empty`. Mapping it to `bool` put `true`/`false` on the wire.
            return "Empty", {}
        elif yt in ["binary", "bits", "instance-identifier"]:
            return "str", {}
        elif yt == "identityref":
            # Marked so the NETCONF emitter can bind the value's module prefix
            # in XML (RFC 7950 Sec 9.10.3); values are RFC 7951 Sec 6.8
            # module-name-qualified strings.
            return "str", {"_identityref": True}
        elif yt == "string":
            # This map carries ints, strs, bools and the "_patterns" list.
            c: dict[str, Any] = {}
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
                            description=self._docstring(d.arg) if d else None,
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

    def _get_default_value(self, stmt) -> str | None:
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

        # Emitted through repr() into a Pydantic `description=`, so it must
        # stay *unescaped* -- escaping here would be escaped a second time.
        return self._clean_text("\n".join(parts).strip())

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
        nodes_map: list[dict[str, Any]] = []

        def collect_nodes(stmt: Any) -> None:
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
                stmt: Any = entry["stmt"]
                suffix: str = entry["suffix"]
                depth: int = entry["depth"]
                parts = [self._to_class_name(stmt.arg)]
                curr = stmt
                for _ in range(int(depth)):
                    parent = getattr(curr, "parent", None)
                    if parent and parent.keyword not in ("module", "submodule"):
                        parts.insert(0, self._to_class_name(parent.arg))
                        curr = parent
                    else:
                        break
                full_name = "".join(parts) + str(suffix)
                entry["current_name"] = full_name
                name_registry.setdefault(full_name, []).append(entry)

            has_collision = False
            for entries in name_registry.values():
                if len(entries) > 1:
                    has_collision = True
                    for entry in entries:
                        entry["depth"] += 1

            if not has_collision:
                break

        for entry in nodes_map:
            stmt_any: Any = entry["stmt"]
            stmt_any._pydantic_class_name = entry["current_name"]

        def propagate_names(stmt: Any) -> None:
            orig = self._get_original_node(stmt)
            if orig and not getattr(stmt, "_pydantic_class_name", None):
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
        """Convert YANG name to Python field name (snake_case).

        Three reserved-name classes are handled, all of which produced
        *silently broken* -- or unimportable -- models before:

        * Python keywords (`class`, `import`, ...) -- already handled.
        * `pydantic.BaseModel` / `pydantic_xml.BaseXmlModel` attribute names.
          A YANG leaf named `schema` produced a field that pydantic itself
          warned "shadows an attribute in parent" and that resolved to the
          inherited `BaseModel.schema()` *method* at runtime, so reading the
          value raised `TypeError: object of type 'method' has no len()`.
          `json`, `dict`, `copy`, `construct` and `validate` are affected the
          same way, and these are ordinary YANG leaf names.
        * Builtins the emitter itself writes bare inside a class body -- see
          `_generated_code_builtins()`. A YANG leaf named `str` or a
          container named `property` made the generated module fail to
          *import* at all (`TypeError: 'property' object is not callable`),
          which the emitter's own `_validate_generated` parse check cannot
          catch because the file is syntactically valid.

        The wire name is unaffected: NETCONF models carry `element(tag=...)`
        and RESTCONF models carry `Field(alias=...)`, both derived from the
        original YANG name, so only the Python attribute is suffixed.
        """
        res = re.sub(r"[^a-zA-Z0-9]", "_", name)

        if keyword.iskeyword(res):
            res = f"{res}_"
        # `while`, not `if`: a YANG leaf literally named `str_` must not
        # collide with the escaped form of `str`.
        while res in _RESERVED_MODEL_ATTRS or res in _GENERATED_CODE_BUILTINS:
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

    def _clean_text(self, text: str) -> str:
        """Collapse a YANG description to a single safe line, *unescaped*.

        For consumers that apply their own escaping (a Pydantic
        `description=` field is emitted with `repr()`), so this must not
        touch backslashes or quotes.
        """
        return text.replace("\r", " ").replace("\n", " ").strip()

    def _docstring(self, text: str) -> str:
        r"""Escape a YANG description for a triple-quoted docstring literal.

        YANG descriptions are free text and routinely contain backslashes
        (XPath, regexes, Windows paths) and quote runs. Emitting either raw
        produced a generated file that does not parse: a trailing backslash
        escapes the closing quote, and three consecutive quotes terminate the
        literal early. The previous implementation replaced a triple quote
        with an identical triple quote (the replacement was written with a raw
        string, so both sides were the same text) -- a no-op that hid both
        cases while a test asserted the escaping was happening.
        """
        # Backslashes first, so the escapes introduced below are not doubled.
        return (
            self._clean_text(text).replace("\\", "\\\\").replace(_TRIPLE, '\\"\\"\\"')
        )

    def _escape_docstring(self, text: str) -> str:
        """Back-compat alias for the single-line cleaner (see `_docstring`)."""
        return self._clean_text(text)
