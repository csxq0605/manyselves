"""Create a secret-free provider skeleton for a brand-new data root."""

import os
from pathlib import Path

import yaml

allowed = {"anthropic", "deepseek", "openai", "openrouter"}
provider = os.environ.get("MANYSELVES_BOOTSTRAP_PROVIDER", "openai").strip().lower()
if provider not in allowed:
    raise SystemExit(f"Unsupported MANYSELVES_BOOTSTRAP_PROVIDER: {provider}")

config_path = Path(os.environ.get("CONFIG_PATH", "/data/manyselves/manyselves.config.yaml"))
if not config_path.exists():
    config_path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "providers": {
            "active": f"{provider}-environment",
            "configurations": [{
                "apiKey": None,
                "enabled": True,
                "id": f"{provider}-environment",
                "name": f"{provider.title()} Environment",
                "provider": provider,
            }],
        },
    }
    temporary = config_path.with_suffix(config_path.suffix + ".tmp")
    temporary.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=True), encoding="utf-8")
    temporary.replace(config_path)
