import React, { useEffect, useMemo, useRef, useState } from "react";
import "./app.css";
import {
  ApiError,
  ApiMenuProvider,
  MockOrderSubmitter,
  SimplePricingProvider,
  ToastMenuProvider,
  ToastPricingProvider,
} from "./kiosk/providersImpl";
import { Menu, Cart, Pricing, MenuItem, ModifierGroup } from "./kiosk/types";
import { addLine, buildCartLine, clearCart, getItem, removeLine, setQty } from "./kiosk/cartLogic";
import { formatMoney } from "./kiosk/money";
import newksLogo from "./assets/newks-logo.svg";

type Screen = "menu" | "item" | "checkout" | "success";

const IDLE_MS = 2 * 60 * 1000;

type LocationOption = {
  id: string;
  name: string;
  externalId?: string;
};

export default function App() {
  const [locations, setLocations] = useState<LocationOption[]>([]);
  const [locationId, setLocationId] = useState<string | null>(null);
  const [savedLocationId, setSavedLocationId] = useState<string | null>(null);
  const [locationReady, setLocationReady] = useState(false);
  const [gateSelection, setGateSelection] = useState<string>("");
  const [demoMode, setDemoMode] = useState(false);
  const [locationError, setLocationError] = useState<string | null>(null);
  const [menuError, setMenuError] = useState<string | null>(null);
  const [showSettings, setShowSettings] = useState(false);
  const [attractMode, setAttractMode] = useState(true);
  const [locationReloadKey, setLocationReloadKey] = useState(0);
  const [menuReloadKey, setMenuReloadKey] = useState(0);
  const [lastToastError, setLastToastError] = useState<string | null>(null);
  const [lastToastErrorDetails, setLastToastErrorDetails] = useState<string | null>(null);
  useEffect(() => {
    setSavedLocationId(localStorage.getItem("toastLocationId"));
  }, []);

  const [debugInfo, setDebugInfo] = useState<string | null>(null);
  const [debugLoading, setDebugLoading] = useState(false);

  const menuLocationId = useMemo(() => (demoMode ? null : locationId), [demoMode, locationId]);

  const menuProvider = useMemo(
    () => (demoMode ? new ApiMenuProvider() : new ToastMenuProvider({ locationId: menuLocationId })),
    [demoMode, menuLocationId]
  );
  const pricingProvider = useMemo(
    () => (demoMode ? new SimplePricingProvider() : new ToastPricingProvider({ locationId })),
    [demoMode, locationId]
  );
  const submitter = useMemo(() => new MockOrderSubmitter(), []);

  const [menu, setMenu] = useState<Menu | null>(null);
  const [cart, setCart] = useState<Cart>({ lines: [] });
  const [pricing, setPricing] = useState<Pricing>({
    subtotalCents: 0,
    taxCents: 0,
    feesCents: 0,
    totalCents: 0,
  });

  const [screen, setScreen] = useState<Screen>("menu");
  const [activeItemId, setActiveItemId] = useState<string | null>(null);
  const [orderId, setOrderId] = useState<string>("");

  const itemById = useMemo(() => {
    const map = new Map<string, MenuItem>();
    (menu?.items ?? []).forEach((item) => map.set(item.id, item));
    return map;
  }, [menu]);

  const activeItem: MenuItem | null = activeItemId ? itemById.get(activeItemId) ?? null : null;

  function resetForLocationChange() {
    localStorage.removeItem("toastLocationId");
    setLocationId(null);
    setGateSelection("");
    setMenu(null);
    setCart(clearCart());
    setPricing({ subtotalCents: 0, taxCents: 0, feesCents: 0, totalCents: 0 });
    setScreen("menu");
    setActiveItemId(null);
  }

  function applyDemoMode(next: boolean) {
    setDemoMode(next);
    setMenuReloadKey((k) => k + 1);
    setLocationReloadKey((k) => k + 1);
    setCart(clearCart());
    setPricing({ subtotalCents: 0, taxCents: 0, feesCents: 0, totalCents: 0 });
    setScreen("menu");
    setActiveItemId(null);
  }

  // idle reset
  const idleTimer = useRef<number | null>(null);
  function resetIdle() {
    if (idleTimer.current) window.clearTimeout(idleTimer.current);
    idleTimer.current = window.setTimeout(() => {
      setCart(clearCart());
      setScreen("menu");
      setActiveItemId(null);
      setAttractMode(true);
    }, IDLE_MS);
  }

  useEffect(() => {
    resetIdle();
    const handler = () => {
      setAttractMode(false);
      resetIdle();
    };
    window.addEventListener("pointerdown", handler);
    window.addEventListener("keydown", handler);
    return () => {
      window.removeEventListener("pointerdown", handler);
      window.removeEventListener("keydown", handler);
      if (idleTimer.current) window.clearTimeout(idleTimer.current);
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    setMenu(null);
    setMenuError(null);
    if (!demoMode && !menuLocationId) {
      return () => {
        cancelled = true;
      };
    }
    menuProvider
      .getMenu()
      .then((next) => {
        if (cancelled) return;
        setMenu(next);
        setMenuError(null);
      })
      .catch((e) => {
        if (cancelled) return;
        const { message, details } = formatApiError(e);
        setMenuError(message);
        setLastToastError(message);
        setLastToastErrorDetails(details);
      });
    return () => {
      cancelled = true;
    };
  }, [menuProvider, menuReloadKey, demoMode, menuLocationId]);

  useEffect(() => {
    let cancelled = false;
    setLocationError(null);
    if (demoMode) {
      const list = [{ id: "demo-location", name: "Demo Location" }];
      setLocations(list);
      setLocationId((prev) => prev ?? list[0].id);
      setLocationReady(true);
      return () => {
        cancelled = true;
      };
    }
    fetch("/api/toast/locations")
      .then(async (res) => {
        if (!res.ok) {
          const message = await res.text();
          throw new ApiError(res.status, message || res.statusText, message);
        }
        return res.json();
      })
      .then((data: LocationOption[]) => {
        if (cancelled) return;
        const list = Array.isArray(data) ? data : [];
        setLocations(list);
        if (!gateSelection && savedLocationId && list.some((loc) => loc.id === savedLocationId)) {
          setGateSelection(savedLocationId);
        }
        setLocationReady(true);
        setLocationError(null);
      })
      .catch((e) => {
        if (cancelled) return;
        const { message, details } = formatApiError(e);
        setLocationError(message);
        setLastToastError(message);
        setLastToastErrorDetails(details);
        setLocationReady(true);
      });
    return () => {
      cancelled = true;
    };
  }, [demoMode, locationReloadKey]);

  useEffect(() => {
    if (locationId) {
      localStorage.setItem("toastLocationId", locationId);
      setSavedLocationId(locationId);
    }
  }, [locationId]);

  useEffect(() => {
    if (!locationId) {
      setMenuError(null);
      setLastToastError(null);
      setLastToastErrorDetails(null);
    }
  }, [locationId]);

  useEffect(() => {
    if (!menu) return;
    pricingProvider
      .priceCart({ menu, cart })
      .then(setPricing)
      .catch(console.error);
  }, [menu, cart, pricingProvider]);

  useEffect(() => {
    if (!activeItemId) return;
    if (!activeItem) {
      console.warn(`Selected item not found: ${activeItemId}`);
      setActiveItemId(null);
      setScreen("menu");
    }
  }, [activeItemId, activeItem]);

  if (attractMode) {
    return (
      <div className="shell">
        <AttractScreen
          onStart={() => {
            setAttractMode(false);
            resetIdle();
          }}
          onSettings={() => setShowSettings(true)}
          status={demoMode ? "Demo mode" : locationError || menuError ? "Needs setup" : "Ready"}
        />
        {showSettings && (
          <SettingsModal
            locations={locations}
            locationId={locationId}
            onClose={() => setShowSettings(false)}
            onSelectLocation={(next) => setLocationId(next)}
            onResetLocation={resetForLocationChange}
            demoMode={demoMode}
            onToggleDemo={applyDemoMode}
            lastError={lastToastError}
            lastErrorDetails={lastToastErrorDetails}
            onRetry={() => {
              setMenuReloadKey((k) => k + 1);
              setLocationReloadKey((k) => k + 1);
            }}
            onDebug={async () => {
              setDebugLoading(true);
              setDebugInfo(null);
              try {
                const url = new URL("/api/debug/menu", window.location.origin);
                if (menuLocationId) url.searchParams.set("locationId", menuLocationId);
                const res = await fetch(url.toString());
                const text = await res.text();
                setDebugInfo(text);
              } catch (error) {
                setDebugInfo(`Debug request failed: ${String(error)}`);
              } finally {
                setDebugLoading(false);
              }
            }}
            debugInfo={debugInfo}
            debugLoading={debugLoading}
          />
        )}
      </div>
    );
  }

  if (!locationReady) {
    return (
      <div className="shell">
        <div className="loading">Loading locations…</div>
      </div>
    );
  }

  if (!locationId) {
    return (
      <div className="shell">
        <div className="locationGate">
          <div className="gateCard">
            <div className="gateTitle">Select your location</div>
            <div className="muted">This is a one-time setup for this device.</div>
            {locationError && (
              <div className="alert">
                <div className="alertTitle">Location lookup failed</div>
                <div className="alertBody">{locationError}</div>
              </div>
            )}
            <select className="select" value={gateSelection} onChange={(e) => setGateSelection(e.target.value)}>
              <option value="" disabled>
                Choose a location…
              </option>
              {locations.map((loc) => (
                <option key={loc.id} value={loc.id}>
                  {loc.name} {loc.externalId ? `(Ext: ${loc.externalId})` : "(Ext: n/a)"}
                </option>
              ))}
            </select>
            <div className="gateActions">
              <button className="btn ghost" onClick={() => setLocationReloadKey((k) => k + 1)}>
                Retry
              </button>
              <button className="btn" onClick={() => applyDemoMode(true)}>
                Use demo
              </button>
              <button className="btn primary" disabled={!gateSelection} onClick={() => setLocationId(gateSelection)}>
                Continue
              </button>
            </div>
          </div>
        </div>
      </div>
    );
  }

  if (menuError) {
    const primaryLabel = demoMode ? "Try live menu" : "Use demo menu";
    const onPrimary = () => applyDemoMode(!demoMode);
    return (
      <div className="shell">
        <ErrorScreen
          title="Menu not available"
          message={menuError}
          onRetry={() => setMenuReloadKey((k) => k + 1)}
          onPrimary={onPrimary}
          primaryLabel={primaryLabel}
          onSettings={() => setShowSettings(true)}
        />
        {showSettings && (
          <SettingsModal
            locations={locations}
            locationId={locationId}
            onClose={() => setShowSettings(false)}
            onSelectLocation={(next) => setLocationId(next)}
            onResetLocation={resetForLocationChange}
            demoMode={demoMode}
            onToggleDemo={applyDemoMode}
            lastError={lastToastError}
            lastErrorDetails={lastToastErrorDetails}
            onRetry={() => {
              setMenuReloadKey((k) => k + 1);
              setLocationReloadKey((k) => k + 1);
            }}
            onDebug={async () => {
              setDebugLoading(true);
              setDebugInfo(null);
              try {
                const url = new URL("/api/debug/menu", window.location.origin);
                if (menuLocationId) url.searchParams.set("locationId", menuLocationId);
                const res = await fetch(url.toString());
                const text = await res.text();
                setDebugInfo(text);
              } catch (error) {
                setDebugInfo(`Debug request failed: ${String(error)}`);
              } finally {
                setDebugLoading(false);
              }
            }}
            debugInfo={debugInfo}
            debugLoading={debugLoading}
          />
        )}
      </div>
    );
  }

  if (!menu)
    return (
      <div className="shell">
        <div className="loading">Loading menu…</div>
      </div>
    );

  return (
    <div className="shell">
      <header className="topbar">
        <div className="brand">
          <img className="logoMark" src={newksLogo} alt="Newk's" />
          <div>
            <div className="title">Newk&#39;s Kiosk</div>
            <div className="subtitle">Tap to order • Pay at counter</div>
          </div>
        </div>
        <div className="topbarRight">
          <div className={`statusTag ${demoMode ? "demo" : "live"}`}>
            {demoMode ? "Demo Mode" : "Live Menu"}
          </div>
          <button className="btn ghost tinyBtn" onClick={() => setShowSettings(true)}>
            Settings
          </button>
          <div className="location">
            {locations.length > 1 ? (
              <label className="locationSelect">
                <span className="muted tiny">Location</span>
                <select
                  className="select"
                  value={locationId ?? ""}
                  onChange={(e) => setLocationId(e.target.value || null)}
                >
                  {locations.map((loc) => (
                    <option key={loc.id} value={loc.id}>
                      {loc.name}
                    </option>
                  ))}
                </select>
              </label>
            ) : locations.length === 1 ? (
              <div className="locationLabel">
                <div className="muted tiny">Location</div>
                <div>{locations[0].name}</div>
              </div>
            ) : (
              <div className="locationLabel">
                <div className="muted tiny">Location</div>
                <div className="muted">Loading…</div>
              </div>
            )}
            <button className="btn ghost tinyBtn" onClick={resetForLocationChange}>
              Change
            </button>
          </div>

          <div className="totals">
            <div className="totalLine">
              <span>Cart</span>
              <b>{formatMoney(pricing.totalCents)}</b>
            </div>
            <button className="btn ghost" onClick={() => setCart(clearCart())} disabled={cart.lines.length === 0}>
              Clear
            </button>
          </div>
        </div>
      </header>

      <main className="main">
        <section className="content">
          {screen === "menu" && (
            <MenuScreen
              menu={menu}
              onSelectItem={(id) => {
                setActiveItemId(id);
                setScreen("item");
              }}
            />
          )}

          {screen === "item" && activeItem && (
            <ItemScreen
              item={activeItem}
              onBack={() => setScreen("menu")}
              onAdd={(selections) => {
                const line = buildCartLine(menu, activeItem.id, selections);
                setCart((c) => addLine(c, line));
                setScreen("menu");
              }}
            />
          )}

          {screen === "checkout" && (
            <CheckoutScreen
              pricing={pricing}
              cart={cart}
              onBack={() => setScreen("menu")}
              onPlaceOrder={async () => {
                if (!menu || cart.lines.length === 0) return;
                const res = await submitter.submit({ menu, cart, pricing });
                setOrderId(res.orderId);
                setCart(clearCart());
                setScreen("success");
              }}
            />
          )}

          {screen === "success" && (
            <SuccessScreen
              orderId={orderId}
              onDone={() => {
                setOrderId("");
                setScreen("menu");
              }}
            />
          )}
        </section>

        <aside className="cart">
          <CartPanel
            cart={cart}
            pricing={pricing}
            onQty={(lineId, qty) => setCart((c) => setQty(c, lineId, qty))}
            onRemove={(lineId) => setCart((c) => removeLine(c, lineId))}
            onCheckout={() => setScreen("checkout")}
          />
        </aside>
      </main>
      {showSettings && (
        <SettingsModal
          locations={locations}
          locationId={locationId}
          onClose={() => setShowSettings(false)}
          onSelectLocation={(next) => setLocationId(next)}
          onResetLocation={resetForLocationChange}
          demoMode={demoMode}
          onToggleDemo={applyDemoMode}
          lastError={lastToastError}
          lastErrorDetails={lastToastErrorDetails}
          onRetry={() => {
            setMenuReloadKey((k) => k + 1);
            setLocationReloadKey((k) => k + 1);
          }}
          onDebug={async () => {
            setDebugLoading(true);
            setDebugInfo(null);
            try {
              const url = new URL("/api/debug/menu", window.location.origin);
              if (menuLocationId) url.searchParams.set("locationId", menuLocationId);
              const res = await fetch(url.toString());
              const text = await res.text();
              setDebugInfo(text);
            } catch (error) {
              setDebugInfo(`Debug request failed: ${String(error)}`);
            } finally {
              setDebugLoading(false);
            }
          }}
          debugInfo={debugInfo}
          debugLoading={debugLoading}
        />
      )}
    </div>
  );
}

function formatApiError(error: unknown): { message: string; details: string | null } {
  let raw = "";
  if (error instanceof ApiError) {
    raw = error.body || error.message;
  } else if (error instanceof Error) {
    raw = error.message;
  } else if (typeof error === "string") {
    raw = error;
  }

  const details = raw || null;
  if (!raw) {
    return { message: "Unable to reach the Toast API. Check credentials and network.", details: null };
  }

  const normalized = raw.trim();
  const isHtml = /<!DOCTYPE|<html|<head|<body/i.test(normalized);
  if (isHtml) {
    return { message: "Toast auth failed (HTML response). Check credentials and access.", details };
  }
  if (normalized.length > 320) {
    return { message: `${normalized.slice(0, 320)}…`, details };
  }
  return { message: normalized, details };
}

function AttractScreen({
  onStart,
  onSettings,
  status,
}: {
  onStart: () => void;
  onSettings: () => void;
  status: string;
}) {
  return (
    <div className="attract">
      <div className="attractCard">
        <img className="attractLogo" src={newksLogo} alt="Newk's" />
        <h1>Welcome to Newk&#39;s</h1>
        <div className="muted">Tap to start your order.</div>
        <div className="statusPill">{status}</div>
        <div className="attractActions">
          <button className="btn primary big" onClick={onStart}>
            Start Order
          </button>
          <button className="btn ghost" onClick={onSettings}>
            Settings
          </button>
        </div>
      </div>
    </div>
  );
}

function ErrorScreen({
  title,
  message,
  onRetry,
  onPrimary,
  primaryLabel,
  onSettings,
}: {
  title: string;
  message: string;
  onRetry: () => void;
  onPrimary: () => void;
  primaryLabel: string;
  onSettings: () => void;
}) {
  return (
    <div className="shell">
      <div className="errorState">
        <img className="attractLogo" src={newksLogo} alt="Newk's" />
        <h2>{title}</h2>
        <div className="muted">{message}</div>
        <div className="errorActions">
          <button className="btn" onClick={onRetry}>
            Retry
          </button>
          <button className="btn" onClick={onSettings}>
            Settings
          </button>
          <button className="btn primary" onClick={onPrimary}>
            {primaryLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

function SettingsModal({
  locations,
  locationId,
  onClose,
  onSelectLocation,
  onResetLocation,
  demoMode,
  onToggleDemo,
  lastError,
  lastErrorDetails,
  onRetry,
  onDebug,
  debugInfo,
  debugLoading,
}: {
  locations: LocationOption[];
  locationId: string | null;
  onClose: () => void;
  onSelectLocation: (id: string) => void;
  onResetLocation: () => void;
  demoMode: boolean;
  onToggleDemo: (next: boolean) => void;
  lastError: string | null;
  lastErrorDetails: string | null;
  onRetry: () => void;
  onDebug: () => void;
  debugInfo: string | null;
  debugLoading: boolean;
}) {
  return (
    <div className="modalBackdrop" role="dialog" aria-modal="true">
      <div className="modal">
        <div className="modalHeader">
          <h3>Settings</h3>
          <button className="btn ghost tinyBtn" onClick={onClose}>
            Close
          </button>
        </div>
        <div className="modalBody">
          <div className="modalSection">
            <div className="sectionTitle">Mode</div>
            <div className="modalRow">
              <span>{demoMode ? "Demo menu enabled" : "Live menu from Toast"}</span>
              <button className="btn" onClick={() => onToggleDemo(!demoMode)}>
                {demoMode ? "Use live menu" : "Use demo menu"}
              </button>
            </div>
            {lastError && (
              <div className="alertBody">
                Last error: {lastError}
                {lastErrorDetails && (
                  <details className="errorDetails">
                    <summary>Show details</summary>
                    <pre className="debugOutput">{lastErrorDetails}</pre>
                  </details>
                )}
              </div>
            )}
          </div>
          <div className="modalSection">
            <div className="sectionTitle">Location</div>
            {locations.length > 0 ? (
              <select
                className="select"
                value={locationId ?? ""}
                onChange={(e) => onSelectLocation(e.target.value)}
              >
                {!locationId && (
                  <option value="" disabled>
                    Choose a location…
                  </option>
                )}
                {locations.map((loc) => (
                  <option key={loc.id} value={loc.id}>
                    {loc.name} {loc.externalId ? `(Ext: ${loc.externalId})` : "(Ext: n/a)"}
                  </option>
                ))}
              </select>
            ) : (
              <div className="muted">No locations loaded yet.</div>
            )}
            <div className="modalActions">
              <button className="btn ghost" onClick={onResetLocation}>
                Reset selection
              </button>
              <button className="btn" onClick={onRetry}>
                Retry Toast API
              </button>
            </div>
            <div className="helperText">
              Selected location GUID: <b>{locationId ?? "None"}</b>
            </div>
          </div>
          <div className="modalSection">
            <div className="sectionTitle">Menu debug</div>
            <div className="modalRow">
              <span>Check menu auth and response summary.</span>
              <button className="btn" onClick={onDebug} disabled={debugLoading}>
                {debugLoading ? "Running…" : "Run debug"}
              </button>
            </div>
            {debugInfo && <pre className="debugOutput">{debugInfo}</pre>}
          </div>
        </div>
      </div>
    </div>
  );
}

function MenuScreen({ menu, onSelectItem }: { menu: Menu; onSelectItem: (itemId: string) => void }) {
  const categories = useMemo(
    () => [...menu.categories].sort((a, b) => (a.sort ?? 999) - (b.sort ?? 999)),
    [menu.categories]
  );

  const menuGroups = useMemo(() => {
    const grouped = new Map<string, { id: string; name: string; sort?: number }[]>();
    categories.forEach((cat) => {
      const parts = cat.name.split(" - ");
      const menuName = parts.length > 1 ? parts[0] : "Menu";
      const groupName = parts.length > 1 ? parts.slice(1).join(" - ") : cat.name;
      const entry = { id: cat.id, name: groupName, sort: cat.sort };
      const existing = grouped.get(menuName) ?? [];
      existing.push(entry);
      grouped.set(menuName, existing);
    });
    for (const [key, list] of grouped.entries()) {
      list.sort((a, b) => (a.sort ?? 999) - (b.sort ?? 999));
      grouped.set(key, list);
    }
    return grouped;
  }, [categories]);

  const menuNames = useMemo(() => Array.from(menuGroups.keys()), [menuGroups]);
  const [activeMenu, setActiveMenu] = useState(menuNames[0] ?? "Menu");
  const [activeCategoryId, setActiveCategoryId] = useState<string | null>(null);

  useEffect(() => {
    if (!menuNames.includes(activeMenu)) {
      setActiveMenu(menuNames[0] ?? "Menu");
    }
  }, [activeMenu, menuNames]);

  useEffect(() => {
    setActiveCategoryId(null);
  }, [activeMenu]);

  const activeGroups = menuGroups.get(activeMenu) ?? [];
  const activeCategory = categories.find((cat) => cat.id === activeCategoryId) ?? null;
  const activeItems = activeCategory
    ? menu.items.filter((i) => i.categoryId === activeCategory.id)
    : [];

  return (
    <div>
      {menuNames.length > 1 && (
        <div className="menuTabs">
          {menuNames.map((name) => (
            <button
              key={name}
              className={`menuTab ${name === activeMenu ? "active" : ""}`}
              onClick={() => {
                setActiveMenu(name);
                setActiveCategoryId(null);
              }}
            >
              {name}
            </button>
          ))}
        </div>
      )}

      {!activeCategory && (
        <div>
          <div className="menuHeader">
            <h2>{activeMenu}</h2>
            <div className="muted">Choose a category</div>
          </div>
          <div className="menuGroupGrid">
            {activeGroups.map((group) => (
              <button
                key={group.id}
                className="menuGroupCard"
                onClick={() => setActiveCategoryId(group.id)}
              >
                <div className="cardTitle">{group.name}</div>
                <div className="muted">Tap to view items</div>
              </button>
            ))}
          </div>
        </div>
      )}

      {activeCategory && (
        <div className="section">
          <div className="menuHeader">
            <button className="btn ghost" onClick={() => setActiveCategoryId(null)}>
              ← Back to categories
            </button>
            <div>
              <h2>{activeCategory.name}</h2>
              <div className="muted">{activeMenu}</div>
            </div>
          </div>
          <div className="grid">
            {activeItems.map((it) => (
              <button key={it.id} className="card" onClick={() => onSelectItem(it.id)}>
                <div className="cardTitle">{it.name}</div>
                {it.description && <div className="cardDesc">{it.description}</div>}
                <div className="cardPrice">{formatMoney(it.basePriceCents)}</div>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function ItemScreen({
  item,
  onBack,
  onAdd,
}: {
  item: MenuItem;
  onBack: () => void;
  onAdd: (selections: { groupId: string; optionIds: string[] }[]) => void;
}) {
  const groups = item.modifierGroups ?? [];
  const [selected, setSelected] = useState<Record<string, string[]>>(() => {
    const init: Record<string, string[]> = {};
    for (const g of groups) init[g.id] = [];
    return init;
  });

  function toggle(group: ModifierGroup, optId: string) {
    setSelected((prev) => {
      const cur = prev[group.id] ?? [];
      const isSingle = (group.maxSelect ?? 1) === 1;
      let next: string[] = cur;

      if (isSingle) {
        next = [optId];
      } else {
        next = cur.includes(optId) ? cur.filter((x) => x !== optId) : [...cur, optId];
        const max = group.maxSelect ?? 999;
        if (next.length > max) next = next.slice(next.length - max);
      }

      return { ...prev, [group.id]: next };
    });
  }

  const selections = groups.map((g) => ({ groupId: g.id, optionIds: selected[g.id] ?? [] }));
  const missingRequired = groups.some((g) => (g.minSelect ?? 0) > (selected[g.id]?.length ?? 0));

  return (
    <div className="itemScreen">
      <div className="itemHeader">
        <button className="btn ghost" onClick={onBack}>
          ← Menu
        </button>
        <div>
          <h2 style={{ margin: "8px 0 0" }}>{item.name}</h2>
          {item.description && <div className="muted">{item.description}</div>}
        </div>
      </div>

      {groups.map((g) => (
        <div key={g.id} className="modGroup">
          <div className="modTitle">
            <b>{g.name}</b>
            <span className="muted">
              {(g.minSelect ?? 0) > 0 ? `Required • ` : ""}
              {`Choose up to ${g.maxSelect ?? 1}`}
            </span>
          </div>

          <div className="modOptions">
            {g.options.map((o) => {
              const on = (selected[g.id] ?? []).includes(o.id);
              return (
                <button key={o.id} className={`chip ${on ? "on" : ""}`} onClick={() => toggle(g, o.id)}>
                  <span>{o.name}</span>
                  {(o.priceDeltaCents ?? 0) !== 0 && <span className="muted">+{formatMoney(o.priceDeltaCents ?? 0)}</span>}
                </button>
              );
            })}
          </div>
        </div>
      ))}

      <div className="itemFooter">
        <button className="btn primary" disabled={missingRequired} onClick={() => onAdd(selections)}>
          Add to cart • {formatMoney(item.basePriceCents)}
        </button>
        {missingRequired && <div className="warn">Please select required options.</div>}
      </div>
    </div>
  );
}

function CartPanel({
  cart,
  pricing,
  onQty,
  onRemove,
  onCheckout,
}: {
  cart: Cart;
  pricing: Pricing;
  onQty: (lineId: string, qty: number) => void;
  onRemove: (lineId: string) => void;
  onCheckout: () => void;
}) {
  return (
    <div className="cartPanel">
      <h3>Your Cart</h3>

      {cart.lines.length === 0 ? (
        <div className="muted">Add items to begin.</div>
      ) : (
        <div className="cartLines">
          {cart.lines.map((l) => (
            <div key={l.lineId} className="cartLine">
              <div className="cartLineTop">
                <div>
                  <b>{l.name}</b>
                  <div className="muted">{formatMoney(l.basePriceCents + l.selectionsPriceCents)} each</div>
                </div>
                <div className="right">
                  <b>{formatMoney(l.lineTotalCents)}</b>
                </div>
              </div>

              <div className="cartLineBottom">
                <div className="qty">
                  <button className="btn ghost" onClick={() => onQty(l.lineId, l.qty - 1)}>
                    -
                  </button>
                  <div className="qtyNum">{l.qty}</div>
                  <button className="btn ghost" onClick={() => onQty(l.lineId, l.qty + 1)}>
                    +
                  </button>
                </div>
                <button className="btn ghost danger" onClick={() => onRemove(l.lineId)}>
                  Remove
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      <div className="summary">
        <div className="row">
          <span>Subtotal</span>
          <span>{formatMoney(pricing.subtotalCents)}</span>
        </div>
        <div className="row">
          <span>Tax</span>
          <span>{formatMoney(pricing.taxCents)}</span>
        </div>
        <div className="row">
          <span>Fees</span>
          <span>{formatMoney(pricing.feesCents)}</span>
        </div>
        <div className="row total">
          <span>Total</span>
          <span>{formatMoney(pricing.totalCents)}</span>
        </div>
      </div>

      <button className="btn primary big" onClick={onCheckout} disabled={cart.lines.length === 0}>
        Checkout
      </button>

      <div className="muted tiny">Demo mode: payment + POS submission are simulated.</div>
    </div>
  );
}

function CheckoutScreen({
  pricing,
  cart,
  onBack,
  onPlaceOrder,
}: {
  pricing: Pricing;
  cart: Cart;
  onBack: () => void;
  onPlaceOrder: () => Promise<void>;
}) {
  return (
    <div className="checkout">
      <button className="btn ghost" onClick={onBack}>
        ← Back
      </button>
      <h2>Checkout (Demo)</h2>
      <div className="muted">We’re simulating payment here. Later: wire to Olo ordering + payments.</div>

      <div className="checkoutBox">
        <div className="row">
          <span>Items</span>
          <b>{cart.lines.reduce((n, l) => n + l.qty, 0)}</b>
        </div>
        <div className="row">
          <span>Total</span>
          <b>{formatMoney(pricing.totalCents)}</b>
        </div>
      </div>

      <button className="btn primary big" onClick={onPlaceOrder} disabled={cart.lines.length === 0}>
        Place Order (Demo)
      </button>
    </div>
  );
}

function SuccessScreen({ orderId, onDone }: { orderId: string; onDone: () => void }) {
  return (
    <div className="success">
      <h2>Order Submitted ✅</h2>
      <div className="muted">Demo order ID:</div>
      <div className="orderId">{orderId}</div>
      <button className="btn primary big" onClick={onDone}>
        Start New Order
      </button>
    </div>
  );
}
