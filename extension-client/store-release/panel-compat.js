/**
 * panel-compat.js — one API for the panel, whichever browser we are in.
 *
 * Chrome has chrome.sidePanel: per-tab, opened programmatically.
 * Firefox has sidebarAction: one global sidebar, and sidebarAction.open() is
 * only allowed while a real user gesture is still live. A click in a content
 * script reaches the background as a message, and that message does NOT carry
 * the gesture — so on Firefox we try the sidebar first and fall back to a small
 * popup window, which always works.
 *
 * Loaded in the background context only. Exposes self.Panel.
 */
(function () {
  const api = typeof browser !== "undefined" ? browser : chrome;

  const hasSidePanel = !!(api.sidePanel && api.sidePanel.setOptions);
  const hasSidebar = !!(api.sidebarAction && api.sidebarAction.open);

  let panelWindowId = null;

  async function openPanelWindow() {
    const url = api.runtime.getURL("sidepanel.html");
    if (panelWindowId != null) {
      try {
        await api.windows.update(panelWindowId, { focused: true });
        return { ok: true, via: "window" };
      } catch (_) {
        panelWindowId = null;      // it was closed behind our back
      }
    }
    try {
      const win = await api.windows.create({
        url,
        type: "popup",
        width: 460,
        height: 860,
      });
      panelWindowId = win.id;
      return { ok: true, via: "window" };
    } catch (e) {
      return { ok: false, error: (e && e.message) || String(e) };
    }
  }

  if (api.windows && api.windows.onRemoved) {
    api.windows.onRemoved.addListener((id) => {
      if (id === panelWindowId) panelWindowId = null;
    });
  }

  const Panel = {
    kind: hasSidePanel ? "sidePanel" : hasSidebar ? "sidebar" : "window",

    async init() {
      if (!hasSidePanel) return;
      // Deliberately NOT openPanelOnActionClick: that makes the panel a single
      // global instance Chrome keeps open on every tab, so it leaks onto
      // non-YouTube tabs and per-tab enabled:false cannot close it.
      try {
        await api.sidePanel.setPanelBehavior({ openPanelOnActionClick: false });
      } catch (_) {}
    },

    /** Chrome only: show/hide the panel per tab. No-op elsewhere. */
    async setEnabledForTab(tabId, enabled) {
      if (!hasSidePanel) return;
      try {
        if (enabled) {
          await api.sidePanel.setOptions({ tabId, path: "sidepanel.html", enabled: true });
        } else {
          await api.sidePanel.setOptions({ tabId, enabled: false });
        }
      } catch (_) {}
    },

    /**
     * NOT async, and nothing is awaited before sidePanel.open().
     *
     * Chrome only allows sidePanel.open() while the user's click is still
     * "live". Any await before it - even awaiting setOptions - ends that
     * window and Chrome rejects the call with "may only be called in response
     * to a user gesture", so the panel silently never opens. setOptions is
     * therefore fire-and-forget and open() is called on the same tick.
     */
    open(tabId) {
      if (hasSidePanel) {
        try {
          api.sidePanel.setOptions({ tabId, path: "sidepanel.html", enabled: true });
        } catch (_) {}
        try {
          return Promise.resolve(api.sidePanel.open({ tabId }))
            .then(() => ({ ok: true, via: "sidePanel" }))
            // Gesture lost anyway (or the API refused): still show the panel,
            // just in a window, rather than doing nothing at all.
            .catch((e) =>
              openPanelWindow().then((r) =>
                r.ok ? { ok: true, via: "window-fallback" } : { ok: false, error: (e && e.message) || String(e) }
              )
            );
        } catch (e) {
          return openPanelWindow();
        }
      }

      if (hasSidebar) {
        try {
          return Promise.resolve(api.sidebarAction.open())
            .then(() => ({ ok: true, via: "sidebar" }))
            .catch(() => openPanelWindow());
        } catch (_) {
          return openPanelWindow();
        }
      }
      return openPanelWindow();
    },
  };

  (typeof self !== "undefined" ? self : globalThis).Panel = Panel;
})();
