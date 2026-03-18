import React, { useEffect, useMemo, useRef, useState } from "react";
import type {
  BoardConfig,
  BoardViewModel,
  BrandingConfig,
  CategoryConfig,
  FeatureSlot,
  GridBlock,
  GridPlacement,
  ItemConfig,
  MarketingConfig,
  ScreenDefinition,
} from "../types";
import { mergeMenuWithConfig } from "../utils/mergeMenuWithConfig";
import { autoLayoutGrid, buildOccupancy, placementFits } from "../utils/autoLayoutGrid";
import { setAdminToken } from "../utils/storage";
import { BoardHeader } from "./BoardHeader";
import { GridBoardRenderer } from "./GridBoardRenderer";

type Props = {
  boardConfig: BoardConfig;
  viewModel: BoardViewModel;
  onChange: (next: BoardConfig) => void;
  onClose: () => void;
  onOpenPreview: () => void;
  locationId?: string | null;
};

type SettingsTab = "appearance" | "content" | "menu" | "screens";
type UploadStatus = { loading: boolean; error: string | null; success: boolean };

export function SettingsPage({ boardConfig, viewModel, onChange, onClose, locationId }: Props) {
  const [draft, setDraft] = useState<BoardConfig>(boardConfig);
  const [activeTab, setActiveTab] = useState<SettingsTab>("appearance");
  const [dragIndex, setDragIndex] = useState<number | null>(null);
  const [categoryFilter, setCategoryFilter] = useState("");
  const [previewScreenIdx, setPreviewScreenIdx] = useState(0);
  const [uploadStatus, setUploadStatus] = useState<UploadStatus>({ loading: false, error: null, success: false });
  const [selectedBlockId, setSelectedBlockId] = useState<string | null>(null);
  const [showAuthPrompt, setShowAuthPrompt] = useState(false);
  const [authInput, setAuthInput] = useState("");
  const [authError, setAuthError] = useState<string | null>(null);
  const [menuExpandedCatIds, setMenuExpandedCatIds] = useState<Set<string>>(new Set());

  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const uploadCallbackRef = useRef<((url: string) => void) | null>(null);
  const previewContainerRef = useRef<HTMLDivElement | null>(null);
  const [previewWidth, setPreviewWidth] = useState(800);
  const [previewHeight, setPreviewHeight] = useState(600);

  // Escape to close
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.preventDefault(); onClose(); }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);

  // Observe preview container size for responsive scaling
  useEffect(() => {
    const el = previewContainerRef.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      for (const entry of entries) {
        setPreviewWidth(entry.contentRect.width);
        setPreviewHeight(entry.contentRect.height);
      }
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // Clear upload success flash
  useEffect(() => {
    if (!uploadStatus.success) return;
    const id = setTimeout(() => setUploadStatus((s) => ({ ...s, success: false })), 2000);
    return () => clearTimeout(id);
  }, [uploadStatus.success]);

  const branding: BrandingConfig = draft.branding ?? {};
  const marketing: MarketingConfig = draft.marketing ?? { promoImageUrls: [] };
  const screens: ScreenDefinition[] =
    Array.isArray(draft.screens) && draft.screens.length > 0
      ? draft.screens
      : [{ id: "1", label: "Screen 1", width: 1920, height: 1080, categoryIds: [], gridColumns: 6, gridRows: 4, gridBlocks: [] }];
  const categories = viewModel.categories.filter((c) => !c.hidden);

  // ── Upload logic ──
  // Uses base64 JSON endpoint to bypass Cloudways nginx blocking multipart uploads
  async function handleUploadChange(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    const cb = uploadCallbackRef.current;
    uploadCallbackRef.current = null;
    if (!cb) return;
    setUploadStatus({ loading: true, error: null, success: false });
    try {
      const buf = await file.arrayBuffer();
      const base64 = btoa(
        new Uint8Array(buf).reduce((s, b) => s + String.fromCharCode(b), ""),
      );
      const headers: Record<string, string> = { "Content-Type": "application/json" };
      const token = window.sessionStorage.getItem("adminToken");
      if (token) headers["Authorization"] = `Bearer ${token}`;
      const resp = await fetch("/api/uploads/menu-board-b64", {
        method: "POST",
        headers,
        body: JSON.stringify({ filename: file.name, data: base64 }),
      });
      if (resp.status === 401) {
        setUploadStatus({ loading: false, error: null, success: false });
        setShowAuthPrompt(true);
        return;
      }
      if (!resp.ok) {
        const text = await resp.text();
        setUploadStatus({ loading: false, error: `Upload failed (${resp.status}): ${text}`, success: false });
        return;
      }
      const data = (await resp.json()) as { url?: string };
      if (data.url) {
        cb(data.url);
        setUploadStatus({ loading: false, error: null, success: true });
      } else {
        setUploadStatus({ loading: false, error: "Upload succeeded but no URL returned", success: false });
      }
    } catch (err) {
      setUploadStatus({ loading: false, error: `Upload error: ${String(err)}`, success: false });
    }
  }

  function triggerUpload(setter: (url: string) => void) {
    uploadCallbackRef.current = setter;
    fileInputRef.current?.click();
  }

  // ── Config updaters ──
  function update(updater: (prev: BoardConfig) => BoardConfig) {
    setDraft((prev) => {
      const next = updater(prev);
      onChange(next);
      return next;
    });
  }

  function setBranding(updates: Partial<BrandingConfig>) {
    update((prev) => ({ ...prev, branding: { ...(prev.branding ?? {}), ...updates } }));
  }

  function setMarketing(updates: Partial<MarketingConfig>) {
    update((prev) => ({
      ...prev,
      marketing: { ...(prev.marketing ?? { promoImageUrls: [] }), ...updates },
    }));
  }

  function setScreens(next: ScreenDefinition[]) {
    update((prev) => ({ ...prev, screens: next }));
  }

  function updateScreen(index: number, updates: Partial<ScreenDefinition>) {
    setScreens(screens.map((s, i) => (i === index ? { ...s, ...updates } : s)));
  }

  function addScreen() {
    const id = String(screens.length + 1);
    setScreens([
      ...screens,
      { id, label: `Screen ${id}`, width: 1920, height: 1080, categoryIds: [], gridColumns: 6, gridRows: 4, gridBlocks: [] },
    ]);
  }

  function removeScreen(index: number) {
    if (screens.length <= 1) return;
    setScreens(screens.filter((_, i) => i !== index));
    if (previewScreenIdx >= screens.length - 1) setPreviewScreenIdx(0);
  }

  function toggleCategoryOnScreen(screenIndex: number, categoryId: string) {
    const screen = screens[screenIndex];
    if (!screen) return;
    const useAll = screen.useAllCategories !== false;
    let next: string[];
    if (useAll && !screen.categoryIds.length) {
      next = categories.filter((c) => c.id !== categoryId).map((c) => c.id);
    } else {
      const isIncluded = screen.categoryIds.includes(categoryId);
      next = isIncluded
        ? screen.categoryIds.filter((id) => id !== categoryId)
        : [...screen.categoryIds, categoryId];
    }
    updateScreen(screenIndex, { categoryIds: next, useAllCategories: false });
  }

  function updateFeatureSlot(index: number, updates: Partial<FeatureSlot>) {
    const slots = [...draft.featureSlots];
    slots[index] = { ...slots[index], ...updates };
    update((prev) => ({ ...prev, featureSlots: slots }));
  }

  function addFeatureSlot() {
    update((prev) => ({
      ...prev,
      featureSlots: [...prev.featureSlots, { id: String(Date.now()), title: "", subtitle: "" }],
    }));
  }

  function removeFeatureSlot(index: number) {
    update((prev) => ({ ...prev, featureSlots: prev.featureSlots.filter((_, i) => i !== index) }));
  }

  function updateCategoryConfig(categoryId: string, updates: Partial<CategoryConfig>) {
    update((prev) => ({
      ...prev,
      categoryConfigs: {
        ...prev.categoryConfigs,
        [categoryId]: {
          ...(prev.categoryConfigs[categoryId] ?? { categoryId }),
          categoryId,
          ...updates,
        },
      },
    }));
  }

  function updateItemConfig(itemId: string, updates: Partial<ItemConfig>) {
    update((prev) => ({
      ...prev,
      itemConfigs: {
        ...prev.itemConfigs,
        [itemId]: {
          ...(prev.itemConfigs[itemId] ?? { itemId }),
          itemId,
          ...updates,
        },
      },
    }));
  }

  function toggleSplitPoint(categoryId: string, itemIndex: number) {
    const catCfg = draft.categoryConfigs[categoryId];
    const current = catCfg?.splitPoints ?? [];
    const next = current.includes(itemIndex)
      ? current.filter((i) => i !== itemIndex)
      : [...current, itemIndex].sort((a, b) => a - b);
    updateCategoryConfig(categoryId, { splitPoints: next.length > 0 ? next : undefined });
  }

  function handleDragStart(index: number) { setDragIndex(index); }
  function handleDragOver(event: React.DragEvent<HTMLDivElement>, index: number) {
    event.preventDefault();
    if (dragIndex === null || dragIndex === index) return;
    const reordered = [...screens];
    const [removed] = reordered.splice(dragIndex, 1);
    reordered.splice(index, 0, removed);
    setScreens(reordered);
    setDragIndex(index);
  }
  function handleDragEnd() { setDragIndex(null); }



  // ── Grid layout helpers ──
  function resetGridLayout(screenIndex: number) {
    const screen = screens[screenIndex];
    if (!screen) return;
    const screenCats = screen.useAllCategories !== false
      ? categories
      : categories.filter((c) => screen.categoryIds.includes(c.id));
    const cols = screen.gridColumns ?? 6;
    const rows = screen.gridRows ?? 4;
    const hasPromo = draft.featureSlots.length > 0;
    const { blocks, rows: newRows } = autoLayoutGrid(screenCats, cols, rows, [], hasPromo, draft.categoryConfigs);
    updateScreen(screenIndex, { gridBlocks: blocks, gridRows: newRows });
  }

  function addPromoBlock(screenIndex: number) {
    const screen = screens[screenIndex];
    if (!screen) return;
    const cols = screen.gridColumns ?? 6;
    const rows = screen.gridRows ?? 4;
    const existing = screen.gridBlocks ?? [];
    const occ = buildOccupancy(existing, cols, rows);
    // Try to place on right side, full height
    const promoColSpan = Math.min(2, cols);
    let placed = false;
    for (let c = cols - promoColSpan; c >= 0 && !placed; c--) {
      let fits = true;
      for (let r = 0; r < rows && fits; r++) {
        for (let cc = c; cc < c + promoColSpan && fits; cc++) {
          if (occ[r]?.[cc]) fits = false;
        }
      }
      if (fits) {
        updateScreen(screenIndex, {
          gridBlocks: [
            ...existing,
            { id: `promo-${Date.now()}`, type: "promo" as const, placement: { col: c + 1, row: 1, colSpan: promoColSpan, rowSpan: rows } },
          ],
        });
        placed = true;
      }
    }
    if (!placed) {
      // Add a column for promo
      updateScreen(screenIndex, {
        gridColumns: cols + promoColSpan,
        gridBlocks: [
          ...existing,
          { id: `promo-${Date.now()}`, type: "promo" as const, placement: { col: cols + 1, row: 1, colSpan: promoColSpan, rowSpan: rows } },
        ],
      });
    }
  }

  function handleGridBlockMove(blockId: string, newPlacement: GridPlacement) {
    const screen = screens[previewScreenIdx];
    if (!screen?.gridBlocks) return;
    const newBlocks = screen.gridBlocks.map((b) =>
      b.id === blockId ? { ...b, placement: newPlacement } : b,
    );
    updateScreen(previewScreenIdx, { gridBlocks: newBlocks });
  }

  function handleGridBlockResize(blockId: string, newPlacement: GridPlacement) {
    const screen = screens[previewScreenIdx];
    if (!screen?.gridBlocks) return;
    const newBlocks = screen.gridBlocks.map((b) =>
      b.id === blockId ? { ...b, placement: newPlacement } : b,
    );
    updateScreen(previewScreenIdx, { gridBlocks: newBlocks });
  }

  function handleGridBlockRemove(blockId: string) {
    const screen = screens[previewScreenIdx];
    if (!screen?.gridBlocks) return;
    updateScreen(previewScreenIdx, {
      gridBlocks: screen.gridBlocks.filter((b) => b.id !== blockId),
    });
    setSelectedBlockId(null);
  }

  function addGridImageBlock(screenIndex: number) {
    triggerUpload((url) => {
      const screen = screens[screenIndex];
      if (!screen) return;
      const cols = screen.gridColumns ?? 6;
      const rows = screen.gridRows ?? 4;
      const existing = screen.gridBlocks ?? [];
      const occ = buildOccupancy(existing, cols, rows);
      let placed = false;
      for (let r = 0; r < rows && !placed; r++) {
        for (let c = 0; c < cols && !placed; c++) {
          if (!occ[r]?.[c]) {
            updateScreen(screenIndex, {
              gridBlocks: [
                ...existing,
                {
                  id: `img-${Date.now()}`,
                  type: "image" as const,
                  imageUrl: url,
                  placement: { col: c + 1, row: r + 1, colSpan: 1, rowSpan: 1 },
                },
              ],
            });
            placed = true;
          }
        }
      }
      if (!placed) {
        // Add a new row
        updateScreen(screenIndex, {
          gridRows: rows + 1,
          gridBlocks: [
            ...existing,
            {
              id: `img-${Date.now()}`,
              type: "image" as const,
              imageUrl: url,
              placement: { col: 1, row: rows + 1, colSpan: 1, rowSpan: 1 },
            },
          ],
        });
      }
    });
  }

  function addGridVideoBlock(screenIndex: number) {
    triggerUpload((url) => {
      const screen = screens[screenIndex];
      if (!screen) return;
      const cols = screen.gridColumns ?? 6;
      const rows = screen.gridRows ?? 4;
      const existing = screen.gridBlocks ?? [];
      const occ = buildOccupancy(existing, cols, rows);
      // Place as 1x1 by default (user can resize with drag handles)
      let placed = false;
      for (let r = 0; r < rows && !placed; r++) {
        for (let c = 0; c < cols && !placed; c++) {
          const p = { col: c + 1, row: r + 1, colSpan: 1, rowSpan: 1 };
          if (placementFits(occ, p, cols, rows)) {
            updateScreen(screenIndex, {
              gridBlocks: [
                ...existing,
                { id: `vid-${Date.now()}`, type: "video" as const, videoUrl: url, placement: p },
              ],
            });
            placed = true;
          }
        }
      }
      if (!placed) {
        updateScreen(screenIndex, {
          gridRows: rows + 1,
          gridBlocks: [
            ...existing,
            {
              id: `vid-${Date.now()}`,
              type: "video" as const,
              videoUrl: url,
              placement: { col: 1, row: rows + 1, colSpan: 1, rowSpan: 1 },
            },
          ],
        });
      }
    });
  }

  // ── Live preview data ──
  const previewScreen = screens[previewScreenIdx] ?? screens[0];
  // Scale to fit both width and height of the preview container (account for header ~40px)
  const previewFrameH = Math.max(previewHeight - 40, 200);
  const previewScale = previewScreen
    ? Math.min(previewWidth / previewScreen.width, previewFrameH / previewScreen.height, 1)
    : 0.5;
  const previewVm = useMemo(
    () => (viewModel?.menu ? mergeMenuWithConfig(viewModel.menu, draft) : null),
    [viewModel?.menu, draft],
  );

  const previewCategories = useMemo(() => {
    const all = previewVm?.categories ?? [];
    if (!previewScreen) return all;
    if (previewScreen.useAllCategories !== false) return all;
    if (!previewScreen.categoryIds.length) return [];
    const idSet = new Set(previewScreen.categoryIds);
    return all.filter((c) => idSet.has(c.id));
  }, [previewVm?.categories, previewScreen]);

  // Auto-populate grid blocks for preview screen if empty
  useEffect(() => {
    if (!previewScreen || (previewScreen.gridBlocks && previewScreen.gridBlocks.length > 0)) return;
    const screenCats = previewScreen.useAllCategories !== false
      ? categories
      : categories.filter((c) => previewScreen.categoryIds.includes(c.id));
    if (!screenCats.length) return;
    const cols = previewScreen.gridColumns ?? 6;
    const rows = previewScreen.gridRows ?? 4;
    const hasPromo = draft.featureSlots.length > 0;
    const { blocks, rows: newRows } = autoLayoutGrid(screenCats, cols, rows, [], hasPromo, draft.categoryConfigs);
    updateScreen(previewScreenIdx, { gridBlocks: blocks, gridRows: newRows });
  }, [previewScreen?.id]);

  // Reconcile grid blocks when category selection or split points change
  useEffect(() => {
    if (!previewScreen) return;
    const existing = previewScreen.gridBlocks ?? [];
    if (!existing.length) return; // let auto-populate handle empty grids

    const screenCats = previewScreen.useAllCategories !== false
      ? categories
      : categories.filter((c) => previewScreen.categoryIds.includes(c.id));
    const enabledIds = new Set(screenCats.map((c) => c.id));

    // Remove blocks for categories no longer on this screen
    let blocks = existing.filter(
      (b) => b.type !== "category" || (b.categoryId && enabledIds.has(b.categoryId))
    );

    // Find categories that need blocks added
    const placedCatIds = new Set(
      blocks.filter((b) => b.type === "category" && b.categoryId).map((b) => b.categoryId!)
    );
    const unplaced = screenCats.filter((c) => !placedCatIds.has(c.id));

    if (unplaced.length > 0) {
      const cols = previewScreen.gridColumns ?? 6;
      const rows = previewScreen.gridRows ?? 4;
      const hasPromo = draft.featureSlots.length > 0;
      const result = autoLayoutGrid(unplaced, cols, rows, blocks, hasPromo, draft.categoryConfigs);
      blocks = result.blocks;
      if (result.rows !== rows) {
        updateScreen(previewScreenIdx, { gridBlocks: blocks, gridRows: result.rows });
        return;
      }
    }

    // Reconcile split parts for existing categories
    for (const cat of screenCats) {
      const splitPoints = draft.categoryConfigs[cat.id]?.splitPoints;
      const expectedParts = splitPoints && splitPoints.length > 0 ? splitPoints.length + 1 : 1;
      const existingParts = blocks.filter((b) => b.type === "category" && b.categoryId === cat.id);
      if (existingParts.length !== expectedParts) {
        // Remove old parts and re-add via auto-layout
        blocks = blocks.filter((b) => !(b.type === "category" && b.categoryId === cat.id));
        const cols = previewScreen.gridColumns ?? 6;
        const rows = previewScreen.gridRows ?? 4;
        const result = autoLayoutGrid([cat], cols, rows, blocks, false, draft.categoryConfigs);
        blocks = result.blocks;
      }
    }

    // Only update if blocks actually changed
    if (JSON.stringify(blocks) !== JSON.stringify(existing)) {
      updateScreen(previewScreenIdx, { gridBlocks: blocks });
    }
  }, [
    previewScreen?.categoryIds?.join(","),
    previewScreen?.useAllCategories,
    JSON.stringify(draft.categoryConfigs),
    previewScreenIdx,
  ]);

  // ── Upload status toast ──
  const statusBar = uploadStatus.loading ? (
    <div className="sp-toast sp-toast-info">Uploading...</div>
  ) : uploadStatus.error ? (
    <div className="sp-toast sp-toast-error">{uploadStatus.error}</div>
  ) : uploadStatus.success ? (
    <div className="sp-toast sp-toast-success">Uploaded!</div>
  ) : null;

  // ── Thumbnail helper ──
  function Thumb({ src, label }: { src?: string; label: string }) {
    if (!src) return null;
    const isVideo = /\.(mp4|webm)$/i.test(src);
    return (
      <div className="sp-thumb">
        {isVideo ? (
          <video src={src} className="sp-thumb-img" muted autoPlay loop playsInline />
        ) : (
          <img src={src} alt={label} className="sp-thumb-img" />
        )}
      </div>
    );
  }

  return (
    <div className="sp-root">
      {/* ── Header ── */}
      <header className="sp-header">
        <div className="sp-header-left">
          <h2 className="sp-title">Menu Board Settings</h2>
          <div className="sp-tabs">
            {(["appearance", "content", "menu", "screens"] as const).map((tab) => (
              <button
                key={tab}
                type="button"
                className={`sp-tab ${activeTab === tab ? "sp-tab-active" : ""}`}
                onClick={() => setActiveTab(tab)}
              >
                {tab === "appearance" ? "Appearance" : tab === "content" ? "Content" : tab === "menu" ? "Menu" : "Screens"}
              </button>
            ))}
          </div>
        </div>
        <button type="button" className="sp-close-btn" onClick={onClose}>Back to Board</button>
      </header>

      {statusBar}

      {/* ── Split: Controls left, Preview right ── */}
      <div className="sp-body">
        <div className="sp-controls">
          {activeTab === "appearance" && (
            <div className="sp-section-list">
              {/* Theme picker */}
              <div className="sp-card">
                <h3 className="sp-card-title">Theme</h3>
                <div className="sp-theme-grid">
                  {(["newksFresh", "modernDark", "light"] as const).map((t) => (
                    <button
                      key={t}
                      type="button"
                      className={`sp-theme-card ${draft.theme === t ? "sp-theme-card-active" : ""}`}
                      onClick={() => update((prev) => ({ ...prev, theme: t }))}
                    >
                      <div className="sp-theme-swatch" style={{
                        background: t === "newksFresh"
                          ? "linear-gradient(135deg, #f7f6f2 60%, #4a7c4e)"
                          : t === "modernDark"
                            ? "linear-gradient(135deg, #1f2933, #05060a)"
                            : "linear-gradient(135deg, #ffffff, #e5e7eb)",
                      }} />
                      <span className="sp-theme-label">
                        {t === "newksFresh" ? "Newk's Fresh" : t === "modernDark" ? "Dark" : "Light"}
                      </span>
                    </button>
                  ))}
                </div>
              </div>

              {/* Logo */}
              <div className="sp-card">
                <h3 className="sp-card-title">Logo</h3>
                <div className="sp-upload-row">
                  <button type="button" className="sp-upload-btn" disabled={uploadStatus.loading}
                    onClick={() => triggerUpload((url) => setBranding({ logoUrl: url }))}>
                    {uploadStatus.loading ? "Uploading..." : "Upload Logo"}
                  </button>
                  <Thumb src={branding.logoUrl} label="Logo" />
                  {branding.logoUrl && (
                    <button type="button" className="sp-text-btn" onClick={() => setBranding({ logoUrl: undefined })}>Remove</button>
                  )}
                </div>
              </div>

              {/* Tagline */}
              <div className="sp-card">
                <h3 className="sp-card-title">Tagline</h3>
                <input type="text" className="sp-input" placeholder="e.g. Fresh-made, every day."
                  value={branding.tagline ?? ""} onChange={(e) => setBranding({ tagline: e.target.value || undefined })} />
              </div>

              {/* Colors */}
              <div className="sp-card">
                <h3 className="sp-card-title">Colors</h3>
                <div className="sp-color-grid">
                  <label className="sp-color-field">
                    <span>Primary</span>
                    <div className="sp-color-row">
                      <input type="color" className="sp-color-swatch" value={branding.primaryColor ?? "#f97316"}
                        onChange={(e) => setBranding({ primaryColor: e.target.value })} />
                      <input type="text" className="sp-input sp-input-sm" placeholder="#f97316"
                        value={branding.primaryColor ?? ""}
                        onChange={(e) => setBranding({ primaryColor: e.target.value || undefined })} />
                    </div>
                  </label>
                  <label className="sp-color-field">
                    <span>Background</span>
                    <div className="sp-color-row">
                      <input type="color" className="sp-color-swatch" value={branding.backgroundColor ?? "#111827"}
                        onChange={(e) => setBranding({ backgroundColor: e.target.value })} />
                      <input type="text" className="sp-input sp-input-sm" placeholder="#111827"
                        value={branding.backgroundColor ?? ""}
                        onChange={(e) => setBranding({ backgroundColor: e.target.value || undefined })} />
                    </div>
                  </label>
                </div>
              </div>

              {/* Background image */}
              <div className="sp-card">
                <h3 className="sp-card-title">Background Image</h3>
                <div className="sp-upload-row">
                  <button type="button" className="sp-upload-btn" disabled={uploadStatus.loading}
                    onClick={() => triggerUpload((url) => setBranding({ backgroundImageUrl: url }))}>
                    Upload Background
                  </button>
                  <Thumb src={branding.backgroundImageUrl} label="Background" />
                  {branding.backgroundImageUrl && (
                    <button type="button" className="sp-text-btn" onClick={() => setBranding({ backgroundImageUrl: undefined })}>Remove</button>
                  )}
                </div>
              </div>

              {/* Display options */}
              <div className="sp-card">
                <h3 className="sp-card-title">Display</h3>
                <label className="sp-checkbox">
                  <input type="checkbox" checked={draft.showPrices}
                    onChange={(e) => update((prev) => ({ ...prev, showPrices: e.target.checked }))} />
                  Show prices
                </label>
                <label className="sp-checkbox">
                  <input type="checkbox" checked={draft.showDescriptions}
                    onChange={(e) => update((prev) => ({ ...prev, showDescriptions: e.target.checked }))} />
                  Show descriptions
                </label>
                <label className="sp-field">
                  Auto-cycle (seconds)
                  <input type="number" min={0} className="sp-input sp-input-sm" value={draft.autoCycleSeconds}
                    onChange={(e) => update((prev) => ({ ...prev, autoCycleSeconds: Number(e.target.value) || 0 }))} />
                </label>
              </div>
            </div>
          )}

          {activeTab === "content" && (
            <div className="sp-section-list">
              <div className="sp-card">
                <h3 className="sp-card-title">Promo Slides</h3>
                <p className="sp-hint">These slides appear in <strong>promo blocks</strong> on your board and auto-cycle every 8 seconds. Add a promo block to any screen from the <strong>Screens</strong> tab using the <strong>"+ Promo"</strong> button.</p>
                {draft.featureSlots.map((slot, i) => (
                  <div key={slot.id} className="sp-feature-slot">
                    <div className="sp-feature-slot-header">
                      <strong>Slide {i + 1}</strong>
                      <button type="button" className="sp-text-btn sp-text-btn-danger" onClick={() => removeFeatureSlot(i)}>Remove</button>
                    </div>
                    <input type="text" className="sp-input" placeholder="Title" value={slot.title ?? ""}
                      onChange={(e) => updateFeatureSlot(i, { title: e.target.value || undefined })} />
                    <input type="text" className="sp-input" placeholder="Subtitle" value={slot.subtitle ?? ""}
                      onChange={(e) => updateFeatureSlot(i, { subtitle: e.target.value || undefined })} />
                    <div className="sp-upload-row">
                      <button type="button" className="sp-upload-btn" disabled={uploadStatus.loading}
                        onClick={() => triggerUpload((url) => updateFeatureSlot(i, { imageUrl: url }))}>
                        Image
                      </button>
                      <Thumb src={slot.imageUrl} label={`Slide ${i + 1}`} />
                      {slot.imageUrl && <button type="button" className="sp-text-btn" onClick={() => updateFeatureSlot(i, { imageUrl: undefined })}>x</button>}
                      <button type="button" className="sp-upload-btn" disabled={uploadStatus.loading}
                        onClick={() => triggerUpload((url) => updateFeatureSlot(i, { videoUrl: url }))}>
                        Video
                      </button>
                      <Thumb src={slot.videoUrl} label={`Slide ${i + 1} video`} />
                      {slot.videoUrl && <button type="button" className="sp-text-btn" onClick={() => updateFeatureSlot(i, { videoUrl: undefined })}>x</button>}
                    </div>
                  </div>
                ))}
                <button type="button" className="sp-add-btn" onClick={addFeatureSlot}>+ Add Promo Slide</button>
              </div>

              <div className="sp-card">
                <h3 className="sp-card-title">Promo Video (global)</h3>
                <p className="sp-hint">This video plays in promo blocks. If a promo slide also has its own video, the slide video takes priority.</p>
                <div className="sp-upload-row">
                  <button type="button" className="sp-upload-btn" disabled={uploadStatus.loading}
                    onClick={() => triggerUpload((url) => setMarketing({ promoVideoUrl: url }))}>
                    Upload Video
                  </button>
                  <Thumb src={marketing.promoVideoUrl} label="Promo video" />
                  {marketing.promoVideoUrl && (
                    <button type="button" className="sp-text-btn" onClick={() => setMarketing({ promoVideoUrl: undefined })}>Remove</button>
                  )}
                </div>
              </div>
            </div>
          )}

          {activeTab === "menu" && (
            <div className="sp-section-list">
              <div className="sp-card">
                <h3 className="sp-card-title">Category & Item Names</h3>
                <p className="sp-hint">Override POS names shown on the board. Set split points to break long categories into multiple cards.</p>
                {categories.map((cat) => {
                  const catCfg = draft.categoryConfigs[cat.id];
                  const expanded = menuExpandedCatIds.has(cat.id);
                  const splitPoints = catCfg?.splitPoints ?? [];
                  return (
                    <div key={cat.id} className="sp-menu-cat-block">
                      <div className="sp-menu-cat-header">
                        <button
                          type="button"
                          className="board-settings-expand-btn"
                          onClick={() => setMenuExpandedCatIds((prev) => {
                            const next = new Set(prev);
                            if (next.has(cat.id)) next.delete(cat.id); else next.add(cat.id);
                            return next;
                          })}
                          aria-expanded={expanded}
                        >
                          {expanded ? "▼" : "▶"}
                        </button>
                        <div className="sp-menu-cat-fields">
                          <div className="sp-menu-name-row">
                            <span className="sp-menu-pos-name">{viewModel.menu.categories.find((c) => c.id === cat.id)?.name ?? cat.name}</span>
                            <input
                              type="text"
                              className="sp-input sp-input-sm"
                              placeholder="Display name override..."
                              value={catCfg?.displayName ?? ""}
                              onChange={(e) => updateCategoryConfig(cat.id, { displayName: e.target.value || undefined })}
                            />
                          </div>
                          {splitPoints.length > 0 && (
                            <span className="sp-menu-split-badge">{splitPoints.length + 1} cards</span>
                          )}
                        </div>
                      </div>
                      {expanded && (
                        <div className="sp-menu-item-list">
                          {cat.items.map((item, idx) => {
                            const itemCfg = draft.itemConfigs[item.source.id];
                            const isSplitPoint = splitPoints.includes(idx + 1);
                            return (
                              <React.Fragment key={item.source.id}>
                                {isSplitPoint && (
                                  <div className="sp-menu-split-divider">
                                    <span>— card break —</span>
                                    <button type="button" className="sp-text-btn sp-text-btn-danger" onClick={() => toggleSplitPoint(cat.id, idx + 1)}>Remove</button>
                                  </div>
                                )}
                                <div className="sp-menu-item-row">
                                  <div className="sp-menu-item-fields">
                                    <span className="sp-menu-pos-name">{item.source.name}</span>
                                    <input
                                      type="text"
                                      className="sp-input sp-input-sm"
                                      placeholder="Display name..."
                                      value={itemCfg?.displayName ?? ""}
                                      onChange={(e) => updateItemConfig(item.source.id, { displayName: e.target.value || undefined })}
                                    />
                                    <input
                                      type="text"
                                      className="sp-input sp-input-sm"
                                      placeholder="Description override..."
                                      value={itemCfg?.descriptionOverride ?? ""}
                                      onChange={(e) => updateItemConfig(item.source.id, { descriptionOverride: e.target.value || undefined })}
                                    />
                                  </div>
                                  {idx < cat.items.length - 1 && (
                                    <button
                                      type="button"
                                      className={`sp-split-btn ${splitPoints.includes(idx + 1) ? "sp-split-btn-active" : ""}`}
                                      title={splitPoints.includes(idx + 1) ? "Remove break after this item" : "Break card after this item"}
                                      onClick={() => toggleSplitPoint(cat.id, idx + 1)}
                                    >
                                      ✂
                                    </button>
                                  )}
                                </div>
                              </React.Fragment>
                            );
                          })}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {activeTab === "screens" && (
            <div className="sp-section-list">
              <div className="sp-card">
                <h3 className="sp-card-title">Screens</h3>
                <p className="sp-hint">Configure each physical display. Click a screen to preview it on the right.</p>
                {screens.map((screen, index) => (
                  <div
                    key={screen.id}
                    className={`sp-screen-block ${previewScreenIdx === index ? "sp-screen-block-active" : ""}`}
                    draggable
                    onDragStart={() => handleDragStart(index)}
                    onDragOver={(e) => handleDragOver(e, index)}
                    onDragEnd={handleDragEnd}
                    onClick={() => setPreviewScreenIdx(index)}
                  >
                    <div className="sp-screen-header">
                      <span className="sp-drag-handle">☰</span>
                      <strong>{screen.label}</strong>
                      <span className="sp-screen-dims">{screen.width}x{screen.height}</span>
                      {screens.length > 1 && (
                        <button type="button" className="sp-text-btn sp-text-btn-danger"
                          onClick={(e) => { e.stopPropagation(); removeScreen(index); }}>Remove</button>
                      )}
                    </div>
                    <div className="sp-screen-fields">
                      <input type="text" className="sp-input" placeholder="Screen label"
                        value={screen.label} onChange={(e) => updateScreen(index, { label: e.target.value })} />
                      <div className="sp-screen-row">
                        <label className="sp-field">Columns <input type="number" min={2} max={12} className="sp-input sp-input-sm"
                          value={screen.gridColumns ?? 6} onChange={(e) => updateScreen(index, { gridColumns: Math.max(2, Math.min(12, Number(e.target.value) || 6)) })} /></label>
                        <label className="sp-field">Rows <input type="number" min={2} max={8} className="sp-input sp-input-sm"
                          value={screen.gridRows ?? 4} onChange={(e) => updateScreen(index, { gridRows: Math.max(2, Math.min(8, Number(e.target.value) || 4)) })} /></label>
                        <label className="sp-field">Font Size
                          <select className="sp-input sp-input-sm" value={screen.fontSize ?? "medium"}
                            onChange={(e) => updateScreen(index, { fontSize: e.target.value as "small" | "medium" | "large" })}>
                            <option value="small">Small</option>
                            <option value="medium">Medium</option>
                            <option value="large">Large</option>
                          </select>
                        </label>
                      </div>
                      <div className="sp-grid-toolbar">
                        <button type="button" disabled={uploadStatus.loading}
                          onClick={(e) => { e.stopPropagation(); addGridImageBlock(index); }}>+ Image</button>
                        <button type="button" disabled={uploadStatus.loading}
                          onClick={(e) => { e.stopPropagation(); addGridVideoBlock(index); }}>+ Video</button>
                        <button type="button" disabled={!draft.featureSlots.length}
                          onClick={(e) => { e.stopPropagation(); addPromoBlock(index); }}>+ Promo</button>
                        <button type="button"
                          onClick={(e) => { e.stopPropagation(); resetGridLayout(index); }}>Reset Layout</button>
                      </div>
                      <div className="sp-screen-row">
                        <label className="sp-field">W <input type="number" min={800} className="sp-input sp-input-sm"
                          value={screen.width} onChange={(e) => updateScreen(index, { width: Number(e.target.value) || 1920 })} /></label>
                        <label className="sp-field">H <input type="number" min={600} className="sp-input sp-input-sm"
                          value={screen.height} onChange={(e) => updateScreen(index, { height: Number(e.target.value) || 1080 })} /></label>
                      </div>
                      <div className="sp-upload-row">
                        <button type="button" className="sp-upload-btn" disabled={uploadStatus.loading}
                          onClick={() => triggerUpload((url) => updateScreen(index, { backgroundImageUrl: url }))}>BG Image</button>
                        <Thumb src={screen.backgroundImageUrl} label="BG" />
                        {screen.backgroundImageUrl && <button type="button" className="sp-text-btn" onClick={() => updateScreen(index, { backgroundImageUrl: undefined })}>x</button>}
                        <button type="button" className="sp-upload-btn" disabled={uploadStatus.loading}
                          onClick={() => triggerUpload((url) => updateScreen(index, { backgroundVideoUrl: url }))}>BG Video</button>
                        <Thumb src={screen.backgroundVideoUrl} label="BG Video" />
                        {screen.backgroundVideoUrl && <button type="button" className="sp-text-btn" onClick={() => updateScreen(index, { backgroundVideoUrl: undefined })}>x</button>}
                      </div>
                      {locationId && (
                        <div className="sp-display-url" style={{ display: "flex", alignItems: "center", gap: 6, margin: "6px 0", fontSize: "0.8rem" }}>
                          <span style={{ color: "#666", whiteSpace: "nowrap" }}>Display URL:</span>
                          <code style={{ flex: 1, padding: "3px 6px", background: "#f1f5f9", borderRadius: 4, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontSize: "0.75rem" }}>
                            {`${window.location.origin}/menuboard/menu-board?screen=${index + 1}&location=${locationId}`}
                          </code>
                          <button type="button" className="sp-text-btn" style={{ fontSize: "0.75rem" }} onClick={(e) => {
                            e.stopPropagation();
                            navigator.clipboard.writeText(`${window.location.origin}/menuboard/menu-board?screen=${index + 1}&location=${locationId}`);
                          }}>Copy</button>
                        </div>
                      )}
                      <div className="sp-cat-toolbar">
                        <input type="text" className="sp-input sp-cat-search" placeholder="Search categories..."
                          value={categoryFilter} onChange={(e) => setCategoryFilter(e.target.value)} />
                        <button type="button" className="sp-text-btn"
                          onClick={() => updateScreen(index, { categoryIds: [], useAllCategories: true })}>All</button>
                        <button type="button" className="sp-text-btn"
                          onClick={() => updateScreen(index, { categoryIds: [], useAllCategories: false })}>None</button>
                      </div>
                      <div className="sp-cat-chips">
                        {categories
                          .filter((cat) => !categoryFilter || cat.name.toLowerCase().includes(categoryFilter.toLowerCase()))
                          .map((cat) => {
                            const useAll = screen.useAllCategories !== false;
                            const on = useAll || screen.categoryIds.includes(cat.id);
                            return (
                              <label key={cat.id} className={`sp-cat-chip ${on ? "sp-cat-chip-on" : ""}`}>
                                <input type="checkbox" checked={on} onChange={() => toggleCategoryOnScreen(index, cat.id)} style={{ display: "none" }} />
                                {cat.name}
                              </label>
                            );
                          })}
                      </div>
                    </div>
                  </div>
                ))}
                <button type="button" className="sp-add-btn" onClick={addScreen}>+ Add Screen</button>
              </div>
            </div>
          )}
        </div>

        {/* ── Live Preview ── */}
        <div className="sp-preview" ref={previewContainerRef}>
          <div className="sp-preview-header">
            <span className="sp-preview-label">Live Preview</span>
            {screens.length > 1 && (
              <div className="sp-preview-tabs">
                {screens.map((s, i) => (
                  <button key={s.id} type="button"
                    className={`sp-preview-tab ${previewScreenIdx === i ? "sp-preview-tab-active" : ""}`}
                    onClick={() => setPreviewScreenIdx(i)}>{s.label}</button>
                ))}
              </div>
            )}
          </div>
          <div className="sp-preview-frame" style={{
            width: "100%",
            aspectRatio: `${previewScreen?.width ?? 1920} / ${previewScreen?.height ?? 1080}`,
            overflow: "hidden",
          }}>
            <div style={{
              width: previewScreen?.width ?? 1920,
              height: previewScreen?.height ?? 1080,
              transform: `scale(${previewScale})`,
              transformOrigin: "top left",
            }}>
              <div
                className={`board-root board-root-${draft.theme}`}
                style={{
                  width: "100%", height: "100%",
                  ...(draft.branding?.backgroundColor ? { background: draft.branding.backgroundColor } : {}),
                  ...(previewScreen?.backgroundImageUrl
                    ? { backgroundImage: `url(${previewScreen.backgroundImageUrl})`, backgroundSize: "cover", backgroundPosition: "center" }
                    : draft.branding?.backgroundImageUrl
                      ? { backgroundImage: `url(${draft.branding.backgroundImageUrl})`, backgroundSize: "cover", backgroundPosition: "center" }
                      : {}),
                }}
              >
                <BoardHeader locationName={null} demoMode={false} onOpenQuickSetup={() => {}} onOpenSettings={() => {}} branding={draft.branding} />
                <div style={{ flex: 1, overflow: "hidden" }}>
                  <GridBoardRenderer
                    blocks={previewScreen?.gridBlocks ?? []}
                    gridColumns={previewScreen?.gridColumns ?? 6}
                    gridRows={previewScreen?.gridRows ?? 4}
                    categories={previewCategories}
                    categoryConfigs={draft.categoryConfigs}
                    featureSlots={draft.featureSlots}
                    freshTheme={draft.theme === "newksFresh"}
                    fontSize={previewScreen?.fontSize ?? "medium"}
                    editorMode={true}
                    previewScale={previewScale}
                    onBlockMove={handleGridBlockMove}
                    onBlockResize={handleGridBlockResize}
                    onBlockSelect={setSelectedBlockId}
                    onBlockRemove={handleGridBlockRemove}
                    selectedBlockId={selectedBlockId}
                  />
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <input ref={fileInputRef} type="file" accept="image/*,video/*" style={{ display: "none" }} onChange={handleUploadChange} />

      {/* Auth prompt modal */}
      {showAuthPrompt && (
        <div style={{
          position: "fixed", inset: 0, background: "rgba(0,0,0,0.5)", zIndex: 9999,
          display: "flex", alignItems: "center", justifyContent: "center",
        }} onClick={() => setShowAuthPrompt(false)}>
          <div style={{
            background: "#fff", borderRadius: 12, padding: 24, width: 340,
            boxShadow: "0 8px 32px rgba(0,0,0,0.2)",
          }} onClick={(e) => e.stopPropagation()}>
            <h3 style={{ margin: "0 0 8px", fontSize: 16 }}>Admin Login</h3>
            <p style={{ margin: "0 0 12px", fontSize: 13, color: "#6b7280" }}>
              Enter your admin token to save settings and upload files.
            </p>
            {authError && <p style={{ color: "#dc2626", fontSize: 12, margin: "0 0 8px" }}>{authError}</p>}
            <input
              type="password"
              className="sp-input"
              placeholder="Admin token"
              value={authInput}
              onChange={(e) => setAuthInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && authInput.trim()) {
                  setAdminToken(authInput.trim());
                  setAuthInput("");
                  setAuthError(null);
                  setShowAuthPrompt(false);
                }
              }}
              autoFocus
            />
            <div style={{ display: "flex", gap: 8, marginTop: 12, justifyContent: "flex-end" }}>
              <button type="button" className="sp-upload-btn" onClick={() => setShowAuthPrompt(false)}>Cancel</button>
              <button
                type="button"
                className="sp-upload-btn"
                style={{ background: "#4a7c4e", color: "#fff", border: "none" }}
                onClick={() => {
                  if (!authInput.trim()) { setAuthError("Token required"); return; }
                  setAdminToken(authInput.trim());
                  setAuthInput("");
                  setAuthError(null);
                  setShowAuthPrompt(false);
                }}
              >
                Save Token
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
