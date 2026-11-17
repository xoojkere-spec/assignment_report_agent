"use strict";

const $ = (id) => document.getElementById(id);
let selected = null;
let current = null;
let activeJob = null;
let activeTab = "draft";
let polling = false;
let connected = false;
const advisorDrafts = new Map();

function selectTab(name) {
  activeTab = name;
  document.querySelectorAll("[data-tab]").forEach((tab) => {
    const selected = tab.dataset.tab === name;
    tab.classList.toggle("selected", selected);
    tab.setAttribute("aria-selected", String(selected));
  });
}

function notify(message, error = false) {
  $("notification").textContent = message;
  $("notification").className = `notification${error ? " error" : ""}`;
  $("notification").hidden = !message;
}

async function api(path, options = {}) {
  const headers = { "X-Assignment-Client": "local", ...options.headers };
  if (options.body && !(options.body instanceof FormData)) headers["Content-Type"] = "application/json";
  const response = await fetch(path, { ...options, headers });
  const body = await response.json();
  if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : "操作失败，请重试。");
  return body;
}

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}

function escapeHTML(text) {
  return text.replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
}

function markdown(text) {
  // 先转义全部原文，只生成有限的展示标签，模型输出不能执行 HTML 或脚本。
  const container = node("div", undefined, "document");
  let code = null;
  for (const line of text.split("\n")) {
    if (line.startsWith("```")) {
      if (code) code = null;
      else { code = node("pre", ""); container.append(code); }
      continue;
    }
    if (code) { code.textContent += line + "\n"; continue; }
    if (!line.trim()) continue;
    const picture = /^!\[([^\]]*)\]\((\/api\/runs\/[^/]+\/research\/assets\/fig-[0-9a-f]{24})\)$/.exec(line.trim());
    if (picture) {
      const image = node("img"); image.src = picture[2]; image.alt = picture[1];
      image.className = "article-figure"; image.loading = "lazy"; container.append(image); continue;
    }
    const heading = /^(#{1,3})\s+(.+)$/.exec(line);
    const bullet = /^(?:[-*]|\d+\.)\s+(.+)$/.exec(line);
    const element = node(heading ? `h${heading[1].length}` : "p");
    element.innerHTML = (bullet ? "• " : "") + escapeHTML(heading ? heading[2] : bullet ? bullet[1] : line)
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    container.append(element);
  }
  return container;
}

async function refreshConfig() {
  const config = await api("/api/config");
  connected = config.configured;
  $("connection-label").textContent = connected ? config.model : "配置模型";
  $("connection-dot").className = connected ? "connected" : "";
  $("api-url").value = config.base_url;
  $("api-model").value = config.model;
  $("api-max-input").value = config.max_input_chars ?? 300000;
  $("api-timeout").value = config.timeout ?? 300;
  $("api-key").value = "";
}

function showCreate() {
  selected = null;
  current = null;
  $("create-panel").hidden = false;
  $("workspace-panel").hidden = true;
  $("breadcrumb").textContent = "新建作业";
  notify("");
  renderList();
}

let runs = [];
function renderList() {
  $("task-list").replaceChildren();
  $("task-count").textContent = runs.length;
  if (!runs.length) $("task-list").append(node("p", "每一份好作业，\n都从一个清晰的思路开始。", "sidebar-empty"));
  for (const run of runs) {
    const button = node("button", undefined, `task-item${selected === run.run_id ? " active" : ""}`);
    button.append(node("b", run.title));
    const label = node("small");
    label.append(node("span", "", "task-dot"), document.createTextNode(run.status === "completed" ? (run.delivery_confirmed ? "已确认完成 · 可导出" : "全文待确认") : run.status === "failed" ? "处理失败 · 可恢复" : `${run.current_index}/${run.sections || "—"} 部分 · 进行中`));
    button.append(label);
    button.addEventListener("click", () => loadRun(run.run_id).catch((e) => notify(e.message, true)));
    $("task-list").append(button);
  }
}

async function refreshList() {
  const data = await api("/api/runs");
  runs = data.runs;
  renderList();
  const busy = data.jobs.find((job) => ["queued", "running"].includes(job.status));
  if (busy && !polling) {
    activeJob = busy;
    try { await loadRun(busy.run_id); }
    catch {
      selected = busy.run_id;
      $("create-panel").hidden = true;
      $("workspace-panel").hidden = false;
      $("run-title").textContent = "正在处理作业";
    }
    notify("已重新连接正在运行的任务。");
    watchJob(busy);
  }
}

async function loadRun(runId, preserveReview = false) {
  const result = await api(`/api/runs/${encodeURIComponent(runId)}`);
  const keepReview = preserveReview && selected === runId && current?.interrupt_id === result.interrupt_id;
  if (selected !== runId) $("advisor-error").hidden = true;
  selected = runId;
  current = result;
  $("create-panel").hidden = true;
  $("workspace-panel").hidden = false;
  const state = result.state;
  const automatic = result.autonomous !== false;
  const delivery = result.delivery || {ready:false, confirmed:false};
  if (!connected && state.model_profile?.kind === "openai_compatible") {
    $("api-url").value = state.model_profile.base_url || "";
    $("api-model").value = state.model_profile.model || "";
  }
  $("run-title").textContent = state.spec?.title || (state.status === "failed" ? "要求解析失败" : "等待澄清作业要求");
  $("breadcrumb").textContent = "作业工作台";
  const partCount = state.outline_plan?.parts.length || state.spec?.sections.length || 0;
  const wordLabel = state.analysis?.facts.some((fact) => fact.field === "word_limit") ? "字数上限" : "内部写作预算";
  const imageLabel = result.research?.image_goal_optional ? `网络插图建议 ${state.spec?.min_web_images ?? 2}（无相关图可跳过）` : `网络插图最低 ≥${state.spec?.min_web_images ?? 2}`;
  $("run-subtitle").textContent = state.status === "failed" ? `处理已停止 · 失败步骤：${(result.failed_nodes || []).join("、") || "请查看运行诊断"}` : state.spec ? `${partCount} 个部分 · ${wordLabel} ${state.spec.word_limit} · ${state.spec.page_limit ? `页数上限 ${state.spec.page_limit} · ` : ""}${state.current_index} 个部分已确认 · 独立来源目标 ≥${state.spec.min_sources ?? 3} · ${imageLabel}` : "请补充缺失信息或确认冲突处理方式。";
  $("run-id").textContent = `任务 ID / ${runId}`;
  const stage = state.status === "completed" ? (delivery.confirmed ? 4 : 3) : !state.spec ? 0 : state.pending?.artifact_id === "outline" ? 1 : 2;
  $("progress").replaceChildren(...["解析要求", "确认大纲", "逐部分写作", "完成确认"].map((label, i) => {
    const step = node("div", undefined, `progress-step ${i < stage ? "done" : i === stage ? "current" : ""}`);
    step.append(node("span", i < stage ? "✓" : String(i + 1)), document.createTextNode(label));
    return step;
  }));
  const waiting = !automatic && Boolean(result.interrupt_id && state.pending);
  $("approval-controls").hidden = !waiting;
  $("completion-controls").hidden = !delivery.ready;
  $("complete-button").hidden = automatic || delivery.confirmed;
  $("resume-button").hidden = automatic ? (state.status === "completed" && delivery.confirmed) : Boolean(result.interrupt_id) || state.status === "completed";
  $("replan-button").hidden = !state.spec;
  $("action-title").textContent = state.status === "failed" ? "处理失败，可恢复" : state.status === "completed" ? (delivery.confirmed ? "全文已确认，可导出" : "审阅全文并确认完成") : waiting ? (stage === 1 ? "确认你的大纲" : "审阅当前部分") : stage === 0 ? "先把要求说清楚" : "继续你的写作";
  $("action-description").textContent = state.status === "failed" ? "任务已经停止。请查看运行诊断，处理原因后从检查点恢复。" : state.status === "completed" ? (delivery.confirmed ? "当前正文版本已确认完成，可以下载可编辑的 Word 文档。" : "各章已确认。请通读整篇正文，确认完成后即可导出 Word。") : waiting ? "满意后确认继续；需要调整时，写下你的修改意见。" : result.interrupt_id ? "在左侧补充信息，确认后继续生成大纲。" : "若上次处理中断，可从已保存的检查点继续。";
  $("approve-button").textContent = stage === 1 ? (state.outline_plan ? "确认章节划分，开始第1章 →" : "确认大纲，开始正文 →") : state.spec && state.current_index === state.spec.sections.length - 1 && stage === 2 ? "确认最后一章，审阅全文 ✓" : "确认本章，生成下一章 →";
  if (automatic) {
    $("run-subtitle").textContent = `${partCount} 个部分 · 已完成 ${state.current_index} 个 · 自动检索、写作与核验`;
    $("action-title").textContent = state.status === "failed" ? "处理已停止，可恢复" : state.status === "completed" ? "全文核验完成，可导出" : "自动写作进行中";
    $("action-description").textContent = "大纲、章节和全文通过对应检查后自动推进，版本、来源及失败原因随任务保存。";
    if (result.interrupt_payload?.kind === "requirements") {
      $("run-subtitle").textContent = "已读取材料 · 等待补充缺失要求";
      $("action-title").textContent = "请回答缺失要求";
      $("action-description").textContent = "未回复时任务保持等待。未指定的项目会由模型评估并记录依据。";
      $("resume-button").hidden = true;
    }
    $("progress").replaceChildren(...["解析要求", "规划大纲", "自动逐章写作", "全文核验与导出"].map((label, i) => {
      const step = node("div", undefined, `progress-step ${i < stage ? "done" : i === stage ? "current" : ""}`);
      step.append(node("span", i < stage ? "✓" : String(i + 1)), document.createTextNode(label)); return step;
    }));
  }
  if (!keepReview) renderContent();
  renderAdvisor();
  renderList();
  setBusy(Boolean(activeJob));
}

function renderAdvisor() {
  const advisor = current?.advisor || {available:false, exchanges:[]};
  $("advisor-panel").hidden = !advisor.available && !advisor.exchanges.length;
  $("advisor-form").hidden = !advisor.available;
  $("advisor-question").value = advisorDrafts.get(selected) || "";
  $("advisor-hint").textContent = advisor.available
    ? "使用当前模型 API，产生正常调用用量。回答供参考，确认仍由你操作。"
    : "本阶段仅查看答疑记录；提问在要求澄清和大纲确认时开放。";
  const root = $("advisor-history");
  root.replaceChildren();
  for (const exchange of advisor.exchanges) {
    const entry = node("article", undefined, "advisor-exchange");
    entry.append(node("b", "你", "advisor-role"), node("p", exchange.question, "advisor-question-text"));
    entry.append(node("b", "答疑助理", "advisor-role"));
    if (exchange.status === "completed") entry.append(markdown(exchange.answer));
    else if (exchange.status === "failed") {
      entry.append(node("p", exchange.error || "回答失败，请重新提问。", "form-error"));
      if (advisor.available) {
        const retry = node("button", "重新提问", "text-button advisor-retry");
        retry.type = "button";
        retry.disabled = Boolean(activeJob);
        retry.addEventListener("click", () => {
          advisorDrafts.set(selected, exchange.question);
          $("advisor-question").value = exchange.question;
          $("advisor-question").focus();
        });
        entry.append(retry);
      }
    } else entry.append(node("p", "正在回答…", "text-muted"));
    root.append(entry);
  }
  if (!advisor.exchanges.length) root.append(node("p", "可以询问术语、评分要求、澄清问题或章节安排。", "advisor-empty"));
  if (activeJob?.kind === "advisor" && activeJob.run_id === selected) root.append(node("p", "正在结合材料回答，请稍候…", "text-muted"));
  root.scrollTop = root.scrollHeight;
}

function renderContent() {
  const root = $("review-content");
  root.replaceChildren();
  if (!current) return;
  const state = current.state;
  if (activeTab === "library") {
    renderLibrary(root);
  } else if (activeTab === "diagnostics") {
    const diagnostics = current.diagnostics || {events: [], usage: {}};
    const usage = diagnostics.usage;
    root.append(node("h3", "API 调用与用量"));
    root.append(node("p", `已记录 ${diagnostics.response_count || 0} 次响应；${diagnostics.usage_reported_count || 0} 次上报用量。`, "source-meta"));
    root.append(node("p", `输入 tokens：${usage.prompt_tokens ?? "未上报"} / 输出 tokens：${usage.completion_tokens ?? "未上报"} / 合计：${usage.total_tokens ?? "未上报"}`, "text-muted"));
    for (const event of [...diagnostics.events].reverse()) {
      const row = node("details", undefined, "history-item");
      row.append(node("summary", `${event.recorded_at} · ${event.stage || "任务"} · ${event.event}`));
      row.append(node("pre", JSON.stringify(event, null, 2)));
      root.append(row);
    }
    if (!diagnostics.events.length) root.append(node("p", "此任务尚无诊断记录。早期调用无法追溯 token 用量。", "empty-content"));
  } else if (activeTab === "sources") {
    const requirements = state.spec || state.analysis?.requirement_details;
    if (requirements) {
      root.append(node("h3", "引用与插图要求"));
      const imageRule = current.research?.image_goal_optional ? `建议使用 ${requirements.min_web_images ?? 2} 张相关网络图片；缺少合适图可跳过。` : `至少 ${requirements.min_web_images ?? 2} 张网上检索并实际插入的不同图片。`;
      root.append(node("p", `至少 ${requirements.min_sources ?? 3} 个正文实际使用的独立来源；${imageRule}`, "text-muted"));
      root.append(node("p", `引用格式：${requirements.citation_style || "尚未指定"}。AI 自动建立来源库并将核验通过的引用和插图写入正文。`, "text-muted"));
      if (requirements.page_limit) root.append(node("p", `页数上限：${requirements.page_limit}；${requirements.page_count_rules || "统计口径未指定"}`, "text-muted"));
      for (const text of requirements.requirements || []) root.append(node("p", text, "text-muted"));
    }
    for (const clarification of state.clarifications || []) {
      if (!Object.keys(clarification.assessment || {}).length) continue;
      root.append(node("h3", "缺失要求的评估依据"));
      for (const [field, reason] of Object.entries(clarification.assessment)) root.append(node("p", `${field}：${reason}`, "text-muted"));
    }
    for (const material of state.materials) {
      const details = node("details", undefined, "source-item");
      details.append(node("summary", material.path));
      details.append(node("p", `${material.role} · ${material.units.length} 个文本单元 · ${material.source_id}`, "source-meta"));
      details.append(node("pre", material.units.map((unit) => `[${unit.location}${unit.method === "ocr" ? " · OCR" : ""}]\n${unit.text}`).join("\n\n")));
      root.append(details);
    }
    if (!state.materials.length) root.append(node("p", "此任务从结构化要求创建，未附带材料。", "empty-content"));
    if (state.analysis?.facts.length) {
      const facts = node("div", undefined, "facts");
      facts.append(node("h3", "已提取的要求与依据"));
      for (const fact of state.analysis.facts) facts.append(node("p", `${fact.field}：${fact.value}`, "text-muted"), node("blockquote", `${fact.quote}\n${fact.source_id} / ${fact.location}`));
      root.append(facts);
    }
  } else if (activeTab === "history") {
    if (current.effective_outline) {
      const effective = node("details", undefined, "history-item");
      effective.append(node("summary", "当前执行大纲 · 按证据自动修订"), markdown(current.effective_outline));
      root.append(effective);
    }
    for (const revision of [...(current.outline_revisions || [])].reverse()) {
      const details = node("details", undefined, "history-item");
      const status = revision.status === "outline_revised" ? "已核验并修订" : revision.status === "outline_revision_rejected" ? "修订被拒绝" : "保留原计划";
      details.append(node("summary", `${revision.recorded_at} · ${revision.artifact_id} 示例 · ${status}`), node("p", revision.reason), node("pre", JSON.stringify({before: revision.before, after: revision.after, source_ids: revision.source_ids, review: revision.review, issues: revision.issues}, null, 2)));
      root.append(details);
    }
    for (const record of state.approvals.filter((a) => a.interrupt_id.startsWith("auto:"))) {
      root.append(node("p", `${record.recorded_at} · 自动推进 ${record.artifact_id} 第 ${record.version} 版 · ${record.feedback}`, "text-muted"));
    }
    for (const artifact of [...state.history].reverse()) {
      const details = node("details", undefined, "history-item");
      details.append(node("summary", `${artifact.artifact_id === "outline" ? "大纲" : artifact.artifact_id.replace("section-", "部分 ")} · 第 ${artifact.version} 版`), node("pre", artifact.content));
      root.append(details);
    }
    if (!state.history.length) root.append(node("p", "尚无版本记录。", "empty-content"));
  } else if (current.autonomous !== false && state.status === "failed") {
    root.append(node("div", "自动处理已停止，当前旧稿不会被视为已通过。", "issues"));
    for (const issue of current.research?.issues || state.analysis?.issues || []) root.append(node("p", issue, "text-muted"));
    if (current.research?.status !== "search_budget_exhausted") {
      root.append(node("p", "可查看运行诊断和历史记录，并从检查点恢复。", "text-muted"));
    }
  } else if (current.autonomous !== false && state.pending && state.status !== "completed") {
    root.append(node("div", "自动写作与核验中，已保存的版本供查看。", "pill"), markdown(state.pending.content));
  } else if (current.interrupt_payload?.kind === "requirements") {
    renderClarification(root);
  } else if (state.pending) {
    const label = node("div", undefined, "content-label");
    label.append(node("span", "等待你的确认", "pill"), node("span", `第 ${state.pending.version} 版`));
    root.append(label, markdown(state.pending.content));
    if (state.pending.artifact_id === "outline") {
      const controls = node("div", undefined, "plan-confirmation");
      controls.append(node("p", "确认章节划分后，AI 将开始第一章正文；每章经你确认后再继续下一章。", "text-muted"));
      const button = node("button", "确认以上划分并开始写作 →", "primary full");
      button.id = "confirm-plan-button";
      button.disabled = Boolean(activeJob);
      button.addEventListener("click", () => $("approve-button").click());
      controls.append(button);
      root.append(controls);
    }
  } else if (state.status === "completed") {
    root.append(node("div", current.autonomous !== false ? "✓ 全文自动核验完成，可以导出 Word 文档。" : current.delivery?.confirmed ? "✓ 全文已确认完成，可以导出 Word 文档。" : "各章均已确认。请通读全文，核对事实、引用和字数后点击确认完成。", "completed-stamp"));
    for (const draft of state.drafts) root.append(markdown(draft.content), node("div", "", "divider"));
    if (current.delivery?.ready && (current.autonomous === false || current.delivery.confirmed)) {
      const button = node("button", current.delivery.confirmed ? "下载已确认正文（DOCX） ↓" : "我已审阅全文，确认完成 ✓", "primary full");
      button.id = "final-document-button";
      button.addEventListener("click", () => $(current.delivery.confirmed ? "export-button" : "complete-button").click());
      root.append(button);
    }
  } else if (state.status === "failed") {
    const latest = (current.diagnostics?.events || []).filter((event) => event.event === "validation.failed").at(-1);
    root.append(node("div", "本次处理已失败，不会继续等待或自动重复调用 API。", "issues"));
    if (latest) root.append(node("pre", JSON.stringify(latest.details, null, 2), "text-muted"));
    root.append(node("p", "在“运行诊断”查看具体校验位置与 API 用量，然后恢复任务。", "text-muted"));
  } else root.append(node("p", "正在准备内容，或等待恢复上次处理。", "empty-content"));
}

function libraryLink(label, url) {
  const link = node("a", label);
  link.href = url; link.target = "_blank"; link.rel = "noopener noreferrer";
  return link;
}

function renderLibrary(root) {
  const library = current.research;
  if (!library?.available) {
    root.append(node("p", "请先确认作业要求，再建立来源库。", "empty-content")); return;
  }
  root.append(node("h2", "正文已使用的文献与插图"));
  const imageCount = library.image_goal_optional ? `${library.used_images || 0} · 建议 ${library.min_web_images}（可跳过）` : `${library.used_images || 0}/${library.required_web_images}`;
  root.append(node("p", `正文已核验引用 ${library.used_sources || 0}/${library.min_sources} · 已核验插图 ${imageCount}`, "library-count"));
  const budget = library.search_budget;
  if (budget) {
    const chapter = budget.chapter === null ? "" : `第 ${budget.chapter} 章 ${budget.chapter_used}/${budget.chapter_limit} 次 · `;
    root.append(node("p", `搜索用量 ${chapter}全篇 ${budget.total_used}/${budget.total_limit} 次`, "text-muted"));
  }
  root.append(node("p", "AI 先复用已核验的文献和图片，缺少必需证据时自动补查。引用、插图和修复结果会经过写后核验。", "text-muted"));
  if (library.status === "not_started") root.append(node("p", "确认大纲并开始正文后，AI 会自动建立本次作业的来源库。", "empty-content"));
  if (library.stale) root.append(node("p", "要求或大纲已变化，旧记录保留；继续写作时 AI 会按当前要求重新检索。", "issues"));
  if (library.issues.length) root.append(node("p", library.issues.join("\n"), "issues library-issues"));
  if (library.queries.length) root.append(node("p", "AI 检索词：" + library.queries.join("；"), "text-muted"));
  if (library.storage === "used_sources_only") {
    root.append(node("p", "这里只收录正文实际引用或插图实际使用且核验通过的来源。未采用资料暂存于取证池，章节通过后清理；已用来源的旧版本随历史保留。", "text-muted"));
    if (!library.sources.length) root.append(node("p", "尚无已提交的正文来源；检索候选不会提前入库。", "empty-content"));
  }
  const usedSources = new Set(Object.values(current.state.grounded_drafts || {}).flatMap((item) => item.citation_ids));
  const usedFigures = new Set(Object.values(current.state.grounded_drafts || {}).flatMap((item) => item.figure_ids));
  for (const source of library.sources) {
    const entry = node("article", undefined, "library-entry");
    entry.append(node("h3", source.title), node("p", `${source.authors.join("、")} · ${source.year || "年份待核对"}`, "text-muted"));
    entry.append(libraryLink("来源网页 ↗", source.url));
    if (source.doi) entry.append(node("p", `DOI：${source.doi}`, "text-muted"));
    if (source.pdf) entry.append(libraryLink(" 下载源论文 PDF ↓", `/api/runs/${encodeURIComponent(selected)}/research/assets/${source.id}`));
    entry.append(node("p", source.verified ? source.verification : "尚未通过来源对应性检查", "text-muted"));
    if (source.error) entry.append(node("p", source.error, "form-error"));
    if (source.verified && !source.integrity_ok) entry.append(node("p", "缓存缺失或变化，请恢复检索。", "form-error"));
    entry.append(node("p", usedSources.has(source.id) && source.integrity_ok ? "✓ 已自动核验并用于正文引用" : "候选资料 · 尚未用于已通过核验的正文", "library-decision"));
    root.append(entry);
  }
  if (library.figures.length) root.append(node("h2", "正文已使用的图片"));
  for (const figure of library.figures) {
    const entry = node("article", undefined, "library-entry");
      entry.append(node("h3", ["licensed_web_image", "web_page_image"].includes(figure.method)
        ? `网络图片 ${figure.figure_number} · 已核对原网页`
        : `Figure ${figure.figure_number} · PDF 第 ${figure.page} 页`));
    if (figure.integrity_ok) {
      const image = node("img"); image.src = `/api/runs/${encodeURIComponent(selected)}/research/assets/${figure.id}`;
      image.alt = figure.caption; image.loading = "lazy"; entry.append(image);
    }
      entry.append(node("p", figure.caption), libraryLink(figure.pdf_url ? "原始 PDF ↗" : "原始网页 ↗", figure.pdf_url || figure.source_url));
    entry.append(node("p", `许可：${figure.license.status === "declared" ? "已保存声明，仍需区分网页或论文声明与单图授权" : "许可或适用条件未确认，仅作提示，不阻断生成和导出"}`, "text-muted"));
    for (const warning of figure.license_warnings || []) entry.append(node("p", warning, "text-muted"));
    if (figure.license.url) entry.append(libraryLink("许可声明 ↗", figure.license.url));
    const evidence = node("details"); evidence.append(node("summary", "查看许可与图片证据"), node("p", `${figure.license.page ? `许可记录位于 PDF 第 ${figure.license.page} 页。` : ""}${figure.license.evidence}`), node("p", figure.bbox ? `裁剪区域：${figure.bbox.map((value) => Math.round(value)).join(", ")} · ${figure.dpi} DPI` : "来自已核对的网页原图", "text-muted"));
    entry.append(evidence);
    entry.append(node("p", usedFigures.has(figure.id) && figure.integrity_ok ? "✓ 已插入正文并通过图意、图注及来源核验" : "候选图片 · 尚未用于已通过核验的正文", "library-decision"));
    root.append(entry);
  }
  if (library.events.length) {
    const history = node("details", undefined, "history-item"); history.append(node("summary", "自动检索与核验记录"));
    const labels = {"writing.search":"按证据缺口检索", "writing.reuse":"复用已核验的证据", "writing.retry":"核验未通过，自动修复", "writing.verified":"章节引用与插图核验通过", "reference.failed":"全文核验未通过，修订受影响章节", "reference.passed":"全文 Reference 核验通过"};
    for (const event of library.events) history.append(node("p", `${event.recorded_at} · ${event.cached_only ? "复用已保存证据" : labels[event.code] || event.code}${event.artifact_id ? " · " + event.artifact_id : ""}${event.issues ? " · " + event.issues.join("；") : ""}${event.error ? " · " + event.error : ""}`));
    root.append(history);
  }
}

function renderClarification(root) {
  root.append(node("div", current.state.analysis?.issues.join("\n") || "请确认作业要求", "issues"));
  if (current.autonomous !== false) {
    const form = node("form", undefined, "clarify-form");
    const label = node("label", "请补充字数、引用格式及图片等要求", "field-label");
    label.htmlFor = "requirements-answer";
    const answer = node("textarea"); answer.id = "requirements-answer"; answer.rows = 5;
    answer.required = true; answer.maxLength = 4000;
    answer.placeholder = "有要求请写明；如果没有额外要求，请回复“没有额外要求”，模型会评估未指定项目。";
    const button = node("button", "提交回答，继续解析 →", "primary"); button.type = "submit";
    form.append(label, answer, button);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        await launch(`/api/runs/${selected}/requirements-answer`, {
          method: "POST",
          body: JSON.stringify({interrupt_id:current.interrupt_id, answer:answer.value}),
        });
      } catch (error) { notify(error.message, true); }
    });
    root.append(form);
    return;
  }
  const form = node("form", undefined, "clarify-form");
  const modeLabel = node("label", undefined, "recommend-option");
  const mode = node("input"); mode.type = "checkbox"; mode.checked = true; mode.name = "recommend";
  modeLabel.append(mode, document.createTextNode(" 由 AI 根据材料推荐章节划分（推荐）"));
  form.append(modeLabel);
  form.append(node("p", "先确认题目与篇幅，AI 会给出章节、论点和字数预算。你确认后才开始正文。", "text-muted"));
  const preserved = {...(current.state.analysis?.requirement_details || {}), ...(current.state.spec || current.state.analysis?.spec || {})};
  const fields = [
    ["title", "作业题目", "input"], ["sections", "部分划分（每行一个）", "textarea"], ["word_limit", "字数上限", "input"],
    ["citation_style", "引用格式", "input"], ["min_sources", "独立引用来源最少数量", "input"], ["min_web_images", "网络插图最少数量", "input"],
    ["requirements", "引用、图片及其他要求（每行一项）", "textarea"],
    ["language", "正文语言", "input"], ["task_type", "作业类型", "input"], ["word_count_rules", "字数统计口径", "input"],
    ["note", "澄清或冲突处理决定", "textarea"],
  ];
  for (const [name, label, tag] of fields) {
    const id = `clarify-${name}`;
    const fieldLabel = node("label", label, "field-label"); fieldLabel.htmlFor = id;
    const input = node(tag); input.id = id; input.name = name; input.required = ["title", "word_limit", "note", "min_sources", "min_web_images"].includes(name);
    if (name === "sections") { input.disabled = true; input.placeholder = "手动划分时每行填写一个真实章节名；不要填写操作指令。"; }
    if (name === "word_limit") { input.type = "number"; input.min = "1"; }
    if (["min_sources", "min_web_images"].includes(name)) {
      input.type = "number"; input.min = name === "min_sources" ? "3" : "0"; input.step = "1";
      input.value = preserved[name] ?? Number(input.min);
    } else if (name in preserved) input.value = Array.isArray(preserved[name]) ? preserved[name].join("\n") : preserved[name] || "";
    const fact = current.state.analysis?.facts.find((item) => item.field === name);
    if (fact && ["title", "sections"].includes(name)) input.value = name === "sections" ? fact.value.split(/[；;、|]/).join("\n") : fact.value;
    if (name === "citation_style") input.placeholder = "例如 APA 7、Harvard；按作业要求填写";
    if (name === "requirements") input.rows = "4";
    form.append(fieldLabel, input);
    if (name === "min_sources") form.append(node("p", "按正文实际使用的独立来源计数，同一来源重复引用只算一个。", "text-muted"));
    if (name === "min_web_images") form.append(node("p", "只计入网上检索且实际插入的不同图片；原创图、重复图和链接不计入。来源库优先从公开论文 PDF 获取候选图。", "text-muted"));
  }
  mode.addEventListener("change", () => {
    form.elements.sections.disabled = mode.checked;
    form.elements.sections.required = !mode.checked;
  });
  const button = node("button", "确认要求，生成大纲 →", "primary"); button.type = "submit";
  form.append(button);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(form);
    const recommend = mode.checked;
    // 完整提交可编辑的要求，避免只保留基础字段而丢失引用与图片约束。
    const spec = {...preserved, title:data.get("title"), sections:recommend ? [] : data.get("sections").split("\n").map((value) => value.trim()).filter(Boolean), section_mode:recommend ? "recommend" : "manual", word_limit:Number(data.get("word_limit"))};
    for (const name of ["citation_style", "language", "task_type", "word_count_rules"]) spec[name] = data.get(name).trim() || null;
    for (const name of ["min_sources", "min_web_images"]) spec[name] = Number(data.get(name));
    spec.requirements = data.get("requirements").split("\n").map((value) => value.trim()).filter(Boolean);
    const body = {interrupt_id:current.interrupt_id, spec, note:data.get("note")};
    try { await launch(`/api/runs/${selected}/clarify`, { method: "POST", body: JSON.stringify(body) }); }
    catch (error) { notify(error.message, true); }
  });
  root.append(form);
}

function setBusy(busy) {
  $("busy-banner").hidden = !busy;
  $("busy-banner").querySelector("b").textContent = activeJob?.kind === "advisor" ? "答疑助理正在回答" : "正在处理，请稍候";
  $("busy-banner").querySelector("small").textContent = activeJob?.kind === "advisor" ? "结合本次材料回答，作业仍在等待你的确认。" : "材料解析、OCR 或模型生成可能需要一些时间。";
  if (activeJob?.kind === "research") {
    $("busy-banner").querySelector("b").textContent = "正在检索论文与图源";
    $("busy-banner").querySelector("small").textContent = "读取公开论文、核对元数据并定位图片；成功结果会保存。";
  }
  $("advisor-send").disabled = busy || !current?.advisor?.available;
  $("advisor-send").textContent = busy && activeJob?.kind === "advisor" ? "正在回答…" : "发送问题 ↗";
  if (activeJob?.kind === "writing" && current?.research.automatic) {
    $("busy-banner").querySelector("b").textContent = "AI 正在检索、写作并核验";
    $("busy-banner").querySelector("small").textContent = "引用或插图不通过时会自动重查重写；通过后才进入章节审阅。";
  }
  document.querySelectorAll(".advisor-retry").forEach((button) => { button.disabled = busy; });
  for (const id of ["start-button", "revise-button", "approve-button", "resume-button", "replan-button"]) $(id).disabled = busy;
  $("complete-button").disabled = busy || !current?.delivery?.ready || current.delivery.confirmed;
  $("export-button").disabled = busy || !current?.delivery?.confirmed;
  if ($("final-document-button")) $("final-document-button").disabled = busy;
  if ($("confirm-plan-button")) $("confirm-plan-button").disabled = busy;
  document.querySelectorAll(".clarify-form button").forEach((button) => { button.disabled = busy; });
}

async function launch(path, options) {
  if (activeJob) throw new Error("已有任务正在处理，请稍候。");
  setBusy(true);
  try {
    const job = await api(path, options);
    activeJob = job;
    if (job.kind === "research") {
      selectTab("library"); renderContent(); notify("正在检索并核对论文与图源，写作进度保持等待确认。"); watchJob(job); return;
    }
    if (job.kind === "advisor") {
      advisorDrafts.delete(job.run_id);
      $("advisor-question").value = "";
      $("advisor-error").hidden = true;
      renderAdvisor();
      watchJob(job);
      return;
    }
    selectTab("draft");
    selected = job.run_id;
    $("create-panel").hidden = true;
    $("workspace-panel").hidden = false;
    $("run-title").textContent = "正在处理作业";
    $("breadcrumb").textContent = "作业工作台";
    notify("");
    watchJob(job);
  } catch (error) { setBusy(false); throw error; }
}

async function watchJob(job) {
  if (polling) return;
  polling = true;
  activeJob = job;
  setBusy(true);
  try {
    let status = job;
    let ticks = 0;
    while (["queued", "running"].includes(status.status)) {
      await new Promise((resolve) => setTimeout(resolve, 1000));
      status = await api(`/api/jobs/${job.job_id}`);
      ticks += 1;
      if (status.status === "running" && ticks % 3 === 0 && job.kind === "writing" && selected === job.run_id) {
        try { await loadRun(job.run_id, true); } catch { /* 初次解析还没有检查点时继续等待后台任务。 */ }
      }
    }
    activeJob = null;
    if (status.status === "failed") notify(status.error || "处理失败，可以恢复任务后重试。", true);
    else notify(job.kind === "advisor" ? "答疑已保存。请参考回答，自行决定是否调整要求或大纲。" : job.kind === "research" ? "检索进度已保存，请查看来源库的缺项与待审阅记录。" : "已保存。审阅内容后，再决定下一步。");
    try { await loadRun(job.run_id, job.kind === "advisor"); } catch { showCreate(); notify(status.error || "任务尚未建立，请检查材料后重试。", true); }
    if (job.kind === "writing" && current?.state.status === "completed" && status.status === "completed") notify(current.autonomous !== false ? "全文自动核验完成，记录已保存，可导出 Word。" : current.delivery?.confirmed ? "全文已确认完成，进度已保存。" : "各章已确认，请审阅全文并确认完成。");
    await refreshList();
    if (job.kind !== "advisor" && job.kind !== "research") $("feedback").value = "";
  } catch (error) {
    if (job.kind === "advisor") {
      try { await loadRun(job.run_id, true); } catch { /* 保留当前表单，等待用户刷新连接。 */ }
      notify(`连接中断：${error.message} 请刷新答疑记录，确认状态后重新提问。`, true);
    } else notify(`连接中断：${error.message} 刷新页面后可恢复任务。`, true);
  }
  finally { activeJob = null; polling = false; setBusy(false); }
}

$("new-task").addEventListener("click", showCreate);
$("open-config").addEventListener("click", () => { $("config-error").textContent = ""; $("config-dialog").showModal(); });
$("close-config").addEventListener("click", () => { $("config-dialog").close(); $("api-key").value = ""; });
$("config-dialog").addEventListener("cancel", () => { $("api-key").value = ""; });
$("config-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await api("/api/config", { method: "POST", body: JSON.stringify({ base_url: $("api-url").value.trim().replace(/\/$/, ""), model: $("api-model").value.trim(), api_key: $("api-key").value, max_input_chars: Number($("api-max-input").value), timeout: Number($("api-timeout").value) }) });
    await refreshConfig(); $("config-dialog").close(); notify("模型配置已保存在当前服务会话中。");
  } catch (error) { $("config-error").textContent = error.message; }
});
$("advisor-question").addEventListener("input", () => {
  if (selected) advisorDrafts.set(selected, $("advisor-question").value);
});
$("advisor-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!current?.advisor?.available || activeJob) return;
  const question = $("advisor-question").value.trim();
  if (!question) { $("advisor-question").focus(); return; }
  if (!connected) { $("config-dialog").showModal(); return; }
  $("advisor-error").hidden = true;
  try {
    await launch(`/api/runs/${encodeURIComponent(selected)}/advisor`, {
      method:"POST", body:JSON.stringify({interrupt_id:current.interrupt_id, question}),
    });
  } catch (error) {
    $("advisor-error").textContent = error.message;
    $("advisor-error").hidden = false;
  }
});
$("clear-config").addEventListener("click", async () => {
  try { await api("/api/config", { method: "DELETE" }); await refreshConfig(); $("config-dialog").close(); notify(connected ? "会话配置已清除，当前使用本地环境配置。" : "会话配置已清除。"); }
  catch (error) { $("config-error").textContent = error.message; }
});
for (const [id, label, fallback] of [["assignment", "assignment-label", "将作业要求拖到这里"], ["rubrics", "rubric-label", "添加 Rubric"], ["courses", "course-label", "讲义、阅读或参考材料"]]) {
  $(id).addEventListener("change", () => { $(label).textContent = [...$(id).files].map((file) => file.name).join("、") || fallback; });
}
for (const eventName of ["dragenter", "dragover"]) $("dropzone").addEventListener(eventName, (event) => { event.preventDefault(); $("dropzone").classList.add("dragover"); });
$("dropzone").addEventListener("dragleave", () => $("dropzone").classList.remove("dragover"));
$("dropzone").addEventListener("drop", (event) => {
  event.preventDefault(); $("dropzone").classList.remove("dragover");
  if (event.dataTransfer.files.length !== 1) { notify("作业要求请一次选择一个文件。", true); return; }
  $("assignment").files = event.dataTransfer.files; $("assignment").dispatchEvent(new Event("change"));
});
$("create-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!connected) { $("config-dialog").showModal(); return; }
  const data = new FormData();
  data.append("assignment", $("assignment").files[0]);
  for (const file of $("rubrics").files) data.append("rubrics", file);
  for (const file of $("courses").files) data.append("courses", file);
  try { await launch("/api/runs", { method: "POST", body: data }); }
  catch (error) { notify(error.message, true); }
});
for (const action of ["approve", "revise"]) $(`${action}-button`).addEventListener("click", async () => {
  if (!current?.state.pending) return;
  const pending = current.state.pending;
  if (action === "revise" && !$("feedback").value.trim()) { $("feedback").focus(); notify("请先填写具体修改意见。", true); return; }
  try { await launch(`/api/runs/${selected}/decision`, { method: "POST", body: JSON.stringify({ interrupt_id: current.interrupt_id, artifact_id: pending.artifact_id, version: pending.version, action, feedback: $("feedback").value }) }); }
  catch (error) { notify(error.message, true); }
});
$("resume-button").addEventListener("click", async () => { try { await launch(`/api/runs/${selected}/resume`, { method: "POST" }); } catch (error) { notify(error.message, true); } });
$("complete-button").addEventListener("click", async () => {
  if (!current?.delivery?.ready || activeJob) return;
  setBusy(true);
  try {
    await api(`/api/runs/${selected}/complete`, {method:"POST", body:JSON.stringify({content_hash:current.delivery.content_hash})});
    await loadRun(selected);
    await refreshList();
    notify("全文已确认完成，现在可以导出 Word 文档。");
  } catch (error) { notify(error.message, true); }
  finally { setBusy(false); }
});
$("export-button").addEventListener("click", async () => {
  if (!current?.delivery?.confirmed) return;
  const button = $("export-button"); button.disabled = true;
  try {
    const response = await fetch(`/api/runs/${selected}/export.docx`);
    if (!response.ok) { const error = await response.json(); throw new Error(error.detail || "导出失败"); }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = node("a"); link.href = url;
    const encoded = /filename\*=UTF-8''([^;]+)/i.exec(response.headers.get("Content-Disposition") || "");
    link.download = encoded ? decodeURIComponent(encoded[1]) : "assignment.docx";
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
    notify("Word 文档已生成，请查看浏览器下载记录。");
  } catch (error) { notify(error.message, true); }
  finally { button.disabled = false; }
});
$("replan-button").addEventListener("click", async () => {
  try { activeTab = "draft"; await launch(`/api/runs/${selected}/replan`, {method:"POST"}); }
  catch (error) { notify(error.message, true); }
});
$("refresh-run").addEventListener("click", () => { if (selected) loadRun(selected).catch((error) => notify(error.message, true)); });
document.querySelectorAll("[data-tab]").forEach((button) => button.addEventListener("click", () => {
  selectTab(button.dataset.tab);
  renderContent();
}));
document.addEventListener("keydown", (event) => { if (!event.ctrlKey && !event.metaKey && !event.altKey && event.key.toLowerCase() === "n" && !["INPUT", "TEXTAREA"].includes(document.activeElement.tagName) && !$("config-dialog").open) showCreate(); });
Promise.all([refreshConfig(), refreshList()]).catch((error) => notify(error.message, true));
