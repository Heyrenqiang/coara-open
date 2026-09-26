/** Slash autocomplete matching CLI ``slash_pickers`` / ``SlashCommandCompleter``. */

interface SlashCommandInfo {
  name: string;
  description: string;
  category: string;
}

interface SlashPickerOption {
  insert: string;
  display: string;
  meta: string;
  /** When true, accepting the row should execute immediately (CLI Enter-to-submit). */
  submit: boolean;
}

export type SlashCompletionRow =
  | {
      kind: "command";
      insert: string;
      display: string;
      meta: string;
      category: string;
      /** Commands with pickers only fill the stem; others submit on accept. */
      submit: boolean;
      opensPicker: boolean;
    }
  | {
      kind: "option";
      insert: string;
      display: string;
      meta: string;
      category?: string;
      submit: boolean;
      opensPicker: boolean;
    };

/** ``/ws`` subcommands that are not the switch picker (CLI ``_OWNED``). */
const WS_OWNED = new Set(["list", "rename", "default", "updates"]);

/** Instant-run stems (no secondary picker). Web 端已砍掉图形等价命令
 * （/status /exit /login /ws /new /model，与服务端 _WEB_COMMAND_BLOCKLIST 对齐）。 */
const INSTANT_COMMANDS = new Set([
  "/help",
  "/compact",
  "/report",
  "/tools",
]);

function filterOptions(
  options: SlashPickerOption[],
  command: string,
  filterPrefix: string,
): SlashPickerOption[] {
  const filter = filterPrefix.trim().toLowerCase();
  if (!filter) return options;
  return options.filter((opt) => {
    const insertL = opt.insert.toLowerCase();
    const displayL = opt.display.toLowerCase();
    const metaL = (opt.meta || "").toLowerCase();
    return (
      insertL.startsWith(`${command.toLowerCase()} ${filter}`) ||
      insertL === command.toLowerCase() ||
      displayL.startsWith(filter) ||
      displayL.includes(filter) ||
      metaL.includes(filter) ||
      insertL.includes(filter)
    );
  });
}

function wsFilterPrefix(text: string): string | null {
  // null → do not show switch picker (owned subcommand / done)
  if (text !== "/ws" && !text.startsWith("/ws ")) return null;
  const rest = text.slice(3);
  if (rest === "") return "";
  if (!rest.startsWith(" ")) return null;
  const tokens = rest.trim().split(/\s+/).filter(Boolean);
  if (tokens.length === 0) return "";
  const head = tokens[0].toLowerCase();
  if (WS_OWNED.has(head)) return null;
  if (head === "switch") {
    if (tokens.length >= 3) return null;
    return tokens[1] ?? "";
  }
  if (tokens.length >= 2) return null;
  return tokens[0];
}

/**
 * Build completion rows for the current input line.
 * Empty → hide menu.
 */
export function buildSlashCompletions(
  text: string,
  commands: SlashCommandInfo[],
  pickers: Record<string, SlashPickerOption[]>,
  pickerCommands: string[] = Object.keys(pickers),
): SlashCompletionRow[] {
  const trimmed = text.trimStart();
  if (!trimmed.startsWith("/")) return [];

  const pickerSet = new Set(pickerCommands.map((c) => c.toLowerCase()));
  const parts = trimmed.split(/\s+/);
  const cmd = (parts[0] || "").toLowerCase();

  // Secondary picker mode (``/ws``, ``/model …``).
  if (pickerSet.has(cmd) && (trimmed === cmd || trimmed.startsWith(`${cmd} `))) {
    if (cmd === "/ws") {
      const filter = wsFilterPrefix(trimmed);
      if (filter === null) return [];
      const options = filterOptions(pickers["/ws"] || [], "/ws", filter);
      return options.map((opt) => ({
        kind: "option" as const,
        insert: opt.insert,
        display: opt.display,
        meta: opt.meta,
        submit: opt.submit,
        opensPicker: false,
      }));
    }

    const rest = trimmed.slice(cmd.length);
    const filterPrefix = rest.trim();
    const options = filterOptions(pickers[cmd] || pickers[parts[0]] || [], cmd, filterPrefix);
    return options.map((opt) => ({
      kind: "option" as const,
      insert: opt.insert,
      display: opt.display,
      meta: opt.meta,
      submit: opt.submit,
      opensPicker: false,
    }));
  }

  // Primary command list: only while typing the first token.
  if (parts.length > 1) return [];

  const head = trimmed.toLowerCase();
  const matched = commands.filter((c) => {
    const name = c.name.toLowerCase();
    const desc = (c.description || "").toLowerCase();
    return name.startsWith(head) || (head.length >= 2 && (name.includes(head.slice(1)) || desc.includes(head.slice(1))));
  });

  return matched.map((c) => {
    const name = c.name.toLowerCase();
    const opensPicker = pickerSet.has(name);
    const submit = !opensPicker && INSTANT_COMMANDS.has(name);
    return {
      kind: "command" as const,
      insert: c.name,
      display: c.name,
      meta: c.description,
      category: c.category,
      submit,
      opensPicker,
    };
  });
}

/** Esc: restore bare command stem when a picker row was partially applied. */
export function slashCancelStem(text: string, pickerCommands: string[]): string | null {
  const parts = text.trim().split(/\s+/);
  if (parts.length < 2) return null;
  const head = parts[0].toLowerCase();
  if (pickerCommands.map((c) => c.toLowerCase()).includes(head)) return head;
  return null;
}
