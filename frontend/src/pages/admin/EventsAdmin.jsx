import { useState, useEffect, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { QRCodeSVG } from 'qrcode.react';
import { eventsAPI, groupsAPI, onboardingCodesAPI } from '../../services/api';
import { useAuth } from '../../contexts/AuthContext';
import { formatUtc } from '../../utils/formatUtc';
import './EventsAdmin.css';

// The fixed set of TAK team colours ATAK accepts.
const TAK_TEAM_COLORS = [
  'White', 'Yellow', 'Orange', 'Magenta', 'Red', 'Maroon', 'Purple',
  'Dark Blue', 'Blue', 'Cyan', 'Teal', 'Green', 'Dark Green', 'Brown',
];

function toLocalInput(iso) {
  if (!iso) return '';
  // ISO -> value for <input type="datetime-local">
  const d = new Date(iso);
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export default function EventsAdmin() {
  const { t } = useTranslation();
  const { hasRole } = useAuth();
  const canEdit = hasRole('events_admin') || hasRole('administrator');
  const [teams, setTeams] = useState([]);
  const [events, setEvents] = useState([]);
  const [groups, setGroups] = useState([]);
  const [codes, setCodes] = useState([]);
  const [error, setError] = useState('');
  const [newLink, setNewLink] = useState(null); // last generated link (newLink.retrievable: can be re-shown later)

  const [teamForm, setTeamForm] = useState({ name: '', color: '', otsGroupId: '' });
  const [leaderForm, setLeaderForm] = useState({}); // teamId -> {username, password}
  const [eventForm, setEventForm] = useState({
    name: '', validFrom: '', validUntil: '', playerAccessMode: 'individual', accountCleanup: 'deactivate',
  });
  const [openLinksFor, setOpenLinksFor] = useState(null);
  const [links, setLinks] = useState([]);
  const [linkForm, setLinkForm] = useState({ teamId: '', role: 'leader', onboardingCodeId: '' });
  const [printSheet, setPrintSheet] = useState([]);
  const [qrLink, setQrLink] = useState(null); // {url, label} shown in the QR panel
  const [accounts, setAccounts] = useState([]);
  const [eudsAvailable, setEudsAvailable] = useState(true);
  const [notice, setNotice] = useState('');
  const [linkRemaining, setLinkRemaining] = useState({}); // linkId -> open accounts left after a close

  const addGenerated = (d) => {
    setNewLink(d);
    setPrintSheet((p) => [...p.filter((x) => x.id !== d.id), { id: d.id, url: d.url, label: `${d.team?.name || ''} · ${d.role}` }]);
  };

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

  // Refetch only the link list (keeps newLink / printSheet).
  const loadLinks = async (eventId) => {
    try {
      const r = await eventsAPI.listLinks(eventId);
      setLinks(r.data.links || []);
    } catch { setLinks([]); }
  };

  const loadAccounts = async (eventId) => {
    try {
      const r = await eventsAPI.listAccounts(eventId);
      setAccounts(r.data.accounts || []);
      setEudsAvailable(r.data.eudsAvailable !== false);
    } catch (e) {
      setAccounts([]);
      setError(e.response?.data?.error || t('events.loadFailed'));
    }
  };

  const moveAccount = (a, value) => {
    if (value === '' && !window.confirm(t('events.parkConfirm'))) return;
    call(() => eventsAPI.moveAccount(openLinksFor, a.id, value === '' ? null : Number(value)))
      .then(() => loadAccounts(openLinksFor));
  };

  const closeAccount = (a) => {
    if (!window.confirm(t('events.closeAccountConfirm'))) return;
    call(() => eventsAPI.closeAccount(openLinksFor, a.id)).then(() => loadAccounts(openLinksFor));
  };

  const closeLinkAccounts = async (l) => {
    if (!window.confirm(t('events.closeLinkAccountsConfirm'))) return;
    setError('');
    let data;
    try {
      data = (await eventsAPI.closeLinkSessions(l.id)).data;
    } catch (e) {
      data = e.response?.data;
      setError(data?.error || t('events.actionFailed'));
    }
    if (data && typeof data.closed === 'number') {
      setNotice(data.remaining > 0
        ? t('events.closedAccountsRemaining', { closed: data.closed, remaining: data.remaining })
        : t('events.closedAccounts', { closed: data.closed }));
      setLinkRemaining((p) => ({ ...p, [l.id]: data.remaining }));
    }
    loadAccounts(openLinksFor);
  };

  const openLinks = async (eventId) => {
    setOpenLinksFor(eventId);
    setNotice('');
    setLinkRemaining({});
    setAccounts([]);
    loadAccounts(eventId);
    setNewLink(null);
    setPrintSheet([]);
    setQrLink(null);
    await loadLinks(eventId);
  };

  const showQr = async (l) => {
    setError('');
    try {
      const r = await eventsAPI.getLinkUrl(l.id);
      setQrLink({ id: l.id, url: r.data.url, label: `${l.team?.name || ''} · ${l.role}` });
    } catch (e) {
      setError(e.response?.data?.error || t('events.actionFailed'));
    }
  };

  const printAll = async () => {
    setError('');
    try {
      const items = [];
      for (const l of links.filter((x) => x.retrievable)) {
        const r = await eventsAPI.getLinkUrl(l.id);
        items.push({ id: l.id, url: r.data.url, label: `${l.team?.name || ''} · ${l.role}` });
      }
      setPrintSheet(items);
      // Let the sheet render before opening the print dialog.
      setTimeout(() => window.print(), 100);
    } catch (e) {
      setError(e.response?.data?.error || t('events.actionFailed'));
    }
  };

  const regenerate = (l) => {
    if (!window.confirm(t('events.regenerateConfirm'))) return;
    call(() => eventsAPI.revokeLink(l.id), (res) => { setQrLink(null); addGenerated(res.data); }).then(() => loadLinks(openLinksFor));
  };

  const createLink = () => {
    const payload = { teamId: Number(linkForm.teamId), role: linkForm.role };
    if (linkForm.role === 'player' && linkForm.onboardingCodeId) payload.onboardingCodeId = Number(linkForm.onboardingCodeId);
    call(
      () => eventsAPI.createLink(openLinksFor, payload),
      (res) => addGenerated(res.data),
    ).then(() => loadLinks(openLinksFor));
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
          <select value={teamForm.color} onChange={(e) => setTeamForm({ ...teamForm, color: e.target.value })}>
            <option value="">{t('events.noColor')}</option>
            {TAK_TEAM_COLORS.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
          <select value={teamForm.otsGroupId} onChange={(e) => setTeamForm({ ...teamForm, otsGroupId: e.target.value })}>
            <option value="">{t('events.noGroup')}</option>
            {groups.filter((g) => !g.missingInOts).map((g) => <option key={g.id} value={g.id}>{g.displayName || g.name}</option>)}
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
                <td>
                  {team.otsGroup?.name || '-'}
                  {team.otsGroup?.missingInOts && (
                    <span className="badge badge-warning" style={{ marginLeft: '0.5rem' }} title={t('admin.groups.notFoundInOtsHint')}>
                      {t('admin.groups.notFoundInOts')}
                    </span>
                  )}
                </td>
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
          <button onClick={createEvent} disabled={!eventForm.name || (eventForm.playerAccessMode === 'shared' && !eventForm.validUntil)}>{t('events.addEvent')}</button>
        </div>
        {eventForm.playerAccessMode === 'shared' && !eventForm.validUntil && (
          <p className="events-hint">{t('events.sharedNeedsEnd')}</p>
        )}
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
              <strong>{newLink.retrievable ? t('events.linkCreated') : t('events.linkOnce')}</strong>
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
                    <button onClick={() => showQr(l)} disabled={!l.retrievable} title={l.retrievable ? '' : t('events.regenerateToShowQr')}>{t('events.qrLink')}</button>
                    <button onClick={() => regenerate(l)}>{t('events.regenerate')}</button>
                    {canEdit && (
                      <button className="events-danger" onClick={() => closeLinkAccounts(l)}>{linkRemaining[l.id] > 0 ? t('events.closeLinkAccountsMore', { remaining: linkRemaining[l.id] }) : t('events.closeLinkAccounts')}</button>
                    )}
                    <button className="events-danger" onClick={() => call(() => eventsAPI.deleteLink(l.id), () => {
                    setPrintSheet((p) => p.filter((x) => x.id !== l.id));
                    setQrLink((q) => (q?.id === l.id ? null : q));
                  }).then(() => loadLinks(openLinksFor))}>{t('common.delete')}</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="events-form-row">
            <button onClick={printAll} disabled={!links.some((l) => l.retrievable)}>{t('events.printAllQrs')}</button>
          </div>

          {/* Device accounts of the selected event */}
          <h3>{t('events.accounts')}</h3>
          <p className="events-hint">{t('events.accountsNotice')}</p>
          {notice && <p className="events-hint">{notice}</p>}
          {!eudsAvailable && <p className="events-hint">{t('events.eudsUnavailable')}</p>}
          <div className="events-form-row">
            <button onClick={() => loadAccounts(openLinksFor)}>{t('events.refresh')}</button>
          </div>
          <div className="events-table-wrap">
            <table className="events-table">
              <thead><tr><th>{t('events.accountCallsign')} / {t('events.accountUsername')}</th><th>{t('events.team')}</th><th>{t('events.accountStatus')}</th><th>{t('events.accountCreated')}</th>{canEdit && <th></th>}</tr></thead>
              <tbody>
                {accounts.length === 0 && <tr><td colSpan={canEdit ? 5 : 4}>{t('events.accountsEmpty')}</td></tr>}
                {accounts.map((a) => (
                  <tr key={a.id}>
                    <td>
                      {(a.euds || []).map((e) => e.callsign).filter(Boolean).join(', ') || '-'}
                      <div className="events-hint">{a.otsUsername}</div>
                    </td>
                    <td>{a.team?.name || '-'}</td>
                    <td>
                      {a.legacy ? t('events.accountLegacy') : (a.parked ? t('events.accountParked') : t('events.accountActive'))}
                    </td>
                    <td>{formatUtc(a.createdAt)}</td>
                    {canEdit && (
                      <td>
                        {!a.legacy && (
                          <>
                            <select
                              aria-label={t('events.moveTo')}
                              value={a.parked ? '' : (a.team?.id ?? '')}
                              onChange={(e) => moveAccount(a, e.target.value)}
                            >
                              <option value="">{t('events.parkNoTeam')}</option>
                              {teams.map((tm) => <option key={tm.id} value={tm.id}>{tm.name}</option>)}
                            </select>
                            <button className="events-danger" onClick={() => closeAccount(a)}>{t('events.closeAccount')}</button>
                          </>
                        )}
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {qrLink && (
            <div className="events-new-link events-qr-panel">
              <strong>{qrLink.label}</strong>
              <QRCodeSVG value={qrLink.url} size={200} level="M" />
              <code>{qrLink.url}</code>
              <div>
                <button onClick={() => navigator.clipboard?.writeText(qrLink.url)}>{t('events.copyLink')}</button>
                <button onClick={() => setQrLink(null)}>{t('common.close')}</button>
              </div>
            </div>
          )}
          {printSheet.length > 0 && (
            <div className="events-print-sheet">
              <div className="events-print-actions no-print">
                <button onClick={() => window.print()}>{t('events.printSheet')}</button>
                <button onClick={() => setPrintSheet([])}>{t('events.clearSheet')}</button>
              </div>
              <div className="events-print-grid">
                {printSheet.map((item, i) => (
                  <div key={i} className="events-print-card">
                    <QRCodeSVG value={item.url} size={200} level="M" />
                    <div className="events-print-label">{item.label}</div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </section>
      )}
    </div>
  );
}
