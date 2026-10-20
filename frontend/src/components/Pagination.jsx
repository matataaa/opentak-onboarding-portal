import { useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import './AdminTable.css';

// 0 means "all": the list is requested without per_page
const PAGE_SIZES = [10, 20, 50, 0];

const Pagination = ({ page, perPage, total, onPageChange, onPerPageChange }) => {
  const { t } = useTranslation();
  const totalPages = perPage ? Math.max(1, Math.ceil(total / perPage)) : 1;

  // Step back when the current page no longer exists, e.g. after deleting its last item
  useEffect(() => {
    if (page > totalPages) onPageChange(totalPages);
  }, [page, totalPages, onPageChange]);

  return (
    <div className="pagination">
      <label className="pagination-info">
        {t('common.perPage')}{' '}
        <select value={perPage} onChange={(e) => onPerPageChange(Number(e.target.value))}>
          {PAGE_SIZES.map(size => (
            <option key={size} value={size}>{size || t('common.all')}</option>
          ))}
        </select>
      </label>
      {totalPages > 1 && (
        <>
          <button
            className="btn btn-secondary btn-sm"
            disabled={page === 1}
            onClick={() => onPageChange(page - 1)}
          >
            {t('common.previous')}
          </button>
          <span className="pagination-info">
            {t('common.pageInfo', { page, totalPages, total })}
          </span>
          <button
            className="btn btn-secondary btn-sm"
            disabled={page >= totalPages}
            onClick={() => onPageChange(page + 1)}
          >
            {t('common.next')}
          </button>
        </>
      )}
    </div>
  );
};

export default Pagination;
