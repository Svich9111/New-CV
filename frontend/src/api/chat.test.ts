/**
 * A4, задача 2.2 — разбор потока SSE и строгие проверки вида ответов чата по контракту (`api/openapi.yaml`,
 * `/chat/messages`, АР-32). Тексты выдуманные.
 */
import { isHelpContact } from "./client";
import { BadEvent, SseParser, isChatHistory, isChatMessage, parseEvent } from "./chat";

const ev = (event: string, data: unknown) => ({ event, data: typeof data === "string" ? data : JSON.stringify(data) });

describe("SseParser — события по стандарту SSE", () => {
  test("целиком: event + data + пустая строка; комментарии пропускаются", () => {
    const p = new SseParser();
    const out = p.push(': ping\n\nevent: delta\ndata: {"text":"При"}\n\nevent: done\ndata: {"message_id":"m","suggest_draft":false}\n\n');
    expect(out).toEqual([
      { event: "delta", data: '{"text":"При"}' },
      { event: "done", data: '{"message_id":"m","suggest_draft":false}' },
    ]);
  });

  test("по одному символу — тот же результат", () => {
    const src = 'event: delta\ndata: {"text":"Привет"}\n\n: keep-alive\n\nevent: done\ndata: {"message_id":"m","suggest_draft":true}\n\n';
    const p = new SseParser();
    const out = [...src].flatMap((ch) => p.push(ch));
    expect(out.map((e) => e.event)).toEqual(["delta", "done"]);
    expect(JSON.parse(out[0].data)).toEqual({ text: "Привет" });
  });

  test("CRLF и одиночный CR; граница куска между \\r и \\n — одно событие, без пустого", () => {
    const p = new SseParser();
    const a = p.push("event: delta\r");
    const b = p.push('\ndata: {"text":"a"}\r\n\r');
    const c = p.push("\n");
    expect([...a, ...b, ...c]).toEqual([{ event: "delta", data: '{"text":"a"}' }]);
    const q = new SseParser();
    // Последний `\r` ждёт возможного `\n` следующего куска; конец потока его дочитывает.
    expect(q.push('event: delta\rdata: {"text":"b"}\r\r')).toEqual([]);
    expect(q.end()).toEqual([{ event: "delta", data: '{"text":"b"}' }]);
  });

  test("несколько data склеиваются через \\n; пробел после двоеточия — один", () => {
    const p = new SseParser();
    expect(p.push("event: x\ndata:  a\ndata:b\n\n")).toEqual([{ event: "x", data: " a\nb" }]);
  });

  test("недописанное событие в конце потока отбрасывается", () => {
    const p = new SseParser();
    expect(p.push('event: done\ndata: {"message_id":"m","suggest_draft":false}\n')).toEqual([]);
    expect(p.end()).toEqual([]);
  });

  test("поля id и retry пропускаются; событие без data не отдаётся", () => {
    const p = new SseParser();
    expect(p.push("id: 1\nretry: 10\nevent: delta\n\n")).toEqual([]);
  });
});

describe("parseEvent — строгая проверка событий", () => {
  test("delta: непустой text; иначе — испорченный поток", () => {
    expect(parseEvent(ev("delta", { text: "ok" }), isHelpContact)).toEqual({ event: "delta", text: "ok" });
    for (const bad of [{ text: "" }, { text: 1 }, {}, "не json", [1]]) {
      expect(() => parseEvent(ev("delta", bad), isHelpContact)).toThrow(BadEvent);
    }
  });

  test("done: message_id строкой и suggest_draft boolean", () => {
    expect(parseEvent(ev("done", { message_id: "m", suggest_draft: false }), isHelpContact)?.event).toBe("done");
    for (const bad of [{ message_id: "m" }, { message_id: 1, suggest_draft: false }, { message_id: "", suggest_draft: true }]) {
      expect(() => parseEvent(ev("done", bad), isHelpContact)).toThrow(BadEvent);
    }
  });

  test("crisis принимается всегда: испорченные контакты отброшены, без текста — text null, replace не boolean — замена", () => {
    const good = { name: "Линия", phone: "8 800 000-00-00", description: "Выдуманная" };
    const r = parseEvent(ev("crisis", { message_id: null, text: "Ответ протокола", replace: false, contacts: [good, { name: 1 }] }), isHelpContact);
    expect(r).toEqual({ event: "crisis", data: { messageId: null, text: "Ответ протокола", replace: false, contacts: [good] } });
    const broken = parseEvent(ev("crisis", "не json"), isHelpContact);
    expect(broken).toEqual({ event: "crisis", data: { messageId: null, text: null, replace: true, contacts: [] } });
    const blank = parseEvent(ev("crisis", { message_id: "m", text: "   ", replace: "no", contacts: "x" }), isHelpContact);
    expect(blank).toEqual({ event: "crisis", data: { messageId: "m", text: null, replace: true, contacts: [] } });
  });

  test("error: незнакомый код и испорченное тело — internal (контракт ChatErrorCode)", () => {
    expect(parseEvent(ev("error", { code: "refusal", message: "x" }), isHelpContact)).toEqual({ event: "error", code: "refusal" });
    expect(parseEvent(ev("error", { code: "новый_код", message: "x" }), isHelpContact)).toEqual({ event: "error", code: "internal" });
    expect(parseEvent(ev("error", "]]"), isHelpContact)).toEqual({ event: "error", code: "internal" });
  });

  test("незнакомое имя события пропускается", () => {
    expect(parseEvent(ev("message", { a: 1 }), isHelpContact)).toBeNull();
  });
});

describe("isChatMessage / isChatHistory — вид истории по контракту", () => {
  const msg = {
    id: "00000000-0000-4000-9000-000000000001",
    author: "owner",
    text: "Привет",
    created_at: "2026-11-20T10:00:00Z",
    client_message_id: null,
    reply_to: null,
    crisis: false,
  };
  const agent = { ...msg, id: "00000000-0000-4000-9000-000000000002", author: "agent" };

  test("граничные корректные: UnavailableText, next_cursor null и строка, пустой список", () => {
    expect(isChatMessage(msg)).toBe(true);
    for (const reason of ["format", "unknown_key", "auth", "encoding", "context"]) {
      expect(isChatMessage({ ...agent, text: { unavailable: true, reason } })).toBe(true);
    }
    expect(isChatHistory({ items: [], next_cursor: null })).toBe(true);
    expect(isChatHistory({ items: [msg], next_cursor: "c.1.0" })).toBe(true);
  });

  test("неверные реплики и страницы", () => {
    const bad = [
      { ...msg, id: "" },
      { ...msg, id: 1 },
      { ...msg, author: "partner" },
      { ...msg, text: null },
      { ...msg, text: { unavailable: false, reason: "format" } },
      { ...msg, text: { unavailable: true, reason: "other" } },
      { ...msg, created_at: "вчера" },
      { ...msg, created_at: undefined },
    ];
    for (const m of bad) expect(isChatMessage(m), JSON.stringify(m)).toBe(false);
    expect(isChatHistory({ items: [msg] })).toBe(false);
    expect(isChatHistory({ items: [msg, { ...msg, crisis: undefined }], next_cursor: null })).toBe(false);
    expect(isChatHistory({ items: [msg], next_cursor: "" })).toBe(false);
    expect(isChatHistory({ items: [msg], next_cursor: 5 })).toBe(false);
    expect(isChatHistory({ items: {}, next_cursor: null })).toBe(false);
    expect(isChatHistory([msg])).toBe(false);
  });
});

describe("ChatMessage — связь реплик (АР-51)", () => {
  const base = { id: "00000000-0000-4000-9000-000000000001", text: "Привет", created_at: "2026-11-20T10:00:00Z" };
  const KEY = "5f0c1e8a-3b7d-4c2e-9a1f-0d4b6e8c2a71";
  const owner = { ...base, author: "owner", client_message_id: KEY, reply_to: null, crisis: false };
  const agent = { ...base, author: "agent", client_message_id: null, reply_to: base.id, crisis: true };

  test("корректные: ключ у реплики владельца (и null — без ключа), связь и кризис у ответа (и null/false — до 0018)", () => {
    expect(isChatMessage(owner)).toBe(true);
    expect(isChatMessage({ ...owner, client_message_id: null })).toBe(true);
    expect(isChatMessage(agent)).toBe(true);
    expect(isChatMessage({ ...agent, reply_to: null, crisis: false })).toBe(true);
  });

  test("новые поля обязательны и проверяются по виду; ключ — только у владельца, связь и кризис — только у агента", () => {
    const bad = [
      { ...owner, client_message_id: undefined },
      { ...owner, client_message_id: "" },
      { ...owner, client_message_id: 7 },
      { ...owner, reply_to: undefined },
      { ...owner, crisis: undefined },
      { ...owner, crisis: "false" },
      { ...owner, reply_to: base.id },
      { ...owner, crisis: true },
      { ...agent, reply_to: "" },
      { ...agent, reply_to: 1 },
      { ...agent, client_message_id: KEY },
      { ...agent, crisis: 1 },
    ];
    for (const m of bad) expect(isChatMessage(m), JSON.stringify(m)).toBe(false);
  });
});

describe("parseEvent — accepted (АР-51)", () => {
  const KEY = "5f0c1e8a-3b7d-4c2e-9a1f-0d4b6e8c2a71";
  const acc = (data: unknown) => parseEvent(ev("accepted", data), isHelpContact);

  test("корректное: id реплики или null (сохранить не удалось), ключ, replayed", () => {
    expect(acc({ message_id: "m1", client_message_id: KEY, replayed: false })).toEqual({
      event: "accepted",
      data: { messageId: "m1", clientMessageId: KEY, replayed: false },
    });
    expect(acc({ message_id: null, client_message_id: KEY, replayed: true })).toEqual({
      event: "accepted",
      data: { messageId: null, clientMessageId: KEY, replayed: true },
    });
  });

  test("не того вида — поток испорчен (BadEvent)", () => {
    for (const d of [
      { client_message_id: KEY, replayed: false },
      { message_id: "", client_message_id: KEY, replayed: false },
      { message_id: "m1", replayed: false },
      { message_id: "m1", client_message_id: "", replayed: false },
      { message_id: "m1", client_message_id: KEY },
      { message_id: "m1", client_message_id: KEY, replayed: "true" },
      "{не json",
      [],
    ]) {
      expect(() => acc(d), JSON.stringify(d)).toThrow(BadEvent);
    }
  });
});
