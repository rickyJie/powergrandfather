import { describe, it, expect } from "vitest";
import { renderMarkdown } from "@/lib/markdown";

describe("renderMarkdown", () => {
  it("renders headings, lists and inline code", async () => {
    const html = await renderMarkdown("# Title\n\n- one\n- two\n\nuse `x`");
    expect(html).toContain("<h1>Title</h1>");
    expect(html).toContain("<li>one</li>");
    expect(html).toContain("<code>x</code>");
  });

  it("syntax-highlights fenced code with Shiki (dual theme survives sanitise)", async () => {
    const html = await renderMarkdown("```python\nprint('hi')\n```");
    // Shiki wraps output in a .shiki <pre>, tokens carry inline colors …
    expect(html).toContain("shiki");
    expect(html).toMatch(/color:/);
    // … and the dark-theme CSS custom property is NOT stripped by DOMPurify,
    // so light/dark switching keeps working.
    expect(html).toContain("--shiki-dark");
  });

  it("neutralises raw HTML / script injection in message text", async () => {
    const html = await renderMarkdown(
      "hello <script>alert(1)</script> <img src=x onerror=alert(2)>"
    );
    // The markup is ESCAPED to inert text, never a live tag/handler.
    expect(html).not.toContain("<script>");
    expect(html).toContain("&lt;script&gt;");
    // No live element carrying the handler — the img is escaped too.
    expect(html).not.toContain("<img");
  });

  it("blocks javascript: links (no live js href)", async () => {
    const html = await renderMarkdown("[click](javascript:alert(1))");
    // markdown-it's validateLink rejects the URL → left as inert plain text,
    // never an anchor with a javascript: href.
    expect(html).not.toContain('href="javascript');
  });

  it("opens http links in a new tab with noopener", async () => {
    const html = await renderMarkdown("[site](https://example.com)");
    expect(html).toContain('target="_blank"');
    expect(html).toContain("noopener");
  });
});
