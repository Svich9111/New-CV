import { render, screen } from "@testing-library/react";
import { App } from "./App";

test("renders product name", () => {
  render(<App />);
  expect(screen.getByRole("heading", { name: "Чувства AI" })).toBeTruthy();
});
