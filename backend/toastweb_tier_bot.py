#!/usr/bin/env python3
"""
toastweb_tier_bot.py -- bulk-remove specific Location Price tiers from ToastWeb menu items.

Drives real Chrome via Playwright against toasttab.com. You log in yourself the first
time; the session persists in a dedicated Chrome profile so later runs go straight to work.

Three modes, in the order you should use them:

  1. --probe      Open the FIRST matching item and dump the real DOM structure of the
                  Location Prices table + Save button. Changes nothing. Use this to
                  confirm the selectors below actually match what Toast is serving.

  2. (default)    Dry run. Walk every matching item, report which tier rows WOULD be
                  removed, never click a trashcan, never save. Writes a CSV report.

  3. --apply      For real: click the trashcan on each matching tier row, verify the row
                  went away, click Save, verify the save landed. Screenshot per item.

Examples
--------
  # 0) confirm selectors against the live page
  python toastweb_tier_bot.py --probe

  # 1) see what it would do to all "pick 3 entree" items
  python toastweb_tier_bot.py --search "pick 3 entree" --tiers "Tier 8,Tier 10,Tier 12"

  # 2) do it, but only the first 3 items, so you can eyeball the result in Toast
  python toastweb_tier_bot.py --search "pick 3 entree" --tiers "Tier 8,Tier 10,Tier 12" \
      --apply --limit 3

  # 3) the rest
  python toastweb_tier_bot.py --search "pick 3 entree" --tiers "Tier 8,Tier 10,Tier 12" --apply
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
    from playwright.sync_api import TimeoutError as PWTimeout
except ImportError:
    sys.exit("Playwright not installed. Run:  python -m pip install playwright")


ITEMS_URL = "https://www.toasttab.com/restaurants/admin/menuitems"

# --- selectors -------------------------------------------------------------------
# Location price rows carry an id like  <tr id="location-price-1100000013336892196">
# and a price cell  <td class="td-input location-price">, with the trashcan in
# <td class="td-actions"><a class="remove-entity"><i class="icon-trash" title="Remove price">
PRICE_ROW_JS = """
() => {
  const isPriceRow = (tr) =>
    (tr.id && tr.id.startsWith('location-price-')) || !!tr.querySelector('td.location-price');
  const vis = (el) => {
    if (!el) return false;
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') return false;
    return el.getClientRects().length > 0;
  };
  return [...document.querySelectorAll('tr')].filter(isPriceRow).map((tr, i) => {
    const tds = [...tr.querySelectorAll('td')];
    const trash = tr.querySelector('a.remove-entity, a[class*="remove"]');
    const undo  = tr.querySelector('a.undo');
    const input = tr.querySelector('td.location-price input, input.location-price, td.td-input input');
    return {
      idx: i,
      id: tr.id || '',
      label: (tds[0] ? tds[0].innerText : '').replace(/\\s+/g, ' ').trim(),
      price: input ? input.value : '',
      row_text: tr.innerText.replace(/\\s+/g, ' ').trim(),
      row_class: tr.className || '',
      has_trash: !!trash,
      trash_visible: vis(trash),
      undo_visible: vis(undo),
      row_visible: vis(tr),
    };
  });
}
"""

SAVE_SELECTORS = [
    'button:has-text("Save")',
    'input[type="submit"][value="Save"]',
    'a:has-text("Save")',
    '#save',
    '.js-save',
]


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# A real price-row label looks like "1.2 - Tier 8 - Dine In" (the numeric prefix is
# Toast's price-group ordering and is optional). Anchoring both ends is what keeps us off
# "Tier 8 - Dine In - Company Only", which is a DIFFERENT price group.
def tier_matchers(tiers: list[str], variant: str = "Dine In",
                  loose: bool = False) -> list[tuple[str, re.Pattern]]:
    """Build label matchers for the target tiers.

    Strict (default): whole label must be "[prefix - ]Tier N - <variant>".
    Loose: tier token anywhere in the label. 'Tier 8' never matches 'Tier 18'/'Tier 80'.
    """
    out = []
    v = re.escape(variant.strip()) if variant.strip() else r"[^-]+"
    for raw in tiers:
        t = raw.strip()
        if not t:
            continue
        m = re.fullmatch(r"tier\s*(\d+)", t, re.I)
        if m:
            n = m.group(1)
            pat = (rf"(?<![\w.])tier\s*{n}(?![\d])" if loose
                   else rf"^\s*(?:[\d.]+\s*-\s*)?tier\s*{n}\s*-\s*{v}\s*$")
        else:
            pat = re.escape(t) if loose else rf"^\s*(?:[\d.]+\s*-\s*)?{re.escape(t)}\s*$"
        out.append((t, re.compile(pat, re.I)))
    return out


# Toast keeps a hidden "Add" template row whose label contains the ENTIRE location
# picker -- every store and price group in the company. It must never be a target.
TEMPLATE_ID_RE = re.compile(r"^new-location-price|INDEX", re.I)
MAX_LABEL_LEN = 120  # a genuine location label is short; the template's is ~15k chars


def row_targets(rows, matchers, require: str) -> list[dict]:
    """Pick the price rows whose location label names one of the target tiers."""
    hits = []
    for r in rows:
        label = r["label"]
        if not label:
            continue
        # --- guards against the hidden template / add-new row --------------------
        if TEMPLATE_ID_RE.search(r.get("id") or ""):
            continue
        if len(label) > MAX_LABEL_LEN:
            continue
        if not r.get("row_visible", True):
            continue
        if not r.get("trash_visible", True):
            continue
        # ------------------------------------------------------------------------
        if require and require.lower() not in label.lower():
            continue
        for name, pat in matchers:
            if pat.search(label):
                hits.append({**r, "matched_tier": name})
                break
    return hits


# --- browser plumbing ------------------------------------------------------------
def open_browser(pw, profile_dir: Path, headless: bool):
    profile_dir.mkdir(parents=True, exist_ok=True)
    return pw.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        channel="chrome",          # real Chrome, not bundled Chromium
        headless=headless,
        viewport={"width": 1600, "height": 1000},
        # NOTE: deliberately no automation-hiding flags (e.g.
        # --disable-blink-features=AutomationControlled). Toast fronts this app with a
        # bot-detection service; defeating it is not something this script will attempt.
        # If you hit "Performing security verification", see the README note -- the answer
        # is a sanctioned path (Toast bulk edit / API / an extension driving your own
        # verified session), not a stealthier browser.
    )


def _safe_count(page, selector: str) -> int:
    """Locator.count() that tolerates the page navigating out from under us."""
    try:
        return page.locator(selector).count()
    except Exception:
        return -1


def on_items_page(page) -> bool:
    """Are we looking at a loaded Items Database list?"""
    try:
        url = (page.url or "").lower()
    except Exception:
        return False
    if "login" in url or "authentication" in url:
        return False
    if "menuitems" not in url:
        return False
    # any one of these means the list actually rendered
    for sel in ('input[type="search"]', 'table tbody tr td a[href]',
                '.dataTables_filter input'):
        if _safe_count(page, sel) > 0:
            return True
    return False


def wait_for_login(page, timeout_s: int) -> None:
    """If Toast bounced us to a login screen, wait for the human to sign in.

    Every probe is wrapped -- Toast's SSO bounces through several redirects and any
    locator call can land mid-navigation.
    """
    deadline = time.time() + timeout_s
    warned = False
    while time.time() < deadline:
        if on_items_page(page):
            log("items page is up")
            return

        needs_login = _safe_count(page, 'input[type="password"]') > 0
        try:
            needs_login = needs_login or "login" in (page.url or "").lower()
        except Exception:
            pass

        if needs_login and not warned:
            log("=" * 70)
            log("Toast wants a login. Sign in IN THE CHROME WINDOW THAT JUST OPENED.")
            log("The bot continues by itself once the Items Database page loads.")
            log("(One time only -- the session is saved to the bot's Chrome profile.)")
            log("=" * 70)
            warned = True

        try:
            page.wait_for_timeout(2000)
        except Exception:
            time.sleep(2)
    raise TimeoutError("Never reached the Items Database page. Log in and re-run.")


TOGGLE_MODIFIERS_JS = """
(want) => {
  for (const cb of document.querySelectorAll('input[type="checkbox"]')) {
    const near = (cb.closest('label') || cb.parentElement || {}).innerText || '';
    const lbl  = cb.id ? (document.querySelector('label[for="'+cb.id+'"]')||{}).innerText || '' : '';
    if (/show\\s*modifiers/i.test(near + ' ' + lbl)) {
      if (cb.checked !== want) { cb.click(); return 'toggled to ' + want; }
      return 'already ' + (want ? 'checked' : 'unchecked');
    }
  }
  return 'checkbox not found';
}
"""

SET_PAGE_SIZE_JS = """
() => {
  for (const s of document.querySelectorAll('select')) {
    const opts = [...s.options];
    const nums = opts.filter(o => /^[\\d,]+$/.test(o.text.trim()));
    if (nums.length < 3) continue;
    const best = nums.reduce((a, b) =>
      parseInt(b.text.replace(/,/g,'')) > parseInt(a.text.replace(/,/g,'')) ? b : a);
    if (s.value === best.value) return 'already ' + best.text.trim();
    s.value = best.value;
    s.dispatchEvent(new Event('change', { bubbles: true }));
    return 'page size -> ' + best.text.trim();
  }
  return 'no page-size select found';
}
"""

# "Item count: 69 (of 2,862)" -- Toast's own count, our ground truth for completeness.
ITEM_COUNT_JS = """
() => {
  const m = document.body.innerText.match(/Item count:\\s*([\\d,]+)\\s*\\(of\\s*([\\d,]+)\\)/i);
  return m ? { shown: +m[1].replace(/,/g,''), total: +m[2].replace(/,/g,'') } : null;
}
"""


def apply_search(page, term: str, show_modifiers: bool) -> None:
    if show_modifiers:
        log("show modifiers: " + str(page.evaluate(TOGGLE_MODIFIERS_JS, True)))
        page.wait_for_timeout(2500)

    if term:
        typed = False
        for sel in ['input[type="search"]', 'input[aria-label*="earch"]',
                    'input[placeholder*="earch"]', '.dataTables_filter input']:
            box = page.locator(sel).first
            try:
                if box.count() and box.is_visible():
                    box.click()
                    box.fill("")
                    box.type(term, delay=40)
                    log(f"searched for {term!r}")
                    page.wait_for_timeout(3500)
                    typed = True
                    break
            except Exception:
                continue
        if not typed:
            log("WARNING: could not find the search box; continuing unfiltered")

    # widen the page size AFTER filtering (Toast re-renders the table on search)
    log("page size: " + str(page.evaluate(SET_PAGE_SIZE_JS)))
    page.wait_for_timeout(2500)


def click_next_page(page) -> bool:
    """Advance the list one page. Returns False when there is no next page."""
    return page.evaluate("""
      () => {
        const cands = [...document.querySelectorAll('a, button, li')].filter(el => {
          const t = (el.innerText || '').trim().toLowerCase();
          const c = (el.className || '') + '';
          return t === 'next' || t === '>' || t === '\\u203a' || /paginate_button.*next|\\bnext\\b/i.test(c);
        });
        for (const el of cands) {
          const cls = ((el.className || '') + '').toLowerCase();
          if (cls.includes('disabled')) continue;
          const target = el.tagName === 'LI' ? (el.querySelector('a') || el) : el;
          target.click();
          return true;
        }
        return false;
      }
    """)


def collect_items(page, max_pages: int = 40) -> list[dict]:
    """Scrape (name, href) for every matching item, walking pagination if present."""
    seen: dict[str, dict] = {}
    for page_no in range(1, max_pages + 1):
        batch = page.evaluate("""
          () => {
            const seen = new Set(), out = [];
            for (const tr of document.querySelectorAll('table tbody tr')) {
              const a = tr.querySelector('td a[href]');
              if (!a) continue;
              const href = a.href;
              if (!/menuitem|\\/item/i.test(href)) continue;
              if (seen.has(href)) continue;
              seen.add(href);
              out.push({ name: a.innerText.replace(/\\s+/g,' ').trim(), href });
            }
            return out;
          }
        """)
        added = 0
        for it in batch:
            if it["href"] not in seen:
                seen[it["href"]] = it
                added += 1
        log(f"  page {page_no}: {len(batch)} row(s), {added} new (running total {len(seen)})")
        if added == 0 and page_no > 1:
            break
        if not click_next_page(page):
            break
        page.wait_for_timeout(2500)
    return list(seen.values())


def collect_items(page) -> list[dict]:
    """Scrape (name, href) for every item row currently listed."""
    return page.evaluate("""
      () => {
        const seen = new Set(), out = [];
        for (const tr of document.querySelectorAll('table tbody tr')) {
          const a = tr.querySelector('td a[href]');
          if (!a) continue;
          const href = a.href;
          if (!/menuitem|\\/item/i.test(href)) continue;
          if (seen.has(href)) continue;
          seen.add(href);
          out.push({ name: a.innerText.replace(/\\s+/g,' ').trim(), href });
        }
        return out;
      }
    """)


def find_save(page):
    for sel in SAVE_SELECTORS:
        loc = page.locator(sel).first
        if loc.count():
            try:
                if loc.is_visible():
                    return loc
            except Exception:
                continue
    return None


def probe(page, item) -> None:
    log(f"PROBE -> {item['name']}")
    log(f"  url: {item['href']}")
    rows = page.evaluate(PRICE_ROW_JS)
    log(f"  price rows detected: {len(rows)}")
    for r in rows:
        log(f"    [{r['idx']:>2}] id={r['id'] or '(none)':<40} "
            f"label={r['label']!r:<32} price={r['price']!r:<8} "
            f"trash={r['has_trash']}/vis={r['trash_visible']} undo_vis={r['undo_visible']}")
    save = find_save(page)
    log(f"  Save button found: {bool(save)}")
    html = page.evaluate("""
      () => {
        const tr = document.querySelector('tr[id^="location-price-"]')
               || document.querySelector('tr td.location-price')?.closest('tr');
        return tr ? tr.outerHTML : '(no price row found)';
      }
    """)
    log("  --- raw HTML of first price row ---")
    print(html)
    log("  -----------------------------------")


def process_item(page, item, matchers, require, apply_changes, shots: Path) -> dict:
    rec = {
        "item": item["name"], "url": item["href"],
        "price_rows": 0, "matched": "", "removed": "", "saved": "", "status": "", "note": "",
    }
    rows = page.evaluate(PRICE_ROW_JS)
    rec["price_rows"] = len(rows)
    if not rows:
        rec["status"] = "no-price-rows"
        rec["note"] = "no Location Prices table (item may use a flat base price)"
        return rec

    targets = row_targets(rows, matchers, require)
    rec["matched"] = " | ".join(t["label"] for t in targets)
    if not targets:
        rec["status"] = "skip-no-match"
        return rec

    if not apply_changes:
        rec["status"] = "would-remove"
        return rec

    removed, failed = [], []
    for t in targets:
        row_sel = f'tr#{t["id"]}' if t["id"] else None
        try:
            if row_sel:
                trash = page.locator(f'{row_sel} a.remove-entity').first
            else:
                trash = page.locator('tr:has(td.location-price) a.remove-entity').nth(t["idx"])
            trash.scroll_into_view_if_needed()
            trash.click()
            page.wait_for_timeout(400)
            removed.append(t["label"])
        except Exception as exc:
            failed.append(f"{t['label']}: {type(exc).__name__}")

    # confirm the rows really are gone / marked removed before we commit
    after = {r["id"]: r for r in page.evaluate(PRICE_ROW_JS)}
    still_live = []
    for t in targets:
        r = after.get(t["id"])
        if r and r["row_visible"] and r["trash_visible"] and not r["undo_visible"]:
            still_live.append(t["label"])

    rec["removed"] = " | ".join(removed)
    if failed or still_live:
        rec["status"] = "ABORT-not-saved"
        rec["note"] = f"click failures={failed} still_live={still_live}"
        return rec

    save = find_save(page)
    if not save:
        rec["status"] = "ABORT-no-save-button"
        return rec
    save.click()
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except PWTimeout:
        pass
    page.wait_for_timeout(1200)

    err = page.locator('.alert-error, .error, [class*="error"]:visible').first
    if err.count():
        try:
            txt = err.inner_text().strip()[:200]
            if txt:
                rec["status"] = "saved?-with-error-banner"
                rec["note"] = txt
        except Exception:
            pass
    rec["saved"] = "yes"
    rec["status"] = rec["status"] or "removed+saved"

    shots.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", item["name"])[:80]
    page.screenshot(path=str(shots / f"{safe}.png"), full_page=False)
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--search", default="pick 3 entree",
                    help="text to type into the Items Database search box")
    ap.add_argument("--tiers", default="Tier 8,Tier 10,Tier 12",
                    help="comma-separated tier names to remove")
    ap.add_argument("--require", default="Dine In",
                    help="location label must also contain this (blank to disable)")
    ap.add_argument("--variant", default="Dine In",
                    help="label must end with this, e.g. 'Dine In'. Keeps us off "
                         "'Tier 8 - Dine In - Company Only', which is a different group.")
    ap.add_argument("--loose", action="store_true",
                    help="match the tier token anywhere in the label instead of the whole "
                         "label. Wider blast radius -- dry-run it first.")
    ap.add_argument("--expect", type=int, default=0,
                    help="abort unless exactly N items are collected (e.g. --expect 69)")
    ap.add_argument("--apply", action="store_true",
                    help="actually delete and save (default is a dry run)")
    ap.add_argument("--probe", action="store_true",
                    help="dump the DOM of the first item and exit; changes nothing")
    ap.add_argument("--limit", type=int, default=0, help="only process the first N items")
    ap.add_argument("--url", default=ITEMS_URL)
    ap.add_argument("--no-show-modifiers", action="store_true")
    ap.add_argument("--profile-dir", default=str(Path.home() / ".toast_bot_profile"))
    ap.add_argument("--report", default="")
    ap.add_argument("--headless", action="store_true",
                    help="not recommended -- you want to watch this one")
    ap.add_argument("--login-timeout", type=int, default=600)
    args = ap.parse_args()

    matchers = tier_matchers(args.tiers.split(","), args.variant, args.loose)
    if not matchers:
        return log("no tiers given") or 2
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report = Path(args.report or f"tier_bot_report_{stamp}.csv")
    shots = Path(f"tier_bot_shots_{stamp}")

    log(f"tiers to remove : {[n for n, _ in matchers]}")
    log(f"label must have : {args.require!r}" if args.require else "label filter  : (none)")
    log(f"mode            : {'PROBE' if args.probe else ('APPLY (will save!)' if args.apply else 'DRY RUN (no changes)')}")

    with sync_playwright() as pw:
        ctx = open_browser(pw, Path(args.profile_dir), args.headless)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_default_timeout(30000)

        log(f"opening {args.url}")
        page.goto(args.url, wait_until="domcontentloaded")
        wait_for_login(page, args.login_timeout)
        page.wait_for_timeout(1500)

        apply_search(page, args.search, not args.no_show_modifiers)

        counts = page.evaluate(ITEM_COUNT_JS)
        if counts:
            log(f"Toast says: {counts['shown']} items match (of {counts['total']})")

        items = collect_items(page)
        log(f"collected {len(items)} item link(s)")

        # completeness: silently processing 50 of 69 is the failure mode that matters
        if counts and counts["shown"] != len(items):
            log(f"MISMATCH: Toast lists {counts['shown']} matching items but we collected "
                f"{len(items)}. Refusing to continue -- a partial run would look like a "
                f"complete one. Check pagination / 'Show Modifiers'.")
            ctx.close()
            return 1
        if args.expect and len(items) != args.expect:
            log(f"MISMATCH: --expect {args.expect} but collected {len(items)}. Stopping.")
            ctx.close()
            return 1

        if not items:
            log("Nothing to do. If the list looked right on screen, the row/link selector")
            log("in collect_items() needs adjusting -- run with --probe and share the output.")
            ctx.close()
            return 1
        if args.limit:
            items = items[: args.limit]
            log(f"limited to first {len(items)}")

        results = []
        for n, item in enumerate(items, 1):
            log(f"({n}/{len(items)}) {item['name']}")
            tab = ctx.new_page()
            try:
                tab.goto(item["href"], wait_until="domcontentloaded")
                tab.wait_for_timeout(2500)
                if args.probe:
                    probe(tab, item)
                    tab.close()
                    break
                rec = process_item(tab, item, matchers, args.require, args.apply, shots)
                results.append(rec)
                log(f"    {rec['status']}"
                    + (f" | matched: {rec['matched']}" if rec["matched"] else "")
                    + (f" | {rec['note']}" if rec["note"] else ""))
                if rec["status"].startswith("ABORT"):
                    log("    STOPPING -- something did not look right; nothing was saved here.")
                    tab.close()
                    break
            except Exception as exc:
                results.append({"item": item["name"], "url": item["href"], "price_rows": 0,
                                "matched": "", "removed": "", "saved": "",
                                "status": "ERROR", "note": f"{type(exc).__name__}: {exc}"})
                log(f"    ERROR {type(exc).__name__}: {exc}")
            finally:
                if not tab.is_closed():
                    tab.close()

        if results:
            with report.open("w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=list(results[0].keys()))
                w.writeheader()
                w.writerows(results)
            log(f"report -> {report}")
            counts: dict[str, int] = {}
            for r in results:
                counts[r["status"]] = counts.get(r["status"], 0) + 1
            log("summary: " + json.dumps(counts))
            if args.apply and shots.exists():
                log(f"screenshots -> {shots}")

        log("done. Chrome stays open for 5s so you can look.")
        page.wait_for_timeout(5000)
        ctx.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
