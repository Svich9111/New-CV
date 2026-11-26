import { useLayoutEffect, useRef } from "react";
import { useTelegram } from "./context";

/**
 * Кнопка «Назад»: нативная BackButton Telegram (ТЗ §8); в старых клиентах и вне Telegram —
 * кнопка в шапке страницы. `onBack = null` — назад некуда, кнопка скрыта.
 */
export function BackButton({ onBack }: { onBack: (() => void) | null }) {
  const tg = useTelegram();
  const native = tg.nativeBack ? tg.webApp : null;
  const ref = useRef(onBack);
  const visible = onBack !== null;

  useLayoutEffect(() => {
    ref.current = onBack;
  });

  useLayoutEffect(() => {
    if (!native) return;
    const bb = native.BackButton;
    if (!visible) {
      bb.hide();
      return;
    }
    const handler = () => ref.current?.();
    bb.onClick(handler);
    bb.show();
    return () => {
      bb.offClick(handler);
      bb.hide();
    };
  }, [native, visible]);

  if (native || !visible) return null;
  return (
    <button type="button" className="back-button" onClick={() => ref.current?.()}>
      ‹ Назад
    </button>
  );
}
