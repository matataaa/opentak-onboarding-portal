import { useState, useEffect, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { settingsAPI, oidcAPI, rolesAPI } from '../../services/api';
import { useAuth } from '../../contexts/AuthContext';
import './Admin.css';
import '../../components/AdminTable.css';

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || window.location.origin;

// UI grouping of the settings page. This is presentation only: the DB `category`
// of each setting is intentionally left unchanged. A setting is shown in
// SETTING_GROUP_OVERRIDES[key] if mapped, otherwise in the group matching its DB
// category (or General if no such group exists).
const GROUPS = [
  { id: 'branding', titleKey: 'admin.settings.branding' },
  { id: 'notifications', titleKey: 'admin.settings.notifications' },
  { id: 'email', titleKey: 'admin.settings.email' },
  { id: 'registration', titleKey: 'admin.settings.registration' },
  { id: 'security', titleKey: 'admin.settings.security' },
  { id: 'qr_enrollment', titleKey: 'admin.settings.qrCodeEnrollment' },
  { id: 'homepage_tiles', titleKey: 'admin.settings.homepageTiles' },
  { id: 'help_support', titleKey: 'admin.settings.helpSupport' },
  { id: 'radios', titleKey: 'admin.settings.radiosSection' },
  { id: 'kiosk', titleKey: 'admin.settings.kioskEnrollment' },
  { id: 'advanced', titleKey: 'admin.settings.advanced' },
  { id: 'general', titleKey: 'admin.settings.general' },
];

const SETTING_GROUP_OVERRIDES = {
  brand_name_enabled: 'branding',
  brand_name_value: 'branding',
  default_theme: 'branding',
  date_format: 'branding',
  atak_homepage_icon_enabled: 'homepage_tiles',
  itak_homepage_icon_enabled: 'homepage_tiles',
  truststore_homepage_icon_enabled: 'homepage_tiles',
  meshtastic_homepage_icon_enabled: 'homepage_tiles',
  open_in_atak_enabled: 'homepage_tiles',
  atak_installer_qr_enabled: 'homepage_tiles',
  atak_installer_qr_url: 'homepage_tiles',
  itak_installer_qr_enabled: 'homepage_tiles',
  itak_installer_qr_url: 'homepage_tiles',
  meshtastic_installer_qr_android_enabled: 'homepage_tiles',
  meshtastic_installer_qr_android_url: 'homepage_tiles',
  meshtastic_installer_qr_iphone_enabled: 'homepage_tiles',
  meshtastic_installer_qr_iphone_url: 'homepage_tiles',
  help_link_enabled: 'help_support',
  help_link_value: 'help_support',
  help_email_enabled: 'help_support',
  help_email_value: 'help_support',
  kiosk_default_theme: 'kiosk',
  forgot_password_enabled: 'advanced',
  events_enabled: 'advanced',
};

// Branding settings rendered in the "Login page" subsection
const LOGIN_PAGE_SETTING_KEYS = [
  'login_layout',
  'login_overlay_strength',
  'login_background_path',
  'login_glass_enabled',
  'login_header_bar_enabled',
  'login_hero_texts',
];

// Fallback label for settings without a translation (e.g. newly added settings)
const formatSettingName = (key) => {
  return key
    .split('_')
    .map(word => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ');
};

// Translated label/description of a setting row, falling back to the key name / DB description
const settingLabel = (t, key) => t(`admin.settings.items.${key}.label`, { defaultValue: formatSettingName(key) });
const settingDescription = (t, s) => t(`admin.settings.items.${s.key}.description`, { defaultValue: s.description || '' });

// Collapsible settings group (heading button + region, content only rendered when open)
function SettingsGroup({ id, title, count, isOpen, onToggle, children }) {
  const { t } = useTranslation();
  const buttonId = `settings-group-${id}-toggle`;
  const panelId = `settings-group-${id}-panel`;

  return (
    <div className="settings-section settings-group">
      <h2 className="settings-group-heading">
        <button
          type="button"
          id={buttonId}
          className="settings-group-toggle"
          aria-expanded={isOpen}
          aria-controls={isOpen ? panelId : undefined}
          onClick={onToggle}
        >
          <span className="settings-group-title">{title}</span>
          <span className="settings-group-count">{t('admin.settings.settingCount', { count })}</span>
          <span className="settings-group-chevron" aria-hidden="true">&#9656;</span>
        </button>
      </h2>
      {isOpen && (
        <div id={panelId} className="settings-group-panel">
          {children}
        </div>
      )}
    </div>
  );
}

// Component for numeric settings (number input with save button)
function NumericSettingItem({ setting, onSave, saving, canEdit = true }) {
  const { t } = useTranslation();
  const [localValue, setLocalValue] = useState(setting?.value || '');
  const [isEditing, setIsEditing] = useState(false);

  useEffect(() => {
    setLocalValue(setting?.value || '');
    setIsEditing(false);
  }, [setting?.value]);

  const handleSave = () => {
    onSave(setting.id, localValue);
    setIsEditing(false);
  };

  const handleCancel = () => {
    setLocalValue(setting?.value || '');
    setIsEditing(false);
  };

  return (
    <div className="setting-item setting-item-numeric">
      <div className="setting-info">
        <h4>{settingLabel(t, setting.key)}</h4>
        <p className="setting-description">{settingDescription(t, setting)}</p>
      </div>
      <div className="setting-numeric-input">
        <input
          type="number"
          min="1"
          value={localValue}
          onChange={(e) => {
            setLocalValue(e.target.value);
            setIsEditing(true);
          }}
          disabled={saving || !canEdit}
        />
        {isEditing && (
          <div className="setting-text-actions">
            <button
              className="btn-save-setting"
              onClick={handleSave}
              disabled={saving}
            >
              {t('common.save')}
            </button>
            <button
              className="btn-cancel-setting"
              onClick={handleCancel}
              disabled={saving}
            >
              {t('common.cancel')}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

// Component for paired settings (toggle + text value)
function PairedSettingItem({ enabledSetting, valueSetting, onToggle, onSave, saving, canEdit = true }) {
  const { t } = useTranslation();
  const [localValue, setLocalValue] = useState(valueSetting?.value || '');
  const [isEditing, setIsEditing] = useState(false);
  const isEnabled = enabledSetting?.value === 'true';

  useEffect(() => {
    setLocalValue(valueSetting?.value || '');
    setIsEditing(false);
  }, [valueSetting?.value]);

  const handleSave = () => {
    onSave(valueSetting.id, localValue);
    setIsEditing(false);
  };

  const handleCancel = () => {
    setLocalValue(valueSetting?.value || '');
    setIsEditing(false);
  };

  return (
    <div className="setting-item setting-item-paired">
      <div className="setting-header">
        <div className="setting-info">
          <h4>{settingLabel(t, enabledSetting.key)}</h4>
          <p className="setting-description">{settingDescription(t, enabledSetting)}</p>
        </div>
        <label className="toggle-switch">
          <input
            type="checkbox"
            checked={isEnabled}
            onChange={() => onToggle(enabledSetting.id, enabledSetting.value)}
            disabled={saving || !canEdit}
          />
          <span className="toggle-slider"></span>
        </label>
      </div>
      {valueSetting && (
        <div className="setting-value-section">
          <p className="setting-value-description">{settingDescription(t, valueSetting)}</p>
          <div className="setting-text-input">
            <input
              type="text"
              value={localValue}
              onChange={(e) => {
                setLocalValue(e.target.value);
                setIsEditing(true);
              }}
              placeholder={t('admin.settings.enterValue')}
              disabled={saving || !canEdit}
            />
            {isEditing && (
              <div className="setting-text-actions">
                <button
                  className="btn-save-setting"
                  onClick={handleSave}
                  disabled={saving}
                >
                  {t('common.save')}
                </button>
                <button
                  className="btn-cancel-setting"
                  onClick={handleCancel}
                  disabled={saving}
                >
                  {t('common.cancel')}
                </button>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// Color name to hex mapping
const colorNameToHex = {
  black: '#000000',
  white: '#ffffff',
  red: '#ff0000',
  green: '#008000',
  blue: '#0000ff',
  yellow: '#ffff00',
  orange: '#ff9800',
  purple: '#800080',
  pink: '#ffc0cb',
  gray: '#808080',
  grey: '#808080',
  brown: '#a52a2a',
  cyan: '#00ffff',
  magenta: '#ff00ff',
  lime: '#00ff00',
  navy: '#000080',
  teal: '#008080',
  maroon: '#800000',
  olive: '#808000',
  silver: '#c0c0c0',
};

// Convert color value to hex for the color picker
const toHexColor = (value) => {
  if (!value) return '#000000';
  // Already a hex value
  if (value.startsWith('#')) return value;
  // Check color name mapping
  const hex = colorNameToHex[value.toLowerCase()];
  if (hex) return hex;
  // Default fallback
  return '#000000';
};

// Component for color settings
function ColorSettingItem({ setting, onSave, saving, canEdit = true }) {
  const { t } = useTranslation();
  const [localValue, setLocalValue] = useState(setting?.value || '#000000');
  const [isEditing, setIsEditing] = useState(false);

  useEffect(() => {
    setLocalValue(setting?.value || '#000000');
    setIsEditing(false);
  }, [setting?.value]);

  const handleSave = () => {
    onSave(setting.id, localValue);
    setIsEditing(false);
  };

  const handleCancel = () => {
    setLocalValue(setting?.value || '#000000');
    setIsEditing(false);
  };

  // When color picker changes, update text to hex value
  const handleColorPickerChange = (e) => {
    setLocalValue(e.target.value);
    setIsEditing(true);
  };

  return (
    <div className="setting-item setting-item-color">
      <div className="setting-info">
        <h4>{settingLabel(t, setting.key)}</h4>
        <p className="setting-description">{settingDescription(t, setting)}</p>
      </div>
      <div className="setting-color-input">
        <input
          type="color"
          value={toHexColor(localValue)}
          onChange={handleColorPickerChange}
          disabled={saving || !canEdit}
          className="color-picker"
        />
        <input
          type="text"
          value={localValue}
          onChange={(e) => {
            setLocalValue(e.target.value);
            setIsEditing(true);
          }}
          placeholder="#ff9800"
          disabled={saving || !canEdit}
          className="color-text"
        />
        {isEditing && (
          <div className="setting-text-actions">
            <button
              className="btn-save-setting"
              onClick={handleSave}
              disabled={saving}
            >
              {t('common.save')}
            </button>
            <button
              className="btn-cancel-setting"
              onClick={handleCancel}
              disabled={saving}
            >
              {t('common.cancel')}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

// Component for dropdown settings (e.g. theme selection)
function DropdownSettingItem({ setting, options, onSave, saving, canEdit = true }) {
  const { t } = useTranslation();
  const [localValue, setLocalValue] = useState(setting?.value || '');
  const [isEditing, setIsEditing] = useState(false);

  useEffect(() => {
    setLocalValue(setting?.value || '');
    setIsEditing(false);
  }, [setting?.value]);

  const handleChange = (e) => {
    setLocalValue(e.target.value);
    setIsEditing(true);
  };

  const handleSave = () => {
    onSave(setting.id, localValue);
    setIsEditing(false);
  };

  const handleCancel = () => {
    setLocalValue(setting?.value || '');
    setIsEditing(false);
  };

  return (
    <div className="setting-item setting-item-color">
      <div className="setting-info">
        <h4>{settingLabel(t, setting.key)}</h4>
        <p className="setting-description">{settingDescription(t, setting)}</p>
      </div>
      <div className="setting-color-input">
        <select
          value={localValue}
          onChange={handleChange}
          disabled={saving || !canEdit}
          style={{ padding: '0.5rem 0.75rem', borderRadius: '4px', fontSize: '0.95rem' }}
        >
          {options.map(opt => (
            <option key={opt.value} value={opt.value}>{opt.label}</option>
          ))}
        </select>
        {isEditing && (
          <div className="setting-text-actions">
            <button
              className="btn-save-setting"
              onClick={handleSave}
              disabled={saving}
            >
              {t('common.save')}
            </button>
            <button
              className="btn-cancel-setting"
              onClick={handleCancel}
              disabled={saving}
            >
              {t('common.cancel')}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

// Component for logo upload
function LogoUploadSetting({ logoSettings, onUpload, onDelete, onDisplayModeChange, saving, canEdit = true }) {
  const { t } = useTranslation();
  const [preview, setPreview] = useState(null);
  const [dragOver, setDragOver] = useState(false);
  const fileInputRef = useRef(null);

  const handleFileSelect = (file) => {
    if (!file) return;

    // Validate file type
    const allowedTypes = ['image/png', 'image/jpeg', 'image/jpg', 'image/gif'];
    if (!allowedTypes.includes(file.type)) {
      alert(t('admin.settings.invalidFileType'));
      return;
    }

    // Validate file size (2MB)
    if (file.size > 2 * 1024 * 1024) {
      alert(t('admin.settings.fileTooLarge'));
      return;
    }

    // Show preview
    const reader = new FileReader();
    reader.onload = (e) => setPreview(e.target.result);
    reader.readAsDataURL(file);

    // Upload
    onUpload(file);
  };

  const handleDrop = (e) => {
    e.preventDefault();
    setDragOver(false);
    const file = e.dataTransfer.files[0];
    handleFileSelect(file);
  };

  const currentLogo = logoSettings?.custom_logo_enabled && logoSettings?.custom_logo_path
    ? `${API_BASE_URL}${logoSettings.custom_logo_path}`
    : logoSettings?.default_logo_path
      ? `${API_BASE_URL}${logoSettings.default_logo_path}`
      : null;

  return (
    <div className="setting-item setting-item-logo">
      <div className="setting-header">
        <div className="setting-info">
          <h4>{t('admin.settings.portalLogo')}</h4>
          <p className="setting-description">{t('admin.settings.portalLogoHelp')}</p>
        </div>
      </div>

      <div className="logo-upload-section">
        {/* Current Logo Preview */}
        {(preview || currentLogo) && (
          <div className="logo-preview-container">
            <img
              src={preview || currentLogo}
              alt={t('admin.settings.currentLogo')}
              className="logo-preview"
            />
          </div>
        )}

        {/* Upload Zone */}
        {canEdit && (
          <div
            className={`logo-dropzone ${dragOver ? 'drag-over' : ''}`}
            onDrop={handleDrop}
            onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
            onDragLeave={() => setDragOver(false)}
            onClick={() => fileInputRef.current?.click()}
          >
            <input
              type="file"
              ref={fileInputRef}
              accept=".png,.jpg,.jpeg,.gif"
              onChange={(e) => handleFileSelect(e.target.files[0])}
              style={{ display: 'none' }}
            />
            <p>{t('admin.settings.dropImage')}</p>
          </div>
        )}

        {/* Display Mode */}
        <div className="logo-display-mode">
          <label>{t('admin.settings.displayMode')}</label>
          <select
            value={logoSettings?.logo_display_mode || 'logo_and_text'}
            onChange={(e) => onDisplayModeChange(e.target.value)}
            disabled={saving || !canEdit}
          >
            <option value="logo_only">{t('admin.settings.logoOnly')}</option>
            <option value="text_only">{t('admin.settings.textOnly')}</option>
            <option value="logo_and_text">{t('admin.settings.logoAndText')}</option>
          </select>
        </div>

        {/* Actions */}
        {logoSettings?.custom_logo_enabled && (
          <button
            className="btn-cancel-setting"
            onClick={onDelete}
            disabled={saving || !canEdit}
          >
            {t('admin.settings.resetToDefault')}
          </button>
        )}
      </div>
    </div>
  );
}

// Component for the login page background image upload
function LoginBackgroundSetting({ backgroundPath, onUpload, onDelete, saving, canEdit = true }) {
  const { t } = useTranslation();
  const [dragOver, setDragOver] = useState(false);
  const fileInputRef = useRef(null);

  const handleFileSelect = (file) => {
    if (!file) return;

    if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type)) {
      alert(t('admin.settings.loginBackgroundInvalidType'));
      return;
    }

    if (file.size > 5 * 1024 * 1024) {
      alert(t('admin.settings.loginBackgroundTooLarge'));
      return;
    }

    onUpload(file);
  };

  const handleDrop = (e) => {
    e.preventDefault();
    setDragOver(false);
    handleFileSelect(e.dataTransfer.files[0]);
  };

  return (
    <div className="setting-item setting-item-logo">
      <div className="setting-header">
        <div className="setting-info">
          <h4>{t('admin.settings.loginBackground')}</h4>
          <p className="setting-description">{t('admin.settings.loginBackgroundHelp')}</p>
        </div>
      </div>

      <div className="logo-upload-section">
        {backgroundPath && (
          <div className="logo-preview-container">
            <img
              src={`${API_BASE_URL}${backgroundPath}`}
              alt={t('admin.settings.currentLoginBackground')}
              className="login-bg-preview"
            />
          </div>
        )}

        {canEdit && (
          <div
            className={`logo-dropzone ${dragOver ? 'drag-over' : ''}`}
            onDrop={handleDrop}
            onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
            onDragLeave={() => setDragOver(false)}
            onClick={() => fileInputRef.current?.click()}
          >
            <input
              type="file"
              ref={fileInputRef}
              accept=".png,.jpg,.jpeg,.webp"
              onChange={(e) => { handleFileSelect(e.target.files[0]); e.target.value = ''; }}
              style={{ display: 'none' }}
            />
            <p>{t('admin.settings.dropImage')}</p>
          </div>
        )}

        {backgroundPath && (
          <button
            className="btn-cancel-setting"
            onClick={onDelete}
            disabled={saving || !canEdit}
          >
            {t('admin.settings.removeLoginBackground')}
          </button>
        )}
      </div>
    </div>
  );
}

const HERO_LANGUAGES = ['en', 'nl', 'de'];
const HERO_FIELDS = ['eyebrow', 'headline', 'description', 'footer'];

const parseHeroTexts = (value) => {
  try {
    const parsed = JSON.parse(value || '{}');
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
  } catch {
    return {};
  }
};

// Editor for the per-language hero texts of the split login layout (stored as JSON)
function HeroTextsSetting({ setting, onSave, saving, canEdit = true }) {
  const { t } = useTranslation();
  const [language, setLanguage] = useState('en');
  const [texts, setTexts] = useState(() => parseHeroTexts(setting?.value));
  const [isEditing, setIsEditing] = useState(false);

  useEffect(() => {
    setTexts(parseHeroTexts(setting?.value));
    setIsEditing(false);
  }, [setting?.value]);

  const handleChange = (field, value) => {
    setTexts(prev => ({ ...prev, [language]: { ...prev[language], [field]: value } }));
    setIsEditing(true);
  };

  const handleSave = () => {
    // Store only non-empty fields and languages
    const cleaned = {};
    HERO_LANGUAGES.forEach(lang => {
      const entry = {};
      HERO_FIELDS.forEach(field => {
        const value = texts[lang]?.[field];
        if (typeof value === 'string' && value.trim()) entry[field] = value.trim();
      });
      if (Object.keys(entry).length > 0) cleaned[lang] = entry;
    });
    onSave(setting.id, JSON.stringify(cleaned));
    setIsEditing(false);
  };

  const handleCancel = () => {
    setTexts(parseHeroTexts(setting?.value));
    setIsEditing(false);
  };

  return (
    <div className="setting-item setting-item-text">
      <div className="setting-info">
        <h4>{t('admin.settings.heroTexts')}</h4>
        <p className="setting-description">{t('admin.settings.heroTextsHelp')}</p>
      </div>
      <div className="setting-text-input">
        <div className="hero-texts-tabs" role="tablist">
          {HERO_LANGUAGES.map(lang => (
            <button
              key={lang}
              type="button"
              role="tab"
              aria-selected={language === lang}
              className={`hero-texts-tab ${language === lang ? 'active' : ''}`}
              onClick={() => setLanguage(lang)}
            >
              {t(`admin.settings.heroLanguage.${lang}`)}
            </button>
          ))}
        </div>
        {HERO_FIELDS.map(field => {
          const inputId = `hero-${language}-${field}`;
          const value = texts[language]?.[field] || '';
          return (
            <div key={field} className="hero-texts-field">
              <label htmlFor={inputId}>{t(`admin.settings.heroField.${field}`)}</label>
              {field === 'description' ? (
                <textarea
                  id={inputId}
                  value={value}
                  maxLength={300}
                  rows={3}
                  onChange={(e) => handleChange(field, e.target.value)}
                  disabled={saving || !canEdit}
                />
              ) : (
                <input
                  id={inputId}
                  type="text"
                  value={value}
                  maxLength={300}
                  onChange={(e) => handleChange(field, e.target.value)}
                  disabled={saving || !canEdit}
                />
              )}
            </div>
          );
        })}
        {isEditing && (
          <div className="setting-text-actions">
            <button className="btn-save-setting" onClick={handleSave} disabled={saving}>
              {t('common.save')}
            </button>
            <button className="btn-cancel-setting" onClick={handleCancel} disabled={saving}>
              {t('common.cancel')}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

// OIDC Provider Form Modal
function OIDCProviderModal({ provider, roles, onSave, onClose, saving, error: externalError }) {
  const { t } = useTranslation();
  const [formData, setFormData] = useState({
    name: '',
    display_name: '',
    button_color: '#4285F4',
    discovery_url: '',
    client_id: '',
    client_secret: '',
    role_claim: 'roles',
    sync_roles: true,
    enabled: true,
    role_mappings: {},
  });
  const [iconFile, setIconFile] = useState(null);
  const [iconPreview, setIconPreview] = useState(null);
  const [removeIcon, setRemoveIcon] = useState(false);
  const iconInputRef = useRef(null);

  useEffect(() => {
    if (provider) {
      setFormData({
        name: provider.name || '',
        display_name: provider.display_name || '',
        button_color: provider.button_color || '#4285F4',
        discovery_url: provider.discovery_url || '',
        client_id: provider.client_id || '',
        client_secret: '',
        role_claim: provider.role_claim || 'roles',
        sync_roles: provider.sync_roles !== false,
        enabled: provider.enabled !== false,
        role_mappings: provider.role_mappings || {},
      });
      setIconFile(null);
      setIconPreview(null);
      setRemoveIcon(false);
    }
  }, [provider]);

  // Compute the effective icon URL for preview
  const effectiveIconUrl = removeIcon && !iconFile
    ? null
    : iconPreview || (provider?.icon_url ? `${API_BASE_URL}${provider.icon_url}` : null);

  const handleIconFileSelect = (file) => {
    if (!file) return;
    const allowedTypes = ['image/png', 'image/jpeg', 'image/jpg', 'image/gif', 'image/svg+xml'];
    if (!allowedTypes.includes(file.type)) {
      alert(t('admin.settings.invalidFileType'));
      return;
    }
    if (file.size > 2 * 1024 * 1024) {
      alert(t('admin.settings.fileTooLarge'));
      return;
    }
    setIconFile(file);
    setRemoveIcon(false);
    const reader = new FileReader();
    reader.onload = (e) => setIconPreview(e.target.result);
    reader.readAsDataURL(file);
  };

  const handleRemoveIconClick = () => {
    setIconFile(null);
    setIconPreview(null);
    setRemoveIcon(true);
    if (iconInputRef.current) iconInputRef.current.value = '';
  };

  const handleChange = (e) => {
    const { name, value, type, checked } = e.target;
    setFormData(prev => ({
      ...prev,
      [name]: type === 'checkbox' ? checked : value,
    }));
  };

  const handleAddMapping = () => {
    setFormData(prev => ({
      ...prev,
      role_mappings: { ...prev.role_mappings, '': '' },
    }));
  };

  const handleMappingKeyChange = (oldKey, newKey) => {
    setFormData(prev => {
      const mappings = { ...prev.role_mappings };
      const value = mappings[oldKey];
      delete mappings[oldKey];
      mappings[newKey] = value;
      return { ...prev, role_mappings: mappings };
    });
  };

  const handleMappingValueChange = (key, value) => {
    setFormData(prev => ({
      ...prev,
      role_mappings: { ...prev.role_mappings, [key]: value },
    }));
  };

  const handleRemoveMapping = (key) => {
    setFormData(prev => {
      const mappings = { ...prev.role_mappings };
      delete mappings[key];
      return { ...prev, role_mappings: mappings };
    });
  };

  const handleSubmit = (e) => {
    e.preventDefault();
    const data = { ...formData };
    // Don't send empty client_secret on edit (means "keep existing")
    if (provider && !data.client_secret) {
      delete data.client_secret;
    }
    onSave(data, iconFile, removeIcon);
  };

  // Compute text color for button preview
  const getContrastColor = (hex) => {
    if (!hex || !hex.startsWith('#')) return '#ffffff';
    const r = parseInt(hex.slice(1, 3), 16);
    const g = parseInt(hex.slice(3, 5), 16);
    const b = parseInt(hex.slice(5, 7), 16);
    const luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
    return luminance > 0.5 ? '#000000' : '#ffffff';
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal oidc-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2>{provider ? t('admin.settings.editProvider') : t('admin.settings.addOidcProvider')}</h2>
          <button className="modal-close" onClick={onClose} aria-label={t('common.close')}>&times;</button>
        </div>

        <form onSubmit={handleSubmit}>
          <div className="modal-body">
            {externalError && (
              <div className="alert alert-error" style={{ marginBottom: '1rem' }}>
                {externalError}
              </div>
            )}
            <div className="oidc-form-grid">
              <div className="form-group">
                <label>{t('admin.settings.internalName')}</label>
                <input
                  type="text"
                  name="name"
                  value={formData.name}
                  onChange={handleChange}
                  placeholder={t('admin.settings.internalNamePlaceholder')}
                  required
                />
                <small>{t('admin.settings.internalNameHelp')}</small>
              </div>

              <div className="form-group">
                <label>{t('admin.settings.displayNameLabel')}</label>
                <input
                  type="text"
                  name="display_name"
                  value={formData.display_name}
                  onChange={handleChange}
                  placeholder={t('admin.settings.displayNamePlaceholder')}
                  required
                />
              </div>

              <div className="form-group">
                <label>{t('admin.settings.discoveryUrl')}</label>
                <input
                  type="url"
                  name="discovery_url"
                  value={formData.discovery_url}
                  onChange={handleChange}
                  placeholder="https://idp.example.com/.well-known/openid-configuration"
                  required
                />
              </div>

              <div className="form-group">
                <label>{t('admin.settings.clientId')}</label>
                <input
                  type="text"
                  name="client_id"
                  value={formData.client_id}
                  onChange={handleChange}
                  placeholder={t('admin.settings.clientIdPlaceholder')}
                  required
                />
              </div>

              <div className="form-group">
                <label>{t('admin.settings.clientSecret')} {provider ? '' : '*'}</label>
                <input
                  type="password"
                  name="client_secret"
                  value={formData.client_secret}
                  onChange={handleChange}
                  placeholder={provider ? t('admin.settings.clientSecretHelp') : t('admin.settings.clientSecretPlaceholder')}
                  required={!provider}
                />
              </div>

              <div className="form-group">
                <label>{t('admin.settings.roleClaimPath')}</label>
                <input
                  type="text"
                  name="role_claim"
                  value={formData.role_claim}
                  onChange={handleChange}
                  placeholder={t('admin.settings.roleClaimPlaceholder')}
                />
                <small>{t('admin.settings.roleClaimHelp')}</small>
              </div>

              <div className="form-group">
                <label>{t('admin.settings.buttonColor')}</label>
                <div className="oidc-color-row">
                  <input
                    type="color"
                    name="button_color"
                    value={toHexColor(formData.button_color)}
                    onChange={handleChange}
                    className="color-picker"
                  />
                  <input
                    type="text"
                    name="button_color"
                    value={formData.button_color}
                    onChange={handleChange}
                    placeholder="#4285F4"
                    className="color-text-input"
                  />
                </div>
              </div>

              <div className="form-group">
                <label>{t('admin.settings.buttonIcon')}</label>
                {effectiveIconUrl && (
                  <div style={{ marginBottom: '0.5rem', display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                    <img src={effectiveIconUrl} alt={t('admin.settings.buttonPreview')} style={{ width: 32, height: 32, objectFit: 'contain', background: '#f0f0f0', borderRadius: 4, padding: 2 }} />
                    <button type="button" className="btn-cancel-setting" onClick={handleRemoveIconClick} style={{ fontSize: '0.8rem' }}>
                      {t('common.remove')}
                    </button>
                  </div>
                )}
                <input
                  type="file"
                  ref={iconInputRef}
                  accept=".png,.jpg,.jpeg,.gif,.svg"
                  onChange={(e) => handleIconFileSelect(e.target.files[0])}
                />
                <small>{t('admin.settings.buttonIconHelp')}</small>
              </div>

              <div className="form-group">
                <label style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                  <input
                    type="checkbox"
                    name="enabled"
                    checked={formData.enabled}
                    onChange={handleChange}
                  />
                  {t('common.enabled')}
                </label>
              </div>

              <div className="form-group">
                <label style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                  <input
                    type="checkbox"
                    name="sync_roles"
                    checked={formData.sync_roles}
                    onChange={handleChange}
                  />
                  {t('admin.settings.syncRolesOidc')}
                </label>
                <small>{t('admin.settings.syncRolesHelp')}</small>
              </div>
            </div>

            {/* Button Preview */}
            <div className="oidc-preview-section">
              <label>{t('admin.settings.buttonPreview')}</label>
              <button
                type="button"
                className="oidc-btn-preview"
                style={{
                  backgroundColor: formData.button_color,
                  color: getContrastColor(toHexColor(formData.button_color)),
                  border: 'none',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  gap: '0.5rem',
                }}
              >
                {effectiveIconUrl && (
                  <img src={effectiveIconUrl} alt="" style={{ width: 20, height: 20, objectFit: 'contain' }} />
                )}
                {formData.display_name || t('admin.settings.signInWithProvider')}
              </button>
            </div>

            {/* Role Mappings - only show when sync_roles is enabled */}
            {formData.sync_roles && (
              <div className="oidc-role-mappings">
                <div className="oidc-role-mappings-header">
                  <label>{t('admin.settings.roleMappings')}</label>
                  <button type="button" className="btn-save-setting" onClick={handleAddMapping}>
                    {t('admin.settings.addMapping')}
                  </button>
                </div>
                <small style={{ color: '#666', display: 'block', marginBottom: '0.75rem' }}>
                  {t('admin.settings.roleMappingsHelp')}
                </small>
                {Object.keys(formData.role_mappings).length === 0 ? (
                  <p style={{ color: '#999', fontSize: '0.9rem', fontStyle: 'italic' }}>
                    {t('admin.settings.noMappings')}
                  </p>
                ) : (
                  <div className="oidc-mapping-list">
                    {Object.entries(formData.role_mappings).map(([oidcRole, localRole], idx) => (
                      <div key={idx} className="oidc-mapping-row">
                        <input
                          type="text"
                          value={oidcRole}
                          onChange={(e) => handleMappingKeyChange(oidcRole, e.target.value)}
                          placeholder={t('admin.settings.oidcRoleName')}
                          className="oidc-mapping-input"
                        />
                        <span className="oidc-mapping-arrow">&rarr;</span>
                        <select
                          value={localRole}
                          onChange={(e) => handleMappingValueChange(oidcRole, e.target.value)}
                          className="oidc-mapping-select"
                        >
                          <option value="">{t('admin.settings.selectLocalRole')}</option>
                          {roles.map(role => (
                            <option key={role.id} value={role.name}>
                              {role.displayName || role.name}
                            </option>
                          ))}
                        </select>
                        <button
                          type="button"
                          className="oidc-mapping-remove"
                          onClick={() => handleRemoveMapping(oidcRole)}
                          title={t('common.remove')}
                        >
                          &times;
                        </button>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>

          <div className="modal-footer">
            <button type="button" className="btn btn-secondary" onClick={onClose}>
              {t('common.cancel')}
            </button>
            <button type="submit" className="btn btn-primary" disabled={saving}>
              {saving ? t('common.saving') : (provider ? t('admin.settings.updateProvider') : t('admin.settings.addProviderBtn'))}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function Settings() {
  const { t } = useTranslation();
  const { hasRole } = useAuth();
  const canEdit = hasRole('settings_admin') || hasRole('administrator');
  const queryClient = useQueryClient();

  const [settings, setSettings] = useState({});
  const [logoSettings, setLogoSettings] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');
  const [saving, setSaving] = useState(false);
  const [openGroups, setOpenGroups] = useState(() => new Set());

  // OIDC Provider state
  const [oidcProviders, setOidcProviders] = useState([]);
  const [showOidcModal, setShowOidcModal] = useState(false);
  const [editingProvider, setEditingProvider] = useState(null);
  const [oidcSaving, setOidcSaving] = useState(false);
  const [oidcModalError, setOidcModalError] = useState('');

  const { data: rolesData } = useQuery({
    queryKey: ['roles'],
    queryFn: async () => {
      const response = await rolesAPI.getAll();
      return response.data;
    },
  });
  const roles = rolesData?.roles || [];

  // Paired settings - these have both _enabled and _value (or _url) variants
  const pairedSettingKeys = ['brand_name', 'atak_installer_qr', 'itak_installer_qr', 'meshtastic_installer_qr_android', 'meshtastic_installer_qr_iphone', 'help_link', 'help_email'];

  useEffect(() => {
    fetchSettings();
    fetchLogoSettings();
    fetchOidcProviders();
  }, []);

  const fetchSettings = async () => {
    try {
      setLoading(true);
      const response = await settingsAPI.admin.getAll();
      setSettings(response.data);
      setError('');
    } catch (err) {
      console.error('Error fetching settings:', err);
      setError(err.response?.data?.error || t('admin.settings.failedLoadSettings'));
    } finally {
      setLoading(false);
    }
  };

  const fetchLogoSettings = async () => {
    try {
      const response = await settingsAPI.admin.getLogo();
      setLogoSettings(response.data);
    } catch (err) {
      console.error('Error fetching logo settings:', err);
    }
  };

  const fetchOidcProviders = async () => {
    try {
      const response = await oidcAPI.admin.getAll();
      setOidcProviders(response.data.providers || []);
    } catch (err) {
      console.error('Error fetching OIDC providers:', err);
    }
  };

  const handleOidcSave = async (data, iconFile, removeIcon) => {
    try {
      setOidcSaving(true);
      setOidcModalError('');
      let response;
      if (editingProvider) {
        response = await oidcAPI.admin.update(editingProvider.id, data);
        setSuccess(t('admin.settings.oidcUpdated'));
      } else {
        response = await oidcAPI.admin.create(data);
        setSuccess(t('admin.settings.oidcCreated'));
      }

      const providerId = response.data.id;

      // Handle icon upload or deletion
      if (iconFile) {
        await oidcAPI.admin.uploadIcon(providerId, iconFile);
      } else if (removeIcon) {
        await oidcAPI.admin.deleteIcon(providerId);
      }

      setShowOidcModal(false);
      setEditingProvider(null);
      setOidcModalError('');
      fetchOidcProviders();
      setTimeout(() => setSuccess(''), 3000);
    } catch (err) {
      console.error('Error saving OIDC provider:', err);
      setOidcModalError(err.response?.data?.error || t('admin.settings.failedSaveOidc'));
    } finally {
      setOidcSaving(false);
    }
  };

  const handleOidcDelete = async (provider) => {
    if (!confirm(t('admin.settings.deleteOidcConfirm', { name: provider.display_name }))) return;
    try {
      setSaving(true);
      setError('');
      await oidcAPI.admin.delete(provider.id);
      setSuccess(t('admin.settings.oidcDeleted'));
      fetchOidcProviders();
      setTimeout(() => setSuccess(''), 3000);
    } catch (err) {
      console.error('Error deleting OIDC provider:', err);
      setError(err.response?.data?.error || t('admin.settings.failedDeleteOidc'));
    } finally {
      setSaving(false);
    }
  };

  const handleLogoUpload = async (file) => {
    try {
      setSaving(true);
      setError('');
      await settingsAPI.admin.uploadLogo(file);
      setSuccess(t('admin.settings.logoUploaded'));
      fetchLogoSettings();
      setTimeout(() => setSuccess(''), 3000);
    } catch (err) {
      console.error('Error uploading logo:', err);
      setError(err.response?.data?.error || t('admin.settings.failedUploadLogo'));
    } finally {
      setSaving(false);
    }
  };

  const handleLogoDelete = async () => {
    try {
      setSaving(true);
      setError('');
      await settingsAPI.admin.deleteLogo();
      setSuccess(t('admin.settings.logoReset'));
      fetchLogoSettings();
      setTimeout(() => setSuccess(''), 3000);
    } catch (err) {
      console.error('Error deleting logo:', err);
      setError(err.response?.data?.error || t('admin.settings.failedResetLogo'));
    } finally {
      setSaving(false);
    }
  };

  const handleLoginBackgroundUpload = async (file) => {
    try {
      setSaving(true);
      setError('');
      await settingsAPI.admin.uploadLoginBackground(file);
      setSuccess(t('admin.settings.loginBackgroundUploaded'));
      fetchSettings();
      setTimeout(() => setSuccess(''), 3000);
    } catch (err) {
      console.error('Error uploading login background:', err);
      setError(err.response?.data?.error || t('admin.settings.failedUploadLoginBackground'));
    } finally {
      setSaving(false);
    }
  };

  const handleLoginBackgroundDelete = async () => {
    try {
      setSaving(true);
      setError('');
      await settingsAPI.admin.deleteLoginBackground();
      setSuccess(t('admin.settings.loginBackgroundRemoved'));
      fetchSettings();
      setTimeout(() => setSuccess(''), 3000);
    } catch (err) {
      console.error('Error deleting login background:', err);
      setError(err.response?.data?.error || t('admin.settings.failedRemoveLoginBackground'));
    } finally {
      setSaving(false);
    }
  };

  const handleDisplayModeChange = async (mode) => {
    try {
      setSaving(true);
      setError('');
      await settingsAPI.admin.updateByKey('logo_display_mode', mode);
      setSuccess(t('admin.settings.displayModeUpdated'));
      fetchLogoSettings();
      setTimeout(() => setSuccess(''), 3000);
    } catch (err) {
      console.error('Error updating display mode:', err);
      setError(err.response?.data?.error || t('admin.settings.failedUpdateDisplayMode'));
    } finally {
      setSaving(false);
    }
  };

  const handleToggle = async (settingId, currentValue) => {
    try {
      setSaving(true);
      setError('');
      setSuccess('');

      const newValue = currentValue === 'true' ? 'false' : 'true';
      await settingsAPI.admin.updateById(settingId, newValue);

      setSuccess(t('admin.settings.settingUpdated'));
      fetchSettings();
      queryClient.invalidateQueries({ queryKey: ['settings'] });

      setTimeout(() => setSuccess(''), 3000);
    } catch (err) {
      console.error('Error updating setting:', err);
      setError(err.response?.data?.error || t('admin.settings.failedUpdateSetting'));
    } finally {
      setSaving(false);
    }
  };

  const handleTextChange = async (settingId, newValue) => {
    try {
      setSaving(true);
      setError('');
      setSuccess('');

      await settingsAPI.admin.updateById(settingId, newValue);

      setSuccess(t('admin.settings.settingUpdated'));
      fetchSettings();
      queryClient.invalidateQueries({ queryKey: ['settings'] });

      setTimeout(() => setSuccess(''), 3000);
    } catch (err) {
      console.error('Error updating setting:', err);
      setError(err.response?.data?.error || t('admin.settings.failedUpdateSetting'));
    } finally {
      setSaving(false);
    }
  };

  // Detect if a setting should be rendered as a number input
  const isNumericSetting = (setting) => {
    // Check if key suggests it's a numeric value
    const numericKeyPatterns = ['_minutes', '_uses', '_count', '_limit', '_timeout', '_size', '_max', '_min'];
    const keyIsNumeric = numericKeyPatterns.some(pattern => setting.key.includes(pattern));

    // Also check if the value is purely numeric
    const valueIsNumeric = /^\d+$/.test(setting.value);

    return keyIsNumeric || (valueIsNumeric && setting.value !== 'true' && setting.value !== 'false');
  };

  const renderSettingToggle = (setting) => {
    const isEnabled = setting.value === 'true';

    return (
      <div key={setting.id} className="setting-item">
        <div className="setting-info">
          <h4>{settingLabel(t, setting.key)}</h4>
          <p className="setting-description">{settingDescription(t, setting)}</p>
        </div>
        <label className="toggle-switch">
          <input
            type="checkbox"
            checked={isEnabled}
            onChange={() => handleToggle(setting.id, setting.value)}
            disabled={saving || !canEdit}
          />
          <span className="toggle-slider"></span>
        </label>
      </div>
    );
  };

  const renderNumericSetting = (setting) => {
    return (
      <NumericSettingItem
        key={setting.id}
        setting={setting}
        onSave={handleTextChange}
        saving={saving}
        canEdit={canEdit}
      />
    );
  };

  // Get paired settings from a category
  const getPairedSettings = (categorySettings) => {
    const pairs = [];
    const processed = new Set();

    for (const baseKey of pairedSettingKeys) {
      const enabledSetting = categorySettings?.find(s => s.key === `${baseKey}_enabled`);
      // Look for both _value and _url suffixes
      const valueSetting = categorySettings?.find(s => s.key === `${baseKey}_value`) ||
                          categorySettings?.find(s => s.key === `${baseKey}_url`);

      if (enabledSetting) {
        pairs.push({ enabledSetting, valueSetting });
        processed.add(`${baseKey}_enabled`);
        if (valueSetting) processed.add(valueSetting.key);
      }
    }

    return { pairs, processed };
  };

  // Color setting keys
  const colorSettingKeys = ['primary_color', 'accent_color'];

  // Dropdown setting keys (rendered with <select>)
  const dropdownSettingKeys = ['default_theme', 'kiosk_default_theme', 'kiosk_language', 'date_format', 'login_layout', 'login_overlay_strength'];
  const dropdownOptions = {
    default_theme: [
      { value: 'light', label: t('admin.settings.light') },
      { value: 'dark', label: t('admin.settings.dark') },
    ],
    kiosk_default_theme: [
      { value: 'light', label: t('admin.settings.light') },
      { value: 'dark', label: t('admin.settings.dark') },
    ],
    kiosk_language: [
      { value: '', label: t('admin.settings.kioskLanguageOption.visitor') },
      { value: 'en', label: 'English' },
      { value: 'nl', label: 'Nederlands' },
      { value: 'de', label: 'Deutsch' },
    ],
    date_format: ['eu', 'us', 'iso'].map(value => (
      { value, label: t(`admin.settings.dateFormatOption.${value}`) }
    )),
    login_layout: ['classic', 'centered', 'split'].map(value => (
      { value, label: t(`admin.settings.loginLayoutOption.${value}`) }
    )),
    login_overlay_strength: ['none', 'light', 'medium', 'strong'].map(value => (
      { value, label: t(`admin.settings.loginOverlayOption.${value}`) }
    )),
  };

  // Get regular (non-paired) settings from a category
  const getRegularSettings = (categorySettings, processedKeys) => {
    // Settings managed by dedicated components (not shown as toggles)
    const managedSettings = ['custom_logo_enabled', 'custom_logo_path', 'logo_display_mode', 'login_background_path', 'login_hero_texts', ...colorSettingKeys, ...dropdownSettingKeys];
    return categorySettings?.filter(s => !processedKeys.has(s.key) && !managedSettings.includes(s.key)) || [];
  };

  // Get color settings from a category
  const getColorSettings = (categorySettings) => {
    return categorySettings?.filter(s => colorSettingKeys.includes(s.key)) || [];
  };

  // Get dropdown settings from a category
  const getDropdownSettings = (categorySettings) => {
    return categorySettings?.filter(s => dropdownSettingKeys.includes(s.key)) || [];
  };

  const renderCategorySettings = (categorySettings) => {
    const { pairs, processed } = getPairedSettings(categorySettings);
    const regularSettings = getRegularSettings(categorySettings, processed);

    // Separate numeric and toggle settings
    const numericSettings = regularSettings.filter(s => isNumericSetting(s));
    const toggleSettings = regularSettings.filter(s => !isNumericSetting(s));

    return (
      <>
        {/* Render paired settings first */}
        {pairs.map(({ enabledSetting, valueSetting }) => (
          <PairedSettingItem
            key={enabledSetting.id}
            enabledSetting={enabledSetting}
            valueSetting={valueSetting}
            onToggle={handleToggle}
            onSave={handleTextChange}
            saving={saving}
            canEdit={canEdit}
          />
        ))}
        {/* Render numeric settings */}
        {numericSettings.map(setting => renderNumericSetting(setting))}
        {/* Render regular toggle settings */}
        {toggleSettings.map(setting => renderSettingToggle(setting))}
      </>
    );
  };

  // Bucket all settings into their UI group
  const settingsByGroup = {};
  Object.entries(settings).forEach(([category, categorySettings]) => {
    (categorySettings || []).forEach(setting => {
      let groupId = SETTING_GROUP_OVERRIDES[setting.key] ?? category;
      if (!GROUPS.some(g => g.id === groupId)) groupId = 'general';
      (settingsByGroup[groupId] ||= []).push(setting);
    });
  });

  // Branding (logo upload) and Security (OIDC providers) always have content
  const visibleGroups = GROUPS.filter(g =>
    g.id === 'branding' || g.id === 'security' || settingsByGroup[g.id]?.length > 0
  );
  const allGroupsOpen = visibleGroups.every(g => openGroups.has(g.id));

  const toggleGroup = (groupId) => {
    setOpenGroups(prev => {
      const next = new Set(prev);
      if (next.has(groupId)) next.delete(groupId);
      else next.add(groupId);
      return next;
    });
  };

  const toggleAllGroups = () => {
    setOpenGroups(allGroupsOpen ? new Set() : new Set(visibleGroups.map(g => g.id)));
  };

  // Branding settings shown in the group's main list (login page settings get their own subsection)
  const mainGroupSettings = (groupId, groupSettings) => (
    groupId === 'branding' ? groupSettings.filter(s => !LOGIN_PAGE_SETTING_KEYS.includes(s.key)) : groupSettings
  );

  const renderLoginPageSettings = (loginSettings) => {
    const backgroundSetting = loginSettings.find(s => s.key === 'login_background_path');
    const heroSetting = loginSettings.find(s => s.key === 'login_hero_texts');

    return (
      <div className="settings-subsection">
        <div className="oidc-section-header">
          <h3>{t('admin.settings.loginPage')}</h3>
          <a href="/login" target="_blank" rel="noopener noreferrer">{t('admin.settings.previewLoginPage')}</a>
        </div>
        <p className="setting-description" style={{ marginBottom: '1rem' }}>
          {t('admin.settings.loginPageHelp')}
        </p>
        <p className="setting-description" style={{ marginBottom: '1rem' }}>
          {t('admin.settings.loginPageContrastHint')}
        </p>
        <div className="settings-list">
          {getDropdownSettings(loginSettings).map(setting => (
            <DropdownSettingItem
              key={setting.id}
              setting={setting}
              options={dropdownOptions[setting.key] || []}
              onSave={handleTextChange}
              saving={saving}
              canEdit={canEdit}
            />
          ))}
          {backgroundSetting && (
            <LoginBackgroundSetting
              backgroundPath={backgroundSetting.value}
              onUpload={handleLoginBackgroundUpload}
              onDelete={handleLoginBackgroundDelete}
              saving={saving}
              canEdit={canEdit}
            />
          )}
          {renderCategorySettings(loginSettings)}
          {heroSetting && (
            <HeroTextsSetting
              setting={heroSetting}
              onSave={handleTextChange}
              saving={saving}
              canEdit={canEdit}
            />
          )}
        </div>
      </div>
    );
  };

  const renderGroupContent = (groupId, groupSettings) => (
    <>
      {groupId === 'kiosk' && (
        <p className="setting-description" style={{ marginBottom: '1rem' }}>
          {t('admin.settings.kioskEnrollmentHelp')} <a href="/kiosk" target="_blank" rel="noopener noreferrer"><code>/kiosk</code></a>
        </p>
      )}
      <div className="settings-list">
        {groupId === 'branding' && (
          <LogoUploadSetting
            logoSettings={logoSettings}
            onUpload={handleLogoUpload}
            onDelete={handleLogoDelete}
            onDisplayModeChange={handleDisplayModeChange}
            saving={saving}
            canEdit={canEdit}
          />
        )}
        {/* Color Settings */}
        {getColorSettings(mainGroupSettings(groupId, groupSettings)).map(setting => (
          <ColorSettingItem
            key={setting.id}
            setting={setting}
            onSave={handleTextChange}
            saving={saving}
            canEdit={canEdit}
          />
        ))}
        {renderCategorySettings(mainGroupSettings(groupId, groupSettings))}
        {/* Dropdown Settings */}
        {getDropdownSettings(mainGroupSettings(groupId, groupSettings)).map(setting => (
          <DropdownSettingItem
            key={setting.id}
            setting={setting}
            options={dropdownOptions[setting.key] || []}
            onSave={handleTextChange}
            saving={saving}
            canEdit={canEdit}
          />
        ))}
      </div>

      {/* Login page appearance (part of Branding) */}
      {groupId === 'branding' && renderLoginPageSettings(groupSettings.filter(s => LOGIN_PAGE_SETTING_KEYS.includes(s.key)))}

      {/* OIDC Providers (part of Security) */}
      {groupId === 'security' && (
        <div className="settings-subsection">
          <div className="oidc-section-header">
            <h3>{t('admin.settings.oidcProviders')}</h3>
            <button
              className="btn btn-primary"
              onClick={() => { setEditingProvider(null); setOidcModalError(''); setShowOidcModal(true); }}
              style={{ fontSize: '0.875rem', padding: '0.5rem 1rem' }}
              disabled={!canEdit}
            >
              {t('admin.settings.addProvider')}
            </button>
          </div>

          {oidcProviders.length === 0 ? (
            <p style={{ color: '#666', fontStyle: 'italic' }}>
              {t('admin.settings.noOidcProviders')}
            </p>
          ) : (
            <div className="oidc-providers-list">
              {oidcProviders.map(provider => (
                <div key={provider.id} className="oidc-provider-card">
                  <div className="oidc-provider-info">
                    <div className="oidc-provider-top">
                      <div>
                        <h4>{provider.display_name}</h4>
                        <span className="oidc-provider-name">{provider.name}</span>
                      </div>
                      <span className={`oidc-provider-status ${provider.enabled ? 'enabled' : 'disabled'}`}>
                        {provider.enabled ? t('common.enabled') : t('common.disabled')}
                      </span>
                    </div>
                    <p className="oidc-provider-url">{provider.discovery_url}</p>
                    <div className="oidc-provider-preview">
                      <button
                        type="button"
                        className="oidc-btn-preview-small"
                        style={{
                          backgroundColor: provider.button_color || '#4285F4',
                          color: getContrastColorUtil(toHexColor(provider.button_color || '#4285F4')),
                          border: 'none',
                          display: 'inline-flex',
                          alignItems: 'center',
                          gap: '0.35rem',
                        }}
                      >
                        {provider.icon_url && (
                          <img src={`${API_BASE_URL}${provider.icon_url}`} alt="" style={{ width: 16, height: 16, objectFit: 'contain' }} />
                        )}
                        {provider.display_name}
                      </button>
                    </div>
                    {provider.role_mappings && Object.keys(provider.role_mappings).length > 0 && (
                      <div className="oidc-provider-mappings">
                        <small>{t('admin.settings.roleMappings')}: {Object.entries(provider.role_mappings).map(([k, v]) => `${k} → ${v}`).join(', ')}</small>
                      </div>
                    )}
                  </div>
                  <div className="oidc-provider-actions">
                    <button
                      className="btn-save-setting"
                      onClick={() => { setEditingProvider(provider); setOidcModalError(''); setShowOidcModal(true); }}
                      disabled={!canEdit}
                    >
                      {t('common.edit')}
                    </button>
                    <button
                      className="btn-cancel-setting"
                      onClick={() => handleOidcDelete(provider)}
                      style={{ color: '#c33' }}
                      disabled={!canEdit}
                    >
                      {t('common.delete')}
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </>
  );

  if (loading) {
    return (
      <div className="admin-page">
        <div className="admin-header">
          <h1>{t('admin.settings.title')}</h1>
        </div>
        <div className="loading">{t('admin.settings.loadingSettings')}</div>
      </div>
    );
  }

  return (
    <div className="admin-page">
      <div className="admin-header">
        <h1>{t('admin.settings.title')}</h1>
        <button type="button" className="btn btn-secondary settings-toggle-all" onClick={toggleAllGroups}>
          {allGroupsOpen ? t('admin.settings.collapseAll') : t('admin.settings.expandAll')}
        </button>
      </div>

      {error && <div className="alert alert-error">{error}</div>}
      {success && <div className="alert alert-success">{success}</div>}

      <div className="settings-container">
        {visibleGroups.map(group => (
          <SettingsGroup
            key={group.id}
            id={group.id}
            title={t(group.titleKey)}
            count={settingsByGroup[group.id]?.length || 0}
            isOpen={openGroups.has(group.id)}
            onToggle={() => toggleGroup(group.id)}
          >
            {renderGroupContent(group.id, settingsByGroup[group.id] || [])}
          </SettingsGroup>
        ))}
      </div>

      {/* OIDC Provider Modal */}
      {showOidcModal && (
        <OIDCProviderModal
          provider={editingProvider}
          roles={roles}
          onSave={handleOidcSave}
          onClose={() => { setShowOidcModal(false); setEditingProvider(null); setOidcModalError(''); }}
          saving={oidcSaving}
          error={oidcModalError}
        />
      )}
    </div>
  );
}

// Utility for contrast color (used in settings list)
function getContrastColorUtil(hex) {
  if (!hex || !hex.startsWith('#')) return '#ffffff';
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  const luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
  return luminance > 0.5 ? '#000000' : '#ffffff';
}

export default Settings;
