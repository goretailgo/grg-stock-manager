{
    'name': 'GRG Stock Transfer API',
    'version': '19.0.1.2.0',
    'summary': 'Central warehouse to store stock transfer APIs (Bearer + JSON-RPC) with PDF report, and per-user POS access',
    'author': 'GoRetailGo',
    'category': 'Inventory',
    'license': 'LGPL-3',
    'depends': ['stock', 'point_of_sale'],
    'data': [
        'security/pos_access_rules.xml',
        'views/views.xml',
        'report/transfer_report.xml',
    ],
    'installable': True,
    'application': False,
}
