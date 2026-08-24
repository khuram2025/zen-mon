#!/usr/bin/env python3
"""Compose clone-resolution deploy files from the live baseline plus scoped code."""

from pathlib import Path
import sys


def replace_block(base: str, source: str, start: str, end: str) -> str:
    base_start = base.index(start)
    base_end = base.index(end, base_start)
    source_start = source.index(start)
    source_end = source.index(end, source_start)
    return base[:base_start] + source[source_start:source_end] + base[base_end:]


def main() -> None:
    if len(sys.argv) != 8:
        raise SystemExit(
            "usage: make-clone-resolution-deploy-sources.py "
            "BASE_AGENTS FULL_AGENTS BASE_SERVERS FULL_SERVERS "
            "BASE_SCHEMA FULL_SCHEMA OUT_DIR"
        )

    (
        base_agents,
        full_agents,
        base_servers,
        full_servers,
        base_schema,
        full_schema,
        output_dir,
    ) = (
        Path(value) for value in sys.argv[1:]
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    agents_base = base_agents.read_text(encoding="utf-8")
    agents_source = full_agents.read_text(encoding="utf-8")
    agents = replace_block(
        agents_base,
        agents_source,
        "async def _enroll_pending_agent(\n",
        '@router.post("/enroll", response_model=AgentEnrollResponse)\n',
    )
    (output_dir / "agents.py").write_text(agents, encoding="utf-8")

    servers_base = base_servers.read_text(encoding="utf-8")
    servers_source = full_servers.read_text(encoding="utf-8")
    servers = servers_base.replace("from uuid import UUID\n", "from uuid import UUID, uuid4\n", 1)
    servers = replace_block(
        servers,
        servers_source,
        '@fleet_router.post("/{agent_id}/resolve-registration-conflict")\n',
        '@fleet_router.post("/{agent_id}/revoke")\n',
    )
    (output_dir / "servers.py").write_text(servers, encoding="utf-8")

    schema = base_schema.read_text(encoding="utf-8")
    schema_source = full_schema.read_text(encoding="utf-8")
    schema = replace_block(
        schema,
        schema_source,
        "class AgentRegistrationConflictResolve(BaseModel):\n",
        "class AgentEnrollRequest(BaseModel):\n",
    )
    schema = schema.replace(
        "    api_key: Optional[str] = None\n",
        "    api_key: Optional[str] = None\n    assigned_agent_uid: Optional[str] = None\n",
        1,
    )
    (output_dir / "agent.py").write_text(schema, encoding="utf-8")


if __name__ == "__main__":
    main()
