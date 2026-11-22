"""Generate a tag list by browsing an OPC UA server."""

from __future__ import annotations

from typing import Any

from .sources.opcua import _VARIANT_TO_DATATYPE


async def browse_opcua(
    endpoint: str,
    root: str = "i=85",
    max_depth: int = 6,
    username: str | None = None,
    password: str | None = None,
    include_ns0: bool = False,
) -> list[dict[str, Any]]:
    """Return tag dicts for every Variable under ``root`` (Objects folder by default)."""
    from asyncua import Client, ua

    client = Client(endpoint)
    if username:
        client.set_user(username)
    if password:
        client.set_password(password)
    tags: list[dict[str, Any]] = []
    seen: set[str] = set()
    async with client:

        async def walk(node: Any, path: list[str], depth: int) -> None:
            if depth > max_depth:
                return
            for child in await node.get_children(refs=ua.ObjectIds.HierarchicalReferences):
                node_id = child.nodeid.to_string()
                if node_id in seen:
                    continue
                seen.add(node_id)
                if child.nodeid.NamespaceIndex == 0 and not include_ns0:
                    continue
                name = (await child.read_browse_name()).Name
                node_class = await child.read_node_class()
                if node_class == ua.NodeClass.Variable:
                    try:
                        vtype = await child.read_data_type_as_variant_type()
                        datatype = _VARIANT_TO_DATATYPE.get(vtype.name, "auto")
                    except Exception:
                        datatype = "auto"
                    access = await child.read_attribute(ua.AttributeIds.UserAccessLevel)
                    level = access.Value.Value or 0
                    tag: dict[str, Any] = {
                        "name": "/".join([*path, name]),
                        "node": node_id,
                        "datatype": datatype,
                    }
                    if level & ua.AccessLevel.CurrentWrite.mask:
                        tag["writable"] = True
                    tags.append(tag)
                elif node_class == ua.NodeClass.Object:
                    await walk(child, [*path, name], depth + 1)

        await walk(client.get_node(root), [], 0)
    return tags
