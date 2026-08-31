import { describe, it, expect } from "vitest";
import { mount } from "@vue/test-utils";
import Vant from "vant";

import MessageInput from "../../../../frontend/src/components/message-stream/MessageInput.vue";

function mountInput() {
  return mount(MessageInput, { global: { plugins: [Vant] } });
}

async function clickSend(wrapper: ReturnType<typeof mountInput>) {
  const buttons = wrapper.findAll("button");
  await buttons[buttons.length - 1].trigger("click");
}

describe("MessageInput", () => {
  it("emits send with trimmed text and a done callback", async () => {
    const wrapper = mountInput();
    await wrapper.find("textarea").setValue("  hello world  ");
    await clickSend(wrapper);
    const sends = wrapper.emitted("send");
    expect(sends).toBeTruthy();
    expect(sends?.[0]?.[0]).toBe("hello world");
    expect(typeof sends?.[0]?.[1]).toBe("function");
  });

  it("clears the draft only after done(true)", async () => {
    const wrapper = mountInput();
    const field = wrapper.find("textarea");
    await field.setValue("hello world");
    await clickSend(wrapper);
    const done = wrapper.emitted("send")?.[0]?.[1] as (ok: boolean) => void;
    // Before confirmation the draft is preserved.
    expect((field.element as HTMLTextAreaElement).value).toBe("hello world");
    done(true);
    await wrapper.vm.$nextTick();
    expect((field.element as HTMLTextAreaElement).value).toBe("");
  });

  it("preserves the draft when done(false) (failed send)", async () => {
    const wrapper = mountInput();
    const field = wrapper.find("textarea");
    await field.setValue("keep me");
    await clickSend(wrapper);
    const done = wrapper.emitted("send")?.[0]?.[1] as (ok: boolean) => void;
    done(false);
    await wrapper.vm.$nextTick();
    expect((field.element as HTMLTextAreaElement).value).toBe("keep me");
  });

  it("shows bash hint when text starts with !", async () => {
    const wrapper = mountInput();
    await wrapper.find("textarea").setValue("!ls -la");
    expect(wrapper.find(".hint").exists()).toBe(true);
    expect(wrapper.find(".hint").text()).toContain("bash");
  });

  it("disables send when text is empty", async () => {
    const wrapper = mountInput();
    const buttons = wrapper.findAll("button");
    const sendBtn = buttons[buttons.length - 1];
    expect((sendBtn.element as HTMLButtonElement).disabled).toBe(true);
  });
});
