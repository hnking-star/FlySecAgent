(() => {
  "use strict";
  let selectedSession = "";
  let selectedObservation = null;
  let current = null;
  let graphFilter = "all";
  let apiQuery = "";
  let apiMode = "all";
  let expandedApiId = null;
  let selectedNodeId = null;
  const $ = (id) => document.getElementById(id);
  const STATUS_LABELS = {
    "tried-hit": "确认命中",
    "tried-miss": "确认未命中",
    "inferred-open": "等待验证",
    "scan-class": "扫描归类",
    published: "已发布",
    unchanged: "无变化",
    running: "整理中",
    failed: "整理失败",
    cancelled: "已取消",
  };

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function setLoading(loading) {
    $("connection").classList.toggle("loading", loading);
    $("connection").lastChild.textContent = loading ? " 正在同步" : " 已连接";
  }

  async function request(path, options = {}) {
    const response = await fetch(path, options);
    if (!response.ok) {
      let body = {};
      try { body = await response.json(); } catch (_) { /* ignored */ }
      throw new Error(body.message || `HTTP ${response.status}`);
    }
    return response;
  }

  function statusLabel(status) { return STATUS_LABELS[status] || status || "等待整理"; }

  function statusKind(status) {
    return Object.prototype.hasOwnProperty.call(STATUS_LABELS, status) ? status : "off";
  }

  async function renderProjects(projects, preferredSession = "") {
    const select = $("project-select"); select.replaceChildren();
    if (!projects.length) {
      select.append(el("option", "", "没有项目"));
      selectedSession = ""; current = null;
      $("overview").replaceChildren();
      showEmpty("还没有测试项目", "在 Coco 中发起一次测试后，新项目会自动出现在这里。");
      return;
    }
    for (const project of projects) {
      const status = project.observation_enabled ? "ON" : "OFF";
      const option = el("option", "", `${status} · ${project.target} · ${project.session_id.slice(0, 8)}`);
      option.value = project.session_id; select.append(option);
    }
    const fromUrl = new URLSearchParams(location.search).get("session");
    const wanted = preferredSession || fromUrl;
    selectedSession = projects.some((p) => p.session_id === wanted) ? wanted : projects[0].session_id;
    select.value = selectedSession;
    await loadProject(preferredSession ? selectedObservation : null);
  }

  async function refreshProjects() {
    setLoading(true);
    try {
      const body = await (await request("/web/projects")).json();
      await renderProjects(body.projects, selectedSession);
      toast("项目和黑板已同步");
    } catch (error) { showError(error); }
    finally { setLoading(false); }
  }

  function resetFilters() {
    graphFilter = "all"; apiQuery = ""; apiMode = "all"; expandedApiId = null; selectedNodeId = null;
    $("api-search").value = ""; $("api-filter").value = "all";
  }

  function showEmpty(title, message) {
    $("empty-title").textContent = title;
    $("empty-message").textContent = message;
    $("content").classList.add("hidden");
    $("empty").classList.remove("hidden");
  }

  async function loadProject(observationId = null) {
    selectedSession = $("project-select").value;
    if (!selectedSession) return;
    selectedObservation = observationId;
    setLoading(true);
    const query = observationId ? `?observation_id=${encodeURIComponent(observationId)}` : "";
    try {
      current = await (await request(`/web/project/${encodeURIComponent(selectedSession)}${query}`)).json();
      history.replaceState(null, "", `/web/?session=${encodeURIComponent(selectedSession)}`);
      renderProject(current);
    } catch (error) { showError(error); }
    finally { setLoading(false); }
  }

  function renderProject(data) {
    renderVersions(data.versions, data.observation?.id || null);
    renderOverview(data);
    if (!data.state) {
      showEmpty("尚未生成黑板", "项目已经存在，但 Observer 还没有发布有效快照。");
      return;
    }
    $("empty").classList.add("hidden"); $("content").classList.remove("hidden");
    selectedNodeId = null;
    renderGraphFilters(data.state.assessments || []);
    renderGraph(data.state.assessments || []);
    renderApis(data.state.apis || [], data.state.assessments || []);
    $("map-text").textContent = data.map_text || "（当前版本没有短反馈）";
    $("record-detail").className = "evidence-view placeholder";
    $("record-detail").textContent = "点击 record 证据查看原始输入与结果。";
  }

  function renderVersions(versions, activeId) {
    const select = $("version-select"); select.replaceChildren();
    const latest = el("option", "", "当前发布版本"); latest.value = ""; select.append(latest);
    for (const version of versions) {
      const label = `#${version.id} · ${version.finished_at || version.started_at} · ${(version.revision || "无 revision").slice(0, 12)}`;
      const option = el("option", "", label); option.value = String(version.id);
      if (version.id === activeId && selectedObservation) option.selected = true;
      select.append(option);
    }
  }

  function statusCounts(items) {
    return items.reduce((out, item) => { out[item.status] = (out[item.status] || 0) + 1; return out; }, {});
  }

  function renderOverview(data) {
    const p = data.project, state = data.state || {}, latest = data.latest_run;
    const target = $("overview"); target.replaceChildren();
    const summary = el("section", "project-summary");
    const copy = el("div", "project-copy");
    copy.append(el("p", "eyebrow", "ACTIVE ENGAGEMENT"), el("h2", "", p.target), el("p", "project-objective", p.objective));
    const badges = el("div", "project-badges");
    badges.append(badge(p.observation_enabled ? "观察开启" : "观察关闭", p.observation_enabled ? "ok" : "off"));
    badges.append(badge(p.observer_paused ? "Observer 暂停" : "Observer 运行", p.observer_paused ? "warn" : "ok"));
    badges.append(badge(statusLabel(latest?.status), statusKind(latest?.status)));
    summary.append(copy, badges); target.append(summary);
    const counts = statusCounts(state.assessments || []);
    const metrics = [
      ["判断", (state.assessments || []).length, `${counts["tried-hit"] || 0} 命中 · ${counts["tried-miss"] || 0} 未命中`],
      ["API", (state.apis || []).length, `${(state.apis || []).filter((a) => (a.tests || []).length).length} 已测试`],
      ["证据位置", `#${p.processed_record_id}`, p.pending_window_end === null ? "窗口已提交" : `待处理至 #${p.pending_window_end}`],
      ["版本", (state.revision || "—").slice(0, 8), data.observation ? `observation #${data.observation.id}` : "尚未发布"],
    ];
    const grid = el("div", "metrics-grid");
    for (const [label, value, note] of metrics) { const card = el("div", "stat"); card.append(el("span", "", label), el("strong", "", value), el("small", "", note)); grid.append(card); }
    target.append(grid);
  }

  function badge(text, kind) { return el("span", `badge ${kind}`, text); }

  function renderGraphFilters(items) {
    const counts = statusCounts(items), target = $("graph-filters"); target.replaceChildren();
    const options = [["all", "全部", items.length], ["tried-hit", "命中", counts["tried-hit"] || 0], ["tried-miss", "未命中", counts["tried-miss"] || 0], ["inferred-open", "待验证", counts["inferred-open"] || 0]];
    for (const [value, label, count] of options) {
      const button = el("button", `filter ${graphFilter === value ? "active" : ""}`, `${label} ${count}`); button.type = "button";
      button.setAttribute("aria-pressed", graphFilter === value ? "true" : "false");
      button.addEventListener("click", () => { graphFilter = value; renderGraphFilters(items); renderGraph(items); }); target.append(button);
    }
  }

  function depths(items) {
    const byId = new Map(items.map((item) => [item.id, item])), memo = new Map();
    function visit(id, visiting = new Set()) {
      if (memo.has(id)) return memo.get(id); if (visiting.has(id)) return 0; visiting.add(id);
      const parents = (byId.get(id)?.dependsOn || []).filter((parent) => byId.has(parent));
      const value = parents.length ? 1 + Math.max(...parents.map((parent) => visit(parent, visiting))) : 0;
      visiting.delete(id); memo.set(id, value); return value;
    }
    for (const item of items) visit(item.id); return memo;
  }

  function renderGraph(allItems) {
    const items = graphFilter === "all" ? allItems : allItems.filter((item) => item.status === graphFilter);
    const graph = $("graph"); graph.replaceChildren();
    if (!items.length) { $("graph-count").textContent = `0/${allItems.length} 个判断`; graph.style.height = "260px"; graph.append(el("div", "graph-empty", "当前筛选没有判断。")); return; }
    const depth = depths(items), maxDepth = Math.max(...depth.values()), positions = new Map();
    const levels = new Map(); let maxRows = 1;
    for (const item of items) { const d = depth.get(item.id) || 0; if (!levels.has(d)) levels.set(d, []); levels.get(d).push(item); }
    for (const [d, rows] of levels) {
      maxRows = Math.max(maxRows, rows.length);
      rows.forEach((item, i) => positions.set(item.id, { x: 236 + d * 280, y: 56 + i * 126 }));
    }
    const width = Math.max(760, 236 + maxDepth * 280 + 266);
    const height = Math.max(320, maxRows * 126 + 80);
    const root = { x: 28, y: 56 + ((maxRows - 1) * 126) / 2 };
    graph.style.height = `${Math.min(height, 650)}px`;
    $("graph-count").textContent = `${items.length}/${allItems.length} 个判断 · ${maxDepth + 1} 层`;
    const stage = el("div", "graph-stage"); stage.style.width = `${width}px`; stage.style.height = `${height}px`;
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg"); svg.setAttribute("width", width); svg.setAttribute("height", height); stage.append(svg);
    const rootLabel = el("span", "graph-layer-label", "测试目标"); rootLabel.style.left = `${root.x}px`; stage.append(rootLabel);
    for (let d = 0; d <= maxDepth; d += 1) {
      const label = el("span", "graph-layer-label", d === 0 ? "第一层发现" : `第 ${d + 1} 层延伸`);
      label.style.left = `${236 + d * 280}px`; stage.append(label);
    }
    const rootNode = el("div", "graph-root"); rootNode.style.left = `${root.x}px`; rootNode.style.top = `${root.y}px`;
    rootNode.append(el("span", "root-kicker", "TEST TARGET"), el("strong", "", current?.project?.target || "当前测试目标"), el("span", "root-note", `${items.length} 个判断`));
    stage.append(rootNode);
    const selected = items.find((item) => item.id === selectedNodeId) || items[0];
    selectedNodeId = selected.id;
    function connect(from, to, fromWidth) {
      const x1 = from.x + fromWidth, y1 = from.y + 49, x2 = to.x, y2 = to.y + 49;
      const bend = Math.max(34, (x2 - x1) / 2);
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("class", "graph-edge");
      path.setAttribute("d", `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`);
      svg.append(path);
      const dot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      dot.setAttribute("class", "graph-edge-dot"); dot.setAttribute("cx", x2); dot.setAttribute("cy", y2); dot.setAttribute("r", "3"); svg.append(dot);
    }
    for (const item of items) {
      const nodePosition = positions.get(item.id);
      const parents = (item.dependsOn || []).map((id) => positions.get(id)).filter(Boolean);
      if (parents.length) parents.forEach((parent) => connect(parent, nodePosition, 232));
      else connect(root, nodePosition, 164);
    }
    for (const item of items) {
      const pos = positions.get(item.id), button = el("button", `graph-node ${selectedNodeId === item.id ? "selected" : ""}`); button.style.left = `${pos.x}px`; button.style.top = `${pos.y}px`;
      button.setAttribute("aria-label", `${item.subject}，${statusLabel(item.status)}`);
      button.append(el("span", "node-title", item.subject), el("span", "node-summary", item.conclusion), el("span", `status ${statusKind(item.status)}`, statusLabel(item.status)));
      button.addEventListener("click", () => { selectedNodeId = item.id; renderGraph(allItems); renderAssessment(item); }); stage.append(button);
    }
    graph.append(stage);
    renderAssessment(selected);
  }

  function renderAssessment(item) {
    const box = $("assessment-detail"); box.className = "assessment-content"; box.replaceChildren();
    const title = el("div", "detail-title"); title.append(el("h3", "", item.subject), el("span", `status ${statusKind(item.status)}`, statusLabel(item.status))); box.append(title);
    section(box, "结论", item.conclusion); section(box, "依据", item.basis); section(box, "不确定性", item.uncertainty || "无");
    renderRelatedApis(box, item, current?.state?.apis || []);
    const refs = el("div", "evidence-list"); for (const ref of item.evidenceRefs || []) refs.append(recordButton(ref));
    const wrap = el("div", "detail-section"); wrap.append(el("h4", "", "证据"), refs); box.append(wrap);
    const attempts = el("div", "detail-section"); attempts.append(el("h4", "", `尝试 ${(item.attempts || []).length}`));
    for (const attempt of item.attempts || []) { const row = el("div", "attempt"); row.append(el("strong", "", attempt.action), el("p", "", attempt.result)); const refs = el("div", "evidence-list"); for (const ref of attempt.evidenceRefs || []) refs.append(recordButton(ref)); row.append(refs); attempts.append(row); }
    if (!(item.attempts || []).length) attempts.append(el("p", "muted", "尚无实际尝试。")); box.append(attempts);
  }

  function renderRelatedApis(parent, assessment, apis) {
    const ids = new Set(assessment.apiIds || []);
    let related = apis.filter((api) => ids.has(api.id));
    if (!related.length && assessment.api) related = apis.filter((api) => api.endpoint === assessment.api);
    const wrap = el("div", "detail-section related-apis");
    wrap.append(el("h4", "", `关联 API ${related.length}`));
    if (!related.length) {
      wrap.append(el("p", "muted", "当前节点还没有关联 API。")); parent.append(wrap); return;
    }
    for (const api of related) {
      const card = el("article", "related-api"), head = el("div", "related-api-head");
      const endpoint = String(api.endpoint), separator = endpoint.indexOf(" ");
      const method = separator > 0 ? endpoint.slice(0, separator) : "API";
      const path = separator > 0 ? endpoint.slice(separator + 1) : endpoint;
      head.append(el("span", "method", method), el("strong", "", path), badge(`${(api.tests || []).length} 测试`, (api.tests || []).length ? "ok" : "warn"));
      card.append(head, el("p", "", api.purpose));
      const params = api.parameters || [];
      if (params.length) {
        const tags = el("div", "tags"); params.forEach((param) => tags.append(el("span", "tag", param.name))); card.append(tags);
      }
      if (!(api.tests || []).length) card.append(el("p", "related-api-empty", "已发现，尚未进行测试。"));
      for (const test of api.tests || []) {
        const testRow = el("div", "related-api-test");
        testRow.append(el("strong", "", test.action), el("p", "", test.result));
        const refs = el("div", "evidence-list"); for (const id of test.record_ids || []) refs.append(recordButton(id));
        testRow.append(refs); card.append(testRow);
      }
      wrap.append(card);
    }
    parent.append(wrap);
  }

  function section(parent, title, value) { const node = el("div", "detail-section"); node.append(el("h4", "", title), el("p", "", value)); parent.append(node); }

  function recordButton(ref) {
    const id = typeof ref === "number" ? ref : Number(String(ref).replace("record:", ""));
    const button = el("button", "evidence", `record:${id}`); button.type = "button"; button.addEventListener("click", () => loadRecord(id)); return button;
  }

  function renderApis(apis) {
    const q = apiQuery.toLowerCase(), filtered = apis.filter((api) => {
      const tested = (api.tests || []).length > 0;
      if (apiMode === "tested" && !tested) return false; if (apiMode === "untested" && tested) return false;
      return !q || [api.endpoint, api.purpose, ...(api.parameters || []).map((p) => `${p.name} ${p.description || ""}`)].join(" ").toLowerCase().includes(q);
    });
    $("api-count").textContent = `${filtered.length}/${apis.length} 个端点`;
    const target = $("api-list"); target.replaceChildren();
    if (!filtered.length) { target.append(el("p", "placeholder", "当前筛选没有 API。")); return; }
    const columns = el("div", "api-table-head");
    columns.append(el("span", "", "方法"), el("span", "", "端点与用途"), el("span", "", "参数"), el("span", "", "测试"), el("span", "", ""));
    target.append(columns);
    for (const api of filtered) {
      const isOpen = expandedApiId === api.id;
      const row = el("article", `api-row ${isOpen ? "open" : ""}`);
      const endpoint = String(api.endpoint), separator = endpoint.indexOf(" ");
      const method = separator > 0 ? endpoint.slice(0, separator) : "API";
      const path = separator > 0 ? endpoint.slice(separator + 1) : endpoint;
      const summary = el("button", "api-summary"); summary.type = "button";
      summary.setAttribute("aria-expanded", isOpen ? "true" : "false");
      summary.setAttribute("aria-controls", `api-details-${api.id}`);
      const identity = el("span", "api-identity");
      const endpointNode = el("strong", "endpoint", path); endpointNode.title = path;
      identity.append(endpointNode, el("small", "api-purpose", api.purpose));
      const tests = api.tests || [];
      summary.append(
        el("span", "method", method),
        identity,
        el("span", "api-param-count", `${(api.parameters || []).length} 个`),
        badge(`${tests.length} 条`, tests.length ? "ok" : "warn"),
        el("span", "api-chevron", "⌄"),
      );
      summary.addEventListener("click", () => { expandedApiId = isOpen ? null : api.id; renderApis(apis); });
      row.append(summary);
      if (isOpen) {
        const details = el("div", "api-details"); details.id = `api-details-${api.id}`;
        const paramBlock = el("section", "api-detail-block"); paramBlock.append(el("h3", "", "参数"));
        const tags = el("div", "tags");
        for (const param of api.parameters || []) tags.append(el("span", "tag", param.description ? `${param.name} · ${param.description}` : param.name));
        if (!(api.parameters || []).length) tags.append(el("span", "tag", "无已知参数"));
        paramBlock.append(tags); details.append(paramBlock);
        const testBlock = el("section", "api-detail-block"); testBlock.append(el("h3", "", `测试记录 ${tests.length}`));
        if (!tests.length) testBlock.append(el("p", "empty-test", "尚无测试记录"));
        for (const test of tests) {
          const testRow = el("div", "api-test"); testRow.append(el("strong", "", test.action), el("p", "", test.result));
          const refs = el("div", "evidence-list"); for (const id of test.record_ids || []) refs.append(recordButton(id));
          testRow.append(refs); testBlock.append(testRow);
        }
        details.append(testBlock); row.append(details);
      }
      target.append(row);
    }
  }

  async function loadRecord(id) {
    setLoading(true);
    try { const body = await (await request(`/web/record/${encodeURIComponent(selectedSession)}/${id}`)).json(); renderRecord(body.record); }
    catch (error) { showError(error); } finally { setLoading(false); }
  }

  function jsonBlock(title, value) { const box = el("section", "json-section"); box.append(el("h4", "", title)); const pre = el("pre", "text-block", JSON.stringify(value, null, 2)); box.append(pre); return box; }

  function renderRecord(record) {
    const box = $("record-detail"); box.className = "evidence-view"; box.replaceChildren();
    const header = el("div", "record-head"); header.append(el("div", "", `record:${record.id}`), badge(record.tool_name, "ok"), el("span", "muted", record.received_at)); box.append(header);
    const grid = el("div", "json-grid"); grid.append(jsonBlock("工具输入", record.tool_input), jsonBlock("工具结果", record.tool_response)); box.append(grid, jsonBlock("元数据", record.metadata));
  }

  async function loadObserverLogs() {
    const id = current?.observation?.id || current?.latest_run?.id; if (!id) return;
    setLoading(true);
    try {
      const body = await (await request(`/web/observation/${encodeURIComponent(selectedSession)}/${id}/logs`)).json();
      const box = $("record-detail"); box.className = "evidence-view"; box.replaceChildren();
      body.logs.forEach((log, index) => { const card = el("section", "log-card"); const head = el("div", "record-head"); head.append(el("strong", "", `${index + 1}. ${log.op || "event"}`), badge(log.ok === false ? "failed" : "recorded", log.ok === false ? "off" : "ok")); card.append(head, jsonBlock("内容", log)); box.append(card); });
      if (!body.logs.length) box.append(el("p", "placeholder", "当前观察没有工具日志。"));
    } catch (error) { showError(error); } finally { setLoading(false); }
  }

  async function loadReport() {
    const query = selectedObservation ? `?observation_id=${selectedObservation}` : ""; setLoading(true);
    try { $("report-text").textContent = await (await request(`/web/report/${encodeURIComponent(selectedSession)}${query}`)).text(); $("report-dialog").showModal(); }
    catch (error) { showError(error); } finally { setLoading(false); }
  }

  function showError(error) { $("record-detail").className = "evidence-view error"; $("record-detail").textContent = `错误：${error.message}`; toast(error.message, true); }
  let toastTimer; function toast(message, bad = false) { const node = $("toast"); node.textContent = message; node.className = `toast show ${bad ? "bad" : ""}`; clearTimeout(toastTimer); toastTimer = setTimeout(() => node.className = "toast", 2200); }

  $("project-select").addEventListener("change", () => { resetFilters(); loadProject(); });
  $("version-select").addEventListener("change", (event) => loadProject(event.target.value || null));
  $("refresh").addEventListener("click", refreshProjects);
  $("report").addEventListener("click", loadReport);
  $("observer-logs").addEventListener("click", loadObserverLogs);
  $("api-search").addEventListener("input", (event) => { apiQuery = event.target.value; renderApis(current?.state?.apis || []); });
  $("api-filter").addEventListener("change", (event) => { apiMode = event.target.value; renderApis(current?.state?.apis || []); });
  $("copy-report").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText($("report-text").textContent); toast("报告已复制"); }
    catch (_) { toast("浏览器未允许复制，请手工选择文本", true); }
  });
  $("close-report").addEventListener("click", () => $("report-dialog").close());
  refreshProjects();
})();
