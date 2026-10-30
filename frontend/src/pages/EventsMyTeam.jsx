import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Link, Navigate, useSearchParams } from 'react-router-dom';
import { QRCodeSVG } from 'qrcode.react';
import { eventsAPI } from '../services/api';
import { useAuth } from '../contexts/AuthContext';
import EnrollmentLink from '../components/EnrollmentLink';
import { formatDateTime, useDateFormat } from '../utils/formatDate';
import './EventsMyTeam.css';

export default function EventsMyTeam() {
  const { t } = useTranslation();
  const dateFormat = useDateFormat();
  const { permissions, leaderDesignations, loading: authLoading } = useAuth();
  // Shared leader session (scope from the token) or personal leader (own account)
  const isLeaderSessionUser = permissions.roles.includes('event_leader');
  const [searchParams] = useSearchParams();
  const wantedEvent = Number(searchParams.get('eventId'));
  const wantedTeam = Number(searchParams.get('teamId'));
  // A personal leader acts for one designation: the one in the URL, or the only one
  const designation = isLeaderSessionUser ? null
    : (leaderDesignations.find((d) => d.eventId === wantedEvent && d.teamId === wantedTeam)
      || (leaderDesignations.length === 1 ? leaderDesignations[0] : null));
  const scope = designation ? { eventId: designation.eventId, teamId: designation.teamId } : undefined;
  const isLeader = isLeaderSessionUser || !!designation;
  const scopeKey = [scope?.eventId ?? null, scope?.teamId ?? null];
  const [copied, setCopied] = useState(false);
  const [forLeader, setForLeader] = useState(false);
  const queryClient = useQueryClient();

  const { data, isLoading, error } = useQuery({
    queryKey: ['eventsMyTeam', ...scopeKey],
    enabled: isLeader,
    queryFn: async () => (await eventsAPI.myTeam(scope)).data,
  });

  const enrollment = useMutation({
    mutationFn: async (asLeader) => (await eventsAPI.myTeamEnrollment({ forLeader: asLeader }, scope)).data,
  });

  const accountsQuery = useQuery({
    queryKey: ['eventsMyTeamAccounts', ...scopeKey],
    enabled: isLeader,
    queryFn: async () => (await eventsAPI.myTeamAccounts(scope)).data,
  });

  const moveAccount = useMutation({
    mutationFn: async ({ sessionId, teamId }) => (await eventsAPI.myTeamMoveAccount(sessionId, teamId, scope)).data,
    onSettled: () => queryClient.invalidateQueries({ queryKey: ['eventsMyTeamAccounts'] }),
  });

  const handleMove = (account, teamId, confirmKey) => {
    if (!window.confirm(t(confirmKey))) return;
    moveAccount.mutate({ sessionId: account.id, teamId });
  };

  const handleCopy = (url) => {
    navigator.clipboard.writeText(url).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }).catch(() => {});
  };

  if (!authLoading && !isLeaderSessionUser && leaderDesignations.length === 0) {
    return <Navigate to="/dashboard" replace />;
  }
  if (!authLoading && !isLeader) {
    // A personal leader of several teams picks one
    return (
      <div className="events-my-team">
        <h1>{t('eventsMyTeam.chooseTeam')}</h1>
        <ul className="events-my-team-choices">
          {leaderDesignations.map((d) => (
            <li key={`${d.eventId}-${d.teamId}`}>
              <Link to={`/events/my-team?eventId=${d.eventId}&teamId=${d.teamId}`}>
                {d.teamName} · {d.eventName}{d.hq ? ` (${t('eventsMyTeam.hq')})` : ''}
              </Link>
            </li>
          ))}
        </ul>
      </div>
    );
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

  // Only show a QR from the latest successful press; never one from an earlier press
  const atak = enrollment.isSuccess ? enrollment.data?.atak : null;

  const accountsData = accountsQuery.data;
  const myTeamId = accountsData?.myTeamId;
  const otherTeams = (accountsData?.teams || []).filter((tm) => tm.id !== myTeamId);
  // Adding needs the own team to have a usable OTS group (it is then listed in teams)
  const myTeamUsable = (accountsData?.teams || []).some((tm) => tm.id === myTeamId);
  // Own team first, then other teams by name, parked last, legacy at the very end
  const rank = (a) => (a.legacy ? 3 : a.inMyTeam ? 0 : a.parked ? 2 : 1);
  const accounts = [...(accountsData?.accounts || [])].sort((a, b) =>
    rank(a) - rank(b) || (a.team?.name || '').localeCompare(b.team?.name || '') || a.id - b.id);

  return (
    <div className="events-my-team">
      <h1>{data.team?.name}</h1>
      <p className="events-my-team-sub">{data.event?.name}</p>
      {data.leaderName && (
        <p className="events-my-team-sub">{t('eventsMyTeam.leaderName', { name: data.leaderName })}</p>
      )}
      {!isLeaderSessionUser && leaderDesignations.length > 1 && (
        <p className="events-my-team-sub"><Link to="/events/my-team">{t('eventsMyTeam.otherTeam')}</Link></p>
      )}

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
          <label className="events-my-team-for-leader">
            <input type="checkbox" checked={forLeader} onChange={(e) => setForLeader(e.target.checked)} />
            {' '}{t('eventsMyTeam.forLeader')}
          </label>
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => { enrollment.reset(); enrollment.mutate(forLeader); }}
            disabled={enrollment.isPending}
          >
            {enrollment.isPending ? t('eventsMyTeam.generating') : t('eventsMyTeam.showAtak')}
          </button>
          {enrollment.isError && (
            <p className="events-my-team-error">
              {enrollment.error?.response?.data?.error || t('eventsMyTeam.enrollmentFailed')}
            </p>
          )}
          {atak?.qrString && (
            <div className="events-my-team-qr">
              <QRCodeSVG value={atak.qrString} size={240} level="M" />
              <p>{t('eventsMyTeam.scanAtak')}</p>
              <EnrollmentLink value={atak.qrString} />
            </div>
          )}
        </section>
      )}
      <section className="events-my-team-card events-my-team-accounts">
        <h2>{t('eventsMyTeam.accountsTitle')}</h2>
        <p className="events-my-team-sub">{t('eventsMyTeam.accountsNotice')}</p>
        <button
          type="button"
          className="btn btn-secondary"
          onClick={() => accountsQuery.refetch()}
          disabled={accountsQuery.isFetching}
        >
          {t('eventsMyTeam.refresh')}
        </button>
        {accountsQuery.isError && (
          <p className="events-my-team-error">
            {accountsQuery.error?.response?.data?.error || t('eventsMyTeam.accountsLoadFailed')}
          </p>
        )}
        {moveAccount.isError && (
          <p className="events-my-team-error">
            {moveAccount.error?.response?.data?.error || t('eventsMyTeam.actionFailed')}
          </p>
        )}
        {accountsData && accountsData.eudsAvailable === false && (
          <p className="events-my-team-sub">{t('eventsMyTeam.eudsUnavailable')}</p>
        )}
        {accountsData && (
          <div className="events-my-team-table-wrap">
            <table className="events-my-team-table">
              <thead>
                <tr>
                  <th>{t('eventsMyTeam.colAccount')}</th>
                  <th>{t('eventsMyTeam.colTeam')}</th>
                  <th>{t('eventsMyTeam.colStatus')}</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {accounts.length === 0 && (
                  <tr><td colSpan={4}>{t('eventsMyTeam.accountsEmpty')}</td></tr>
                )}
                {accounts.map((a) => (
                  <tr key={a.id} className={a.inMyTeam ? 'events-my-team-own' : ''}>
                    <td>
                      {(a.euds || []).map((e) => e.callsign).filter(Boolean).join(', ') || '-'}
                      <div className="events-my-team-hint">{a.otsUsername}</div>
                      <div className="events-my-team-hint">{formatDateTime(a.createdAt, dateFormat, { utc: true }) || '-'}</div>
                    </td>
                    <td>{a.parked ? '-' : (a.team?.name || '-')}</td>
                    <td>
                      {a.legacy
                        ? t('eventsMyTeam.statusLegacy')
                        : (a.parked ? t('eventsMyTeam.statusParked') : t('eventsMyTeam.statusActive'))}
                      {a.role === 'leader' && <span className="events-my-team-leader-tag">{t('eventsMyTeam.leaderDevice')}</span>}
                      {a.leaderName && <div className="events-my-team-hint">{t('eventsMyTeam.enrolledBy', { name: a.leaderName })}</div>}
                    </td>
                    <td className="events-my-team-actions">
                      {!a.legacy && a.inMyTeam && (
                        <>
                          <button
                            type="button"
                            className="btn btn-secondary"
                            disabled={moveAccount.isPending}
                            onClick={() => handleMove(a, null, 'eventsMyTeam.removeConfirm')}
                          >
                            {t('eventsMyTeam.removeFromTeam')}
                          </button>
                          {otherTeams.length > 0 && (
                            <select
                              aria-label={t('eventsMyTeam.moveTo')}
                              value=""
                              disabled={moveAccount.isPending}
                              onChange={(e) => {
                                const { value } = e.target;
                                e.target.value = '';
                                if (value) handleMove(a, Number(value), 'eventsMyTeam.moveConfirm');
                              }}
                            >
                              <option value="">{t('eventsMyTeam.moveTo')}</option>
                              {otherTeams.map((tm) => <option key={tm.id} value={tm.id}>{tm.name}</option>)}
                            </select>
                          )}
                        </>
                      )}
                      {!a.legacy && a.parked && myTeamUsable && (
                        <button
                          type="button"
                          className="btn btn-primary"
                          disabled={moveAccount.isPending}
                          onClick={() => handleMove(a, myTeamId, 'eventsMyTeam.addConfirm')}
                        >
                          {t('eventsMyTeam.addToMyTeam')}
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
