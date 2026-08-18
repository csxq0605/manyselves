export const supportedCommands = [
  { command: "/help", description: "显示可用命令" },
  { command: "/new", description: "新建并切换会话" },
  { command: "/clear", description: "清空当前会话" },
  { command: "/history", description: "刷新会话历史" },
  { command: "/retry", description: "重试最近一次失败发送" },
  { command: "/stop", description: "停止当前生成" },
] as const;

export type SupportedCommand = typeof supportedCommands[number]["command"];

export function isSupportedCommand(value: string): value is SupportedCommand {
  return supportedCommands.some((item) => item.command === value);
}
