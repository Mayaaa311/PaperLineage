const CONFERENCES = ["NeurIPS", "ICLR", "ICML", "CVPR", "ECCV", "ACL", "EMNLP", "KDD"];
const USER_ID = "demo-user";

let currentResults = [];
let currentPaperId = null;
let currentPaperUrl = null;
let activeTraceId = null;
let currentSearchState = null;
let currentPage = 1;
let totalPages = 1;
let currentAllPaperIds = [];
let currentCy = null;
let currentFavorites = [];

const searchForm = document.getElementById("search-form");
const searchModeEl = document.getElementById("search-mode");
const queryLabelTextEl = document.getElementById("query-label-text");
const queryTextEl = document.getElementById("query-text");
const resultsList = document.getElementById("results-list");
const favoritesList = document.getElementById("favorites-list");
const resultsMeta = document.getElementById("results-meta");
const conferenceOptions = document.getElementById("conference-options");
const allConferencesEl = document.getElementById("all-conferences");
const saveSearchBtn = document.getElementById("save-search");
const prevPageBtn = document.getElementById("prev-page");
const nextPageBtn = document.getElementById("next-page");
const pageIndicator = document.getElementById("page-indicator");

const detailEmpty = document.getElementById("detail-empty");
const detailContent = document.getElementById("detail-content");
const detailTitle = document.getElementById("detail-title");
const detailMeta = document.getElementById("detail-meta");
const detailReadBtn = document.getElementById("detail-read-btn");
const detailAbstract = document.getElementById("detail-abstract");
const detailReferences = document.getElementById("detail-references");
const detailInsights = document.getElementById("detail-insights");
const detailLogic = document.getElementById("detail-logic");
const detailEvidence = document.getElementById("detail-evidence");
const detailLimitations = document.getElementById("detail-limitations");
const detailKeyDeps = document.getElementById("detail-key-deps");
const detailDatasetDeps = document.getElementById("detail-dataset-deps");
const traceStatus = document.getElementById("trace-status");

const traceDepthInput = document.getElementById("trace-depth");
const startTraceBtn = document.getElementById("start-trace");

const graphEmpty = document.getElementById("graph-empty");
const graphEl = document.getElementById("graph");
const edgeExplainer = document.getElementById("edge-explainer");
const graphResetBtn = document.getElementById("graph-reset-view");
const visualizeFavoritesLinksBtn = document.getElementById("visualize-favorites-links");
const favoritesGraphModal = document.getElementById("favorites-graph-modal");
const favoritesGraphPicker = document.getElementById("favorites-graph-picker");
const favoritesGraphCancelBtn = document.getElementById("favorites-graph-cancel");
const favoritesGraphOpenBtn = document.getElementById("favorites-graph-open");

function resolveReadableUrl(rawUrl) {
  if (typeof rawUrl !== "string") {
    return null;
  }
  const value = rawUrl.trim();
  if (!value) {
    return null;
  }
  if (value.startsWith("10.")) {
    return `https://doi.org/${value}`;
  }
  try {
    const parsed = new URL(value);
    if (parsed.protocol === "http:" || parsed.protocol === "https:") {
      return parsed.toString();
    }
  } catch (err) {
    return null;
  }
  return null;
}

function createConferenceChips() {
  conferenceOptions.innerHTML = "";
  CONFERENCES.forEach((conf) => {
    const label = document.createElement("label");
    label.className = "chip";
    label.innerHTML = `<input type="checkbox" value="${conf}" /> ${conf}`;
    conferenceOptions.appendChild(label);
  });
  updateConferenceFilterUI();
}

function updateConferenceFilterUI() {
  const allConferences = !!allConferencesEl?.checked;
  const inputs = conferenceOptions.querySelectorAll("input[type='checkbox']");
  inputs.forEach((input) => {
    input.disabled = allConferences;
  });
}

async function api(path, method = "GET", body = null) {
  const opts = { method, headers: {} };
  if (body) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || "Request failed");
  }
  return res.json();
}

function renderPaperCards(container, papers, inFavorites = false) {
  container.innerHTML = "";
  container.classList.remove("empty");
  if (!papers.length) {
    container.classList.add("empty");
    container.textContent = inFavorites ? "No saved papers yet." : "No papers found.";
    return;
  }

  const template = document.getElementById("paper-card-template");
  papers.forEach((paper) => {
    const card = template.content.cloneNode(true);
    const article = card.querySelector(".paper-card");
    const title = card.querySelector(".paper-title");
    const meta = card.querySelector(".paper-meta");
    const snippet = card.querySelector(".paper-snippet");
    const saveBtn = card.querySelector(".save-btn");
    const detailBtn = card.querySelector(".detail-btn");
    const readBtn = card.querySelector(".read-btn");

    title.textContent = paper.title;
    const reviewText = (paper.review_score_avg !== null && paper.review_score_avg !== undefined)
      ? ` • Review: ${Number(paper.review_score_avg).toFixed(2)}${paper.review_count ? ` (n=${paper.review_count})` : ""}`
      : "";
    const decisionText = paper.decision ? ` • Decision: ${paper.decision}` : "";
    meta.textContent = `${paper.venue || "Unknown venue"} • ${paper.year || "Unknown year"} • Citations: ${paper.citation_count || 0}${reviewText}${decisionText}`;
    snippet.textContent = paper.abstract_snippet || "No abstract snippet available.";

    const setSaveButton = (saved) => {
      saveBtn.textContent = saved ? "Saved" : "Save";
      saveBtn.classList.toggle("saved", saved);
      paper.is_favorited = saved;
    };
    setSaveButton(!!paper.is_favorited);

    saveBtn.addEventListener("click", async () => {
      try {
        if (paper.is_favorited) {
          await api(`/api/favorites/${paper.id}?user_id=${encodeURIComponent(USER_ID)}`, "DELETE");
          setSaveButton(false);
        } else {
          await api("/api/favorites", "POST", { user_id: USER_ID, paper_id: paper.id });
          setSaveButton(true);
        }
        await loadFavorites();
      } catch (err) {
        alert(`Favorite action failed: ${err.message}`);
      }
    });

    detailBtn.addEventListener("click", () => {
      loadPaperDetail(paper.id, { fromFavorites: inFavorites });
    });

    const readableUrl = resolveReadableUrl(paper.url);
    const hasUrl = !!readableUrl;
    if (!hasUrl) {
      readBtn.classList.add("disabled");
      readBtn.textContent = "No Link";
      readBtn.disabled = true;
    } else {
      readBtn.addEventListener("click", () => {
        window.open(readableUrl, "_blank", "noopener,noreferrer");
      });
    }

    article.dataset.paperId = paper.id;
    container.appendChild(card);
  });
}

function updatePaginationUI() {
  pageIndicator.textContent = `Page ${currentPage} / ${totalPages}`;
  prevPageBtn.disabled = currentPage <= 1;
  nextPageBtn.disabled = currentPage >= totalPages;
}

function collectSearchFormState() {
  const searchMode = searchModeEl.value;
  const queryText = queryTextEl.value.trim();
  const startYear = parseInt(document.getElementById("start-year").value, 10);
  const endYear = parseInt(document.getElementById("end-year").value, 10);
  const limit = parseInt(document.getElementById("limit").value, 10);
  const allConferences = !!allConferencesEl?.checked;
  const selectedConfs = Array.from(conferenceOptions.querySelectorAll("input:checked")).map((n) => n.value);
  return {
    searchMode,
    queryText,
    startYear,
    endYear,
    limit,
    selectedConfs: allConferences ? [] : selectedConfs,
    allConferences,
  };
}

async function executeSearch(pageToLoad = 1) {
  if (!currentSearchState) {
    return;
  }
  const {
    searchMode,
    queryText,
    startYear,
    endYear,
    limit,
    selectedConfs,
    allConferences,
  } = currentSearchState;
  if (!queryText) {
    alert(searchMode === "paper_name" ? "Paper name is required." : "Topic is required.");
    return;
  }

  resultsMeta.textContent = "Searching...";
  resultsList.classList.remove("empty");
  resultsList.textContent = "Loading papers...";

  try {
    const data = await api("/api/papers/search", "POST", {
      topic: searchMode === "topic" ? queryText : "",
      paper_name: searchMode === "paper_name" ? queryText : null,
      search_mode: searchMode,
      conferences: selectedConfs,
      start_year: Number.isFinite(startYear) ? startYear : null,
      end_year: Number.isFinite(endYear) ? endYear : null,
      user_id: USER_ID,
      page: pageToLoad,
      page_size: 10,
      max_results: Number.isFinite(limit) ? limit : 300,
      use_saved_search: true,
    });
    currentResults = data.papers;
    currentAllPaperIds = Array.isArray(data.all_paper_ids) ? data.all_paper_ids : currentResults.map((x) => x.id);
    currentPage = data.page || pageToLoad;
    totalPages = data.total_pages || 1;
    updatePaginationUI();
    const sourceText = data.source === "saved_search" ? " • loaded from saved search" : "";
    resultsMeta.textContent = `${data.total} papers (showing ${currentResults.length} on this page)${sourceText}`;
    if (!data.total) {
      resultsList.classList.add("empty");
      const confText = allConferences ? " across all conferences" : (selectedConfs.length ? ` in ${selectedConfs.join(", ")}` : "");
      const yearText = Number.isFinite(startYear) || Number.isFinite(endYear)
        ? ` for years ${Number.isFinite(startYear) ? startYear : "?"}-${Number.isFinite(endYear) ? endYear : "?"}`
        : "";
      resultsList.textContent = `No papers found${confText}${yearText}. Try widening year range (for example 2022-2026) or removing conference filter.`;
    } else {
      renderPaperCards(resultsList, currentResults);
    }
  } catch (err) {
    resultsMeta.textContent = "";
    currentAllPaperIds = [];
    resultsList.classList.add("empty");
    resultsList.textContent = `Search failed: ${err.message}`;
    currentPage = 1;
    totalPages = 1;
    updatePaginationUI();
  }
}

async function saveCurrentSearch() {
  if (!currentSearchState) {
    alert("Run a search first.");
    return;
  }
  if (!currentAllPaperIds.length) {
    alert("No search results to save.");
    return;
  }

  const {
    searchMode,
    queryText,
    startYear,
    endYear,
    limit,
    selectedConfs,
  } = currentSearchState;

  try {
    const data = await api("/api/searches/save", "POST", {
      topic: searchMode === "topic" ? queryText : "",
      paper_name: searchMode === "paper_name" ? queryText : null,
      search_mode: searchMode,
      conferences: selectedConfs,
      start_year: Number.isFinite(startYear) ? startYear : null,
      end_year: Number.isFinite(endYear) ? endYear : null,
      user_id: USER_ID,
      max_results: Number.isFinite(limit) ? limit : 300,
      paper_ids: currentAllPaperIds,
    });
    if (data?.success) {
      resultsMeta.textContent = `${resultsMeta.textContent || ""} • saved`;
    } else {
      alert("Failed to save search.");
    }
  } catch (err) {
    alert(`Save search failed: ${err.message}`);
  }
}

async function runSearch(event) {
  event.preventDefault();
  currentSearchState = collectSearchFormState();
  currentPage = 1;
  totalPages = 1;
  updatePaginationUI();
  await executeSearch(1);
}

function updateSearchModeUI() {
  const mode = searchModeEl.value;
  if (mode === "paper_name") {
    queryLabelTextEl.textContent = "Paper name";
    queryTextEl.placeholder = "e.g. An Image is Worth 16x16 Words";
  } else {
    queryLabelTextEl.textContent = "Topic";
    queryTextEl.placeholder = "e.g. diffusion model controllability";
  }
}

async function loadFavorites() {
  try {
    const data = await api(`/api/favorites?user_id=${encodeURIComponent(USER_ID)}`);
    currentFavorites = Array.isArray(data.papers) ? data.papers : [];
    renderPaperCards(favoritesList, currentFavorites, true);
  } catch (err) {
    currentFavorites = [];
    favoritesList.classList.add("empty");
    favoritesList.textContent = `Failed to load favorites: ${err.message}`;
  }
}

function openFavoritesGraphModal() {
  if (!currentFavorites.length) {
    alert("No favorites to visualize yet.");
    return;
  }
  favoritesGraphPicker.innerHTML = "";
  currentFavorites.forEach((paper) => {
    const label = document.createElement("label");
    label.className = "modal-paper-item";
    label.innerHTML = `
      <input type="checkbox" value="${paper.id}" checked />
      <div>
        <p class="modal-paper-title">${paper.title}</p>
        <p class="modal-paper-meta">${paper.venue || "Unknown venue"} • ${paper.year || "Unknown year"} • Citations: ${paper.citation_count || 0}</p>
      </div>
    `;
    favoritesGraphPicker.appendChild(label);
  });
  favoritesGraphModal.classList.remove("hidden");
  favoritesGraphModal.setAttribute("aria-hidden", "false");
}

function closeFavoritesGraphModal() {
  favoritesGraphModal.classList.add("hidden");
  favoritesGraphModal.setAttribute("aria-hidden", "true");
}

function openFavoritesGraphPage() {
  const selected = Array.from(
    favoritesGraphPicker.querySelectorAll("input[type='checkbox']:checked")
  ).map((x) => x.value);
  if (!selected.length) {
    alert("Select at least one paper.");
    return;
  }
  const payload = {
    user_id: USER_ID,
    paper_ids: selected,
    created_at: Date.now(),
  };
  sessionStorage.setItem("favoritesGraphSelection", JSON.stringify(payload));
  window.location.href = "/favorites-links";
}

async function loadCachedTraceForPaper(paperId) {
  try {
    const data = await api(
      `/api/traces/by-paper/latest?paper_id=${encodeURIComponent(paperId)}&user_id=${encodeURIComponent(USER_ID)}`
    );
    if (!data?.found || !data.trace) {
      traceStatus.textContent = "No cached lineage graph yet. Building cache now...";
      const traceDepth = parseInt(traceDepthInput.value, 10);
      const start = await api("/api/traces", "POST", {
        user_id: USER_ID,
        paper_id: paperId,
        trace_depth: Number.isFinite(traceDepth) ? traceDepth : 2,
      });
      activeTraceId = start.trace_id;
      await pollTraceStatus(activeTraceId);
      return;
    }
    const trace = data.trace;
    activeTraceId = trace.trace_id;
    renderGraph(trace);
    traceStatus.textContent = `Loaded cached trace (depth ${trace.trace_depth}). Nodes: ${trace.nodes.length}, edges: ${trace.edges.length}`;
  } catch (err) {
    graphEl.style.display = "none";
    graphEmpty.style.display = "block";
    graphEmpty.textContent = "Failed to load cached lineage graph.";
    traceStatus.textContent = `Cached trace lookup failed: ${err.message}`;
  }
}

async function loadPaperDetail(paperId, opts = {}) {
  const fromFavorites = !!opts.fromFavorites;
  detailEmpty.classList.add("hidden");
  detailContent.classList.remove("hidden");
  detailTitle.textContent = "Loading...";
  detailMeta.textContent = "";
  setDetailReadState(null);
  detailAbstract.textContent = "";
  detailReferences.textContent = "";
  detailInsights.innerHTML = "";
  detailLogic.textContent = "";
  detailEvidence.innerHTML = "";
  detailLimitations.innerHTML = "";
  detailKeyDeps.innerHTML = "";
  detailDatasetDeps.innerHTML = "";
  traceStatus.textContent = fromFavorites
    ? "Loading cached summary and cached lineage graph..."
    : "";
  currentPaperId = paperId;
  activeTraceId = null;
  currentCy = null;
  graphEl.style.display = "none";
  graphEmpty.style.display = "block";
  graphEmpty.textContent = fromFavorites
    ? "Loading cached lineage graph..."
    : "Run trace-back to generate lineage graph.";

  try {
    const detailUrl = `/api/papers/${paperId}?user_id=${encodeURIComponent(USER_ID)}${
      fromFavorites ? "&prefer_cached=true" : ""
    }`;
    const data = await api(detailUrl);
    detailTitle.textContent = data.title;
    const reviewText = (data.review_score_avg !== null && data.review_score_avg !== undefined)
      ? ` • Review: ${Number(data.review_score_avg).toFixed(2)}${data.review_count ? ` (n=${data.review_count})` : ""}`
      : "";
    const decisionText = data.decision ? ` • Decision: ${data.decision}` : "";
    detailMeta.textContent = `${data.venue || "Unknown venue"} • ${data.year || "Unknown year"} • Citations: ${data.citation_count || 0}${reviewText}${decisionText}`;
    currentPaperUrl = resolveReadableUrl(data.url);
    setDetailReadState(currentPaperUrl);
    detailAbstract.textContent = data.abstract || "No abstract available.";
    detailReferences.textContent = `References available: ${data.references_count || 0}`;
    renderBulletList(detailInsights, data.quick_takeaways, "No concise takeaways generated yet.");
    detailLogic.textContent = data.logic_summary || "No logic summary generated yet.";
    renderBulletList(detailEvidence, data.evidence_points, "No evidence summary generated yet.");
    renderBulletList(detailLimitations, data.limitations, "No explicit limitations extracted yet.");
    renderDependencyList(detailKeyDeps, data.key_dependencies || [], "No high-confidence dependency extracted.");
    renderDependencyList(detailDatasetDeps, data.dataset_dependencies || [], "No dataset/benchmark paper extracted.");
    if (fromFavorites) {
      await loadCachedTraceForPaper(paperId);
    } else {
      traceStatus.textContent = "";
    }
  } catch (err) {
    detailTitle.textContent = "Failed to load paper";
    detailMeta.textContent = "";
    setDetailReadState(null);
    detailAbstract.textContent = err.message;
  }
}

function setDetailReadState(url) {
  const hasUrl = typeof url === "string" && url.trim().length > 0;
  if (!hasUrl) {
    detailReadBtn.classList.add("disabled");
    detailReadBtn.textContent = "No Link Available";
    detailReadBtn.disabled = true;
    return;
  }
  detailReadBtn.classList.remove("disabled");
  detailReadBtn.textContent = "Read Full Paper";
  detailReadBtn.disabled = false;
}

function renderBulletList(container, items, emptyText) {
  container.innerHTML = "";
  const safeItems = Array.isArray(items) ? items.filter((x) => typeof x === "string" && x.trim()) : [];
  if (!safeItems.length) {
    const li = document.createElement("li");
    li.textContent = emptyText;
    container.appendChild(li);
    return;
  }
  safeItems.forEach((item) => {
    const li = document.createElement("li");
    li.textContent = item;
    container.appendChild(li);
  });
}

function renderDependencyList(container, deps, emptyText) {
  container.innerHTML = "";
  if (!Array.isArray(deps) || !deps.length) {
    const li = document.createElement("li");
    li.textContent = emptyText || "No dependency extracted.";
    container.appendChild(li);
    return;
  }
  deps.forEach((dep) => {
    const li = document.createElement("li");
    const confidence = Number.isFinite(dep.confidence) ? dep.confidence.toFixed(2) : "0.60";
    const title = dep.title || "Unknown paper";
    const readableUrl = resolveReadableUrl(dep.url);
    if (readableUrl) {
      const link = document.createElement("a");
      link.href = readableUrl;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = title;
      li.appendChild(link);
    } else {
      li.appendChild(document.createTextNode(title));
    }
    li.appendChild(document.createTextNode(` (${dep.role || "dependency"}, confidence ${confidence}) — ${dep.reason || ""}`));
    container.appendChild(li);
  });
}

detailReadBtn.addEventListener("click", () => {
  if (!currentPaperUrl) {
    return;
  }
  window.open(currentPaperUrl, "_blank", "noopener,noreferrer");
});

async function startTrace() {
  if (!currentPaperId) {
    alert("Open a paper detail first.");
    return;
  }
  const traceDepth = parseInt(traceDepthInput.value, 10);
  traceStatus.textContent = "Starting trace-back job...";
  currentCy = null;
  graphEl.style.display = "none";
  graphEmpty.style.display = "block";
  graphEmpty.textContent = "Tracing method lineage...";

  try {
    const data = await api("/api/traces", "POST", {
      user_id: USER_ID,
      paper_id: currentPaperId,
      trace_depth: Number.isFinite(traceDepth) ? traceDepth : 2,
    });
    activeTraceId = data.trace_id;
    pollTraceStatus(activeTraceId);
  } catch (err) {
    traceStatus.textContent = `Trace start failed: ${err.message}`;
  }
}

async function pollTraceStatus(traceId) {
  const started = Date.now();
  const timeoutMs = 120000;

  while (Date.now() - started < timeoutMs) {
    try {
      const data = await api(`/api/traces/${traceId}`);
      traceStatus.textContent = `Trace status: ${data.status}`;

      if (data.status === "completed") {
        renderGraph(data);
        traceStatus.textContent = `Trace completed. Nodes: ${data.nodes.length}, edges: ${data.edges.length}`;
        return;
      }
      if (data.status === "failed") {
        graphEmpty.style.display = "block";
        graphEl.style.display = "none";
        graphEmpty.textContent = `Trace failed: ${data.error_message || "Unknown error"}`;
        return;
      }
    } catch (err) {
      traceStatus.textContent = `Trace polling failed: ${err.message}`;
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 1700));
  }

  traceStatus.textContent = "Trace timed out. You can retry.";
}

function renderGraph(traceData) {
  const incomingCountByNode = new Map();
  (traceData.nodes || []).forEach((node) => {
    incomingCountByNode.set(node.paper_id, 0);
  });
  (traceData.edges || []).forEach((edge) => {
    const target = edge.target_paper_id;
    incomingCountByNode.set(target, (incomingCountByNode.get(target) || 0) + 1);
  });
  const maxIncoming = Math.max(0, ...Array.from(incomingCountByNode.values()));

  const nodeColorFromIncoming = (incoming) => {
    if (!incoming || incoming <= 0) {
      return "#c8d7cf";
    }
    const ratio = maxIncoming > 0 ? incoming / maxIncoming : 0;
    if (ratio >= 0.67) {
      return "#0f6b55";
    }
    if (ratio >= 0.34) {
      return "#2f9b76";
    }
    return "#7abf9f";
  };

  const edgeTierFromConfidence = (confidence) => {
    const value = Number.isFinite(confidence) ? confidence : 0.0;
    if (value >= 0.78) {
      return "high";
    }
    if (value >= 0.62) {
      return "medium";
    }
    return "low";
  };

  const edgeColorFromTier = (tier) => {
    if (tier === "high") {
      return "#0f7b6c";
    }
    if (tier === "medium") {
      return "#f39c12";
    }
    return "#d85b47";
  };

  const edgeWidthFromTier = (tier) => {
    if (tier === "high") {
      return 3.0;
    }
    if (tier === "medium") {
      return 2.4;
    }
    return 1.9;
  };

  const edgeLineStyle = (relationType) => {
    if (relationType === "foundational_method") {
      return "solid";
    }
    if (relationType === "direct_technical_dependency") {
      return "dashed";
    }
    return "solid";
  };

  const compactTitle = (title) => {
    if (typeof title !== "string" || !title.trim()) {
      return "Unknown paper";
    }
    const t = title.trim();
    return t.length > 68 ? `${t.slice(0, 68)}...` : t;
  };

  const firstSentence = (text) => {
    if (typeof text !== "string" || !text.trim()) {
      return "";
    }
    const trimmed = text.trim();
    const first = trimmed.split(/[.!?]/)[0].trim();
    if (!first) {
      return "";
    }
    return first.length > 120 ? `${first.slice(0, 120)}...` : `${first}.`;
  };

  const relationLabel = (relationType) => {
    if (relationType === "foundational_method") {
      return "foundational method";
    }
    if (relationType === "direct_technical_dependency") {
      return "direct technical dependency";
    }
    return (relationType || "dependency").replaceAll("_", " ");
  };

  const relevanceLabel = (tier) => {
    if (tier === "high") {
      return "high relevance";
    }
    if (tier === "medium") {
      return "medium relevance";
    }
    return "low relevance";
  };

  const elements = [];
  const nodeTitleById = new Map();
  traceData.nodes.forEach((node) => {
    nodeTitleById.set(node.paper_id, node.title);
    const incoming = incomingCountByNode.get(node.paper_id) || 0;
    elements.push({
      data: {
        id: node.paper_id,
        label: `${node.title.slice(0, 56)}${node.title.length > 56 ? "..." : ""}\nL${node.level} • In ${incoming}`,
        fullTitle: node.title,
        level: node.level,
        incoming,
        nodeColor: nodeColorFromIncoming(incoming),
      },
    });
  });

  traceData.edges.forEach((edge, idx) => {
    const confidence = Number.isFinite(edge.confidence) ? edge.confidence : 0.0;
    const tier = edgeTierFromConfidence(confidence);
    elements.push({
      data: {
        id: `e-${idx}-${edge.source_paper_id}-${edge.target_paper_id}`,
        source: edge.source_paper_id,
        target: edge.target_paper_id,
        relationType: edge.relation_type,
        confidence,
        tier,
        color: edgeColorFromTier(tier),
        width: edgeWidthFromTier(tier),
        lineStyle: edgeLineStyle(edge.relation_type),
        reason: edge.reason,
      },
    });
  });

  graphEmpty.style.display = "none";
  graphEl.style.display = "block";
  graphEl.innerHTML = "";
  if (edgeExplainer) {
    edgeExplainer.textContent = "Click an edge to inspect the paper link.";
  }

  const cy = cytoscape({
    container: graphEl,
    elements,
    style: [
      {
        selector: "node",
        style: {
          "background-color": "data(nodeColor)",
          "text-wrap": "wrap",
          "text-max-width": 130,
          color: "#10231b",
          "font-size": 9,
          "font-family": "Space Grotesk",
          label: "data(label)",
          width: 42,
          height: 42,
          "border-width": 2,
          "border-color": "#dff5ef",
        },
      },
      {
        selector: "node[level = 0]",
        style: {
          width: 52,
          height: 52,
          "border-width": 4,
          "border-color": "#ff7a3e",
        },
      },
      {
        selector: "edge",
        style: {
          width: "data(width)",
          "curve-style": "bezier",
          "line-color": "data(color)",
          "target-arrow-color": "data(color)",
          "target-arrow-shape": "triangle",
          "line-style": "data(lineStyle)",
          opacity: 0.92,
          "overlay-padding": 10,
        },
      },
    ],
    layout: {
      name: "breadthfirst",
      directed: true,
      roots: [traceData.root_paper_id],
      spacingFactor: 1.25,
      padding: 22,
      animate: true,
      animationDuration: 420,
    },
  });

  cy.on("tap", "node", (evt) => {
    const nodeId = evt.target.id();
    loadPaperDetail(nodeId);
  });

  const onEdgeSelected = (evt) => {
    const edge = evt.target.data();
    const srcTitle = compactTitle(nodeTitleById.get(edge.source) || "");
    const dstTitle = compactTitle(nodeTitleById.get(edge.target) || "");
    const rel = relationLabel(edge.relationType);
    const relTier = relevanceLabel(edge.tier);
    const reason = firstSentence(edge.reason);
    const text = `${srcTitle} builds on ${dstTitle} (${rel}, ${relTier}). ${reason}`.trim();
    traceStatus.textContent = text;
    if (edgeExplainer) {
      edgeExplainer.textContent = text;
    }
  };
  cy.on("tap", "edge", onEdgeSelected);
  cy.on("click", "edge", onEdgeSelected);
  currentCy = cy;
}

function resetGraphView() {
  if (!currentCy) {
    traceStatus.textContent = "No graph to reset yet.";
    return;
  }
  currentCy.animate(
    {
      fit: { padding: 22 },
      duration: 260,
    },
    {
      complete: () => currentCy.center(),
    }
  );
}

document.getElementById("refresh-favorites").addEventListener("click", loadFavorites);
startTraceBtn.addEventListener("click", startTrace);
searchForm.addEventListener("submit", runSearch);
searchModeEl.addEventListener("change", updateSearchModeUI);
allConferencesEl.addEventListener("change", updateConferenceFilterUI);
saveSearchBtn.addEventListener("click", saveCurrentSearch);
prevPageBtn.addEventListener("click", () => {
  if (currentPage <= 1) {
    return;
  }
  executeSearch(currentPage - 1);
});
nextPageBtn.addEventListener("click", () => {
  if (currentPage >= totalPages) {
    return;
  }
  executeSearch(currentPage + 1);
});
graphResetBtn.addEventListener("click", resetGraphView);
visualizeFavoritesLinksBtn.addEventListener("click", openFavoritesGraphModal);
favoritesGraphCancelBtn.addEventListener("click", closeFavoritesGraphModal);
favoritesGraphOpenBtn.addEventListener("click", openFavoritesGraphPage);
favoritesGraphModal.addEventListener("click", (evt) => {
  if (evt.target === favoritesGraphModal) {
    closeFavoritesGraphModal();
  }
});

createConferenceChips();
updateSearchModeUI();
updatePaginationUI();
loadFavorites();
