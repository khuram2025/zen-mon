"""Isolated synchronous SDK worker. Secrets arrive via stdin, never argv or disk."""
import json
import sys
from dataclasses import asdict

from .common import ConnectionConfig, ConnectorError


def main():
    try:
        config = ConnectionConfig(**json.load(sys.stdin))
        if config.provider == "vmware":
            from .vmware import collect
        elif config.provider == "hyperv":
            from .hyperv import collect
        else:
            raise ConnectorError("Unsupported SDK collector")
        result = {"snapshot": asdict(collect(config))}
    except Exception as exc:
        result = {"error": str(exc) if isinstance(exc, ConnectorError) else "SDK collector failed"}
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main()
