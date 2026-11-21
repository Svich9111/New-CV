/* Прототип 0.6 «Чувства AI»: кликабельные экраны §8 ТЗ на выдуманных данных.
   Без сборки и внешних зависимостей. Ничего не отправляет в сеть.
   Главный инвариант (§2 п.2): сообщение партнёру появляется только в sendDraft(),
   а sendDraft() вызывается только нажатием главной кнопки «Отправить …» на экране «Черновик». */
(function () {
  "use strict";

  // ---------- Выдуманные персонажи ----------
  const NAMES = {
    anna: { name: "Анна", nom: "Аня", gen: "Ани", dat: "Ане", ins: "Аней", fem: true },
    igor: { name: "Игорь", nom: "Игорь", gen: "Игоря", dat: "Игорю", ins: "Игорем", fem: false },
  };
  const other = (p) => (p === "anna" ? "igor" : "anna");

  const TITLES = {
    welcome: "Чувства AI",
    onboarding: "Знакомство",
    invite: "Приглашение",
    home: "Чувства AI",
    chat: "Твой агент",
    draft: "Черновик",
    messages: "Сообщения пары",
    memory: "Что агент помнит",
    profile: "Профиль и пара",
    subscription: "Подписка",
    help: "Помощь",
  };

  const MEMORY_CATS = [
    ["fact", "Факты"],
    ["date", "Важные даты"],
    ["pref", "Предпочтения"],
    ["sensitive", "Чувствительные темы"],
    ["observation", "Наблюдения агента"],
    ["partner", "Из сообщений партнёра"],
  ];

  const INITIAL_MEMORY = {
    anna: [
      { cat: "fact", text: "Работает дизайнером, по вторникам часто задерживается", src: "твои слова · 2 ноября" },
      { cat: "fact", text: "Живёт с Игорем третий год", src: "твои слова · 2 ноября" },
      { cat: "date", text: "Годовщина знакомства — 14 июня", src: "твои слова · 5 ноября" },
      { cat: "pref", text: "Не любит, когда планы меняются в последний момент", src: "твои слова · 12 ноября" },
      { cat: "sensitive", text: "Тяжело переживает ощущение, что она не на первом месте", src: "твои слова · 12 ноября" },
      { cat: "observation", text: "Чаще пишет поздно вечером", src: "вывод агента · уверенность средняя" },
    ],
    igor: [
      { cat: "fact", text: "Работает посменно, в конце месяца — авралы", src: "твои слова · 4 ноября" },
      { cat: "date", text: "День рождения Ани — 3 марта", src: "твои слова · 4 ноября" },
      { cat: "pref", text: "Не любит, когда решают за него", src: "твои слова · 9 ноября" },
      { cat: "sensitive", text: "Болезненно реагирует на тему денег", src: "твои слова · 9 ноября" },
      { cat: "observation", text: "После ночных смен отвечает коротко", src: "вывод агента · уверенность низкая" },
    ],
  };

  // Черновики по схеме «чувство — ситуация — потребность — просьба» (§4.3, §6.2)
  const DRAFTS = {
    anna: [
      {
        text:
          "Игорь, мне грустно, когда наши планы отменяются в последний момент — в этом месяце так было уже три раза. " +
          "Мне важно проводить с тобой время и чувствовать, что оно важно и тебе. " +
          "Давай договоримся: если что-то меняется, ты предупреждаешь заранее. И выберем вечер на этой неделе, который точно будет нашим?",
        parts: [
          ["Чувство", "мне грустно"],
          ["Ситуация", "планы отменяются в последний момент, третий раз за месяц"],
          ["Потребность", "время вместе и ощущение, что оно важно обоим"],
          ["Просьба", "предупреждать заранее и выбрать вечер на неделе"],
        ],
      },
      {
        text:
          "Игорь, сегодня мне стало одиноко, когда наш вечер снова сорвался. " +
          "Я скучаю по нашему времени вдвоём. " +
          "Можешь предупреждать об изменениях заранее? И давай вместе выберем вечер на выходных.",
        parts: [
          ["Чувство", "мне одиноко"],
          ["Ситуация", "вечер снова сорвался"],
          ["Потребность", "время вдвоём"],
          ["Просьба", "предупреждать заранее, выбрать вечер на выходных"],
        ],
      },
    ],
    igor: [
      {
        text:
          "Аня, мне жаль, что тебе грустно из-за отменённых вечеров. " +
          "Наше время мне тоже важно — я просто не замечал, как часто это стало происходить. " +
          "Давай в субботу поужинаем вдвоём, а если что-то поменяется, я скажу заранее.",
        parts: [
          ["Чувство", "мне жаль, что тебе грустно"],
          ["Ситуация", "отменённые вечера"],
          ["Потребность", "наше время важно и мне"],
          ["Просьба", "ужин вдвоём в субботу"],
        ],
      },
      {
        text:
          "Аня, я прочитал и понял, что тебе было обидно. Мне важно, чтобы ты чувствовала, что ты для меня главная. " +
          "Можем сегодня вечером спокойно поговорить об этом?",
        parts: [
          ["Чувство", "понимаю, что тебе было обидно"],
          ["Ситуация", "твоё сообщение"],
          ["Потребность", "чтобы ты чувствовала себя важной"],
          ["Просьба", "поговорить сегодня вечером"],
        ],
      },
    ],
  };
  const EXAMPLE_EDIT = "Ты вечно всё отменяешь в последний момент.";

  // Тексты агента: сначала отражает чувство, потом уточняет, потом предлагает (§2, §4.2)
  const SCRIPT = {
    anna: {
      greeting: "Привет, Аня. Я здесь, чтобы выслушать. Что у тебя на душе?",
      suggest: [
        "Ему вообще плевать на меня",
        "Он третий раз за месяц отменяет наш вечер. Сегодня написал за час до встречи.",
      ],
      replies: [
        "Звучит так, будто тебе сейчас очень обидно — как будто тебя не замечают. Это тяжёлое чувство, и хорошо, что ты о нём говоришь.\n\nРасскажешь, что случилось? Что задело сильнее всего?",
        "Понимаю. Ты ждала этот вечер, а он снова сорвался — и уже не в первый раз.\n\nФакт — три отмены за месяц. А «плевать» — это то, как это ощущается изнутри, и это ощущение важно. Похоже, за обидой стоит желание чувствовать, что ваше время ценно и для него. Так?\n\nЧто тебе сейчас ближе: просто выговориться, получить совет или подготовить сообщение Игорю?",
      ],
      options: true,
    },
    igor: {
      greeting: "Привет, Игорь. Как ты? Можно рассказать что угодно — это видишь только ты.",
      suggest: ["Я не думал, что это её так задевает. Мне правда важно время с ней."],
      replies: [
        "Похоже, ты не ожидал, что для неё это настолько важно, и тебе сейчас немного не по себе. Это понятно — и то, что тебе важно ваше время, хорошо слышно.\n\nМожно ответить коротко, а главное обсудить вживую — например, за ужином. Подготовить ответ Ане?",
      ],
      options: false,
    },
  };
  const GENERIC_REPLY = "Слышу тебя. Расскажи ещё, если хочется, — я не тороплю.";

  // ---------- Состояние ----------
  function freshUser(id) {
    const n = NAMES[id];
    const p = NAMES[other(id)];
    return {
      stack: id === "anna" ? ["welcome"] : ["invite"],
      slide: 0,
      onboarded: false,
      form: { name: n.name, call: n.nom, partner: p.name, rules: false, data: false, sensitive: false, age: false },
      chat: [{ from: "agent", text: SCRIPT[id].greeting }],
      stage: 0,
      input: "",
      inputVoice: false,
      typing: false,
      recording: false,
      draft: null,
      memory: INITIAL_MEMORY[id].map((m, i) => Object.assign({ id: id + "-m" + i }, m)),
      pendingDelete: null,
      notify: { messages: true, billing: true },
      inviteSent: false,
      showOriginal: {},
      replyContext: false,
      plan: "month",
      sheet: null,
    };
  }

  const S = {
    persona: "anna",
    theme: "auto",
    pairActive: false,
    messages: [],
    subscription: { status: "trial", paidBy: null },
    users: { anna: freshUser("anna"), igor: freshUser("igor") },
  };
  let memSeq = 0;

  const me = () => S.users[S.persona];
  const N = () => NAMES[S.persona];
  const P = () => NAMES[other(S.persona)];
  const current = () => me().stack[me().stack.length - 1];

  // ---------- Утилиты ----------
  function esc(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  }
  const HEART = '<svg class="heart" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 21s-7.5-4.6-10-9.3C.4 8.4 2.3 4 6.4 4c2.3 0 3.8 1.3 5.6 3.3C13.8 5.3 15.3 4 17.6 4c4.1 0 6 4.4 4.4 7.7C19.5 16.4 12 21 12 21z"/></svg>';
  const MIC = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 15a3 3 0 0 0 3-3V6a3 3 0 1 0-6 0v6a3 3 0 0 0 3 3zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.9V21h2v-2.1A7 7 0 0 0 19 12h-2z"/></svg>';
  const SEND = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 20.5 21 12 3 3.5 3 10l12 2-12 2z"/></svg>';

  function haptic(el) {
    try { if (navigator.vibrate) navigator.vibrate(10); } catch (e) { /* нет вибрации */ }
    if (!el) return;
    el.classList.remove("haptic");
    void el.offsetWidth;
    el.classList.add("haptic");
  }

  let toastTimer = null;
  function toast(text) {
    const t = document.getElementById("toast");
    t.textContent = text;
    t.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove("show"), 2600);
  }

  // Имитация адаптации агентом получателя (§4.4, §6.2): меняется форма, не смысл.
  function adaptText(text) {
    const rules = [
      [/Ты вечно всё отменяешь в последний момент\.?/g, "Мне кажется, отмены в последний момент стали повторяться слишком часто."],
      [/(?<![а-яё])(вечно|всегда)(?![а-яё])/giu, "часто"],
      [/(?<![а-яё])никогда(?![а-яё])/giu, "редко"],
      [/[Тт]ебе (вообще )?плевать на меня\.?/g, "Мне не хватает ощущения, что я тебе важна."],
    ];
    let out = text;
    for (const [re, rep] of rules) out = out.replace(re, rep);
    return { text: out, adapted: out !== text };
  }
  const HARSH = /(?<![а-яё])(вечно|всегда|никогда|плевать)(?![а-яё])/iu;

  function partnerMemoryFrom(text, author) {
    const a = NAMES[author];
    if (/заранее/i.test(text)) return a.nom + (a.fem ? " просила" : " просил") + " предупреждать об изменениях планов заранее";
    if (/суббот/i.test(text)) return a.nom + (a.fem ? " предложила" : " предложил") + " поужинать вдвоём в субботу";
    return a.nom + (a.fem ? " написала" : " написал") + ", что ей важно ваше время вместе";
  }

  // ---------- Навигация ----------
  function go(screen) {
    const u = me();
    if (current() !== screen) u.stack.push(screen);
    u.sheet = null;
    render(true);
  }
  function resetTo(screen) {
    me().stack = [screen];
    me().sheet = null;
    render(true);
  }
  function back() {
    const u = me();
    if (current() === "welcome" && u.slide > 0) { u.slide -= 1; render(true); return; }
    if (u.sheet) { u.sheet = null; render(); return; }
    if (u.stack.length > 1) { u.stack.pop(); render(true); }
  }
  function canGoBack() {
    const u = me();
    return u.stack.length > 1 || (current() === "welcome" && u.slide > 0);
  }

  // ---------- Действия ----------
  function sendToAgent(textArg) {
    const u = me();
    const text = (textArg != null ? textArg : u.input).trim();
    if (!text || u.typing) return;
    u.chat.push({ from: "own", text, voice: textArg == null && u.inputVoice });
    u.input = "";
    u.inputVoice = false;
    u.typing = true;
    const persona = S.persona;
    render();
    setTimeout(() => {
      const uu = S.users[persona];
      const sc = SCRIPT[persona];
      let reply;
      if (persona === "igor" && !uu.replyContext) reply = GENERIC_REPLY;
      else reply = sc.replies[uu.stage] || GENERIC_REPLY;
      uu.stage += 1;
      uu.typing = false;
      uu.chat.push({ from: "agent", text: reply, options: sc.options && uu.stage === sc.replies.length });
      if (S.persona === persona) render();
    }, 650);
  }

  function chooseOption(kind) {
    const u = me();
    const map = {
      vent: ["Просто выговориться", "Хорошо, я просто побуду рядом и послушаю. Пиши как есть, не подбирая слов — здесь это видишь только ты."],
      advice: ["Получить совет", "Можно начать не с упрёка, а с чувства: не «ты опять», а «мне грустно, когда…». И выбрать спокойный момент, а не сразу после отмены.\n\nЕсли захочешь, вместе подготовим сообщение — отправишь его, только когда сама решишь."],
    };
    if (kind === "draft") { openDraft(); return; }
    const [q, a] = map[kind];
    u.chat.push({ from: "own", text: q });
    u.chat.push({ from: "agent", text: a });
    render();
  }

  function openDraft() {
    const u = me();
    if (!u.draft) {
      const v = DRAFTS[S.persona][0];
      u.draft = { idx: 0, text: v.text, edited: false };
    }
    go("draft");
  }

  function sendDraft() {
    const u = me();
    if (!S.pairActive || !u.draft) return;
    const original = u.draft.text.trim();
    if (!original) return;
    const to = other(S.persona);
    const ad = adaptText(original);
    // Ответ на последнее входящее отмечаем «отвечено»
    S.messages.forEach((m) => { if (m.to === S.persona && m.from === to) m.status = "replied"; });
    S.messages.push({
      id: "msg" + (S.messages.length + 1),
      from: S.persona,
      to,
      original,
      delivered: ad.text,
      adapted: ad.adapted,
      status: "delivered",
      time: S.messages.length ? "20:15" : "19:42",
      originalViewed: false,
    });
    S.users[to].memory.push({
      id: "pm" + ++memSeq,
      cat: "partner",
      text: partnerMemoryFrom(original, S.persona),
      src: "одобренное сообщение " + NAMES[S.persona].gen + " · сегодня",
    });
    u.draft = null;
    u.chat.push({
      from: "agent",
      text:
        "Сообщение у " + P().gen + ". Ты " + (N().fem ? "сказала" : "сказал") + " о важном — это непросто.\n\n" +
        "Когда " + P().nom + " ответит, я помогу разобраться. А если захочется поговорить вживую — помогу подготовиться.",
    });
    u.stack = u.stack.filter((s) => s !== "draft");
    toast("Отправлено. " + P().nom + " получит уведомление без текста сообщения.");
    go("messages");
  }

  function replyWithAgent() {
    const u = me();
    if (!u.replyContext) {
      u.replyContext = true;
      if (S.persona === "igor") u.stage = 0;
      else u.stage = SCRIPT.anna.replies.length + 1;
      const incoming = S.messages.filter((m) => m.to === S.persona).slice(-1)[0];
      const p = P();
      let text;
      if (S.persona === "igor") {
        text =
          "Аня прислала тебе сообщение. Похоже, ей грустно из-за отменённых вечеров и важно, чтобы ваше время было ценным и для тебя. " +
          "Её просьба — предупреждать заранее и выбрать вечер вдвоём.\n\nКак ты это воспринял? Что первым пришло в голову?";
      } else {
        text = p.nom + " ответил. Кажется, он тебя услышал. Как тебе его ответ? Что хочется сказать в ответ — или, может, лучше обсудить это вживую?";
      }
      if (incoming) u.chat.push({ from: "agent", text });
    }
    go("chat");
  }

  function finishOnboarding() {
    const u = me();
    u.onboarded = true;
    if (S.persona === "anna") {
      resetTo("invite");
    } else {
      S.pairActive = true;
      toast("Пара создана. Вы с Аней на связи");
      resetTo("home");
    }
  }

  function onboardingValid() {
    const f = me().form;
    return !!(f.name.trim() && f.call.trim() && f.partner.trim() && f.rules && f.data && f.sensitive && f.age);
  }

  // ---------- Экраны ----------
  const screens = {
    welcome() {
      const u = me();
      const slides = [
        {
          art: HEART.replace('class="heart"', 'class="heart" style="width:44px;height:44px"'),
          h: "Твой личный агент",
          p: "Он выслушает без осуждения и поможет сказать партнёру о том, что беспокоит, — на языке чувств, а не упрёков.",
        },
        {
          art: "🔒",
          h: "Что приватно",
          p: "Разговор с агентом видишь только ты. Партнёр и его агент получат только те сообщения, которые ты отправишь кнопкой «Отправить».",
        },
        {
          art: "🤝",
          h: "Чем мы не являемся",
          p: "«Чувства AI» не заменяет психолога и не оказывает психологических и медицинских услуг. Если тебе или кому-то рядом угрожает опасность — в разделе «Помощь» есть контакты служб поддержки.",
        },
      ];
      const s = slides[u.slide];
      return {
        html:
          '<section class="slide" data-screen="welcome" data-slide="' + (u.slide + 1) + '">' +
          '<div class="slide__art" aria-hidden="true">' + s.art + "</div>" +
          "<h1>" + esc(s.h) + "</h1>" +
          '<p class="lead">' + esc(s.p) + "</p>" +
          '<div class="dots" aria-hidden="true">' + slides.map((_, i) => '<span class="' + (i === u.slide ? "on" : "") + '"></span>').join("") + "</div>" +
          '<p class="small muted" style="margin-top:8px">Слайд ' + (u.slide + 1) + " из 3</p>" +
          "</section>",
        main: {
          text: u.slide < 2 ? "Далее" : "Начать",
          action: () => { if (u.slide < 2) { u.slide += 1; render(true); } else go("onboarding"); },
        },
      };
    },

    onboarding() {
      const f = me().form;
      const n = N();
      const chk = (key, label) =>
        '<label class="check"><input type="checkbox" data-model="form.' + key + '"' + (f[key] ? " checked" : "") + "><span>" + label + "</span></label>";
      return {
        html:
          '<section data-screen="onboarding">' +
          "<h1>Давай познакомимся</h1>" +
          '<p class="muted">Это нужно агенту, чтобы говорить с тобой по-человечески. Номер телефона не нужен.</p>' +
          '<div class="field"><label for="f-name">Твоё имя</label><input class="input" id="f-name" data-model="form.name" value="' + esc(f.name) + '" autocomplete="off"></div>' +
          '<div class="field"><label for="f-call">Как к тебе обращаться</label><input class="input" id="f-call" data-model="form.call" value="' + esc(f.call) + '" autocomplete="off"></div>' +
          '<div class="field"><label for="f-partner">Имя партнёра</label><input class="input" id="f-partner" data-model="form.partner" value="' + esc(f.partner) + '" autocomplete="off"' + (S.persona === "igor" ? " readonly" : "") + "></div>" +
          '<div class="field"><span class="label">Согласия</span>' +
          chk("rules", 'Принимаю <u>правила сервиса</u>') +
          chk("data", "Даю согласие на обработку персональных данных") +
          chk("sensitive", "Даю отдельное согласие на обработку сведений о чувствах и отношениях") +
          chk("age", "Мне исполнилось 18 лет") +
          "</div>" +
          '<div class="note"><span class="note__icon">i</span><span>Сервис не оказывает психологических и медицинских услуг. Тексты документов готовит юрист — здесь заглушки.</span></div>' +
          (S.persona === "igor" ? '<p class="small muted">' + esc(n.nom) + ", после этого шага пара с Аней будет создана.</p>" : "") +
          "</section>",
        main: {
          text: "Продолжить",
          disabled: !onboardingValid(),
          hint: onboardingValid() ? "" : "Чтобы продолжить, заполни поля и отметь все четыре пункта",
          action: finishOnboarding,
        },
      };
    },

    invite() {
      const u = me();
      if (S.persona === "anna") {
        let status;
        if (S.pairActive) status = '<div class="status" data-status="active"><span class="status__mark">' + HEART + '</span><span><b>Игорь принял приглашение.</b> Вы пара.</span></div>';
        else if (u.inviteSent) status = '<div class="status" data-status="waiting"><span class="status__mark">…</span><span><b>Ждём Игоря.</b> Ссылка отправлена, он ещё не принял приглашение.</span></div>';
        else status = '<div class="status" data-status="ready"><span class="status__mark">○</span><span><b>Ссылка готова.</b> Ещё не отправлена.</span></div>';
        return {
          html:
            '<section data-screen="invite">' +
            "<h1>Пригласи Игоря</h1>" +
            '<p class="muted">У Игоря будет свой агент. Ваши личные разговоры с агентами друг другу не видны.</p>' +
            '<div class="link-box" aria-label="Ссылка-приглашение">t.me/chuvstva_ai_bot?start=inv_7Kq2xP</div>' +
            '<p class="small muted">Ссылка одноразовая и действует 7 дней — до 28 ноября.</p>' +
            '<div class="card card--soft">' + status + "</div>" +
            '<p class="small">Пока Игорь не принял приглашение, можно говорить со своим агентом. Отправлять сообщения партнёру можно будет после того, как пара создана.</p>' +
            '<button type="button" class="btn btn--block btn--ghost" data-action="to-home">' + (S.pairActive ? "На главный" : "Пока поговорить с агентом") + "</button>" +
            "</section>",
          main: S.pairActive
            ? { text: "На главный", action: () => resetTo("home") }
            : {
                text: u.inviteSent ? "Отправить ещё раз в Telegram" : "Отправить в Telegram",
                action: () => { u.inviteSent = true; toast("Открылся выбор чата Telegram (имитация). Ссылка отправлена"); render(); },
              },
        };
      }
      // Сторона Игоря: входящее приглашение
      const annaReady = S.users.anna.onboarded;
      if (S.pairActive) {
        return {
          html: '<section data-screen="invite"><h1>Вы пара</h1><div class="card card--soft"><div class="status" data-status="active"><span class="status__mark">' + HEART + "</span><span>Приглашение Ани принято.</span></div></div></section>",
          main: { text: "На главный", action: () => resetTo("home") },
        };
      }
      return {
        html:
          '<section data-screen="invite">' +
          (annaReady
            ? "<h1>Аня приглашает тебя</h1>" +
              '<p class="lead">Аня пользуется «Чувствами AI» и хочет, чтобы вам было проще говорить о важном.</p>' +
              '<div class="card card--soft"><p><b>Как это устроено</b></p><p class="small">У тебя будет свой агент. Он не видит разговоров Ани, а она — твоих. Между вами передаются только сообщения, которые каждый сам нажал «Отправить».</p></div>' +
              '<p class="small muted">Приглашение действует до 28 ноября.</p>'
            : "<h1>Приглашения пока нет</h1>" +
              '<div class="card card--soft"><div class="status" data-status="none"><span class="status__mark">○</span><span>Аня ещё не прошла знакомство и не отправила ссылку. В прототипе переключись на «Анна» вверху.</span></div></div>') +
          "</section>",
        main: annaReady ? { text: "Принять приглашение", action: () => go("onboarding") } : null,
      };
    },

    home() {
      const u = me();
      const p = P();
      const incoming = S.messages.filter((m) => m.to === S.persona);
      const unread = incoming.filter((m) => m.status === "delivered").length;
      const pairCard = S.pairActive
        ? '<div class="card card--soft" data-pair="active"><div class="row">' + HEART + '<div class="grow"><b>Вы в паре с ' + esc(p.ins) + "</b>" +
          '<div class="small muted">Подписка: ' + (S.subscription.status === "paid" ? "активна" : "пробный период, 7 дней") + "</div></div></div></div>"
        : '<button type="button" class="card card--soft card--link" data-action="go" data-to="invite" data-pair="pending"><div class="row"><span class="status__mark">…</span>' +
          '<div class="grow"><b>Ждём, когда ' + esc(p.nom) + " примет приглашение</b>" +
          '<div class="small muted">Пока можно говорить с агентом. Отправка сообщений откроется после создания пары.</div></div><span class="chev">›</span></div></button>';
      const bot = unread && u.notify.messages
        ? '<div class="note" data-bot-notice><span class="note__icon">✉</span><span><b>Из бота:</b> «' + esc(p.nom) + (p.fem ? " отправила" : " отправил") + " тебе сообщение. Открой приложение, чтобы прочитать». <span class=\"muted\">Текста в уведомлении нет.</span></span></div>"
        : "";
      const unreadBadge = unread
        ? '<span class="badge badge--love" data-unread="' + unread + '"><span class="dot"></span> ' + unread + " " + (unread === 1 ? "непрочитанное" : "непрочитанных") + "</span>"
        : '<span class="badge">нет новых</span>';
      return {
        html:
          '<section data-screen="home">' +
          "<h1>Привет, " + esc(u.form.call || N().nom) + "</h1>" +
          pairCard + bot +
          '<button type="button" class="card card--link" data-action="go" data-to="chat"><div class="row"><div class="grow"><h3>Поговорить с агентом</h3>' +
          '<div class="small muted">Выговориться, разобраться в чувствах, подготовить сообщение. Это видишь только ты.</div></div><span class="chev">›</span></div></button>' +
          '<button type="button" class="card card--link" data-action="go" data-to="messages"><div class="row"><div class="grow"><h3>Сообщения с ' + esc(p.ins) + "</h3>" +
          '<div style="margin-top:6px">' + unreadBadge + '</div></div><span class="chev">›</span></div></button>' +
          '<div class="list" style="margin-top:20px">' +
          '<button type="button" class="list__row" data-action="go" data-to="memory"><span>Что агент помнит</span><span class="chev">›</span></button>' +
          '<button type="button" class="list__row" data-action="go" data-to="profile"><span>Профиль и пара</span><span class="chev">›</span></button>' +
          '<button type="button" class="list__row" data-action="go" data-to="subscription"><span>Подписка</span><span class="small muted">' + (S.subscription.status === "paid" ? "активна" : "пробный период") + "</span></button>" +
          '<button type="button" class="list__row" data-action="go" data-to="help"><span>Помощь</span><span class="chev">›</span></button>' +
          "</div></section>",
        main: { text: "Поговорить с агентом", action: () => go("chat") },
      };
    },

    chat() {
      const u = me();
      const sc = SCRIPT[S.persona];
      const p = P();
      let suggest = [];
      if (S.persona === "anna" && u.stage < sc.suggest.length) suggest = [sc.suggest[u.stage]];
      if (S.persona === "igor" && u.replyContext && u.stage < sc.suggest.length) suggest = [sc.suggest[u.stage]];
      const last = u.chat[u.chat.length - 1];
      const msgs = u.chat
        .map((m) =>
          '<div class="msg msg--' + (m.from === "own" ? "own" : "agent") + '" data-from="' + m.from + '">' + esc(m.text) +
          (m.voice ? '<span class="msg__meta">голосовое · расшифровано</span>' : "") + "</div>")
        .join("");
      const options =
        last && last.options && !u.typing
          ? '<div class="chips" data-options>' +
            '<button type="button" class="chip" data-action="opt" data-kind="vent">Просто выговориться</button>' +
            '<button type="button" class="chip" data-action="opt" data-kind="advice">Получить совет</button>' +
            '<button type="button" class="chip" data-action="opt" data-kind="draft">Подготовить сообщение</button></div>'
          : "";
      const hasOwn = u.chat.some((m) => m.from === "own");
      return {
        chat: true,
        html:
          '<section class="chat" data-screen="chat" aria-live="polite">' +
          '<div class="chat-day">Сегодня · этот разговор видишь только ты</div>' + msgs +
          (u.typing ? '<div class="msg msg--agent msg--typing" data-typing>Агент печатает…</div>' : "") + options +
          "</section>" +
          (suggest.length && !u.typing
            ? '<div class="chips" data-suggest><span class="tiny muted" style="width:100%">Подсказка прототипа — нажми, чтобы вставить:</span>' +
              suggest.map((s) => '<button type="button" class="chip" data-action="suggest" data-text="' + esc(s) + '">' + esc(s) + "</button>").join("") + "</div>"
            : "") +
          (u.recording ? '<div class="recording" data-recording>● Идёт запись (имитация). Нажми на микрофон ещё раз, чтобы закончить.</div>' : "") +
          '<div class="composer">' +
          '<button type="button" class="icon-btn" data-action="mic" aria-label="' + (u.recording ? "Закончить запись" : "Записать голосовое") + '" aria-pressed="' + u.recording + '">' + MIC + "</button>" +
          '<textarea id="chat-input" rows="1" data-model="input" placeholder="Напиши, что тебя беспокоит" aria-label="Сообщение агенту">' + esc(u.input) + "</textarea>" +
          '<button type="button" class="icon-btn icon-btn--fill" data-action="send-agent" aria-label="Отправить агенту">' + SEND + "</button>" +
          "</div>",
        main: hasOwn ? { text: S.persona === "anna" ? "Подготовить сообщение " + p.dat : "Подготовить ответ " + p.dat, action: openDraft } : null,
      };
    },

    draft() {
      const u = me();
      const p = P();
      if (!u.draft) u.draft = { idx: 0, text: DRAFTS[S.persona][0].text, edited: false };
      const d = u.draft;
      const v = DRAFTS[S.persona][d.idx];
      return {
        html:
          '<section data-screen="draft">' +
          '<div class="note"><span class="note__icon">i</span><span>Черновик видишь только ты. Пока ты не нажмёшь «Отправить», ' + esc(p.nom) + " ничего не получит.</span></div>" +
          '<div class="field"><label for="draft-text">Сообщение для ' + esc(p.gen) + "</label>" +
          '<textarea class="textarea" id="draft-text" data-model="draft.text">' + esc(d.text) + "</textarea></div>" +
          '<p class="small muted" id="draft-meta">' + draftMeta() + "</p>" +
          '<div id="draft-hint">' + draftHint() + "</div>" +
          (S.persona === "anna"
            ? '<div class="chips"><span class="tiny muted" style="width:100%">Подсказка прототипа — пример правки Ани:</span><button type="button" class="chip" data-action="example-edit">Добавить «' + esc(EXAMPLE_EDIT) + "»</button></div>"
            : "") +
          '<dl class="parts" aria-label="Из чего состоит черновик">' + v.parts.map(([k, val]) => "<dt>" + k + "</dt><dd>" + esc(val) + "</dd>").join("") + "</dl>" +
          '<p class="tiny muted">Схема: чувство — ситуация — потребность — просьба' + (d.edited ? " (по исходному варианту агента)" : "") + "</p>" +
          '<div class="btn-row">' +
          '<button type="button" class="btn" data-action="edit">Править</button>' +
          '<button type="button" class="btn" data-action="variant">Другой вариант</button>' +
          "</div>" +
          '<div class="btn-row"><button type="button" class="btn btn--text" data-action="discard">Не отправлять</button></div>' +
          "</section>",
        main: {
          text: "Отправить " + p.dat,
          disabled: !S.pairActive || !d.text.trim(),
          hint: !S.pairActive ? "Отправка откроется, когда " + p.nom + " примет приглашение" : !d.text.trim() ? "Текст пустой — нечего отправлять" : "",
          action: sendDraft,
          id: "send",
        },
      };
    },

    messages() {
      const p = P();
      const list = S.messages.filter((m) => m.from === S.persona || m.to === S.persona);
      // Открытие экрана = прочтение входящих
      list.forEach((m) => { if (m.to === S.persona && m.status === "delivered") m.status = "read"; });
      const STATUS = { delivered: "✓ доставлено", read: "✓✓ прочитано", replied: "↩ отвечено" };
      const items = list
        .map((m) => {
          if (m.from === S.persona) {
            return '<div class="sent" data-msg="sent" data-id="' + m.id + '">' + esc(m.original) +
              '<span class="sent__status" data-status="' + m.status + '">Ты · ' + m.time + " · " + STATUS[m.status] + "</span></div>";
          }
          const shown = me().showOriginal[m.id];
          const author = NAMES[m.from];
          return (
            '<article class="partner-card" data-msg="incoming" data-id="' + m.id + '">' +
            '<div class="partner-card__head"><span>От ' + esc(author.gen) + " · " + m.time + "</span>" +
            (m.adapted ? '<span class="badge" data-adapted>✎ адаптировано</span>' : '<span class="badge">без изменений</span>') + "</div>" +
            '<div class="partner-card__text" data-delivered>' + esc(m.delivered) + "</div>" +
            (m.adapted
              ? '<p class="small muted">Твой агент смягчил резкие слова. Смысл, факты и просьба ' + esc(author.gen) + " сохранены.</p>"
              : '<p class="small muted">Передано так, как ' + (author.fem ? "написала" : "написал") + " " + esc(author.nom) + ".</p>") +
            (shown ? '<div class="original" data-original><span class="original__label">Оригинал — как ' + (author.fem ? "написала и одобрила " : "написал и одобрил ") + esc(author.nom) + "</span>" + esc(m.original) + "</div>" : "") +
            '<div class="btn-row">' +
            (m.adapted ? '<button type="button" class="btn btn--ghost" data-action="toggle-original" data-id="' + m.id + '">' + (shown ? "Скрыть оригинал" : "Показать оригинал") + "</button>" : "") +
            '<button type="button" class="btn btn--fill" data-action="reply">Ответить с агентом</button>' +
            "</div></article>"
          );
        })
        .join("");
      return {
        html:
          '<section data-screen="messages">' +
          '<p class="small muted">Здесь только то, что вы с ' + esc(p.ins) + " отправили друг другу кнопкой «Отправить». Разговоры с агентами сюда не попадают.</p>" +
          (items ? '<div class="feed">' + items + "</div>" : '<div class="empty" data-empty><p><b>Пока сообщений нет</b></p><p class="small">Когда кто-то из вас отправит сообщение, оно появится здесь.</p></div>') +
          "</section>",
        main: null,
      };
    },

    memory() {
      const u = me();
      const cats = MEMORY_CATS.map(([key, label]) => {
        const items = u.memory.filter((m) => m.cat === key);
        const body = items.length
          ? items
              .map((m) =>
                '<div class="mem-item" data-mem="' + m.id + '"><div class="grow"><div>' + esc(m.text) +
                (key === "observation" ? ' <span class="badge">наблюдение, не твои слова</span>' : "") + "</div>" +
                '<div class="mem-item__meta">Источник: ' + esc(m.src) + "</div>" +
                (u.pendingDelete === m.id
                  ? '<div class="confirm"><span class="small">Удалить навсегда? Агент больше не вспомнит это.</span>' +
                    '<button type="button" class="btn btn--fill" data-action="mem-confirm" data-id="' + m.id + '">Удалить</button>' +
                    '<button type="button" class="btn btn--ghost" data-action="mem-cancel">Оставить</button></div>'
                  : "") +
                "</div>" +
                (u.pendingDelete === m.id ? "" : '<button type="button" class="mem-del" data-action="mem-del" data-id="' + m.id + '" aria-label="Удалить запись: ' + esc(m.text) + '">Удалить</button>') +
                "</div>")
              .join("")
          : '<p class="small muted">Пока пусто</p>';
        return '<div class="mem-cat"><h2>' + label + "</h2>" + body + "</div>";
      }).join("");
      return {
        html:
          '<section data-screen="memory">' +
          '<div class="note" data-private-note><span class="note__icon">🔒</span><span><b>Партнёр этого не видит.</b> Эти записи видишь только ты и твой агент. Любую можно удалить.</span></div>' +
          cats +
          "</section>",
        main: null,
      };
    },

    profile() {
      const u = me();
      const p = P();
      const sw = (key, label) =>
        '<div class="list__row"><span>' + label + '</span><button type="button" class="switch" role="switch" aria-checked="' + u.notify[key] + '" data-action="toggle-notify" data-key="' + key + '">' +
        '<span class="switch__text">' + (u.notify[key] ? "вкл" : "выкл") + '</span><span class="switch__track" aria-hidden="true"></span></button></div>';
      const sheet = u.sheet
        ? '<div class="sheet-backdrop" data-sheet="' + u.sheet + '"><div class="sheet" role="dialog" aria-modal="true">' +
          (u.sheet === "leave"
            ? "<h2 style=\"margin-top:0\">Выйти из пары?</h2><p>Обмен сообщениями с " + esc(p.ins) + " прекратится. Твой агент и твоя память останутся. " + esc(p.nom) + " увидит, что пары больше нет.</p>"
            : "<h2 style=\"margin-top:0\">Удалить аккаунт?</h2><p>Будут удалены профиль, разговоры с агентом, память и черновики — в течение 30 дней, включая резервные копии. Вернуть их будет нельзя.</p>") +
          '<div class="btn-row"><button type="button" class="btn btn--fill" data-action="sheet-confirm">' + (u.sheet === "leave" ? "Выйти из пары" : "Удалить аккаунт") + "</button>" +
          '<button type="button" class="btn" data-action="sheet-cancel">Отмена</button></div></div></div>'
        : "";
      return {
        html:
          '<section data-screen="profile">' +
          "<h2 style=\"margin-top:0\">Профиль</h2>" +
          '<div class="list">' +
          '<div class="list__row"><span>Имя</span><span class="muted">' + esc(u.form.name) + "</span></div>" +
          '<div class="list__row"><span>Как обращаться</span><span class="muted">' + esc(u.form.call) + "</span></div>" +
          "</div>" +
          "<h2>Пара</h2>" +
          '<div class="list">' +
          '<div class="list__row"><span>Партнёр</span><span class="muted">' + esc(p.name) + " · " + (S.pairActive ? "в паре" : "ждём ответа") + "</span></div>" +
          '<button type="button" class="list__row" data-action="sheet" data-sheet="leave"><span>Выйти из пары</span><span class="chev">›</span></button>' +
          "</div>" +
          "<h2>Уведомления в Telegram</h2>" +
          '<div class="list">' + sw("messages", "Новые сообщения от партнёра") + sw("billing", "Статус подписки") + "</div>" +
          '<p class="tiny muted">В уведомлениях нет текста сообщений.</p>' +
          "<h2>Мои данные</h2>" +
          '<div class="list">' +
          '<button type="button" class="list__row" data-action="export"><span>Выгрузить мои данные</span><span class="chev">›</span></button>' +
          '<button type="button" class="list__row" data-action="sheet" data-sheet="delete"><span>Удалить аккаунт и все данные</span><span class="chev">›</span></button>' +
          "</div>" +
          "</section>" + sheet,
        main: null,
      };
    },

    subscription() {
      const u = me();
      const sub = S.subscription;
      const paid = sub.status === "paid";
      const plans = [
        ["month", "Месяц", "390 ⭐"],
        ["year", "Год", "3 290 ⭐"],
      ];
      return {
        html:
          '<section data-screen="subscription">' +
          "<h1>Подписка на пару</h1>" +
          '<p class="muted">Платит один — доступ получают оба. Статус видите вы оба.</p>' +
          '<div class="card card--soft" data-sub="' + sub.status + '"><div class="row row--between"><b>Статус</b>' +
          (paid ? '<span class="badge badge--solid">активна</span>' : '<span class="badge">пробный период</span>') + "</div>" +
          '<p class="small" style="margin:8px 0 0">' +
          (paid
            ? "Активна до 21 декабря. " + (sub.paidBy === S.persona ? "Оплачено тобой." : "Оплатил" + (NAMES[sub.paidBy].fem ? "а " : " ") + NAMES[sub.paidBy].nom + ".")
            : "Осталось 7 дней — до 28 ноября. Отсчёт идёт с момента создания пары.") +
          "</p></div>" +
          (paid
            ? ""
            : "<h2>Тариф</h2>" +
              plans.map(([id, name, price]) =>
                '<button type="button" class="plan" role="radio" aria-checked="' + (u.plan === id) + '" data-action="plan" data-plan="' + id + '">' +
                '<span class="grow"><b>' + name + "</b>" + (id === "year" ? '<span class="small muted"> · выгоднее</span>' : "") + "</span>" +
                '<span class="plan__price">' + price + "</span>" + (u.plan === id ? '<span class="tiny">выбран</span>' : "") + "</button>").join("") +
              '<p class="tiny muted">Цены — пример для макета; окончательные задаются в настройках (ТЗ §11, [уточняется]). Оплата — Telegram Stars.</p>') +
          "<h2>Что входит</h2>" +
          '<ul class="small" style="padding-left:20px;margin:0 0 12px"><li>Разговоры с агентом без дневного лимита</li><li>Сообщения партнёру с адаптацией</li><li>Память агента</li></ul>' +
          '<div class="note"><span class="note__icon">i</span><span>Контакты помощи и кризисная поддержка работают без подписки. Когда подписка закончится, история и память сохранятся.</span></div>' +
          "</section>",
        main: paid
          ? null
          : {
              text: "Оплатить " + (u.plan === "year" ? "3 290 ⭐" : "390 ⭐") + " · Telegram Stars",
              action: () => { sub.status = "paid"; sub.paidBy = S.persona; toast("Окно оплаты Telegram Stars (имитация): оплачено. Подписка активна у вас обоих"); render(); },
            },
      };
    },

    help() {
      const contacts = [
        ["Экстренные службы", "000 (заглушка)", "если есть угроза жизни — звони сразу"],
        ["Круглосуточная психологическая помощь", "8 800 000-00-00 (заглушка)", "анонимно, бесплатно"],
        ["Помощь при домашнем насилии", "8 800 000-00-01 (заглушка)", "консультация и убежище"],
      ];
      return {
        html:
          '<section data-screen="help">' +
          '<div class="card card--soft" data-disclaimer><b>«Чувства AI» не заменяет психолога.</b><p class="small" style="margin:6px 0 0">Сервис не оказывает психологических и медицинских услуг и не ставит диагнозов. Если тяжело справляться — обратись к специалисту.</p></div>' +
          "<h2>Если тебе или кому-то рядом опасно</h2>" +
          '<div class="note note--stub" data-stub><span class="note__icon">!</span><span><b>Заглушка.</b> Номера ниже выдуманы. Настоящий список служб по странам проверяется перед запуском (ТЗ §9.3, [уточняется]).</span></div>' +
          contacts.map(([n, ph, d]) => '<div class="contact"><div>' + n + '</div><div class="contact__phone">' + ph + '</div><div class="small muted">' + d + "</div></div>").join("") +
          '<p class="small muted">Контакты помощи доступны всегда, даже без подписки.</p>' +
          "<h2>Правила</h2>" +
          '<ul class="small" style="padding-left:20px;margin:0 0 12px">' +
          "<li>Агент помогает сказать о чувствах — он не адвокат и не судья.</li>" +
          "<li>Ничего не уходит партнёру без твоего «Отправить».</li>" +
          "<li>Агент партнёра может смягчить форму, но не смысл; оригинал виден по запросу.</li>" +
          "<li>Твой разговор с агентом не видят ни партнёр, ни его агент.</li>" +
          "<li>При угрозах и насилии агент ничего не смягчает и подскажет, куда обратиться.</li></ul>" +
          "<h2>Документы</h2>" +
          '<div class="list">' +
          '<button type="button" class="list__row" data-action="doc"><span>Пользовательское соглашение</span><span class="chev">›</span></button>' +
          '<button type="button" class="list__row" data-action="doc"><span>Политика обработки данных</span><span class="chev">›</span></button>' +
          '<button type="button" class="list__row" data-action="doc"><span>Согласие на обработку чувствительных сведений</span><span class="chev">›</span></button>' +
          "</div>" +
          "</section>",
        main: null,
      };
    },
  };

  function draftMeta() {
    const d = me().draft;
    if (!d) return "";
    return "Вариант " + (d.idx + 1) + " из " + DRAFTS[S.persona].length + (d.edited ? " · изменено тобой" : " · от агента");
  }
  function draftHint() {
    const d = me().draft;
    if (!d) return "";
    const m = d.text.match(HARSH);
    if (!m) return "";
    return '<div class="note" data-harsh-hint><span class="note__icon">i</span><span>В тексте есть «' + esc(m[0]) + "». Агент " + esc(P().gen) +
      " может смягчить форму — смысл и просьба сохранятся, а оригинал " + esc(P().nom) + " сможет открыть.</span></div>";
  }

  // ---------- Отрисовка ----------
  let mainAction = null;
  function applyMain(def) {
    const wrap = document.getElementById("tg-main-wrap");
    const btn = document.getElementById("tg-main");
    const hint = document.getElementById("tg-main-hint");
    if (!def.main) { wrap.hidden = true; mainAction = null; return; }
    wrap.hidden = false;
    btn.textContent = def.main.text;
    btn.disabled = !!def.main.disabled;
    btn.setAttribute("aria-disabled", String(!!def.main.disabled));
    mainAction = def.main.action;
    if (def.main.hint) { hint.hidden = false; hint.textContent = def.main.hint; } else { hint.hidden = true; hint.textContent = ""; }
  }

  function render(scrollTop) {
    const scr = current();
    const def = screens[scr]();
    const content = document.getElementById("content");
    content.className = "content" + (def.chat ? " content--chat" : "");
    content.dataset.current = scr;
    content.innerHTML = def.html;
    document.getElementById("tg-title").innerHTML = esc(TITLES[scr]) + '<small>' + esc(N().name) + " · прототип</small>";
    const backBtn = document.getElementById("tg-back");
    const closeBtn = document.getElementById("tg-close");
    backBtn.hidden = !canGoBack();
    closeBtn.hidden = canGoBack();
    applyMain(def);
    document.querySelectorAll("[data-persona]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.persona === S.persona)));
    document.getElementById("jump").value = "";
    if (def.chat) content.scrollTop = content.scrollHeight;
    else if (scrollTop) content.scrollTop = 0;
  }

  function setTheme(t) {
    S.theme = t;
    if (t === "auto") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", t);
    document.querySelectorAll("[data-theme-set]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.themeSet === t)));
    try { localStorage.setItem("proto-theme", t); } catch (e) { /* хранилище недоступно */ }
  }

  // ---------- События ----------
  document.addEventListener("click", (e) => {
    const t = e.target.closest("button, [data-action]");
    if (!t) return;
    if (t.id === "tg-back") { haptic(t); back(); return; }
    if (t.id === "tg-close") { toast("В Telegram это закрыло бы Mini App"); return; }
    if (t.id === "tg-main") { if (!t.disabled && mainAction) { haptic(t); mainAction(); } return; }
    if (t.dataset.persona) { S.persona = t.dataset.persona; document.getElementById("toast").classList.remove("show"); render(true); return; }
    if (t.dataset.themeSet) { setTheme(t.dataset.themeSet); return; }
    const u = me();
    switch (t.dataset.action) {
      case "go": go(t.dataset.to); break;
      case "to-home": resetTo("home"); break;
      case "suggest": u.input = t.dataset.text; u.inputVoice = false; render(); document.getElementById("chat-input").focus(); break;
      case "send-agent": sendToAgent(); break;
      case "mic":
        haptic(t);
        if (!u.recording) { u.recording = true; render(); }
        else {
          u.recording = false;
          const sc = SCRIPT[S.persona];
          u.input = sc.suggest[Math.min(u.stage, sc.suggest.length - 1)];
          u.inputVoice = true;
          toast("Голосовое расшифровано — проверь текст перед отправкой агенту");
          render();
        }
        break;
      case "opt": chooseOption(t.dataset.kind); break;
      case "edit": { const ta = document.getElementById("draft-text"); ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); break; }
      case "example-edit":
        if (u.draft && u.draft.text.indexOf(EXAMPLE_EDIT) === -1) { u.draft.text = u.draft.text.trim() + " " + EXAMPLE_EDIT; u.draft.edited = true; }
        render();
        break;
      case "variant": {
        const n = DRAFTS[S.persona].length;
        const idx = (u.draft.idx + 1) % n;
        u.draft = { idx, text: DRAFTS[S.persona][idx].text, edited: false };
        toast("Агент предложил другой вариант");
        render();
        break;
      }
      case "discard":
        u.draft = null;
        u.chat.push({ from: "agent", text: "Хорошо, ничего не отправляю — черновик удалён. Иногда важно просто проговорить. Я рядом, если захочешь вернуться к этому." });
        u.stack = u.stack.filter((s) => s !== "draft");
        if (current() !== "chat") u.stack.push("chat");
        toast("Не отправлено. Черновик удалён");
        render(true);
        break;
      case "toggle-original": {
        const id = t.dataset.id;
        u.showOriginal[id] = !u.showOriginal[id];
        const m = S.messages.find((x) => x.id === id);
        if (m && u.showOriginal[id]) m.originalViewed = true;
        render();
        break;
      }
      case "reply": replyWithAgent(); break;
      case "mem-del": u.pendingDelete = t.dataset.id; render(); break;
      case "mem-cancel": u.pendingDelete = null; render(); break;
      case "mem-confirm":
        u.memory = u.memory.filter((m) => m.id !== t.dataset.id);
        u.pendingDelete = null;
        haptic(t);
        toast("Запись удалена. Агент её больше не вспомнит");
        render();
        break;
      case "toggle-notify": u.notify[t.dataset.key] = !u.notify[t.dataset.key]; render(); break;
      case "sheet": u.sheet = t.dataset.sheet; render(); break;
      case "sheet-cancel": u.sheet = null; render(); break;
      case "sheet-confirm": u.sheet = null; toast("В прототипе это действие не выполняется"); render(); break;
      case "export": toast("Файл с твоими данными придёт в чат с ботом (имитация)"); break;
      case "plan": u.plan = t.dataset.plan; render(); break;
      case "doc": toast("Текст документа готовит юрист — пока заглушка"); break;
      default: break;
    }
  });

  document.addEventListener("input", (e) => {
    const el = e.target;
    const model = el.dataset && el.dataset.model;
    if (!model) return;
    const u = me();
    const val = el.type === "checkbox" ? el.checked : el.value;
    if (model.startsWith("form.")) {
      u.form[model.slice(5)] = val;
      applyMain(screens.onboarding());
    } else if (model === "input") {
      u.input = val;
      u.inputVoice = false;
    } else if (model === "draft.text" && u.draft) {
      u.draft.text = val;
      u.draft.edited = true;
      document.getElementById("draft-meta").textContent = draftMeta();
      document.getElementById("draft-hint").innerHTML = draftHint();
      applyMain(screens.draft());
    }
  });
  document.addEventListener("keydown", (e) => {
    if (e.target.id === "chat-input" && e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendToAgent(); }
  });

  document.getElementById("jump").addEventListener("change", (e) => {
    const v = e.target.value;
    if (!v) return;
    if (v === "draft") { openDraft(); return; }
    if (v === "welcome") me().slide = 0;
    go(v);
  });

  // ---------- Старт ----------
  let saved = "auto";
  try { saved = localStorage.getItem("proto-theme") || "auto"; } catch (e) { /* хранилище недоступно */ }
  setTheme(saved);
  render(true);
})();
