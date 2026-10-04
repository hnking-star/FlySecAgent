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
  let projects = [];
  let view = "projects";
  let detailGeneration = 0;
  let projectsGeneration = 0;
  let evidenceGeneration = 0;
  let reportGeneration = 0;
  let pendingRequests = 0;
  let connectionError = false;
  let selectedTab = "graph";
  let graphZoom = 1;
  let graphFit = false;
  let graphDimensions = null;
  let graphNormalView = null;
  const $ = (id) => document.getElementById(id);
  const STATUS_LABELS = {
    published: "已发布", unchanged: "无记忆变化", running: "整理中", failed: "整理失败", cancelled: "已取消",
    observation: "观察记录", hypothesis: "解释 / 假设", legacy_summary: "历史摘要 · 未重新核验",
    completed: "已执行", error: "执行报错", denied: "调用被拒", interrupted: "执行中断", unknown: "执行状态未确认",
    supports: "支持本次假设", contradicts: "不支持本次假设", inconclusive: "结果不可判定", not_evaluated: "未评价目标"
  };

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function setLoading(loading) {
    pendingRequests = Math.max(0, pendingRequests + (loading ? 1 : -1));
    $("connection").classList.toggle("loading", pendingRequests > 0);
    $("connection").classList.toggle("error", pendingRequests === 0 && connectionError);
    $("connection").lastChild.textContent = pendingRequests > 0 ? " 正在同步" : connectionError ? " 请求失败" : " 已连接";
  }

  async function request(path, options = {}) {
    const response = await fetch(path, options);
    if (!response.ok) {
      let body = {};
      try { body = await response.json(); } catch (_) { /* ignored */ }
      throw new Error(body.message || body.detail?.message || `HTTP ${response.status}`);
    }
    connectionError = false;
    return response;
  }

  function statusLabel(status) { return STATUS_LABELS[status] || status || "等待整理"; }

  function statusKind(status) {
    return Object.prototype.hasOwnProperty.call(STATUS_LABELS, status) ? status : "off";
  }

  function projectName(project) {
    if (project.name) return project.name;
    try { return new URL(project.target).host.replace(/^www\./, "") || project.target || "未命名项目"; }
    catch (_) { return project.target || "未命名项目"; }
  }

  function projectState(project) {
    if (!project.observation_enabled) return ["closed", "观察已关闭", "off"];
    if ((project.curator_paused ?? project.observer_paused)) return ["paused", "记忆整理器已暂停", "warn"];
    return ["enabled", "观察已开启", "ok"];
  }

  function projectTimestamp(project) { return project.published_at || project.created_at || ""; }

  function timestampValue(value) {
    const date = Date.parse(value || "");
    return Number.isNaN(date) ? 0 : date;
  }

  function formatDate(value) {
    if (!value) return "尚无记录";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
  }

  function renderProjects() {
    const select = $("project-select"); select.replaceChildren();
    if (!projects.length) {
      select.append(el("option", "", "没有项目"));
      select.disabled = true;
    } else {
      select.disabled = false;
      for (const project of projects) {
        const option = el("option", "", `${projectName(project)} · ${project.session_id.slice(0, 8)}`);
        option.value = project.session_id; select.append(option);
      }
      select.value = selectedSession;
    }
    $("sidebar-project-count").textContent = String(projects.length);
    const stats = [
      ["已加载项目", projects.length, "当前列表 · 最多 200 个"],
      ["观察已开启", projects.filter((p) => p.observation_enabled).length, "采集开启，不代表正在测试"],
      ["已有黑板", projects.filter((p) => p.current_observation_id != null).length, "已有成功发布快照"],
      ["待首次发布", projects.filter((p) => p.current_observation_id == null).length, "尚无已发布黑板"],
    ];
    const statBox = $("project-stats"); statBox.replaceChildren();
    for (const [label, value, note] of stats) {
      const stat = el("div", "portfolio-stat");
      stat.append(el("span", "portfolio-stat-label", label), el("strong", "portfolio-stat-value", value), el("small", "portfolio-stat-note", note));
      statBox.append(stat);
    }
    renderProjectList(); renderRecentProjects();
  }

  function renderProjectList() {
    const query = $("project-search").value.trim().toLowerCase(), status = $("project-status").value, sort = $("project-sort").value;
    const filtered = projects.filter((project) => {
      if (status === "unpublished" && project.current_observation_id != null) return false;
      if (status !== "all" && status !== "unpublished" && projectState(project)[0] !== status) return false;
      return !query || [project.name, project.target, project.session_id, project.objective].join(" ").toLowerCase().includes(query);
    });
    filtered.sort((a, b) => {
      if (sort === "target") return String(a.target).localeCompare(String(b.target), "zh-CN") || String(a.session_id).localeCompare(String(b.session_id));
      const difference = timestampValue(b.created_at) - timestampValue(a.created_at);
      return (sort === "oldest" ? -difference : difference) || String(a.session_id).localeCompare(String(b.session_id));
    });
    $("project-results").textContent = query || status !== "all" ? `${filtered.length} / ${projects.length} 个项目` : `${projects.length} 个项目`;
    const list = $("project-list"); list.replaceChildren();
    const empty = $("project-list-empty"); empty.classList.toggle("hidden", filtered.length > 0);
    empty.textContent = projects.length ? "没有匹配的项目，请调整搜索或筛选条件。" : "还没有项目。主 Agent 启用观察并创建会话后，项目会显示在这里。";
    for (const project of filtered) {
      const button = el("button", "project-row"); button.type = "button";
      button.setAttribute("aria-label", `查看项目 ${projectName(project)}`);
      const identity = el("span", "project-identity"), copy = el("span", "project-copy");
      const name = el("strong", "project-name", projectName(project)); name.title = projectName(project);
      const subtitle = projectName(project) === project.target ? project.objective : project.target;
      const target = el("span", "project-target", subtitle); target.title = subtitle;
      copy.append(name, target, el("span", "project-session", `Session ${project.session_id.slice(0, 12)}`));
      identity.append(el("span", "project-icon", projectName(project).slice(0, 1).toUpperCase()), copy);
      const [, stateText, kind] = projectState(project), state = el("span", "project-state");
      state.append(badge(stateText, kind), el("small", "muted", project.agent_activity_known === 0 ? "宿主活动状态待确认" : project.agent_turn_active ? "主 Agent 回合活跃" : "主 Agent 回合已结束"));
      const publication = el("span", "project-publication");
      publication.append(el("strong", "", project.current_observation_id == null ? "未发布" : `版本 #${project.current_observation_id}`), el("small", "muted", project.current_observation_id == null ? "等待首次整理" : "已发布快照"));
      const position = el("span", "project-position");
      position.append(el("strong", "", `#${project.processed_record_id || 0}`), el("small", "muted", "已处理记录位置"));
      const updated = el("span", "project-updated");
      updated.append(el("strong", "", formatDate(projectTimestamp(project))), el("small", "muted", project.published_at ? "最近发布" : "创建时间"));
      updated.title = projectTimestamp(project);
      button.append(identity, state, publication, position, updated, el("span", "project-arrow", "↗"));
      button.addEventListener("click", () => openProject(project.session_id)); list.append(button);
    }
  }

  function renderRecentProjects() {
    const box = $("recent-projects"); box.replaceChildren();
    const recent = [...projects].sort((a, b) => timestampValue(projectTimestamp(b)) - timestampValue(projectTimestamp(a))).slice(0, 5);
    for (const project of recent) {
      const button = el("button", `recent-project ${view === "detail" && selectedSession === project.session_id ? "active" : ""}`); button.type = "button";
      button.append(el("span", "recent-project-name", projectName(project)), el("small", "recent-project-meta", `${project.session_id.slice(0, 8)} · ${project.current_observation_id == null ? "待发布" : "已发布"}`));
      button.title = `${projectName(project)} · ${project.target}`;
      button.addEventListener("click", () => openProject(project.session_id)); box.append(button);
    }
    if (!recent.length) box.append(el("p", "sidebar-empty", "暂无项目"));
  }

  function setView(next) {
    view = next;
    $("projects-view").classList.toggle("hidden", next !== "projects");
    $("project-detail-view").classList.toggle("hidden", next !== "detail");
    $("detail-toolbar").classList.toggle("hidden", next !== "detail");
    $("breadcrumb-detail").classList.toggle("hidden", next !== "detail");
    $("nav-projects").classList.toggle("active", next === "projects");
    $("nav-projects").setAttribute("aria-current", next === "projects" ? "page" : "false");
    renderRecentProjects();
  }

  function updateUrl(session, push) {
    const url = new URL(location.href);
    if (session) url.searchParams.set("session", session); else url.searchParams.delete("session");
    if (url.href !== location.href) history[push ? "pushState" : "replaceState"](null, "", `${url.pathname}${url.search}${url.hash}`);
  }

  function clearDetail() {
    graphNormalView = null; exitGraphFullscreen(); graphZoom = 1; graphFit = false; graphDimensions = null;
    current = null; selectedObservation = null; evidenceGeneration += 1; reportGeneration += 1;
    resetFilters();
    $("overview").replaceChildren(); $("graph").replaceChildren(); $("api-list").replaceChildren();
    $("map-text").textContent = ""; $("report-text").textContent = "";
    $("topic-detail").className = "placeholder"; $("topic-detail").textContent = "点击主题查看观察、测试、未确认问题与证据。";
    $("record-detail").className = "evidence-view placeholder"; $("record-detail").textContent = "点击 record 证据查看原始输入与结果。";
    $("version-select").replaceChildren(el("option", "", "当前发布版本")); $("version-select").disabled = true;
    $("report").disabled = true; $("curator-logs").disabled = true;
    if ($("report-dialog").open) $("report-dialog").close();
  }

  function openProjects(push = true) {
    const leavingDetail = view !== "projects";
    detailGeneration += 1; selectedSession = ""; clearDetail(); setView("projects"); updateUrl("", push);
    setDetailTab("graph");
    if (leavingDetail) window.scrollTo(0, 0);
    $("sidebar-detail").textContent = "选择项目，查看探索图与 API 台账。";
  }

  async function openProject(session, push = true, observationId = null) {
    const enteringDetail = view !== "detail" || session !== selectedSession;
    if (session !== selectedSession) setDetailTab("graph");
    selectedSession = session;
    $("project-select").value = session;
    setView("detail"); updateUrl(session, push);
    if (enteringDetail) window.scrollTo(0, 0);
    await loadProject(observationId);
  }

  async function syncRoute() {
    const session = new URLSearchParams(location.search).get("session");
    if (session) await openProject(session, false); else openProjects(false);
  }

  async function refreshProjects() {
    const generation = ++projectsGeneration;
    setLoading(true);
    try {
      const body = await (await request("/web/projects")).json();
      if (generation !== projectsGeneration) return;
      projects = body.projects || []; renderProjects();
      const session = new URLSearchParams(location.search).get("session");
      if (session) await openProject(session, false, session === selectedSession ? selectedObservation : null);
      else openProjects(false);
    } catch (error) {
      if (generation !== projectsGeneration) return;
      if (!projects.length && view === "projects") {
        $("project-list-empty").classList.remove("hidden"); $("project-list-empty").textContent = `无法加载项目：${error.message}。请确认本地服务可用后刷新。`;
      } else if (!current && view === "detail") {
        showEmpty("无法同步项目", `${error.message}。请确认本地服务可用后刷新，或返回项目列表。`);
      }
      showError(error);
    }
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
    if (!selectedSession || view !== "detail") return;
    const session = selectedSession, generation = ++detailGeneration;
    clearDetail();
    selectedObservation = observationId;
    const project = projects.find((p) => p.session_id === session);
    $("breadcrumb-detail").textContent = project ? projectName(project) : "项目详情";
    $("sidebar-detail").textContent = "正在加载项目详情…";
    showEmpty("正在加载项目", "正在读取该会话的已发布黑板与历史版本。");
    setLoading(true);
    const query = observationId ? `?observation_id=${encodeURIComponent(observationId)}` : "";
    try {
      const data = await (await request(`/web/project/${encodeURIComponent(session)}${query}`)).json();
      if (generation !== detailGeneration || session !== selectedSession || view !== "detail") return;
      if (data.project?.session_id !== session) throw new Error("返回的项目身份与当前会话不一致");
      current = data;
      renderProject(current);
    } catch (error) {
      if (generation !== detailGeneration || session !== selectedSession || view !== "detail") return;
      current = null; $("overview").replaceChildren();
      showEmpty("无法加载项目", `${error.message}。请刷新重试或返回项目列表。`);
      $("sidebar-detail").textContent = "项目详情加载失败";
      showError(error);
    }
    finally { setLoading(false); }
  }

  function renderProject(data) {
    $("breadcrumb-detail").textContent = projectName(data.project);
    $("sidebar-detail").textContent = `${projectState(data.project)[1]} · ${data.observation ? `黑板 #${data.observation.id}` : "尚未发布黑板"}`;
    $("report").disabled = false;
    $("curator-logs").disabled = !(data.observation?.id || data.latest_run?.id);
    if (data.state && !data.view) { showEmpty("后端协议尚未更新", "当前页面需要 memory v2 服务，请完成后端重启后刷新。"); return; }
    renderVersions(data.versions, data.observation?.id || null);
    renderOverview(data);
    if (!data.state) {
      showEmpty("尚未生成黑板", "项目已经存在，但 记忆整理器还没有发布有效快照。");
      return;
    }
    $("empty").classList.add("hidden"); $("content").classList.remove("hidden");
    selectedNodeId = null;
    renderGraphFilters(data.view.nodes);
    renderGraph(data.view.nodes);
    renderApis(data.view.apis);
    $("map-text").textContent = data.memory_digest || "（当前版本没有记忆摘要）";
    $("record-detail").className = "evidence-view placeholder";
    $("record-detail").textContent = "点击 record 证据查看原始输入与结果。";
  }

  function renderVersions(versions, activeId) {
    const select = $("version-select"); select.replaceChildren();
    select.disabled = !versions.length;
    const latest = el("option", "", "当前发布版本"); latest.value = ""; select.append(latest);
    for (const version of versions) {
      const label = `#${version.id} · ${version.finished_at || version.started_at} · ${(version.revision || "无 revision").slice(0, 12)}`;
      const option = el("option", "", label); option.value = String(version.id);
      if (String(version.id) === String(activeId) && selectedObservation) option.selected = true;
      select.append(option);
    }
  }

  function renderOverview(data) {
    const p = data.project, state = data.state || {}, latest = data.latest_run, counts = data.view?.counts || {};
    const target = $("overview"); target.replaceChildren();
    const summary = el("section", "project-summary");
    const copy = el("div", "project-copy");
    copy.append(el("p", "eyebrow", "PROJECT OVERVIEW"), el("h2", "", projectName(p)), el("p", "project-target", p.target), el("p", "project-objective", p.objective), el("p", "project-session", `Session ${p.session_id}`));
    const badges = el("div", "project-badges");
    const [, label, kind] = projectState(p);
    badges.append(badge(label, kind));
    badges.append(badge(p.agent_activity_known === 0 ? "宿主活动状态待确认" : p.agent_turn_active ? "主 Agent 回合活跃" : "主 Agent 回合已结束", p.agent_turn_active ? "ok" : "off"));
    badges.append(badge(`最近整理：${statusLabel(latest?.status)}`, statusKind(latest?.status)));
    summary.append(copy, badges); target.append(summary);
    const metrics = [
      ["证据记忆", counts.topics || 0, `${counts.observations || 0} 观察 · ${counts.hypotheses || 0} 解释 · ${counts.legacy_notes || 0} 历史摘要`],
      ["API / 测试", (data.view?.apis || []).length, `${counts.tests || 0} 测试记录 · ${counts.open_questions || 0} 未确认问题`],
      ["快照截至", data.observation ? `#${data.observation.end_record_id}` : "—", data.observation ? `项目已处理至 #${p.processed_record_id}` : "尚未发布快照"],
      ["版本", (state.revision || "—").slice(0, 8), data.observation ? `observation #${data.observation.id}` : "尚未发布"],
    ];
    const grid = el("div", "metrics-grid");
    for (const [label, value, note] of metrics) { const card = el("div", "stat"); card.append(el("span", "", label), el("strong", "", value), el("small", "", note)); grid.append(card); }
    target.append(grid);
  }

  function badge(text, kind) { return el("span", `badge ${kind}`, text); }

  function matchesGraph(item, filter) {
    return filter === "all" || (filter === "observations" && item.fact_count > 0) ||
      (filter === "issues" && item.execution_issues > 0) || (filter === "questions" && item.open_count > 0) ||
      (filter === "history" && item.legacy_count > 0);
  }

  function renderGraphFilters(items) {
    const target = $("graph-filters"); target.replaceChildren();
    const options = [["all", "全部"], ["observations", "有观察"], ["issues", "执行问题"], ["questions", "未确认"], ["history", "历史摘要"]];
    for (const [value, label] of options) {
      const count = items.filter(item => matchesGraph(item, value)).length;
      const button = el("button", `filter ${graphFilter === value ? "active" : ""}`, `${label} ${count}`); button.type = "button";
      button.setAttribute("aria-pressed", String(graphFilter === value));
      button.addEventListener("click", () => { graphFilter = value; renderGraphFilters(items); renderGraph(items); }); target.append(button);
    }
  }

  function depths(items) {
    const byId = new Map(items.map((item) => [item.id, item])), memo = new Map();
    function visit(id, visiting = new Set()) {
      if (memo.has(id)) return memo.get(id); if (visiting.has(id)) return 0; visiting.add(id);
      const parents = (byId.get(id)?.parents || []).filter((parent) => byId.has(parent));
      const value = parents.length ? 1 + Math.max(...parents.map((parent) => visit(parent, visiting))) : 0;
      visiting.delete(id); memo.set(id, value); return value;
    }
    for (const item of items) visit(item.id); return memo;
  }

  function renderGraph(allItems) {
    const items = graphFilter === "all" ? allItems : allItems.filter((item) => matchesGraph(item, graphFilter));
    const graph = $("graph"); graph.replaceChildren();
    if (!items.length) {
      graphDimensions = null; $("graph-zoom-reset").textContent = "100%";
      $("graph-count").textContent = `0/${allItems.length} 个主题`; graph.style.height = "260px";
      graph.append(el("div", "graph-empty", "当前筛选没有主题。"));
      selectedNodeId = null; $("topic-detail").className = "placeholder"; $("topic-detail").textContent = "当前筛选没有可查看的主题。";
      return;
    }
    const depth = depths(items), maxDepth = Math.max(...depth.values()), positions = new Map();
    const levels = new Map(); let maxRows = 1;
    for (const item of items) { const d = depth.get(item.id) || 0; if (!levels.has(d)) levels.set(d, []); levels.get(d).push(item); }
    for (const [d, rows] of levels) {
      maxRows = Math.max(maxRows, rows.length);
      rows.forEach((item, i) => positions.set(item.id, { x: 236 + d * 280, y: 56 + i * 126 }));
    }
    const width = Math.max(760, 236 + maxDepth * 280 + 266);
    const height = Math.max(320, maxRows * 126 + 80);
    graphDimensions = { width, height };
    const root = { x: 28, y: 56 + ((maxRows - 1) * 126) / 2 };
    graph.style.height = `${Math.min(height, 650)}px`;
    $("graph-count").textContent = `${items.length}/${allItems.length} 个主题 · ${maxDepth + 1} 层`;
    const stage = el("div", "graph-stage"); stage.style.width = `${width}px`; stage.style.height = `${height}px`;
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg"); svg.setAttribute("width", width); svg.setAttribute("height", height); stage.append(svg);
    const rootLabel = el("span", "graph-layer-label", "测试目标"); rootLabel.style.left = `${root.x}px`; stage.append(rootLabel);
    for (let d = 0; d <= maxDepth; d += 1) {
      const label = el("span", "graph-layer-label", d === 0 ? "测试主题" : `观察关联 ${d + 1}`);
      label.style.left = `${236 + d * 280}px`; stage.append(label);
    }
    const rootNode = el("div", "graph-root"); rootNode.style.left = `${root.x}px`; rootNode.style.top = `${root.y}px`;
    rootNode.append(el("span", "root-kicker", "TEST TARGET"), el("strong", "", current?.project?.target || "当前测试目标"), el("span", "root-note", `${items.length} 个主题`));
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
      const parents = (item.parents || []).map((id) => positions.get(id)).filter(Boolean);
      if (parents.length) parents.forEach((parent) => connect(parent, nodePosition, 232));
      else connect(root, nodePosition, 164);
    }
    for (const item of items) {
      const pos = positions.get(item.id), button = el("button", `graph-node ${selectedNodeId === item.id ? "selected" : ""}`); button.style.left = `${pos.x}px`; button.style.top = `${pos.y}px`;
      button.setAttribute("aria-label", `${item.title}，观察 ${item.fact_count}，测试 ${item.test_count}`);
      button.title = `${item.title}\n${item.summary}`;
      button.append(el("span", "node-title", item.title), el("span", "node-summary", item.summary), el("span", "status topic", `观察 ${item.fact_count} · 测试 ${item.test_count} · 问题 ${item.open_count}`));
      button.addEventListener("click", () => { selectedNodeId = item.id; renderGraph(allItems); renderTopic(item); }); stage.append(button);
    }
    const canvas = el("div", "graph-canvas"); canvas.append(stage); graph.append(canvas);
    updateGraphZoom();
    renderTopic(selected);
  }

  function updateGraphZoom() {
    const graph = $("graph"), canvas = graph.querySelector(".graph-canvas"), stage = graph.querySelector(".graph-stage");
    if (!canvas || !stage || !graphDimensions) return;
    if (graphFit) {
      if (!graph.clientWidth || !graph.clientHeight) return;
      graphZoom = Math.min(1, Math.max(1, graph.clientWidth - 24) / graphDimensions.width, Math.max(1, graph.clientHeight - 24) / graphDimensions.height);
    }
    canvas.style.width = `${Math.ceil(graphDimensions.width * graphZoom)}px`;
    canvas.style.height = `${Math.ceil(graphDimensions.height * graphZoom)}px`;
    stage.style.transform = `scale(${graphZoom})`;
    $("graph-zoom-reset").textContent = `${Math.round(graphZoom * 100)}%`;
    if (graphFit) { graph.scrollLeft = 0; graph.scrollTop = 0; }
  }

  function setGraphZoom(zoom) {
    graphFit = false; graphZoom = Math.max(.05, Math.min(2, zoom)); updateGraphZoom();
  }

  function fitGraph() { graphFit = true; updateGraphZoom(); }

  function syncGraphFullscreen() {
    const panel = $("graph-panel"), expanded = panel.classList.contains("graph-fullscreen-overlay");
    panel.classList.toggle("graph-expanded", expanded);
    $("graph-fullscreen").textContent = expanded ? "退出全屏 ↙" : "全屏 ↗";
    $("graph-fullscreen").setAttribute("aria-label", expanded ? "退出探索图全屏" : "全屏查看探索图");
    $("graph-fullscreen").setAttribute("aria-pressed", String(expanded));
    if (expanded) {
      graphFit = true;
    } else if (graphNormalView) {
      const saved = graphNormalView; graphNormalView = null;
      graphZoom = saved.zoom; graphFit = saved.fit;
      requestAnimationFrame(() => {
        updateGraphZoom(); $("graph").scrollLeft = saved.left; $("graph").scrollTop = saved.top;
      });
      $("graph-fullscreen").focus({ preventScroll: true });
    }
    requestAnimationFrame(updateGraphZoom);
  }

  function exitGraphFullscreen() {
    const panel = $("graph-panel");
    if (panel.classList.contains("graph-fullscreen-overlay")) {
      panel.classList.remove("graph-fullscreen-overlay"); document.body.classList.remove("graph-overlay-open"); syncGraphFullscreen();
    }
  }

  function toggleGraphFullscreen() {
    const panel = $("graph-panel"), graph = $("graph");
    if (panel.classList.contains("graph-expanded")) { exitGraphFullscreen(); return; }
    graphNormalView = { zoom: graphZoom, fit: graphFit, left: graph.scrollLeft, top: graph.scrollTop };
    // Page-level expansion is deterministic in desktop embedded browsers too;
    // native fullscreen may resolve without expanding the requested element.
    panel.classList.add("graph-fullscreen-overlay"); document.body.classList.add("graph-overlay-open"); syncGraphFullscreen();
  }

  function evidenceButtons(parent, ids) {
    const refs = el("div", "evidence-list"); (ids || []).forEach(id => refs.append(recordButton(id))); parent.append(refs);
  }

  function testCard(test) {
    const row = el("div", "api-test");
    row.append(el("strong", "", test.action), badge(statusLabel(test.execution), ["error", "denied", "interrupted"].includes(test.execution) ? "warn" : "off"),
               el("p", "", test.result), el("p", "test-scope", `本次结果：${statusLabel(test.outcome)} · 条件：${test.scope || "仅限所引证据"}`));
    evidenceButtons(row, test.evidence_ids || test.record_ids); return row;
  }

  function renderTopic(item) {
    const box = $("topic-detail"); box.className = "topic-content"; box.replaceChildren();
    const title = el("div", "detail-title"); title.append(el("h3", "", item.title), badge(`${item.test_count} 测试`, "off")); box.append(title);
    if (item.summary) section(box, "主题摘要（不代表全面测试）", item.summary);
    const facts = el("div", "detail-section"); facts.append(el("h4", "", "观察与解释"));
    for (const fact of item.facts || []) {
      const row = el("article", "memory-fact"); row.append(badge(statusLabel(fact.kind), fact.kind === "observation" ? "ok" : "off"), el("p", "", fact.statement));
      row.append(el("p", "test-scope", `范围：${fact.scope || "仅限所引证据条件"}`)); evidenceButtons(row, fact.evidence_ids); facts.append(row);
    }
    if (!(item.facts || []).length) facts.append(el("p", "muted", "尚无已记录观察。")); box.append(facts);
    const tests = el("div", "detail-section"); tests.append(el("h4", "", `测试与执行问题 ${item.test_count}`));
    (item.tests || []).forEach(test => tests.append(testCard(test))); box.append(tests);
    const questions = el("div", "detail-section"); questions.append(el("h4", "", `未确认问题 ${item.open_count}`));
    for (const question of item.questions || []) { const row = el("article", "memory-question"); row.append(el("p", "", question.question)); evidenceButtons(row, question.evidence_ids); questions.append(row); }
    if (!(item.questions || []).length) questions.append(el("p", "muted", "没有已记录问题，不表示测试已全面完成。")); box.append(questions);
    if ((item.origin_facts || []).length) {
      const origins = el("div", "detail-section"); origins.append(el("h4", "", "关联依据"));
      for (const fact of item.origin_facts) { origins.append(el("p", "", `${statusLabel(fact.kind)}：${fact.statement}`)); evidenceButtons(origins, fact.evidence_ids); } box.append(origins);
    }
    renderRelatedApis(box, item, current?.view?.apis || []);
  }

  function renderRelatedApis(parent, topic, apis) {
    const ids = new Set(topic.api_ids || []);
    let related = apis.filter((api) => ids.has(api.id));
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
      head.append(el("span", "method", method), el("strong", "", path), badge(`${api.completed_tests || 0} 完成 / ${(api.tests || []).length} 记录`, api.completed_tests ? "ok" : "off"));
      card.append(head, el("p", "", api.purpose));
      const params = api.parameters || [];
      if (params.length) {
        const tags = el("div", "tags"); params.forEach((param) => tags.append(el("span", "tag", param.name))); card.append(tags);
      }
      if (!(api.tests || []).length) card.append(el("p", "related-api-empty", "已发现，尚未进行测试。"));
      for (const test of api.tests || []) card.append(testCard(test));
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
      const tested = api.completed_tests > 0;
      if (apiMode === "tested" && !tested) return false; if (apiMode === "untested" && tested) return false;
      return !q || [api.endpoint, api.purpose, ...(api.parameters || []).map((p) => `${p.name} ${p.description || ""}`)].join(" ").toLowerCase().includes(q);
    });
    $("api-count").textContent = `${filtered.length}/${apis.length} 个端点`;
    const target = $("api-list"); target.replaceChildren();
    if (!filtered.length) { target.append(el("p", "placeholder", "当前筛选没有 API。")); return; }
    const columns = el("div", "api-table-head");
    columns.append(el("span", "", "方法"), el("span", "", "端点与用途"), el("span", "", "参数"), el("span", "", "完成/记录"), el("span", "", ""));
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
      const testCount = badge(`${api.completed_tests || 0}/${tests.length}`, api.completed_tests ? "ok" : "off");
      testCount.title = `${api.completed_tests || 0} 次已完成尝试，${tests.length} 条记录；不代表全面覆盖`;
      testCount.setAttribute("aria-label", testCount.title);
      summary.append(
        el("span", "method", method),
        identity,
        el("span", "api-param-count", `${(api.parameters || []).length} 个`),
        testCount,
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
          testBlock.append(testCard(test));
        }
        details.append(testBlock); row.append(details);
      }
      target.append(row);
    }
  }

  async function loadRecord(id) {
    if (!current || view !== "detail") return;
    const session = selectedSession, generation = detailGeneration, evidence = ++evidenceGeneration;
    setDetailTab("evidence");
    $("record-detail").className = "evidence-view placeholder";
    $("record-detail").textContent = `正在读取 record:${id}…`;
    setLoading(true);
    try {
      const body = await (await request(`/web/record/${encodeURIComponent(session)}/${id}`)).json();
      if (view !== "detail" || session !== selectedSession || generation !== detailGeneration || evidence !== evidenceGeneration) return;
      renderRecord(body.record);
    } catch (error) {
      if (view === "detail" && session === selectedSession && generation === detailGeneration && evidence === evidenceGeneration) showError(error);
    } finally { setLoading(false); }
  }

  function jsonBlock(title, value) { const box = el("section", "json-section"); box.append(el("h4", "", title)); const pre = el("pre", "text-block", JSON.stringify(value, null, 2)); box.append(pre); return box; }

  function renderRecord(record) {
    const box = $("record-detail"); box.className = "evidence-view"; box.replaceChildren();
    const header = el("div", "record-head"); header.append(el("div", "", `record:${record.id}`), badge(record.tool_name, "ok"), el("span", "muted", record.received_at)); box.append(header);
    const grid = el("div", "json-grid"); grid.append(jsonBlock("工具输入", record.tool_input), jsonBlock("工具结果", record.tool_response)); box.append(grid, jsonBlock("元数据", record.metadata));
  }

  async function loadCuratorLogs() {
    const id = current?.observation?.id || current?.latest_run?.id; if (!id) return;
    const session = selectedSession, generation = detailGeneration, evidence = ++evidenceGeneration;
    setDetailTab("evidence");
    $("record-detail").className = "evidence-view placeholder";
    $("record-detail").textContent = "正在读取 记忆整理器工具日志…";
    setLoading(true);
    try {
      const body = await (await request(`/web/observation/${encodeURIComponent(session)}/${id}/logs`)).json();
      if (view !== "detail" || session !== selectedSession || generation !== detailGeneration || evidence !== evidenceGeneration) return;
      const box = $("record-detail"); box.className = "evidence-view"; box.replaceChildren();
      body.logs.forEach((log, index) => { const card = el("section", "log-card"); const head = el("div", "record-head"); head.append(el("strong", "", `${index + 1}. ${log.op || "event"}`), badge(log.ok === false ? "failed" : "recorded", log.ok === false ? "off" : "ok")); card.append(head, jsonBlock("内容", log)); box.append(card); });
      if (!body.logs.length) box.append(el("p", "placeholder", "当前观察没有工具日志。"));
    } catch (error) {
      if (view === "detail" && session === selectedSession && generation === detailGeneration && evidence === evidenceGeneration) showError(error);
    } finally { setLoading(false); }
  }

  async function loadReport() {
    if (!current || view !== "detail") return;
    const session = selectedSession, generation = detailGeneration, report = ++reportGeneration;
    const query = selectedObservation ? `?observation_id=${encodeURIComponent(selectedObservation)}` : ""; setLoading(true);
    try {
      const text = await (await request(`/web/report/${encodeURIComponent(session)}${query}`)).text();
      if (view !== "detail" || session !== selectedSession || generation !== detailGeneration || report !== reportGeneration) return;
      $("report-text").textContent = text;
      if (!$("report-dialog").open) $("report-dialog").showModal();
    } catch (error) {
      if (view === "detail" && session === selectedSession && generation === detailGeneration && report === reportGeneration) showError(error);
    } finally { setLoading(false); }
  }

  function setDetailTab(tab, focus = false) {
    selectedTab = tab;
    for (const name of ["graph", "apis", "evidence"]) {
      const button = $(`workspace-tab-${name}`), panel = $(`tab-${name}`), active = name === tab;
      button.classList.toggle("active", active); button.setAttribute("aria-selected", active ? "true" : "false"); button.tabIndex = active ? 0 : -1;
      panel.classList.toggle("hidden", !active);
      if (active && focus) button.focus();
    }
  }

  function showError(error) {
    connectionError = true;
    if (view === "detail") { $("record-detail").className = "evidence-view error"; $("record-detail").textContent = `错误：${error.message}`; }
    toast(error.message, true);
  }
  let toastTimer; function toast(message, bad = false) { const node = $("toast"); node.textContent = message; node.className = `toast show ${bad ? "bad" : ""}`; clearTimeout(toastTimer); toastTimer = setTimeout(() => node.className = "toast", 2200); }

  $("project-select").addEventListener("change", (event) => openProject(event.target.value));
  $("version-select").addEventListener("change", (event) => loadProject(event.target.value || null));
  $("refresh").addEventListener("click", refreshProjects);
  $("graph-fullscreen").addEventListener("click", toggleGraphFullscreen);
  $("graph-fit").addEventListener("click", fitGraph);
  $("graph-zoom-out").addEventListener("click", () => setGraphZoom(graphZoom / 1.2));
  $("graph-zoom-in").addEventListener("click", () => setGraphZoom(graphZoom * 1.2));
  $("graph-zoom-reset").addEventListener("click", () => setGraphZoom(1));
  document.addEventListener("keydown", (event) => {
    const panel = $("graph-panel");
    if (!panel.classList.contains("graph-expanded")) return;
    if (event.key === "Escape" && panel.classList.contains("graph-fullscreen-overlay")) {
      event.preventDefault(); exitGraphFullscreen();
    } else if (event.key === "Tab") {
      const buttons = [...panel.querySelectorAll("button:not(:disabled)")];
      const first = buttons[0], last = buttons[buttons.length - 1];
      if (!panel.contains(document.activeElement)) { event.preventDefault(); (event.shiftKey ? last : first).focus(); }
      else if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  });
  new ResizeObserver(() => { if (graphFit) updateGraphZoom(); }).observe($("graph"));
  $("report").addEventListener("click", loadReport);
  $("curator-logs").addEventListener("click", loadCuratorLogs);
  $("api-search").addEventListener("input", (event) => { apiQuery = event.target.value; renderApis(current?.view?.apis || []); });
  $("api-filter").addEventListener("change", (event) => { apiMode = event.target.value; renderApis(current?.view?.apis || []); });
  $("project-search").addEventListener("input", renderProjectList);
  $("project-status").addEventListener("change", renderProjectList);
  $("project-sort").addEventListener("change", renderProjectList);
  for (const id of ["nav-projects", "breadcrumb-projects", "back-to-projects"]) $(id).addEventListener("click", () => openProjects());
  for (const name of ["graph", "apis", "evidence"]) {
    const button = $(`workspace-tab-${name}`);
    button.addEventListener("click", () => setDetailTab(name));
    button.addEventListener("keydown", (event) => {
      const names = ["graph", "apis", "evidence"], index = names.indexOf(selectedTab);
      const target = { ArrowRight: (index + 1) % 3, ArrowLeft: (index + 2) % 3, Home: 0, End: 2 }[event.key];
      if (target === undefined) return;
      event.preventDefault(); setDetailTab(names[target], true);
    });
  }
  window.addEventListener("popstate", syncRoute);
  $("copy-report").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText($("report-text").textContent); toast("报告已复制"); }
    catch (_) { toast("浏览器未允许复制，请手工选择文本", true); }
  });
  $("close-report").addEventListener("click", () => $("report-dialog").close());
  setView(new URLSearchParams(location.search).get("session") ? "detail" : "projects");
  setDetailTab("graph");
  if (view === "detail") showEmpty("正在加载项目", "正在同步项目列表与当前会话的黑板。");
  refreshProjects();
})();
