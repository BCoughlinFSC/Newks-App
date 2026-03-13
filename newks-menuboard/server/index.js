import "dotenv/config";
import express from "express";
import cors from "cors";
import { mkdirSync, createWriteStream, existsSync, readFileSync, writeFileSync } from "node:fs";
import { randomUUID, createHash } from "node:crypto";
import { join, extname } from "node:path";
import { fileURLToPath } from "node:url";
import { mockMenu } from "./mockMenu.js";
import { mockToastMenu } from "./mockToastMenu.js";

const __dirname = fileURLToPath(new URL(".", import.meta.url));
const UPLOADS_DIR = join(__dirname, "..", "public", "uploads");
mkdirSync(UPLOADS_DIR, { recursive: true });

const CONFIGS_DIR = join(__dirname, "data", "board-configs");
mkdirSync(CONFIGS_DIR, { recursive: true });

const app = express();
app.use(cors());
app.use(express.json({ limit: "1mb" }));
// Serve uploaded files statically
app.use("/uploads", express.static(UPLOADS_DIR));

const PORT = Number(process.env.PORT ?? 8787);
const FORCE_DEMO = (process.env.TOAST_FORCE_DEMO ?? "").toLowerCase() === "true";

const tokenCache = {
  accessToken: null,
  expiresAtMs: 0,
};

const locationsCache = {
  data: null,
  expiresAtMs: 0,
};

const menuCache = new Map();
const MENU_CACHE_TTL_MS = Number(process.env.TOAST_MENU_CACHE_TTL_MS ?? 300_000);
const MENU_USE_STANDARD_AUTH = (process.env.TOAST_MENU_USE_STANDARD_AUTH ?? "").toLowerCase() === "true";
const TOAST_CONFIG_API_BASE_URL =
  process.env.TOAST_CONFIG_API_BASE_URL ?? "https://ws-api.toasttab.com/config/v2";
const MENU_MAX_RETRIES = Number(process.env.TOAST_MENU_MAX_RETRIES ?? 5);
const MENU_RETRY_BASE_MS = Number(process.env.TOAST_MENU_RETRY_BASE_MS ?? 1200);

function isUuid(value) {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
}

class HttpError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

async function fetchWithRetry(url, options = {}) {
  let attempt = 0;
  let delayMs = MENU_RETRY_BASE_MS;
  while (attempt < MENU_MAX_RETRIES) {
    const response = await fetch(url, options);
    if (response.status !== 429) return response;
    const retryAfter = response.headers.get("Retry-After");
    let waitMs = delayMs;
    if (retryAfter) {
      const parsed = Number(retryAfter);
      if (Number.isFinite(parsed)) {
        waitMs = Math.max(parsed * 1000, waitMs);
      }
    }
    await new Promise((resolve) => setTimeout(resolve, waitMs));
    delayMs = Math.min(delayMs * 1.8, 10_000);
    attempt += 1;
  }
  return fetch(url, options);
}

async function getAccessToken(options = {}) {
  const useStandard = Boolean(options.useStandard);
  if (!useStandard) {
    if (process.env.TOAST_API_TOKEN) return process.env.TOAST_API_TOKEN;
    if (process.env.TOAST_ACCESS_TOKEN) return process.env.TOAST_ACCESS_TOKEN;
  }

  const clientId = useStandard
    ? process.env.TOAST_STANDARD_API_CLIENT_ID
    : process.env.TOAST_API_CLIENT_ID ?? process.env.TOAST_STANDARD_API_CLIENT_ID;
  const clientSecret = useStandard
    ? process.env.TOAST_STANDARD_API_CLIENT_SECRET
    : process.env.TOAST_API_CLIENT_SECRET ?? process.env.TOAST_STANDARD_API_CLIENT_SECRET;
  if (!clientId || !clientSecret) return null;

  const now = Date.now();
  if (tokenCache.accessToken && tokenCache.expiresAtMs - 60_000 > now) {
    return tokenCache.accessToken;
  }

  const baseUrl = useStandard
    ? process.env.TOAST_STANDARD_API_BASE_URL ?? "https://ws-api.toasttab.com"
    : process.env.TOAST_API_BASE_URL;
  const derivedAuthBase = baseUrl
    ? baseUrl.includes("/era/v1")
      ? baseUrl.replace("/era/v1", "")
      : baseUrl.includes("/")
        ? baseUrl.split("/").slice(0, -1).join("/")
        : baseUrl
    : null;
  const tokenUrl =
    process.env.TOAST_OAUTH_TOKEN_URL ??
    (derivedAuthBase ? `${derivedAuthBase}/authentication/v1/authentication/login` : null) ??
    "https://ws-api.toasttab.com/authentication/v1/authentication/login";
  const userAccessType = process.env.TOAST_USER_ACCESS_TYPE ?? "TOAST_MACHINE_CLIENT";
  console.log(
    `Toast auth: tokenUrl=${tokenUrl}, userAccessType=${userAccessType}, auth=${useStandard ? "standard" : "default"}`
  );

  let response;
  let data;
  try {
    response = await fetch(tokenUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        clientId,
        clientSecret,
        userAccessType,
      }),
    });
  } catch (error) {
    console.error("Toast auth request failed", error);
    return null;
  }

  if (!response.ok) {
    const message = await response.text();
    console.error("Toast auth error", message || response.statusText);
    return null;
  }

  try {
    data = await response.json();
  } catch (error) {
    console.error("Toast auth parse error", error);
    return null;
  }

  const accessToken =
    data?.accessToken ?? data?.access_token ?? data?.token ?? data?.id_token ?? data?.tokenValue ?? null;
  const expiresInSeconds = data?.expiresIn ?? data?.expires_in ?? 3600;

  if (!accessToken) {
    console.error("Toast auth missing access token");
    return null;
  }

  tokenCache.accessToken = accessToken;
  tokenCache.expiresAtMs = now + Number(expiresInSeconds) * 1000;

  return accessToken;
}

function normalizeRefs(list) {
  if (!Array.isArray(list)) return [];
  return list
    .map((item) => (typeof item === "string" ? item : item?.guid ?? item?.id ?? null))
    .filter(Boolean);
}

async function fetchConfigJson(path, accessToken, restaurantExternalId) {
  const url = new URL(`${TOAST_CONFIG_API_BASE_URL.replace(/\/$/, "")}/${path.replace(/^\//, "")}`);
  const headers = {
    Authorization: `Bearer ${accessToken}`,
    "Toast-Restaurant-External-ID": restaurantExternalId,
  };
  const response = await fetch(url.toString(), { headers });
  if (!response.ok) {
    const message = await response.text();
    throw new HttpError(response.status, message || "Config API error");
  }
  return response.json();
}

async function fetchConfigList(path, accessToken, restaurantExternalId) {
  let results = [];
  let pageToken = null;
  do {
    const url = new URL(`${TOAST_CONFIG_API_BASE_URL.replace(/\/$/, "")}/${path.replace(/^\//, "")}`);
    if (pageToken) url.searchParams.set("pageToken", pageToken);
    const headers = {
      Authorization: `Bearer ${accessToken}`,
      "Toast-Restaurant-External-ID": restaurantExternalId,
    };
    const response = await fetch(url.toString(), { headers });
    if (!response.ok) {
      const message = await response.text();
      throw new HttpError(response.status, message || "Config API error");
    }
    const data = await response.json();
    if (Array.isArray(data)) {
      results = results.concat(data);
    }
    pageToken = response.headers.get("Toast-Next-Page-Token");
  } while (pageToken);
  return results;
}

async function buildMenuFromConfig(restaurantExternalId, accessToken) {
  const primaryToken = accessToken ?? (await getAccessToken({ useStandard: false }));
  const fallbackToken = await getAccessToken({ useStandard: true });
  if (!primaryToken && !fallbackToken) {
    throw new HttpError(401, "Missing credentials for Configuration API");
  }

  let menus = null;
  let configToken = primaryToken;
  try {
    if (!configToken) throw new HttpError(401, "Missing config access token");
    menus = await fetchConfigList("/menus", configToken, restaurantExternalId);
  } catch (error) {
    if (error?.status === 401 && fallbackToken && fallbackToken !== primaryToken) {
      configToken = fallbackToken;
      menus = await fetchConfigList("/menus", configToken, restaurantExternalId);
    } else {
      throw error;
    }
  }
  if (!Array.isArray(menus) || menus.length === 0) {
    throw new HttpError(404, "No menus found in Configuration API");
  }

  const menuGroupById = new Map();
  const menuItemById = new Map();
  const optionGroupById = new Map();
  const optionItemIds = new Set();
  const baseItemIds = new Set();
  const inheritedOptionGroups = new Map();

  async function fetchMenuItem(itemId) {
    if (!itemId || menuItemById.has(itemId)) return;
    const item = await fetchConfigJson(`/menuItems/${itemId}`, configToken, restaurantExternalId);
    menuItemById.set(itemId, item);
    const optionGroups = normalizeRefs(item?.optionGroups);
    if (optionGroups.length > 0) {
      inheritedOptionGroups.set(itemId, new Set(optionGroups));
      for (const groupId of optionGroups) {
        await fetchOptionGroup(groupId);
      }
    }
  }

  async function fetchOptionGroup(groupId) {
    if (!groupId || optionGroupById.has(groupId)) return;
    const group = await fetchConfigJson(`/menuOptionGroups/${groupId}`, configToken, restaurantExternalId);
    optionGroupById.set(groupId, group);
    const items = normalizeRefs(group?.items);
    for (const itemId of items) {
      optionItemIds.add(itemId);
      await fetchMenuItem(itemId);
    }
  }

  async function fetchMenuGroup(groupId) {
    if (!groupId || menuGroupById.has(groupId)) return;
    const group = await fetchConfigJson(`/menuGroups/${groupId}`, configToken, restaurantExternalId);
    menuGroupById.set(groupId, group);

    const subgroupIds = normalizeRefs(group?.subgroups ?? group?.menuGroups ?? group?.groups);
    for (const subId of subgroupIds) {
      await fetchMenuGroup(subId);
    }

    const optionGroups = normalizeRefs(group?.optionGroups);
    for (const optId of optionGroups) {
      await fetchOptionGroup(optId);
    }

    const items = normalizeRefs(group?.items);
    for (const itemId of items) {
      baseItemIds.add(itemId);
      await fetchMenuItem(itemId);
      const item = menuItemById.get(itemId);
      if (item?.inheritOptionGroups && optionGroups.length > 0) {
        const existing = inheritedOptionGroups.get(itemId) ?? new Set();
        optionGroups.forEach((g) => existing.add(g));
        inheritedOptionGroups.set(itemId, existing);
      }
    }
  }

  const menuDetails = [];
  for (const menu of menus) {
    const menuId = menu?.guid ?? menu?.id;
    if (!menuId) continue;
    const menuDetail = await fetchConfigJson(`/menus/${menuId}`, configToken, restaurantExternalId);
    menuDetails.push(menuDetail);
    const groupIds = normalizeRefs(menuDetail?.groups ?? menuDetail?.menuGroups);
    for (const groupId of groupIds) {
      await fetchMenuGroup(groupId);
    }
  }

  function buildGroupNode(groupId, visited = new Set()) {
    if (visited.has(groupId)) return null;
    visited.add(groupId);
    const group = menuGroupById.get(groupId);
    if (!group) return null;
    const childIds = normalizeRefs(group?.subgroups ?? group?.menuGroups ?? group?.groups);
    const items = normalizeRefs(group?.items);
    return {
      guid: groupId,
      id: groupId,
      name: group?.name ?? "Category",
      itemIds: items,
      menuGroups: childIds.map((id) => buildGroupNode(id, new Set(visited))).filter(Boolean),
    };
  }

  const menuGroups = [];
  for (const menuDetail of menuDetails) {
    const groupIds = normalizeRefs(menuDetail?.groups ?? menuDetail?.menuGroups);
    for (const groupId of groupIds) {
      const node = buildGroupNode(groupId);
      if (node) menuGroups.push(node);
    }
  }

  const menuItems = [];
  for (const itemId of baseItemIds) {
    const item = menuItemById.get(itemId);
    if (!item) continue;
    const inherited = inheritedOptionGroups.get(itemId);
    const optionGroups = new Set(normalizeRefs(item?.optionGroups));
    if (inherited) inherited.forEach((id) => optionGroups.add(id));
    menuItems.push({
      guid: itemId,
      id: itemId,
      name: item?.name ?? "Item",
      description: item?.description,
      price: item?.price ?? item?.basePrice ?? item?.unitPrice ?? 0,
      modifierGroupIds: Array.from(optionGroups),
    });
  }

  const modifierGroups = [];
  for (const [groupId, group] of optionGroupById.entries()) {
    modifierGroups.push({
      guid: groupId,
      id: groupId,
      name: group?.name ?? "Options",
      modifierOptionIds: normalizeRefs(group?.items),
    });
  }

  const modifierOptions = [];
  for (const itemId of optionItemIds) {
    const item = menuItemById.get(itemId);
    if (!item) continue;
    modifierOptions.push({
      guid: itemId,
      id: itemId,
      name: item?.name ?? "Option",
      price: item?.price ?? item?.basePrice ?? item?.unitPrice ?? 0,
    });
  }

  return {
    menus: menuDetails.map((m) => ({
      guid: m?.guid ?? m?.id,
      id: m?.guid ?? m?.id,
      name: m?.name ?? "Menu",
      menuGroups,
    })),
    menuGroups,
    menuItems,
    modifierGroups,
    modifierOptions,
  };
}

function normalizeToastLocations(payload) {
  const raw =
    (Array.isArray(payload) && payload) ||
    payload?.restaurants ||
    payload?.locations ||
    payload?.data ||
    payload?.results ||
    [];

  if (!Array.isArray(raw)) return [];

  const includeTest = (process.env.TOAST_INCLUDE_TEST_LOCATIONS ?? "").toLowerCase() === "true";
  const includeArchived = (process.env.TOAST_INCLUDE_ARCHIVED_LOCATIONS ?? "").toLowerCase() === "true";
  const includeInactive = (process.env.TOAST_INCLUDE_INACTIVE_LOCATIONS ?? "").toLowerCase() === "true";
  const includeNameContains = (process.env.TOAST_INCLUDE_LOCATION_NAME_CONTAINS ?? "")
    .split(",")
    .map((v) => v.trim().toLowerCase())
    .filter(Boolean);

  const mapped = raw
    .map((loc) => {
      const idCandidates = [
        loc?.restaurantGuid,
        loc?.locationGuid,
        loc?.guid,
        loc?.id,
        loc?.restaurantId,
        loc?.externalId,
        loc?.restaurantExternalId,
        loc?.external_id,
      ].filter(Boolean);
      const id =
        idCandidates.find((value) => isUuid(String(value))) ?? (idCandidates[0] ?? null);
      const externalId =
        loc?.externalId || loc?.restaurantExternalId || loc?.restaurantId || loc?.external_id || null;
      const name = loc?.restaurantName || loc?.locationName || loc?.name || "Unnamed Location";
      const active = loc?.active ?? loc?.isActive ?? true;
      const archived = loc?.archived ?? loc?.isArchived ?? false;
      const testMode = loc?.testMode ?? loc?.isTestMode ?? false;

      if (!id) return null;
      const nameLower = String(name).toLowerCase();
      const matchesIncludeName =
        includeNameContains.length > 0 && includeNameContains.some((needle) => nameLower.includes(needle));
      if (!active && !includeInactive && !matchesIncludeName) return null;
      if (archived && !includeArchived && !matchesIncludeName) return null;
      if (testMode && !includeTest && !matchesIncludeName) return null;
      return { id: String(id), name: String(name), externalId: externalId ? String(externalId) : undefined };
    })
    .filter(Boolean);

  const deduped = [];
  const seen = new Set();
  for (const loc of mapped) {
    if (seen.has(loc.id)) continue;
    seen.add(loc.id);
    deduped.push(loc);
  }

  return deduped.sort((a, b) => a.name.localeCompare(b.name));
}

async function getToastLocations(locationsUrl, accessToken) {
  const now = Date.now();
  if (locationsCache.data && locationsCache.expiresAtMs > now) {
    return locationsCache.data;
  }

  const response = await fetch(locationsUrl, {
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  if (!response.ok) {
    const message = await response.text();
    throw new HttpError(response.status, message || "Toast locations error");
  }

  const data = await response.json();
  const normalized = normalizeToastLocations(data);
  locationsCache.data = normalized;
  locationsCache.expiresAtMs = now + 5 * 60 * 1000;
  return normalized;
}

async function resolveMenuLocationId(locationId) {
  if (!locationId) return null;
  const raw = locationId.toString();
  if (isUuid(raw)) return raw;
  const locationsUrl =
    process.env.TOAST_LOCATIONS_URL ??
    (process.env.TOAST_API_BASE_URL ? `${process.env.TOAST_API_BASE_URL}/restaurants-information` : null);
  const token =
    (await getAccessToken({ useStandard: false })) ?? (await getAccessToken({ useStandard: true }));
  if (!locationsUrl || !token) return raw;
  try {
    const locations = await getToastLocations(locationsUrl, token);
    const match = locations.find((loc) => loc.id === raw || loc.externalId === raw);
    if (match?.id && isUuid(match.id)) return match.id;
  } catch (error) {
    console.warn("Location resolve error", error);
  }
  return raw;
}

app.get("/api/health", (_req, res) => {
  res.json({ ok: true });
});

app.get("/api/menu", (_req, res) => {
  res.json(mockMenu);
});

app.get("/api/toast/locations", async (_req, res) => {
  if (FORCE_DEMO) {
    return res.json([{ id: "demo-location", name: "Demo Location" }]);
  }
  const proxyBaseUrl = process.env.TOAST_PROXY_BASE_URL;
  if (proxyBaseUrl) {
    const url = new URL(`${proxyBaseUrl.replace(/\/$/, "")}/locations`);
    if ((process.env.TOAST_INCLUDE_TEST_LOCATIONS ?? "").toLowerCase() === "true") {
      url.searchParams.set("includeTest", "true");
    }
    if ((process.env.TOAST_INCLUDE_ARCHIVED_LOCATIONS ?? "").toLowerCase() === "true") {
      url.searchParams.set("includeArchived", "true");
    }
    if ((process.env.TOAST_INCLUDE_INACTIVE_LOCATIONS ?? "").toLowerCase() === "true") {
      url.searchParams.set("includeInactive", "true");
    }
    try {
      const response = await fetch(url.toString());
      if (!response.ok) {
        const message = await response.text();
        return res.status(response.status).json({ error: message || "Proxy locations error" });
      }
      const data = await response.json();
      return res.json(normalizeToastLocations(data));
    } catch (error) {
      console.error("Proxy locations error", error);
      return res.status(500).json({ error: "Proxy locations request failed" });
    }
  }

  const locationsUrl =
    process.env.TOAST_LOCATIONS_URL ??
    (process.env.TOAST_API_BASE_URL ? `${process.env.TOAST_API_BASE_URL}/restaurants-information` : null);
  const accessToken = await getAccessToken();

  if (!locationsUrl || !accessToken) {
    return res.json([{ id: "demo-location", name: "Demo Location" }]);
  }

  try {
    const locations = await getToastLocations(locationsUrl, accessToken);
    return res.json(locations);
  } catch (error) {
    console.error("Toast locations error", error);
    return res
      .status(error?.status || 500)
      .json({ error: error?.message || "Toast locations request failed" });
  }
});

app.get("/api/toast/menu", async (req, res) => {
  if (FORCE_DEMO) {
    return res.json(mockToastMenu);
  }
  const rawLocationId = req.query.locationId?.toString() ?? null;
  const resolvedLocationId = await resolveMenuLocationId(rawLocationId);
  if (!resolvedLocationId) {
    return res.status(400).json({ error: "LocationId is required to fetch menu data." });
  }
  const cacheKey = (resolvedLocationId ?? "default").toLowerCase();
  const cached = menuCache.get(cacheKey);
  if (cached && cached.expiresAtMs > Date.now()) {
    return res.json(cached.data);
  }

  const proxyMenuUrl = process.env.TOAST_PROXY_MENU_URL;
  if (proxyMenuUrl) {
    const url = new URL(proxyMenuUrl);
    if (resolvedLocationId) url.searchParams.set("restaurant_guid", resolvedLocationId);
    try {
      const response = await fetch(url.toString());
      if (!response.ok) {
        const message = await response.text();
        return res.status(response.status).json({ error: message || "Proxy menu error" });
      }
      const data = await response.json();
      menuCache.set(cacheKey, { data, expiresAtMs: Date.now() + MENU_CACHE_TTL_MS });
      return res.json(data);
    } catch (error) {
      console.error("Proxy menu error", error);
      return res.status(500).json({ error: "Proxy menu request failed" });
    }
  }

  const standardBase = process.env.TOAST_STANDARD_API_BASE_URL ?? "https://ws-api.toasttab.com";
  const defaultMenuUrl = `${standardBase.replace(/\/$/, "")}/menus/v2/menus`;
  const menuUrl = process.env.TOAST_MENU_URL ?? defaultMenuUrl;
  const useStandardAuth = true;
  const accessToken = await getAccessToken({ useStandard: useStandardAuth });

  if (!menuUrl || !accessToken) {
    return res.json(mockToastMenu);
  }

  try {
    const url = new URL(menuUrl);
    const headers = { Authorization: `Bearer ${accessToken}` };
    if (resolvedLocationId) {
      // Always use Toast-Restaurant-External-ID header - it should contain the restaurant GUID
      headers["Toast-Restaurant-External-ID"] = resolvedLocationId;
    }
    let response = await fetchWithRetry(url.toString(), { headers });
    if (!response.ok && response.status === 401 && url.hostname.includes("ws-api.toasttab.com")) {
      const apiHostUrl = new URL(url.toString());
      apiHostUrl.hostname = "api.toasttab.com";
      response = await fetchWithRetry(apiHostUrl.toString(), { headers });
    }
    if (!response.ok) {
      const message = await response.text();
      if (response.status === 429 && cached) {
        console.warn("Toast menu rate limit hit; returning cached menu.");
        return res.json(cached.data);
      }
      if (response.status === 404) {
        try {
          const fallback = await buildMenuFromConfig(resolvedLocationId ?? rawLocationId ?? "", accessToken);
          menuCache.set(cacheKey, { data: fallback, expiresAtMs: Date.now() + MENU_CACHE_TTL_MS });
          return res.json(fallback);
        } catch (error) {
          console.error("Config menu fallback error", error);
          const fallbackMessage = error?.message || "Config API fallback failed";
          return res.status(502).json({
            error: `Menus V2 returned 404 and Config API failed: ${fallbackMessage}`,
          });
        }
      }
      return res.status(response.status).json({ error: message || "Toast menu error" });
    }
    const data = await response.json();
    menuCache.set(cacheKey, { data, expiresAtMs: Date.now() + MENU_CACHE_TTL_MS });
    return res.json(data);
  } catch (error) {
    console.error("Toast menu error", error);
    return res.status(500).json({ error: "Toast menu request failed" });
  }
});

app.post("/api/toast/prices", async (req, res) => {
  const pricingUrl = process.env.TOAST_PRICING_URL;
  const accessToken = await getAccessToken();

  if (FORCE_DEMO) {
    const cart = req.body?.cart;
    if (!cart || !Array.isArray(cart.lines)) {
      return res.status(400).json({ error: "cart.lines is required" });
    }
    const subtotalCents = cart.lines.reduce((sum, line) => sum + (line.lineTotalCents ?? 0), 0);
    const taxCents = Math.round(subtotalCents * 0.085);
    const feesCents = subtotalCents > 0 ? 0 : 0;
    return res.json({
      subtotalCents,
      taxCents,
      feesCents,
      totalCents: subtotalCents + taxCents + feesCents,
    });
  }

  if (pricingUrl && accessToken) {
    try {
      const response = await fetch(pricingUrl, {
        method: "POST",
        headers: { Authorization: `Bearer ${accessToken}`, "Content-Type": "application/json" },
        body: JSON.stringify(req.body ?? {}),
      });
      if (!response.ok) {
        const message = await response.text();
        return res.status(response.status).json({ error: message || "Toast pricing error" });
      }
      const data = await response.json();
      return res.json(data);
    } catch (error) {
      console.error("Toast pricing error", error);
      return res.status(500).json({ error: "Toast pricing request failed" });
    }
  }

  const cart = req.body?.cart;
  if (!cart || !Array.isArray(cart.lines)) {
    return res.status(400).json({ error: "cart.lines is required" });
  }

  const subtotalCents = cart.lines.reduce((sum, line) => sum + (line.lineTotalCents ?? 0), 0);
  const taxCents = Math.round(subtotalCents * 0.085);
  const feesCents = subtotalCents > 0 ? 0 : 0;
  return res.json({
    subtotalCents,
    taxCents,
    feesCents,
    totalCents: subtotalCents + taxCents + feesCents,
  });
});

app.get("/api/debug/menu", async (req, res) => {
  const menuUrl = process.env.TOAST_MENU_URL;
  const locationId = req.query.locationId?.toString();
  const useStandardAuth =
    MENU_USE_STANDARD_AUTH ||
    (menuUrl ? menuUrl.includes("api.toasttab.com/menus") || menuUrl.includes("/menus/") : false);

  if (!menuUrl) {
    return res.json({ ok: false, error: "TOAST_MENU_URL not set" });
  }

  const accessToken = await getAccessToken({ useStandard: useStandardAuth });
  if (!accessToken) {
    return res.json({
      ok: false,
      error: useStandardAuth
        ? "Missing Standard API credentials (TOAST_STANDARD_API_CLIENT_ID/SECRET)"
        : "Missing API credentials (TOAST_API_CLIENT_ID/SECRET)",
      useStandardAuth,
      menuUrl,
    });
  }

  try {
    const url = new URL(menuUrl);
    const headers = { Authorization: `Bearer ${accessToken}` };
    let usedHeader = null;
    if (locationId) {
      const value = locationId.toString();
      // Always use Toast-Restaurant-External-ID header - it should contain the restaurant GUID
      headers["Toast-Restaurant-External-ID"] = value;
      usedHeader = "Toast-Restaurant-External-ID";
    }
    const response = await fetch(url.toString(), { headers });
    const rawText = await response.text();
    if (!response.ok) {
      return res.json({
        ok: false,
        status: response.status,
        statusText: response.statusText,
        useStandardAuth,
        menuUrl,
        locationId: locationId ?? null,
        usedHeader,
        error: rawText?.slice(0, 500) || "Menu request failed",
      });
    }
    let parsed;
    try {
      parsed = JSON.parse(rawText);
    } catch (error) {
      return res.json({
        ok: false,
        status: response.status,
        useStandardAuth,
        menuUrl,
        locationId: locationId ?? null,
        usedHeader,
        error: "Menu response was not valid JSON",
      });
    }
    const summary = {
      menus: Array.isArray(parsed?.menus) ? parsed.menus.length : null,
      menuGroups: Array.isArray(parsed?.menuGroups) ? parsed.menuGroups.length : null,
      menuItems: Array.isArray(parsed?.menuItems || parsed?.items) ? (parsed.menuItems || parsed.items).length : null,
      modifierGroups: Array.isArray(parsed?.modifierGroups) ? parsed.modifierGroups.length : null,
      modifierOptions: Array.isArray(parsed?.modifierOptions) ? parsed.modifierOptions.length : null,
    };
    return res.json({
      ok: true,
      status: response.status,
      useStandardAuth,
      menuUrl,
      locationId: locationId ?? null,
      usedHeader,
      summary,
    });
  } catch (error) {
    console.error("Menu debug error", error);
    return res.json({ ok: false, error: "Menu debug request failed" });
  }
});

// ── File upload for menu board assets (logos, promo images, videos) ──
const ALLOWED_EXTENSIONS = new Set([".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".mp4", ".webm"]);
const MAX_UPLOAD_BYTES = 50 * 1024 * 1024; // 50 MB

app.post("/api/uploads/menu-board", requireAdmin, (req, res) => {
  const contentType = req.headers["content-type"] ?? "";
  if (!contentType.includes("multipart/form-data")) {
    return res.status(400).json({ error: "Content-Type must be multipart/form-data" });
  }

  const boundaryMatch = contentType.match(/boundary=([^\s;]+)/);
  if (!boundaryMatch) {
    return res.status(400).json({ error: "Missing boundary in Content-Type" });
  }

  const chunks = [];
  let totalSize = 0;
  let aborted = false;

  req.on("data", (chunk) => {
    totalSize += chunk.length;
    if (totalSize > MAX_UPLOAD_BYTES && !aborted) {
      aborted = true;
      req.destroy();
      return res.status(413).json({ error: "File too large (max 50 MB)" });
    }
    chunks.push(chunk);
  });

  req.on("end", () => {
    if (aborted || res.headersSent) return;
    try {
      const buffer = Buffer.concat(chunks);
      const boundary = boundaryMatch[1];

      // Find the file part
      const boundaryBuf = Buffer.from(`--${boundary}`);
      const parts = [];
      let start = 0;
      while (true) {
        const idx = buffer.indexOf(boundaryBuf, start);
        if (idx === -1) break;
        if (start > 0) parts.push(buffer.subarray(start, idx));
        start = idx + boundaryBuf.length;
      }

      let filename = null;
      let fileData = null;
      for (const part of parts) {
        const headerEnd = part.indexOf("\r\n\r\n");
        if (headerEnd === -1) continue;
        const headerStr = part.subarray(0, headerEnd).toString();
        const fnMatch = headerStr.match(/filename="([^"]+)"/);
        if (fnMatch) {
          filename = fnMatch[1];
          // Skip the \r\n\r\n header separator and trim trailing \r\n
          fileData = part.subarray(headerEnd + 4);
          if (fileData.length >= 2 && fileData[fileData.length - 2] === 0x0d && fileData[fileData.length - 1] === 0x0a) {
            fileData = fileData.subarray(0, fileData.length - 2);
          }
          break;
        }
      }

      if (!filename || !fileData || fileData.length === 0) {
        return res.status(400).json({ error: "No file found in upload" });
      }

      const ext = extname(filename).toLowerCase();
      if (!ALLOWED_EXTENSIONS.has(ext)) {
        return res.status(400).json({ error: `File type ${ext} not allowed. Use: ${[...ALLOWED_EXTENSIONS].join(", ")}` });
      }

      const savedName = `${randomUUID()}${ext}`;
      const savedPath = join(UPLOADS_DIR, savedName);
      const ws = createWriteStream(savedPath);
      ws.end(fileData);
      ws.on("finish", () => {
        const url = `/uploads/${savedName}`;
        console.log(`Upload saved: ${savedName} (${fileData.length} bytes)`);
        return res.json({ url });
      });
      ws.on("error", (err) => {
        console.error("Upload write error", err);
        return res.status(500).json({ error: "Failed to save file" });
      });
    } catch (err) {
      console.error("Upload parse error", err);
      return res.status(500).json({ error: "Upload processing failed" });
    }
  });
});

// ── Admin auth middleware ──
const ADMIN_TOKEN = process.env.ADMIN_TOKEN ?? "";

function requireAdmin(req, res, next) {
  // If no ADMIN_TOKEN is set, allow all requests (dev mode)
  if (!ADMIN_TOKEN) return next();
  const authHeader = req.headers.authorization ?? "";
  const token = authHeader.startsWith("Bearer ") ? authHeader.slice(7) : "";
  if (token === ADMIN_TOKEN) return next();
  return res.status(401).json({ error: "Unauthorized. Admin token required." });
}

// ── Board config API ──
function sanitizeLocationId(id) {
  // Allow UUIDs and simple alphanumeric+hyphen IDs
  return id.replace(/[^a-zA-Z0-9\-_]/g, "");
}

// GET config (public — TVs can read without auth)
app.get("/api/board-config/:locationId", (req, res) => {
  const locId = sanitizeLocationId(req.params.locationId);
  if (!locId) return res.status(400).json({ error: "Invalid location ID" });

  const configPath = join(CONFIGS_DIR, `${locId}.json`);
  if (!existsSync(configPath)) {
    return res.json({ config: null, version: null });
  }
  try {
    const raw = readFileSync(configPath, "utf-8");
    const data = JSON.parse(raw);
    const version = createHash("md5").update(raw).digest("hex").slice(0, 12);
    return res.json({ config: data, version });
  } catch (err) {
    console.error(`Config read error for ${locId}:`, err);
    return res.status(500).json({ error: "Failed to read config" });
  }
});

// GET config version only (lightweight polling endpoint)
app.get("/api/board-config/:locationId/version", (req, res) => {
  const locId = sanitizeLocationId(req.params.locationId);
  if (!locId) return res.status(400).json({ error: "Invalid location ID" });

  const configPath = join(CONFIGS_DIR, `${locId}.json`);
  if (!existsSync(configPath)) {
    return res.json({ version: null });
  }
  try {
    const raw = readFileSync(configPath, "utf-8");
    const version = createHash("md5").update(raw).digest("hex").slice(0, 12);
    return res.json({ version });
  } catch (err) {
    return res.json({ version: null });
  }
});

// POST config (admin only)
app.post("/api/board-config/:locationId", requireAdmin, (req, res) => {
  const locId = sanitizeLocationId(req.params.locationId);
  if (!locId) return res.status(400).json({ error: "Invalid location ID" });

  const config = req.body;
  if (!config || typeof config !== "object") {
    return res.status(400).json({ error: "Request body must be a JSON object" });
  }

  const configPath = join(CONFIGS_DIR, `${locId}.json`);
  try {
    const raw = JSON.stringify(config, null, 2);
    writeFileSync(configPath, raw, "utf-8");
    const version = createHash("md5").update(raw).digest("hex").slice(0, 12);
    console.log(`Config saved for location ${locId} (version: ${version})`);
    return res.json({ ok: true, version });
  } catch (err) {
    console.error(`Config write error for ${locId}:`, err);
    return res.status(500).json({ error: "Failed to save config" });
  }
});

// ── Production static file serving ──
const MENUBOARD_BUILD_DIR = join(__dirname, "..", "..", "backend", "menuboard");
const isProduction = process.env.NODE_ENV === "production";

if (isProduction && existsSync(MENUBOARD_BUILD_DIR)) {
  console.log(`Serving production build from ${MENUBOARD_BUILD_DIR}`);
  app.use("/menuboard", express.static(MENUBOARD_BUILD_DIR));
  // SPA fallback: any /menuboard/* route that doesn't match a file → index.html
  app.get("/menuboard/*", (req, res) => {
    res.sendFile(join(MENUBOARD_BUILD_DIR, "index.html"));
  });
} else if (isProduction) {
  console.warn("Production mode but no build found at", MENUBOARD_BUILD_DIR);
}

app.listen(PORT, () => {
  console.log(`Menu board backend listening on http://localhost:${PORT}`);
  if (ADMIN_TOKEN) {
    console.log("Admin auth enabled (ADMIN_TOKEN is set)");
  } else {
    console.log("Admin auth disabled (no ADMIN_TOKEN — all writes allowed)");
  }
  if (isProduction) {
    console.log(`Production mode — serving built app at /menuboard/`);
  }
});