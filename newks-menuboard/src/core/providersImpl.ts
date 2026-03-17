import { MenuProvider, OrderSubmitter, PricingProvider } from "./providers";
import { Cart, Menu, ModifierGroup, ModifierOption, Pricing } from "./types";

type ProviderOptions = {
  baseUrl?: string;
  locationId?: string | null;
};

export class ApiError extends Error {
  status: number;
  body?: string;

  constructor(status: number, message: string, body?: string) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

async function fetchJson<T>(input: string, init?: RequestInit): Promise<T> {
  const res = await fetch(input, init);
  if (!res.ok) {
    const body = await res.text();
    const message = body || res.statusText || "Request failed";
    throw new ApiError(res.status, `Request failed (${res.status}): ${message}`, body);
  }
  return res.json() as Promise<T>;
}

function buildUrl(path: string, options?: ProviderOptions): string {
  const baseUrl = options?.baseUrl ?? "";
  const url = new URL(`${baseUrl}${path}`, window.location.origin);
  if (options?.locationId) url.searchParams.set("locationId", options.locationId);
  return url.toString();
}

export class ApiMenuProvider implements MenuProvider {
  constructor(private options?: ProviderOptions) {}

  async getMenu(): Promise<Menu> {
    return fetchJson<Menu>(buildUrl("/api/menu", this.options));
  }
}

type ToastMenuResponse = {
  menus?: ToastMenu[];
  menuGroups?: ToastMenuGroup[];
  menuItems?: ToastMenuItem[];
  items?: ToastMenuItem[];
  modifierGroups?: ToastModifierGroup[];
  modifierOptions?: ToastModifierOption[];
  modifierGroupReferences?: Record<string, ToastModifierGroup>;
  modifierOptionReferences?: Record<string, ToastModifierOption>;
};

type ToastMenu = {
  guid?: string;
  id?: string;
  name?: string;
  menuGroups?: ToastMenuGroup[];
  groups?: ToastMenuGroup[];
};

type ToastMenuGroup = {
  guid?: string;
  id?: string;
  name?: string;
  itemIds?: string[];
  menuItemIds?: string[];
  items?: string[];
  menuGroups?: ToastMenuGroup[];
  groups?: ToastMenuGroup[];
};

/** Toast can send image as a URL string, or { url }, or images[] - see Toast Menus Image/Images schema */
function resolveImageUrl(obj: {
  imageUrl?: string;
  image?: string | { url?: string };
  images?: string[];
}): string | undefined {
  if (Array.isArray(obj.images) && obj.images.length > 0 && typeof obj.images[0] === "string")
    return obj.images[0];
  if (obj.imageUrl && typeof obj.imageUrl === "string") return obj.imageUrl;
  if (obj.image != null) {
    if (typeof obj.image === "string") return obj.image;
    if (typeof obj.image === "object" && typeof (obj.image as { url?: string }).url === "string")
      return (obj.image as { url: string }).url;
  }
  return undefined;
}

type ToastMenuItem = {
  guid?: string;
  id?: string;
  name?: string;
  description?: string;
  imageUrl?: string;
  image?: string | { url?: string };
  images?: string[];
  price?: number;
  basePrice?: number;
  unitPrice?: number;
  menuGroupGuid?: string;
  menuGroupId?: string;
  menuGroup?: { guid?: string; id?: string };
  menuGroupGuids?: string[];
  modifierGroupIds?: string[];
  modifierGroupGuids?: string[];
  modifierGroups?: string[];
  modifierGroupReferences?: { guid?: string; id?: string }[];
  outOfStock86?: boolean;
  outOfStock?: boolean;
  visibility?: string;
};

type ToastModifierGroup = {
  guid?: string;
  id?: string;
  name?: string;
  minSelections?: number;
  maxSelections?: number;
  modifierOptionIds?: string[];
  optionIds?: string[];
  modifierOptions?: ToastModifierOption[];
  options?: ToastModifierOption[];
};

type ToastModifierOption = {
  guid?: string;
  id?: string;
  name?: string;
  price?: number;
  priceAmount?: number;
  amount?: number;
  value?: number;
  imageUrl?: string;
  image?: string | { url?: string };
  images?: string[];
};

function getId(entity?: { guid?: string; id?: string } | null): string | undefined {
  if (!entity) return undefined;
  return entity.guid ?? entity.id;
}

function normalizeMoneyToCents(value: number | undefined): number {
  if (value === undefined || value === null || Number.isNaN(value)) return 0;
  if (!Number.isFinite(value)) return 0;
  if (!Number.isInteger(value)) return Math.round(value * 100);
  return value;
}

function extractMenuGroups(response: ToastMenuResponse): ToastMenuGroup[] {
  const fromMenus = response.menus?.flatMap((m) => m.menuGroups ?? m.groups ?? []) ?? [];
  const direct = response.menuGroups ?? [];
  return [...fromMenus, ...direct];
}

function flattenGroups(groups: ToastMenuGroup[]): ToastMenuGroup[] {
  const out: ToastMenuGroup[] = [];
  const stack = [...groups];
  while (stack.length) {
    const group = stack.shift();
    if (!group) continue;
    out.push(group);
    const nested = group.menuGroups ?? group.groups ?? [];
    stack.push(...nested);
  }
  return out;
}

function stripParentheticalSegments(name?: string | null): string | undefined {
  if (!name) return name ?? undefined;
  return name.replace(/\s*\([^)]*\)/g, "").trim() || name;
}

function collectItemsFromGroups(groups: ToastMenuGroup[]): { items: ToastMenuItem[]; itemToGroupId: Map<string, string> } {
  const collected: ToastMenuItem[] = [];
  const itemToGroupId = new Map<string, string>();
  for (const group of groups) {
    const groupId = getId(group);
    const groupItems = (group as unknown as { menuItems?: ToastMenuItem[] }).menuItems ?? [];
    for (const item of groupItems) {
      const itemId = getId(item);
      if (itemId && groupId) {
        itemToGroupId.set(itemId, groupId);
      }
      collected.push(item);
    }
  }
  return { items: collected, itemToGroupId };
}

function mapToastMenuToMenu(response: ToastMenuResponse): Menu {
  const rawGroups = flattenGroups(extractMenuGroups(response));
  const groupItemData = collectItemsFromGroups(rawGroups);
  const rawItems = response.menuItems ?? response.items ?? groupItemData.items;
  const rawModifierGroups =
    response.modifierGroups ?? Object.values(response.modifierGroupReferences ?? {});
  const rawModifierOptions =
    response.modifierOptions ?? Object.values(response.modifierOptionReferences ?? {});

  const modifierOptionById = new Map<string, ToastModifierOption>();
  for (const option of rawModifierOptions) {
    const id = getId(option);
    if (id) modifierOptionById.set(id, option);
  }

  const modifierGroupById = new Map<string, ToastModifierGroup>();
  for (const group of rawModifierGroups) {
    const id = getId(group);
    if (id) modifierGroupById.set(id, group);
  }

  const itemToGroupId = new Map<string, string>(groupItemData.itemToGroupId);
  for (const group of rawGroups) {
    const groupId = getId(group);
    if (!groupId) continue;
    const itemIds = group.itemIds ?? group.menuItemIds ?? group.items ?? [];
    for (const itemId of itemIds) {
      itemToGroupId.set(itemId, groupId);
    }
  }

  const itemToCategoryId = new Map<string, string>();
  const menuCategories: { id: string; name: string; sort: number }[] = [];
  const menus = response.menus ?? [];
  menus.forEach((menu, menuIndex) => {
    const menuId = getId(menu) ?? `menu-${menuIndex + 1}`;
    const menuGroups = flattenGroups(menu.menuGroups ?? menu.groups ?? []);
    menuGroups.forEach((group, groupIndex) => {
      const groupId = getId(group) ?? `group-${menuId}-${groupIndex + 1}`;
      const categoryId = `${menuId}:${groupId}`;
      menuCategories.push({
        id: categoryId,
        // Show only the group name (no menu prefix) and strip things like "(OLO)"
        name: stripParentheticalSegments(group.name) ?? `Category ${groupIndex + 1}`,
        sort: menuCategories.length + 1,
      });
      const groupItems = (group as unknown as { menuItems?: ToastMenuItem[] }).menuItems ?? [];
      for (const item of groupItems) {
        const itemId = getId(item);
        if (itemId && !itemToCategoryId.has(itemId)) {
          itemToCategoryId.set(itemId, categoryId);
        }
      }
      const groupItemIds = group.itemIds ?? group.menuItemIds ?? group.items ?? [];
      for (const itemId of groupItemIds) {
        if (itemId && !itemToCategoryId.has(itemId)) {
          itemToCategoryId.set(itemId, categoryId);
        }
      }
    });
  });

  const categories =
    menuCategories.length > 0
      ? menuCategories
      : rawGroups.length > 0
        ? rawGroups.map((g, index) => ({
            id: getId(g) ?? `cat-${index + 1}`,
            name: g.name ?? `Category ${index + 1}`,
            sort: index + 1,
          }))
        : [{ id: "cat-menu", name: "Menu", sort: 1 }];

  const fallbackCategoryId = categories[0]?.id ?? "cat-menu";

  const items = rawItems.map((item, index) => {
    const itemId = getId(item) ?? `item-${index + 1}`;
    const categoryId =
      itemToCategoryId.get(itemId) ??
      itemToGroupId.get(itemId) ??
      item.menuGroupGuid ??
      item.menuGroupId ??
      getId(item.menuGroup) ??
      item.menuGroupGuids?.[0] ??
      fallbackCategoryId;

    const modifierGroupIds =
      item.modifierGroupIds ??
      item.modifierGroupGuids ??
      item.modifierGroups ??
      item.modifierGroupReferences?.map((ref) => getId(ref)).filter(Boolean) ??
      [];

    const modifierGroups = modifierGroupIds
      .map((id) => (id ? modifierGroupById.get(id) : undefined))
      .filter(Boolean)
      .map((group) => mapModifierGroup(group!, modifierOptionById));

    const imageUrl = resolveImageUrl(item);

    // Toast Menus V2 uses "outOfStock86", Config API uses "visibility" === "HIDDEN"
    const outOfStock = !!(item.outOfStock86 ?? item.outOfStock ?? item.visibility === "HIDDEN");

    return {
      id: itemId,
      name: item.name ?? `Item ${index + 1}`,
      description: item.description,
      imageUrl: imageUrl ?? undefined,
      basePriceCents: normalizeMoneyToCents(item.price ?? item.basePrice ?? item.unitPrice ?? 0),
      modifierGroups: modifierGroups.length > 0 ? modifierGroups : undefined,
      categoryId,
      outOfStock: outOfStock || undefined,
    };
  });

  return { categories, items };
}

function mapModifierGroup(group: ToastModifierGroup, optionLookup: Map<string, ToastModifierOption>): ModifierGroup {
  const groupId = getId(group) ?? "mod-group";
  const optionIds = group.modifierOptionIds ?? group.optionIds ?? [];
  const directOptions = group.modifierOptions ?? group.options ?? [];

  const options: ModifierOption[] = [];

  for (const option of directOptions) {
    const optId = getId(option) ?? `${groupId}-opt-${options.length + 1}`;
    options.push(mapModifierOption(option, optId));
  }

  for (const optionId of optionIds) {
    if (!optionId || options.some((o) => o.id === optionId)) continue;
    const option = optionLookup.get(optionId);
    if (!option) continue;
    options.push(mapModifierOption(option, optionId));
  }

  return {
    id: groupId,
    name: group.name ?? "Options",
    minSelect: group.minSelections ?? 0,
    maxSelect: group.maxSelections ?? 1,
    options,
  };
}

function mapModifierOption(option: ToastModifierOption, fallbackId: string): ModifierOption {
  const optId = getId(option) ?? fallbackId;
  const priceValue = option.price ?? option.priceAmount ?? option.amount ?? option.value ?? 0;
  const imageUrl = resolveImageUrl(option);
  return {
    id: optId,
    name: option.name ?? "Option",
    priceDeltaCents: normalizeMoneyToCents(priceValue),
    imageUrl: imageUrl ?? undefined,
  };
}

export class ToastMenuProvider implements MenuProvider {
  constructor(private options?: ProviderOptions) {}

  async getMenu(): Promise<Menu> {
    const response = await fetchJson<ToastMenuResponse>(buildUrl("/api/toast/menu", this.options));
    return mapToastMenuToMenu(response);
  }
}

export class ToastPricingProvider implements PricingProvider {
  constructor(private options?: ProviderOptions) {}

  async priceCart(args: { menu: Menu; cart: Cart }): Promise<Pricing> {
    return fetchJson<Pricing>(buildUrl("/api/toast/prices", this.options), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...args, locationId: this.options?.locationId ?? null }),
    });
  }
}

export class SimplePricingProvider implements PricingProvider {
  async priceCart(args: { menu: Menu; cart: Cart }): Promise<Pricing> {
    const subtotalCents = args.cart.lines.reduce((sum, l) => sum + l.lineTotalCents, 0);
    const taxCents = Math.round(subtotalCents * 0.085); // placeholder tax
    const feesCents = subtotalCents > 0 ? 0 : 0;
    return {
      subtotalCents,
      taxCents,
      feesCents,
      totalCents: subtotalCents + taxCents + feesCents,
    };
  }
}

export class MockOrderSubmitter implements OrderSubmitter {
  async submit(): Promise<{ orderId: string }> {
    await new Promise((r) => setTimeout(r, 400));
    return { orderId: "DEMO-" + Math.floor(Math.random() * 1_000_000).toString().padStart(6, "0") };
  }
}
