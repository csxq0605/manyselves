# 配置管理说明

## 配置文件结构

```
deploy/
├── config/
│   ├── manyselves.config.example.yaml  # 模板文件（被 git 追踪）
│   └── manyselves.config.yaml           # 实际配置（包含 API Key，不追踪）
├── compose.yaml
└── api/
```

---

## 快速开始

### 1. 初始化配置文件

```bash
./scripts/init-config.sh
```

这会从模板创建配置文件：`deploy/config/manyselves.config.yaml`

### 2. 添加 API Key

编辑配置文件：

```bash
nano deploy/config/manyselves.config.yaml
```

找到对应的 provider，添加你的 API Key：

```yaml
providers:
  active: mimo-cn
  configurations:
    - apiKey: null  # 通过环境变量或设置页面写入
      apiBase: https://token-plan-cn.xiaomimimo.com/anthropic
      defaultModel: mimo-v2.5-pro
      enabled: true
      id: mimo-cn
      name: Xiaomi MiMo Token Plan (China)
      presetId: anthropic-xiaomi-mimo-token-plan-china
      provider: anthropic
```

`active` 指向保存后的**配置 ID**（上例为 `mimo-cn`），而不是协议名或预设名。

---

## 设置页面的连接配置

设置页面将三个概念分开保存，避免同协议供应商互相覆盖：

- **预设 ID**：只用来填充已知服务商的推荐参数，例如 `anthropic-xiaomi-mimo-token-plan-china`。
- **协议**：请求格式，当前常用 `anthropic` 或 `openai`。
- **配置 ID**：一条持久化配置的唯一标识，也是 `providers.active` 的值。

保存会在一次事务中写入 API 地址、模型、协议、附加请求头、活动配置和 Agent 默认值，并重建运行时。API Key 是只写字段：接口响应和重新打开页面都不会返回它。

“测试连接”只使用当前表单值进行临时请求，不保存配置、不切换活动配置，也不会重建运行时。附加请求头应填写 JSON 对象，例如：

```json
{"X-Client": "manyselves"}
```

旧版或自定义配置不会被自动删除。确认不再使用后，可在设置页面的“自定义或旧配置”区域手动删除。

---

## 部署方式

### 方式 1：源码和镜像在同一台机器（推荐）

**直接使用源码中的配置文件：**

```bash
# 设置环境变量
export MANYSELVES_CONFIG_PATH=/app/deploy/config/manyselves.config.yaml
export MANYSELVES_CONFIG_SOURCE=./config
export MANYSELVES_DATA_DIR=/data/manyselves
export MANYSELVES_OPENAI_API_KEY=your-api-key

# 启动
cd deploy
docker-compose up -d
```

**优点：**
- ✅ 不需要复制配置文件
- ✅ 修改配置后重启容器即可
- ✅ 配置文件被 git 追踪

### 方式 2：分离部署（配置文件独立）

**复制配置文件到数据目录：**

```bash
# 初始化配置
./scripts/init-config.sh

# 复制到数据目录
mkdir -p /data/manyselves
cp deploy/config/manyselves.config.yaml /data/manyselves/

# 启动
cd deploy
docker-compose up -d
```

---

## 环境变量

### 必需变量

```bash
# 数据目录
export MANYSELVES_DATA_DIR=/data/manyselves

# API Key（任选其一）
export MANYSELVES_OPENAI_API_KEY=your-api-key
export MANYSELVES_ANTHROPIC_API_KEY=your-api-key
```

### 可选变量

```bash
# 配置文件路径
export MANYSELVES_CONFIG_PATH=/app/deploy/config/manyselves.config.yaml

# 配置文件源（挂载源码配置）
export MANYSELVES_CONFIG_SOURCE=./config

# 管理员账号
export MANYSELVES_ADMIN_USERNAME=admin
export MANYSELVES_ADMIN_PASSWORD=your-password

# 网络配置
export MANYSELVES_HTTP_BIND=0.0.0.0
export MANYSELVES_HTTP_PORT=9090
export MANYSELVES_ALLOWED_ORIGINS='["http://localhost:9090"]'
```

---

## 配置文件说明

### `manyselves.config.example.yaml`（模板）

- ✅ 被 git 追踪
- ✅ 包含所有支持的 provider 配置
- ✅ 不包含敏感信息（API Key 为 null）

### `manyselves.config.yaml`（实际配置）

- ❌ 不被 git 追踪（包含 API Key）
- ✅ 从模板复制而来
- ✅ 需要手动添加 API Key

---

## 常见问题

### 1. 配置文件找不到

**问题：** `Config file not found`

**解决：**

```bash
# 初始化配置
./scripts/init-config.sh

# 或者手动复制
cp deploy/config/manyselves.config.example.yaml deploy/config/manyselves.config.yaml
```

### 2. API Key 未设置

**问题：** `API key is required`

**解决：**

编辑配置文件：

```bash
nano deploy/config/manyselves.config.yaml
```

添加 API Key：

```yaml
- apiKey: tp-your-api-key-here
```

或使用环境变量：

```bash
export MANYSELVES_OPENAI_API_KEY=your-api-key
```

### 3. 权限问题

**问题：** `Permission denied: /data/manyselves`

**解决：**

```bash
sudo chown -R $USER:$USER /data/manyselves
```

---

## 安全建议

1. **不要提交 API Key 到 git**
   - `manyselves.config.yaml` 已在 `.gitignore` 中
   - 使用环境变量更安全

2. **使用只读挂载**
   - Docker Compose 中配置文件以只读方式挂载
   - 防止容器内进程修改配置

3. **定期轮换 API Key**
   - 使用 API Key 管理服务（如 Vault）
   - 不要长期使用同一个 API Key
