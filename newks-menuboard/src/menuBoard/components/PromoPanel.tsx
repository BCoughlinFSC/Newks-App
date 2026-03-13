import React, { useEffect, useState } from "react";
import type { FeatureSlot } from "../types";

type Props = {
  featureSlots: FeatureSlot[];
  freshTheme?: boolean;
  /** When true, fills its parent container (for use inside a grid block) */
  fillContainer?: boolean;
};

export function PromoPanel({ featureSlots, freshTheme = false, fillContainer = false }: Props) {
  if (!featureSlots.length) return null;
  const primary = featureSlots[0];

  // Cycling through multiple slots (if provided)
  const [activeIdx, setActiveIdx] = useState(0);
  useEffect(() => {
    if (featureSlots.length <= 1) return;
    const id = window.setInterval(() => {
      setActiveIdx((i) => (i + 1) % featureSlots.length);
    }, 8000);
    return () => window.clearInterval(id);
  }, [featureSlots.length]);

  const slot = featureSlots[activeIdx] ?? primary;

  // ── newksFresh: full-bleed hero image with gradient text overlay ──
  if (freshTheme) {
    return (
      <aside className="board-promo" style={{ display: "flex", flexDirection: "column", ...(fillContainer ? { width: "100%", height: "100%", overflow: "hidden" } : {}) }}>
        {slot.videoUrl ? (
          <div className="board-promo-image-wrap" style={{ position: "relative", ...(fillContainer ? { flex: 1, minHeight: 0 } : { aspectRatio: "9 / 16", maxHeight: "70vh" }) }}>
            <video
              src={slot.videoUrl}
              autoPlay
              muted
              loop
              playsInline
              className="board-promo-image"
              style={{ width: "100%", height: "100%", objectFit: "cover" }}
            />
            {(slot.title || slot.subtitle) && (
              <div className="board-promo-overlay">
                {slot.title && <div className="board-promo-title">{slot.title}</div>}
                {slot.subtitle && <div className="board-promo-subtitle">{slot.subtitle}</div>}
              </div>
            )}
          </div>
        ) : slot.imageUrl ? (
          <div className="board-promo-image-wrap" style={{ position: "relative", ...(fillContainer ? { flex: 1, minHeight: 0 } : {}) }}>
            <img
              src={slot.imageUrl}
              alt={slot.title ?? "Promotion"}
              className="board-promo-image"
              style={{ width: "100%", height: fillContainer ? "100%" : "auto", objectFit: "cover" }}
            />
            {(slot.title || slot.subtitle) && (
              <div className="board-promo-overlay">
                {slot.title && <div className="board-promo-title">{slot.title}</div>}
                {slot.subtitle && <div className="board-promo-subtitle">{slot.subtitle}</div>}
              </div>
            )}
          </div>
        ) : (
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              justifyContent: "center",
              background: "linear-gradient(135deg, #4a7c4e 0%, #3a6340 100%)",
              padding: 24,
              gap: 10,
              borderRadius: 14,
              minHeight: 180,
            }}
          >
            {slot.title && (
              <div className="board-promo-title" style={{ textAlign: "center" }}>
                {slot.title}
              </div>
            )}
            {slot.subtitle && (
              <div className="board-promo-subtitle" style={{ textAlign: "center" }}>
                {slot.subtitle}
              </div>
            )}
          </div>
        )}

        {/* Cycling dots for multiple slots */}
        {featureSlots.length > 1 && (
          <div style={{ display: "flex", justifyContent: "center", gap: 6, padding: "8px 0 4px" }}>
            {featureSlots.map((_, i) => (
              <div key={i} style={{
                width: 8, height: 8, borderRadius: "50%",
                background: i === activeIdx ? "#4a7c4e" : "#d1d5db",
                transition: "background 0.3s",
              }} />
            ))}
          </div>
        )}
      </aside>
    );
  }

  // ── default dark/light theme promo ──
  return (
    <aside className="board-promo">
      {primary.imageUrl && (
        <div className="board-promo-image-wrap">
          <img
            src={primary.imageUrl}
            alt={primary.title ?? "Promotion"}
            className="board-promo-image"
          />
        </div>
      )}
      {primary.videoUrl && !primary.imageUrl && (
        <div className="board-promo-image-wrap">
          <video
            src={primary.videoUrl}
            autoPlay
            muted
            loop
            playsInline
            className="board-promo-image"
          />
        </div>
      )}
      <div className="board-promo-text">
        {primary.title && <div className="board-promo-title">{primary.title}</div>}
        {primary.subtitle && <div className="board-promo-subtitle">{primary.subtitle}</div>}
      </div>
    </aside>
  );
}
