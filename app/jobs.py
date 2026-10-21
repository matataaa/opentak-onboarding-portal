from app.extensions import scheduler
from app.models import UserModel, AnnouncementModel, OneTimeTokenModel, EventSessionModel, SystemSettingsModel, db
from datetime import datetime, timedelta
from flask import current_app
from app.ots import otsClient
from app.services.event_team_colors import sync_event_team_colors, remove_event_colors_for_username, read_profiles
import os
import shutil
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
                db.session.delete(user)
                db.session.flush()
                otsClient.delete_user(username)
                db.session.commit()
                print(f"Removed expired user {username}")
            except Exception as e:
                db.session.rollback()
                print(f"Failed to remove expired user {username}: {e}")
                current_app.logger.error(f"Failed to remove expired user {username}: {e}")
    return


@scheduler.task(id="send_scheduled_announcements", trigger="interval", minutes=1, misfire_grace_time=60, max_instances=1)
def send_scheduled_announcements():
    """Check for and send scheduled announcements"""
    print('Checking for scheduled announcements...')
    with scheduler.app.app_context():
        due_announcements = AnnouncementModel.get_scheduled_due()

        for announcement in due_announcements:
            print(f"Sending scheduled announcement: {announcement.title}")
            try:
                announcement.status = 'sent'
                announcement.sent_at = datetime.now()
                db.session.commit()

                # Send emails if enabled
                if announcement.send_email:
                    from app.api_v1.announcements import _send_announcement_emails
                    _send_announcement_emails(announcement)

                print(f"Successfully sent announcement: {announcement.title}")
            except Exception as e:
                print(f"Failed to send announcement {announcement.id}: {e}")
                db.session.rollback()
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


@scheduler.task(id="close_expired_event_sessions", trigger="interval", minutes=15, misfire_grace_time=300, max_instances=1)
def close_expired_event_sessions():
    """Close event-player sessions whose event has ended.

    The team colour profiles on the session account's devices are removed first.
    Shared sessions own a temporary OTS account; clean it up per the event's
    accountCleanup policy (deactivate by default, or delete). Individual
    sessions belong to a normal self-registered account, which is left alone;
    only the session is closed. Leader accounts are persistent shared logins,
    not EventSessions, so they are untouched here.
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
        for session in sessions:
            session_id = session.id
            try:
                event = session.event
                cleanup = event.accountCleanup if event else 'deactivate'
                username = session.otsUsername

                # Colours first; the account is only touched once they are gone
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

                account_ok = True
                if username and session.mode == 'shared' and colour_ok:
                    try:
                        if cleanup == 'delete':
                            otsClient.delete_user(username)
                        else:
                            otsClient.deactivate_user(username)
                        print(f"Event session cleanup: {cleanup} {username}")
                    except Exception as e:
                        account_ok = False
                        print(f"Event session cleanup failed for {username}: {e}")
                elif username and session.mode == 'shared':
                    account_ok = False

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
            except Exception as e:
                db.session.rollback()
                print(f"Closing event session {session_id} failed: {e}")
    return
