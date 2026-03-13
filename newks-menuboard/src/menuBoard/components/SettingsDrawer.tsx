import React, { useEffect, useMemo, useState } from "react";
import type { BoardConfig, BoardViewModel } from "../types";

type Props = {
  boardConfig: BoardConfig;
  viewModel: BoardViewModel;
  onChange: (next: BoardConfig) => void;
  onClose: () => void;
  onResetLocation?: () => void;
};

export function SettingsDrawer({ boardConfig, viewModel, onChange, onClose, onResetLocation }: Props) {
  const [draft, setDraft] = useState<BoardConfig>(boardConfig);
  const [expandedCategoryIds, setExpandedCategoryIds] = useState<Set<string>>(new Set());

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.preventDefault(); onClose(); }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);

  const itemsByCategoryId = useMemo(() => {
    const map = new Map<string, typeof viewModel.menu.items>();
    for (const item of viewModel.menu.items) {
      const list = map.get(item.categoryId) ?? [];
      list.push(item);
      map.set(item.categoryId, list);
    }
    return map;
  }, [viewModel.menu.items]);

  function applyAndClose() {
    onChange(draft);
    onClose();
  }

  function toggleCategoryExpanded(categoryId: string) {
    setExpandedCategoryIds((prev) => {
      const next = new Set(prev);
      if (next.has(categoryId)) next.delete(categoryId);
      else next.add(categoryId);
      return next;
    });
  }

  function updateItemHidden(itemId: string, hidden: boolean) {
    setDraft((prev) => ({
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

  function updateCategoryHidden(categoryId: string, hidden: boolean) {
    setDraft((prev) => ({
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

  function moveCategory(categoryId: string, direction: -1 | 1) {
    setDraft((prev) => {
      const ids =
        prev.categoryOrder.length > 0
          ? [...prev.categoryOrder]
          : viewModel.menu.categories.map((c) => c.id);
      const index = ids.indexOf(categoryId);
      if (index === -1) return prev;
      const nextIndex = index + direction;
      if (nextIndex < 0 || nextIndex >= ids.length) return prev;
      const updated = [...ids];
      const [removed] = updated.splice(index, 1);
      updated.splice(nextIndex, 0, removed);
      return {
        ...prev,
        categoryOrder: updated,
      };
    });
  }

  return (
    <div className="board-settings-backdrop" role="dialog" aria-modal="true">
      <div className="board-settings">
        <div className="board-settings-header">
          <h3>Board Settings</h3>
          <button className="board-settings-close" onClick={onClose}>
            ✕
          </button>
        </div>
        <div className="board-settings-body">
          <section className="board-settings-section">
            <h4>Display</h4>
            <div className="board-settings-row">
              <label>
                Theme
                <select
                  className="board-settings-number"
                  value={draft.theme}
                  onChange={(e) =>
                    setDraft({
                      ...draft,
                      theme: e.target.value === "light" ? "light" : "modernDark",
                    })
                  }
                >
                  <option value="modernDark">Dark</option>
                  <option value="light">Light</option>
                </select>
              </label>
            </div>
            <div className="board-settings-row">
              <label>
                <input
                  type="checkbox"
                  checked={draft.showPrices}
                  onChange={(e) => setDraft({ ...draft, showPrices: e.target.checked })}
                />
                Show prices
              </label>
            </div>
            <div className="board-settings-row">
              <label>
                <input
                  type="checkbox"
                  checked={draft.showDescriptions}
                  onChange={(e) => setDraft({ ...draft, showDescriptions: e.target.checked })}
                />
                Show item descriptions
              </label>
            </div>
            <div className="board-settings-row">
              <label>
                Auto-cycle every{" "}
                <input
                  type="number"
                  min={0}
                  className="board-settings-number"
                  value={draft.autoCycleSeconds}
                  onChange={(e) =>
                    setDraft({
                      ...draft,
                      autoCycleSeconds: Math.max(0, Number(e.target.value) || 0),
                    })
                  }
                />{" "}
                seconds (0 to disable)
              </label>
            </div>
          </section>

          {onResetLocation && (
            <section className="board-settings-section">
              <h4>Location</h4>
              <div className="board-settings-row">
                <span>Reset this screen to the location selection step.</span>
                <button
                  type="button"
                  className="board-settings-btn"
                  onClick={onResetLocation}
                >
                  Change location
                </button>
              </div>
            </section>
          )}

          <section className="board-settings-section">
            <h4>Categories & items</h4>
            <div className="board-settings-category-list">
              {viewModel.menu.categories.map((cat) => {
                const cfg = draft.categoryConfigs[cat.id];
                const hidden = cfg?.hidden ?? false;
                const expanded = expandedCategoryIds.has(cat.id);
                const items = itemsByCategoryId.get(cat.id) ?? [];
                return (
                  <div key={cat.id} className="board-settings-category-block">
                    <div className="board-settings-category-row">
                      <button
                        type="button"
                        className="board-settings-expand-btn"
                        onClick={() => toggleCategoryExpanded(cat.id)}
                        aria-expanded={expanded}
                      >
                        {expanded ? "▼" : "▶"}
                      </button>
                      <label className="board-settings-category-label">
                        <input
                          type="checkbox"
                          checked={!hidden}
                          onChange={(e) => updateCategoryHidden(cat.id, !e.target.checked)}
                        />
                        {cat.name}
                      </label>
                      <div className="board-settings-category-actions">
                        <button
                          className="board-settings-chip"
                          type="button"
                          onClick={() => moveCategory(cat.id, -1)}
                        >
                          ↑
                        </button>
                        <button
                          className="board-settings-chip"
                          type="button"
                          onClick={() => moveCategory(cat.id, 1)}
                        >
                          ↓
                        </button>
                      </div>
                    </div>
                    {expanded && (
                      <div className="board-settings-item-list">
                        {items.map((item) => {
                          const itemCfg = draft.itemConfigs[item.id];
                          const itemHidden = itemCfg?.hidden ?? false;
                          return (
                            <label key={item.id} className="board-settings-item-row">
                              <input
                                type="checkbox"
                                checked={!itemHidden}
                                onChange={(e) => updateItemHidden(item.id, !e.target.checked)}
                              />
                              <span className="board-settings-item-name">{item.name}</span>
                            </label>
                          );
                        })}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </section>
        </div>
        <div className="board-settings-footer">
          <button className="board-settings-btn ghost" onClick={onClose}>
            Cancel
          </button>
          <button className="board-settings-btn primary" onClick={applyAndClose}>
            Save changes
          </button>
        </div>
      </div>
    </div>
  );
}

