const shanghaiDateTimeFormatter = new Intl.DateTimeFormat("zh-CN", {
  day: "2-digit",
  hour: "2-digit",
  hour12: false,
  hourCycle: "h23",
  minute: "2-digit",
  month: "2-digit",
  second: "2-digit",
  timeZone: "Asia/Shanghai",
  year: "numeric",
});

const explicitTimeZonePattern = /(Z|[+-]\d{2}:\d{2})$/i;

export function formatShanghaiDateTime(timestamp: string): string {
  const value = timestamp.trim();
  if (!value) return timestamp;
  const normalized = explicitTimeZonePattern.test(value) ? value : `${value}Z`;
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) return timestamp;

  const parts = Object.fromEntries(
    shanghaiDateTimeFormatter.formatToParts(date).map((part) => [part.type, part.value]),
  );
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`;
}
