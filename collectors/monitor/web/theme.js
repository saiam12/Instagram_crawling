"use strict";
(() => {
  let dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  try {
    const saved = localStorage.getItem("collector-monitor-theme");
    if (saved === "dark" || saved === "light") dark = saved === "dark";
  } catch {}
  const apply = () => { document.documentElement.dataset.theme = dark ? "dark" : "light"; };
  apply();
  document.addEventListener("DOMContentLoaded", () => {
    const button = document.getElementById("theme-toggle");
    button.setAttribute("aria-pressed", String(dark));
    button.addEventListener("click", () => {
      dark = !dark;
      apply();
      button.setAttribute("aria-pressed", String(dark));
      try { localStorage.setItem("collector-monitor-theme", dark ? "dark" : "light"); } catch {}
    });
  });
})();
