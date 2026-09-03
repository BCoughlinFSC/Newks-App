#!/usr/bin/env python3
"""
build_harness.py -- end-to-end test page per job, against the fixture.

For EVERY job in ../jobs it writes tests/harness-<id>.html containing the fixture, the
job, the real engine, and a GENERIC oracle derived from the job's own rules:

  * audit() runs clean and reports no problems
  * apply() reaches save (or legitimately has nothing to do)
  * every setPrice-matching row now holds that price
  * every ensureRow label exists exactly once at its price
  * every removeRow-matching row is gone from what would be saved
  * every OTHER row is byte-identical to the seed -- nothing collateral moved
  * a second audit() finds nothing to do (idempotent)

Because the oracle is derived from the rules, a new job is covered the moment you add it.

Open each generated file in a browser; it prints PASS/FAIL and sets document.title.
"""

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixture_item.html"
ENGINE = HERE.parent / "engine.js"
JOBS = HERE.parent / "jobs"

RUNNER = """
<pre id="out" style="background:#111;color:#eee;padding:1rem;white-space:pre-wrap;font-size:12px"></pre>
<script>
(async () => {
  const log = [];
  const say = (s) => { log.push(s); document.getElementById('out').textContent = log.join('\\n'); };
  let ok = true;
  const check = (cond, label, detail) => {
    if (!cond) { ok = false; say('FAIL  ' + label + (detail ? '  -> ' + detail : '')); }
    else say('ok    ' + label);
  };

  const JOB = window.__TOAST_JOB;
  const t = window.__newksToastTool;
  say('JOB: ' + JOB.name);
  check(!!t, 'engine exposed __newksToastTool');
  if (!t) { say('ABORT'); document.title = 'HARNESS FAIL'; return; }

  const seed = new Map(window.__SEED);
  const setRules = JOB.rules.filter((r) => r.type === 'setPrice');
  const ensures  = JOB.rules.filter((r) => r.type === 'ensureRow');
  const removes  = JOB.rules.filter((r) => r.type === 'removeRow');
  const veto = (l) => (JOB.neverTouch || []).some((s) => l.toLowerCase().includes(s.toLowerCase()));
  const m2 = (x) => Number(x).toFixed(2);

  // ---------- AUDIT ----------
  const a = t.audit();
  say(''); say('AUDIT: ' + JSON.stringify(a, null, 2)); say('');
  check(a.problems.length === 0, 'audit reports no problems', a.problems.join('; '));

  // audit must never propose touching a vetoed row
  const proposed = [].concat(a.setPrices, a.fixExisting, a.removeRows)
                     .map((s) => s.split(':')[0].trim());
  check(!proposed.some(veto), 'audit proposes nothing on a neverTouch row',
        proposed.filter(veto).join('; '));

  // ---------- APPLY ----------
  const r = await t.apply();
  say('APPLY: ' + JSON.stringify(r, null, 2)); say('');
  const finished = r.status === 'applied+save-clicked' || r.status === 'nothing-to-do';
  check(finished, 'apply finished cleanly', r.status +
        (r.problems ? ' :: ' + r.problems.join('; ') : ''));

  const saved = window.__saved;
  if (r.status === 'applied+save-clicked') {
    check(Array.isArray(saved), 'save was clicked');
  }
  if (!Array.isArray(saved)) {
    say(ok ? '==== PASS (nothing to do) ====' : '==== FAIL ====');
    document.title = ok ? 'HARNESS PASS' : 'HARNESS FAIL';
    return;
  }

  const byLabel = new Map(saved.map((row) => [row.label, row.price]));
  say('SAVED ROWS:');
  saved.forEach((row) => say('   ' + row.label + ' = ' + row.price));
  say('');

  // ---------- generic oracle ----------
  for (const rule of setRules) {
    const re = new RegExp(rule.match, 'i');
    for (const [label] of seed) {
      if (!re.test(label) || veto(label)) continue;
      if (removes.some((rm) => new RegExp(rm.match, 'i').test(label))) continue;
      check(byLabel.get(label) === m2(rule.price),
            'setPrice: "' + label + '" = ' + m2(rule.price),
            'got ' + byLabel.get(label));
    }
  }
  for (const e of ensures) {
    const hits = saved.filter((row) => row.label === e.rowLabel);
    check(hits.length === 1, 'ensureRow: "' + e.rowLabel + '" present exactly once',
          'found ' + hits.length);
    if (hits.length === 1) {
      check(hits[0].price === m2(e.price),
            'ensureRow: "' + e.rowLabel + '" = ' + m2(e.price), 'got ' + hits[0].price);
    }
  }
  for (const rule of removes) {
    const re = new RegExp(rule.match, 'i');
    for (const [label] of seed) {
      if (!re.test(label) || veto(label)) continue;
      check(!byLabel.has(label), 'removeRow: "' + label + '" is gone');
    }
  }

  // nothing collateral moved
  const targeted = (label) =>
    setRules.some((x) => new RegExp(x.match, 'i').test(label)) ||
    removes.some((x) => new RegExp(x.match, 'i').test(label)) ||
    ensures.some((e) => e.rowLabel === label);
  for (const [label, price] of seed) {
    if (targeted(label) && !veto(label)) continue;
    check(byLabel.get(label) === price,
          'untouched: "' + label + '" still ' + price, 'got ' + byLabel.get(label));
  }

  // ---------- idempotency ----------
  const again = t.audit();
  const left = again.setPrices.length + again.fixExisting.length
             + again.addRows.length + again.removeRows.length;
  check(left === 0 && again.problems.length === 0,
        'second audit finds nothing to do (idempotent)',
        JSON.stringify({ left: left, problems: again.problems }));

  say('');
  say(ok ? '==== PASS ====' : '==== FAIL ====');
  document.title = ok ? 'HARNESS PASS' : 'HARNESS FAIL';
})();
</script>
"""


def main() -> int:
    for f in (FIXTURE, ENGINE):
        if not f.exists():
            print(f"missing {f}")
            return 1
    fixture = FIXTURE.read_text(encoding="utf-8")
    engine = ENGINE.read_text(encoding="utf-8")

    made = []
    for jf in sorted(JOBS.glob("*.json")):
        job = json.loads(jf.read_text(encoding="utf-8"))
        out = HERE / f"harness-{job['id']}.html"
        out.write_text(
            fixture
            + "\n<script>window.__TOAST_JOB = " + json.dumps(job) + ";</script>\n"
            + "<script>\n" + engine + "\n</script>\n"
            + RUNNER,
            encoding="utf-8",
        )
        made.append((job["id"], out))
        print(f"wrote {out.name}  ({out.stat().st_size:,} bytes)  [{job['name']}]")
    if not made:
        print("no jobs found")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
