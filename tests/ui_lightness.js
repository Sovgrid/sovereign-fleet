// Coordinator review aid (UI-REDESIGN "Lightness"): paste into the browser console on the preview at 390 px.
// Prints visible words, controls and length per screen with nothing opened, against the RFC's targets.
(async () => {
  const T = { home: [60, 8, 1], schedule: [80, 10, 1.3], appearance: [60, 16, 1.5], task: [60, 4, 1.2], approval: [70, 4, 1] };
  const wait = () => new Promise(r => setTimeout(r, 80));
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && !e.closest("details:not([open]) > :not(summary)"); };
  const rows = [];
  for (const sec of document.querySelectorAll("[data-screen]")) {
    location.hash = "#" + sec.dataset.screen; await wait();
    let words = 0; const w = document.createTreeWalker(sec, NodeFilter.SHOW_TEXT); let n;
    while ((n = w.nextNode())) if (n.parentElement && vis(n.parentElement)) words += n.textContent.trim().split(/\s+/).filter(Boolean).length;
    // a folded row (<details class="fold">) is a list item, not a control
    const controls = [...sec.querySelectorAll("button,a.btn,select,input,summary:not(.fold > summary)")].filter(vis).length;
    const len = Math.round(sec.scrollHeight / innerHeight * 10) / 10;
    const t = T[sec.dataset.screen] || [60, 10, 1.2];
    rows.push({ screen: sec.dataset.screen, words, controls, len, ok: words <= t[0] && controls <= t[1] && len <= t[2] });
  }
  console.table(rows); return rows;
})();
