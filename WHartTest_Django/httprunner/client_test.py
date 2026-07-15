import unittest
from unittest.mock import patch

from requests.exceptions import ConnectionError

from httprunner.client import HttpSession
from httprunner.utils import HTTP_BIN_URL


class TestHttpSession(unittest.TestCase):
    def setUp(self):
        self.session = HttpSession()

    def test_request_http(self):
        self.session.request("get", f"{HTTP_BIN_URL}/get")
        address = self.session.data.address
        self.assertGreater(len(address.server_ip), 0)
        self.assertEqual(address.server_port, 80)
        self.assertGreater(len(address.client_ip), 0)
        self.assertGreater(address.client_port, 10000)

    def test_request_https(self):
        self.session.request("get", "https://postman-echo.com/get")
        address = self.session.data.address
        self.assertGreater(len(address.server_ip), 0)
        self.assertEqual(address.server_port, 443)
        self.assertGreater(len(address.client_ip), 0)
        self.assertGreater(address.client_port, 10000)

    def test_request_http_allow_redirects(self):
        self.session.request(
            "get",
            f"{HTTP_BIN_URL}/redirect-to?url=https%3A%2F%2Fgithub.com",
            allow_redirects=True,
        )
        address = self.session.data.address
        self.assertNotEqual(address.server_ip, "N/A")
        self.assertEqual(address.server_port, 443)
        self.assertNotEqual(address.server_ip, "N/A")
        self.assertGreater(address.client_port, 10000)

    def test_request_https_allow_redirects(self):
        self.session.request(
            "get",
            "https://postman-echo.com/redirect-to?url=https%3A%2F%2Fgithub.com",
            allow_redirects=True,
        )
        address = self.session.data.address
        self.assertNotEqual(address.server_ip, "N/A")
        self.assertEqual(address.server_port, 443)
        self.assertNotEqual(address.server_ip, "N/A")
        self.assertGreater(address.client_port, 10000)

    def test_request_http_not_allow_redirects(self):
        self.session.request(
            "get",
            f"{HTTP_BIN_URL}/redirect-to?url=https%3A%2F%2Fgithub.com",
            allow_redirects=False,
        )
        address = self.session.data.address
        self.assertEqual(address.server_ip, "N/A")
        self.assertEqual(address.server_port, 0)
        self.assertEqual(address.client_ip, "N/A")
        self.assertEqual(address.client_port, 0)

    def test_request_https_not_allow_redirects(self):
        self.session.request(
            "get",
            "https://postman-echo.com/redirect-to?url=https%3A%2F%2Fgithub.com",
            allow_redirects=False,
        )
        address = self.session.data.address
        self.assertEqual(address.server_ip, "N/A")
        self.assertEqual(address.server_port, 0)
        self.assertEqual(address.client_ip, "N/A")
        self.assertEqual(address.client_port, 0)

    def test_transport_error_is_recorded_in_response_data(self):
        with patch(
            "requests.Session.request",
            side_effect=ConnectionError("connection refused"),
        ):
            response = self.session.request("get", "http://example.invalid/api")

        self.assertEqual(response.status_code, 0)
        req_resp = self.session.data.req_resps[-1]
        self.assertTrue(req_resp.response.is_transport_error)
        self.assertEqual(req_resp.response.error_type, "ConnectionError")
        self.assertEqual(req_resp.response.error, "connection refused")
        self.assertEqual(
            req_resp.response.body,
            {
                "transport_error": {
                    "type": "ConnectionError",
                    "message": "connection refused",
                }
            },
        )
