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
                "loading, and rerun the no-money test. The run stopped at the merchant check.")
        time.sleep(0.25)


def assert_session(owner, surface):
    assert_organization(owner)
    if (urlparse(surface.url).hostname or "").lower() != MERCHANT_HOST:
        raise RuntimeError("Paytronix merchant frame changed host; stopping")
    assert_merchant(surface)


def maximize_window(page):
    """Expand the launcher's Chrome window before inspecting responsive controls."""
    session = None
    try:
        session = page.context.new_cdp_session(page)
        window = session.send("Browser.getWindowForTarget")
        session.send("Browser.setWindowBounds", {"windowId": window["windowId"],
                                                "bounds": {"windowState": "maximized"}})
    except Exception:
        print("Could not maximize Chrome automatically. Maximize its window if the merchant header is hidden.")
    finally:
        if session is not None:
            try:
                session.detach()
            except Exception:
                pass


def find_reload_surface(context, timeout_seconds=45):
    """Select a verified visible merchant frame, allowing duplicate Evo tabs.

    Frame titles are not stable during SSO. Discover by the known merchant host,
    then verify both the visible organization and merchant. Duplicate tabs never
    permit weaker identity checks or a standalone Paytronix fallback.
    """
    deadline = time.monotonic() + timeout_seconds
    expanded = set()
    last_reason = "No Access Evo tab was found in the launcher's Chrome window."
    while True:
        evo_pages = [p for p in context.pages
                     if (urlparse(p.url).hostname or "").lower() == "us.access-evo.com"]
        frame_count = 0
        # Prefer the last opened candidate; any selected candidate must pass the
        # same organization, host, and visible merchant checks.
        for owner in reversed(evo_pages):
            try:
                if id(owner) not in expanded:
                    maximize_window(owner)
                    expanded.add(id(owner))
                assert_organization(owner)
                frames = [f for f in owner.frames[1:]
                          if (urlparse(f.url).hostname or "").lower() == MERCHANT_HOST]
                frame_count += len(frames)
                for surface in reversed(frames):
                    if not surface.frame_element().is_visible():
                        last_reason = "Paytronix frame is present but hidden; select the Loyalty tab in Access Evo."
                        continue
                    try:
                        assert_merchant(surface, timeout_seconds=0)
                        assert_session(owner, surface)
                        owner.bring_to_front()
                        print(f"Verified Newk's Eatery merchant 1085; {len(evo_pages)} Access Evo tab(s) open.")
                        return owner, surface
                    except Exception as exc:
                        last_reason = str(exc).splitlines()[0]
                if not frames:
                    last_reason = "Access Evo is open, but its Paytronix merchant frame has not loaded. Open Loyalty / Paytronix."
            except Exception as exc:
                last_reason = str(exc).splitlines()[0]
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Could not select a verified Newk's Paytronix session. "
                f"Found {len(evo_pages)} Access Evo tab(s) and {frame_count} merchant frame(s). "
                f"{last_reason} Use the Chrome window opened by this launcher.")
        time.sleep(0.5)
