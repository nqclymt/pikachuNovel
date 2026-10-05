"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.resolve(__dirname, "../webui/static/wizard-v0.js"), "utf8").replace(/\bboot\(\);\s*$/, "");
function wizard() {
  const context = { console, document: { querySelector: () => null, querySelectorAll: () => [] } };
  vm.createContext(context);
  vm.runInContext(source, context);
  return context;
}

test("CLI settings show Google login and hide API fields without discarding them", () => {
  const context = wizard();
  const html = context.modelConfigFields("adaptive_builder_lite", {
    label: "正文", backend: "antigravity_cli", model: "", api_key_configured: true,
    base_url: "https://saved-api.example/v1", cli_path: "C:/Apps/agy.exe", cli_agent: "novelist",
  });
  assert.match(html, /value="antigravity_cli" selected/);
  assert.match(html, /data-openai-fields hidden/);
  assert.doesNotMatch(html, /data-antigravity-fields hidden/);
  assert.match(html, /https:\/\/saved-api.example\/v1/);
  assert.match(html, /已配置，留空保持不变/);
  assert.match(html, /Google 登录 · 待测试/);
  assert.match(html, /启动\/登录 agy/);
  assert.match(html, /测试连接（使用额度）/);
  assert.match(html, /tools: \[\]/);
});

test("legacy API configuration and empty editor inheritance stay the defaults", () => {
  const context = wizard();
  const api = context.modelConfigFields("data_builder", { label: "参考", api_key_configured: true });
  assert.match(api, /value="openai" selected/);
  assert.match(api, /data-antigravity-fields hidden/);
  assert.match(api, /API Key 已配置/);
  const inherited = context.modelConfigFields("humanize_builder", { label: "精修" });
  assert.match(inherited, /沿用正文模型/);
  const explicit = context.modelConfigFields("humanize_builder", { label: "精修", backend: "antigravity_cli" });
  assert.doesNotMatch(explicit, /沿用正文模型/);
});

test("scheduler distinguishes queue, running, paused and ready states", () => {
  const context = wizard();
  assert.equal(context.antigravitySchedulerText({ paused: false }), "调度就绪");
  assert.match(context.antigravitySchedulerText({ running: true, waiting: 3 }), /执行 1 个请求，排队 3/);
  assert.match(context.antigravitySchedulerText({ paused: true, message: "额度受限" }), /暂停：额度受限/);
  const panel = context.antigravityPanelMarkup();
  assert.match(panel, /启动\/登录 agy/);
  assert.match(panel, /继续已中断的写作/);
  assert.match(panel, /id="resume-antigravity-scheduler" disabled/);
});

test("launch button opens interactive agy with the current unsaved path", async () => {
  const context = wizard();
  const classes = new Set();
  const result = { textContent: "", classList: { add: (key) => classes.add(key), remove: (key) => classes.delete(key) } };
  const button = { disabled: false };
  const fields = {
    DATA_BUILDER_MODEL: "",
    DATA_BUILDER_CLI_PATH: " C:/Unstored/agy.exe ",
    DATA_BUILDER_CLI_AGENT: "",
    DATA_BUILDER_CLI_EFFORT: "medium",
  };
  const section = {
    dataset: { modelGroup: "data_builder" },
    querySelector: (selector) => selector === "[data-antigravity-result]"
      ? result
      : { value: fields[selector.match(/name="([^"]+)"/)[1]] },
    querySelectorAll: () => [button],
  };
  button.closest = () => section;
  let request;
  context.api = async (path, options) => {
    request = { path, body: JSON.parse(options.body) };
    return { opened: true, proxy: "system" };
  };
  context.showToast = () => {};

  await context.launchAntigravity({ currentTarget: button });

  assert.deepEqual(request, {
    path: "/api/config/antigravity/launch",
    body: { cli_path: "C:/Unstored/agy.exe" },
  });
  assert.match(result.textContent, /系统代理/);
  assert.equal(button.disabled, false);
  assert.equal(classes.has("error"), false);
});

test("connection test values come from the current form rather than saved settings", () => {
  const context = wizard();
  const fields = {
    ADAPTIVE_BUILDER_LITE_MODEL: "",
    ADAPTIVE_BUILDER_LITE_CLI_PATH: " C:/Unstored/agy.exe ",
    ADAPTIVE_BUILDER_LITE_CLI_AGENT: " novelist ",
    ADAPTIVE_BUILDER_LITE_CLI_EFFORT: "high",
  };
  const result = context.antigravityFormValues({
    dataset: { modelGroup: "adaptive_builder_lite" },
    querySelector: (selector) => ({ value: fields[selector.match(/name="([^"]+)"/)[1]] }),
  });
  assert.equal(JSON.stringify(result), JSON.stringify({ model: "", cli_path: "C:/Unstored/agy.exe", cli_agent: "novelist", cli_effort: "high" }));
});

test("saving passes empty CLI defaults but omits blank API keys", async () => {
  const context = wizard();
  const fields = [
    { name: "DATA_BUILDER_BACKEND", value: "antigravity_cli" },
    { name: "DATA_BUILDER_MODEL", value: "" },
    { name: "DATA_BUILDER_CLI_PATH", value: "" },
    { name: "DATA_BUILDER_API_KEY", value: "" },
  ];
  let posted;
  context.api = async (_path, options) => { posted = JSON.parse(options.body); };
  context.closeSettings = () => {};
  context.showToast = () => {};
  const button = { disabled: false };
  await context.saveModelConfig({ preventDefault() {}, currentTarget: {
    querySelector: () => button, querySelectorAll: () => fields,
  } });
  assert.deepEqual(posted.values, { DATA_BUILDER_BACKEND: "antigravity_cli", DATA_BUILDER_MODEL: "", DATA_BUILDER_CLI_PATH: "" });
  assert.equal(button.disabled, false);
});

test("a detected binary with a failed version probe is shown as an error", async () => {
  const context = wizard();
  const classes = new Set();
  const result = { textContent: "", classList: { add: (key) => classes.add(key), remove: (key) => classes.delete(key) } };
  const button = { disabled: false };
  const section = {
    dataset: { modelGroup: "data_builder" },
    querySelector: (selector) => selector === "[data-antigravity-result]" ? result : { value: "" },
    querySelectorAll: () => [button],
  };
  button.closest = () => section;
  context.api = async () => ({ installed: true, path: "C:/Apps/agy.exe", error: "版本检测超时" });
  await context.checkAntigravity({ currentTarget: button });
  assert.match(result.textContent, /版本检测超时/);
  assert.ok(classes.has("error"));
  assert.equal(button.disabled, false);
});

test("changing backend toggles fields while preserving the configured model", () => {
  const context = wizard();
  const apiFields = { hidden: false };
  const cliFields = { hidden: true };
  const model = { value: "saved-model", placeholder: "" };
  const status = { textContent: "", classList: { add() {}, remove() {} } };
  let change;
  const section = { dataset: { modelGroup: "data_builder" }, querySelector: (selector) => ({
    "[data-openai-fields]": apiFields,
    "[data-antigravity-fields]": cliFields,
    "[data-model-config-status]": status,
    '[name="DATA_BUILDER_MODEL"]': model,
  })[selector] };
  const select = { value: "antigravity_cli", closest: () => section, addEventListener: (_type, handler) => { change = handler; } };
  context.document.querySelectorAll = (selector) => selector === "[data-model-backend]" ? [select] : [];
  context.bindModelBackendActions();
  change();
  assert.equal(apiFields.hidden, true);
  assert.equal(cliFields.hidden, false);
  assert.match(model.placeholder, /agy 默认模型/);
  select.value = "openai";
  change();
  assert.equal(apiFields.hidden, false);
  assert.equal(cliFields.hidden, true);
  assert.equal(model.value, "saved-model");
});
