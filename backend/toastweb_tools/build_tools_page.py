#!/usr/bin/env python3
"""
build_tools_page.py -- turn engine.js + jobs/*.json into frontend/toast-tools.html.

The generated page is served by the Cloudways app at /toast-tools.html, so anyone with
app access can drag the buttons to their own bookmarks bar. Each job produces two:

    <job> AUDIT   read-only. Lists everything it would change. Cannot write.
    <job> FIX     confirm-gated. Applies, verifies, then ONE save.

Plus one shared read-only "Probe options" button for diagnosing an item whose location
dropdown does not offer a group the job needs.

ADDING A NEW JOB
    1. copy a file in jobs/, change the rules
    2. python build_tools_page.py
    3. commit, deploy, re-drag that job's two buttons

The build REFUSES to write if anything fails:
    * validate_jobs.mjs  -- anchored patterns, self-conflicts, canary labels, bad ids
    * node --check       -- on every assembled bookmarklet, because a mangled escape
                            once turned a button into a silent no-op
"""

import json
import subprocess
import sys
import tempfile
from html import escape
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
ENGINE = HERE / "engine.js"
JOBS = HERE / "jobs"
VALIDATOR = HERE / "validate_jobs.mjs"
OUT = HERE.parent.parent / "frontend" / "toast-tools.html"

# No backslash escapes in these tails on purpose: newlines come from
# String.fromCharCode(10). A previous version used '\n' and an editing pass turned them
# into real newlines inside single-quoted strings -- an unterminated literal, and the
# whole bookmarklet silently failed to parse.
AUDIT_TAIL = """
;(() => {
  const NL = String.fromCharCode(10);
  const a = window.__newksToastTool.audit();
  console.log(a);
  const block = (t, arr) => t + ' (' + arr.length + '):' + NL +
    (arr.length ? arr.join(NL) : '  (none)');
  alert([
    'JOB:  ' + a.job,
    'ITEM: ' + a.item,
    block('SET PRICE', a.setPrices),
    block('FIX EXISTING', a.fixExisting),
    block('ADD ROW', a.addRows),
    block('REMOVE ROW', a.removeRows),
    block('LEFT ALONE', a.leftAlone),
    'PROBLEMS: ' + (a.problems.length ? NL + a.problems.join(NL) : 'none')
  ].join(NL + NL));
})();
"""

APPLY_TAIL = """
;(async () => {
  const NL = String.fromCharCode(10);
  const t = window.__newksToastTool;
  const a = t.audit();
  console.log(a);
  if (a.problems.length) {
    alert('JOB:  ' + a.job + NL + 'ITEM: ' + a.item + NL + NL +
          'NOT SAFE:' + NL + a.problems.join(NL));
    return;
  }
  const n = a.setPrices.length + a.fixExisting.length + a.addRows.length + a.removeRows.length;
  if (!n) { alert('JOB:  ' + a.job + NL + 'ITEM: ' + a.item + NL + NL +
                  'Already correct. Nothing to do.'); return; }
  const block = (ttl, arr) => ttl + ' (' + arr.length + '):' + NL +
    (arr.length ? arr.join(NL) : '  (none)');
  if (!confirm([
    'JOB:  ' + a.job,
    'ITEM: ' + a.item,
    block('SET PRICE', a.setPrices),
    block('FIX EXISTING', a.fixExisting),
    block('ADD ROW', a.addRows),
    block('REMOVE ROW', a.removeRows),
    'Then SAVE once. Proceed?'
  ].join(NL + NL))) { console.log('cancelled'); return; }
  const r = await t.apply();
  console.log(r);
  if (r.status !== 'applied+save-clicked') {
    alert(['STOPPED - nothing saved.',
           'Reload the page to discard any partial edits.',
           'status: ' + r.status,
           (r.problems && r.problems.length ? r.problems.join(NL) : '')].join(NL + NL));
  }
})();
"""

PROBE_TAIL = """
;(() => {
  const NL = String.fromCharCode(10);
  const p = window.__newksToastTool.probe();
  console.log(p);
  try { navigator.clipboard.writeText(JSON.stringify(p, null, 2)); } catch (e) {}
  alert([
    'ITEM: ' + p.item,
    'location options on this item: ' + p.totalOptions,
    'Add button: ' + p.addButtonText,
    'THIS JOB NEEDS:' + NL + p.wantedByThisJob.map((w) =>
      '  ' + w.rowLabel + '  byId=' + w.foundById + '  byLabel=' + w.foundByLabel).join(NL),
    'Company Only options here:' + NL +
      (p.companyOnlyOptions.length ? '  ' + p.companyOnlyOptions.join(NL + '  ') : '  (none)'),
    'Full JSON copied to clipboard.'
  ].join(NL + NL));
})();
"""


def run_node(args, label):
    try:
        return subprocess.run(["node", *args], capture_output=True, text=True)
    except FileNotFoundError:
        print(f"  WARNING: node not found, skipped {label}")
        return None


def syntax_ok(js: str, label: str) -> bool:
    path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
            fh.write(js)
            path = fh.name
        res = run_node(["--check", path], label)
        if res is None:
            return True
        if res.returncode != 0:
            print(f"  SYNTAX ERROR in {label}:")
            print("    " + (res.stderr or "").strip().replace("\n", "\n    ")[:600])
            return False
        return True
    finally:
        if path:
            Path(path).unlink(missing_ok=True)


def main() -> int:
    if not ENGINE.exists():
        print(f"missing {ENGINE}")
        return 1

    res = run_node([str(VALIDATOR), str(JOBS)], "job validation")
    if res is not None:
        print(res.stdout.rstrip())
        if res.returncode != 0:
            print("refusing to build -- fix the job configs above")
            return 1

    engine = ENGINE.read_text(encoding="utf-8")
    if not syntax_ok(engine, "engine.js"):
        return 1

    jobs = []
    for f in sorted(JOBS.glob("*.json")):
        jobs.append(json.loads(f.read_text(encoding="utf-8")))
    if not jobs:
        print("no jobs found")
        return 1

    def bookmarklet(job, tail):
        js = f"window.__TOAST_JOB = {json.dumps(job)};\n{engine}{tail}"
        return js, "javascript:" + quote(js, safe="")

    cards, probe_href = [], None
    for job in jobs:
        made = {}
        for kind, tail in (("AUDIT", AUDIT_TAIL), ("FIX", APPLY_TAIL)):
            js, href = bookmarklet(job, tail)
            if not syntax_ok(js, f"{job['id']} {kind}"):
                print("refusing to write the page")
                return 1
            made[kind] = href
        if probe_href is None:
            js, probe_href = bookmarklet(job, PROBE_TAIL)
            if not syntax_ok(js, "probe"):
                return 1

        rules = []
        for r in job["rules"]:
            if r["type"] == "setPrice":
                rules.append(f'set <code>{escape(r["match"])}</code> &rarr; <strong>{r["price"]:.2f}</strong>')
            elif r["type"] == "ensureRow":
                rules.append(f'ensure <code>{escape(r["rowLabel"])}</code> @ <strong>{r["price"]:.2f}</strong>')
            else:
                rules.append(f'remove <code>{escape(r["match"])}</code>')
        never = ", ".join(f"<code>{escape(n)}</code>" for n in job.get("neverTouch", [])) or "&mdash;"
        cards.append(f"""
<section class="job">
  <h2>{escape(job['name'])}</h2>
  <p>{escape(job.get('description',''))}</p>
  <p class="how">{escape(job.get('guidance',''))}</p>
  <p>
    <a class="bm audit" href="{escape(made['AUDIT'], quote=True)}">{escape(job['name'])} — AUDIT</a>
    <a class="bm apply" href="{escape(made['FIX'], quote=True)}">{escape(job['name'])} — FIX</a>
  </p>
  <details><summary>What this job does ({len(job['rules'])} rule(s))</summary>
    <ul>{''.join(f'<li>{r}</li>' for r in rules)}</ul>
    <p><strong>Never touched:</strong> {never}</p>
  </details>
</section>""")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ToastWeb Bulk Edit Tools</title>
<style>
 body {{ font:15px/1.6 system-ui,-apple-system,sans-serif; margin:0; background:#f6f7f9; color:#16181d }}
 .wrap {{ max-width:56rem; margin:2rem auto; padding:0 1rem }}
 h1 {{ font-size:22px; margin:0 0 .3rem }}
 .job {{ background:#fff; border:1px solid #e3e5e9; border-radius:10px; padding:1rem 1.2rem; margin:1rem 0 }}
 .job h2 {{ font-size:16px; margin:0 0 .3rem }}
 .how {{ color:#555; font-size:13.5px }}
 .bm {{ display:inline-block; padding:.55rem 1rem; margin:.3rem .5rem .3rem 0; border:2px solid #333;
        border-radius:8px; text-decoration:none; font-weight:600; color:#111; font-size:14px }}
 .audit {{ background:#eef6ff; border-color:#2c6fbb }}
 .apply {{ background:#ffecec; border-color:#b23b3b }}
 .probe {{ background:#f0f7ee; border-color:#4a7c46 }}
 code {{ background:#f1f2f4; padding:.05rem .3rem; border-radius:3px; font-size:12.5px }}
 .warn {{ background:#fff8e5; border-left:4px solid #e0a800; padding:.8rem 1rem; border-radius:0 8px 8px 0 }}
 details {{ margin-top:.5rem; font-size:13.5px }} summary {{ cursor:pointer; color:#2c6fbb }}
 ol li {{ margin:.25rem 0 }}
</style>
</head>
<body>
<div id="shared-nav"></div>
<div class="wrap">

<h1>ToastWeb Bulk Edit Tools</h1>
<p>One-click edits to the <strong>Location Prices</strong> table on a ToastWeb menu item,
run in your own signed-in browser. <strong>Drag the buttons below onto your bookmarks
bar</strong>, open a menu item in ToastWeb, and click them there.</p>

<div class="warn">
  <p><strong>Always click AUDIT first.</strong> It changes nothing — it lists exactly what
  FIX would do. FIX shows the same list and waits for you to confirm before it saves.</p>
  <p>Nothing is saved unless every single edit is re-read and verified. If anything does not
  stick, it stops <em>without saving</em> — reload the page to discard partial edits.</p>
  <p>These are safe to re-run. An item that is already correct reports "nothing to do", so
  you do not need to track which items you have done.</p>
</div>

{''.join(cards)}

<section class="job">
  <h2>Diagnostics</h2>
  <p>If a FIX says a location group is <em>not in this item's location list</em>, use this to
  see what the item actually offers. Read-only; copies full JSON to your clipboard.</p>
  <p><a class="bm probe" href="{escape(probe_href, quote=True)}">Probe options</a></p>
</section>

<section class="job">
  <h2>Adding a new job</h2>
  <ol>
    <li>Copy a file in <code>backend/toastweb_tools/jobs/</code> and edit the rules.</li>
    <li>Run <code>python backend/toastweb_tools/build_tools_page.py</code>.</li>
    <li>Commit, deploy, then re-drag that job's two buttons.</li>
  </ol>
  <p>Rule types: <code>setPrice</code> (match &rarr; price), <code>ensureRow</code>
  (add if missing / correct if wrong), <code>removeRow</code> (match &rarr; delete).</p>
  <p>The build refuses to publish a job whose match pattern is not anchored
  <code>^...$</code>, that collides with its own <code>ensureRow</code> labels, or that hits
  a known near-miss label such as <code>Tier 8 - Dine In - Company Only</code>. Every button
  is syntax-checked before the page is written.</p>
</section>

<p style="color:#777;font-size:12.5px">Generated by
<code>backend/toastweb_tools/build_tools_page.py</code> from <code>engine.js</code> +
<code>jobs/*.json</code>. Do not edit this file by hand.</p>
</div>
<script src="/shared-nav.js"></script>
</body>
</html>
""", encoding="utf-8")

    print(f"wrote {OUT}")
    for job in jobs:
        print(f"  {job['id']:<24} {job['name']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
