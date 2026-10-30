import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { settingsAPI } from '../services/api';
import { useAuth } from '../contexts/AuthContext';
import LanguageSelector from './LanguageSelector';
import './AuthLayout.css';

const LAYOUTS = ['classic', 'centered', 'split'];
const OVERLAY_ALPHA = { none: 0, light: 0.25, medium: 0.45, strong: 0.65 };
const HERO_FIELDS = ['eyebrow', 'headline', 'description', 'footer'];

function getHeroTexts(heroTexts, language) {
  const texts = heroTexts && typeof heroTexts === 'object' ? heroTexts : {};
  const lang = (language || 'en').split('-')[0];
  const pick = (entry) => {
    const result = {};
    HERO_FIELDS.forEach((field) => {
      const value = entry?.[field];
      if (typeof value === 'string' && value.trim()) result[field] = value;
    });
    return result;
  };
  const localized = pick(texts[lang]);
  return Object.keys(localized).length > 0 ? localized : pick(texts.en);
}

/**
 * Wrapper for the login and other sign-in pages. The "classic" layout (default)
 * renders exactly the original auth container; the other layouts add the
 * configured background, overlay, glass card, header bar and hero panel.
 */
function AuthLayout({ children, hero = false, languageSelector = true }) {
  const { i18n } = useTranslation();
  const { user } = useAuth();
  // Only for signed-out visitors; it switches the UI language without touching a profile.
  const showLang = languageSelector && !user;

  const { data: settings, isPending } = useQuery({
    queryKey: ['settings'],
    queryFn: async () => {
      const response = await settingsAPI.get();
      return response.data;
    },
  });

  // Until the settings are known, render an empty classic container to avoid a
  // classic -> branded flash. If the request fails, fall back to classic below.
  if (isPending) {
    return <div className="auth-container" />;
  }

  const layout = LAYOUTS.includes(settings?.login_layout) ? settings.login_layout : 'classic';

  if (layout === 'classic') {
    return (
      <>
        {showLang && <LanguageSelector className="auth-lang-select auth-lang-select--floating" />}
        <div className="auth-container">{children}</div>
      </>
    );
  }

  const brandName = settings?.brand_name || 'OpenTAK Portal';
  const logoEnabled = settings?.custom_logo_enabled === true || settings?.custom_logo_enabled === 'true';
  const logoPath = logoEnabled && settings?.custom_logo_path
    ? settings.custom_logo_path
    : settings?.default_logo_path;
  const displayMode = settings?.logo_display_mode || 'logo_and_text';
  const showLogo = logoPath && (displayMode === 'logo_only' || displayMode === 'logo_and_text');
  const showText = displayMode === 'text_only' || displayMode === 'logo_and_text';

  const overlayAlpha = OVERLAY_ALPHA[settings?.login_overlay_strength] ?? OVERLAY_ALPHA.medium;
  const glass = settings?.login_glass_enabled !== false;
  const headerBar = settings?.login_header_bar_enabled === true;
  const background = settings?.login_background_path;

  const heroTexts = hero && layout === 'split'
    ? getHeroTexts(settings?.login_hero_texts, i18n.resolvedLanguage || i18n.language)
    : {};
  const showHero = Object.keys(heroTexts).length > 0;

  const classNames = [
    'auth-layout',
    'auth-layout--branded',
    showHero ? 'auth-layout--split' : 'auth-layout--centered',
    glass ? 'auth-layout--glass' : '',
  ].filter(Boolean).join(' ');

  const style = {
    '--accent-color': settings?.accent_color || '#ff9800',
    '--auth-overlay-alpha': overlayAlpha,
  };
  if (background) {
    style['--auth-bg-image'] = `url("${background}")`;
  }

  return (
    <div className={classNames} style={style}>
      {headerBar && (
        <header className="auth-layout__header">
          {showLogo && <img src={logoPath} alt={showText ? '' : brandName} />}
          {showText && <span>{brandName}</span>}
          {showLang && <LanguageSelector className="auth-lang-select auth-layout__header-lang" />}
        </header>
      )}
      {!headerBar && showLang && (
        <LanguageSelector className="auth-lang-select auth-lang-select--floating" />
      )}
      <div className="auth-layout__body">
        {showHero ? (
          <div className="auth-layout__split">
            <section className="auth-layout__hero">
              {heroTexts.eyebrow && <p className="auth-layout__hero-eyebrow">{heroTexts.eyebrow}</p>}
              {heroTexts.headline && <h1 className="auth-layout__hero-headline">{heroTexts.headline}</h1>}
              {heroTexts.description && <p className="auth-layout__hero-description">{heroTexts.description}</p>}
              {heroTexts.footer && <p className="auth-layout__hero-footer">{heroTexts.footer}</p>}
            </section>
            <div className="auth-layout__main">{children}</div>
          </div>
        ) : (
          <div className="auth-layout__main">{children}</div>
        )}
      </div>
    </div>
  );
}

export default AuthLayout;
