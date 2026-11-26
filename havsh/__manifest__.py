# -*- coding: utf-8 -*-
{
    'name': 'HAV.sh Companion Module',
    'version': '19.0.1.0.0',
    'summary': 'Synchronize Data with hav.sh',
    'description': 'Keeps the module list of the external management platform hav.sh up to date: after a change the changed modules are sent together, and all of them once a day.',
    'category': 'Hidden',
    'author': 'HAV Media GmbH',
    'website': 'https://hav.media',
    'license': 'OPL-1',
    'depends': ['base'],
    'auto_install': [],
    'data': [
        'data/ir_cron.xml',
    ],
    'installable': True,
    'uninstall_hook': '_uninstall_hook',
    'application': False,
}