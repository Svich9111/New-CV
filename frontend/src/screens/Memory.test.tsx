/**
 * A4, задача 2.5 — экран «Что агент помнит» (ФТ-13, ФТ-14; ТЗ §2 п.4, п.6, §6.3, §8) на заглушке API по контракту.
 * Тексты записей выдуманные.
 */
import { act, screen, waitFor, within } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { FakeServer } from "../test/fakeApi";
import { fakeTelegram } from "../test/fakeTelegram";
import { heading, renderApp, type Rendered } from "../test/renderApp";
import { CONFIRM_TEXT } from "./Memory";

const FACT = "Работает дизайнером, по вторникам часто задерживается";
const OBSERVATION = "Чаще пишет поздно вечером";
const FROM_PARTNER = "Игорь просил предупреждать об отменах заранее";

function pairedServer(): FakeServer {
  const server = new FakeServer().onboarded().pendingCouple();
  server.partnerAccepts();
  return server;
}

async function openMemory(server = pairedServer().seedMemory(), tg = fakeTelegram()): Promise<Rendered> {
  const r = renderApp({ server, tg });
  await heading("Вы пара");
  await r.user.click(screen.getByRole("button", { name: /Что агент помнит/ }));
  await heading("Что агент помнит");
  await waitFor(() => expect(screen.queryByText("Загружаем память…")).toBeNull());
  return r;
}

const row = (text: string) => screen.getByText(text).closest("li") as HTMLElement;
const category = (name: string) => screen.getByRole("heading", { level: 2, name }).closest("[data-category]") as HTMLElement;
const deletes = (r: Rendered) => r.server.log.filter((l) => l.method === "DELETE");
const live = () => document.querySelector("[data-live]") as HTMLElement;
const delBtn = (text: string) => within(row(text)).getByRole("button", { name: /^Удалить запись/ });

async function askDelete(r: Rendered, text: string) {
  await r.user.click(delBtn(text));
  return screen.getByRole("group", { name: CONFIRM_TEXT });
}

async function confirmDelete(r: Rendered, text: string) {
  const group = await askDelete(r, text);
  await r.user.click(within(group).getByRole("button", { name: "Удалить навсегда" }));
}

describe("Вход и «Назад»", () => {
  test("с главного экрана «Вы пара»; MainButton скрыта (красного на экране нет); «Назад» Telegram — на главный", async () => {
    const r = await openMemory();
    expect(r.tg!.main.visible).toBe(false);
    expect(r.tg!.back.visible).toBe(true);
    await r.pressBack();
    expect(await heading("Вы пара")).toBeInTheDocument();
  });

  test("с экрана приглашения (пара ждёт партнёра)", async () => {
    const server = new FakeServer().onboarded().pendingCouple().seedMemory();
    const r = renderApp({ server });
    await screen.findByText(/Приглашение уже создано/);
    await r.user.click(screen.getByRole("button", { name: "Что агент помнит" }));
    await heading("Что агент помнит");
    expect(await screen.findByText(FACT)).toBeInTheDocument();
    await r.pressBack();
    expect(await heading("Пригласи партнёра")).toBeInTheDocument();
  });

  test("без нативной BackButton (Bot API 6.0) — «‹ Назад» в странице", async () => {
    const r = await openMemory(undefined, fakeTelegram({ version: "6.0" }));
    await r.user.click(screen.getByRole("button", { name: "‹ Назад" }));
    expect(await heading("Вы пара")).toBeInTheDocument();
  });

  test("один GET /memory без параметра type; повторный вход на экран — новый запрос (без кэша)", async () => {
    const r = await openMemory();
    expect(r.server.calls("GET", "/memory").map((c) => c.query)).toEqual([""]);
    await r.pressBack();
    await heading("Вы пара");
    await r.user.click(screen.getByRole("button", { name: /Что агент помнит/ }));
    await screen.findByText(FACT);
    expect(r.server.calls("GET", "/memory")).toHaveLength(2);
  });
});

describe("Категории и подписи (ТЗ §6.3, ФТ-14)", () => {
  test("шесть категорий в порядке ТЗ; записи в своих категориях", async () => {
    await openMemory();
    const names = screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent);
    expect(names).toEqual(["Факты", "Важные даты", "Предпочтения", "Чувствительные темы", "Наблюдения агента", "Из сообщений партнёра"]);
    expect(within(category("Факты")).getByText(FACT)).toBeInTheDocument();
    expect(within(category("Наблюдения агента")).getByText(OBSERVATION)).toBeInTheDocument();
    expect(within(category("Из сообщений партнёра")).getByText(FROM_PARTNER)).toBeInTheDocument();
  });

  test("наблюдения агента и записи из сообщений партнёра отделены подписью и значком; слова владельца — без значка", async () => {
    await openMemory();
    expect(within(category("Наблюдения агента")).getByText("Вывод агента, а не твои слова.")).toBeInTheDocument();
    expect(within(category("Из сообщений партнёра")).getByText("Из сообщений, которые партнёр тебе отправил.")).toBeInTheDocument();
    expect(within(row(OBSERVATION)).getByText("вывод агента, а не твои слова")).toHaveClass("badge");
    expect(within(row(OBSERVATION)).getByText(/Источник: вывод агента/)).toBeInTheDocument();
    expect(within(row(FROM_PARTNER)).getByText("от партнёра")).toHaveClass("badge");
    expect(within(row(FROM_PARTNER)).getByText(/Источник: сообщение партнёра/)).toBeInTheDocument();
    expect(row(OBSERVATION)).toHaveClass("mem-item--other");
    expect(row(FACT)).not.toHaveClass("mem-item--other");
    expect(row(FACT).querySelector(".badge")).toBeNull();
    expect(within(row(FACT)).getByText(/Источник: твои слова · 1 ноября/)).toBeInTheDocument();
  });

  test("вывод агента в другой категории (факт) тоже помечен значком — по источнику, а не по типу", async () => {
    const server = pairedServer();
    server.addMemory({ type: "fact", text: "Похоже, работает из дома", source: "agent_inference", confidence: 0.7 });
    await openMemory(server);
    expect(within(row("Похоже, работает из дома")).getByText("вывод агента, а не твои слова")).toBeInTheDocument();
  });

  test("у «Важных дат» — дата события; у остальных записей её нет", async () => {
    await openMemory();
    const date = within(category("Важные даты"));
    expect(date.getByText("Годовщина знакомства")).toBeInTheDocument();
    expect(date.getByText(/^Дата: 14 июня 2027/)).toBeInTheDocument();
    expect(within(row(FACT)).queryByText(/^Дата:/)).toBeNull();
  });

  test("уверенность числом не показывается; вывод агента с уверенностью ниже 0,5 — «агент не уверен»", async () => {
    const server = pairedServer().seedMemory();
    server.addMemory({ type: "observation", text: "Любит прогулки", source: "agent_inference", confidence: 0.5 });
    await openMemory(server);
    expect(within(row(OBSERVATION)).getByText(/агент не уверен/)).toBeInTheDocument();
    expect(within(row("Любит прогулки")).queryByText(/агент не уверен/)).toBeNull();
    expect(document.querySelector("[data-screen=memory]")!.textContent).not.toMatch(/0[.,]\d|\d+\s?%/);
  });

  test("пустая категория — «Пока пусто»", async () => {
    const server = pairedServer();
    server.addMemory({ type: "fact", text: "Одна запись" });
    await openMemory(server);
    expect(within(category("Факты")).queryByText("Пока пусто")).toBeNull();
    expect(within(category("Важные даты")).getByText("Пока пусто")).toBeInTheDocument();
  });

  test("памяти нет — объяснение, откуда берутся записи; категорий нет", async () => {
    await openMemory(pairedServer());
    expect(screen.getByText("Пока агент ничего не запомнил.")).toBeInTheDocument();
    expect(screen.getByText(/Записи появляются после разговоров с агентом/)).toBeInTheDocument();
    expect(screen.queryAllByRole("heading", { level: 2 })).toHaveLength(0);
  });
});

describe("Приватность на экране (ТЗ §8, §2 п.4)", () => {
  test("пояснение «Партнёр и его агент этого не видят» — и при загрузке, и при ошибке", async () => {
    await openMemory();
    expect(screen.getByText("Партнёр и его агент этого не видят.")).toBeInTheDocument();
  });

  test("пояснение видно и когда память не загрузилась", async () => {
    const server = pairedServer().inject("GET", "/memory", 500, { code: "internal", message: "x" });
    await openMemory(server);
    expect(screen.getByText("Партнёр и его агент этого не видят.")).toBeInTheDocument();
  });
});

describe("Удаление: подтверждение и 204", () => {
  test("«Удалить» — подтверждение на месте, запроса нет; «Отмена» — запроса нет, запись на месте", async () => {
    const r = await openMemory();
    const group = await askDelete(r, FACT);
    expect(within(group).getByText(CONFIRM_TEXT)).toBeInTheDocument();
    expect(deletes(r)).toHaveLength(0);
    await r.user.click(within(group).getByRole("button", { name: "Отмена" }));
    expect(screen.queryByRole("group", { name: CONFIRM_TEXT })).toBeNull();
    expect(screen.getByText(FACT)).toBeInTheDocument();
    expect(deletes(r)).toHaveLength(0);
  });

  test("Escape в подтверждении — отмена, запроса нет", async () => {
    const r = await openMemory();
    await askDelete(r, FACT);
    await r.user.keyboard("{Escape}");
    expect(screen.queryByRole("group", { name: CONFIRM_TEXT })).toBeNull();
    expect(deletes(r)).toHaveLength(0);
  });

  test("подтверждено — ровно один DELETE /memory/{id} без тела; запись исчезает; повторный GET её не возвращает", async () => {
    const r = await openMemory();
    const id = r.server.memory.find((m) => m.text === FACT)!.id;
    await confirmDelete(r, FACT);
    await waitFor(() => expect(screen.queryByText(FACT)).toBeNull());
    expect(deletes(r)).toEqual([expect.objectContaining({ method: "DELETE", path: `/memory/${id}`, query: "", body: undefined })]);
    expect(deletes(r)[0].auth).toMatch(/^Bearer /);
    expect(live()).toHaveTextContent("Запись удалена.");
    expect(r.server.deletedMemory.has(id)).toBe(true);
    expect(r.tg!.haptics).toContain("notify:success");
    // Повторный вход на экран: сервер записи не отдаёт.
    await r.pressBack();
    await heading("Вы пара");
    await r.user.click(screen.getByRole("button", { name: /Что агент помнит/ }));
    await screen.findByText(OBSERVATION);
    expect(screen.queryByText(FACT)).toBeNull();
  });

  test("подтверждение текстом не обещает очистку сводок и реплик (АР-37 ждёт оператора)", () => {
    expect(CONFIRM_TEXT).toMatch(/навсегда/);
    expect(CONFIRM_TEXT).not.toMatch(/сводк|реплик|переписк/i);
  });

  test("удалена последняя запись — экран объясняет, что памяти нет", async () => {
    const server = pairedServer();
    server.addMemory({ type: "fact", text: "Единственная" });
    const r = await openMemory(server);
    await confirmDelete(r, "Единственная");
    expect(await screen.findByText("Пока агент ничего не запомнил.")).toBeInTheDocument();
  });

  test("двойное нажатие «Удалить навсегда» — один запрос", async () => {
    const r = await openMemory();
    const group = await askDelete(r, FACT);
    await r.user.dblClick(within(group).getByRole("button", { name: "Удалить навсегда" }));
    await waitFor(() => expect(screen.queryByText(FACT)).toBeNull());
    expect(deletes(r)).toHaveLength(1);
  });

  test("пока запрос идёт — «Удаляем…», повторное нажатие не шлёт второй запрос", async () => {
    const server = pairedServer().seedMemory();
    let release: () => void = () => undefined;
    const held = new Promise<void>((res) => (release = res));
    const fetch = server.fetch;
    server.fetch = async (input, init) => {
      if ((init?.method ?? "GET") === "DELETE") await held;
      return fetch(input, init);
    };
    const r = await openMemory(server);
    await confirmDelete(r, FACT);
    const busy = within(row(FACT)).getByRole("button", { name: /^Удаляем/ });
    expect(busy).toHaveTextContent("Удаляем…");
    expect(busy).toHaveAttribute("aria-disabled", "true");
    await r.user.click(busy);
    expect(screen.queryByRole("group", { name: CONFIRM_TEXT })).toBeNull();
    await act(async () => release());
    await waitFor(() => expect(screen.queryByText(FACT)).toBeNull());
    expect(deletes(r)).toHaveLength(1);
  });
});

describe("Удаление: ошибки", () => {
  test("404 (запись уже удалена с другого устройства) — убрана без ошибки, нейтральная подпись", async () => {
    const r = await openMemory();
    const id = r.server.memory.find((m) => m.text === FACT)!.id;
    r.server.deletedMemory.add(id);
    await confirmDelete(r, FACT);
    await waitFor(() => expect(screen.queryByText(FACT)).toBeNull());
    expect(live()).toHaveTextContent("Эта запись уже была удалена.");
    expect(screen.queryByRole("alert")).toBeNull();
    expect(deletes(r)).toHaveLength(1);
  });

  test("409 conflict_retry один раз — один автоповтор, запись удалена", async () => {
    const server = pairedServer().seedMemory();
    const id = server.memory.find((m) => m.text === FACT)!.id;
    server.inject("DELETE", `/memory/${id}`, 409, { code: "conflict_retry", message: "x" });
    const r = await openMemory(server);
    await confirmDelete(r, FACT);
    await waitFor(() => expect(screen.queryByText(FACT)).toBeNull());
    expect(deletes(r)).toHaveLength(2);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  test("409 дважды — «Не получилось удалить, попробуй ещё раз», запись на месте; «Повторить» удаляет", async () => {
    const server = pairedServer().seedMemory();
    const id = server.memory.find((m) => m.text === FACT)!.id;
    server.inject("DELETE", `/memory/${id}`, 409, { code: "conflict_retry", message: "x" }, undefined, 2);
    const r = await openMemory(server);
    await confirmDelete(r, FACT);
    expect(await within(row(FACT)).findByRole("alert")).toHaveTextContent("Не получилось удалить, попробуй ещё раз.");
    expect(deletes(r)).toHaveLength(2);
    expect(r.tg!.haptics).toContain("notify:error");
    await r.user.click(within(row(FACT)).getByRole("button", { name: /^Повторить удаление: «.+» \(Факты\)$/ }));
    await waitFor(() => expect(screen.queryByText(FACT)).toBeNull());
    expect(deletes(r)).toHaveLength(3);
  });

  test("нет связи — запись на месте, подпись и «Повторить»", async () => {
    const server = pairedServer().seedMemory();
    const id = server.memory.find((m) => m.text === FACT)!.id;
    server.failNetwork("DELETE", `/memory/${id}`);
    const r = await openMemory(server);
    await confirmDelete(r, FACT);
    expect(await within(row(FACT)).findByRole("alert")).toHaveTextContent(/Нет связи с сервером — запись не удалена/);
    expect(server.deletedMemory.has(id)).toBe(false);
    await r.user.click(within(row(FACT)).getByRole("button", { name: /^Повторить удаление/ }));
    await waitFor(() => expect(screen.queryByText(FACT)).toBeNull());
    expect(server.deletedMemory.has(id)).toBe(true);
  });

  test("5xx — запись на месте, подпись нейтральная (без красного), «Повторить»", async () => {
    const server = pairedServer().seedMemory();
    const id = server.memory.find((m) => m.text === FACT)!.id;
    server.inject("DELETE", `/memory/${id}`, 503, { code: "service_unavailable", message: "x" });
    const r = await openMemory(server);
    await confirmDelete(r, FACT);
    const alert = await within(row(FACT)).findByRole("alert");
    expect(alert).toHaveTextContent(/Запись на месте/);
    expect(alert).toHaveClass("note--error");
    expect(within(row(FACT)).getByRole("button", { name: /^Повторить удаление/ })).toBeInTheDocument();
    // «Удалить» снова — с новым подтверждением, подпись ошибки убирается.
    await askDelete(r, FACT);
    expect(within(row(FACT)).queryByRole("alert")).toBeNull();
  });

  test("401 (сессия истекла) — один повторный вход и один повтор, запись удалена", async () => {
    const r = await openMemory();
    r.server.expireSession();
    await confirmDelete(r, FACT);
    await waitFor(() => expect(screen.queryByText(FACT)).toBeNull());
    expect(r.server.calls("POST", "/auth/telegram")).toHaveLength(2);
    expect(deletes(r)).toHaveLength(2);
  });

  test("401 и повторный вход не удался — запись на месте, причина подписью", async () => {
    const r = await openMemory();
    r.server.expireSession();
    r.server.inject("POST", "/auth/telegram", 401, { code: "invalid_init_data", message: "x" });
    await confirmDelete(r, FACT);
    expect(await within(row(FACT)).findByRole("alert")).toHaveTextContent(/Telegram/);
    expect(deletes(r)).toHaveLength(1);
  });
});

describe("Загрузка: ошибки и вид ответа", () => {
  test("UnavailableText — «Текст записи недоступен», запись можно удалить", async () => {
    const r = await openMemory();
    const unavailable = screen.getByText("Текст записи недоступен");
    const li = unavailable.closest("li") as HTMLElement;
    expect(within(li).getByText(/Её можно удалить/)).toBeInTheDocument();
    const btn = within(li).getByRole("button", { name: "Удалить запись: текст записи недоступен (Факты)" });
    const id = li.dataset.memoryId!;
    await r.user.click(btn);
    await r.user.click(screen.getByRole("button", { name: "Удалить навсегда" }));
    await waitFor(() => expect(screen.queryByText("Текст записи недоступен")).toBeNull());
    expect(deletes(r).map((d) => d.path)).toEqual([`/memory/${id}`]);
  });

  test("ответ не по схеме — «Память пришла с ошибкой», ни одной записи; «Попробовать снова» загружает", async () => {
    const server = pairedServer().seedMemory();
    server.inject("GET", "/memory", 200, {
      items: [
        { id: "30000000-0000-4000-8000-000000000001", type: "fact", text: "Чужая запись", source: "owner_dialog", confidence: 1.5, created_at: "2026-11-01T09:00:00Z" },
      ],
    });
    const r = await openMemory(server);
    expect(screen.getByRole("alert")).toHaveTextContent("Память пришла с ошибкой");
    expect(screen.queryByText("Чужая запись")).toBeNull();
    expect(screen.queryAllByRole("listitem")).toHaveLength(0);
    await r.user.click(screen.getByRole("button", { name: "Попробовать снова" }));
    expect(await screen.findByText(FACT)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  test("нет связи при загрузке — подпись и «Попробовать снова»", async () => {
    const server = pairedServer().seedMemory().failNetwork("GET", "/memory");
    const r = await openMemory(server);
    expect(screen.getByRole("alert")).toHaveTextContent(/Нет связи с сервером/);
    await r.user.click(screen.getByRole("button", { name: "Попробовать снова" }));
    expect(await screen.findByText(FACT)).toBeInTheDocument();
  });

  test("память доступна без завершённого онбординга (заглушка как сервер: 403 нет)", async () => {
    const server = new FakeServer().seedMemory();
    const r = renderApp({ server });
    await heading("Твой личный агент");
    const items = await r.api.memoryList();
    expect(items).toHaveLength(7);
  });
});

describe("Тексты записей — только текст (XSS, хранилища, адрес)", () => {
  test("разметка в записи показывается текстом, не исполняется", async () => {
    const evil = '<img src=x onerror="window.__xss=1"><script>window.__xss=2</script>';
    const server = pairedServer();
    server.addMemory({ type: "fact", text: evil });
    await openMemory(server);
    const li = row(evil);
    expect(li.querySelector("img, script")).toBeNull();
    expect(li.querySelector(".mem-item__text")!.textContent).toBe(evil);
    expect((window as { __xss?: number }).__xss).toBeUndefined();
  });

  test("тексты не попадают в хранилища браузера, cookie и адрес; консоль молчит (setup)", async () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    const href = window.location.href;
    const r = await openMemory();
    await confirmDelete(r, FACT);
    await waitFor(() => expect(screen.queryByText(FACT)).toBeNull());
    expect(setItem).not.toHaveBeenCalled();
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
    expect(document.cookie).toBe("");
    expect(window.location.href).toBe(href);
    for (const l of r.server.log) expect(l.path + l.query).not.toMatch(/дизайнер/);
  });
});

describe("Доступность", () => {
  test("у «Удалить» доступное имя с текстом и категорией; список записей подписан категорией", async () => {
    await openMemory();
    expect(delBtn(FACT)).toHaveAccessibleName(`Удалить запись: «${FACT}» (Факты)`);
    expect(delBtn(OBSERVATION)).toHaveAccessibleName(`Удалить запись: «${OBSERVATION}» (Наблюдения агента)`);
    expect(screen.getByRole("list", { name: "Факты" })).toBeInTheDocument();
  });

  test("подтверждение: фокус на «Отмена»; после отмены — снова на «Удалить» этой записи", async () => {
    const r = await openMemory();
    const group = await askDelete(r, FACT);
    expect(within(group).getByRole("button", { name: "Отмена" })).toHaveFocus();
    await r.user.keyboard("{Enter}");
    expect(delBtn(FACT)).toHaveFocus();
  });

  test("после удаления фокус — на «Удалить» соседней записи; итог — aria-live=polite", async () => {
    const r = await openMemory();
    await confirmDelete(r, FACT);
    await waitFor(() => expect(screen.queryByText(FACT)).toBeNull());
    expect(live()).toHaveAttribute("aria-live", "polite");
    expect(document.activeElement).toHaveAccessibleName(/^Удалить запись: текст записи недоступен \(Факты\)$/);
  });

  test("ошибки — role=alert", async () => {
    const server = pairedServer().seedMemory();
    const id = server.memory.find((m) => m.text === FACT)!.id;
    server.inject("DELETE", `/memory/${id}`, 500, { code: "internal", message: "x" });
    const r = await openMemory(server);
    await confirmDelete(r, FACT);
    expect(await within(row(FACT)).findByRole("alert")).toHaveTextContent(/сбой на сервере/);
  });
});

describe("Стиль (ТЗ §8)", () => {
  test("удаление и ошибки экрана памяти — без красного акцента", () => {
    const css = readFileSync(join(__dirname, "..", "app.css"), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
    const mem = [...css.matchAll(/([^{}]+)\{([^}]*)\}/g)].filter((m) => /mem-|confirm|btn--fill|badge/.test(m[1]));
    expect(mem.length).toBeGreaterThan(5);
    for (const m of mem) expect(m[2], m[1]).not.toMatch(/--accent/);
  });
});
