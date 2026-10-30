import { useState, useEffect, useCallback, useRef } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import { QRCodeSVG } from 'qrcode.react';
import { eventsAPI, groupsAPI, onboardingCodesAPI } from '../../services/api';
import { useAuth } from '../../contexts/AuthContext';
import { useNotification } from '../../contexts/NotificationContext';
import { formatDateTime, useDateFormat } from '../../utils/formatDate';
import '../../components/AdminTable.css';
import './EventsAdmin.css';

// The fixed set of TAK team colours ATAK accepts.
const TAK_TEAM_COLORS = [
  'White', 'Yellow', 'Orange', 'Magenta', 'Red', 'Maroon', 'Purple',
  'Dark Blue', 'Blue', 'Cyan', 'Teal', 'Green', 'Dark Green', 'Brown',
];

// How the device accounts of a link take part in their team's OTS group.
const LINK_DIRECTIONS = [
  ['BOTH', 'events.directionBoth'],
  ['IN', 'events.directionIn'],
  ['OUT', 'events.directionOut'],
];

// How the organizer (HQ) team takes part in the groups of the other teams of an event.
const ORGANIZER_DIRECTIONS = [
  ['OUT', 'events.organizerDirectionOut'],
  ['BOTH', 'events.organizerDirectionBoth'],
  ['IN', 'events.organizerDirectionIn'],
];

export default function EventsAdmin() {
  const { t } = useTranslation();
  const dateFormat = useDateFormat();
  const { hasRole } = useAuth();
  const { showSuccess, showWarning } = useNotification();
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
  const [linkForm, setLinkForm] = useState({ teamId: '', role: 'leader', onboardingCodeId: '', direction: 'BOTH' });
  const [printSheet, setPrintSheet] = useState([]);
  const [printPerPage, setPrintPerPage] = useState(1);
  const [qrLink, setQrLink] = useState(null); // {url, label} shown in the QR panel
  const [accounts, setAccounts] = useState([]);
  const [leaderSessions, setLeaderSessions] = useState([]);
  const [eudsAvailable, setEudsAvailable] = useState(true);
  const [notice, setNotice] = useState('');
  const [linkRemaining, setLinkRemaining] = useState({}); // linkId -> open accounts left after a close
  const [linkRetry, setLinkRetry] = useState({}); // linkId -> last direction save left accounts unchanged

  const linksPanelRef = useRef(null);

  // Bring the links panel into view when an event's links are opened
  useEffect(() => {
    if (openLinksFor) linksPanelRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }, [openLinksFor]);

  const openEvent = events.find((ev) => ev.id === openLinksFor);
  const hqTeamId = openEvent?.organizerTeamId ?? null;
  // Organizer candidates: teams with a link in this event and a usable OTS group
  const organizerOptions = teams.filter((tm) => (tm.id === hqTeamId
    || (links.some((l) => l.eventTeamId === tm.id) && tm.otsGroup && !tm.otsGroup.missingInOts)));
  // HQ links grant visibility of every team: mark them on screen and on the print sheet
  const printItem = (l, url) => ({ id: l.id, url, label: `${l.team?.name || ''} · ${l.role}`, hq: l.eventTeamId === hqTeamId });

  const addGenerated = (d) => {
    setNewLink(d);
    setPrintSheet((p) => [...p.filter((x) => x.id !== d.id), printItem(d, d.url)]);
  };

  // Organizer (HQ) devices re-provisioned in OTS by a save; quiet when nothing changed
  const notifyHq = (data, okKey = 'events.hqReprovisioned', failKey = 'events.hqReprovisionFailed', quiet = true) => {
    const { reprovisioned = 0, reprovisionFailed = 0 } = data || {};
    if (reprovisionFailed > 0) {
      showWarning(t(failKey, { done: reprovisioned, failed: reprovisionFailed }), 12000);
    } else if (!quiet || reprovisioned > 0) {
      showSuccess(t(okKey, { done: reprovisioned }));
    }
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

  const loadLeaderSessions = async (eventId) => {
    try {
      const r = await eventsAPI.listLeaderSessions(eventId);
      setLeaderSessions(r.data.leaderSessions || []);
    } catch { setLeaderSessions([]); }
  };

  // Ends one person's leader session; the link and the devices they created stay
  const revokeLeaderSession = (s) => {
    if (!window.confirm(t('events.leaderSessionRevokeConfirm', { name: s.displayName }))) return;
    call(() => eventsAPI.revokeLeaderSession(openLinksFor, s.id)).then(() => loadLeaderSessions(openLinksFor));
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
    if (!window.confirm(t(l.role === 'leader' ? 'events.closeLeaderLinkAccountsConfirm' : 'events.closeLinkAccountsConfirm'))) return;
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
    setLinkRetry({});
    setAccounts([]);
    setLeaderSessions([]);
    loadAccounts(eventId);
    loadLeaderSessions(eventId);
    setNewLink(null);
    setPrintSheet([]);
    setQrLink(null);
    await loadLinks(eventId);
  };

  const showQr = async (l) => {
    setError('');
    try {
      const r = await eventsAPI.getLinkUrl(l.id);
      setQrLink(printItem(l, r.data.url));
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
        items.push(printItem(l, r.data.url));
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
    call(() => eventsAPI.revokeLink(l.id), (res) => { setQrLink(null); addGenerated(res.data); })
      .then(() => { loadLinks(openLinksFor); loadLeaderSessions(openLinksFor); });
  };

  const createLink = () => {
    const payload = { teamId: Number(linkForm.teamId), role: linkForm.role, direction: linkForm.direction };
    if (linkForm.role === 'player' && linkForm.onboardingCodeId) payload.onboardingCodeId = Number(linkForm.onboardingCodeId);
    call(
      () => eventsAPI.createLink(openLinksFor, payload),
      (res) => { addGenerated(res.data); notifyHq(res.data); },
    ).then(() => loadLinks(openLinksFor));
  };

  const applyOrganizer = (changes) => call(() => eventsAPI.updateEvent(openLinksFor, changes),
    (res) => notifyHq(res.data, 'events.organizerReprovisioned', 'events.organizerReprovisionFailed', false));

  const changeOrganizer = (changes) => {
    if (!window.confirm(t('events.organizerChangeConfirm'))) return;
    applyOrganizer(changes);
  };

  // Saving the current direction again (Re-apply) retries the accounts that failed
  const applyDirection = (l, direction) => {
    call(() => eventsAPI.updateLink(l.id, { direction }), (res) => {
      const { reprovisioned = 0, reprovisionFailed = 0 } = res.data;
      setLinkRetry((p) => ({ ...p, [l.id]: reprovisionFailed > 0 }));
      if (reprovisionFailed > 0) {
        showWarning(t('events.directionReprovisionFailed', { done: reprovisioned, failed: reprovisionFailed }), 12000);
      } else {
        showSuccess(t('events.directionReprovisioned', { done: reprovisioned }));
      }
    }).then(() => loadLinks(openLinksFor));
  };

  const changeDirection = (l, direction) => {
    if (!window.confirm(t('events.directionChangeConfirm'))) return;
    applyDirection(l, direction);
  };

  return (
    <div className="admin-page events-admin">
      <div className="admin-header">
        <div>
          <h1>{t('events.title')}</h1>
          <p className="events-intro">{t('events.intro')}</p>
        </div>
      </div>
      {error && <div className="alert alert-error">{error}</div>}

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
          <button className="btn btn-sm btn-primary" onClick={createTeam} disabled={!teamForm.name}>{t('events.addTeam')}</button>
        </div>
        <div className="admin-table-container">
          <table className="admin-table">
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
                        <button className="btn btn-sm btn-secondary" onClick={() => setLeader(team.id)}>{t('events.setLeader')}</button>
                      </span>
                    )}
                  </td>
                  <td><button className="btn btn-sm btn-danger" onClick={() => call(() => eventsAPI.deleteTeam(team.id), (res) => notifyHq(res.data))}>{t('common.delete')}</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
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
          <button className="btn btn-sm btn-primary" onClick={createEvent} disabled={!eventForm.name || (eventForm.playerAccessMode === 'shared' && !eventForm.validUntil)}>{t('events.addEvent')}</button>
        </div>
        {eventForm.playerAccessMode === 'shared' && !eventForm.validUntil && (
          <p className="events-hint">{t('events.sharedNeedsEnd')}</p>
        )}
        <div className="admin-table-container">
          <table className="admin-table">
            <thead><tr><th>{t('events.eventName')}</th><th>{t('events.validUntil')}</th><th>{t('events.playerMode')}</th><th>{t('events.cleanup')}</th><th></th></tr></thead>
            <tbody>
              {events.map((ev) => (
                <tr key={ev.id} className={ev.id === openLinksFor ? 'events-row-selected' : undefined}>
                  <td>{ev.name}</td>
                  <td>{ev.validUntil ? formatDateTime(ev.validUntil, dateFormat) : t('common.never')}</td>
                  <td>{ev.playerAccessMode}</td>
                  <td>{ev.accountCleanup}</td>
                  <td>
                    <div className="table-actions">
                      <button
                        type="button"
                        className={`btn btn-sm ${ev.id === openLinksFor ? 'btn-info' : 'btn-secondary'}`}
                        aria-expanded={ev.id === openLinksFor}
                        aria-controls="events-links-panel"
                        onClick={() => (ev.id === openLinksFor ? setOpenLinksFor(null) : openLinks(ev.id))}
                      >
                        <span aria-hidden="true">{ev.id === openLinksFor ? '▾' : '▸'}</span> {t('events.links')}
                      </button>
                      <button className="btn btn-sm btn-danger" onClick={() => call(() => eventsAPI.deleteEvent(ev.id), () => {
                        if (ev.id === openLinksFor) setOpenLinksFor(null);
                      })}>{t('common.delete')}</button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      {/* Links for the selected event: framed panel directly under the events table */}
      {openLinksFor && (
        <div id="events-links-panel" className="events-links-panel" ref={linksPanelRef}>
          <div className="events-links-panel-header">
            <h2>{t('events.linksFor', { name: openEvent?.name || `#${openLinksFor}` })}</h2>
            <button type="button" className="btn btn-sm btn-secondary" onClick={() => setOpenLinksFor(null)}>{t('common.close')}</button>
          </div>
          <div className="events-form-row">
            <label>{t('events.organizerTeam')}
              <select
                value={hqTeamId ?? ''}
                disabled={!canEdit}
                onChange={(e) => changeOrganizer({ organizerTeamId: e.target.value === '' ? null : Number(e.target.value) })}
              >
                <option value="">{t('events.organizerNone')}</option>
                {organizerOptions.map((tm) => <option key={tm.id} value={tm.id}>{tm.name}</option>)}
              </select>
            </label>
            <label>{t('events.organizerDirection')}
              <select
                value={openEvent?.organizerDirection || 'OUT'}
                disabled={!canEdit}
                onChange={(e) => changeOrganizer({ organizerDirection: e.target.value })}
              >
                {ORGANIZER_DIRECTIONS.map(([value, key]) => <option key={value} value={value}>{t(key)}</option>)}
              </select>
            </label>
            {canEdit && (
              <button
                type="button"
                className="btn btn-sm btn-secondary"
                title={t('events.reapplyHint')}
                onClick={() => applyOrganizer({ organizerTeamId: hqTeamId, organizerDirection: openEvent?.organizerDirection || 'OUT' })}
              >
                {t('events.reapply')}
              </button>
            )}
          </div>
          <p className="events-hint">{t('events.organizerHint')}</p>
          {openEvent?.playerAccessMode === 'individual' && <p className="events-hint">{t('events.organizerIndividualHint')}</p>}
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
            <select aria-label={t('events.direction')} value={linkForm.direction} onChange={(e) => setLinkForm({ ...linkForm, direction: e.target.value })}>
              {LINK_DIRECTIONS.map(([value, key]) => <option key={value} value={value}>{t(key)}</option>)}
            </select>
            <button className="btn btn-sm btn-primary" onClick={createLink} disabled={!linkForm.teamId}>{t('events.generateLink')}</button>
          </div>
          <p className="events-hint">{t('events.directionHint')} {t('admin.groups.directionHelp')}</p>
          <div className="admin-table-container">
            <table className="admin-table">
              <thead><tr><th>{t('events.team')}</th><th>{t('events.role')}</th><th>{t('events.direction')}</th><th>{t('events.code')}</th><th>{t('events.version')}</th><th></th></tr></thead>
              <tbody>
                {links.map((l) => (
                  <tr key={l.id}>
                    <td>
                      {l.team?.name}
                      {l.eventTeamId === hqTeamId && <span className="badge badge-primary" style={{ marginLeft: '0.5rem' }} title={t('events.hqPrintLabel')}>{t('events.hqBadge')}</span>}
                    </td>
                    <td>{l.role}</td>
                    <td>
                      {canEdit ? (
                        <>
                          <select aria-label={t('events.direction')} value={l.direction || 'BOTH'} onChange={(e) => changeDirection(l, e.target.value)}>
                            {LINK_DIRECTIONS.map(([value, key]) => <option key={value} value={value}>{t(key)}</option>)}
                          </select>
                          {linkRetry[l.id] && (
                            <button type="button" className="btn btn-sm btn-secondary" title={t('events.reapplyHint')} onClick={() => applyDirection(l, l.direction || 'BOTH')}>
                              {t('events.reapply')}
                            </button>
                          )}
                        </>
                      ) : t((LINK_DIRECTIONS.find(([value]) => value === l.direction) || LINK_DIRECTIONS[0])[1])}
                    </td>
                    <td>{l.onboardingCode?.name || l.onboardingCode?.onboardingCode || '-'}</td>
                    <td>v{l.tokenVersion}</td>
                    <td>
                      <div className="table-actions">
                        <button className="btn btn-sm btn-success" onClick={() => showQr(l)} disabled={!l.retrievable} title={l.retrievable ? '' : t('events.regenerateToShowQr')}>{t('events.qrLink')}</button>
                        <button className="btn btn-sm btn-warning" onClick={() => regenerate(l)}>{t('events.regenerate')}</button>
                        {canEdit && (
                          <button className="btn btn-sm btn-danger" onClick={() => closeLinkAccounts(l)}>{linkRemaining[l.id] > 0 ? t('events.closeLinkAccountsMore', { remaining: linkRemaining[l.id] }) : t('events.closeLinkAccounts')}</button>
                        )}
                        <button className="btn btn-sm btn-danger" onClick={() => call(() => eventsAPI.deleteLink(l.id), (res) => {
                        notifyHq(res.data);
                        setPrintSheet((p) => p.filter((x) => x.id !== l.id));
                        setQrLink((q) => (q?.id === l.id ? null : q));
                      }).then(() => { loadLinks(openLinksFor); loadLeaderSessions(openLinksFor); })}>{t('common.delete')}</button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {qrLink && (
            <div className="events-new-link events-qr-panel">
              <strong>{qrLink.label}</strong>
              {qrLink.hq && <span className="events-print-hq">{t('events.hqPrintLabel')}</span>}
              <QRCodeSVG value={qrLink.url} size={200} level="M" />
              <code>{qrLink.url}</code>
              <div className="table-actions">
                <button className="btn btn-sm btn-success" onClick={() => navigator.clipboard?.writeText(qrLink.url)}>{t('events.copyLink')}</button>
                <button className="btn btn-sm btn-secondary" onClick={() => setQrLink(null)}>{t('common.close')}</button>
              </div>
            </div>
          )}
          <div className="events-form-row">
            <button className="btn btn-sm btn-secondary" onClick={printAll} disabled={!links.some((l) => l.retrievable)}>{t('events.printAllQrs')}</button>
            <select value={printPerPage} onChange={(e) => setPrintPerPage(Number(e.target.value))} aria-label={t('events.printLayout')}>
              <option value={1}>{t('events.printOnePerPage')}</option>
              <option value={2}>{t('events.printTwoPerPage')}</option>
            </select>
          </div>

          {/* People who joined through a leader link of the selected event */}
          <h3>{t('events.leaderSessions')}</h3>
          <p className="events-hint">{t('events.leaderSessionsNotice')}</p>
          <div className="events-form-row">
            <button className="btn btn-sm btn-secondary" onClick={() => loadLeaderSessions(openLinksFor)}>{t('events.refresh')}</button>
          </div>
          <div className="admin-table-container">
            <table className="admin-table">
              <thead><tr><th>{t('events.leaderSessionName')}</th><th>{t('events.team')}</th><th>{t('events.leaderSessionJoined')}</th><th>{t('events.leaderSessionDevices')}</th><th>{t('events.accountStatus')}</th>{canEdit && <th></th>}</tr></thead>
              <tbody>
                {leaderSessions.length === 0 && <tr><td colSpan={canEdit ? 6 : 5}>{t('events.leaderSessionsEmpty')}</td></tr>}
                {leaderSessions.map((s) => (
                  <tr key={s.id}>
                    <td>{s.displayName}</td>
                    <td>{s.team?.name || '-'}</td>
                    <td>{formatDateTime(s.createdAt, dateFormat, { utc: true }) || '-'}</td>
                    <td>{s.deviceCount}</td>
                    <td>{s.status === 'revoked'
                      ? t('events.leaderSessionRevokedAt', { date: formatDateTime(s.revokedAt, dateFormat) })
                      : t(s.status === 'ended' ? 'events.leaderSessionEnded' : 'events.accountActive')}</td>
                    {canEdit && (
                      <td>
                        {!s.revokedAt && (
                          <div className="table-actions">
                            <button className="btn btn-sm btn-danger" onClick={() => revokeLeaderSession(s)}>{t('events.leaderSessionRevoke')}</button>
                          </div>
                        )}
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Device accounts of the selected event */}
          <h3>{t('events.accounts')}</h3>
          <p className="events-hint">{t('events.accountsNotice')}</p>
          {notice && <p className="events-hint">{notice}</p>}
          {!eudsAvailable && <p className="events-hint">{t('events.eudsUnavailable')}</p>}
          <div className="events-form-row">
            <button className="btn btn-sm btn-secondary" onClick={() => loadAccounts(openLinksFor)}>{t('events.refresh')}</button>
          </div>
          <div className="admin-table-container">
            <table className="admin-table">
              <thead><tr><th>{t('events.accountCallsign')} / {t('events.accountUsername')}</th><th>{t('events.team')}</th><th>{t('events.accountStatus')}</th><th>{t('events.accountCreated')}</th>{canEdit && <th></th>}</tr></thead>
              <tbody>
                {accounts.length === 0 && <tr><td colSpan={canEdit ? 5 : 4}>{t('events.accountsEmpty')}</td></tr>}
                {accounts.map((a) => (
                  <tr key={a.id}>
                    <td>
                      {(a.euds || []).map((e) => e.callsign).filter(Boolean).join(', ') || '-'}
                      <div className="events-hint">{a.otsUsername}</div>
                    </td>
                    <td>
                      {a.parked ? '-' : (a.team?.name || '-')}
                      {!a.parked && a.team && a.team.id === hqTeamId && <span className="badge badge-primary" style={{ marginLeft: '0.5rem' }}>{t('events.hqBadge')}</span>}
                    </td>
                    <td>
                      {a.legacy ? t('events.accountLegacy') : (a.parked ? t('events.accountParked') : t('events.accountActive'))}
                      {a.role === 'leader' && <span className="badge badge-primary" style={{ marginLeft: '0.5rem' }}>{t('events.accountLeader')}</span>}
                      {a.leaderName && <div className="events-hint">{t('events.enrolledBy', { name: a.leaderName })}</div>}
                    </td>
                    <td>{formatDateTime(a.createdAt, dateFormat, { utc: true }) || '-'}</td>
                    {canEdit && (
                      <td>
                        {!a.legacy && (
                          <div className="table-actions">
                            <select
                              aria-label={t('events.moveTo')}
                              value={a.parked ? '' : (a.team?.id ?? '')}
                              onChange={(e) => moveAccount(a, e.target.value)}
                            >
                              <option value="">{t('events.parkNoTeam')}</option>
                              {teams.map((tm) => <option key={tm.id} value={tm.id}>{tm.id === hqTeamId ? `${tm.name} (${t('events.hqBadge')})` : tm.name}</option>)}
                            </select>
                            <button className="btn btn-sm btn-danger" onClick={() => closeAccount(a)}>{t('events.closeAccount')}</button>
                          </div>
                        )}
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {printSheet.length > 0 && (
            <div className="events-print-sheet">
              <div className="events-print-actions no-print">
                <button className="btn btn-sm btn-primary" onClick={() => window.print()}>{t('events.printSheet')}</button>
                <button className="btn btn-sm btn-secondary" onClick={() => setPrintSheet([])}>{t('events.clearSheet')}</button>
              </div>
              <div className="events-print-grid">
                {printSheet.map((item, i) => (
                  <div key={i} className="events-print-card">
                    <QRCodeSVG value={item.url} size={200} level="M" />
                    <div className="events-print-label">{item.label}</div>
                    {item.hq && <div className="events-print-label events-print-hq">{t('events.hqPrintLabel')}</div>}
                  </div>
                ))}
              </div>
              {/* Printed copy: a direct child of <body> so pages break cleanly */}
              {createPortal(
                <div className={`events-print-root per-page-${printPerPage}`}>
                  {printSheet.map((item, i) => (
                    <div key={i} className="events-print-page-card">
                      <div className="events-print-label">{item.label}</div>
                      {item.hq && <div className="events-print-label events-print-hq">{t('events.hqPrintLabel')}</div>}
                      <QRCodeSVG value={item.url} size={512} level="M" marginSize={2} />
                    </div>
                  ))}
                </div>,
                document.body
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
