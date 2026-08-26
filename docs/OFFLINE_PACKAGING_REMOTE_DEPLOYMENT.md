# 离线打包远程部署

本文档用于将本地最新源码构建为 `linux/amd64` 离线镜像包，并部署到无法联网或不方便在线构建的 CentOS 服务器。

本文以以下环境为例：

- 项目目录：`D:\yuanxi-algo\manyselves`
- 当前版本：`v0.0.2`
- CentOS 地址：`192.168.8.28`
- 上传账户：`algo_001`
- 服务账户：`manyselves`
- 安装目录：`/opt/manyselves`
- 持久化数据目录：`/srv/manyselves/data`
- 服务端口：`9090`

如果实际服务器地址、版本或端口不同，请替换对应参数。

## 一、重要原则

1. 本地源码必须先提交到 Git，再进行打包。
2. 不要使用 `--allow-dirty` 打正式发布包。
3. 同一个版本重新打包时，镜像标签仍可保持 `v0.0.2`，但必须重新上传镜像包、源码包和校验文件。
4. 服务器持久化数据始终保存在 `/srv/manyselves/data`，部署时不要删除或覆盖该目录。
5. 普通源码升级不要使用 `--restore-data`。
6. 不要将本地或服务器的 `deploy/.env` 打进发布包或提交到 Git。

## 二、本地环境要求

本地需要：

- Windows 11 或 Windows 10
- WSL 2
- Docker Desktop
- Docker Desktop 已启用当前 WSL 发行版的 Integration
- Git
- PowerShell

在 WSL 中确认 Docker 可用：

```powershell
wsl bash -lc "docker version && docker compose version"
```

如果提示找不到 `docker`，打开 Docker Desktop：

1. 进入 `Settings`。
2. 打开 `Resources` → `WSL Integration`。
3. 启用当前使用的 WSL 发行版。
4. 点击 `Apply & Restart`。

## 三、提交并检查本地源码

进入项目目录：

```powershell
cd D:\yuanxi-algo\manyselves
```

检查分支和修改：

```powershell
git branch --show-current
git status
```

正式打包前，工作区必须显示：

```text
nothing to commit, working tree clean
```

如果存在新代码尚未提交，先执行：

```powershell
git add <需要提交的文件>
git commit -m "填写本次修改说明"
```

再次检查：

```powershell
git status
git log -1 --oneline
Get-Content VERSION
```

`VERSION` 当前应为：

```text
v0.0.2
```

> `scripts/build-local.sh` 使用 `git archive HEAD` 生成源码包。未提交的源码不会进入源码归档，因此正式发布必须先提交。

## 四、本地构建离线镜像和源码包

项目已提供构建脚本：

```text
scripts/build-local.sh
```

在 PowerShell 中执行：

```powershell
wsl bash -lc "cd /mnt/d/yuanxi-algo/manyselves && ./scripts/build-local.sh --version v0.0.2 --output-dir /mnt/d/yuanxi-algo/manyselves/release"
```

脚本会自动完成以下操作：

1. 使用 `linux/amd64` 平台构建 API 镜像。
2. 使用 `linux/amd64` 平台构建 Web 镜像。
3. 将两个镜像保存到同一个离线镜像包。
4. 从当前 Git `HEAD` 生成源码压缩包。
5. 生成 SHA256 校验文件。

对应的核心镜像命令是：

```bash
DOCKER_DEFAULT_PLATFORM=linux/amd64 docker compose \
  -f deploy/compose.yaml \
  --env-file release/manyselves-release-v0.0.2/manyselves-build.env \
  build
```

保存 API 和 Web 镜像：

```bash
docker save \
  -o release/manyselves-release-v0.0.2/manyselves-v0.0.2-linux-amd64-images.tar \
  manyselves-api:v0.0.2 \
  manyselves-web:v0.0.2
```

生成源码包：

```bash
git archive \
  --format=tar.gz \
  --output=release/manyselves-release-v0.0.2/manyselves-v0.0.2-source.tar.gz \
  --prefix=manyselves-v0.0.2/ \
  HEAD
```

正常完成后，发布目录为：

```text
D:\yuanxi-algo\manyselves\release\manyselves-release-v0.0.2
```

目录内应包含：

```text
manyselves-v0.0.2-linux-amd64-images.tar
manyselves-v0.0.2-source.tar.gz
manyselves-v0.0.2.sha256
manyselves-build.env
```

其中需要上传服务器的是前三个文件，`manyselves-build.env` 仅用于本地构建。

## 五、本地校验发布包

执行：

```powershell
wsl bash -lc "cd /mnt/d/yuanxi-algo/manyselves/release/manyselves-release-v0.0.2 && sha256sum -c manyselves-v0.0.2.sha256"
```

必须显示：

```text
manyselves-v0.0.2-linux-amd64-images.tar: OK
manyselves-v0.0.2-source.tar.gz: OK
```

检查镜像架构：

```powershell
wsl bash -lc "docker image inspect manyselves-api:v0.0.2 manyselves-web:v0.0.2 --format='{{.RepoTags}} {{.Os}}/{{.Architecture}}'"
```

应显示两个镜像均为：

```text
linux/amd64
```

## 六、上传到 CentOS

进入发布目录：

```powershell
cd D:\yuanxi-algo\manyselves\release\manyselves-release-v0.0.2
```

为了避免服务器 `/tmp` 中旧的同名文件权限冲突，先上传为 `.new`：

```powershell
scp manyselves-v0.0.2-linux-amd64-images.tar algo_001@192.168.8.28:/tmp/manyselves-v0.0.2-linux-amd64-images.tar.new
scp manyselves-v0.0.2-source.tar.gz algo_001@192.168.8.28:/tmp/manyselves-v0.0.2-source.tar.gz.new
scp manyselves-v0.0.2.sha256 algo_001@192.168.8.28:/tmp/manyselves-v0.0.2.sha256.new
```

## 七、服务器替换文件并授权

登录服务器：

```bash
ssh algo_001@192.168.8.28
```

将新文件替换为正式文件名：

```bash
sudo mv -f /tmp/manyselves-v0.0.2-linux-amd64-images.tar.new /tmp/manyselves-v0.0.2-linux-amd64-images.tar
sudo mv -f /tmp/manyselves-v0.0.2-source.tar.gz.new /tmp/manyselves-v0.0.2-source.tar.gz
sudo mv -f /tmp/manyselves-v0.0.2.sha256.new /tmp/manyselves-v0.0.2.sha256
```

授权给服务账户：

```bash
sudo chown manyselves:manyselves /tmp/manyselves-v0.0.2-linux-amd64-images.tar
sudo chown manyselves:manyselves /tmp/manyselves-v0.0.2-source.tar.gz
sudo chown manyselves:manyselves /tmp/manyselves-v0.0.2.sha256

sudo chmod 0644 /tmp/manyselves-v0.0.2-linux-amd64-images.tar
sudo chmod 0644 /tmp/manyselves-v0.0.2-source.tar.gz
sudo chmod 0644 /tmp/manyselves-v0.0.2.sha256
```

## 八、切换到服务账户并停止旧服务

由 `algo_001` 切换到 `manyselves`：

```bash
sudo -iu manyselves
export XDG_RUNTIME_DIR=/run/user/$(id -u)
```

停止现有服务：

```bash
podman compose \
  -f /opt/manyselves/manyselves-v0.0.2/deploy/compose.yaml \
  --env-file /opt/manyselves/manyselves-v0.0.2/deploy/.env \
  down
```

也可以使用单行命令，避免续行符输入错误：

```bash
podman compose -f /opt/manyselves/manyselves-v0.0.2/deploy/compose.yaml --env-file /opt/manyselves/manyselves-v0.0.2/deploy/.env down
```

确认服务停止：

```bash
podman ps -a
```

停止容器不会删除 `/srv/manyselves/data`。

## 九、服务器校验上传文件

```bash
cd /tmp
sha256sum -c manyselves-v0.0.2.sha256
```

必须显示：

```text
manyselves-v0.0.2-linux-amd64-images.tar: OK
manyselves-v0.0.2-source.tar.gz: OK
```

如果不是 `OK`，不要继续部署，应重新上传对应文件。

## 十、解压并执行部署脚本

创建独立临时目录：

```bash
STAGE_DIR="$(mktemp -d /tmp/manyselves-deploy.XXXXXX)"
tar -xzf /tmp/manyselves-v0.0.2-source.tar.gz -C "$STAGE_DIR"
cd "$STAGE_DIR/manyselves-v0.0.2"
chmod +x scripts/deploy-centos-podman.sh
```

执行部署：

```bash
./scripts/deploy-centos-podman.sh \
  --version v0.0.2 \
  --artifact-dir /tmp \
  --install-dir /opt/manyselves \
  --data-dir /srv/manyselves/data \
  --public-host 192.168.8.28 \
  --port 9090
```

该脚本会自动：

1. 校验发布包。
2. 使用 `podman load` 导入 API 和 Web 镜像。
3. 将新源码安装到 `/opt/manyselves/manyselves-v0.0.2`。
4. 保存并恢复服务器现有的 `deploy/.env`。
5. 保留并重新挂载 `/srv/manyselves/data`。
6. 修复 Rootless Podman 数据目录权限。
7. 使用新镜像重新创建并启动服务。
8. 等待服务健康检查通过。

> 普通源码升级不要添加 `--restore-data`。该参数仅用于明确需要从数据归档恢复数据的场景。

## 十一、检查部署结果

检查容器：

```bash
podman ps -a
```

检查镜像：

```bash
podman images | grep manyselves
```

由于可以使用同一个 `v0.0.2` 标签重复构建，不要只根据标签判断是否为新镜像；应结合本地构建输出或镜像 ID 判断。

健康检查：

```bash
curl -fsS http://127.0.0.1:9090/api/v1/health/live
```

预期返回：

```json
{"status":"live"}
```

就绪检查：

```bash
curl -fsS http://127.0.0.1:9090/api/v1/health/ready
```

浏览器访问：

```text
http://192.168.8.28:9090
```

## 十二、查看日志

推荐使用单行命令：

```bash
podman compose -f /opt/manyselves/manyselves-v0.0.2/deploy/compose.yaml --env-file /opt/manyselves/manyselves-v0.0.2/deploy/.env logs --tail 150 api web
```

持续跟踪日志：

```bash
podman compose -f /opt/manyselves/manyselves-v0.0.2/deploy/compose.yaml --env-file /opt/manyselves/manyselves-v0.0.2/deploy/.env logs -f --tail 150 api web
```

## 十三、以后停止和启动服务

停止：

```bash
podman compose -f /opt/manyselves/manyselves-v0.0.2/deploy/compose.yaml --env-file /opt/manyselves/manyselves-v0.0.2/deploy/.env down
```

启动：

```bash
podman compose -f /opt/manyselves/manyselves-v0.0.2/deploy/compose.yaml --env-file /opt/manyselves/manyselves-v0.0.2/deploy/.env up -d --no-build
```

重新创建容器：

```bash
podman compose -f /opt/manyselves/manyselves-v0.0.2/deploy/compose.yaml --env-file /opt/manyselves/manyselves-v0.0.2/deploy/.env up -d --no-build --force-recreate
```

## 十四、数据目录说明

服务器真实数据目录：

```text
/srv/manyselves/data
```

容器内部访问路径：

```text
/data/manyselves
```

二者是同一份数据的宿主机路径和容器路径。

检查实际挂载：

```bash
API_CID="$(podman ps --filter name=manyselves --format '{{.ID}} {{.Names}}' | awk '/api/{print $1; exit}')"
podman inspect "$API_CID" --format '{{range .Mounts}}{{println .Source " -> " .Destination}}{{end}}'
```

应看到类似：

```text
/srv/manyselves/data -> /data/manyselves
```

部署期间禁止执行：

```bash
rm -rf /srv/manyselves/data
```

也不要将空目录或本地测试数据恢复到该目录。

## 十五、同版本重新部署注意事项

当版本仍为 `v0.0.2`，但源码已经更新时：

1. 本地重新执行完整构建脚本。
2. 必须重新上传镜像包、源码包和 SHA256 文件。
3. 服务器必须重新执行 `podman load`，部署脚本会自动完成。
4. 必须使用 `--force-recreate` 重新创建容器，部署脚本会自动完成。
5. 浏览器如果仍显示旧前端，可强制刷新或清理站点缓存。
6. 发布前应保留上一份离线包，以便出现问题时重新部署上一份包。

## 十六、常见错误

### 1. WSL 中找不到 Docker

```text
The command 'docker' could not be found in this WSL 2 distro.
```

处理方法：启用 Docker Desktop 的 WSL Integration，并重新打开 WSL 终端。

### 2. 校验和失败

```text
FAILED
```

说明上传文件不完整或三个文件不是同一次构建生成的，应重新上传全部发布文件。

### 3. Podman 尝试从 localhost 拉取镜像

说明镜像未正确导入，或 `deploy/.env` 中镜像名称不正确。检查：

```bash
podman images | grep manyselves
grep -E '^(MANYSELVES_API_IMAGE|MANYSELVES_WEB_IMAGE)=' /opt/manyselves/manyselves-v0.0.2/deploy/.env
```

正常值类似：

```text
MANYSELVES_API_IMAGE=localhost/manyselves-api:v0.0.2
MANYSELVES_WEB_IMAGE=localhost/manyselves-web:v0.0.2
```

### 4. 数据目录 Permission denied

部署脚本会执行 Rootless Podman 权限修复。如果仍有问题，使用 `manyselves` 账户执行：

```bash
podman unshare chown -R 999:999 /srv/manyselves/data
podman unshare chmod -R u+rwX,g+rwX,o-rwx /srv/manyselves/data
```

然后重新创建服务：

```bash
podman compose -f /opt/manyselves/manyselves-v0.0.2/deploy/compose.yaml --env-file /opt/manyselves/manyselves-v0.0.2/deploy/.env up -d --no-build --force-recreate
```

