from app.extensions import scheduler
from app.models import UserModel, AnnouncementModel, OneTimeTokenModel, EventSessionModel, db
from datetime import datetime, timedelta
from flask import current_app
from app.ots import otsClient
from app.services.event_team_colors import sync_event_team_colors, remove_event_colors_for_username, read_profiles
import os
import shutil
import time


@scheduler.task(id="remove_expired_accounts", trigger="cron", hour=1, misfire_grace_time=3600, max_instances=1)
#@scheduler.task(id="remove_expired_accounts", trigger="interval", seconds=1) #DEBUG
def remove_expired_accounts():
    print('Removing expired accounts...')
    with scheduler.app.app_context():
        today = datetime.now()
        expired_users = UserModel.query.filter(UserModel.expiryDate < today).all()
        for user in expired_users:
            print(f"Removing user {user.username} with expiry date {user.expiryDate}")
            otsClient.delete_user(user.username)
            db.session.delete(user)
        db.session.commit()
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
            event = session.event
            cleanup = event.accountCleanup if event else 'deactivate'
            username = session.otsUsername
            if username and existing is not None and session.team and session.team.color:
                try:
                    remove_event_colors_for_username(username, session.team.color, existing)
                except Exception as e:
                    print(f"Event colour removal failed for {username}: {e}")
            if username and session.mode != 'individual':
                try:
                    if cleanup == 'delete':
                        otsClient.delete_user(username)
                    else:
                        otsClient.deactivate_user(username)
                    print(f"Event session cleanup: {cleanup} {username}")
                except Exception as e:
                    print(f"Event session cleanup failed for {username}: {e}")
            session.closedAt = now
            session.closeReason = 'expired'
            db.session.commit()
    return
