"""Record the pinned SDK's stateless-era request and handler schemas."""

import json
from pathlib import Path

from mcp.types import methods
from mcp_validation_schema import collect, source_versions


def generate():
    version = "2026-07-28"
    served = ("server/discover", "subscriptions/listen", "tools/list", "tools/call",
              "prompts/list", "prompts/get", "resources/list", "resources/templates/list", "resources/read")
    return {"source": source_versions(), "version": version,
            "spec_methods": sorted(methods.SPEC_CLIENT_METHODS),
            "requests": collect({name: model for (name, v), model in methods.CLIENT_REQUESTS.items() if v == version}),
            "handlers": collect({name: methods.MONOLITH_REQUESTS[name] for name in served})}


if __name__ == "__main__":
    target = Path(__file__).resolve().parents[1] / "src/mcp/modern-schema.json"
    target.write_text(json.dumps(generate(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
