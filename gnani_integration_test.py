"""Offline tests for CarePilot's documented Gnani Prisma REST integration.
Run with: python gnani_integration_test.py
These tests mock the provider and do not need a real API key or internet.
"""
import io
import os
import tempfile
from pathlib import Path

os.environ['GNANI_API_KEY'] = 'test-key-not-real'
import app as careloop
import requests

with tempfile.TemporaryDirectory(prefix='careloop-gnani-test-') as tmp:
    root = Path(tmp)
    careloop.AUTH_DB = root / 'accounts.db'
    careloop.DB = root / 'unused.db'
    careloop.USERS_DIR = root / 'workspaces'
    careloop.USERS_DIR.mkdir()
    careloop.init_db()
    careloop.app.config.update(TESTING=True, SECRET_KEY='test-secret')
    client = careloop.app.test_client()
    response = client.post('/register', data={
        'name': 'Test User', 'email': 'gnani-test@example.com',
        'password': 'test-password-123', 'confirm': 'test-password-123'
    }, follow_redirects=True)
    assert response.status_code == 200

    calls = {}
    class FakeResponse:
        ok = True
        status_code = 200
        text = ''
        def json(self):
            return {
                'success': True,
                'transcript': 'नमस्ते, कल सुबह 10 बजे फोन करना है।',
                'detected_language_code': 'hi-IN',
                'model': 'prisma-v3.0',
                'request_id': 'test-request-id'
            }

    def fake_post(url, **kwargs):
        calls['url'] = url
        calls.update(kwargs)
        return FakeResponse()

    original_post = requests.post
    requests.post = fake_post
    try:
        response = client.post('/voice/transcribe', data={
            'audio_file': (io.BytesIO(b'fake-audio-bytes'), 'sample.mp3', 'audio/mpeg')
        }, content_type='multipart/form-data')
    finally:
        requests.post = original_post

    payload = response.get_json()
    assert response.status_code == 200, payload
    assert payload['ok'] is True and 'नमस्ते' in payload['transcript']
    assert calls['url'] == 'https://api.vachana.ai/stt/v3'
    assert calls['headers']['X-API-Key-ID'] == 'test-key-not-real'
    assert calls['data'] == {'model': 'prisma-v3.0', 'language_code': 'indic-auto', 'format': 'codemix'}
    assert calls['files']['audio_file'][0] == 'sample.mp3'
    assert client.get('/api/status').get_json()['gnani_connected'] is True

print('Gnani REST mock test passed: endpoint, API-key header, multipart upload, Prisma fields, transcript parsing and live-status state.')
