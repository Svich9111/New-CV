/**
 * A8, задача 2.2, раунд 1 — экран чата: негативные сценарии на заглушке API по контракту (ФТ-05; ТЗ §2 п.2,
 * п.4, п.7, §8, §9.1, §9.3, §10). Двойная отправка, «Остановить» в разные моменты, XSS в ответе и в истории,
 * приватность (консоль — страж в setup.ts, хранилища, cookie, адрес, Telegram), aria-live без кусков, уход в
 * «Помощь» в разные моменты, кризис с контактами сразу. Тексты выдуманные.
 */
import { act, screen, waitFor, within } from "@testing-library/react";
import { CONTACTS, FakeServer, crisis, delta, done, gate } from "../test/fakeApi";
import { heading, renderApp, type Rendered } from "../test/renderApp";

const SECRET = "Секрет-Зинаида-42";

function pairedServer(): FakeServer {
  const server = new FakeServer().onboarded().pendingCouple();
  server.partnerAccepts();
  return server;
}

/** `POST /chat/messages` ждёт `open()` до ответа сервера (до заголовков — фаза «waiting»). */
function holdPost(server: FakeServer) {
  const base = server.fetch;
  let open = () => undefined as void;
  const p = new Promise<void>((r) => (open = r));
  let used = false;
  server.fetch = async (input, init = {}) => {
    if (!used && init.method === "POST" && String(input).endsWith("/chat/messages")) {
      used = true;
      // Как настоящий fetch: отмена до ответа рвёт ожидание.
      await Promise.race([
        p,
        new Promise<void>((_r, rej) => init.signal?.addEventListener("abort", () => rej(new DOMException("aborted", "AbortError")))),
      ]);
    }
    return base(input, init);
  };
  return { open };
}

async function openChat(server = pairedServer()): Promise<Rendered> {
  const r = renderApp({ server });
  await heading("Вы пара");
  await r.pressMain();
  await heading("Твой агент");
  await waitFor(() => expect(screen.queryByText("Загружаем переписку…")).toBeNull());
  return r;
}

const input = () => screen.getByRole("textbox", { name: "Сообщение агенту" }) as HTMLTextAreaElement;
const sendBtn = () => screen.getByRole("button", { name: "Отправить агенту" });
const stopBtn = () => screen.getByRole("button", { name: "Остановить" });
const list = () => screen.getByRole("list", { name: "Переписка с агентом" });
const bubbles = () =>
  [...list().querySelectorAll<HTMLElement>(":scope > li")].filter((li) => !li.hasAttribute("data-typing") && !li.hasAttribute("data-checking"));
const texts = () => bubbles().map((li) => `${li.dataset.author}:${li.querySelector(".msg__text")?.textContent}`);
const live = () => document.querySelector("[data-live]") as HTMLElement;
const posts = (r: Rendered) => r.server.calls("POST", "/chat/messages");
const helpBtn = () => screen.getByRole("button", { name: "Помощь и контакты служб поддержки" });

async function say(r: Rendered, text: string) {
  await r.user.clear(input());
  await r.user.type(input(), text);
  await r.user.click(sendBtn());
}

/** Все значения области aria-live за время теста (как их получит экранный диктор). */
function recordLive(): string[] {
  const seen: string[] = [];
  const el = live();
  seen.push(el.textContent ?? "");
  new MutationObserver(() => seen.push(el.textContent ?? "")).observe(el, { childList: true, characterData: true, subtree: true });
  return seen;
}

describe("A8 2.2 — двойная отправка (карточка: одна реплика)", () => {
  test("Enter дважды подряд и клик «Отправить» сразу после Enter — одна реплика", async () => {
    const server = pairedServer();
    const g = gate();
    server.reply({ steps: [g.step, delta("Ок"), done()] });
    const r = await openChat(server);
    await r.user.type(input(), "Один раз");
    await r.user.keyboard("{Enter}{Enter}");
    // На месте «Отправить» теперь «Остановить»; второе касание в пределах DOUBLE_TAP_MS ответ не останавливает.
    await r.user.click(stopBtn());
    expect(posts(r)).toHaveLength(1);
    await act(async () => g.open());
    await waitFor(() => expect(texts()).toEqual(["owner:Один раз", "agent:Ок"]));
    expect(server.streamsAborted).toBe(0);
  });

  test("Enter во время ответа — реплика не уходит и остаётся в поле; после ответа — уходит одна", async () => {
    const server = pairedServer();
    const g = gate();
    server.reply({ steps: [delta("Нач"), g.step, done()] });
    const r = await openChat(server);
    await say(r, "Первая");
    await waitFor(() => expect(texts()).toContain("agent:Нач"));
    await r.user.type(input(), "Вторая{Enter}");
    expect(posts(r)).toHaveLength(1);
    expect(input().value).toBe("Вторая");
    await act(async () => g.open());
    await waitFor(() => expect(sendBtn()).toBeEnabled());
    await r.user.keyboard("{Enter}");
    await waitFor(() => expect(posts(r)).toHaveLength(2));
    // АР-51: в теле, кроме текста, — ключ новой реплики (свой, не ключ первой).
    expect(posts(r)[1].body).toEqual({ text: "Вторая", client_message_id: expect.any(String) });
    expect((posts(r)[1].body as { client_message_id: string }).client_message_id).not.toBe(
      (posts(r)[0].body as { client_message_id: string }).client_message_id,
    );
  });

  test("Enter при наборе IME (isComposing) не отправляет", async () => {
    const r = await openChat();
    await r.user.type(input(), "Привет");
    await act(async () => {
      input().dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, isComposing: true }));
    });
    expect(posts(r)).toHaveLength(0);
  });
});

describe("A8 2.2 — «Остановить» в разные моменты", () => {
  test("до ответа сервера (фаза «Агент пишет…»): запрос отменён, сверка с историей; сервер не получил — «Не отправлено»", async () => {
    const server = pairedServer();
    holdPost(server);
    const r = await openChat(server);
    await say(r, "Вопрос");
    expect(within(list()).getByText("Агент пишет…")).toBeInTheDocument();
    r.advanceClock(1000);
    await r.user.click(stopBtn());
    expect(await screen.findByRole("alert")).toHaveTextContent(/Ответ остановлен\..*Не отправлено/);
    expect(screen.getByRole("button", { name: "Отправить ещё раз" })).toBeInTheDocument();
    expect(server.chat).toHaveLength(0);
    expect(r.tg!.haptics).toContain("notify:error");
  });

  test("в пределах DOUBLE_TAP_MS после отправки «Остановить» не срабатывает; через секунду — срабатывает", async () => {
    const server = pairedServer();
    const g = gate();
    server.reply({ steps: [delta("Кусок"), g.step, delta(" ещё"), done()] });
    const r = await openChat(server);
    await say(r, "Вопрос");
    await waitFor(() => expect(texts()).toContain("agent:Кусок"));
    await r.user.click(stopBtn());
    expect(server.streamsAborted).toBe(0);
    r.advanceClock(1000);
    await r.user.click(stopBtn());
    await waitFor(() => expect(server.streamsAborted).toBe(1));
    expect(await screen.findByText("Ответ остановлен и не сохранён.")).toBeInTheDocument();
    expect(await screen.findByRole("alert")).toHaveTextContent(/Ответ остановлен\..*Сообщение сохранено, ответа нет/);
    await act(async () => g.open());
    expect(texts()).toEqual(["owner:Вопрос", "agent:Кусок"]);
  });

  test("после того как сервер сохранил ответ, но до done: «Остановить» — показан сохранённый ответ, без повтора", async () => {
    const server = pairedServer();
    const g = gate();
    server.reply({ steps: [delta("Почти"), { saveAgent: "Почти готово." }, g.step, done()] });
    const r = await openChat(server);
    await say(r, "Вопрос");
    await waitFor(() => expect(texts()).toContain("agent:Почти"));
    r.advanceClock(1000);
    await r.user.click(stopBtn());
    await waitFor(() => expect(texts()).toEqual(["owner:Вопрос", "agent:Почти готово."]));
    expect(screen.queryByRole("alert")).toBeNull();
    expect(posts(r)).toHaveLength(1);
  });

  test("после done кнопки «Остановить» нет — нечего останавливать", async () => {
    const r = await openChat();
    await say(r, "Вопрос");
    await waitFor(() => expect(texts()).toEqual(["owner:Вопрос", "agent:Слышу тебя."]));
    expect(screen.queryByRole("button", { name: "Остановить" })).toBeNull();
  });
});

describe("A8 2.2 — XSS (карточка: только текст React)", () => {
  const XSS = '<img src=x onerror="window.__xss=1"><script>window.__xss=2</script><a href="javascript:alert(1)">x</a>';

  test("в кусках ответа агента, в истории, в кризисном тексте и в контакте помощи — текст, а не элементы", async () => {
    const server = pairedServer();
    server.addChat("owner", XSS);
    server.addChat("agent", XSS);
    server.reply({ steps: [delta("<b>жирно</b>"), delta(XSS), done()] });
    const r = await openChat(server);
    await say(r, "<i>моя</i>");
    await waitFor(() => expect(texts().at(-1)).toBe(`agent:<b>жирно</b>${XSS}`));
    server.reply({
      steps: [crisis(XSS, { contacts: [{ name: "<img src=x>", phone: "javascript:alert(1)", description: "<script>x</script>" }] })],
    });
    await say(r, "Мне плохо");
    await waitFor(() => expect(texts().at(-1)).toBe(`agent:${XSS}`));
    expect(list().querySelectorAll("img, script, b, i, a[href^='javascript']")).toHaveLength(0);
    expect((window as { __xss?: number }).__xss).toBeUndefined();
    const hrefs = [...list().querySelectorAll("a")].map((a) => a.getAttribute("href"));
    expect(hrefs.every((h) => /^tel:[\d+]*$/.test(h ?? ""))).toBe(true);
    expect(hrefs).toContain("tel:112");
  });
});

describe("A8 2.2 — приватность (ТЗ §9.1, принцип 4)", () => {
  test("тексты реплик и ответов не попадают в хранилища, cookie, адрес, history.state, window.name и Telegram", async () => {
    const server = pairedServer();
    server.addChat("agent", `Из истории ${SECRET}`);
    server.reply({ steps: [delta(`Ответ ${SECRET}`), done()] });
    server.reply({ steps: [delta(`Ответ2 ${SECRET}`)], end: "error" });
    server.reply({ status: 429, body: { code: "rate_limited", message: SECRET }, headers: { "Retry-After": "60" } });
    server.reply({ steps: [crisis(`Протокол ${SECRET}`)] });
    const r = await openChat(server);
    await say(r, `Реплика ${SECRET}`);
    await waitFor(() => expect(texts().at(-1)).toContain("Ответ "));
    await say(r, `Вторая ${SECRET}`);
    await screen.findByRole("button", { name: "Повторить" });
    await say(r, `Третья ${SECRET}`);
    await screen.findByText(/лимит сообщений агенту исчерпан/);
    await say(r, `Четвёртая ${SECRET}`);
    await screen.findByRole("region", { name: "Контакты помощи" });
    await r.user.type(input(), `Недописанное ${SECRET}`);
    await r.user.click(helpBtn());
    await heading("Помощь");
    await r.pressBack();
    await heading("Твой агент");

    const dump = [
      JSON.stringify({ ...localStorage }),
      JSON.stringify({ ...sessionStorage }),
      document.cookie,
      window.location.href,
      JSON.stringify(window.history.state),
      window.name,
      JSON.stringify(r.tg!.opened),
      document.title,
    ].join("\n");
    expect(dump).not.toContain(SECRET);
    expect(dump).not.toContain("Зинаида");
    // Сервер получает реплику только в теле POST, не в адресе.
    expect(r.server.log.every((l) => !l.query.includes("Зинаида") && !decodeURIComponent(l.path).includes("Зинаида"))).toBe(true);
    // Сообщение сервера при 429 (`message`) не выводится — свой текст клиента.
    expect(screen.queryByText(SECRET)).toBeNull();
  });
});

describe("A8 2.2 — aria-live: диктор не читает куски (карточка «Доступность»)", () => {
  test("за весь поток из 5 кусков в области aria-live — только «Агент пишет…» и затем ответ целиком", async () => {
    const server = pairedServer();
    const gs = [gate(), gate(), gate(), gate()];
    server.reply({ steps: [delta("Раз "), gs[0].step, delta("два "), gs[1].step, delta("три "), gs[2].step, delta("четыре "), gs[3].step, delta("пять."), done()] });
    const r = await openChat(server);
    const seen = recordLive();
    await say(r, "Привет");
    for (const g of gs) {
      await act(async () => g.open());
      await act(async () => new Promise((res) => setTimeout(res, 5)));
    }
    await waitFor(() => expect(live().textContent).toBe("Ответ агента: Раз два три четыре пять."));
    const distinct = [...new Set(seen)].filter((s) => s !== "");
    expect(distinct).toEqual(["Агент пишет…", "Ответ агента: Раз два три четыре пять."]);
    // Ни лента, ни пузырь не объявлены живой областью: куски не зачитываются и через предков.
    const b = bubbles()[1];
    expect(b.closest("[aria-live]")).toBeNull();
    expect(list().closest("[role=status],[role=log],[role=alert]")).toBeNull();
  });

  test("обрыв после кусков — куски не объявляются; ошибка — role=alert; диктор не повторяет недописанное", async () => {
    const server = pairedServer();
    server.reply({ steps: [delta("Недопис"), delta("анное")], end: "error" });
    const r = await openChat(server);
    const seen = recordLive();
    await say(r, "Привет");
    await screen.findByRole("alert");
    expect(seen.some((s) => s.includes("Недопис"))).toBe(false);
  });

  test("у поля, «Отправить», «Остановить», «Повторить» — доступные имена; поле связано со счётчиком", async () => {
    const server = pairedServer();
    server.reply({ steps: [delta("x")], end: "error" });
    const r = await openChat(server);
    expect(input()).toHaveAccessibleName("Сообщение агенту");
    expect(sendBtn()).toHaveAccessibleName("Отправить агенту");
    await say(r, "Привет");
    expect(await screen.findByRole("button", { name: "Повторить" })).toBeInTheDocument();
  });
});

describe("A8 2.2 — уход в «Помощь» в разные моменты и возврат", () => {
  test("до первого куска (ожидание сервера): поток продолжается без экрана, «Назад» — ответ целиком, aria-live — итог", async () => {
    const server = pairedServer();
    const post = holdPost(server);
    const r = await openChat(server);
    await say(r, "Мне плохо");
    await r.user.click(helpBtn());
    await heading("Помощь");
    await act(async () => post.open());
    await act(async () => new Promise((res) => setTimeout(res, 20)));
    await r.pressBack();
    await heading("Твой агент");
    await waitFor(() => expect(texts()).toEqual(["owner:Мне плохо", "agent:Слышу тебя."]));
    expect(live().textContent).toBe("Ответ агента: Слышу тебя.");
    expect(server.streamsAborted).toBe(0);
  });

  test("во время сверки после обрыва: сверка завершается, «Назад» — итог сверки на месте", async () => {
    const server = pairedServer();
    server.reply({ steps: [delta("Кус")], end: "error" });
    const base = server.fetch;
    let release = () => undefined as void;
    const slow = new Promise<void>((res) => (release = res));
    let n = 0;
    server.fetch = async (input, init = {}) => {
      if ((init.method ?? "GET") === "GET" && String(input).includes("/chat/messages") && ++n === 2) await slow;
      return base(input, init);
    };
    const r = await openChat(server);
    await say(r, "Вопрос");
    await screen.findByText("Проверяем, дошло ли сообщение…");
    await r.user.click(helpBtn());
    await heading("Помощь");
    await act(async () => release());
    await r.pressBack();
    await heading("Твой агент");
    expect(await screen.findByRole("button", { name: "Повторить" })).toBeInTheDocument();
    expect(texts()).toEqual(["owner:Вопрос", "agent:Кус"]);
  });

  test("из блока кризиса «Все контакты помощи» и обратно — ответ протокола и контакты на месте", async () => {
    const server = pairedServer();
    server.reply({ steps: [crisis("Ответ протокола.")] });
    const r = await openChat(server);
    await say(r, "Мне очень плохо");
    await r.user.click(await screen.findByRole("button", { name: "Все контакты помощи" }));
    await heading("Помощь");
    await r.pressBack();
    await heading("Твой агент");
    const block = screen.getByRole("region", { name: "Контакты помощи" });
    expect(within(block).getByRole("link", { name: "112" })).toHaveAttribute("href", "tel:112");
  });
});

describe("A8 2.2 — кризис: контакты сразу (ТЗ §9.3)", () => {
  test("контакты из события — сразу, без запроса /help/contacts; 112 добавлен, если его нет", async () => {
    const server = pairedServer();
    const other = [{ name: "Телефон доверия", phone: "8 800 000-00-00", description: "Выдуманный номер.", round_the_clock: true }];
    server.reply({ steps: [crisis("Ответ протокола.", { contacts: other })] });
    const r = await openChat(server);
    await say(r, "Мне очень плохо");
    const block = await screen.findByRole("region", { name: "Контакты помощи" });
    const links = within(block).getAllByRole("link").map((a) => a.getAttribute("href"));
    expect(links).toEqual(["tel:88000000000", "tel:112"]);
    expect(server.calls("GET", "/help/contacts")).toHaveLength(0);
    expect(r.tg!.haptics).not.toContain("notify:error");
  });

  test.each([
    ["data не JSON", { raw: "event: crisis\ndata: {oops\n\n" }],
    ["пустой объект", { raw: "event: crisis\ndata: {}\n\n" }],
  ])("испорченное событие crisis (%s) после кусков — куски убраны, свой текст и tel:112", async (_n, step) => {
    const server = pairedServer();
    server.reply({ steps: [delta("Это всё ерунда"), step] });
    const r = await openChat(server);
    await say(r, "Мне очень плохо");
    const block = await screen.findByRole("region", { name: "Контакты помощи" });
    expect(within(block).getByRole("link", { name: "112" })).toHaveAttribute("href", "tel:112");
    expect(screen.queryByText(/ерунда/)).toBeNull();
    expect(live().textContent).not.toMatch(/ерунда/);
    expect(texts().at(-1)).toMatch(/^agent:То, что ты описываешь, звучит серьёзно/);
  });

  test("replace: true — показанные куски нигде не остаются (ни в ленте, ни в aria-live)", async () => {
    const server = pairedServer();
    const g = gate();
    server.reply({ steps: [delta("Мягкая отговорка"), g.step, crisis("Ответ протокола.", { replace: true })] });
    const r = await openChat(server);
    const seen = recordLive();
    await say(r, "Мне очень плохо");
    await waitFor(() => expect(texts()).toContain("agent:Мягкая отговорка"));
    await act(async () => g.open());
    await screen.findByRole("region", { name: "Контакты помощи" });
    expect(document.body.textContent).not.toContain("Мягкая отговорка");
    expect(seen.some((s) => s.includes("Мягкая"))).toBe(false);
  });

  test("кризис без message_id (не сохранён) — контакты всё равно показаны", async () => {
    const server = pairedServer();
    server.reply({ steps: [crisis("Ответ протокола.", { id: null, contacts: CONTACTS })] });
    const r = await openChat(server);
    await say(r, "Мне очень плохо");
    const block = await screen.findByRole("region", { name: "Контакты помощи" });
    expect(within(block).getByRole("link", { name: "112" })).toBeInTheDocument();
  });
});

describe("A8 2.2 — лимит 429 на экране (ТЗ §10)", () => {
  test("без Retry-After — «Агент снова ответит завтра.», ввод не заблокирован, помощь упомянута", async () => {
    const server = pairedServer();
    server.reply({ status: 429, body: { code: "rate_limited", message: "x" } });
    const r = await openChat(server);
    await say(r, "Раз");
    expect(await screen.findByText(/Агент снова ответит завтра\./)).toBeInTheDocument();
    expect(screen.getByText(/контакты помощи доступны всегда/)).toBeInTheDocument();
    expect(input()).toBeEnabled();
    await r.user.type(input(), "Два");
    expect(sendBtn()).toBeEnabled();
  });

  // A8-2.2-D1 на экране: огромный Retry-After — «Агент снова ответит Invalid Date после Invalid Date …».
  test("A8-2.2-D1: Retry-After огромный — на экране нет «Invalid Date»", async () => {
    const server = pairedServer();
    server.reply({ status: 429, body: { code: "rate_limited", message: "x" }, headers: { "Retry-After": "99999999999999" } });
    const r = await openChat(server);
    await say(r, "Раз");
    await screen.findByText(/лимит сообщений агенту исчерпан/);
    expect(document.body.textContent).not.toMatch(/Invalid Date/);
  });
});

describe("A8 2.2 — курсор и 422 на экране (Б-62)", () => {
  /** 2-й запрос истории (по курсору) — 422, 3-й (первая страница заново) — 503, дальше — заглушка. */
  function flaky(server: FakeServer) {
    const base = server.fetch;
    const st = { n: 0 };
    const err = (status: number, code: string) =>
      new Response(JSON.stringify({ code, message: "x" }), { status, headers: { "Content-Type": "application/json" } });
    server.fetch = async (input, init = {}) => {
      if ((init.method ?? "GET") === "GET" && String(input).includes("/chat/messages")) {
        st.n += 1;
        if (st.n === 2) return err(422, "validation_error");
        if (st.n === 3) return err(503, "service_unavailable");
      }
      return base(input, init);
    };
    return st;
  }

  test("422 по курсору, и первая страница заново тоже не грузится — ошибка и повтор; без цикла; прежний курсор не повторён", async () => {
    const server = pairedServer().seedChat(40);
    const st = flaky(server);
    const r = await openChat(server);
    await r.user.click(screen.getByRole("button", { name: "Показать более ранние сообщения" }));
    expect(await screen.findByRole("button", { name: "Загрузить переписку снова" })).toBeInTheDocument();
    await act(async () => new Promise((res) => setTimeout(res, 30)));
    expect(st.n).toBe(3);
    await r.user.click(screen.getByRole("button", { name: "Загрузить переписку снова" }));
    await waitFor(() => expect(bubbles()).toHaveLength(30));
    expect(st.n).toBe(4);
    // До заглушки дошли только запросы без курсора: первая страница и повтор.
    expect(server.calls("GET", "/chat/messages").every((c) => !c.query.includes("cursor="))).toBe(true);
    await act(async () => new Promise((res) => setTimeout(res, 30)));
    expect(st.n).toBe(4);
  });

  // A8-2.2-D3: если перезапуск истории после 422 по курсору сам упал (503), а повтор «Загрузить переписку снова»
  // прошёл — подписи «Переписку пришлось загрузить заново…» нет: `loadFirst(true)` выходит по ошибке до
  // `restarted: true`, а повтор идёт с `restart = false`. Лента молча стала короче (было 40, стало 30).
  // Ожидается подпись (карточка, критерий 1; Б-62).
  test("A8-2.2-D3: после 422 → 503 → повтор подпись «Переписку пришлось загрузить заново» есть", async () => {
    const server = pairedServer().seedChat(40);
    flaky(server);
    const r = await openChat(server);
    await r.user.click(screen.getByRole("button", { name: "Показать более ранние сообщения" }));
    await r.user.click(await screen.findByRole("button", { name: "Загрузить переписку снова" }));
    await waitFor(() => expect(bubbles()).toHaveLength(30));
    expect(screen.getByText(/Переписку пришлось загрузить заново/)).toBeInTheDocument();
  });
});

describe("A8 2.2 — нативные элементы и отклик (ТЗ §8)", () => {
  test("«Отправить» и «Остановить» — impact:light; успешный ответ — без notify:error; сбой — notify:error один раз", async () => {
    const server = pairedServer();
    const g = gate();
    server.reply({ steps: [delta("Кусок"), g.step, done()] });
    const r = await openChat(server);
    r.tg!.haptics.length = 0;
    await say(r, "Раз");
    await waitFor(() => expect(texts()).toContain("agent:Кусок"));
    expect(r.tg!.haptics).toEqual(["impact:light"]);
    await act(async () => g.open());
    await waitFor(() => expect(sendBtn()).toBeInTheDocument());
    expect(r.tg!.haptics).toEqual(["impact:light"]);
    const g2 = gate();
    server.reply({ steps: [delta("Ещё"), g2.step, done()] });
    await say(r, "Два");
    await waitFor(() => expect(texts()).toContain("agent:Ещё"));
    r.advanceClock(1000);
    await r.user.click(stopBtn());
    await screen.findByRole("alert");
    expect(r.tg!.haptics).toEqual(["impact:light", "impact:light", "impact:light", "notify:error"]);
    await act(async () => g2.open());
  });

  test("в чате MainButton скрыта, BackButton видна; «Назад» посреди ответа — ответ не обрывается", async () => {
    const server = pairedServer();
    const g = gate();
    server.reply({ steps: [delta("Нач"), g.step, delta("ало."), done()] });
    const r = await openChat(server);
    expect(r.tg!.main.visible).toBe(false);
    expect(r.tg!.back.visible).toBe(true);
    await say(r, "Раз");
    await waitFor(() => expect(texts()).toContain("agent:Нач"));
    await r.pressBack();
    await heading("Вы пара");
    await act(async () => g.open());
    await r.pressMain();
    await heading("Твой агент");
    await waitFor(() => expect(texts()).toEqual(["owner:Раз", "agent:Начало."]));
    expect(server.streamsAborted).toBe(0);
  });
});

describe("A8 2.2 — новая реплика и кризис видны без ручной прокрутки (ТЗ §8, §9.3)", () => {
  // A8-2.2-D4: лента прокручивается к концу, только если окно уже было в пределах 160 px от низа, причём проверка
  // идёт ПОСЛЕ того, как новый пузырь вырос (useLayoutEffect). Своя реплика, длинный ответ или блок кризиса выше
  // 160 px — и прокрутки нет: в Chromium 375×760 (скрипт a8-2.2/probe.cjs) после «Отправить» реплика, ответ и
  // блок «Помощь рядом» с tel:112 остаются ниже закреплённого поля ввода; к тому же `scrollIntoView` метки
  // конца ставит последний пузырь под закреплённый `.chat-bottom` (sticky). Ожидается: после своей отправки и
  // при кризисе лента прокручивается к новому пузырю (карточка: «контакты помощи … сразу на экране»).
  test("A8-2.2-D4: своя отправка и кризис прокручивают ленту к концу, даже если окно не у самого низа", async () => {
    const server = pairedServer().seedChat(10);
    server.reply({ steps: [crisis("Ответ протокола.")] });
    const r = await openChat(server);
    const calls: Element[] = [];
    // В jsdom у элементов нет scrollIntoView (экран вызывает его через `?.`) — записываем вызовы.
    Element.prototype.scrollIntoView = function (this: Element) {
      calls.push(this);
    };
    // Длинная лента, окно в её начале (как в Chromium после открытия чата с историей).
    Object.defineProperty(document.documentElement, "scrollHeight", { configurable: true, get: () => 3000 });
    try {
      await say(r, "Мне не хочется жить");
      await screen.findByRole("region", { name: "Контакты помощи" });
      expect(calls.length).toBeGreaterThan(0);
    } finally {
      delete (document.documentElement as { scrollHeight?: number }).scrollHeight;
      delete (Element.prototype as { scrollIntoView?: unknown }).scrollIntoView;
    }
  });
});
