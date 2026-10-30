import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';
import LanguageDetector from 'i18next-browser-languagedetector';

import en from './locales/en.json';
import nl from './locales/nl.json';
import de from './locales/de.json';

const resources = {
  en: { translation: en },
  nl: { translation: nl },
  de: { translation: de }
};

i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    resources,
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

// Separate instance pinned to one language (kiosk screen): no detector, so it never
// touches the visitor's stored language, and not registered as the global instance
export const createFixedLanguageI18n = (lng) => {
  const instance = i18n.createInstance();
  instance.init({
    resources,
    lng,
    fallbackLng: 'en',
    interpolation: {
      escapeValue: false
    },
    initAsync: false
  });
  return instance;
};

export default i18n;
