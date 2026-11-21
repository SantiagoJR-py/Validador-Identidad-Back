import importlib
import io
import os
import sys
import time

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))


@pytest.fixture()
def ready(tmp_path, monkeypatch):
    monkeypatch.setenv('IDS_HOST_API_KEY', 'test-host-secret-abcdefghijklmnopqrstuvwxyz')
    monkeypatch.setenv('IDS_SIGNING_KEY', 'test-signing-secret-abcdefghijklmnopqrstuvwxyz')
    monkeypatch.setenv('IDS_DATABASE_URL', 'sqlite:///' + str(tmp_path / 'test.sqlite3'))
    import backend.app.main as m
    m = importlib.reload(m)
    monkeypatch.setattr(m, 'init_ocr', lambda: None)
    with TestClient(m.app) as client:
        yield m, client


def session(client):
    r=client.post('/api/v1/host/sessions',headers={'X-API-Key':'test-host-secret-abcdefghijklmnopqrstuvwxyz'},json={'parent_origin':'http://localhost:8080'})
    assert r.status_code==200,r.text
    return r.json()


def fixture_image():
    img=np.full((750,1200,3),255,dtype=np.uint8)
    cv2.putText(img,'REPUBLICA DE COLOMBIA',(80,160),cv2.FONT_HERSHEY_SIMPLEX,1.2,(0,0,0),3)
    cv2.putText(img,'CEDULA DE CIUDADANIA',(80,245),cv2.FONT_HERSHEY_SIMPLEX,1,(0,0,0),2)
    cv2.putText(img,'1234567890',(80,330),cv2.FONT_HERSHEY_SIMPLEX,1.3,(0,0,0),3)
    success,out=cv2.imencode('.jpg',img)
    assert success
    return out.tobytes()


def test_session_auth_and_embed_headers(ready):
    m,c=ready
    assert c.post('/api/v1/host/sessions',json={'parent_origin':'http://localhost:8080'}).status_code==401
    assert c.post('/api/v1/host/sessions',headers={'X-API-Key':'test-host-secret-abcdefghijklmnopqrstuvwxyz'},json={'parent_origin':'https://cualquiera.example'}).status_code==200
    assert c.post('/api/v1/host/sessions',headers={'X-API-Key':'test-host-secret-abcdefghijklmnopqrstuvwxyz'},json={'parent_origin':'no-es-un-origen'}).status_code==422
    s=session(c)
    assert c.get('/api/v1/embed/session',headers={'Authorization':'Bearer '+s['token']}).json()['parent_origin']=='http://localhost:8080'
    assert 'frame-ancestors *' in c.get('/embed/').headers['content-security-policy']
    assert c.get('/api/v1/embed/session',headers={'Authorization':'Bearer '+'invalid'}).status_code==401
    assert m.verify_token(s['token'])['sid']==s['session_id']


def test_quality_ocr_finish_and_logs(ready,monkeypatch):
    m,c=ready
    monkeypatch.setattr(m,'ocr_lines',lambda img:[{'text':'REPUBLICA DE COLOMBIA','confidence':.98},{'text':'1234567890','confidence':.99}])
    s=session(c)
    hdr={'Authorization':'Bearer '+s['token']}
    img=fixture_image()
    r=c.post('/api/v1/embed/quality',headers=hdr,files={'image':('test.jpg',img,'image/jpeg')})
    assert r.status_code==200,r.text
    assert 'brightness' in r.json()
    assert c.post('/api/v1/embed/finish',headers=hdr).status_code==409
    for side in ('front','back'):
        response=c.post('/api/v1/embed/extract?side='+side,headers=hdr,files={'image':('test.jpg',img,'image/jpeg')})
        assert response.status_code==200,response.text
        assert response.json()['fields']['document_number']=='1234567890'
    done=c.post('/api/v1/embed/finish',headers=hdr)
    assert done.status_code==200,done.text
    assert done.json()['status']=='CAPTURE_COMPLETED'
    host={'X-API-Key':'test-host-secret-abcdefghijklmnopqrstuvwxyz'}
    events=c.get('/api/v1/host/sessions/'+s['session_id']+'/events',headers=host).json()['events']
    assert 'CAPTURE_COMPLETED' in [e['type'] for e in events]
    assert '1234567890' not in str(events)
    result=c.get('/api/v1/host/sessions/'+s['session_id']+'/result',headers=host)
    assert result.json()['data']['front']['fields']['document_number']=='1234567890'


def test_reject_non_images(ready):
    m,c=ready
    s=session(c)
    hdr={'Authorization':'Bearer '+s['token']}
    r=c.post('/api/v1/embed/extract?side=front',headers=hdr,files={'image':('bad.jpg',b'not an image','image/jpeg')})
    assert r.status_code==422
    events=c.get('/api/v1/host/sessions/'+s['session_id']+'/events',headers={'X-API-Key':'test-host-secret-abcdefghijklmnopqrstuvwxyz'}).json()['events']
    assert any(e['type']=='EXTRACTION_FAILED' for e in events)


def test_record_persisted_with_json(ready,monkeypatch):
    import sqlalchemy as sa
    m,c=ready
    monkeypatch.setattr(m,'ocr_lines',lambda img:[{'text':'REPUBLICA DE COLOMBIA','confidence':.98},{'text':'1234567890','confidence':.99}])
    s=session(c)
    hdr={'Authorization':'Bearer '+s['token']}
    with m.engine().connect() as db:
        assert db.execute(sa.select(m.RECORDS.c.status)).scalar()=='STARTED'
    for side in ('front','back'):
        c.post('/api/v1/embed/extract?side='+side,headers=hdr,files={'image':('t.jpg',fixture_image(),'image/jpeg')})
    assert c.post('/api/v1/embed/finish',headers=hdr).status_code==200
    with m.engine().connect() as db:
        row=db.execute(sa.select(m.RECORDS)).mappings().one()
    assert row['status']=='COMPLETED' and row['completed_at'] is not None
    assert row['document_number']=='1234567890'
    assert row['extracted_json']['front']['fields']['document_number']=='1234567890'
    assert 'ocr_lines' not in row['extracted_json']['front']


@pytest.mark.parametrize('lines,given,surnames', [
    (['APELLIDOS', 'GARCIA RODRIGUEZ', 'NOMBRES', 'JUAN CARLOS'], 'JUAN CARLOS', 'GARCIA RODRIGUEZ'),
    (['APELLIDOSGARCIA RODRIGUEZ', 'NOMBRES JUAN CARLOS'], 'JUAN CARLOS', 'GARCIA RODRIGUEZ'),
    (['Apellidos: MUÑOZ PEÑA', 'Nombres: JOSÉ ÁNGEL'], 'JOSÉ ÁNGEL', 'MUÑOZ PEÑA'),
    (['APELL1DOS', 'GARCIA', 'RODRIGUEZ', 'N0MBRES', 'JUAN', 'CARLOS', 'FECHA DE NACIMIENTO'], 'JUAN CARLOS', 'GARCIA RODRIGUEZ'),
    (['GARCIA RODRIGUEZ', 'JUAN CARLOS', 'APELLIDOS', 'NOMBRES'], None, None),  # ambiguous: no guess
])
def test_name_extraction(ready, lines, given, surnames):
    m, _ = ready
    f = m.extract_fields([{'text': t, 'confidence': .9} for t in lines])
    assert (f['given_names'], f['surnames']) == (given, surnames)


def test_number_with_glued_label(ready):
    m, _ = ready
    assert m.extract_fields([{'text': 'NUMER0521.025.150', 'confidence': .9}])['document_number'] == '521025150'
