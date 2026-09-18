"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const verdictNames = {aligned: "对齐", partial: "部分对齐", misaligned: "不对齐", uncertain: "暂无法判断"};
  let catalog = [];
  let selected = null;
  let selectedCard = null;
  let filter = "pending";
  let query = "";
  let sequence = 0;
  let pendingRequest = null;
  const evidenceCache = new Map();
  const evidenceInflight = new Map();

  const escape = (value) => String(value ?? "")
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;");

  async function json(url, options = {}) {
    const response = await fetch(url, {cache: "no-cache", ...options});
    let data;
    try { data = await response.json(); } catch { throw new Error("服务器返回了无法读取的内容"); }
    if (!response.ok) throw new Error(data.error || `请求失败：${response.status}`);
    return data;
  }

  function visible() {
    return catalog.filter((card) => {
      if (filter === "pending" && (card.verdict || card.source_status !== "local")) return false;
      if (filter === "reviewed" && !card.verdict) return false;
      if (filter === "evidence" && card.source_status === "local") return false;
      if (filter === "stale" && !card.stale) return false;
      const needle = `${card.title} ${card.label} ${card.declaration} ${card.chapter}`.toLowerCase();
      return needle.includes(query);
    });
  }

  function renderList() {
    const rows = visible();
    $("card-list").innerHTML = rows.length ? rows.map((card) => `
      <button class="list-row ${card.id === selected ? "active" : ""} ${card.stale ? "stale" : card.verdict ? "reviewed" : ""}" data-id="${escape(card.id)}">
        <span class="dot"></span><span><b>${escape(card.title)}</b><small>${escape(card.label)} · ${escape(card.declaration)}</small></span>
      </button>`).join("") : `<div class="list-empty">这里暂时没有卡片。可切换筛选条件查看全部对应关系。</div>`;
    const done = catalog.filter((card) => card.verdict).length;
    $("progress-label").textContent = `${done} / ${catalog.length}`;
    $("progress-fill").style.width = catalog.length ? `${100 * done / catalog.length}%` : "0%";
  }

  async function loadCatalog() {
    const data = await json("./api/catalog");
    catalog = data.cards;
    $("revision").textContent = `SOURCE ${data.source_commit.slice(0, 9)} · SNAPSHOT ${data.digest.slice(0, 9)}`;
    $("unlinked").textContent = `${data.unlinked_nodes} 个 Blueprint 节点尚未指定 Lean 对象`;
    renderList();
    return data;
  }

  async function evidence(id) {
    if (evidenceCache.has(id)) return evidenceCache.get(id);
    if (evidenceInflight.has(id)) return evidenceInflight.get(id);
    const promise = json(`./api/evidence?id=${encodeURIComponent(id)}`).then((card) => {
      evidenceCache.delete(id);
      evidenceCache.set(id, card);
      while (evidenceCache.size > 12) evidenceCache.delete(evidenceCache.keys().next().value);
      return card;
    }).finally(() => evidenceInflight.delete(id));
    evidenceInflight.set(id, promise);
    return promise;
  }

  function prefetchNeighbor(id) {
    const rows = visible();
    const current = rows.findIndex((item) => item.id === id);
    const next = rows[current + 1];
    if (next && !evidenceCache.has(next.id)) {
      const schedule = window.requestIdleCallback || ((callback) => setTimeout(callback, 120));
      schedule(() => evidence(next.id).catch(() => {}));
    }
  }

  function displayHistory(card, rows) {
    $("history-count").textContent = `(${rows.length})`;
    $("history").innerHTML = rows.length ? rows.map((row) => `
      <div class="history-item ${row.fingerprint === card.fingerprint ? "" : "stale"}">
        <b>${escape(verdictNames[row.verdict] || row.verdict)}</b><small>${escape(row.reviewer)} · ${escape(new Date(row.created_at).toLocaleString("zh-CN"))}${row.fingerprint === card.fingerprint ? "" : " · 旧版本"}</small>
        ${row.rationale ? `<p>${escape(row.rationale)}</p>` : ""}
      </div>`).join("") : `<span class="muted">还没有审核记录。</span>`;
  }

  function renderCard(card, historyRows) {
    selectedCard = card;
    $("empty").hidden = true;
    $("review-card").hidden = false;
    $("breadcrumb").textContent = `${card.chapter} / ${card.kind.toUpperCase()}`;
    $("card-title").textContent = card.title;
    $("meta").innerHTML = `<span>${escape(card.label)}</span><span>${escape(card.declaration)}</span>`;
    const latest = historyRows[0];
    const current = latest && latest.fingerprint === card.fingerprint ? latest : null;
    const badge = $("status-badge");
    badge.className = `status-badge ${current ? "reviewed" : latest ? "stale" : ""}`;
    badge.textContent = current ? `✓ ${verdictNames[current.verdict]}` : latest ? "⟳ 内容已变更，需重审" : "待人工审核";
    $("statement").innerHTML = window.Stage3Latex?.toHtml(card.statement) || escape(card.statement);
    window.Stage3Latex?.typeset([$("statement")]);
    $("nl-location").textContent = `${card.blueprint_file}:${card.blueprint_line}`;
    $("formal-info").innerHTML = `<strong>${escape(card.declaration)}</strong><p>${
      card.source_status === "local" ? "已定位到 KIP126 中的源码。请人工核对陈述及条件。" :
      card.source_status === "external" ? "这是外部库声明；当前快照没有它的源码。请结合 Mathlib 文档或源码核对。" :
      "尚未在 KIP126 源码中定位到此名称。请先核实映射是否仍然有效。"
    }</p>`;
    $("lean-code").textContent = card.lean?.source || `-- 当前仓库未定位到 ${card.declaration} 的源码`;
    $("lean-location").textContent = card.lean ? `${card.lean.file}:${card.lean.line}${card.lean.truncated ? " · 仅显示前 100 行" : ""}` : "源码未定位 · 不应仅凭名称判定对齐";
    $("dependencies").innerHTML = card.dependencies.length ? card.dependencies.map((value) => `<span class="chip">${escape(value)}</span>`).join("") : "此节点未列出依赖。";
    displayHistory(card, historyRows);
    $("rationale").value = current?.rationale || "";
    document.querySelectorAll('input[name="verdict"]').forEach((input) => { input.checked = input.value === current?.verdict; });
    $("save-message").textContent = "";
    pendingRequest = null;
  }

  async function openCard(id) {
    if (!id) return;
    const turn = ++sequence;
    selected = id;
    history.replaceState(null, "", `#${encodeURIComponent(id)}`);
    renderList();
    $("empty").hidden = false;
    $("review-card").hidden = true;
    $("empty").querySelector("h2").textContent = "正在加载卡片…";
    try {
      const [card, result] = await Promise.all([
        evidence(id), json(`./api/history?id=${encodeURIComponent(id)}`),
      ]);
      if (turn !== sequence) return;
      renderCard(card, result.history);
      prefetchNeighbor(id);
    } catch (error) {
      if (turn !== sequence) return;
      $("empty").querySelector("h2").textContent = "卡片加载失败";
      $("empty").querySelector("p").textContent = error.message;
    }
  }

  function nextCard() {
    const rows = visible();
    const i = rows.findIndex((item) => item.id === selected);
    if (rows[i + 1]) openCard(rows[i + 1].id);
    else if (rows[0] && i < 0) openCard(rows[0].id);
  }

  async function save() {
    if (!selectedCard) return;
    const verdict = document.querySelector('input[name="verdict"]:checked')?.value;
    const rationale = $("rationale").value.trim();
    const reviewer = $("reviewer").value.trim();
    if (!reviewer) { $("save-message").textContent = "请先填写审核人姓名"; $("reviewer").focus(); return; }
    if (!verdict) { $("save-message").textContent = "请选择一个结论"; return; }
    if (verdict !== "aligned" && !rationale) { $("save-message").textContent = "请填写判断依据"; $("rationale").focus(); return; }
    localStorage.setItem("kip126-reviewer", reviewer);
    const requestId = pendingRequest || crypto.randomUUID();
    pendingRequest = requestId;
    $("save").disabled = true;
    $("save-message").textContent = "正在保存…";
    try {
      await json("./api/judgments", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({
        request_id: requestId, card_id: selectedCard.id, fingerprint: selectedCard.fingerprint,
        reviewer, verdict, rationale,
      })});
      pendingRequest = null;
      const id = selectedCard.id;
      await loadCatalog();
      const result = await json(`./api/history?id=${encodeURIComponent(id)}`);
      if (selectedCard?.id === id) renderCard(selectedCard, result.history);
      $("save-message").textContent = "已保存，可继续审核或修改判断";
    } catch (error) {
      $("save-message").textContent = error.message + "。可再次点击安全重试。";
    } finally { $("save").disabled = false; }
  }

  $("card-list").addEventListener("click", (event) => {
    const row = event.target.closest("[data-id]");
    if (row) openCard(row.dataset.id);
  });
  $("filters").addEventListener("click", (event) => {
    const button = event.target.closest("[data-filter]");
    if (!button) return;
    filter = button.dataset.filter;
    $("filters").querySelectorAll("button").forEach((item) => item.classList.toggle("active", item === button));
    renderList();
  });
  $("search").addEventListener("input", (event) => { query = event.target.value.trim().toLowerCase(); renderList(); });
  $("save").addEventListener("click", save);
  $("next").addEventListener("click", nextCard);
  $("rationale").addEventListener("input", () => { pendingRequest = null; });
  $("reviewer").addEventListener("input", () => { pendingRequest = null; });
  $("verdicts").addEventListener("change", () => { pendingRequest = null; });
  document.addEventListener("keydown", (event) => {
    if (event.ctrlKey && event.key === "Enter") { event.preventDefault(); save(); return; }
    if (event.target.matches("input, textarea")) return;
    const rows = visible();
    const i = rows.findIndex((item) => item.id === selected);
    if (event.key.toLowerCase() === "j" && rows[i + 1]) openCard(rows[i + 1].id);
    if (event.key.toLowerCase() === "k" && rows[i - 1]) openCard(rows[i - 1].id);
    if (/^[1-4]$/.test(event.key)) {
      const input = document.querySelectorAll('input[name="verdict"]')[Number(event.key) - 1];
      if (input) { input.checked = true; pendingRequest = null; }
    }
  });

  $("reviewer").value = localStorage.getItem("kip126-reviewer") || "";
  loadCatalog().then(() => {
    const fromHash = decodeURIComponent(location.hash.slice(1));
    const start = catalog.find((item) => item.id === fromHash) || visible()[0] || catalog[0];
    if (start) openCard(start.id);
    else $("empty").querySelector("h2").textContent = "当前快照没有候选对应关系";
  }).catch((error) => {
    $("empty").querySelector("h2").textContent = "审核队列加载失败";
    $("empty").querySelector("p").textContent = error.message;
  });
})();
