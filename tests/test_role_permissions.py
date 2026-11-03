"""
role_permissions rows live and die with their role, also on production SQLite,
which does not enforce foreign keys (only tests/conftest.py turns them on).
"""
import pytest
from sqlalchemy.exc import IntegrityError

from app.models import RolePermissionModel, UserRoleModel, db as _db


def set_sqlite_fks(enabled):
    # A no-op inside a transaction: the callers commit or roll back first.
    conn = _db.session.connection()
    conn.exec_driver_sql(f"PRAGMA foreign_keys={'ON' if enabled else 'OFF'}")
    assert conn.exec_driver_sql('PRAGMA foreign_keys').scalar() == int(enabled)
    _db.session.commit()


@pytest.fixture
def clean_roles(app):
    yield
    _db.session.rollback()
    set_sqlite_fks(True)
    for role in UserRoleModel.query.filter(UserRoleModel.name.like('perm_test_%')).all():
        _db.session.delete(role)
    _db.session.commit()


@pytest.fixture
def fks_off(clean_roles):
    _db.session.commit()
    set_sqlite_fks(False)


def make_role(name, *permissions, **kwargs):
    role = UserRoleModel(name=f'perm_test_{name}', **kwargs)
    role.permissions = [RolePermissionModel(permission=p) for p in permissions]
    _db.session.add(role)
    _db.session.commit()
    return role


def rows_for(role_id):
    return RolePermissionModel.query.filter_by(role_id=role_id).count()


def test_deleting_a_role_removes_its_rows_with_fks_off(fks_off):
    doomed = make_role('doomed', 'users.create', 'roles.view')
    other = make_role('other', 'users.create')
    doomed_id = doomed.id

    UserRoleModel.delete_by_id(doomed_id)

    assert rows_for(doomed_id) == 0
    assert rows_for(other.id) == 1


def test_new_role_with_a_reused_id_has_no_rows(fks_off):
    old = make_role('old', 'users.create')
    old_id = old.id
    UserRoleModel.delete_by_id(old_id)

    new = make_role('new', id=old_id)

    assert new.id == old_id
    assert rows_for(old_id) == 0
    assert new.permissions == []


def test_unique_pair_is_enforced(clean_roles):
    role = make_role('unique', 'users.create')
    other = make_role('unique_other', 'users.create')  # same key on another role is fine
    _db.session.add(RolePermissionModel(role_id=role.id, permission='roles.view'))  # other key too
    _db.session.commit()
    assert rows_for(other.id) == 1

    _db.session.add(RolePermissionModel(role_id=role.id, permission='users.create'))
    with pytest.raises(IntegrityError, match='role_id|uq_role_permissions_role_id_permission'):
        _db.session.commit()
    _db.session.rollback()
    assert rows_for(role.id) == 2
