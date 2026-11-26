import { useLayoutEffect, useRef } from "react";
import { useTelegram, useThemeVersion } from "./context";
import { cssToken } from "./webapp";

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
 */
export function MainButton({ text, onClick, disabled = false, progress = false }: Props) {
  const tg = useTelegram();
  const wa = tg.webApp;
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
    const mb = wa.MainButton;
    const handler = () => press.current(() => tg.impact("light"));
    mb.onClick(handler);
    mb.show();
    return () => {
      mb.offClick(handler);
      mb.hideProgress();
      mb.hide();
    };
  }, [wa, tg]);

  useLayoutEffect(() => {
    if (!wa) return;
    const mb = wa.MainButton;
    mb.setText(text);
    const active = !disabled;
    // Недоступная кнопка выглядит серой: состояние видно не только по отсутствию реакции.
    const color = cssToken(active ? "--accent" : "--disabled-bg");
    const textColor = cssToken(active ? "--on-accent" : "--disabled-text");
    if (color && textColor) mb.setParams?.({ color, text_color: textColor });
    if (active) mb.enable();
    else mb.disable();
    if (progress) mb.showProgress(false);
    else mb.hideProgress();
  }, [wa, text, disabled, progress, theme]);

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
