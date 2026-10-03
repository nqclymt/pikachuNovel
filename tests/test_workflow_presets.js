"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const base = path.resolve(__dirname, "..");
const presets = require(path.join(base, "webui/static/workflow-presets.js"));
const wizardSource = fs.readFileSync(path.join(base, "webui/static/wizard-v0.js"), "utf8").replace(/\bboot\(\);\s*$/, "");
function wizard(elements = {}) {
  const sandbox = { WorkflowPromptPresets: presets, console, document: { querySelector: (q) => elements[q] || null, querySelectorAll: () => [] } };
  vm.createContext(sandbox);
  vm.runInContext(wizardSource, sandbox);
  vm.runInContext('wizardState.workspace="fixture"; wizardState.summary={story_design:{stage_count:2},volumes:[{volume:1,arcs:[{idx:1,title:"fixture",start_ch:1,end_ch:5}]}]};', sandbox);
  return sandbox;
}
function countButtons(html) { return (html.match(/data-preset-index=/g) || []).length; }

test("retained chapters can be reset after the story arc was removed", () => {
  const context = wizard();
  vm.runInContext('wizardState.summary.volumes[0].arcs = [{idx:1,start_ch:1,end_ch:5,chapters:[1,2,3,4,5],missing_story_arc:true}];', context);
  const outline = context.chaptersChatPanelMarkup(1, 1, { turns: [], has_outlines: true });
  const draft = context.draftChatPanelMarkup(1, 1, { turns: [], has_drafts: true });
  for (const [html, kind] of [[outline, "chapters"], [draft, "draft"]]) {
    assert.match(html, /原情节已删除或为空/);
    const reset = html.match(new RegExp(`<button id="reset-${kind}-chat"[^>]*>`))[0];
    assert.ok(!reset.includes("disabled"));
    assert.match(html, new RegExp(`<textarea id="${kind}-chat-input"[^>]*disabled`));
    assert.match(html, new RegExp(`<button id="send-${kind}-chat"[^>]*disabled`));
  }
});

test("unassigned batch has an explicit label and an exact sparse chapter range", () => {
  const context = wizard();
  const batch = { idx: -1, missing_story_arc: true, chapters: [1, 3, 4], start_ch: 1, end_ch: 4 };
  assert.equal(context.chapterBatchLabel(batch), "未归属章节");
  assert.equal(context.chapterBatchRange(batch), "第 1、3-4 章");
  vm.runInContext(`wizardState.summary.volumes[0].arcs = [${JSON.stringify(batch)}];`, context);
  assert.equal(context.selectedChapterBatch(1, 99), -1);
  const html = context.draftChatPanelMarkup(1, -1, { has_drafts: true });
  assert.match(html, /未归属章节/);
  assert.doesNotMatch(html, /情节单元-1/);
  assert.match(html, /id="reset-draft-chat"/);
});

test("loading after deletion selects a remaining batch instead of a stale id", async () => {
  const context = wizard();
  vm.runInContext('wizardState.summary.volumes[0].arcs = [{idx:2,start_ch:6,end_ch:8}];', context);
  const requested = [];
  context.api = async (url) => { requested.push(url); return { status: "idle", turns: [] }; };
  context.renderChaptersChat = () => {};
  context.renderDraftChat = () => {};
  await context.loadChaptersChat(1, 1);
  await context.loadDraftChat(1, 1);
  assert.ok(requested.includes("/api/workspaces/fixture/chapters/1/2/conversation"));
  assert.ok(requested.includes("/api/workspaces/fixture/drafts/1/2/conversation"));
  assert.ok(requested.every(url => !url.includes("/1/1/")));
  vm.runInContext('wizardState.summary.volumes[0].arcs = [];', context);
  assert.equal(context.selectedChapterBatch(1, 2), null);
});

test("review groups use recorded chapters rather than the surrounding range", () => {
  const context = wizard();
  vm.runInContext('wizardState.summary.volumes[0].arcs = [{idx:1,start_ch:1,end_ch:3,chapters:[1,3],missing_story_arc:true},{idx:-1,start_ch:2,end_ch:2,chapters:[2],missing_story_arc:true}];', context);
  const files = [1, 2, 3].map(ch => ({ path: `file_system/chapters/vol_01/${String(ch).padStart(3, "0")}_第${ch}章.md` }));
  const groups = context.chapterArcReviewGroups({ id: "draft" }, files);
  const batches = groups[0].volumes[0].arcs;
  assert.equal(batches.find(b => b.idx === -1).artifacts.length, 1);
  assert.equal(batches.find(b => b.idx === 1).artifacts.length, 2);
  assert.match(batches.find(b => b.idx === 1).description, /第 1、3 章/);
});

test("retained content is not treated as an existing story arc to regenerate", () => {
  const context = wizard();
  vm.runInContext('wizardState.summary.volumes[0].arcs[0].missing_story_arc = true;', context);
  assert.doesNotMatch(context.arcsChatPanelMarkup(1, { turns: [], has_arcs: false }), /id="reset-arcs-chat"/);
  assert.match(context.draftForm(), /id="draft-arc" disabled/);
});
test("four stage-specific groups contain 32 distinct nonempty suggestions", () => {
  assert.deepEqual(Object.keys(presets.groups), ["concept", "stage", "arcs", "chapters"]);
  for (const items of Object.values(presets.groups)) {
    assert.equal(items.length, 8);
    assert.equal(new Set(items.map(x => x.label)).size, 8);
    assert.equal(new Set(items.map(x => x.text)).size, 8);
    assert.ok(items.every(x => x.label && x.text));
  }
});
test("append preserves custom text and whitespace exactly", () => {
  const original = "  original\ncustom line  ";
  assert.equal(presets.append(original, "suggestion"), original + "\nsuggestion");
  assert.equal(presets.append("", "suggestion"), "suggestion");
  assert.equal(presets.append("original\n", "suggestion"), "original\nsuggestion");
});
test("repeat selection does not duplicate, including line wrapping", () => {
  assert.equal(presets.append("existing\nnew suggestion", "new suggestion"), "existing\nnew suggestion");
  assert.equal(presets.append("new\nsuggestion", "new suggestion"), "new\nsuggestion");
  assert.equal(presets.append("preserved", ""), "preserved");
});
test("markup defaults to no selections and uses non-submit buttons", () => {
  for (const key of Object.keys(presets.groups)) {
    const html = presets.markup(key);
    assert.equal(countButtons(html), 8);
    assert.equal((html.match(/type="button"/g) || []).length, 9);
    assert.ok(!html.includes("is-added"));
    assert.ok(!html.includes("onclick="));
    assert.ok(html.includes("title="));
  }
});
test("unknown groups and prototype names are not rendered", () => {
  for (const name of ["other", "__proto__", "constructor", "toString"]) assert.equal(presets.markup(name), "");
});
test("preset title and label are HTML escaped", () => {
  const html = presets.markup("draft", false, [{ label: '<img src="x">', text: '\" <script> & \' ' }]);
  assert.ok(!html.includes("<img"));
  assert.ok(!html.includes("<script>"));
  assert.ok(html.includes("&quot;"));
  assert.ok(html.includes("&amp;"));
});
test("all five actual composers include the correct preset group", () => {
  const s = wizard();
  for (const [key, expression] of [
    ["concept", "designChatPanelMarkup('concept',{turns:[]})"],
    ["stage", "designChatPanelMarkup('stage',{turns:[]})"],
    ["arcs", "arcsChatPanelMarkup(1,{turns:[]})"],
    ["chapters", "chaptersChatPanelMarkup(1,1,{turns:[]})"],
    ["draft", "draftChatPanelMarkup(1,1,{turns:[]})"],
  ]) {
    const html = vm.runInContext(expression, s);
    assert.equal(countButtons(html), 8, key);
    assert.ok(html.includes(`data-workflow-presets="${key}"`));
  }
});
test("queued/running/paused/stopping composers disable preset buttons and input", () => {
  const s = wizard();
  for (const state of ["queued", "running", "pausing", "paused", "stopping"]) {
    for (const expression of [
      "designChatPanelMarkup('stage',{turns:[]},JOB)", "designChatPanelMarkup('concept',{turns:[]},JOB)",
      "arcsChatPanelMarkup(1,{turns:[]},JOB)", "chaptersChatPanelMarkup(1,1,{turns:[]},JOB)",
      "draftChatPanelMarkup(1,1,{turns:[]},JOB)",
    ]) {
      const html = vm.runInContext(expression.replace("JOB", JSON.stringify({status:state})), s);
      const tags = html.match(/<button[^>]*data-preset-index=[^>]*>/g) || [];
      assert.equal(tags.length, 8);
      assert.ok(tags.every(tag => tag.includes("disabled")), expression + state);
      assert.ok(/<textarea[^>]*disabled/.test(html), expression + state);
    }
  }
});
test("chapters and draft disable suggestions when there is no story arc", () => {
  const s = wizard();
  vm.runInContext("wizardState.summary.volumes=[]", s);
  for (const expression of ["chaptersChatPanelMarkup(1,0,{turns:[]})", "draftChatPanelMarkup(1,0,{turns:[]})"]) {
    const html = vm.runInContext(expression,s);
    assert.ok((html.match(/<button[^>]*data-preset-index=[^>]*>/g) || []).every(tag => tag.includes("disabled")));
  }
});
test("entry HTML loads preset module before wizard", () => {
  const html = fs.readFileSync(path.join(base,"webui/static/index.html"),"utf8");
  assert.ok(html.indexOf('/assets/workflow-presets.js') > 0);
  assert.ok(html.indexOf('/assets/workflow-presets.js') < html.indexOf('/assets/wizard-v0.js'));
});
for (const [group, selector, sendSelector, invocation] of [
  ["stage", "#chat-input", "#send-design-chat", "sendDesignMessage('stage')"],
  ["arcs", "#arcs-chat-input", "#send-arcs-chat", "sendArcsMessage(1)"],
  ["chapters", "#chapters-chat-input", "#send-chapters-chat", "sendChaptersMessage(1,1)"],
  ["draft", "#draft-chat-input", "#send-draft-chat", "sendDraftMessage(1,1)"],
]) test(`${group}: one pending request, failed submission preserves input and unlocks`, async () => {
  const original = "  custom instruction\n keep spaces  ";
  const input = {value:original, disabled:false};
  const button = {disabled:false};
  const s = wizard({[selector]:input, [sendSelector]:button});
  let reject, calls=0;
  s.fakeApi = (url, options) => {
    calls++;
    assert.equal(JSON.parse(options.body).message, original.trim());
    return new Promise((resolve, fail) => { reject=fail; });
  };
  vm.runInContext("api=fakeApi; showToast=()=>{};",s);
  const first = vm.runInContext(invocation,s);
  assert.equal(input.disabled,true);
  await vm.runInContext(invocation,s);
  assert.equal(calls,1);
  reject(new Error("test request rejected before job creation"));
  await first;
  assert.equal(input.value,original);
  assert.equal(input.disabled,false);
  assert.equal(button.disabled,false);
  assert.equal(s.started,undefined, "started must remain function-local");
});
