const editableExtensions = new Set([
  "cfg", "conf", "css", "csv", "html", "ini", "js", "json", "jsx", "log", "md",
  "py", "sh", "sql", "toml", "ts", "tsx", "txt", "yaml", "yml",
]);

export function isEditableTextPath(path: string): boolean {
  const extension = path.split(".").at(-1)?.toLowerCase() ?? "";
  return editableExtensions.has(extension);
}
