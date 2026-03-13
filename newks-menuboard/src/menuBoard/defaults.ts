import type { BoardConfig } from "./types";

export const MENU_BOARD_STORAGE_KEY = "menuBoardConfig";

export const defaultBoardConfig: BoardConfig = {
  theme: "newksFresh",
  categoryOrder: [],
  categoryConfigs: {},
  itemConfigs: {},
  featureSlots: [],
  autoCycleSeconds: 15,
  showPrices: true,
  showDescriptions: true,
  branding: {},
  marketing: { promoImageUrls: [] },
  screens: [
    { id: "1", label: "Screen 1", width: 1920, height: 1080, categoryIds: [], gridColumns: 6, gridRows: 4, gridBlocks: [] },
  ],
};

