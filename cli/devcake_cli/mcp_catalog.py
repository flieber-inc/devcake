"""The operator MCP tool catalogue, derived from the app's OpenAPI document.

ADR-0041: nothing here names a route. The app describes itself; one tool
per JSON operation is built from that description — the tool name is the
operation id (the route function's name), the description is the route's
docstring, the input schema folds path, query and body parameters into one
object. A route that must never be a tool carries `x-devcake-mcp: never`
at its definition; operations that take or return anything but JSON are
excluded by that fact alone. Pure and dependency-free so the derivation is
testable without the MCP SDK installed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

NEVER_MARKER = "x-devcake-mcp"
JSON = "application/json"
BODY_KEY = "body"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    method: str                      # lower-case HTTP method
    path: str                        # OpenAPI path template
    path_params: tuple[str, ...]
    query_params: tuple[str, ...]
    body: bool
    input_schema: dict = field(default_factory=dict)

    def as_tool(self) -> dict:
        """The MCP `Tool` shape (camelCase key, as the protocol spells it)."""
        return {"name": self.name, "description": self.description,
                "inputSchema": self.input_schema}


def _resolve(node: Any, components: dict, seen: tuple[str, ...] = ()) -> Any:
    """Inline `$ref`s so the input schema stands alone (MCP schemas carry no
    components section); a self-referential schema stops at the cycle."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            name = ref.rsplit("/", 1)[-1]
            if name in seen:
                return {"type": "object", "description": f"(recursive: {name})"}
            target = components.get(name, {})
            return _resolve(target, components, seen + (name,))
        return {k: _resolve(v, components, seen) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve(v, components, seen) for v in node]
    return node


def _json_only(op: dict) -> bool:
    """True when every declared request and 2xx response body is JSON."""
    body = (op.get("requestBody") or {}).get("content") or {}
    if body and JSON not in body:
        return False
    if any(mt != JSON for mt in body):
        return False
    for code, resp in (op.get("responses") or {}).items():
        if not str(code).startswith("2"):
            continue
        content = (resp or {}).get("content") or {}
        if any(mt != JSON for mt in content):
            return False
    return True


def build_tools(doc: dict, *, read_only: bool) -> list[ToolSpec]:
    """One ToolSpec per admitted operation, in document order."""
    components = (doc.get("components") or {}).get("schemas") or {}
    out: list[ToolSpec] = []
    for path, item in (doc.get("paths") or {}).items():
        for method, op in item.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                continue
            if not isinstance(op, dict) or op.get(NEVER_MARKER) == "never":
                continue
            if read_only and method.lower() != "get":
                continue
            if not _json_only(op):
                continue
            name = op.get("operationId")
            if not name:
                continue
            props: dict[str, Any] = {}
            required: list[str] = []
            path_params: list[str] = []
            query_params: list[str] = []
            for prm in op.get("parameters") or []:
                pname, where = prm.get("name"), prm.get("in")
                if not pname or where not in ("path", "query"):
                    continue
                schema = _resolve(prm.get("schema") or {"type": "string"}, components)
                if prm.get("description") and "description" not in schema:
                    schema = {**schema, "description": prm["description"]}
                props[pname] = schema
                (path_params if where == "path" else query_params).append(pname)
                if where == "path" or prm.get("required"):
                    required.append(pname)
            body = op.get("requestBody") or {}
            has_body = bool(body)
            if has_body:
                schema = _resolve(((body.get("content") or {}).get(JSON) or {}).get("schema")
                                  or {"type": "object"}, components)
                props[BODY_KEY] = {**schema, "description": "The JSON request body."} \
                    if "description" not in schema else schema
                if body.get("required"):
                    required.append(BODY_KEY)
            description = (op.get("description") or op.get("summary") or "").strip()
            out.append(ToolSpec(
                name=name, description=description, method=method.lower(), path=path,
                path_params=tuple(path_params), query_params=tuple(query_params),
                body=has_body,
                input_schema={"type": "object", "properties": props,
                              "required": required, "additionalProperties": False}))
    return out


def render_call(tool: ToolSpec, arguments: dict | None) -> tuple[str, str, dict, Any]:
    """(method, concrete path, query dict, body) for one invocation; a
    missing path parameter is a caller error, reported as ValueError."""
    args = dict(arguments or {})
    path = tool.path
    for name in tool.path_params:
        if name not in args:
            raise ValueError(f"missing path parameter {name!r}")
        path = path.replace("{" + name + "}", str(args.pop(name)))
    query = {name: args.pop(name) for name in tool.query_params if name in args}
    body = args.pop(BODY_KEY, None) if tool.body else None
    if args:
        raise ValueError(f"unknown argument(s): {', '.join(sorted(args))}")
    return tool.method, path, query, body
