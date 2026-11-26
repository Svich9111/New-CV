import { useLayoutEffect, useRef, useState } from "react";
import { useTelegram } from "./context";
import { safely } from "./webapp";

/**
 * Кнопка «Назад»: нативная BackButton Telegram (ТЗ §8); в старых клиентах, вне Telegram и если метод
 * нативной кнопки отказал (A8-1.5-D6) — кнопка в шапке страницы. `onBack = null` — назад некуда, кнопка скрыта.
 */
export function BackButton({ onBack }: { onBack: (() => void) | null }) {
  const tg = useTelegram();
  const [nativeOk, setNativeOk] = useState(() => tg.nativeBack);
  const native = nativeOk ? tg.webApp : null;
  const ref = useRef(onBack);
  const visible = onBack !== null;

  useLayoutEffect(() => {
    ref.current = onBack;
  });

  useLayoutEffect(() => {
    if (!native) return;
    if (!visible) {
      if (!tg.withBack((bb) => bb.hide())) setNativeOk(false);
      return;
    }
    const handler = () => ref.current?.();
    const ok = tg.withBack((bb) => {
      bb.onClick(handler);
      bb.show();
    });
    if (!ok) {
      safely(() => native.BackButton.offClick(handler));
      setNativeOk(false);
      return;
    }
    return () => {
      safely(() => native.BackButton.offClick(handler));
      tg.withBack((bb) => bb.hide());
    };
  }, [native, tg, visible]);

  if (native || !visible) return null;
  return (
    <button type="button" className="back-button" onClick={() => ref.current?.()}>
      ‹ Назад
    </button>
  );
}
