# 设置页面调试指南

## 422 错误排查步骤

### 1. 查看浏览器控制台
打开浏览器开发者工具 (F12)，查看 Console 标签页中的错误信息。

### 2. 查看网络请求详情
1. 打开开发者工具的 Network 标签页
2. 点击保存配置按钮
3. 找到失败的请求（红色）
4. 点击请求查看详情：
   - **Headers**: 检查 `X-Control-Lease-Token` 是否存在
   - **Payload**: 检查请求体格式
   - **Response**: 查看具体错误信息

### 3. 常见错误原因

#### Lease Token 问题
- **症状**: `CONTROL_LEASE_REQUIRED` 错误
- **解决**: 确保应用已正确初始化

#### 请求体格式问题
- **症状**: 422 错误，响应中提示字段缺失
- **解决**: 检查请求体是否包含 `api_key` 字段

#### Provider 不存在
- **症状**: 404 错误，提示 `PRESET_NOT_FOUND`
- **解决**: 选择有效的提供商

### 4. 测试 API 端点

```bash
# 启动应用
./scripts/start.sh

# 在另一个终端测试
curl -X POST "http://localhost:9000/api/v1/settings/presets/mimo/apply" \
  -H "Content-Type: application/json" \
  -H "X-Control-Lease-Token: <your-token>" \
  -d '{"api_key": "your-api-key"}'
```

### 5. 后端日志查看

查看终端输出中的详细错误信息：
```bash
# 后端会打印详细的错误堆栈
INFO:     127.0.0.1:xxxxx - "POST /api/v1/settings/presets/... HTTP/1.1" 422 Unprocessable Entity
```

## 获取帮助

如果以上步骤无法解决问题，请提供：
1. 浏览器控制台的完整错误信息
2. Network 标签中失败请求的 Response 内容
3. 后端终端的错误日志