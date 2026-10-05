// Apply the stored theme before first paint; same key as the classic console.
// A file, not an inline script: the Console's Content-Security-Policy allows
// script from 'self' only.
try {
  var t = localStorage.getItem("pl_theme");
  if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
} catch (e) {}
