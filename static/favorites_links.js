const USER_ID = "demo-user";

let currentCy = null;
let currentPaperUrl = null;
let selectedPaperIds = [];
let currentGraphData = null;

const selectionMeta = document.getElementById("links-selection-meta");
const backBtn = document.getElementById("back-home");
const reloadBtn = document.getElementById("reload-links-graph");
const resetBtn = document.getElementById("links-reset-view");

const detailEmpty = document.getElementById("fl-detail-empty");
const detailContent = document.getElementById("fl-detail-content");
const detailTitle = document.getElementById("fl-detail-title");
const detailMeta = document.getElementById("fl-detail-meta");
const detailReadBtn = document.getElementById("fl-detail-read-btn");
const detailAbstract = document.getElementById("fl-detail-abstract");
const detailReferences = document.getElementById("fl-detail-references");
const detailInsights = document.getElementById("fl-detail-insights");
const detailLogic = document.getElementById("fl-detail-logic");
const detailEvidence = document.getElementById("fl-detail-evidence");
const detailLimitations = document.getElementById("fl-detail-limitations");
const detailKeyDeps = document.getElementById("fl-detail-key-deps");
const detailDatasetDeps = document.getElementById("fl-detail-dataset-deps");

const edgeExplainer = document.getElementById("fl-edge-explainer");
const graphEmpty = document.getElementById("fl-graph-empty");
const graphEl = document.getElementById("favorites-links-graph");

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

function parseSelectedIds() {
  const raw = sessionStorage.getItem("favoritesGraphSelection");
  if (raw) {
    try {
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed?.paper_ids)) {
        return Array.from(new Set(parsed.paper_ids.map((x) => String(x).trim()).filter(Boolean)));
      }
    } catch (err) {
      // no-op
    }
  }
  const params = new URLSearchParams(window.location.search);
  const fromQuery = (params.get("paper_ids") || "").split(",").map((x) => x.trim()).filter(Boolean);
  return Array.from(new Set(fromQuery));
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
  detailLimitations.innerHTML = "";
  detailKeyDeps.innerHTML = "";
  detailDatasetDeps.innerHTML = "";

  try {
    const data = await api(`/api/papers/${paperId}?user_id=${encodeURIComponent(USER_ID)}&prefer_cached=true`);
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
  } catch (err) {
    detailTitle.textContent = "Failed to load paper";
    detailAbstract.textContent = err.message;
  }
}

function renderGraph(data) {
  currentGraphData = data;
  const incomingCountByNode = new Map();
  (data.nodes || []).forEach((node) => {
    incomingCountByNode.set(node.paper_id, 0);
  });
  (data.edges || []).forEach((edge) => {
    const target = edge.target_paper_id;
    incomingCountByNode.set(target, (incomingCountByNode.get(target) || 0) + 1);
  });
  const maxIncoming = Math.max(0, ...Array.from(incomingCountByNode.values()));

  const nodeColorFromIncoming = (incoming) => {
    if (!incoming || incoming <= 0) {
      return "#c8d7cf";
    }
    const ratio = maxIncoming > 0 ? incoming / maxIncoming : 0;
    if (ratio >= 0.67) return "#0f6b55";
    if (ratio >= 0.34) return "#2f9b76";
    return "#7abf9f";
  };

  const compactTitle = (title) => {
    if (typeof title !== "string" || !title.trim()) {
      return "Unknown paper";
    }
    const t = title.trim();
    return t.length > 72 ? `${t.slice(0, 72)}...` : t;
  };

  const tierFromConfidence = (confidence) => {
    const value = Number.isFinite(confidence) ? confidence : 0.0;
    if (value >= 0.78) return "high";
    if (value >= 0.62) return "medium";
    return "low";
  };

  const colorFromTier = (tier, relationType) => {
    if (relationType === "related_topic") {
      return "#3e78c6";
    }
    if (tier === "high") return "#0f7b6c";
    if (tier === "medium") return "#f39c12";
    return "#d85b47";
  };

  const elements = [];
  const nodeTitleById = new Map();

  (data.nodes || []).forEach((node) => {
    nodeTitleById.set(node.paper_id, node.title);
    const incoming = incomingCountByNode.get(node.paper_id) || 0;
    elements.push({
      data: {
        id: node.paper_id,
        label: `${compactTitle(node.title)}\nL${node.level} • In ${incoming}`,
        fullTitle: node.title,
        level: node.level,
        isRoot: !!node.is_selected_root,
        incoming,
        nodeColor: nodeColorFromIncoming(incoming),
      },
    });
  });

  (data.edges || []).forEach((edge, idx) => {
    const tier = tierFromConfidence(edge.confidence);
    const relationType = edge.relation_type || "related_topic";
    elements.push({
      data: {
        id: `e-${idx}-${edge.source_paper_id}-${edge.target_paper_id}`,
        source: edge.source_paper_id,
        target: edge.target_paper_id,
        relationType,
        confidence: Number.isFinite(edge.confidence) ? edge.confidence : 0.0,
        tier,
        color: colorFromTier(tier, relationType),
        width: relationType === "related_topic" ? 1.7 : (tier === "high" ? 3.0 : tier === "medium" ? 2.4 : 1.9),
        lineStyle: relationType === "related_topic" ? "dotted" : (relationType === "direct_technical_dependency" ? "dashed" : "solid"),
        reason: edge.reason || "",
      },
    });
  });

  graphEmpty.style.display = "none";
  graphEl.style.display = "block";
  graphEl.innerHTML = "";
  edgeExplainer.textContent = "Click an edge to inspect how two papers are linked.";

  const cy = cytoscape({
    container: graphEl,
    elements,
    style: [
      {
        selector: "node",
        style: {
          "background-color": "data(nodeColor)",
          "text-wrap": "wrap",
          "text-max-width": 150,
          color: "#10231b",
          "font-size": 9,
          "font-family": "Space Grotesk",
          label: "data(label)",
          width: 40,
          height: 40,
          "border-width": 2,
          "border-color": "#dff5ef",
        },
      },
      {
        selector: "node[isRoot = 1]",
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
          opacity: 0.93,
          "overlay-padding": 10,
        },
      },
    ],
    layout: {
      name: "cose",
      fit: true,
      padding: 28,
      animate: false,
      nodeRepulsion: 90000,
      idealEdgeLength: 130,
      edgeElasticity: 90,
      gravity: 0.8,
      numIter: 1100,
    },
  });

  cy.on("tap", "node", (evt) => {
    const nodeId = evt.target.id();
    loadPaperDetail(nodeId);
  });

  cy.on("tap", "edge", (evt) => {
    const edge = evt.target.data();
    const source = compactTitle(nodeTitleById.get(edge.source) || "");
    const target = compactTitle(nodeTitleById.get(edge.target) || "");
    const relation = String(edge.relationType || "dependency").replaceAll("_", " ");
    const conf = Number.isFinite(edge.confidence) ? edge.confidence.toFixed(2) : "0.00";
    const reason = String(edge.reason || "").trim();
    const sentence = `${source} -> ${target} (${relation}, confidence ${conf}). ${reason}`.trim();
    edgeExplainer.textContent = sentence;
  });

  currentCy = cy;
}

async function loadLinksGraph() {
  if (!selectedPaperIds.length) {
    selectionMeta.textContent = "No selected papers were provided. Go back and choose favorites.";
    graphEmpty.style.display = "block";
    graphEl.style.display = "none";
    graphEmpty.textContent = "No selected favorites.";
    return;
  }

  selectionMeta.textContent = `Loading graph for ${selectedPaperIds.length} selected favorites...`;
  graphEmpty.style.display = "block";
  graphEl.style.display = "none";
  graphEmpty.textContent = "Loading cached favorite links graph...";

  try {
    const data = await api("/api/favorites/links-graph", "POST", {
      user_id: USER_ID,
      paper_ids: selectedPaperIds,
      max_related_edges: 48,
    });
    const nodeCount = (data.nodes || []).length;
    const edgeCount = (data.edges || []).length;
    selectionMeta.textContent = `${(data.selected_paper_ids || []).length} selected favorites • ${nodeCount} nodes • ${edgeCount} edges`;

    if (!nodeCount) {
      graphEmpty.style.display = "block";
      graphEl.style.display = "none";
      graphEmpty.textContent = "No cached nodes found yet. Build traces from favorites first.";
      return;
    }

    renderGraph(data);

    const startId = (data.selected_paper_ids || [])[0] || (data.nodes[0] && data.nodes[0].paper_id);
    if (startId) {
      loadPaperDetail(startId);
    }
  } catch (err) {
    selectionMeta.textContent = "Failed to load links graph.";
    graphEmpty.style.display = "block";
    graphEl.style.display = "none";
    graphEmpty.textContent = `Graph load failed: ${err.message}`;
  }
}

function resetView() {
  if (!currentCy) {
    return;
  }
  currentCy.animate(
    {
      fit: { padding: 28 },
      duration: 260,
    },
    {
      complete: () => currentCy.center(),
    }
  );
}

detailReadBtn.addEventListener("click", () => {
  if (!currentPaperUrl) {
    return;
  }
  window.open(currentPaperUrl, "_blank", "noopener,noreferrer");
});

backBtn.addEventListener("click", () => {
  window.location.href = "/";
});

reloadBtn.addEventListener("click", () => {
  loadLinksGraph();
});

resetBtn.addEventListener("click", resetView);

selectedPaperIds = parseSelectedIds();
loadLinksGraph();
