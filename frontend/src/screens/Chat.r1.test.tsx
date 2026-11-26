/**
 * A4, задача 2.2, исправления раунда 1: прокрутка ленты (A8-2.2-D4), строка о помощи под ошибкой ответа
 * (MAJOR-3, АР-34 п.2), текст лимита (MINOR-1), срок лимита (A8-2.2-D1). Тексты выдуманные.
 */
import { act, screen, waitFor, within } from "@testing-library/react";
import { limitUntilText } from "../api/messages";
import { FakeServer, crisis, delta, done, errorEvent, gate } from "../test/fakeApi";
import { heading, renderApp, type Rendered } from "../test/renderApp";

function pairedServer(): FakeServer {
  const server = new FakeServer().onboarded().pendingCouple();
  server.partnerAccepts();
  return server;
}

async function openChat(server = pairedServer()): Promise<Rendered> {
  const r = renderApp({ server });
  await heading("Вы пара");
  await r.pressMain();
  await heading("Твой агент");
  await screen.findByRole("textbox", { name: "Сообщение агенту" });
  await waitFor(() => expect(screen.queryByText("Загружаем переписку…")).toBeNull());
  return r;
}

const input = () => screen.getByRole("textbox", { name: "Сообщение агенту" }) as HTMLTextAreaElement;
const sendBtn = () => screen.getByRole("button", { name: "Отправить агенту" });

async function say(r: Rendered, text: string) {
  await r.user.clear(input());
  await r.user.type(input(), text);
  await r.user.click(sendBtn());
}

describe("Прокрутка ленты (A8-2.2-D4; ТЗ §8, §9.3)", () => {
  /** Записанные вызовы scrollIntoView: элемент и выравнивание. */
  let calls: { el: Element; block?: string }[];
  /** Высоты, которые «раскладка» отдаёт элементам по селектору (в jsdom раскладки нет). */
  let heights: Record<string, number>;
  let scrollY: number;
  const origRect = Element.prototype.getBoundingClientRect;

  beforeEach(() => {
    calls = [];
    heights = { ".chat-bottom": 120 };
    scrollY = 0;
    Element.prototype.scrollIntoView = function (this: Element, arg?: boolean | ScrollIntoViewOptions) {
      calls.push({ el: this, block: typeof arg === "object" ? arg.block : undefined });
    };
    Element.prototype.getBoundingClientRect = function (this: Element) {
      const sel = Object.keys(heights).find((s) => this.matches(s));
      const h = sel ? heights[sel] : 0;
      return { x: 0, y: 0, top: 0, left: 0, right: 0, bottom: h, width: 0, height: h, toJSON: () => ({}) } as DOMRect;
    };
    Object.defineProperty(document.documentElement, "scrollHeight", { configurable: true, get: () => 3000 });
    Object.defineProperty(window, "scrollY", { configurable: true, get: () => scrollY });
    Object.defineProperty(window, "innerHeight", { configurable: true, value: 700 });
  });

  afterEach(() => {
    delete (Element.prototype as { scrollIntoView?: unknown }).scrollIntoView;
    Element.prototype.getBoundingClientRect = origRect;
    delete (document.documentElement as { scrollHeight?: number }).scrollHeight;
    delete (window as { scrollY?: number }).scrollY;
  });

  /** Человек сам прокрутил ленту: вверх (читать) или к самому низу. */
  const scrollTo = (y: number) =>
    act(() => {
      scrollY = y;
      window.dispatchEvent(new Event("scroll"));
    });
  const toEndCalls = () => calls.filter((c) => c.el.classList.contains("chat-end"));
  const newBtn = () => screen.queryByRole("button", { name: "К новым" });

  test("при открытии лента у конца; отступ прокрутки — по высоте закреплённого низа", async () => {
    await openChat(pairedServer().seedChat(10));
    expect(toEndCalls().length).toBeGreaterThan(0);
    expect(toEndCalls().at(-1)?.block).toBe("end");
    const screenEl = document.querySelector("[data-screen=chat]") as HTMLElement;
    expect(screenEl.style.getPropertyValue("--chat-bottom-h")).toBe("120px");
  });

  test("своя отправка прокручивает к концу, даже если человек читал выше", async () => {
    const server = pairedServer().seedChat(10);
    const g = gate();
    server.reply({ steps: [g.step, delta("Слышу тебя."), done()] });
    const r = await openChat(server);
    await scrollTo(0);
    calls.length = 0;
    await say(r, "Новая реплика");
    expect(toEndCalls().length).toBeGreaterThan(0);
    await act(async () => g.open());
    await screen.findByText("Слышу тебя.");
  });

  test("человек прокрутил вверх посреди ответа — не дёргаем, «К новым» возвращает к концу", async () => {
    const server = pairedServer().seedChat(10);
    const g = gate();
    server.reply({ steps: [delta("Начало"), g.step, delta(" и конец."), done()] });
    const r = await openChat(server);
    await scrollTo(2300); // у низа: 3000 − 700
    await say(r, "Новая реплика");
    await screen.findByText("Начало");
    expect(newBtn()).toBeNull();
    await scrollTo(400);
    calls.length = 0;
    await act(async () => g.open());
    await screen.findByText("Начало и конец.");
    expect(calls).toHaveLength(0);
    const btn = await screen.findByRole("button", { name: "К новым" });
    expect(btn.closest(".chat-bottom")).not.toBeNull();
    await r.user.click(btn);
    expect(toEndCalls().length).toBeGreaterThan(0);
    expect(newBtn()).toBeNull();
  });

  test("человек у низа — лента следует за ответом без кнопки", async () => {
    const server = pairedServer().seedChat(10);
    const g = gate();
    server.reply({ steps: [delta("Начало"), g.step, delta(" и конец."), done()] });
    const r = await openChat(server);
    await say(r, "Новая реплика");
    await screen.findByText("Начало");
    await scrollTo(2290);
    calls.length = 0;
    await act(async () => g.open());
    await screen.findByText("Начало и конец.");
    expect(toEndCalls().length).toBeGreaterThan(0);
    expect(newBtn()).toBeNull();
  });

  test("кризис — блок «Помощь рядом» в зоне видимости, даже если человек ушёл читать выше", async () => {
    const server = pairedServer().seedChat(10);
    const g = gate();
    server.reply({ steps: [g.step, crisis("Ответ протокола.")] });
    const r = await openChat(server);
    await say(r, "Мне не хочется жить");
    await scrollTo(0);
    calls.length = 0;
    await act(async () => g.open());
    await screen.findByRole("region", { name: "Контакты помощи" });
    // Ответ с блоком помещается над полем ввода — к концу ленты.
    expect(toEndCalls().length).toBeGreaterThan(0);
  });

  test("кризисный ответ выше окна — прокрутка к началу блока «Помощь рядом» (номера и 112 видны)", async () => {
    const server = pairedServer().seedChat(10);
    const g = gate();
    server.reply({ steps: [g.step, crisis("Очень длинный ответ протокола.")] });
    const r = await openChat(server);
    await say(r, "Мне не хочется жить");
    heights["li.msg--agent"] = 900; // больше 700 − 120 − зазоры
    calls.length = 0;
    await act(async () => g.open());
    const block = await screen.findByRole("region", { name: "Контакты помощи" });
    const last = calls.at(-1);
    expect(last?.el).toBe(block);
    expect(last?.block).toBe("start");
    expect(within(block).getByRole("link", { name: "112" })).toHaveAttribute("href", "tel:112");
  });

  test("подгрузка более ранних сверху не прокручивает к концу и не показывает «К новым»", async () => {
    const r = await openChat(pairedServer().seedChat(40));
    await scrollTo(0);
    calls.length = 0;
    await r.user.click(screen.getByRole("button", { name: "Показать более ранние сообщения" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Показать более ранние сообщения" })).toBeNull());
    expect(toEndCalls()).toHaveLength(0);
    expect(newBtn()).toBeNull();
  });
});

describe("Строка о помощи под ошибкой ответа (MAJOR-3, АР-34 п.2; ТЗ §9.3)", () => {
  test.each(["refusal", "internal", "unavailable", "неизвестный_код"])("error «%s» — 112 ссылкой и «Все контакты помощи»", async (code) => {
    const server = pairedServer();
    server.reply({ steps: [errorEvent(code)] });
    const r = await openChat(server);
    await say(r, "Мне плохо");
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/Если тебе сейчас опасно или очень тяжело — звони 112\./);
    expect(within(alert).getByRole("link", { name: "112" })).toHaveAttribute("href", "tel:112");
    expect(alert).not.toHaveTextContent(/сказать иначе/);
    await r.user.click(within(alert).getByRole("button", { name: "Все контакты помощи" }));
    await heading("Помощь");
  });

  test("refusal — без совета «скажи иначе», повтор есть", async () => {
    const server = pairedServer();
    server.reply({ steps: [errorEvent("refusal")] });
    const r = await openChat(server);
    await say(r, "Мне плохо");
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Агент не смог ответить на это сообщение. Можно повторить.");
    expect(within(alert).getByRole("button", { name: "Повторить" })).toBeInTheDocument();
  });
});

describe("Текст лимита 429 (MINOR-1, A8-2.2-D1)", () => {
  test("не обещает обычного ответа; 112 ссылкой прямо в уведомлении", async () => {
    const server = pairedServer();
    server.reply({ status: 429, body: { code: "rate_limited", message: "x" }, headers: { "Retry-After": "3600" } });
    const r = await openChat(server);
    await say(r, "Раз");
    const box = (await screen.findByText(/лимит сообщений агенту исчерпан/)).closest(".note") as HTMLElement;
    expect(box).toHaveTextContent(/всё равно напиши: на такое сообщение тебе ответят и сверх лимита и подскажут, куда обратиться/);
    expect(box).not.toHaveTextContent(/агент ответит и сверх лимита/);
    expect(within(box).getByRole("link", { name: "112" })).toHaveAttribute("href", "tel:112");
  });

  test("срок дальше года — дата с годом; за пределами дат и не число — «позже»", () => {
    const now = new Date(2026, 10, 26, 12, 0, 0).getTime();
    expect(limitUntilText(400 * 86_400, now)).toBe("31 декабря 2027 г. после 12:00");
    expect(limitUntilText(99_999_999_999_999, now)).toBe("позже");
    expect(limitUntilText(Number.NaN, now)).toBe("позже");
    expect(limitUntilText(Number.POSITIVE_INFINITY, now)).toBe("позже");
    // До года — как раньше, без года.
    expect(limitUntilText(3 * 86_400, now)).toBe("29 ноября после 12:00 (через 72 ч)");
  });
});
