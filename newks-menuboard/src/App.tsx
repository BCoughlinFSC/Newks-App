import React, { useEffect, useMemo, useState } from "react";
import "./app.css";
import { ApiError, ApiMenuProvider, ToastMenuProvider } from "./core/providersImpl";
import { Menu } from "./core/types";
import newksLogo from "./assets/newks-logo.svg";
import { loadBoardConfig, saveBoardConfig, loadBoardConfigFromServer, saveBoardConfigToServer } from "./menuBoard/utils/storage";
import type { BoardConfig } from "./menuBoard/types";
import { MenuBoardPage } from "./menuBoard/components/MenuBoardPage";
import { SetupWizard } from "./menuBoard/components/SetupWizard";
import { mergeMenuWithConfig } from "./menuBoard/utils/mergeMenuWithConfig";

type LocationOption = {
  id: string;
  name: string;
  externalId?: string;
};

const LOCATIONS_CACHE_KEY = "toastLocationsCache";
const LOCATIONS_CACHE_TTL_MS = 24 * 60 * 60 * 1000; // 24 hours

type LocationsCache = {
  timestamp: number;
  locations: LocationOption[];
};

function getLocationFromUrl(): string | null {
  if (typeof window === "undefined") return null;
  const p = new URLSearchParams(window.location.search);
  return p.get("location") ?? null;
}

function getScreenIndexFromUrl(): number | null {
  if (typeof window === "undefined") return null;
  const p = new URLSearchParams(window.location.search);
  const s = p.get("screen");
  if (s == null) return null;
  const n = parseInt(s, 10);
  return Number.isFinite(n) && n >= 1 ? n : null;
}

export default function App() {
  const urlLocationId = getLocationFromUrl();
  const [locations, setLocations] = useState<LocationOption[]>([]);
  const [locationId, setLocationId] = useState<string | null>(urlLocationId);
  const [savedLocationId, setSavedLocationId] = useState<string | null>(null);
  const [locationReady, setLocationReady] = useState(false);
  const [gateSelection, setGateSelection] = useState<string>("");
  const [demoMode, setDemoMode] = useState(false);
  const [locationError, setLocationError] = useState<string | null>(null);
  const [menuError, setMenuError] = useState<string | null>(null);
  const [showSettings, setShowSettings] = useState(false);
  // Skip attract screen when loaded via direct display URL with location param
  const hasDirectUrl = !!(urlLocationId || getScreenIndexFromUrl());
  const [attractMode, setAttractMode] = useState(!hasDirectUrl);
  const [locationReloadKey, setLocationReloadKey] = useState(0);
  const [menuReloadKey, setMenuReloadKey] = useState(0);
  const [lastToastError, setLastToastError] = useState<string | null>(null);
  const [lastToastErrorDetails, setLastToastErrorDetails] = useState<string | null>(null);
  const [boardConfig, setBoardConfig] = useState<BoardConfig>(() => loadBoardConfig());

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

  const [menu, setMenu] = useState<Menu | null>(null);

  function resetForLocationChange() {
    localStorage.removeItem("toastLocationId");
    setLocationId(null);
    setSavedLocationId(null);
    setGateSelection("");
    setMenu(null);
  }

  function applyDemoMode(next: boolean) {
    setDemoMode(next);
    setMenuReloadKey((k) => k + 1);
    setLocationReloadKey((k) => k + 1);
  }

  // Save config to localStorage always, and to server when locationId is available
  const [configVersion, setConfigVersion] = useState<string | null>(null);

  useEffect(() => {
    saveBoardConfig(boardConfig);
    // Also save to server if we have a location
    if (locationId) {
      saveBoardConfigToServer(locationId, boardConfig).then((result) => {
        if (result.version) setConfigVersion(result.version);
      });
    }
  }, [boardConfig, locationId]);

  // Load config from server when locationId changes
  useEffect(() => {
    if (!locationId) return;
    let cancelled = false;
    loadBoardConfigFromServer(locationId).then(({ config, version }) => {
      if (cancelled) return;
      if (config) {
        setBoardConfig(config);
        setConfigVersion(version);
      }
    });
    return () => { cancelled = true; };
  }, [locationId]);

  useEffect(() => {
    const handler = () => {
      setAttractMode(false);
    };
    window.addEventListener("pointerdown", handler);
    window.addEventListener("keydown", handler);
    return () => {
      window.removeEventListener("pointerdown", handler);
      window.removeEventListener("keydown", handler);
    };
  }, []);

  // No-op idle reset — preserves the call-site in AttractScreen without breaking things
  function resetIdle() {}

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
    // Try cached locations first to avoid calling Toast on every refresh
    try {
      const raw = localStorage.getItem(LOCATIONS_CACHE_KEY);
      if (raw) {
        const parsed = JSON.parse(raw) as LocationsCache;
        if (parsed && Array.isArray(parsed.locations) && typeof parsed.timestamp === "number") {
          const age = Date.now() - parsed.timestamp;
          if (age >= 0 && age < LOCATIONS_CACHE_TTL_MS) {
            const list = parsed.locations;
            setLocations(list);
            if (!gateSelection && savedLocationId && list.some((loc) => loc.id === savedLocationId)) {
              setGateSelection(savedLocationId);
            }
            setLocationReady(true);
            setLocationError(null);
            return () => {
              cancelled = true;
            };
          }
        }
      }
    } catch {
      // Ignore cache errors and fall back to network
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
        try {
          const cache: LocationsCache = { timestamp: Date.now(), locations: list };
          localStorage.setItem(LOCATIONS_CACHE_KEY, JSON.stringify(cache));
        } catch {
          // Ignore cache write failures
        }
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

  // If location came from URL, mark as ready immediately
  useEffect(() => {
    if (urlLocationId && !locationReady) {
      setLocationReady(true);
    }
  }, [urlLocationId, locationReady]);

  // Automatically reuse a previously saved location on reload if it is still valid
  useEffect(() => {
    if (!locationReady) return;
    if (locationId) return;
    if (!savedLocationId) return;
    if (!locations.some((loc) => loc.id === savedLocationId)) return;
    setLocationId(savedLocationId);
  }, [locationReady, locationId, savedLocationId, locations]);

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

  // Show setup wizard on first run (no setupComplete flag)
  if (!boardConfig.setupComplete) {
    const wizardVm = mergeMenuWithConfig(menu, boardConfig);
    return (
      <SetupWizard
        menu={menu}
        boardConfig={boardConfig}
        viewModel={wizardVm}
        onComplete={(config) => setBoardConfig(config)}
        onSkip={() => setBoardConfig({ ...boardConfig, setupComplete: true })}
      />
    );
  }

  return (
    <div className="shell">
      <MenuBoardPage
        menu={menu}
        boardConfig={boardConfig}
        onBoardConfigChange={setBoardConfig}
        locationName={locations.find((l) => l.id === locationId)?.name ?? null}
        demoMode={demoMode}
        onResetLocation={resetForLocationChange}
        locationId={locationId}
        configVersion={configVersion}
        onMenuRefresh={() => setMenuReloadKey((k) => k + 1)}
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
  React.useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.preventDefault(); onClose(); }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);

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

