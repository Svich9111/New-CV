import type { ApiError } from "../api/client";
import { MainButton } from "../telegram/MainButton";
import { Heading } from "../ui/Heading";
import { Notice } from "../ui/Notice";

/** Вход не удался (ФТ-01): причина подписью; повтор — там, где он может помочь. */
export function AuthError({ error, onRetry }: { error: ApiError; onRetry: () => void }) {
  const retryable = error.status === 0 || error.status >= 500 || error.status === 429 || error.code === "conflict_retry";
  return (
    <section data-screen="auth-error">
      <Heading>Не получилось войти</Heading>
      <Notice kind="error">{error.message}</Notice>
      {retryable && <MainButton text="Попробовать снова" onClick={onRetry} />}
    </section>
  );
}
