import type { Menu } from "../../core/types";
import type { BoardCategory, BoardConfig, BoardItem, BoardViewModel } from "../types";

function stripParentheticalSegments(name: string): string {
  // Remove any "(...)" segments and trim spaces
  return name.replace(/\s*\([^)]*\)/g, "").trim();
}

export function mergeMenuWithConfig(menu: Menu, config: BoardConfig): BoardViewModel {
  const categoryOrder = config.categoryOrder.length
    ? config.categoryOrder
    : menu.categories.map((c) => c.id);

  const categorySortLookup = new Map<string, number>();
  categoryOrder.forEach((id, index) => {
    categorySortLookup.set(id, index + 1);
  });

  const categories: BoardCategory[] = menu.categories.map((cat, index) => {
    const catConfig = config.categoryConfigs[cat.id];
    const sortOrder =
      catConfig?.sortOrder ??
      categorySortLookup.get(cat.id) ??
      cat.sort ??
      index + 1;

    const displayName = catConfig?.displayName ?? cat.name;

    return {
      id: cat.id,
      name: displayName,
      sortOrder,
      hidden: !!catConfig?.hidden,
      items: [],
    };
  });

  const categoryById = new Map<string, BoardCategory>();
  categories.forEach((c) => categoryById.set(c.id, c));

  for (const item of menu.items) {
    const itemConfig = config.itemConfigs[item.id];
    const hidden = !!itemConfig?.hidden;
    const featured = !!itemConfig?.featured;

    const effectiveName =
      itemConfig?.displayName ??
      stripParentheticalSegments(item.name);
    const effectiveDescription =
      config.showDescriptions === false
        ? undefined
        : itemConfig?.descriptionOverride ?? item.description;
    const effectivePriceCents =
      config.showPrices === false
        ? 0
        : itemConfig?.priceOverrideCents ?? item.basePriceCents;
    const effectiveImageUrl =
      itemConfig?.imageOverrideUrl ?? item.imageUrl ?? undefined;

    const boardItem: BoardItem = {
      source: item,
      hidden,
      featured,
      effectiveName,
      effectiveDescription,
      effectivePriceCents,
      effectiveImageUrl,
    };

    const category = categoryById.get(item.categoryId);
    if (!category) continue;
    category.items.push(boardItem);
  }

  const visibleCategories = categories
    .map((cat) => ({
      ...cat,
      items: cat.items.filter((it) => !it.hidden),
    }))
    .filter((cat) => !cat.hidden && cat.items.length > 0)
    .sort((a, b) => a.sortOrder - b.sortOrder);

  return {
    categories: visibleCategories,
    menu,
  };
}

