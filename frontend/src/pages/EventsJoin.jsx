import { useEffect, useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { QRCodeSVG } from 'qrcode.react';
import { eventsAPI } from '../services/api';
import { useAuth } from '../contexts/AuthContext';
import './EventsJoin.css';

export default function EventsJoin() {
  const { token } = useParams();
  const navigate = useNavigate();
  const { t } = useTranslation();
  const { oidcLogin } = useAuth();
  const [state, setState] = useState('loading'); // loading | player | error
  const [data, setData] = useState(null);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    eventsAPI.join(token)
      .then(async (res) => {
        if (cancelled) return;
        const d = res.data;
        if (d.role === 'leader') {
          await oidcLogin(d.access_token, d.refresh_token);
          navigate('/dashboard');
          return;
        }
        if (d.role === 'player' && d.mode === 'individual') {
          navigate(`/register/${d.onboardingCode}`);
          return;
        }
        setData(d);
        setState('player');
      })
      .catch((e) => {
        if (cancelled) return;
        setError(e.response?.data?.error || t('eventsJoin.failed'));
        setState('error');
      });
    return () => { cancelled = true; };
  }, [token, navigate, oidcLogin, t]);

  if (state === 'loading') {
    return <div className="events-join"><p>{t('eventsJoin.loading')}</p></div>;
  }
  if (state === 'error') {
    return (
      <div className="events-join">
        <h1>{t('eventsJoin.title')}</h1>
        <p className="events-join-error">{error}</p>
      </div>
    );
  }
  return (
    <div className="events-join">
      <h1>{data.team?.name}</h1>
      <p>{t('eventsJoin.sharedIntro')}</p>
      {data.atak?.qrString ? (
        <div className="events-join-qr">
          <QRCodeSVG value={data.atak.qrString} size={240} level="M" />
          <p>{t('eventsJoin.scanAtak')}</p>
        </div>
      ) : (
        <p className="events-join-error">{t('eventsJoin.noEnrollment')}</p>
      )}
    </div>
  );
}
