import { Cart, CartLine, Menu, MenuItem } from "./types";

function uid(): string {
  return Math.random().toString(36).slice(2, 10) + "-" + Date.now().toString(36);
}

export function getItem(menu: Menu, itemId: string): MenuItem {
  const item = menu.items.find((i) => i.id === itemId);
  if (!item) throw new Error(`Item not found: ${itemId}`);
  return item;
}

export function calcSelectionsPriceCents(item: MenuItem, selections: CartLine["selections"]): number {
  if (!item.modifierGroups?.length) return 0;
  let total = 0;
  for (const sel of selections) {
    const group = item.modifierGroups.find((g) => g.id === sel.groupId);
    if (!group) continue;
    for (const optId of sel.optionIds) {
      const opt = group.options.find((o) => o.id === optId);
      total += opt?.priceDeltaCents ?? 0;
    }
  }
  return total;
}

export function buildCartLine(menu: Menu, itemId: string, selections: CartLine["selections"]): CartLine {
  const item = getItem(menu, itemId);
  const selectionsPriceCents = calcSelectionsPriceCents(item, selections);
  const lineTotalCents = item.basePriceCents + selectionsPriceCents;
  return {
    lineId: uid(),
    itemId: item.id,
    name: item.name,
    qty: 1,
    basePriceCents: item.basePriceCents,
    selections,
    selectionsPriceCents,
    lineTotalCents,
  };
}

export function addLine(cart: Cart, line: CartLine): Cart {
  return { lines: [...cart.lines, line] };
}

export function setQty(cart: Cart, lineId: string, qty: number): Cart {
  return {
    lines: cart.lines
      .map((l) =>
        l.lineId === lineId
          ? { ...l, qty, lineTotalCents: (l.basePriceCents + l.selectionsPriceCents) * qty }
          : l
      )
      .filter((l) => l.qty > 0),
  };
}

export function removeLine(cart: Cart, lineId: string): Cart {
  return { lines: cart.lines.filter((l) => l.lineId !== lineId) };
}

export function clearCart(): Cart {
  return { lines: [] };
}
