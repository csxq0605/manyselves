# 409 Conflict 错误诊断

## 错误信息

```
POST /api/v1/conversations/{id}/activate 409 Conflict
```

## 错误原因

**RuntimeBusyError** - Runtime 正忙

当试图激活对话时，系统要求所有 agent 必须处于空闲状态，但当前有 agent 正在运行。

## 触发条件

激活对话时会检查：

```python
def require_switch_safe(self) -> None:
    """Reject session switches while a turn or persistence callback owns state."""
    statuses = self.facade.snapshot().agent_statuses.values()

    # 必须满足所有条件才能切换
    if (
        any(status != "idle" for status in statuses)  # 所有 agent 必须为 idle
        or self.pending_persistence                    # 不能有待处理的持久化
        or bool(self._streams)                         # 不能有活跃的流
    ):
        raise RuntimeBusyError()  # ❌ 抛出 409 错误
```

## 常见场景

### 1. 前一个对话还在运行

**症状：**
- 发送消息后立即尝试切换对话
- Agent 还在生成响应

**解决：**
- ✅ 等待响应完成（状态变为 idle）
- ✅ 看到响应结束后再切换

### 2. 流式响应未完成

**症状：**
- 正在接收流式响应
- 突然切换对话

**解决：**
- ✅ 等待流式响应完成
- ✅ 不要在响应过程中切换

### 3. 后台持久化未完成

**症状：**
- 刚保存了配置
- 立即切换对话

**解决：**
- ✅ 等待 1-2 秒
- ✅ 让后台持久化完成

### 4. Agent 状态卡住

**症状：**
- 响应已完成，但状态不是 idle
- 可能是异常中断导致

**解决：**
- ✅ 重启应用（最简单）
- ✅ 检查是否有错误日志

## 调试方法

### 1. 检查日志

```bash
# 查看是否有错误
grep -i "error\|exception" logs/app.log
```

### 2. 检查 Agent 状态

在浏览器控制台：
```javascript
// 查看 agent 状态
fetch('/api/v1/agents')
  .then(r => r.json())
  .then(data => console.table(data.agents))
```

### 3. 重启应用

最简单的解决方案：
```bash
# 停止当前服务 (Ctrl+C)
./scripts/start.sh
```

## 预防措施

### ✅ 最佳实践

1. **等待响应完成**
   - 看到响应结束标记
   - 等待状态指示器变为 idle

2. **避免快速切换**
   - 发送消息后等待 1-2 秒
   - 不要在响应过程中切换对话

3. **检查状态**
   - 界面上应该显示状态指示
   - 确保没有加载中的图标

### ❌ 常见错误

1. **发送消息后立即切换**
   ```typescript
   // ❌ 错误做法
   await sendMessage(content)
   await activateConversation(newId)  // 太快！
   ```

2. **流式响应中途切换**
   ```typescript
   // ❌ 错误做法
   stream.on('data', (chunk) => {
     if (someCondition) {
       activateConversation(otherId)  // 流还在进行中！
     }
   })
   ```

## 技术细节

### 错误响应格式

```json
{
  "error": {
    "code": "RUNTIME_BUSY",
    "message": "Runtime has active work",
    "retryable": true
  }
}
```

### retryable: true 的含义

这个错误是**可重试**的：
- ✅ 等待几秒后可以重试
- ✅ 通常是临时状态
- ✅ 不会造成数据丢失

## 自动重试机制

前端可以实现自动重试：

```typescript
async function activateWithRetry(sessionId: string, retries = 3) {
  for (let i = 0; i < retries; i++) {
    try {
      return await api.activateConversation(sessionId)
    } catch (error) {
      if (error.code === 'RUNTIME_BUSY' && i < retries - 1) {
        await new Promise(resolve => setTimeout(resolve, 2000))
        continue
      }
      throw error
    }
  }
}
```

## 总结

**409 Conflict 是保护机制，防止在运行中切换对话。**

- ✅ 等待当前操作完成
- ✅ 避免快速切换
- ✅ 必要时重启应用
- ✅ 这是正常的安全检查