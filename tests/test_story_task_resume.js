"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.resolve(__dirname, "../webui/static/wizard-v0.js"), "utf8").replace(/\bboot\(\);\s*$/, "");
const context = { console, document: { querySelector: () => null, querySelectorAll: () => [] } };
vm.createContext(context);
vm.runInContext(source, context);

for (const status of ["idle", "interrupted", "failed", "stopped"]) {
  test(`outline ${status} task exposes original-request resume action`, () => {
    const html = context.chaptersJobMarkup({
      status, can_resume: true, completed: 2, total: 2,
      request: { mode: "refine" }, error: status === "failed" ? "<model error>" : "",
    });
    assert.match(html, /id="continue-chapters-job"/);
    assert.match(html, /按原要求继续/);
    assert.doesNotMatch(html, /id="pause-chapters-job"/);
    assert.doesNotMatch(html, /第 3 章前中断/);
    if (status === "failed") {
      assert.match(html, /&lt;model error&gt;/);
      assert.doesNotMatch(html, /<model error>/);
    }
  });
}

test("unrecoverable terminal outline job does not show live controls", () => {
  for (const status of ["completed", "interrupted", "failed", "stopped"]) {
    assert.equal(context.chaptersJobMarkup({ status, can_resume: false }), "");
  }
});
