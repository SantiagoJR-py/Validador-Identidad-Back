"""Standalone, embeddable Colombian ID OCR MVP.

The embed token is short-lived and bound to one allowed parent origin. The server
never trusts client-supplied origin values when delivering postMessage events.
"""
from __future__ import annotations

import base64
import difflib
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from importlib.util import find_spec
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import cv2
import numpy as np
import sqlalchemy as sa
from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from PIL import Image, ImageOps, UnidentifiedImageError

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / 'frontend'
# MySQL by default. Tests/dev may point IDS_DATABASE_URL to e.g. sqlite:///file.db
DATABASE_URL = os.getenv('IDS_DATABASE_URL', 'mysql+pymysql://santi:123@127.0.0.1:3306/validation_identity?charset=utf8mb4')
API_KEY = os.getenv('IDS_HOST_API_KEY', '')
SIGNING_KEY = os.getenv('IDS_SIGNING_KEY', '')
ALLOWED_ORIGINS = [x.strip().rstrip('/') for x in os.getenv('IDS_ALLOWED_ORIGINS', 'http://localhost:4200,http://localhost:8080').split(',') if x.strip()]
MAX_BYTES = int(os.getenv('IDS_MAX_IMAGE_BYTES', str(8 * 1024 * 1024)))
RESULT_TTL = int(os.getenv('IDS_RESULT_TTL_SECONDS', '900'))
SESSION_TTL = min(900, int(os.getenv('IDS_SESSION_TTL_SECONDS', '600')))
ENABLE_DEV_OCR_FALLBACK = os.getenv('IDS_DEV_TESSERACT_FALLBACK', 'true').lower() == 'true'
LOG = logging.getLogger('ids')
logging.basicConfig(level=os.getenv('IDS_LOG_LEVEL', 'INFO'), format='%(asctime)s %(levelname)s %(message)s')
OCR_ENGINE = None
OCR_STATE = 'not_initialized'
OCR_LOCK = threading.Lock()  # OCR engines are not guaranteed to be re-entrant


META = sa.MetaData()
# Operational log: one row per event of a session (no document number, no OCR text).
EVENTS = sa.Table('events', META,
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer, 'sqlite'), primary_key=True, autoincrement=True),
    sa.Column('session_id', sa.String(32), nullable=False),
    sa.Column('event_type', sa.String(40), nullable=False),
    sa.Column('detail', sa.String(180), nullable=False, server_default=''),
    sa.Column('created_at', sa.DateTime, nullable=False),
    sa.Index('idx_events_session', 'session_id', 'id'))
# Short-lived encrypted working copy used while the user is capturing (TTL).
RESULTS = sa.Table('results', META,
    sa.Column('session_id', sa.String(32), primary_key=True),
    sa.Column('data', sa.LargeBinary(16 * 1024 * 1024), nullable=False),
    sa.Column('created_at', sa.Integer, nullable=False))
# Permanent record of each person's process: queryable columns + the full extraction as JSON.
RECORDS = sa.Table('identity_records', META,
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer, 'sqlite'), primary_key=True, autoincrement=True),
    sa.Column('session_id', sa.String(32), nullable=False, unique=True),
    sa.Column('external_reference', sa.String(100)),
    sa.Column('parent_origin', sa.String(255)),
    sa.Column('status', sa.String(30), nullable=False),  # STARTED | COMPLETED
    sa.Column('started_at', sa.DateTime, nullable=False),
    sa.Column('completed_at', sa.DateTime),
    sa.Column('document_type', sa.String(40)),
    sa.Column('document_number', sa.String(20), index=True),
    sa.Column('name_candidate', sa.String(255)),
    sa.Column('surnames', sa.String(255)),
    sa.Column('given_names', sa.String(255)),
    sa.Column('date_candidates', sa.JSON),
    sa.Column('front_usable', sa.Boolean),
    sa.Column('back_usable', sa.Boolean),
    sa.Column('fields_need_review', sa.Boolean),
    sa.Column('extracted_json', sa.JSON))
_ENGINE = None


def engine():
    global _ENGINE
    if _ENGINE is None or str(_ENGINE.url) != DATABASE_URL:
        _ENGINE = sa.create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=1800)
    return _ENGINE


def now_utc():
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def setup_db():
    META.create_all(engine())
    # create_all never alters existing tables: add columns introduced after the first deploy.
    have = {c['name'] for c in sa.inspect(engine()).get_columns('identity_records')}
    with engine().begin() as db:
        for col in RECORDS.columns:
            if col.name not in have:
                db.execute(sa.text(f'ALTER TABLE identity_records ADD COLUMN {col.name} {col.type.compile(engine().dialect)} NULL'))


def db_event(sid: str, event_type: str, detail: str = ''):
    # Never log card image, document number or OCR text.
    with engine().begin() as db:
        db.execute(EVENTS.insert().values(session_id=sid, event_type=event_type[:40], detail=detail[:180], created_at=now_utc()))


def crypto():
    from cryptography.fernet import Fernet
    # Enforce separate, secret, stable signing key; derive encryption key for MVP.
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256((SIGNING_KEY + ':results:v1').encode()).digest()))


def save_result(sid: str, data: dict):
    payload = crypto().encrypt(json.dumps(data, ensure_ascii=False).encode())
    with engine().begin() as db:
        db.execute(RESULTS.delete().where(RESULTS.c.session_id == sid))
        db.execute(RESULTS.insert().values(session_id=sid, data=payload, created_at=int(time.time())))


def load_result(sid: str):
    with engine().begin() as db:
        db.execute(RESULTS.delete().where(RESULTS.c.created_at < int(time.time()) - RESULT_TTL))
        row = db.execute(sa.select(RESULTS.c.data).where(RESULTS.c.session_id == sid)).first()
    return json.loads(crypto().decrypt(row[0])) if row else None


def start_record(sid: str, origin: str, external_reference: str | None):
    with engine().begin() as db:
        db.execute(RECORDS.insert().values(session_id=sid, external_reference=external_reference, parent_origin=origin,
                                           status='STARTED', started_at=now_utc()))


def complete_record(sid: str, data: dict):
    """Persist what was extracted. ocr_lines (raw text) are left out; parsed fields and quality are kept."""
    slim = {side: {k: v for k, v in d.items() if k != 'ocr_lines'} for side, d in data.items()}
    f, b = slim['front']['fields'], slim['back']['fields']
    pick = lambda key: f.get(key) or b.get(key)
    values = dict(status='COMPLETED', completed_at=now_utc(),
                  document_type=next((t for t in (f.get('document_type'), b.get('document_type')) if t and t != 'UNKNOWN'), 'UNKNOWN'),
                  document_number=pick('document_number'), name_candidate=pick('name_candidate'),
                  surnames=pick('surnames'), given_names=pick('given_names'),
                  date_candidates=sorted(set(f.get('date_candidates', []) + b.get('date_candidates', []))),
                  front_usable=bool(slim['front'].get('usable')), back_usable=bool(slim['back'].get('usable')),
                  fields_need_review=True, extracted_json=slim)
    with engine().begin() as db:
        if not db.execute(RECORDS.update().where(RECORDS.c.session_id == sid).values(**values)).rowcount:
            db.execute(RECORDS.insert().values(session_id=sid, started_at=now_utc(), **values))


def b64u(data: bytes):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def b64ud(data: str):
    return base64.urlsafe_b64decode(data + '=' * (-len(data) % 4))


def sign_token(claims: dict):
    payload = b64u(json.dumps(claims, separators=(',', ':'), sort_keys=True).encode())
    sig = b64u(hmac.new(SIGNING_KEY.encode(), payload.encode(), hashlib.sha256).digest())
    return f'{payload}.{sig}'


def verify_token(token: str):
    try:
        payload, signature = token.split('.')
        expected = b64u(hmac.new(SIGNING_KEY.encode(), payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            raise ValueError('signature')
        data = json.loads(b64ud(payload))
        if data['exp'] < int(time.time()) or data['origin'] not in ALLOWED_ORIGINS or not re.fullmatch('[a-f0-9]{32}', data['sid']):
            raise ValueError('expired/origin/session')
        return data
    except (ValueError, KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(401, 'Invalid or expired session')


def require_host(api_key: str | None):
    if not API_KEY or not hmac.compare_digest(api_key or '', API_KEY):
        raise HTTPException(401, 'Host API key required')


def require_session(authorization: str | None):
    if not authorization or not authorization.startswith('Bearer '):
        raise HTTPException(401, 'Embed session token required')
    return verify_token(authorization[7:])


class SessionRequest(BaseModel):
    parent_origin: str
    external_reference: str | None = Field(default=None, max_length=100)


def init_ocr():
    """Engine priority: PaddleOCR -> RapidOCR (ONNX, light, no system packages) -> Tesseract (dev)."""
    global OCR_ENGINE, OCR_STATE
    try:
        from paddleocr import PaddleOCR
        # PaddleOCR 3.x interface. Offline deployments must preload model artifacts.
        OCR_ENGINE = PaddleOCR(use_doc_orientation_classify=False, use_doc_unwarping=False,
                               use_textline_orientation=True, lang='en')
        OCR_STATE = 'paddleocr'
        return
    except Exception as exc:
        LOG.warning('PaddleOCR unavailable: %s', type(exc).__name__)
    try:
        from rapidocr_onnxruntime import RapidOCR
        OCR_ENGINE = RapidOCR()
        OCR_STATE = 'rapidocr'
        return
    except Exception as exc:
        LOG.warning('RapidOCR unavailable: %s', type(exc).__name__)
    OCR_ENGINE = None
    OCR_STATE = 'unavailable'
    if ENABLE_DEV_OCR_FALLBACK and shutil.which('tesseract') and find_spec('pytesseract'):
        OCR_STATE = 'tesseract_fallback'


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not API_KEY or len(API_KEY) < 24 or not SIGNING_KEY or len(SIGNING_KEY) < 32:
        raise RuntimeError('Set secure IDS_HOST_API_KEY (>=24 chars) and IDS_SIGNING_KEY (>=32 chars)')
    for origin in ALLOWED_ORIGINS:
        p = urlparse(origin)
        if p.scheme not in ('https', 'http') or not p.netloc or (p.scheme == 'http' and p.hostname not in ('localhost', '127.0.0.1')):
            raise RuntimeError('Allowed origins must be HTTPS or localhost HTTP')
    setup_db()
    init_ocr()
    yield


app = FastAPI(title='Identity Document Scanner API', version='0.1.0', lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_credentials=False,
                   allow_methods=['GET', 'POST'], allow_headers=['Authorization', 'Content-Type', 'X-API-Key'])


@app.middleware('http')
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Permissions-Policy'] = 'camera=(self), microphone=()'
    if request.url.path.startswith('/embed'):
        # Browser enforces this; server sets only allowlisted ancestor origins.
        ancestors = ' '.join(ALLOWED_ORIGINS)
        response.headers['Content-Security-Policy'] = ("default-src 'self'; img-src 'self' blob: data:; "
            "script-src 'self'; style-src 'self'; connect-src 'self'; "
            f"frame-ancestors 'self' {ancestors}; base-uri 'none'; form-action 'none'")
    else:
        response.headers['Content-Security-Policy'] = "frame-ancestors 'none'"
    return response


@app.get('/api/v1/health')
def health():
    return {'status': 'ok', 'ocr': OCR_STATE}


@app.post('/api/v1/host/sessions')
def create_session(body: SessionRequest, x_api_key: str | None = Header(default=None)):
    require_host(x_api_key)
    origin = body.parent_origin.rstrip('/')
    if origin not in ALLOWED_ORIGINS:
        raise HTTPException(403, 'Parent origin not allowed')
    sid = secrets.token_hex(16)
    exp = int(time.time()) + SESSION_TTL
    token = sign_token({'sid': sid, 'origin': origin, 'exp': exp})
    db_event(sid, 'SESSION_CREATED')
    start_record(sid, origin, body.external_reference)
    return {'session_id': sid, 'token': token, 'expires_at': exp, 'embed_path': f'/embed/#token={token}'}


@app.get('/api/v1/embed/session')
def session_context(authorization: str | None = Header(default=None)):
    c = require_session(authorization)
    return {'session_id': c['sid'], 'parent_origin': c['origin'], 'expires_at': c['exp'], 'ocr': OCR_STATE}


def read_image(image_bytes: bytes):
    try:
        image = Image.open(io.BytesIO(image_bytes))
        if image.format not in ('JPEG', 'PNG', 'WEBP'):
            raise ValueError('Unsupported format')
        image = ImageOps.exif_transpose(image)
        if image.width * image.height > 18_000_000 or min(image.size) < 200:
            raise ValueError('Image dimensions out of bounds')
        image = image.convert('RGB')
        return cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise HTTPException(422, f'Invalid image: {str(exc)}')


def quality_metrics(img: np.ndarray):
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    resized = cv2.resize(gray, (min(900, w), max(1, round(h * min(900, w) / w))))
    sharpness = float(cv2.Laplacian(resized, cv2.CV_64F).var())
    brightness = float(np.mean(resized))
    clipped_dark = float(np.mean(resized < 20))
    clipped_light = float(np.mean(resized > 245))
    warnings, blocking = [], []
    # Only severe problems are "blocking" (they explain a failed read). Glare and low
    # resolution are informational: if the OCR can read the text, the photo is fine.
    if sharpness < 22: blocking.append('BLURRY')
    if brightness < 40: blocking.append('TOO_DARK')
    if brightness > 235: blocking.append('TOO_BRIGHT')
    warnings += blocking
    if sharpness < 45 and 'BLURRY' not in warnings: warnings.append('SOFT_FOCUS')
    if clipped_light > .30: warnings.append('POSSIBLE_GLARE')
    if max(w, h) < 900: warnings.append('LOW_RESOLUTION')
    return {'width': w, 'height': h, 'sharpness': round(sharpness, 1),
            'brightness': round(brightness, 1), 'clipped_dark_ratio': round(clipped_dark, 3),
            'clipped_light_ratio': round(clipped_light, 3), 'warnings': warnings,
            'blocking': blocking, 'acceptable': not blocking}


def rectify_document(img: np.ndarray):
    """Try largest quadrilateral; fall back to original for tight/undetected crops."""
    h, w = img.shape[:2]
    scale = min(1, 1100 / max(w, h))
    small = cv2.resize(img, (round(w * scale), round(h * scale)))
    edges = cv2.Canny(cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (5, 5), 0), 45, 145)
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    for c in sorted(contours, key=cv2.contourArea, reverse=True)[:15]:
        perimeter = cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c, .025 * perimeter, True)
        if len(approx) != 4 or cv2.contourArea(approx) < .2 * small.shape[0] * small.shape[1]:
            continue
        pts = approx.reshape(4, 2).astype(np.float32) / scale
        summ = pts.sum(axis=1); diff = np.diff(pts, axis=1).ravel()
        ordered = np.array([pts[np.argmin(summ)], pts[np.argmin(diff)], pts[np.argmax(summ)], pts[np.argmax(diff)]], dtype=np.float32)
        a, b, c2, d = ordered
        ow = int(max(np.linalg.norm(b - a), np.linalg.norm(c2 - d)))
        oh = int(max(np.linalg.norm(d - a), np.linalg.norm(c2 - b)))
        if ow < 300 or oh < 200: continue
        dest = np.array([[0, 0], [ow-1, 0], [ow-1, oh-1], [0, oh-1]], dtype=np.float32)
        transformed = cv2.warpPerspective(img, cv2.getPerspectiveTransform(ordered, dest), (ow, oh))
        return transformed, True
    return img, False


def ocr_lines(img):
    """One OCR pass over a BGR image. Returns [{'text','confidence'}]."""
    if OCR_STATE == 'paddleocr' and OCR_ENGINE is not None:
        # PaddleOCR 3.x returns OCRResult objects with rec_texts, rec_scores.
        with OCR_LOCK:
            results = OCR_ENGINE.predict(img)
        lines = []
        for result in results:
            d = result.json if hasattr(result, 'json') else result
            if callable(d): d = d()
            if isinstance(d, dict) and 'res' in d: d = d['res']
            for t, score in zip(d.get('rec_texts', []), d.get('rec_scores', [])):
                lines.append({'text': str(t), 'confidence': round(float(score), 3)})
        return lines
    if OCR_STATE == 'rapidocr' and OCR_ENGINE is not None:
        with OCR_LOCK:
            result, _ = OCR_ENGINE(img)
        return [{'text': str(t), 'confidence': round(float(sc), 3)} for _box, t, sc in (result or [])]
    if ENABLE_DEV_OCR_FALLBACK:
        try:
            import pytesseract
            data = pytesseract.image_to_data(img, lang='eng', output_type=pytesseract.Output.DICT)
            return [{'text': t.strip(), 'confidence': round(max(0, float(c))/100, 3)} for t, c in zip(data['text'], data['conf']) if t.strip() and float(c) >= 0]
        except Exception as exc:
            LOG.warning('Fallback OCR failed: %s', type(exc).__name__)
    raise HTTPException(503, 'OCR engine unavailable. Install PaddleOCR model or enable Tesseract development fallback.')


ALNUM = re.compile(r'[^0-9A-Za-zÁÉÍÓÚÜÑáéíóúüñ]')
GOOD_SCORE = 90  # enough readable text: stop trying more image variants


def text_score(lines: list[dict]):
    return sum(l['confidence'] * len(ALNUM.sub('', l['text'])) for l in lines if l['confidence'] >= .3)


def normalize_for_ocr(img: np.ndarray):
    """Bring the card to a size OCR engines like (long side ~1600 px)."""
    h, w = img.shape[:2]
    long_side = max(h, w)
    if long_side < 1300:
        f = min(2.5, 1600 / long_side)
        return cv2.resize(img, (round(w * f), round(h * f)), interpolation=cv2.INTER_CUBIC)
    if long_side > 2200:
        f = 2000 / long_side
        return cv2.resize(img, (round(w * f), round(h * f)), interpolation=cv2.INTER_AREA)
    return img


def enhance_for_ocr(img: np.ndarray):
    """Local contrast + mild sharpening: helps with uneven light, soft focus and holograms."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8)).apply(gray)
    soft = cv2.GaussianBlur(gray, (0, 0), 1.2)
    return cv2.cvtColor(cv2.addWeighted(gray, 1.6, soft, -.6, 0), cv2.COLOR_GRAY2BGR)


def read_document(img: np.ndarray):
    """OCR with fallbacks: original -> enhanced -> perspective-corrected. Keeps the best read."""
    base = normalize_for_ocr(img)
    best_lines = ocr_lines(base)
    best, used_rect = text_score(best_lines), False
    if best >= GOOD_SCORE:
        return best_lines, used_rect
    variants = [(enhance_for_ocr(base), False)]
    rectified, ok = rectify_document(base)
    if ok:
        variants += [(rectified, True), (enhance_for_ocr(rectified), True)]
    for variant, is_rect in variants:
        lines = ocr_lines(variant)
        score = text_score(lines)
        if score > best:
            best, best_lines, used_rect = score, lines, is_rect
        if best >= GOOD_SCORE:
            break
    return best_lines, used_rect


NOT_NAME_WORDS = ('REPUBLICA', 'COLOMBIA', 'IDENTIFICACION', 'PERSONAL', 'CEDULA', 'CIUDADANIA', 'NUMERO', 'FECHA',
                  'LUGAR', 'NACIMIENTO', 'ESTATURA', 'SEXO', 'FIRMA', 'INDICE', 'DERECHO', 'REGISTRADOR', 'EXPEDICION',
                  'NACIONAL', 'SANGUINEO', 'APELLIDOS', 'NOMBRES', 'SURNAME', 'GIVEN')
LABELS = {'surnames': ('APELLIDOS', 'SURNAMES', 'SURNAME'), 'given': ('NOMBRES', 'GIVENNAMES', 'NAMES')}
OCR_DIGIT_AS_LETTER = str.maketrans('01586$|!', 'OISBSSII')


def _letters(text: str):
    """Uppercase letters only, with common OCR digit/letter swaps undone (N0MBRES -> NOMBRES)."""
    return re.sub(r'[^A-ZÁÉÍÓÚÜÑ]', '', text.upper().translate(OCR_DIGIT_AS_LETTER).replace('Á', 'A').replace('É', 'E')
                  .replace('Í', 'I').replace('Ó', 'O').replace('Ú', 'U'))


def _label_of(line: str):
    """Returns (kind, remainder_of_line) when the line starts with a surnames/given-names label (typo tolerant)."""
    words = line.strip().split()
    for take in range(1, min(3, len(words)) + 1):
        head = _letters(' '.join(words[:take]))
        for kind, names in LABELS.items():
            if any(difflib.SequenceMatcher(None, head, n).ratio() >= .8 for n in names if len(n) > 5):
                return kind, ' '.join(words[take:])
    # Label glued to its value: "APELLIDOSGARCIA RODRIGUEZ"
    flat = _letters(line)
    for kind, names in LABELS.items():
        for n in names:
            if len(n) > 5 and flat.startswith(n) and len(flat) > len(n):
                return kind, re.sub(r'^\W*[A-Za-zÁÉÍÓÚÜÑ0-9]{%d}' % len(n), '', line.strip(), count=1)
    return None, ''


def _is_name_text(text: str):
    t = re.sub(r'[^A-ZÁÉÍÓÚÜÑ ]', ' ', text.upper()).strip()
    letters = _letters(t)
    if len(letters) < 3 or re.search(r'\d', text):
        return None
    if any(w in letters for w in NOT_NAME_WORDS):
        return None
    return re.sub(r'\s+', ' ', t)


def extract_names(texts: list[str]):
    """Surnames and given names, from the text after each label. Ambiguous layouts return None, never a guess."""
    out = {}
    labels = [(i, *_label_of(t)) for i, t in enumerate(texts)]
    labels = [(i, k, rest) for i, k, rest in labels if k]
    for pos, (i, kind, rest) in enumerate(labels):
        if kind in out:
            continue
        stop = labels[pos + 1][0] if pos + 1 < len(labels) else len(texts)
        parts = []
        first = _is_name_text(rest) if rest.strip() else None
        if first:
            parts.append(first)
        for t in texts[i + 1:stop]:
            v = _is_name_text(t)
            if not v:
                break
            parts.append(v)
            if len(parts) >= 2:
                break
        if parts:
            out[kind] = ' '.join(parts)
    return out.get('surnames'), out.get('given')


def extract_fields(lines: list[dict]):
    texts = [l['text'].strip() for l in lines if l['text'].strip()]
    full = '\n'.join(texts)
    upper = full.upper()
    # Conservative parsing: unclear fields are null; no guesses or LLM completions.
    number = None
    for ln in texts:  # Colombian format first: 1.234.567.890 (undo "NUMER0" glued to the digits)
        ln = re.sub(r'(?i)N[UÚ]M[E3]R[O0]\.?:?', ' ', ln)
        m = re.search(r'(?<!\d)\d{1,3}(?:[.,\s]\d{3}){1,3}(?!\d)', ln)
        if m and 6 <= len(re.sub(r'\D', '', m.group())) <= 10:
            number = re.sub(r'\D', '', m.group())
            break
    for ln in ([] if number else texts):
        candidate = re.sub(r'[^0-9]', '', ln)
        if 6 <= len(candidate) <= 10 and ('.' in ln or len(ln.strip()) <= 14):
            number = candidate
            break
    date_matches = re.findall(r'\b(?:\d{1,2}[-/.]\d{1,2}[-/.]\d{4}|\d{4}[-/.]\d{1,2}[-/.]\d{1,2})\b', full)
    surnames, given = extract_names(texts)
    name = ' '.join(x for x in (given, surnames) if x) or None
    doc_type = 'COLOMBIAN_ID_CANDIDATE' if 'COLOMBIA' in upper or 'CEDULA' in upper or 'CÉDULA' in upper else 'UNKNOWN'
    return {'document_type': doc_type, 'document_number': number, 'name_candidate': name,
            'surnames': surnames, 'given_names': given,
            'date_candidates': date_matches, 'fields_need_review': True,
            'note': 'OCR-only: document authenticity and holder identity are NOT verified.'}


FRONT_KEYS = ('COLOMBIA', 'CEDULA', 'CIUDADANIA', 'REPUBLICA', 'IDENTIFICACION', 'NUMERO', 'APELLIDOS', 'NOMBRES')
BACK_KEYS = ('FECHA', 'LUGAR', 'NACIMIENTO', 'ESTATURA', 'SEXO', 'INDICE', 'DERECHO', 'REGISTRADOR',
             'EXPEDICION', 'NACIONAL', 'SANGUINEO', '<<', 'COL')
REASON_ORDER = ('BLURRY', 'TOO_DARK', 'TOO_BRIGHT', 'POSSIBLE_GLARE', 'LOW_RESOLUTION')


def assess_capture(side: str, lines: list[dict], fields: dict, quality: dict):
    """Decide whether a side was read well enough. Returns (usable, reason_if_not).

    Deliberately lenient: the OCR result is the judge, not the image statistics. Spaces
    are ignored because OCR engines often glue words together (REPUBLICADECOLOMBIA).
    """
    good = [l for l in lines if l['confidence'] >= .4 and len(ALNUM.sub('', l['text'])) >= 2]
    blob = ''.join(re.sub(r'[^A-Z0-9<]', '', l['text'].upper()) for l in good)
    keys = FRONT_KEYS if side == 'front' else BACK_KEYS
    hits = sum(1 for k in keys if k in blob)
    chars = sum(len(ALNUM.sub('', l['text'])) for l in good)
    usable = (len(good) >= 2 and chars >= 10 and (hits >= 1 or fields['document_number'] is not None)) \
        or (len(good) >= 5 and chars >= 40)
    if usable:
        return True, None
    reason = next((w for w in REASON_ORDER if w in quality['warnings']), 'NO_TEXT')
    return False, reason


def capture_rank(result: dict):
    return (bool(result.get('usable')), result.get('score', 0))


def process_image(img: np.ndarray, side: str):
    quality = quality_metrics(img)
    lines, rectified = read_document(img)
    fields = extract_fields(lines)
    usable, reason = assess_capture(side, lines, fields, quality)
    return {'side': side, 'status': 'COMPLETED' if usable else 'REQUIRES_REVIEW',
            'usable': usable, 'reason': reason, 'score': round(text_score(lines), 1),
            'quality': quality, 'rectified': rectified, 'fields': fields,
            'ocr_lines': lines, 'ocr_engine': OCR_STATE}


async def decode_upload(file: UploadFile):
    if file.content_type not in ('image/jpeg', 'image/png', 'image/webp'):
        raise HTTPException(415, 'JPEG, PNG or WebP required')
    buf = await file.read(MAX_BYTES + 1)
    if len(buf) > MAX_BYTES:
        raise HTTPException(413, 'Image too large')
    return read_image(buf)


@app.post('/api/v1/embed/quality')
async def quality(request: Request, image: UploadFile = File(...), authorization: str | None = Header(default=None)):
    c = require_session(authorization)
    img = await decode_upload(image)
    metrics = quality_metrics(img)
    db_event(c['sid'], 'IMAGE_QUALITY_CHECKED', 'acceptable' if metrics['acceptable'] else ','.join(metrics['warnings']))
    return metrics


@app.post('/api/v1/embed/extract')
async def extract(request: Request, side: Literal['front','back'], image: UploadFile = File(...), authorization: str | None = Header(default=None)):
    c = require_session(authorization)
    sid = c['sid']
    db_event(sid, 'EXTRACTION_STARTED', side)
    try:
        img = await decode_upload(image)
        result = await run_in_threadpool(process_image, img, side)  # keep the event loop free
        previous = load_result(sid) or {}
        # Keep the best attempt per side: a worse retry must never overwrite a better read.
        if side not in previous or capture_rank(result) >= capture_rank(previous[side]):
            previous[side] = result
            save_result(sid, previous)
        db_event(sid, 'EXTRACTION_COMPLETED', f"{side};lines={len(result['ocr_lines'])};usable={result['usable']};{result['reason'] or 'ok'}")
        return result
    except HTTPException as exc:
        db_event(sid, 'EXTRACTION_FAILED', f'{side};http={exc.status_code}')
        raise
    except Exception:
        LOG.exception('OCR processing exception (no image data logged)')
        db_event(sid, 'EXTRACTION_FAILED', f'{side};processing_error')
        raise HTTPException(500, 'OCR processing failed')


@app.post('/api/v1/embed/finish')
def finish(authorization: str | None = Header(default=None)):
    c = require_session(authorization)
    data = load_result(c['sid'])
    if not data or 'front' not in data or 'back' not in data:
        db_event(c['sid'], 'VALIDATION_FAILED', 'Missing front/back')
        raise HTTPException(409, 'Both sides must be processed')
    complete_record(c['sid'], data)
    db_event(c['sid'], 'CAPTURE_COMPLETED', 'front+back')
    return {'status': 'CAPTURE_COMPLETED', 'session_id': c['sid'], 'data': data,
            'warning': 'OCR-only: not identity verification'}


@app.get('/api/v1/host/sessions/{session_id}/events')
def host_events(session_id: str, after_id: int = 0, x_api_key: str | None = Header(default=None)):
    require_host(x_api_key)
    if not re.fullmatch('[a-f0-9]{32}', session_id): raise HTTPException(400, 'Bad session ID')
    with engine().connect() as db:
        rows = db.execute(sa.select(EVENTS.c.id, EVENTS.c.event_type, EVENTS.c.detail, EVENTS.c.created_at)
                          .where(EVENTS.c.session_id == session_id, EVENTS.c.id > after_id).order_by(EVENTS.c.id).limit(200)).all()
    return {'session_id': session_id, 'events': [{'id': r[0], 'type': r[1], 'detail': r[2], 'timestamp': int(r[3].replace(tzinfo=timezone.utc).timestamp())} for r in rows]}


@app.get('/api/v1/host/sessions/{session_id}/result')
def host_result(session_id: str, x_api_key: str | None = Header(default=None)):
    require_host(x_api_key)
    if not re.fullmatch('[a-f0-9]{32}', session_id): raise HTTPException(400, 'Bad session ID')
    data = load_result(session_id)
    if data is None: raise HTTPException(404, 'Not found or expired')
    return {'session_id': session_id, 'data': data}


@app.get('/embed/')
def embed_app():
    return FileResponse(FRONTEND / 'index.html', media_type='text/html')


@app.get('/embed/{asset_name}')
def embed_asset(asset_name: str):
    if asset_name not in ('app.js', 'style.css'):
        raise HTTPException(404, 'Not found')
    return FileResponse(FRONTEND / asset_name)

class ClientEvent(BaseModel):
    code: Literal['CAMERA_ERROR','QUALITY_ERROR','PROCESSING_ERROR','SERVICE_UNAVAILABLE']
    side: Literal['front','back'] | None = None


@app.post('/api/v1/embed/events')
def client_event(body: ClientEvent, authorization: str | None = Header(default=None)):
    c = require_session(authorization)
    db_event(c['sid'], 'CLIENT_ERROR', f'{body.code};{body.side or "unknown"}')
    return {'ok': True}
