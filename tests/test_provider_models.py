import json
import unittest
import urllib.error
from io import BytesIO
from unittest.mock import patch, MagicMock
from local_web.provider_models import fetch_provider_models, ModelDirectoryError, NoRedirect

class DirectoryTest(unittest.TestCase):
    def fetch(self, data):
        opener = MagicMock()
        opener.open.return_value = BytesIO(json.dumps(data).encode())
        with patch('urllib.request.build_opener', return_value=opener):
            result = fetch_provider_models('https://example.com/v1/', 'fake-key')
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, 'https://example.com/v1/models')
        self.assertEqual(request.get_header('Authorization'), 'Bearer fake-key')
        self.assertEqual(request.get_method(), 'GET')
        return result

    def test_normalizes_and_deduplicates_ids(self):
        self.assertEqual(self.fetch({'data':[{'id':' model-a '},{'id':'model-a'}, {'id':'b'}, {}, 'bad']}), {'models':['model-a','b']})

    def test_empty_directory(self):
        self.assertEqual(self.fetch({'data':[]}), {'models':[]})

    def test_invalid_response(self):
        with self.assertRaises(ModelDirectoryError): self.fetch({'models':[]})

    def test_errors_do_not_echo_key(self):
        opener=MagicMock()
        opener.open.side_effect=urllib.error.HTTPError('https://example.com',401,'bad',{},BytesIO(b'fake-key'))
        with patch('urllib.request.build_opener',return_value=opener):
            with self.assertRaises(ModelDirectoryError) as caught:
                fetch_provider_models('https://example.com','fake-key')
        self.assertNotIn('fake-key',str(caught.exception))
        self.assertIn('401',str(caught.exception))

    def test_rejects_insecure_url_and_empty_key(self):
        with self.assertRaises(ValueError): fetch_provider_models('http://example.com','key')
        with self.assertRaises(ModelDirectoryError): fetch_provider_models('https://example.com',' ')

    def test_redirects_cannot_forward_key(self):
        self.assertIsNone(NoRedirect().redirect_request(None,None,302,'',{},'https://other.example'))
