import { render, screen } from "@testing-library/react";
import type { ChatMessage } from "../lib/api";
import { MessageBubble, activityLabel } from "./MessageBubble";
import { addMessage } from "./Widget";

const URL_INTL = "https://help.northwind.example/articles/shipping-international";
const URL_RET = "https://help.northwind.example/articles/return-policy";

// Exactly what the server sends for a cited answer (baton.channels.base.WebAdapter).
const cited: ChatMessage = {
  id: 7,
  role: "agent",
  body:
    `Shipping to Canada takes 6-10 business days [1](${URL_INTL}). ` +
    `Returns are accepted within 30 days [2](${URL_RET}).\n\n` +
    `Sources:\n1. [International orders and shipping to Canada](${URL_INTL})\n` +
    `2. [Return policy](${URL_RET})`,
  citations: [
    { article_id: "shipping-international", title: "International orders and shipping to Canada", url: URL_INTL },
    { article_id: "return-policy", title: "Return policy", url: URL_RET },
  ],
};

describe("MessageBubble", () => {
  it("renders citation markers as numbered links to the articles", () => {
    render(<MessageBubble message={cited} />);
    const markers = screen.getAllByRole("link", { name: /^Source \d$/ });
    expect(markers.map((a) => a.textContent)).toEqual(["[1]", "[2]"]);
    expect(markers.map((a) => a.getAttribute("href"))).toEqual([URL_INTL, URL_RET]);
    expect(markers[0].closest("sup")).not.toBeNull();
  });

  it("lists the sources with their titles", () => {
    render(<MessageBubble message={cited} />);
    const source = screen.getByRole("link", { name: "Return policy" });
    expect(source.getAttribute("href")).toBe(URL_RET);
    expect(screen.getByText("Sources:")).toBeTruthy();
    expect(screen.getByRole("link", { name: "International orders and shipping to Canada" })).toBeTruthy();
  });

  it("shows customer text literally, without markdown", () => {
    const { container } = render(
      <MessageBubble message={{ id: 1, role: "customer", body: "**urgent** [x](https://evil.example)", citations: [] }} />,
    );
    expect(container.querySelector("strong")).toBeNull();
    expect(container.querySelector("a")).toBeNull();
    expect(container.textContent).toContain("**urgent**");
  });

  it("labels agent and teammate replies differently", () => {
    render(<MessageBubble message={{ id: 2, role: "human_agent", body: "Hi, Sam here.", citations: [] }} />);
    expect(screen.getByText("Northwind teammate")).toBeTruthy();
  });
});

describe("widget helpers", () => {
  it("does not show a pushed message twice", () => {
    const one: ChatMessage = { id: 3, role: "agent", body: "hi", citations: [] };
    expect(addMessage(addMessage([], one), one)).toHaveLength(1);
    const unsaved: ChatMessage = { id: null, role: "agent", body: "hi", citations: [] };
    expect(addMessage([unsaved], unsaved)).toHaveLength(2);
  });

  it("describes what the agent is doing", () => {
    expect(activityLabel("tool:get_order")).toBe("Looking up your order…");
    expect(activityLabel("tool:something_new")).toBe("Working on it…");
    expect(activityLabel("thinking")).toBe("Typing…");
  });
});
