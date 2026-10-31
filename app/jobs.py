from app.extensions import scheduler
from app.models import UserModel, AnnouncementModel, OneTimeTokenModel, EventSessionModel, SystemSettingsModel, db
from datetime import datetime, timedelta
from flask import current_app
from app.ots import otsClient, delete_user_if_present
from app.settings import MAIL_ENABLED
from app.services.event_team_colors import sync_event_team_colors, remove_event_colors_for_username, read_profiles
import json
import os
import shutil
import smtplib
import time

# Give up on OTS cleanup of an expired event session after this long
EVENT_CLEANUP_GIVE_UP = timedelta(days=7)


@scheduler.task(id="remove_expired_accounts", trigger="cron", hour=1, misfire_grace_time=3600, max_instances=1)
#@scheduler.task(id="remove_expired_accounts", trigger="interval", seconds=1) #DEBUG
def remove_expired_accounts():
    print('Removing expired accounts...')
    with scheduler.app.app_context():
        if SystemSettingsModel.get_setting('remove_expired_accounts_enabled', False) is not True:
            return
        today = datetime.now()
        expired_users = []
        for user in UserModel.query.filter(UserModel.expiryDate < today).all():
            if any(r.name in ('administrator', 'admin') for r in user.roles):
                print(f"Expired accounts: skipping administrator {user.username}")
                continue
            expired_users.append(user)
        print(f"Expired accounts to remove: {len(expired_users)} {[u.username for u in expired_users]}")
        for user in expired_users:
            username = user.username
            try:
                # Flush first so a local delete that cannot succeed (FK constraints) is
                # found before OTS is touched; then OTS, then commit. A failure keeps
                # the user and is retried next night.
                from app.api_v1.events import remove_user_leader_designations
                remove_user_leader_designations(user.id)
                UserModel.clear_references(user.id)
                db.session.delete(user)
                db.session.flush()
                delete_user_if_present(otsClient, username)
                db.session.commit()
                print(f"Removed expired user {username}")
            except Exception as e:
                db.session.rollback()
                print(f"Failed to remove expired user {username}: {e}")
                current_app.logger.error(f"Failed to remove expired user {username}: {e}")
    return


# Minutes to wait after the 1st, 2nd, ... failed email run; the 6th failed run is terminal
EMAIL_RETRY_MINUTES = [1, 5, 15, 60, 240]
EMAIL_MAX_ATTEMPTS = len(EMAIL_RETRY_MINUTES) + 1


def _is_recipient_error(e):
    """True when a send error concerns only this recipient (drop it, go on) rather than the run"""
    if isinstance(e, smtplib.SMTPSenderRefused):
        return False
    if isinstance(e, (smtplib.SMTPRecipientsRefused, smtplib.SMTPNotSupportedError, ValueError)):
        return True
    return isinstance(e, smtplib.SMTPResponseException) and 500 <= e.smtp_code < 600


def _send_announcement_email_run(announcement):
    """One delivery run for an announcement; sends only to the still-pending recipients.

    Progress is committed after every recipient so a mail is never repeated.
    """
    from app.api_v1.announcements import _send_announcement_email_to_user, _load_id_list
    from app.notifications import get_frontend_url_safe

    frontend_url = get_frontend_url_safe()
    # Lease: push the next attempt out before sending anything, so a crash or a failing
    # progress commit mid-run cannot make the next run re-mail recipients.
    attempts = announcement.email_attempts or 0
    announcement.email_next_attempt_at = datetime.now() + timedelta(
        minutes=EMAIL_RETRY_MINUTES[min(attempts, len(EMAIL_RETRY_MINUTES) - 1)])
    db.session.commit()

    pending = _load_id_list(announcement.email_pending_user_ids)
    failures = _load_id_list(announcement.email_failures)

    for user_id in list(pending):
        user = UserModel.get_user_by_id(user_id)
        if user is not None and user.email:
            try:
                _send_announcement_email_to_user(announcement, user, frontend_url)
            except Exception as e:
                if not _is_recipient_error(e):
                    raise
                failures.append({'userId': user_id, 'error': str(e)[:500]})
                announcement.email_failures = json.dumps(failures)
        # sent, refused, deleted or without an address: no longer pending
        pending.remove(user_id)
        announcement.email_pending_user_ids = json.dumps(pending)
        db.session.commit()

    announcement.email_status = 'sent'
    announcement.email_last_error = None
    announcement.email_next_attempt_at = None
    db.session.commit()


@scheduler.task(id="send_scheduled_announcements", trigger="interval", minutes=1, misfire_grace_time=60, max_instances=1)
def send_scheduled_announcements():
    """Publish due scheduled announcements, then deliver pending announcement emails"""
    from app.api_v1.announcements import _queue_announcement_emails
    with scheduler.app.app_context():
        # Phase A: publish
        for announcement in AnnouncementModel.get_scheduled_due():
            announcement_id = announcement.id
            try:
                print(f"Publishing scheduled announcement: {announcement.title}")
                announcement.status = 'sent'
                announcement.sent_at = datetime.now()
                _queue_announcement_emails(announcement)
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                print(f"Failed to publish announcement {announcement_id}: {e}")

        # Phase B: email delivery
        for announcement in AnnouncementModel.get_email_due():
            announcement_id = announcement.id
            try:
                if not MAIL_ENABLED:
                    announcement.email_status = 'skipped'
                    announcement.email_pending_user_ids = json.dumps([])
                    announcement.email_next_attempt_at = None
                    db.session.commit()
                    print(f"Announcement {announcement_id}: email disabled, skipping delivery")
                    continue
                print(f"Sending emails for announcement: {announcement.title}")
                _send_announcement_email_run(announcement)
            except Exception as e:
                db.session.rollback()
                try:
                    announcement = AnnouncementModel.get_by_id(announcement_id)
                    attempts = (announcement.email_attempts or 0) + 1
                    announcement.email_attempts = attempts
                    announcement.email_last_error = str(e)[:500]
                    if attempts >= EMAIL_MAX_ATTEMPTS:
                        announcement.email_status = 'failed'
                        announcement.email_next_attempt_at = None
                    else:
                        announcement.email_next_attempt_at = datetime.now() + timedelta(minutes=EMAIL_RETRY_MINUTES[attempts - 1])
                    db.session.commit()
                    print(f"Announcement {announcement_id}: email run failed (attempt {attempts}): {e}")
                except Exception as inner:
                    db.session.rollback()
                    print(f"Could not record email failure for announcement {announcement_id}: {inner}")
    return


@scheduler.task(id="cleanup_temp_downloads", trigger="interval", minutes=15, misfire_grace_time=120, max_instances=1)
def cleanup_temp_downloads():
    """Remove temp download directories older than 15 minutes and expired tokens"""
    from app.api_v1.tak_profiles import DOWNLOAD_TEMP_DIR
    cutoff = time.time() - (15 * 60)
    removed = 0

    if os.path.exists(DOWNLOAD_TEMP_DIR):
        for entry in os.listdir(DOWNLOAD_TEMP_DIR):
            entry_path = os.path.join(DOWNLOAD_TEMP_DIR, entry)
            try:
                if os.path.isdir(entry_path) and os.stat(entry_path).st_mtime < cutoff:
                    shutil.rmtree(entry_path)
                    removed += 1
            except Exception:
                pass

    if removed:
        print(f"Cleaned up {removed} temp download directories")

    with scheduler.app.app_context():
        expired = OneTimeTokenModel.query.filter(
            OneTimeTokenModel.expires_at < datetime.now()
        ).delete()
        db.session.commit()
        if expired:
            print(f"Cleaned up {expired} expired download tokens")


@scheduler.task(id="apply_event_team_colors", trigger="interval", minutes=2, misfire_grace_time=60, max_instances=1)
def apply_event_team_colors():
    """Apply event team colours to the ATAK devices of event participants"""
    with scheduler.app.app_context():
        try:
            sync_event_team_colors()
        except Exception as e:
            print(f"Event team colour sync failed: {e}")
            current_app.logger.exception("Event team colour sync failed")
    return


def cleanup_event_session(session, existing):
    """OTS cleanup of one event session account per the event's accountCleanup policy.

    Shared by the close job and the admin close endpoints. Returns
    (account_ok, colour_ok, groups_ok): account_ok means the account is deactivated
    (or deleted); colour and group removal are best-effort follow-ups.
    existing is the result of read_profiles() or None when that failed.
    """
    event = session.event
    cleanup = event.accountCleanup if event else 'deactivate'
    username = session.otsUsername

    owns_account = bool(username) and session.mode in ('shared', 'device')
    delete_mode = cleanup == 'delete'
    account_ok = True

    # Security-critical step first: in deactivate mode the account is
    # deactivated before anything else, so a colour failure can never
    # keep it usable.
    if owns_account and not delete_mode:
        try:
            otsClient.deactivate_user(username)
            print(f"Event session cleanup: deactivate {username}")
        except Exception as e:
            account_ok = False
            print(f"Event session cleanup failed for {username}: {e}")

    colour_ok = True
    if username and session.team and session.team.color:
        if existing is None:
            colour_ok = False
        else:
            try:
                colour_ok = remove_event_colors_for_username(username, session.team.color, existing)
            except Exception as e:
                colour_ok = False
                print(f"Event colour removal failed for {username}: {e}")

    if owns_account and delete_mode:
        # The account is only deleted once the colours are gone; until
        # then (or if the delete fails) it is deactivated so it is
        # never left usable while the job retries.
        deleted = False
        if colour_ok:
            try:
                delete_user_if_present(otsClient, username)
                deleted = True
                print(f"Event session cleanup: delete {username}")
            except Exception as e:
                print(f"Event session cleanup failed for {username}: {e}")
        if not deleted:
            account_ok = False
            try:
                otsClient.deactivate_user(username)
                print(f"Event session cleanup: deactivate {username} (delete pending)")
            except Exception as e:
                print(f"Event session deactivation failed for {username}: {e}")

    # Group membership is best-effort and only needed when the account
    # is kept (teams are reused across events); a failure here is
    # retried but never blocks or undoes the steps above.
    groups_ok = True
    if owns_account and account_ok and session.mode == 'device' and not delete_mode:
        from app.api_v1.events import session_group_memberships  # lazy: avoids a circular import
        from app.api_v1.groups import _ots_status_code
        for group_name, direction in sorted(session_group_memberships(session)):
            try:
                otsClient.remove_user_from_group(username, group_name, direction)
            except Exception as e:
                if _ots_status_code(e) == 404:
                    continue  # group is gone, so is the membership
                groups_ok = False
                print(f"Event session group removal failed for {username}: {e}")
    return account_ok, colour_ok, groups_ok


@scheduler.task(id="close_expired_event_sessions", trigger="interval", minutes=15, misfire_grace_time=300, max_instances=1)
def close_expired_event_sessions():
    """Close event-player sessions whose event has ended.

    The team colour profiles on the session account's devices are removed first.
    Shared and device sessions own a temporary OTS account (device accounts are
    also removed from their team's OTS group first); clean it up per the event's
    accountCleanup policy (deactivate by default, or delete). Individual
    sessions belong to a normal self-registered account, which is left alone;
    only the session is closed. Leader accounts are persistent shared logins,
    not EventSessions, so they are untouched here.
    Afterwards the per-event parked OTS group is removed for events that have no
    open device session left (best-effort), and open device accounts whose live
    group change failed are re-provisioned (reconcile_device_sessions).
    """
    print('Closing expired event sessions...')
    with scheduler.app.app_context():
        now = datetime.now()
        sessions = EventSessionModel.query.filter(
            EventSessionModel.closedAt.is_(None),
            EventSessionModel.expiresAt.isnot(None),
            EventSessionModel.expiresAt < now,
        ).all()
        existing = None
        if sessions:
            # Read the OTS profiles once for all sessions of this run
            try:
                existing = read_profiles()
            except Exception as e:
                print(f"Event colour removal skipped, could not read OTS profiles: {e}")
        closed_event_ids = set()
        for session in sessions:
            session_id = session.id
            try:
                username = session.otsUsername
                event_id = session.eventId
                account_ok, colour_ok, groups_ok = cleanup_event_session(session, existing)
                account_ok = account_ok and groups_ok

                if colour_ok and account_ok:
                    session.closeReason = 'expired'
                elif now - session.expiresAt > EVENT_CLEANUP_GIVE_UP:
                    session.closeReason = 'expired (OTS cleanup failed)'
                    print(f"Event session {session.id} ({username}): giving up OTS cleanup")
                    current_app.logger.error(
                        f"Event session {session.id} ({username}): OTS cleanup failed, closing anyway")
                else:
                    print(f"Event session {session.id} ({username}): OTS cleanup incomplete, will retry")
                    continue
                session.closedAt = now
                db.session.commit()
                closed_event_ids.add(event_id)
            except Exception as e:
                db.session.rollback()
                print(f"Closing event session {session_id} failed: {e}")
        if closed_event_ids:
            from app.api_v1.events import cleanup_parked_group  # lazy: avoids a circular import
            for event_id in closed_event_ids:
                cleanup_parked_group(event_id)

        # Heal device accounts whose live group change failed (best effort, idempotent)
        try:
            from app.api_v1.events import reconcile_device_sessions  # lazy: avoids a circular import
            done, failed = reconcile_device_sessions()
            if done or failed:
                current_app.logger.info(
                    f"Event device group reconcile: {done} re-provisioned, {failed} failed (retried next run)")
        except Exception as e:
            db.session.rollback()
            print(f"Event device group reconcile failed: {e}")
            current_app.logger.exception("Event device group reconcile failed")
    return
