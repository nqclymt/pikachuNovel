/* Offline DOM integration checks. Loaded with the real wizard, without boot(). */
(async function () {
  const checks = [];
  const assert = (condition, label) => { if (!condition) throw new Error(label); checks.push(label); };
  let requests = 0;
  window.fetch = async () => { requests++; throw new Error("Offline test: unexpected request"); };
  showToast = () => {};
  const setup = (kind, status = "idle", width = 1000) => {
    const canvas = document.getElementById("fixture");
    canvas.style.width = `${width}px`;
    const hostId = kind === "arcs" ? "arcs-chat-host" : kind === "chapters" ? "chapters-chat-host" : kind === "draft" ? "draft-chat-host" : "design-chat-host";
    canvas.innerHTML = `<div id="${hostId}"></div>`;
    wizardState.workspace = "test-only";
    wizardState.summary = {story_design:{stage_count:2},volumes:[{volume:1,arcs:[{idx:1,title:"test",start_ch:1,end_ch:5}]}]};
    if (kind === "arcs") renderArcsChat(1,{turns:[]},{status});
    else if (kind === "chapters") renderChaptersChat(1,1,{turns:[]},{status});
    else if (kind === "draft") renderDraftChat(1,1,{turns:[]},{status});
    else renderDesignChat(kind,{turns:[]},{status});
    return { host:document.getElementById(hostId), input:canvas.querySelector("textarea"), buttons:[...canvas.querySelectorAll("[data-preset-index]")], undo:canvas.querySelector("[data-preset-undo]") };
  };
  try {
    for (const kind of ["concept","stage","arcs","chapters","draft"]) {
      let ui=setup(kind);
      assert(ui.buttons.length===8, `${kind}: 8 suggestions`);
      assert(ui.input.value==="", `${kind}: no default input`);
      const custom="  custom text\nsecond line  ";
      ui.input.value=custom;
      ui.input.dispatchEvent(new Event("input",{bubbles:true}));
      ui.buttons[0].click();
      const first=ui.input.value;
      assert(first.startsWith(custom+"\n"), `${kind}: custom input preserved`);
      assert(ui.buttons[0].classList.contains("is-added"), `${kind}: selected feedback`);
      ui.buttons[0].click();
      assert(ui.input.value===first, `${kind}: duplicate click ignored`);
      ui.buttons[1].click();
      assert(ui.input.value.startsWith(first+"\n"), `${kind}: combine suggestions`);
      ui.undo.click();
      assert(ui.input.value===first, `${kind}: undo only last insertion`);
      ui.buttons[2].click();
      ui.input.value+="\nmanual change";
      ui.input.dispatchEvent(new Event("input",{bubbles:true}));
      assert(ui.undo.disabled, `${kind}: edited input cannot be removed by undo`);
      const manual=ui.input.value;
      ui.undo.click();
      assert(ui.input.value===manual, `${kind}: manual text protected`);
      WorkflowPromptPresets.setBusy(ui.input,true);
      assert(ui.buttons.every(button=>button.disabled), `${kind}: immediate lock during send`);
      WorkflowPromptPresets.setBusy(ui.input,false);
      assert(ui.buttons.every(button=>!button.disabled), `${kind}: unlock after failure`);
      for (const state of ["queued","running","pausing","paused","stopping"]) {
        ui=setup(kind,state);
        assert(ui.buttons.every(button=>button.disabled), `${kind}: locked in ${state}`);
        ui.buttons[0].click();
        assert(ui.input.value==="", `${kind}: no insertion in ${state}`);
      }
      ui=setup(kind);
      assert(ui.input.value==="" && ui.undo.disabled, `${kind}: no leaked input on new panel`);
      for (const width of [1000,520]) {
        ui=setup(kind,"idle",width);
        const panel=ui.host.querySelector("[data-workflow-presets]");
        const box=panel.getBoundingClientRect();
        assert(ui.buttons.every(b => {const r=b.getBoundingClientRect(); return r.left>=box.left-1 && r.right<=box.right+1;}), `${kind}: buttons fit ${width}px panel`);
      }
    }
    assert(requests===0,"preset clicks make zero network requests");
    document.documentElement.dataset.testResult="passed";
    document.getElementById("report").textContent=JSON.stringify({status:"passed",checks:checks.length,details:checks,networkRequests:requests});
  } catch (error) {
    document.documentElement.dataset.testResult="failed";
    document.getElementById("report").textContent=JSON.stringify({status:"failed",checks:checks.length,error:error.stack});
  }
})();
