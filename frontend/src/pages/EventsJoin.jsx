import { useEffect, useRef, useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { QRCodeSVG } from 'qrcode.react';
import { eventsAPI } from '../services/api';
import { useAuth } from '../contexts/AuthContext';
import EnrollmentLink from '../components/EnrollmentLink';
import './EventsJoin.css';

export default function EventsJoin() {
  const { token } = useParams();
  const navigate = useNavigate();
  const { t } = useTranslation();
  const { oidcLogin } = useAuth();
  const [state, setState] = useState('loading'); // loading | player | error
  const [data, setData] = useState(null);
  const [error, setError] = useState('');

  // Each join creates a device account, so it must be sent exactly once per
  // token. oidcLogin/t/navigate change identity while auth loads (and StrictMode
  // re-runs effects in dev); keep them in a ref and guard the request by token.
  const latest = useRef({});
  latest.current = { navigate, oidcLogin, t };
  const joinedToken = useRef(null);

  useEffect(() => {
    if (joinedToken.current === token) return;
    joinedToken.current = token;
    eventsAPI.join(token)
      .then(async (res) => {
        const d = res.data;
        if (d.role === 'leader') {
          await latest.current.oidcLogin(d.access_token, d.refresh_token);
          latest.current.navigate('/events/my-team');
          return;
        }
        if (d.role === 'player' && d.mode === 'individual') {
          latest.current.navigate(`/register/${d.onboardingCode}`);
          return;
        }
        setData(d);
        setState('player');
      })
      .catch((e) => {
        setError(e.response?.data?.error || latest.current.t('eventsJoin.failed'));
        setState('error');
      });
  }, [token]);

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
          <EnrollmentLink value={data.atak.qrString} />
          <p className="events-join-hint">{t('eventsShared.singleUseHint')}</p>
        </div>
      ) : (
        <p className="events-join-error">{t('eventsJoin.noEnrollment')}</p>
      )}
    </div>
  );
}
