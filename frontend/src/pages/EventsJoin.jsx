import { useEffect, useRef, useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { QRCodeSVG } from 'qrcode.react';
import { eventsAPI } from '../services/api';
import { useAuth } from '../contexts/AuthContext';
import EnrollmentLink from '../components/EnrollmentLink';
import './EventsJoin.css';

// Convenience only: the name last used on this browser for a leader link
const LEADER_NAME_KEY = 'eventLeaderName';
const LEADER_NAME_MAX = 80;

const readLeaderName = () => {
  try { return localStorage.getItem(LEADER_NAME_KEY) || ''; } catch { return ''; }
};

const saveLeaderName = (name) => {
  try { localStorage.setItem(LEADER_NAME_KEY, name); } catch { /* storage unavailable */ }
};

export default function EventsJoin() {
  const { token } = useParams();
  const navigate = useNavigate();
  const { t } = useTranslation();
  const { oidcLogin } = useAuth();
  const [state, setState] = useState('loading'); // loading | name | player | error
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  const [leaderName, setLeaderName] = useState('');
  const [nameError, setNameError] = useState('');
  const [joining, setJoining] = useState(false);

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
        // Leader links ask who is joining before the leader session is issued
        if (e.response?.status === 400 && e.response?.data?.code === 'display_name_required') {
          setLeaderName(readLeaderName());
          setState('name');
          return;
        }
        setError(e.response?.data?.error || latest.current.t('eventsJoin.failed'));
        setState('error');
      });
  }, [token]);

  const joinAsLeader = async (e) => {
    e.preventDefault();
    const name = leaderName.trim();
    if (!name) return;
    setJoining(true);
    setNameError('');
    try {
      const d = (await eventsAPI.join(token, { displayName: name })).data;
      saveLeaderName(name);
      await oidcLogin(d.access_token, d.refresh_token);
      navigate('/events/my-team');
    } catch (err) {
      setNameError(err.response?.data?.error || t('eventsJoin.failed'));
      setJoining(false);
    }
  };

  if (state === 'loading') {
    return <div className="events-join"><p>{t('eventsJoin.loading')}</p></div>;
  }
  if (state === 'name') {
    return (
      <div className="events-join">
        <h1>{t('eventsJoin.title')}</h1>
        <form className="events-join-name" onSubmit={joinAsLeader}>
          <label htmlFor="events-join-leader-name">{t('eventsJoin.leaderNameLabel')}</label>
          <input
            id="events-join-leader-name"
            type="text"
            value={leaderName}
            maxLength={LEADER_NAME_MAX}
            autoComplete="name"
            autoFocus
            onChange={(e) => setLeaderName(e.target.value)}
          />
          <p className="events-join-hint">{t('eventsJoin.leaderNameHint')}</p>
          <button type="submit" className="btn btn-primary" disabled={joining || !leaderName.trim()}>
            {joining ? t('eventsJoin.loading') : t('eventsJoin.leaderJoin')}
          </button>
          {nameError && <p className="events-join-error">{nameError}</p>}
        </form>
      </div>
    );
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
