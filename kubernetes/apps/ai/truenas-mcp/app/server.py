"""Read-only MCP adapter for the TrueNAS SCALE 24.04 REST API."""

import http.client
import json
import os
import ssl
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field

RESOURCES = {
    "system": ("system/info", ("version", "hostname", "uptime", "cores", "physmem", "loadavg")),
    "pools": ("pool", ("id", "name", "status", "healthy", "size", "allocated", "free")),
    "datasets": ("pool/dataset", ("id", "name", "type", "used", "available", "compression", "quota")),
    "smb_shares": ("sharing/smb", ("id", "name", "path", "enabled", "ro")),
    "nfs_shares": ("sharing/nfs", ("id", "paths", "enabled", "ro")),
    "services": ("service", ("id", "service", "state", "enable")),
    "alerts": ("alert/list", ("uuid", "level", "formatted", "datetime", "dismissed")),
}
Resource = Literal["system", "pools", "datasets", "smb_shares", "nfs_shares", "services", "alerts"]
mcp = MCPServer("TrueNAS read-only", log_level="WARNING")


def query(resource: Resource, limit: int, offset: int) -> dict:
    endpoint, fields = RESOURCES[resource]
    context = ssl.create_default_context(cafile=os.environ["TRUENAS_CA_FILE"])
    # Trust only the captured self-signed leaf; its SAN is localhost, not the NAS IP.
    context.check_hostname = False
    connection = http.client.HTTPSConnection("10.10.1.3", timeout=20, context=context)
    try:
        connection.request(
            "GET",
            f"/api/v2.0/{endpoint}",
            headers={"Authorization": "Bearer " + os.environ["TRUENAS_API_KEY"]},
        )
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError(f"TrueNAS returned HTTP {response.status}")
        body = response.read(4 * 1024 * 1024 + 1)
        if len(body) > 4 * 1024 * 1024:
            raise RuntimeError("TrueNAS response exceeds 4 MiB")
        data = json.loads(body)
        rows = data if isinstance(data, list) else [data]
        return {
            "items": [{field: row[field] for field in fields if field in row} for row in rows[offset : offset + limit]],
            "total": len(rows),
        }
    except (OSError, ValueError, http.client.HTTPException):
        # API response bodies and credentials must never appear in MCP errors.
        raise RuntimeError("TrueNAS request failed; check TLS, connectivity, and API credentials") from None
    finally:
        connection.close()


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
def truenas_query(
    resource: Resource,
    limit: Annotated[int, Field(ge=1, le=100)] = 50,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> dict:
    """Read system info, pools, datasets, SMB/NFS shares, services, or alerts. Results are paginated and omit unselected configuration fields."""
    return query(resource, limit, offset)


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=8080,
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=[f"{host}:*" for host in ("truenas-mcp", "truenas-mcp.ai", "truenas-mcp.ai.svc", "truenas-mcp.ai.svc.cluster.local")],
        ),
    )
