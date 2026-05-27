"""
YANG to Pydantic v2 converter plugin

Converts YANG modules to Pydantic v2 Python classes with proper handling of:
- Data nodes (container, list, leaf, leaf-list, choice/case)
- Groupings and uses statements
- RPCs and notifications
- Type mappings with validation
- RFC 7951 JSON encoding compliance
"""

import hashlib
import keyword
import optparse
import os
import re
from typing import Any

from pyang import plugin, statements


def pyang_plugin_init():
    """Register the plugin"""
    plugin.register_plugin(Yang2Restconf())


class Yang2Restconf(plugin.PyangPlugin):
    """Main plugin class for YANG to Pydantic conversion"""

    def __init__(self):
        plugin.PyangPlugin.__init__(self, "yang2restconf")
        self.multiple_modules = True

    def add_output_format(self, fmts):
        """Register the output format"""
        fmts["restconf"] = self

    def add_opts(self, optparser):
        """Add plugin-specific options"""
        optlist = [
            optparse.make_option(
                "--sdk-output-dir",
                dest="sdk_output_dir",
                default="./generated_sdk",
                help="Output directory for generated Pydantic client and models (default: ./generated_sdk)",
            ),
            optparse.make_option(
                "--sdk-config-only",
                dest="sdk_config_only",
                action="store_true",
                help="Only generate models for config true nodes",
            ),
        ]
        g = optparser.add_option_group("Pydantic output specific options")
        g.add_options(optlist)

    def setup_fmt(self, ctx):
        """Setup format context"""
        ctx.implicit_errors = False

    def emit(self, ctx, modules, fd):
        """Main emission function - generates Pydantic models"""
        # Directory structure setup
        output_dir = ctx.opts.sdk_output_dir
        config_only = ctx.opts.sdk_config_only

        models_dir = os.path.join(output_dir, "data_models")
        navigators_dir = os.path.join(output_dir, "data_navigators")
        templates_dir = os.path.join(output_dir, "user_templates")

        os.makedirs(models_dir, exist_ok=True)
        os.makedirs(navigators_dir, exist_ok=True)
        os.makedirs(templates_dir, exist_ok=True)

        global_data_nodes = []  # List of (module_py_name, class_name, alias, field_name)
        global_rpc_nodes = []  # List of (module_py_name, class_name, alias, field_name)

        # 1. Generate Pydantic Models
        for module in modules:
            converter = PydanticConverter(ctx, [module], config_only)
            output = converter.generate()
            module_py_name = module.arg.replace("-", "_")
            global_data_nodes.extend(
                [(module_py_name, c, a, f) for c, a, f in converter.root_data_registry]
            )
            global_rpc_nodes.extend(
                [(module_py_name, c, a, f) for c, a, f in converter.root_rpc_registry]
            )

            output_file = os.path.join(models_dir, f"{module_py_name}.py")
            with open(output_file, "w") as f:
                f.write(output)
            fd.write(f"Generated Model: {output_file}\n")

        # 2. Generate data_models/__init__.py
        init_file = os.path.join(models_dir, "__init__.py")
        with open(init_file, "w") as f:
            f.write("from __future__ import annotations\n\n")
            f.write('"""Auto-generated Pydantic models from YANG schemas"""\n')
            f.write("from typing import TYPE_CHECKING, Any\n\n")
            f.write("from pydantic import BaseModel, ConfigDict, Field\n\n")
            f.write("if TYPE_CHECKING:\n")
            for module in modules:
                module_py_name = module.arg.replace("-", "_")

                module_nodes = [
                    c
                    for m, c, a, fl in global_data_nodes + global_rpc_nodes
                    if m == module_py_name
                ]
                if module_nodes:
                    f.write(f"    from . import {module_py_name}\n")

            f.write("\nclass Data(BaseModel):\n")
            f.write('    """Aggregate root data nodes (config and state)."""\n')
            if not global_data_nodes:
                f.write("    pass\n")
            else:
                for m, cls, alias, field in global_data_nodes:
                    f.write(
                        f"    {m}_{field}: {m}.{cls} | None = Field(None, alias='{alias}')\n"
                    )
            f.write(
                "\n    model_config = ConfigDict(extra='forbid', validate_assignment=True, validate_default=True, defer_build=True)\n"
            )

            f.write("\n\nclass Operations(BaseModel):\n")
            f.write('    """Aggregate RPC operations."""\n')
            if not global_rpc_nodes:
                f.write("    pass\n")
            else:
                for m, cls, alias, field in global_rpc_nodes:
                    f.write(
                        f"    {m}_{field}: {m}.{cls} | None = Field(None, alias='{alias}')\n"
                    )
            f.write(
                "\n    model_config = ConfigDict(extra='forbid', validate_assignment=True, validate_default=True, defer_build=True)\n"
            )

        fd.write(f"Generated Models Init: {init_file}\n")

        # 3. Generate Client Scaffolding (Always)
        self._generate_session_manager(output_dir)
        self._generate_client_init(output_dir)
        self._generate_navigator_base(navigators_dir)
        self._generate_model_base(models_dir)
        self._generate_user_templates(templates_dir)

        gen = ClientGenerator(ctx, modules, global_data_nodes, global_rpc_nodes)
        gen.generate(navigators_dir)

        fd.write(f"Generated Client Scaffolding in: {output_dir}\n")

    def _generate_user_templates(self, directory):
        content = """\"\"\"
Custom configuration templates are defined in this directory. 

To maintain consistency, the directory structure here should mirror `data_navigators/`. 
Templates are invoked using the `.from_template()` method of the corresponding 
navigator node.

Example:
If `../data_navigators/ne.py` contains `RoutingProtocolListNode`, implement the 
template logic in `../templates/ne.py` as follows:

```python
@TemplateRegistry.register("RoutingProtocolListNode")
def build_routing_config(
    *,
    ospf_router_id: str | None,
    is_ospf_asbr: bool | None,
    another_config_variable: str | None
) -> list[ne.RoutingProtocolItem]:
    from ..data_models.ne import RoutingProtocolItem

    # Transform input variables into valid Pydantic model instances.
    # The return type must match the navigator's .retrieve() method 
    # to support direct create, update, or replace operations.
    return [RoutingProtocolItem(...)]
```

Once registered, you can generate configuration by calling 
`node.from_template(...)` on any instance of the associated node.
\"\"\"

import importlib
import pkgutil
from pathlib import Path

# Automatically import all modules in the current directory.
# This triggers the @TemplateRegistry.register decorators in each file.
for loader, module_name, is_pkg in pkgutil.iter_modules(__path__):
    if module_name != "__init__":
        importlib.import_module(f".{module_name}", package=__name__)

# Clean up namespace so only the modules/registry remain if desired
del pkgutil, importlib, Path
for _var in ('loader', 'module_name', 'is_pkg'):
    if _var in dir():
        del _var

"""
        with open(os.path.join(directory, "__init__.py"), "w") as f:
            f.write(content)


    def _generate_session_manager(self, directory):
        content = """from __future__ import annotations

import logging
import os
import socket
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3 import PoolManager

log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())


class TCPKeepAliveAdapter(HTTPAdapter):
    def __init__(self, idle=60, interval=60, count=6, **kwargs):
        self._idle = idle
        self._interval = interval
        self._count = count
        super().__init__(**kwargs)

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        pool_kwargs["socket_options"] = self._socket_options()
        self.poolmanager = PoolManager(
            num_pools=connections,
            maxsize=maxsize,
            block=block,
            **pool_kwargs,
        )

    def proxy_manager_for(self, proxy, **proxy_kwargs):
        proxy_kwargs["socket_options"] = self._socket_options()
        return super().proxy_manager_for(proxy, **proxy_kwargs)

    def _socket_options(self):
        options = [
            (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
        ]

        if hasattr(socket, "TCP_KEEPIDLE"):
            options.append((socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, self._idle))
        if hasattr(socket, "TCP_KEEPINTVL"):
            options.append((socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, self._interval))
        if hasattr(socket, "TCP_KEEPCNT"):
            options.append((socket.IPPROTO_TCP, socket.TCP_KEEPCNT, self._count))

        return options


class RestconfClient:
    \"\"\"Restconf API client.\"\"\"

    def __init__(
        self,
        loopback_ip: str | None = None,
        management_ip: str | None = None,
        port: int = 8443,
        username: str | None = None,
        password: str | None = None,
        verify: bool = True,
    ):
        self.url = None
        self.fallback_url = None

        self.urls = []
        if loopback_ip:
            self.urls.append(f"https://{loopback_ip}:{port}/restconf")
        if management_ip:
            self.urls.append(f"https://{management_ip}:{port}/restconf")

        if not self.urls:
            raise ValueError("Either loopback_ip or management_ip must be provided")

        self._session = requests.Session()

        adapter = TCPKeepAliveAdapter(idle=60, interval=60, count=6)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

        self._session.verify = verify  # Warning: production risk if False
        self._session.trust_env = verify  # Disable env vars if TLS verification is off
        self._session.headers.update({"Content-Type": "application/yang-data+json"})

        user = username or os.environ.get("DEVICE_USERNAME")
        pw = password or os.environ.get("DEVICE_PASSWORD")
        if not user or not pw:
            raise UserWarning("Authentication credentials missing.")
        self._session.auth = (user, pw)

        from .data_navigators import Data, Operations

        self.data = Data(self, "/data", "")
        self.operations = Operations(self, "/operations", "")

    def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        \"\"\"Make authenticated API request.\"\"\"
        errors = []

        for base_url in self.urls:
            url = base_url + path
            try:
                msg = f"Request: {method} {url} {kwargs}"
                log.info(msg)
                response = self._session.request(method, url, timeout=(10, 2400), **kwargs)

                msg = f"Response ({response.status_code}): {response.text}"
                log.info(msg)
                response.raise_for_status()
                return response.json() if response.text.strip() else {}

            except (requests.ConnectionError, requests.Timeout) as e:
                msg = f"Failed to connect to {base_url}: {e}"
                log.exception(msg)
                errors.append(e)
                continue  # Try the next URL in self.urls

            except requests.HTTPError as e:
                # Capture the response body for debugging before crashing
                status = e.response.status_code
                text = e.response.text
                msg = f"HTTP {status} Error: {text}"
                log.exception(msg)
                raise requests.HTTPError(msg, response=e.response) from e

        # If we get here, all URLs failed
        msg = f"All connection attempts to {self.urls} have failed."
        raise ExceptionGroup(msg, errors)
"""
        with open(os.path.join(directory, "session_manager.py"), "w") as f:
            f.write(content)

    def _generate_client_init(self, directory):
        content = """from . import user_templates
from .session_manager import RestconfClient
"""
        with open(os.path.join(directory, "__init__.py"), "w") as f:
            f.write(content)

    def _generate_model_base(self, directory):
        content = """from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class YangBaseModel(BaseModel):
    \"\"\"Base for all YANG-derived models.
    Add exclude fields based on json_schema_extra['is_config'] and content required.
    \"\"\"
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        defer_build=True,
        validate_default=True
    )

    def model_dump(self, *, content: Literal["config", "all", "nonconfig"] = "all", **kwargs: Any) -> dict[str, Any]:
        # Force JSON mode to ensure 64-bit numbers become strings and Enums become values
        kwargs.setdefault("mode", "json")
        kwargs.setdefault("by_alias", True)
        
        if content == "all":
            return super().model_dump(**kwargs)

        cls = type(self)
        exclude: set[str] = set()
        for field_name, field_info in cls.model_fields.items():
            is_config = field_info.json_schema_extra.get("is_config", True)
            if content == "config" and not is_config:
                exclude.add(field_name)
            if content == "nonconfig" and is_config:
                exclude.add(field_name)


        # Merge with any exclude the caller already passed
        user_exclude = kwargs.get("exclude")
        if user_exclude:
            if isinstance(user_exclude, (set, list, tuple)):
                exclude |= set(user_exclude)
            else: # dict-style exclude not implemented yet
                raise NotImplementedError

        kwargs["exclude"] = exclude if exclude else None
        return super().model_dump(**kwargs)

"""
        with open(os.path.join(directory, "_base.py"), "w") as f:
            f.write(content)

    def _generate_navigator_base(self, directory):
        content = """from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Generic, TypeVar
from urllib.parse import quote

if TYPE_CHECKING:
    from ..session_manager import RestconfClient


def _get_max_depth(data: Any, current_depth: int = 1) -> int:
    \"\"\"Calculates max depth, treating lists of primitives as leaf values.\"\"\"
    if isinstance(data, dict) and data:
        return max((_get_max_depth(v, current_depth + 1) for v in data.values()), default=current_depth)

    if isinstance(data, list) and data:
        # Check if the list contains complex structures (dicts or nested lists)
        if any(isinstance(item, (dict, list)) for item in data):
            return max((_get_max_depth(item, current_depth + 1) for item in data), default=current_depth)
        # If it's a list of primitives (leaf-list), don't increment depth
        return current_depth

    return current_depth


def _prune_at_max_depth(data: Any, target_depth: int, current_depth: int = 1) -> Any:
    \"\"\"Deletes empty dicts only if they are located at the target_depth.\"\"\"
    if isinstance(data, dict):
        # If we reached target depth and it's an empty dict, this is a candidate for deletion
        # However, the parent call handles the deletion. Here we filter children.
        return {
            k: _prune_at_max_depth(v, target_depth, current_depth + 1)
            for k, v in data.items()
            if not (isinstance(v, dict) and not v and current_depth + 1 == target_depth)
        }
    if isinstance(data, list):
        return [
            _prune_at_max_depth(item, target_depth, current_depth + 1)
            for item in data
            if not (isinstance(item, (dict, list)) and not item and current_depth + 1 == target_depth)
        ]
    return data


class TemplateRegistry:
    _registry: dict[str, Callable] = {}

    @classmethod
    def register(cls, node_class_name: str, func: Callable):
        def decorator(func: Callable):
            cls._registry[node_class_name] = func
            return func
        return decorator

    @classmethod
    def get(cls, node_class_name: str) -> Callable | None:
        return cls._registry.get(node_class_name)


class Node:
    __slots__ = ("_client", "_name", "_path")

    def __init__(self, client: RestconfClient, path: str, name: str):
        self._client = client
        self._path = path
        self._name = name

    def _retrieve(
        self,
        *,
        content: str = "all",
        with_defaults: str = "report-all",
        depth: int | str = "unbounded",
        fields: list[str] | None = None,
    ) -> Any:
        params = {
            "content": content,
            "depth": depth,
            "with-defaults": with_defaults,
        }
        if fields:
            params["fields"] = ";".join(fields)

        resp = self._client._request("GET", self._path, params=params)

        payload = None

        # Handle if the response is a dictionary
        if isinstance(resp, dict):
            # Check for exact key match
            if self._name in resp:
                inner = resp[self._name]
                payload = inner

            # Check for namespaced key match (e.g. ietf-hardware:slot-item)
            else:
                # Find any key that ends with ":name"
                found_keys = [k for k in resp.keys() if k.endswith(f":{self._name}")]
                if found_keys:
                    inner = resp[found_keys[0]]
                    payload = inner
                else:
                    # Fallback: Assume the response itself is the object (unwrapped)
                    payload = resp

        else:
            raise TypeError(f"Unexpected response type: {type(resp)}")

        if depth != "unbounded":
            observed_max = _get_max_depth(payload)
            payload = _prune_at_max_depth(payload, observed_max)

        return payload

    def _update(self, **kwargs: Any) -> None:
        kwargs = {k: v for k, v in kwargs.items() if not ((isinstance(v, list) or isinstance(v, dict)) and len(v) == 0)}
        data = {self._name: kwargs}
        return self._client._request("PATCH", self._path, json=data)

    def _replace(self, **kwargs: Any) -> None:
        kwargs = {k: v for k, v in kwargs.items() if not ((isinstance(v, list) or isinstance(v, dict)) and len(v) == 0)}
        data = {self._name: kwargs}
        return self._client._request("PUT", self._path, json=data)

    def delete(self) -> None:
        return self._client._request("DELETE", self._path)

    def from_template(self, **kwargs: Any) -> dict[str, Any]:
        \"\"\"Call user template if registered.\"\"\"
        node_name = self.__class__.__name__
        func = TemplateRegistry.get(node_name)
        if not func:
            msg = f"No template registered for {node_name}. You can register one using @TemplateRegistry.register('{node_name}')"
            raise NotImplementedError(msg)
        return func(**kwargs)


class ItemNode:
    __slots__ = ("_client", "_name", "_path")

    def __init__(self, client: RestconfClient, path: str, name: str):
        self._client = client
        self._path = path
        self._name = name

    def _retrieve(
        self,
        *,
        content: str = "all",
        with_defaults: str = "report-all",
        depth: int | str = "unbounded",
        fields: list[str] | None = None,
    ) -> Any:
        params = {
            "content": content,
            "depth": depth,
            "with-defaults": with_defaults,
        }
        if fields:
            params["fields"] = ";".join(fields)
        resp = self._client._request("GET", self._path, params=params)

        payload = None

        # Handle if the response is a list (e.g., direct list return)
        if isinstance(resp, list):
            if not resp:
                raise ValueError(f"Received empty list from API for {self._name}")
            payload = resp[0]

        # Handle if the response is a dictionary
        elif isinstance(resp, dict):
            # Check for exact key match
            if self._name in resp:
                inner = resp[self._name]
                if isinstance(inner, list):
                    if not inner:
                        raise ValueError(f"Received empty list inside key '{self._name}'")
                    payload = inner[0]
                else:
                    # Handle case where it returns a single object instead of a list
                    payload = inner

            # Check for namespaced key match (e.g. ietf-hardware:slot-item)
            else:
                # Find any key that ends with ":name"
                found_keys = [k for k in resp.keys() if k.endswith(f":{self._name}")]

                if found_keys:
                    inner = resp[found_keys[0]]
                    if isinstance(inner, list):
                        if not inner:
                            raise ValueError(f"Received empty list inside key '{found_keys[0]}'")
                        payload = inner[0]
                    else:
                        payload = inner
                else:
                    # Fallback: Assume the response itself is the object (unwrapped)
                    payload = resp

        else:
            raise TypeError(f"Unexpected response type: {type(resp)}")

        if depth != "unbounded":
            observed_max = _get_max_depth(payload)
            payload = _prune_at_max_depth(payload, observed_max)

        return payload

    def _update(self, **kwargs: Any) -> None:
        kwargs = {k: v for k, v in kwargs.items() if not ((isinstance(v, list) or isinstance(v, dict)) and len(v) == 0)}
        data = {self._name: [kwargs]}
        return self._client._request("PATCH", self._path, json=data)

    def _replace(self, **kwargs: Any) -> None:
        kwargs = {k: v for k, v in kwargs.items() if not ((isinstance(v, list) or isinstance(v, dict)) and len(v) == 0)}
        data = {self._name: [kwargs]}
        return self._client._request("PUT", self._path, json=data)

    def delete(self) -> None:
        return self._client._request("DELETE", self._path)

    def from_template(self, **kwargs: Any) -> dict[str, Any]:
        \"\"\"Call user template if registered.\"\"\"
        node_name = self.__class__.__name__
        func = TemplateRegistry.get(node_name)
        if not func:
            msg = f"No template registered for {node_name}. You can register one using @TemplateRegistry.register('{node_name}')"
            raise NotImplementedError(msg)
        return func(**kwargs)


TNode = TypeVar("TNode", bound=ItemNode)


class ListNode(Generic[TNode]):
    __slots__ = ("_client", "_item_cls", "_name", "_path")

    def __init__(
        self,
        client: RestconfClient,
        path: str,
        name: str,
        item_cls: type[TNode],
    ):
        self._client = client
        self._path = path
        self._name = name
        self._item_cls = item_cls

    def __call__(self, *keys: str | int) -> TNode:
        \"\"\"
        Supports composite keys.
        Usage: client.data.interfaces('eth0')
        or client.data.routes('1.1.1.1', 'vrf-A')
        \"\"\"
        if not keys:
            raise ValueError("Keys must be provided")

        encoded_keys = ",".join(quote(str(k), safe="") for k in keys)

        new_path = f"{self._path}={encoded_keys}"

        return self._item_cls(self._client, new_path, self._name)

    def _retrieve(
        self,
        *,
        content: str = "all",
        with_defaults: str = "report-all",
        depth: int | str = "unbounded",
        fields: list[str] | None = None,
    ) -> Any:
        params = {
            "content": content,
            "depth": depth,
            "with-defaults": with_defaults,
        }
        if fields:
            params["fields"] = ";".join(fields)
        resp = self._client._request("GET", self._path, params=params)

        data_list = []

        # 1. Handle Root List
        if isinstance(resp, list):
            data_list = resp

        # 2. Handle Dictionary
        elif isinstance(resp, dict):
            # 2a. Exact key match
            if self._name in resp:
                inner = resp[self._name]
                if isinstance(inner, list):
                    data_list = inner
                elif isinstance(inner, dict):
                    data_list = [inner]
                elif inner is None:
                    data_list = []
                else:
                    # Fallback for primitives or unknown types, though unlikely for a list node
                    # We might want to raise, but let's try to treat as single item or error?
                    # Raising is safer.
                    raise ValueError(f"Unexpected value type for '{self._name}': {type(inner)}")

            # 2b. Namespaced key match
            else:
                found_keys = [k for k in resp.keys() if k.endswith(f":{self._name}")]
                if found_keys:
                    inner = resp[found_keys[0]]
                    if isinstance(inner, list):
                        data_list = inner
                    elif isinstance(inner, dict):
                        data_list = [inner]
                    elif inner is None:
                        data_list = []
                else:
                    # 2c. No wrapper found. Assume response is a single item (unwrapped)
                    # For a list retrieval, this is the "list of 1 item" case where the list wrapper is missing.
                    data_list = [resp]
        else:
            raise TypeError(f"Unexpected response type: {type(resp)}")

        if depth != "unbounded":
            observed_max = _get_max_depth(data_list)
            data_list = _prune_at_max_depth(data_list, observed_max)

        return data_list

    def _create(self, data_list: list[dict[str, Any]]) -> None:
        envelope = {self._name: data_list}
        url = self._path.removesuffix(f"/{self._name}")
        self._client._request("POST", url, json=envelope)

    def _replace(self, data_list: list[dict[str, Any]]) -> None:
        envelope = {self._name: data_list}
        url = self._path.removesuffix(f"/{self._name}")
        return self._client._request("PUT", url, json=envelope)

    def delete(self) -> None:
        return self._client._request("DELETE", self._path)

    def from_template(self, **kwargs: Any) -> dict[str, Any]:
        \"\"\"Call user template if registered.\"\"\"
        node_name = self.__class__.__name__
        func = TemplateRegistry.get(node_name)
        if not func:
            msg = f"No template registered for {node_name}. You can register one using @TemplateRegistry.register('{node_name}')"
            raise NotImplementedError(msg)
        return func(**kwargs)


"""
        with open(os.path.join(directory, "_base.py"), "w") as f:
            f.write(content)


class PydanticConverter:
    """Converter from YANG to Pydantic models"""

    def __init__(self, ctx, modules, config_only=False):
        self.ctx = ctx
        self.modules = modules
        self.config_only = config_only

        self.classes: list[str] = []
        self.imports: set[str] = set()
        self.groupings: dict[str, any] = {}
        self.module_names: set[str] = set()

        self.enum_registry: dict[str, str] = {}
        self.uses_refs: dict[str, str] = {}  # uses arg -> class name

        self.root_data_registry: list[
            tuple[str, str, str]
        ] = []  # (class, alias, field)
        self.root_rpc_registry: list[tuple[str, str, str]] = []  # (class, alias, field)

    def generate(self) -> str:
        """Generate complete Python module with Pydantic models"""

        # 1. Resolve all class names with collision detection first
        for module in self.modules:
            self._resolve_names(module)

        # 2. Collect groupings and module names
        for module in self.modules:
            self._collect_groupings(module)
            self.module_names.add(module.arg)

        for module in self.modules:
            self._generate_module_classes(module)

        output = self._generate_header()
        output += "\n\n"
        output += "\n\n".join(self.classes)
        output += "\n"

        return output

    def _generate_header(self) -> str:
        """Generate file header with imports and validator helper"""
        header = """\"\"\"Auto-generated Pydantic models from YANG schemas\"\"\"

from typing import Annotated, Any, TypeVar
from pydantic import BaseModel, Field, field_validator, ConfigDict, AfterValidator, BeforeValidator, PlainSerializer
from enum import Enum
from decimal import Decimal
import re

from ._base import YangBaseModel

# RFC 7951: 64-bit numbers MUST be represented as JSON strings.
# here we use PlainSerializer to ensure string output during JSON serialization (mode='json').
def format_at_least_two_places(v: Decimal) -> str:
    # Normalize to remove unnecessary trailing zeros (e.g., 1.500 -> 1.5)
    v = v.normalize()
     
    # Exponent 0 = integer (1), -1 = one decimal (1.1), -2 = two decimals (1.11)
    # If it's greater than -2, it means we have 0 or 1 decimal places.
    if v.as_tuple().exponent > -2:
        return format(v.quantize(Decimal("0.01")), "f")
    
    # Otherwise, return the normalized string (keeps 3+ decimal places)
    return format(v, "f")

Decimal64 = Annotated[
    Decimal, 
    PlainSerializer(format_at_least_two_places, return_type=str)
]
Uint64 = Annotated[int, PlainSerializer(lambda v: str(v), return_type=str)]
Int64 = Annotated[int, PlainSerializer(lambda v: str(v), return_type=str)]

def check_pattern(pattern: str, v: str) -> str:
    if isinstance(v, str) and not re.match(pattern, v):
        raise ValueError(f'Value does not match pattern: {pattern}')
    return v

T = TypeVar("T")

def restconf_list_validator(v: Any) -> Any:
    \"\"\"
    RESTCONF quirk: Truncated lists (via depth) often return {} instead of [].
    Also handles 'None' or missing data if needed.
    \"\"\"
    if isinstance(v, dict) and not v:
        return []
    # In some truly cursed implementations, a single item list is returned as an object
    # but we will stick to the 'empty dict to list' fix for now.
    return v

# Define a reusable type for RESTCONF lists
RestconfList = Annotated[list[T], BeforeValidator(restconf_list_validator)]"""

        if self.imports:
            header += "\n"
            for imp in sorted(self.imports):
                header += f"{imp}\n"

        return header

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

    def _collect_groupings(self, stmt):
        """Recursively collect all groupings"""
        if stmt.keyword == "grouping":
            key = self._get_qualified_name(stmt)
            self.groupings[key] = stmt

        if hasattr(stmt, "i_children"):
            for child in stmt.i_children:
                self._collect_groupings(child)

    def _get_original_node(self, stmt):
        """
        Pyang does not provide an `i_orig_stmt` pointer.
        We must manually backtrack through the `uses` expansion to find the original node inside the grouping.
        """
        if not hasattr(stmt, "i_uses") or not stmt.i_uses:
            return None

        # Pyang appends nested uses statements to the list, so the last one is the immediate trigger
        uses_stmt = (
            stmt.i_uses[-1] if isinstance(stmt.i_uses, (list, tuple)) else stmt.i_uses
        )
        grouping = getattr(uses_stmt, "i_grouping", None)
        if not grouping:
            return None

        path = []
        curr = stmt
        # Walk up from the copied node until we hit the parent where the uses statement was executed
        while curr is not None and curr != getattr(uses_stmt, "parent", None):
            path.append((curr.keyword, curr.arg))
            curr = getattr(curr, "parent", None)

        if curr is None:
            return None

        path.reverse()  # Now it's top-down from the grouping's perspective

        # Walk down the original grouping to find the exact corresponding node
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
        """
        Iteratively resolve class names to ensure uniqueness within the module.
        Strategy: Start with simple name. If collision, walk up the ancestry tree.
        Stores result in stmt._pydantic_class_name.
        """
        nodes_map = []

        def collect_nodes(stmt):
            # 1. Deduplicate identical nodes copied from groupings.
            orig = self._get_original_node(stmt)
            if orig and len(getattr(stmt, "i_children", [])) == len(
                getattr(orig, "i_children", [])
            ):
                return  # Skip, let it inherit the name from the grouping

            if stmt.keyword in ["container", "rpc", "grouping"]:
                nodes_map.append({"stmt": stmt, "suffix": "", "depth": 0})
            elif stmt.keyword == "list":
                nodes_map.append({"stmt": stmt, "suffix": "Item", "depth": 0})
            elif stmt.keyword == "notification":
                nodes_map.append({"stmt": stmt, "suffix": "Notification", "depth": 0})

            # Traverse standard data children
            if hasattr(stmt, "i_children"):
                for child in stmt.i_children:
                    collect_nodes(child)

            # 2. Traverse groupings. They live in substmts, not i_children!
            # Without this, the original grouping nodes never get a base class name.
            if hasattr(stmt, "substmts"):
                for sub in stmt.substmts:
                    if sub.keyword == "grouping":
                        collect_nodes(sub)

        collect_nodes(module)

        # Iteration loop
        max_iterations = 30
        for _ in range(max_iterations):
            name_registry = {}  # name -> list of node_entries

            for entry in nodes_map:
                stmt = entry["stmt"]
                suffix = entry["suffix"]
                depth = entry["depth"]

                # Build name walking up 'depth' parents
                parts = [self._to_class_name(stmt.arg)]
                curr = stmt
                for _ in range(depth):
                    parent = getattr(curr, "parent", None)
                    if parent and parent.keyword not in ("module", "submodule"):
                        parts.insert(0, self._to_class_name(parent.arg))
                        curr = parent
                    else:
                        break  # Hit root

                full_name = "".join(parts) + suffix
                entry["current_name"] = full_name

                if full_name not in name_registry:
                    name_registry[full_name] = []
                name_registry[full_name].append(entry)

            # Check collisions
            has_collision = False
            for name, entries in name_registry.items():
                if len(entries) > 1:
                    has_collision = True
                    for entry in entries:
                        entry["depth"] += 1

            if not has_collision:
                break

        # Apply final names
        for entry in nodes_map:
            entry["stmt"]._pydantic_class_name = entry["current_name"]

        # Post-pass: Propagate the deduplicated names to Pyang's copied nodes
        def propagate_names(stmt):
            orig = self._get_original_node(stmt)
            if orig:
                # If it wasn't given a unique name (because we skipped it above), inherit the original
                if not getattr(stmt, "_pydantic_class_name", None):
                    orig_name = getattr(orig, "_pydantic_class_name", None)
                    if orig_name:
                        stmt._pydantic_class_name = orig_name

            if hasattr(stmt, "i_children"):
                for child in stmt.i_children:
                    propagate_names(child)

        propagate_names(module)

    def _generate_module_classes(self, module):
        """Generate classes for a module"""

        for grouping_key, grouping in self.groupings.items():
            if self._get_module_name(grouping) == module.arg and hasattr(
                grouping, "_pydantic_class_name"
            ):
                class_name = grouping._pydantic_class_name
                self.uses_refs[grouping.arg] = class_name
                class_code = self._generate_grouping_class(grouping, class_name)
                if class_code:
                    self.classes.append(class_code)

        data_children = [
            ch
            for ch in module.i_children
            if ch.keyword in statements.data_definition_keywords
        ]

        if data_children:
            for child in data_children:
                if child.keyword in ["container", "list"]:
                    # Use resolved name
                    class_name = getattr(
                        child, "_pydantic_class_name", self._to_class_name(child.arg)
                    )
                    # For lists, the class name generated is for the Item, handled inside _generate_class logic or passed?
                    # _generate_class logic below uses class_name for container, or item_type for list.
                    class_code = self._generate_class(child, class_name=class_name)
                    if class_code:
                        self.classes.append(class_code)

                    self.root_data_registry.append(
                        (
                            class_name,
                            f"{module.arg}:{child.arg}",
                            self._to_field_name(child.arg),
                        )
                    )

            root_class = self._generate_root_data_class(module, data_children)
            if root_class:
                self.classes.append(root_class)

        rpcs = [ch for ch in module.i_children if ch.keyword == "rpc"]
        for rpc in rpcs:
            class_code = self._generate_rpc_class(rpc)
            if class_code:
                self.classes.append(class_code)

                self.root_rpc_registry.append(
                    (
                        rpc._pydantic_class_name,
                        f"{module.arg}:{rpc.arg}",
                        self._to_field_name(rpc.arg),
                    )
                )

        notifs = [ch for ch in module.i_children if ch.keyword == "notification"]
        for notif in notifs:
            class_code = self._generate_notification_class(notif)
            if class_code:
                self.classes.append(class_code)

    def _generate_grouping_class(self, grouping, class_name):
        """Generate a class for a grouping"""
        lines = []

        lines.append(f"class {class_name}(YangBaseModel):")

        desc = grouping.search_one("description")
        if desc:
            lines.append(f'    """{self._escape_docstring(desc.arg)}"""')
        else:
            lines.append(f'    """Grouping: {grouping.arg}"""')

        lines.append("")

        children = getattr(grouping, "i_children", [])
        fields = self._generate_fields(children, grouping)
        if not fields:
            lines.append("    pass")
        else:
            lines.extend(fields)

        return "\n".join(lines)

    def _generate_class(self, stmt, class_name=None, bypass_config_check=False):
        """Generate a Pydantic class for a YANG node"""
        if (
            not bypass_config_check
            and self.config_only
            and hasattr(stmt, "i_config")
            and not stmt.i_config
        ):
            return None

        if class_name is None:
            class_name = getattr(
                stmt, "_pydantic_class_name", self._to_class_name(stmt.arg)
            )

        lines = []

        lines.append(f"class {class_name}(YangBaseModel):")

        desc = stmt.search_one("description")
        if desc:
            lines.append(f'    """{self._escape_docstring(desc.arg)}"""')
        else:
            lines.append(f'    """{stmt.keyword.capitalize()}: {stmt.arg}"""')

        lines.append("")

        if hasattr(stmt, "i_children"):
            fields = self._generate_fields(
                stmt.i_children,
                stmt,
                parent_class_name=class_name,
                bypass_config_check=bypass_config_check,
            )
            if not fields:
                lines.append("    pass")
            else:
                lines.extend(fields)
        else:
            lines.append("    pass")

        return "\n".join(lines)

    def _generate_root_data_class(self, module, children):
        """Generate root data class for module"""
        class_name = f"{self._to_class_name(module.arg)}Data"

        lines = []
        lines.append(f"class {class_name}(YangBaseModel):")
        lines.append(f'    """Root data model for {module.arg}"""')
        lines.append("")

        fields = self._generate_fields(children, module)
        if not fields:
            lines.append("    pass")
        else:
            lines.extend(fields)

        return "\n".join(lines)

    def _generate_rpc_class(self, rpc):
        """Generate classes for RPC"""
        sub_classes = []
        base_name = getattr(rpc, "_pydantic_class_name", self._to_class_name(rpc.arg))

        envelope_lines = [f"class {base_name}(BaseModel):", f'    """RPC: {rpc.arg}"""']

        input_stmt = rpc.search_one("input")
        if input_stmt and hasattr(input_stmt, "i_children") and input_stmt.i_children:
            suffix = "Input"
            cls_name = (
                base_name if base_name.endswith(suffix) else f"{base_name}{suffix}"
            )
            input_class = self._generate_class(
                input_stmt, cls_name, bypass_config_check=True
            )
            if input_class:
                sub_classes.append(input_class)
                envelope_lines.append(f"    input: {cls_name}")

        output_stmt = rpc.search_one("output")
        if (
            output_stmt
            and hasattr(output_stmt, "i_children")
            and output_stmt.i_children
        ):
            suffix = "Output"
            cls_name = (
                base_name if base_name.endswith(suffix) else f"{base_name}{suffix}"
            )
            output_class = self._generate_class(
                output_stmt, cls_name, bypass_config_check=True
            )
            if output_class:
                sub_classes.append(output_class)
                envelope_lines.append(
                    f"    output: {cls_name} | None = Field(default=None)"
                )

        if not sub_classes:
            envelope_lines.append("    pass")

        envelope_lines.append(
            "\n    model_config = ConfigDict(extra='forbid', validate_assignment=True, validate_default=True, defer_build=True)"
        )

        result = "\n\n".join(sub_classes)
        if result:
            result += "\n\n"
        result += "\n".join(envelope_lines)

        return result

    def _generate_notification_class(self, notif):
        """Generate class for notification"""
        base_name = getattr(
            notif, "_pydantic_class_name", self._to_class_name(notif.arg)
        )
        suffix = "Notification"
        class_name = base_name if base_name.endswith(suffix) else f"{base_name}{suffix}"

        return self._generate_class(notif, class_name, bypass_config_check=True)

    def _generate_fields(
        self, children, parent_stmt, parent_class_name=None, bypass_config_check=False
    ) -> list[str]:
        """Generate Pydantic fields from YANG children"""
        fields = []

        filtered_children = []
        for child in children:
            if (
                not bypass_config_check
                and self.config_only
                and hasattr(child, "i_config")
                and not child.i_config
            ):
                continue
            filtered_children.append(child)

        processed_children = []
        for child in filtered_children:
            if child.keyword == "uses":
                grouping_name = child.arg
                if grouping_name in self.uses_refs:
                    fields.append(f"    # Uses: {grouping_name}")

                    grouping = self.groupings.get(grouping_name)
                    if grouping and hasattr(grouping, "i_children"):
                        processed_children.extend(grouping.i_children)
                continue
            processed_children.extend([child])

        choice_fields = []
        for child in processed_children:
            if child.keyword == "choice":
                choice_fields.extend(
                    self._generate_choice_fields(child, parent_stmt, parent_class_name)
                )
                continue

            if child.keyword == "case":
                continue

            field_code = self._generate_field(
                child,
                parent_stmt,
                parent_class_name=parent_class_name,
                bypass_config_check=bypass_config_check,
            )
            if field_code:
                fields.append(field_code)

        fields.extend(choice_fields)

        return fields

    def _generate_choice_fields(
        self, choice_stmt, parent_stmt, parent_class_name=None
    ) -> list[str]:
        """Generate fields for a choice node"""
        fields = []

        fields.append(f"    # Choice: {choice_stmt.arg}")

        for case in choice_stmt.i_children:
            if case.keyword == "case":
                fields.append(f"    # Case: {case.arg}")

                if hasattr(case, "i_children"):
                    for case_child in case.i_children:
                        field_code = self._generate_field(
                            case_child,
                            parent_stmt,
                            parent_class_name=parent_class_name,
                            is_choice=True,
                            bypass_config_check=False,
                        )
                        if field_code:
                            fields.append(field_code)

        return fields

    def _generate_field(
        self,
        stmt,
        parent_stmt,
        parent_class_name=None,
        is_choice=False,
        bypass_config_check=False,
    ) -> str:
        """Generate a single Pydantic field"""
        field_name = self._to_field_name(stmt.arg)

        constraints = {}
        type_str = "Any"
        is_optional = False
        is_config = getattr(stmt, "i_config", True)
        field_params = [f"json_schema_extra={{'is_config': {is_config}}}"]

        if stmt.keyword == "container":
            type_str = getattr(
                stmt, "_pydantic_class_name", self._to_class_name(stmt.arg)
            )
            nested_class = self._generate_class(
                stmt, class_name=type_str, bypass_config_check=bypass_config_check
            )
            if nested_class and nested_class not in self.classes:
                self.classes.append(nested_class)
            is_optional = not self._is_mandatory(stmt)

        elif stmt.keyword == "list":
            item_type = getattr(
                stmt, "_pydantic_class_name", self._to_class_name(stmt.arg) + "Item"
            )
            nested_class = self._generate_class(
                stmt, class_name=item_type, bypass_config_check=bypass_config_check
            )
            if nested_class and nested_class not in self.classes:
                self.classes.append(nested_class)
            type_str = f"RestconfList[{item_type}]"
            is_optional = not self._is_mandatory(stmt)

        elif stmt.keyword == "leaf":
            type_str, constraints = self._get_leaf_type(stmt)

            if "_patterns" in constraints:
                patterns = constraints.pop("_patterns")
                validators = []

                for p in patterns:
                    py_pat = self._convert_yang_regex(p)

                    full_pat = f"^(?:{py_pat})$"

                    validators.append(
                        f"AfterValidator(lambda v: check_pattern({repr(full_pat)}, v))"
                    )

                if validators:
                    type_str = f"Annotated[{type_str}, {', '.join(validators)}]"

            is_optional = not self._is_mandatory(stmt) and not hasattr(stmt, "i_is_key")

        elif stmt.keyword == "leaf-list":
            item_type, constraints = self._get_leaf_type(stmt)

            inner_constraints = []
            for k in ["ge", "le", "gt", "lt"]:
                if k in constraints:
                    val = constraints.pop(k)
                    inner_constraints.append(f"Field({k}={val})")

            if inner_constraints:
                item_type = f"Annotated[{item_type}, {', '.join(inner_constraints)}]"

            if "_patterns" in constraints:
                patterns = constraints.pop("_patterns")
                validators = []
                for p in patterns:
                    py_pat = self._convert_yang_regex(p)
                    full_pat = f"^(?:{py_pat})$"
                    validators.append(
                        f"AfterValidator(lambda v: check_pattern({repr(full_pat)}, v))"
                    )

                if validators:
                    item_type = f"Annotated[{item_type}, {', '.join(validators)}]"

            type_str = f"RestconfList[{item_type}]"
            is_optional = not self._is_mandatory(stmt)

        elif stmt.keyword in ["anydata", "anyxml"]:
            type_str = "Any"
            is_optional = True
        else:
            return None

        if is_choice:
            is_optional = True

        field_def = f"{type_str} | None" if is_optional else type_str

        default_val = self._get_default_value(stmt)

        full_description = self._build_field_description(stmt)
        if full_description:
            field_params.append(f"description={repr(full_description)}")

        for key, val in constraints.items():
            field_params.append(f"{key}={val}")

        if default_val is not None:
            if is_optional and default_val == "None":
                field_params.append("default=None")
            else:
                field_params.append(f"default={default_val}")
        elif is_optional:
            field_params.append("default=None")

        module_name = self._get_module_name(stmt)
        parent_module = self._get_module_name(parent_stmt)
        is_root = parent_stmt.keyword in ("module", "submodule")
        yang_name = stmt.arg
        if is_root or module_name != parent_module:
            field_params.append(f'alias="{module_name}:{yang_name}"')
        elif field_name != yang_name:
            field_params.append(f'alias="{yang_name}"')

        if field_params:
            return f"    {field_name}: {field_def} = Field({', '.join(field_params)})"

        if is_optional or default_val is not None:
            rhs = default_val if default_val else "None"
            return f"    {field_name}: {field_def} = {rhs}"

        return f"    {field_name}: {field_def}"

    def _build_field_description(self, stmt) -> str:
        """
        Combines the base description with 'when' and 'must' constraints.
        """
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

        return "\n".join(parts).strip()

    def _combine_yang_patterns(self, patterns) -> str:
        """
        Combines multiple YANG patterns into a single Python regex.
        RFC 7950: Multiple patterns = Logical AND (all must match).
        """
        if not patterns:
            return ""

        cleaned = []
        for p_stmt in patterns:
            p = p_stmt.arg

            anchored = f"^(?:{p})$"

            modifier = p_stmt.search_one("modifier")
            if modifier and modifier.arg == "invert-match":
                cleaned.append(f"(?!(?:{anchored}))")
            else:
                cleaned.append(f"(?=(?:{anchored}))")

        if len(cleaned) == 1 and not patterns[0].search_one("modifier"):
            return f"^(?:{patterns[0].arg})$"

        return f"{''.join(cleaned)}.*"

    def _get_leaf_type(self, stmt) -> tuple[str, dict[str, Any]]:
        """Get Pydantic type for a leaf/leaf-list"""
        type_stmt = stmt.search_one("type")
        if not type_stmt:
            return "str", {}

        constraints = {}
        yang_type = type_stmt.arg

        if yang_type in ["int8", "int16", "int32"]:
            constraints = self._get_range_constraints(type_stmt)
            return "int", constraints
        elif yang_type == "int64":
            constraints = self._get_range_constraints(type_stmt)
            constraints.setdefault("ge", -9223372036854775808)
            constraints.setdefault("le", 9223372036854775807)
            return "Int64", constraints
        elif yang_type in ["uint8", "uint16", "uint32"]:
            constraints = self._get_range_constraints(type_stmt)
            if "ge" not in constraints:
                constraints["ge"] = 0
            return "int", constraints
        elif yang_type == "uint64":
            constraints = self._get_range_constraints(type_stmt)
            constraints.setdefault("ge", 0)
            constraints.setdefault("le", 18446744073709551615)
            return "Uint64", constraints
        elif yang_type == "decimal64":
            constraints = self._get_range_constraints(type_stmt)
            return "Decimal64", constraints

        elif yang_type == "string":
            constraints = {}

            length = type_stmt.search_one("length")
            if length:
                match = re.search(r"(\d+)\.\.(\d+)", length.arg)
                if match:
                    constraints["min_length"] = int(match.group(1))
                    constraints["max_length"] = int(match.group(2))
                elif length.arg.isdigit():
                    constraints["min_length"] = int(length.arg)
                    constraints["max_length"] = int(length.arg)

            pattern_stmts = type_stmt.search("pattern")
            if pattern_stmts:
                constraints["_patterns"] = [p.arg for p in pattern_stmts]

            return "str", constraints

        elif yang_type == "boolean":
            return "bool", {}

        elif yang_type == "binary":
            return "str", {}

        elif yang_type == "enumeration":
            suggested_name = self._to_class_name(stmt.arg) + "Enum"
            enum_values = [e.arg for e in type_stmt.search("enum")]
            enum_name = self._generate_enum_class(
                suggested_name, enum_values, type_stmt
            )
            return enum_name, {}

        elif yang_type == "bits":
            return "str", {}

        elif yang_type == "empty":
            return "bool", {}

        elif yang_type == "leafref":
            path_stmt = type_stmt.search_one("path")
            if path_stmt and hasattr(type_stmt, "i_type_spec"):
                target = getattr(type_stmt.i_type_spec, "i_target_node", None)
                if target:
                    return self._get_leaf_type(target)
            return "str", {}

        elif yang_type == "identityref":
            return "str", {}

        elif yang_type == "instance-identifier":
            return "str", {}

        elif yang_type == "union":
            types = []
            for sub_type in type_stmt.search("type"):
                sub_type_str, _ = self._get_type_from_stmt(sub_type)
                types.append(sub_type_str)
            if types:
                return f"{' | '.join(types)}", {}
            return "str", {}

        elif hasattr(type_stmt, "i_typedef") and type_stmt.i_typedef:
            typedef = type_stmt.i_typedef
            return self._get_leaf_type(typedef)

        return "str", {}

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

    def _get_type_from_stmt(self, type_stmt) -> tuple[str, list[str]]:
        """Helper to get type from a type statement directly"""
        yang_type = type_stmt.arg

        if yang_type in ["int8", "int16", "int32", "int64"]:
            return "int", []
        elif yang_type in ["uint8", "uint16", "uint32", "uint64"]:
            return "int", []

        elif yang_type == "decimal64":
            return "float", []

        elif yang_type == "string":
            return "str", []

        elif yang_type == "boolean":
            return "bool", []

        elif yang_type == "binary":
            return "str", []

        elif yang_type == "enumeration":
            return "str", []

        elif yang_type == "bits":
            return "str", []

        elif yang_type == "empty":
            return "bool", []

        elif yang_type == "leafref":
            return "str", []

        elif yang_type == "identityref":
            return "str", []

        elif yang_type == "instance-identifier":
            return "str", []

        elif hasattr(type_stmt, "i_typedef") and type_stmt.i_typedef:
            typedef = type_stmt.i_typedef
            typedef_type = typedef.search_one("type")
            if typedef_type:
                return self._get_type_from_stmt(typedef_type)

        return "str", []

    def _get_enum_fingerprint(self, type_stmt) -> str:
        """
        Creates a unique hash based on enum labels and values.
        Descriptions are ignored to ensure structural identity wins.
        """
        enums = type_stmt.search("enum")
        # Sort by label to ensure deterministic fingerprint
        data_parts = []
        for e in sorted(enums, key=lambda x: x.arg):
            val_stmt = e.search_one("value")
            val = val_stmt.arg if val_stmt else ""
            data_parts.append(f"{e.arg}:{val}")

        fp = "|".join(data_parts)
        return hashlib.md5(fp.encode()).hexdigest()

    def _generate_enum_class(self, enum_name, values, type_stmt) -> str:
        """
        Generate an Enum class or return an existing one from the registry.
        Returns the final class name.
        """
        fingerprint = self._get_enum_fingerprint(type_stmt)

        if fingerprint in self.enum_registry:
            return self.enum_registry[fingerprint]

        # Handle potential name collisions for DIFFERENT enums sharing the same leaf name
        actual_name = enum_name
        counter = 1
        existing_names = set(self.enum_registry.values())
        while actual_name in existing_names:
            actual_name = f"{enum_name}_{counter}"
            counter += 1

        self.enum_registry[fingerprint] = actual_name

        lines = []
        lines.append(f"class {actual_name}(str, Enum):")

        doc_lines = []
        desc_stmt = type_stmt.search_one("description")
        if desc_stmt:
            doc_lines.append(self._escape_docstring(desc_stmt.arg))
        else:
            doc_lines.append(f"Enumeration for {enum_name}")

        doc_lines.append("")
        doc_lines.append("Values:")
        for enum_stmt in type_stmt.search("enum"):
            val_desc = enum_stmt.search_one("description")
            if val_desc:
                doc_lines.append(
                    f"  * {enum_stmt.arg}: {self._escape_string(val_desc.arg)}"
                )
            else:
                doc_lines.append(f"  * {enum_stmt.arg}")

        lines.append('    """' + "\n    ".join(doc_lines) + '\n    """\n')

        for value in values:
            py_name = self._to_enum_name(value)
            lines.append(f'    {py_name} = "{value}"')

        self.classes.append("\n".join(lines))
        return actual_name

    def _is_mandatory(self, stmt) -> bool:
        """Check if a node is mandatory"""
        if stmt.search_one("when") or stmt.search("must"):
            return False
        if stmt.keyword in ["leaf", "choice"]:
            m = stmt.search_one("mandatory")
            return m and m.arg == "true"
        elif stmt.keyword in ["list", "leaf-list"]:
            min_els = stmt.search_one("min-elements")
            return min_els and int(min_els.arg) > 0
        elif stmt.keyword == "container":
            return False
        return False

    def _get_default_value(self, stmt) -> str:
        """Get default value for a field"""
        default = stmt.search_one("default")
        if not default:
            return None

        type_stmt = stmt.search_one("type")
        if type_stmt:
            # We must resolve the "base" type in case this is a typedef of a typedef
            base_type_stmt = type_stmt
            while (
                base_type_stmt.arg != "enumeration"
                and hasattr(base_type_stmt, "i_typedef")
                and base_type_stmt.i_typedef
            ):
                base_type_stmt = base_type_stmt.i_typedef.search_one("type")

            yang_type = base_type_stmt.arg

            if yang_type == "boolean":
                return default.arg.title()
            elif yang_type in [
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
            elif yang_type == "enumeration":
                fingerprint = self._get_enum_fingerprint(base_type_stmt)
                actual_enum_class_name = self.enum_registry.get(fingerprint)
                if actual_enum_class_name:
                    py_enum_member = self._to_enum_name(default.arg)
                    return f"{actual_enum_class_name}.{py_enum_member}"
                # Fallback: if for some reason the registry is empty (shouldn't happen)
                return repr(default.arg)

        return repr(default.arg)

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

        text = text.replace('"""', r"\"\"\"")
        return text

    def _escape_string(self, text: str) -> str:
        """Escape text for use in string"""
        text = text.replace("\\", "\\\\")
        text = text.replace('"', '\\"')
        text = text.replace("\n", " ")
        return text


class ClientGenerator:
    """Orchestrates the generation of client navigator code"""

    def __init__(self, ctx, modules, root_data_nodes, root_rpc_nodes):
        self.ctx = ctx
        self.modules = modules
        self.root_data_nodes = root_data_nodes  # [(mod, cls, alias, field), ...]
        self.root_rpc_nodes = root_rpc_nodes

    def generate(self, output_dir):
        generated_modules = []

        for module in self.modules:
            converter = NavigatorConverter(self.ctx, module)
            code = converter.generate()
            if code:
                module_py_name = module.arg.replace("-", "_")
                generated_modules.append(module_py_name)
                with open(os.path.join(output_dir, f"{module_py_name}.py"), "w") as f:
                    f.write(code)

        self._generate_init(output_dir, generated_modules)

    def _generate_init(self, output_dir, generated_modules):
        lines = []
        lines.append("from __future__ import annotations")
        lines.append("from ._base import Node")
        lines.append("from typing import TYPE_CHECKING")
        lines.append("")
        lines.append("if TYPE_CHECKING:")
        for mod in generated_modules:
            lines.append(f"    from . import {mod}")

        lines.append("\n\nclass Data(Node):")
        lines.append('    """Navigator for data nodes."""\n')

        # --- LOOP 2: DATA PROPERTIES ---
        for mod, cls, alias, field in self.root_data_nodes:
            node_type = self._find_node_type(mod, field)

            # Logic here must match the imports above
            if node_type == "list":
                suffix = "ListNode"
            else:
                suffix = "Node"

            prop_name = f"{mod}_{field}"
            cls_ref = f"{mod}.{cls}"

            lines.append("    @property")
            lines.append(f"    def {prop_name}(self) -> {cls_ref}{suffix}:")
            if suffix == "ListNode":
                lines.append(f"        from .{mod} import {cls}{suffix}, {cls}ItemNode")
                lines.append(
                    f'        return {cls}{suffix}(self._client, f"{{self._path}}/{alias}", "{alias}", {cls}ItemNode)\n'
                )
            else:
                lines.append(f"        from .{mod} import {cls}{suffix}")
                lines.append(
                    f'        return {cls}{suffix}(self._client, f"{{self._path}}/{alias}", "{alias}")\n'
                )

        lines.append("\nclass Operations(Node):")
        lines.append('    """Navigator for RPC operations."""\n')

        # --- LOOP 3: RPC PROPERTIES ---
        for mod, cls, alias, field in self.root_rpc_nodes:
            prop_name = f"{mod}_{field}"
            cls_ref = f"{mod}.{cls}"
            lines.append("    @property")
            lines.append(f"    def {prop_name}(self) -> {cls_ref}Node:")
            lines.append(f"        from .{mod} import {cls}Node")
            lines.append(
                f'        return {cls}Node(self._client, f"{{self._path}}/{alias}", "{alias}")\n'
            )

        with open(os.path.join(output_dir, "__init__.py"), "w") as f:
            f.write("\n".join(lines))

    def _find_node_type(self, module_name, field_name):
        for mod in self.modules:
            if mod.arg.replace("-", "_") == module_name:
                for child in mod.i_children:
                    if child.arg.replace("-", "_") == field_name:
                        return child.keyword
        return "container"


class NavigatorConverter:
    """Generates navigator class for a single module"""

    def __init__(self, ctx, module):
        self.ctx = ctx
        self.module = module
        self.generated_classes = set()

    def generate(self) -> str:
        has_nodes = False
        for child in self.module.i_children:
            if child.keyword in ["container", "list", "rpc"]:
                has_nodes = True
                break

        if not has_nodes:
            return ""

        module_py_name = self.module.arg.replace("-", "_")

        lines = []
        lines.append("from __future__ import annotations")
        lines.append("")
        lines.append("from typing import TYPE_CHECKING, Any")
        lines.append("")
        lines.append("from ._base import Node, ListNode, ItemNode")
        lines.append("")
        lines.append("if TYPE_CHECKING:")
        lines.append(f"    from ..data_models import {module_py_name}")
        lines.append("")

        def process_node(stmt):
            if hasattr(stmt, "i_children"):
                for child in stmt.i_children:
                    process_node(child)

            if stmt.keyword == "container":
                self._generate_container(stmt, lines)
            elif stmt.keyword == "list":
                self._generate_list(stmt, lines)
            elif stmt.keyword == "rpc":
                self._generate_rpc(stmt, lines)

        process_node(self.module)

        return "\n".join(lines)

    def _generate_container(self, stmt, lines):
        class_name = getattr(
            stmt, "_pydantic_class_name", self._to_class_name(stmt.arg)
        )

        if class_name in self.generated_classes:
            return
        self.generated_classes.add(class_name)

        pydantic_module = self.module.arg.replace("-", "_")
        pydantic_class = f"{pydantic_module}.{class_name}"

        lines.append(f"class {class_name}Node(Node):")
        lines.append(f'    """Navigator for {stmt.arg}"""')
        lines.append("")

        lines.append(
            f'    def retrieve(self, *, content: str = "all", with_defaults: str = "report-all", depth: int | str = 2, fields: list[str] | None = None) -> {pydantic_class}:'
        )
        lines.append(
            f"        from ..data_models.{pydantic_module} import {class_name}"
        )
        lines.append(
            "        resp = self._retrieve(content=content, with_defaults=with_defaults, depth=depth, fields=fields)"
        )
        lines.append(f"        return {class_name}.model_validate(resp)")
        lines.append("")

        lines.append(
            f"    def update(self, data: {pydantic_class} | dict | str | None = None, **kwargs: Any) -> None:"
        )
        lines.append(
            f"        from ..data_models.{pydantic_module} import {class_name}"
        )
        lines.append("")
        lines.append("        if data is None and kwargs:")
        lines.append(
            '            data = {k.replace("_", "-"): v for k, v in kwargs.items()}'
        )
        lines.append("")
        lines.append("        if isinstance(data, dict):")
        lines.append(f"            data = {class_name}.model_validate(data)")
        lines.append("        elif isinstance(data, str):")
        lines.append(f"            data = {class_name}.model_validate_json(data)")
        lines.append("")
        lines.append("        if data is None:")
        lines.append(
            '            raise ValueError("No data provided for update. Provide a dict, string, or kwargs.")'
        )
        lines.append("")
        lines.append(
            '        payload = data.model_dump(content="config", exclude_unset=True)'
        )
        lines.append("")
        lines.append("        return self._update(**payload)")
        lines.append("")
        lines.append("")

        lines.append(
            f"    def replace(self, data: {pydantic_class} | dict | str | None = None, **kwargs: Any) -> None:"
        )
        lines.append(
            f"        from ..data_models.{pydantic_module} import {class_name}"
        )
        lines.append("")
        lines.append("        if data is None and kwargs:")
        lines.append(
            '            data = {k.replace("_", "-"): v for k, v in kwargs.items()}'
        )
        lines.append("")
        lines.append("        if isinstance(data, dict):")
        lines.append(f"            data = {class_name}.model_validate(data)")
        lines.append("        elif isinstance(data, str):")
        lines.append(f"            data = {class_name}.model_validate_json(data)")
        lines.append("")
        lines.append("        if data is None:")
        lines.append(
            '            raise ValueError("No data provided for replace. Provide a dict, string, or kwargs.")'
        )
        lines.append("")
        lines.append(
            '        payload = data.model_dump(content="config", exclude_unset=True)'
        )
        lines.append("")
        lines.append("        return self._replace(**payload)")
        lines.append("")

        self._generate_children_props(stmt, lines)
        lines.append("")

    def _generate_list(self, stmt, lines):
        item_class_name = getattr(
            stmt, "_pydantic_class_name", self._to_class_name(stmt.arg) + "Item"
        )

        if item_class_name in self.generated_classes:
            return
        self.generated_classes.add(item_class_name)

        pydantic_module = self.module.arg.replace("-", "_")

        pydantic_class = f"{pydantic_module}.{item_class_name}"

        if item_class_name.endswith("Item"):
            list_node_name = item_class_name[:-4]  # Strip last 4 chars 'Item'
        else:
            list_node_name = item_class_name  # Fallback

        list_node_name += "List"  # e.g. SystemInterfaceList

        lines.append(f"class {item_class_name}Node(ItemNode):")
        lines.append(f'    """Navigator for list item {stmt.arg}"""')
        lines.append("")

        lines.append(
            f'    def retrieve(self, *, content: str = "all", with_defaults: str = "report-all", depth: int | str = 2, fields: list[str] | None = None) -> {pydantic_class}:'
        )
        lines.append(
            f"        from ..data_models.{pydantic_module} import {item_class_name}"
        )
        lines.append(
            "        resp = self._retrieve(content=content, with_defaults=with_defaults, depth=depth, fields=fields)"
        )
        lines.append(f"        return {item_class_name}.model_validate(resp)")
        lines.append("")

        lines.append(
            f"    def update(self, data: {pydantic_class} | dict | str | None = None, **kwargs: Any) -> None:"
        )
        lines.append(
            f"        from ..data_models.{pydantic_module} import {item_class_name}"
        )
        lines.append("")
        lines.append("        if data is None and kwargs:")
        lines.append(
            '            data = {k.replace("_", "-"): v for k, v in kwargs.items()}'
        )
        lines.append("")
        lines.append("        if isinstance(data, dict):")
        lines.append(f"            data = {item_class_name}.model_validate(data)")
        lines.append("        elif isinstance(data, str):")
        lines.append(f"            data = {item_class_name}.model_validate_json(data)")
        lines.append("")
        lines.append("        if data is None:")
        lines.append(
            '            raise ValueError("No data provided for update. Provide a dict, string, or kwargs.")'
        )
        lines.append("")
        lines.append(
            '        payload = data.model_dump(content="config", exclude_unset=True)'
        )
        lines.append("        return self._update(**payload)")
        lines.append("")

        lines.append(
            f"    def replace(self, data: {pydantic_class} | dict | str | None = None, **kwargs: Any) -> None:"
        )
        lines.append(
            f"        from ..data_models.{pydantic_module} import {item_class_name}"
        )
        lines.append("")
        lines.append("        if data is None and kwargs:")
        lines.append(
            '            data = {k.replace("_", "-"): v for k, v in kwargs.items()}'
        )
        lines.append("")
        lines.append("        if isinstance(data, dict):")
        lines.append(f"            data = {item_class_name}.model_validate(data)")
        lines.append("        elif isinstance(data, str):")
        lines.append(f"            data = {item_class_name}.model_validate_json(data)")
        lines.append("")
        lines.append("        if data is None:")
        lines.append(
            '            raise ValueError("No data provided for replace. Provide a dict, string, or kwargs.")'
        )
        lines.append("")
        lines.append(
            '        payload = data.model_dump(content="config", exclude_unset=True)'
        )
        lines.append("        return self._replace(**payload)")
        lines.append("")

        self._generate_children_props(stmt, lines)
        lines.append("")

        lines.append(f"class {list_node_name}Node(ListNode[{item_class_name}Node]):")
        lines.append(f'    """Navigator for list {stmt.arg}"""')
        lines.append("")
        lines.append(
            f'    def retrieve(self, *, content: str = "all", with_defaults: str = "report-all", depth: int | str = 2, fields: list[str] | None = None) -> list[{pydantic_class}]:'
        )
        lines.append(
            f"        from ..data_models.{pydantic_module} import {item_class_name}"
        )
        lines.append(
            "        resp = self._retrieve(content=content, with_defaults=with_defaults, depth=depth, fields=fields)"
        )
        lines.append(
            f"        return [{item_class_name}.model_validate(item) for item in resp]"
        )
        lines.append("")

        lines.append(f"    def create(self, data: list[{pydantic_class}]) -> None:")
        lines.append(
            '        payload = [x.model_dump(content="config", exclude_unset=True) for x in data]'
        )
        lines.append("        return self._create(payload)")
        lines.append("")

        lines.append(f"    def replace(self, data: list[{pydantic_class}]) -> None:")
        lines.append(
            '        payload = [x.model_dump(content="config", exclude_unset=True) for x in data]'
        )
        lines.append("        return self._replace(payload)")
        lines.append("")

    def _generate_rpc(self, stmt, lines):
        class_name = getattr(
            stmt, "_pydantic_class_name", self._to_class_name(stmt.arg)
        )

        if class_name in self.generated_classes:
            return
        self.generated_classes.add(class_name)

        pydantic_module = self.module.arg.replace("-", "_")

        input_cls_name = f"{class_name}Input"
        input_cls = f"{pydantic_module}.{input_cls_name}"
        output_cls_name = f"{class_name}Output"
        output_cls = f"{pydantic_module}.{output_cls_name}"

        lines.append(f"class {class_name}Node(Node):")
        lines.append(f'    """Navigator for RPC {stmt.arg}"""')
        lines.append("")

        has_input = stmt.search_one("input") is not None
        has_output = stmt.search_one("output") is not None

        if has_input:
            call_args = f"self, input_data: {input_cls} | dict | str | None = None, **kwargs: Any"
        else:
            call_args = "self"

        ret_type = f" -> {output_cls}" if has_output else " -> None"

        lines.append(f"    def __call__({call_args}){ret_type}:")
        
        if has_input or has_output:
            import_parts = [class_name]
            if has_input: import_parts.append(input_cls_name)
            if has_output: import_parts.append(output_cls_name)
            lines.append(
                f"        from ..data_models.{pydantic_module} import {', '.join(import_parts)}"
            )

        if has_input:
            lines.append("        if input_data is None and kwargs:")
            lines.append("            input_data = {k.replace(\"_\", \"-\"): v for k, v in kwargs.items()}")
            lines.append("")
            lines.append("        if isinstance(input_data, dict):")
            lines.append(
                f"            input_data = {input_cls_name}.model_validate(input_data)"
            )
            lines.append("        elif isinstance(input_data, str):")
            lines.append(
                f"            input_data = {input_cls_name}.model_validate_json(input_data)"
            )
            lines.append("")
            lines.append(f"        rpc_data = {class_name}(input=input_data)")
            lines.append(
                '        payload = rpc_data.model_dump(mode="json", exclude_unset=True, by_alias=True)'
            )
            lines.append(
                "        resp = self._client._request('POST', self._path, json=payload)"
            )
        else:
            lines.append("        resp = self._client._request('POST', self._path)")

        if has_output:
            lines.append("")
            lines.append('        if "output" in resp:')
            lines.append('            data = resp.get("output")')
            lines.append(f'        elif "{self.module.arg}:output" in resp:')
            lines.append(f'            data = resp.get("{self.module.arg}:output")')
            lines.append("        else:")
            lines.append("            data = resp")
            lines.append("")
            lines.append(f"        return {output_cls_name}.model_validate(data)")

        lines.append("")

    def _generate_children_props(self, parent_stmt, lines):
        if not hasattr(parent_stmt, "i_children"):
            return

        for child in parent_stmt.i_children:
            if child.keyword == "container":
                self._generate_child_prop(child, lines, "Node")
            elif child.keyword == "list":
                self._generate_child_prop(child, lines, "ListNode")

    def _generate_child_prop(self, stmt, lines, suffix):
        resolved_name = getattr(
            stmt, "_pydantic_class_name", self._to_class_name(stmt.arg)
        )
        prop_name = self._to_field_name(stmt.arg)

        path_name = stmt.arg

        if suffix == "ListNode":
            if resolved_name.endswith("Item"):
                base = resolved_name[:-4]
            else:
                base = resolved_name
            navigator_cls = f"{base}ListNode"
            item_cls = f"{base}ItemNode"
            lines.append("    @property")
            lines.append(f"    def {prop_name}(self) -> {navigator_cls}:")
            lines.append(
                f'        return {navigator_cls}(self._client, f"{{self._path}}/{path_name}", "{stmt.arg}", {item_cls})'
            )
        else:
            navigator_cls = f"{resolved_name}Node"
            lines.append("    @property")
            lines.append(f"    def {prop_name}(self) -> {navigator_cls}:")
            lines.append(
                f'        return {navigator_cls}(self._client, f"{{self._path}}/{path_name}", "{stmt.arg}")'
            )

    def _to_class_name(self, name: str) -> str:
        parts = re.split(r"[-_]", name)
        res = "".join(word.capitalize() for word in parts)
        if keyword.iskeyword(res):
            return res + "_"
        return res

    def _to_field_name(self, name: str) -> str:
        res = name.replace("-", "_")
        if keyword.iskeyword(res):
            res = f"{res}_"
        return res
