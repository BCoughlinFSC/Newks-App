import React, { useMemo } from "react";
import type { BoardConfig, BoardCategory, BoardViewModel, ScreenDefinition } from "../types";
import { CategoryPanel } from "./CategoryPanel";
import { PromoPanel } from "./PromoPanel";
import "../menuBoard.css";

type Props = {
  viewModel: BoardViewModel | null;
  boardConfig: BoardConfig;
  screens: ScreenDefinition[];
  onBack: () => void;
};

function buildColumns(categories: BoardCategory[], colCount: number): BoardCategory[][] {
  const cols: BoardCategory[][] = Array.from({ length: colCount }, () => []);
  categories.forEach((cat, index) => {
    cols[index % colCount].push(cat);
  });
  return cols;
}

export function PreviewView({ viewModel, boardConfig, screens, onBack }: Props) {
  const theme = boardConfig.theme;
  const hasPromo = boardConfig.featureSlots.length > 0;
  const hasMenu = viewModel != null && viewModel.categories.length > 0;

  return (
    <div className={`board-root board-root-${theme} board-preview-root`}>
      <div className="board-preview-header">
        <button type="button" className="board-settings-btn primary" onClick={onBack}>
          ← Back to board
        </button>
        <span className="board-preview-title">Preview my screens</span>
      </div>
      {!hasMenu && (
        <p className="board-preview-no-menu">
          No menu loaded. Use <strong>Quick Setup</strong> to load a menu and see preview content.
        </p>
      )}
      <div className="board-preview-grid">
        {screens.map((screen) => (
          <ScreenPreviewBox
            key={screen.id}
            screen={screen}
            viewModel={viewModel}
            boardConfig={boardConfig}
            hasPromoDefault={hasPromo}
          />
        ))}
      </div>
    </div>
  );
}

function ScreenPreviewBox({
  screen,
  viewModel,
  boardConfig,
  hasPromoDefault,
}: {
  screen: ScreenDefinition;
  viewModel: BoardViewModel | null;
  boardConfig: BoardConfig;
  hasPromoDefault: boolean;
}) {
  const theme = boardConfig.theme;
  const categories = useMemo(() => {
    const all = viewModel?.categories ?? [];
    if (screen.useAllCategories !== false) {
      return all;
    }
    if (!screen.categoryIds.length) return [];
    const idSet = new Set(screen.categoryIds);
    return all.filter((c) => idSet.has(c.id));
  }, [viewModel?.categories, screen.categoryIds]);
  const showPromo = screen.showPromo && hasPromoDefault;
  const columns = useMemo(() => buildColumns(categories, 3), [categories]);
  const scale = Math.min(320 / screen.width, 180 / screen.height, 1);
  const boxWidth = screen.width * scale;
  const boxHeight = screen.height * scale;
  const layout = screen.layout ?? "menuOnly";
  const hasHeroBackground = !!(screen.backgroundImageUrl || screen.backgroundVideoUrl);

  return (
    <div className="board-preview-box">
      <div className="board-preview-box-label">
        {screen.label} ({screen.width}×{screen.height})
      </div>
      <div className="board-preview-scaled-wrap" style={{ width: boxWidth, height: boxHeight }}>
        <div
          className="board-preview-scaled"
          style={{
            width: screen.width,
            height: screen.height,
            transform: `scale(${scale})`,
            transformOrigin: "top left",
          }}
        >
          <div
            className={`board-preview-inner board-root-${theme} board-layout-${layout} ${
              hasHeroBackground ? "board-preview-with-hero" : ""
            }`}
            style={
              screen.backgroundImageUrl
                ? {
                    backgroundImage: `url(${screen.backgroundImageUrl})`,
                    backgroundSize: "cover",
                    backgroundPosition: "center",
                  }
                : undefined
            }
          >
            {screen.backgroundVideoUrl && (
              <video
                className="board-preview-hero-video"
                src={screen.backgroundVideoUrl}
                autoPlay
                loop
                muted
                playsInline
              />
            )}
            {layout !== "fullHero" && (
              <div className={`board-main ${showPromo ? "board-main-with-promo" : "board-main-full"}`}>
                <div className="board-columns">
                  {columns.map((col, colIdx) => (
                    <div key={colIdx} className="board-column">
                      {col.map((cat) => (
                        <CategoryPanel key={cat.id} category={cat} />
                      ))}
                    </div>
                  ))}
                </div>
                {showPromo && <PromoPanel featureSlots={boardConfig.featureSlots} />}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
