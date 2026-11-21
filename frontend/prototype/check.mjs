// Проверка прототипа 0.6 (Playwright, Chromium). Запуск: node frontend/prototype/check.mjs
// Что проверяется — критерии карточки docs/tasks/0.6.md.
import { createRequire } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, join } from "node:path";
import { mkdirSync, readFileSync } from "node:fs";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const HERE = dirname(fileURLToPath(import.meta.url));
const URL = pathToFileURL(join(HERE, "index.html")).href;
const SHOTS = join(HERE, "screens");
mkdirSync(SHOTS, { recursive: true });

const SCREENS = ["welcome", "onboarding", "invite", "home", "chat", "draft", "messages", "memory", "profile", "subscription", "help"];
const ACCENT = "rgb(225, 29, 46)";

let failed = 0;
let passed = 0;
function ok(cond, name) {
  if (cond) { passed++; console.log("  ✓ " + name); }
  else { failed++; console.log("  ✗ " + name); }
}
async function step(title, fn) {
  console.log("\n" + title);
  try { await fn(); }
  catch (e) { failed++; console.log("  ✗ исключение: " + (e && e.message ? e.message.split("\n")[0] : e)); }
}

const browser = await chromium.launch();

async function newPage(width = 375, colorScheme = "light") {
  const ctx = await browser.newContext({ viewport: { width, height: 812 }, deviceScaleFactor: 1, colorScheme });
  const page = await ctx.newPage();
  page.errors = [];
  page.external = [];
  page.on("pageerror", (e) => page.errors.push(String(e)));
  page.on("console", (m) => { if (m.type() === "error") page.errors.push(m.text()); });
  page.on("request", (r) => { if (!r.url().startsWith("file:") && !r.url().startsWith("data:")) page.external.push(r.url()); });
  await page.goto(URL);
  return page;
}

const H = (page) => ({
  screen: () => page.locator("#content").getAttribute("data-current"),
  main: () => page.locator("#tg-main"),
  clickMain: async () => { await page.locator("#tg-main").click(); },
  persona: async (p) => { await page.click(`[data-persona="${p}"]`); },
  jump: async (s) => { await page.selectOption("#jump", s); },
  back: async () => { await page.click("#tg-back"); },
  noHScroll: () =>
    page.evaluate(() => {
      const d = document.documentElement;
      const c = document.getElementById("content");
      const wide = [...document.querySelectorAll("#phone *")].filter((el) => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.right > d.clientWidth + 0.5;
      });
      return d.scrollWidth <= d.clientWidth && c.scrollWidth <= c.clientWidth && wide.length === 0;
    }),
  agentCount: () => page.locator('#content .msg[data-from="agent"]').count(),
});

async function onboard(page) {
  for (const k of ["rules", "data", "sensitive", "age"]) await page.check(`[data-model="form.${k}"]`);
}

// Все элементы, где виден красный акцент, — только из разрешённого списка (главное действие и близость)
async function accentViolations(page) {
  return page.evaluate((ACCENT) => {
    const allowed = (el) =>
      el.matches("#tg-main:not(:disabled), .heart, .heart *, .dot, .badge--love, .partner-card");
    const bad = [];
    for (const el of document.querySelectorAll("body *")) {
      const cs = getComputedStyle(el);
      const r = el.getBoundingClientRect();
      if (r.width === 0 && r.height === 0) continue;
      const props = [cs.color, cs.backgroundColor, cs.fill, cs.borderTopColor, cs.borderRightColor, cs.borderBottomColor];
      const borderLeft = cs.borderLeftWidth !== "0px" ? cs.borderLeftColor : "";
      const usesBorder = (c, w) => c === ACCENT && w !== "0px";
      const hit =
        (cs.color === ACCENT && el.textContent.trim()) ||
        cs.backgroundColor === ACCENT ||
        (cs.fill === ACCENT && el.closest("svg")) ||
        usesBorder(cs.borderTopColor, cs.borderTopWidth) ||
        usesBorder(cs.borderRightColor, cs.borderRightWidth) ||
        usesBorder(cs.borderBottomColor, cs.borderBottomWidth) ||
        borderLeft === ACCENT;
      void props;
      if (hit && !allowed(el)) bad.push(el.tagName.toLowerCase() + "." + [...el.classList].join("."));
    }
    return bad;
  }, ACCENT);
}

// ---------- 1. Токены совпадают с src/theme.css ----------
await step("1. Палитра: токены прототипа = frontend/src/theme.css (ТЗ §8)", async () => {
  const theme = readFileSync(join(HERE, "../src/theme.css"), "utf8");
  const proto = readFileSync(join(HERE, "styles.css"), "utf8");
  const tokens = (css, sel) => {
    const m = css.match(new RegExp(sel.replace(/[[\]"()]/g, "\\$&") + "\\s*\\{([^}]*)\\}"));
    return m ? Object.fromEntries([...m[1].matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)].map((x) => [x[1], x[2].trim().toLowerCase()])) : {};
  };
  for (const sel of [":root", ':root[data-theme="dark"]']) {
    const a = tokens(theme, sel);
    const b = tokens(proto, sel);
    for (const [k, v] of Object.entries(a)) ok(b[k] === v, `${sel} ${k} = ${v}`);
  }
  ok(/--bg:\s*#ffffff/i.test(proto) && /--text:\s*#111111/i.test(proto) && /--accent:\s*#e11d2e/i.test(proto), "белый фон, #111111, акцент #E11D2E");
});

// ---------- 2. Каждый экран открывается, без горизонтальной прокрутки ----------
for (const width of [360, 375, 430]) {
  await step(`2. Все 11 экранов открываются у обоих партнёров, ширина ${width} px — без горизонтальной прокрутки`, async () => {
    const page = await newPage(width);
    const h = H(page);
    for (const persona of ["anna", "igor"]) {
      await h.persona(persona);
      for (const s of SCREENS) {
        await h.jump(s);
        const cur = await h.screen();
        const vis = await page.locator(`#content [data-screen="${s}"]`).count();
        const noScroll = await h.noHScroll();
        if (width === 375) ok(cur === s && vis === 1 && noScroll, `${persona}: «${s}» открыт, прокрутки по горизонтали нет`);
        else if (!(cur === s && vis === 1 && noScroll)) ok(false, `${persona}: «${s}» на ${width} px`);
      }
    }
    if (width !== 375) ok(true, `все экраны на ${width} px без горизонтальной прокрутки`);
    ok(page.errors.length === 0, "нет ошибок JS" + (page.errors.length ? ": " + page.errors.join("; ") : ""));
    ok(page.external.length === 0, "нет внешних запросов (без CDN)" + (page.external.length ? ": " + page.external.join(", ") : ""));
    await page.context().close();
  });
}

// ---------- 3. Ключевой сценарий §1 кликами ----------
await step("3. Ключевой сценарий «Анна и Игорь» (ТЗ §1, §4.1–4.4) — кликами", async () => {
  const page = await newPage(375);
  const h = H(page);
  const igorIncoming = async () => {
    await h.persona("igor");
    const before = await h.screen();
    let n = 0;
    if (before === "home" || before === "messages") {
      await h.jump("messages");
      n = await page.locator('[data-msg="incoming"]').count();
      await h.back();
    } else {
      n = await page.evaluate(() => document.querySelectorAll('[data-msg="incoming"]').length);
    }
    await h.persona("anna");
    return n;
  };

  // Приветствие: 3 слайда
  ok((await h.screen()) === "welcome", "Анна: старт — «Приветствие»");
  ok((await page.locator("[data-slide]").getAttribute("data-slide")) === "1", "слайд 1 из 3");
  ok(await page.locator("#tg-close").isVisible(), "на первом экране вместо «Назад» — «Закрыть» (как в Telegram)");
  await h.clickMain();
  ok((await page.locator("[data-slide]").getAttribute("data-slide")) === "2", "MainButton «Далее» → слайд 2");
  ok(await page.locator("#tg-back").isVisible(), "BackButton появился");
  await h.back();
  ok((await page.locator("[data-slide]").getAttribute("data-slide")) === "1", "BackButton → слайд 1");
  await h.clickMain(); await h.clickMain();
  ok(/не заменяет психолога/.test(await page.locator("#content").innerText()), "слайд 3: продукт не заменяет психолога (§8, §9.4)");
  ok((await h.main().innerText()) === "Начать", "MainButton «Начать»");
  await h.clickMain();

  // Онбординг
  ok((await h.screen()) === "onboarding", "Онбординг");
  ok(await h.main().isDisabled(), "без согласий MainButton недоступна");
  ok(/отметь все четыре/.test(await page.locator("#tg-main-hint").innerText()), "недоступность пояснена подписью, а не только цветом");
  await onboard(page);
  ok(!(await h.main().isDisabled()), "после согласий и 18+ — доступна");
  await h.clickMain();

  // Приглашение
  ok((await h.screen()) === "invite", "Приглашение: ссылка и статус");
  ok((await page.locator(".link-box").innerText()).includes("t.me/"), "есть ссылка-приглашение");
  ok((await h.main().innerText()) === "Отправить в Telegram", "MainButton «Отправить в Telegram»");
  await h.clickMain();
  ok((await page.locator('[data-status="waiting"]').innerText()).includes("Ждём Игоря"), "статус ожидания подписан словами");
  await page.click('[data-action="to-home"]');
  ok((await h.screen()) === "home", "Главный (до принятия приглашения)");
  ok(await page.locator('[data-pair="pending"]').isVisible(), "статус пары: ждём Игоря");

  // До принятия приглашения отправка недоступна (§4.1 п.5)
  await h.jump("draft");
  ok(await h.main().isDisabled(), "черновик до создания пары: «Отправить» недоступна");
  ok(/примет приглашение/.test(await page.locator("#tg-main-hint").innerText()), "с подписью почему");
  await h.back();

  // Игорь принимает приглашение
  await h.persona("igor");
  ok((await h.screen()) === "invite", "Игорь: входящее приглашение от Ани");
  ok((await h.main().innerText()) === "Принять приглашение", "MainButton «Принять приглашение»");
  await h.clickMain();
  ok((await h.screen()) === "onboarding", "Игорь: онбординг");
  await onboard(page);
  await h.clickMain();
  ok((await h.screen()) === "home", "Игорь: главный, пара создана");
  ok(await page.locator('[data-pair="active"]').isVisible(), "статус «Вы в паре»");
  await h.persona("anna");
  ok(await page.locator('[data-pair="active"]').isVisible(), "Анна тоже видит «Вы в паре с Игорем»");

  // Чат с агентом
  await page.click('[data-action="go"][data-to="chat"]');
  ok((await h.screen()) === "chat", "Анна: чат с агентом");
  ok(!(await h.main().isVisible()), "до первой реплики «Подготовить сообщение» не показана");
  await page.click('[data-action="suggest"]');
  ok((await page.inputValue("#chat-input")) === "Ему вообще плевать на меня", "Анна пишет: «Ему вообще плевать на меня»");
  await page.click('[data-action="send-agent"]');
  ok(await page.locator("[data-typing]").isVisible(), "агент «печатает» (имитация потокового ответа)");
  await page.waitForFunction(() => document.querySelectorAll('#content .msg[data-from="agent"]').length === 2);
  const r1 = await page.locator('#content .msg[data-from="agent"]').nth(1).innerText();
  ok(/обидно/.test(r1) && /\?/.test(r1) && r1.indexOf("обидно") < r1.indexOf("?"), "агент сначала отражает чувство, потом уточняет");
  // Голос: запись → расшифровка → в поле ввода
  await page.click('[data-action="mic"]');
  ok(await page.locator("[data-recording]").isVisible(), "голосовое: идёт запись (подписано)");
  await page.click('[data-action="mic"]');
  ok((await page.inputValue("#chat-input")).includes("третий раз за месяц"), "голосовое расшифровано в текст");
  await page.click('[data-action="send-agent"]');
  await page.waitForSelector("[data-options]");
  ok(/голосовое · расшифровано/.test(await page.locator("#content").innerText()), "реплика помечена как голосовая");
  const r2 = await page.locator('#content .msg[data-from="agent"]').last().innerText();
  ok(/Понимаю/.test(r2) && /подготовить сообщение/i.test(r2), "агент предлагает варианты: выговориться / совет / сообщение");
  ok(!/должна|нужно было|неправильно/i.test(r1 + r2), "без морализаторства");
  ok((await h.main().innerText()) === "Подготовить сообщение Игорю", "MainButton «Подготовить сообщение»");
  await h.clickMain();

  // Черновик
  ok((await h.screen()) === "draft", "Черновик");
  ok((await page.locator(".parts dt").allInnerTexts()).join("—") === "Чувство—Ситуация—Потребность—Просьба", "схема «чувство — ситуация — потребность — просьба»");
  const v1 = await page.inputValue("#draft-text");
  ok(/мне грустно/.test(v1) && /Мне важно проводить с тобой время/.test(v1), "текст как в §1: «Мне грустно… Мне важно проводить с тобой время»");
  await page.click('[data-action="variant"]');
  ok((await page.locator("#draft-meta").innerText()).startsWith("Вариант 2 из 2") && (await page.inputValue("#draft-text")) !== v1, "«Другой вариант» даёт новый текст");
  ok((await igorIncoming()) === 0, "у Игоря входящего нет (черновик не отправлен)");
  await page.click('[data-action="discard"]');
  ok((await h.screen()) === "chat", "«Не отправлять» → обратно в чат");
  ok(/ничего не отправляю/.test(await page.locator('#content .msg[data-from="agent"]').last().innerText()), "агент подтверждает, что ничего не ушло");
  ok((await igorIncoming()) === 0, "после «Не отправлять» у Игоря входящего нет");

  await h.clickMain();
  ok((await h.screen()) === "draft", "снова черновик");
  await page.click('[data-action="edit"]');
  ok(await page.evaluate(() => document.activeElement && document.activeElement.id === "draft-text"), "«Править» ставит курсор в текст");
  await page.click('[data-action="example-edit"]');
  await page.locator("#draft-text").press("Control+End");
  await page.keyboard.type(" Мне правда это важно.");
  const edited = await page.inputValue("#draft-text");
  ok(edited.includes("вечно") && edited.endsWith("Мне правда это важно."), "Анна правит текст своими словами");
  ok((await page.locator("#draft-meta").innerText()).includes("изменено тобой"), "пометка «изменено тобой»");
  ok(await page.locator("[data-harsh-hint]").isVisible(), "подсказка: агент Игоря может смягчить форму");
  ok((await igorIncoming()) === 0, "до клика «Отправить» у Игоря входящего нет (§2 п.2)");
  ok((await h.screen()) === "draft" && (await page.inputValue("#draft-text")) === edited, "правка сохранилась при переключении сторон");
  ok((await h.main().innerText()) === "Отправить Игорю", "MainButton «Отправить Игорю»");
  await h.clickMain();

  // Сообщения пары у Анны
  ok((await h.screen()) === "messages", "Анна: «Сообщения пары»");
  ok((await page.locator('[data-msg="sent"]').count()) === 1, "отправленное в ленте");
  ok((await page.locator('[data-msg="sent"] [data-status]').innerText()).includes("доставлено"), "статус «доставлено» подписан");
  const sentBg = await page.locator('[data-msg="sent"]').evaluate((el) => getComputedStyle(el).backgroundColor);
  ok(sentBg === "rgb(17, 17, 17)", "сообщение владельца — чёрная плашка");

  // Игорь получает
  await h.persona("igor");
  ok((await h.screen()) === "home", "Игорь: главный");
  ok(await page.locator("[data-unread]").isVisible(), "непрочитанное: «1 непрочитанное»");
  ok(/Текста в уведомлении нет/.test(await page.locator("[data-bot-notice]").innerText()), "уведомление бота без текста сообщения");
  ok(!(await page.locator("[data-bot-notice]").innerText()).includes("грустно"), "в уведомлении нет текста");
  await page.click('[data-action="go"][data-to="messages"]');
  const card = page.locator('[data-msg="incoming"]');
  ok((await card.count()) === 1, "у Игоря входящее после «Отправить»");
  ok((await card.locator("[data-adapted]").innerText()).includes("адаптировано"), "пометка «адаптировано»");
  const delivered = await card.locator("[data-delivered]").innerText();
  ok(!/вечно/.test(delivered) && /Мне важно проводить с тобой время/.test(delivered) && /заранее/.test(delivered), "форма смягчена, смысл и просьба сохранены");
  const cardStyle = await card.evaluate((el) => { const cs = getComputedStyle(el); return [cs.borderLeftColor, cs.borderLeftWidth, cs.backgroundColor]; });
  ok(cardStyle[0] === ACCENT && cardStyle[1] === "4px" && cardStyle[2] === "rgb(255, 255, 255)", "сообщение партнёра — белая карточка с красной линией слева");
  ok((await card.locator("[data-original]").count()) === 0, "оригинал скрыт до запроса");
  await card.getByRole("button", { name: "Показать оригинал" }).click();
  ok((await card.locator("[data-original]").innerText()).includes("Ты вечно всё отменяешь"), "«Показать оригинал» — текст, одобренный Аней");
  ok(await card.getByRole("button", { name: "Скрыть оригинал" }).isVisible(), "кнопка меняет подпись на «Скрыть оригинал»");
  await card.getByRole("button", { name: "Ответить с агентом" }).click();
  ok((await h.screen()) === "chat", "«Ответить с агентом» → чат Игоря");
  ok(/Аня прислала тебе сообщение/.test(await page.locator('#content .msg[data-from="agent"]').last().innerText()), "агент Игоря помогает понять, что стоит за сообщением");
  await page.click('[data-action="suggest"]');
  await page.click('[data-action="send-agent"]');
  await page.waitForFunction(() => document.querySelectorAll('#content .msg[data-from="agent"]').length === 3);
  ok(/вживую/.test(await page.locator('#content .msg[data-from="agent"]').last().innerText()), "агент предлагает живой разговор (мост, §2 п.5)");
  await h.clickMain();
  ok((await h.screen()) === "draft" && (await h.main().innerText()) === "Отправить Ане", "Игорь: черновик ответа, «Отправить Ане»");
  await h.clickMain();
  ok((await page.locator('[data-msg="sent"]').count()) === 1, "ответ Игоря ушёл после «Отправить»");

  // Память Игоря получила запись только из одобренного сообщения
  await h.jump("memory");
  ok(/просила предупреждать/.test(await page.locator("#content").innerText()), "память Игоря: запись из одобренного сообщения Ани (ФТ-14)");
  ok(/Партнёр этого не видит/.test(await page.locator("[data-private-note]").innerText()), "пояснение «партнёр этого не видит»");
  ok(!/плевать/.test(await page.locator("#content").innerText()), "в памяти Игоря нет слов из личного чата Ани");

  // Анна видит ответ
  await h.persona("anna");
  await h.jump("messages");
  ok((await page.locator('[data-msg="sent"] [data-status]').innerText()).includes("отвечено"), "у Анны статус «отвечено»");
  ok((await page.locator('[data-msg="incoming"]').count()) === 1, "у Анны входящий ответ Игоря");
  ok((await page.locator('[data-msg="incoming"] .badge').innerText()).includes("без изменений"), "мягкий ответ передан без изменений — подписано");

  ok(page.errors.length === 0, "нет ошибок JS" + (page.errors.length ? ": " + page.errors.join("; ") : ""));
  await page.context().close();
});

// ---------- 4. Память: удаление ----------
await step("4. Память: категории и удаление записи (ФТ-13)", async () => {
  const page = await newPage(375);
  const h = H(page);
  await h.jump("memory");
  const cats = await page.locator(".mem-cat h2").allInnerTexts();
  ok(cats.length === 6, "6 категорий: " + cats.join(", "));
  const before = await page.locator(".mem-item").count();
  await page.locator('[data-action="mem-del"]').first().click();
  ok(/Удалить навсегда/.test(await page.locator(".confirm").innerText()), "удаление с подтверждением");
  await page.click('[data-action="mem-confirm"]');
  ok((await page.locator(".mem-item").count()) === before - 1, "запись удалена");
  await page.context().close();
});

// ---------- 5. Красный — только главное действие и близость; тёмная тема ----------
await step("5. Красный только для главного действия и знаков близости; не для ошибок и не в «Помощи»", async () => {
  const page = await newPage(375);
  const h = H(page);
  const bad = new Set();
  for (const persona of ["anna", "igor"]) {
    await h.persona(persona);
    for (const s of SCREENS) {
      await h.jump(s);
      for (const b of await accentViolations(page)) bad.add(s + ": " + b);
    }
  }
  ok(bad.size === 0, "лишнего красного нет" + (bad.size ? ": " + [...bad].join("; ") : ""));
  await h.jump("help");
  ok(/не заменяет психолога/.test(await page.locator("[data-disclaimer]").innerText()), "«Помощь»: не заменяет психолога (§9.4)");
  ok(/Заглушка/.test(await page.locator("[data-stub]").innerText()) && /выдуманы/.test(await page.locator("[data-stub]").innerText()), "кризисные контакты помечены как выдуманные заглушки (§9.3)");
  await page.context().close();
});

await step("6. Тёмная тема: prefers-color-scheme и переключатель", async () => {
  const page = await newPage(375, "dark");
  const bg = () => page.locator("#phone").evaluate((el) => getComputedStyle(el).backgroundColor);
  ok((await bg()) === "rgb(0, 0, 0)", "по prefers-color-scheme: чёрный фон");
  const accent = await page.locator("#tg-main").evaluate((el) => getComputedStyle(el).backgroundColor);
  ok(accent === ACCENT, "тот же красный акцент в тёмной теме");
  await page.click('[data-theme-set="light"]');
  ok((await bg()) === "rgb(255, 255, 255)" && (await page.getAttribute("html", "data-theme")) === "light", "переключатель «Светлая» перекрывает системную");
  await page.click('[data-theme-set="dark"]');
  ok((await bg()) === "rgb(0, 0, 0)", "переключатель «Тёмная»");
  await page.context().close();
});

// ---------- 7. Скриншоты ----------
await step("7. Скриншоты светлой и тёмной темы → frontend/prototype/screens/", async () => {
  let count = 0;
  for (const scheme of ["light", "dark"]) {
    const page = await newPage(375, scheme);
    const h = H(page);
    // Готовим состояние ключевого сценария: пара создана, Анна отправила, Игорь получил
    await h.clickMain(); await h.clickMain(); await h.clickMain();
    await onboard(page); await h.clickMain();
    await h.persona("igor"); await h.clickMain(); await onboard(page); await h.clickMain();
    await h.persona("anna");
    await page.click('[data-action="to-home"]');
    await page.click('[data-action="go"][data-to="chat"]');
    await page.click('[data-action="suggest"]'); await page.click('[data-action="send-agent"]');
    await page.waitForFunction(() => document.querySelectorAll('#content .msg[data-from="agent"]').length === 2);
    await page.click('[data-action="suggest"]'); await page.click('[data-action="send-agent"]');
    await page.waitForSelector("[data-options]");
    const shot = async (name) => {
      await page.evaluate(() => document.getElementById("toast").classList.remove("show"));
      await page.waitForTimeout(250); await page.screenshot({ path: join(SHOTS, `${scheme}-${name}.png`) }); count++; };
    await shot("05-chat");
    await h.clickMain();
    await page.click('[data-action="example-edit"]');
    await shot("06-draft");
    await h.clickMain();
    await shot("07a-messages-anna");
    await h.persona("igor");
    await shot("04-home-igor");
    await page.click('[data-action="go"][data-to="messages"]');
    await page.getByRole("button", { name: "Показать оригинал" }).click();
    await shot("07b-messages-igor");
    await h.persona("anna");
    const order = [["welcome", "01-welcome"], ["onboarding", "02-onboarding"], ["invite", "03-invite"], ["home", "04-home-anna"], ["memory", "08-memory"], ["profile", "09-profile"], ["subscription", "10-subscription"], ["help", "11-help"]];
    for (const [s, name] of order) {
      if (s === "welcome") {
        for (let i = 0; i < 3; i++) {
          await h.jump("welcome");
          for (let k = 0; k < i; k++) await h.clickMain();
          await shot(`01-welcome-${i + 1}`);
        }
        continue;
      }
      await h.jump(s);
      await shot(name);
    }
    await page.context().close();
  }
  ok(count === 2 * 15, `сохранено ${count} скриншотов`);
});

await browser.close();
console.log(`\nИтог: ${passed} пройдено, ${failed} не пройдено`);
process.exit(failed ? 1 : 0);
