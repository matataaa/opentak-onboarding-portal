import { useState, useEffect, useMemo } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { rolesAPI } from '../../services/api';
import { useNotification } from '../../contexts/NotificationContext';
import { useAuth } from '../../contexts/AuthContext';
import {
  functionDescription, functionLabel, indexCatalog, moduleLabel, moduleLevel, rowPermissions,
  withRequirements, withoutDependants,
} from '../../utils/permissions';
import '../../components/AdminTable.css';
import './Roles.css';

const EMPTY_META = { name: '', displayName: '', description: '' };

const sameSet = (a, b) => a.size === b.size && [...a].every((key) => b.has(key));

function RoleEditor() {
  const { t } = useTranslation();
  const { roleId } = useParams();
  const isNew = !roleId;
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { showSuccess } = useNotification();
  const { can, permissions: myPermissions, updateUser } = useAuth();

  // Unsaved edits; null means "as saved"
  const [draftPermissions, setDraftPermissions] = useState(null);
  const [draftMeta, setDraftMeta] = useState(null);
  const [selected, setSelected] = useState(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [missing, setMissing] = useState([]);
  const [previewKeys, setPreviewKeys] = useState(null);

  const { data: role, isLoading: roleLoading, error: roleError } = useQuery({
    queryKey: ['role', roleId],
    queryFn: async () => (await rolesAPI.getById(roleId)).data,
    enabled: !isNew,
  });

  const { data: catalogData } = useQuery({
    queryKey: ['permissionCatalog'],
    queryFn: async () => (await rolesAPI.getPermissionCatalog()).data,
    enabled: !isNew,
  });
  const modules = useMemo(() => catalogData?.modules || [], [catalogData]);
  const index = useMemo(() => indexCatalog(modules), [modules]);
  const functionOf = useMemo(() => {
    const map = new Map();
    modules.forEach((m) => m.functions.forEach((fn) => fn.permissions.forEach((p) => map.set(p.key, fn.function))));
    return map;
  }, [modules]);

  const savedPermissions = useMemo(() => new Set(role?.permissions || []), [role]);
  const permissions = draftPermissions || savedPermissions;
  const savedMeta = role ? { name: role.name, displayName: role.displayName || '', description: role.description || '' } : EMPTY_META;
  const meta = draftMeta || savedMeta;

  const permissionsReadOnly = isNew || !role || role.locked || !can('roles.permissions.edit');
  const metaEditable = isNew ? can('roles.edit') : !!role && !role.isProtected && role.lockedReason !== 'aboveLevel' && can('roles.edit');

  const permissionChanges = [...permissions].filter((k) => !savedPermissions.has(k)).length
    + [...savedPermissions].filter((k) => !permissions.has(k)).length;
  const metaChanges = Object.keys(EMPTY_META).filter((field) => meta[field] !== savedMeta[field]).length;
  const changes = permissionChanges + metaChanges;
  const permissionsDirty = permissionChanges > 0;

  // Ask the server what a save would do, a moment after the last click
  useEffect(() => {
    if (permissionsReadOnly || !permissionsDirty) return undefined;
    const keys = [...permissions].sort();
    const timer = setTimeout(() => setPreviewKeys(keys), 400);
    return () => clearTimeout(timer);
  }, [permissions, permissionsDirty, permissionsReadOnly]);

  const currentKey = [...permissions].sort().join(',');
  const previewKey = permissionsDirty && previewKeys ? previewKeys.join(',') : null;
  const { data: preview, error: previewError, isFetching: previewLoading } = useQuery({
    queryKey: ['rolePreview', roleId, previewKey],
    queryFn: async () => (await rolesAPI.previewPermissions(roleId, previewKeys)).data,
    enabled: !!previewKey,
    retry: false,
  });

  // Leaving the page (reload, close, external link) with unsaved changes
  useEffect(() => {
    if (!changes) return undefined;
    const warn = (e) => {
      e.preventDefault();
      e.returnValue = '';
    };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [changes]);

  const isGrantable = (key) => index.get(key)?.grantable;
  // Ticking a key also ticks what it needs: all of that must be held already or grantable
  const canAdd = (current, key) => [...withRequirements([key], index)].every((k) => current.has(k) || isGrantable(k));

  // Only the keys being changed (and what they need or what needs them) move: other stored keys stay as they are
  const change = (fn) => {
    const next = fn(new Set(permissions));
    setDraftPermissions(sameSet(next, savedPermissions) ? null : next);
    setMissing([]);
  };

  const add = (set, key) => {
    if (canAdd(set, key)) withRequirements([key], index).forEach((k) => set.add(k));
    return set;
  };

  const toggle = (p) => change((next) => (next.has(p.key) ? withoutDependants(next, [p.key], index) : add(next, p.key)));

  const quick = (module, mode) => change((current) => {
    const removed = [];
    module.functions.forEach((fn) => {
      const { view, edit } = rowPermissions(fn);
      if (edit && !edit.administratorOnly) removed.push(edit.key);
      if (mode === 'none' && view && !view.administratorOnly) removed.push(view.key);
    });
    const next = mode === 'all' ? current : withoutDependants(current, removed.filter((key) => current.has(key)), index);
    if (mode === 'none') return next;
    module.functions.forEach((fn) => {
      const { view, edit } = rowPermissions(fn);
      [view, mode === 'all' ? edit : null].forEach((p) => {
        if (p && !p.administratorOnly) add(next, p.key);
      });
    });
    return next;
  });

  const labelsOf = (keys) => keys.map((key) => (functionOf.has(key) ? functionLabel(t, functionOf.get(key)) : key)).join(', ');

  const handleError = (err) => {
    setError(err.response?.data?.error || t('admin.roles.failedSave'));
    setMissing(err.response?.data?.missingPermissions || []);
  };

  const handleCreate = async (e) => {
    e.preventDefault();
    setError('');
    setSaving(true);
    try {
      const response = await rolesAPI.create({
        name: meta.name.trim(), displayName: meta.displayName.trim() || null, description: meta.description,
      });
      queryClient.invalidateQueries(['roles']);
      navigate(`/admin/roles/${response.data.role.id}`, { replace: true });
    } catch (err) {
      setError(err.response?.data?.error || t('admin.roles.failedCreate'));
    } finally {
      setSaving(false);
    }
  };

  const handleSave = async () => {
    setError('');
    setMissing([]);
    setSaving(true);
    try {
      if (metaChanges) {
        await rolesAPI.update(role.id, {
          name: meta.name.trim(), displayName: meta.displayName.trim() || null, description: meta.description,
        });
        setDraftMeta(null);
      }
      if (permissionsDirty) {
        await rolesAPI.setPermissions(role.id, [...permissions].sort());
        setDraftPermissions(null);
      }
      await queryClient.invalidateQueries({ queryKey: ['role', roleId] });
      queryClient.invalidateQueries({ queryKey: ['roles'] });
      showSuccess(t('admin.roles.saved'));
      // Holding this role yourself: what you may do just changed
      if (myPermissions.roles.includes(role.name)) updateUser();
    } catch (err) {
      handleError(err);
      // A first step may have been saved already
      queryClient.invalidateQueries({ queryKey: ['role', roleId] });
    } finally {
      setSaving(false);
    }
  };

  const undo = () => {
    setDraftPermissions(null);
    setDraftMeta(null);
    setError('');
    setMissing([]);
  };

  if (!isNew && roleLoading) {
    return <div className="admin-page"><div className="loading-state">{t('common.loading')}</div></div>;
  }
  if (!isNew && (roleError || !role)) {
    return (
      <div className="admin-page">
        <div className="alert alert-error">{roleError?.response?.data?.error || t('admin.roles.failedLoad')}</div>
      </div>
    );
  }

  const current = modules.find((m) => m.module === selected) || modules[0];
  const sensitiveCount = [...permissions].filter((key) => index.get(key)?.sensitive).length;
  const lockedText = role?.lockedReason === 'system' ? t('admin.roles.lockedSystem')
    : role?.lockedReason === 'aboveLevel' ? t('admin.roles.lockedAboveLevel')
      : !isNew && !can('roles.permissions.edit') ? t('admin.roles.readOnly') : null;
  const title = isNew ? t('admin.roles.newRole')
    : (metaEditable || !permissionsReadOnly ? t('admin.roles.editTitle') : t('admin.roles.viewTitle'));

  const setField = (field) => (e) => {
    const next = { ...meta, [field]: e.target.value };
    setDraftMeta(Object.keys(EMPTY_META).every((f) => next[f] === savedMeta[f]) ? null : next);
  };

  const fields = (
    <div className="role-fields">
      <div className="form-group">
        <label htmlFor="role-name">{t('admin.roles.nameLabel')}</label>
        <input id="role-name" type="text" value={meta.name} onChange={setField('name')} disabled={!metaEditable} required />
      </div>
      <div className="form-group">
        <label htmlFor="role-display-name">{t('admin.roles.displayNameLabel')}</label>
        <input id="role-display-name" type="text" value={meta.displayName} onChange={setField('displayName')} disabled={!metaEditable} />
      </div>
      <div className="form-group">
        <label htmlFor="role-description">{t('admin.roles.descriptionLabel')}</label>
        <input id="role-description" type="text" value={meta.description} onChange={setField('description')} disabled={!metaEditable} />
      </div>
    </div>
  );

  const checkbox = (fn, p, column) => {
    if (!p) return <span className="role-none" aria-hidden="true">–</span>;
    const checked = permissions.has(p.key);
    const disabled = permissionsReadOnly || p.administratorOnly || (!checked && !canAdd(permissions, p.key));
    const label = functionLabel(t, fn.function);
    return (
      <label className="role-check">
        <input
          type="checkbox"
          aria-label={t(column === 'view' ? 'admin.roles.checkboxView' : 'admin.roles.checkboxEdit', { label })}
          checked={checked}
          disabled={disabled}
          onChange={() => toggle(p)}
        />
      </label>
    );
  };

  const row = (fn) => {
    const { view, edit } = rowPermissions(fn);
    const keys = [view, edit].filter(Boolean);
    const sensitive = keys.some((p) => p.sensitive);
    const adminOnly = keys.some((p) => p.administratorOnly);
    const blocked = !permissionsReadOnly && !adminOnly
      && keys.some((p) => !permissions.has(p.key) && !canAdd(permissions, p.key));
    const description = functionDescription(t, fn.function);
    return (
      <div key={fn.function} className="role-grid role-row">
        <div className="role-row-text">
          <div className="role-row-label">
            <span>{functionLabel(t, fn.function)}</span>
            {sensitive && <span className="badge badge-warning">{t('admin.roles.sensitive')}</span>}
            {adminOnly && <span className="badge badge-secondary">{t('admin.roles.administratorOnly')}</span>}
          </div>
          {adminOnly && <div className="role-row-reason">{t('admin.roles.administratorOnlyHelp')}</div>}
          {description && <div className="role-row-desc">{description}</div>}
          {blocked && <div className="role-row-reason">{t('admin.roles.notGrantable')}</div>}
        </div>
        <div className="role-cell">{checkbox(fn, view, 'view')}</div>
        <div className="role-cell">{checkbox(fn, edit, 'edit')}</div>
      </div>
    );
  };

  const impact = preview?.impact;

  return (
    <div className="admin-page roles-page">
      <div className="roles-crumb">
        <Link to="/admin/roles">{t('admin.roles.title')}</Link> / {isNew ? t('admin.roles.newRole') : (role.displayName || role.name)}
      </div>
      <div className="admin-header">
        <h1>{title}</h1>
        <div className="roles-buttons">
          <Link className="btn btn-secondary" to="/admin/roles/overview">{t('admin.roles.overviewLink')}</Link>
        </div>
      </div>

      {isNew ? (
        <form className="roles-card" onSubmit={handleCreate}>
          {fields}
          <div className="role-savebar">
            <div className="role-impact">
              {t('admin.roles.createHelp')}
              {error && <div className="alert alert-error">{error}</div>}
            </div>
            <div className="roles-buttons">
              <button type="submit" className="btn btn-primary" disabled={saving || !meta.name.trim()}>
                {t('admin.roles.createRole')}
              </button>
            </div>
          </div>
        </form>
      ) : (
        <>
          <div className="roles-card">
            {fields}
            {(role.takProfiles?.length > 0 || role.meshtastic?.length > 0) && (
              <div className="role-audience">
                {t('admin.roles.audience', {
                  profiles: t('admin.roles.audienceProfiles', { count: role.takProfiles?.length || 0 }),
                  channels: t('admin.roles.audienceChannels', { count: role.meshtastic?.length || 0 }),
                })}
              </div>
            )}
          </div>

          {lockedText && <div className="alert alert-info">{lockedText}</div>}

          {current && (
            <div className="role-layout">
              <nav className="roles-card role-modules" aria-label={t('admin.roles.modules')}>
                {modules.map((m) => {
                  const level = moduleLevel(m, permissions);
                  return (
                    <button
                      key={m.module}
                      type="button"
                      className={`role-module-btn ${m.module === current.module ? 'active' : ''}`}
                      aria-current={m.module === current.module ? 'true' : undefined}
                      onClick={() => setSelected(m.module)}
                    >
                      <span>{moduleLabel(t, m.module)}</span>
                      {m.enabled ? (
                        <span className={`level-pill level-${level}`}>{t(`admin.roles.level.${level}`)}</span>
                      ) : (
                        <span className="level-pill level-none">{t('admin.roles.moduleOff')}</span>
                      )}
                    </button>
                  );
                })}
              </nav>

              <section className="roles-card role-functions">
                <div className="roles-card-head">
                  <h2>{moduleLabel(t, current.module)}</h2>
                  {!permissionsReadOnly && (
                    <div className="roles-buttons">
                      <button type="button" className="btn btn-sm btn-secondary" onClick={() => quick(current, 'view')}>{t('admin.roles.quickAllView')}</button>
                      <button type="button" className="btn btn-sm btn-secondary" onClick={() => quick(current, 'all')}>{t('admin.roles.quickAll')}</button>
                      <button type="button" className="btn btn-sm btn-secondary" onClick={() => quick(current, 'none')}>{t('admin.roles.quickNone')}</button>
                    </div>
                  )}
                </div>
                {!current.enabled && <div className="roles-notice">{t('admin.roles.moduleOffNotice')}</div>}
                <div className="role-grid role-grid-head" style={{ marginTop: current.enabled ? 0 : '1rem' }}>
                  <div>{t('admin.roles.functionColumn')}</div>
                  <div className="role-center">{t('admin.roles.viewColumn')}</div>
                  <div className="role-center">{t('admin.roles.editColumn')}</div>
                </div>
                {current.functions.map(row)}
                <div className="role-legend">{t('admin.roles.legend')}</div>
              </section>
            </div>
          )}

          {(metaEditable || !permissionsReadOnly) && (
            <div className="roles-card role-savebar">
              <div className="role-impact" aria-live="polite">
                <strong>{changes ? t('admin.roles.unsaved', { count: changes }) : t('admin.roles.noChanges')}</strong>
                {permissionsDirty && !previewError && (
                  <div>
                    {previewLoading || !impact || previewKey !== currentKey ? t('admin.roles.impactChecking') : t('admin.roles.impact', {
                      users: t('admin.roles.impactUsers', { count: impact.userCount }),
                      codes: t('admin.roles.impactCodes', { count: impact.codeCount }),
                    })}
                  </div>
                )}
                {!permissionsReadOnly && <div>{t('admin.roles.sensitiveCount', { count: sensitiveCount })}</div>}
                {previewError && !error && (
                  <div className="alert alert-error">
                    {previewError.response?.data?.error || t('admin.roles.failedSave')}
                    {previewError.response?.data?.missingPermissions?.length > 0 && (
                      <div>{t('admin.roles.missingRights', { rights: labelsOf(previewError.response.data.missingPermissions) })}</div>
                    )}
                  </div>
                )}
                {error && (
                  <div className="alert alert-error">
                    {error}
                    {missing.length > 0 && <div>{t('admin.roles.missingRights', { rights: labelsOf(missing) })}</div>}
                  </div>
                )}
              </div>
              <div className="roles-buttons">
                <button type="button" className="btn btn-secondary" onClick={undo} disabled={!changes || saving}>
                  {t('admin.roles.undo')}
                </button>
                <button type="button" className="btn btn-primary" onClick={handleSave} disabled={!changes || saving || !meta.name.trim()}>
                  {saving ? t('common.saving') : t('admin.roles.save')}
                </button>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}

export default RoleEditor;
