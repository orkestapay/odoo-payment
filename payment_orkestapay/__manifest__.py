# Part of payment_orkestapay. See LICENSE file for full copyright and licensing details.
{
    'name': "OrkestaPay Payment Provider",
    'version': '19.0.1.0.0',
    'category': 'Accounting/Payment Providers',
    'sequence': 350,
    'summary': "Accept card payments in your Odoo shop with the OrkestaPay hosted checkout.",
    'author': "OrkestaPay",
    'website': "https://orkestapay.com",
    'support': "support@orkestapay.com",
    'license': 'LGPL-3',
    'images': ['static/description/banner.png'],
    'depends': ['payment'],
    'data': [
        'views/payment_orkestapay_templates.xml',
        'views/payment_provider_views.xml',
        'views/payment_transaction_views.xml',

        'data/payment_provider_data.xml',  # Depends on views/payment_orkestapay_templates.xml
        'data/ir_cron_data.xml',
    ],
    'post_init_hook': 'post_init_hook',
    'uninstall_hook': 'uninstall_hook',
}
