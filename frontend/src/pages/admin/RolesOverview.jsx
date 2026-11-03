import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { rolesAPI } from '../../services/api';
import { LEVELS, moduleLabel, moduleLevel } from '../../utils/permissions';
import '../../components/AdminTable.css';
import './Roles.css';

function LevelPill({ level }) {
  const { t } = useTranslation();
  return (
    <span className={`level-pill level-${level}`}>
      <span className="level-dot" aria-hidden="true" />
      {t(`admin.roles.level.${level}`)}
    </span>
  );
}

function RolesOverview() {
  const { t } = useTranslation();

  const { data: rolesData, isLoading: rolesLoading, error } = useQuery({
    queryKey: ['roles', 'list', ''],
    queryFn: async () => (await rolesAPI.getAll({ search: '' })).data,
  });
  const { data: catalogData, isLoading: catalogLoading } = useQuery({
    queryKey: ['permissionCatalog'],
    queryFn: async () => (await rolesAPI.getPermissionCatalog()).data,
  });

  // Administrator first, then the custom roles, then the other system roles
  const roles = useMemo(() => {
    const all = rolesData?.roles || [];
    const rank = (r) => (r.name === 'administrator' ? 0 : r.isCustom ? 1 : 2);
    return [...all].sort((a, b) => rank(a) - rank(b));
  }, [rolesData]);
  const modules = catalogData?.modules || [];

  if (rolesLoading || catalogLoading) {
    return <div className="admin-page"><div className="loading-state">{t('common.loading')}</div></div>;
  }
  if (error) {
    return <div className="admin-page"><div className="alert alert-error">{error.response?.data?.error || t('admin.roles.failedLoad')}</div></div>;
  }

  const held = new Map(roles.map((r) => [r.id, new Set(r.permissions || [])]));

  return (
    <div className="admin-page roles-page">
      <div className="roles-crumb">
        <Link to="/admin/roles">{t('admin.roles.title')}</Link> / {t('admin.roles.overviewTitle')}
      </div>
      <div className="admin-header">
        <h1>{t('admin.roles.overviewTitle')}</h1>
      </div>
      <div className="roles-legend">
        {LEVELS.map((level) => (
          <span key={level}><LevelPill level={level} /> {t(`admin.roles.levelHelp.${level}`)}</span>
        ))}
      </div>
      <div className="roles-card">
        <div className="roles-matrix-scroll">
          <table className="roles-matrix">
            <thead>
              <tr>
                <th scope="col">{t('admin.roles.module')}</th>
                {roles.map((r) => (
                  <th key={r.id} scope="col">
                    <Link to={`/admin/roles/${r.id}`}>{r.displayName || r.name}</Link>
                    <span className="roles-matrix-count">{t('admin.roles.userCountShort', { count: r.userCount || 0 })}</span>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {modules.map((m) => (
                <tr key={m.module}>
                  <th scope="row">
                    {moduleLabel(t, m.module)}
                    {!m.enabled && <span className="roles-matrix-off">{t('admin.roles.moduleOffTag')}</span>}
                  </th>
                  {roles.map((r) => (
                    <td key={r.id}><LevelPill level={moduleLevel(m, held.get(r.id))} /></td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

export default RolesOverview;
