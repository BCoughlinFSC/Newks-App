import type { Menu, MenuItem } from "../core/types";

export type BoardTheme = "modernDark" | "light" | "newksFresh";

/** @deprecated kept for config migration only */
export type BoardLayout = "threeColumnModernDark";

export type CategoryConfig = {
  categoryId: string;
  hidden?: boolean;
  displayName?: string;
  sortOrder?: number;
  /**
   * Item indices (0-based) where the category should split into a new card.
   * E.g. [5, 10] creates 3 cards: items 0-4, 5-9, 10+.
   */
  splitPoints?: number[];
};

export type ItemConfig = {
  itemId: string;
  hidden?: boolean;
  featured?: boolean;
  displayName?: string;
  descriptionOverride?: string;
  priceOverrideCents?: number;
  imageOverrideUrl?: string;
};

export type FeatureSlot = {
  id: string;
  title?: string;
  subtitle?: string;
  imageUrl?: string;
  videoUrl?: string;
};

/** Visual branding overrides for the board */
export type BrandingConfig = {
  logoUrl?: string;
  primaryColor?: string;
  backgroundColor?: string;
  backgroundImageUrl?: string;
  tagline?: string;
};

/** Marketing / promo content (images and video) */
export type MarketingConfig = {
  promoImageUrls: string[];
  promoVideoUrl?: string;
};

/** @deprecated kept for config migration only */
export type ScreenLayout = "menuOnly" | "heroLeft" | "heroRight" | "fullHero";

/** Position and size of a block within the grid */
export type GridPlacement = {
  col: number;      // 1-based column start
  row: number;      // 1-based row start
  colSpan: number;  // columns to span (min 1)
  rowSpan: number;  // rows to span (min 1)
};

/** A block placed on the grid — category card, image, video, or promo carousel */
export type GridBlock = {
  id: string;
  type: "category" | "image" | "video" | "promo";
  categoryId?: string;   // for type "category"
  categoryPart?: number; // for split categories: 0-based part index
  imageUrl?: string;     // for type "image"
  videoUrl?: string;     // for type "video"
  placement: GridPlacement;
};

export type ScreenDefinition = {
  id: string;
  label: string;
  width: number;
  height: number;
  /** Category IDs to show on this screen (meaning depends on useAllCategories). */
  categoryIds: string[];
  /**
   * When true or undefined, this screen shows all available categories
   * and categoryIds is ignored. When false, only categories whose IDs
   * are listed in categoryIds are shown (empty list = show none).
   */
  useAllCategories?: boolean;
  /** Optional background image for this screen */
  backgroundImageUrl?: string;
  /** Optional background video for this screen */
  backgroundVideoUrl?: string;
  /** Grid layout */
  gridColumns: number;
  gridRows: number;
  gridBlocks: GridBlock[];
  fontSize?: "small" | "medium" | "large";
  /** @deprecated kept for config migration */
  showPromo?: boolean;
  /** @deprecated kept for config migration */
  layout?: ScreenLayout;
  /** @deprecated kept for config migration */
  gridEnabled?: boolean;
};

export type BoardConfig = {
  theme: BoardTheme;
  categoryOrder: string[];
  categoryConfigs: Record<string, CategoryConfig>;
  itemConfigs: Record<string, ItemConfig>;
  featureSlots: FeatureSlot[];
  autoCycleSeconds: number;
  showPrices: boolean;
  showDescriptions: boolean;
  /** Customization: branding, marketing, multi-screen */
  branding?: BrandingConfig;
  marketing?: MarketingConfig;
  screens?: ScreenDefinition[];
  /** Set to true after the setup wizard has been completed */
  setupComplete?: boolean;
  /** @deprecated kept for config migration */
  layout?: BoardLayout;
};

export type BoardItem = {
  source: MenuItem;
  hidden: boolean;
  featured: boolean;
  effectiveName: string;
  effectiveDescription?: string;
  effectivePriceCents: number;
  effectiveImageUrl?: string;
};

export type BoardCategory = {
  id: string;
  name: string;
  sortOrder: number;
  hidden: boolean;
  items: BoardItem[];
};

export type BoardViewModel = {
  categories: BoardCategory[];
  menu: Menu;
};

