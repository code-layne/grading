"""`grading review`: the review queue as a page on localhost.

The page is served to 127.0.0.1 only. Every change is a JSON POST carrying a
token made when the server starts, so another site open in the browser can't
post decisions (a cross-origin JSON post needs a preflight this server never
answers, and it can't read the token), and requests for any other host name
are refused, so DNS rebinding gets nowhere either.

Everything shown is by tracking id: this module, like review.py, never reads
the roster.
"""

import secrets
import webbrowser

from flask import Flask, abort, g, jsonify, render_template_string, request, send_file

from . import db
from .ingest import PlaceError, dismiss_page, place_by_hand
from .manifests import ManifestError
from .review import TIER_TITLES, Review, ReviewError


def create_app(cfg, token: str | None = None) -> Flask:
    app = Flask(__name__)
    app.config["TOKEN"] = token or secrets.token_urlsafe(24)

    def review() -> Review:
        if "review" not in g:
            g.conn = db.connect(cfg.database)
            g.review = Review(cfg, g.conn)
        return g.review

    @app.teardown_appcontext
    def close(exc):
        conn = g.pop("conn", None)
        if conn is not None:
            conn.close()

    @app.before_request
    def guard():
        host = (request.host or "").rsplit(":", 1)[0]
        if host not in ("127.0.0.1", "localhost"):
            abort(403)
        if request.method == "POST" and request.headers.get("X-Grading-Token") != app.config["TOKEN"]:
            abort(403)

    @app.after_request
    def no_store(resp):
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Frame-Options"] = "DENY"
        return resp

    @app.get("/")
    def queue():
        r = review()
        groups = r.groups()
        i = min(max(request.args.get("i", 0, type=int), 0), max(len(groups) - 1, 0))
        current = r.group(groups[i]["key"]) if groups else None
        return render_template_string(PAGE, view="queue", groups=groups, i=i, g_=current, counts=r.counts(),
                                      titles=TIER_TITLES, token=app.config["TOKEN"])

    @app.get("/unplaced")
    def unplaced():
        r = review()
        pages = r.unplaced()
        i = min(max(request.args.get("i", 0, type=int), 0), max(len(pages) - 1, 0))
        return render_template_string(PAGE, view="unplaced", pages=pages, i=i, p=pages[i] if pages else None,
                                      assignments=r.assignments(), counts=r.counts(), token=app.config["TOKEN"])

    @app.get("/crop/<int:crop_id>.png")
    def crop(crop_id):
        path = review().crop_path(crop_id)
        if path is None or not path.is_file():
            abort(404)
        return send_file(path, mimetype="image/png")

    @app.get("/page/<int:page_id>.png")
    def page(page_id):
        path = review().page_path(page_id)
        if path is None or not path.is_file():
            abort(404)
        return send_file(path, mimetype="image/png")

    @app.get("/api/candidates")
    def candidates():
        return jsonify(review().candidates(request.args.get("assignment", ""), request.args.get("page", 1, type=int)))

    def body():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            abort(400)
        return data

    @app.errorhandler(ReviewError)
    @app.errorhandler(PlaceError)
    @app.errorhandler(ManifestError)
    def refused(e):
        return jsonify({"error": str(e)}), 400

    @app.post("/api/approve")
    def approve():
        d = body()
        n = review().approve(d["key"], d.get("points"), d.get("feedback"))
        return jsonify({"message": f"ruling saved: {n} answer{'s' if n != 1 else ''} scored"})

    @app.post("/api/decide")
    def decide():
        d = body()
        review().decide(int(d["crop_id"]), d.get("points"), d.get("feedback"))
        return jsonify({"message": "scored this crop only"})

    @app.post("/api/reading")
    def reading():
        d = body()
        key = review().correct(int(d["crop_id"]), d.get("reading"))
        return jsonify({"message": "reading fixed" + ("" if key else ": that settled it"), "key": key})

    @app.post("/api/blanks")
    def blanks():
        d = body()
        review().confirm_blanks([int(x) for x in d.get("confirm", [])], [int(x) for x in d.get("not_blank", [])])
        n = len(d.get("not_blank", []))
        return jsonify({"message": "blanks confirmed" + (f"; {n} sent back to `grading read`" if n else "")})

    @app.post("/api/place")
    def place():
        d = body()
        r = review()
        res = place_by_hand(cfg, r.conn, int(d["page_id"]), str(d.get("assignment", "")), int(d.get("page") or 0),
                            str(d.get("tracking_id", "")))
        what = "placed" if res.status == "placed" else "stored as a duplicate of a page already placed"
        return jsonify({"message": f"{what}: {res.tracking_id} {res.assignment} page {res.page}. "
                                   "Run `grading read` to grade it."})

    @app.post("/api/dismiss")
    def dismiss():
        dismiss_page(review().conn, int(body()["page_id"]))
        return jsonify({"message": "page dismissed"})

    return app


def serve(cfg, port: int = 5005, open_browser: bool = True):
    app = create_app(cfg)
    url = f"http://127.0.0.1:{port}/"
    print(f"review: {url}  (Ctrl-C to stop)")
    if open_browser:
        webbrowser.open(url)
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="token" content="{{ token }}">
<title>Grading review</title>
<style>
:root { --bg:#fcfcfb; --panel:#f3f2ef; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781; --line:#dddcd7;
        --accent:#2a78d6; --warn:#b4540a; --bad:#c22b2b; --ok:#2f7d32; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#1a1a19; --panel:#242423; --ink:#f0efec; --ink2:#c3c2b7; --muted:#898781; --line:#383835;
          --accent:#3987e5; --warn:#e08a3c; --bad:#ef6a6a; --ok:#6bbf6e; }
  img.crop, img.page { filter: invert(0.92); }
}
* { box-sizing: border-box; } [hidden] { display:none !important; }
body { margin:0; background:var(--bg); color:var(--ink); font:14px/1.45 -apple-system, system-ui, sans-serif; }
header { display:flex; gap:16px; align-items:center; padding:8px 16px; border-bottom:1px solid var(--line); flex-wrap:wrap; }
header b { font-size:15px; } header a { color:var(--accent); text-decoration:none; }
header .sp { flex:1; } .muted { color:var(--muted); } .ink2 { color:var(--ink2); }
main { display:grid; grid-template-columns: 300px 1fr; min-height: calc(100vh - 42px); }
@media (max-width: 800px) { main { grid-template-columns: 1fr; } nav { max-height: 30vh; } }
nav { border-right:1px solid var(--line); overflow:auto; max-height: calc(100vh - 42px); background:var(--panel); }
nav h3 { margin:12px 12px 4px; font-size:12px; text-transform:uppercase; letter-spacing:.04em; color:var(--ink2); }
nav a { display:block; padding:5px 12px; color:var(--ink); text-decoration:none; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
nav a.on { background:var(--accent); color:#fff; }
nav .n { float:right; color:inherit; opacity:.8; margin-left:6px; }
section { padding:16px 20px; overflow:auto; max-height: calc(100vh - 42px); }
.tag { display:inline-block; font-size:11px; padding:1px 6px; border-radius:8px; border:1px solid var(--line); margin-right:4px; color:var(--ink2); }
.tag.r0 { border-color:var(--bad); color:var(--bad); } .tag.r1 { border-color:var(--warn); color:var(--warn); }
.crops { display:flex; flex-wrap:wrap; gap:12px; margin:12px 0; }
.card { border:2px solid var(--line); border-radius:6px; padding:8px; background:var(--panel); max-width:100%; }
.card.on { border-color:var(--accent); } .card.nb { border-color:var(--warn); }
img.crop { display:block; max-width:min(560px, 100%); background:#fff; }
img.page { display:block; max-width:100%; background:#fff; border:1px solid var(--line); }
table { border-collapse:collapse; margin:8px 0; } td, th { border-bottom:1px solid var(--line); padding:3px 10px 3px 0; text-align:left; vertical-align:top; }
th { font-weight:600; color:var(--ink2); font-size:12px; }
code, .reading { font-family: ui-monospace, Menlo, monospace; }
.reading { font-size:16px; }
.row { display:flex; gap:16px; align-items:flex-start; flex-wrap:wrap; margin:10px 0; }
label { display:block; font-size:12px; color:var(--ink2); margin-bottom:2px; }
input, textarea, select { font:inherit; color:var(--ink); background:var(--bg); border:1px solid var(--line); border-radius:4px; padding:4px 6px; }
input:focus, textarea:focus, select:focus { outline:2px solid var(--accent); }
textarea { width:min(560px, 100%); height:64px; }
#msg { padding:6px 16px; } #msg.err { color:var(--bad); } #msg.ok { color:var(--ok); }
kbd { font-family:ui-monospace, Menlo, monospace; font-size:11px; border:1px solid var(--line); border-bottom-width:2px; border-radius:3px; padding:0 4px; }
.keys { color:var(--ink2); font-size:12px; margin-top:16px; } .keys span { margin-right:12px; white-space:nowrap; }
.big { font-size:18px; }
</style></head>
<body>
<header>
  <b>Review</b>
  <a href="/" {% if view == 'queue' %}style="font-weight:600"{% endif %}>Queue ({{ counts['items'] }})</a>
  <a href="/unplaced" {% if view == 'unplaced' %}style="font-weight:600"{% endif %}>Unplaced pages ({{ counts['unplaced'] }})</a>
  <span class="sp"></span><span class="muted">{{ counts['rulings'] }} rulings · <kbd>?</kbd> keys</span>
</header>
<div id="msg"></div>
{% if view == 'queue' %}
<main>
<nav>
  {% set ns = namespace(tier=None) %}
  {% for grp in groups %}
    {% if grp.tier != ns.tier %}{% set ns.tier = grp.tier %}<h3>{{ titles[grp.tier] }}</h3>{% endif %}
    <a href="/?i={{ loop.index0 }}" class="{{ 'on' if loop.index0 == i }}">
      <span class="n">×{{ grp.count }}</span>
      {% if grp.key[0] == 'blanks' %}Blank boxes{% else %}{{ grp.question }} · <code>{{ (grp.reading if grp.reading else '(no reading)')[:40] }}</code>{% endif %}
    </a>
  {% endfor %}
  {% if not groups %}<p class="muted" style="padding:12px">Nothing to review.</p>{% endif %}
</nav>
<section>
{% if g_ and g_.key[0] == 'blanks' %}
  <h2>Blank boxes to confirm <span class="muted">({{ g_.count }})</span></h2>
  <p class="ink2">Ingest found these boxes empty. Mark any that hold light writing with <kbd>x</kbd>, then confirm the batch with <kbd>a</kbd>. Confirmed blanks score zero; marked ones go back to <code>grading read</code>.</p>
  <div class="crops">
  {% for it in g_['items'] %}
    <div class="card" data-crop="{{ it.crop_id }}">
      <div><b>{{ it.question }}</b> · {{ it.tracking_id }} <span class="muted">{{ it.assignment }}</span> <span class="tag nbtag" hidden>has writing</span></div>
      <img class="crop" src="/crop/{{ it.crop_id }}.png" alt="answer box {{ it.question }}">
    </div>
  {% endfor %}
  </div>
{% elif g_ %}
  <h2>{{ g_.question }} <span class="muted">{{ g_.assignment }} · {{ g_.q.type }} · {{ '%g' % g_.q.points }} pt</span></h2>
  <div>{% for r in g_.reasons %}<span class="tag {{ 'r0' if r == 'verify-disagreement' else 'r1' if r in ('illegible','unparseable','low-confidence','read-failed','verify-failed','rubric-failed','not-a-letter','not-a-number','reading-corrected') }}">{{ r }}</span>{% endfor %}
    <span class="muted">{{ g_.count }} answer{{ 's' if g_.count != 1 }} in this group{% if g_.matching > g_.count %}, {{ g_.matching }} with this answer in all{% endif %}</span></div>
  <div class="row">
    <div><label>Reading</label><div class="reading">{{ g_['items'][0].reading if g_['items'][0].reading else '(none)' }}</div></div>
    {% if g_.q.type != 'rubric' %}<div><label>Key</label><div class="reading">{{ g_.q.answer }}</div><span class="muted">{{ g_.q.check or '' }}{% if g_.q.tolerance %} ±{{ g_.q.tolerance }}{% endif %}</span></div>{% endif %}
    <div><label>Proposed score</label><div class="big">{{ ('%g' % g_.proposed) if g_.proposed is not none else '—' }} / {{ '%g' % g_.q.points }}</div></div>
    {% if g_.ruling %}<div><label>Current ruling</label>{{ '%g' % g_.ruling.points }} pt</div>{% endif %}
  </div>
  <div class="crops">
  {% for it in g_['items'] %}
    <div class="card" data-crop="{{ it.crop_id }}" data-verify="{{ it.verify_reading or '' }}">
      <div><b>{{ it.tracking_id }}</b>
        {% if it.confidence and it.confidence != 'high' %}<span class="tag r1">{{ it.confidence }} confidence</span>{% endif %}
        {% if it.corrected_from is not none %}<span class="tag">fixed from <code>{{ it.corrected_from }}</code></span>{% endif %}
        {% if it.read_status %}<span class="tag r1">{{ it.read_status }}</span>{% endif %}</div>
      <img class="crop" src="/crop/{{ it.crop_id }}.png" alt="answer box">
      {% if it.verify %}
        <div class="ink2" style="margin-top:4px">Verify read <code>{{ it.verify_reading }}</code>{% if it.verify.confidence != 'high' %} ({{ it.verify.confidence }}){% endif %}:
          {% if it.verify.agrees %}agrees{% else %}<b style="color:var(--bad)">disputes</b>{% endif %}{% if it.verify.dispute %} — {{ it.verify.dispute }}{% endif %}
          {% if it.verify_points is not none %} · scores {{ '%g' % it.verify_points }}{% endif %}</div>
      {% endif %}
    </div>
  {% endfor %}
  </div>
  {% set ev = g_['items'][0] %}
  {% if g_.q.type == 'rubric' and g_.q.rubric %}
    <h3>Evidence</h3>
    <table><tr><th>Criterion</th><th>Points</th><th>Evidence</th><th>Confidence</th>{% if ev.verify and ev.verify.criteria %}<th>Verify</th>{% endif %}</tr>
    {% for c in g_.q.rubric.criteria %}{% set cid = c.id|string %}
      {% set got = (ev.criteria or {}).get(cid) %}{% set ver = ((ev.verify or {}).get('criteria') or {}).get(cid) %}
      <tr><td><b>{{ cid }}</b> <span class="ink2">{{ c.full or '' }}</span></td>
          <td>{{ ('%g' % got.points) if got else '—' }} / {{ '%g' % c.points }}</td>
          <td><i>{{ got.evidence if got and got.evidence else '' }}</i></td><td>{{ got.confidence if got else '' }}</td>
          {% if ev.verify and ev.verify.criteria %}<td{% if ver and got and ver.points != got.points %} style="color:var(--bad);font-weight:600"{% endif %}>{{ ('%g' % ver.points) if ver else '' }}</td>{% endif %}</tr>
    {% endfor %}</table>
  {% endif %}
  <div class="row">
    <div><label for="score">Score {% if g_.individual %}(this crop only){% endif %}</label>
      <input id="score" size="6" inputmode="decimal" placeholder="{{ ('%g' % g_.proposed) if g_.proposed is not none else '' }}"></div>
    <div style="flex:1"><label for="feedback">Feedback note for the student</label>
      <textarea id="feedback">{{ g_.feedback or '' }}</textarea></div>
  </div>
  <div class="row">
    <div><label for="reading">Fix the reading of the highlighted crop</label>
      <input id="reading" size="40" placeholder="what the student wrote"></div>
  </div>
  {% if g_.individual %}<p class="ink2">There is no reading to rule on, so a score here applies to the highlighted crop only. Fixing the reading scores it like any other answer.</p>{% endif %}
{% else %}
  <h2>Nothing to review</h2>
  <p class="ink2">Every item is settled. <code>grading report</code> can run.</p>
{% endif %}
  <div class="keys" id="keys">
    <span><kbd>j</kbd>/<kbd>k</kbd> next/previous group</span><span><kbd>h</kbd>/<kbd>l</kbd> previous/next crop</span>
    <span><kbd>a</kbd> approve</span><span><kbd>s</kbd> change score</span><span><kbd>f</kbd> edit feedback</span>
    <span><kbd>r</kbd> fix reading</span><span><kbd>v</kbd> take the verify reading</span><span><kbd>x</kbd> blank has writing</span>
    <span><kbd>Enter</kbd> save field</span><span><kbd>⌘/Ctrl Enter</kbd> approve from feedback</span><span><kbd>Esc</kbd> leave field</span>
    <span><kbd>u</kbd> unplaced pages</span>
  </div>
</section>
</main>
<script>
const KEY = {{ (g_.key if g_ else none)|tojson }};
const INDIVIDUAL = {{ (g_.individual if g_ else false)|tojson }};
const COUNT = {{ groups|length }}, I = {{ i }};
</script>
{% else %}
<main>
<nav>
  <h3>Unplaced pages</h3>
  {% for pg in pages %}<a href="/unplaced?i={{ loop.index0 }}" class="{{ 'on' if loop.index0 == i }}">#{{ pg.id }} · {{ pg.reason }}</a>{% endfor %}
  {% if not pages %}<p class="muted" style="padding:12px">No unplaced pages.</p>{% endif %}
</nav>
<section>
{% if p %}
  <h2>Page #{{ p.id }} <span class="muted">{{ p.reason }}</span></h2>
  <p class="ink2">Scan #{{ p.scan_id }} (ingested {{ p.ingested_at }}, {{ p.page_count }} pages), page {{ p.scan_page }}, received {{ p.received_on }}.
     {% if p.detail %}<br>{{ p.detail }}{% endif %}<br>
     The name strip is blanked: find this page in the paper stack (or with <code>grading status</code>, which names the scan file), and its tracking id in roster-map.csv.</p>
  <div class="row">
    <div><label for="assignment">Assignment</label>
      <select id="assignment">{% for a in assignments %}<option value="{{ a.assignment }}" data-pages="{{ a.pages }}">{{ a.assignment }}</option>{% endfor %}</select></div>
    <div><label for="pageno">Page</label><input id="pageno" type="number" min="1" value="1" style="width:5em"></div>
    <div><label for="tid">Tracking id</label><input id="tid" size="8" list="tids" autocomplete="off" style="text-transform:uppercase"><datalist id="tids"></datalist></div>
  </div>
  <p class="ink2" id="cands"></p>
  <img class="page" src="/page/{{ p.id }}.png" alt="unplaced page">
{% endif %}
  <div class="keys"><span><kbd>j</kbd>/<kbd>k</kbd> next/previous page</span><span><kbd>t</kbd> tracking id</span>
    <span><kbd>Enter</kbd> place it</span><span><kbd>d</kbd> dismiss (not a student page)</span><span><kbd>q</kbd> back to the queue</span></div>
</section>
</main>
<script>const PAGE_ID = {{ (p.id if p else none)|tojson }}, COUNT = {{ pages|length }}, I = {{ i }};</script>
{% endif %}
<script>
const TOKEN = document.querySelector('meta[name=token]').content;
const VIEW = {{ view|tojson }};
const msg = document.getElementById('msg');
function show(text, ok) { msg.textContent = text; msg.className = ok ? 'ok' : 'err'; }
const flash = sessionStorage.getItem('flash'); if (flash) { show(flash, true); sessionStorage.removeItem('flash'); }
async function post(url, data, next) {
  const r = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Grading-Token': TOKEN},
                              body: JSON.stringify(data)});
  const out = await r.json().catch(() => ({error: 'HTTP ' + r.status}));
  if (!r.ok || out.error) { show(out.error || ('HTTP ' + r.status), false); return; }
  sessionStorage.setItem('flash', out.message || 'saved');
  location.href = next;
}
function typing(e) { return ['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target.tagName); }
function go(href) { location.href = href; }

if (VIEW === 'queue') {
  const cards = [...document.querySelectorAll('.card')];
  let at = 0; const notBlank = new Set();
  function focusCard(n) { if (!cards.length) return; at = (n + cards.length) % cards.length;
    cards.forEach((c, k) => c.classList.toggle('on', k === at)); cards[at].scrollIntoView({block: 'nearest'}); }
  focusCard(0);
  const here = '/?i=' + I;
  const score = document.getElementById('score'), fb = document.getElementById('feedback'), rd = document.getElementById('reading');
  function crop() { return cards.length ? +cards[at].dataset.crop : null; }
  function approve() {
    if (!KEY) return;
    if (KEY[0] === 'blanks') {
      return post('/api/blanks', {confirm: cards.map(c => +c.dataset.crop), not_blank: [...notBlank]}, here);
    }
    const pts = score && score.value.trim() !== '' ? score.value.trim() : null;
    if (INDIVIDUAL) {
      if (pts === null) { show('type a score for this crop first (s)', false); score.focus(); return; }
      return post('/api/decide', {crop_id: crop(), points: pts, feedback: fb.value}, here);
    }
    post('/api/approve', {key: KEY, points: pts, feedback: fb.value}, here);
  }
  function fixReading(text) { post('/api/reading', {crop_id: crop(), reading: text}, here); }
  if (score) score.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); approve(); } });
  if (fb) fb.addEventListener('keydown', e => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); approve(); } });
  if (rd) rd.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); if (rd.value.trim()) fixReading(rd.value.trim()); } });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') { document.activeElement.blur(); return; }
    if (typing(e) || e.metaKey || e.ctrlKey || e.altKey) return;
    const k = e.key;
    if (k === 'j' || k === 'ArrowDown') { if (I + 1 < COUNT) go('/?i=' + (I + 1)); }
    else if (k === 'k' || k === 'ArrowUp') { if (I > 0) go('/?i=' + (I - 1)); }
    else if (k === 'l' || k === 'ArrowRight') focusCard(at + 1);
    else if (k === 'h' || k === 'ArrowLeft') focusCard(at - 1);
    else if (k === 'a') approve();
    else if (k === 's' && score) score.focus();
    else if (k === 'f' && fb) fb.focus();
    else if (k === 'r' && rd) rd.focus();
    else if (k === 'v' && cards.length && cards[at].dataset.verify) fixReading(cards[at].dataset.verify);
    else if (k === 'x' && KEY && KEY[0] === 'blanks') {
      const id = +cards[at].dataset.crop;
      if (notBlank.has(id)) notBlank.delete(id); else notBlank.add(id);
      cards[at].classList.toggle('nb', notBlank.has(id));
      cards[at].querySelector('.nbtag').hidden = !notBlank.has(id);
    }
    else if (k === 'u') go('/unplaced');
    else if (k === '?') document.getElementById('keys').scrollIntoView();
    else return;
    e.preventDefault();
  });
} else {
  const sel = document.getElementById('assignment'), pg = document.getElementById('pageno'), tid = document.getElementById('tid');
  async function cands() {
    if (!sel) return;
    pg.max = sel.selectedOptions[0] ? sel.selectedOptions[0].dataset.pages : 1;
    const r = await fetch('/api/candidates?assignment=' + encodeURIComponent(sel.value) + '&page=' + pg.value);
    const c = await r.json();
    document.getElementById('tids').innerHTML = c.known.map(t => '<option value="' + t + '">').join('');
    document.getElementById('cands').textContent = c.missing_this_page.length
      ? 'Have other pages of this assignment but not page ' + pg.value + ': ' + c.missing_this_page.join(', ')
      : 'Nobody with other pages of this assignment is missing page ' + pg.value + '.';
  }
  if (sel) { sel.addEventListener('change', cands); pg.addEventListener('change', cands); cands(); }
  function place() { post('/api/place', {page_id: PAGE_ID, assignment: sel.value, page: pg.value, tracking_id: tid.value.trim()}, '/unplaced?i=' + I); }
  [sel, pg, tid].forEach(el => el && el.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); place(); } }));
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') { document.activeElement.blur(); return; }
    if (typing(e) || e.metaKey || e.ctrlKey || e.altKey) return;
    const k = e.key;
    if (k === 'j' || k === 'ArrowDown') { if (I + 1 < COUNT) go('/unplaced?i=' + (I + 1)); }
    else if (k === 'k' || k === 'ArrowUp') { if (I > 0) go('/unplaced?i=' + (I - 1)); }
    else if (k === 't' && tid) tid.focus();
    else if (k === 'd' && PAGE_ID !== null) { if (confirm('Dismiss page #' + PAGE_ID + '? It will not be graded.')) post('/api/dismiss', {page_id: PAGE_ID}, '/unplaced?i=' + I); }
    else if (k === 'q') go('/');
    else return;
    e.preventDefault();
  });
}
</script>
</body></html>
"""
