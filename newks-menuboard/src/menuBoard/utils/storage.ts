import { defaultBoardConfig, MENU_BOARD_STORAGE_KEY } from "../defaults";
import type { BoardConfig, ScreenDefinition } from "../types";

/** Legacy default promo we no longer show; treat as empty when loading old configs. */
function isLegacyDefaultPromo(slots: unknown): boolean {
  if (!Array.isArray(slots) || slots.length !== 1) return false;
  const first = slots[0] as { id?: string; title?: string } | undefined;
  return first?.id === "primary" && first?.title === "Try Our Favorites";
}

/** Migrate old screen definitions to grid-only format */
function migrateScreen(s: Partial<ScreenDefinition> & { id: string; label: string; width: number; height: number; categoryIds: string[] }): ScreenDefinition {
  return {
    ...s,
    gridColumns: s.gridColumns ?? 6,
    gridRows: s.gridRows ?? 4,
    gridBlocks: s.gridBlocks ?? [],
  };
}

export function loadBoardConfig(): BoardConfig {
  if (typeof window === "undefined") return defaultBoardConfig;
  try {
    const raw = window.localStorage.getItem(MENU_BOARD_STORAGE_KEY);
    if (!raw) return defaultBoardConfig;
    const parsed = JSON.parse(raw) as Partial<BoardConfig>;
    const featureSlots =
      isLegacyDefaultPromo(parsed.featureSlots)
        ? defaultBoardConfig.featureSlots
        : (parsed.featureSlots ?? defaultBoardConfig.featureSlots);
    const rawScreens = Array.isArray(parsed.screens) && parsed.screens.length > 0
      ? parsed.screens
      : defaultBoardConfig.screens!;
    return {
      ...defaultBoardConfig,
      ...parsed,
      categoryConfigs: parsed.categoryConfigs ?? defaultBoardConfig.categoryConfigs,
      itemConfigs: parsed.itemConfigs ?? defaultBoardConfig.itemConfigs,
      featureSlots,
      branding: parsed.branding ?? defaultBoardConfig.branding,
      marketing: parsed.marketing ?? defaultBoardConfig.marketing,
      screens: rawScreens.map(migrateScreen),
    };
  } catch {
    return defaultBoardConfig;
  }
}

export function saveBoardConfig(config: BoardConfig): void {
  if (typeof window === "undefined") return;
  try {
    const toStore = normalizeConfig(config);
    window.localStorage.setItem(MENU_BOARD_STORAGE_KEY, JSON.stringify(toStore));
  } catch {
    // ignore
  }
}

function normalizeConfig(config: BoardConfig): BoardConfig {
  const rawScreens = Array.isArray(config.screens) && config.screens.length > 0
    ? config.screens
    : defaultBoardConfig.screens!;
  return {
    ...config,
    theme: config.theme,
    categoryOrder: config.categoryOrder ?? [],
    categoryConfigs: config.categoryConfigs ?? {},
    itemConfigs: config.itemConfigs ?? {},
    featureSlots: config.featureSlots ?? [],
    autoCycleSeconds: config.autoCycleSeconds ?? 0,
    showPrices: config.showPrices ?? true,
    showDescriptions: config.showDescriptions ?? true,
    branding: config.branding ?? {},
    marketing: config.marketing ?? { promoImageUrls: [] },
    screens: rawScreens.map(migrateScreen),
  };
}

// ── Server sync functions ──

/** Get the admin token from sessionStorage (set during auth). */
function getAdminToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.sessionStorage.getItem("adminToken");
}

/** Store admin token in sessionStorage. */
export function setAdminToken(token: string): void {
  if (typeof window === "undefined") return;
  window.sessionStorage.setItem("adminToken", token);
}

/** Clear admin token. */
export function clearAdminToken(): void {
  if (typeof window === "undefined") return;
  window.sessionStorage.removeItem("adminToken");
}

/** Load board config from the server. Returns null if not found or on error. */
export async function loadBoardConfigFromServer(
  locationId: string,
): Promise<{ config: BoardConfig | null; version: string | null }> {
  try {
    const resp = await fetch(`/api/board-config/${encodeURIComponent(locationId)}`);
    if (!resp.ok) return { config: null, version: null };
    const data = await resp.json();
    if (!data.config) return { config: null, version: null };
    // Merge with defaults just like localStorage loading
    const parsed = data.config as Partial<BoardConfig>;
    const featureSlots =
      isLegacyDefaultPromo(parsed.featureSlots)
        ? defaultBoardConfig.featureSlots
        : (parsed.featureSlots ?? defaultBoardConfig.featureSlots);
    const rawScreens = Array.isArray(parsed.screens) && parsed.screens.length > 0
      ? parsed.screens
      : defaultBoardConfig.screens!;
    const config: BoardConfig = {
      ...defaultBoardConfig,
      ...parsed,
      categoryConfigs: parsed.categoryConfigs ?? defaultBoardConfig.categoryConfigs,
      itemConfigs: parsed.itemConfigs ?? defaultBoardConfig.itemConfigs,
      featureSlots,
      branding: parsed.branding ?? defaultBoardConfig.branding,
      marketing: parsed.marketing ?? defaultBoardConfig.marketing,
      screens: rawScreens.map(migrateScreen),
    };
    return { config, version: data.version ?? null };
  } catch {
    return { config: null, version: null };
  }
}

/** Save board config to the server. Returns version on success, throws on auth error. */
export async function saveBoardConfigToServer(
  locationId: string,
  config: BoardConfig,
): Promise<{ ok: boolean; version?: string; authError?: boolean }> {
  try {
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    const token = getAdminToken();
    if (token) headers["Authorization"] = `Bearer ${token}`;

    const resp = await fetch(`/api/board-config/${encodeURIComponent(locationId)}`, {
      method: "POST",
      headers,
      body: JSON.stringify(normalizeConfig(config)),
    });

    if (resp.status === 401) {
      return { ok: false, authError: true };
    }
    if (!resp.ok) return { ok: false };
    const data = await resp.json();
    return { ok: true, version: data.version };
  } catch {
    return { ok: false };
  }
}

/** Get just the config version from server (lightweight polling). */
export async function getConfigVersion(locationId: string): Promise<string | null> {
  try {
    const resp = await fetch(`/api/board-config/${encodeURIComponent(locationId)}/version`);
    if (!resp.ok) return null;
    const data = await resp.json();
    return data.version ?? null;
  } catch {
    return null;
  }
}

