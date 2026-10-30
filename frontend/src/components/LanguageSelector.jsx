import { useTranslation } from 'react-i18next';

const LANGUAGES = [['en', 'English'], ['nl', 'Nederlands'], ['de', 'Deutsch']];

/**
 * Language dropdown. Only switches the UI language; callers that want to
 * persist the choice (e.g. to the user's profile) pass onChange.
 */
function LanguageSelector({ onChange, className, id }) {
  const { t, i18n } = useTranslation();

  return (
    <select
      id={id}
      className={className}
      aria-label={t('common.language')}
      value={i18n.resolvedLanguage}
      onChange={(e) => {
        const lang = e.target.value;
        i18n.changeLanguage(lang);
        onChange?.(lang);
      }}
    >
      {LANGUAGES.map(([code, name]) => (
        <option key={code} value={code} lang={code}>{name}</option>
      ))}
    </select>
  );
}

export default LanguageSelector;
