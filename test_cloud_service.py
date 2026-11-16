import unittest
import json
from unittest.mock import patch
import cloud_service


class _FakeBlob:
    def __init__(self, path, store): self.path, self.store, self.content_type = path, store, "application/octet-stream"
    def upload_from_string(self, payload, content_type=None, timeout=None):
        self.store[self.path] = payload; self.content_type = content_type or self.content_type
    def exists(self): return self.path in self.store
    def download_as_bytes(self): return self.store[self.path]
    def delete(self): self.store.pop(self.path, None)

class _FakeBucket:
    def __init__(self): self.store = {}
    def blob(self, path): return _FakeBlob(path, self.store)

class _FakeSnapshot:
    def __init__(self, data): self.data, self.exists = data, data is not None
    def to_dict(self): return self.data

class _FakeDoc:
    def __init__(self, db, sid): self.db, self.sid = db, sid
    def get(self): return _FakeSnapshot(self.db.records.get(self.sid))
    def set(self, value, merge=False):
        old = self.db.records.get(self.sid, {}) if merge else {}
        self.db.records[self.sid] = {**old, **value}
    def delete(self): self.db.records.pop(self.sid, None)

class _FakeCollection:
    def __init__(self, db): self.db = db
    def document(self, sid): return _FakeDoc(self.db, sid)

class _FakeDB:
    def __init__(self): self.records = {}
    def collection(self, _): return _FakeCollection(self)

class CloudSecuritySmokeTests(unittest.TestCase):
    def setUp(self):
        self.client = cloud_service.app.test_client()

    def test_internal_upload_requires_service_secret(self):
        response = self.client.post('/internal/sessions', data={})
        self.assertEqual(response.status_code, 401)

    def test_internal_lookup_requires_service_secret(self):
        response = self.client.get('/internal/sessions/20261116-120000-a1b2c3')
        self.assertEqual(response.status_code, 401)

    def test_guest_route_rejects_invalid_session_and_token_before_firebase(self):
        response = self.client.get('/g/not-a-session/not-a-token')
        self.assertEqual(response.status_code, 404)

    def test_guest_route_rejects_malformed_token(self):
        response = self.client.get('/g/20261116-120000-a1b2c3/short')
        self.assertEqual(response.status_code, 404)


    def test_upload_writes_hash_only_and_private_storage_paths(self):
        sid = '20261116-120000-a1b2c3'
        token = 'A' * 48
        fake_db, fake_bucket = _FakeDB(), _FakeBucket()
        metadata = {"sessionId": sid, "accessToken": token, "status": "ready", "revision": 1,
                    "photoSlots": ["1", "2", "3"], "clearOutputs": False}
        image_jpg = b"\xff\xd8\xff" + b"photo-data"
        image_png = b"\x89PNG\r\n\x1a\n" + b"strip-data"
        image_gif = b"GIF89a" + b"gif-data"
        with patch.dict('os.environ', {'CLOUD_SYNC_SECRET':'test-secret'}), \
             patch.object(cloud_service, '_init_firebase', lambda: None), \
             patch.object(cloud_service, '_db', fake_db), patch.object(cloud_service, '_bucket', fake_bucket):
            response = self.client.post('/internal/sessions', headers={'Authorization':'Bearer test-secret'},
                data={'metadata':json.dumps(metadata),
                      'photo1':( __import__('io').BytesIO(image_jpg), 'p1.jpg', 'image/jpeg'),
                      'photo2':( __import__('io').BytesIO(image_jpg), 'p2.jpg', 'image/jpeg'),
                      'photo3':( __import__('io').BytesIO(image_jpg), 'p3.jpg', 'image/jpeg'),
                      'strip':( __import__('io').BytesIO(image_png), 'strip.png', 'image/png'),
                      'gif':( __import__('io').BytesIO(image_gif), 'gif.gif', 'image/gif')},
                content_type='multipart/form-data')
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        record = fake_db.records[sid]
        self.assertEqual(record['accessTokenHash'], __import__('hashlib').sha256(token.encode()).hexdigest())
        self.assertNotIn(token, json.dumps(record))
        self.assertEqual(len(record['photos']), 3)
        self.assertTrue(record['stripPath'].startswith('guest_sessions/'))
        self.assertTrue(record['gifPath'].endswith('.gif'))
        self.assertEqual(len(fake_bucket.store), 5)

if __name__ == '__main__':
    unittest.main()
