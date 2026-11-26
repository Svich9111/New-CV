/**
 * Личный чат с агентом по контракту `api/openapi.yaml` (`/chat/messages`, АР-32): проверка вида ответов
 * истории и разбор потока Server-Sent Events ответа агента.
 *
 * Поток: `event: <имя>` + `data: <JSON в одну строку>` + пустая строка; строки-комментарии (`:`) —
 * поддержание соединения; ноль или больше `delta`, затем ровно одно из `done`, `crisis`, `error`.
 * Тексты реплик здесь только разбираются — никуда не пишутся и не журналируются.
 */
import type { ChatErrorCode, ChatHistory, ChatMessage, ChatStreamDone, HelpContact } from "./types";

const isObject = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const isStr = (v: unknown): v is string => typeof v === "string";
const nonEmpty = (v: unknown): v is string => isStr(v) && v.length > 0;

const UNAVAILABLE_REASONS = ["format", "unknown_key", "auth", "encoding", "context"];

/** `ExportText`: строка или `UnavailableText` (`unavailable: true` и код причины из перечня). */
export function isExportText(v: unknown): boolean {
  return (
    isStr(v) || (isObject(v) && v.unavailable === true && isStr(v.reason) && UNAVAILABLE_REASONS.includes(v.reason))
  );
}

/** `ChatMessage`: id, автор из перечня, текст или отметка недоступности, время создания — разбираемая дата. */
export function isChatMessage(v: unknown): v is ChatMessage {
  return (
    isObject(v) &&
    nonEmpty(v.id) &&
    (v.author === "owner" || v.author === "agent") &&
    isExportText(v.text) &&
    isStr(v.created_at) &&
    !Number.isNaN(Date.parse(v.created_at))
  );
}

/** Страница истории: `items` — массив `ChatMessage`, `next_cursor` — непустая строка или `null`. */
export function isChatHistory(v: unknown): v is ChatHistory {
  return (
    isObject(v) &&
    Array.isArray(v.items) &&
    v.items.every(isChatMessage) &&
    (v.next_cursor === null || nonEmpty(v.next_cursor))
  );
}

/** Сырое событие SSE (имя и строка `data`). */
export interface RawEvent {
  event: string;
  data: string;
}

/**
 * Разбор SSE по кускам произвольной длины (граница куска может прийти посреди строки, между `\r` и `\n`,
 * посреди многобайтного символа — последнее решает `TextDecoder` с `stream: true` у вызывающего).
 * По стандарту: строки делятся `\r\n`, `\n` или `\r`; пустая строка завершает событие; `:` — комментарий;
 * несколько `data:` склеиваются через `\n`; один пробел после двоеточия отбрасывается.
 */
export class SseParser {
  private buf = "";
  private event = "";
  private data: string[] = [];
  private hasData = false;

  push(chunk: string): RawEvent[] {
    this.buf += chunk;
    const out: RawEvent[] = [];
    for (;;) {
      const m = /\r\n|\n|\r/.exec(this.buf);
      if (!m) break;
      // `\r` в самом конце куска: возможно, следующий кусок начнётся с `\n` — ждём его.
      if (m[0] === "\r" && m.index === this.buf.length - 1) break;
      const line = this.buf.slice(0, m.index);
      this.buf = this.buf.slice(m.index + m[0].length);
      const ev = this.line(line);
      if (ev) out.push(ev);
    }
    return out;
  }

  /** Поток закончился. Недописанное событие (без пустой строки в конце) по стандарту отбрасывается. */
  end(): RawEvent[] {
    const out: RawEvent[] = [];
    if (this.buf.endsWith("\r")) {
      const ev = this.line(this.buf.slice(0, -1));
      this.buf = "";
      if (ev) out.push(ev);
    }
    return out;
  }

  private line(line: string): RawEvent | null {
    if (line === "") {
      const ev = this.hasData ? { event: this.event || "message", data: this.data.join("\n") } : null;
      this.event = "";
      this.data = [];
      this.hasData = false;
      return ev;
    }
    if (line.startsWith(":")) return null;
    const i = line.indexOf(":");
    const field = i < 0 ? line : line.slice(0, i);
    let value = i < 0 ? "" : line.slice(i + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") this.event = value;
    else if (field === "data") {
      this.data.push(value);
      this.hasData = true;
    }
    // `id`, `retry` и прочие поля контракт не использует — пропускаются.
    return null;
  }
}

/** Кризисное событие после проверки: контакты — только записи по схеме, без повторов (112 добавит экран). */
export interface CrisisEvent {
  messageId: string | null;
  /** Итоговый ответ протокола; `null` — событие пришло без текста (клиент покажет свой текст и контакты). */
  text: string | null;
  replace: boolean;
  contacts: HelpContact[];
}

export type StreamEvent =
  | { event: "delta"; text: string }
  | { event: "done"; data: ChatStreamDone }
  | { event: "crisis"; data: CrisisEvent }
  | { event: "error"; code: ChatErrorCode };

const ERROR_CODES: ChatErrorCode[] = ["refusal", "unavailable", "quota", "bad_output", "internal"];

/** Вид события не по контракту (`delta`/`done`): поток считается испорченным. */
export class BadEvent extends Error {}

function parseJson(s: string): unknown {
  try {
    return JSON.parse(s);
  } catch {
    return undefined;
  }
}

function contactsOf(v: unknown, isContact: (c: unknown) => c is HelpContact): HelpContact[] {
  return Array.isArray(v) ? v.filter(isContact) : [];
}

/**
 * Событие по контракту. `delta` и `done` не того вида — `BadEvent` (испорченный поток). Кризис принимается
 * всегда, даже испорченный: помощь показывается при любом виде события (ТЗ §9.3). `error` не того вида или с
 * незнакомым кодом — `internal` (контракт: «незнакомый код клиент показывает как internal»).
 * Незнакомое имя события — `null` (пропускается).
 */
export function parseEvent(raw: RawEvent, isContact: (c: unknown) => c is HelpContact): StreamEvent | null {
  const data = parseJson(raw.data);
  switch (raw.event) {
    case "delta":
      if (!isObject(data) || !nonEmpty(data.text)) throw new BadEvent();
      return { event: "delta", text: data.text };
    case "done":
      if (!isObject(data) || !nonEmpty(data.message_id) || typeof data.suggest_draft !== "boolean") throw new BadEvent();
      return { event: "done", data: { message_id: data.message_id, suggest_draft: data.suggest_draft } };
    case "crisis": {
      const d = isObject(data) ? data : {};
      return {
        event: "crisis",
        data: {
          messageId: nonEmpty(d.message_id) ? d.message_id : null,
          text: nonEmpty(d.text) && d.text.trim().length > 0 ? d.text : null,
          // Вид поля не тот — показанное заменяется целиком: показать лишнее хуже, чем показать ответ протокола.
          replace: d.replace !== false,
          contacts: contactsOf(d.contacts, isContact),
        },
      };
    }
    case "error": {
      const code = isObject(data) && isStr(data.code) && (ERROR_CODES as string[]).includes(data.code) ? data.code : "internal";
      return { event: "error", code: code as ChatErrorCode };
    }
    default:
      return null;
  }
}
