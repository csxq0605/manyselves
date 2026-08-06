# 本地启动指南

## 架构说明

ManySelves 采用 **单服务架构**：

- **React 前端**：构建后嵌入 FastAPI
- **FastAPI 后端**：提供 API 和静态文件服务
- **只启动一个服务**：前端和后端在同一个端口（9000）

---

## 快速启动

```bash
./start.sh
```

**启动过程：**

1. ✅ 自动检查并构建前端（如果需要）
2. ✅ 创建 Python 虚拟环境（如果不存在）
3. ✅ 安装依赖
4. ✅ 启动 FastAPI 服务

**访问地址：**

- 🏠 主页：http://localhost:9000
- 📚 API 文档：http://localhost:9000/docs
- 🔧 ReDoc：http://localhost:9000/redoc

---

## 前置要求

### Python

- **版本：** Python 3.12+
- **Linux/WSL：** 需要安装 `python3-venv`

```bash
# Ubuntu/Debian/WSL
sudo apt update
sudo apt install python3-venv
```

### Node.js

- **版本：** Node.js 22+（仅构建前端时需要）

```bash
# 检查版本
node --version  # 应该 >= 22.0.0

# 使用 nvm 安装（推荐）
nvm install 22
nvm use 22
```

**注意：** 如果前端已经构建过（存在 `frontend/dist/`），则不需要 Node.js。

---

## 环境配置

首次启动会自动创建 `.env` 文件：

```env
MANYSELVES_DATA_DIR=.manyselves
MANYSELVES_INITIAL_PROJECT_ID=default
MANYSELVES_ADMIN_USERNAME=admin
MANYSELVES_ADMIN_PASSWORD=yuanxi@2026
MANYSELVES_HTTP_BIND=127.0.0.1
MANYSELVES_HTTP_PORT=9000
```

### API Key 配置（必需）

编辑 `.env` 文件，添加你的 API Key：

```env
# OpenAI
MANYSELVES_OPENAI_API_KEY=your-openai-api-key

# 或 Anthropic
MANYSELVES_ANTHROPIC_API_KEY=your-anthropic-api-key
```

---

## 手动构建前端

如果只想构建前端：

```bash
cd frontend

# 安装依赖
npm install

# 构建
npm run build

# 构建结果在 frontend/dist/
```

---

## 开发模式

### 只启动后端（跳过前端构建）

如果你确定前端已构建且无需更新：

```bash
# 直接运行 FastAPI
python run_web.py --reload
```

### 前端开发服务器

如果需要实时预览前端修改：

```bash
cd frontend
npm run dev
```

然后访问 http://localhost:5173

**注意：** 这种模式下，前端会代理 API 请求到 http://localhost:9000。

---

## Windows 环境

在 Windows PowerShell 中：

```powershell
.\start.bat
```

或手动启动：

```powershell
# 构建前端
cd frontend
npm install
npm run build
cd ..

# 启动后端
python run_web.py --reload
```

---

## 常见问题

### 1. WSL 中虚拟环境激活失败

**问题：** `.venv/bin/activate: No such file or directory`

**原因：** Windows 创建的虚拟环境在 WSL 中不兼容

**解决：** 脚本会自动检测并重新创建，或手动删除：

```bash
rm -rf .venv
./start.sh
```

### 2. Python venv 模块不可用

**问题：** `ensurepip is not available`

**解决：**

```bash
sudo apt update
sudo apt install python3-venv
```

### 3. 前端构建失败

**可能原因：**

- Node.js 版本过低（需要 22+）
- 依赖未安装

**解决：**

```bash
cd frontend
rm -rf node_modules package-lock.json
npm install
npm run build
```

### 4. 找不到静态文件

**问题：** 访问 http://localhost:9000 显示 404

**原因：** 前端未构建或构建失败

**解决：**

```bash
# 检查构建结果
ls frontend/dist/

# 如果不存在，重新运行启动脚本
./start.sh
```

---

## 端口说明

| 服务 | 端口 | 说明 |
|------|------|------|
| 主服务 | 9000 | FastAPI + React SPA |
| API 文档 | 9000/docs | Swagger UI |
| ReDoc | 9000/redoc | ReDoc 文档 |

---

## 下一步

启动成功后：

1. 访问 http://localhost:9000
2. 使用默认账号登录：`admin` / `yuanxi@2026`
3. 创建你的第一个项目

---

## 相关文档

- [部署指南](./DEPLOY.md)
- [Provider 配置](./PROVIDERS.md)