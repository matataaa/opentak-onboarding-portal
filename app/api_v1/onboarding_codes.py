"""
Onboarding Codes API endpoints
CRUD operations for onboarding code management
"""

from flask import request, jsonify
from flask_jwt_extended import jwt_required, get_jwt
from app.api_v1 import api_v1
from app.models import OnboardingCodeModel, UserModel, UserRoleModel, OTSGroupModel, GroupOnboardingCodeAssociation, db
from app.pagination import search_and_paginate
from app.rbac import (APPROVER_BEYOND_ACTOR_ERROR, CODE_BEYOND_ACTOR_ERROR, ROLE_BEYOND_ACTOR_ERROR, deny_beyond_actor,
                      drop_fields_without_permission, has_privileged_role, holder_beyond_actor, ignored_fields_body,
                      is_administrator, is_privileged_role, require_permission, role_ids_beyond_actor,
                      role_ids_include_privileged, roles_from_ids)
from sqlalchemy import case
from datetime import datetime


PRIVILEGED_ROLE_ERROR = 'Only an administrator can attach the administrator role to an onboarding code'
FIELD_PERMISSIONS = {
    # What an account gets, and whether anyone looks at it first
    'onboarding.roles.assign': ('roleIds', 'approverRoleId', 'requireApproval', 'autoApprove'),
    'onboarding.groups.assign': ('groups', 'groupIds'),
}


def _approver_denied(roles):
    """Setting or replacing an administrator role as approver is for administrators only (the phase 0 rule). Any other
    approver role is fine: approving still needs every permission the code hands out (see /approvals)."""
    if not is_administrator() and any(is_privileged_role(role) for role in roles):
        return jsonify({'error': APPROVER_BEYOND_ACTOR_ERROR}), 403
    return None


def _is_hidden(code):
    """A code carrying a privileged role hands out admin accounts: only administrators see it (phase 0)."""
    return has_privileged_role(code) and not is_administrator()


def _above_level(code):
    """Whoever has the code value gets the code's roles: a code handing out more than the user holds is listed
    read-only, without its value."""
    return holder_beyond_actor(code)


def _code_to_dict(code, details=False):
    above_level = _above_level(code)
    return {
        'id': code.id,
        'name': code.name,
        'description': code.description,
        'onboardingCode': None if above_level else code.onboardingCode,
        'aboveLevel': above_level,
        'uses': code.uses,
        'maxUses': code.maxUses,
        'autoApprove': code.autoApprove,
        'requireApproval': code.requireApproval,
        'approverRole': {
            'id': code.approverRole.id,
            'name': code.approverRole.name,
            'displayName': code.approverRole.display_name
        } if code.approverRole else None,
        'onboardContact': {
            'id': code.onboardContact.id,
            'username': code.onboardContact.username
        } if code.onboardContact else None,
        'validFrom': code.validFrom.isoformat() if code.validFrom else None,
        'expiryDate': code.expiryDate.isoformat() if code.expiryDate else None,
        'userExpiryDate': code.userExpiryDate.isoformat() if code.userExpiryDate else None,
        'roles': [{'id': r.id, 'name': r.name, 'displayName': r.display_name} for r in code.roles],
        'groups': [{'id': a.group.id, 'name': a.group.name, 'displayName': a.group.display_name, 'direction': a.direction}
                   for a in code.group_associations],
        **({'users': [{'id': u.id, 'username': u.username} for u in code.users]} if details else {}),
    }


def _above_level_denied(code):
    if _above_level(code):
        return jsonify({'error': CODE_BEYOND_ACTOR_ERROR}), 403
    return None


def _flagged_new_groups(data, existing_ids=()):
    """Groups in the request that are flagged missing in OTS and not already associated."""
    ids = [g.get('id') for g in data.get('groups') or [] if isinstance(g, dict)]
    ids += list(data.get('groupIds') or [])
    def as_int(value):
        try:
            return None if isinstance(value, bool) else int(value)
        except (TypeError, ValueError):
            return None
    return OTSGroupModel.flagged_among([i for i in ids if as_int(i) not in existing_ids])


@api_v1.route('/onboarding-codes', methods=['GET'])
@jwt_required()
@require_permission('onboarding.view')
def get_onboarding_codes():
    """Get onboarding codes (admin only), optionally filtered by search and paginated
    (all codes when per_page is omitted)"""
    query = OnboardingCodeModel.query
    if not is_administrator():
        # Before the search: searching on the value of a hidden code must not confirm it either
        privileged_ids = [role.id for role in UserRoleModel.get_all_roles() if is_privileged_role(role)]
        query = query.filter(~OnboardingCodeModel.roles.any(UserRoleModel.id.in_(privileged_ids)))
    # Nor on the value of a code above the user's level, which is listed without it
    above_level = OnboardingCodeModel.roles.any(UserRoleModel.id.in_(role_ids_beyond_actor()))
    searchable_value = case((above_level, ''), else_=OnboardingCodeModel.onboardingCode)

    codes, meta = search_and_paginate(
        query, OnboardingCodeModel.name,
        [OnboardingCodeModel.name, OnboardingCodeModel.description, searchable_value])

    return jsonify({
        'codes': [_code_to_dict(code) for code in codes],
        **meta
    }), 200


@api_v1.route('/onboarding-codes/<int:code_id>', methods=['GET'])
@jwt_required()
@require_permission('onboarding.view')
def get_onboarding_code(code_id):
    """Get onboarding code by ID"""
    code = OnboardingCodeModel.get_onboarding_code_by_id(code_id)
    if not code or _is_hidden(code):
        return jsonify({'error': 'Onboarding code not found'}), 404

    return jsonify(_code_to_dict(code, details=True)), 200


@api_v1.route('/onboarding-codes/validate/<code>', methods=['GET'])
def validate_onboarding_code(code):
    """Validate onboarding code (public endpoint for registration)"""
    onboarding_code = OnboardingCodeModel.get_onboarding_code_by_code(code)

    if not onboarding_code:
        return jsonify({'valid': False, 'error': 'Invalid code'}), 404

    # Check not-yet-active
    if onboarding_code.validFrom and onboarding_code.validFrom > datetime.now():
        return jsonify({'valid': False, 'error': 'Code is not active yet'}), 400

    # Check expiry
    if onboarding_code.expiryDate and onboarding_code.expiryDate < datetime.now():
        return jsonify({'valid': False, 'error': 'Code has expired'}), 400

    # Check max uses
    if onboarding_code.maxUses and onboarding_code.uses >= onboarding_code.maxUses:
        return jsonify({'valid': False, 'error': 'Code has reached maximum uses'}), 400

    return jsonify({
        'valid': True,
        'name': onboarding_code.name,
        'description': onboarding_code.description
    }), 200


@api_v1.route('/onboarding-codes', methods=['POST'])
@jwt_required()
@require_permission('onboarding.edit')
def create_onboarding_code():
    """Create a new onboarding code (admin only)"""
    data = request.get_json()
    ignored = drop_fields_without_permission(data, FIELD_PERMISSIONS)

    if not is_administrator() and role_ids_include_privileged(data.get('roleIds')):
        return jsonify({'error': PRIVILEGED_ROLE_ERROR}), 403
    denied = deny_beyond_actor(ROLE_BEYOND_ACTOR_ERROR, roles_from_ids(data.get('roleIds'))) \
        or _approver_denied(roles_from_ids([data.get('approverRoleId')]))
    if denied:
        return denied

    required_fields = ['name', 'onboardingCode']
    for field in required_fields:
        if not data.get(field):
            return jsonify({'error': f'{field} is required'}), 400

    # Check if code already exists
    existing = OnboardingCodeModel.get_onboarding_code_by_code(data['onboardingCode'])
    if existing:
        return jsonify({'error': 'Onboarding code already exists'}), 409

    flagged = _flagged_new_groups(data)
    if flagged:
        return jsonify({'error': 'Groups not found in OpenTAK Server: ' + ', '.join(g.name for g in flagged)}), 400

    try:
        # Parse dates
        valid_from = None
        if data.get('validFrom'):
            valid_from = datetime.fromisoformat(data['validFrom'].replace('Z', '+00:00'))

        expiry_date = None
        if data.get('expiryDate'):
            expiry_date = datetime.fromisoformat(data['expiryDate'].replace('Z', '+00:00'))

        user_expiry_date = None
        if data.get('userExpiryDate'):
            user_expiry_date = datetime.fromisoformat(data['userExpiryDate'].replace('Z', '+00:00'))

        # Get onboard contact
        onboard_contact_id = data.get('onboardContactId')

        code = OnboardingCodeModel.create_onboarding_code(
            onboardingcode=data['onboardingCode'],
            name=data['name'],
            description=data.get('description', ''),
            maxuses=data.get('maxUses'),
            onboardcontact=onboard_contact_id,
            validfrom=valid_from,
            expirydate=expiry_date,
            userexpirydate=user_expiry_date,
            autoapprove=data.get('autoApprove', False),
            requireapproval=data.get('requireApproval', False),
            approverroleid=data.get('approverRoleId')
        )

        # Add roles
        if data.get('roleIds'):
            for role_id in data['roleIds']:
                role = UserRoleModel.get_by_id(role_id)
                if role:
                    code.roles.append(role)

        # Add groups with direction
        if data.get('groups'):
            for g in data['groups']:
                group = OTSGroupModel.get_by_id(g['id'])
                if group:
                    assoc = GroupOnboardingCodeAssociation(
                        group_id=group.id,
                        onboardingcode_id=code.id,
                        direction=g.get('direction', 'BOTH')
                    )
                    db.session.add(assoc)
        elif data.get('groupIds'):
            for group_id in data['groupIds']:
                group = OTSGroupModel.get_by_id(group_id)
                if group:
                    assoc = GroupOnboardingCodeAssociation(
                        group_id=group.id,
                        onboardingcode_id=code.id,
                        direction='BOTH'
                    )
                    db.session.add(assoc)

        db.session.commit()

        return jsonify({
            'message': 'Onboarding code created successfully',
            'code': {
                'id': code.id,
                'name': code.name,
                'onboardingCode': code.onboardingCode
            },
            **ignored_fields_body(ignored)
        }), 201

    except Exception as e:
        return jsonify({'error': f'Failed to create onboarding code: {str(e)}'}), 400


@api_v1.route('/onboarding-codes/<int:code_id>', methods=['PUT'])
@jwt_required()
@require_permission('onboarding.edit')
def update_onboarding_code(code_id):
    """Update onboarding code (admin only)"""
    code = OnboardingCodeModel.get_onboarding_code_by_id(code_id)
    if not code or _is_hidden(code):
        return jsonify({'error': 'Onboarding code not found'}), 404
    denied = _above_level_denied(code)
    if denied:
        return denied

    data = request.get_json()
    ignored = drop_fields_without_permission(data, FIELD_PERMISSIONS)

    if not is_administrator() and role_ids_include_privileged(data.get('roleIds')):
        return jsonify({'error': PRIVILEGED_ROLE_ERROR}), 403
    # A full list with the roles it has now passes: those are within reach, or this code would be hidden
    denied = deny_beyond_actor(ROLE_BEYOND_ACTOR_ERROR, roles_from_ids(data.get('roleIds')))
    if not denied and 'approverRoleId' in data and data['approverRoleId'] != code.approverRoleId:
        denied = _approver_denied(roles_from_ids([data['approverRoleId']]) + ([code.approverRole] if code.approverRole else []))
    if denied:
        return denied

    if 'groups' in data or 'groupIds' in data:
        flagged = _flagged_new_groups(
            data, {a.group_id for a in GroupOnboardingCodeAssociation.query.filter_by(onboardingcode_id=code.id)})
        if flagged:
            return jsonify({'error': 'Groups not found in OpenTAK Server: ' + ', '.join(g.name for g in flagged)}), 400

    try:
        if data.get('name'):
            code.name = data['name']
        if 'description' in data:
            code.description = data['description']
        if 'maxUses' in data:
            code.maxUses = data['maxUses']
        if 'onboardContactId' in data:
            code.onboardContactId = data['onboardContactId']

        if 'validFrom' in data:
            if data['validFrom']:
                code.validFrom = datetime.fromisoformat(data['validFrom'].replace('Z', '+00:00'))
            else:
                code.validFrom = None

        if 'expiryDate' in data:
            if data['expiryDate']:
                code.expiryDate = datetime.fromisoformat(data['expiryDate'].replace('Z', '+00:00'))
            else:
                code.expiryDate = None

        if 'userExpiryDate' in data:
            if data['userExpiryDate']:
                code.userExpiryDate = datetime.fromisoformat(data['userExpiryDate'].replace('Z', '+00:00'))
            else:
                code.userExpiryDate = None

        if 'autoApprove' in data:
            code.autoApprove = data['autoApprove']

        if 'requireApproval' in data:
            code.requireApproval = data['requireApproval']

        if 'approverRoleId' in data:
            code.approverRoleId = data['approverRoleId']

        # Update roles if roleIds is provided in the request
        if 'roleIds' in data:
            code.roles.clear()
            for role_id in data['roleIds']:
                role = UserRoleModel.get_by_id(role_id)
                if role:
                    code.roles.append(role)

        # Update groups with direction
        if 'groups' in data:
            GroupOnboardingCodeAssociation.query.filter_by(onboardingcode_id=code.id).delete()
            for g in data['groups']:
                group = OTSGroupModel.get_by_id(g['id'])
                if group:
                    assoc = GroupOnboardingCodeAssociation(
                        group_id=group.id,
                        onboardingcode_id=code.id,
                        direction=g.get('direction', 'BOTH')
                    )
                    db.session.add(assoc)
        elif 'groupIds' in data:
            GroupOnboardingCodeAssociation.query.filter_by(onboardingcode_id=code.id).delete()
            for group_id in data['groupIds']:
                group = OTSGroupModel.get_by_id(group_id)
                if group:
                    assoc = GroupOnboardingCodeAssociation(
                        group_id=group.id,
                        onboardingcode_id=code.id,
                        direction='BOTH'
                    )
                    db.session.add(assoc)

        # Commit directly to ensure relationship changes are saved
        db.session.commit()

        return jsonify({
            'message': 'Onboarding code updated successfully',
            **ignored_fields_body(ignored)
        }), 200

    except Exception as e:
        db.session.rollback()
        return jsonify({'error': f'Failed to update onboarding code: {str(e)}'}), 400


@api_v1.route('/onboarding-codes/<int:code_id>', methods=['DELETE'])
@jwt_required()
@require_permission('onboarding.delete')
def delete_onboarding_code(code_id):
    """Delete onboarding code (admin only)"""
    code = OnboardingCodeModel.get_onboarding_code_by_id(code_id)
    if not code or _is_hidden(code):
        return jsonify({'error': 'Onboarding code not found'}), 404
    denied = _above_level_denied(code)
    if denied:
        return denied

    try:
        OnboardingCodeModel.delete_onboarding_code_by_id(code_id)
        return jsonify({'message': 'Onboarding code deleted successfully'}), 200

    except Exception as e:
        return jsonify({'error': f'Failed to delete onboarding code: {str(e)}'}), 400
