/* Optional input suggestions. This module never sends requests or saves story data. */
const WorkflowPromptPresets = (() => {
  "use strict";
  const groups = {
    concept: [
      { label: "正常设计", text: "基于已提供的构思与资料，生成世界观、粗略大纲和阶段粗纲，保留已确认的人物与主题。" },
      { label: "核心矛盾", text: "明确主角的目标、主要阻力与选择代价，让核心矛盾能够持续推进。" },
      { label: "人物动机", text: "明确主要人物各自的目标、顾虑和行动依据，让关键事件由人物选择推动。" },
      { label: "能力与代价", text: "明确能力的用途、限制与代价，让成长带来实际变化，不强行新增数值系统。" },
      { label: "关系递进", text: "让人物关系随着具体事件逐步变化，区分利益、信任与感情，不用重复表态代替推进。" },
      { label: "悬念分层", text: "区分读者已知、主角已知与尚未公开的信息，安排悬念的铺垫、阶段揭示和最终回答。" },
      { label: "阶段分工", text: "明确各阶段独立目标、进入与结束条件，核对阶段数量和顺序，避免第一阶段提前完成全书结局。" },
      { label: "保留设定调整", text: "保留已确认的核心设定，针对我本轮提出的问题调整设计，未涉及的内容保持一致。" },
    ],
    stage: [
      { label: "按阶段展开", text: "依据已确认的阶段粗纲展开长线主线和舞台路线图，保持阶段对应关系，按顺序完善未完成部分。" },
      { label: "舞台边界", text: "核对舞台与阶段粗纲的编号、数量、起点和终点。每个舞台只承担本阶段事件，非最终舞台不提前完成全书结局。" },
      { label: "前后承接", text: "承接上一舞台已发生的事件、人物状态和关系变化，明确离开旧舞台与进入新舞台的原因，不让状态无故退回。" },
      { label: "阶段目标", text: "明确本舞台主角要解决的问题、主要阻碍、关键选择以及结束时的实际变化。" },
      { label: "冲突递进", text: "通过新阻碍、信息变化、选择和后果逐步推进冲突，不只是重复羞辱或反复强调危险。" },
      { label: "伏笔收放", text: "区分本舞台新埋伏笔、需要回收的伏笔和留给后续的悬念，揭示时机遵循阶段粗纲。" },
      { label: "成长有代价", text: "让能力、地位、资源或关系变化来自可见行动和代价，保留成长影响，不在下一场把所有收获清零。" },
      { label: "避免重复舞台", text: "区分各舞台的冲突方式、人物关系和解决问题的路径，避免只换地图重演同一套桥段。" },
    ],
    arcs: [
      { label: "正常生成", text: "按当前选定舞台生成或完善连续的故事情节单元，每个单元有具体目标、行动、阻碍和结果，不越过舞台边界。" },
      { label: "连续时间线", text: "按事件实际发生顺序组织情节，承接前一单元结果，已经发生或解决的事件不在后续重新写成尚未发生。" },
      { label: "情节去重", text: "检查相邻情节是否仅换地点重复同一种冲突，合并无新增变化的桥段，让后续单元带来新的行动、信息或关系变化。" },
      { label: "主动选择", text: "让主角与配角根据各自目标主动判断、选择并承担后果，行动符合已有能力和性格。" },
      { label: "因果与转折", text: "把转折建立在已铺垫的信息、人物行动或规则上，让意外回看时有依据，不靠临时设定推进。" },
      { label: "张弛有度", text: "根据冲突需要安排蓄势、推进、转折与余波，日常承担人物或关系推进，不用无关琐事填充。" },
      { label: "关系推进", text: "通过交涉、合作、误判或代价推进已有关系，让态度变化有事件依据，不强加新的感情线。" },
      { label: "控制信息差", text: "明确各情节允许透露的新信息与仍须保留的悬念，保持人物认知边界，不反复用同一个疑问作结。" },
    ],
    chapters: [
      { label: "正常拆章", text: "按当前选定情节单元拆分或完善章纲，保持事件顺序、人物状态和揭示边界，按场景任务自然分章。" },
      { label: "章章有进展", text: "明确每章相较上一章新增的行动、信息、阻碍或关系变化，避免连续数章只换地点重演相同冲突。" },
      { label: "背景与事件分开", text: "区分必须保持成立的背景设定与本章新事件，身份、境界和性格是约束，不是每章必须重新强调的内容。" },
      { label: "场景可落笔", text: "把事件落实到人物在什么场合做什么、遭遇什么阻碍、如何回应以及产生什么结果，不只写抽象情绪标签。" },
      { label: "能力过程具体", text: "涉及能力或知识解决问题时，交代观察依据、判断、操作和可见结果，在已有设定内提供具体过程。" },
      { label: "避免口癖复读", text: "人物特点主要通过选择与行动体现，不把口癖或同一句心理结论当作每章必填项。" },
      { label: "控制信息揭示", text: "标明本章新增信息与必须保留的未知，不提前兑现后续真相，章尾从未完成的行动或新问题自然延伸。" },
      { label: "状态连续", text: "核对章初状态、事件变化与章末状态，确保伤势、资源、能力、关系和信息衔接，不把章末目标提前当作章初事实。" },
    ],
  };
  const labels = { concept: "全书设计", stage: "舞台设计", arcs: "故事情节", chapters: "逐章章纲", draft: "正文" };
  const controls = new WeakMap();
  const own = (obj, key) => Object.prototype.hasOwnProperty.call(obj, key);
  const esc = (text) => String(text).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const compact = (text) => String(text || "").replace(/\s+/g, "");
  const contains = (value, text) => Boolean(compact(text)) && compact(value).includes(compact(text));
  const append = (value, text) => {
    value = String(value || "");
    return !text || contains(value, text) ? value : value + (value && !/[\r\n]$/.test(value) ? "\n" : "") + text;
  };
  function itemsFor(key, items) { return Array.isArray(items) ? items : own(groups, key) ? groups[key] : []; }
  function markup(key, disabled = false, items = null) {
    if (!own(labels, key)) return "";
    const list = itemsFor(key, items);
    return `<div class="workflow-prompt-presets" data-workflow-presets="${key}" role="group" aria-label="${labels[key]}快捷要求"><div class="workflow-presets-heading"><strong>${labels[key]} · 快捷要求</strong><button type="button" data-preset-undo disabled>撤销上次填入</button></div><div class="workflow-preset-list">${list.map((item, index) => `<button type="button" class="workflow-preset" data-preset-index="${index}" title="${esc(item.text)}" ${disabled ? "disabled" : ""}>${esc(item.label)}</button>`).join("")}</div><small data-preset-status role="status" aria-live="polite">点击填入，可组合；确认后再发送。未选择的要求不会生效。</small></div>`;
  }
  function bind(container, input, items = null) {
    const key = container?.dataset.workflowPresets;
    if (!container || !input || !own(labels, key)) return;
    controls.get(input)?.dispose();
    const list = itemsFor(key, items);
    const buttons = Array.from(container.querySelectorAll("[data-preset-index]"));
    const undo = container.querySelector("[data-preset-undo]");
    const status = container.querySelector("[data-preset-status]");
    let last = null;
    const locked = () => input.disabled || input.readOnly;
    const sync = () => {
      if (last && input.value !== last.after) last = null;
      container.setAttribute("aria-disabled", String(locked()));
      buttons.forEach(button => {
        const item = list[Number(button.dataset.presetIndex)];
        const added = Boolean(item && contains(input.value, item.text));
        button.disabled = locked();
        button.classList.toggle("is-added", added);
        button.setAttribute("aria-controls", input.id);
        button.setAttribute("aria-label", item ? item.label + (added ? "（已填入）" : "") : "");
      });
      if (undo) undo.disabled = locked() || !last;
    };
    const changed = () => {
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
      input.dispatchEvent(new input.ownerDocument.defaultView.Event("input", { bubbles: true }));
      sync();
    };
    const click = (event) => {
      const button = event.target.closest("button");
      if (!button || !container.contains(button) || button.disabled || locked()) return;
      if (button.hasAttribute("data-preset-undo")) {
        if (last && input.value === last.after) {
          input.value = last.before;
          last = null;
          if (status) status.textContent = "已撤销上次填入，原有输入保持不变。";
          changed();
        }
        return;
      }
      const index = Number(button.dataset.presetIndex);
      const item = Number.isInteger(index) ? list[index] : null;
      if (!item) return;
      const before = input.value, after = append(before, item.text);
      if (before === after) {
        if (status) status.textContent = `「${item.label}」已在输入框中，没有重复添加。`;
        return;
      }
      last = { before, after };
      input.value = after;
      if (status) status.textContent = `已填入「${item.label}」，可继续编辑；尚未发送。`;
      changed();
    };
    container.addEventListener("click", click);
    input.addEventListener("input", sync);
    const observer = new input.ownerDocument.defaultView.MutationObserver(sync);
    observer.observe(input, { attributes: true, attributeFilter: ["disabled", "readonly"] });
    controls.set(input, { sync, dispose() { observer.disconnect(); container.removeEventListener("click", click); input.removeEventListener("input", sync); } });
    sync();
  }
  function setBusy(input, busy) {
    if (input) { input.disabled = Boolean(busy); controls.get(input)?.sync(); }
  }
  return Object.freeze({ groups, labels, contains, append, markup, bind, setBusy });
})();
if (typeof module !== "undefined" && module.exports) module.exports = WorkflowPromptPresets;
