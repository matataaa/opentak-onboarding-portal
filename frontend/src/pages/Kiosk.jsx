import { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import axios from 'axios';
import { useQueryClient } from '@tanstack/react-query';
import { QRCodeSVG } from 'qrcode.react';
import { useTranslation, I18nextProvider } from 'react-i18next';
import { createFixedLanguageI18n } from '../i18n';
import { kioskAPI, settingsAPI } from '../services/api';
import ProgramRadioModal from '../components/ProgramRadioModal';
import ConfigValidatorModal from '../components/ConfigValidatorModal';
import EnrollRadioModal from '../components/EnrollRadioModal';
import KioskBackground from '../components/KioskBackground';
import { meshtasticSerial } from '../services/meshtasticSerial';
import './Kiosk.css';

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || window.location.origin;

const KIOSK_SESSION_KEY = 'kiosk_session';
const KIOSK_LANGUAGES = ['en', 'nl', 'de'];

function saveKioskSession(data) {
  sessionStorage.setItem(KIOSK_SESSION_KEY, JSON.stringify(data));
  // The kiosk token stays out of localStorage: the modals get the kiosk axios
  // instance instead, so closing the tab cannot leave it behind for the shared
  // api client. Drop any earlier user's session on this browser so the 401
  // refresh interceptor and AuthContext cannot fall back into it.
  localStorage.removeItem('access_token');
  localStorage.removeItem('refresh_token');
  localStorage.removeItem('user');
}

function clearKioskSession() {
  sessionStorage.removeItem(KIOSK_SESSION_KEY);
  // Also clears a kiosk token left in localStorage by older versions
  localStorage.removeItem('access_token');
  localStorage.removeItem('refresh_token');
  localStorage.removeItem('user');
}

function loadKioskSession() {
  try {
    const raw = sessionStorage.getItem(KIOSK_SESSION_KEY);
    if (!raw) return null;
    const data = JSON.parse(raw);
    // Check if session has expired
    if (data.expiryTime && Date.now() >= data.expiryTime) {
      clearKioskSession();
      return null;
    }
    return data;
  } catch {
    clearKioskSession();
    return null;
  }
}

function Kiosk() {
  const queryClient = useQueryClient();
  const [state, setState] = useState('loading'); // loading, qr, authenticated, disabled, error
  const [sessionId, setSessionId] = useState(null);
  const [qrUrl, setQrUrl] = useState(null);
  const [accessToken, setAccessToken] = useState(null);
  const [refreshToken, setRefreshToken] = useState(null);
  const [kioskUser, setKioskUser] = useState(null);
  const [settings, setSettings] = useState(null);
  const [timeRemaining, setTimeRemaining] = useState(0);
  const [errorMessage, setErrorMessage] = useState(''); // i18n key

  // Fixed kiosk language from the settings; otherwise follow the browser (global instance)
  const kioskLanguage = settings?.kiosk_language;
  const kioskI18n = useMemo(
    () => (KIOSK_LANGUAGES.includes(kioskLanguage) ? createFixedLanguageI18n(kioskLanguage) : null),
    [kioskLanguage]
  );
  const { t } = useTranslation(undefined, { i18n: kioskI18n || undefined });

  useEffect(() => {
    if (!kioskI18n) return;
    const previousLang = document.documentElement.lang;
    document.documentElement.lang = kioskLanguage;
    return () => { document.documentElement.lang = previousLang; };
  }, [kioskI18n, kioskLanguage]);

  // Authenticated view state
  const [atakQrData, setAtakQrData] = useState(null);
  const [itakQrData, setItakQrData] = useState(null);
  const [radios, setRadios] = useState([]);
  const [programmingRadio, setProgrammingRadio] = useState(null);
  const [validatingRadio, setValidatingRadio] = useState(null);

  // Radio enrollment state
  const [showEnrollModal, setShowEnrollModal] = useState(false);

  const pollRef = useRef(null);
  const timerRef = useRef(null);
  const timeoutMinutesRef = useRef(10);
  const expiryTimeRef = useRef(null);
  const refreshInFlightRef = useRef(false);

  // Create an axios instance authenticated with the kiosk token
  const getKioskApi = useCallback(() => {
    if (!accessToken) return null;
    return axios.create({
      baseURL: `${API_BASE_URL}/api/v1`,
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${accessToken}`,
      },
    });
  }, [accessToken]);
  const kioskClient = useMemo(() => getKioskApi(), [getKioskApi]);

  // Load settings on mount and restore session if saved
  useEffect(() => {
    settingsAPI.get().then((res) => {
      setSettings(res.data);

      // Apply kiosk theme
      const kioskTheme = res.data?.kiosk_default_theme || 'dark';
      document.documentElement.setAttribute('data-theme', kioskTheme);

      if (!res.data?.kiosk_enrollment_enabled) {
        setState('disabled');
        return;
      }

      // Check for saved session (survives page refresh)
      const saved = loadKioskSession();
      if (saved) {
        // Older versions also kept the kiosk token in localStorage
        localStorage.removeItem('access_token');
        setAccessToken(saved.accessToken);
        setRefreshToken(saved.refreshToken);
        setKioskUser(saved.user);
        expiryTimeRef.current = saved.expiryTime;
        setState('authenticated');
      } else {
        createNewSession();
      }
    }).catch(() => {
      setState('error');
      setErrorMessage('kiosk.failedLoadSettings');
    });
  }, []);

  const requestSession = async () => {
    const res = await kioskAPI.createSession();
    setSessionId(res.data.session_id);
    setQrUrl(res.data.qr_url);
    setState('qr');
  };

  // Create a new kiosk session
  const createNewSession = async () => {
    setState('loading');
    try {
      await requestSession();
    } catch (err) {
      if (err.response?.status === 403) {
        setState('disabled');
      } else {
        setState('error');
        setErrorMessage('kiosk.failedCreateSession');
      }
    }
  };

  // Replace an expired session while the old QR stays on screen. The poll keeps
  // firing meanwhile, so the guard makes sure only one session gets created.
  const refreshSession = async () => {
    if (refreshInFlightRef.current) return;
    refreshInFlightRef.current = true;
    try {
      await requestSession();
    } catch (err) {
      // Otherwise the next poll retries, so an unattended kiosk recovers by itself
      if (err.response?.status === 403) setState('disabled');
    } finally {
      refreshInFlightRef.current = false;
    }
  };

  // Poll for session status when in QR state
  useEffect(() => {
    if (state !== 'qr' || !sessionId) return;

    // A slow response for a session that has already been replaced must not refresh again
    let replaced = false;
    let emptyReads = 0;

    const poll = async () => {
      try {
        const res = await kioskAPI.getSessionStatus(sessionId);
        // The status endpoint hands out tokens once, so a late overlapping response
        // would overwrite them with nulls
        if (replaced) return;
        if (res.data.status === 'authenticated' && !res.data.access_token) {
          // Another read took the tokens, or the response carrying them is still on
          // its way. Wait for that, then give up on this session.
          emptyReads += 1;
          if (emptyReads >= 4) refreshSession();
        } else if (res.data.status === 'authenticated') {
          const token = res.data.access_token;
          const refresh = res.data.refresh_token;
          const user = res.data.user;

          // Calculate expiry time for session persistence
          const timeoutStr = settings?.kiosk_session_timeout_minutes || '10';
          const timeout = parseInt(timeoutStr) || 10;
          const expiry = Date.now() + timeout * 60 * 1000;

          // Save session to survive page refresh
          saveKioskSession({
            accessToken: token,
            refreshToken: refresh,
            user,
            expiryTime: expiry,
          });

          setAccessToken(token);
          setRefreshToken(refresh);
          setKioskUser(user);
          expiryTimeRef.current = expiry;
          setState('authenticated');
        } else if (res.data.status === 'expired') {
          // Session expired, create a new one
          refreshSession();
        }
      } catch (err) {
        // Another kiosk's session create cleans up expired sessions, so ours can be
        // gone before we saw it expire. Other poll errors are ignored.
        if (err.response?.data?.error === 'Session not found' && !replaced) refreshSession();
      }
    };

    pollRef.current = setInterval(poll, 2500);
    return () => {
      replaced = true;
      clearInterval(pollRef.current);
    };
  }, [state, sessionId]);

  // When authenticated, fetch QR codes and radios, start countdown
  useEffect(() => {
    if (state !== 'authenticated' || !accessToken) return;

    const kioskApi = getKioskApi();
    if (!kioskApi) return;

    // Fetch ATAK QR
    if (settings?.generate_atak_qr_code) {
      kioskApi.get('/qr/atak').then(res => setAtakQrData(res.data)).catch(() => {});
    }

    // Fetch iTAK QR
    if (settings?.generate_itak_qr_code) {
      kioskApi.get('/qr/itak').then(res => setItakQrData(res.data)).catch(() => {});
    }

    // Fetch user's assigned radios only
    if (settings?.radios_enabled) {
      kioskApi.get('/radios').then(res => {
        const list = res.data?.radios || [];
        setRadios(list.filter(r => r.radioType === 'meshtastic' && r.assignedTo === kioskUser.id));
      }).catch(() => {});
    }

    // Start countdown timer using absolute expiry time
    // (setInterval is throttled in background tabs, so we must compare against real clock)
    // Use existing expiryTimeRef if already set (from session restore), otherwise calculate new
    if (!expiryTimeRef.current) {
      const timeoutStr = settings?.kiosk_session_timeout_minutes || '10';
      const timeout = parseInt(timeoutStr) || 10;
      timeoutMinutesRef.current = timeout;
      expiryTimeRef.current = Date.now() + timeout * 60 * 1000;
    }

    const updateTimer = () => {
      const remaining = Math.max(0, Math.floor((expiryTimeRef.current - Date.now()) / 1000));
      setTimeRemaining(remaining);
      if (remaining <= 0) {
        handleSignOut();
      }
    };

    updateTimer();
    timerRef.current = setInterval(updateTimer, 1000);

    // Also check immediately when tab regains focus
    const handleVisibility = () => {
      if (!document.hidden) updateTimer();
    };
    document.addEventListener('visibilitychange', handleVisibility);

    return () => {
      clearInterval(timerRef.current);
      document.removeEventListener('visibilitychange', handleVisibility);
    };
  }, [state, accessToken]);

  const handleSignOut = () => {
    clearInterval(timerRef.current);
    clearInterval(pollRef.current);
    clearKioskSession();
    // The next person at this kiosk must not see this user's cached groups
    queryClient.removeQueries({ queryKey: ['meshtasticGroups'] });
    expiryTimeRef.current = null;
    setAccessToken(null);
    setRefreshToken(null);
    setKioskUser(null);
    setAtakQrData(null);
    setItakQrData(null);
    setRadios([]);
    setProgrammingRadio(null);
    setValidatingRadio(null);
    setShowEnrollModal(false);
    createNewSession();
  };

  const formatTime = (seconds) => {
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    return `${m}:${s.toString().padStart(2, '0')}`;
  };

  // Prevent navigation away from kiosk
  useEffect(() => {
    const handleBeforeUnload = (e) => {
      if (state === 'authenticated') {
        e.preventDefault();
        e.returnValue = '';
      }
    };
    window.addEventListener('beforeunload', handleBeforeUnload);
    return () => window.removeEventListener('beforeunload', handleBeforeUnload);
  }, [state]);

  // Loading state
  if (state === 'loading') {
    return (
      <div className="kiosk-container">
        <div className="kiosk-loading">
          <div className="kiosk-spinner" />
          <p>{t('kiosk.loading')}</p>
        </div>
      </div>
    );
  }

  // Disabled state
  if (state === 'disabled') {
    return (
      <div className="kiosk-container">
        <div className="kiosk-message">
          <h2>{t('kiosk.title')}</h2>
          <p>{t('kiosk.notEnabled')}</p>
        </div>
      </div>
    );
  }

  // Error state
  if (state === 'error') {
    return (
      <div className="kiosk-container">
        <div className="kiosk-message">
          <h2>{t('kiosk.error')}</h2>
          <p>{t(errorMessage || 'kiosk.somethingWrong')}</p>
          <button className="kiosk-retry-btn" onClick={createNewSession}>
            {t('kiosk.tryAgain')}
          </button>
        </div>
      </div>
    );
  }

  // QR display state
  if (state === 'qr') {
    return (
      <div className="kiosk-container kiosk-container--qr">
        <KioskBackground />
        <div className="kiosk-qr-screen">
          <div className="kiosk-qr-info">
            {settings?.custom_logo_enabled && settings?.custom_logo_path ? (
              <img src={`${API_BASE_URL}${settings.custom_logo_path}`} alt={t('kiosk.logoAlt')} className="kiosk-logo" />
            ) : (
              <img src={`${API_BASE_URL}/static/img/logo.png`} alt={t('kiosk.logoAlt')} className="kiosk-logo" />
            )}
            <h1>{settings?.brand_name || 'OpenTAK Portal'}</h1>
            <p className="kiosk-subtitle">{t('kiosk.scanToLogin')}</p>
            <p className="kiosk-qr-instruction">
              {t('kiosk.scanDesc')}
              <span className="kiosk-polling-dot" />
            </p>
          </div>
          <div className="kiosk-qr-code">
            {qrUrl && <QRCodeSVG value={qrUrl} size={280} level="M" marginSize={4} />}
          </div>
        </div>
      </div>
    );
  }

  // Authenticated kiosk view
  if (state === 'authenticated' && kioskUser) {
    const isWarning = timeRemaining < 60;

    const dashboard = (
      <div className="kiosk-container kiosk-container--dashboard">
        <KioskBackground />
        <div className="kiosk-dashboard">
          <div className="kiosk-header">
            <div className="kiosk-header-brand">
              {settings?.custom_logo_enabled && settings?.custom_logo_path ? (
                <img src={`${API_BASE_URL}${settings.custom_logo_path}`} alt={t('kiosk.logoAlt')} className="kiosk-header-logo" />
              ) : (
                <img src={`${API_BASE_URL}/static/img/logo.png`} alt={t('kiosk.logoAlt')} className="kiosk-header-logo" />
              )}
              <h1>{t('kiosk.welcome', { name: kioskUser.callsign || kioskUser.username })}</h1>
            </div>
            <div className="kiosk-header-actions">
              <span className={`kiosk-timer ${isWarning ? 'warning' : ''}`}>
                {t('kiosk.session')}{formatTime(timeRemaining)}
              </span>
              <button className="kiosk-signout-btn" onClick={handleSignOut}>
                {t('kiosk.signOut')}
              </button>
            </div>
          </div>

          <div className="kiosk-sections">
            {/* QR Codes Section */}
            {(atakQrData || itakQrData) && (
              <div className="kiosk-section kiosk-section--qr">
                <h2>{t('kiosk.takEnrollment')}</h2>
                <div className="kiosk-qr-pair">
                  {atakQrData?.qr_string && (
                    <div className="kiosk-qr-item">
                      <QRCodeSVG value={atakQrData.qr_string} size={360} level="M" marginSize={4} />
                      <p>{t('kiosk.atak')}</p>
                    </div>
                  )}
                  {itakQrData?.qr_string && (
                    <div className="kiosk-qr-item">
                      <QRCodeSVG value={itakQrData.qr_string} size={360} level="M" marginSize={4} />
                      <p>{t('kiosk.itak')}</p>
                    </div>
                  )}
                </div>
              </div>
            )}

            {/* Radio Section */}
            {(radios.length > 0 || settings?.user_radio_enrollment_enabled) && (
              <div className="kiosk-section">
                <h2>{t('kiosk.yourRadios')}</h2>
                {radios.length > 0 ? (
                  <div className="kiosk-radio-list">
                    {radios.map(radio => (
                      <div key={radio.id} className="kiosk-radio-item">
                        <span className="kiosk-radio-name">{radio.name}</span>
                        {radio.platform === 'meshtastic' && meshtasticSerial.getBrowserSupport().isSupported && (settings?.user_program_radio_enabled || settings?.user_validate_radio_enabled) && (
                          <div className="kiosk-radio-actions">
                            {settings?.user_validate_radio_enabled && (
                              <button
                                className="kiosk-btn-validate"
                                onClick={() => setValidatingRadio(radio)}
                              >
                                {t('kiosk.validate')}
                              </button>
                            )}
                            {settings?.user_program_radio_enabled && (
                              <button
                                className="kiosk-btn-program"
                                onClick={() => setProgrammingRadio(radio)}
                              >
                                {t('kiosk.program')}
                              </button>
                            )}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                ) : (
                  <p style={{ color: 'var(--text-muted)', marginBottom: '1rem' }}>{t('kiosk.noRadios')}</p>
                )}
                {settings?.user_radio_enrollment_enabled && (
                  <button
                    className="kiosk-btn-program"
                    style={{ marginTop: '1rem' }}
                    onClick={() => setShowEnrollModal(true)}
                  >
                    + {t('kiosk.registerRadio')}
                  </button>
                )}
              </div>
            )}

            {/* User Info Section */}
            <div className="kiosk-section">
              <h2>{t('kiosk.account')}</h2>
              <p><strong>{t('kiosk.username')}:</strong> {kioskUser.username}</p>
              <p><strong>{t('kiosk.name')}:</strong> {kioskUser.firstName} {kioskUser.lastName}</p>
              <p><strong>{t('kiosk.callsign')}:</strong> {kioskUser.callsign || '-'}</p>
            </div>
          </div>
        </div>

        {/* Program Radio Modal */}
        {programmingRadio && (
          <ProgramRadioModal
            radio={programmingRadio}
            onClose={() => setProgrammingRadio(null)}
            apiClient={kioskClient}
          />
        )}

        {/* Config Validator Modal */}
        {validatingRadio && (
          <ConfigValidatorModal
            radio={validatingRadio}
            onClose={() => setValidatingRadio(null)}
            apiClient={kioskClient}
          />
        )}

        {/* Enroll Radio Modal */}
        {showEnrollModal && (
          <EnrollRadioModal
            onClose={() => setShowEnrollModal(false)}
            apiClient={kioskClient}
            onSuccess={() => {
              // Re-fetch radios after enrollment
              const kioskApi = getKioskApi();
              if (kioskApi) {
                kioskApi.get('/radios').then(res => {
                  const list = res.data?.radios || [];
                  setRadios(list.filter(r => r.radioType === 'meshtastic' && r.assignedTo === kioskUser.id));
                }).catch(() => {});
              }
            }}
          />
        )}
      </div>
    );

    // The radio modals translate via the provider, so they follow the kiosk language too
    return kioskI18n ? <I18nextProvider i18n={kioskI18n}>{dashboard}</I18nextProvider> : dashboard;
  }

  return null;
}

export default Kiosk;
