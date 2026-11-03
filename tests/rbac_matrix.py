"""
Route x identity access matrix: every route in the url_map, every method, for every built-in
admin role and the role-less identities, against real seeded rows with OTS mocked.

A cell is "<status>", plus "|<error text>" on a 403 and "~<probe>" when the case has a probe (what
the response showed or what the call really changed: a field that is silently ignored still gives 200).
Every call starts from the same database (restored from a snapshot) and the same upload folders,
so a DELETE cannot influence later results.

tests/rbac_golden.json is the expected matrix; tests/test_rbac_golden.py compares against it.
It is a behaviour freeze: regenerate it only for a deliberate access change, and review the diff.

A second file pins each route to its permission key, which the built-in roles cannot tell (a built-in
<x>_admin holds every key of its module): tests/rbac_key_sweep.json holds, per case of a module, the keys of
that module that change its status or probe. See key_sweep().

    python -m tests.rbac_matrix --write        regenerate tests/rbac_golden.json
    python -m tests.rbac_matrix --write-sweep  regenerate tests/rbac_key_sweep.json
    python -m tests.rbac_matrix --stats        print per-identity counts of the current code

Parity with the role-based guards was proven by generating rbac_golden.json from the code at commit
4e6056e (the last one with those guards) using this harness: to add a probe or case, do that again
instead of regenerating from current code.
"""
import argparse
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from collections import Counter
from datetime import datetime, timedelta
from unittest import mock

from tests import conftest  # noqa: F401  (sets the test environment before app is imported)

import requests
from flask_jwt_extended import create_access_token, create_refresh_token

from app import create_app
from app.api_v1 import (events as events_mod, magic_link as magic_link_mod, oidc as oidc_mod, qr as qr_mod,
                        tak_profiles as tak_mod)
from app.models import (AnnouncementModel, ApiKeyModel, ChannelGroupMembership, EventLeaderSessionModel, EventLinkModel,
                        EventModel, EventSessionModel, EventTeamLeaderModel, EventTeamModel,
                        GroupOnboardingCodeAssociation, GroupUserAssociation, KioskSessionModel, MeshtasticChannelGroup,
                        MeshtasticModel, OIDCProviderModel, OnboardingCodeModel, OTSGroupModel, PendingRegistrationModel,
                        RadioModel, SystemSettingsModel, TakProfileModel, UserModel, UserRoleModel, db)
from app.ots import OTSClient
from app.rbac import ADMIN_ROLES

GOLDEN_PATH = os.path.join(os.path.dirname(__file__), 'rbac_golden.json')
SWEEP_PATH = os.path.join(os.path.dirname(__file__), 'rbac_key_sweep.json')
API = '/api/v1'
GOOD_PASSWORD = 'Str0ngpassword'
# A valid 1x1 PNG header is all the upload checks look at
PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 32

ANONYMOUS = 'anonymous'
NO_ROLE = 'no_role'  # real user without any role
# Role-less identities besides the built-in roles. The kiosk and leader accounts hold the
# administrator role in the database: those sessions must not act with it.
SPECIAL_IDENTITIES = [
    ANONYMOUS,
    NO_ROLE,
    'no_user_row',  # valid token whose user does not exist (no role rows can be found for it)
    'admin_row',  # holds a role named 'admin': privileged in OTS terms, no portal permissions
    'ots_user',  # holds the OTS role 'user'
    'audience',  # holds a plain role that only serves as an audience
    'event_leader',
    'kiosk',
]
IDENTITIES = list(ADMIN_ROLES) + SPECIAL_IDENTITIES


def _body(resp):
    return resp.get_json(silent=True) or {}


def _names(items, key='name'):
    return sorted(str(i[key]) for i in items)


def probe_keys(key):
    """The sorted names found under a list key of the response"""
    return lambda resp, ids: ','.join(_names(_body(resp).get(key, []))) if key in _body(resp) else None


def probe_field(*fields):
    return lambda resp, ids: json.dumps({f: _body(resp)[f] for f in fields if f in _body(resp)}, sort_keys=True)


_VOLATILE = re.compile(r'(At|_at|^expires.*|^is_expired)$')


def _without_volatile(value):
    if isinstance(value, dict):
        return {k: _without_volatile(v) for k, v in value.items() if not _VOLATILE.search(k)}
    if isinstance(value, list):
        return [_without_volatile(v) for v in value]
    return value


def probe_body(resp, ids):
    """The whole JSON response without timestamps, for routes that mask or scope what they show"""
    body = resp.get_json(silent=True)
    return json.dumps(_without_volatile(body), sort_keys=True) if resp.status_code == 200 and body else None


def probe_shape(resp, ids):
    """Only which fields: for a response about the caller, whose values differ per identity"""
    return ','.join(sorted(_body(resp))) if resp.status_code == 200 else None


def probe_self(resp, ids):
    """What a PUT really changed on the caller, leaving out what differs per identity anyway"""
    user = db.session.get(UserModel, ids['self'])
    if not user:
        return None
    return json.dumps({
        'gotAudienceRole': 'matrix_audience' in [r.name for r in user.roles],
        'groups': sorted(a.group_id for a in user.group_associations),
        'firstName': user.firstName,
        'emailChanged': user.email != f'{user.username}@example.com',
        'expiry': bool(user.expiryDate),
        'passwordResets': FakeState.password_resets,
    }, sort_keys=True)


def probe_permissions(resp, ids):
    body = _body(resp)
    return json.dumps({k: body.get(k) for k in ('roles', 'modules', 'isAdmin')}, sort_keys=True)


def probe_role_list(resp, ids):
    roles = _body(resp).get('roles', [])
    return 'userCount' if roles and all('userCount' in r for r in roles) else ('no-userCount' if roles else None)


def probe_announcement_list(resp, ids):
    announcements = _body(resp).get('announcements')
    if not announcements:
        return None
    return json.dumps(sorted(a['emailLastError'] or '' for a in announcements))


def probe_announcement_detail(resp, ids):
    body = _body(resp)
    if 'emailPending' not in body:
        return None
    return json.dumps({'lastError': body['emailLastError'], 'pending': body['emailPending'],
                       'failures': body['emailFailures']}, sort_keys=True)


def probe_user(user_key):
    """What a PUT really changed on a user"""
    def probe(resp, ids):
        user = db.session.get(UserModel, ids[user_key])
        return json.dumps({
            'roles': sorted(r.name for r in user.roles),
            'groups': sorted(f'{a.group_id}:{a.direction}' for a in user.group_associations),
            'email': user.email,
            'firstName': user.firstName,
            'expiry': bool(user.expiryDate),
            'passwordResets': FakeState.password_resets,
        }, sort_keys=True)
    return probe


def probe_code(code_key):
    def probe(resp, ids):
        code = db.session.get(OnboardingCodeModel, ids[code_key])
        return json.dumps({
            'roles': sorted(r.name for r in code.roles),
            'groups': sorted(f'{a.group_id}:{a.direction}' for a in GroupOnboardingCodeAssociation.query.filter_by(
                onboardingcode_id=code.id)),
            'approver': code.approverRoleId,
            'requireApproval': code.requireApproval,
            'autoApprove': code.autoApprove,
            'description': code.description,
        }, sort_keys=True)
    return probe


def probe_new_code(resp, ids):
    code = OnboardingCodeModel.get_onboarding_code_by_code('matrixnewcode')
    if not code:
        return None
    return json.dumps({
        'roles': sorted(r.name for r in code.roles),
        'groups': sorted(a.group_id for a in GroupOnboardingCodeAssociation.query.filter_by(onboardingcode_id=code.id)),
        'approver': code.approverRoleId,
        'requireApproval': code.requireApproval,
    }, sort_keys=True)


def probe_new_user(resp, ids):
    user = UserModel.query.filter_by(username='matrixnew').first()
    if not user:
        return None
    return json.dumps({'roles': sorted(r.name for r in user.roles), 'groups': sorted(a.group_id for a in user.group_associations)})


def probe_radio(resp, ids):
    radio = db.session.get(RadioModel, ids['radio_other'])
    return json.dumps({'assignedTo': radio.assignedTo, 'owner': radio.owner, 'description': radio.description},
                      sort_keys=True)


def probe_new_radio(resp, ids):
    radio = RadioModel.query.filter_by(name='matrix-new-radio').first()
    return json.dumps({'assignedTo': radio.assignedTo, 'owner': radio.owner}, sort_keys=True) if radio else None


def probe_profile(resp, ids):
    profile = db.session.get(TakProfileModel, ids['profile_public'])
    return json.dumps({'roles': sorted(r.name for r in profile.roles), 'description': profile.description,
                       'isPublic': profile.isPublic}, sort_keys=True)


def probe_new_profile(resp, ids):
    profile = TakProfileModel.query.filter_by(name='matrix-new-profile').first()
    return json.dumps({'roles': sorted(r.name for r in profile.roles), 'isPublic': profile.isPublic}) if profile else None


def probe_announcement(key):
    def probe(resp, ids):
        a = db.session.get(AnnouncementModel, ids[key])
        return json.dumps({'status': a.status, 'emailStatus': a.email_status, 'sendEmail': a.send_email,
                           'title': a.title}, sort_keys=True)
    return probe


def probe_new_announcement(resp, ids):
    a = AnnouncementModel.query.filter_by(title='matrix-new').first()
    return json.dumps({'status': a.status, 'emailStatus': a.email_status}, sort_keys=True) if a else None


def probe_setting(key):
    return lambda resp, ids: SystemSettingsModel.query.filter_by(key=key).first().value


def probe_team(resp, ids):
    team = db.session.get(EventTeamModel, ids['team_b'])
    return json.dumps({'leader': team.leaderUserId, 'color': team.color}, sort_keys=True)


def enable_user_programming(ids):
    SystemSettingsModel.set_setting('user_program_radio_enabled', 'true')
    SystemSettingsModel.set_setting('user_validate_radio_enabled', 'true')


def _zip_bytes():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('manifest.xml', '<x/>')
    return buf.getvalue()


class Case:
    def __init__(self, name, method, path, json_body=None, form=None, files=None, setup=None, probe=None):
        self.name, self.method, self.path = name, method, path
        self.json_body, self.form, self.files = json_body, form, files
        self.setup, self.probe = setup, probe


def _settings_cases():
    """A case for every setting, by key: writing a setting is authorised per key."""
    with_defaults = [
        ('notify_admin_pending_registration', 'false'), ('notify_admin_new_registration', 'false'),
        ('allow_manual_approval', 'false'), ('remove_expired_accounts_enabled', 'false'),
        ('brand_name_enabled', 'true'), ('brand_name_value', 'x'), ('help_link_enabled', 'true'),
        ('help_link_value', 'x'), ('help_email_enabled', 'true'), ('help_email_value', 'x'),
        ('open_in_atak_enabled', 'true'), ('atak_homepage_icon_enabled', 'true'),
        ('itak_homepage_icon_enabled', 'true'), ('truststore_homepage_icon_enabled', 'true'),
        ('meshtastic_homepage_icon_enabled', 'true'), ('claim_radio_enabled', 'true'),
        ('user_program_radio_enabled', 'true'), ('user_validate_radio_enabled', 'true'),
        ('user_radio_enrollment_enabled', 'true'), ('oidc_enabled', 'true'), ('oidc_auto_create_user', 'true'),
        ('meshtastic_enabled', 'true'), ('radios_enabled', 'true'), ('events_enabled', 'true'),
        ('atak_installer_qr_enabled', 'true'), ('atak_installer_qr_url', 'x'), ('itak_installer_qr_enabled', 'true'),
        ('itak_installer_qr_url', 'x'), ('meshtastic_installer_qr_android_enabled', 'true'),
        ('meshtastic_installer_qr_android_url', 'x'), ('meshtastic_installer_qr_iphone_enabled', 'true'),
        ('meshtastic_installer_qr_iphone_url', 'x'), ('generate_atak_qr_code', 'true'),
        ('generate_itak_qr_code', 'true'), ('qr_token_expiry_minutes', '60'), ('qr_token_max_uses', '1'),
        ('callsign_qr_code_enabled', 'true'), ('forgot_password_enabled', 'true'), ('custom_logo_enabled', 'true'),
        ('custom_logo_path', 'x'), ('logo_display_mode', 'logo_and_text'), ('primary_color', '#000000'),
        ('accent_color', '#ff9800'), ('login_layout', 'classic'), ('login_background_path', 'x'),
        ('login_overlay_strength', 'medium'), ('login_glass_enabled', 'true'), ('login_header_bar_enabled', 'true'),
        ('login_hero_texts', '{}'), ('kiosk_enrollment_enabled', 'true'), ('kiosk_session_timeout_minutes', '10'),
        ('magic_link_login_enabled', 'true'), ('magic_link_expiry_minutes', '15'), ('default_theme', 'light'),
        ('kiosk_default_theme', 'dark'), ('kiosk_language', 'en'), ('date_format', 'eu'),
    ]
    return [Case(f'PUT setting {key}', 'PUT', f'{API}/admin/settings/key/{key}', {'value': value},
                 probe=probe_setting(key)) for key, value in with_defaults]


def build_cases():
    """{name, method, path, ...}: path placeholders are seeded ids; {self} is the caller's user id."""
    a = API
    c = [
        # ---------------------------------------------------------------- announcements
        Case('list announcements (admin)', 'GET', f'{a}/admin/announcements', probe=probe_announcement_list),
        Case('create announcement draft', 'POST', f'{a}/admin/announcements',
             {'title': 'matrix-new', 'content': 'c', 'targetType': 'all'}, probe=probe_new_announcement),
        Case('create announcement send now', 'POST', f'{a}/admin/announcements',
             {'title': 'matrix-new', 'content': 'c', 'targetType': 'all', 'sendImmediately': True, 'sendEmail': True},
             probe=probe_new_announcement),
        Case('create announcement scheduled', 'POST', f'{a}/admin/announcements',
             {'title': 'matrix-new', 'content': 'c', 'targetType': 'all', 'sendEmail': True,
              'scheduledAt': '2099-01-01T00:00:00'}, probe=probe_new_announcement),
        Case('get announcement (admin)', 'GET', f'{a}/admin/announcements/{{ann_sent}}',
             probe=probe_announcement_detail),
        Case('update announcement draft', 'PUT', f'{a}/admin/announcements/{{ann_draft}}', {'title': 'edited'},
             probe=probe_announcement('ann_draft')),
        Case('update announcement to scheduled', 'PUT', f'{a}/admin/announcements/{{ann_draft}}',
             {'scheduledAt': '2099-01-01T00:00:00', 'sendEmail': True}, probe=probe_announcement('ann_draft')),
        Case('update scheduled announcement', 'PUT', f'{a}/admin/announcements/{{ann_scheduled}}', {'title': 'edited'},
             probe=probe_announcement('ann_scheduled')),
        Case('delete announcement', 'DELETE', f'{a}/admin/announcements/{{ann_draft}}'),
        Case('send announcement', 'POST', f'{a}/admin/announcements/{{ann_draft}}/send',
             probe=probe_announcement('ann_draft')),
        Case('user announcements', 'GET', f'{a}/announcements'),
        Case('dismiss announcement', 'POST', f'{a}/announcements/{{ann_sent}}/dismiss'),
        Case('announcement pixel', 'GET', f'{a}/announcements/{{ann_sent}}/pixel/{{target}}.gif'),
        Case('read announcement', 'POST', f'{a}/announcements/{{ann_sent}}/read'),
        Case('announcement history', 'GET', f'{a}/announcements/history'),
        Case('announcement unread count', 'GET', f'{a}/announcements/unread-count'),
        # ---------------------------------------------------------------- api keys
        Case('list api keys', 'GET', f'{a}/admin/api-keys'),
        Case('create api key', 'POST', f'{a}/admin/api-keys', {'name': 'matrix', 'permissions': ['users:read']}),
        Case('get api key', 'GET', f'{a}/admin/api-keys/{{api_key}}'),
        Case('update api key', 'PUT', f'{a}/admin/api-keys/{{api_key}}', {'description': 'x'}),
        Case('delete api key', 'DELETE', f'{a}/admin/api-keys/{{api_key}}'),
        Case('regenerate api key', 'POST', f'{a}/admin/api-keys/{{api_key}}/regenerate'),
        Case('api key permissions', 'GET', f'{a}/admin/api-keys/permissions'),
        # ---------------------------------------------------------------- logo / settings / oidc
        Case('upload background', 'POST', f'{a}/admin/login-background',
             files=lambda: {'background': (io.BytesIO(PNG), 'bg.png')}),
        Case('delete background', 'DELETE', f'{a}/admin/login-background'),
        Case('upload logo', 'POST', f'{a}/admin/logo', files=lambda: {'logo': (io.BytesIO(PNG), 'logo.png')}),
        Case('delete logo', 'DELETE', f'{a}/admin/logo'),
        Case('get logo settings', 'GET', f'{a}/admin/logo'),
        Case('list oidc providers (admin)', 'GET', f'{a}/admin/oidc/providers'),
        Case('create oidc provider', 'POST', f'{a}/admin/oidc/providers',
             {'name': 'matrixnew', 'display_name': 'n', 'discovery_url': 'https://idp.invalid/x', 'client_id': 'i',
              'client_secret': 's'}),
        Case('update oidc provider', 'PUT', f'{a}/admin/oidc/providers/{{oidc}}', {'display_name': 'x'}),
        Case('delete oidc provider', 'DELETE', f'{a}/admin/oidc/providers/{{oidc}}'),
        Case('upload oidc icon', 'POST', f'{a}/admin/oidc/providers/{{oidc}}/icon',
             files=lambda: {'icon': (io.BytesIO(PNG), 'icon.png')}),
        Case('delete oidc icon', 'DELETE', f'{a}/admin/oidc/providers/{{oidc}}/icon'),
        Case('get settings (admin)', 'GET', f'{a}/admin/settings'),
        Case('update setting by id', 'PUT', f'{a}/admin/settings/{{setting}}', {'value': 'x'},
             probe=probe_setting('brand_name_value')),
        Case('update setting by id, module flag', 'PUT', f'{a}/admin/settings/{{setting_module}}', {'value': 'true'}),
        Case('update unknown setting by key', 'PUT', f'{a}/admin/settings/key/no_such_setting', {'value': 'x'}),
        Case('update setting without value', 'PUT', f'{a}/admin/settings/key/brand_name_value', {}),
        Case('update setting by unknown id', 'PUT', f'{a}/admin/settings/999999', {'value': 'x'}),
        # ---------------------------------------------------------------- approvals
        Case('my approvals', 'GET', f'{a}/approvals'),
        Case('approval approve', 'POST', f'{a}/approvals/{{pending_approval}}/approve'),
        Case('approval reject', 'POST', f'{a}/approvals/{{pending_approval}}/reject', {'reason': 'x'}),
        Case('approval check', 'GET', f'{a}/approvals/check'),
        Case('registration approve token', 'GET', f'{a}/registration/approve/approvaltoken'),
        Case('registration reject token', 'GET', f'{a}/registration/reject/approvaltoken'),
        # ---------------------------------------------------------------- auth
        Case('change password', 'POST', f'{a}/auth/change-password',
             {'currentPassword': GOOD_PASSWORD, 'newPassword': GOOD_PASSWORD + '1'}),
        Case('complete profile', 'POST', f'{a}/auth/complete-profile', {'callsign': 'CS'}),
        Case('forgot password', 'POST', f'{a}/auth/forgot-password', {'email': 'nobody@example.com'}),
        Case('impersonate', 'POST', f'{a}/auth/impersonate/{{target}}'),
        Case('login', 'POST', f'{a}/auth/login', {'username': 'nobody', 'password': 'wrong'}),
        Case('magic link', 'POST', f'{a}/auth/magic-link', {'email': 'nobody@example.com'}),
        Case('magic link verify', 'POST', f'{a}/auth/magic-link/verify', {'token': 'nope'}),
        Case('auth me', 'GET', f'{a}/auth/me'),
        Case('oidc authorize', 'GET', f'{a}/auth/oidc/{{oidc}}/authorize'),
        Case('oidc callback', 'GET', f'{a}/auth/oidc/{{oidc}}/callback'),
        Case('oidc providers', 'GET', f'{a}/auth/oidc/providers'),
        Case('auth permissions', 'GET', f'{a}/auth/permissions', probe=probe_permissions),
        Case('refresh', 'POST', f'{a}/auth/refresh'),
        Case('register', 'POST', f'{a}/auth/register', {}),
        Case('reset password', 'POST', f'{a}/auth/reset-password', {'token': 'nope', 'password': GOOD_PASSWORD}),
        Case('set password', 'POST', f'{a}/auth/set-password', {'password': GOOD_PASSWORD}),
        Case('verify email', 'POST', f'{a}/auth/verify-email', {'token': 'nope'}),
        # ---------------------------------------------------------------- events
        Case('list events', 'GET', f'{a}/events', probe=probe_keys('events')),
        Case('create event', 'POST', f'{a}/events',
             {'name': 'matrix-new', 'validFrom': '2026-01-01T00:00:00', 'validUntil': '2099-01-01T00:00:00'}),
        Case('get event', 'GET', f'{a}/events/{{event}}'),
        Case('update event', 'PUT', f'{a}/events/{{event}}', {'description': 'x'}),
        Case('delete event', 'DELETE', f'{a}/events/{{event}}'),
        Case('event accounts', 'GET', f'{a}/events/{{event}}/accounts'),
        Case('close event account', 'POST', f'{a}/events/{{event}}/accounts/{{device_session}}/close'),
        Case('move event account', 'POST', f'{a}/events/{{event}}/accounts/{{device_session}}/move', {'teamId': None}),
        Case('event leader sessions', 'GET', f'{a}/events/{{event}}/leader-sessions'),
        Case('revoke leader session', 'POST', f'{a}/events/{{event}}/leader-sessions/{{leader_session_other}}/revoke'),
        Case('event links', 'GET', f'{a}/events/{{event}}/links', probe=probe_body),
        Case('create event link', 'POST', f'{a}/events/{{event}}/links', {'teamId': '{team_b}', 'role': 'player'}),
        Case('team leader candidates', 'GET', f'{a}/events/{{event}}/team-leader-candidates?search=matrix&teamId={{team_a}}'),
        Case('event team leaders', 'GET', f'{a}/events/{{event}}/team-leaders'),
        Case('designate team leader', 'POST', f'{a}/events/{{event}}/team-leaders',
             {'teamId': '{team_a}', 'userId': '{target}'}),
        Case('remove team leader designation', 'DELETE', f'{a}/events/{{event}}/team-leaders/{{designation}}'),
        Case('join event', 'POST', f'{a}/events/join/nonsense', {}),
        Case('update event link', 'PUT', f'{a}/events/links/{{link_player}}', {'direction': 'BOTH'}),
        Case('delete event link', 'DELETE', f'{a}/events/links/{{link_player}}'),
        Case('close link sessions', 'POST', f'{a}/events/links/{{link_player}}/close-sessions'),
        Case('revoke event link', 'POST', f'{a}/events/links/{{link_player}}/revoke'),
        Case('event link url', 'GET', f'{a}/events/links/{{link_player}}/url'),
        Case('my team', 'GET', f'{a}/events/my-team'),
        Case('my team accounts', 'GET', f'{a}/events/my-team/accounts'),
        Case('my team move account', 'POST', f'{a}/events/my-team/accounts/{{device_session}}/move', {'teamId': None}),
        Case('my team enrollment', 'POST', f'{a}/events/my-team/enrollment', {}),
        Case('list event teams', 'GET', f'{a}/events/teams'),
        Case('create event team', 'POST', f'{a}/events/teams', {'name': 'matrix-new'}),
        Case('get event team', 'GET', f'{a}/events/teams/{{team_a}}'),
        Case('update event team', 'PUT', f'{a}/events/teams/{{team_b}}', {'color': 'Red'}, probe=probe_team),
        Case('update event team leader field', 'PUT', f'{a}/events/teams/{{team_b}}', {'leaderUserId': '{target}'},
             probe=probe_team),
        Case('delete event team', 'DELETE', f'{a}/events/teams/{{team_b}}'),
        Case('create team leader account', 'POST', f'{a}/events/teams/{{team_b}}/leader',
             {'username': 'matrixleader', 'password': GOOD_PASSWORD}),
        # ---------------------------------------------------------------- groups
        Case('list groups', 'GET', f'{a}/groups'),
        Case('create group', 'POST', f'{a}/groups', {'name': 'matrixnew'}),
        Case('get group', 'GET', f'{a}/groups/{{group}}', probe=probe_body),
        Case('update group', 'PUT', f'{a}/groups/{{group}}', {'description': 'x'}),
        Case('delete group', 'DELETE', f'{a}/groups/{{group}}'),
        Case('group members', 'GET', f'{a}/groups/{{group}}/members', probe=probe_body),
        Case('add group member', 'POST', f'{a}/groups/{{group}}/members', {'username': 'matrix_ots_user'}),
        Case('remove group member', 'DELETE', f'{a}/groups/{{group}}/members/matrix_target'),
        Case('sync groups', 'POST', f'{a}/groups/sync'),
        # ---------------------------------------------------------------- kiosk
        Case('kiosk create session', 'POST', f'{a}/kiosk/session'),
        Case('kiosk authenticate', 'POST', f'{a}/kiosk/session/matrixkiosksession/authenticate'),
        Case('kiosk status', 'GET', f'{a}/kiosk/session/matrixkiosksession/status'),
        # ---------------------------------------------------------------- meshtastic
        Case('list meshtastic', 'GET', f'{a}/meshtastic', probe=probe_keys('configs')),
        Case('create meshtastic', 'POST', f'{a}/meshtastic', {'name': 'matrix-new', 'url': 'https://meshtastic.org/e/#DDDD'}),
        Case('get meshtastic public', 'GET', f'{a}/meshtastic/{{channel_public}}'),
        Case('get meshtastic role-bound', 'GET', f'{a}/meshtastic/{{channel_role}}'),
        Case('get meshtastic private', 'GET', f'{a}/meshtastic/{{channel_private}}'),
        Case('update meshtastic', 'PUT', f'{a}/meshtastic/{{channel_public}}', {'description': 'x'}),
        Case('delete meshtastic', 'DELETE', f'{a}/meshtastic/{{channel_private}}'),
        Case('sync one meshtastic', 'POST', f'{a}/meshtastic/{{channel_public}}/sync'),
        Case('meshtastic admin list', 'GET', f'{a}/meshtastic/admin', probe=probe_keys('configs')),
        Case('meshtastic channel groups', 'GET', f'{a}/meshtastic/channels/{{channel_public}}/groups'),
        Case('list meshtastic groups', 'GET', f'{a}/meshtastic/groups', probe=probe_keys('groups')),
        Case('create meshtastic group', 'POST', f'{a}/meshtastic/groups', {'name': 'matrix-new'}),
        Case('get meshtastic group public', 'GET', f'{a}/meshtastic/groups/{{mgroup_public}}', probe=probe_body),
        Case('get meshtastic group private', 'GET', f'{a}/meshtastic/groups/{{mgroup_private}}', probe=probe_body),
        Case('update meshtastic group', 'PUT', f'{a}/meshtastic/groups/{{mgroup_public}}', {'description': 'x'}),
        Case('delete meshtastic group', 'DELETE', f'{a}/meshtastic/groups/{{mgroup_private}}'),
        Case('add channel to group', 'POST', f'{a}/meshtastic/groups/{{mgroup_private}}/channels',
             {'channel_id': '{channel_private}', 'channelId': '{channel_private}', 'slot_number': 1, 'slotNumber': 1}),
        Case('remove channel from group', 'DELETE', f'{a}/meshtastic/groups/{{mgroup_public}}/channels/{{channel_public}}'),
        Case('update channel slot', 'PUT', f'{a}/meshtastic/groups/{{mgroup_public}}/channels/{{channel_public}}/slot',
             {'slot_number': 2, 'slotNumber': 2}),
        Case('regenerate group url', 'POST', f'{a}/meshtastic/groups/{{mgroup_public}}/regenerate-url'),
        Case('meshtastic groups admin', 'GET', f'{a}/meshtastic/groups/admin', probe=probe_body),
        Case('regenerate all urls', 'POST', f'{a}/meshtastic/groups/regenerate-all-urls'),
        Case('sync meshtastic', 'POST', f'{a}/meshtastic/sync'),
        Case('ungrouped channels', 'GET', f'{a}/meshtastic/ungrouped', probe=probe_body),
        # ---------------------------------------------------------------- onboarding codes
        Case('list codes', 'GET', f'{a}/onboarding-codes', probe=probe_keys('codes')),
        Case('create code', 'POST', f'{a}/onboarding-codes', {'name': 'n', 'onboardingCode': 'matrixnewcode'},
             probe=probe_new_code),
        Case('create code with roles/groups', 'POST', f'{a}/onboarding-codes',
             {'name': 'n', 'onboardingCode': 'matrixnewcode', 'roleIds': ['{role_audience}'], 'groupIds': ['{group}'],
              'approverRoleId': '{role_audience}'}, probe=probe_new_code),
        Case('create code with administrator', 'POST', f'{a}/onboarding-codes',
             {'name': 'n', 'onboardingCode': 'matrixnewcode', 'roleIds': ['{role_administrator}']}, probe=probe_new_code),
        Case('get code', 'GET', f'{a}/onboarding-codes/{{code}}'),
        Case('get privileged code', 'GET', f'{a}/onboarding-codes/{{code_privileged}}'),
        Case('update code basics', 'PUT', f'{a}/onboarding-codes/{{code}}', {'description': 'x'},
             probe=probe_code('code')),
        Case('update code roles', 'PUT', f'{a}/onboarding-codes/{{code}}', {'roleIds': []}, probe=probe_code('code')),
        Case('update code approver', 'PUT', f'{a}/onboarding-codes/{{code}}', {'approverRoleId': None},
             probe=probe_code('code')),
        Case('update code approval flags', 'PUT', f'{a}/onboarding-codes/{{code}}',
             {'requireApproval': True, 'autoApprove': True}, probe=probe_code('code')),
        Case('create code with approval flags', 'POST', f'{a}/onboarding-codes',
             {'name': 'n', 'onboardingCode': 'matrixnewcode', 'requireApproval': True}, probe=probe_new_code),
        Case('update code groups', 'PUT', f'{a}/onboarding-codes/{{code}}', {'groupIds': []}, probe=probe_code('code')),
        Case('update code groups with direction', 'PUT', f'{a}/onboarding-codes/{{code}}',
             {'groups': [{'id': '{group}', 'direction': 'IN'}]}, probe=probe_code('code')),
        Case('update privileged code', 'PUT', f'{a}/onboarding-codes/{{code_privileged}}', {'description': 'x'}),
        Case('delete code', 'DELETE', f'{a}/onboarding-codes/{{code_spare}}'),
        Case('delete privileged code', 'DELETE', f'{a}/onboarding-codes/{{code_privileged_spare}}'),
        Case('validate code', 'GET', f'{a}/onboarding-codes/validate/matrixcode'),
        # ---------------------------------------------------------------- pending registrations
        Case('list pending', 'GET', f'{a}/pending-registrations', probe=probe_body),
        Case('create pending', 'POST', f'{a}/pending-registrations',
             {'username': 'matrixpending', 'password': GOOD_PASSWORD, 'email': 'mp@example.com', 'firstName': 'f',
              'lastName': 'l', 'callsign': 'c', 'onboarding_code_id': '{code}'}),
        Case('get pending', 'GET', f'{a}/pending-registrations/{{pending}}', probe=probe_body),
        Case('get pending of privileged code', 'GET', f'{a}/pending-registrations/{{pending_privileged}}', probe=probe_body),
        Case('update pending', 'PUT', f'{a}/pending-registrations/{{pending}}', {'firstName': 'x'}),
        Case('delete pending', 'DELETE', f'{a}/pending-registrations/{{pending}}'),
        Case('approve pending', 'POST', f'{a}/pending-registrations/{{pending}}/approve'),
        Case('approve pending of privileged code', 'POST', f'{a}/pending-registrations/{{pending_privileged}}/approve'),
        Case('resend verification', 'POST', f'{a}/pending-registrations/{{pending}}/resend'),
        Case('cleanup expired', 'POST', f'{a}/pending-registrations/cleanup-expired'),
        # ---------------------------------------------------------------- qr
        Case('qr atak', 'GET', f'{a}/qr/atak'),
        Case('qr itak', 'GET', f'{a}/qr/itak'),
        # ---------------------------------------------------------------- radios
        Case('list radios', 'GET', f'{a}/radios', probe=probe_keys('radios')),
        Case('create radio', 'POST', f'{a}/radios', {'name': 'matrix-new-radio'}, probe=probe_new_radio),
        Case('create radio with assignment', 'POST', f'{a}/radios',
             {'name': 'matrix-new-radio', 'assignedTo': '{target}', 'owner': '{target}'}, probe=probe_new_radio),
        Case('get radio assigned to no_role', 'GET', f'{a}/radios/{{radio_own}}'),
        Case('get radio owned by no_role', 'GET', f'{a}/radios/{{radio_owned}}'),
        Case('get radio of someone else', 'GET', f'{a}/radios/{{radio_other}}'),
        Case('update radio', 'PUT', f'{a}/radios/{{radio_other}}', {'description': 'x'}, probe=probe_radio),
        Case('update radio assignment', 'PUT', f'{a}/radios/{{radio_other}}', {'assignedTo': None, 'owner': None},
             probe=probe_radio),
        Case('update radio assignment by id', 'PUT', f'{a}/radios/{{radio_other}}',
             {'assignedToId': None, 'ownerId': None}, probe=probe_radio),
        Case('delete radio', 'DELETE', f'{a}/radios/{{radio_other}}'),
        Case('assign radio', 'PUT', f'{a}/radios/{{radio_other}}/assign', {'userId': None}, probe=probe_radio),
        Case('claim radio', 'POST', f'{a}/radios/{{radio_unassigned}}/claim', {'claimCode': 'AA:BB:CC:00:00:04'}),
        Case('claim code', 'GET', f'{a}/radios/{{radio_other}}/claim-code'),
        Case('compare config', 'POST', f'{a}/radios/{{radio_other}}/compare-config',
             {'channel_group_id': '{mgroup_public}', 'current_config': {}}),
        Case('compare config, users allowed, other radio', 'POST', f'{a}/radios/{{radio_other}}/compare-config',
             {'channel_group_id': '{mgroup_public}', 'current_config': {}}, setup=enable_user_programming),
        Case('compare config, users allowed, own radio', 'POST', f'{a}/radios/{{radio_own}}/compare-config',
             {'channel_group_id': '{mgroup_public}', 'current_config': {}}, setup=enable_user_programming),
        Case('program config', 'POST', f'{a}/radios/{{radio_other}}/program-config', {'channel_id': '{channel_public}'}),
        Case('program config, users allowed, other radio', 'POST', f'{a}/radios/{{radio_other}}/program-config',
             {'channel_id': '{channel_public}'}, setup=enable_user_programming),
        Case('program config, users allowed, own radio', 'POST', f'{a}/radios/{{radio_own}}/program-config',
             {'channel_id': '{channel_public}'}, setup=enable_user_programming),
        Case('program config, users allowed, owned radio', 'POST', f'{a}/radios/{{radio_owned}}/program-config',
             {'channel_id': '{channel_public}'}, setup=enable_user_programming),
        Case('radio by node', 'GET', f'{a}/radios/claim-by-node/!cc000004'),
        Case('claim radio by node', 'POST', f'{a}/radios/claim-by-node/!cc000004', {}),
        Case('enroll radio', 'POST', f'{a}/radios/enroll', {'name': 'matrix-enrolled', 'longName': 'l', 'shortName': 's'}),
        # ---------------------------------------------------------------- roles
        Case('list roles', 'GET', f'{a}/roles', probe=probe_role_list),
        Case('create role', 'POST', f'{a}/roles', {'name': 'matrix_new', 'description': 'x'}),
        Case('create role, reserved name', 'POST', f'{a}/roles', {'name': 'administrator'}),
        Case('get role', 'GET', f'{a}/roles/{{role_audience}}'),
        Case('update role', 'PUT', f'{a}/roles/{{role_spare}}', {'description': 'x'}),
        Case('update protected role', 'PUT', f'{a}/roles/{{role_user_admin}}', {'description': 'x'}),
        Case('delete role', 'DELETE', f'{a}/roles/{{role_spare}}'),
        Case('delete protected role', 'DELETE', f'{a}/roles/{{role_user_admin}}'),
        Case('permission catalog', 'GET', f'{a}/roles/permission-catalog'),
        Case('set role permissions', 'PUT', f'{a}/roles/{{role_spare}}/permissions', {'permissions': ['roles.view']}),
        Case('set protected role permissions', 'PUT', f'{a}/roles/{{role_user_admin}}/permissions', {'permissions': []}),
        Case('preview role permissions', 'POST', f'{a}/roles/{{role_spare}}/permissions/preview',
             {'permissions': ['roles.view']}),
        Case('duplicate role', 'POST', f'{a}/roles/{{role_spare}}/duplicate', {'name': 'matrix_copy'}),
        # ---------------------------------------------------------------- public settings / docs
        Case('public settings', 'GET', f'{a}/settings'),
        Case('swagger json', 'GET', f'{a}/swagger.json'),
        Case('version', 'GET', f'{a}/version'),
        Case('upload file', 'GET', f'{a}/uploads/seed.png'),
        Case('spa root', 'GET', '/'),
        Case('spa path', 'GET', '/some/client/route'),
        Case('swagger ui', 'GET', '/api/docs/'),
        Case('swagger ui path', 'GET', '/api/docs/index.html'),
        Case('swagger ui static', 'GET', '/api/docs/dist/swagger-ui.css'),
        Case('static file', 'GET', '/static/matrix-missing.css'),
        # ---------------------------------------------------------------- tak profiles
        Case('list profiles', 'GET', f'{a}/tak-profiles', probe=probe_keys('profiles')),
        Case('create profile', 'POST', f'{a}/tak-profiles',
             form={'name': 'matrix-new-profile'}, files=lambda: {'datapackage': (io.BytesIO(_zip_bytes()), 'new.zip')},
             probe=probe_new_profile),
        Case('create profile with roles', 'POST', f'{a}/tak-profiles',
             form={'name': 'matrix-new-profile', 'roleIds[]': '{role_audience}'},
             files=lambda: {'datapackage': (io.BytesIO(_zip_bytes()), 'new.zip')}, probe=probe_new_profile),
        Case('get profile public', 'GET', f'{a}/tak-profiles/{{profile_public}}'),
        Case('get profile role-bound', 'GET', f'{a}/tak-profiles/{{profile_role}}'),
        Case('get profile private', 'GET', f'{a}/tak-profiles/{{profile_private}}'),
        Case('update profile', 'PUT', f'{a}/tak-profiles/{{profile_public}}', form={'description': 'x'},
             probe=probe_profile),
        Case('update profile public', 'PUT', f'{a}/tak-profiles/{{profile_public}}', form={'isPublic': 'false'},
             probe=probe_profile),
        Case('create profile public', 'POST', f'{a}/tak-profiles', form={'name': 'matrix-new-profile', 'isPublic': 'true'},
             files=lambda: {'datapackage': (io.BytesIO(_zip_bytes()), 'new.zip')}, probe=probe_new_profile),
        Case('update profile roles', 'PUT', f'{a}/tak-profiles/{{profile_public}}',
             form={'roleIds[]': '{role_audience}'}, probe=probe_profile),
        Case('delete profile', 'DELETE', f'{a}/tak-profiles/{{profile_private}}'),
        Case('download profile public', 'GET', f'{a}/tak-profiles/{{profile_public}}/download'),
        Case('download profile private', 'GET', f'{a}/tak-profiles/{{profile_private}}/download'),
        Case('download token public', 'POST', f'{a}/tak-profiles/{{profile_public}}/download-token'),
        Case('download token private', 'POST', f'{a}/tak-profiles/{{profile_private}}/download-token'),
        Case('download by bad token', 'GET', f'{a}/tak-profiles/{{profile_public}}/download/nonsense/profile.zip'),
        Case('profile files', 'GET', f'{a}/tak-profiles/{{profile_public}}/files'),
        # ---------------------------------------------------------------- users
        Case('list users', 'GET', f'{a}/users'),
        Case('create user', 'POST', f'{a}/users',
             {'username': 'matrixnew', 'password': GOOD_PASSWORD, 'email': 'mn@example.com', 'firstName': 'f',
              'lastName': 'l', 'callsign': 'c'}),
        Case('create user with administrator', 'POST', f'{a}/users',
             {'username': 'matrixnew', 'password': GOOD_PASSWORD, 'email': 'mn@example.com', 'firstName': 'f',
              'lastName': 'l', 'callsign': 'c', 'roleIds': ['{role_administrator}']}, probe=probe_new_user),
        Case('create user with role', 'POST', f'{a}/users',
             {'username': 'matrixnew', 'password': GOOD_PASSWORD, 'email': 'mn@example.com', 'firstName': 'f',
              'lastName': 'l', 'callsign': 'c', 'roleIds': ['{role_audience}'], 'groupIds': ['{group}']},
             probe=probe_new_user),
        Case('get other user', 'GET', f'{a}/users/{{target}}', probe=probe_body),
        Case('get self', 'GET', f'{a}/users/{{self}}', probe=probe_shape),
        Case('update other basics', 'PUT', f'{a}/users/{{target}}', {'firstName': 'edited'}, probe=probe_user('target')),
        Case('update other email', 'PUT', f'{a}/users/{{target}}', {'email': 'new@example.com'},
             probe=probe_user('target')),
        Case('update other roles', 'PUT', f'{a}/users/{{target}}', {'roleIds': ['{role_audience}']},
             probe=probe_user('target')),
        Case('update other administrator role', 'PUT', f'{a}/users/{{target}}', {'roleIds': ['{role_administrator}']},
             probe=probe_user('target')),
        Case('update other groups', 'PUT', f'{a}/users/{{target}}', {'groupIds': []}, probe=probe_user('target')),
        Case('update other groups with direction', 'PUT', f'{a}/users/{{target}}',
             {'groups': [{'id': '{group}', 'direction': 'IN'}]}, probe=probe_user('target')),
        Case('update other password', 'PUT', f'{a}/users/{{target}}', {'password': GOOD_PASSWORD},
             probe=probe_user('target')),
        Case('update other expiry', 'PUT', f'{a}/users/{{target}}', {'expiryDate': '2099-01-01T00:00:00'},
             probe=probe_user('target')),
        Case('update privileged user', 'PUT', f'{a}/users/{{target_privileged}}', {'firstName': 'edited'},
             probe=probe_user('target_privileged')),
        Case('update self basics', 'PUT', f'{a}/users/{{self}}', {'firstName': 'edited'}, probe=probe_self),
        Case('update self email', 'PUT', f'{a}/users/{{self}}', {'email': 'new@example.com'}, probe=probe_self),
        Case('update self roles', 'PUT', f'{a}/users/{{self}}', {'roleIds': ['{role_audience}']}, probe=probe_self),
        Case('update self groups', 'PUT', f'{a}/users/{{self}}', {'groupIds': ['{group}']}, probe=probe_self),
        Case('update self password', 'PUT', f'{a}/users/{{self}}', {'password': GOOD_PASSWORD}, probe=probe_self),
        Case('update self expiry', 'PUT', f'{a}/users/{{self}}', {'expiryDate': '2099-01-01T00:00:00'}, probe=probe_self),
        Case('delete user', 'DELETE', f'{a}/users/{{target}}'),
        Case('delete privileged user', 'DELETE', f'{a}/users/{{target_privileged}}'),
        Case('delete self', 'DELETE', f'{a}/users/{{self}}'),
    ]
    return c + _settings_cases()


class FakeState:
    """State of the fake OTS. Reset for every call."""
    accounts = {}
    password_resets = 0


def _fake_get_users(self, page=None, per_page=None, username=None, email=None):
    return {'response': {'total_pages': 1, 'results': [
        {'username': name, 'roles': [{'name': r} for r in roles]}
        for name, roles in FakeState.accounts.items() if username in (None, name)]}}


def _fake_set_user_roles(self, username, roles):
    FakeState.accounts[username] = list(roles)


def _fake_reset_password(self, username, password):
    FakeState.password_resets += 1


def _fake_create_user(self, username, password, roles=('user',)):
    FakeState.accounts[username] = list(roles)
    return {'success': True}


def _fake_user_exists(self, username):
    return username in FakeState.accounts


def _fake_default(self, *args, **kwargs):
    return {'response': {'total_pages': 1, 'results': [], 'token': 'fake-qr'}, 'success': True}


def _fake_qr(self, *args, **kwargs):
    return {'response': {'qr_string': 'tak://fake', 'exp': 9999999999, 'max': 100, 'total_uses': 0, 'success': True}}


def _fake_group_members(self, group_name):
    return {'response': {'results': [], 'total_pages': 1}}


FAKE_OTS_METHODS = {
    'get_users': _fake_get_users,
    'set_user_roles': _fake_set_user_roles,
    'reset_user_password': _fake_reset_password,
    'create_user': _fake_create_user,
    'user_exists': _fake_user_exists,
    'get_group_members': _fake_group_members,
    'get_atak_qr_string': _fake_qr,
    'create_atak_qr_string': _fake_qr,
    'get_itak_qr_string': _fake_qr,
    'create_itak_qr_string': _fake_qr,
}


def _resolve(value, ids):
    """Substitute '{key}' placeholders in a JSON body. A whole-string placeholder gives the int id."""
    if isinstance(value, str) and value.startswith('{') and value.endswith('}') and value[1:-1] in ids:
        return ids[value[1:-1]]
    if isinstance(value, list):
        return [_resolve(v, ids) for v in value]
    if isinstance(value, dict):
        return {k: _resolve(v, ids) for k, v in value.items()}
    return value


class Matrix:
    """One private app with its own in-memory database; use as a context manager."""

    def __init__(self):
        self.ids = {}
        self.tokens = {}
        self.claims = {}
        self.user_ids = {}
        self.cases = build_cases()

    def __enter__(self):
        self.tmp = tempfile.mkdtemp(prefix='rbac-matrix-')
        self.datapackages = os.path.join(self.tmp, 'datapackages')
        self.instance = os.path.join(self.tmp, 'instance')
        self.seed_files = os.path.join(self.tmp, 'seed-files')
        os.makedirs(self.seed_files)
        for sub, name in (('datapackages/public', 'manifest.xml'), ('datapackages/private', 'manifest.xml'),
                          ('instance/uploads', 'seed.png')):
            os.makedirs(os.path.join(self.seed_files, sub), exist_ok=True)
            with open(os.path.join(self.seed_files, sub, name), 'wb') as f:
                f.write(PNG)
        self._restore_files()

        self.patches = [
            mock.patch.object(OTSClient, name, FAKE_OTS_METHODS.get(name, _fake_default))
            for name, fn in vars(OTSClient).items()
            if callable(fn) and not name.startswith('__') and name != 'get_all_pages'
        ]
        self.patches += [
            mock.patch.object(tak_mod, 'DATAPACKAGE_UPLOAD_FOLDER', self.datapackages),
            mock.patch.object(tak_mod, 'DOWNLOAD_TEMP_DIR', os.path.join(self.tmp, 'downloads')),
            # The mail thread is pointless here (mail is disabled) and would outlive the call
            mock.patch.object(magic_link_mod.threading, 'Thread', mock.MagicMock()),
            mock.patch.object(oidc_mod, '_fetch_discovery', lambda url: {'issuer': url}),
            # Nothing in here may reach the network
            mock.patch('requests.sessions.Session.send', side_effect=requests.ConnectionError('no network in the matrix')),
        ]
        os.makedirs(os.path.join(self.tmp, 'downloads'))
        for patch in self.patches:
            patch.start()

        self.app = create_app()
        self.app.instance_path = self.instance
        # An unhandled error counts as the 500 it is in production, not as a test crash
        self.app.config['PROPAGATE_EXCEPTIONS'] = False
        self.app.config['SECRET_KEY'] = 'matrix-strong-secret-key-with-at-least-32-chars'
        with self.app.app_context():
            db.create_all()
            self._seed()
            db.session.remove()
            self.raw = db.engine.raw_connection().driver_connection
        self.snapshot()
        return self

    def __exit__(self, *exc):
        for patch in self.patches:
            patch.stop()
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()
        self.db_snapshot.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------------ state
    def snapshot(self):
        """Freeze the current database as the state every call starts from"""
        self.db_snapshot = getattr(self, 'db_snapshot', None) or sqlite3.connect(':memory:')
        self.raw.commit()
        self.raw.backup(self.db_snapshot)
        self._restore_accounts()

    def _restore_accounts(self):
        with self.app.app_context():
            self.base_accounts = {u.username: ['administrator' if r.name == 'administrator' else 'user' for r in u.roles][:1] or ['user']
                                  for u in UserModel.query.all()}
            db.session.remove()
        self._reset_process_state()

    def _reset_process_state(self):
        FakeState.accounts = {k: list(v) for k, v in self.base_accounts.items()}
        FakeState.password_resets = 0
        events_mod._device_attempts.clear()
        qr_mod._qr_cache.clear()

    def _restore_files(self):
        for folder in ('datapackages', 'instance'):
            shutil.rmtree(os.path.join(self.tmp, folder), ignore_errors=True)
            shutil.copytree(os.path.join(self.seed_files, folder), os.path.join(self.tmp, folder))

    def restore(self):
        self.raw.rollback()
        self.db_snapshot.backup(self.raw)
        self._reset_process_state()

    # ------------------------------------------------------------------ seed
    def _make_user(self, username, *roles):
        user = UserModel(username=username, email=f'{username}@example.com', firstName='F', lastName='L',
                         callsign=username.upper(), roles=list(roles))
        db.session.add(user)
        db.session.flush()
        return user

    def add_identity(self, name, *roles):
        """Add a user holding these roles (must be called with an app context)"""
        user = self._make_user(f'matrix_{name}', *roles)
        db.session.commit()
        self._token(name, user.id)
        return user

    def _token(self, name, user_id, **claims):
        self.user_ids[name] = user_id
        self.claims[name] = claims
        self.tokens[name] = create_access_token(identity=str(user_id), additional_claims=claims)

    def _seed(self):
        ids = self.ids
        SystemSettingsModel.initialize_defaults()
        for key in ('meshtastic_enabled', 'radios_enabled', 'events_enabled', 'kiosk_enrollment_enabled',
                    'claim_radio_enabled', 'user_radio_enrollment_enabled', 'magic_link_login_enabled'):
            SystemSettingsModel.set_setting(key, 'true')

        roles = {}
        for name, (description, display_name) in ADMIN_ROLES.items():
            roles[name] = UserRoleModel(name=name, description=description, display_name=display_name)
            db.session.add(roles[name])
        audience = UserRoleModel(name='matrix_audience', display_name='Audience')
        spare = UserRoleModel(name='matrix_spare', display_name='Spare')
        admin_row = UserRoleModel(name='admin', display_name='Admin row')
        ots_user = UserRoleModel(name='user', display_name='User', description='OTS role')
        db.session.add_all([audience, spare, admin_row, ots_user])
        db.session.flush()
        ids.update(role_audience=audience.id, role_spare=spare.id, role_administrator=roles['administrator'].id,
                   role_user_admin=roles['user_admin'].id)
        self.role_ids = {name: role.id for name, role in roles.items()}

        # Identities
        for name, role in roles.items():
            self.add_identity(name, role)
        self.add_identity(NO_ROLE)
        self.add_identity('admin_row', admin_row)
        self.add_identity('ots_user', ots_user)
        self.add_identity('audience', audience)
        kiosk = self.add_identity('kiosk', roles['administrator'])
        self._token('kiosk', kiosk.id, kiosk_session=True)
        self._token('no_user_row', 999999)
        self.user_ids[ANONYMOUS] = 999999
        self.tokens[ANONYMOUS] = None

        # Users to act on
        target = self._make_user('matrix_target')
        target_privileged = self._make_user('matrix_target_privileged', roles['administrator'])
        approver = self._make_user('matrix_approver', audience)
        db.session.flush()
        ids.update(target=target.id, target_privileged=target_privileged.id, approver=approver.id)

        # OTS groups
        group = OTSGroupModel(name='matrix_group')
        db.session.add(group)
        db.session.flush()
        ids['group'] = group.id
        db.session.add(GroupUserAssociation(group_id=group.id, user_id=target.id, direction='BOTH'))

        # Onboarding codes and pending registrations
        future = datetime.now() + timedelta(days=7)
        code = OnboardingCodeModel(name='plain', onboardingCode='matrixcode', description='d', roles=[audience],
                                   approverRoleId=audience.id)
        code_privileged = OnboardingCodeModel(name='privileged', onboardingCode='matrixprivcode',
                                              roles=[roles['administrator']])
        code_approval = OnboardingCodeModel(name='approval', onboardingCode='matrixappcode', requireApproval=True,
                                            approverRoleId=audience.id, roles=[audience])
        code_spare = OnboardingCodeModel(name='spare', onboardingCode='matrixsparecode')
        code_privileged_spare = OnboardingCodeModel(name='privileged spare', onboardingCode='matrixprivsparecode',
                                                    roles=[roles['administrator']])
        db.session.add_all([code, code_privileged, code_approval, code_spare, code_privileged_spare])
        db.session.flush()
        ids.update(code=code.id, code_privileged=code_privileged.id, code_spare=code_spare.id,
                   code_privileged_spare=code_privileged_spare.id)
        db.session.add(GroupOnboardingCodeAssociation(group_id=group.id, onboardingcode_id=code.id, direction='BOTH'))

        def pending(name, code_row, **kwargs):
            row = PendingRegistrationModel(username=name, email=f'{name}@example.com', password='x', firstName='f',
                                           lastName='l', callsign=name, onboarding_code_id=code_row.id,
                                           verification_token=f'verify-{name}', expires_at=future, **kwargs)
            db.session.add(row)
            db.session.flush()
            return row.id
        ids['pending'] = pending('matrixpending1', code)
        ids['pending_privileged'] = pending('matrixpending2', code_privileged)
        ids['pending_approval'] = pending('matrixpending3', code_approval, approval_status='pending_approval',
                                          approval_token='approvaltoken')

        # TAK profiles
        public = TakProfileModel(name='public', description='d', isPublic=True, takTemplateFolderLocation='public', takPrefFileLocation='manifest.xml')
        by_role = TakProfileModel(name='role-bound', description='d', isPublic=False, takTemplateFolderLocation='public',
                                  takPrefFileLocation='manifest.xml', roles=[audience])
        private = TakProfileModel(name='private', description='d', isPublic=False, takTemplateFolderLocation='private',
                                  takPrefFileLocation='manifest.xml')
        db.session.add_all([public, by_role, private])
        db.session.flush()
        ids.update(profile_public=public.id, profile_role=by_role.id, profile_private=private.id)

        # Meshtastic
        ch_public = MeshtasticModel(name='public', url='https://meshtastic.org/e/#AAAA', isPublic=True, yamlConfig='a: 1')
        ch_role = MeshtasticModel(name='role-bound', url='https://meshtastic.org/e/#BBBB', isPublic=False, roles=[audience])
        ch_private = MeshtasticModel(name='private', url='https://meshtastic.org/e/#CCCC', isPublic=False)
        grp_public = MeshtasticChannelGroup(name='public', isPublic=True)
        grp_private = MeshtasticChannelGroup(name='private', isPublic=False)
        db.session.add_all([ch_public, ch_role, ch_private, grp_public, grp_private])
        db.session.flush()
        db.session.add(ChannelGroupMembership(channel_id=ch_public.id, group_id=grp_public.id, slot_number=0))
        db.session.add(ChannelGroupMembership(channel_id=ch_role.id, group_id=grp_private.id, slot_number=0))
        ids.update(channel_public=ch_public.id, channel_role=ch_role.id, channel_private=ch_private.id,
                   mgroup_public=grp_public.id, mgroup_private=grp_private.id)

        # Radios
        no_role_id = self.user_ids[NO_ROLE]

        def radio(name, mac, **kwargs):
            row = RadioModel(name=name, radioType='meshtastic', mac=mac, description='d', platform='p', softwareVersion='1',
                             model='m', vendor='v', shortName=mac[-2:], longName=f'long-{name}', role='CLIENT',
                             publicKey='pk', privateKey='sk', **kwargs)
            db.session.add(row)
            db.session.flush()
            return row.id
        ids['radio_own'] = radio('assigned-to-no-role', 'AA:BB:CC:00:00:01', assignedTo=no_role_id)
        ids['radio_owned'] = radio('owned-by-no-role', 'AA:BB:CC:00:00:02', owner=no_role_id)
        ids['radio_other'] = radio('someone-else', 'AA:BB:CC:00:00:03', assignedTo=target.id, owner=target.id)
        ids['radio_unassigned'] = radio('unassigned', 'AA:BB:CC:00:00:04')

        # Announcements
        now = datetime.now()
        failures = json.dumps([{'userId': target.id, 'error': 'smtp secret detail'}])
        sent = AnnouncementModel(title='sent', content='c', target_type='all', status='sent', sent_at=now,
                                 send_email=True, email_status='failed', email_last_error='raw smtp error',
                                 email_pending_user_ids=json.dumps([target.id]), email_failures=failures,
                                 created_by=target.id)
        draft = AnnouncementModel(title='draft', content='c', target_type='all', status='draft', send_email=False,
                                  created_by=target.id)
        scheduled = AnnouncementModel(title='scheduled', content='c', target_type='all', status='scheduled',
                                      scheduled_at=future, send_email=True, created_by=target.id)
        db.session.add_all([sent, draft, scheduled])
        db.session.flush()
        ids.update(ann_sent=sent.id, ann_draft=draft.id, ann_scheduled=scheduled.id)

        # API key
        api_key = ApiKeyModel(name='matrix', key_prefix='matrix', key_hash='matrix-hash', permissions='["users:read"]',
                              created_by=target.id)
        db.session.add(api_key)
        db.session.flush()
        ids['api_key'] = api_key.id

        # OIDC
        oidc = OIDCProviderModel(name='matrix', display_name='Matrix', discovery_url='https://idp.invalid/x',
                                 client_id='i', client_secret='s', enabled=True, icon_url='/api/v1/uploads/seed.png')
        db.session.add(oidc)
        db.session.flush()
        ids['oidc'] = oidc.id

        # Settings looked up by id
        ids['setting'] = SystemSettingsModel.query.filter_by(key='brand_name_value').first().id
        ids['setting_module'] = SystemSettingsModel.query.filter_by(key='radios_enabled').first().id

        # Kiosk session
        db.session.add(KioskSessionModel(session_id='matrixkiosksession', status='pending',
                                         expires_at=datetime.utcnow() + timedelta(minutes=10)))

        # Events: leader identity, two teams, links, a device account, leader sessions, a designation
        event = EventModel(name='matrix', playerAccessMode='shared', validFrom=now - timedelta(hours=1),
                           validUntil=now + timedelta(days=7), active=True)
        db.session.add(event)
        db.session.flush()
        leader_user = self._make_user('matrix_event_leader', roles['administrator'])
        group_a = OTSGroupModel(name='matrix_team_a')
        group_b = OTSGroupModel(name='matrix_team_b')
        db.session.add_all([group_a, group_b])
        db.session.flush()
        team_a = EventTeamModel(name='A', otsGroupId=group_a.id, leaderUserId=leader_user.id)
        team_b = EventTeamModel(name='B', otsGroupId=group_b.id)
        db.session.add_all([team_a, team_b])
        db.session.flush()
        ids.update(event=event.id, team_a=team_a.id, team_b=team_b.id)

        def link(team, role, nonce):
            row = EventLinkModel(eventId=event.id, eventTeamId=team.id, role=role, tokenHash=f'x-{nonce}',
                                 tokenVersion=1, tokenNonce=nonce)
            db.session.add(row)
            db.session.flush()
            row.tokenHash = events_mod._hash_token(events_mod._derive_link_token(row))
            return row
        link_player = link(team_a, 'player', 'nonce-player')
        link_leader = link(team_a, 'leader', 'nonce-leader')
        ids['link_player'] = link_player.id
        device = EventSessionModel(eventId=event.id, eventTeamId=team_a.id, otsUsername='evdevice', mode='device',
                                   expiresAt=now + timedelta(days=7), eventLinkId=link_player.id, role='player',
                                   direction='BOTH', otsGroupId=group_a.id)
        leader_session = EventLeaderSessionModel(eventId=event.id, eventTeamId=team_a.id, eventLinkId=link_leader.id,
                                                 displayName='leader')
        other_session = EventLeaderSessionModel(eventId=event.id, eventTeamId=team_a.id, eventLinkId=link_leader.id,
                                                displayName='other')
        designation = EventTeamLeaderModel(eventId=event.id, eventTeamId=team_a.id, userId=approver.id,
                                           source='designation')
        db.session.add_all([device, leader_session, other_session, designation])
        db.session.commit()
        ids.update(device_session=device.id, leader_session_other=other_session.id, designation=designation.id)
        self._token('event_leader', leader_user.id, event_leader_team=team_a.id, event_leader_event=event.id,
                    event_leader_until=9999999999, event_leader_link=link_leader.id,
                    event_leader_link_version=link_leader.tokenVersion, event_leader_link_nonce=link_leader.tokenNonce,
                    event_leader_session=leader_session.id)
        self.refresh_tokens = {name: create_refresh_token(identity=str(self.user_ids[name]), additional_claims=claims)
                               for name, claims in self.claims.items()}

    # ------------------------------------------------------------------ calls
    def _path(self, case, identity):
        return case.path.format(self=self.user_ids[identity], **self.ids)

    def _is_unbuilt_frontend(self, case, identity, resp):
        return (resp.status_code == 404 and str(_body(resp).get('error')).startswith('Frontend ')
                and self.endpoint_of(case, identity) == 'serve_spa')

    def endpoint_of(self, case, identity=NO_ROLE):
        path = self._path(case, identity).split('?')[0]
        return self.app.url_map.bind('localhost').match(path, method=case.method)[0]

    def request(self, case, identity):
        """Run one case from the clean state; returns (cell, response). Probes run before the restore."""
        ids = dict(self.ids, self=self.user_ids[identity])
        if case.setup:
            with self.app.app_context():
                case.setup(ids)
        headers = {}
        token = self.tokens[identity]
        if case.name == 'refresh' and identity in self.refresh_tokens:
            token = self.refresh_tokens[identity]
        if token:
            headers['Authorization'] = f'Bearer {token}'
        kwargs = {}
        if case.files or case.form:
            data = {k: _resolve(v, ids) for k, v in (case.form or {}).items()}
            data.update(case.files() if case.files else {})
            kwargs.update(data=data, content_type='multipart/form-data')
        elif case.method != 'GET':
            kwargs['json'] = _resolve(case.json_body if case.json_body is not None else {}, ids)
        client = self.app.test_client()
        resp = client.open(self._path(case, identity), method=case.method, headers=headers, **kwargs)
        cell = str(resp.status_code)
        # The catch-all has nothing to serve in a checkout without a frontend build (frontend/dist is not
        # in git). Either way it answered, for everyone alike: the matrix must not depend on that build.
        if self._is_unbuilt_frontend(case, identity, resp):
            cell = '200'
        if resp.status_code == 403:
            cell += '|' + str(_body(resp).get('error'))
        probe = None
        if case.probe and resp.status_code < 500:
            with self.app.app_context():
                probe = case.probe(resp, ids)
                db.session.remove()
        if probe is not None:
            cell += '~' + probe
        return cell, resp

    def call(self, case, identity):
        try:
            cell, resp = self.request(case, identity)
            return cell, resp
        finally:
            self.restore()
            if case.method != 'GET':
                self._restore_files()

    def case(self, name):
        return next(c for c in self.cases if c.name == name)

    def run(self, identities=None, cases=None):
        """{case name: {identity: cell}}"""
        result = {}
        for case in cases or self.cases:
            result[case.name] = {identity: self.call(case, identity)[0] for identity in identities or IDENTITIES}
        return result


def to_golden(result):
    """Cells grouped per case, so a diff reads as 'who gets what'."""
    golden = {}
    for name, row in sorted(result.items()):
        grouped = {}
        for identity, cell in row.items():
            grouped.setdefault(cell, []).append(identity)
        golden[name] = {cell: sorted(identities) for cell, identities in sorted(grouped.items())}
    return golden


def per_identity_counts(result):
    counts = {identity: Counter() for identity in IDENTITIES}
    for row in result.values():
        for identity, cell in row.items():
            counts[identity][cell.split('|')[0].split('~')[0]] += 1
    return counts


def print_stats(result, out=sys.stdout):
    counts = per_identity_counts(result)
    statuses = sorted({s for c in counts.values() for s in c})
    print(f'{len(result)} cases x {len(counts)} identities = {len(result) * len(counts)} calls', file=out)
    print(f'{"identity":24}' + ''.join(f'{s:>5}' for s in statuses), file=out)
    for identity, c in counts.items():
        print(f'{identity:24}' + ''.join(f'{c[s]:>5}' for s in statuses), file=out)


# Case path below /api/v1 -> catalogue module whose keys may affect it
_MODULE_PREFIXES = (
    ('/admin/announcements', 'announcements'), ('/admin/api-keys', 'api_keys'),
    ('/admin/login-background', 'settings'), ('/admin/logo', 'settings'), ('/admin/oidc', 'settings'),
    ('/admin/settings', 'settings'), ('/events/', 'events'), ('/events', 'events'), ('/groups', 'groups'),
    ('/meshtastic', 'meshtastic'), ('/onboarding-codes', 'onboarding'), ('/pending-registrations', 'registrations'),
    ('/radios', 'radios'), ('/roles', 'roles'), ('/tak-profiles', 'tak_profiles'), ('/users', 'users'),
)


def case_module(case):
    path = case.path[len(API):] if case.path.startswith(API) else case.path
    return next((module for prefix, module in _MODULE_PREFIXES if path.startswith(prefix)), None)


def _with_requirements(key):
    """A key plus what it needs to count at all, directly or not"""
    from app.permissions import requirements_of

    keys, todo = set(), [key]
    while todo:
        current = todo.pop()
        if current not in keys:
            keys.add(current)
            todo.extend(requirements_of(current))
    return keys


def _status_and_probe(cell):
    head, _, probe = cell.partition('~')
    return head.split('|')[0], probe


# Keys that only show next to another key: the route is guarded by the other one, or asks for both
_SWEEP_NEXT_TO = {
    'announcements.send': ('announcements.edit',),
    'events.leaders.edit': ('events.teams.edit',),
    'meshtastic.channels.view': ('meshtastic.groups.view',),
    'radios.assign': ('radios.edit',),
    'roles.edit': ('roles.permissions.edit',),
    'roles.permissions.edit': ('roles.edit',),
    'users.accounts.edit': ('users.roles.assign',),
    'users.groups.assign': ('users.accounts.edit', 'users.create'),
    'users.password.reset': ('users.accounts.edit',),
    'users.roles.assign': ('users.accounts.edit', 'users.create'),
}


def key_sweep(matrix):
    """{case: [keys of its module that make a difference to it]}

    Each key is compared with a custom role holding what the key needs but not the key (no keys at all
    for most view keys), so the file says which key owns a route or field, not merely who can get there.
    "x next to y" compares x + y with y alone, see _SWEEP_NEXT_TO.
    """
    # Here, not at the top: rbac_golden.json is generated with this file on code from before permissions
    from app.models import RolePermissionModel
    from app.permissions import ASSIGNABLE_PERMISSIONS, CATALOGUE, requirements_of

    def needs(key):
        return set().union(*map(_with_requirements, requirements_of(key))) if requirements_of(key) else set()

    measured = []  # (label, module, keys with, keys without)
    for module, functions in CATALOGUE.items():
        for key in sorted(k for k in functions if k in ASSIGNABLE_PERMISSIONS):
            measured.append((key, module, needs(key) | {key}, needs(key)))
            for next_to in _SWEEP_NEXT_TO.get(key, ()):
                other = _with_requirements(next_to)
                measured.append((f'{key} next to {next_to}', module, needs(key) | {key} | other, needs(key) | other))

    identities = {}
    with matrix.app.app_context():
        for held in {frozenset(s) for _, _, with_key, without in measured for s in (with_key, without)}:
            identity = f'sweep{len(identities)}'
            role = UserRoleModel(name=f'matrix_{identity}', display_name=identity)
            role.permissions = [RolePermissionModel(permission=k) for k in sorted(held)]
            db.session.add(role)
            db.session.flush()
            matrix.add_identity(identity, role)
            identities[held] = identity
        db.session.remove()
    matrix.snapshot()

    result = {}
    for case in matrix.cases:
        module = case_module(case)
        if not module:
            continue
        cells = {}

        def outcome(held):
            identity = identities[frozenset(held)]
            if identity not in cells:
                cells[identity] = _status_and_probe(matrix.call(case, identity)[0])
            return cells[identity]
        result[case.name] = sorted(label for label, key_module, with_key, without in measured
                                   if key_module == module and outcome(with_key) != outcome(without))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--write', action='store_true', help='regenerate tests/rbac_golden.json')
    parser.add_argument('--write-sweep', action='store_true', help='regenerate tests/rbac_key_sweep.json')
    parser.add_argument('--sweep-output', help='write the key sweep to this file instead')
    parser.add_argument('--stats', action='store_true', help='print per-identity status counts')
    parser.add_argument('--output', help='write the matrix to this file instead of the golden file')
    args = parser.parse_args()
    # Every Flask app named 'app' shares this logger, so only silence it where nothing else runs
    logging.getLogger('app').disabled = True
    if args.write_sweep or args.sweep_output:
        with Matrix() as matrix:
            sweep = key_sweep(matrix)
        with open(args.sweep_output or SWEEP_PATH, 'w') as f:
            json.dump(sweep, f, indent=1, sort_keys=True)
            f.write('\n')
        return
    with Matrix() as matrix:
        result = matrix.run()
    if args.write or args.output:
        with open(args.output or GOLDEN_PATH, 'w') as f:
            json.dump(to_golden(result), f, indent=1, sort_keys=True)
            f.write('\n')
    if args.stats or not (args.write or args.output):
        print_stats(result)


if __name__ == '__main__':
    main()
