'use strict';
// A self-contained iframe client: no external JS or SDK required.
// Flow: camera -> detect card inside guide -> hold 2 s -> auto capture -> quality check
// -> OCR -> (front) flip animation -> back -> finish. Bad captures are discarded and retried.
const $ = (s) => document.querySelector(s);
const ui = {
  intro: $('#intro'), capture: $('#capture'), result: $('#result'),
  video: $('#video'), guide: $('#guide'), card: $('#card3d'), holdBar: $('#holdBar'),
  busy: $('#busy'), busyText: $('#busyText'), help: $('#cameraHelp'),
  notice: $('#notice'), cameraState: $('#cameraState'), retry: $('#retryCamera'), manual: $('#manualCapture')
};
const token = new URLSearchParams(location.hash.slice(1)).get('token');
if (token) history.replaceState({}, '', '/embed/'); // fragment is never sent to HTTP servers

const HOLD_MS = 2000;          // card must stay detected this long before capturing
const SCAN_MS = 150;           // detection interval
const CARD_RATIO = 85.6 / 54;  // ID-1 card format
const CAPTURE_MARGIN = 0.05;   // extra border around the guide so the backend can find the edges
const MAX_ATTEMPTS = 3;        // every capture counts; after this the best read is accepted and flagged for review
const HOLD_RELAXED_MS = 900;   // hold time once the user has been struggling (fewer demands)
const MANUAL_AFTER_MS = 4000;  // show the manual "take photo" button after this long without a capture
const FORCE_AFTER_MS = 9000;   // capture anyway (if the phone is steady) after this long without edge detection
const BURST = 4;               // frames grabbed per capture; the sharpest one is sent
const BURST_GAP_MS = 120;
const WARNING_TEXT = {
  BLURRY: 'La foto salió borrosa. Mantén la cédula quieta.',
  TOO_DARK: 'Hay poca luz. Busca un lugar más iluminado.',
  TOO_BRIGHT: 'Hay demasiada luz sobre la cédula.',
  POSSIBLE_GLARE: 'Detectamos reflejos. Inclina un poco la cédula.'
};
const SIDE_TEXT = {
  front: { step: '1 de 2 · Frente', help: 'Ubica el frente de tu cédula dentro de la silueta.' },
  back: { step: '2 de 2 · Reverso', help: 'Ahora ubica el reverso de tu cédula dentro de la silueta.' }
};

let context = null, stream = null, step = 'front', busy = false;
let scanTimer = null, detectedSince = 0, lastGray = null, scanStartedAt = 0;
const attempts = { front: 0, back: 0 };
let steadySince = 0;
const scanCanvas = document.createElement('canvas');
const scanCtx = scanCanvas.getContext('2d', { willReadFrequently: true });
const eventPrefix = 'IDS_';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
function show(section) { for (const id of ['intro', 'capture', 'result']) ui[id].classList.toggle('hidden', id !== section); }
function notice(message) { ui.notice.textContent = message || ''; }
function help(message) { ui.help.textContent = message; }
function setBusy(text) { ui.busy.classList.toggle('hidden', !text); if (text) ui.busyText.textContent = text; }
function setHold(fraction) { ui.holdBar.style.width = Math.round(Math.max(0, Math.min(1, fraction)) * 100) + '%'; }
function setGuideState(state) { ui.guide.classList.remove('detected', 'ok', 'bad'); if (state) ui.guide.classList.add(state); }

function notify(type, details = {}) {
  if (!context) return;
  if (type === 'ERROR') api('events', 'POST', JSON.stringify({ code: details.code, side: details.side || null })).catch(() => {});
  // Always use backend-verified exact origin. Never use '*' as targetOrigin.
  window.parent.postMessage({ source: 'IDS', type: eventPrefix + type, sessionId: context.session_id, ...details }, context.parent_origin);
}

class ApiError extends Error { constructor(status) { super('HTTP ' + status); this.status = status; } }
async function api(path, method = 'GET', body = null) {
  const headers = { Authorization: 'Bearer ' + token };
  if (body && !(body instanceof FormData)) headers['Content-Type'] = 'application/json';
  const response = await fetch('/api/v1/embed/' + path, { method, headers, body, cache: 'no-store' });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new ApiError(response.status);
  return payload;
}
// Users only see friendly messages; technical details never reach the UI.
function friendlyError(e) {
  if (e instanceof ApiError && e.status === 401) return 'Tu sesión expiró. Solicita un nuevo enlace de captura.';
  return 'No pudimos procesar la foto. Intentémoslo de nuevo.';
}

async function boot() {
  try {
    if (!token) throw new Error('missing token');
    context = await api('session');
    $('#start').disabled = false;
    notify('READY', { expiresAt: context.expires_at });
  } catch (e) { notice('Sesión inválida o expirada. Solicita un nuevo enlace de captura.'); }
}

async function startCamera() {
  try {
    stopCamera();
    ui.retry.classList.add('hidden');
    if (!navigator.mediaDevices?.getUserMedia) throw Object.assign(new Error(), { name: 'Unsupported' });
    stream = await navigator.mediaDevices.getUserMedia({ audio: false, video: { facingMode: { ideal: 'environment' }, width: { ideal: 1920 }, height: { ideal: 1080 } } });
    try { // continuous autofocus where supported (most blurry shots come from a stuck focus)
      const track = stream.getVideoTracks()[0], caps = track.getCapabilities ? track.getCapabilities() : {};
      if (caps.focusMode && caps.focusMode.includes('continuous')) await track.applyConstraints({ advanced: [{ focusMode: 'continuous' }] });
    } catch (_) { /* optional */ }
    ui.video.srcObject = stream;
    await ui.video.play();
    ui.cameraState.textContent = 'Cámara activa';
    show('capture'); notice('');
    enterSide(step, false);
  } catch (e) {
    show('capture');
    ui.cameraState.textContent = '';
    ui.retry.classList.remove('hidden');
    notice(e.name === 'NotAllowedError' ? 'Debes permitir el acceso a la cámara para continuar.'
      : e.name === 'Unsupported' ? 'Tu navegador no permite usar la cámara en esta página.'
      : 'No pudimos iniciar la cámara. Intenta de nuevo.');
    notify('ERROR', { code: 'CAMERA_ERROR' });
  }
}
function stopCamera() {
  stopScan();
  if (stream) { for (const track of stream.getTracks()) track.stop(); stream = null; }
  ui.video.srcObject = null;
}

function enterSide(side, announce = true) {
  step = side;
  $('#step').textContent = SIDE_TEXT[side].step;
  help(SIDE_TEXT[side].help);
  ui.card.classList.toggle('flipped', side === 'back');
  if (announce || side === 'front') notify('CAPTURE_STARTED', { side });
  startScan();
}

// ---------- Card detection (client side, lightweight) ----------
// Maps the on-screen guide to video pixel coordinates (video uses object-fit: cover).
function guideRectInVideo(margin = 0) {
  const v = ui.video, vr = v.getBoundingClientRect(), gr = ui.guide.getBoundingClientRect();
  const vw = v.videoWidth, vh = v.videoHeight;
  const scale = Math.max(vr.width / vw, vr.height / vh);
  const ox = (vr.width - vw * scale) / 2, oy = (vr.height - vh * scale) / 2;
  let x = (gr.left - vr.left - ox) / scale, y = (gr.top - vr.top - oy) / scale;
  let w = gr.width / scale, h = gr.height / scale;
  x -= w * margin; y -= h * margin; w *= 1 + 2 * margin; h *= 1 + 2 * margin;
  const x0 = Math.max(0, x), y0 = Math.max(0, y);
  return { x: x0, y: y0, w: Math.min(vw, x + w) - x0, h: Math.min(vh, y + h) - y0 };
}

function startScan() { stopScan(); steadySince = 0; ui.manual.classList.add('hidden'); detectedSince = 0; lastGray = null; scanStartedAt = Date.now(); setHold(0); setGuideState(null); scanTimer = setInterval(scanFrame, SCAN_MS); }
function stopScan() { if (scanTimer) clearInterval(scanTimer); scanTimer = null; }

function scanFrame() {
  if (busy || !stream || !ui.video.videoWidth) return;
  const margin = 0.18;
  const r = guideRectInVideo(margin);
  if (r.w < 50 || r.h < 50) return;
  const W = 220, H = Math.round(W * r.h / r.w);
  scanCanvas.width = W; scanCanvas.height = H;
  scanCtx.drawImage(ui.video, r.x, r.y, r.w, r.h, 0, 0, W, H);
  const px = scanCtx.getImageData(0, 0, W, H).data;
  const gray = new Float32Array(W * H);
  for (let i = 0, j = 0; i < gray.length; i++, j += 4) gray[i] = px[j] * .299 + px[j + 1] * .587 + px[j + 2] * .114;

  // Expected guide edges inside the analysis window. The window may be clipped by the
  // video bounds (guide close to the screen edge on phones), so map the real guide rect.
  const g = guideRectInVideo(0), sx = W / r.w, sy = H / r.h;
  const gx0 = (g.x - r.x) * sx, gx1 = (g.x + g.w - r.x) * sx;
  const gy0 = (g.y - r.y) * sy, gy1 = (g.y + g.h - r.y) * sy;
  const tolX = (gx1 - gx0) * .1, tolY = (gy1 - gy0) * .13, T = 22;
  const vertical = (cx) => edgeScore(gray, W, H, cx, tolX, gy0, gy1, true, T);
  const horizontal = (cy) => edgeScore(gray, W, H, cy, tolY, gx0, gx1, false, T);
  const sides = [vertical(gx0), vertical(gx1), horizontal(gy0), horizontal(gy1)].filter((s) => s > .42).length;

  let motion = 0;
  if (lastGray && lastGray.length === gray.length) {
    for (let i = 0; i < gray.length; i += 3) motion += Math.abs(gray[i] - lastGray[i]);
    motion /= gray.length / 3;
  }
  lastGray = gray;

  const now = Date.now(), elapsed = now - scanStartedAt;
  const steady = motion < 14;
  // Struggling users (earlier failed attempts or a long wait) get relaxed requirements.
  const struggling = attempts[step] > 0 || elapsed > MANUAL_AFTER_MS;
  const found = sides >= (struggling ? 2 : 3);
  const holdMs = struggling ? HOLD_RELAXED_MS : HOLD_MS;
  if (steady) { if (!steadySince) steadySince = now; } else steadySince = 0;
  if (elapsed > MANUAL_AFTER_MS) ui.manual.classList.remove('hidden');

  if (found && steady) {
    if (!detectedSince) detectedSince = now;
    const progress = (now - detectedSince) / holdMs;
    setGuideState('detected'); setHold(progress);
    help('¡Bien! Mantén la cédula quieta…');
    if (progress >= 1) autoCapture();
  } else if (elapsed > FORCE_AFTER_MS && steadySince && now - steadySince > 1200) {
    // Edge detection can fail on light tables or textured cards: do not trap the user.
    autoCapture();
  } else {
    detectedSince = 0; setHold(0); setGuideState(null);
    if (found) help('Mantén la cédula quieta.');
    else if (elapsed > MANUAL_AFTER_MS) help('Llena la silueta con la cédula y mantén el celular quieto, o toca «Tomar foto ahora».');
    else help(SIDE_TEXT[step].help);
  }
}

// Fraction of the expected edge line covered by strong gradient pixels in the right direction.
function edgeScore(g, W, H, center, tol, from, to, isVertical, T) {
  let best = 0;
  const a = Math.round(from + (to - from) * .1), b = Math.round(to - (to - from) * .1);
  const lo = Math.max(1, Math.round(center - tol)), hi = Math.min((isVertical ? W : H) - 2, Math.round(center + tol));
  for (let c = lo; c <= hi; c++) {
    let hits = 0, total = 0;
    for (let t = a; t <= b; t++) {
      const i = isVertical ? t * W + c : c * W + t;
      const dx = Math.abs(g[i + 1] - g[i - 1]), dy = Math.abs(g[i + W] - g[i - W]);
      const d = isVertical ? dx : dy, other = isVertical ? dy : dx;
      if (d > T && d > other) hits++;
      total++;
    }
    if (total && hits / total > best) best = hits / total;
  }
  return best;
}

// ---------- Capture & processing ----------
function sharpness(canvas) {
  const w = 240, h = Math.max(1, Math.round(240 * canvas.height / canvas.width));
  scanCanvas.width = w; scanCanvas.height = h;
  scanCtx.drawImage(canvas, 0, 0, w, h);
  const d = scanCtx.getImageData(0, 0, w, h).data, g = new Float32Array(w * h);
  for (let i = 0, j = 0; i < g.length; i++, j += 4) g[i] = d[j] * .299 + d[j + 1] * .587 + d[j + 2] * .114;
  let sum = 0, sq = 0, n = 0;
  for (let y = 1; y < h - 1; y++) for (let x = 1; x < w - 1; x++) {
    const i = y * w + x, lap = 4 * g[i] - g[i - 1] - g[i + 1] - g[i - w] - g[i + w];
    sum += lap; sq += lap * lap; n++;
  }
  return sq / n - (sum / n) ** 2; // variance of Laplacian
}

// Grab a short burst and keep the sharpest frame: a single frame is often caught mid-refocus.
async function grabImage() {
  let best = null, bestScore = -1;
  for (let i = 0; i < BURST; i++) {
    const r = guideRectInVideo(CAPTURE_MARGIN);
    const canvas = document.createElement('canvas');
    canvas.width = Math.round(r.w); canvas.height = Math.round(r.h);
    canvas.getContext('2d').drawImage(ui.video, r.x, r.y, r.w, r.h, 0, 0, canvas.width, canvas.height);
    const score = sharpness(canvas);
    if (score > bestScore) { best = canvas; bestScore = score; }
    if (i < BURST - 1) await sleep(BURST_GAP_MS);
  }
  return new Promise((resolve) => best.toBlob(resolve, 'image/jpeg', .95));
}

async function retryCapture(message) {
  setGuideState('bad'); setBusy(null); notice(message);
  await sleep(1500);
  setGuideState(null); notice('');
  startScan();
}

async function autoCapture() {
  if (busy) return;
  busy = true; stopScan();
  const side = step;
  try {
    setGuideState('ok'); setBusy('Verificando imagen…');
    let blob = await grabImage();
    if (!blob) return await retryCapture('No pudimos tomar la foto. Intentémoslo de nuevo.');

    // The OCR result is the judge of a good photo; image statistics only explain a failure.
    setBusy('Leyendo documento…');
    const fd = new FormData(); fd.append('image', blob, 'capture.jpg');
    const output = await api('extract?side=' + side, 'POST', fd);
    blob = null;
    attempts[side]++;
    if (!output.usable && attempts[side] < MAX_ATTEMPTS) {
      const reason = (output.quality?.warnings || []).find((w) => WARNING_TEXT[w]);
      return await retryCapture(reason ? WARNING_TEXT[reason] : (side === 'front'
        ? 'No logramos leer el frente. Acerca la cédula hasta llenar la silueta.'
        : 'No logramos leer el reverso. Acerca la cédula hasta llenar la silueta.'));
    }
    notify('SIDE_PROCESSED', { side, status: output.status, quality: output.quality.acceptable });

    if (side === 'front') {
      setBusy(null); notice('');
      await flipToBack();
      enterSide('back');
    } else {
      setBusy('Finalizando…');
      const completed = await api('finish', 'POST');
      stopCamera(); setBusy(null); notice('');
      show('result');
      // Host receives extracted data only for verified parent origin. Data is never rendered here.
      notify('COMPLETE', { status: completed.status, data: completed.data });
    }
  } catch (e) {
    notify('ERROR', { code: 'PROCESSING_ERROR', side });
    if (e instanceof ApiError && e.status === 401) { stopCamera(); setBusy(null); notice(friendlyError(e)); return; }
    await retryCapture(friendlyError(e));
  } finally {
    busy = false;
  }
}

async function flipToBack() {
  $('#step').textContent = SIDE_TEXT.back.step;
  help('¡Frente listo! Ahora voltea tu cédula.');
  setGuideState('ok');
  ui.guide.classList.add('flipping');
  await sleep(50);
  ui.card.classList.add('flipped');
  await sleep(1400);
  ui.guide.classList.remove('flipping');
  setGuideState(null);
  await sleep(600); // give the user a moment to turn the physical card
}

ui.manual.addEventListener('click', () => { if (!busy) autoCapture(); });
$('#start').addEventListener('click', startCamera);
$('#retryCamera').addEventListener('click', startCamera);
window.addEventListener('pagehide', stopCamera);
boot();
