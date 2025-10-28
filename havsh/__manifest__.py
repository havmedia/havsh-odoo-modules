# -*- coding: utf-8 -*-
{
    'name': 'HAV.sh Companion Module',
    'version': '17.0.1.0.0',
    'summary': 'Synchronize Data with hav.sh',
    'description': 'Keeps Odoo synchronized with the external management platform hav.sh using real-time webhooks and scheduled full syncs.',
    'category': 'Hidden',
    'author': 'HAV Media GmbH',
    'website': 'https://hav.media',
    'license': 'OPL-1',
    'depends': ['base'],
    'auto_install': ['web'],
    'data': [
        'data/ir_cron.xml',
    ],
    'installable': True,
    'application': False,
}