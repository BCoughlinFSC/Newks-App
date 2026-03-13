import React, { useEffect, useRef, useState } from "react";
import newksLogo from "../../assets/newks-logo.svg";
import type { BrandingConfig } from "../types";

type Props = {
  locationName: string | null;
  demoMode: boolean;
  onOpenQuickSetup: () => void;
  onOpenSettings: () => void;
  branding?: BrandingConfig;
};

/**
 * BoardHeader — Clean professional header for the menu board.
 *
 * Admin buttons are hidden from customers. They can be revealed by:
 *   • Pressing Ctrl+Shift+S  (keyboard)
 *   • Tapping the logo area 5 times quickly  (touch)
 */
export function BoardHeader({
  locationName,
  demoMode,
  onOpenQuickSetup,
  onOpenSettings,
  branding,
}: Props) {
  const [now, setNow] = useState(() => new Date());
  const [adminVisible, setAdminVisible] = useState(false);
  const tapCount = useRef(0);
  const tapTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    const id = window.setInterval(() => setNow(new Date()), 60_000);
    return () => window.clearInterval(id);
  }, []);

  // Keyboard shortcut: Ctrl+Shift+S
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.ctrlKey && e.shiftKey && e.key === "S") {
        e.preventDefault();
        setAdminVisible((v) => !v);
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);

  function handleLogoTap() {
    tapCount.current += 1;
    if (tapTimer.current) clearTimeout(tapTimer.current);
    tapTimer.current = setTimeout(() => {
      tapCount.current = 0;
    }, 1500);
    if (tapCount.current >= 5) {
      tapCount.current = 0;
      setAdminVisible((v) => !v);
    }
  }

  const timeString = now.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const logoSrc =
    branding?.logoUrl && branding.logoUrl.trim().length > 0 ? branding.logoUrl : newksLogo;
  const titleText = "Newk's";
  const subtitleText = branding?.tagline ?? "Fresh-made, every day.";

  return (
    <header className="board-header">
      {/* Brand / logo — tap 5× to reveal admin */}
      <div
        className="board-brand"
        onClick={handleLogoTap}
        style={{ cursor: "default", userSelect: "none" }}
        aria-hidden="true"
      >
        <img className="board-logo" src={logoSrc} alt={titleText} />
        <div className="board-brand-text">
          <div className="board-title">{titleText}</div>
          <div className="board-subtitle">{subtitleText}</div>
        </div>
      </div>

      <div className="board-header-right">
        {/* Status pill — only show in demo mode or when admin is visible */}
        {(demoMode || adminVisible) && (
          <div className={`board-pill ${demoMode ? "demo" : "live"}`}>
            {demoMode ? "Demo Menu" : "Live Menu"}
          </div>
        )}

        {/* Location + time — subtle, always visible */}
        {locationName && (
          <div className="board-location-time">
            <div className="board-location">
              <span className="label">Location</span>
              <span className="value">{locationName}</span>
            </div>
            <div className="board-time">
              <span className="label">Time</span>
              <span className="value">{timeString}</span>
            </div>
          </div>
        )}

        {/* Admin buttons — hidden by default */}
        {adminVisible && (
          <>
            <button className="board-settings-btn" onClick={onOpenQuickSetup}>
              Quick Setup
            </button>
            <button className="board-settings-btn" onClick={onOpenSettings}>
              Settings
            </button>
            <button
              className="board-settings-btn"
              style={{ fontSize: 10, opacity: 0.6 }}
              onClick={() => setAdminVisible(false)}
            >
              ✕ Hide
            </button>
          </>
        )}
      </div>
    </header>
  );
}
