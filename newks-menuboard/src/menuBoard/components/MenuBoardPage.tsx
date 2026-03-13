import React, { useEffect, useMemo, useRef, useState } from "react";
import type { Menu } from "../../core/types";
import type { BoardConfig, BoardViewModel, ScreenDefinition } from "../types";
import { mergeMenuWithConfig } from "../utils/mergeMenuWithConfig";
import { autoLayoutGrid } from "../utils/autoLayoutGrid";
import { getConfigVersion, loadBoardConfigFromServer } from "../utils/storage";
import { BoardHeader } from "./BoardHeader";
import { GridBoardRenderer } from "./GridBoardRenderer";
import { SettingsDrawer } from "./SettingsDrawer";
import { SettingsPage } from "./SettingsPage";
import "../menuBoard.css";

function getScreenIndexFromUrl(): number | null {
  if (typeof window === "undefined") return null;
  const p = new URLSearchParams(window.location.search);
  const s = p.get("screen");
  if (s == null) return null;
  const n = parseInt(s, 10);
  return Number.isFinite(n) && n >= 1 ? n : null;
}

type Props = {
  menu: Menu | null;
  boardConfig: BoardConfig;
  onBoardConfigChange: (next: BoardConfig) => void;
  locationName: string | null;
  demoMode: boolean;
  onResetLocation?: () => void;
  locationId?: string | null;
  configVersion?: string | null;
  onMenuRefresh?: () => void;
};

export function MenuBoardPage({
  menu,
  boardConfig,
  onBoardConfigChange,
  locationName,
  demoMode,
  onResetLocation,
  locationId,
  configVersion,
  onMenuRefresh,
}: Props) {
  const [quickSetupOpen, setQuickSetupOpen] = useState(false);
  const [settingsPageOpen, setSettingsPageOpen] = useState(false);

  const viewModel: BoardViewModel | null = useMemo(
    () => (menu ? mergeMenuWithConfig(menu, boardConfig) : null),
    [menu, boardConfig]
  );

  const defaultScreen: ScreenDefinition = { id: "1", label: "Screen 1", width: 1920, height: 1080, categoryIds: [], gridColumns: 6, gridRows: 4, gridBlocks: [] };
  const screens = useMemo(
    () => (Array.isArray(boardConfig.screens) && boardConfig.screens.length > 0 ? boardConfig.screens : [defaultScreen]),
    [boardConfig.screens]
  );

  const screenIndexFromUrl = getScreenIndexFromUrl();

  const categories = useMemo(() => {
    const all = viewModel?.categories ?? [];
    if (!screenIndexFromUrl || screenIndexFromUrl > screens.length) return all;
    const screen = screens[screenIndexFromUrl - 1];
    if (screen.useAllCategories !== false) {
      return all;
    }
    if (!screen.categoryIds.length) return [];
    const idSet = new Set(screen.categoryIds);
    return all.filter((c) => idSet.has(c.id));
  }, [viewModel?.categories, screenIndexFromUrl, screens]);

  const activeScreen = screenIndexFromUrl && screenIndexFromUrl <= screens.length
    ? screens[screenIndexFromUrl - 1]
    : screens[0];

  // Auto-populate grid blocks on first render if screen has none
  const didAutoLayout = useRef(false);
  useEffect(() => {
    if (didAutoLayout.current) return;
    if (!activeScreen || !categories.length) return;
    if (activeScreen.gridBlocks && activeScreen.gridBlocks.length > 0) return;

    didAutoLayout.current = true;
    const hasPromo = boardConfig.featureSlots.length > 0;
    const cols = activeScreen.gridColumns ?? 6;
    const rows = activeScreen.gridRows ?? 4;
    const visibleCats = categories.filter((c) => !c.hidden);
    const { blocks, rows: newRows } = autoLayoutGrid(visibleCats, cols, rows, [], hasPromo);

    const updatedScreens = (boardConfig.screens ?? [defaultScreen]).map((s) =>
      s.id === activeScreen.id ? { ...s, gridBlocks: blocks, gridRows: newRows } : s
    );
    onBoardConfigChange({ ...boardConfig, screens: updatedScreens });
  }, [activeScreen, categories, boardConfig]);

  // ── Auto-refresh: poll for config changes from server (TV display mode) ──
  const [knownVersion, setKnownVersion] = useState(configVersion ?? null);
  useEffect(() => { setKnownVersion(configVersion ?? null); }, [configVersion]);

  useEffect(() => {
    if (settingsPageOpen || !locationId) return;
    const POLL_INTERVAL_MS = 15_000;
    const MENU_REFRESH_INTERVAL_MS = 5 * 60_000;

    const configTimer = window.setInterval(async () => {
      const serverVersion = await getConfigVersion(locationId);
      if (serverVersion && serverVersion !== knownVersion) {
        const { config, version } = await loadBoardConfigFromServer(locationId);
        if (config) {
          onBoardConfigChange(config);
          setKnownVersion(version);
        }
      }
    }, POLL_INTERVAL_MS);

    const menuTimer = window.setInterval(() => {
      onMenuRefresh?.();
    }, MENU_REFRESH_INTERVAL_MS);

    return () => {
      window.clearInterval(configTimer);
      window.clearInterval(menuTimer);
    };
  }, [settingsPageOpen, locationId, knownVersion, onBoardConfigChange, onMenuRefresh]);

  return (
    <>
      {settingsPageOpen && viewModel ? (
        <SettingsPage
          boardConfig={boardConfig}
          viewModel={viewModel}
          onChange={onBoardConfigChange}
          onClose={() => setSettingsPageOpen(false)}
          onOpenPreview={() => {}}
        />
      ) : (
        <div
          className={`board-root board-root-${boardConfig.theme} board-display-mode`}
          style={{
            ...(boardConfig.branding?.backgroundColor
              ? { background: boardConfig.branding.backgroundColor }
              : {}),
            ...(boardConfig.branding?.backgroundImageUrl
              ? {
                  backgroundImage: `url(${boardConfig.branding.backgroundImageUrl})`,
                  backgroundSize: "cover",
                  backgroundPosition: "center",
                }
              : {}),
          }}
        >
          <BoardHeader
            locationName={locationName}
            demoMode={demoMode}
            onOpenQuickSetup={() => setQuickSetupOpen(true)}
            onOpenSettings={() => setSettingsPageOpen(true)}
            branding={boardConfig.branding}
          />
          <div style={{ flex: 1, overflow: "hidden" }}>
            <GridBoardRenderer
              blocks={activeScreen?.gridBlocks ?? []}
              gridColumns={activeScreen?.gridColumns ?? 6}
              gridRows={activeScreen?.gridRows ?? 4}
              categories={categories}
              categoryConfigs={boardConfig.categoryConfigs}
              featureSlots={boardConfig.featureSlots}
              freshTheme={boardConfig.theme === "newksFresh"}
              fontSize={activeScreen?.fontSize ?? "medium"}
              editorMode={false}
            />
          </div>
        </div>
      )}
      {quickSetupOpen && viewModel && (
        <SettingsDrawer
          boardConfig={boardConfig}
          viewModel={viewModel}
          onChange={onBoardConfigChange}
          onClose={() => setQuickSetupOpen(false)}
          onResetLocation={onResetLocation}
        />
      )}
    </>
  );
}

