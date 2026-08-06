# 本地直接运行指南（无 Docker）

本文档介绍如何直接使用 Python 运行 ManySelves，无需 Docker 容器。

## 📋 系统要求

- **Python**: 3.12 或更高版本
- **操作系统**: Windows / macOS / Linux
- **内存**: 至少 4GB RAM
- **磁盘**: 至少 1GB 可用空间

---

## 🚀 快速启动

### Windows

```cmd
# 双击运行
start.bat

# 或命令行
python run_web.py
```

### Linux/macOS

```bash
# 添加执行权限
chmod +x start.sh

# 运行
./start.sh

# 或直接
python run_web.py
```

### 访问服务

启动后访问：
- **前端**: http://localhost:9000
- **API 文档**: http://localhost:9000/docs
- **健康检查**: http://localhost:9000/api/v1/health

---

## ⚙️ 配置

### 方式 1：环境变量（推荐）

创建 `.env` 文件：

```bash
# 复制示例配置
cp deploy/.env.example .env

# 编辑配置
nano .env
```

**必需配置：**

```bash
# API Key（至少配置一个）
MANYSELVES_OPENAI_API_KEY=your-api-key-here
# 或
MANYSELVES_ANTHROPIC_API_KEY=your-anthropic-key

# 数据目录
MANYSELVES_DATA_DIR=/path/to/your/data

# 管理员账号
MANYSELVES_ADMIN_USERNAME=admin
MANYSELVES_ADMIN_PASSWORD=your-password
```

**可选配置：**

```bash
# 网络配置
MANYSELVES_HTTP_BIND=0.0.0.0  # 监听所有网卡
MANYSELVES_HTTP_PORT=9000     # 端口号

# 模型配置
MANYSELVES_BOOTSTRAP_PROVIDER=openai
MANYSELVES_BOOTSTRAP_API_BASE=https://api.openai.com/v1
MANYSELVES_BOOTSTRAP_MODEL=gpt-4o

# 允许的前端源（JSON 数组）
MANYSELVES_ALLOWED_ORIGINS=["http://localhost:3000","http://192.168.1.100:9000"]
```

### 方式 2：配置文件

编辑 `manyselves.config.yaml`：

```yaml
providers:
  active: openai-official

  configurations:
  - id: openai-official
    name: OpenAI
    provider: openai
    apiBase: https://api.openai.com/v1
    defaultModel: gpt-4o
    enabled: true
    apiKey: null  # 在界面设置
```

---

## 🔧 命令行参数

```bash
python run_web.py --help
```

### 常用参数

```bash
# 指定监听地址和端口
python run_web.py --host 0.0.0.0 --port 9000

# 开发模式（自动重载）
python run_web.py --reload

# 生产模式（多进程）
python run_web.py --workers 4

# 指定数据目录
python run_web.py --data-dir /path/to/data

# 日志级别
python run_web.py --log-level debug
```

---

## 🏭 生产环境部署

### 使用 Systemd（Linux）

创建服务文件 `/etc/systemd/system/manyselves.service`:

```ini
[Unit]
Description=ManySelves Web API
After=network.target

[Service]
Type=notify
User=www-data
Group=www-data
WorkingDirectory=/opt/manyselves
Environment="PATH=/opt/manyselves/.venv/bin"
EnvironmentFile=/opt/manyselves/.env
ExecStart=/opt/manyselves/.venv/bin/python run_web.py \
  --host 0.0.0.0 \
  --port 9000 \
  --workers 4
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

启动服务：

```bash
sudo systemctl daemon-reload
sudo systemctl enable manyselves
sudo systemctl start manyselves
sudo systemctl status manyselves
```

### 使用 Gunicorn（生产环境）

```bash
# 安装 Gunicorn
pip install gunicorn

# 运行
gunicorn manyselves.webapi.main:create_app \
  --workers 4 \
  --worker-class uvicorn.workers.UvicornWorker \
  --bind 0.0.0.0:9000 \
  --timeout 120 \
  --access-logfile - \
  --error-logfile -
```

---

## 🐛 故障排查

### 问题 1：端口被占用

```bash
# 错误信息
OSError: [Errno 98] Address already in use

# 解决方案
# 1. 查找占用进程
lsof -i :9000

# 2. 终止进程
kill -9 <PID>

# 3. 或更换端口
python run_web.py --port 9001
```

### 问题 2：缺少依赖

```bash
# 错误信息
ModuleNotFoundError: No module named 'xxx'

# 解决方案
pip install -e .
```

### 问题 3：API Key 无效

```bash
# 错误信息
AuthenticationError: Invalid API key

# 解决方案
# 1. 检查 API Key 是否正确
echo $MANYSELVES_OPENAI_API_KEY

# 2. 检查 API Key 格式（tp-xxxxx）

# 3. 检查 API Base URL
echo $MANYSELVES_BOOTSTRAP_API_BASE
```

### 问题 4：数据目录权限

```bash
# 错误信息
PermissionError: [Errno 13] Permission denied

# 解决方案
# 1. 检查目录权限
ls -la .manyselves

# 2. 修改权限
chmod 755 .manyselves
chown -R $USER:$USER .manyselves
```

---

## 🔐 安全建议

### 1. 使用 HTTPS（生产环境）

使用 Nginx 反向代理：

```nginx
server {
    listen 443 ssl;
    server_name your-domain.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location / {
        proxy_pass http://127.0.0.1:9000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### 2. 修改默认密码

```bash
# 编辑 .env
MANYSELVES_ADMIN_PASSWORD=your-strong-password
```

### 3. 限制访问来源

```bash
# 仅允许特定 IP 访问
MANYSELVES_ALLOWED_ORIGINS=["https://your-domain.com"]
```

### 4. 定期备份数据

```bash
# 备份脚本 backup.sh
#!/bin/bash
DATE=$(date +%Y%m%d_%H%M%S)
tar -czf manyselves-backup-$DATE.tar.gz .manyselves/
# 保留最近 7 天的备份
find . -name "manyselves-backup-*.tar.gz" -mtime +7 -delete
```

---

## 📊 性能调优

### 1. 工作进程数

```bash
# 推荐：CPU 核心数
python run_web.py --workers 4
```

### 2. 内存限制

```bash
# 限制每个进程内存（Linux）
systemctl edit manyselves.service

[Service]
MemoryMax=2G
```

### 3. 日志轮转

```bash
# /etc/logrotate.d/manyselves
/var/log/manyselves/*.log {
    daily
    missingok
    rotate 7
    compress
    delaycompress
    notifempty
    create 0640 www-data www-data
}
```

---

## 🔄 更新升级

```bash
# 拉取最新代码
git pull

# 更新依赖
pip install -e .

# 重启服务
sudo systemctl restart manyselves
```

---

## 📚 相关文档

- [部署指南](../docs/DEPLOY.md) - Docker 部署方式
- [配置说明](../docs/QUICKSTART.md) - 详细配置选项
- [API 文档](http://localhost:9000/docs) - 在线 API 文档

---

## 💬 获取帮助

- **问题反馈**: https://github.com/your-org/manyselves/issues
- **文档**: https://manyselves.readthedocs.io
- **社区**: https://discord.gg/manyselves