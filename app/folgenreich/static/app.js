const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const post = async (url, body) => {
  const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || 'Das hat nicht geklappt.');
  return r.json();
};

const currentTab = document.querySelector('.tabs [aria-current]');
if (currentTab) currentTab.scrollIntoView({ block: 'nearest', inline: 'center' });

/* Poster ohne Bild zeigen den Titel */
document.addEventListener('error', e => {
  if (e.target.tagName === 'IMG' && e.target.closest('.poster')) e.target.remove();
}, true);

let toastTimer;
function toast(text, action) {
  const t = document.getElementById('toast');
  t.textContent = text;
  if (action) {
    const b = document.createElement('button');
    b.textContent = action.label;
    b.onclick = () => { t.hidden = true; action.run(); };
    t.append(' ', b);
  }
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, action ? 7000 : 3500);
}

const place = { warten: 'wandert nach Warten', archiv: 'kommt ins Archiv' };

document.addEventListener('click', async e => {
  const next = e.target.closest('[data-next]');
  if (next) {
    next.disabled = true;
    const id = next.dataset.next;
    try {
      const d = await post(`/api/m/${id}/next`);
      if (next.hasAttribute('data-reload')) return location.reload();
      const card = next.closest('.card');
      if (d.html) card.outerHTML = d.html; else card.remove();
      const undo = async () => {
        await post(`/api/m/${id}/ep`, { season: Math.floor(d.undo / 100000), number: d.undo % 100000, on: false });
        location.reload();
      };
      toast(`${d.label} gesehen${place[d.tab] ? ', Serie ' + place[d.tab] : ''}.`, { label: 'Rückgängig', run: undo });
    } catch (err) { next.disabled = false; toast(err.message); }
    return;
  }
  const ep = e.target.closest('[data-ep]');
  if (ep) {
    const on = ep.getAttribute('aria-pressed') !== 'true';
    const media = ep.closest('[data-media]').dataset.media;
    await post(`/api/m/${media}/ep`, { season: +ep.dataset.s, number: +ep.dataset.ep, on });
    reloadAt(ep.dataset.s);
    return;
  }
  const until = e.target.closest('[data-until]');
  if (until) {
    const media = until.closest('[data-media]').dataset.media;
    await post(`/api/m/${media}/until`, { pos: +until.dataset.until });
    reloadAt(Math.floor(until.dataset.until / 100000));
    return;
  }
  const season = e.target.closest('[data-season]');
  if (season) {
    const media = season.closest('[data-media]').dataset.media;
    await post(`/api/m/${media}/season`, { season: +season.dataset.season, on: season.dataset.on === '1' });
    reloadAt(season.dataset.season);
    return;
  }
  const chip = e.target.closest('[data-sub]:not(a)');
  if (chip && chip.tagName === 'BUTTON') {
    $$('[data-sub]', chip.parentNode).forEach(c => c.setAttribute('aria-pressed', c === chip));
    applyFilter();
  }
});

function reloadAt(season) {
  const y = window.scrollY;
  const u = new URL(location.href);
  u.searchParams.set('s', season);
  sessionStorageSafe(y);
  location.replace(u);
}
function sessionStorageSafe(y) { try { sessionStorage.setItem('fr-y', y); } catch { /* egal */ } }
try {
  const y = sessionStorage.getItem('fr-y');
  if (y) { sessionStorage.removeItem('fr-y'); window.scrollTo(0, +y); }
} catch { /* egal */ }

document.addEventListener('submit', e => {
  const q = e.target.dataset.confirm;
  if (q && !confirm(q)) e.preventDefault();
});

function applyFilter() {
  const box = document.querySelector('[data-filter]');
  const q = (box ? box.value : '').trim().toLowerCase();
  const chip = document.querySelector('.chips [aria-pressed="true"]');
  const sub = chip ? chip.dataset.sub : '';
  $$('[data-list] > *').forEach(el => {
    const okText = !q || (el.dataset.title || '').toLowerCase().includes(q);
    const okSub = !sub || (el.dataset.sub || '').split('|').includes(sub);
    el.hidden = !(okText && okSub);
  });
}
document.addEventListener('input', e => { if (e.target.matches('[data-filter]')) applyFilter(); });

/* Buchfoto: Wenn der Browser Strichcodes selbst lesen kann, geht das schneller */
const photo = document.getElementById('book-photo');
if (photo) photo.addEventListener('change', async () => {
  const file = photo.files[0];
  if (!file) return;
  const out = document.getElementById('photo-result');
  out.innerHTML = '<p class="lead">Foto wird gelesen …</p>';
  const form = new FormData();
  try {
    if ('BarcodeDetector' in window && !photo.dataset.url) {
      const hit = await new BarcodeDetector({ formats: ['ean_13'] }).detect(await createImageBitmap(file));
      if (hit[0]) form.set('isbn', hit[0].rawValue);
    }
  } catch { /* der Server versucht es selbst */ }
  if (!form.has('isbn')) form.set('photo', file);
  try {
    const r = await fetch(photo.dataset.url || '/api/buch-foto', { method: 'POST', body: form });
    if (r.ok) out.innerHTML = await r.text();
    else out.innerHTML = '';
    if (!r.ok) toast((await r.json()).error);
  } catch { out.innerHTML = ''; toast('Der Server ist gerade nicht erreichbar.'); }
  photo.value = '';
});
