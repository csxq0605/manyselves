# Podman 部署注意事项

CentOS/RHEL 推荐使用 `scripts/deploy-centos-podman.sh`，不要手动用宽权限修复挂载目录。

## rootless Podman 必备条件

管理员先执行：

```bash
sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 manyselves
sudo loginctl enable-linger manyselves
```

服务用户重新登录后执行：

```bash
sudo -iu manyselves
export XDG_RUNTIME_DIR=/run/user/$(id -u)
podman system migrate
```

## 数据目录权限

不要使用 `chmod 777`。脚本会自动检测 API 镜像内的运行 UID/GID，并执行：

```bash
podman unshare chown -R <container_uid>:<container_gid> /srv/manyselves/data
podman unshare chmod -R u+rwX,g+rwX,o-rwx /srv/manyselves/data
```

Compose 文件中的挂载带有 `:Z`，用于 CentOS/RHEL SELinux 标签隔离。
