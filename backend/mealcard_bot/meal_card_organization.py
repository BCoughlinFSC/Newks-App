"""Organization checks use the visible Access Evo switcher, never URL guesses."""
import re
import time
import unicodedata
from urllib.parse import urlparse, parse_qs

EXPECTED_ORGANIZATION = "Newk's Eatery"


def assert_organization(page):
    switcher = page.get_by_role("button", name="Switch Organisation", exact=True)
    if switcher.count() != 1:
        raise RuntimeError("Cannot verify Access Evo organization; no Paytronix action permitted")
    actual = " ".join(switcher.inner_text().replace("\u2019", "'").split())
    if actual != EXPECTED_ORGANIZATION:
        raise RuntimeError(f"Select {EXPECTED_ORGANIZATION} in Access Evo before running. Selected: {actual!r}")


EXPECTED_MERCHANT = "Newk's Eatery (1085)"
MERCHANT_HOST = "azr1px.px.us.access-evo.com"


def normalize_label(text):
    text = unicodedata.normalize("NFKC", text).replace("\u2019", "'").replace("\u02bc", "'")
    return " ".join(text.split())


def assert_merchant(surface, timeout_seconds=45):
    """Check rendered merchant identity without relying on the first exact node.

    Responsive navigation can contain duplicate/hidden spans and split labels.
    Body inner_text reads rendered text and normalization handles line wrapping,
    smart apostrophes, and nonbreaking spaces. URL values never substitute for
    a positive visible merchant identity.
    """
    deadline = time.monotonic() + timeout_seconds
    expected = re.compile(r"(?<![\w])Newk's Eatery\s*\(\s*1085\s*\)", re.I)
    merchant_label = re.compile(r"(?<![\w])Newk's Eatery\s*\(\s*(\d+)\s*\)", re.I)
    last_text = ""
    while True:
        try:
            last_text = normalize_label(surface.locator("body").inner_text(timeout=5000))
            label = merchant_label.search(last_text)
            if label and label.group(1) != "1085":
                raise RuntimeError("Wrong Paytronix merchant: " + label.group(0))
            if expected.search(last_text):
                home = surface.get_by_role("link", name="px PXS", exact=True)
                if home.count() == 1:
                    ids = parse_qs(urlparse(home.get_attribute("href") or "").query).get("currentMerchantId", [])
                    if ids and ids != ["1085"]:
                        raise RuntimeError("Paytronix merchant link conflicts with Newk's merchant 1085")
                return
        except RuntimeError:
            raise
        except Exception:
            # The iframe may still be navigating/hydrating after SSO.
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "Cannot verify the visible Paytronix merchant as Newk's Eatery (1085). "
                "Access Evo organization was checked separately. Open the Paytronix "
                "sidebar so the merchant name is visible, wait for the page to finish "
                "loading, and rerun the no-money test. No wallet submission was attempted.")
        time.sleep(0.25)


def assert_session(owner, surface):
    assert_organization(owner)
    if (urlparse(surface.url).hostname or "").lower() != MERCHANT_HOST:
        raise RuntimeError("Paytronix merchant frame changed host; stopping")
    assert_merchant(surface)


def find_reload_surface(context):
    """Bind the visible Newk's merchant frame to its verified Evo shell."""
    candidates = []
    for page in context.pages:
        if (urlparse(page.url).hostname or "").lower() != "us.access-evo.com":
            continue
        iframe = page.locator('iframe[title="Paytronix"]')
        if iframe.count() != 1:
            continue
        handle = iframe.element_handle()
        surface = handle.content_frame() if handle else None
        if surface is not None:
            candidates.append((page, surface))
    if len(candidates) != 1:
        raise RuntimeError("Open exactly one Access Evo Paytronix session in debug Chrome")
    owner, surface = candidates[0]
    assert_session(owner, surface)
    return owner, surface
