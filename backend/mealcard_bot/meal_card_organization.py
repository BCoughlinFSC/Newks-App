"""Organization checks use the visible Access Evo switcher, never URL guesses."""
from urllib.parse import urlparse

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


def assert_merchant(surface):
    marker = surface.get_by_text(EXPECTED_MERCHANT, exact=True).first
    marker.wait_for(state="visible", timeout=20000)
    if not marker.is_visible():
        raise RuntimeError("Cannot verify Newk's Eatery merchant 1085")


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
