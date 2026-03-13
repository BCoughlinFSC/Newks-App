import React from "react";
import type { BoardCategory, BoardItem } from "../types";
import { formatMoney } from "../../core/money";

type Props = {
  category: BoardCategory;
  /** When true, uses the newksFresh hero-card style for featured items */
  freshTheme?: boolean;
  /** Override items to show (for split categories) */
  itemsOverride?: BoardItem[];
  /** Override title (e.g. "Sandwiches (cont.)") */
  titleOverride?: string;
};

export function CategoryPanel({ category, freshTheme = false, itemsOverride, titleOverride }: Props) {
  const items = itemsOverride ?? category.items;
  const title = titleOverride ?? category.name;

  return (
    <section className="board-category">
      <h2 className="board-category-title">{title}</h2>
      <div className="board-items">
        {items.map((item) => {
          // Featured item with image → hero card (newksFresh only)
          if (freshTheme && item.featured && item.effectiveImageUrl) {
            return (
              <div key={item.source.id} className="board-item board-item-hero">
                <div className="board-item-hero-img-wrap">
                  <img
                    src={item.effectiveImageUrl}
                    alt={item.effectiveName}
                    className="board-item-hero-img"
                    loading="lazy"
                  />
                  {item.effectivePriceCents > 0 && (
                    <div className="board-item-hero-badge">
                      {formatMoney(item.effectivePriceCents)}
                    </div>
                  )}
                </div>
                <div className="board-item-hero-body">
                  <div className="board-item-hero-name">{item.effectiveName}</div>
                  {item.effectiveDescription && (
                    <div className="board-item-hero-desc">{item.effectiveDescription}</div>
                  )}
                </div>
              </div>
            );
          }

          // Standard item row
          return (
            <div key={item.source.id} className="board-item">
              <div className="board-item-main">
                <div className="board-item-text">
                  <div className="board-item-name">
                    {item.effectiveName}
                    {item.featured && (
                      <span className="board-item-pill">Featured</span>
                    )}
                  </div>
                  {item.effectiveDescription && (
                    <div className="board-item-description">
                      {item.effectiveDescription}
                    </div>
                  )}
                </div>
                {item.effectivePriceCents > 0 && (
                  <div className="board-item-price">
                    {formatMoney(item.effectivePriceCents)}
                  </div>
                )}
              </div>
              {/* Show thumbnail image for non-hero items that have images (non-fresh themes) */}
              {!freshTheme && item.effectiveImageUrl && (
                <div className="board-item-image-wrap">
                  <img
                    src={item.effectiveImageUrl}
                    alt={item.effectiveName}
                    className="board-item-image"
                    loading="lazy"
                  />
                </div>
              )}
            </div>
          );
        })}
      </div>
    </section>
  );
}
