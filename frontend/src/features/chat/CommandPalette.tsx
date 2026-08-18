import { supportedCommands, type SupportedCommand } from "./commands";

export interface CommandPaletteProps {
  readonly onSelect: (command: SupportedCommand) => void;
  readonly query: string;
}

export function CommandPalette({ onSelect, query }: CommandPaletteProps) {
  const normalized = query.trim().toLowerCase();
  if (!normalized.startsWith("/")) return null;
  const matches = supportedCommands.filter((item) => item.command.startsWith(normalized));
  if (matches.length === 0) return null;

  return (
    <div aria-label="命令面板" className="command-palette">
      {matches.map((item) => (
        <button key={item.command} onClick={() => onSelect(item.command)} type="button">
          <code>{item.command}</code> {item.description}
        </button>
      ))}
    </div>
  );
}
