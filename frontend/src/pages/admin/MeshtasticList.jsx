import { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { useQuery, useMutation, useQueryClient, keepPreviousData } from '@tanstack/react-query';
import { meshtasticAPI } from '../../services/api';
import { useNotification } from '../../contexts/NotificationContext';
import { useAuth } from '../../contexts/AuthContext';
import Pagination from '../../components/Pagination';
import '../../components/AdminTable.css';

function MeshtasticList() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showError, showSuccess, confirm } = useNotification();
  const { hasRole } = useAuth();
  const canEdit = hasRole('meshtastic_admin') || hasRole('administrator');
  const [showModal, setShowModal] = useState(false);
  const [editing, setEditing] = useState(null);
  const [formData, setFormData] = useState({ name: '', url: '', description: '' });
  const [error, setError] = useState('');
  const [syncing, setSyncing] = useState(false);
  const [page, setPage] = useState(1);
  const [perPage, setPerPage] = useState(20);
  const [search, setSearch] = useState('');
  const [debouncedSearch, setDebouncedSearch] = useState('');

  // Debounce search input
  useEffect(() => {
    const timer = setTimeout(() => {
      setDebouncedSearch(search);
      setPage(1); // Reset to page 1 when search changes
    }, 500); // Wait 500ms after user stops typing

    return () => clearTimeout(timer);
  }, [search]);

  const { data: configsData, isLoading, error: configsError } = useQuery({
    queryKey: ['meshtasticAdmin', page, perPage, debouncedSearch],
    queryFn: async () => {
      // perPage 0 = all: omit paging so the API returns every channel
      const params = perPage ? { page, per_page: perPage, search: debouncedSearch } : { search: debouncedSearch };
      const response = await meshtasticAPI.getAllAdmin(params);
      return response.data;
    },
    // Keep showing the current rows while the next page/search loads, so the search box keeps focus
    placeholderData: keepPreviousData,
  });

  const updateMutation = useMutation({
    mutationFn: ({ id, data }) => meshtasticAPI.update(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries(['meshtasticAdmin']);
      setShowModal(false);
      resetForm();
    },
    onError: (err) => setError(err.response?.data?.error || t('admin.meshtastic.failedUpdate')),
  });

  const deleteMutation = useMutation({
    mutationFn: (id) => meshtasticAPI.delete(id),
    onSuccess: () => queryClient.invalidateQueries(['meshtasticAdmin']),
    onError: (err) => showError(err.response?.data?.error || t('admin.meshtastic.failedDelete')),
  });

  const syncToOtsMutation = useMutation({
    mutationFn: (id) => meshtasticAPI.syncToOts(id),
    onSuccess: (response) => {
      queryClient.invalidateQueries(['meshtasticAdmin']);
      if (response.data.warning) {
        showError(response.data.warning);
      } else {
        showSuccess(t('admin.meshtastic.pushSuccess'));
      }
    },
    onError: (err) => showError(err.response?.data?.error || t('admin.meshtastic.failedPush')),
  });

  const handleSyncFromOts = async () => {
    setSyncing(true);
    try {
      const response = await meshtasticAPI.syncFromOts();
      const { created, updated, errors } = response.data;
      queryClient.invalidateQueries(['meshtasticAdmin']);
      if (errors?.length) {
        showError(t('admin.meshtastic.syncIssues', { errors: errors.join(', ') }));
      } else {
        showSuccess(t('admin.meshtastic.syncComplete', { created, updated }));
      }
    } catch (err) {
      showError(err.response?.data?.error || t('admin.meshtastic.failedSync'));
    } finally {
      setSyncing(false);
    }
  };

  const resetForm = () => {
    setFormData({ name: '', url: '', description: '' });
    setEditing(null);
    setError('');
  };

  const handleSubmit = (e) => {
    e.preventDefault();
    if (editing) {
      updateMutation.mutate({ id: editing.id, data: formData });
    }
  };

  if (isLoading) return <div className="admin-page"><div className="loading-state">{t('common.loading')}</div></div>;

  // Show API errors if any
  if (configsError) {
    return (
      <div className="admin-page">
        <div className="alert alert-error">
          {t('admin.meshtastic.failedLoad')}: {configsError.response?.data?.error || configsError.message}
        </div>
      </div>
    );
  }

  const configs = configsData?.configs || [];
  const total = configsData?.total || 0;

  return (
    <div className="admin-page">
      <div className="admin-header">
        <div>
          <h1>{t('admin.meshtastic.title')}</h1>
          <p style={{ color: '#666', fontSize: '0.9rem', marginTop: '0.25rem' }}>
            {t('admin.meshtastic.subtitle')} <a href="/admin/meshtastic/groups">{t('admin.meshtastic.manageGroups')}</a>
          </p>
        </div>
        <div className="admin-actions">
          <input
            type="text"
            className="search-box"
            placeholder={t('admin.meshtastic.searchPlaceholder')}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          {canEdit && (
            <button className="btn btn-primary" onClick={handleSyncFromOts} disabled={syncing}>
              {syncing ? t('common.syncing') : t('admin.meshtastic.syncFromOts')}
            </button>
          )}
        </div>
      </div>
      <div className="admin-table-container">
        {configs.length === 0 ? (
          <div className="empty-state">
            {debouncedSearch ? t('common.noResults') : t('admin.meshtastic.noChannels')}
          </div>
        ) : (
          <table className="admin-table">
            <thead>
              <tr>
                <th>{t('common.name')}</th>
                <th>{t('common.description')}</th>
                <th>{t('admin.meshtastic.otsSync')}</th>
                <th>{t('common.actions')}</th>
              </tr>
            </thead>
            <tbody>
              {configs.map(config => (
                <tr key={config.id}>
                  <td><strong>{config.name}</strong></td>
                  <td>{config.description || '-'}</td>
                  <td>
                    {config.synced_at ? (
                      <span className="badge badge-success" title={t('admin.meshtastic.lastSynced', { date: new Date(config.synced_at).toLocaleString() })}>
                        {t('admin.meshtastic.otsSynced')}
                      </span>
                    ) : config.url ? (
                      <span className="badge badge-warning">{t('admin.meshtastic.otsNotSynced')}</span>
                    ) : (
                      <span className="badge badge-secondary">{t('admin.meshtastic.noUrl')}</span>
                    )}
                  </td>
                  <td>
                    <div className="table-actions">
                      {canEdit && (
                        <button className="btn btn-sm btn-secondary" onClick={async () => {
                          // Fetch full config details
                          try {
                            const response = await meshtasticAPI.getById(config.id);
                            const fullConfig = response.data;
                            setEditing(fullConfig);
                            setFormData({
                              name: fullConfig.name,
                              url: fullConfig.url || '',
                              description: fullConfig.description || ''
                            });
                            setShowModal(true);
                          } catch (err) {
                            showError(t('admin.meshtastic.failedLoadDetails') + ': ' + (err.response?.data?.error || err.message));
                          }
                        }}>{t('common.edit')}</button>
                      )}
                      {canEdit && !config.synced_at && config.url && (
                        <button
                          className="btn btn-sm btn-info"
                          onClick={() => syncToOtsMutation.mutate(config.id)}
                          disabled={syncToOtsMutation.isPending}
                        >
                          {t('admin.meshtastic.pushToOts')}
                        </button>
                      )}
                      {canEdit && (
                        <button className="btn btn-sm btn-danger" onClick={async () => {
                          const confirmed = await confirm(t('admin.meshtastic.deleteConfirm', { name: config.name }), t('admin.meshtastic.deleteConfig'));
                          if (confirmed) deleteMutation.mutate(config.id);
                        }}>{t('common.delete')}</button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {total > 0 && (
          <Pagination
            page={page}
            perPage={perPage}
            total={total}
            onPageChange={setPage}
            onPerPageChange={(size) => { setPerPage(size); setPage(1); }}
          />
        )}
      </div>

      {showModal && (
        <div className="modal-overlay">
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>{t('admin.meshtastic.editChannel')}</h2>
              <button className="modal-close" onClick={() => setShowModal(false)}>×</button>
            </div>
            <form onSubmit={handleSubmit}>
              <div className="modal-body">
                {error && <div className="alert alert-error">{error}</div>}
                <div className="form-group">
                  <label>{t('admin.meshtastic.channelUrl')}</label>
                  <input
                    type="text"
                    value={formData.url}
                    disabled={true}
                  />
                  <span className="help-text">{t('admin.meshtastic.channelUrlHelp')}</span>
                </div>
                <div className="form-group">
                  <label>{t('admin.meshtastic.nameOptional')}</label>
                  <input
                    type="text"
                    value={formData.name}
                    onChange={(e) => setFormData({...formData, name: e.target.value})}
                    disabled={editing?.synced_at}
                    placeholder={t('admin.meshtastic.namePlaceholder')}
                  />
                  {editing?.synced_at && <span className="help-text">{t('admin.meshtastic.nameHelpSynced')}</span>}
                </div>
                <div className="form-group">
                  <label>{t('admin.meshtastic.descriptionLabel')}</label>
                  <textarea value={formData.description} onChange={(e) => setFormData({...formData, description: e.target.value})} />
                </div>
              </div>
              <div className="modal-footer">
                <button type="button" className="btn btn-secondary" onClick={() => setShowModal(false)}>{t('common.cancel')}</button>
                <button type="submit" className="btn btn-primary" disabled={updateMutation.isPending}>{t('common.update')}</button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}

export default MeshtasticList;
