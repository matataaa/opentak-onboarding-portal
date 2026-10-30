"""
Switchable modules (meshtastic, radios, events; see MODULE_SETTINGS in models.py).

ONE `before_request` hook on the `api_v1` blueprint refuses every request to a
disabled module's endpoints with 403 MODULE_DISABLED, for every auth type
(JWT, API key, kiosk, event leader, anonymous). 403 rather than 401, so the
SPA's refresh interceptor does not loop. Settings and auth endpoints are never
gated, so an admin can always turn a module back on.
"""
from flask import jsonify, request

from app.api_v1 import api_v1
from app.models import SystemSettingsModel

# Path prefix -> module
MODULE_PREFIXES = (
    ('/api/v1/meshtastic', 'meshtastic'),
    ('/api/v1/radios', 'radios'),
    ('/api/v1/events', 'events'),
)

# Radio endpoints that use Meshtastic channel configs also need the meshtastic module
MESHTASTIC_RADIO_ENDPOINTS = frozenset({
    'api_v1.get_program_config',
    'api_v1.compare_config',
    'api_v1.enroll_radio',
})


def _required_modules():
    path = request.path
    modules = [module for prefix, module in MODULE_PREFIXES
               if path == prefix or path.startswith(prefix + '/')]
    if request.endpoint in MESHTASTIC_RADIO_ENDPOINTS:
        modules.append('meshtastic')
    return modules


@api_v1.before_request
def refuse_disabled_modules():
    if request.method == 'OPTIONS':
        return None
    for module in _required_modules():
        if not SystemSettingsModel.is_module_enabled(module):
            return jsonify({'error': f'The {module} module is disabled', 'code': 'MODULE_DISABLED'}), 403
    return None
