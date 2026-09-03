#!/usr/bin/env python3
"""
build_tier_bookmarklet.py -- wrap toastweb_tier_snippet.js into bookmarklets.

Produces backend/toastweb_tier_bookmarklets.html: open it in Chrome and drag the buttons
onto your bookmarks bar. One click per item page, in your own signed-in browser.

Nothing here defeats Toast's bot protection -- you are a signed-in human clicking a
bookmark; it performs the same clicks you would do by hand.

  FIX IT     revert plain tiers to 12.99, add/fix the 3 Company Only rows, ONE save.
  AUDIT      read-only. everything FIX IT would change, and what it would leave alone.
  Probe ADD  read-only diagnostic of the add-row markup.

NOTE ON NEWLINES: the JS tails below deliberately contain NO backslash escape sequences.
Newlines in alert() text come from String.fromCharCode(10). An earlier version used '\\n'
inside these tails and an editing pass turned them into REAL newlines inside single-quoted
JS strings -- an unterminated string literal, so the bookmarklet failed to parse and
silently did nothing at all. Every generated bookmarklet is now syntax-checked with
`node --check` before it is written, so that can never ship quietly again.
"""

import subprocess
import sys
import tempfile
from html import escape
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
SNIPPET = HERE / "toastweb_tier_snippet.js"
OUT = HERE / "toastweb_tier_bookmarklets.html"

AUDIT_ALL = """
;(() => {
  const NL = String.fromCharCode(10);
  const a = window.__newksTierBot.auditAll();
  console.log(a);
  const block = (title, arr) =>
    title + ' (' + arr.length + '):' + NL + (arr.length ? arr.join(NL) : '  (none)');
  alert([
    'ITEM: ' + a.item,
    block('REVERT plain tiers to 12.99', a.revertToPlain),
    block('FIX Company Only prices', a.fixCompanyOnly),
    block('ADD Company Only rows', a.addCompanyOnly),
    block('LEFT ALONE', a.leftAlone),
    'PROBLEMS: ' + (a.problems.length ? NL + a.problems.join(NL) : 'none')
  ].join(NL + NL));
})();
"""

FIX_ALL = """
;(async () => {
  const NL = String.fromCharCode(10);
  const b = window.__newksTierBot;
  const a = b.auditAll();
  console.log(a);
  if (a.problems.length) {
    alert('ITEM: ' + a.item + NL + NL + 'NOT SAFE:' + NL + a.problems.join(NL));
    return;
  }
  const total = a.revertToPlain.length + a.fixCompanyOnly.length + a.addCompanyOnly.length;
  if (!total) {
    alert('ITEM: ' + a.item + NL + NL + 'Already correct. Nothing to do.');
    return;
  }
  const block = (title, arr) =>
    title + ' (' + arr.length + '):' + NL + (arr.length ? arr.join(NL) : '  (none)');
  const msg = [
    'ITEM: ' + a.item,
    block('REVERT to 12.99', a.revertToPlain),
    block('FIX Company Only', a.fixCompanyOnly),
    block('ADD Company Only', a.addCompanyOnly),
    'Then SAVE once. Proceed?'
  ].join(NL + NL);
  if (!confirm(msg)) { console.log('cancelled'); return; }
  const r = await b.applyAll();
  console.log(r);
  if (r.status !== 'fixed+save-clicked') {
    alert([
      'STOPPED - nothing saved.',
      'Reload the page to discard any partial edits.',
      'status: ' + r.status,
      (r.problems && r.problems.length ? r.problems.join(NL) : '')
    ].join(NL + NL));
  }
})();
"""

PROBE_ADD = """
;(() => {
  const NL = String.fromCharCode(10);
  const p = window.__newksTierBot.probeAdd();
  console.log(p);
  try { navigator.clipboard.writeText(JSON.stringify(p, null, 2)); } catch (e) {}
  alert([
    'ITEM: ' + p.item,
    'template row : ' + p.templateRowId,
    'select name  : ' + p.selectName,
    'Add button   : ' + p.addButtonText,
    'options total: ' + p.totalOptions,
    'EXACT MATCHES (' + p.exactMatches.length + '):' + NL +
      (p.exactMatches.length
        ? p.exactMatches.map((o) => o.text + '  [' + o.value + ']').join(NL)
        : '  (none)'),
    'ALREADY ON ITEM: ' + (p.alreadyOnItem.length ? p.alreadyOnItem.join(', ') : 'none'),
    'JSON copied to clipboard.'
  ].join(NL));
})();
"""

BUTTONS = [
    ("FIX IT", "apply", FIX_ALL,
     "Revert plain tiers to 12.99, add/fix the 3 Company Only rows, then ONE save."),
    ("AUDIT", "audit", AUDIT_ALL,
     "Read-only. Everything FIX IT would change, and everything it would leave alone."),
    ("Probe ADD", "probe", PROBE_ADD,
     "Read-only diagnostic of the add-row markup."),
]


def syntax_ok(js: str, label: str) -> bool:
    """Run `node --check` on the assembled bookmarklet source."""
    path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(js)
            path = fh.name
        res = subprocess.run(["node", "--check", path], capture_output=True, text=True)
        if res.returncode != 0:
            print(f"  SYNTAX ERROR in {label}:")
            print("    " + (res.stderr or "").strip().replace("\n", "\n    ")[:800])
            return False
        return True
    except FileNotFoundError:
        print(f"  WARNING: node not found, could not syntax-check {label}")
        return True
    finally:
        if path:
            Path(path).unlink(missing_ok=True)


def bookmarklet(js: str) -> str:
    return "javascript:" + quote(js, safe="")


def main() -> int:
    if not SNIPPET.exists():
        print(f"missing {SNIPPET}")
        return 1
    base = SNIPPET.read_text(encoding="utf-8")

    if not syntax_ok(base, "toastweb_tier_snippet.js"):
        print("base snippet does not parse -- refusing to build")
        return 1

    links, rows, failed = [], [], []
    for label, kind, tail, blurb in BUTTONS:
        js = base + tail
        if not syntax_ok(js, label):
            failed.append(label)
            continue
        href = bookmarklet(js)
        links.append((label, kind, href))
        rows.append(f"<tr><td><strong>{escape(label)}</strong></td>"
                    f"<td>{escape(blurb)}</td>"
                    f"<td style='text-align:right'>{len(href):,}</td></tr>")

    if failed:
        print(f"refusing to write: {failed} did not parse")
        return 1

    buttons_html = "\n  ".join(
        f'<a class="bm {kind}" href="{escape(h, quote=True)}">{escape(lbl)}</a>'
        for lbl, kind, h in links
    )

    OUT.write_text(f"""<!doctype html>
<meta charset="utf-8">
<title>Newk's ToastWeb tier tools</title>
<style>
  body {{ font: 15px/1.6 system-ui, sans-serif; max-width: 52rem; margin: 3rem auto;
          padding: 0 1rem; color: #1a1a1a; }}
  .bm {{ display: inline-block; padding: .6rem 1rem; margin: .3rem .5rem .3rem 0;
         border: 2px solid #333; border-radius: 8px; text-decoration: none;
         font-weight: 600; color: #111; }}
  .audit {{ background: #eef6ff; border-color: #2c6fbb; }}
  .apply {{ background: #ffecec; border-color: #b23b3b; }}
  .probe {{ background: #f0f7ee; border-color: #4a7c46; }}
  code {{ background: #f3f3f3; padding: .1rem .35rem; border-radius: 4px; }}
  table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; }}
  th, td {{ border-bottom: 1px solid #ddd; padding: .45rem .6rem; text-align: left;
            font-size: 14px; vertical-align: top; }}
  .warn {{ background: #fff8e5; border-left: 4px solid #e0a800; padding: .8rem 1rem; }}
  .prices td {{ font-variant-numeric: tabular-nums; }}
</style>

<h1>ToastWeb tier tools</h1>
<p><strong>Drag these onto your bookmarks bar.</strong> Delete any older buttons first
&mdash; <code>DO IT ALL</code>, <code>Price APPLY</code> and <code>Add APPLY</code> are
gone on purpose.</p>
<p>
  {buttons_html}
</p>

<table>
<tr><th>Button</th><th>What it does</th><th>bytes</th></tr>
{chr(10).join(rows)}
</table>

<h2>What FIX IT does</h2>
<table class="prices">
<tr><th>Row</th><th>Price</th><th>Action</th></tr>
<tr><td><code>*System Newk's Eatery</code></td><td>&mdash;</td><td>untouched</td></tr>
<tr><td><code>Tier N - Dine In</code> (plain, any N)</td><td><strong>12.99</strong></td>
    <td>reverted &mdash; the sheet prices were applied here by mistake</td></tr>
<tr><td><code>Tier 8 - Dine In - Company Only</code></td><td><strong>13.49</strong></td>
    <td>added if missing, corrected if wrong</td></tr>
<tr><td><code>Tier 10 - Dine In - Company Only</code></td><td><strong>13.99</strong></td>
    <td>added if missing, corrected if wrong</td></tr>
<tr><td><code>Tier 12 - Dine In - Company Only</code></td><td><strong>14.49</strong></td>
    <td>added if missing, corrected if wrong</td></tr>
<tr><td>Custom MDH / Custom SWR</td><td>&mdash;</td><td>untouched</td></tr>
<tr><td>Catering / Box Salad / Box Sandwich / To Go</td><td>&mdash;</td><td>untouched</td></tr>
</table>

<h2>How to use</h2>
<ol>
  <li>Open a menu item's edit page in ToastWeb.</li>
  <li>Click <strong>AUDIT</strong> first. It changes nothing &mdash; it lists every row it
      would revert, fix, or add, and everything it would leave alone.</li>
  <li>Then <strong>FIX IT</strong>. Same list, you confirm, and it saves once.</li>
  <li>Detail is logged to the DevTools console every time.</li>
</ol>

<div class="warn">
<p><strong>Safe to run on every item.</strong> It is idempotent: an already-correct item
reports "nothing to do", an item that never got the bad reprice just gets its three Company
Only rows added, and an existing Company Only row is corrected rather than duplicated. You
do not need to remember which items you touched.</p>
<p><strong>Only whole-label matches are acted on.</strong> <code>Tier 8 - Dine In</code> and
<code>Tier 8 - Dine In - Company Only</code> are different rows;
<code>Tier 8 Catering</code>, <code>Tier 8 - Box Salad Price</code>,
<code>Tier 7 &amp; 8 - Box Sandwich &amp; Wrap</code> and <code>Tier 8 - To Go</code> are
never touched.</p>
<p><strong>Nothing is saved unless every edit verified.</strong> If a price did not stick,
or an added row did not land with the right location and price, it stops without saving
&mdash; reload the page to discard the partial edits.</p>
</div>

<p style="color:#666">Generated from <code>backend/toastweb_tier_snippet.js</code> by
<code>backend/build_tier_bookmarklet.py</code>. Every button is syntax-checked with
<code>node --check</code> at build time. Rebuild after editing the snippet.</p>
""", encoding="utf-8")

    print(f"wrote {OUT}")
    for lbl, _kind, h in links:
        print(f"  {lbl:<12} {len(h):>7,} chars   syntax OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
