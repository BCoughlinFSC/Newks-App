/* engine.js -- generic bulk editor for the ToastWeb "Location Prices" table.
 *
 * Runs inside a browser you are already signed in to. Nothing here defeats Toast's bot
 * protection: you are a real signed-in user and this performs the same clicks and
 * keystrokes you would do by hand.
 *
 * The engine is JOB-AGNOSTIC. A job is data, injected as `window.__TOAST_JOB` by
 * build_tools_page.py, shaped like:
 *
 *   {
 *     id, name, description, guidance,
 *     neverTouch: ["Catering", "Box Salad", ...],   // substring veto, belt-and-braces
 *     rules: [
 *       { type: "setPrice",   match: "^...$", price: 12.99, label: "..." },
 *       { type: "ensureRow",  rowLabel: "Tier 8 - Dine In - Company Only",
 *                             locationId: "1100000019181590750", price: 13.49 },
 *       { type: "removeRow",  match: "^...$", label: "..." }
 *     ]
 *   }
 *
 * SAFETY INVARIANTS -- every one of these was bought with a real bug:
 *  - match patterns are WHOLE-LABEL and anchored. "Tier 8 - Dine In" must never match
 *    "Tier 8 - Dine In - Company Only", "Tier 8 Catering" or "Tier 8 - Box Salad Price".
 *    The builder REFUSES to ship an unanchored pattern.
 *  - neverTouch is a second veto layer on top of that.
 *  - Toast's hidden add template (id contains INDEX, class "template") is never a target;
 *    its label is the entire ~15k-char location picker and matches almost anything.
 *  - a row we just ADDED shows a <select>, whose innerText is every option concatenated.
 *    Label such rows from the SELECTED OPTION, never innerText, or the engine cannot see
 *    the row it just created.
 *  - Toast may insert a cloned row on a later tick, so adds poll rather than check once.
 *  - EVERY edit is re-read and verified before Save is clicked. Anything unexpected
 *    aborts WITHOUT saving rather than committing a half-done item.
 */
(() => {
  const JOB = window.__TOAST_JOB;
  if (!JOB) { console.error('__TOAST_JOB missing'); return; }

  const MAX_LABEL_LEN = 120;
  const TEMPLATE_ID = /INDEX/i;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const money = (n) => Number(n).toFixed(2);
  const sameMoney = (a, b) => Math.abs(Number(a) - Number(b)) < 0.005;

  const isTemplateRow = (tr) =>
    !!tr && ((tr.classList && tr.classList.contains('template')) || TEMPLATE_ID.test(tr.id || ''));

  const visible = (el) => {
    if (!el) return false;
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') return false;
    return el.getClientRects().length > 0;
  };

  const itemName = () => {
    const el = document.querySelector('.breadcrumb li:last-child, h1, .page-title');
    return el ? el.innerText.trim() : document.title;
  };

  const priceRows = () =>
    [...document.querySelectorAll('tr')].filter(
      (tr) => (tr.id && tr.id.startsWith('location-price-')) || tr.querySelector('td.location-price')
    );

  /* A saved row shows its location as text. A row we just added shows a <select>, and
   * innerText on a select yields EVERY option -- read the selected option instead. */
  const labelOf = (tr) => {
    const sel = tr.querySelector('select[name*="restaurantSet.id"]');
    if (sel) {
      const o = sel.options[sel.selectedIndex];
      return o ? o.text.replace(/\s+/g, ' ').trim() : '';
    }
    const td = tr.querySelector('td');
    return ((td && td.innerText) || '').replace(/\s+/g, ' ').trim();
  };

  const inputOf = (tr) => tr.querySelector('td.location-price input, td.td-input input');

  /* A row whose trashcan has been clicked stays in the DOM until Save: Toast hides the
   * trash and shows an "undo remove" link. Treat it as already gone, otherwise a second
   * audit reports "no trash control" on rows we deliberately removed. */
  const isStagedRemoved = (tr) => {
    const undo = tr.querySelector('a.undo');
    const trash = tr.querySelector('a.remove-entity');
    if (undo && visible(undo)) return true;
    return !!undo && !!trash && !visible(trash);
  };

  const vetoed = (label) =>
    (JOB.neverTouch || []).some((s) => label.toLowerCase().includes(String(s).toLowerCase()));

  /** Real, editable, non-template rows with a sane label. */
  const realRows = () =>
    priceRows().filter((tr) => {
      if (isTemplateRow(tr)) return false;
      const l = labelOf(tr);
      return !!l && l.length <= MAX_LABEL_LEN;
    });

  function setPrice(input, value) {
    const proto = Object.getPrototypeOf(input);
    const d = Object.getOwnPropertyDescriptor(proto, 'value');
    if (d && d.set) d.set.call(input, value);
    else input.value = value;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
  }

  const findSave = () =>
    [...document.querySelectorAll('button, input[type="submit"], a')].find((el) => {
      const t = (el.innerText || el.value || '').trim().toLowerCase();
      return t === 'save' && visible(el);
    });

  const templateRow = () =>
    document.querySelector('tr[id*="new-location-price"]') ||
    [...document.querySelectorAll('tr')].find(isTemplateRow);

  const isAddish = (el) => visible(el) && /^\+?\s*add$/i.test((el.innerText || '').trim());

  /* The page has other "Add" buttons (modifier groups). Scope to the price table. */
  function addButton() {
    const tr = templateRow();
    const table = tr && tr.closest('table');
    if (!table) return null;
    const inside = [...table.querySelectorAll('a, button')].find(isAddish);
    if (inside) return inside;
    let node = table.parentElement;
    for (let d = 0; node && d < 4; d++, node = node.parentElement) {
      const after = [...node.querySelectorAll('a, button')]
        .filter(isAddish)
        .find((el) => table.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING);
      if (after) return after;
    }
    return null;
  }

  const priceTableRows = () => {
    const tpl = templateRow();
    const table = tpl && tpl.closest('table');
    return [...(table || document).querySelectorAll('tr')].filter((tr) => !isTemplateRow(tr));
  };

  async function waitForNewRows(before, timeoutMs = 5000) {
    const t0 = Date.now();
    let last = [];
    while (Date.now() - t0 < timeoutMs) {
      last = priceTableRows().filter((tr) => !before.has(tr));
      if (last.length) return last;
      await sleep(100);
    }
    return last;
  }

  /* ---------------------------------------------------------------------------------
   * Plan
   * ------------------------------------------------------------------------------- */
  function buildPlan() {
    const setRules = (JOB.rules || []).filter((r) => r.type === 'setPrice');
    const rmRules = (JOB.rules || []).filter((r) => r.type === 'removeRow');
    const ensure = (JOB.rules || []).filter((r) => r.type === 'ensureRow');

    const priceChanges = [], removals = [], adds = [], fixes = [];
    const untouched = [], problems = [];
    const present = new Map();                       // label -> tr, for ensureRow

    for (const tr of realRows()) {
      const label = labelOf(tr);
      const input = inputOf(tr);
      present.set(label.toLowerCase(), tr);

      if (!visible(tr)) { untouched.push(label); continue; }
      if (isStagedRemoved(tr)) { untouched.push(label + '  [removed, pending save]'); continue; }
      if (vetoed(label)) { untouched.push(label + '  [neverTouch]'); continue; }

      // an ensureRow target that already exists -> price fix, not a price rule
      const ens = ensure.find((e) => e.rowLabel.toLowerCase() === label.toLowerCase());
      if (ens) {
        if (input && !sameMoney(input.value, ens.price)) {
          fixes.push({ label, from: input.value, to: money(ens.price), _input: input });
        }
        continue;
      }

      const rm = rmRules.find((r) => new RegExp(r.match, 'i').test(label));
      if (rm) {
        const trash = tr.querySelector('a.remove-entity');
        if (!trash || !visible(trash)) { problems.push(`no trash control on "${label}"`); continue; }
        removals.push({ label, price: input ? input.value : '', _tr: tr, _trash: trash });
        continue;
      }

      const sp = setRules.find((r) => new RegExp(r.match, 'i').test(label));
      if (sp) {
        if (!input) { problems.push(`no price input on "${label}"`); continue; }
        if (!sameMoney(input.value, sp.price)) {
          priceChanges.push({ label, from: input.value, to: money(sp.price), _input: input });
        }
        continue;
      }
      untouched.push(label);
    }

    // rows that need creating
    const tpl = templateRow();
    const sel = tpl && tpl.querySelector('select[name*="restaurantSet.id"]');
    for (const e of ensure) {
      if (present.has(e.rowLabel.toLowerCase())) continue;
      if (!sel) { problems.push('no location <select> in the template row'); continue; }
      // resolve by id, then CONFIRM the label. Option order is NOT stable between items.
      let opt = [...sel.options].find((o) => o.value === e.locationId);
      if (opt && opt.text.trim().toLowerCase() !== e.rowLabel.toLowerCase()) {
        problems.push(`id ${e.locationId} is "${opt.text.trim()}", expected "${e.rowLabel}"`);
        continue;
      }
      if (!opt) opt = [...sel.options].find((o) => o.text.trim().toLowerCase() === e.rowLabel.toLowerCase());
      if (!opt) {
        const co = [...sel.options].filter((o) => /company only/i.test(o.text)).map((o) => o.text.trim());
        problems.push(
          `"${e.rowLabel}" is not in this item's location list (${sel.options.length} options). ` +
          (co.length ? `Company Only options here: ${co.join(' | ')}.`
                     : 'This item has NO "Company Only" options at all.')
        );
        continue;
      }
      adds.push({ label: opt.text.trim(), value: opt.value, price: money(e.price) });
    }

    return { priceChanges, fixes, removals, adds, untouched, problems };
  }

  const api = {
    job: JOB,

    audit() {
      const p = buildPlan();
      return {
        item: itemName(),
        job: JOB.name,
        setPrices: p.priceChanges.map((r) => `${r.label}:  ${r.from} -> ${r.to}`),
        fixExisting: p.fixes.map((r) => `${r.label}:  ${r.from} -> ${r.to}`),
        addRows: p.adds.map((a) => `${a.label} @ ${a.price}`),
        removeRows: p.removals.map((r) => `${r.label}  ($${r.price})`),
        leftAlone: p.untouched,
        problems: p.problems,
      };
    },

    async apply() {
      const plan = buildPlan();
      if (plan.problems.length) {
        return { item: itemName(), status: 'ABORT-plan-problem', saved: false,
                 problems: plan.problems };
      }
      const total = plan.priceChanges.length + plan.fixes.length
                  + plan.adds.length + plan.removals.length;
      if (!total) return { item: itemName(), status: 'nothing-to-do', saved: false };

      for (const r of plan.priceChanges) setPrice(r._input, r.to);
      for (const r of plan.fixes) setPrice(r._input, r.to);
      for (const r of plan.removals) r._trash.click();

      const btn = plan.adds.length ? addButton() : null;
      if (plan.adds.length && !btn) {
        return { item: itemName(), status: 'ABORT-no-add-button', saved: false };
      }
      for (const a of plan.adds) {
        const before = new Set(priceTableRows());
        btn.click();
        const fresh = await waitForNewRows(before);
        if (fresh.length !== 1) {
          return { item: itemName(), status: 'ABORT-add-click-unclear', saved: false,
                   problems: [`clicking Add produced ${fresh.length} new row(s), expected 1`] };
        }
        const tr = fresh[0];
        const sel = tr.querySelector('select[name*="restaurantSet.id"]');
        const input = inputOf(tr);
        if (!sel || !input) {
          return { item: itemName(), status: 'ABORT-new-row-shape', saved: false,
                   problems: ['the new row has no select or no price input'] };
        }
        sel.value = a.value;
        sel.dispatchEvent(new Event('change', { bubbles: true }));
        setPrice(input, a.price);
      }

      // ---- verify everything before saving ----
      const after = buildPlan();
      const bad = [];
      for (const r of after.priceChanges) bad.push(`${r.label} is ${r.from}, wanted ${r.to}`);
      for (const r of after.fixes) bad.push(`${r.label} is ${r.from}, wanted ${r.to}`);
      for (const a of after.adds) bad.push(`${a.label} still missing`);
      for (const r of after.removals) bad.push(`${r.label} still present`);
      for (const a of plan.adds) {
        const row = priceTableRows().find((tr) => {
          const s = tr.querySelector('select[name*="restaurantSet.id"]');
          return s && s.value === a.value;
        });
        if (!row) { bad.push(`${a.label}: added row not found`); continue; }
        const input = inputOf(row);
        if (!input || !sameMoney(input.value, a.price)) {
          bad.push(`${a.label}: price is "${input ? input.value : '(none)'}", wanted ${a.price}`);
        }
      }
      if (bad.length) {
        return { item: itemName(), status: 'INCOMPLETE-do-not-save', saved: false, problems: bad };
      }

      const save = findSave();
      if (!save) return { item: itemName(), status: 'ABORT-no-save-button', saved: false };
      save.click();
      return {
        item: itemName(), job: JOB.name, status: 'applied+save-clicked', saved: true,
        setPrices: plan.priceChanges.map((r) => `${r.label}: ${r.from} -> ${r.to}`),
        fixExisting: plan.fixes.map((r) => `${r.label}: ${r.from} -> ${r.to}`),
        addRows: plan.adds.map((a) => `${a.label} @ ${a.price}`),
        removeRows: plan.removals.map((r) => r.label),
      };
    },

    /** Read-only: what location options does this item actually offer? */
    probe() {
      const tr = templateRow();
      const sel = tr && tr.querySelector('select[name*="restaurantSet.id"]');
      const options = sel ? [...sel.options].map((o) => ({ value: o.value, text: o.text.trim() })) : [];
      return {
        item: itemName(),
        templateRowId: tr ? tr.id : '(none)',
        addButtonText: addButton() ? addButton().innerText.trim() : '(not found)',
        totalOptions: options.length,
        wantedByThisJob: (JOB.rules || []).filter((r) => r.type === 'ensureRow').map((e) => ({
          rowLabel: e.rowLabel,
          locationId: e.locationId,
          foundById: options.some((o) => o.value === e.locationId),
          foundByLabel: options.some((o) => o.text.toLowerCase() === e.rowLabel.toLowerCase()),
        })),
        companyOnlyOptions: options.filter((o) => /company only/i.test(o.text)).map((o) => o.text),
        currentRows: realRows().map((tr2) => {
          const i = inputOf(tr2);
          return labelOf(tr2) + ' = ' + (i ? i.value : '');
        }),
      };
    },
  };

  window.__newksToastTool = api;
  return api.audit();
})();
