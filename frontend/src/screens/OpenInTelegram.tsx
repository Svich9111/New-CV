import { Heading } from "../ui/Heading";

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
