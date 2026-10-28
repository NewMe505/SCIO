/* SCIO's live dashboard (issue #68, ADR 0017): a read-only page over the dashboard API.
 *
 * Reads GET /api/roots, /api/threads, /api/threads/{root}/{thread_id} and /api/graphs, and
 * listens on the /ws WebSocket for ChangeEvents. Every string from the API can be model
 * output, so the DOM is built with createElement and textContent only: nothing here writes
 * HTML from data or evaluates code (tests/live/test_dashboard_page.py scans for it).
 */
"use strict";

(function () {
  const BACKOFF_START_MS = 1000;
  const BACKOFF_MAX_MS = 15000;
  const MERMAID_WAIT_MS = 15000;
  const DEFER_MAX_MS = 3000;
  const SVG_NS = "http://www.w3.org/2000/svg";
  const STATUS_LABEL = {
    in_progress: "in progress",
    waiting: "waiting",
    crashed: "crashed",
    done: "done",
    halted: "halted",
    error: "error",
  };
  // the lit node's style follows the thread's status colour
  const LIT_STYLE = {
    waiting: "fill:#fcd34d,stroke:#92400e,stroke-width:3px,color:#1f1300",
    crashed: "fill:#fca5a5,stroke:#991b1b,stroke-width:3px,color:#2a0606",
    halted: "fill:#fca5a5,stroke:#991b1b,stroke-width:3px,color:#2a0606",
    done: "fill:#86efac,stroke:#166534,stroke-width:3px,color:#052e16",
    error: "fill:#d1d5db,stroke:#374151,stroke-width:3px,color:#111827",
    in_progress: "fill:#93c5fd,stroke:#1e40af,stroke-width:3px,color:#0b1b3f",
  };

  const state = {
    roots: [],
    threads: [],
    graphs: new Map(),
    graphsLoaded: false,
    route: parseRoute(),
    detail: null, // {key, data} or {key, error, status}
    pending: null,
    refreshing: false,
    deferred: false, // a live redraw waits (briefly) while text in the page is selected
    deferTimer: null,
    forceRedraw: false,
    mermaid: "loading", // loading | ready | failed
    svgCache: new Map(),
    graphSeq: 0,
  };

  // ---------------------------------------------------------------- DOM helpers

  function el(tag, props, children) {
    const node = document.createElement(tag);
    setProps(node, props);
    return add(node, children);
  }

  function svgEl(tag, props) {
    const node = document.createElementNS(SVG_NS, tag);
    setProps(node, props);
    return node;
  }

  function setProps(node, props) {
    if (!props) return;
    for (const [name, value] of Object.entries(props)) {
      if (value === null || value === undefined || value === false) continue;
      if (name === "text") node.textContent = String(value);
      else if (name === "class") node.setAttribute("class", String(value));
      else node.setAttribute(name, value === true ? "" : String(value));
    }
  }

  function add(node, children) {
    if (children === null || children === undefined) return node;
    for (const child of Array.isArray(children) ? children : [children]) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : String(child));
    }
    return node;
  }

  function tok(value) {
    return String(value || "none").toLowerCase().replace(/[^a-z0-9_-]/g, "_");
  }

  function list(value) {
    return Array.isArray(value) ? value : [];
  }

  function entries(map) {
    if (!map || typeof map !== "object") return [];
    return Object.entries(map)
      .map(([name, value]) => [name, Number(value)])
      .filter(([, value]) => Number.isFinite(value))
      .sort((a, b) => b[1] - a[1]);
  }

  // ---------------------------------------------------------------- formatting

  function usd(value) {
    const n = Number(value);
    return "$" + (Number.isFinite(n) ? n : 0).toFixed(4);
  }

  function int(value) {
    const n = Number(value);
    return Number.isFinite(n) ? n.toLocaleString() : "—";
  }

  function dash(value) {
    return value === null || value === undefined || value === "" ? "—" : String(value);
  }

  function parseTime(value) {
    const t = Date.parse(value);
    return Number.isFinite(t) ? t : null;
  }

  function absTime(value) {
    const t = parseTime(value);
    return t === null ? "" : new Date(t).toLocaleString();
  }

  function shortDate(t) {
    return new Date(t).toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  function relTime(value, now) {
    const t = parseTime(value);
    if (t === null) return "never";
    const sec = Math.round((now - t) / 1000);
    if (sec < 0) return sec > -45 ? "just now" : shortDate(t); // a clock ahead of this one
    if (sec < 45) return "just now";
    const min = Math.round(sec / 60);
    if (min < 60) return min + " min ago";
    const hours = Math.round(min / 60);
    if (hours < 36) return hours + " h ago";
    const days = Math.round(hours / 24);
    if (days < 14) return days + " d ago";
    return shortDate(t);
  }

  function timeEl(value) {
    if (parseTime(value) === null) return el("span", { class: "muted", text: "never" });
    return el("time", {
      datetime: value,
      title: absTime(value),
      "data-rel": "1",
      text: relTime(value, Date.now()),
    });
  }

  function clockEl(value) {
    const t = parseTime(value);
    if (t === null) return el("span", { class: "muted", text: dash(value) });
    return el("time", {
      datetime: value,
      title: absTime(value),
      text: new Date(t).toLocaleTimeString(undefined, { hour12: false }),
    });
  }

  function tickTimes() {
    const now = Date.now();
    for (const node of document.querySelectorAll("time[data-rel]")) {
      node.textContent = relTime(node.getAttribute("datetime"), now);
    }
  }

  function safeHref(url) {
    if (typeof url !== "string" || !url) return null;
    try {
      const parsed = new URL(url, location.href);
      return parsed.protocol === "http:" || parsed.protocol === "https:" ? parsed.href : null;
    } catch (err) {
      return null;
    }
  }

  function key(root, threadId) {
    return root + "\u0000" + threadId;
  }

  function threadHref(root, threadId) {
    return "#/thread/" + encodeURIComponent(root) + "/" + encodeURIComponent(threadId);
  }

  function threadPath(root, threadId) {
    return "/api/threads/" + encodeURIComponent(root) + "/" + encodeURIComponent(threadId);
  }

  // ---------------------------------------------------------------- small pieces

  function badge(status, extra) {
    return el("span", {
      class: "badge s-" + tok(status) + (extra ? " " + extra : ""),
      text: STATUS_LABEL[status] || dash(status),
    });
  }

  function featureBadge(status) {
    return el("span", { class: "badge f-" + tok(status), text: dash(status) });
  }

  function card(title, children, extraClass) {
    const section = el("section", { class: "card" + (extraClass ? " " + extraClass : "") });
    if (title) section.append(el("h2", { text: title }));
    return add(section, children);
  }

  function facts(pairs) {
    const dl = el("dl", { class: "facts" });
    for (const [label, value] of pairs) {
      if (value === undefined) continue;
      dl.append(el("div", { class: "fact" }, [el("dt", { text: label }), el("dd", {}, value)]));
    }
    return dl;
  }

  function mono(text, title) {
    return el("code", { class: "mono", title: title || null, text: dash(text) });
  }

  function commitEl(sha) {
    if (!sha) return el("span", { class: "muted", text: "—" });
    return mono(String(sha).slice(0, 12), String(sha));
  }

  function externalLink(url, text) {
    const href = safeHref(url);
    if (!href) return el("span", { text: dash(text || url) });
    return el("a", { href: href, target: "_blank", rel: "noopener noreferrer", text: text || href });
  }

  function pre(lines, extraClass) {
    return el("pre", { class: "lines" + (extraClass ? " " + extraClass : ""), text: lines.join("\n") });
  }

  function table(headers, rows, opts) {
    const options = opts || {};
    const thead = el("thead", {}, el("tr", {}, headers.map((h) => el("th", { scope: "col", class: h.cls || null, title: h.title || null, text: h.text }))));
    const tbody = el("tbody", {}, rows);
    const tableNode = el("table", { class: "grid" + (options.cls ? " " + options.cls : "") }, [thead, tbody]);
    return el("div", { class: "table-wrap" }, tableNode);
  }

  function td(children, cls) {
    return el("td", { class: cls || null }, children);
  }

  function resumeEl(command) {
    return el("code", {
      class: "resume",
      tabindex: "0",
      title: "Select to copy; run it in a terminal",
      text: command,
    });
  }

  function waitingBox(summary) {
    const wait = summary.waiting || {};
    const payload = wait.payload && typeof wait.payload === "object" ? wait.payload : {};
    const box = el("div", { class: "waiting-box" }, [
      el("span", { class: "badge waiting-for-you", text: "waiting for you" }),
      el("p", { class: "question", text: wait.question || "This thread is paused for a decision." }),
    ]);
    if (wait.node) box.append(el("p", { class: "meta" }, ["asked by ", mono(wait.node)]));
    if (typeof payload.resume === "string" && payload.resume) {
      box.append(el("p", { class: "meta resume-line" }, ["resume with ", resumeEl(payload.resume)]));
    }
    return box;
  }

  function crashBox(summary, detailsKey) {
    return el("details", { class: "crash-box", "data-key": detailsKey }, [
      el("summary", {}, [badge("crashed"), " the process died in a node; a resume clears it"]),
      el("pre", { class: "lines", text: summary.crash || "" }),
    ]);
  }

  function errorBox(summary) {
    return el("p", { class: "error-box" }, [
      el("strong", { text: "Could not read this thread: " }),
      el("span", { text: summary.error || "unknown error" }),
    ]);
  }

  // ---------------------------------------------------------------- bars (inline SVG)

  function bars(map, label, extraLabel) {
    const rows = entries(map);
    if (!rows.length) return el("p", { class: "muted", text: "No cost recorded." });
    const max = Math.max(...rows.map((r) => r[1])) || 1;
    const ul = el("ul", { class: "bars", "aria-label": label });
    for (const [name, value] of rows) {
      const pct = value > 0 ? Math.max((value / max) * 100, 1) : 0;
      const svg = svgEl("svg", { class: "bar-svg", width: "100%", height: "14", "aria-hidden": "true", focusable: "false" });
      if (pct > 0) {
        // a 4px rounded data end, square at the baseline: the second rect squares the left end
        svg.append(svgEl("rect", { class: "bar", x: "0", y: "1", width: pct + "%", height: "12", rx: "4", ry: "4" }));
        svg.append(svgEl("rect", { class: "bar", x: "0", y: "1", width: pct / 2 + "%", height: "12" }));
      }
      const extra = extraLabel ? extraLabel(name) : "";
      ul.append(
        el("li", { class: "bar-row", title: name + ": " + usd(value) + (extra ? " (" + extra + ")" : "") }, [
          el("span", { class: "bar-label", text: name }),
          el("span", { class: "bar-track" }, svg),
          el("span", { class: "bar-value", text: usd(value) + (extra ? " · " + extra : "") }),
        ]),
      );
    }
    return ul;
  }

  // ---------------------------------------------------------------- Mermaid

  function onMermaidTag(event) {
    const target = event.target;
    if (!target || target.id !== "mermaid-js" || state.mermaid !== "loading") return;
    if (event.type === "load" && window.mermaid) mermaidReady();
    else mermaidFailed();
  }

  function mermaidReady() {
    try {
      window.mermaid.initialize({ startOnLoad: false, securityLevel: "strict" });
      state.mermaid = "ready";
    } catch (err) {
      state.mermaid = "failed";
    }
    renderCurrentDiagrams();
  }

  function mermaidFailed() {
    state.mermaid = "failed";
    renderCurrentDiagrams();
  }

  function setupMermaid() {
    // app.js runs before the Mermaid tag is parsed, so these capture its load or error
    document.addEventListener("load", onMermaidTag, true);
    document.addEventListener("error", onMermaidTag, true);
    if (window.mermaid) mermaidReady();
    window.setTimeout(() => {
      if (state.mermaid !== "loading") return;
      if (window.mermaid) mermaidReady();
      else mermaidFailed();
    }, MERMAID_WAIT_MS);
  }

  function litText(graph, lit, status) {
    if (!lit.length) return graph.mermaid;
    let text = graph.mermaid.endsWith("\n") ? graph.mermaid : graph.mermaid + "\n";
    text += "\tclassDef current " + (LIT_STYLE[status] || LIT_STYLE.in_progress) + "\n";
    for (const node of lit) text += "\tclass " + node + " current\n";
    return text;
  }

  async function renderMermaid(text) {
    const id = "scio-graph-" + ++state.graphSeq;
    try {
      const out = await window.mermaid.render(id, text);
      const markup = out && typeof out.svg === "string" ? out.svg : "";
      // parsed inert (no script runs in a DOMParser document), then imported
      const parsed = new DOMParser().parseFromString(markup, "text/html");
      const svg = parsed.querySelector("svg");
      if (!svg) throw new Error("Mermaid returned no SVG");
      return document.importNode(svg, true);
    } finally {
      for (const stray of [document.getElementById(id), document.getElementById("d" + id)]) {
        if (stray && !stray.closest(".diagram")) stray.remove();
      }
    }
  }

  function diagramFallback(graph, lit, reason) {
    return el("div", { class: "diagram-fallback" }, [
      el("p", { class: "muted", text: reason }),
      el("p", {}, [
        "Current node: ",
        lit.length ? el("strong", { class: "current-node", text: lit.join(", ") }) : el("span", { class: "muted", text: "none" }),
      ]),
      el("pre", { class: "lines diagram-text", text: graph.mermaid }),
    ]);
  }

  function drawDiagram(holder) {
    const graph = state.graphs.get(holder.dataset.graph);
    if (!graph) {
      holder.replaceChildren(el("p", { class: "muted", text: "No diagram is registered for this graph." }));
      return;
    }
    const lit = list(JSON.parse(holder.dataset.lit || "[]"));
    const text = litText(graph, lit, holder.dataset.status);
    const cached = state.svgCache.get(text);
    if (cached) {
      holder.replaceChildren(cached.cloneNode(true));
      holder.dataset.drawn = "svg";
      return;
    }
    if (state.mermaid !== "ready") {
      const reason = state.mermaid === "loading" ? "Loading the diagram renderer; the graph as text meanwhile." : "Mermaid did not load, so the graph is shown as text.";
      holder.replaceChildren(diagramFallback(graph, lit, reason));
      holder.dataset.drawn = "text";
      return;
    }
    holder.replaceChildren(el("p", { class: "muted", text: "Drawing the graph..." }));
    renderMermaid(text).then(
      (svg) => {
        if (state.svgCache.size > 24) state.svgCache.clear();
        state.svgCache.set(text, svg);
        if (!holder.isConnected) return;
        holder.replaceChildren(svg.cloneNode(true));
        holder.dataset.drawn = "svg";
      },
      (err) => {
        if (!holder.isConnected) return;
        const reason = "Mermaid could not draw this graph (" + (err && err.message ? err.message : "unknown error") + "), so it is shown as text.";
        holder.replaceChildren(diagramFallback(graph, lit, reason));
        holder.dataset.drawn = "text";
      },
    );
  }

  function renderCurrentDiagrams() {
    for (const holder of document.querySelectorAll(".diagram[data-graph]")) drawDiagram(holder);
  }

  function graphCard(summary) {
    const graph = state.graphs.get(summary.graph);
    const lit = graph ? list(summary.next_nodes).filter((n) => graph.nodes.includes(n)) : [];
    const ended = !list(summary.next_nodes).length;
    const caption = el("p", { class: "meta" }, [
      mono(summary.graph),
      " · ",
      lit.length ? ["current: ", el("strong", { text: lit.join(", ") })] : ended ? "no next node (the thread ended or has not started)" : "next: " + list(summary.next_nodes).join(", "),
    ].flat());
    const holder = el("div", {
      class: "diagram",
      "data-graph": summary.graph,
      "data-lit": JSON.stringify(lit),
      "data-status": summary.status,
    });
    return card("Graph", [caption, holder], "graph-card");
  }

  // ---------------------------------------------------------------- the threads list

  function groupBy(items, keyOf) {
    const groups = new Map();
    for (const item of items) {
      const k = keyOf(item);
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k).push(item);
    }
    return groups;
  }

  function orderWithChildren(threads) {
    const ids = new Set(threads.map((t) => t.thread_id));
    const children = groupBy(
      threads.filter((t) => t.parent_thread && ids.has(t.parent_thread)),
      (t) => t.parent_thread,
    );
    const out = [];
    for (const t of threads) {
      if (t.parent_thread && ids.has(t.parent_thread)) continue;
      out.push([t, 0]);
      const kids = (children.get(t.thread_id) || []).slice();
      kids.sort((a, b) => a.thread_id.localeCompare(b.thread_id, undefined, { numeric: true }));
      for (const kid of kids) out.push([kid, 1]);
    }
    return out;
  }

  function threadRows(t, depth) {
    const ref = t.root + "/" + t.thread_id;
    const rowClass = "row st-" + tok(t.status) + (depth ? " child" : "") + (t.status === "error" ? " muted-row" : "");
    const statusCell = badge(t.status);
    if (t.status === "crashed" && t.crash) statusCell.setAttribute("title", t.crash);
    if (t.status === "waiting" && t.waiting && t.waiting.question) statusCell.setAttribute("title", t.waiting.question);
    const rows = [
      el("tr", { class: rowClass, "data-thread": ref }, [
        td([
          el("a", { class: "tid", href: threadHref(t.root, t.thread_id), text: t.thread_id }),
          t.title ? el("div", { class: "title", text: t.title }) : null,
        ], "c-thread"),
        td(t.kind, "c-kind"),
        td(statusCell, "c-status"),
        td([el("span", { class: "phase", text: dash(t.phase) }), el("span", { class: "graph", text: t.graph })], "c-phase"),
        td(dash(t.exit_reason), "c-exit"),
        td(timeEl(t.updated_at), "c-updated"),
        td(usd(t.usd), "c-cost num"),
      ]),
    ];
    let sub = null;
    if (t.status === "waiting" || t.waiting) sub = waitingBox(t);
    else if (t.status === "crashed") sub = crashBox(t, "crash:" + ref);
    else if (t.status === "error") sub = errorBox(t);
    if (sub) {
      rows.push(
        el("tr", { class: "sub" + (depth ? " child" : "") + (t.status === "error" ? " muted-row" : ""), "data-sub-for": ref },
          el("td", { colspan: "7" }, el("div", { class: "sub-inner" }, sub))),
      );
    }
    return rows;
  }

  const LIST_HEADERS = [
    { text: "Thread", cls: "c-thread" },
    { text: "Kind", cls: "c-kind" },
    { text: "Status", cls: "c-status" },
    { text: "Phase · graph", cls: "c-phase" },
    { text: "Exit", cls: "c-exit" },
    { text: "Updated", cls: "c-updated" },
    { text: "Cost", cls: "c-cost num" },
  ];

  function rootSection(root, threads) {
    const section = el("section", { class: "root-section", "data-root": root.name }, [
      el("h2", { class: "root-name" }, [
        el("span", { text: root.name }),
        root.path ? el("span", { class: "root-path", text: root.path }) : null,
      ]),
    ]);
    if (root.present === false) {
      section.append(el("p", { class: "muted", text: "No checkpoint DB in this workspace yet." }));
      return section;
    }
    if (!threads.length) {
      section.append(el("p", { class: "muted", text: "No threads." }));
      return section;
    }
    const groups = groupBy(threads, (t) => t.product || "");
    const names = [...groups.keys()].sort((a, b) => (a === "" ? 1 : 0) - (b === "" ? 1 : 0));
    const tableNode = el("table", { class: "grid threads" }, [
      el("thead", {}, el("tr", {}, LIST_HEADERS.map((h) => el("th", { scope: "col", class: h.cls || null, text: h.text })))),
    ]);
    for (const name of names) {
      const tbody = el("tbody", { "data-product": name || "none" }, [
        el("tr", { class: "group" }, el("th", { colspan: "7", scope: "rowgroup" }, el("span", { class: "group-label" }, [
          name ? "product " : "",
          el("strong", { text: name || "no product" }),
        ]))),
      ]);
      for (const [t, depth] of orderWithChildren(groups.get(name))) add(tbody, threadRows(t, depth));
      tableNode.append(tbody);
    }
    section.append(el("div", { class: "table-wrap" }, tableNode));
    return section;
  }

  function statusSummary(threads) {
    const counts = new Map();
    for (const t of threads) counts.set(t.status, (counts.get(t.status) || 0) + 1);
    const order = ["waiting", "crashed", "halted", "in_progress", "done", "error"];
    const chips = order.filter((s) => counts.has(s)).map((s) => el("span", { class: "chip" }, [badge(s), " " + counts.get(s)]));
    const roots = new Set(threads.map((t) => t.root));
    return el("p", { class: "summary-line" }, [
      el("span", { text: threads.length + (threads.length === 1 ? " thread" : " threads") + " in " + Math.max(roots.size, state.roots.length) + " workspace(s)" }),
      ...chips,
    ]);
  }

  function buildList() {
    const frag = document.createDocumentFragment();
    frag.append(el("h1", { class: "page-title", text: "Threads" }));
    frag.append(statusSummary(state.threads));
    const byRoot = groupBy(state.threads, (t) => t.root);
    const roots = state.roots.slice();
    for (const name of byRoot.keys()) {
      if (!roots.some((r) => r.name === name)) roots.push({ name: name, path: "", present: true });
    }
    if (!roots.length) frag.append(el("p", { class: "muted", text: "No workspaces. Start the dashboard with --root <dir>." }));
    for (const root of roots) frag.append(rootSection(root, byRoot.get(root.name) || []));
    return frag;
  }

  // ---------------------------------------------------------------- one thread

  // summary.usd counts finished runs (the figure scio report prints); the attempts still
  // running have spent running_usd on top of it
  function runningNote(detail) {
    const running = Number(detail.running_usd);
    if (!(running > 0)) return null;
    return el("span", { class: "running-usd", text: " + " + usd(running) + " running" });
  }

  function headerCard(detail) {
    const s = detail.summary;
    const pairs = [
      ["Status", badge(s.status)],
      ["Phase", el("span", { class: "head-phase", text: dash(s.phase) })],
      ["Exit", dash(s.exit_reason)],
      ["Updated", timeEl(s.updated_at)],
      ["Cost", [el("span", { class: "head-cost", text: usd(s.usd) }), runningNote(detail)]],
    ];
    if (s.parent_thread) {
      pairs.push(["Build", el("a", { class: "parent-link", href: threadHref(s.root, s.parent_thread), text: s.parent_thread })]);
    }
    const head = el("section", { class: "card head st-" + tok(s.status), "data-thread": s.root + "/" + s.thread_id }, [
      el("p", { class: "eyebrow" }, [s.kind + " thread", " · ", s.root, s.product ? " · product " + s.product : ""]),
      el("h1", { class: "thread-title", text: s.title || s.thread_id }),
      el("p", { class: "tid-line" }, mono(s.thread_id)),
      facts(pairs),
    ]);
    if (s.waiting || s.status === "waiting") head.append(waitingBox(s));
    if (s.status === "crashed") head.append(crashBox(s, "crash:head"));
    if (s.error) head.append(errorBox(s));
    return head;
  }

  function timelineCard(detail) {
    let items = list(detail.timeline);
    if (!items.length && detail.task) items = list(detail.task.timeline).map((r) => ({ at: r.at, source: r.source, kind: "", text: r.text }));
    if (!items.length) return card("Timeline", el("p", { class: "muted", text: "No entries yet." }), "timeline-card");
    const rows = items.map((item) =>
      el("tr", { class: "k-" + tok(item.kind) }, [
        td(clockEl(item.at), "c-time"),
        td(dash(item.source), "c-source"),
        td(item.kind ? el("span", { class: "kind k-" + tok(item.kind), text: item.kind }) : "", "c-kindcol"),
        td(item.text || "", "c-text"),
      ]),
    );
    return card("Timeline (" + items.length + ")", table([{ text: "Time" }, { text: "Source" }, { text: "Kind" }, { text: "Entry" }], rows, { cls: "timeline" }), "timeline-card");
  }

  function noReport(summary) {
    if (summary.status === "error") return null;
    return card(null, el("p", { class: "muted", text: "No report for this thread yet: it shows its summary only." }));
  }

  // ---- build

  function progressCard(detail) {
    const b = detail.build;
    const p = detail.progress;
    const milestone = b && b.current_milestone ? b.current_milestone : null;
    const title = el("h2", {}, [
      milestone ? milestone + ": " : "",
      detail.milestone_title || (milestone ? "" : "No milestone planned yet"),
    ]);
    const doneMilestones = b ? list(b.milestones_done) : p ? list(p.milestones).filter((m) => m.status === "done").map((m) => m.id) : [];
    const pairs = [["Milestones done", doneMilestones.length ? doneMilestones.join(", ") : "none yet"]];
    if (p) {
      const features = list(p.features);
      const done = features.filter((f) => f.status === "done").length;
      pairs.push(["Features done", done + " of " + features.length]);
      if (p.stack) pairs.push(["Stack", p.stack]);
    }
    if (milestone || detail.milestone_features.length) pairs.push(["This milestone", list(detail.milestone_features).join(", ") || "—"]);
    return el("section", { class: "card progress-card" }, [title, facts(pairs), featuresTable(detail)]);
  }

  function featuresTable(detail) {
    const b = detail.build;
    const rows = b ? list(b.features) : [];
    const links = list(detail.feature_threads);
    const descriptions = new Map(detail.progress ? list(detail.progress.features).map((f) => [f.id, f.description]) : []);
    const ids = list(detail.milestone_features).slice();
    for (const id of [...rows.map((r) => r.feature_id), ...links.map((l) => l.feature_id)]) if (!ids.includes(id)) ids.push(id);
    if (!ids.length) return el("p", { class: "muted", text: "No features planned yet." });
    const root = detail.summary.root;
    const threadLine = (threadId, link) => {
      if (!threadId) return el("div", { class: "feature-thread muted", text: "not run" });
      if (link && link.present) return el("div", { class: "feature-thread" }, el("a", { class: "feature-link", href: threadHref(root, threadId), text: threadId }));
      return el("div", { class: "feature-thread muted", title: "no checkpoint for this thread in this workspace", text: threadId });
    };
    // the feature's thread sits in its cell, so the table stays narrow enough to show it
    const featureCell = (id, threadId, link) => [
      el("strong", { text: id }),
      descriptions.get(id) ? el("div", { class: "title", text: descriptions.get(id) }) : null,
      threadLine(threadId, link),
    ];
    const featureCost = detail.cost_by_feature && typeof detail.cost_by_feature === "object" ? detail.cost_by_feature : {};
    const out = [];
    for (const id of ids) {
      const attempts = rows.filter((r) => r.feature_id === id).sort((a, b2) => a.attempt - b2.attempt);
      const linksFor = links.filter((l) => l.feature_id === id);
      const linkOf = (r) => linksFor.find((l) => l.attempt === r.attempt && l.thread_id === r.thread_id) || linksFor.find((l) => l.thread_id === r.thread_id);
      // a link with no FeatureRow is an attempt still running: SCIO records a run when it ends
      const unfinished = linksFor.filter((l) => !attempts.some((r) => linkOf(r) === l));
      const entries = [...attempts.map((r) => ({ row: r, attempt: r.attempt })), ...unfinished.map((l) => ({ link: l, attempt: l.attempt }))];
      entries.sort((a, b2) => a.attempt - b2.attempt);
      // cost_by_feature spans every attempt; what the finished rows do not account for is the
      // unfinished attempts' spend so far
      const finishedUsd = attempts.reduce((sum, r) => sum + (Number(r.usd) || 0), 0);
      const unfinishedUsd = id in featureCost ? Math.max(Number(featureCost[id]) - finishedUsd, 0) : null;
      for (const entry of entries) {
        const r = entry.row;
        if (r) {
          out.push(el("tr", { "data-feature": id, "data-attempt": String(r.attempt) }, [
            td(featureCell(id, r.thread_id, linkOf(r)), "c-feature"),
            td(String(r.attempt), "num"),
            td(dash(r.tier)),
            td(featureBadge(r.status)),
            td(int(r.iterations), "num"),
            td(int(r.escalations_resolved) + "/" + int(r.escalations), "num"),
            td(usd(r.usd), "num c-cost"),
            td(commitEl(r.commit)),
          ]));
          continue;
        }
        const l = entry.link;
        const cost = unfinishedUsd !== null && unfinished.length === 1 ? usd(unfinishedUsd) : "—";
        out.push(el("tr", { "data-feature": id, "data-attempt": String(l.attempt), class: "unfinished" }, [
          td(featureCell(id, l.thread_id, l), "c-feature"),
          td(String(l.attempt), "num"),
          td("—"),
          td(featureBadge(l.status)),
          td("—", "num"),
          td("—", "num"),
          td(cost, "num c-cost"),
          td("—"),
        ]));
      }
      if (!entries.length) {
        out.push(el("tr", { "data-feature": id, class: "not-run" }, [
          td(featureCell(id, null, null), "c-feature"),
          td("—", "num"),
          td("—"),
          td(featureBadge("not run")),
          td("—", "num"),
          td("—", "num"),
          td("—", "num"),
          td("—"),
        ]));
      }
    }
    return table(
      [
        { text: "Feature and thread" },
        { text: "Attempt", cls: "num" },
        { text: "Tier" },
        { text: "Status" },
        { text: "Iterations", cls: "num" },
        { text: "Escalations", cls: "num", title: "resolved / total" },
        { text: "Cost", cls: "num" },
        { text: "Commit" },
      ],
      out,
      { cls: "features" },
    );
  }

  function integrationCard(detail) {
    const ok = detail.integration_ok;
    const status = ok === true ? featureBadge("passed") : ok === false ? featureBadge("failed") : el("span", { class: "muted", text: "not run yet" });
    let pr;
    if (detail.pr_url) pr = externalLink(detail.pr_url, detail.pr_number ? "PR #" + detail.pr_number : null);
    else if (detail.pr_number) pr = el("span", { text: "#" + detail.pr_number });
    else pr = el("span", { class: "muted", text: "no PR yet" });
    const lines = list(detail.integration);
    return card("Integration", [
      facts([["Checks", status], ["Pull request", pr]]),
      lines.length ? pre(lines, "integration-lines") : el("p", { class: "muted", text: "The integration checks have not run." }),
    ], "integration-card");
  }

  function costCard(detail, extraFacts, extraBars, extraLabel) {
    const s = detail.summary;
    const parts = [
      el("p", { class: "total" }, [el("span", { class: "total-label", text: "Total " }), el("strong", { class: "total-cost", text: usd(s.usd) }), runningNote(detail)]),
    ];
    if (extraFacts) parts.push(facts(extraFacts));
    parts.push(el("h3", { text: "By role" }), bars(detail.cost_by_role, "Cost by role", extraLabel));
    if (extraBars) parts.push(...extraBars);
    return card("Cost", parts, "cost-card");
  }

  function buildSections(detail) {
    const b = detail.build;
    const main = [progressCard(detail), integrationCard(detail)];
    if (b && list(b.human_notes).length) main.push(card("Human notes", el("ul", { class: "notes" }, list(b.human_notes).map((n) => el("li", { text: n })))));
    main.push(timelineCard(detail));
    const side = [
      graphCard(detail.summary),
      costCard(
        detail,
        b ? [["Orchestrator", usd(b.orchestrator_usd)], ["Features", usd(b.features_usd)]] : null,
        [el("h3", { text: "By feature" }), bars(detail.cost_by_feature, "Cost by feature")],
      ),
    ];
    return [side, main];
  }

  // ---- task (a scio run, or a feature run inside a build)

  function taskSections(detail) {
    const t = detail.task;
    const s = detail.summary;
    const main = [];
    if (t) {
      const pairs = [
        ["Title", t.title || "—"],
        ["Tier", dash(t.tier)],
        ["Phase", dash(t.phase)],
        ["Exit", dash(t.exit_reason)],
        ["Iterations", int(t.iterations)],
        ["Tool calls", int(t.tool_calls)],
        ["Branch", t.branch ? mono(t.branch) : "—"],
        ["PR", t.pr_number ? "#" + t.pr_number : "—"],
        ["Tokens", int(t.tokens_used) + " of " + int(t.tokens_max)],
        ["Budget", usd(t.usd_used) + " of " + usd(t.usd_max)],
        ["Advisor calls", dash(t.advisor_calls)],
        ["Started", t.started_at ? timeEl(t.started_at) : "—"],
        ["Finished", t.finished_at ? timeEl(t.finished_at) : "—"],
      ];
      const run = card("Run", [facts(pairs)], "run-card");
      if (list(t.features).length) {
        run.append(el("h3", { text: "Features" }), el("ul", { class: "notes" }, list(t.features).map((f) => el("li", { text: f }))));
      }
      main.push(run);
      const escalations = list(t.escalations);
      main.push(card("Escalations", escalations.length
        ? table(
          [{ text: "Id" }, { text: "Trigger" }, { text: "Step" }, { text: "Verdict" }, { text: "Outcome" }, { text: "Tokens", cls: "num" }, { text: "Cost", cls: "num" }],
          escalations.map((e) => el("tr", {}, [
            td(mono(e.id)), td(dash(e.trigger)), td(dash(e.step_id)), td(dash(e.verdict)), td(dash(e.outcome)), td(int(e.tokens), "num"), td(usd(e.usd), "num"),
          ])),
          { cls: "escalations" },
        )
        : el("p", { class: "muted", text: "No escalations." }), "escalations-card"));
    } else {
      const none = noReport(s);
      if (none) main.push(none);
    }
    const verify = list(detail.verify_lines);
    main.push(card("Verify output", verify.length ? pre(verify, "verify-lines") : el("p", { class: "muted", text: "No verify output." }), "verify-card"));
    main.push(timelineCard(detail));
    const roleRows = t ? list(t.by_role) : [];
    const roleTable = roleRows.length
      ? [el("h3", { text: "Calls and tokens" }), table(
        [{ text: "Role" }, { text: "Calls", cls: "num" }, { text: "Input", cls: "num" }, { text: "Output", cls: "num" }, { text: "Cache read", cls: "num" }, { text: "Cost", cls: "num" }],
        roleRows.map((r) => el("tr", { "data-role": r.role }, [
          td(r.role), td(int(r.calls), "num"), td(int(r.input_tokens), "num"), td(int(r.output_tokens), "num"), td(int(r.cache_read_tokens), "num"), td(usd(r.usd), "num"),
        ])),
        { cls: "roles" },
      )]
      : [];
    const side = [graphCard(s), costCard(detail, null, roleTable)];
    return [side, main];
  }

  // ---- bootstrap (a scio new)

  function bootstrapSections(detail) {
    const r = detail.bootstrap;
    const s = detail.summary;
    const main = [];
    if (r) {
      main.push(card("Product", facts([
        ["Product", dash(r.product || s.product)],
        ["Phase", dash(r.phase)],
        ["Exit", dash(r.exit_reason)],
        ["Features", int(r.features)],
        ["Revisions", int(r.revisions)],
        ["Bootstrap commit", commitEl(r.bootstrap_commit)],
        ["Repository", r.repo_url ? externalLink(r.repo_url) : "—"],
        ["Directory", r.target_dir ? mono(r.target_dir) : "—"],
      ]), "product-card"));
    } else {
      const none = noReport(s);
      if (none) main.push(none);
    }
    const questions = list(detail.questions);
    main.push(card("Interview questions", questions.length
      ? el("ol", { class: "questions" }, questions.map((q) => el("li", { text: q })))
      : el("p", { class: "muted", text: "No questions asked." }), "questions-card"));
    const notes = list(detail.revision_notes);
    main.push(card("Revision rounds", notes.length
      ? el("ul", { class: "revisions" }, notes.map((n, i) => el("li", {}, [el("strong", { text: "Round " + (i + 1) + ": " }), el("span", { text: n })])))
      : el("p", { class: "muted", text: "The first draft was approved; no revision rounds." }), "revisions-card"));
    main.push(timelineCard(detail));
    const calls = r && r.calls_by_role ? r.calls_by_role : {};
    const callsLabel = (role) => (role in calls ? calls[role] + (calls[role] === 1 ? " call" : " calls") : "");
    const side = [graphCard(s), costCard(detail, null, null, callsLabel)];
    return [side, main];
  }

  function buildThreadView(detail) {
    const s = detail.summary;
    const frag = document.createDocumentFragment();
    frag.append(headerCard(detail));
    let sections;
    if (s.kind === "build") sections = buildSections(detail);
    else if (s.kind === "bootstrap") sections = bootstrapSections(detail);
    else sections = taskSections(detail);
    const [side, main] = sections;
    frag.append(el("div", { class: "cols kind-" + tok(s.kind) }, [el("div", { class: "col-side" }, side), el("div", { class: "col-main" }, main)]));
    return frag;
  }

  // ---------------------------------------------------------------- rendering

  function setCrumbs(items) {
    const nav = document.getElementById("crumbs");
    const parts = [];
    items.forEach((item, i) => {
      if (i) parts.push(el("span", { class: "sep", "aria-hidden": "true", text: "›" }));
      parts.push(item.href ? el("a", { href: item.href, text: item.text }) : el("span", { "aria-current": "page", text: item.text }));
    });
    nav.replaceChildren(...parts);
  }

  function openDetailsKeys() {
    return new Set([...document.querySelectorAll("#app details[open][data-key]")].map((d) => d.dataset.key));
  }

  function restoreDetails(keys) {
    for (const node of document.querySelectorAll("#app details[data-key]")) if (keys.has(node.dataset.key)) node.open = true;
  }

  function selectingInMain() {
    const selection = window.getSelection();
    return Boolean(selection && !selection.isCollapsed && selection.anchorNode && document.getElementById("app").contains(selection.anchorNode));
  }

  // A live redraw would drop text being selected (a resume command to copy): it waits until
  // the selection goes, the window loses focus, or DEFER_MAX_MS pass, never longer.
  function deferRedraw() {
    state.deferred = true;
    if (!state.deferTimer) state.deferTimer = window.setTimeout(flushDeferred, DEFER_MAX_MS);
  }

  function flushDeferred() {
    window.clearTimeout(state.deferTimer);
    state.deferTimer = null;
    if (!state.deferred) return;
    state.forceRedraw = true;
    try {
      renderCurrent(true);
    } finally {
      state.forceRedraw = false;
    }
  }

  function replaceMain(content, keepScroll) {
    const main = document.getElementById("app");
    if (keepScroll && !state.forceRedraw && selectingInMain()) {
      deferRedraw();
      return;
    }
    state.deferred = false;
    window.clearTimeout(state.deferTimer);
    state.deferTimer = null;
    const y = window.scrollY;
    const keys = keepScroll ? openDetailsKeys() : new Set();
    const active = document.activeElement;
    const focusHref = keepScroll && active && main.contains(active) ? active.getAttribute("href") : null;
    main.replaceChildren(content);
    restoreDetails(keys);
    if (keepScroll) window.scrollTo(0, y);
    if (focusHref) {
      const again = main.querySelector('a[href="' + CSS.escape(focusHref) + '"]');
      if (again) again.focus({ preventScroll: true });
    }
    renderCurrentDiagrams();
  }

  function renderList(keepScroll) {
    document.title = "SCIO dashboard";
    setCrumbs([{ text: "Threads" }]);
    replaceMain(buildList(), keepScroll);
  }

  function renderThread(keepScroll) {
    const route = state.route;
    const k = key(route.root, route.id);
    const d = state.detail;
    document.title = route.id + " · SCIO dashboard";
    const crumbs = [{ text: "Threads", href: "#/" }, { text: route.root }];
    if (d && d.key === k && d.data && d.data.summary.parent_thread) {
      crumbs.push({ text: d.data.summary.parent_thread, href: threadHref(route.root, d.data.summary.parent_thread) });
    }
    crumbs.push({ text: route.id });
    setCrumbs(crumbs);
    if (!d || d.key !== k) {
      replaceMain(el("p", { class: "muted", text: "Loading the thread..." }), false);
      return;
    }
    if (d.error) {
      const message = d.status === 404 ? "This workspace has no thread with that id (it may have been deleted)." : "Could not load this thread: " + d.error;
      replaceMain(card(null, [el("p", { class: "error-box", text: message }), el("p", {}, el("a", { href: "#/", text: "Back to all threads" }))]), keepScroll);
      return;
    }
    replaceMain(buildThreadView(d.data), keepScroll);
  }

  function renderCurrent(keepScroll) {
    if (state.route.view === "thread") renderThread(keepScroll);
    else renderList(keepScroll);
  }

  // ---------------------------------------------------------------- data

  async function getJSON(path) {
    const res = await fetch(path, { cache: "no-store", headers: { Accept: "application/json" } });
    if (!res.ok) {
      const err = new Error(path + " answered " + res.status);
      err.status = res.status;
      throw err;
    }
    return res.json();
  }

  async function loadGraphs() {
    const graphs = await getJSON("/api/graphs");
    state.graphs = new Map(list(graphs).map((g) => [g.name, g]));
    state.graphsLoaded = true;
  }

  async function loadDetail(route) {
    const k = key(route.root, route.id);
    try {
      const data = await getJSON(threadPath(route.root, route.id));
      return { key: k, data: data };
    } catch (err) {
      return { key: k, error: err.message || String(err), status: err.status || 0 };
    }
  }

  function openThreadAffected(keys, before) {
    const route = state.route;
    if (route.view !== "thread") return false;
    const open = key(route.root, route.id);
    if (keys.has(open)) return true;
    const byKey = new Map();
    for (const t of [...before, ...state.threads]) byKey.set(key(t.root, t.thread_id), t);
    for (const k of keys) {
      const t = byKey.get(k);
      if (t && t.parent_thread && key(t.root, t.parent_thread) === open) return true;
    }
    return false;
  }

  async function refreshOnce(req) {
    const before = state.threads;
    const [roots, threads] = await Promise.all([getJSON("/api/roots"), getJSON("/api/threads")]);
    state.roots = list(roots);
    state.threads = list(threads);
    if (!state.graphsLoaded) {
      try {
        await loadGraphs();
      } catch (err) {
        // the diagrams say so; the next refresh asks again
      }
    }
    const route = state.route;
    if (route.view !== "thread") {
      renderList(true);
      return;
    }
    const k = key(route.root, route.id);
    const stale = !state.detail || state.detail.key !== k;
    if (!(req.all || stale || openThreadAffected(req.keys, before))) return;
    const detail = await loadDetail(route);
    if (state.route.view !== "thread" || key(state.route.root, state.route.id) !== detail.key) return;
    state.detail = detail;
    renderThread(!stale);
  }

  function requestRefresh(req) {
    const pending = state.pending || { all: false, keys: new Set() };
    if (req.all) pending.all = true;
    for (const k of req.keys || []) pending.keys.add(k);
    state.pending = pending;
    document.documentElement.dataset.refresh = "busy";
    if (!state.refreshing) runRefreshes();
  }

  async function runRefreshes() {
    state.refreshing = true;
    try {
      while (state.pending) {
        const req = state.pending;
        state.pending = null;
        try {
          await refreshOnce(req);
          showNotice(null);
        } catch (err) {
          showNotice("Could not refresh (" + (err && err.message ? err.message : String(err)) + "); showing the last data.");
        }
      }
    } finally {
      state.refreshing = false;
      document.documentElement.dataset.refresh = "idle";
    }
  }

  function showNotice(text) {
    const notice = document.getElementById("notice");
    notice.hidden = !text;
    notice.textContent = text || "";
  }

  // ---------------------------------------------------------------- live updates

  const live = { socket: null, attempt: 0, timer: null };

  function setConn(name, detail) {
    const conn = document.getElementById("conn");
    conn.dataset.state = name;
    conn.querySelector(".conn-label").textContent = name;
    conn.title = "Live updates: " + name + (detail ? " (" + detail + ")" : "");
  }

  function refsOf(value) {
    return list(value)
      .filter((r) => r && typeof r.root === "string" && typeof r.thread_id === "string")
      .map((r) => key(r.root, r.thread_id));
  }

  function connect() {
    live.timer = null;
    const scheme = location.protocol === "https:" ? "wss:" : "ws:";
    let socket;
    try {
      socket = new WebSocket(scheme + "//" + location.host + "/ws");
    } catch (err) {
      scheduleReconnect();
      return;
    }
    live.socket = socket;
    let greeted = false;
    socket.addEventListener("message", (event) => {
      let message;
      try {
        message = JSON.parse(event.data);
      } catch (err) {
        return;
      }
      if (!greeted) {
        // the hello: anything may have changed while this socket was down
        greeted = true;
        live.attempt = 0;
        setConn("live");
        requestRefresh({ all: true });
        return;
      }
      requestRefresh({ keys: [...refsOf(message && message.changed), ...refsOf(message && message.removed)] });
    });
    socket.addEventListener("close", () => {
      if (live.socket !== socket) return;
      live.socket = null;
      scheduleReconnect();
    });
  }

  function scheduleReconnect() {
    const delay = Math.min(BACKOFF_START_MS * 2 ** live.attempt, BACKOFF_MAX_MS);
    live.attempt += 1;
    const offline = delay >= BACKOFF_MAX_MS || navigator.onLine === false;
    setConn(offline ? "offline" : "reconnecting", "next try in " + Math.round(delay / 1000) + " s");
    live.timer = window.setTimeout(connect, delay);
  }

  // ---------------------------------------------------------------- routing

  function parseRoute() {
    const parts = location.hash.replace(/^#\/?/, "").split("/");
    if (parts[0] === "thread" && parts.length >= 3 && parts[1] && parts[2]) {
      try {
        return { view: "thread", root: decodeURIComponent(parts[1]), id: decodeURIComponent(parts.slice(2).join("/")) };
      } catch (err) {
        return { view: "list" };
      }
    }
    return { view: "list" };
  }

  function onRoute() {
    state.route = parseRoute();
    window.scrollTo(0, 0);
    if (state.route.view === "list") {
      renderList(false);
      return;
    }
    renderThread(false);
    requestRefresh({ keys: [key(state.route.root, state.route.id)] });
  }

  // ---------------------------------------------------------------- start

  setupMermaid();
  window.addEventListener("hashchange", onRoute);
  document.addEventListener("selectionchange", () => {
    if (state.deferred && !selectingInMain()) flushDeferred();
  });
  window.addEventListener("blur", () => {
    if (state.deferred) flushDeferred();
  });
  window.setInterval(tickTimes, 30000);
  if (state.route.view === "thread") renderThread(false);
  requestRefresh({ all: true });
  connect();
})();
