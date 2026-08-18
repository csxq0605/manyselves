# 简化部署指南（局域网环境）

## 🎯 适用场景

- ✅ 局域网环境
- ✅ 开发/测试环境
- ✅ 单用户或信任环境
- ❌ 公网生产环境（请使用标准版）

---

## 📋 简化内容

### 标准版 vs 简化版对比

| 特性 | 标准版 | 简化版 |
|------|--------|--------|
| 用户权限 | 非 root 用户（`manyselves`） | root 用户 |
| 文件系统 | 只读（`read_only: true`） | 可读写 |
| tmpfs | 严格限制（`noexec,nosuid`） | 无限制 |
| 权限管理 | 复杂（chown, chmod） | 简单（默认权限） |
| 适用环境 | 生产环境 | 局域网/开发 |

---

## 🚀 快速部署

### 1. 设置 API Key

```bash
export MANYSELVES_OPENAI_API_KEY=your-api-key
```

### 2. 一键部署

```bash
./scripts/deploy-simple.sh
```

**脚本会自动：**
- ✅ 初始化配置文件
- ✅ 构建 Docker 镜像
- ✅ 启动服务

### 3. 访问服务

- 主页：http://localhost:9090
- API 文档：http://localhost:9000/docs

---

## 📁 文件结构

### 简化版配置文件

```
deploy/
├── api/
│   └── Dockerfile.simple         # 简化版 Dockerfile（root 用户）
├── compose.simple.yaml            # 简化版 Docker Compose
└── config/
    ├── manyselves.config.example.yaml  # 配置模板
    └── manyselves.config.yaml           # 实际配置（需创建）
```

### 数据目录

```
data/manyselves/           # 数据目录（自动创建）
├── default/               # 默认项目
├── sessions/              # 会话数据
└── projects/              # 其他项目
```

---

## ⚙️ 环境变量

### 必需变量

```bash
# API Key（任选其一）
export MANYSELVES_OPENAI_API_KEY=your-api-key
export MANYSELVES_ANTHROPIC_API_KEY=your-api-key
```

### 可选变量

```bash
# 管理员账号
export MANYSELVES_ADMIN_USERNAME=admin
export MANYSELVES_ADMIN_PASSWORD=your-password

# 网络配置
export MANYSELVES_HTTP_BIND=0.0.0.0
export MANYSELVES_HTTP_PORT=9090
export MANYSELVES_API_PORT=9000

# 数据目录
export MANYSELVES_DATA_DIR=./data/manyselves
```

---

## 🔧 手动部署步骤

如果需要手动控制每个步骤：

### 1. 初始化配置文件

```bash
cd deploy
mkdir -p config
cp config/manyselves.config.example.yaml config/manyselves.config.yaml
```

### 2. 编辑配置文件（可选）

```bash
nano config/manyselves.config.yaml
```

添加 API Key：

```yaml
providers:
  configurations:
    - apiKey: your-api-key-here
```

### 3. 构建镜像

```bash
docker-compose -f compose.simple.yaml build
```

### 4. 启动服务

```bash
docker-compose -f compose.simple.yaml up -d
```

### 5. 查看日志

```bash
docker-compose -f compose.simple.yaml logs -f
```

### 6. 停止服务

```bash
docker-compose -f compose.simple.yaml down
```

---

## 🛠️ 常见问题

### 1. 权限问题

**问题：** `Permission denied`

**解决：**

```bash
# 简化版不应该有权限问题，如果有：
sudo chown -R $USER:$USER data/
```

### 2. 端口冲突

**问题：** `port is already allocated`

**解决：**

```bash
# 修改端口
export MANYSELVES_HTTP_PORT=8080
export MANYSELVES_API_PORT=8000
docker-compose -f compose.simple.yaml up -d
```

### 3. 镜像构建失败

**问题：** 构建时错误

**解决：**

```bash
# 清理并重新构建
docker-compose -f compose.simple.yaml down
docker system prune -f
docker-compose -f compose.simple.yaml build --no-cache
```

### 4. 配置文件找不到

**问题：** `Config file not found`

**解决：**

```bash
# 检查配置文件
ls -la deploy/config/manyselves.config.yaml

# 如果不存在，创建它
cp deploy/config/manyselves.config.example.yaml deploy/config/manyselves.config.yaml
```

---

## 🔄 切换到标准版

如果需要切换到生产环境：

```bash
# 使用标准版配置
docker-compose -f compose.yaml up -d
```

**注意：** 标准版有严格的权限限制，适合公网部署。

---

## 📝 Dockerfile 对比

### 简化版 (Dockerfile.simple)

```dockerfile
# ✅ 使用 root 用户
# ✅ 无严格权限限制
# ✅ 简单易懂

FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install -e .
EXPOSE 9000
CMD ["python", "run_web.py"]
```

### 标准版 (Dockerfile)

```dockerfile
# ❌ 非 root 用户
# ❌ 只读文件系统
# ❌ 严格权限管理

FROM python:3.12-slim
RUN useradd -r manyselves
USER manyselves
COPY --chown=manyselves:manyselves . .
# ... 复杂的权限设置
```

---

## ⚠️ 安全提示

**简化版适用于：**
- ✅ 局域网环境
- ✅ 开发/测试
- ✅ 信任环境

**不适用于：**
- ❌ 公网生产环境
- ❌ 多用户环境
- ❌ 需要严格安全控制的场景

**如果部署到公网，请使用标准版：**

```bash
# 使用标准版（生产环境）
docker-compose -f compose.yaml up -d
```