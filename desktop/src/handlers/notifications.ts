import { Notification } from "electron";

import type { z } from "zod";
import type { notificationSchema } from "../ipc-contract.js";

export function showNotification(input: z.infer<typeof notificationSchema>): void {
  if (!Notification.isSupported()) return;
  new Notification({ body: "运行状态已更新", title: input.title }).show();
}
