# Part of payment_orkestapay. See LICENSE file for full copyright and licensing details.

import base64
import binascii
import hashlib
import hmac
import re
import time

from odoo.exceptions import ValidationError

from odoo.addons.payment_orkestapay import const


class OrkestaPayApiError(ValidationError):
    """Error raised when a call to the OrkestaPay API fails.

    It subclasses `ValidationError` so that the generic payment flows that catch `ValidationError`
    keep working, while carrying the details needed to tell a timeout from a rejected request.
    """

    def __init__(
        self, message, *, status_code=None, error_code=None, request_id=None,
        is_network_error=False,
    ):
        super().__init__(message)
        self.status_code = status_code  # HTTP status code, None if no response was received.
        self.error_code = error_code  # OrkestaPay error `code`, e.g. `INVALID_STATUS`.
        self.request_id = request_id  # OrkestaPay `request_id`, for support.
        self.is_network_error = is_network_error  # Timeout or connection error.

    @property
    def is_retryable(self):
        """Whether the outcome of the request is unknown (timeout, connection error or 5xx)."""
        return self.is_network_error or (self.status_code or 0) >= 500


class WebhookVerificationError(Exception):
    """Raised when a webhook does not carry a valid Svix signature."""


# `access_token=<value>` in URLs, and `"access_token": "<value>"` or `'client_secret': '<value>'`
# in JSON or Python reprs.
_SENSITIVE_TEXT_PATTERNS = [
    re.compile(r'(access_token=)[^&\s\'"]+'),
    *(
        re.compile(rf'([\'"]{key}[\'"]\s*:\s*[\'"])[^\'"]*')
        for key in sorted(const.SENSITIVE_KEYS)
    ),
]


def mask_sensitive_text(text):
    """Mask access tokens and secrets in a text before logging it.

    :param str text: The text to mask (a payload repr or a response body).
    :return: The masked text.
    :rtype: str
    """
    for pattern in _SENSITIVE_TEXT_PATTERNS:
        text = pattern.sub(r'\1[REDACTED]', text or '')
    return text


def _get_header(headers, name):
    """Return the value of a Svix header, falling back to its white-label `webhook-` variant.

    :param headers: The request headers (any mapping with a case-insensitive `get`).
    :param str name: The header name without prefix (`id`, `timestamp` or `signature`).
    :return: The header value, or None.
    :rtype: str|None
    """
    return headers.get(f'svix-{name}') or headers.get(f'webhook-{name}')


def verify_svix_signature(secret, headers, raw_body, now=None):
    """Verify the Svix signature of a webhook, as documented in
    https://docs.svix.com/receiving/verifying-payloads/how-manual.

    The signed content is `{msg_id}.{timestamp}.{raw_body}`, signed with HMAC-SHA256 using the
    base64-decoded secret (without its `whsec_` prefix). The signature header may contain several
    space-separated signatures prefixed by their version (`v1,<base64>`); one valid `v1` signature
    is enough.

    :param str secret: The endpoint's signing secret (`whsec_...`).
    :param headers: The request headers.
    :param bytes raw_body: The raw request body, exactly as received.
    :param float now: The current Unix time; defaults to `time.time()` (used by tests).
    :return: None
    :raise WebhookVerificationError: If the signature is missing, expired or invalid.
    """
    msg_id = _get_header(headers, 'id')
    timestamp = _get_header(headers, 'timestamp')
    signature_header = _get_header(headers, 'signature')
    if not (msg_id and timestamp and signature_header):
        raise WebhookVerificationError("missing signature headers")

    try:
        timestamp_value = int(timestamp)
    except ValueError:
        raise WebhookVerificationError("invalid timestamp header") from None
    now = time.time() if now is None else now
    if abs(now - timestamp_value) > const.WEBHOOK_TIMESTAMP_TOLERANCE:
        raise WebhookVerificationError("timestamp outside of the tolerance window")

    if not secret:
        raise WebhookVerificationError("no signing secret configured")
    try:
        key = base64.b64decode(secret.removeprefix('whsec_'), validate=True)
    except (binascii.Error, ValueError):
        raise WebhookVerificationError("malformed signing secret") from None

    if isinstance(raw_body, str):
        raw_body = raw_body.encode()
    signed_content = f'{msg_id}.{timestamp}.'.encode() + raw_body
    expected_signature = base64.b64encode(
        hmac.new(key, signed_content, hashlib.sha256).digest()
    )
    for versioned_signature in signature_header.split(' '):
        version, _sep, signature = versioned_signature.partition(',')
        if version != 'v1' or not signature:
            continue
        if hmac.compare_digest(expected_signature, signature.encode()):
            return
    raise WebhookVerificationError("no matching signature")
