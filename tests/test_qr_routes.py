"""
Legacy QR route removed.
"""


def test_legacy_route_is_gone(app):
    assert not any('Marti' in rule.rule for rule in app.url_map.iter_rules())
