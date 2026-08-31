import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import MockAdapter from "axios-mock-adapter";
import { http } from "../../../../frontend/src/api/client";

describe("http client transient-retry", () => {
  let mock: MockAdapter;
  beforeEach(() => {
    mock = new MockAdapter(http);
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("retries an idempotent GET after a transient network error", async () => {
    // First attempt: network error; retry: 200.
    mock.onGet("/api/health").networkErrorOnce();
    mock.onGet("/api/health").reply(200, { status: "ok" });

    const p = http.get("/api/health");
    // Advance past the 400ms retry backoff.
    await vi.advanceTimersByTimeAsync(500);
    const res = await p;
    expect(res.status).toBe(200);
  });

  it("does NOT retry a plain POST (non-idempotent) on network error", async () => {
    mock.onPost("/api/sessions/x/message").networkError();
    await expect(http.post("/api/sessions/x/message", { text: "hi" })).rejects.toBeTruthy();
    // Only the single original attempt was made.
    const posts = mock.history.post.filter((h) => h.url === "/api/sessions/x/message");
    expect(posts.length).toBe(1);
  });

  it("DOES retry a POST marked __idempotent after a transient error", async () => {
    // First attempt: network error; retry: 200. Safe because the backend dedups
    // by client_msg_id, so a resend can't double-type into the PTY.
    mock.onPost("/api/sessions/x/message").networkErrorOnce();
    mock.onPost("/api/sessions/x/message").reply(200, { sent: "x" });

    const p = http.post(
      "/api/sessions/x/message",
      { text: "hi", client_msg_id: "abc" },
      { __idempotent: true } as never
    );
    await vi.advanceTimersByTimeAsync(500); // past the 400ms backoff
    const res = await p;
    expect(res.status).toBe(200);
    const posts = mock.history.post.filter((h) => h.url === "/api/sessions/x/message");
    expect(posts.length).toBe(2); // original + one retry
  });
});
