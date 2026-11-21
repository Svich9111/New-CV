// Проверка прототипа 0.6 (Playwright, Chromium). Запуск: node frontend/prototype/check.mjs
// Что проверяется — критерии карточки docs/tasks/0.6.md и замечания раунда 1 (docs/reviews/0.6-*.md).
// Скриншоты по умолчанию пишутся во временный каталог; в закоммиченный screens/ — только с флагом --update-screens.
import { createRequire } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, join } from "node:path";
import { mkdirSync, mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const HERE = dirname(fileURLToPath(import.meta.url));
const URL = pathToFileURL(join(HERE, "index.html")).href;
const UPDATE_SCREENS = process.argv.includes("--update-screens");
const SHOTS = UPDATE_SCREENS ? join(HERE, "screens") : mkdtempSync(join(tmpdir(), "proto-0.6-screens-"));
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
  // Дождаться конца защиты от двойного касания и вернуть «настоящую» недоступность кнопки
  disabled: () => page.locator("#tg-main").isDisabled(),
  // Для «Отправить»/«Оплатить» дождаться конца защиты от двойного касания (как сделал бы человек)
  clickMain: async () => { await page.waitForFunction(() => !document.querySelector("#tg-main[data-guard]")); await page.locator("#tg-main").click(); },
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
  ok(await h.disabled(), "без согласий MainButton недоступна");
  ok(/отметь все четыре/.test(await page.locator("#tg-main-hint").innerText()), "недоступность пояснена подписью, а не только цветом");
  await onboard(page);
  ok(!(await h.disabled()), "после согласий и 18+ — доступна");
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
  ok(await h.disabled(), "черновик до создания пары: «Отправить» недоступна");
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
  ok((await page.locator("#content").innerText()).includes("Аня написала (дословно): «" + edited + "»"), "память Игоря: Анна правила текст — запись дословно из одобренного сообщения (ФТ-14, N-1)");
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

// ---------- Общая подготовка: пара создана ----------
async function setupPair(page) {
  const h = H(page);
  await h.clickMain(); await h.clickMain(); await h.clickMain();
  await onboard(page); await h.clickMain();
  await h.persona("igor"); await h.clickMain(); await onboard(page); await h.clickMain();
  await h.persona("anna");
  await page.click('[data-action="to-home"]');
}
// Анна отправляет: "v1edit" — вариант 1 + пример правки; "v2" — вариант 2; иначе — свой текст целиком
async function annaSends(page, mode) {
  const h = H(page);
  await h.persona("anna");
  await h.jump("draft");
  if (mode === "v2") await page.click('[data-action="variant"]');
  else if (mode === "v1edit") await page.click('[data-action="example-edit"]');
  else if (mode !== "v1") await page.fill("#draft-text", mode);
  const sent = await page.inputValue("#draft-text");
  await h.clickMain();
  return sent;
}
// Весь текст, который видит Игорь: главный, лента (с оригиналом), чат после «Ответить с агентом»,
// оба варианта черновика ответа, память, остальные экраны
async function everythingIgorSees(page) {
  const h = H(page);
  const texts = [];
  // innerText не включает значения полей — добавляем текст черновика и ввода отдельно (T-1)
  const grab = async () => texts.push(await page.evaluate(() =>
    document.getElementById("phone").innerText + "\n" + [...document.querySelectorAll("#phone textarea, #phone input")].map((e) => e.value).join("\n")));
  await h.persona("igor");
  await h.jump("home"); await grab();
  await h.jump("messages");
  const show = page.getByRole("button", { name: "Показать оригинал" });
  if (await show.count()) await show.first().click();
  await grab();
  await page.getByRole("button", { name: "Ответить с агентом" }).first().click();
  await page.click('[data-action="suggest"]');
  await page.click('[data-action="send-agent"]');
  await page.waitForFunction(() => !document.querySelector("[data-typing]"));
  await grab();
  await h.clickMain();
  await grab();
  await page.click('[data-action="variant"]');
  ok((await page.locator("#draft-meta").innerText()).startsWith("Вариант 2 из 2"), "T-1: открыт и второй вариант ответа Игоря");
  await grab();
  for (const s of ["memory", "profile", "subscription", "help", "invite"]) { await h.jump(s); await grab(); }
  return texts.join("\n");
}
// Слова, которые есть только в личном чате и памяти Анны (её агент и её записи), — у Игоря их быть не должно
const ANNA_PRIVATE = ["плевать", "обидно", "обид", "задел", "не замечают", "первом месте", "ценн", "дизайнер", "вторник", "годовщин", "поздно вечером", "третий раз за месяц", "за час до встречи", "главная"];

await step("8. M1: адаптация меняет только заданные фразы — факты, отрицания, просьбы и «люблю» не трогаются (§2 п.3, §6.2 п.5)", async () => {
  const page = await newPage(375);
  const keep = [
    "Ты никогда не предупреждаешь заранее.",
    "Я всегда тебя жду и всегда тебя люблю.",
    "Тебе плевать на меня.",
    "Ему плевать на меня.",
    "Я никогда не думала, что ты уйдёшь. Пожалуйста, предупреждай заранее.",
    "Ты вечно занят, а я тебя люблю.",
    "Мы встречаемся в субботу в 19:00 у кафе на Садовой.",
  ];
  for (const t of keep) {
    const r = await page.evaluate((t) => window.ChuvstvaProto.adaptText(t), t);
    ok(r.text === t && !r.adapted, `без изменений: «${t}»`);
  }
  const r = await page.evaluate(() => window.ChuvstvaProto.adaptText(
    "Я тебя люблю и всегда жду. Ты вечно всё отменяешь в последний момент. Пожалуйста, предупреждай заранее — в пятницу в 19:00."));
  ok(r.adapted && !/вечно всё отменяешь/.test(r.text), "заданная фраза смягчена");
  ok(r.text.startsWith("Я тебя люблю и всегда жду.") && r.text.endsWith("Пожалуйста, предупреждай заранее — в пятницу в 19:00."), "соседние «люблю», «всегда», просьба и время — слово в слово");
  ok(/отмены в последний момент/.test(r.text), "факт (отмены в последний момент) сохранён в смягчённой фразе");
  const r2 = await page.evaluate(() => window.ChuvstvaProto.adaptText("Ты опять меня не слышишь. Мне это важно."));
  ok(r2.adapted && r2.text === "Мне кажется, сейчас ты меня не слышишь. Мне это важно.", "вторая заданная фраза смягчается без родовых окончаний");

  // Через интерфейс: резкий, но незаданный текст доходит как есть и подсказка ничего не обещает (m8)
  await setupPair(page);
  const h = H(page);
  await h.jump("draft");
  const harsh = "Ты никогда не предупреждаешь заранее. Я всегда тебя жду и всегда тебя люблю. Тебе плевать на меня.";
  await page.fill("#draft-text", harsh);
  ok((await page.locator("[data-harsh-hint]").count()) === 0, "m8: для фраз без заданного смягчения подсказка не обещает смягчения");
  await h.clickMain();
  await h.persona("igor"); await h.jump("messages");
  const card = page.locator('[data-msg="incoming"]');
  ok((await card.locator("[data-delivered]").innerText()) === harsh, "Игорю доставлено слово в слово");
  ok((await card.locator(".badge").innerText()).includes("без изменений") && (await card.locator("[data-adapted]").count()) === 0, "пометка «без изменений», не «адаптировано»");
  ok(!/сохранены/.test(await card.innerText()), "нет ложного «смысл сохранён»");
  await page.context().close();
});

await step("9. M2/M3: агент Игоря пересказывает только отправленное; личный чат и память Анны не видны Игорю нигде (§2 п.4, §6.2, §9.2)", async () => {
  for (const mode of ["v1edit", "v2", "Привет. Давай поговорим вечером о выходных."]) {
    const page = await newPage(375);
    const h = H(page);
    await setupPair(page);
    // Анна сначала исповедуется своему агенту
    await page.click('[data-action="go"][data-to="chat"]');
    await page.click('[data-action="suggest"]'); await page.click('[data-action="send-agent"]');
    await page.waitForFunction(() => document.querySelectorAll('#content .msg[data-from="agent"]').length === 2);
    await page.click('[data-action="suggest"]'); await page.click('[data-action="send-agent"]');
    await page.waitForSelector("[data-options]");
    const sent = await annaSends(page, mode);
    const all = await everythingIgorSees(page);
    const leaked = ANNA_PRIVATE.filter((w) => all.toLowerCase().includes(w) && !sent.toLowerCase().includes(w));
    const label = mode.length > 8 ? "свой текст" : mode;
    ok(leaked.length === 0, `${label}: у Игоря нет слов из личного чата и памяти Анны` + (leaked.length ? ": " + leaked.join(", ") : ""));
    await h.jump("chat");
    const recap = await page.locator('#content .msg[data-from="agent"]').filter({ hasText: "Аня прислала тебе сообщение" }).first().innerText();
    // Основы слов: пересказ может менять форму («выберем» → «выбрать»), но не добавлять содержание
    const claims = ["грустн", "одинок", "проводить", "предупрежд", "вечер на выходных", "этой неделе", "три раза", "отмен", "сорвал", "скуча"];
    const invented = claims.filter((w) => recap.includes(w) && !sent.includes(w));
    ok(invented.length === 0, `${label}: пересказ агента Игоря — только из отправленного` + (invented.length ? ": " + invented.join(", ") : ""));
    if (label === "свой текст") ok(/Пересказывать его своими словами не буду/.test(recap), "неизвестный текст: агент не додумывает, предлагает прочитать слова Ани");
    await page.context().close();
  }
});

await step("10. M4: двойное касание MainButton не перескакивает через черновик и не отправляет дважды (§2 п.2)", async () => {
  const page = await newPage(375);
  const h = H(page);
  await setupPair(page);
  await page.click('[data-action="go"][data-to="chat"]');
  await page.click('[data-action="suggest"]'); await page.click('[data-action="send-agent"]');
  await page.waitForFunction(() => document.querySelectorAll('#content .msg[data-from="agent"]').length === 2);
  await page.dblclick("#tg-main");
  ok((await h.screen()) === "draft", "двойное касание «Подготовить сообщение» → остаёмся на «Черновике»");
  ok(!(await page.locator("#tg-main").isDisabled()), "«Отправить» не мигает серым — защита невидима");
  await page.waitForFunction(() => !document.querySelector("#tg-main[data-guard]"));
  await page.locator("#tg-main").click({ clickCount: 3, delay: 40 });
  const sentCount = await page.evaluate(() => document.querySelectorAll('[data-msg="sent"]').length);
  ok((await h.screen()) === "messages" && sentCount === 1, "тройное касание «Отправить» — ровно одно сообщение");
  await h.persona("igor"); await h.jump("messages");
  ok((await page.locator('[data-msg="incoming"]').count()) === 1, "у Игоря ровно одно входящее");
  // Двойное касание без пары: черновик и никакой отправки
  const p2 = await newPage(375);
  const h2 = H(p2);
  await h2.clickMain(); await h2.clickMain(); await h2.clickMain(); await onboard(p2); await h2.clickMain();
  await p2.click('[data-action="to-home"]');
  await p2.dblclick("#tg-main");
  ok((await h2.screen()) === "chat", "двойное касание «Поговорить с агентом» → чат, дальше не проваливается");
  await page.context().close(); await p2.context().close();
});

await step("11. Исправления MINOR: m1/D-4, m3, m4, m5, m6, D-1, D-2, D-5, t1", async () => {
  const page = await newPage(375);
  const h = H(page);
  // m5: до создания пары — ни «Осталось 7 дней», ни оплаты, ни «без лимита»
  await h.clickMain(); await h.clickMain(); await h.clickMain(); await onboard(page); await h.clickMain();
  await h.jump("subscription");
  const sub = await page.locator("#content").innerText();
  ok(!/Осталось/.test(sub) && /начнётся, когда пара будет создана/.test(sub), "m5: до пары пробный период не идёт — подписано словами");
  ok(!(await h.main().isVisible()) && (await page.locator(".plan").count()) === 0, "m5: до пары оплаты нет");
  ok(!/без дневного лимита/.test(sub), "m5: нет «без дневного лимита» (§10)");
  await page.context().close();

  const p = await newPage(375);
  const g = H(p);
  await setupPair(p);
  await g.jump("subscription");
  ok(/осталось 7 дней/.test(await p.locator("#content").innerText()) && (await g.main().isVisible()), "m5: после создания пары — пробный период и оплата");
  // m4: «Помощь» из чата в один шаг, номера — tel:
  await g.jump("chat");
  await p.getByRole("button", { name: /Помощь/ }).click();
  ok((await g.screen()) === "help", "m4: из чата в «Помощь» одним нажатием");
  const tels = await p.locator('a.contact__phone[href^="tel:"]').count();
  ok(tels === 3, "m4: номера помощи — ссылки tel: (" + tels + ")");
  // m6: «Другой вариант» после правки — с подтверждением
  await g.jump("draft");
  await p.locator("#draft-text").press("Control+End");
  await p.keyboard.type(" Моя приписка.");
  await p.click('[data-action="variant"]');
  ok(await p.locator("[data-confirm-variant]").isVisible(), "m6: после правки «Другой вариант» спрашивает подтверждение");
  await p.click('[data-action="variant-cancel"]');
  ok((await p.inputValue("#draft-text")).endsWith("Моя приписка."), "m6: «Оставить мой текст» сохраняет правку");
  await p.click('[data-action="variant"]'); await p.click('[data-action="variant-confirm"]');
  ok((await p.locator("#draft-meta").innerText()).startsWith("Вариант 2 из 2"), "m6: «Заменить» подставляет новый вариант");
  await p.click('[data-action="variant"]');
  ok((await p.locator("#draft-meta").innerText()).startsWith("Вариант 1 из 2"), "m6: без правки подтверждение не нужно");
  // m3: изменённое выделено в доставленном тексте и в оригинале
  await p.click('[data-action="example-edit"]');
  await g.clickMain();
  await g.persona("igor"); await g.jump("messages");
  const card = p.locator('[data-msg="incoming"]');
  ok((await card.locator("[data-delivered] mark.changed").count()) === 1, "m3: в доставленном тексте выделена изменённая фраза");
  ok(/фраз: 1/.test(await card.locator("[data-what-changed]").innerText()), "m3: подписано, что изменено и сколько");
  await card.getByRole("button", { name: "Показать оригинал" }).click();
  ok((await card.locator("[data-original] mark.changed").innerText()).includes("Ты вечно всё отменяешь"), "m3: в оригинале выделено, что было");
  const markBg = await card.locator("mark.changed").first().evaluate((el) => getComputedStyle(el).backgroundColor);
  ok(markBg !== ACCENT, "m3: выделение не красное");
  // t1: после сценария — без горизонтальной прокрутки и лишнего красного, в обеих темах
  for (const theme of ["light", "dark"]) {
    await p.click(`[data-theme-set="${theme}"]`);
    for (const persona of ["igor", "anna"]) {
      await g.persona(persona);
      for (const s of ["home", "messages", "chat", "memory"]) {
        await g.jump(s);
        const bad = await accentViolations(p);
        ok((await g.noHScroll()) && bad.length === 0, `t1 ${theme}/${persona}/${s}: без прокрутки и лишнего красного` + (bad.length ? ": " + bad.join(", ") : ""));
      }
    }
  }
  // D-1: «непрочитанное» — текст цветом --text, красные только точка и рамка
  await p.click('[data-theme-set="dark"]');
  const p3 = await newPage(375, "dark");
  const g3 = H(p3);
  await setupPair(p3); await annaSends(p3, "v1"); await g3.persona("igor"); await g3.jump("home");
  const badge = await p3.locator("[data-unread]").evaluate((el) => { const cs = getComputedStyle(el); return [cs.color, cs.borderTopColor]; });
  ok(badge[0] === "rgb(255, 255, 255)" && badge[1] === ACCENT, "D-1: текст метки белый в тёмной теме, рамка красная");
  // m1/D-4: память Анны из ответа Игоря — без искажений и с верным родом
  await g3.jump("messages");
  await p3.getByRole("button", { name: "Ответить с агентом" }).click();
  await g3.clickMain();
  await g3.clickMain();
  await g3.persona("anna"); await g3.jump("memory");
  const mem = await p3.locator("#content").innerText();
  ok(/Игорь обещал сказать заранее/.test(mem) && !/Игорь просил/.test(mem) && !/ей важно/.test(mem), "D-4: память Анны передаёт смысл ответа Игоря (обещал, не «просил»)");
  // D-2: при открытом диалоге «Назад» доступна и закрывает его
  await g3.jump("profile");
  await p3.click('[data-action="sheet"][data-sheet="leave"]');
  await g3.back();
  ok((await p3.locator(".sheet-backdrop").count()) === 0 && (await g3.screen()) === "profile", "D-2: «Назад» закрывает диалог");
  // D-5: переключатели названы по смыслу
  const names = await p3.getByRole("switch").evaluateAll((els) => els.map((e) => e.getAttribute("aria-label")));
  ok(names.join("|") === "Новые сообщения от партнёра|Статус подписки", "D-5: доступные имена переключателей");
  // m7: «Закрыть» — одна строка
  await g3.jump("home");
  const closeH = await p3.evaluate(() => { const b = document.getElementById("tg-close"); return b.hidden ? 0 : b.getBoundingClientRect().height; });
  ok(closeH === 0 || closeH <= 44, "m7: кнопка «Закрыть» в шапке не переносится");
  await p.context().close(); await p3.context().close();
});

await step("12. N-1: пересказ и память получателя — только по точному совпадению с вариантом; N-3: «Помощь» в чате видна всегда", async () => {
  const page = await newPage(375);
  const h = H(page);
  await setupPair(page);
  await h.jump("draft");
  const base = await page.inputValue("#draft-text");
  const tail = " Хотя нет, предупреждать не нужно.";
  await page.locator("#draft-text").press("Control+End");
  await page.keyboard.type(tail);
  const sent = await page.inputValue("#draft-text");
  ok(sent === base + tail, "Анна дописала к варианту отказ от просьбы");
  await h.clickMain();
  await h.persona("igor"); await h.jump("messages");
  await page.getByRole("button", { name: "Ответить с агентом" }).click();
  const recap = await page.locator('#content .msg[data-from="agent"]').last().innerText();
  ok(/Пересказывать его своими словами не буду/.test(recap) && !/просит/.test(recap), "N-1: изменённый вариант — агент Игоря не пересказывает заготовку");
  await h.jump("memory");
  const mem = await page.locator("#content").innerText();
  ok(!/Аня просит/.test(mem) && mem.includes("«" + sent + "»"), "N-1: в памяти Игоря — дословный текст, без заготовленной фразы");
  // Ответ Игоря: набор «нейтральный», заготовка про отмены не подставляется
  await h.jump("draft");
  ok(!/срывались/.test(await page.inputValue("#draft-text")), "N-1: черновик Игоря не опирается на заготовку Ани");
  // Точное совпадение + заданная фраза SOFTEN — пересказ остаётся
  const p2 = await newPage(375);
  const h2 = H(p2);
  await setupPair(p2); await annaSends(p2, "v1edit");
  await h2.persona("igor"); await h2.jump("messages");
  await p2.getByRole("button", { name: "Ответить с агентом" }).click();
  ok(/Она просит предупреждать заранее/.test(await p2.locator('#content .msg[data-from="agent"]').last().innerText()), "вариант без изменений (+ заданная фраза) — пересказ по варианту");
  // N-3: после длинной переписки вход в «Помощь» виден без прокрутки
  await h2.persona("anna"); await h2.jump("chat");
  for (let i = 0; i < 6; i++) {
    await p2.fill("#chat-input", "Ещё одна мысль номер " + (i + 1) + ", довольно длинная, чтобы лента точно ушла за экран.");
    await p2.click('[data-action="send-agent"]');
    await p2.waitForFunction(() => !document.querySelector("[data-typing]"));
  }
  const vis = await p2.evaluate(() => {
    const b = document.querySelector(".help-link"); const c = document.getElementById("content");
    const r = b.getBoundingClientRect(); const cr = c.getBoundingClientRect();
    return c.scrollHeight > c.clientHeight && r.top >= cr.top && r.bottom <= cr.bottom;
  });
  ok(vis, "N-3: «Помощь» в чате видна при длинной ленте");
  await p2.locator(".help-link").click();
  ok((await h2.screen()) === "help", "N-3: переход в «Помощь»");
  await page.context().close(); await p2.context().close();
});

// ---------- 7. Скриншоты ----------
await step("7. Скриншоты светлой и тёмной темы → " + (UPDATE_SCREENS ? "frontend/prototype/screens/" : SHOTS + " (для обновления screens/ — флаг --update-screens)"), async () => {
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
