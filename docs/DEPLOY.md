# ManySelves CentOS 离线部署指南

推荐流程是：本地 WSL/Docker Desktop 构建离线产物，上传到 CentOS，然后用 rootless Podman 启动。

## 本地打包

```bash
cd /mnt/d/yuanxi-algo/manyselves
./scripts/build-local.sh --version v0.0.1
```

如需一起打包本地 `deploy/data`：

```bash
./scripts/build-local.sh --version v0.0.1 --with-data
```

构建完成后，上传 release 目录中的文件：

```bash
scp /tmp/manyselves-release-v0.0.1/manyselves-v0.0.1-* \
  manyselves@SERVER:/tmp/
```

## CentOS 首次准备

管理员执行一次：

```bash
sudo useradd -m -s /bin/bash manyselves 2>/dev/null || true
sudo install -d -m 0750 -o manyselves -g manyselves /opt/manyselves
sudo install -d -m 0700 -o manyselves -g manyselves /srv/manyselves/data /srv/manyselves/backups
sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 manyselves
sudo loginctl enable-linger manyselves
```

进入服务用户：

```bash
sudo -iu manyselves
export XDG_RUNTIME_DIR=/run/user/$(id -u)
podman system migrate 2>/dev/null || true
podman info
```

## 一键启动

```bash
cd /tmp
sha256sum -c manyselves-v0.0.1.sha256
tar -xzf manyselves-v0.0.1-source.tar.gz
cd manyselves-v0.0.1
bash scripts/deploy-centos-podman.sh \
  --version v0.0.1 \
  --artifact-dir /tmp \
  --install-dir /opt/manyselves \
  --data-dir /srv/manyselves/data \
  --public-host SERVER \
  --port 9090
```

脚本会自动完成：

- 校验 `sha256`
- `podman load` 加载离线镜像
- 解压版本源码到 `/opt/manyselves/manyselves-v0.0.1`
- 创建或保留 `deploy/.env`
- 写入当前版本镜像名和访问地址
- 用 `podman unshare chown` 修复 rootless 挂载目录权限
- 使用 `podman compose up -d --no-build` 离线启动

如果需要恢复数据包：

```bash
bash scripts/deploy-centos-podman.sh \
  --version v0.0.1 \
  --artifact-dir /tmp \
  --public-host SERVER \
  --restore-data
```

## 常用命令

```bash
cd /opt/manyselves/manyselves-v0.0.1
podman compose -f deploy/compose.yaml --env-file deploy/.env ps
podman compose -f deploy/compose.yaml --env-file deploy/.env logs -f --tail 200 api web
podman compose -f deploy/compose.yaml --env-file deploy/.env restart
podman compose -f deploy/compose.yaml --env-file deploy/.env down
```

## 模型配置

模型配置保存在 `/srv/manyselves/data/manyselves.config.yaml`。推荐在页面的“设置 / 模型配置”中维护；API Key 保存后不会回显明文，这是安全设计。
