import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { BoardCategory, CategoryConfig, FeatureSlot, GridBlock, GridPlacement } from "../types";
import { buildOccupancy, placementFits } from "../utils/autoLayoutGrid";
import { CategoryPanel } from "./CategoryPanel";
import { PromoPanel } from "./PromoPanel";

type Props = {
  blocks: GridBlock[];
  gridColumns: number;
  gridRows: number;
  categories: BoardCategory[];
  categoryConfigs?: Record<string, CategoryConfig>;
  featureSlots: FeatureSlot[];
  freshTheme: boolean;
  fontSize: "small" | "medium" | "large";
  editorMode?: boolean;
  previewScale?: number;
  onBlockMove?: (blockId: string, newPlacement: GridPlacement) => void;
  onBlockResize?: (blockId: string, newPlacement: GridPlacement) => void;
  onEmptyCellClick?: (col: number, row: number) => void;
  onBlockSelect?: (blockId: string | null) => void;
  onBlockRemove?: (blockId: string) => void;
  selectedBlockId?: string | null;
};

type ResizeState = {
  blockId: string;
  startX: number;
  startY: number;
  origPlacement: GridPlacement;
  corner: "br" | "bl" | "tr" | "tl";
  cellW: number;
  cellH: number;
  proposed: GridPlacement | null;
};

export function GridBoardRenderer({
  blocks,
  gridColumns,
  gridRows,
  categories,
  categoryConfigs = {},
  featureSlots,
  freshTheme,
  fontSize,
  editorMode = false,
  previewScale = 1,
  onBlockMove,
  onBlockResize,
  onEmptyCellClick,
  onBlockSelect,
  onBlockRemove,
  selectedBlockId,
}: Props) {
  const gridRef = useRef<HTMLDivElement | null>(null);
  const [dragBlockId, setDragBlockId] = useState<string | null>(null);
  const [dragOverCell, setDragOverCell] = useState<{ col: number; row: number } | null>(null);
  const [resizeState, setResizeState] = useState<ResizeState | null>(null);

  const catMap = useMemo(() => {
    const m = new Map<string, BoardCategory>();
    categories.forEach((c) => m.set(c.id, c));
    return m;
  }, [categories]);

  const occupancy = useMemo(
    () => buildOccupancy(blocks, gridColumns, gridRows),
    [blocks, gridColumns, gridRows],
  );

  // ── Drag-and-drop (move) ──
  const handleDragStart = useCallback((e: React.DragEvent, blockId: string) => {
    e.dataTransfer.effectAllowed = "move";
    setDragBlockId(blockId);
  }, []);

  const handleCellDragOver = useCallback((e: React.DragEvent, col: number, row: number) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    setDragOverCell({ col, row });
  }, []);

  const handleCellDrop = useCallback(
    (e: React.DragEvent, col: number, row: number) => {
      e.preventDefault();
      setDragOverCell(null);
      if (!dragBlockId || !onBlockMove) {
        setDragBlockId(null);
        return;
      }
      const block = blocks.find((b) => b.id === dragBlockId);
      if (!block) { setDragBlockId(null); return; }
      const newPlacement: GridPlacement = {
        col,
        row,
        colSpan: block.placement.colSpan,
        rowSpan: block.placement.rowSpan,
      };
      if (placementFits(occupancy, newPlacement, gridColumns, gridRows, dragBlockId, blocks)) {
        onBlockMove(dragBlockId, newPlacement);
      }
      setDragBlockId(null);
    },
    [dragBlockId, blocks, onBlockMove, occupancy, gridColumns, gridRows],
  );

  const handleDragEnd = useCallback(() => {
    setDragBlockId(null);
    setDragOverCell(null);
  }, []);

  // ── Resize (mouse events) ──
  const handleResizeStart = useCallback(
    (e: React.MouseEvent, blockId: string, corner: ResizeState["corner"]) => {
      e.preventDefault();
      e.stopPropagation();
      const grid = gridRef.current;
      if (!grid) return;
      const rect = grid.getBoundingClientRect();
      const cellW = rect.width / gridColumns;
      const cellH = rect.height / gridRows;
      const block = blocks.find((b) => b.id === blockId);
      if (!block) return;
      setResizeState({
        blockId,
        startX: e.clientX,
        startY: e.clientY,
        origPlacement: { ...block.placement },
        corner,
        cellW,
        cellH,
        proposed: null,
      });
    },
    [blocks, gridColumns, gridRows],
  );

  useEffect(() => {
    if (!resizeState) return;
    const scale = previewScale || 1;

    function onMouseMove(e: MouseEvent) {
      if (!resizeState) return;
      const { startX, startY, origPlacement, corner, cellW, cellH } = resizeState;
      const dx = (e.clientX - startX) / scale;
      const dy = (e.clientY - startY) / scale;
      const dCols = Math.round(dx / cellW);
      const dRows = Math.round(dy / cellH);

      let proposed: GridPlacement;
      if (corner === "br") {
        proposed = {
          col: origPlacement.col,
          row: origPlacement.row,
          colSpan: Math.max(1, origPlacement.colSpan + dCols),
          rowSpan: Math.max(1, origPlacement.rowSpan + dRows),
        };
      } else if (corner === "bl") {
        const newCol = origPlacement.col + dCols;
        proposed = {
          col: Math.max(1, newCol),
          row: origPlacement.row,
          colSpan: Math.max(1, origPlacement.colSpan - dCols),
          rowSpan: Math.max(1, origPlacement.rowSpan + dRows),
        };
      } else if (corner === "tr") {
        const newRow = origPlacement.row + dRows;
        proposed = {
          col: origPlacement.col,
          row: Math.max(1, newRow),
          colSpan: Math.max(1, origPlacement.colSpan + dCols),
          rowSpan: Math.max(1, origPlacement.rowSpan - dRows),
        };
      } else {
        // tl
        const newCol = origPlacement.col + dCols;
        const newRow = origPlacement.row + dRows;
        proposed = {
          col: Math.max(1, newCol),
          row: Math.max(1, newRow),
          colSpan: Math.max(1, origPlacement.colSpan - dCols),
          rowSpan: Math.max(1, origPlacement.rowSpan - dRows),
        };
      }

      // Clamp to grid bounds
      if (proposed.col + proposed.colSpan - 1 > gridColumns) {
        proposed.colSpan = gridColumns - proposed.col + 1;
      }
      if (proposed.row + proposed.rowSpan - 1 > gridRows) {
        proposed.rowSpan = gridRows - proposed.row + 1;
      }

      setResizeState((prev) => (prev ? { ...prev, proposed } : null));
    }

    function onMouseUp() {
      if (!resizeState) return;
      const { blockId, proposed } = resizeState;
      if (proposed && onBlockResize) {
        if (placementFits(occupancy, proposed, gridColumns, gridRows, blockId, blocks)) {
          onBlockResize(blockId, proposed);
        }
      }
      setResizeState(null);
    }

    window.addEventListener("mousemove", onMouseMove);
    window.addEventListener("mouseup", onMouseUp);
    return () => {
      window.removeEventListener("mousemove", onMouseMove);
      window.removeEventListener("mouseup", onMouseUp);
    };
  }, [resizeState, previewScale, gridColumns, gridRows, onBlockResize, occupancy, blocks]);

  // ── Render blocks ──
  function renderBlockContent(block: GridBlock) {
    if (block.type === "category") {
      const cat = block.categoryId ? catMap.get(block.categoryId) : null;
      if (!cat) {
        return <div className="board-grid-missing">Category not found</div>;
      }
      const splitPoints = categoryConfigs[cat.id]?.splitPoints;
      const part = block.categoryPart ?? 0;
      if (splitPoints && splitPoints.length > 0) {
        const start = part === 0 ? 0 : splitPoints[part - 1];
        const end = part < splitPoints.length ? splitPoints[part] : cat.items.length;
        const slicedItems = cat.items.slice(start, end);
        const title = part > 0 ? `${cat.name} (cont.)` : cat.name;
        return <CategoryPanel category={cat} freshTheme={freshTheme} itemsOverride={slicedItems} titleOverride={title} />;
      }
      return <CategoryPanel category={cat} freshTheme={freshTheme} />;
    }
    if (block.type === "image" && block.imageUrl) {
      return (
        <img
          src={block.imageUrl}
          alt="Media"
          style={{ width: "100%", height: "100%", objectFit: "cover", display: "block", borderRadius: 14 }}
        />
      );
    }
    if (block.type === "video" && block.videoUrl) {
      return (
        <video
          src={block.videoUrl}
          autoPlay
          muted
          loop
          playsInline
          style={{ width: "100%", height: "100%", objectFit: "cover", display: "block", borderRadius: 14 }}
        />
      );
    }
    if (block.type === "promo") {
      return (
        <PromoPanel
          featureSlots={featureSlots}
          freshTheme={freshTheme}
          fillContainer
        />
      );
    }
    return <div className="board-grid-missing">Empty block</div>;
  }

  // ── Build empty cells for editor ──
  const emptyCells: { col: number; row: number }[] = [];
  if (editorMode) {
    for (let r = 0; r < gridRows; r++) {
      for (let c = 0; c < gridColumns; c++) {
        if (!occupancy[r]?.[c]) {
          emptyCells.push({ col: c + 1, row: r + 1 });
        }
      }
    }
  }

  const isResizing = resizeState !== null;

  return (
    <div
      ref={gridRef}
      className={`board-grid grid-font-${fontSize} ${editorMode ? "board-grid-editor" : ""}`}
      style={{
        gridTemplateColumns: `repeat(${gridColumns}, 1fr)`,
        gridTemplateRows: `repeat(${gridRows}, 1fr)`,
      }}
      onClick={() => onBlockSelect?.(null)}
    >
      {/* Empty cell overlays (editor) */}
      {emptyCells.map(({ col, row }) => (
        <div
          key={`empty-${col}-${row}`}
          className={`board-grid-cell-overlay ${dragOverCell?.col === col && dragOverCell?.row === row ? "board-grid-cell-highlight" : ""}`}
          style={{ gridColumn: col, gridRow: row }}
          onDragOver={(e) => handleCellDragOver(e, col, row)}
          onDrop={(e) => handleCellDrop(e, col, row)}
          onClick={(e) => {
            e.stopPropagation();
            onEmptyCellClick?.(col, row);
          }}
        >
          <span className="board-grid-cell-plus">+</span>
        </div>
      ))}

      {/* Placed blocks */}
      {blocks.map((block) => {
        const p = resizeState?.blockId === block.id && resizeState.proposed
          ? resizeState.proposed
          : block.placement;
        const isSelected = selectedBlockId === block.id;
        const isDragging = dragBlockId === block.id;

        return (
          <div
            key={block.id}
            className={`board-grid-block ${isSelected ? "board-grid-block-selected" : ""} ${isDragging ? "board-grid-block-dragging" : ""}`}
            style={{
              gridColumn: `${p.col} / span ${p.colSpan}`,
              gridRow: `${p.row} / span ${p.rowSpan}`,
            }}
            draggable={editorMode && !isResizing}
            onDragStart={(e) => editorMode && handleDragStart(e, block.id)}
            onDragEnd={handleDragEnd}
            onClick={(e) => {
              if (editorMode) {
                e.stopPropagation();
                onBlockSelect?.(block.id);
              }
            }}
          >
            {renderBlockContent(block)}

            {/* Resize handles (editor + selected) */}
            {editorMode && isSelected && (
              <>
                <div
                  className="board-grid-handle board-grid-handle-br"
                  onMouseDown={(e) => handleResizeStart(e, block.id, "br")}
                />
                <div
                  className="board-grid-handle board-grid-handle-tl"
                  onMouseDown={(e) => handleResizeStart(e, block.id, "tl")}
                />
                <div
                  className="board-grid-handle board-grid-handle-tr"
                  onMouseDown={(e) => handleResizeStart(e, block.id, "tr")}
                />
                <div
                  className="board-grid-handle board-grid-handle-bl"
                  onMouseDown={(e) => handleResizeStart(e, block.id, "bl")}
                />
                {onBlockRemove && (
                  <button
                    type="button"
                    className="board-grid-remove-btn"
                    onClick={(e) => {
                      e.stopPropagation();
                      onBlockRemove(block.id);
                    }}
                  >
                    ×
                  </button>
                )}
              </>
            )}
          </div>
        );
      })}

      {/* Resize preview overlay */}
      {resizeState?.proposed && (
        <div
          className="board-grid-resize-preview"
          style={{
            gridColumn: `${resizeState.proposed.col} / span ${resizeState.proposed.colSpan}`,
            gridRow: `${resizeState.proposed.row} / span ${resizeState.proposed.rowSpan}`,
          }}
        />
      )}
    </div>
  );
}
