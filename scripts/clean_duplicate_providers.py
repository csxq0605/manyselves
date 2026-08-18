"""
清理重复的提供商配置

问题：
1. 有多个重复的 "自定义 Xiaomi MiMo Token Plan (China)" 配置
2. URL 错误：使用 /v1 而不是 /anthropic
3. 应该使用预设而不是自定义配置

解决方案：
1. 删除重复配置
2. 使用正确的预设应用配置
"""

# 临时脚本：清理配置
from manyselves.config.schema import AppConfig
from manyselves.config.manager import ConfigManager
from pathlib import Path

# 查找配置文件
config_path = Path(".manyselves") / "cd95f12f-8c69-429f-ae62-8594aaf4eabb" / "config.yaml"

if config_path.exists():
    print(f"Found config: {config_path}")

    # 加载配置
    manager = ConfigManager(config_path)
    config = manager.config

    print(f"\n当前配置数量: {len(config.providers.configurations)}")
    print("\n提供商列表:")
    for p in config.providers.configurations:
        print(f"  - ID: {p.id}")
        print(f"    Name: {p.name}")
        print(f"    Provider: {p.provider}")
        print(f"    Base URL: {p.api_base}")
        print(f"    Active: {p.id == config.providers.active}")
        print()

    # 清理逻辑：只保留一个 MiMo 配置
    mimo_configs = [p for p in config.providers.configurations if 'mimo' in p.name.lower()]

    if len(mimo_configs) > 1:
        print(f"⚠️  发现 {len(mimo_configs)} 个 MiMo 配置，建议只保留一个")

        # 保留最新的，删除其他的
        to_keep = mimo_configs[-1]  # 保留最后一个
        to_remove = mimo_configs[:-1]  # 删除其他的

        for config_to_remove in to_remove:
            print(f"  删除: {config_to_remove.name} (ID: {config_to_remove.id})")
            config.providers.configurations.remove(config_to_remove)

        # 更新 active
        config.providers.active = to_keep.id

        # 修正 URL（如果错误）
        if to_keep.api_base and '/v1' in to_keep.api_base:
            correct_url = to_keep.api_base.replace('/v1', '/anthropic')
            print(f"  修正 URL: {to_keep.api_base} → {correct_url}")
            to_keep.api_base = correct_url

        # 保存
        manager.save_config()
        print("\n✅ 清理完成")
    else:
        print("✅ 配置正常，无需清理")
else:
    print("❌ 未找到配置文件")