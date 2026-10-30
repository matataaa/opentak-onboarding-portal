import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';
import LanguageDetector from 'i18next-browser-languagedetector';

import en from './locales/en.json';
import nl from './locales/nl.json';
import de from './locales/de.json';

i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    resources: {
      en: { translation: en },
      nl: { translation: nl },
      de: { translation: de }
    },
    fallbackLng: 'en',
    interpolation: {
      escapeValue: false
    },
    detection: {
      order: ['localStorage', 'navigator'],
      caches: ['localStorage']
    }
  });

// Keep <html lang> in step with the UI language (screen readers, hyphenation)
const setDocumentLang = () => {
  document.documentElement.lang = i18n.resolvedLanguage || 'en';
};
setDocumentLang();
i18n.on('languageChanged', setDocumentLang);

export default i18n;
