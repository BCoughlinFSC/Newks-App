import type { BoardCategory, CategoryConfig, GridBlock, GridPlacement } from "../types";

/**
 * Build an occupancy matrix from existing grid blocks.
 * Returns a 2D boolean array [row][col] (0-based internally).
 */
function buildOccupancy(
  blocks: GridBlock[],
  cols: number,
  rows: number,
): boolean[][] {
  const matrix: boolean[][] = Array.from({ length: rows }, () =>
    Array(cols).fill(false),
  );
  for (const block of blocks) {
    const { col, row, colSpan, rowSpan } = block.placement;
    for (let r = row - 1; r < Math.min(row - 1 + rowSpan, rows); r++) {
      for (let c = col - 1; c < Math.min(col - 1 + colSpan, cols); c++) {
        matrix[r][c] = true;
      }
    }
  }
  return matrix;
}

/**
 * Check if a placement fits without overlap.
 */
export function placementFits(
  matrix: boolean[][],
  placement: GridPlacement,
  cols: number,
  rows: number,
  excludeBlockId?: string,
  blocks?: GridBlock[],
): boolean {
  // If excludeBlockId given, rebuild matrix without that block
  let m = matrix;
  if (excludeBlockId && blocks) {
    const filtered = blocks.filter((b) => b.id !== excludeBlockId);
    m = buildOccupancy(filtered, cols, rows);
  }

  const { col, row, colSpan, rowSpan } = placement;
  if (col < 1 || row < 1) return false;
  if (col + colSpan - 1 > cols || row + rowSpan - 1 > rows) return false;

  for (let r = row - 1; r < row - 1 + rowSpan; r++) {
    for (let c = col - 1; c < col - 1 + colSpan; c++) {
      if (m[r]?.[c]) return false;
    }
  }
  return true;
}

/**
 * Find the first free cell that fits a given span, scanning left-to-right, top-to-bottom.
 */
function findFreeCell(
  matrix: boolean[][],
  cols: number,
  rows: number,
  wantColSpan: number,
  wantRowSpan: number,
): GridPlacement | null {
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      const placement: GridPlacement = {
        col: c + 1,
        row: r + 1,
        colSpan: wantColSpan,
        rowSpan: wantRowSpan,
      };
      if (placementFits(matrix, placement, cols, rows)) {
        return placement;
      }
    }
  }
  return null;
}

/**
 * Auto-layout categories into grid blocks.
 * Preserves existing blocks and places any unplaced categories.
 * Expands rows if needed to fit everything.
 */
export function autoLayoutGrid(
  categories: BoardCategory[],
  gridColumns: number,
  gridRows: number,
  existingBlocks: GridBlock[] = [],
  includePromo?: boolean,
  categoryConfigs?: Record<string, CategoryConfig>,
): { blocks: GridBlock[]; rows: number } {
  const cols = gridColumns;
  let rows = gridRows;

  // Start with existing blocks
  const blocks: GridBlock[] = [...existingBlocks];

  // Place promo block if requested and not already present
  if (includePromo && !blocks.some((b) => b.type === "promo")) {
    const promoColSpan = Math.min(2, cols);
    const promoPlacement: GridPlacement = {
      col: cols - promoColSpan + 1,
      row: 1,
      colSpan: promoColSpan,
      rowSpan: rows,
    };
    blocks.push({
      id: "promo-1",
      type: "promo",
      placement: promoPlacement,
    });
  }

  // Find categories that already have a block (including split parts)
  const placedCatIds = new Set(
    blocks.filter((b) => b.type === "category" && b.categoryId).map((b) => b.categoryId!),
  );

  // Categories that need placement
  const unplaced = categories.filter((c) => !placedCatIds.has(c.id));

  for (const cat of unplaced) {
    // Determine how many parts this category needs
    const splitPoints = categoryConfigs?.[cat.id]?.splitPoints;
    const partCount = splitPoints && splitPoints.length > 0 ? splitPoints.length + 1 : 1;

    for (let part = 0; part < partCount; part++) {
      let matrix = buildOccupancy(blocks, cols, rows);

      // Try colSpan: 2 first (nicer default), then 1
      let placement = findFreeCell(matrix, cols, rows, 2, 1);
      if (!placement) {
        placement = findFreeCell(matrix, cols, rows, 1, 1);
      }

      if (!placement) {
        // Expand grid rows to fit
        rows += 1;
        matrix = buildOccupancy(blocks, cols, rows);
        placement = findFreeCell(matrix, cols, rows, 2, 1);
        if (!placement) {
          placement = findFreeCell(matrix, cols, rows, 1, 1);
        }
      }

      if (placement) {
        blocks.push({
          id: part === 0 ? `cat-${cat.id}` : `cat-${cat.id}-part${part}`,
          type: "category",
          categoryId: cat.id,
          categoryPart: partCount > 1 ? part : undefined,
          placement,
        });
      }
    }
  }

  return { blocks, rows };
}

export { buildOccupancy };
