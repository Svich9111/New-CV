import { Heading } from "../ui/Heading";
import { Loading, Notice } from "../ui/Notice";

/** Вне Telegram: нет подписанного initData — войти нельзя. Помощь доступна и здесь. */
export function OpenInTelegram() {
  return (
    <section data-screen="outside">
      <Heading>Открой в Telegram</Heading>
      <p className="lead">«Чувства AI» работает внутри Telegram: вход — по твоему аккаунту, без пароля.</p>
      <ol className="steps">
        <li>Открой бота «Чувства AI» в Telegram.</li>
        <li>Нажми кнопку «Открыть» или перейди по ссылке-приглашению от партнёра.</li>
      </ol>
    </section>
  );
}

/** Открыто из Telegram, а SDK Telegram ещё грузится (медленная сеть, n-2): ждём, «Помощь» уже доступна. */
export function WaitingForTelegram() {
  return (
    <section data-screen="sdk-waiting">
      <Heading>Чувства AI</Heading>
      <Loading>Загружаем «Чувства AI»… Связь медленная — это может занять немного времени.</Loading>
    </section>
  );
}

/** Открыто из Telegram, но SDK Telegram не загрузился: причина подписью и повтор (кнопка в странице). */
export function TelegramUnavailable() {
  return (
    <section data-screen="sdk-failed">
      <Heading>Не получилось связаться с Telegram</Heading>
      <Notice kind="error">
        Приложение не загрузило модуль Telegram — скорее всего, нет связи. Проверь интернет и попробуй снова.
      </Notice>
      <button type="button" className="btn btn--block btn--ghost" onClick={() => window.location.reload()}>
        Попробовать снова
      </button>
    </section>
  );
}
