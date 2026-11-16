// Station Search: built-in/configured conversation histories and every Herdr agent's live
// terminals, in one place. search.py (next to this file) keeps an accent-blind full-text index.
// Filters: /agent-id /open, #folder. Click or Enter: go to it (its live terminal,
// or resume it in a new tab). The chevron or → shows the match in its conversation; ← goes back.
// ↑↓ move the keyboard from the field to the list; typing there goes back to the field.
// Language: the system's (English or Spanish); shared("station-search.lang") overrides it.
import { View, div, image, svg } from "gpui-kit";
import { v_flex, h_flex, InputState, Input, TextView } from "gpui-base";
import { props, closeOverlay, theme, run, exec, nextCommand, appHome, shared, setShared } from "herdr";

const SCRIPT = () => `${appHome().replace(/\\/g, "/")}/mods/station-search/search.py`;
const ICON = { claude: "icons/claude.svg", codex: "icons/codex.svg", hermes: "icons/hermes.svg", antigravity: "icons/antigravity.svg", pi: "icons/pi.svg" };
const TINTED = new Set(["hermes"]);
const NAME = { claude: "Claude", codex: "Codex", hermes: "Hermes", antigravity: "Antigravity", pi: "Pi", terminal: "Terminal" };
const RESUME = {
  claude: (id) => ["claude", "--resume", id],
  codex: (id) => ["codex", "resume", id],
  hermes: (id) => ["hermes", "--resume", id],
  antigravity: (id) => ["agy", "--conversation", id],
};

const TEXT = {
  en: {
    placeholder: "Search every conversation…  (#folder to filter)", all: "All", open: "Open",
    start: "Type to search all agents and open terminals.",
    none: "No results.", busy: "searching…", listKeys: "↑↓ pick · → view · Enter open · Esc close",
    detailKeys: "← back · Enter open · Esc back", live: "open", matches: (n) => `${n} matches`,
    toTerminal: "Go to terminal", resume: "Resume session", screen: "Match on the terminal's screen:",
    you: "You", loading: "Loading…", previewOnly: "Read-only history", locale: "en-US",
    ago: (n, u) => `${n}${{ m: "m", h: "h", d: "d" }[u]} ago`,
    noPython: "Station Search needs Python 3 (python3 or python on PATH).",
    searchError: "Search could not finish. Check Python, source access and the local index, then retry.",
  },
  es: {
    placeholder: "Buscar en todas las conversaciones…  (#carpeta para filtrar)", all: "Todo", open: "Abiertas",
    start: "Escribí algo para buscar en todos los agentes y las terminales abiertas.",
    none: "Sin resultados.", busy: "buscando…", listKeys: "↑↓ elegir · → ver · Enter abrir · Esc cerrar",
    detailKeys: "← volver · Enter abrir · Esc volver", live: "abierta", matches: (n) => `${n} coincidencias`,
    toTerminal: "Ir a la terminal", resume: "Retomar sesión", screen: "Coincidencia en la pantalla de la terminal:",
    you: "Vos", loading: "Cargando…", previewOnly: "Historial de solo lectura", locale: "es-AR",
    ago: (n, u) => `hace ${n} ${{ m: "min", h: "h", d: "d" }[u]}`,
    noPython: "Station Search necesita Python 3 (python3 o python en el PATH).",
    searchError: "No se pudo completar la búsqueda. Revisá Python, los permisos de las fuentes y el índice local; luego reintentá.",
  },
};

function blend(a, b, k) {
  const p = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
  if (!/^#[0-9a-f]{6}/i.test(a ?? "") || !/^#[0-9a-f]{6}/i.test(b ?? "")) return a;
  const [x, y] = [p(a), p(b)];
  return "#" + x.map((v, i) => Math.round(v * k + y[i] * (1 - k)).toString(16).padStart(2, "0")).join("");
}

export default class Search extends View {
  init(_props, cx) {
    this.lang = shared("station-search.lang") ?? "en";
    this.py = null;
    this.text = "";
    this.filter = "";
    this.results = [];
    this.agentTypes = Object.entries(NAME).filter(([id]) => id !== "terminal").map(([id, name]) => ({ id, name }));
    this.selected = 0;
    this.resultHeights = [];
    this.resultsY = 0;
    this.resultsScale = 1;
    this.followSelection = true;
    this.revealResult = false;
    this.busy = false;
    this.failure = false;
    this.asked = 0;
    this.key_ = null;
    this.detail = false;
    this.preview = { key: null, msgs: [], busy: false };
    this.tasks = [];
    this.makeInput();
    this.tasks.push(cx.timer.after(400, () => this.input.focus()));
    this.handle = cx.focus_handle();
    this.tasks.push(cx.spawn(async (cx) => {
      for (const name of ["python3", "python"]) {
        const r = await exec(name, ["-I", "-c", "import sys; print(sys.version_info[0])"]).catch(() => null);
        if (r?.code === 0 && r.stdout.trim() === "3") { this.py = name; break; }
      }
      this.py ??= false;
      if (this.py) {
        const catalog = await exec(this.py, ["-I", SCRIPT(), "agents"]).catch(() => null);
        try { this.agentTypes = JSON.parse(catalog?.stdout || "null") ?? this.agentTypes; } catch {}
      }
      if (this.py && !shared("station-search.lang")) {
        const r = await exec(this.py, ["-I", SCRIPT(), "lang"]).catch(() => null);
        this.setLang(r?.stdout.trim());
      }
      this.key_ = null;
      cx.notify();
    }));
    this.tasks.push(cx.spawn(async (cx) => {
      for (;;) {
        const c = await nextCommand();
        if (c.startsWith("query:")) { this.text = c.slice(6); this.input.set_value(this.text); cx.notify(); }
        if (c.startsWith("select:")) { this.selectResult(Number(c.slice(7))); cx.notify(); }
        if (c === "metrics") setShared("station-search.metrics", { count: this.results.length, selected: this.selected,
          y: this.resultsY, body: this.resultsBodyH, viewport: this.resultsViewH, rows: this.resultHeights });
        if (c === "metrics:clear") setShared("station-search.metrics", null);
        if (c === "detail") { this.detail = true; cx.notify(); }
        if (c.startsWith("scroll:")) {
          if (this.detail) this.scroll(Number(c.slice(7)), cx);
          else this.scrollResults(Number(c.slice(7)), cx);
        }
        if (c.startsWith("lang:")) { setShared("station-search.lang", c.slice(5) || null); this.setLang(c.slice(5)); cx.notify(); }
      }
    }));
  }

  close() {
    for (const t of [...this.tasks, this.query, this.peek]) t?.cancel();
  }

  get t() { return TEXT[this.lang] ?? TEXT.en; }

  // The field is rebuilt when the language changes, so its placeholder follows.
  makeInput() {
    this.input?.release?.();
    this.input = InputState.new({ value: this.text, placeholder: this.t.placeholder });
    this.input.on("change", (_e, cx) => { this.text = this.input.value(); cx.notify(); });
    this.input.on("submit", (_e, cx) => this.open(this.results[this.selected], cx));
    this.input.focus();
  }

  setLang(code) {
    if (!TEXT[code] || code === this.lang) return;
    this.lang = code;
    this.makeInput();
  }

  ago(iso) {
    if (!iso) return "";
    const s = (Date.now() - Date.parse(iso)) / 1000;
    if (s < 3600) return this.t.ago(Math.max(1, Math.floor(s / 60)), "m");
    if (s < 86400) return this.t.ago(Math.floor(s / 3600), "h");
    if (s < 86400 * 30) return this.t.ago(Math.floor(s / 86400), "d");
    return new Date(iso).toLocaleDateString(this.t.locale);
  }

  // Both run from render: a changed query or selection starts its work there (an input event's cx can't spawn).
  ask(cx) {
    if (!this.py) return;
    const key = `${this.filter} ${this.text}`.trim();
    if (key === this.key_) return;
    this.key_ = key;
    const n = ++this.asked;
    this.query?.cancel();
    this.query = null;
    if (!this.text.trim()) { this.results = []; this.busy = false; return; }
    this.busy = true;
    this.failure = false;
    this.query = cx.spawn(async (cx) => {
      await cx.sleep(250);
      if (n !== this.asked) return;
      const r = await exec(this.py, ["-I", SCRIPT(), "query", ...key.split(/\s+/)], { timeout: 600000 }).catch(() => null);
      if (n !== this.asked) return;
      try {
        const rows = JSON.parse(r?.stdout || "null");
        if (r?.code !== 0 || !Array.isArray(rows)) throw new Error("search failed");
        this.results = rows;
      } catch { this.results = []; this.failure = true; }
      for (const result of this.results) {
        if (result.agent !== "terminal" && !this.agentTypes.some((a) => a.id === result.agent)) {
          this.agentTypes.push({ id: result.agent, name: NAME[result.agent] ?? result.agent });
        }
      }
      this.selected = 0;
      this.resultsY = 0;
      this.followSelection = true;
      this.revealResult = true;
      this.detail = false;
      this.busy = false;
      this.query = null;
      cx.notify();
    });
  }

  look(cx) {
    const r = this.detail ? this.results[this.selected] : null;
    const key = r?.row ? `${r.row}|${this.text}` : null;
    if (key === this.preview.key) return;
    this.preview = { key, msgs: [], busy: Boolean(key) };
    this.scrollY = 0;
    this.peek?.cancel();
    this.peek = null;
    if (!key) return;
    this.peek = cx.spawn(async (cx) => {
      const out = await exec(this.py, ["-I", SCRIPT(), "context", String(r.row), ...this.text.split(/\s+/).filter(Boolean)]).catch(() => null);
      if (this.preview.key !== key) return;
      try { this.preview.msgs = JSON.parse(out?.stdout || "[]"); } catch { this.preview.msgs = []; }
      this.preview.busy = false;
      cx.notify();
    });
  }

  open(r, cx) {
    if (!r) return;
    const argv = r.resume ?? RESUME[r.agent]?.(r.session);
    if (!r.terminal && !argv?.length) {
      this.selected = this.results.indexOf(r);
      this.show(true, cx);
      return;
    }
    closeOverlay(props()?.overlay);
    if (r.terminal) return run("agent.focus", { terminal: r.terminal });
    if (argv) run("tab.new", { name: (r.title || NAME[r.agent]).slice(0, 40), folder: r.cwd || undefined, argv });
  }

  show(on, cx) {
    this.detail = on && Boolean(this.results[this.selected]);
    if (!this.detail) { this.followSelection = true; this.revealResult = true; }
    cx.notify();
  }

  selectResult(index) {
    if (!Number.isFinite(index)) return;
    this.selected = Math.max(0, Math.min(this.results.length - 1, Math.trunc(index)));
    this.followSelection = true;
    this.revealResult = true;
  }

  scrollResults(by, cx, manual = true) {
    if (manual) { this.followSelection = false; this.revealResult = false; }
    const max = Math.max(0, (this.resultsBodyH ?? 0) - (this.resultsViewH ?? 0));
    const next = Math.max(0, Math.min(max, this.resultsY + by));
    if (next !== this.resultsY) { this.resultsY = next; cx.notify(); }
  }

  revealSelectedResult() {
    if (this.detail || !this.revealResult || !this.results.length || !this.resultsViewH || !this.resultsBodyH) return;
    let top = 0;
    for (let i = 0; i <= this.selected; i++) {
      if (this.resultHeights[i] == null) return; // Wait for actual layout measurements.
      if (i < this.selected) top += this.resultHeights[i] + 2 * this.resultsScale;
    }
    const bottom = top + this.resultHeights[this.selected];
    const next = top < this.resultsY ? top : bottom > this.resultsY + this.resultsViewH ? bottom - this.resultsViewH : this.resultsY;
    this.resultsY = Math.max(0, Math.min(Math.max(0, this.resultsBodyH - this.resultsViewH), next));
    this.revealResult = false;
  }

  key(e, cx) {
    const listing = this.handle.is_focused();
    if (e.key === "escape") {
      if (this.detail) this.show(false, cx);
      else closeOverlay(props()?.overlay);
      cx.stop_propagation();
    } else if (e.key === "down" || e.key === "up") {
      if (this.detail) this.scroll(e.key === "down" ? 60 : -60, cx);
      else this.selectResult(this.selected + (e.key === "down" ? 1 : -1));
      this.handle.focus();
      cx.notify();
    } else if (listing && e.key === "right") this.show(true, cx);
    else if (listing && e.key === "left") this.show(false, cx);
    else if (listing && e.key === "enter") this.open(this.results[this.selected], cx);
    else if (listing && (e.key === "backspace" || (e.key_char && !e.modifiers?.control && !e.modifiers?.alt))) {
      const next = e.key === "backspace" ? this.text.slice(0, -1) : this.text + e.key_char;
      this.input.set_value(next);
      this.input.set_selected_range({ start: next.length, end: next.length });
      this.input.focus();
      this.text = next;
      this.detail = false;
      cx.notify();
    }
  }

  snippet(text, t) {
    const parts = (text ?? "").split(/(\[[^\]]*\])/);
    return h_flex().min_w(0).overflow_hidden().text_size(12).text_color(t.muted).children(parts.filter(Boolean).map((p) =>
      p.startsWith("[") && p.endsWith("]")
        ? div().flex_none().text_color(t.accent).font_semibold().child(p.slice(1, -1))
        : div().flex_none().whitespace_nowrap().child(p)));
  }

  icon(agent, t) {
    if (!ICON[agent]) return div().size(16).text_size(11).child(">_");
    return TINTED.has(agent) ? svg(ICON[agent]).size(16).text_color(t.foreground) : image(ICON[agent]).size(16);
  }

  row(r, i, t) {
    const on = i === this.selected;
    const where = (r.cwd ?? "").split(/[\\/]/).filter(Boolean).pop() ?? "";
    const meta = [NAME[r.agent] ?? r.agent, where, r.kind === "live" ? "" : this.ago(r.when), r.hits > 1 ? this.t.matches(r.hits) : ""].filter(Boolean).join(" · ");
    return h_flex().id(`r-${i}`).w_full().flex_none().pl(10).py(7).gap(9).items_start().rounded(8).cursor_pointer()
      .on_size_change((size, cx) => {
        if (this.resultHeights[i] === size.height) return;
        this.resultHeights[i] = size.height;
        if (this.followSelection) this.revealResult = true;
        cx.notify();
      })
      .bg(on ? t.selected : t.popover).hover((s) => s.bg(on ? t.selected : t.hover))
      .on_click((_e, cx) => this.open(r, cx))
      .child(div().flex_none().pt(2).child(this.icon(r.agent, t)))
      .child(v_flex().flex_1().min_w(0).gap(2)
        .child(h_flex().gap(8).min_w(0)
          .child(div().flex_1().min_w(0).truncate().text_size(13).text_color(t.foreground).font_medium().child(r.title || r.session || r.terminal))
          .when(Boolean(r.terminal), (el) => el.child(div().flex_none().px(6).rounded(4).text_size(10.5).bg(t.raised).text_color(t.accent).child(this.t.live))))
        .child(div().text_size(11).text_color(t.dim).truncate().child(meta))
        .child(this.snippet(r.snippet, t)))
      .child(div().id(`c-${i}`).flex_none().self_center().w(28).h(28).flex().items_center().justify_center().rounded(6)
        .text_size(16).text_color(t.muted).hover((s) => s.bg(t.raised).text_color(t.foreground))
        .on_click((_e, cx) => { cx.stop_propagation(); this.selected = i; this.show(true, cx); })
        .child("›"));
  }

  side(t) {
    const r = this.results[this.selected];
    if (!r) return div();
    const mark = blend(t.accent, t.popover, 0.45);
    const body = (text) => text.replace(/\x01/g, `<mark color="${mark}">`).replace(/\x02/g, "</mark>");
    const who = (m) => (m.role === "user" ? this.t.you : NAME[r.agent] ?? r.agent);
    const head = h_flex().gap(8).items_center().pb(8).border_b(1).border_color(t.border)
      .child(div().id("back").flex_none().w(28).h(28).flex().items_center().justify_center().rounded(6).cursor_pointer()
        .text_size(16).text_color(t.muted).hover((s) => s.bg(t.raised).text_color(t.foreground))
        .on_click((_e, cx) => this.show(false, cx)).child("‹"))
      .child(this.icon(r.agent, t))
      .child(div().flex_1().min_w(0).truncate().text_size(13).font_semibold().child(r.title || ""))
      .child(div().id("open").flex_none().px(10).py(4).rounded(6).text_size(12).cursor_pointer()
        .bg(t.solid).text_color(t.solid_foreground).hover((s) => s.opacity(0.88))
        .on_click((_e, cx) => this.open(r, cx)).child(r.terminal ? this.t.toTerminal : (r.resume?.length || RESUME[r.agent]) ? this.t.resume : this.t.previewOnly));
    if (r.kind === "live") {
      return v_flex().size_full().gap(10).child(head)
        .child(div().text_size(12.5).text_color(t.muted).child(this.t.screen))
        .child(this.snippet(r.snippet, t));
    }
    const msgs = this.preview.msgs.map((m, i) => v_flex().id(`m-${i}`).w_full().gap(4).p(10).rounded(8)
      .bg(m.hit ? t.raised : t.popover).border_1().border_color(m.hit ? t.accent : t.border)
      .child(div().text_size(11).text_color(m.role === "user" ? t.accent : t.dim).font_semibold()
        .child(`${who(m)}${m.when ? " · " + this.ago(m.when) : ""}`))
      .child(TextView.markdown(`md-${this.preview.key}-${i}`, body(m.text)).selectable(true)));
    return v_flex().size_full().gap(8).child(head)
      .child(div().id("preview").flex_1().min_h(0).overflow_hidden().text_size(12.5)
        .on_size_change((s, cx) => { this.viewH = s.height; this.scroll(0, cx); })
        .on_scroll_wheel((e, cx) => this.scroll(-e.delta.y, cx))
        .child(v_flex().id("preview-body").w_full().gap(8).mt(-(this.scrollY ?? 0))
          .on_size_change((s, cx) => { this.bodyH = s.height; this.scroll(0, cx); })
          .when(this.preview.busy, (el) => el.child(div().text_color(t.dim).child(this.t.loading)))
          .children(msgs)));
  }

  // The Markdown preview has its own measured scroll position, separate from the results.
  scroll(by, cx) {
    const max = Math.max(0, (this.bodyH ?? 0) - (this.viewH ?? 0));
    const next = Math.max(0, Math.min(max, (this.scrollY ?? 0) + by));
    if (next !== this.scrollY) { this.scrollY = next; cx.notify(); }
  }

  render(cx) {
    this.ask(cx);
    this.look(cx);
    this.revealSelectedResult();
    const t = theme(), s = this.t;
    const filters = [["", s.all], ...this.agentTypes.map(({ id, name }) => [`/${id}`, name]), ["/open", s.open]];
    const chip = ([token, label]) => {
      const on = this.filter === token;
      return div().id(`f-${token || "all"}`).px(10).py(3).rounded(6).text_size(12).cursor_pointer()
        .bg(on ? t.selected : t.popover).text_color(on ? t.foreground : t.muted).hover((st) => st.bg(t.hover))
        .on_click((_e, cx) => { this.filter = token; cx.notify(); }).child(label);
    };
    const empty = this.py === false ? s.noPython : !this.text.trim() ? s.start : this.failure ? s.searchError : s.none;
    // The overlay keeps its full size and draws no frame; the card hugs its content at the top,
    // so the field never moves while results come and go.
    const card = v_flex().w_full().flex_none().p(14).gap(10).bg(t.popover).text_color(t.foreground)
      .rounded(12).border_1().border_color(t.border).shadow_lg()
      .child(h_flex().flex_none().h(38).pr(6).gap(6).items_center().rounded(9).border_1().border_color(t.accent).bg(t.background)
        .on_size_change((size, cx) => {
          // Layout measurements include app zoom; builder lengths are scaled by it again.
          const scale = size.height / 38;
          if (scale > 0 && Math.abs(scale - this.resultsScale) > 0.0001) {
            this.resultsScale = scale;
            if (this.followSelection) this.revealResult = true;
            cx.notify();
          }
        })
        .child(Input.new(this.input).flex_1().min_w(0).h_full().px(10).border_0().bg(t.background).text_size(13.5))
        .child(h_flex().flex_none().p(2).gap(2).rounded(6).bg(t.raised).children(Object.keys(TEXT).map((code) =>
          div().id(`lang-${code}`).px(6).py(1).rounded(4).text_size(10.5).font_semibold().cursor_pointer()
            .bg(code === this.lang ? t.selected : t.raised).text_color(code === this.lang ? t.foreground : t.dim)
            .hover((st) => st.text_color(t.foreground))
            .on_click((_e, cx) => { setShared("station-search.lang", code); this.setLang(code); cx.notify(); })
            .child(code.toUpperCase())))))
      .child(h_flex().flex_none().flex_wrap().gap(4).items_center().children(filters.map(chip)))
      .child(div().text_size(11).text_color(t.dim).child(this.busy ? s.busy : this.detail ? s.detailKeys : s.listKeys))
      .child(this.detail
        ? div().h(460).child(this.side(t))
        : v_flex().id("results").flex_none().max_h(460).overflow_hidden()
          .when(this.resultsBodyH != null, (el) => el.h(Math.min(460, this.resultsBodyH / this.resultsScale)))
          .on_size_change((size, cx) => {
            this.resultsViewH = size.height;
            if (this.followSelection) this.revealResult = true;
            this.scrollResults(0, cx, false);
            cx.notify();
          })
          .on_scroll_wheel((event, cx) => { this.scrollResults(-event.delta.y, cx); cx.stop_propagation(); })
          .child(v_flex().id("results-body").w_full().flex_none().gap(2).mt(-this.resultsY / this.resultsScale)
          .on_size_change((size, cx) => {
            this.resultsBodyH = size.height;
            if (this.followSelection) this.revealResult = true;
            this.scrollResults(0, cx, false);
            cx.notify();
          })
          .when(this.results.length === 0 && this.busy, (el) => el.child(h_flex().w_full().py(28).justify_center()
            .child(svg("mods/workspace-chrome/spinner.svg").size(22).text_color(t.accent).spin(900))))
          .when(this.results.length === 0 && !this.busy, (el) => el.child(div().p(20).text_size(12.5).text_color(t.dim).child(empty)))
          .children(this.results.map((r, i) => this.row(r, i, t)))));
    return v_flex().id("station-search").track_focus(this.handle).on_key_down((e, cx) => this.key(e, cx))
      .size_full().px(10).pt(4).child(card);
  }
}
