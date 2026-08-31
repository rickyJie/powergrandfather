import { describe, it, expect, vi, beforeEach } from "vitest";
import MockAdapter from "axios-mock-adapter";

// We must import the client module fresh in each test because it reads
// window.location.search at module init (for ?token= bootstrap).
async function importClient() {
  vi.resetModules();
  const mod = await import("../../../../frontend/src/api/client");
  return mod;
}

describe("api/client — X-CSM-Client injection", () => {
  beforeEach(() => {
    // Reset location + storage between tests
    window.history.replaceState({}, "", "/m/");
    try {
      localStorage.clear();
    } catch {
      /* ignore */
    }
  });

  it("injects X-CSM-Client: 1 on every request", async () => {
    const { http } = await importClient();
    const mock = new MockAdapter(http);
    mock.onGet("/api/health").reply(200, { status: "ok" });

    const res = await http.get("/api/health");
    expect(res.status).toBe(200);
    // MockAdapter captures request configs in mock.history
    expect(mock.history.get[0].headers?.["X-CSM-Client"]).toBe("1");
  });

  it("bootstraps ?token= from URL into localStorage on module init", async () => {
    window.history.replaceState({}, "", "/m/?token=abc123");
    await importClient();
    expect(localStorage.getItem("csm_access_token")).toBe("abc123");
  });

  it("attaches x-csm-token header when token stored", async () => {
    localStorage.setItem("csm_access_token", "stored-token");
    const { http } = await importClient();
    const mock = new MockAdapter(http);
    mock.onGet("/api/sessions").reply(200, []);
    await http.get("/api/sessions");
    expect(mock.history.get[0].headers?.["x-csm-token"]).toBe("stored-token");
  });

  it("clears token on 401 response", async () => {
    localStorage.setItem("csm_access_token", "bad-token");
    const { http } = await importClient();
    const mock = new MockAdapter(http);
    mock.onGet("/api/anything").reply(401, { detail: "invalid" });
    await expect(http.get("/api/anything")).rejects.toBeDefined();
    expect(localStorage.getItem("csm_access_token")).toBeNull();
  });
});
