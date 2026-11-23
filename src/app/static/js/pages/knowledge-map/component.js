// The knowledge map's Alpine component (knowledge_map.html), registered as `knowledgeMap` by
// ../knowledge-map.js. `T` are the texts of the data island `km-i18n` in the language of the
// page. The template evaluates on the CSP build of Alpine: every statement, arrow function or
// global it needs is a method here. `doc` and `fetchImpl` are the page's document and fetch.
import { csrfToken } from "../../core/csrf.js";

export function createKnowledgeMap(T, { doc = document, fetchImpl = fetch } = {}) {
  const csrf = () => csrfToken(doc);
  const NS = "http://www.w3.org/2000/svg";
  const W = 1040, NODE_W = 200, NODE_H = 36, TOP = 40;
  const X = { topic: 10, source: 290, access: 570, agent: 830 };
  const STRIP = { topic: "--in", source: "--p", reference: "--n", access: "--s", agent: "--a" };

  async function api(method, path, body) {
    const res = await fetchImpl("/api/v1/knowledge-map" + path, {
      method,
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const d = data.detail;
      const err = new Error(typeof d === "string" ? d : (d && d.message) || res.statusText);
      if (d && Array.isArray(d.problems)) err.problems = d.problems;
      throw err;
    }
    return data;
  }
  function el(tag, attrs, parent) {
    const n = doc.createElementNS(NS, tag);
    for (const k in attrs) n.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(n);
    return n;
  }
  const hostOf = url => { try { return new URL(url).host; } catch (e) { return url || ""; } };
  const visibleTo = (roles, required) =>
    roles === null || !required || !required.length || required.some(r => roles.includes(r));

  return {
    T, tab: "map", entries: [], findings: [], warnings: [], files: [], generatedAt: null,
    server: {}, serverError: "", selected: null, viewAs: "*", filter: "",
    sim: { need: "", role: "*", busy: false, result: null },
    usage: null, history: [], lastEval: null, evalBusy: false,
    suggestions: [], suggestionsLoaded: false, busy: false,
    file: null, draft: "", problems: [], message: "", messageClass: "", jobLink: "",
    gov: { data: null, sub: "findings", edit: null, form: {}, exc: null, busy: false },
    ed: null,

    async start() {
      await this.load();
      api("GET", "/server").then(d => { this.server = d.settings || {}; })
        .catch(e => { this.serverError = e.message; });
    },
    async load() {
      const d = await api("GET", "");
      this.entries = d.entries; this.findings = d.findings.filter(f => f.category !== "governance"); this.warnings = d.warnings;
      this.files = d.files; this.generatedAt = d.generated_at;
      if (this.file) this.openFile(this.files.find(f => f.key === this.file.key));
      this.$nextTick(() => this.draw());
    },

    // ── presentation helpers
    kindLabel(k) { return T["kind_" + k] || k; },
    kindSummary() {
      const c = {}; this.entries.forEach(e => { c[e.kind] = (c[e.kind] || 0) + 1; });
      return Object.entries(c).map(([k, n]) => {
        const forms = T["count_" + k];
        return forms ? forms[n === 1 ? 0 : 1].replace("{n}", n) : n + " " + this.kindLabel(k);
      }).join(" · ");
    },
    highFindings() { return this.findings.filter(f => f.severity === "high").length; },
    sortedFindings() {
      const o = { high: 0, medium: 1, low: 2 };
      return [...this.findings].sort((a, b) => (o[a.severity] ?? 3) - (o[b.severity] ?? 3));
    },
    rolesText(e) { return (e.required_roles || []).length ? e.required_roles.join(", ") : T.all_roles; },
    allRoles() {
      const s = new Set();
      this.entries.forEach(e => { (e.required_roles || []).forEach(r => s.add(r));
        (e.access || []).forEach(p => (p.required_roles || []).forEach(r => s.add(r))); });
      ["user"].forEach(r => s.add(r));
      return [...s].sort();
    },
    filtered() {
      const q = this.filter.toLowerCase();
      return this.entries.filter(e => !q || (e.id + " " + e.system + " " + e.coverage).toLowerCase().includes(q));
    },
    nameOf(id) { const e = this.entries.find(x => x.id === id); return e ? e.system : id; },
    maxOf(o) { return Math.max(1, ...Object.values(o || {})); },
    when(ts) { if (!ts) return "—"; const d = new Date(ts); return isNaN(d) ? ts : d.toLocaleString(doc.documentElement.lang || undefined); },
    pct(v) { return v === null || v === undefined ? "—" : Math.round(v * 100) + " %"; },
    partLabel(p) { return T.parts[p]; },
    evidenceText(s) { const e = s.evidence || {}; return Object.entries(e).map(([k, v]) => k + ": " + v).join(" · "); },
    checkText(s) { const c = s.check || {}; return "Hit@1 " + (c.hit_at_1 || []).map(v => this.pct(v)).join(" → ") + (c.regression ? " ⚠" : " ✓"); },
    yamlish(o) { return JSON.stringify(o, null, 2); },
    selectId(id) { this.selected = this.entries.find(e => e.id === id) || null; this.tab = "map"; this.$nextTick(() => this.draw()); },
    flash(text, cls, jobId) { this.message = text; this.messageClass = cls; this.jobLink = jobId ? "/jobs/" + jobId : ""; },

    // ── what the template computed itself before the CSP build of Alpine
    showEntry(e) { this.selected = e; this.tab = "map"; this.draw(); },
    showUsage() { this.tab = "usage"; this.loadUsage(); },
    showSuggestions() { this.tab = "suggestions"; this.loadSuggestions(); },
    showGovernance() { this.tab = "governance"; this.loadGovernance(); },
    confidenceText(r) { return Number(r.confidence).toFixed(2); },
    recentRuns() { return this.history.slice(-8).reverse(); },
    discardDraft() { this.draft = this.file.content; this.problems = []; },
    subjectsText(r) { return r.data_subjects.map(x => this.lbl("subjects", x)).join(", "); },
    hasProposal(r) { return Object.keys(r.open_proposal).length > 0; },
    exceptionNote(x) { return [x.reason, x.accepted_by_role, x.risk_ref].filter(Boolean).join(" · "); },
    confirmTier(m) { m.choice = m.suggested_tier; return this.saveModel(m); },

    // ── the graph: four lanes, every node placed near what it connects to
    draw() {
      const svg = doc.getElementById("km-graph"); if (!svg) return;
      svg.innerHTML = "";
      const roles = this.viewAs === "*" ? null : [this.viewAs, "user"];
      const routable = this.entries.filter(e => e.kind === "source" || e.kind === "reference");
      const agents = this.entries.filter(e => e.kind === "agent");
      const servers = this.entries.filter(e => e.kind === "mcp");
      const topics = new Map();
      routable.forEach(e => e.topics.forEach(t => topics.set(t.id, t.label)));
      // access nodes: one per (via, target) — the wiki over MCP and over RAG are two
      const access = new Map();
      const accKey = p => p.via + ":" + (p.target || p.location || "") + (p.collection ? "/" + p.collection : "");
      const addAcc = (p, from) => {
        if (p.via === "none") return null;
        const k = accKey(p);
        if (!access.has(k)) {
          const server = servers.find(s => (s.access || []).some(a => a.target === p.target));
          access.set(k, { id: "a:" + k, kind: "access", via: p.via, label: p.via.toUpperCase() + " · " + (p.via === "a2a" ? hostOf(p.target) : (p.target || "")),
            sub: p.collection || (server && server.undeclared ? T.undeclared : ""), undeclared: !!(server && server.undeclared),
            system: server ? server.system : p.target, coverage: server ? server.coverage : "", access: [p], from: [], via_roles: [] });
        }
        if (from) { access.get(k).from.push(from); access.get(k).via_roles.push(p.required_roles || []); }
        return access.get(k);
      };
      const nodes = [], edges = [], pos = {};
      [...topics].forEach(([id, label], i) => { const n = { id: "t:" + id, lane: "topic", label }; nodes.push(n); pos[n.id] = { x: X.topic, y: TOP + i * (NODE_H + 10) }; });
      const meanY = ids => { const ys = ids.map(id => pos[id] && pos[id].y).filter(v => v !== undefined); return ys.length ? ys.reduce((a, b) => a + b, 0) / ys.length : Infinity; };
      // Each node sits level with what it connects to; a node with nothing to line up
      // with (an undescribed server, an Agent2Agent agent) goes below the others.
      const place = (list, x, anchor, gap) => {
        let last = TOP - gap;
        list.map(n => ({ n, y: anchor(n) })).sort((a, b) => a.y - b.y)
          .forEach(w => { const y = Number.isFinite(w.y) ? Math.max(w.y, last + gap) : last + gap; pos[w.n.id] = { x, y }; last = y; });
      };
      const srcNodes = routable.map(e => ({ ...e, lane: e.kind === "reference" ? "reference" : "source", entry: e }));
      place(srcNodes, X.source, n => meanY(n.topics.map(t => "t:" + t.id)), NODE_H + 18);
      srcNodes.forEach(n => {
        n.topics.forEach(t => edges.push({ a: "t:" + t.id, b: n.id, cls: (n.source_of_truth_for || []).includes(t.id) ? "sot" : "", roles: n.required_roles }));
        (n.access || []).forEach(p => { const a = addAcc(p, n.id); if (a) edges.push({ a: n.id, b: a.id, cls: "", roles: p.required_roles && p.required_roles.length ? p.required_roles : n.required_roles }); });
        (n.not_covered || []).forEach(nc => { if (nc.instead && routable.some(r => r.id === nc.instead)) edges.push({ a: n.id, b: nc.instead, cls: "instead" }); });
      });
      agents.forEach(g => (g.access || []).forEach(p => addAcc(p)));
      // A connected server no source reaches still gets its box; one a source already
      // reaches (over MCP or as the vector index) is that box.
      servers.forEach(s => (s.access || []).forEach(p => {
        if (![...access.values()].some(a => a.access[0].target === p.target)) addAcc(p);
      }));
      const accNodes = [...access.values()].map(a => ({ ...a, lane: "access" }));
      place(accNodes, X.access, n => n.from.length ? meanY(n.from) : Infinity, NODE_H + 12);
      const agNodes = agents.map(g => ({ ...g, lane: "agent", entry: g }));
      agNodes.forEach(g => {
        const linked = accNodes.filter(a => (g.uses || []).includes(a.access[0].target) || (g.access || []).some(p => accKey(p) === a.id.slice(2)));
        g.linked = linked.map(a => a.id);
        linked.forEach(a => edges.push({ a: a.id, b: g.id, cls: "", roles: g.required_roles }));
      });
      place(agNodes, X.agent, n => meanY(n.linked), NODE_H + 24);
      nodes.push(...srcNodes, ...accNodes, ...agNodes);

      const maxY = Math.max(TOP + NODE_H, ...Object.values(pos).map(p => p.y)) + NODE_H + 20;
      svg.setAttribute("viewBox", `0 0 ${W} ${maxY}`);
      [["topic", T.lane_topics], ["source", T.lane_sources], ["access", T.lane_access], ["agent", T.lane_agents]]
        .forEach(([k, label]) => { el("text", { x: X[k], y: 18, class: "lane" }, svg).textContent = label; });
      const gE = el("g", {}, svg), gN = el("g", {}, svg);
      const mid = id => ({ x: pos[id].x, y: pos[id].y + NODE_H / 2 });
      const byId = Object.fromEntries(nodes.map(n => [n.id, n]));
      const vis = n => {
        if (!n) return false;
        if (n.lane === "topic") return srcNodes.some(s => s.topics.some(t => "t:" + t.id === n.id) && visibleTo(roles, s.required_roles));
        if (n.lane === "access") return n.undeclared || n.from.length === 0
          || n.from.some((f, i) => visibleTo(roles, byId[f] && byId[f].required_roles) && visibleTo(roles, n.via_roles[i]));
        return visibleTo(roles, n.required_roles);
      };
      edges.forEach(ed => {
        if (!pos[ed.a] || !pos[ed.b]) return;
        const A = mid(ed.a), B = mid(ed.b); let d;
        if (ed.cls === "instead") { const x = A.x - 2, bend = 50 + Math.abs(B.y - A.y) * 0.15; d = `M${x},${A.y} C${x - bend},${A.y} ${x - bend},${B.y} ${x},${B.y}`; }
        else { const x1 = A.x + NODE_W, dx = (B.x - x1) * 0.5; d = `M${x1},${A.y} C${x1 + dx},${A.y} ${B.x - dx},${B.y} ${B.x},${B.y}`; }
        ed.path = el("path", { d, class: "edge " + ed.cls }, gE);
      });
      const self = this;
      const css = v => "oklch(var(" + v + "))";
      nodes.forEach(n => {
        if (!pos[n.id]) return;
        const g = el("g", { class: "node", transform: `translate(${pos[n.id].x},${pos[n.id].y})`, tabindex: 0, role: "button", "aria-label": n.label || n.system }, gN);
        el("rect", { class: "body", width: NODE_W, height: NODE_H, rx: 7 }, g);
        el("rect", { width: 4, height: NODE_H, rx: 2, fill: css(STRIP[n.lane]) }, g);
        const title = (n.label || n.system || "");
        el("text", { x: 12, y: n.lane === "topic" ? 22 : 15, "font-weight": 600 }, g).textContent = title.length > 28 ? title.slice(0, 27) + "…" : title;
        if (n.lane !== "topic") {
          const sub = n.lane === "reference" ? T.no_tool : n.lane === "access" ? n.sub : n.lane === "agent" ? (n.access && n.access.length ? "A2A" : (n.uses || []).join(", ")) : self.rolesText(n);
          el("text", { x: 12, y: 29, class: "sub" }, g).textContent = (sub || "").slice(0, 34);
        }
        if (n.undeclared) el("circle", { cx: NODE_W - 12, cy: 12, r: 4.5, fill: css("--er") }, g);
        n.g = g;
        const pick = () => { self.selected = n.lane === "topic" ? { id: n.id.slice(2), label: n.label, kind: "topic" } : (n.entry || n); hi(); };
        g.addEventListener("click", pick);
        g.addEventListener("keydown", ev => { if (ev.keyCode === 13 || ev.keyCode === 32) { ev.preventDefault(); pick(); } });
        g.addEventListener("mouseenter", () => hi(n.id));
        g.addEventListener("mouseleave", () => hi());
      });
      function hi(hover) {
        const sel = self.selected && (nodes.find(n => n.entry === self.selected || n.id === self.selected.id) || {}).id;
        const focus = hover || sel;
        nodes.forEach(n => { if (!n.g) return; n.g.classList.toggle("dim", !vis(n)); n.g.classList.toggle("sel", n.id === sel); });
        edges.forEach(ed => { if (!ed.path) return; const shown = vis(byId[ed.a]) && vis(byId[ed.b]) && visibleTo(roles, ed.roles);
          ed.path.classList.toggle("dim", !shown); ed.path.classList.toggle("hi", shown && (ed.a === focus || ed.b === focus)); });
      }
      hi();
    },

    // ── routing test
    async simulate() {
      this.sim.busy = true;
      try { this.sim.result = await api("POST", "/simulate", { need: this.sim.need, roles: this.sim.role === "*" ? null : [this.sim.role, "user"] }); }
      catch (e) { this.flash(T.unreachable.replace("{detail}", e.message), "alert-error"); }
      finally { this.sim.busy = false; }
    },

    // ── usage and gold set
    async loadUsage() {
      try {
        this.usage = await api("GET", "/stats?days=14");
        // The history keeps the figures over all questions of each run; the split into
        // development and held-out questions exists only for a run made on this page.
        this.history = (await api("GET", "/eval/history")).runs || [];
        if (!this.lastEval && this.history.length) this.lastEval = { all: this.history[this.history.length - 1] };
      } catch (e) { this.flash(T.unreachable.replace("{detail}", e.message), "alert-error"); }
    },
    async runEval() {
      this.evalBusy = true;
      try { this.lastEval = await api("POST", "/eval"); await this.loadUsage(); }
      catch (e) { this.flash(T.error.replace("{detail}", e.message), "alert-error"); }
      finally { this.evalBusy = false; }
    },

    // ── suggestions
    async loadSuggestions() {
      try { this.suggestions = ((await api("GET", "/suggestions?days=28")).suggestions || []).map(x => ({ ...x, confirmed: false })); this.suggestionsLoaded = true; }
      catch (e) { this.flash(T.unreachable.replace("{detail}", e.message), "alert-error"); }
    },
    async decide(s, decision) {
      this.busy = true;
      try {
        const r = await api("POST", "/suggestions/" + s.id, { decision, confirm_no_personal_data: !!s.confirmed });
        if (decision === "rejected") { this.flash(T.rejected, "alert-info"); }
        else { await this.follow(r.job_id, T.accepted); }
        this.suggestions = this.suggestions.filter(x => x.id !== s.id);
      } catch (e) { this.flash(T.error.replace("{detail}", e.message), "alert-error"); }
      finally { this.busy = false; }
    },

    // ── operator files
    openFile(f) { if (!f) return; this.file = f; this.draft = f.content; this.problems = []; },
    async checkDraft() {
      if (!this.file) return;
      try { this.problems = (await api("POST", "/files/" + this.file.key + "/check", { content: this.draft })).problems; }
      catch (e) { this.problems = [e.message]; }
    },
    async saveFile() {
      this.busy = true;
      try {
        const r = await api("PUT", "/files/" + this.file.key, { content: this.draft, revision: this.file.revision });
        await this.follow(r.job_id, T.saved);
      } catch (e) { this.flash(T.error.replace("{detail}", e.message), "alert-error"); }
      finally { this.busy = false; }
    },
    // ── governance
    carriesText(s) { return ["example_query", "gold_question", "not_covered"].includes(s.kind); },
    async loadGovernance() {
      try {
        const d = await api("GET", "/governance");
        d.models.forEach(m => { m.choice = m.tier || ""; });
        this.gov.data = d;
      } catch (e) { this.flash(T.error.replace("{detail}", e.message), "alert-error"); }
    },
    govHigh() { return this.gov.data ? this.gov.data.findings.filter(f => f.severity === "high" && !f.exception).length : 0; },
    sevLabel(s) { return T["sev_" + s] || s; },
    lbl(group, v) { return (T[group] && T[group][v]) || v || T.not_set; },
    has(row, gap) { return row.gaps.includes(gap); },
    excText(f) {
      if (f.exception) return T.until.replace("{date}", f.exception.expires);
      return f.expired_exception ? T.expired.replace("{date}", f.expired_exception.expires) : "";
    },
    canAccept(f) { return this.gov.data.exceptable.includes(f.code); },
    resultLabel(r) { return { ok: T.no_action, attention: T.action_needed, not_assessable: T.not_assessable }[r] || r; },
    editEntry(row) {
      const g = row.governance || {};
      this.gov.edit = row;
      this.gov.form = { owner: g.owner || "", classification: g.classification || "", personal_data: g.personal_data || "",
        data_subjects: [...(g.data_subjects || [])], ropa_ref: g.ropa_ref || "" };
    },
    async govWrite(method, path, body, after) {
      this.gov.busy = true;
      try {
        const r = await api(method, "/governance" + path, { revision: this.gov.data.revision, ...body });
        if (after) after();
        await this.follow(r.job_id, T.saved);
      } catch (e) { this.flash(T.error.replace("{detail}", e.message), "alert-error"); }
      finally { this.gov.busy = false; }
    },
    saveEntry() {
      const f = this.gov.form;
      return this.govWrite("PUT", "/entries/" + encodeURIComponent(this.gov.edit.id), {
        owner: f.owner, classification: f.classification || null, personal_data: f.personal_data || null,
        data_subjects: f.data_subjects, ropa_ref: f.ropa_ref }, () => { this.gov.edit = null; });
    },
    confirmProposal(row) { return this.govWrite("POST", "/entries/" + encodeURIComponent(row.id) + "/confirm-proposal", {}); },
    saveModel(m) {
      return this.govWrite("PUT", "/models/" + m.name.split("/").map(encodeURIComponent).join("/"), { tier: m.choice || null });
    },
    keptExceptions() {
      return this.gov.data.exceptions.map(x => ({ finding: x.finding, subject: x.subject, expires: x.expires,
        reason: x.reason || null, accepted_by_role: x.accepted_by_role || null, risk_ref: x.risk_ref || null }));
    },
    acceptRisk(f) {
      // Renewing starts from the exception there is for this finding and subject.
      const old = this.gov.data.exceptions.find(x => x.finding === f.code && x.subject === f.target) || {};
      this.gov.exc = { finding: f.code, subject: f.target, expires: "", reason: old.reason || "",
        accepted_by_role: old.accepted_by_role || "", risk_ref: old.risk_ref || "" };
    },
    saveException() {
      const x = this.gov.exc;
      const kept = this.keptExceptions().filter(k => !(k.finding === x.finding && k.subject === x.subject));
      return this.govWrite("PUT", "/exceptions", { exceptions: [...kept, { ...x, reason: x.reason || null,
        accepted_by_role: x.accepted_by_role || null, risk_ref: x.risk_ref || null }] }, () => { this.gov.exc = null; });
    },
    removeException(i) {
      const kept = this.keptExceptions(); kept.splice(i, 1);
      return this.govWrite("PUT", "/exceptions", { exceptions: kept });
    },

    // ── entry editor: one entry as a form, saved as sources.yaml or overrides.yaml
    blankForm() {
      return { kind: "source", system: "", coverage: "", location: "", delegate_when: "", topics: [], example_queries: [],
        not_covered: [], access: [], required_roles: [], on_denied: "hidden", uses: [], counter_examples: [] };
    },
    toForm(fields) {
      const f = { ...this.blankForm(), ...JSON.parse(JSON.stringify(fields || {})) };
      const sot = f.source_of_truth_for || [];
      f.topics = (f.topics || []).map(t => typeof t === "string" ? { id: t, label: t } : t)
        .map(t => ({ ...t, label: t.label || t.id, sot: sot.includes(t.id) }));
      f.not_covered = (f.not_covered || []).map(n => ({ what: n.what || "", instead: n.instead || "" }));
      f.access = (f.access || []).map(p => ({ ...p }));
      delete f.source_of_truth_for;
      return f;
    },
    fieldsOut() {
      const f = this.ed.f;
      const out = { kind: f.kind, system: f.system, coverage: f.coverage, required_roles: f.required_roles,
        on_denied: f.required_roles.length ? f.on_denied : "hidden" };
      if (f.kind === "reference") { out.location = f.location; }
      if (f.kind === "agent") { out.delegate_when = f.delegate_when; out.uses = f.uses; }
      if (f.kind !== "reference") out.access = f.access.map(p => { const c = { ...p }; if (c.via !== "rag") delete c.collection; return c; });
      if (f.kind !== "agent") {
        out.topics = f.topics.map(t => { const c = { ...t }; delete c.sot; return c; });
        out.source_of_truth_for = f.topics.filter(t => t.sot).map(t => t.id);
        out.example_queries = f.example_queries;
        out.not_covered = f.not_covered.filter(n => n.what.trim());
        out.counter_examples = f.counter_examples;
      }
      if (this.ed.mode === "new") out.id = this.ed.id;
      return out;
    },
    openEditor(state) {
      this.ed = { step: "form", mode: "edit", id: "", origin: "own", declared_by: "", overridden: [], reset: [], revision: "",
        f: this.blankForm(), initial: "", replaces: [], newTopic: "", newQuestion: "", newRole: "", probe: null,
        probeBusy: false, busy: false, confirmDelete: false, showYaml: false, problems: [], idTouched: false, ...state };
      this.ed.initial = JSON.stringify(this.fieldsOut());
      this.tab = "edit";
    },
    async editEntry(id) {
      if (this.ed && this.edDirty() && !this.edDiscardOk()) return;
      try {
        const d = await api("GET", "/entries/" + encodeURIComponent(id));
        this.openEditor({ mode: "edit", id: d.id, origin: d.origin, declared_by: d.declared_by, overridden: d.overridden,
          revision: d.revision, f: this.toForm(d.fields) });
      } catch (e) { this.flash(T.error.replace("{detail}", e.message), "alert-error"); }
    },
    async newEntry(preset) {
      if (this.ed && this.edDirty() && !this.edDiscardOk()) return;
      let revision = "";
      try { revision = (this.files.find(f => f.key === "sources") || {}).revision || ""; } catch (e) { /* no file yet */ }
      this.openEditor({ mode: "new", step: preset ? "form" : "kind", revision, ...(preset || {}) });
    },
    describeServer(e) {
      const target = ((e.access || [])[0] || {}).target || e.system;
      this.newEntry({ f: this.toForm({ kind: "source", system: e.system, access: [{ via: "mcp", target }],
        required_roles: e.required_roles || [] }), replaces: [e.id] });
      this.$nextTick(() => this.autoId());
    },
    pickKind(k) {
      const f = this.blankForm();
      if (k === "server") f.access = [{ via: "mcp", target: "" }];
      if (k === "place") f.kind = "reference";
      if (k === "agent") { f.kind = "agent"; f.access = [{ via: "a2a", target: "" }]; }
      this.ed.f = f; this.ed.step = "form"; this.ed.initial = JSON.stringify(this.fieldsOut());
    },
    // A discarded draft is asked about in the page, never with a browser dialog.
    edDiscardOk() { if (this.ed.discardArmed) return true; this.ed.discardArmed = true; this.tab = "edit";
      this.flash(T.ed_unsaved, "alert-warning"); return false; },
    edDirty() { return !!this.ed && this.ed.step === "form" && (JSON.stringify(this.fieldsOut()) !== this.ed.initial || this.ed.reset.length > 0); },
    closeEditor() { this.ed = null; this.tab = "entries"; },
    autoId() {
      if (!this.ed || this.ed.mode !== "new" || this.ed.idTouched) return;
      const slug = (this.ed.f.system || "").toLowerCase().replace(/ä/g, "ae").replace(/ö/g, "oe").replace(/ü/g, "ue").replace(/ß/g, "ss")
        .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 60);
      const prefix = { reference: "place", agent: "agent" }[this.ed.f.kind] || "source";
      this.ed.id = slug ? prefix + "." + slug : "";
    },
    isChanged(key) {
      if (this.ed.reset.includes(key)) return false;
      if (this.ed.overridden.includes(key)) return true;
      const now = this.fieldsOut(), then = JSON.parse(this.ed.initial);
      return JSON.stringify(now[key]) !== JSON.stringify(then[key]);
    },
    resetField(key) { this.ed.reset.push(key); },
    addTopic() {
      const label = this.ed.newTopic.trim(); if (!label) return;
      const known = this.knownTopics().find(t => t.label.toLowerCase() === label.toLowerCase());
      const id = known ? known.id : label.toLowerCase().replace(/ä/g, "ae").replace(/ö/g, "oe").replace(/ü/g, "ue").replace(/ß/g, "ss").replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
      if (id && !this.ed.f.topics.some(t => t.id === id)) this.ed.f.topics.push({ id, label: known ? known.label : label, sot: false });
      this.ed.newTopic = "";
    },
    addQuestion() { const q = this.ed.newQuestion.trim(); if (q && !this.ed.f.example_queries.includes(q)) this.ed.f.example_queries.push(q); this.ed.newQuestion = ""; },
    addRole() { const r = this.ed.newRole.trim(); if (r && !this.ed.f.required_roles.includes(r)) this.ed.f.required_roles.push(r); this.ed.newRole = ""; },
    knownTopics() { const m = new Map(); this.entries.forEach(e => (e.topics || []).forEach(t => m.set(t.id, t))); return [...m.values()]; },
    knownTargets() { const s = new Set(); this.entries.forEach(e => (e.access || []).forEach(p => { if (p.target) s.add(p.target); })); return [...s].sort(); },
    routableOthers() { return this.entries.filter(e => (e.kind === "source" || e.kind === "reference") && e.id !== this.ed.id); },
    qualityChecks() {
      const f = this.ed.f, words = (f.coverage || "").trim().split(/\s+/).filter(Boolean).length;
      const checks = [
        { ok: !!f.system.trim(), label: T.quality.name },
        { ok: words >= 8, label: T.quality.description },
      ];
      if (f.kind === "agent") {
        checks.push({ ok: !!f.delegate_when.trim(), label: T.quality.hand_over });
        checks.push({ ok: f.access.some(p => p.target), label: T.quality.address });
        return checks;
      }
      checks.push({ ok: f.topics.length > 0, label: T.quality.topic });
      checks.push({ ok: f.example_queries.length >= 3, label: T.quality.examples });
      checks.push(f.kind === "reference" ? { ok: !!f.location.trim(), label: T.quality.place }
        : { ok: f.access.some(p => p.target), label: T.quality.access });
      checks.push({ ok: !!(this.ed.probe && this.ed.probe.results && this.ed.probe.results.every(r => r.draft_place === 0)),
        label: T.quality.probe });
      return checks;
    },
    probeOf(q) { return this.ed.probe && this.ed.probe.results ? this.ed.probe.results.find(r => r.question === q) : null; },
    probeClass(r) { if (r.draft_place === 0) return r.margin !== null && r.margin < 0.1 ? "badge-warning" : "badge-success"; return "badge-error"; },
    probeText(r) {
      if (r.abstention) return T.ed_abstains;
      if (r.draft_place === 0) return r.margin !== null && r.margin < 0.1 ? T.ed_lands_here + " · " + T.ed_close : T.ed_lands_here;
      const top = (r.ranked || [])[0];
      return T.ed_lands_at.replace("{name}", top ? (top.system || this.nameOf(top.source_id)) : "—");
    },
    async runProbe() {
      this.ed.probeBusy = true;
      const fields = { ...this.fieldsOut(), id: this.ed.id || "draft" };
      try { this.ed.probe = await api("POST", "/entries/probe", { fields, questions: this.ed.f.example_queries.slice(0, 10), replaces: this.ed.replaces }); }
      catch (e) { this.flash(T.unreachable.replace("{detail}", e.message), "alert-error"); }
      finally { this.ed.probeBusy = false; }
    },
    yamlPreview() {
      const out = this.fieldsOut();
      const clean = o => Array.isArray(o) ? o.map(clean).filter(v => v !== "" && !(Array.isArray(v) && !v.length))
        : (o && typeof o === "object") ? Object.fromEntries(Object.entries(o).map(([k, v]) => [k, clean(v)])
            .filter(([k, v]) => v !== "" && v !== undefined && !(Array.isArray(v) && !v.length) && !(k === "on_denied" && v === "hidden") && !(k === "kind" && v === "source")))
        : o;
      const y = (v, ind) => {
        const pad = "  ".repeat(ind);
        if (Array.isArray(v)) return v.map(x => (x && typeof x === "object") ? pad + "- " + y(x, ind + 1).trimStart() : pad + "- " + JSON.stringify(x)).join("\n");
        if (v && typeof v === "object") return Object.entries(v).map(([k, x]) => (x && typeof x === "object") ? pad + k + ":\n" + y(x, ind + 1) : pad + k + ": " + JSON.stringify(x)).join("\n");
        return pad + JSON.stringify(v);
      };
      if (this.ed.origin === "own" || this.ed.mode === "new") return "sources:\n" + y([{ id: this.ed.id, ...clean(out) }], 1);
      const then = JSON.parse(this.ed.initial), patch = {};
      Object.keys(out).forEach(k => { if (JSON.stringify(out[k]) !== JSON.stringify(then[k]) && !this.ed.reset.includes(k)) patch[k] = out[k]; });
      return "sources:\n  " + this.ed.id + ":\n" + (Object.keys(patch).length ? y(patch, 2) : "    {}");
    },
    // The form stays open until the render has taken the change: when the render refuses
    // it (the previous file is back), the input is still there to correct.
    async saveEditor() {
      const ed = this.ed;
      ed.busy = true; ed.problems = [];
      try {
        const body = { fields: this.fieldsOut(), reset: ed.reset, revision: ed.revision };
        const r = ed.mode === "new" ? await api("POST", "/entries", body)
          : await api("PUT", "/entries/" + encodeURIComponent(ed.id), body);
        const ok = await this.follow(r.job_id, T.ed_saved);
        if (this.ed !== ed) return;
        if (ok) { this.ed = null; this.tab = "entries"; this.selected = this.entries.find(e => e.id === ed.id) || null; }
        else if (ok === false) { ed.problems = [T.ed_not_rendered]; }
      } catch (e) { ed.problems = e.problems || [e.message]; }
      finally { ed.busy = false; }
    },
    async removeEditor() {
      const ed = this.ed;
      ed.busy = true; ed.problems = [];
      try {
        const r = await api("DELETE", "/entries/" + encodeURIComponent(ed.id), { revision: ed.revision });
        const ok = await this.follow(r.job_id, T.ed_removed);
        if (this.ed !== ed) return;
        if (ok) { this.ed = null; this.selected = null; this.tab = "entries"; }
        else if (ok === false) { ed.confirmDelete = false; ed.problems = [T.ed_not_rendered]; }
      } catch (e) { ed.problems = [e.message]; }
      finally { ed.busy = false; }
    },

    // Waits for a job: true when it succeeded, false when it failed, null while it still runs.
    async follow(jobId, done) {
      this.flash(T.job_running, "alert-info");
      for (let i = 0; i < 300; i++) {
        await new Promise(r => setTimeout(r, 1500));
        const res = await fetchImpl("/api/v1/jobs/" + jobId);
        const job = await res.json().catch(() => ({}));
        if (job.status === "succeeded") { this.flash(done, "alert-success", jobId); await this.load(); if (this.gov.data) await this.loadGovernance(); return true; }
        if (job.status === "failed") { this.flash(T.job_failed, "alert-error", jobId); await this.load(); if (this.gov.data) await this.loadGovernance(); return false; }
      }
      return null;
    },
  };
}
