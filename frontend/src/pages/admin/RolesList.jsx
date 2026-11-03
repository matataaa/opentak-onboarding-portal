import { useState, useEffect, useMemo } from 'react';
import { useQuery, useMutation, useQueryClient, keepPreviousData } from '@tanstack/react-query';
import { Link, useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { rolesAPI } from '../../services/api';
import { useNotification } from '../../contexts/NotificationContext';
import { useAuth } from '../../contexts/AuthContext';
import { isPrivilegedRole } from '../../utils/roles';
import { moduleLabel, moduleLevel } from '../../utils/permissions';
import Pagination from '../../components/Pagination';
import '../../components/AdminTable.css';
import './Roles.css';

// Pills per module for what a role holds: one pill when every module has the same level
export function RoleModulePills({ role, modules }) {
  const { t } = useTranslation();
  const held = new Set(role.permissions || []);
  const levels = modules.map((m) => ({ module: m.module, level: moduleLevel(m, held) }));
  const used = levels.filter((l) => l.level !== 'none');
  if (used.length === 0) {
    return <span className="level-pill level-none">{t('admin.roles.noRights')}</span>;
  }
  if (used.length === levels.length && used.every((l) => l.level === used[0].level)) {
    return (
      <span className={`level-pill level-${used[0].level}`} title={t(`admin.roles.level.${used[0].level}`)}>
        {t('admin.roles.allModules', { count: levels.length })}
      </span>
    );
  }
  return (
    <div className="roles-pills">
      {used.map((l) => (
        <span key={l.module} className={`level-pill level-${l.level}`} title={t(`admin.roles.level.${l.level}`)}>
          {moduleLabel(t, l.module)}
        </span>
      ))}
    </div>
  );
}

function RolesList() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { showError, confirm } = useNotification();
  const { can } = useAuth();
  const canCreate = can('roles.edit');
  const canDelete = can('roles.delete');
  const canDuplicate = can('roles.edit') && can('roles.permissions.edit');
  const [page, setPage] = useState(1);
  const [perPage, setPerPage] = useState(20);
  const [search, setSearch] = useState('');
  const [debouncedSearch, setDebouncedSearch] = useState('');
  // null: open only while a search finds system roles alone
  const [systemOpen, setSystemOpen] = useState(null);
  const [duplicating, setDuplicating] = useState(null);
  const [duplicateForm, setDuplicateForm] = useState({ name: '', displayName: '' });
  const [error, setError] = useState('');

  // Debounce search input
  useEffect(() => {
    const timer = setTimeout(() => {
      setDebouncedSearch(search);
      setPage(1); // Reset to page 1 when search changes
    }, 500); // Wait 500ms after user stops typing

    return () => clearTimeout(timer);
  }, [search]);

  // All matching roles: custom ones are paged here, system ones go in their own block
  const { data: rolesData, isLoading } = useQuery({
    queryKey: ['roles', 'list', debouncedSearch],
    queryFn: async () => {
      const response = await rolesAPI.getAll({ search: debouncedSearch });
      return response.data;
    },
    // Keep showing the current rows while the next search loads, so the search box keeps focus
    placeholderData: keepPreviousData,
  });

  const { data: catalogData } = useQuery({
    queryKey: ['permissionCatalog'],
    queryFn: async () => (await rolesAPI.getPermissionCatalog()).data,
  });
  const modules = useMemo(() => catalogData?.modules || [], [catalogData]);
  // What the current user may hand on (the catalogue knows, also for disabled modules)
  const grantable = useMemo(() => new Set(modules.flatMap((m) => m.functions.flatMap(
    (fn) => fn.permissions.filter((p) => p.grantable).map((p) => p.key)))), [modules]);
  const administratorOnly = useMemo(() => new Set(modules.flatMap((m) => m.functions.flatMap(
    (fn) => fn.permissions.filter((p) => p.administratorOnly).map((p) => p.key)))), [modules]);

  const deleteMutation = useMutation({
    mutationFn: (id) => rolesAPI.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries(['roles']);
    },
    onError: (err) => {
      showError(err.response?.data?.error || t('admin.roles.failedDelete'));
    },
  });

  const duplicateMutation = useMutation({
    mutationFn: ({ id, data }) => rolesAPI.duplicate(id, data),
    onSuccess: (response) => {
      queryClient.invalidateQueries(['roles']);
      setDuplicating(null);
      navigate(`/admin/roles/${response.data.role.id}`);
    },
    onError: (err) => {
      setError(err.response?.data?.error || t('admin.roles.failedDuplicate'));
    },
  });

  const handleDelete = async (role) => {
    const confirmed = await confirm(
      t('admin.roles.deleteConfirm', { name: role.displayName || role.name }),
      t('admin.roles.deleteRole')
    );
    if (confirmed) {
      deleteMutation.mutate(role.id);
    }
  };

  const openDuplicate = (role) => {
    setDuplicating(role);
    setDuplicateForm({ name: '', displayName: '' });
    setError('');
  };

  const handleDuplicate = (e) => {
    e.preventDefault();
    setError('');
    duplicateMutation.mutate({
      id: duplicating.id,
      data: { name: duplicateForm.name.trim(), displayName: duplicateForm.displayName.trim() || null },
    });
  };

  // A copy is all or nothing: the user must be able to grant every key it would get
  const beyondLevel = (role) => (role.permissions || []).some((key) => !administratorOnly.has(key) && !grantable.has(key));

  const roles = rolesData?.roles || [];
  const customRoles = roles.filter((r) => r.isCustom);
  const systemRoles = roles.filter((r) => !r.isCustom);
  const pagedCustom = perPage ? customRoles.slice((page - 1) * perPage, page * perPage) : customRoles;
  const showSystem = systemOpen ?? (!!debouncedSearch && customRoles.length === 0);

  if (isLoading) {
    return <div className="admin-page"><div className="loading-state">{t('common.loading')}</div></div>;
  }

  const duplicateButton = (role) => canDuplicate && !isPrivilegedRole(role.name) && (
    <button
      type="button"
      className="btn btn-sm btn-secondary"
      onClick={() => openDuplicate(role)}
      disabled={beyondLevel(role)}
      title={beyondLevel(role) ? t('admin.roles.beyondLevel') : undefined}
    >
      {t('admin.roles.duplicate')}
    </button>
  );

  const deleteButton = (role) => canDelete && !role.isProtected && role.lockedReason !== 'aboveLevel' && (
    <button type="button" className="btn btn-sm btn-danger" onClick={() => handleDelete(role)}>
      {t('common.delete')}
    </button>
  );

  const roleName = (role) => (
    <>
      <strong>{role.displayName || role.name}</strong>
      {role.displayName && role.displayName !== role.name && <div className="roles-sub">{role.name}</div>}
    </>
  );

  return (
    <div className="admin-page roles-page">
      <div className="admin-header">
        <h1>{t('admin.roles.title')}</h1>
        <div className="admin-actions">
          <input
            type="text"
            className="search-box"
            placeholder={t('admin.roles.searchPlaceholder')}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <Link className="btn btn-secondary" to="/admin/roles/overview">{t('admin.roles.overviewLink')}</Link>
          {canCreate && (
            <Link className="btn btn-primary" to="/admin/roles/new">{t('admin.roles.newRole')}</Link>
          )}
        </div>
      </div>

      <section className="roles-card">
        <div className="roles-card-head">
          <div>
            <h2>{t('admin.roles.customRoles')}</h2>
            <div className="roles-sub">{t('admin.roles.customRolesHelp')}</div>
          </div>
        </div>
        <div className="admin-table-container">
          {customRoles.length === 0 ? (
            <div className="empty-state">
              {debouncedSearch ? t('common.noResults') : t('admin.roles.noCustomRoles')}
            </div>
          ) : (
            <table className="admin-table">
              <thead>
                <tr>
                  <th>{t('admin.roles.role')}</th>
                  <th>{t('common.description')}</th>
                  <th>{t('admin.roles.modules')}</th>
                  <th className="roles-num">{t('admin.roles.users')}</th>
                  <th className="roles-num">{t('admin.roles.codes')}</th>
                  <th>{t('common.actions')}</th>
                </tr>
              </thead>
              <tbody>
                {pagedCustom.map((role) => (
                  <tr key={role.id}>
                    <td>{roleName(role)}</td>
                    <td>{role.description || '-'}</td>
                    <td><RoleModulePills role={role} modules={modules} /></td>
                    <td className="roles-num">{role.userCount || 0}</td>
                    <td className="roles-num">{role.codeCount || 0}</td>
                    <td>
                      <div className="table-actions">
                        <Link className="btn btn-sm btn-secondary" to={`/admin/roles/${role.id}`}>{t('admin.roles.rights')}</Link>
                        {duplicateButton(role)}
                        {deleteButton(role)}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {customRoles.length > 0 && (
            <Pagination
              page={page}
              perPage={perPage}
              total={customRoles.length}
              onPageChange={setPage}
              onPerPageChange={(size) => { setPerPage(size); setPage(1); }}
            />
          )}
        </div>
      </section>

      <section className="roles-card">
        <div className="roles-card-head">
          <div>
            <h2>{t('admin.roles.systemRoles')}</h2>
            <div className="roles-sub">{t('admin.roles.systemRolesHelp')}</div>
          </div>
          <button
            type="button"
            className="btn btn-secondary"
            aria-expanded={showSystem}
            aria-controls="roles-system-table"
            onClick={() => setSystemOpen(!showSystem)}
          >
            {showSystem ? t('admin.roles.collapse') : t('admin.roles.showAll', { count: systemRoles.length })}
          </button>
        </div>
        {showSystem && (
          <div className="admin-table-container" id="roles-system-table">
            {systemRoles.length === 0 ? (
              <div className="empty-state">{debouncedSearch ? t('common.noResults') : t('admin.roles.noRoles')}</div>
            ) : (
              <table className="admin-table">
                <thead>
                  <tr>
                    <th>{t('admin.roles.role')}</th>
                    <th>{t('admin.roles.rights')}</th>
                    <th className="roles-num">{t('admin.roles.users')}</th>
                    <th>{t('common.actions')}</th>
                  </tr>
                </thead>
                <tbody>
                  {systemRoles.map((role) => (
                    <tr key={role.id}>
                      <td>
                        {roleName(role)}
                        <div><span className="badge badge-secondary">{t('admin.roles.locked')}</span></div>
                      </td>
                      <td><RoleModulePills role={role} modules={modules} /></td>
                      <td className="roles-num">{role.userCount || 0}</td>
                      <td>
                        <div className="table-actions">
                          <Link className="btn btn-sm btn-secondary" to={`/admin/roles/${role.id}`}>{t('common.view')}</Link>
                          {duplicateButton(role)}
                          {deleteButton(role)}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}
      </section>

      {/* Duplicate: asks for the name of the copy */}
      {duplicating && (
        <div className="modal-overlay">
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>{t('admin.roles.duplicateTitle', { name: duplicating.displayName || duplicating.name })}</h2>
              <button className="modal-close" onClick={() => setDuplicating(null)}>×</button>
            </div>
            <form onSubmit={handleDuplicate}>
              <div className="modal-body">
                {error && <div className="alert alert-error">{error}</div>}
                <p className="roles-sub">{t('admin.roles.duplicateHelp')}</p>
                <div className="form-group">
                  <label htmlFor="duplicate-name">{t('admin.roles.nameLabel')}</label>
                  <input
                    id="duplicate-name"
                    type="text"
                    value={duplicateForm.name}
                    onChange={(e) => setDuplicateForm({ ...duplicateForm, name: e.target.value })}
                    required
                  />
                  <span className="help-text">{t('admin.roles.nameHelp')}</span>
                </div>
                <div className="form-group">
                  <label htmlFor="duplicate-display-name">{t('admin.roles.displayNameLabel')}</label>
                  <input
                    id="duplicate-display-name"
                    type="text"
                    value={duplicateForm.displayName}
                    onChange={(e) => setDuplicateForm({ ...duplicateForm, displayName: e.target.value })}
                  />
                  <span className="help-text">{t('admin.roles.displayNameHelp')}</span>
                </div>
              </div>
              <div className="modal-footer">
                <button type="button" className="btn btn-secondary" onClick={() => setDuplicating(null)}>
                  {t('common.cancel')}
                </button>
                <button type="submit" className="btn btn-primary" disabled={duplicateMutation.isPending || !duplicateForm.name.trim()}>
                  {t('admin.roles.duplicate')}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}

export default RolesList;
