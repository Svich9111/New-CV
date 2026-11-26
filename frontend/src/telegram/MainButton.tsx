import { useLayoutEffect, useRef, useState } from "react";
import { useTelegram, useThemeVersion } from "./context";
import { cssToken, safely } from "./webapp";

/** Второе касание в течение этого времени игнорируется — пока экран не успел показать «идёт запрос». */
export const DOUBLE_TAP_MS = 500;

interface Props {
  text: string;
  onClick: () => void;
  disabled?: boolean;
  /** Идёт запрос: кнопка показывает индикатор и не принимает нажатий (защита от двойного касания). */
  progress?: boolean;
}

/**
 * Главное действие экрана — нативная MainButton Telegram (ТЗ §8). Красный акцент — только здесь.
 * Вне Telegram (или в тестах без SDK) — запасная кнопка в странице с тем же поведением.
 * На экране одновременно не больше одной MainButton. Нативная кнопка обновляется в той же фиксации, что и
 * страница (layout-эффект): экран и кнопка Telegram не расходятся ни на кадр.
 * Каждый вызов SDK защищён (`TelegramApi.withMain`): если метод нативной кнопки бросает, экран не падает —
 * вместо неё рисуется запасная кнопка в странице (A8-1.5-D6).
 */
export function MainButton({ text, onClick, disabled = false, progress = false }: Props) {
  const tg = useTelegram();
  const [native, setNative] = useState(() => tg.nativeMain);
  const wa = native ? tg.webApp : null;
  // Цвета кнопки — из токенов текущей темы: при смене темы на лету они перечитываются.
  const theme = useThemeVersion();
  const state = useRef({ onClick, blocked: disabled || progress });
  const lastTap = useRef(0);

  // Одна точка входа для нативной и запасной кнопки: недоступна, идёт запрос или двойное касание — мимо.
  const press = useRef((haptic: () => void) => {
    const now = Date.now();
    if (state.current.blocked || now - lastTap.current < DOUBLE_TAP_MS) return;
    lastTap.current = now;
    haptic();
    state.current.onClick();
  });

  useLayoutEffect(() => {
    state.current = { onClick, blocked: disabled || progress };
  });

  useLayoutEffect(() => {
    if (!wa) return;
    const handler = () => press.current(() => tg.impact("light"));
    const ok = tg.withMain((mb) => {
      mb.onClick(handler);
      mb.show();
    });
    if (!ok) {
      safely(() => wa.MainButton.offClick(handler));
      setNative(false);
      return;
    }
    return () => {
      // Снятие обработчика — отдельно от скрытия: отказ hide() не оставляет живой обработчик прошлого экрана.
      safely(() => wa.MainButton.offClick(handler));
      tg.withMain((mb) => {
        mb.hideProgress();
        mb.hide();
      });
    };
  }, [wa, tg]);

  useLayoutEffect(() => {
    if (!wa) return;
    const active = !disabled;
    // Недоступная кнопка выглядит серой: состояние видно не только по отсутствию реакции.
    const color = cssToken(active ? "--accent" : "--disabled-bg");
    const textColor = cssToken(active ? "--on-accent" : "--disabled-text");
    const ok = tg.withMain((mb) => {
      mb.setText(text);
      if (color && textColor) mb.setParams?.({ color, text_color: textColor });
      if (active) mb.enable();
      else mb.disable();
      if (progress) mb.showProgress(false);
      else mb.hideProgress();
    });
    if (!ok) setNative(false);
  }, [wa, tg, text, disabled, progress, theme]);

  if (wa) return null;
  return (
    <button
      type="button"
      className="main-button"
      disabled={disabled || progress}
      aria-busy={progress || undefined}
      onClick={() => press.current(() => undefined)}
    >
      {progress ? "Подожди…" : text}
    </button>
  );
}
