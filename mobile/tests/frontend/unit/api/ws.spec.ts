import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { ChatWebSocket } from "../../../../frontend/src/api/ws";

// Minimal fake WebSocket for driving ChatWebSocket in tests.
class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  readyState = FakeWebSocket.OPEN;
  url: string;
  onopen: (() => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  sentPayloads: unknown[] = [];

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
    // Simulate async open on next tick
    queueMicrotask(() => this.onopen?.());
  }

  send(data: unknown) {
    this.sentPayloads.push(data);
  }

  close() {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.(new CloseEvent("close"));
  }

  // Simulate the server closing the socket with a specific code.
  closeWith(code: number) {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.(new CloseEvent("close", { code }));
  }

  emit(data: unknown) {
    this.onmessage?.(new MessageEvent("message", { data: JSON.stringify(data) }));
  }
}

describe("ChatWebSocket", () => {
  const originalWs = window.WebSocket;
  beforeEach(() => {
    FakeWebSocket.instances = [];
    (window as unknown as { WebSocket: unknown }).WebSocket = FakeWebSocket;
  });
  afterEach(() => {
    (window as unknown as { WebSocket: unknown }).WebSocket = originalWs;
  });

  it("dispatches typed events via onEvent", async () => {
    const events: unknown[] = [];
    const sock = new ChatWebSocket("/api/ws/agents/conversations/abc", {
      onEvent: (ev) => events.push(ev),
    });
    // Wait for connect microtask
    await Promise.resolve();
    const fake = FakeWebSocket.instances[0];
    fake.emit({ type: "session_status", status: "running", external_session_id: "sid1", jsonl_path: "/x" });
    fake.emit({ type: "assistant_text", ts: "2026-08-14T00:00:00Z", text: "hello" });
    expect(events).toHaveLength(2);
    const first = events[0] as { type: string };
    expect(first.type).toBe("session_status");
    sock.close();
  });

  it("sendBytes writes Uint8Array to underlying socket", async () => {
    const sock = new ChatWebSocket("/api/sessions/abc/ws", {});
    await Promise.resolve();
    const fake = FakeWebSocket.instances[0];
    const ok = sock.sendBytes(new Uint8Array([3]));
    expect(ok).toBe(true);
    expect(fake.sentPayloads).toHaveLength(1);
    expect(fake.sentPayloads[0]).toBeInstanceOf(Uint8Array);
    sock.close();
  });

  it("does nothing when send while closed", () => {
    const sock = new ChatWebSocket("/api/ws/agents/conversations/abc", {});
    sock.close();
    const fake = FakeWebSocket.instances[0];
    const ok = sock.sendBytes(new Uint8Array([1, 2, 3]));
    expect(ok).toBe(false);
    // send() is a soft-noop when not open
    sock.send("noop");
    expect(fake.sentPayloads).toHaveLength(0);
  });

  it("does NOT reconnect on terminal close codes (4404/4401/4500)", async () => {
    vi.useFakeTimers();
    try {
      const sock = new ChatWebSocket("/api/ws/agents/conversations/abc", {});
      await Promise.resolve();
      FakeWebSocket.instances[0].closeWith(4404);
      // Advance well past any backoff window.
      vi.advanceTimersByTime(60_000);
      expect(FakeWebSocket.instances).toHaveLength(1); // no reconnect
      sock.close();
    } finally {
      vi.useRealTimers();
    }
  });

  it("reconnects on a non-terminal close code", async () => {
    vi.useFakeTimers();
    try {
      const sock = new ChatWebSocket("/api/ws/agents/conversations/abc", {});
      await Promise.resolve();
      FakeWebSocket.instances[0].closeWith(1006); // abnormal → transient
      vi.advanceTimersByTime(2000); // first backoff is ~1s
      expect(FakeWebSocket.instances.length).toBeGreaterThanOrEqual(2);
      sock.close();
    } finally {
      vi.useRealTimers();
    }
  });

  it("sends a heartbeat ping while open and swallows the pong ack", async () => {
    vi.useFakeTimers();
    try {
      const events: unknown[] = [];
      const sock = new ChatWebSocket("/api/ws/agents/conversations/abc", {
        onEvent: (ev) => events.push(ev),
      });
      await vi.advanceTimersByTimeAsync(0); // flush open microtask
      const fake = FakeWebSocket.instances[0];
      await vi.advanceTimersByTimeAsync(25_000); // one ping interval
      expect(fake.sentPayloads).toContain("ping");
      // A pong ack keeps the watchdog fed but must NOT surface as an app event.
      fake.emit({ type: "pong" });
      expect(events).toHaveLength(0);
      sock.close();
    } finally {
      vi.useRealTimers();
    }
  });

  it("force-closes and reconnects when a ping we sent goes unanswered", async () => {
    // `performance` MUST be faked alongside the timers. The watchdog judges on
    // `performance.now()` (a wall clock jumps on NTP correction and on
    // suspend/resume, which a phone does constantly), so leaving it real while
    // the timers jump 70s means the code sees ~0ms elapse and never concludes
    // anything — the test would pass or fail for reasons unrelated to the rule.
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date", "performance"] });
    try {
      const sock = new ChatWebSocket("/api/ws/agents/conversations/abc", {});
      await vi.advanceTimersByTimeAsync(0);
      expect(FakeWebSocket.instances).toHaveLength(1);
      // The window is deliberately tight enough to tell the two rules apart.
      // Ping goes out at 25s, nothing answers, so the PAIRING rule concludes
      // dead at the 45s tick (20s > PONG_GRACE_MS) and the reconnect lands
      // ~1s later. The old elapsed-time rule measured from the last inbound
      // frame (t=0) and needed `> 45_000`, so it would not have fired until
      // 60s — still one socket at this point. Widen this and the test stops
      // discriminating.
      //
      // Throttle-immunity itself (a late tick must NOT kill an answered
      // socket) can't be staged here: fake timers fire every interval on
      // schedule, so the stall that caused the bug cannot be reproduced
      // through this seam. That case is pinned directly on the rule in
      // tests/frontend/unit/lib/wsLiveness.spec.ts.
      await vi.advanceTimersByTimeAsync(50_000);
      expect(FakeWebSocket.instances.length).toBeGreaterThanOrEqual(2);
      sock.close();
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps backoff GROWING when the socket flaps before the stability window", async () => {
    vi.useFakeTimers();
    try {
      const delays: number[] = [];
      const sock = new ChatWebSocket("/api/ws/agents/conversations/abc", {
        onReconnect: (_attempt, delayMs) => delays.push(delayMs),
      });
      await vi.advanceTimersByTimeAsync(0);
      // Flap three times, each far under the 8s stability window, so `attempts`
      // must NOT be reset by onopen — the backoff has to grow, not pin at 1s.
      for (let i = 0; i < 3; i++) {
        const fake = FakeWebSocket.instances[FakeWebSocket.instances.length - 1];
        fake.closeWith(1006);
        await vi.advanceTimersByTimeAsync(delays[delays.length - 1]);
      }
      expect(delays.slice(0, 3)).toEqual([1000, 2000, 4000]);
      sock.close();
    } finally {
      vi.useRealTimers();
    }
  });
});
