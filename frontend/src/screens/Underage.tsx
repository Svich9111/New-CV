import { Heading } from "../ui/Heading";

/** Младше 18 — доступ не предоставляется (ТЗ §9.3). */
export function Underage() {
  return (
    <section data-screen="underage">
      <Heading>Сервис доступен только с 18 лет</Heading>
      <p>«Чувства AI» рассчитан на взрослых. Если тебе нужна поддержка прямо сейчас — загляни в «Помощь».</p>
    </section>
  );
}
