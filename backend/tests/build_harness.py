#!/usr/bin/env python3
"""
build_harness.py -- build a self-contained end-to-end test page for the tier snippet.

Inlines fixture_toast_item.html + toastweb_tier_snippet.js into tests/harness.html, then
runs auditAll() and applyAll() against the fixture and asserts the outcome. Open the
generated file in a browser; the page prints PASS/FAIL and the details.

This exists because two separate bugs shipped as "the button does nothing":
  1. a cloned add-row was discarded by our own template filter
  2. mangled newline escapes made the bookmarklet a syntax error
Neither was catchable by matcher unit tests alone -- both needed a real DOM.
"""

from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixture_toast_item.html"
SNIPPET = HERE.parent / "toastweb_tier_snippet.js"
OUT = HERE / "harness.html"

RUNNER = """
<pre id="out" style="background:#111;color:#eee;padding:1rem;white-space:pre-wrap"></pre>
<script>
(async () => {
  const log = [];
  const say = (s) => { log.push(s); document.getElementById('out').textContent = log.join('\\n'); };
  let ok = true;
  const check = (cond, label, detail) => {
    if (!cond) { ok = false; say('FAIL  ' + label + (detail ? '  -> ' + detail : '')); }
    else say('ok    ' + label);
  };

  const b = window.__newksTierBot;
  check(!!b, 'snippet exposed __newksTierBot');
  if (!b) { say('ABORT'); return; }

  // ---------- AUDIT ----------
  const a = b.auditAll();
  say('');
  say('AUDIT: ' + JSON.stringify(a, null, 2));
  say('');

  check(a.problems.length === 0, 'audit reports no problems', a.problems.join('; '));
  check(a.revertToPlain.length === 10,
        'reverts exactly the 10 plain tier rows', 'got ' + a.revertToPlain.length);
  check(a.revertToPlain.every((s) => / -> 12\\.99$/.test(s)),
        'every revert targets 12.99');
  check(a.addCompanyOnly.length === 1 && /Tier 8 - Dine In - Company Only @ 13\\.49/.test(a.addCompanyOnly[0]),
        'adds Tier 8 Company Only @ 13.49', a.addCompanyOnly.join('; '));
  check(a.fixCompanyOnly.length === 0,
        'Tier 10 + 12 Company Only already correct, not touched', a.fixCompanyOnly.join('; '));
  const alone = a.leftAlone.join(' | ');
  check(/\\*System/.test(alone) && /Custom MDH/.test(alone) && /Custom SWR/.test(alone),
        'leaves *System, Custom MDH, Custom SWR alone', alone);
  check(!a.revertToPlain.some((s) => /Company Only/.test(s)),
        'no Company Only row is treated as a plain tier');

  // ---------- APPLY ----------
  const r = await b.applyAll();
  say('');
  say('APPLY: ' + JSON.stringify(r, null, 2));
  say('');
  check(r.status === 'fixed+save-clicked', 'applyAll reached save', r.status
        + (r.problems ? ' :: ' + r.problems.join('; ') : ''));
  check(r.saved === true, 'save was clicked');

  // ---------- what the page would have submitted ----------
  const saved = window.__saved || [];
  say('SAVED ROWS:');
  saved.forEach((s) => say('   ' + s));
  say('');
  const find = (name) => saved.find((s) => s.indexOf(name + ' =') === 0
                                        || s.indexOf(name) === 0);
  check(saved.length === 16, 'saved 16 rows (15 seeded + 1 added)', 'got ' + saved.length);
  check(/= 13\\.49$/.test(find('Tier 8 - Dine In - Company Only') || ''),
        'Tier 8 Company Only saved at 13.49', find('Tier 8 - Dine In - Company Only'));
  check(/= 13\\.99$/.test(find('Tier 10 - Dine In - Company Only') || ''),
        'Tier 10 Company Only still 13.99');
  check(/= 14\\.49$/.test(find('Tier 12 - Dine In - Company Only') || ''),
        'Tier 12 Company Only still 14.49');
  for (const n of [7, 9, 11, 13, 14, 15, 16, 17, 18, 19]) {
    const row = saved.find((s) => new RegExp('Tier ' + n + ' - Dine In =').test(s));
    check(/= 12\\.99$/.test(row || ''), 'plain Tier ' + n + ' reverted to 12.99', row);
  }
  check(/= 14\\.39$/.test(find('2.8 - Custom MDH - Dine In') || ''),
        'Custom MDH untouched at 14.39', find('2.8 - Custom MDH - Dine In'));
  check(/= 12\\.99$/.test(find("*System Newk's Eatery") || ''),
        '*System untouched at 12.99');

  // ---------- idempotency ----------
  const again = b.auditAll();
  check(again.revertToPlain.length === 0 && again.fixCompanyOnly.length === 0
        && again.addCompanyOnly.length === 0,
        'second audit finds nothing to do (idempotent)',
        JSON.stringify({r: again.revertToPlain.length, f: again.fixCompanyOnly.length,
                        a: again.addCompanyOnly.length}));

  say('');
  say(ok ? '==== PASS ====' : '==== FAIL ====');
  document.title = ok ? 'HARNESS PASS' : 'HARNESS FAIL';
})();
</script>
"""


def main() -> int:
    for f in (FIXTURE, SNIPPET):
        if not f.exists():
            print(f"missing {f}")
            return 1
    fixture = FIXTURE.read_text(encoding="utf-8")
    snippet = SNIPPET.read_text(encoding="utf-8")
    OUT.write_text(
        fixture + "\n<script>\n" + snippet + "\n</script>\n" + RUNNER,
        encoding="utf-8",
    )
    print(f"wrote {OUT}  ({OUT.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
