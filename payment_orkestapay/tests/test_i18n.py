# Part of payment_orkestapay. See LICENSE file for full copyright and licensing details.

from odoo.tests import tagged

from odoo.addons.payment_orkestapay.tests.common import OrkestaPayCommon


@tagged('post_install', '-at_install')
class TestSpanishTranslations(OrkestaPayCommon):
    """`es_MX` (and any other Spanish) falls back on `i18n/es.po`."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env['res.lang']._activate_lang('es_MX')
        cls.env['ir.module.module']._load_module_terms(
            ['payment_orkestapay'], ['es_MX'], overwrite=True
        )
        cls.env_es = cls.env(context=dict(cls.env.context, lang='es_MX'))

    def test_provider_form_is_translated(self):
        fields = self.env_es['payment.provider'].fields_get(
            ['orkestapay_webhook_secret', 'orkestapay_webhook_url'], ['string', 'help']
        )
        self.assertEqual(
            fields['orkestapay_webhook_secret']['string'], "Secreto de firma del webhook"
        )
        self.assertEqual(fields['orkestapay_webhook_url']['string'], "URL del webhook")
        arch = self.env_es.ref('payment_orkestapay.payment_provider_form').arch
        self.assertIn("Probar conexión", arch)
        self.assertIn("En el portal de OrkestaPay, vaya a Desarrolladores", arch)

    def test_code_messages_are_translated(self):
        self.assertEqual(
            self.env_es._("We could not start the payment with OrkestaPay. Please try again or"
                          " choose another payment method."),
            "No pudimos iniciar el pago con OrkestaPay. Intente de nuevo o elija otro método de"
            " pago.",
        )
        self.assertEqual(
            self.env_es._("Connection successful (%(environment)s)", environment="sandbox"),
            "Conexión exitosa (sandbox)",
        )
