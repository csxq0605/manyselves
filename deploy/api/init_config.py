"""Create a secret-free provider skeleton for a brand-new data root."""

import os
from pathlib import Path

import yaml

# 默认使用 MiMo API（小米 AI 服务）
allowed = {"anthropic", "deepseek", "openai", "openrouter"}
provider = os.environ.get("MANYSELVES_BOOTSTRAP_PROVIDER", "openai").strip().lower()
if provider not in allowed:
    raise SystemExit(f"Unsupported MANYSELVES_BOOTSTRAP_PROVIDER: {provider}")

config_path = Path(os.environ.get("CONFIG_PATH", "/data/manyselves/manyselves.config.yaml"))

# 只在配置文件不存在时创建
if not config_path.exists():
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)

        # Support custom API base and default model via environment variables
        # 默认使用 MiMo API
        api_base = os.environ.get(
            "MANYSELVES_BOOTSTRAP_API_BASE",
            "https://token-plan-cn.xiaomimimo.com/v1"  # 默认 MiMo API
        )
        default_model = os.environ.get(
            "MANYSELVES_BOOTSTRAP_MODEL",
            "mimo-v2.5-pro"  # 默认 MiMo 模型
        )

        document = {
            "providers": {
                "active": f"mimo-{provider}",
                "configurations": [{
                    "apiKey": None,
                    "apiBase": api_base,
                    "defaultModel": default_model,
                    "enabled": True,
                    "id": f"mimo-{provider}",
                    "name": "MiMo AI" if provider == "openai" else f"MiMo {provider.title()}",
                    "provider": provider,
                }],
            },
        }
        temporary = config_path.with_suffix(config_path.suffix + ".tmp")
        temporary.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=True), encoding="utf-8")
        temporary.replace(config_path)
        print(f"Created default config at {config_path}")
    except PermissionError as e:
        # 如果无法创建配置文件，打印友好提示并退出
        print(f"Warning: Cannot create config file {config_path}: {e}")
        print(f"Please create the config file manually before starting the container.")
        print(f"Example config location: deploy/manyselves.config.yaml.example")
        # 不抛出异常，让容器继续启动（如果配置文件已存在会正常工作）
