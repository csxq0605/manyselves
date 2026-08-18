# ManySelves 离线部署快速指南

## 1. 本地 WSL 构建

```bash
cd /mnt/d/yuanxi-algo/manyselves
chmod +x scripts/build-local.sh
./scripts/build-local.sh --version v0.0.1
```

产物默认在 `/tmp/manyselves-release-v0.0.1/`：

- `manyselves-v0.0.1-linux-amd64-images.tar`
- `manyselves-v0.0.1-source.tar.gz`
- `manyselves-v0.0.1.sha256`

## 2. 上传到 CentOS

```bash
scp /tmp/manyselves-release-v0.0.1/manyselves-v0.0.1-* \
  manyselves@SERVER:/tmp/
```

## 3. CentOS Podman 启动

```bash
sudo -iu manyselves
export XDG_RUNTIME_DIR=/run/user/$(id -u)
cd /tmp
sha256sum -c manyselves-v0.0.1.sha256
tar -xzf manyselves-v0.0.1-source.tar.gz
cd manyselves-v0.0.1
bash scripts/deploy-centos-podman.sh \
  --version v0.0.1 \
  --artifact-dir /tmp \
  --public-host SERVER
```

如果首次运行提示目录或 rootless Podman 权限不足，按脚本打印的 `sudo install`、`usermod --add-subuids`、`loginctl enable-linger` 命令让管理员执行一次即可。

## 4. 访问

打开 `http://SERVER:9090`，使用 `deploy/.env` 中的管理员账号登录。
