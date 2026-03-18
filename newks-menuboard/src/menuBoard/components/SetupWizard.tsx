import React, { useEffect, useMemo, useRef, useState } from "react";
import type {
  BoardConfig,
  BoardViewModel,
  BrandingConfig,
  FeatureSlot,
  ScreenDefinition,
} from "../types";
import { autoLayoutGrid } from "../utils/autoLayoutGrid";
import { mergeMenuWithConfig } from "../utils/mergeMenuWithConfig";
import { setAdminToken } from "../utils/storage";
import type { Menu } from "../../core/types";

type Props = {
  menu: Menu;
  boardConfig: BoardConfig;
  viewModel: BoardViewModel;
  onComplete: (config: BoardConfig) => void;
  onSkip: () => void;
};

type Step = 1 | 2 | 3 | 4;

type UploadStatus = { loading: boolean; error: string | null; success: boolean };

export function SetupWizard({ menu, boardConfig, viewModel, onComplete, onSkip }: Props) {
  const [step, setStep] = useState<Step>(1);
  const [draft, setDraft] = useState<BoardConfig>({ ...boardConfig });
  const [expandedCategoryIds, setExpandedCategoryIds] = useState<Set<string>>(new Set());
  const [uploadStatus, setUploadStatus] = useState<UploadStatus>({ loading: false, error: null, success: false });
  const [showAuthPrompt, setShowAuthPrompt] = useState(false);
  const [authInput, setAuthInput] = useState("");
  const [authError, setAuthError] = useState<string | null>(null);

  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const uploadCallbackRef = useRef<((url: string) => void) | null>(null);

  // Clear upload success flash
  useEffect(() => {
    if (!uploadStatus.success) return;
    const id = setTimeout(() => setUploadStatus((s) => ({ ...s, success: false })), 2000);
    return () => clearTimeout(id);
  }, [uploadStatus.success]);

  const branding: BrandingConfig = draft.branding ?? {};

  const itemsByCategoryId = useMemo(() => {
    const map = new Map<string, typeof menu.items>();
    for (const item of menu.items) {
      const list = map.get(item.categoryId) ?? [];
      list.push(item);
      map.set(item.categoryId, list);
    }
    return map;
  }, [menu.items]);

  // Upload logic — uses base64 JSON to bypass Cloudways nginx blocking multipart uploads
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

  function update(updater: (prev: BoardConfig) => BoardConfig) {
    setDraft((prev) => updater(prev));
  }

  function setBranding(updates: Partial<BrandingConfig>) {
    update((prev) => ({ ...prev, branding: { ...(prev.branding ?? {}), ...updates } }));
  }

  function updateCategoryHidden(categoryId: string, hidden: boolean) {
    update((prev) => ({
      ...prev,
      categoryConfigs: {
        ...prev.categoryConfigs,
        [categoryId]: {
          ...(prev.categoryConfigs[categoryId] ?? { categoryId }),
          categoryId,
          hidden,
        },
      },
    }));
  }

  function updateItemHidden(itemId: string, hidden: boolean) {
    update((prev) => ({
      ...prev,
      itemConfigs: {
        ...prev.itemConfigs,
        [itemId]: {
          ...(prev.itemConfigs[itemId] ?? { itemId }),
          itemId,
          hidden,
        },
      },
    }));
  }

  function updateFeatureSlot(index: number, updates: Partial<FeatureSlot>) {
    update((prev) => {
      const slots = [...prev.featureSlots];
      slots[index] = { ...slots[index], ...updates };
      return { ...prev, featureSlots: slots };
    });
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

  function handleFinish() {
    // Build grid layouts for each screen
    const vm = mergeMenuWithConfig(menu, draft);
    const visibleCats = vm.categories.filter((c) => !c.hidden);
    const hasPromo = draft.featureSlots.length > 0;

    const screens = (draft.screens ?? []).map((screen) => {
      const screenCats = screen.useAllCategories !== false
        ? visibleCats
        : visibleCats.filter((c) => screen.categoryIds.includes(c.id));
      const cols = screen.gridColumns ?? 6;
      const rows = screen.gridRows ?? 4;
      const { blocks, rows: newRows } = autoLayoutGrid(screenCats, cols, rows, [], hasPromo);
      return { ...screen, gridBlocks: blocks, gridRows: newRows };
    });

    const finalConfig: BoardConfig = {
      ...draft,
      screens,
      setupComplete: true,
    };
    onComplete(finalConfig);
  }

  // Screen config for step 4
  function setScreenCount(count: number) {
    update((prev) => {
      const current = prev.screens ?? [{ id: "1", label: "Screen 1", width: 1920, height: 1080, categoryIds: [], gridColumns: 6, gridRows: 4, gridBlocks: [] }];
      if (count > current.length) {
        const newScreens = [...current];
        for (let i = current.length; i < count; i++) {
          const id = String(i + 1);
          newScreens.push({ id, label: `Screen ${id}`, width: 1920, height: 1080, categoryIds: [], gridColumns: 6, gridRows: 4, gridBlocks: [] });
        }
        return { ...prev, screens: newScreens };
      }
      return { ...prev, screens: current.slice(0, count) };
    });
  }

  function updateScreen(index: number, updates: Partial<ScreenDefinition>) {
    update((prev) => {
      const screens = [...(prev.screens ?? [])];
      if (screens[index]) screens[index] = { ...screens[index], ...updates };
      return { ...prev, screens };
    });
  }

  const screenCount = draft.screens?.length ?? 1;

  function Thumb({ src, label }: { src?: string; label: string }) {
    if (!src) return null;
    const isVideo = /\.(mp4|webm)$/i.test(src);
    return (
      <div style={{ width: 48, height: 48, borderRadius: 6, overflow: "hidden", flexShrink: 0 }}>
        {isVideo ? (
          <video src={src} style={{ width: "100%", height: "100%", objectFit: "cover" }} muted autoPlay loop playsInline />
        ) : (
          <img src={src} alt={label} style={{ width: "100%", height: "100%", objectFit: "cover" }} />
        )}
      </div>
    );
  }

  const stepTitles = ["Pick Your Menus", "Brand & Style", "Upload Content", "Screens"];

  return (
    <div style={{
      position: "fixed", inset: 0, background: "#f7f6f2", zIndex: 9999,
      display: "flex", flexDirection: "column", fontFamily: "system-ui, -apple-system, sans-serif",
    }}>
      {/* Header */}
      <header style={{
        display: "flex", alignItems: "center", justifyContent: "space-between",
        padding: "16px 32px", borderBottom: "1px solid #e5e7eb",
        background: "#fff",
      }}>
        <div>
          <h2 style={{ margin: 0, fontSize: 20, color: "#1f2937" }}>Menu Board Setup</h2>
          <p style={{ margin: "4px 0 0", fontSize: 13, color: "#6b7280" }}>Step {step} of 4: {stepTitles[step - 1]}</p>
        </div>
        <button
          type="button"
          onClick={onSkip}
          style={{
            background: "none", border: "1px solid #d1d5db", borderRadius: 8, padding: "6px 16px",
            fontSize: 13, color: "#6b7280", cursor: "pointer",
          }}
        >
          Skip Setup
        </button>
      </header>

      {/* Progress bar */}
      <div style={{ height: 3, background: "#e5e7eb" }}>
        <div style={{ height: "100%", width: `${(step / 4) * 100}%`, background: "#4a7c4e", transition: "width 0.3s" }} />
      </div>

      {/* Upload status toast */}
      {uploadStatus.loading && <div style={{ background: "#dbeafe", padding: "8px 32px", fontSize: 13 }}>Uploading...</div>}
      {uploadStatus.error && <div style={{ background: "#fee2e2", padding: "8px 32px", fontSize: 13, color: "#dc2626" }}>{uploadStatus.error}</div>}
      {uploadStatus.success && <div style={{ background: "#dcfce7", padding: "8px 32px", fontSize: 13, color: "#16a34a" }}>Uploaded!</div>}

      {/* Body */}
      <div style={{ flex: 1, overflow: "auto", padding: "24px 32px" }}>
        {/* Step 1: Pick Your Menus */}
        {step === 1 && (
          <div style={{ maxWidth: 640, margin: "0 auto" }}>
            <p style={{ fontSize: 14, color: "#6b7280", marginBottom: 16 }}>
              Choose which menu categories and items to display on your board.
            </p>
            {menu.categories.map((cat) => {
              const cfg = draft.categoryConfigs[cat.id];
              const hidden = cfg?.hidden ?? false;
              const expanded = expandedCategoryIds.has(cat.id);
              const items = itemsByCategoryId.get(cat.id) ?? [];
              return (
                <div key={cat.id} style={{ marginBottom: 2, background: "#fff", borderRadius: 10, border: "1px solid #e5e7eb" }}>
                  <div style={{ display: "flex", alignItems: "center", padding: "10px 14px", gap: 10 }}>
                    <button
                      type="button"
                      onClick={() => {
                        const next = new Set(expandedCategoryIds);
                        if (next.has(cat.id)) next.delete(cat.id); else next.add(cat.id);
                        setExpandedCategoryIds(next);
                      }}
                      style={{ background: "none", border: "none", cursor: "pointer", fontSize: 12, padding: 0, color: "#6b7280" }}
                    >
                      {expanded ? "▼" : "▶"}
                    </button>
                    <label style={{ display: "flex", alignItems: "center", gap: 8, flex: 1, cursor: "pointer", fontSize: 14, fontWeight: 500 }}>
                      <input type="checkbox" checked={!hidden} onChange={(e) => updateCategoryHidden(cat.id, !e.target.checked)} />
                      {cat.name}
                    </label>
                    <span style={{ fontSize: 12, color: "#9ca3af" }}>{items.length} items</span>
                  </div>
                  {expanded && (
                    <div style={{ padding: "0 14px 10px 42px" }}>
                      {items.map((item) => {
                        const itemCfg = draft.itemConfigs[item.id];
                        const itemHidden = itemCfg?.hidden ?? false;
                        return (
                          <label key={item.id} style={{ display: "flex", alignItems: "center", gap: 8, padding: "4px 0", fontSize: 13, cursor: "pointer" }}>
                            <input type="checkbox" checked={!itemHidden} onChange={(e) => updateItemHidden(item.id, !e.target.checked)} />
                            {item.name}
                          </label>
                        );
                      })}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}

        {/* Step 2: Brand & Style */}
        {step === 2 && (
          <div style={{ maxWidth: 560, margin: "0 auto" }}>
            {/* Theme */}
            <div style={{ marginBottom: 24 }}>
              <h3 style={{ fontSize: 15, marginBottom: 10 }}>Theme</h3>
              <div style={{ display: "flex", gap: 12 }}>
                {(["newksFresh", "modernDark", "light"] as const).map((t) => (
                  <button
                    key={t}
                    type="button"
                    onClick={() => update((prev) => ({ ...prev, theme: t }))}
                    style={{
                      flex: 1, padding: 12, borderRadius: 10, cursor: "pointer",
                      border: draft.theme === t ? "2px solid #4a7c4e" : "2px solid #e5e7eb",
                      background: "#fff", textAlign: "center",
                    }}
                  >
                    <div style={{
                      height: 40, borderRadius: 6, marginBottom: 8,
                      background: t === "newksFresh"
                        ? "linear-gradient(135deg, #f7f6f2 60%, #4a7c4e)"
                        : t === "modernDark"
                          ? "linear-gradient(135deg, #1f2933, #05060a)"
                          : "linear-gradient(135deg, #ffffff, #e5e7eb)",
                    }} />
                    <span style={{ fontSize: 13, fontWeight: 500 }}>
                      {t === "newksFresh" ? "Newk's Fresh" : t === "modernDark" ? "Dark" : "Light"}
                    </span>
                  </button>
                ))}
              </div>
            </div>

            {/* Logo */}
            <div style={{ marginBottom: 24 }}>
              <h3 style={{ fontSize: 15, marginBottom: 10 }}>Logo</h3>
              <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                <button type="button" disabled={uploadStatus.loading}
                  onClick={() => triggerUpload((url) => setBranding({ logoUrl: url }))}
                  style={{ padding: "8px 16px", borderRadius: 8, border: "1px solid #d1d5db", background: "#fff", cursor: "pointer", fontSize: 13 }}
                >
                  Upload Logo
                </button>
                <Thumb src={branding.logoUrl} label="Logo" />
                {branding.logoUrl && (
                  <button type="button" onClick={() => setBranding({ logoUrl: undefined })}
                    style={{ background: "none", border: "none", color: "#dc2626", cursor: "pointer", fontSize: 13 }}>Remove</button>
                )}
              </div>
            </div>

            {/* Tagline */}
            <div style={{ marginBottom: 24 }}>
              <h3 style={{ fontSize: 15, marginBottom: 10 }}>Tagline</h3>
              <input type="text" placeholder="e.g. Fresh-made, every day."
                value={branding.tagline ?? ""}
                onChange={(e) => setBranding({ tagline: e.target.value || undefined })}
                style={{ width: "100%", padding: "10px 14px", borderRadius: 8, border: "1px solid #d1d5db", fontSize: 14 }}
              />
            </div>

            {/* Colors */}
            <div style={{ marginBottom: 24 }}>
              <h3 style={{ fontSize: 15, marginBottom: 10 }}>Colors</h3>
              <div style={{ display: "flex", gap: 24 }}>
                <label style={{ display: "flex", flexDirection: "column", gap: 6, fontSize: 13 }}>
                  Primary
                  <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <input type="color" value={branding.primaryColor ?? "#4a7c4e"}
                      onChange={(e) => setBranding({ primaryColor: e.target.value })}
                      style={{ width: 36, height: 36, border: "none", cursor: "pointer" }} />
                    <input type="text" placeholder="#4a7c4e" value={branding.primaryColor ?? ""}
                      onChange={(e) => setBranding({ primaryColor: e.target.value || undefined })}
                      style={{ width: 90, padding: "6px 10px", borderRadius: 6, border: "1px solid #d1d5db", fontSize: 13 }} />
                  </div>
                </label>
                <label style={{ display: "flex", flexDirection: "column", gap: 6, fontSize: 13 }}>
                  Background
                  <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <input type="color" value={branding.backgroundColor ?? "#111827"}
                      onChange={(e) => setBranding({ backgroundColor: e.target.value })}
                      style={{ width: 36, height: 36, border: "none", cursor: "pointer" }} />
                    <input type="text" placeholder="#111827" value={branding.backgroundColor ?? ""}
                      onChange={(e) => setBranding({ backgroundColor: e.target.value || undefined })}
                      style={{ width: 90, padding: "6px 10px", borderRadius: 6, border: "1px solid #d1d5db", fontSize: 13 }} />
                  </div>
                </label>
              </div>
            </div>

            {/* Background Image */}
            <div style={{ marginBottom: 24 }}>
              <h3 style={{ fontSize: 15, marginBottom: 10 }}>Background Image</h3>
              <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                <button type="button" disabled={uploadStatus.loading}
                  onClick={() => triggerUpload((url) => setBranding({ backgroundImageUrl: url }))}
                  style={{ padding: "8px 16px", borderRadius: 8, border: "1px solid #d1d5db", background: "#fff", cursor: "pointer", fontSize: 13 }}
                >
                  Upload Background
                </button>
                <Thumb src={branding.backgroundImageUrl} label="BG" />
                {branding.backgroundImageUrl && (
                  <button type="button" onClick={() => setBranding({ backgroundImageUrl: undefined })}
                    style={{ background: "none", border: "none", color: "#dc2626", cursor: "pointer", fontSize: 13 }}>Remove</button>
                )}
              </div>
            </div>
          </div>
        )}

        {/* Step 3: Upload Content */}
        {step === 3 && (
          <div style={{ maxWidth: 560, margin: "0 auto" }}>
            <p style={{ fontSize: 14, color: "#6b7280", marginBottom: 16 }}>
              Add promotional slides that will appear in a promo block on your board. These auto-cycle every 8 seconds.
              You can skip this step if you don't need promos.
            </p>
            {draft.featureSlots.map((slot, i) => (
              <div key={slot.id} style={{ background: "#fff", borderRadius: 10, border: "1px solid #e5e7eb", padding: 16, marginBottom: 12 }}>
                <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 10 }}>
                  <strong style={{ fontSize: 14 }}>Slide {i + 1}</strong>
                  <button type="button" onClick={() => removeFeatureSlot(i)}
                    style={{ background: "none", border: "none", color: "#dc2626", cursor: "pointer", fontSize: 13 }}>Remove</button>
                </div>
                <input type="text" placeholder="Title" value={slot.title ?? ""}
                  onChange={(e) => updateFeatureSlot(i, { title: e.target.value || undefined })}
                  style={{ width: "100%", padding: "8px 12px", borderRadius: 6, border: "1px solid #d1d5db", fontSize: 14, marginBottom: 8 }} />
                <input type="text" placeholder="Subtitle" value={slot.subtitle ?? ""}
                  onChange={(e) => updateFeatureSlot(i, { subtitle: e.target.value || undefined })}
                  style={{ width: "100%", padding: "8px 12px", borderRadius: 6, border: "1px solid #d1d5db", fontSize: 14, marginBottom: 10 }} />
                <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
                  <button type="button" disabled={uploadStatus.loading}
                    onClick={() => triggerUpload((url) => updateFeatureSlot(i, { imageUrl: url }))}
                    style={{ padding: "6px 14px", borderRadius: 6, border: "1px solid #d1d5db", background: "#fff", cursor: "pointer", fontSize: 13 }}
                  >Image</button>
                  <Thumb src={slot.imageUrl} label={`Slide ${i + 1}`} />
                  {slot.imageUrl && <button type="button" onClick={() => updateFeatureSlot(i, { imageUrl: undefined })}
                    style={{ background: "none", border: "none", color: "#dc2626", cursor: "pointer", fontSize: 12 }}>x</button>}
                  <button type="button" disabled={uploadStatus.loading}
                    onClick={() => triggerUpload((url) => updateFeatureSlot(i, { videoUrl: url }))}
                    style={{ padding: "6px 14px", borderRadius: 6, border: "1px solid #d1d5db", background: "#fff", cursor: "pointer", fontSize: 13 }}
                  >Video</button>
                  <Thumb src={slot.videoUrl} label={`Slide ${i + 1} video`} />
                  {slot.videoUrl && <button type="button" onClick={() => updateFeatureSlot(i, { videoUrl: undefined })}
                    style={{ background: "none", border: "none", color: "#dc2626", cursor: "pointer", fontSize: 12 }}>x</button>}
                </div>
              </div>
            ))}
            <button type="button" onClick={addFeatureSlot}
              style={{
                width: "100%", padding: "12px", borderRadius: 8, border: "2px dashed #d1d5db",
                background: "none", cursor: "pointer", fontSize: 14, color: "#4a7c4e", fontWeight: 500,
              }}
            >
              + Add Promo Slide
            </button>
          </div>
        )}

        {/* Step 4: Screens */}
        {step === 4 && (
          <div style={{ maxWidth: 560, margin: "0 auto" }}>
            <p style={{ fontSize: 14, color: "#6b7280", marginBottom: 16 }}>
              How many screens will display your menu? Each screen can show different categories.
              After setup, you can drag and resize blocks in the Settings editor.
            </p>
            <div style={{ marginBottom: 24 }}>
              <h3 style={{ fontSize: 15, marginBottom: 10 }}>Number of Screens</h3>
              <div style={{ display: "flex", gap: 10 }}>
                {[1, 2, 3, 4].map((n) => (
                  <button
                    key={n}
                    type="button"
                    onClick={() => setScreenCount(n)}
                    style={{
                      width: 48, height: 48, borderRadius: 10, cursor: "pointer",
                      border: screenCount === n ? "2px solid #4a7c4e" : "2px solid #e5e7eb",
                      background: screenCount === n ? "#ecfdf5" : "#fff",
                      fontSize: 18, fontWeight: 600,
                    }}
                  >{n}</button>
                ))}
              </div>
            </div>

            {(draft.screens ?? []).map((screen, index) => (
              <div key={screen.id} style={{ background: "#fff", borderRadius: 10, border: "1px solid #e5e7eb", padding: 16, marginBottom: 12 }}>
                <h4 style={{ margin: "0 0 12px", fontSize: 14 }}>{screen.label}</h4>
                <div style={{ display: "flex", gap: 16, marginBottom: 12 }}>
                  <label style={{ fontSize: 13, display: "flex", flexDirection: "column", gap: 4 }}>
                    Resolution
                    <select
                      value={`${screen.width}x${screen.height}`}
                      onChange={(e) => {
                        const [w, h] = e.target.value.split("x").map(Number);
                        updateScreen(index, { width: w, height: h });
                      }}
                      style={{ padding: "8px 12px", borderRadius: 6, border: "1px solid #d1d5db", fontSize: 13 }}
                    >
                      <option value="1920x1080">1920 x 1080 (Full HD)</option>
                      <option value="3840x2160">3840 x 2160 (4K)</option>
                      <option value="1280x720">1280 x 720 (HD)</option>
                    </select>
                  </label>
                </div>
                <div style={{ display: "flex", gap: 16 }}>
                  <label style={{ fontSize: 13, display: "flex", flexDirection: "column", gap: 4 }}>
                    Grid Columns
                    <input type="number" min={2} max={12} value={screen.gridColumns ?? 6}
                      onChange={(e) => updateScreen(index, { gridColumns: Math.max(2, Math.min(12, Number(e.target.value) || 6)) })}
                      style={{ width: 70, padding: "6px 10px", borderRadius: 6, border: "1px solid #d1d5db", fontSize: 13 }} />
                  </label>
                  <label style={{ fontSize: 13, display: "flex", flexDirection: "column", gap: 4 }}>
                    Grid Rows
                    <input type="number" min={2} max={8} value={screen.gridRows ?? 4}
                      onChange={(e) => updateScreen(index, { gridRows: Math.max(2, Math.min(8, Number(e.target.value) || 4)) })}
                      style={{ width: 70, padding: "6px 10px", borderRadius: 6, border: "1px solid #d1d5db", fontSize: 13 }} />
                  </label>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Footer with navigation */}
      <footer style={{
        display: "flex", justifyContent: "space-between", alignItems: "center",
        padding: "16px 32px", borderTop: "1px solid #e5e7eb", background: "#fff",
      }}>
        <button
          type="button"
          disabled={step === 1}
          onClick={() => setStep((s) => (s > 1 ? (s - 1) as Step : s))}
          style={{
            padding: "10px 24px", borderRadius: 8, border: "1px solid #d1d5db",
            background: "#fff", cursor: step === 1 ? "default" : "pointer",
            fontSize: 14, color: step === 1 ? "#d1d5db" : "#374151",
          }}
        >
          Back
        </button>
        <div style={{ display: "flex", gap: 8 }}>
          {[1, 2, 3, 4].map((n) => (
            <div key={n} style={{
              width: 8, height: 8, borderRadius: "50%",
              background: n === step ? "#4a7c4e" : n < step ? "#86efac" : "#d1d5db",
            }} />
          ))}
        </div>
        {step < 4 ? (
          <button
            type="button"
            onClick={() => setStep((s) => (s < 4 ? (s + 1) as Step : s))}
            style={{
              padding: "10px 24px", borderRadius: 8, border: "none",
              background: "#4a7c4e", color: "#fff", cursor: "pointer",
              fontSize: 14, fontWeight: 500,
            }}
          >
            Next
          </button>
        ) : (
          <button
            type="button"
            onClick={handleFinish}
            style={{
              padding: "10px 24px", borderRadius: 8, border: "none",
              background: "#4a7c4e", color: "#fff", cursor: "pointer",
              fontSize: 14, fontWeight: 600,
            }}
          >
            Finish Setup
          </button>
        )}
      </footer>

      <input ref={fileInputRef} type="file" accept="image/*,video/*" style={{ display: "none" }} onChange={handleUploadChange} />

      {/* Auth prompt modal */}
      {showAuthPrompt && (
        <div style={{
          position: "fixed", inset: 0, background: "rgba(0,0,0,0.5)", zIndex: 10000,
          display: "flex", alignItems: "center", justifyContent: "center",
        }} onClick={() => setShowAuthPrompt(false)}>
          <div style={{
            background: "#fff", borderRadius: 12, padding: 24, width: 340,
            boxShadow: "0 8px 32px rgba(0,0,0,0.2)",
          }} onClick={(e) => e.stopPropagation()}>
            <h3 style={{ margin: "0 0 8px", fontSize: 16 }}>Admin Login</h3>
            <p style={{ margin: "0 0 12px", fontSize: 13, color: "#6b7280" }}>
              Enter your admin token to upload files.
            </p>
            {authError && <p style={{ color: "#dc2626", fontSize: 12, margin: "0 0 8px" }}>{authError}</p>}
            <input
              type="password"
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
              style={{ width: "100%", padding: "10px 14px", borderRadius: 8, border: "1px solid #d1d5db", fontSize: 14 }}
            />
            <div style={{ display: "flex", gap: 8, marginTop: 12, justifyContent: "flex-end" }}>
              <button type="button" onClick={() => setShowAuthPrompt(false)}
                style={{ padding: "8px 16px", borderRadius: 8, border: "1px solid #d1d5db", background: "#fff", cursor: "pointer", fontSize: 13 }}>Cancel</button>
              <button
                type="button"
                onClick={() => {
                  if (!authInput.trim()) { setAuthError("Token required"); return; }
                  setAdminToken(authInput.trim());
                  setAuthInput("");
                  setAuthError(null);
                  setShowAuthPrompt(false);
                }}
                style={{ padding: "8px 16px", borderRadius: 8, border: "none", background: "#4a7c4e", color: "#fff", cursor: "pointer", fontSize: 13 }}
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
