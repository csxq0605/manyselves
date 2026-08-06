# 快速部署指南

## 一键部署（推荐）

### Windows WSL 本地构建

```bash
cd /mnt/d/yuanxi-algo/manyselves
chmod +x scripts/build-local.sh
./scripts/build-local.sh
```

### 传输到服务器

```bash
scp manyselves-images.tar manyselves-deploy.tar.gz \
  algo_001@192.168.8.28:/home/algo_001/
```

### 服务器部署

```bash
ssh algo_001@192.168.8.28

# 解压部署文件
mkdir -p ~/manyselves
tar -xzf manyselves-deploy.tar.gz -C ~/manyselves --strip-components=0
cd ~/manyselves

# 运行部署脚本
chmod +x scripts/deploy-server.sh
./scripts/deploy-server.sh
```

首次运行会提示设置 API Key：

```bash
vi ~/manyselves/deploy/.env
# 修改：MANYSELVES_OPENAI_API_KEY=tp-你的密钥
```

再次运行：

```bash
./scripts/deploy-server.sh
```

访问：`http://192.168.8.28:9090`

## 详细文档

查看 [docs/DEPLOY.md](docs/DEPLOY.md) 了解完整部署流程。