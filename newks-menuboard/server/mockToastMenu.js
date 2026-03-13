import { mockMenu } from "./mockMenu.js";

function buildMockToastMenu(menu) {
  const modifierOptions = [];
  const modifierGroups = [];
  const menuGroups = menu.categories.map((cat) => ({
    guid: cat.id,
    name: cat.name,
    itemIds: menu.items.filter((item) => item.categoryId === cat.id).map((item) => item.id),
  }));

  const menuItems = menu.items.map((item) => {
    const modifierGroupIds = [];
    for (const group of item.modifierGroups ?? []) {
      modifierGroupIds.push(group.id);
      modifierGroups.push({
        guid: group.id,
        name: group.name,
        minSelections: group.minSelect ?? 0,
        maxSelections: group.maxSelect ?? 1,
        modifierOptionIds: group.options.map((opt) => opt.id),
      });
      for (const option of group.options) {
        modifierOptions.push({
          guid: option.id,
          name: option.name,
          price: option.priceDeltaCents ?? 0,
        });
      }
    }

    return {
      guid: item.id,
      name: item.name,
      description: item.description,
      price: item.basePriceCents,
      menuGroupGuid: item.categoryId,
      modifierGroupIds,
    };
  });

  return {
    menus: [
      {
        guid: "menu-rooted",
        name: "Rooted Menu",
        menuGroups,
      },
    ],
    menuItems,
    modifierGroups,
    modifierOptions,
  };
}

export const mockToastMenu = buildMockToastMenu(mockMenu);
