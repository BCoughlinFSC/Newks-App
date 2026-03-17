import React, { useRef, useEffect, useState } from "react";
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
  /** Uniform font size hint from parent grid (ensures consistent sizing across cards) */
  uniformFontSize?: number | null;
};

export function CategoryPanel({ category, freshTheme = false, itemsOverride, titleOverride, uniformFontSize }: Props) {
  const items = itemsOverride ?? category.items;
  const title = titleOverride ?? category.name;
  const containerRef = useRef<HTMLElement>(null);
  const [localFontSize, setLocalFontSize] = useState<number | null>(null);

  useEffect(() => {
    // Skip local calculation when uniform size is provided
    if (uniformFontSize != null) return;
    const el = containerRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => {
      const h = el.clientHeight;
      if (!h || !items.length) return;
      const availableH = h * 0.8;
      const idealSize = Math.floor(availableH / (items.length * 1.8));
      setLocalFontSize(Math.max(10, Math.min(32, idealSize)));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [items.length, uniformFontSize]);

  const autoFontSize = uniformFontSize ?? localFontSize;

  const fontStyle = autoFontSize ? {
    "--auto-item-font": `${autoFontSize}px`,
    "--auto-title-font": `${Math.min(autoFontSize * 1.3, 36)}px`,
    "--auto-desc-font": `${Math.max(autoFontSize * 0.75, 9)}px`,
  } as React.CSSProperties : {};

  return (
    <section className={`board-category ${autoFontSize ? "board-category-autofont" : ""}`} ref={containerRef} style={fontStyle}>
      <h2 className="board-category-title">{title}</h2>
      <div className="board-items">
        {items.map((item) => {
          // Featured item with image → hero card (newksFresh only)
          if (freshTheme && item.featured && item.effectiveImageUrl) {
            return (
              <div key={item.source.id} className={`board-item board-item-hero ${item.outOfStock ? "board-item-86d" : ""}`}>
                <div className="board-item-hero-img-wrap">
                  <img
                    src={item.effectiveImageUrl}
                    alt={item.effectiveName}
                    className="board-item-hero-img"
                    loading="lazy"
                  />
                  {item.outOfStock && (
                    <div className="board-item-86d-badge">86'd</div>
                  )}
                  {!item.outOfStock && item.effectivePriceCents > 0 && (
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
            <div key={item.source.id} className={`board-item ${item.outOfStock ? "board-item-86d" : ""}`}>
              <div className="board-item-main">
                <div className="board-item-text">
                  <div className="board-item-name">
                    {item.effectiveName}
                    {item.outOfStock && (
                      <span className="board-item-86d-pill">86'd</span>
                    )}
                    {!item.outOfStock && item.featured && (
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
