import { render } from "@testing-library/react";
import { Markdown } from "./markdown";

const html = (text: string) => render(<Markdown text={text} />).container;

describe("Markdown", () => {
  it("renders bold, italic, code and line breaks", () => {
    const el = html("**Good news:** it's *on the way*.\nTrack it with `NW-10002`.");
    expect(el.querySelector("strong")?.textContent).toBe("Good news:");
    expect(el.querySelector("em")?.textContent).toBe("on the way");
    expect(el.querySelector("code")?.textContent).toBe("NW-10002");
    expect(el.querySelectorAll("br")).toHaveLength(1);
  });

  it("renders bullet and numbered lists", () => {
    const el = html("- one\n- two\n\n1. first\n2. second");
    expect(el.querySelectorAll("ul li")).toHaveLength(2);
    expect(el.querySelectorAll("ol li")).toHaveLength(2);
  });

  it("links only http(s) URLs and opens them safely", () => {
    const el = html("[guide](https://help.northwind.example/a) and [bad](javascript:alert(1))");
    const links = el.querySelectorAll("a");
    expect(links).toHaveLength(1);
    expect(links[0].getAttribute("href")).toBe("https://help.northwind.example/a");
    expect(links[0].getAttribute("target")).toBe("_blank");
    expect(links[0].getAttribute("rel")).toBe("noopener noreferrer");
    expect(el.textContent).toContain("[bad](javascript:alert(1))");
  });

  it("never interprets HTML in the text", () => {
    const el = html('<img src=x onerror="alert(1)"> <script>alert(1)</script>');
    expect(el.querySelector("img")).toBeNull();
    expect(el.querySelector("script")).toBeNull();
    expect(el.textContent).toContain("<img");
  });

  it("does not treat arithmetic as emphasis", () => {
    expect(html("2 * 3 * 4").querySelector("em")).toBeNull();
  });
});
