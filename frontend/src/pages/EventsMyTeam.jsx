import { useState } from 'react';
import { useQuery, useMutation } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Navigate } from 'react-router-dom';
import { QRCodeSVG } from 'qrcode.react';
import { eventsAPI } from '../services/api';
import { useAuth } from '../contexts/AuthContext';
import EnrollmentLink from '../components/EnrollmentLink';
import './EventsMyTeam.css';

export default function EventsMyTeam() {
  const { t } = useTranslation();
  const { permissions, loading: authLoading } = useAuth();
  const isLeader = permissions.roles.includes('event_leader');
  const [copied, setCopied] = useState(false);

  const { data, isLoading, error } = useQuery({
    queryKey: ['eventsMyTeam'],
    enabled: isLeader,
    queryFn: async () => (await eventsAPI.myTeam()).data,
  });

  const enrollment = useMutation({
    mutationFn: async () => (await eventsAPI.myTeamEnrollment()).data,
  });

  const handleCopy = (url) => {
    navigator.clipboard.writeText(url).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }).catch(() => {});
  };

  if (!authLoading && !isLeader) {
    return <Navigate to="/dashboard" replace />;
  }
  if (authLoading || isLoading) {
    return <div className="events-my-team"><p>{t('eventsMyTeam.loading')}</p></div>;
  }
  if (error || !data) {
    return (
      <div className="events-my-team">
        <p className="events-my-team-error">{error?.response?.data?.error || t('eventsMyTeam.loadFailed')}</p>
      </div>
    );
  }

  const atak = enrollment.data?.atak;

  return (
    <div className="events-my-team">
      <h1>{data.team?.name}</h1>
      <p className="events-my-team-sub">{data.event?.name}</p>

      <section className="events-my-team-card">
        <h2>{t('eventsMyTeam.playerLink')}</h2>
        {data.playerLink ? (
          <div className="events-my-team-qr">
            <QRCodeSVG value={data.playerLink.url} size={220} level="M" />
            <input type="text" readOnly value={data.playerLink.url} onFocus={(e) => e.target.select()} />
            <button type="button" className="btn btn-secondary" onClick={() => handleCopy(data.playerLink.url)}>
              {copied ? t('eventsMyTeam.copied') : t('eventsMyTeam.copy')}
            </button>
          </div>
        ) : (
          <p className="events-my-team-error">
            {data.reason === 'notRetrievable' ? t('eventsMyTeam.notRetrievable') : t('eventsMyTeam.missing')}
          </p>
        )}
      </section>

      {data.atakAvailable && (
        <section className="events-my-team-card">
          <h2>{t('eventsMyTeam.atakEnrollment')}</h2>
          {atak?.qrString ? (
            <div className="events-my-team-qr">
              <QRCodeSVG value={atak.qrString} size={240} level="M" />
              <p>{t('eventsMyTeam.scanAtak')}</p>
              <EnrollmentLink value={atak.qrString} />
            </div>
          ) : (
            <>
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => enrollment.mutate()}
                disabled={enrollment.isPending}
              >
                {enrollment.isPending ? t('eventsMyTeam.generating') : t('eventsMyTeam.showAtak')}
              </button>
              {enrollment.isError && (
                <p className="events-my-team-error">
                  {enrollment.error?.response?.data?.error || t('eventsMyTeam.enrollmentFailed')}
                </p>
              )}
            </>
          )}
        </section>
      )}
    </div>
  );
}
