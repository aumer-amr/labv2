"""Register the Git-managed Memini filter before WebUI accepts requests."""

import asyncio
import os
from pathlib import Path


async def bootstrap() -> None:
    from open_webui import config  # Runs WebUI's schema migrations before registration.
    from open_webui.models.functions import FunctionForm, FunctionMeta, Functions

    content = Path(__file__).with_name("memini_memory.py").read_text()
    existing = await Functions.get_function_by_id("memini_memory")
    if existing:
        result = await Functions.update_function_by_id(
            "memini_memory", {"content": content, "is_active": True, "is_global": True}
        )
    else:
        result = await Functions.insert_new_function(
            os.environ["MEMINI_SHARED_USER_ID"],
            "filter",
            FunctionForm(
                id="memini_memory", name="Memini Memory", content=content,
                meta=FunctionMeta(description="Persistent memory via Memini"),
            ),
        )
        if result:
            result = await Functions.update_function_by_id(
                "memini_memory", {"is_active": True, "is_global": True}
            )
    if result is None:
        raise RuntimeError("Memini filter registration failed")
    valves = await Functions.get_function_valves_by_id("memini_memory") or {}
    valves.update(
        base_url=os.environ["MEMINI_BASE_URL"], namespace="openwebui",
        scope_by_user=True, recall=True, capture=True, timeout_ms=30000,
    )
    if await Functions.update_function_valves_by_id("memini_memory", valves) is None:
        raise RuntimeError("Memini filter configuration failed")


if __name__ == "__main__":
    asyncio.run(bootstrap())
