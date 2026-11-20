"""Synthetic benchmark of the server-side capture logic (not a pytest file).

Renders fake ID-card sides, degrades them like a phone camera would (blur, low light,
glare, tilt, low resolution, noise, JPEG) and reports how many are accepted.

    IDS_HOST_API_KEY=x*24 IDS_SIGNING_KEY=x*32 python -m backend.tests.bench_capture [N]

It measures the OCR/acceptance logic only. It does NOT replace testing with real
cards and real phones, and Hershey fonts are easier than real card typography.
"""
import os
import random
import sys
import time

os.environ.setdefault('IDS_HOST_API_KEY', 'b' * 32)
os.environ.setdefault('IDS_SIGNING_KEY', 'b' * 40)

import cv2  # noqa: E402
import numpy as np  # noqa: E402

import backend.app.main as m  # noqa: E402

FONT = cv2.FONT_HERSHEY_SIMPLEX


def render(side: str, rng: random.Random):
    img = np.full((540, 856, 3), (235, 238, 228), np.uint8)
    ink = (30, 30, 30)
    if side == 'front':
        cv2.putText(img, 'REPUBLICA DE COLOMBIA', (40, 60), FONT, .9, ink, 2)
        cv2.putText(img, 'IDENTIFICACION PERSONAL', (40, 100), FONT, .7, ink, 2)
        cv2.rectangle(img, (40, 140), (260, 420), (170, 160, 150), -1)
        n = rng.randint(10_000_000, 1_999_999_999)
        cv2.putText(img, f'NUMERO {n:,}'.replace(',', '.'), (290, 190), FONT, 1.0, ink, 2)
        cv2.putText(img, 'APELLIDOS', (290, 250), FONT, .6, ink, 1)
        cv2.putText(img, rng.choice(['GARCIA RODRIGUEZ', 'MARTINEZ LOPEZ', 'PEREZ GOMEZ']), (290, 285), FONT, .9, ink, 2)
        cv2.putText(img, 'NOMBRES', (290, 340), FONT, .6, ink, 1)
        cv2.putText(img, rng.choice(['JUAN CARLOS', 'MARIA FERNANDA', 'LUIS ALBERTO']), (290, 375), FONT, .9, ink, 2)
    else:
        for i, t in enumerate(['FECHA DE NACIMIENTO 12-MAR-1990', 'LUGAR DE NACIMIENTO MANIZALES (CALDAS)',
                               'ESTATURA 1.72  G.S. RH O+  SEXO M', 'FECHA Y LUGAR DE EXPEDICION 20-MAY-2008']):
            cv2.putText(img, t, (40, 80 + i * 60), FONT, .65, ink, 2)
        for i in range(3):
            cv2.putText(img, 'I<COL' + ''.join(rng.choice('0123456789<') for _ in range(24)), (30, 380 + i * 45), FONT, .8, ink, 2)
    return img


def degrade(img, rng: random.Random, profile: str):
    h, w = img.shape[:2]
    p = {  # (long side px, blur sigma, brightness, tilt deg, glare, noise)
        'good': (1500, (0, .6), (.85, 1.1), 3, 0, 4),
        'phone_typical': (1100, (.4, 1.4), (.6, 1.15), 6, .1, 8),
        'low_res': (720, (.2, 1.0), (.7, 1.1), 5, .05, 6),
        'dim_room': (1100, (.4, 1.2), (.38, .55), 5, 0, 14),
        'glare': (1100, (.3, 1.2), (.85, 1.1), 5, .28, 6),
        'blurry': (1100, (1.8, 2.6), (.7, 1.1), 5, .05, 6),
    }[profile]
    long_side, (b0, b1), (l0, l1), tilt, glare, noise = p
    long_side = int(long_side * rng.uniform(.9, 1.1))
    s = long_side / w
    out = cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
    H, W = out.shape[:2]
    # tilt + small perspective on a dark table background, then crop like the guide would
    canvas = np.full((int(H * 1.25), int(W * 1.12), 3), (40, 38, 36), np.uint8)
    y0, x0 = (canvas.shape[0] - H) // 2, (canvas.shape[1] - W) // 2
    canvas[y0:y0 + H, x0:x0 + W] = out
    M = cv2.getRotationMatrix2D((canvas.shape[1] / 2, canvas.shape[0] / 2), rng.uniform(-tilt, tilt), 1)
    canvas = cv2.warpAffine(canvas, M, (canvas.shape[1], canvas.shape[0]), borderValue=(40, 38, 36))
    sigma = rng.uniform(b0, b1) * long_side / 1100
    if sigma > .1:
        canvas = cv2.GaussianBlur(canvas, (0, 0), sigma)
    canvas = np.clip(canvas.astype(np.float32) * rng.uniform(l0, l1), 0, 255)
    if glare:
        ch, cw = canvas.shape[:2]
        mask = np.zeros((ch, cw), np.float32)
        cv2.ellipse(mask, (rng.randint(cw // 4, 3 * cw // 4), rng.randint(ch // 4, 3 * ch // 4)),
                    (int(cw * glare * 1.2), int(ch * glare * 1.2)), rng.randint(0, 90), 0, 360, 1, -1)
        mask = cv2.GaussianBlur(mask, (0, 0), cw / 40)[..., None]
        canvas = canvas * (1 - mask) + 255 * mask
    canvas += np.random.default_rng(rng.randint(0, 10 ** 6)).normal(0, noise, canvas.shape)
    canvas = np.clip(canvas, 0, 255).astype(np.uint8)
    ok, buf = cv2.imencode('.jpg', canvas, [cv2.IMWRITE_JPEG_QUALITY, rng.randint(70, 92)])
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def old_logic(img, side):
    """The previous acceptance rule: one OCR pass, strict thresholds, blocking glare."""
    q = m.quality_metrics(img)
    old_block = q['sharpness'] < 45 or q['brightness'] < 55 or q['brightness'] > 215 or q['clipped_light_ratio'] > .22
    lines = m.ocr_lines(img)
    fields = m.extract_fields(lines)
    conf = [l for l in lines if l['confidence'] >= .5 and len(l['text'].strip()) >= 2]
    read_ok = len(conf) >= 3 and sum(len(l['text']) for l in conf) >= 15
    if read_ok and side == 'front':
        read_ok = fields['document_type'] != 'UNKNOWN' or fields['document_number'] is not None
    return (not old_block) and read_ok


def main(n: int):
    m.init_ocr()
    print('OCR engine:', m.OCR_STATE)
    rng = random.Random(7)
    profiles = ['good', 'phone_typical', 'low_res', 'dim_room', 'glare', 'blurry']
    tot_new = tot_old = tot = 0
    t0 = time.time()
    for prof in profiles:
        new = old = 0
        for side in ('front', 'back'):
            for _ in range(n):
                img = degrade(render(side, rng), rng, prof)
                new += bool(m.process_image(img, side)['usable'])
                old += bool(old_logic(img, side))
        total = 2 * n
        tot_new += new; tot_old += old; tot += total
        print(f'{prof:14s} anterior {old / total:5.0%}   nueva {new / total:5.0%}   ({total} imágenes)')
    print(f'{"TOTAL":14s} anterior {tot_old / tot:5.0%}   nueva {tot_new / tot:5.0%}   ({time.time() - t0:.0f}s)')


if __name__ == '__main__':
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 12)
