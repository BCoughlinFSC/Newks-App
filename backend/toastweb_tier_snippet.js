/* toastweb_tier_snippet.js -----------------------------------------------------------
 * Fix the Location Prices table on ONE ToastWeb menu item page.
 *
 * Runs inside a browser you are already signed in to. No bot-detection is involved or
 * defeated: you are a real signed-in user, this just performs the clicks and keystrokes
 * you would otherwise do by hand.
 *
 * TARGET END STATE (per item)
 *   *System Newk's Eatery             untouched
 *   Tier N - Dine In  (plain)         12.99          <- reverted; the sheet prices were
 *                                                      applied here BY MISTAKE
 *   Tier 8  - Dine In - Company Only  13.49          <- added if missing, fixed if wrong
 *   Tier 10 - Dine In - Company Only  13.99
 *   Tier 12 - Dine In - Company Only  14.49
 *   Custom MDH / Custom SWR           untouched
 *   Catering / Box Salad / Box Sandwich / To Go      untouched
 *
 * USAGE (DevTools console, on a menu item edit page):
 *     __newksTierBot.auditAll()    // read-only: everything that would change
 *     __newksTierBot.applyAll()    // do it, verify, then ONE save   (async - await it)
 *     __newksTierBot.probeAdd()    // read-only diagnostic of the add-row markup
 *
 * Phase-1 helpers (the Tier 8/10/12 plain-row removal, already completed) are kept for
 * reference: audit() / removeAndSave().
 *
 * applyAll() is IDEMPOTENT: safe to run on an item that is already correct, and safe on
 * an item that was never touched. It verifies every edit landed BEFORE saving, and
 * aborts without saving if anything did not stick.
 * ---------------------------------------------------------------------------------- */
(() => {
  const VARIANT = 'Dine In';

  /* The plain tier rows were repriced from the sheet by mistake. They go back to this. */
  const PLAIN_TIER_PRICE = 12.99;

  /* The new Company Only groups. These are the ones that SHOULD carry the sheet prices.
   * GUIDs confirmed identical across 3 probed items. */
  const COMPANY_ONLY_PRICES = { 8: 13.49, 10: 13.99, 12: 14.49 };
  const COMPANY_ONLY_IDS = {
    8: '1100000019181590750',
    10: '1100000019182217600',
    12: '1100000019182223700',
  };

  /* Toast keeps a hidden "Add" template row whose label contains the ENTIRE location
   * picker -- every store and price group in the company (~15k chars). It must never be
   * treated as a real row.
   * TEMPLATE_ID matches ONLY the INDEX placeholder, NOT the "new-location-price" prefix:
   * Toast clones the template into new-location-price-0 for genuinely added rows, so a
   * prefix match would discard rows we had just created. */
  const TEMPLATE_ID = /INDEX/i;
  const MAX_LABEL_LEN = 120;

  const isTemplateRow = (tr) =>
    !!tr && ((tr.classList && tr.classList.contains('template')) || TEMPLATE_ID.test(tr.id || ''));

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const esc = (s) => String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const money = (n) => Number(n).toFixed(2);
  const sameMoney = (a, b) => Math.abs(Number(a) - Number(b)) < 0.005;

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

  /* ---------------------------------------------------------------------------------
   * Row access
   * ------------------------------------------------------------------------------- */
  const priceRows = () =>
    [...document.querySelectorAll('tr')].filter(
      (tr) => (tr.id && tr.id.startsWith('location-price-')) || tr.querySelector('td.location-price')
    );

  /* A SAVED row shows its location as text in the first cell. A row we just added shows
   * a <select> instead -- and innerText on a select yields EVERY option concatenated
   * (~15k chars), which would blow past MAX_LABEL_LEN and make our own added row
   * invisible to buildPlan(). So for select rows, read the selected option. */
  const labelOf = (tr) => {
    const sel = tr.querySelector('select[name*="restaurantSet.id"]');
    if (sel) {
      const o = sel.options[sel.selectedIndex];
      return o ? o.text.replace(/\s+/g, ' ').trim() : '';
    }
    const td = tr.querySelector('td');
    return ((td && td.innerText) || '').replace(/\s+/g, ' ').trim();
  };

  const inputOf = (tr) =>
    tr.querySelector('td.location-price input, td.td-input input');

  /** Real, editable price rows: not the template, not the giant picker label. */
  const realRows = () =>
    priceRows().filter(
      (tr) => !isTemplateRow(tr) && labelOf(tr).length <= MAX_LABEL_LEN && labelOf(tr)
    );

  /* A plain tier row is the WHOLE label "[prefix - ]Tier N - Dine In".
   * Anchoring both ends is what keeps us off "Tier 8 - Dine In - Company Only",
   * "Tier 8 Catering", "Tier 8 - Box Salad Price", "Tier 7 & 8 - ...", "Tier 8 - To Go". */
  const plainTierRe = new RegExp(
    `^\\s*(?:[\\d.]+\\s*-\\s*)?tier\\s*(\\d+)\\s*-\\s*${esc(VARIANT)}\\s*$`, 'i'
  );
  const plainTierNumber = (label) => {
    const m = label.match(plainTierRe);
    return m ? Number(m[1]) : null;
  };

  /* A Company Only row is "Tier N - Dine In - Company Only". */
  const companyOnlyRe = new RegExp(
    `^\\s*(?:[\\d.]+\\s*-\\s*)?tier\\s*(\\d+)\\s*-\\s*${esc(VARIANT)}\\s*-\\s*company only\\s*$`, 'i'
  );
  const companyOnlyNumber = (label) => {
    const m = label.match(companyOnlyRe);
    return m ? Number(m[1]) : null;
  };
  const companyOnlyLabel = (n) => `Tier ${n} - ${VARIANT} - Company Only`;

  function setPrice(input, value) {
    const proto = Object.getPrototypeOf(input);
    const d = Object.getOwnPropertyDescriptor(proto, 'value');
    if (d && d.set) d.set.call(input, value);
    else input.value = value;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
  }

  function findSave() {
    return [...document.querySelectorAll('button, input[type="submit"], a')].find((el) => {
      const t = (el.innerText || el.value || '').trim().toLowerCase();
      return t === 'save' && visible(el);
    });
  }

  /* ---------------------------------------------------------------------------------
   * Add-row support
   * ------------------------------------------------------------------------------- */
  const templateRow = () =>
    document.querySelector('tr[id*="new-location-price"]') ||
    [...document.querySelectorAll('tr')].find(isTemplateRow);

  const isAddish = (el) => visible(el) && /^\+?\s*add$/i.test((el.innerText || '').trim());

  /* The page has several "Add" buttons (modifier groups etc). Scope to the Location
   * Prices one by anchoring on the template row's table. */
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

  /* Toast may insert the cloned row on a later tick, so poll rather than check once. */
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

  function probeAdd() {
    const tr = templateRow();
    const sel = tr && tr.querySelector('select[name*="restaurantSet.id"]');
    const options = sel ? [...sel.options].map((o, i) => ({ i, value: o.value, text: o.text.trim() })) : [];
    const wanted = Object.keys(COMPANY_ONLY_IDS).map(Number);
    return {
      item: itemName(),
      templateRowId: tr ? tr.id : '(none)',
      selectName: sel ? sel.name : '(no select)',
      priceInputName: tr && inputOf(tr) ? inputOf(tr).name : '(no price input)',
      addButtonText: addButton() ? addButton().innerText.trim() : '(not found)',
      totalOptions: options.length,
      exactMatches: options.filter((o) =>
        wanted.some((n) => o.text.toLowerCase() === companyOnlyLabel(n).toLowerCase())),
      allCompanyOnlyOptions: options.filter((o) => /company only/i.test(o.text)).map((o) => o.text),
      alreadyOnItem: realRows().map(labelOf).filter((l) => companyOnlyNumber(l) !== null),
    };
  }

  /* ---------------------------------------------------------------------------------
   * The plan
   * ------------------------------------------------------------------------------- */
  function buildPlan() {
    const revert = [];      // plain tier rows -> PLAIN_TIER_PRICE
    const fixCO = [];       // existing Company Only rows with a wrong price
    const addCO = [];       // Company Only rows that are missing entirely
    const untouched = [];
    const problems = [];

    const rows = realRows();
    const coPresent = new Set();

    for (const tr of rows) {
      const label = labelOf(tr);
      const input = inputOf(tr);
      if (!visible(tr) || !input) { untouched.push(label); continue; }

      const plainN = plainTierNumber(label);
      if (plainN !== null) {
        if (!sameMoney(input.value, PLAIN_TIER_PRICE)) {
          revert.push({ label, from: input.value, to: money(PLAIN_TIER_PRICE), _input: input });
        }
        continue;
      }

      const coN = companyOnlyNumber(label);
      if (coN !== null) {
        coPresent.add(coN);
        const target = COMPANY_ONLY_PRICES[coN];
        if (target === undefined) { untouched.push(label); continue; }
        if (!sameMoney(input.value, target)) {
          fixCO.push({ label, from: input.value, to: money(target), _input: input });
        }
        continue;
      }
      untouched.push(label);            // *System, Custom MDH/SWR, Catering, Box, To Go...
    }

    // which Company Only rows still need creating?
    const tpl = templateRow();
    const sel = tpl && tpl.querySelector('select[name*="restaurantSet.id"]');
    for (const n of Object.keys(COMPANY_ONLY_PRICES).map(Number)) {
      if (coPresent.has(n)) continue;
      const want = companyOnlyLabel(n);
      if (!sel) { problems.push('no location <select> in the template row'); continue; }
      // resolve by id, then CONFIRM the label -- option order is NOT stable between
      // items (306 vs 307 options), so position is never trusted.
      let opt = [...sel.options].find((o) => o.value === COMPANY_ONLY_IDS[n]);
      if (opt && opt.text.trim().toLowerCase() !== want.toLowerCase()) {
        problems.push(`id ${COMPANY_ONLY_IDS[n]} is "${opt.text.trim()}", expected "${want}"`);
        continue;
      }
      if (!opt) opt = [...sel.options].find((o) => o.text.trim().toLowerCase() === want.toLowerCase());
      if (!opt) {
        // Say WHY, so a missing option is diagnosable from the alert alone.
        const co = [...sel.options].filter((o) => /company only/i.test(o.text))
                                   .map((o) => o.text.trim());
        const near = [...sel.options]
          .filter((o) => new RegExp(`tier\\s*${n}\\b`, 'i').test(o.text))
          .map((o) => o.text.trim());
        problems.push(
          `"${want}" is not in this item's location list ` +
          `(${sel.options.length} options). ` +
          (co.length ? `Company Only options here: ${co.join(' | ')}. `
                     : 'This item has NO "Company Only" options at all. ') +
          (near.length ? `Tier ${n} options here: ${near.join(' | ')}.` : `No Tier ${n} options here.`)
        );
        continue;
      }
      addCO.push({ tier: n, label: opt.text.trim(), value: opt.value, price: money(COMPANY_ONLY_PRICES[n]) });
    }

    return { revert, fixCO, addCO, untouched, problems };
  }

  /* ---------------------------------------------------------------------------------
   * Public API
   * ------------------------------------------------------------------------------- */
  const api = {
    probeAdd,

    auditAll() {
      const p = buildPlan();
      return {
        item: itemName(),
        revertToPlain: p.revert.map((r) => `${r.label}:  ${r.from} -> ${r.to}`),
        fixCompanyOnly: p.fixCO.map((r) => `${r.label}:  ${r.from} -> ${r.to}`),
        addCompanyOnly: p.addCO.map((r) => `${r.label} @ ${r.price}`),
        leftAlone: p.untouched,
        problems: p.problems,
        addButtonFound: !!addButton(),
      };
    },

    async applyAll() {
      const plan = buildPlan();
      if (plan.problems.length) {
        return { item: itemName(), status: 'ABORT-plan-problem', saved: false,
                 problems: plan.problems };
      }
      if (!plan.revert.length && !plan.fixCO.length && !plan.addCO.length) {
        return { item: itemName(), status: 'nothing-to-do', saved: false };
      }

      // 1) price edits on existing rows
      for (const r of plan.revert) setPrice(r._input, r.to);
      for (const r of plan.fixCO) setPrice(r._input, r.to);

      // 2) create the missing Company Only rows
      const btn = plan.addCO.length ? addButton() : null;
      if (plan.addCO.length && !btn) {
        return { item: itemName(), status: 'ABORT-no-add-button', saved: false };
      }
      for (const a of plan.addCO) {
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

      // 3) verify EVERYTHING before saving
      const after = buildPlan();
      const bad = [];
      for (const r of after.revert) bad.push(`plain ${r.label} is ${r.from}, wanted ${r.to}`);
      for (const r of after.fixCO) bad.push(`${r.label} is ${r.from}, wanted ${r.to}`);
      for (const a of after.addCO) bad.push(`${a.label} still missing`);
      // and confirm each added row really carries the intended location + price
      for (const a of plan.addCO) {
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
      if (!save) {
        return { item: itemName(), status: 'ABORT-no-save-button', saved: false };
      }
      save.click();
      return {
        item: itemName(),
        status: 'fixed+save-clicked',
        saved: true,
        reverted: plan.revert.map((r) => `${r.label}: ${r.from} -> ${r.to}`),
        fixedCompanyOnly: plan.fixCO.map((r) => `${r.label}: ${r.from} -> ${r.to}`),
        addedCompanyOnly: plan.addCO.map((a) => `${a.label} @ ${a.price}`),
      };
    },

    /* ----- phase 1 (plain Tier 8/10/12 removal) -- already completed, kept for
     * reference and in case a row needs removing again. ----- */
    audit() {
      const targets = realRows().filter((tr) => {
        const n = plainTierNumber(labelOf(tr));
        return n !== null && COMPANY_ONLY_PRICES[n] !== undefined;
      });
      return {
        item: itemName(),
        willRemove: targets.map((tr) => `${labelOf(tr)}  ($${inputOf(tr) ? inputOf(tr).value : ''})`),
        willKeep: realRows().filter((tr) => !targets.includes(tr)).map(labelOf),
      };
    },

    removeAndSave() {
      const targets = realRows().filter((tr) => {
        const n = plainTierNumber(labelOf(tr));
        return n !== null && COMPANY_ONLY_PRICES[n] !== undefined;
      });
      if (!targets.length) return { item: itemName(), status: 'no-match', saved: false };
      const labels = targets.map(labelOf);
      for (const tr of targets) {
        const trash = tr.querySelector('a.remove-entity');
        if (!trash) return { item: itemName(), status: 'ABORT-no-trash', saved: false };
        trash.click();
      }
      const still = realRows().filter((tr) => {
        const n = plainTierNumber(labelOf(tr));
        return n !== null && COMPANY_ONLY_PRICES[n] !== undefined && visible(tr)
          && visible(tr.querySelector('a.remove-entity'));
      });
      if (still.length) {
        return { item: itemName(), status: 'INCOMPLETE-do-not-save', saved: false,
                 stillLive: still.map(labelOf) };
      }
      const save = findSave();
      if (!save) return { item: itemName(), status: 'ABORT-no-save-button', saved: false };
      save.click();
      return { item: itemName(), status: 'removed+save-clicked', saved: true, removed: labels };
    },
  };

  window.__newksTierBot = api;
  return api.auditAll();
})();
