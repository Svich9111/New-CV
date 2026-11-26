// Отметка загрузки SDK Telegram для приложения (src/telegram/webapp.ts, waitForTelegramSdk).
// Внешний файл вместо onload/onerror в разметке: строгий CSP без 'unsafe-inline'/'unsafe-hashes' (n-3).
// Подключается обычным (не async) скриптом ДО тега SDK: слушатели на document в фазе захвата ловят
// load/error скрипта SDK, когда бы они ни случились, — гонки «скрипт успел до подписки» нет.
(function () {
  "use strict";
  var ID = "telegram-web-app-sdk";
  function mark(state) {
    return function (event) {
      var el = event.target;
      if (el && el.id === ID && el.dataset && !el.dataset.state) el.dataset.state = state;
    };
  }
  document.addEventListener("load", mark("loaded"), true);
  document.addEventListener("error", mark("failed"), true);
})();
