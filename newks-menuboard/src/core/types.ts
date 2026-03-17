export type Money = number; // cents

export type ModifierOption = {
  id: string;
  name: string;
  priceDeltaCents?: Money;
  imageUrl?: string;
};

export type ModifierGroup = {
  id: string;
  name: string;
  minSelect?: number;  // default 0
  maxSelect?: number;  // default 1
  options: ModifierOption[];
};

export type MenuItem = {
  id: string;
  name: string;
  description?: string;
  imageUrl?: string;
  basePriceCents: Money;
  modifierGroups?: ModifierGroup[];
  categoryId: string;
  outOfStock?: boolean;
};

export type MenuCategory = {
  id: string;
  name: string;
  sort?: number;
};

export type Menu = {
  categories: MenuCategory[];
  items: MenuItem[];
};

export type CartModifierSelection = {
  groupId: string;
  optionIds: string[];
};

export type CartLine = {
  lineId: string; // unique per cart line
  itemId: string;
  name: string;
  qty: number;
  basePriceCents: Money;
  selections: CartModifierSelection[];
  selectionsPriceCents: Money;
  lineTotalCents: Money;
};

export type Cart = {
  lines: CartLine[];
};

export type Pricing = {
  subtotalCents: Money;
  taxCents: Money;
  feesCents: Money;
  totalCents: Money;
};
