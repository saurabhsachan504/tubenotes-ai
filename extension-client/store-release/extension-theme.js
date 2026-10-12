/* Shared light/dark preference for every TubeNotes extension page. */
(function () {
  "use strict";
  const api = typeof browser !== "undefined" ? browser : chrome;
  const KEY = "tn_extension_theme";

  function normalize(theme) {
    return theme === "light" ? "light" : "dark";
  }

  function apply(theme) {
    const current = normalize(theme);
    document.documentElement.dataset.theme = current;
    document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
      const next = current === "dark" ? "light" : "dark";
      button.setAttribute("aria-label", `Switch to ${next} theme`);
      button.title = `Switch to ${next} theme`;
      button.setAttribute("aria-pressed", String(current === "light"));
    });
  }

  async function set(theme) {
    const current = normalize(theme);
    apply(current);
    try { await api.storage.local.set({ [KEY]: current }); } catch (_) {}
  }

  async function init() {
    let theme = "dark";
    try {
      const stored = await api.storage.local.get(KEY);
      theme = normalize(stored && stored[KEY]);
    } catch (_) {}
    apply(theme);
    document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
      button.addEventListener("click", () => {
        set(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
      });
    });
  }

  self.TubeNotesTheme = { init, set };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init, { once: true });
  else init();
})();
