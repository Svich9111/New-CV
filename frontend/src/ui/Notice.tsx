import type { ReactNode } from "react";

/**
 * Пояснение или ошибка. Ошибка — нейтральная плашка со значком «!» и подписью, не красная
 * (ТЗ §8: красный не используется для ошибок); состояние передаётся текстом, а не цветом.
 */
export function Notice({ kind = "info", children }: { kind?: "info" | "error"; children: ReactNode }) {
  return (
    <div className={`note note--${kind}`} role={kind === "error" ? "alert" : undefined}>
      <span className="note__icon" aria-hidden="true">
        {kind === "error" ? "!" : "i"}
      </span>
      <span>
        {kind === "error" && <span className="visually-hidden">Ошибка: </span>}
        {children}
      </span>
    </div>
  );
}

export function Loading({ children = "Загружаем…" }: { children?: ReactNode }) {
  return (
    <p className="muted" role="status">
      {children}
    </p>
  );
}

/** Сердце — знак близости, один из двух случаев красного акцента (ТЗ §8). */
export function Heart({ size = 14 }: { size?: number }) {
  return (
    <svg className="heart" width={size} height={size} viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M12 21s-7.5-4.6-10-9.3C.4 8.4 2.3 4 6.4 4c2.3 0 3.8 1.3 5.6 3.3C13.8 5.3 15.3 4 17.6 4c4.1 0 6 4.4 4.4 7.7C19.5 16.4 12 21 12 21z" />
    </svg>
  );
}
