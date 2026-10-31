"""
Deleting a user keeps OTS and the local database in step: a delete that cannot succeed
locally never reaches OTS, and an OTS failure keeps the user in both stores.
"""
from datetime import datetime, timedelta

import pytest
import sqlalchemy as sa
from flask_jwt_extended import create_access_token

from app import email as email_module, jobs
from app.api_key_auth import api_key_required
from app.api_v1 import users as users_mod
from app.exceptions import AuthenticationError, AuthorizationError, BadRequestError
from app.extensions import scheduler
from app.ots import OTSClient
from app.models import (AnnouncementModel, AnnouncementReadModel, ApiKeyModel, EventModel, EventTeamLeaderModel,
                        EventTeamModel, GroupUserAssociation, KioskSessionModel, MeshtasticChannelGroup,
                        MeshtasticModel, OneTimeTokenModel, OTSGroupModel, PendingRegistrationModel,
                        OnboardingCodeModel, RadioModel, SystemSettingsModel, TakProfileModel, UserModel,
                        UserRoleModel, db as _db)


class FakeOTS:
    def __init__(self):
        self.deleted = []
        self.error = None
        self.error_for = {}  # username -> exception from delete_user
        self.accounts = []  # usernames listed by get_users, one per page
        self.list_error = None

    def __call__(self, *args):
        return self

    def delete_user(self, username):
        self.deleted.append(username)
        if self.error or username in self.error_for:
            raise self.error or self.error_for[username]

    def get_users(self, page=None, per_page=None):
        if self.list_error:
            raise self.list_error
        return {'response': {'total_pages': len(self.accounts),
                             'results': [{'username': n} for n in self.accounts[page - 1:page]]}}

    get_all_pages = OTSClient.get_all_pages


@pytest.fixture
def ots(monkeypatch, app):
    fake = FakeOTS()
    monkeypatch.setattr(users_mod, 'OTSClient', fake)
    monkeypatch.setattr(jobs, 'otsClient', fake)
    monkeypatch.setattr(scheduler, 'app', app, raising=False)
    return fake


def primary_keys(model):
    return {_db.inspect(o).identity for o in model.query.all()}


@pytest.fixture
def data(app):
    """Removes only what the test created, also after a failure left references behind."""
    models = (KioskSessionModel, AnnouncementReadModel, OneTimeTokenModel, AnnouncementModel, ApiKeyModel,
              RadioModel, PendingRegistrationModel, EventTeamLeaderModel, EventTeamModel, EventModel,
              GroupUserAssociation, OnboardingCodeModel, TakProfileModel, MeshtasticModel, MeshtasticChannelGroup,
              OTSGroupModel, UserModel, UserRoleModel, SystemSettingsModel)
    with app.app_context():
        before = {m: primary_keys(m) for m in models}
        yield _db
        _db.session.rollback()
        # One object per commit, several passes: a refused delete must not block the others
        for _ in range(3):
            for m in models:
                for key in primary_keys(m) - before[m]:
                    try:
                        _db.session.delete(_db.session.get(m, key))
                        _db.session.commit()
                    except Exception:
                        _db.session.rollback()


def add_user(name, **kwargs):
    user = UserModel(username=name, **kwargs)
    _db.session.add(user)
    _db.session.commit()
    return user


@pytest.fixture
def admin(data):
    role = UserRoleModel.query.filter_by(name='administrator').first()
    if not role:
        role = UserRoleModel(name='administrator')
        _db.session.add(role)
    user = UserModel(username='zz_admin', roles=[role])
    _db.session.add(user)
    _db.session.commit()
    return {'Authorization': f'Bearer {create_access_token(identity=str(user.id))}'}


def add_token(user):
    _db.session.add(OneTimeTokenModel(token=f'tok-{user.username}', user_id=user.id, token_type='password_reset',
                                      expires_at=datetime.now() + timedelta(hours=1)))


def add_announcement(creator):
    a = AnnouncementModel(title='A', content='c', target_type='all', status='draft', created_by=creator.id)
    _db.session.add(a)
    _db.session.flush()
    return a


def present(name):
    """Sees the session as the endpoint left it, so a missing rollback shows up."""
    return UserModel.query.filter_by(username=name).first() is not None


def references_to(user_id):
    """Row count per column with a foreign key to users.id."""
    return {
        f'{table.name}.{column.name}': _db.session.execute(
            sa.select(sa.func.count()).select_from(table).where(column == user_id)).scalar()
        for table in _db.metadata.sorted_tables for column in table.c
        if any(fk.target_fullname == 'users.id' for fk in column.foreign_keys)
    }


def test_delete_clears_every_reference(client, admin, ots):
    creator = add_user('zz_creator')
    user = add_user('zz_gone')
    uid = user.id
    other = add_user('zz_onboarded', onboardedBy=uid)
    add_token(user)
    read = AnnouncementReadModel(announcement_id=add_announcement(creator).id, user_id=uid)
    kiosk = KioskSessionModel(session_id='zz-kiosk', user_id=uid, expires_at=datetime.now() + timedelta(hours=1))
    radio = RadioModel(name='zz-radio', platform='meshtastic', radioType='meshtastic', assignedTo=uid, owner=uid)
    code = OnboardingCodeModel(name='zz-code', onboardingCode='zz-code', onboardContactId=uid)
    user.roles.append(UserRoleModel(name='zz-role'))
    user.takprofiles.append(TakProfileModel(name='zz-profile', description='d'))
    user.onboarding_codes.append(code)
    user.meshtastic.append(MeshtasticModel(name='zz-channel'))
    user.meshtastic_groups.append(MeshtasticChannelGroup(name='zz-channel-group'))
    user.targeted_announcements.append(add_announcement(creator))
    group = OTSGroupModel(name='zz-group')
    _db.session.add_all([read, kiosk, radio, code, group])
    _db.session.flush()
    _db.session.add(GroupUserAssociation(group_id=group.id, user_id=uid))
    _db.session.flush()
    pending = PendingRegistrationModel(username='zz_pending', email='zz@example.com', password='x',
                                       onboarding_code_id=code.id, verification_token='zz-verify',
                                       expires_at=datetime.now() + timedelta(hours=1), approved_by=uid)
    event = EventModel(name='zz-event', createdBy=uid)
    _db.session.add_all([pending, event])
    _db.session.flush()
    team = EventTeamModel(name='zz-team', leaderUserId=uid)
    _db.session.add(team)
    _db.session.flush()
    _db.session.add(EventTeamLeaderModel(eventId=event.id, eventTeamId=team.id, userId=uid))
    add_announcement(user)
    _db.session.add(ApiKeyModel(name='k', key_hash='zz-hash', key_prefix='zz', created_by=uid))
    _db.session.commit()
    radio_id, pending_id, event_id, team_id, other_id = radio.id, pending.id, event.id, team.id, other.id
    code_id = code.id
    assert all(references_to(uid).values())

    r = client.delete(f'/api/v1/users/{uid}', headers=admin)

    assert r.status_code == 200, r.get_json()
    assert 'both OTS' in r.get_json()['message']
    assert ots.deleted == ['zz_gone']
    _db.session.expire_all()
    assert not present('zz_gone')
    assert not any(references_to(uid).values())
    assert _db.session.get(OnboardingCodeModel, code_id).onboardContactId is None
    radio = _db.session.get(RadioModel, radio_id)
    assert radio.assignedTo is None and radio.owner is None
    assert _db.session.get(PendingRegistrationModel, pending_id).approved_by is None
    assert _db.session.get(UserModel, other_id).onboardedBy is None
    assert _db.session.get(EventModel, event_id).createdBy is None
    assert _db.session.get(EventTeamModel, team_id).leaderUserId is None


def test_integrity_error_at_flush_is_refused_before_ots(client, admin, ots, monkeypatch):
    user = add_user('zz_blocked')
    add_token(user)
    _db.session.add(RadioModel(name='zz-radio', platform='meshtastic', radioType='meshtastic', owner=user.id))
    _db.session.commit()
    uid = user.id
    # A reference nothing clears yet: the real foreign key refuses the delete
    monkeypatch.setattr(UserModel, 'clear_references', staticmethod(lambda user_id: None))

    r = client.delete(f'/api/v1/users/{uid}', headers=admin)

    assert r.status_code == 409
    assert 'other records' in r.get_json()['error']
    assert ots.deleted == []
    assert present('zz_blocked')
    # The cleanup done before the refused flush was rolled back too
    assert OneTimeTokenModel.query.filter_by(user_id=uid).count() == 1


@pytest.fixture(params=[True, False], ids=['fk_on', 'fk_off'])
def foreign_keys(request, data):
    """Production SQLite does not enforce foreign keys; only the test suite turns them on."""
    if request.param:
        yield
        return
    _db.session.commit()
    _db.session.execute(sa.text('PRAGMA foreign_keys=OFF'))
    assert _db.session.execute(sa.text('PRAGMA foreign_keys')).scalar() == 0
    _db.session.commit()
    yield
    _db.session.rollback()
    _db.session.execute(sa.text('PRAGMA foreign_keys=ON'))
    _db.session.commit()


@api_key_required()
def _protected():
    return 'ok'


def key_status(app, raw_key):
    """Status of a request carrying this key through the real decorator (no route uses it yet)."""
    with app.test_request_context('/', headers={'X-API-Key': raw_key}):
        return app.make_response(_protected()).status_code


def add_owned_records(user):
    announcement = add_announcement(user)
    api_key, raw_key = ApiKeyModel.create_api_key('zz-key', user.id)
    _db.session.commit()
    return announcement.id, api_key.id, raw_key


def assert_records_outlive(app, announcement_id, api_key_id, raw_key):
    _db.session.expire_all()
    assert _db.session.get(AnnouncementModel, announcement_id).created_by is None
    api_key = _db.session.get(ApiKeyModel, api_key_id)
    assert api_key.created_by is None and api_key.is_active is False
    assert key_status(app, raw_key) == 401


def test_owner_is_deleted_and_records_outlive(client, app, admin, ots, foreign_keys):
    user = add_user('zz_owner')
    announcement_id, api_key_id, raw_key = add_owned_records(user)
    assert key_status(app, raw_key) == 200

    r = client.delete(f'/api/v1/users/{user.id}', headers=admin)

    assert r.status_code == 200, r.get_json()
    assert ots.deleted == ['zz_owner']
    assert not present('zz_owner')
    assert_records_outlive(app, announcement_id, api_key_id, raw_key)


def test_job_removes_expired_owner_and_their_keys_stop_working(app, data, ots, foreign_keys):
    SystemSettingsModel.set_setting('remove_expired_accounts_enabled', 'true', 'registration', 'test')
    user = add_user('zz_owner', expiryDate=datetime.now() - timedelta(days=1))
    announcement_id, api_key_id, raw_key = add_owned_records(user)
    assert key_status(app, raw_key) == 200

    jobs.remove_expired_accounts()

    assert ots.deleted == ['zz_owner']
    assert not present('zz_owner')
    assert_records_outlive(app, announcement_id, api_key_id, raw_key)


def test_key_without_creator_can_be_listed_and_deleted_by_admin(client, admin, ots):
    user = add_user('zz_owner')
    _, api_key_id, _ = add_owned_records(user)
    assert client.delete(f'/api/v1/users/{user.id}', headers=admin).status_code == 200

    listed = client.get('/api/v1/admin/api-keys', headers=admin)
    assert listed.status_code == 200, listed.get_json()
    entry = next(k for k in listed.get_json()['api_keys'] if k['id'] == api_key_id)
    assert entry['created_by'] is None and entry['is_active'] is False
    assert client.get(f'/api/v1/admin/api-keys/{api_key_id}', headers=admin).status_code == 200

    assert client.delete(f'/api/v1/admin/api-keys/{api_key_id}', headers=admin).status_code == 200
    assert _db.session.get(ApiKeyModel, api_key_id) is None


def test_scheduled_announcement_of_deleted_author_still_goes_out(client, app, admin, ots, monkeypatch):
    sent = []
    monkeypatch.setattr(email_module.mail, 'send', lambda msg: sent.append(msg.recipients[0]))
    monkeypatch.setattr(email_module, 'MAIL_ENABLED', True)
    monkeypatch.setattr(jobs, 'MAIL_ENABLED', True)
    author = add_user('zz_author')
    recipient = add_user('zz_recipient', email='zz_recipient@example.com')
    announcement = AnnouncementModel(title='A', content='c', target_type='users', status='scheduled', send_email=True,
                                     scheduled_at=datetime.now() - timedelta(minutes=1), created_by=author.id)
    announcement.target_users.append(recipient)
    _db.session.add(announcement)
    _db.session.commit()
    announcement_id = announcement.id
    assert client.delete(f'/api/v1/users/{author.id}', headers=admin).status_code == 200

    jobs.send_scheduled_announcements()

    _db.session.expire_all()
    announcement = _db.session.get(AnnouncementModel, announcement_id)
    assert announcement.created_by is None and announcement.status == 'sent'
    assert sent == ['zz_recipient@example.com']


def test_ots_bad_request_for_missing_account_deletes_locally(client, admin, ots):
    uid = add_user('zz_not_in_ots').id
    ots.error = BadRequestError({'status_code': 400})
    ots.accounts = ['someone']

    r = client.delete(f'/api/v1/users/{uid}', headers=admin)

    assert r.status_code == 200
    assert 'both OTS' not in r.get_json()['message']
    assert not present('zz_not_in_ots')


def bad_request():
    return BadRequestError({'status_code': 400})


@pytest.mark.parametrize('accounts, list_error', [(['someone', 'zz_refused'], None),
                                                  ([], ConnectionError('OTS went away'))])
def test_ots_bad_request_while_account_may_exist_keeps_user(client, admin, ots, accounts, list_error):
    uid = add_user('zz_refused').id
    ots.error = bad_request()  # LDAP mode, own account, failure during delete
    ots.accounts = accounts
    ots.list_error = list_error

    r = client.delete(f'/api/v1/users/{uid}', headers=admin)

    assert r.status_code == 502
    assert present('zz_refused')


@pytest.mark.parametrize('error', [
    ConnectionError('Could not connect to OTS server'),
    AuthenticationError({'status_code': 400}),
    AuthorizationError({'status_code': 401}),
    Exception({'status_code': 500}),
])
def test_ots_failure_keeps_user_and_rolls_back(client, admin, ots, error):
    user = add_user('zz_kept')
    add_token(user)
    _db.session.commit()
    uid = user.id
    ots.error = error

    r = client.delete(f'/api/v1/users/{uid}', headers=admin)

    assert r.status_code == 502
    assert 'kept' in r.get_json()['error']
    assert present('zz_kept')
    assert OneTimeTokenModel.query.filter_by(user_id=uid).count() == 1


def test_delete_requires_admin(client, data, ots):
    user = add_user('zz_plain')
    headers = {'Authorization': f'Bearer {create_access_token(identity=str(user.id))}'}
    assert client.delete(f'/api/v1/users/{user.id}', headers=headers).status_code == 403
    assert ots.deleted == []


def test_expired_account_job_removes_user_holding_references(data, ots):
    SystemSettingsModel.set_setting('remove_expired_accounts_enabled', 'true', 'registration', 'test')
    user = add_user('zz_expired', expiryDate=datetime.now() - timedelta(days=1))
    uid = user.id
    add_token(user)
    _db.session.add(RadioModel(name='zz-radio', platform='meshtastic', radioType='meshtastic', owner=uid))
    _db.session.commit()

    jobs.remove_expired_accounts()

    assert ots.deleted == ['zz_expired']
    assert not present('zz_expired')
    assert OneTimeTokenModel.query.filter_by(user_id=uid).count() == 0


def test_job_removes_user_missing_from_ots_but_keeps_refused_one(data, ots):
    SystemSettingsModel.set_setting('remove_expired_accounts_enabled', 'true', 'registration', 'test')
    add_user('zz_expired', expiryDate=datetime.now() - timedelta(days=1))
    ots.error = bad_request()

    ots.accounts = ['zz_expired']
    jobs.remove_expired_accounts()
    assert present('zz_expired')

    ots.accounts = []
    jobs.remove_expired_accounts()
    assert not present('zz_expired')


def case_variant_account(ots, retry_fails=False):
    """OTS holds ZZ_Enzo on page 2; deleting the portal's lowercase name answers 400."""
    ots.accounts = ['someone', 'ZZ_Enzo']
    ots.error_for = {'zz_enzo': bad_request()}
    if retry_fails:
        ots.error_for['ZZ_Enzo'] = bad_request()


def test_endpoint_deletes_ots_account_differing_in_case(client, admin, ots):
    uid = add_user('zz_enzo').id
    case_variant_account(ots)

    r = client.delete(f'/api/v1/users/{uid}', headers=admin)

    assert r.status_code == 200
    assert 'both OTS' in r.get_json()['message']
    assert ots.deleted == ['zz_enzo', 'ZZ_Enzo']
    assert not present('zz_enzo')


def test_endpoint_keeps_user_when_case_variant_delete_fails(client, admin, ots):
    uid = add_user('zz_enzo').id
    case_variant_account(ots, retry_fails=True)

    r = client.delete(f'/api/v1/users/{uid}', headers=admin)

    assert r.status_code == 502
    assert ots.deleted == ['zz_enzo', 'ZZ_Enzo']
    assert present('zz_enzo')


def test_endpoint_refuses_to_guess_between_case_variants(client, admin, ots):
    uid = add_user('zz_enzo').id
    case_variant_account(ots)
    ots.accounts.append('Zz_enzO')

    r = client.delete(f'/api/v1/users/{uid}', headers=admin)

    assert r.status_code == 502
    assert ots.deleted == ['zz_enzo']
    assert present('zz_enzo')


@pytest.mark.parametrize('retry_fails', [False, True])
def test_job_handles_ots_account_differing_in_case(data, ots, retry_fails):
    SystemSettingsModel.set_setting('remove_expired_accounts_enabled', 'true', 'registration', 'test')
    add_user('zz_enzo', expiryDate=datetime.now() - timedelta(days=1))
    case_variant_account(ots, retry_fails)

    jobs.remove_expired_accounts()

    assert ots.deleted == ['zz_enzo', 'ZZ_Enzo']
    assert present('zz_enzo') is retry_fails
