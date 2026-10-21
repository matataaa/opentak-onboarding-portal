import { useState, useEffect, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { eventsAPI, groupsAPI, onboardingCodesAPI } from '../../services/api';
import './EventsAdmin.css';

function toLocalInput(iso) {
  if (!iso) return '';
  // ISO -> value for <input type="datetime-local">
  const d = new Date(iso);
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export default function EventsAdmin() {
  const { t } = useTranslation();
  const [teams, setTeams] = useState([]);
  const [events, setEvents] = useState([]);
  const [groups, setGroups] = useState([]);
  const [codes, setCodes] = useState([]);
  const [error, setError] = useState('');
  const [newLink, setNewLink] = useState(null); // last generated link (shown once)

  const [teamForm, setTeamForm] = useState({ name: '', color: '', otsGroupId: '' });
  const [leaderForm, setLeaderForm] = useState({}); // teamId -> {username, password}
  const [eventForm, setEventForm] = useState({
    name: '', validFrom: '', validUntil: '', playerAccessMode: 'individual', accountCleanup: 'deactivate',
  });
  const [openLinksFor, setOpenLinksFor] = useState(null);
  const [links, setLinks] = useState([]);
  const [linkForm, setLinkForm] = useState({ teamId: '', role: 'leader', onboardingCodeId: '' });

  const reload = useCallback(async () => {
    try {
      const [tr, er] = await Promise.all([eventsAPI.listTeams(), eventsAPI.listEvents()]);
      setTeams(tr.data.teams || []);
      setEvents(er.data.events || []);
    } catch (e) {
      setError(e.response?.data?.error || t('events.loadFailed'));
    }
  }, [t]);

  useEffect(() => {
    reload();
    groupsAPI.getAll().then((r) => setGroups(r.data.groups || r.data || [])).catch(() => {});
    onboardingCodesAPI.getAll({ per_page: 1000 }).then((r) => setCodes(r.data.onboardingCodes || r.data.codes || r.data || [])).catch(() => {});
  }, [reload]);

  const call = async (fn, after) => {
    setError('');
    try {
      const res = await fn();
      if (after) after(res);
      await reload();
    } catch (e) {
      setError(e.response?.data?.error || t('events.actionFailed'));
    }
  };

  const createTeam = () => call(
    () => eventsAPI.createTeam({
      name: teamForm.name,
      color: teamForm.color || null,
      otsGroupId: teamForm.otsGroupId ? Number(teamForm.otsGroupId) : null,
    }),
    () => setTeamForm({ name: '', color: '', otsGroupId: '' }),
  );

  const setLeader = (teamId) => {
    const f = leaderForm[teamId] || {};
    call(
      () => eventsAPI.createTeamLeader(teamId, { username: f.username, password: f.password }),
      () => setLeaderForm((p) => ({ ...p, [teamId]: {} })),
    );
  };

  const createEvent = () => call(
    () => eventsAPI.createEvent({
      name: eventForm.name,
      validFrom: eventForm.validFrom || null,
      validUntil: eventForm.validUntil || null,
      playerAccessMode: eventForm.playerAccessMode,
      accountCleanup: eventForm.accountCleanup,
    }),
    () => setEventForm({ name: '', validFrom: '', validUntil: '', playerAccessMode: 'individual', accountCleanup: 'deactivate' }),
  );

  const openLinks = async (eventId) => {
    setOpenLinksFor(eventId);
    setNewLink(null);
    try {
      const r = await eventsAPI.listLinks(eventId);
      setLinks(r.data.links || []);
    } catch { setLinks([]); }
  };

  const createLink = () => {
    const payload = { teamId: Number(linkForm.teamId), role: linkForm.role };
    if (linkForm.role === 'player' && linkForm.onboardingCodeId) payload.onboardingCodeId = Number(linkForm.onboardingCodeId);
    call(
      () => eventsAPI.createLink(openLinksFor, payload),
      (res) => setNewLink(res.data),
    ).then(() => openLinks(openLinksFor));
  };

  return (
    <div className="events-admin">
      <h1>{t('events.title')}</h1>
      <p className="events-intro">{t('events.intro')}</p>
      {error && <div className="events-error">{error}</div>}

      {/* Teams */}
      <section className="events-section">
        <h2>{t('events.teams')}</h2>
        <div className="events-form-row">
          <input placeholder={t('events.teamName')} value={teamForm.name} onChange={(e) => setTeamForm({ ...teamForm, name: e.target.value })} />
          <input placeholder={t('events.color')} value={teamForm.color} onChange={(e) => setTeamForm({ ...teamForm, color: e.target.value })} />
          <select value={teamForm.otsGroupId} onChange={(e) => setTeamForm({ ...teamForm, otsGroupId: e.target.value })}>
            <option value="">{t('events.noGroup')}</option>
            {groups.map((g) => <option key={g.id} value={g.id}>{g.displayName || g.name}</option>)}
          </select>
          <button onClick={createTeam} disabled={!teamForm.name}>{t('events.addTeam')}</button>
        </div>
        <table className="events-table">
          <thead><tr><th>{t('events.teamName')}</th><th>{t('events.color')}</th><th>{t('events.group')}</th><th>{t('events.leader')}</th><th></th></tr></thead>
          <tbody>
            {teams.map((team) => (
              <tr key={team.id}>
                <td>{team.name}</td>
                <td>{team.color || '-'}</td>
                <td>{team.otsGroup?.name || '-'}</td>
                <td>
                  {team.leaderUser ? team.leaderUser.username : (
                    <span className="events-leader-form">
                      <input placeholder={t('events.leaderUsername')} value={(leaderForm[team.id] || {}).username || ''} onChange={(e) => setLeaderForm((p) => ({ ...p, [team.id]: { ...(p[team.id] || {}), username: e.target.value } }))} />
                      <input type="password" placeholder={t('events.password')} value={(leaderForm[team.id] || {}).password || ''} onChange={(e) => setLeaderForm((p) => ({ ...p, [team.id]: { ...(p[team.id] || {}), password: e.target.value } }))} />
                      <button onClick={() => setLeader(team.id)}>{t('events.setLeader')}</button>
                    </span>
                  )}
                </td>
                <td><button className="events-danger" onClick={() => call(() => eventsAPI.deleteTeam(team.id))}>{t('common.delete')}</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      {/* Events */}
      <section className="events-section">
        <h2>{t('events.events')}</h2>
        <div className="events-form-row">
          <input placeholder={t('events.eventName')} value={eventForm.name} onChange={(e) => setEventForm({ ...eventForm, name: e.target.value })} />
          <label>{t('events.validFrom')}<input type="datetime-local" value={eventForm.validFrom} onChange={(e) => setEventForm({ ...eventForm, validFrom: e.target.value })} /></label>
          <label>{t('events.validUntil')}<input type="datetime-local" value={eventForm.validUntil} onChange={(e) => setEventForm({ ...eventForm, validUntil: e.target.value })} /></label>
          <select value={eventForm.playerAccessMode} onChange={(e) => setEventForm({ ...eventForm, playerAccessMode: e.target.value })}>
            <option value="individual">{t('events.modeIndividual')}</option>
            <option value="shared">{t('events.modeShared')}</option>
          </select>
          <select value={eventForm.accountCleanup} onChange={(e) => setEventForm({ ...eventForm, accountCleanup: e.target.value })}>
            <option value="deactivate">{t('events.cleanupDeactivate')}</option>
            <option value="delete">{t('events.cleanupDelete')}</option>
          </select>
          <button onClick={createEvent} disabled={!eventForm.name}>{t('events.addEvent')}</button>
        </div>
        <table className="events-table">
          <thead><tr><th>{t('events.eventName')}</th><th>{t('events.validUntil')}</th><th>{t('events.playerMode')}</th><th>{t('events.cleanup')}</th><th></th></tr></thead>
          <tbody>
            {events.map((ev) => (
              <tr key={ev.id}>
                <td>{ev.name}</td>
                <td>{ev.validUntil ? toLocalInput(ev.validUntil).replace('T', ' ') : t('common.never')}</td>
                <td>{ev.playerAccessMode}</td>
                <td>{ev.accountCleanup}</td>
                <td>
                  <button onClick={() => openLinks(ev.id)}>{t('events.links')}</button>
                  <button className="events-danger" onClick={() => call(() => eventsAPI.deleteEvent(ev.id))}>{t('common.delete')}</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      {/* Links for the selected event */}
      {openLinksFor && (
        <section className="events-section">
          <h2>{t('events.linksFor')} #{openLinksFor}</h2>
          {newLink && (
            <div className="events-new-link">
              <strong>{t('events.linkOnce')}</strong>
              <code>{newLink.url}</code>
            </div>
          )}
          <div className="events-form-row">
            <select value={linkForm.teamId} onChange={(e) => setLinkForm({ ...linkForm, teamId: e.target.value })}>
              <option value="">{t('events.selectTeam')}</option>
              {teams.map((tm) => <option key={tm.id} value={tm.id}>{tm.name}</option>)}
            </select>
            <select value={linkForm.role} onChange={(e) => setLinkForm({ ...linkForm, role: e.target.value })}>
              <option value="leader">{t('events.roleLeader')}</option>
              <option value="player">{t('events.rolePlayer')}</option>
            </select>
            {linkForm.role === 'player' && (
              <select value={linkForm.onboardingCodeId} onChange={(e) => setLinkForm({ ...linkForm, onboardingCodeId: e.target.value })}>
                <option value="">{t('events.noCode')}</option>
                {codes.map((c) => <option key={c.id} value={c.id}>{c.name || c.onboardingCode}</option>)}
              </select>
            )}
            <button onClick={createLink} disabled={!linkForm.teamId}>{t('events.generateLink')}</button>
          </div>
          <table className="events-table">
            <thead><tr><th>{t('events.team')}</th><th>{t('events.role')}</th><th>{t('events.code')}</th><th>{t('events.version')}</th><th></th></tr></thead>
            <tbody>
              {links.map((l) => (
                <tr key={l.id}>
                  <td>{l.team?.name}</td>
                  <td>{l.role}</td>
                  <td>{l.onboardingCode?.name || l.onboardingCode?.onboardingCode || '-'}</td>
                  <td>v{l.tokenVersion}</td>
                  <td>
                    <button onClick={() => call(() => eventsAPI.revokeLink(l.id), (res) => setNewLink(res.data)).then(() => openLinks(openLinksFor))}>{t('events.revoke')}</button>
                    <button className="events-danger" onClick={() => call(() => eventsAPI.deleteLink(l.id)).then(() => openLinks(openLinksFor))}>{t('common.delete')}</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
    </div>
  );
}
