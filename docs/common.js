// Shared helpers for all pages
const PAGES = [["index.html", "Analysis"], ["explore.html", "Explore"], ["bestfit.html", "Best fit"], ["season.html", "Summer guide"], ["forecast.html", "PL forecast"], ["map.html", "Station map"], ["briefing.html", "Briefing"]];
function navBar() {
  const here = location.pathname.split("/").pop() || "index.html";
  const n = document.createElement("nav");
  n.className = "top";
  n.innerHTML = `<span class="brand">Southwest PL</span>` +
    PAGES.map(([h, t]) => `<a href="${h}" class="${h === here ? "on" : ""}">${t}</a>`).join("");
  document.body.prepend(n);
}
async function getJSON(u) { const r = await fetch(u, { cache: "no-store" }); if (!r.ok) throw new Error(u + " " + r.status); return r.json(); }
async function getText(u) { const r = await fetch(u, { cache: "no-store" }); if (!r.ok) throw new Error(u + " " + r.status); return r.text(); }
function parseCSV(t) {
  const lines = t.trim().split(/\r?\n/); const head = lines[0].split(",");
  return lines.slice(1).map(l => { const v = l.split(","); const o = {}; head.forEach((h, i) => o[h] = v[i]); return o; });
}
const fmt = (x, d = 0) => x == null || x === "" || isNaN(x) ? "–" : (+x).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d });
const clsBadge = c => c == null ? "–" : `<span class="cls cls${c}">${c === 45 ? "<50" : c + "th"}</span>`;
const plBadge = p => p == null ? "–" : `<span class="plb plb${p}">PL ${p}</span>`;
const CAL_SVG = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/></svg>`;
// date box: <span class="datebox" data-for="id"></span> -> date input + calendar icon + prev/next
function dateBox(holder, min, max, value, onChange) {
  holder.innerHTML = `<button class="icon" title="Previous day">‹</button><input type="date" min="${min}" max="${max}" value="${value}"><button class="icon cal" title="Pick a date">${CAL_SVG}</button><button class="icon" title="Next day">›</button>`;
  const [prev, input, cal, next] = holder.children;
  const set = d => { if (d < min) d = min; if (d > max) d = max; input.value = d; onChange(d); };
  const shift = k => { const t = new Date(input.value + "T12:00"); t.setDate(t.getDate() + k); set(t.toISOString().slice(0, 10)); };
  prev.onclick = () => shift(-1); next.onclick = () => shift(1);
  cal.onclick = () => { try { input.showPicker(); } catch (e) { input.focus(); } };
  input.onchange = () => set(input.value);
  return { set, get: () => input.value };
}
function download(name, text, type = "text/csv") {
  const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([text], { type })); a.download = name; a.click();
}
