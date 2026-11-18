# Part of payment_orkestapay. See LICENSE file for full copyright and licensing details.

from odoo.tests import BaseCase, tagged

from odoo.addons.payment_orkestapay.utils import is_checkout_url


@tagged('post_install', '-at_install')
class TestCheckoutUrl(BaseCase):

    def test_orkestapay_https_urls_are_accepted(self):
        for url in (
            'https://checkout.sand.orkestapay.com/chk_1?h=abc',
            'https://checkout.orkestapay.com/chk_1',
            'https://orkestapay.com/chk_1',
            'HTTPS://Checkout.OrkestaPay.com/chk_1',
            'https://checkout.orkestapay.com:443/chk_1',
        ):
            with self.subTest(url=url):
                self.assertTrue(is_checkout_url(url))

    def test_other_urls_are_rejected(self):
        for url in (
            'http://checkout.orkestapay.com/chk_1',  # Not HTTPS.
            'javascript:alert(1)',
            '//checkout.orkestapay.com/chk_1',  # No scheme.
            'https://example.com/chk_1',
            'https://evilorkestapay.com/chk_1',
            'https://checkout.orkestapay.com.example.com/chk_1',
            'https://checkout.orkestapay.com@example.com/chk_1',
            'https://user@checkout.orkestapay.com/chk_1',  # Credentials.
            'https://example.com\\@checkout.orkestapay.com/chk_1',  # Browsers read `\\` as `/`.
            'https://checkout.orkestapay.com:8443/chk_1',
            'https://checkout.orkestapay.com/chk 1',
            'https://checkout.orkestapay.com/chk_1\n',
            'https://checkout.orkestapay.com/chk_1\t',
            'https://evil.com%2f.orkestapay.com/chk_1',  # Percent-encoded host.
            'https://checkout.orkestap\u0430y.com/chk_1',  # Non-ASCII (Cyrillic a).
            'https://evil.com\uff0f.orkestapay.com/chk_1',  # Fullwidth slash.
            'https://[bad/chk_1',  # Malformed.
            'https://checkout.orkestapay.com:99999/chk_1',  # Invalid port.
            '',
            None,
            {'url': 'https://checkout.orkestapay.com/chk_1'},
        ):
            with self.subTest(url=url):
                self.assertFalse(is_checkout_url(url))
