import unittest, json, hashlib
from unittest.mock import patch
import cloud_service

class CloudSecuritySmokeTests(unittest.TestCase):
    def setUp(self): self.client=cloud_service.app.test_client()
    def test_internal_upload_requires_service_secret(self):
        self.assertEqual(self.client.post('/internal/sessions',data={}).status_code,401)
    def test_internal_lookup_requires_service_secret(self):
        self.assertEqual(self.client.get('/internal/sessions/20261116-120000-a1b2c3').status_code,401)
    def test_guest_route_rejects_invalid_session_and_token_before_supabase(self):
        self.assertEqual(self.client.get('/g/not-a-session/not-a-token').status_code,404)
    def test_guest_route_rejects_malformed_token(self):
        self.assertEqual(self.client.get('/g/20261116-120000-a1b2c3/short').status_code,404)
    def test_health_requires_supabase_configuration(self):
        with patch.dict('os.environ',{},clear=True):
            self.assertEqual(self.client.get('/health').status_code,503)
    def test_token_hash_is_sha256(self):
        token='T'*48
        self.assertEqual(hashlib.sha256(token.encode()).hexdigest(),hashlib.sha256(token.encode('utf-8')).hexdigest())

if __name__=='__main__': unittest.main()
