# ManySelves 部署指南

## 部署架构

```
Windows WSL (开发环境)
    ↓ 构建
[Docker 镜像] + [源码包]
    ↓ 传输
CentOS 服务器 (生产环境)
    ↓ 部署
Podman 容器运行
```

## 前置条件

### 本地环境（Windows WSL）

- Docker Desktop
- WSL 2
- Git

### 服务器环境（CentOS）

- Podman
- podman-compose

## 部署步骤

### 第一步：本地构建（Windows WSL）

```bash
# 进入项目目录
cd /mnt/d/yuanxi-algo/manyselves

# 执行构建脚本
chmod +x scripts/build-local.sh
./scripts/build-local.sh
```

构建完成后生成：
- `manyselves-images.tar` - Docker 镜像（约 800MB）
- `manyselves-deploy.tar.gz` - 源码和配置（约 5MB）

### 第二步：传输到服务器

```bash
# 传输文件到服务器
scp manyselves-images.tar manyselves-deploy.tar.gz \
  algo_001@192.168.8.28:/home/algo_001/
```

### 第三步：服务器部署（CentOS）

```bash
# SSH 到服务器
ssh algo_001@192.168.8.28

# 运行部署脚本
chmod +x manyselves-deploy.tar.gz
# 解压并运行
mkdir -p ~/manyselves
tar -xzf manyselves-deploy.tar.gz -C ~/manyselves --strip-components=0
cd ~/manyselves
chmod +x scripts/deploy-server.sh
./scripts/deploy-server.sh
```

首次运行会提示编辑 `.env` 文件：

```bash
# 编辑配置文件
vi ~/manyselves/deploy/.env

# 修改以下配置：
MANYSELVES_OPENAI_API_KEY=tp-你的实际密钥
MANYSELVES_ALLOWED_ORIGINS=["http://你的服务器IP:9090"]
```

修改后再次运行部署脚本：

```bash
cd ~/manyselves
./scripts/deploy-server.sh
```

### 第四步：验证部署

访问：`http://192.168.8.28:9090`

检查日志：

```bash
# 查看实时日志
podman logs -f manyselves-phase1_api_1

# 应该看到：
# Sending openai request: model=mimo-v2.5-pro
```

## 配置说明

### 环境变量（deploy/.env）

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| `MANYSELVES_DATA_DIR` | 数据目录 | `/data/manyselves` |
| `MANYSELVES_OPENAI_API_KEY` | MiMo API Key | 必填 |
| `MANYSELVES_ADMIN_USERNAME` | 管理员用户名 | `admin` |
| `MANYSELVES_ADMIN_PASSWORD` | 管理员密码 | `yuanxi@2026` |
| `MANYSELVES_ALLOWED_ORIGINS` | 允许的前端源 | `["http://192.168.8.28:9090"]` |
| `MANYSELVES_BOOTSTRAP_API_BASE` | API 地址 | `https://token-plan-cn.xiaomimimo.com/v1` |
| `MANYSELVES_BOOTSTRAP_MODEL` | 默认模型 | `mimo-v2.5-pro` |

### 数据目录权限

```bash
# 数据目录权限（宽松模式）
chmod -R 777 /data/manyselves
```

## 常用命令

### 容器管理

```bash
# 查看容器状态
podman ps

# 查看日志
podman logs -f manyselves-phase1_api_1
podman logs -f manyselves-phase1_web_1

# 重启服务
cd ~/manyselves
podman compose -f deploy/compose.yaml restart

# 停止服务
podman compose -f deploy/compose.yaml down

# 启动服务
podman compose -f deploy/compose.yaml --env-file deploy/.env up -d
```

### 配置管理

```bash
# 查看配置文件
cat /data/manyselves/manyselves.config.yaml

# 编辑配置文件
vi /data/manyselves/manyselves.config.yaml

# 修改后重启服务
podman compose -f deploy/compose.yaml restart
```

## 故障排查

### 问题 1：权限错误

```bash
# 检查数据目录权限
ls -la /data/manyselves/

# 修复权限
chmod -R 777 /data/manyselves
```

### 问题 2：模型配置未生效

```bash
# 检查日志中的模型
podman logs manyselves-phase1_api_1 | grep "Sending openai request"

# 应该显示：model=mimo-v2.5-pro
# 如果显示：model=gpt-4o，说明配置未生效
```

### 问题 3：容器无法启动

```bash
# 查看详细日志
podman logs manyselves-phase1_api_1 2>&1 | tail -50

# 检查环境变量
podman exec manyselves-phase1_api_1 env | grep MANYSELVES
```

## 更新部署

### 更新代码

1. 本地重新构建：

```bash
./scripts/build-local.sh
```

2. 传输到服务器：

```bash
scp manyselves-images.tar manyselves-deploy.tar.gz \
  algo_001@192.168.8.28:/home/algo_001/
```

3. 服务器重新部署：

```bash
cd ~/manyselves
./scripts/deploy-server.sh
```

### 只更新配置

```bash
# 编辑配置文件
vi /data/manyselves/manyselves.config.yaml

# 重启服务
podman compose -f deploy/compose.yaml restart
```

## 备份与恢复

### 备份数据

```bash
# 备份配置和数据
tar -czf manyselves-backup-$(date +%Y%m%d).tar.gz \
  /data/manyselves/ \
  ~/manyselves/deploy/.env
```

### 恢复数据

```bash
# 恢复备份
tar -xzf manyselves-backup-YYYYMMDD.tar.gz -C /

# 重启服务
podman compose -f deploy/compose.yaml restart
```

## 安全建议

1. **API Key 管理**
   - 不要在代码中硬编码 API Key
   - 使用 `.env` 文件管理敏感信息
   - 设置文件权限为 `600`

2. **网络安全**
   - 配置防火墙规则
   - 使用 HTTPS（建议配置反向代理）
   - 限制管理后台访问 IP

3. **定期更新**
   - 定期更新镜像
   - 定期备份数据
   - 定期更换 API Key

## 技术支持

- 项目地址：`d:\yuanxi-algo\manyselves`
- 默认管理员：`admin` / `yuanxi@2026`
- 默认模型：`mimo-v2.5-pro`（MiMo API）