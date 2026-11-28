/**
 * A8, связка 0021 / АР-57 (задача 3.4, раунд 3 клиента) — экран «Сообщения пары» под остановленным безопасностью
 * (ТЗ §9.3; ФТ-11; принцип 4 и 7). Все 36 строк сервера (32 сочетания и 4 порядка Б-103, а не 8 показательных): ровно эти блоки по порядку, под
 * ними 112; при нарушении контракта — консервативный набор этого сообщения, соседние не затронуты; 112 — и когда
 * `/help/contacts` недоступен; блоки у сообщения партнёра не показываются. Тексты выдуманные.
 */
import { screen, waitFor, within } from "@testing-library/react";
import { CRISIS_BLOCKS, crisisBlocks, type CrisisBlock } from "../couple/crisis";
import { SERVER_CRISIS_CASES } from "../test/crisisCases";
import { FakeServer } from "../test/fakeApi";
import { heading, renderApp, type Rendered } from "../test/renderApp";

function pairedServer(): FakeServer {
  const server = new FakeServer().onboarded().pendingCouple();
  server.partnerAccepts();
  return server;
}

async function openFeed(server: FakeServer): Promise<Rendered> {
  const r = renderApp({ server });
  await heading("Вы пара");
  await r.user.click(screen.getByRole("button", { name: /Сообщения пары/ }));
  await heading("Сообщения пары");
  await waitFor(() => expect(screen.queryByText("Загружаем сообщения…")).toBeNull());
  return r;
}

const item = (id: string) => document.querySelector(`[data-id="${id}"]`) as HTMLElement;
const texts = (el: HTMLElement) => Array.from(el.querySelectorAll(".crisis__text")).map((p) => p.textContent);
const ALL = Object.keys(CRISIS_BLOCKS) as CrisisBlock[];

describe("A8 · все 36 строк сервера на экране — 32 сочетания и 4 порядка Б-103 (АР-57)", () => {
  test.each(SERVER_CRISIS_CASES.map((c) => [`${c.types.join("+")}@${c.main}${c.first ? `^${c.first}` : ""}`, c] as const))(
    "%s — ровно свой набор по порядку, вне набора ничего, 112 после блоков",
    async (_n, c) => {
      const server = pairedServer();
      const row = server.addFeed({
        author: "me",
        original: "Остановленное",
        status: "withheld",
        withheld_reason: "safety",
        withheld_crisis: c.main,
        withheld_crisis_blocks: [...c.blocks],
      });
      await openFeed(server);
      const li = item(row.id);
      const help = within(li).getByRole("region", { name: "Контакты помощи" });
      expect(help).toHaveAttribute("data-protocol", "server");
      expect(texts(help)).toEqual(c.blocks.map((b) => CRISIS_BLOCKS[b]));
      for (const b of ALL) if (!c.blocks.includes(b)) expect(li).not.toHaveTextContent(CRISIS_BLOCKS[b]);
      const tel = await within(help).findByRole("link", { name: "112" });
      expect(tel).toHaveAttribute("href", "tel:112");
      const last = help.querySelectorAll(".crisis__text")[c.blocks.length - 1];
      expect(last.compareDocumentPosition(tel) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    },
  );
});

describe("A8 · 112 всегда и откат по одному сообщению", () => {
  test("/help/contacts отвечает 500 — 112 есть и под набором сервера, и под консервативным", async () => {
    const server = pairedServer().inject(
      "GET",
      "/help/contacts",
      500,
      { code: "internal", message: "x" },
      undefined,
      5,
    );
    const a = server.addFeed({
      author: "me",
      original: "Остановленное А",
      status: "withheld",
      withheld_reason: "safety",
      withheld_crisis: "self_harm",
      withheld_crisis_blocks: ["not_sent", "life_threat", "self_harm"],
    });
    const b = server.addFeed({
      author: "me",
      original: "Остановленное Б",
      status: "withheld",
      withheld_reason: "safety",
      withheld_crisis: null,
    });
    await openFeed(server);
    for (const row of [a, b]) {
      const help = within(item(row.id)).getByRole("region", {
        name: "Контакты помощи",
      });
      expect(await within(help).findByRole("link", { name: "112" })).toHaveAttribute("href", "tel:112");
    }
  });

  test("в одной ленте: набор сервера, null, нарушение и сообщение партнёра с блоками — каждое по-своему", async () => {
    const server = pairedServer();
    const good = server.addFeed({
      author: "me",
      original: "Хорошее",
      status: "withheld",
      withheld_reason: "safety",
      withheld_crisis: "violence",
      withheld_crisis_blocks: ["not_sent", "violence", "self_harm"],
    });
    const old = server.addFeed({
      author: "me",
      original: "Старое",
      status: "withheld",
      withheld_reason: "safety",
      withheld_crisis: "life_threat",
    });
    const bad = server.addFeed({
      author: "me",
      original: "Испорченное",
      status: "withheld",
      withheld_reason: "safety",
      withheld_crisis: "threat_outgoing",
      extra: { withheld_crisis_blocks: ["not_sent", "violence", "violence"] },
    });
    const partner = server.addFeed({
      author: "partner",
      original: "Привет от партнёра",
      status: "delivered",
      extra: { withheld_crisis_blocks: ALL },
    });
    const refused = server.addFeed({
      author: "me",
      original: "Отказ",
      status: "withheld",
      withheld_reason: "model_refusal",
      extra: { withheld_crisis_blocks: ALL },
    });
    await openFeed(server);
    const region = (id: string) => within(item(id)).queryByRole("region", { name: "Контакты помощи" });

    expect(region(good.id)).toHaveAttribute("data-protocol", "server");
    expect(texts(region(good.id)!)).toEqual(
      ["not_sent", "violence", "self_harm"].map((x) => CRISIS_BLOCKS[x as CrisisBlock]),
    );

    expect(region(old.id)).toHaveAttribute("data-protocol", "conservative");
    expect(texts(region(old.id)!)).toEqual(crisisBlocks("life_threat").map((x) => CRISIS_BLOCKS[x]));

    expect(region(bad.id)).toHaveAttribute("data-protocol", "conservative");
    expect(texts(region(bad.id)!)).toEqual(crisisBlocks("threat_outgoing").map((x) => CRISIS_BLOCKS[x]));
    expect(item(bad.id)).not.toHaveTextContent(CRISIS_BLOCKS.violence);

    // Партнёр и другая причина — ни одного блока протокола, ни региона кризиса.
    for (const id of [partner.id, refused.id]) {
      expect(region(id)).toBeNull();
      for (const x of ALL) expect(item(id)).not.toHaveTextContent(CRISIS_BLOCKS[x]);
    }
    expect(item(partner.id)).toHaveTextContent("Привет от партнёра");
  });
});
