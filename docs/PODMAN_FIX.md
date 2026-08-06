# Podman 镜像命名问题解决

## 🔍 问题分析

### Podman vs Docker 镜像命名差异

| 工具 | 镜像名称 |
|------|---------|
| **Docker** | `manyselves-api:phase1` |
| **Podman** | `localhost/manyselves-api:phase1` |

**原因：** Podman 默认将没有仓库前缀的镜像视为本地镜像，自动添加 `localhost/` 前缀。

---

## ✅ 解决方案

### 1. 修改 Docker Compose 镜像名称

**之前：**

```yaml
services:
  api:
    image: manyselves-api:phase1
```

**现在：**

```yaml
services:
  api:
    image: docker.io/library/manyselves-api:phase1
```

**效果：**
- Docker: 保持 `docker.io/library/manyselves-api:phase1`
- Podman: 不再添加 `localhost/` 前缀

---

### 2. 更新构建脚本

**scripts/build-local.sh：**

```bash
docker save -o manyselves-images.tar \
  docker.io/library/manyselves-api:phase1 \
  docker.io/library/manyselves-web:phase1
```

---

### 3. 更新部署脚本

**scripts/deploy-server.sh：**

```bash
podman load -i manyselves-images.tar

# 检查是否有 localhost/ 前缀（向后兼容）
if podman images | grep -q "localhost/manyselves-api"; then
  echo "检测到 localhost/ 前缀，重新 tag 镜像..."
  podman tag localhost/manyselves-api:phase1 manyselves-api:phase1
  podman tag localhost/manyselves-web:phase1 manyselves-web:phase1
fi
```

---

## 📋 修改的文件

| 文件 | 修改内容 |
|------|---------|
| `deploy/compose.yaml` | 镜像名称添加 `docker.io/library/` 前缀 |
| `deploy/compose.simple.yaml` | 镜像名称添加 `docker.io/library/` 前缀 |
| `scripts/build-local.sh` | 保存镜像时使用完整名称 |
| `scripts/deploy-server.sh` | 添加自动重新 tag 逻辑（向后兼容） |

---

## 🚀 使用方法

### 本地构建（Docker）

```bash
./scripts/build-local.sh
```

生成的镜像：
- `docker.io/library/manyselves-api:phase1`
- `docker.io/library/manyselves-web:phase1`

### 服务器部署（Podman）

```bash
./scripts/deploy-server.sh
```

镜像加载后：
- ✅ 不会出现 `localhost/` 前缀
- ✅ 与配置文件一致

---

## 🔧 验证

### 本地 Docker

```bash
docker images | grep manyselves
# 输出：
# docker.io/library/manyselves-api   phase1   ...
# docker.io/library/manyselves-web   phase1   ...
```

### 服务器 Podman

```bash
podman images | grep manyselves
# 输出：
# docker.io/library/manyselves-api   phase1   ...
# docker.io/library/manyselves-web   phase1   ...
```

**不再出现 `localhost/` 前缀！**

---

## 📝 为什么使用 `docker.io/library/`？

### 镜像仓库命名规则

```
[仓库地址]/[命名空间]/[镜像名]:[标签]
```

示例：
- `docker.io/library/manyselves-api:phase1` (完整名称)
- `manyselves-api:phase1` (简写，Docker 默认)

### Podman 行为

| 输入 | Podman 解析 |
|------|------------|
| `manyselves-api:phase1` | `localhost/manyselves-api:phase1` (本地镜像) |
| `docker.io/library/manyselves-api:phase1` | 保持不变 (明确指定仓库) |

---

## ⚠️ 注意事项

### 兼容性

- ✅ Docker：完全兼容
- ✅ Podman：完全兼容
- ✅ 其他容器运行时：应该兼容

### 镜像推送

如果需要推送到私有仓库：

```bash
# 推送到私有仓库
docker tag docker.io/library/manyselves-api:phase1 \
  your-registry.com/manyselves-api:phase1

docker push your-registry.com/manyselves-api:phase1
```

---

## 🔄 回退方案

如果需要恢复之前的命名方式：

```bash
# 重新 tag 为简短名称
docker tag docker.io/library/manyselves-api:phase1 \
  manyselves-api:phase1

# 或在 Podman 中
podman tag docker.io/library/manyselves-api:phase1 \
  manyselves-api:phase1
```