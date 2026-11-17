# Part of payment_orkestapay. See LICENSE file for full copyright and licensing details.

from odoo.tests import BaseCase, tagged

from odoo.addons.payment_orkestapay.utils import WebhookVerificationError, verify_svix_signature

# Official Svix test vector: https://docs.svix.com/receiving/verifying-payloads/how
SVIX_SECRET = 'whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw'
SVIX_MSG_ID = 'msg_p5jXN8AQM9LWM0D4loKWxJek'
SVIX_TIMESTAMP = 1614265330
SVIX_PAYLOAD = b'{"test": 2432232314}'
SVIX_SIGNATURE = 'v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE='

# Svix manual verification guide vector:
# https://docs.svix.com/receiving/verifying-payloads/how-manual
MANUAL_SECRET = 'whsec_plJ3nmyCDGBKInavdOK15jsl'
MANUAL_MSG_ID = 'msg_loFOjxBNrRLzqYUf'
MANUAL_TIMESTAMP = 1731705121
MANUAL_PAYLOAD = b'{"event_type":"ping","data":{"success":true}}'
MANUAL_SIGNATURE = 'v1,rAvfW3dJ/X/qxhsaXPOyyCGmRKsaKWcsNccKXlIktD0='


def _headers(
    msg_id=SVIX_MSG_ID, timestamp=SVIX_TIMESTAMP, signature=SVIX_SIGNATURE, prefix='svix',
):
    return {
        f'{prefix}-id': msg_id,
        f'{prefix}-timestamp': str(timestamp),
        f'{prefix}-signature': signature,
    }


@tagged('post_install', '-at_install')
class TestSvixSignature(BaseCase):

    def test_official_vector_is_valid(self):
        # The clock is "mocked" through `now`, set to the timestamp of the vector.
        verify_svix_signature(SVIX_SECRET, _headers(), SVIX_PAYLOAD, now=SVIX_TIMESTAMP)

    def test_manual_guide_vector_is_valid(self):
        headers = _headers(MANUAL_MSG_ID, MANUAL_TIMESTAMP, MANUAL_SIGNATURE)
        verify_svix_signature(MANUAL_SECRET, headers, MANUAL_PAYLOAD, now=MANUAL_TIMESTAMP + 10)

    def test_altered_body_is_rejected(self):
        with self.assertRaises(WebhookVerificationError):
            verify_svix_signature(
                SVIX_SECRET, _headers(), b'{"test": 2432232315}', now=SVIX_TIMESTAMP
            )

    def test_body_is_used_raw(self):
        """Re-serialized JSON (e.g., without the space) must not validate."""
        with self.assertRaises(WebhookVerificationError):
            verify_svix_signature(
                SVIX_SECRET, _headers(), b'{"test":2432232314}', now=SVIX_TIMESTAMP
            )

    def test_invalid_signature_is_rejected(self):
        headers = _headers(signature='v1,Ceo5qEr07ixe2NLpvHk3FH9bwy/WavXrAFQ/9tdO6mc=')
        with self.assertRaises(WebhookVerificationError):
            verify_svix_signature(SVIX_SECRET, headers, SVIX_PAYLOAD, now=SVIX_TIMESTAMP)

    def test_wrong_secret_is_rejected(self):
        with self.assertRaises(WebhookVerificationError):
            verify_svix_signature(MANUAL_SECRET, _headers(), SVIX_PAYLOAD, now=SVIX_TIMESTAMP)

    def test_expired_timestamp_is_rejected(self):
        for now in (SVIX_TIMESTAMP + 301, SVIX_TIMESTAMP - 301):
            with self.subTest(now=now), self.assertRaises(WebhookVerificationError):
                verify_svix_signature(SVIX_SECRET, _headers(), SVIX_PAYLOAD, now=now)

    def test_timestamp_within_tolerance_is_accepted(self):
        for now in (SVIX_TIMESTAMP + 300, SVIX_TIMESTAMP - 300):
            with self.subTest(now=now):
                verify_svix_signature(SVIX_SECRET, _headers(), SVIX_PAYLOAD, now=now)

    def test_missing_headers_are_rejected(self):
        for missing in ('svix-id', 'svix-timestamp', 'svix-signature'):
            headers = _headers()
            del headers[missing]
            with self.subTest(missing=missing), self.assertRaises(WebhookVerificationError):
                verify_svix_signature(SVIX_SECRET, headers, SVIX_PAYLOAD, now=SVIX_TIMESTAMP)
        with self.assertRaises(WebhookVerificationError):
            verify_svix_signature(SVIX_SECRET, {}, SVIX_PAYLOAD, now=SVIX_TIMESTAMP)

    def test_webhook_prefixed_headers_are_accepted(self):
        headers = _headers(prefix='webhook')
        verify_svix_signature(SVIX_SECRET, headers, SVIX_PAYLOAD, now=SVIX_TIMESTAMP)

    def test_one_valid_signature_among_several_is_enough(self):
        signature = f'v1,Ceo5qEr07ixe2NLpvHk3FH9bwy/WavXrAFQ/9tdO6mc= {SVIX_SIGNATURE}'
        verify_svix_signature(
            SVIX_SECRET, _headers(signature=signature), SVIX_PAYLOAD, now=SVIX_TIMESTAMP
        )

    def test_other_signature_versions_are_ignored(self):
        signature = SVIX_SIGNATURE.replace('v1,', 'v2,')
        with self.assertRaises(WebhookVerificationError):
            verify_svix_signature(
                SVIX_SECRET, _headers(signature=signature), SVIX_PAYLOAD, now=SVIX_TIMESTAMP
            )

    def test_missing_or_malformed_secret_is_rejected(self):
        for secret in (False, '', 'whsec_not-base64!'):
            with self.subTest(secret=secret), self.assertRaises(WebhookVerificationError):
                verify_svix_signature(secret, _headers(), SVIX_PAYLOAD, now=SVIX_TIMESTAMP)

    def test_non_numeric_timestamp_is_rejected(self):
        with self.assertRaises(WebhookVerificationError):
            verify_svix_signature(
                SVIX_SECRET, _headers(timestamp='yesterday'), SVIX_PAYLOAD, now=SVIX_TIMESTAMP
            )
