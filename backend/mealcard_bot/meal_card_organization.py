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


def find_reload_surface(context):
    """Only use merchant controls embedded in the verified organization shell.

    The existing reload adapter supports legacy pxsweb controls. A modern Evo
    merchant surface must be adapted from observed controls before live use.
    Never escape into another tab or navigate to a different merchant host.
    """
    candidates = []
    for page in context.pages:
        if (urlparse(page.url).hostname or "").lower() != "us.access-evo.com":
            continue
        if page.get_by_role("button", name="Switch Organisation", exact=True).count():
            candidates.append(page)
    if len(candidates) != 1:
        raise RuntimeError("Open exactly one Access Evo organization session for the meal-card run")
    owner = candidates[0]
    assert_organization(owner)
    frames = [frame for frame in owner.frames[1:]
              if (urlparse(frame.url).hostname or "").lower() in {"pxsweb.com", "www.pxsweb.com"}
              and frame.locator("#lookup-input-cardNumber").count()]
    if len(frames) != 1:
        raise RuntimeError("Access Evo organization verified, but the legacy reload controls are unavailable. "
                           "The Access Evo reload adapter needs a read-only validation before live use. "
                           "No wallet has been adjusted.")
    return owner, frames[0]
