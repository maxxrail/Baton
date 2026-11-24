import { act, fireEvent, render, screen } from "@testing-library/react";
import { Widget } from "./Widget";

// Mounts the real widget and opens it, with the network faked. This caught a crash that the
// rendering tests missed: Chrome's scrollIntoView returns a Promise, and an effect that
// returned it took the whole widget down on open.

class FakeSocket {
  static OPEN = 1;
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  sent: string[] = [];
  constructor(public url: string) {
    sockets.push(this);
  }
  send(data: string) {
    this.sent.push(data);
  }
  close() {
    this.onclose?.();
  }
}
let sockets: FakeSocket[] = [];

beforeEach(() => {
  sockets = [];
  const store = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
  });
  Element.prototype.scrollIntoView = () => Promise.resolve() as unknown as void;
  vi.stubGlobal("WebSocket", FakeSocket);
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/conversations") {
        return new Response(JSON.stringify({ id: "c1", channel: "web", status: "open" }), {
          status: 201,
        });
      }
      return new Response("{}", { status: 404 });
    }),
  );
});

afterEach(() => vi.unstubAllGlobals());

describe("Widget", () => {
  it("opens, starts a conversation, connects and shows pushed replies", async () => {
    render(<Widget />);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Chat with Northwind Goods" }));
    });
    expect(screen.getByRole("region", { name: "Northwind Goods chat" })).toBeTruthy();
    expect(window.localStorage.getItem("baton.conversation")).toBe("c1");
    expect(sockets[0].url).toMatch(/\/ws\/chat\/c1$/);

    const ws = sockets[0];
    await act(async () => {
      ws.readyState = FakeSocket.OPEN;
      ws.onopen?.();
    });
    fireEvent.change(screen.getByRole("textbox", { name: "Message" }), {
      target: { value: "where is my order?" },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Send" }));
    });
    expect(JSON.parse(ws.sent[0])).toEqual({ type: "message", text: "where is my order?" });
    expect(screen.getByRole("status").textContent).toContain("Reading your message");

    await act(async () => {
      ws.onmessage?.({ data: JSON.stringify({ type: "typing", activity: "tool:get_order" }) });
    });
    expect(screen.getByRole("status").textContent).toContain("Looking up your order");

    await act(async () => {
      ws.onmessage?.({
        data: JSON.stringify({
          type: "message",
          message: { id: 9, role: "agent", body: "It shipped **today**.", citations: [] },
        }),
      });
    });
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.getByText("today").tagName).toBe("STRONG");
  });
});
