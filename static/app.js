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
const detailKeyDeps = document.getElementById("detail-key-deps");
const traceStatus = document.getElementById("trace-status");

const traceDepthInput = document.getElementById("trace-depth");
const startTraceBtn = document.getElementById("start-trace");

const graphEmpty = document.getElementById("graph-empty");
const graphEl = document.getElementById("graph");

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
    meta.textContent = `${paper.venue || "Unknown venue"} • ${paper.year || "Unknown year"} • Citations: ${paper.citation_count || 0}`;
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
      loadPaperDetail(paper.id);
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
    renderPaperCards(favoritesList, data.papers, true);
  } catch (err) {
    favoritesList.classList.add("empty");
    favoritesList.textContent = `Failed to load favorites: ${err.message}`;
  }
}

async function loadPaperDetail(paperId) {
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
  detailKeyDeps.innerHTML = "";
  traceStatus.textContent = "";
  currentPaperId = paperId;

  try {
    const data = await api(`/api/papers/${paperId}?user_id=${encodeURIComponent(USER_ID)}`);
    detailTitle.textContent = data.title;
    detailMeta.textContent = `${data.venue || "Unknown venue"} • ${data.year || "Unknown year"} • Citations: ${data.citation_count || 0}`;
    currentPaperUrl = resolveReadableUrl(data.url);
    setDetailReadState(currentPaperUrl);
    detailAbstract.textContent = data.abstract || "No abstract available.";
    detailReferences.textContent = `References available: ${data.references_count || 0}`;
    renderBulletList(detailInsights, data.quick_takeaways, "No concise takeaways generated yet.");
    detailLogic.textContent = data.logic_summary || "No logic summary generated yet.";
    renderBulletList(detailEvidence, data.evidence_points, "No evidence summary generated yet.");
    renderDependencyList(detailKeyDeps, data.key_dependencies || []);
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

function renderDependencyList(container, deps) {
  container.innerHTML = "";
  if (!Array.isArray(deps) || !deps.length) {
    const li = document.createElement("li");
    li.textContent = "No high-confidence dependency extracted.";
    container.appendChild(li);
    return;
  }
  deps.forEach((dep) => {
    const li = document.createElement("li");
    const confidence = Number.isFinite(dep.confidence) ? dep.confidence.toFixed(2) : "0.60";
    li.textContent = `${dep.title || "Unknown paper"} (${dep.role || "dependency"}, confidence ${confidence}) — ${dep.reason || ""}`;
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
  const elements = [];
  traceData.nodes.forEach((node) => {
    elements.push({
      data: {
        id: node.paper_id,
        label: `${node.title.slice(0, 56)}${node.title.length > 56 ? "..." : ""}\nL${node.level}`,
        level: node.level,
      },
    });
  });

  traceData.edges.forEach((edge, idx) => {
    elements.push({
      data: {
        id: `e-${idx}-${edge.source_paper_id}-${edge.target_paper_id}`,
        source: edge.source_paper_id,
        target: edge.target_paper_id,
        label: `${edge.relation_type} (${edge.confidence.toFixed(2)})`,
        reason: edge.reason,
      },
    });
  });

  graphEmpty.style.display = "none";
  graphEl.style.display = "block";
  graphEl.innerHTML = "";

  const cy = cytoscape({
    container: graphEl,
    elements,
    style: [
      {
        selector: "node",
        style: {
          "background-color": "#0f7b6c",
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
          "background-color": "#ff7a3e",
          width: 52,
          height: 52,
          "border-color": "#ffe3d3",
        },
      },
      {
        selector: "edge",
        style: {
          width: 1.8,
          "curve-style": "bezier",
          "line-color": "#7a968b",
          "target-arrow-color": "#7a968b",
          "target-arrow-shape": "triangle",
          label: "data(label)",
          "font-size": 8,
          color: "#3a5547",
          "text-background-color": "rgba(255,255,255,0.7)",
          "text-background-opacity": 1,
          "text-background-padding": 2,
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

  cy.on("tap", "edge", (evt) => {
    const edge = evt.target.data();
    traceStatus.textContent = `Edge reason: ${edge.reason}`;
  });
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

createConferenceChips();
updateSearchModeUI();
updatePaginationUI();
loadFavorites();
