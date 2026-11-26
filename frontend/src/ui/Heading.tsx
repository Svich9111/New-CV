import { useLayoutEffect, useRef, type ReactNode } from "react";

/** Заголовок экрана; при открытии экрана фокус переходит на него (экранные дикторы читают новый экран). */
export function Heading({ children }: { children: ReactNode }) {
  const ref = useRef<HTMLHeadingElement>(null);
  useLayoutEffect(() => {
    ref.current?.focus({ preventScroll: true });
  }, []);
  return (
    <h1 ref={ref} tabIndex={-1}>
      {children}
    </h1>
  );
}
