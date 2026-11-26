import { WELCOME_SLIDES } from "../app/routes";
import { useApp } from "../app/store";
import { MainButton } from "../telegram/MainButton";
import { Heading } from "../ui/Heading";
import { Heart } from "../ui/Notice";

const SLIDES = [
  {
    art: <Heart size={44} />,
    title: "Твой личный агент",
    text: "Он выслушает без осуждения и поможет сказать партнёру о том, что беспокоит, — на языке чувств, а не упрёков.",
  },
  {
    art: "🔒",
    title: "Что приватно",
    text: "Разговор с агентом видишь только ты. Партнёр и его агент получат только те сообщения, которые ты сам отправишь кнопкой «Отправить».",
  },
  {
    art: "🤝",
    title: "Чем мы не являемся",
    text: "«Чувства AI» не заменяет психолога и не оказывает психологических и медицинских услуг. Если тебе или кому-то рядом угрожает опасность — в разделе «Помощь» есть контакты служб поддержки.",
  },
];

/** Приветствие: три слайда (ТЗ §8) — что делает агент, что приватно, что продукт не заменяет психолога. */
export function Welcome({ slide }: { slide: number }) {
  const { push } = useApp();
  const s = SLIDES[slide] ?? SLIDES[0];
  const last = slide >= WELCOME_SLIDES - 1;
  return (
    <section className="slide" data-screen="welcome" data-slide={slide + 1}>
      <div className="slide__art" aria-hidden="true">
        {s.art}
      </div>
      <Heading>{s.title}</Heading>
      <p className="lead">{s.text}</p>
      <div className="dots" aria-hidden="true">
        {SLIDES.map((_, i) => (
          <span key={i} className={i === slide ? "on" : undefined} />
        ))}
      </div>
      <p className="small muted">
        Слайд {slide + 1} из {WELCOME_SLIDES}
      </p>
      <MainButton
        text={last ? "Начать" : "Далее"}
        onClick={() => push(last ? { name: "onboarding" } : { name: "welcome", slide: slide + 1 })}
      />
    </section>
  );
}
