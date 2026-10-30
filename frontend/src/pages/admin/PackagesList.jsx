import { useTranslation } from 'react-i18next';

function PackagesList() {
  const { t } = useTranslation();

  return (
    <div>
      <h1>{t('admin.packages.title')}</h1>
      <p>{t('admin.packages.comingSoon')}</p>
    </div>
  );
}

export default PackagesList;
