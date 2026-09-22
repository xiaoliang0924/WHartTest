from django.test import SimpleTestCase

from api_testcases.rate_limit import (
    is_rate_limited_response,
    parse_rate_limit_wait_seconds,
)


class RateLimitHelperTest(SimpleTestCase):
    def test_detects_http_429(self):
        self.assertTrue(is_rate_limited_response(429))

    def test_detects_invalid_id_style_400_rate_limit(self):
        body = {
            "statusCode": 400,
            "code": "RATE_LIMIT_EXCEEDED",
            "message": "Rate limit exceeded, retry in 58 seconds",
        }
        self.assertTrue(is_rate_limited_response(400, body))

    def test_parse_retry_in_message(self):
        body = {"message": "Rate limit exceeded, retry in 58 seconds"}
        wait = parse_rate_limit_wait_seconds(status_code=400, body=body)
        self.assertEqual(wait, 59.0)

    def test_non_rate_limit_400(self):
        body = {"message": "工单ID无效"}
        self.assertFalse(is_rate_limited_response(400, body))
