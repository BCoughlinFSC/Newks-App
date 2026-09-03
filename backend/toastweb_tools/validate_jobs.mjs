/* validate_jobs.mjs -- check every job config with REAL JavaScript regex semantics
 * (the engine runs in a browser, so validating with Python's `re` would be a proxy).
 *
 * Refuses anything that could repeat a bug we have already hit:
 *   - an unanchored match pattern. "Tier 8 - Dine In" as a substring also matches
 *     "Tier 8 - Dine In - Company Only", which is a DIFFERENT price group.
 *   - a setPrice/removeRow pattern that also matches one of the job's own ensureRow
 *     labels -- i.e. the job would fight itself.
 *   - a neverTouch entry that vetoes the job's own ensureRow label, making it a no-op.
 *
 * Usage: node validate_jobs.mjs <jobsDir>
 */
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';

const dir = process.argv[2];
const files = readdirSync(dir).filter((f) => f.endsWith('.json')).sort();

// labels that must NEVER be hit by a plain-tier style rule -- the near-miss set that
// caused real damage risk in Aug 2026. Every job is checked against these.
const CANARIES = [
  'Tier 8 - Dine In - Company Only',
  'Tier 10 - Dine In - Company Only',
  'Tier 12 - Dine In - Company Only',
  'Tier 8 Catering',
  'Tier 8 - Box Salad Price',
  'Tier 7 & 8 - Box Sandwich & Wrap',
  '1.2 - Tier 8 - To Go',
  "*System Newk's Eatery",
  '2.8 - Custom MDH - Dine In',
];

let ok = true;
const failed = new Set();
const fail = (f, m) => { ok = false; failed.add(f); console.log(`FAIL  ${f}: ${m}`); };

console.log(`validating ${files.length} job(s) in ${dir}`);
for (const f of files) {
  let job;
  try {
    job = JSON.parse(readFileSync(join(dir, f), 'utf8'));
  } catch (e) {
    fail(f, `not valid JSON -- ${e.message}`);
    continue;
  }

  for (const k of ['id', 'name', 'rules']) {
    if (!job[k]) fail(f, `missing required field "${k}"`);
  }
  if (!Array.isArray(job.rules) || !job.rules.length) {
    fail(f, 'rules must be a non-empty array');
    continue;
  }

  const ensureLabels = job.rules.filter((r) => r.type === 'ensureRow').map((r) => r.rowLabel);
  const matchers = [];

  for (const [i, r] of job.rules.entries()) {
    const where = `rule ${i} (${r.type})`;
    if (!['setPrice', 'ensureRow', 'removeRow'].includes(r.type)) {
      fail(f, `${where}: unknown type`);
      continue;
    }
    if (r.type === 'ensureRow') {
      if (!r.rowLabel) fail(f, `${where}: needs rowLabel`);
      if (!r.locationId) fail(f, `${where}: needs locationId`);
      if (typeof r.price !== 'number') fail(f, `${where}: needs a numeric price`);
      if (!/^\d{6,}$/.test(String(r.locationId || ''))) {
        fail(f, `${where}: locationId "${r.locationId}" does not look like a Toast id`);
      }
      continue;
    }
    if (!r.match) { fail(f, `${where}: needs a match pattern`); continue; }
    if (!r.match.startsWith('^') || !r.match.endsWith('$')) {
      fail(f, `${where}: match must be anchored ^...$ -- an unanchored pattern matches ` +
              `longer labels like "... - Company Only"`);
      continue;
    }
    let re;
    try { re = new RegExp(r.match, 'i'); }
    catch (e) { fail(f, `${where}: bad regex -- ${e.message}`); continue; }
    if (r.type === 'setPrice' && typeof r.price !== 'number') {
      fail(f, `${where}: setPrice needs a numeric price`);
    }
    matchers.push({ where, re });
  }

  // a rule must not target the job's own ensureRow labels
  for (const { where, re } of matchers) {
    for (const lbl of ensureLabels) {
      if (re.test(lbl)) {
        fail(f, `${where} also matches this job's own ensureRow label "${lbl}" -- ` +
                `the job would fight itself`);
      }
    }
  }

  // neverTouch must not veto the job's own targets
  for (const nt of job.neverTouch || []) {
    for (const lbl of ensureLabels) {
      if (lbl.toLowerCase().includes(String(nt).toLowerCase())) {
        fail(f, `neverTouch "${nt}" vetoes this job's own ensureRow label "${lbl}"`);
      }
    }
  }

  // canaries: anything a rule hits must be deliberate
  const veto = (l) => (job.neverTouch || []).some((s) => l.toLowerCase().includes(String(s).toLowerCase()));
  for (const { where, re } of matchers) {
    for (const c of CANARIES) {
      if (re.test(c) && !veto(c) && !ensureLabels.includes(c)) {
        fail(f, `${where} matches the canary label "${c}" and neverTouch does not veto it`);
      }
    }
  }

  if (failed.has(f)) continue;
  const counts = job.rules.reduce((a, r) => ({ ...a, [r.type]: (a[r.type] || 0) + 1 }), {});
  console.log(`  ok  ${f}  ${job.name} — ` +
              Object.entries(counts).map(([k, v]) => `${v} ${k}`).join(', '));
}

console.log(ok ? 'PASS - all jobs valid' : 'FAIL - see above');
process.exit(ok ? 0 : 1);
