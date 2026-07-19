# watchdog.py — RoadSeva SLA Watchdog Engine
# =============================================
# Background service that runs every hour and enforces accountability.
# No human needs to escalate anything. The system does it automatically.
#
# SLA LADDER (48/72/96/120h — GVMC production standard):
#   Hour 0   → Complaint filed → WAS assigned, SLA clock starts
#   Hour 36  → WAS warning SMS (12h before breach)
#   Hour 48  → WAS breach → AE takes over
#   Hour 60  → AE warning SMS (12h before breach)
#   Hour 72  → AE breach → Zonal Commissioner takes over
#   Hour 84  → ZC warning SMS (12h before breach)
#   Hour 96  → ZC breach → Commissioner dashboard red flag
#   Hour 108 → Commissioner warning SMS (12h before max)
#   Hour 120 → Maximum escalation — daily reminders
#
# SECOND DISPUTE:
#   Citizen disputes → AE site visit required (no auto-close)
#   Only AE can force close a disputed ticket
#
# BUGS FIXED (original):
#   WD1: _get_conn() hardcoded SQLite → uses database.get_conn()
#   WD2: submitted_at parsed with %H:%M only → try both formats
#   WD3: log.info(..., count=n) keyword args crash → f-strings
#   WD4: get_sla_dashboard_data() hardcoded SQLite → database.get_conn()
#   WD5: _escalate_ticket() never updated assigned_to → fixed via database layer
#   WD6: escalate_report() updates escalation_level but never reassigns
#        assigned_to to the new responsible staff member — dashboards
#        showed stale ownership after escalation. Fixed in _handle_sla_breach()
#        by writing assigned_to = new_staff[0].name right after escalation.
#
# KNOWN PENDING (tracked in database_py_pending_fixes.md, not yet fixed):
#   - staff table has no phone/mobile column → SMS sends are currently no-ops
#   - get_staff_for_escalation_level() level 1/2 don't filter by division/zone,
#     so every AE/ZC system-wide gets notified for every escalation anywhere.
#     watchdog.py below already passes zone=... at call sites in anticipation
#     of that database.py fix landing — until then, the zone kwarg is harmlessly
#     ignored by the current database.py implementation.

import logging
from datetime import datetime, timezone, timedelta

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    _APSCHEDULER_AVAILABLE = True
except ImportError:
    _APSCHEDULER_AVAILABLE = False

import database

log = logging.getLogger("roadseva.watchdog")

# ── SLA ladder constants ──────────────────────────────────────────────────────
# These match the frozen architecture:
# Level 0 (WAS)  → 48h total
# Level 1 (AE)   → 72h total (24h window after WAS breach)
# Level 2 (ZC)   → 96h total (24h window after AE breach)
# Level 3 (Comm) → 120h total (24h window after ZC breach)

SLA_LEVEL_HOURS = {0: 48, 1: 72, 2: 96, 3: 120}
WARNING_HOURS_BEFORE = 12   # Send warning SMS 12h before each breach

AUTO_CLOSE_HOURS = 72       # Citizen has 72h to respond after work marked done


# ── Time helpers ──────────────────────────────────────────────────────────────

def _now_ist_str() -> str:
    IST = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


def _now_ist_dt() -> datetime:
    IST = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(IST).replace(tzinfo=None)


def _parse_dt(s: str) -> datetime:
    """Try seconds format first, fall back to minutes-only."""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"Cannot parse datetime: {s!r}")


# ── Schema migration ──────────────────────────────────────────────────────────

def init_watchdog_schema():
    """
    Add legacy SLA columns and create sla_breach_log table.
    Safe to call multiple times — idempotent.
    New schema columns (sla_due_at, escalation_level, escalation_count)
    are handled by database.py _safe_add_columns().
    """
    conn = database.get_conn()
    c    = conn.cursor()
    if database.USE_POSTGRES:
        try:
            conn.rollback()
            conn.commit()
        except Exception:
            pass

    # Legacy columns — kept for backward compat with old reports in DB
    legacy_cols = [
        ("sla_expiry_time",  "TEXT DEFAULT NULL"),
        ("sla_warning",      "INTEGER DEFAULT 0"),
    ]
    for col, defn in legacy_cols:
        try:
            c.execute(f"ALTER TABLE reports ADD COLUMN {col} {defn}")
            conn.commit()
        except Exception:
            try: conn.rollback()
            except: pass

    # Create sla_breach_log table
    if database.USE_POSTGRES:
        c.execute("""
            CREATE TABLE IF NOT EXISTS sla_breach_log (
                id               SERIAL PRIMARY KEY,
                report_id        TEXT NOT NULL,
                breached_at      TEXT NOT NULL,
                from_status      TEXT NOT NULL,
                hours_overdue    INTEGER NOT NULL,
                escalation_level TEXT NOT NULL,
                ward             TEXT,
                severity         TEXT,
                division_name    TEXT
            )
        """)
    else:
        c.execute("""
            CREATE TABLE IF NOT EXISTS sla_breach_log (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id        TEXT NOT NULL,
                breached_at      TEXT NOT NULL,
                from_status      TEXT NOT NULL,
                hours_overdue    INTEGER NOT NULL,
                escalation_level TEXT NOT NULL,
                ward             TEXT,
                severity         TEXT,
                division_name    TEXT
            )
        """)
        # Protect audit trail
        c.execute("""
            CREATE TRIGGER IF NOT EXISTS protect_sla_breach_log_delete
            BEFORE DELETE ON sla_breach_log
            BEGIN
                SELECT RAISE(ABORT, 'SLA breach log is append-only');
            END
        """)
        c.execute("""
            CREATE TRIGGER IF NOT EXISTS protect_sla_breach_log_update
            BEFORE UPDATE ON sla_breach_log
            BEGIN
                SELECT RAISE(ABORT, 'SLA breach log is append-only');
            END
        """)

    conn.commit()
    conn.close()
    log.info("[watchdog] schema ready")


# ── Core SLA engine ───────────────────────────────────────────────────────────

def run_sla_engine():
    """
    Main watchdog job — runs every hour.

    For each escalation level (0→3):
      1. Find reports whose SLA warning window is approaching → send warning SMS
      2. Find reports whose SLA is breached → escalate to next level

    Uses database.py SLA functions directly:
      get_sla_warning_reports(level, warning_hours)
      get_sla_breached_reports(level)
      escalate_report(report_id, from_level)
      log_sla_event(...)
      get_staff_for_escalation_level(level, ward, zone)
    """
    now_str   = _now_ist_str()
    warned    = 0
    escalated = 0

    # Process each SLA level
    for level in [0, 1, 2, 3]:

        # ── Step 1: Warnings (12h before breach) ──────────────────────────────
        try:
            warning_reports = database.get_sla_warning_reports(
                level, warning_hours=WARNING_HOURS_BEFORE
            )
            for report in warning_reports:
                try:
                    _send_sla_warning(report, level, now_str)
                    warned += 1
                except Exception as e:
                    log.error(f"[watchdog] warning_failed report_id={report.get('report_id')} level={level} error={str(e)[:80]}")
        except Exception as e:
            log.error(f"[watchdog] warning_query_failed level={level} error={str(e)[:80]}")

        # ── Step 2: Breaches → escalate ───────────────────────────────────────
        try:
            breached_reports = database.get_sla_breached_reports(level)
            for report in breached_reports:
                try:
                    _handle_sla_breach(report, level, now_str)
                    escalated += 1
                except Exception as e:
                    log.error(f"[watchdog] escalation_failed report_id={report.get('report_id')} level={level} error={str(e)[:80]}")
        except Exception as e:
            log.error(f"[watchdog] breach_query_failed level={level} error={str(e)[:80]}")

    if warned or escalated:
        log.info(f"[watchdog] engine_complete escalated={escalated} warned={warned}")


def _send_sla_warning(report: dict, level: int, now_str: str):
    """
    Send warning SMS/notification 12h before SLA breach.
    Logs the event. Actual SMS sent via notifications.py if available.
    """
    report_id = report.get("report_id", "")
    ward      = report.get("ward", "")
    division  = report.get("division_name", "")
    role_name = database.SLA_LEVEL_ROLE.get(level, "commissioner")

    # Build warning message
    hours_left = WARNING_HOURS_BEFORE
    msg = (
        f"⚠️ URGENT: {report_id} not resolved. "
        f"{hours_left}h remaining before escalation. "
        f"Ward {ward} — {report.get('damage_type','')} — "
        f"{report.get('severity','').upper()}."
    )

    # Find staff to notify — pass zone/division so database.py can scope
    # correctly once that filtering lands (see database_py_pending_fixes.md)
    staff_list = database.get_staff_for_escalation_level(level, ward=ward, zone=division)

    sms_sent = False
    for staff in staff_list:
        phone = staff.get("phone", "") or staff.get("mobile", "")
        if phone:
            try:
                from notifications import send_sms
                send_sms(phone, msg)
                sms_sent = True
            except Exception:
                pass
        # Always log even if SMS not sent
        database.log_sla_event(
            report_id      = report_id,
            event_type     = "warning",
            escalation_level = level,
            target_role    = role_name,
            target_name    = staff.get("name", ""),
            message_sent   = msg,
            sms_sent       = sms_sent,
        )

    # If no staff found, still log the warning attempt
    if not staff_list:
        database.log_sla_event(
            report_id        = report_id,
            event_type       = "warning_no_staff",
            escalation_level = level,
            target_role      = role_name,
            target_name      = "",
            message_sent     = msg,
            sms_sent         = False,
        )

    log.info(f"[watchdog] warning_sent report_id={report_id} level={level} staff_count={len(staff_list)}")


def _handle_sla_breach(report: dict, level: int, now_str: str):
    """
    Handle a confirmed SLA breach:
    1. Escalate report to next level in database
    2. Reassign assigned_to to the new responsible staff member
    3. Notify new responsible party
    4. Log breach in sla_breach_log
    5. Notify old responsible party that it has been escalated away from them
    """
    report_id = report.get("report_id", "")
    ward      = report.get("ward", "")
    division  = report.get("division_name", "")
    severity  = report.get("severity", "unknown")

    # Calculate hours overdue
    try:
        sla_due_at    = report.get("sla_due_at", "")
        expiry_dt     = _parse_dt(sla_due_at) if sla_due_at else _now_ist_dt()
        hours_overdue = max(0, int((_now_ist_dt() - expiry_dt).total_seconds() / 3600))
    except Exception:
        hours_overdue = 0

    # Escalate in database (moves escalation_level from level → level+1)
    escalated = database.escalate_report(report_id, from_level=level, escalated_by="SLA Watchdog")

    if level >= 3:
        # Already at Commissioner — just send daily reminder, no further escalation
        _send_commissioner_reminder(report, now_str)
        return

    if not escalated:
        log.warning(f"[watchdog] escalate_failed report_id={report_id} level={level}")
        return

    new_level    = level + 1
    new_role     = database.SLA_LEVEL_ROLE.get(new_level, "commissioner")
    new_staff    = database.get_staff_for_escalation_level(new_level, ward=ward, zone=division)
    old_role     = database.SLA_LEVEL_ROLE.get(level, "was")
    old_staff    = database.get_staff_for_escalation_level(level, ward=ward, zone=division)

    # ── BUG FIX (WD6): reassign assigned_to to the new responsible staff ──────
    # Without this, the report keeps showing the old WAS/AE name even though
    # responsibility has moved up the ladder — dashboards showed stale ownership.
    if new_staff:
        new_assignee = new_staff[0].get("name", "")
        if new_assignee:
            conn = database.get_conn(); c = conn.cursor()
            try:
                c.execute(database._q(
                    "UPDATE reports SET assigned_to=? WHERE report_id=?"
                ), (new_assignee, report_id))
                conn.commit()
            except Exception as e:
                log.error(f"[watchdog] reassign_failed report_id={report_id} error={str(e)[:80]}")
            finally:
                conn.close()

    # Message to new responsible person
    breach_msg = (
        f"🚨 ESCALATED TO YOU: {report_id} "
        f"Ward {ward} — {report.get('damage_type','')} — {severity.upper()} "
        f"({hours_overdue}h overdue). "
        f"You must resolve within 24 hours."
    )

    # Message to old responsible person
    escalated_away_msg = (
        f"⛔ {report_id} has been escalated from your level due to SLA breach "
        f"({hours_overdue}h overdue). It is now with your {new_role.replace('_',' ')}. "
        f"You are still expected to assist."
    )

    # Notify new responsible party
    sms_sent = False
    for staff in new_staff:
        phone = staff.get("phone", "") or staff.get("mobile", "")
        if phone:
            try:
                from notifications import send_sms
                send_sms(phone, breach_msg)
                sms_sent = True
            except Exception:
                pass
        database.log_sla_event(
            report_id        = report_id,
            event_type       = "breach_escalated",
            escalation_level = new_level,
            target_role      = new_role,
            target_name      = staff.get("name", ""),
            message_sent     = breach_msg,
            sms_sent         = sms_sent,
        )

    # Notify old responsible party
    for staff in old_staff:
        phone = staff.get("phone", "") or staff.get("mobile", "")
        if phone:
            try:
                from notifications import send_sms
                send_sms(phone, escalated_away_msg)
            except Exception:
                pass

    # Write to sla_breach_log (immutable audit trail)
    _write_breach_log(report_id, now_str, report.get("status",""), hours_overdue,
                      f"Level {level} → Level {new_level} ({new_role})",
                      ward, severity, division)

    log.info(f"[watchdog] breach_handled report_id={report_id} level={level}→{new_level} overdue_h={hours_overdue} notified={len(new_staff)}")


def _send_commissioner_reminder(report: dict, now_str: str):
    """Send daily reminder for complaints at max escalation (level 3)."""
    report_id = report.get("report_id", "")
    ward      = report.get("ward", "")

    # FIX WD-COMM: use IST-aware datetime, not naive datetime.now()
    try:
        submitted = _parse_dt(report.get("submitted_at", now_str))
        days_open = max(0, (_now_ist_dt() - submitted).days)
    except Exception:
        days_open = 5  # fallback

    msg = (
        f"🔴 STILL UNRESOLVED: {report_id} "
        f"Ward {ward} — {report.get('damage_type','')} — "
        f"{report.get('severity','').upper()} "
        f"({days_open} days open). "
        f"Maximum escalation reached. Personal intervention required."
    )

    commissioner_staff = database.get_staff_for_escalation_level(3, ward=ward)
    for staff in commissioner_staff:
        phone = staff.get("phone", "") or staff.get("mobile", "")
        if phone:
            try:
                from notifications import send_sms
                send_sms(phone, msg)
            except Exception:
                pass
        database.log_sla_event(
            report_id        = report_id,
            event_type       = "commissioner_reminder",
            escalation_level = 3,
            target_role      = "commissioner",
            target_name      = staff.get("name", ""),
            message_sent     = msg,
            sms_sent         = False,
        )


def _write_breach_log(report_id, breached_at, from_status, hours_overdue,
                      escalation_level, ward, severity, division_name):
    """Write immutable breach record to sla_breach_log."""
    conn = database.get_conn(); c = conn.cursor()
    try:
        c.execute(database._q("""
            INSERT INTO sla_breach_log
                (report_id, breached_at, from_status, hours_overdue,
                 escalation_level, ward, severity, division_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """), (report_id, breached_at, from_status, hours_overdue,
               escalation_level, ward, severity, division_name))
        conn.commit()
    except Exception as e:
        log.error(f"[watchdog] breach_log_write_failed: {e}")
    finally:
        conn.close()


# ── Scheduler ─────────────────────────────────────────────────────────────────

_scheduler = None


def start_watchdog():
    """Start background SLA scheduler. Called from main.py lifespan."""
    global _scheduler

    try:
        init_watchdog_schema()
    except Exception as e:
        log.error(f"[watchdog] schema init failed: {e}")
        return

    if not _APSCHEDULER_AVAILABLE:
        log.warning("[watchdog] apscheduler not installed — run: pip install apscheduler")
        try:
            run_sla_engine()
        except Exception as e:
            log.error(f"[watchdog] startup_run_failed: {e}")
        return

    _scheduler = BackgroundScheduler(
        job_defaults={"misfire_grace_time": 300}
    )
    _scheduler.add_job(
        run_sla_engine,
        trigger="interval",
        hours=1,
        id="sla_watchdog",
        replace_existing=True,
    )
    _scheduler.add_job(
        run_citizen_review_engine,
        trigger="interval",
        hours=1,
        id="citizen_review_watchdog",
        replace_existing=True,
    )
    _scheduler.add_job(
        run_annual_backup,
        trigger="cron",
        month=1, day=1, hour=2, minute=0,
        id="annual_backup",
        replace_existing=True,
    )
    _scheduler.start()
    log.info("[watchdog] started — sla=1h citizen_review=1h annual_backup=Jan1")

    # Run once immediately at startup to catch overnight breaches
    try:
        run_sla_engine()
    except Exception as e:
        log.error(f"[watchdog] startup_run_failed: {e}")


def stop_watchdog():
    """Gracefully stop scheduler. Called from main.py lifespan shutdown."""
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        log.info("[watchdog] stopped")


# ── Dashboard data ────────────────────────────────────────────────────────────

def get_sla_dashboard_data() -> dict:
    """
    SLA metrics for commissioner and /health/watchdog endpoint.
    Uses new escalation_level column from database.py schema.
    """
    conn = database.get_conn()
    try:
        now_dt         = _now_ist_dt()
        now_str        = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        warning_cutoff = (now_dt + timedelta(hours=WARNING_HOURS_BEFORE)).strftime("%Y-%m-%d %H:%M:%S")
        c              = conn.cursor()

        # Breached now: sla_due_at in the past, not resolved
        c.execute(database._q("""
            SELECT COUNT(*) as cnt FROM reports
            WHERE sla_due_at != ''
            AND sla_due_at < ?
            AND status NOT IN ('resolved', 'closed', 'pending_triage')
        """), (now_str,))
        breached_now = dict(c.fetchone())["cnt"] or 0

        # Warning now: sla_due_at within next 12h
        c.execute(database._q("""
            SELECT COUNT(*) as cnt FROM reports
            WHERE sla_due_at != ''
            AND sla_due_at <= ?
            AND sla_due_at > ?
            AND status NOT IN ('resolved', 'closed', 'pending_triage')
        """), (warning_cutoff, now_str))
        warning_now = dict(c.fetchone())["cnt"] or 0

        # Total escalations ever
        try:
            c.execute("SELECT COUNT(*) as cnt FROM sla_breach_log")
            total_escalations = dict(c.fetchone())["cnt"] or 0
        except Exception:
            total_escalations = 0

        # Recent breaches for dashboard table
        try:
            c.execute("""
                SELECT report_id, from_status, hours_overdue,
                       escalation_level, ward, severity,
                       division_name, breached_at
                FROM sla_breach_log
                ORDER BY breached_at DESC
                LIMIT 10
            """)
            recent_breaches = [dict(r) for r in c.fetchall()]
        except Exception:
            recent_breaches = []

        # Complaints at each escalation level (for zone performance cards)
        level_counts = {}
        for level in [0, 1, 2, 3]:
            try:
                c.execute(database._q("""
                    SELECT COUNT(*) as cnt FROM reports
                    WHERE escalation_level = ?
                    AND status NOT IN ('resolved','closed','pending_triage')
                """), (level,))
                level_counts[level] = dict(c.fetchone())["cnt"] or 0
            except Exception:
                level_counts[level] = 0

        return {
            "sla_breached_now":  breached_now,
            "sla_warning_now":   warning_now,
            "total_escalations": total_escalations,
            "recent_breaches":   recent_breaches,
            "level_counts":      level_counts,
        }
    except Exception as e:
        log.error(f"[watchdog] get_sla_dashboard_data error: {e}")
        return {
            "sla_breached_now":  0,
            "sla_warning_now":   0,
            "total_escalations": 0,
            "recent_breaches":   [],
            "level_counts":      {0: 0, 1: 0, 2: 0, 3: 0},
        }
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# CITIZEN REVIEW ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

def run_citizen_review_engine():
    """
    Runs every hour alongside SLA engine.

    Case 1: Resolved complaints with no citizen response after 72h → auto-close.
            This is NOT a dispute — citizen simply never replied to the
            satisfaction SMS. Treated as implicit acceptance. This does NOT
            violate the "no auto-close on dispute" rule, since no dispute
            was ever raised.
    Case 2: Disputed complaints → AE site visit required (no auto-force-close).
            Production rule: only AE can close a disputed ticket.
    """
    now_ist = _now_ist_dt()
    cutoff  = (now_ist - timedelta(hours=AUTO_CLOSE_HOURS)).strftime("%Y-%m-%d %H:%M:%S")

    conn = database.get_conn()
    c    = conn.cursor()
    auto_closed = 0

    try:
        # Case 1: No citizen response within 72h → auto-close
        c.execute(database._q("""
            SELECT report_id FROM reports
            WHERE status = 'resolved'
            AND (citizen_satisfied IS NULL)
            AND work_done_at != ''
            AND work_done_at < ?
        """), (cutoff,))
        no_response = [dict(r)["report_id"] for r in c.fetchall()]
        conn.close()

        for rid in no_response:
            try:
                # Auto-close: no citizen response treated as implicit acceptance
                database.update_report_status(rid, "closed", "SLA Watchdog — auto-close 72h")
                database.add_comment(rid,
                    "✅ Auto-closed: No citizen response within 72h of work completion.",
                    "SLA Watchdog")
                auto_closed += 1
                log.info(f"[citizen_review] auto_closed report_id={rid} reason=no_response_72h")
            except Exception as e:
                log.error(f"[citizen_review] auto_close_failed report_id={rid} error={str(e)[:80]}")

        if auto_closed:
            log.info(f"[citizen_review] engine_complete auto_closed={auto_closed}")

        # Case 2: Disputed complaints — ensure AE site visit is flagged
        # (already handled by database.submit_citizen_review → ae_site_visit_required=1)
        # Watchdog just sends reminder SMS to AE if disputed for >24h

        conn2 = database.get_conn(); c2 = conn2.cursor()
        reminder_cutoff = (now_ist - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        c2.execute(database._q("""
            SELECT report_id, ward, division_name FROM reports
            WHERE status = 'disputed'
            AND ae_site_visit_required = 1
            AND ae_site_visit_done = 0
            AND updated_at < ?
        """), (reminder_cutoff,))
        pending_disputes = [dict(r) for r in c2.fetchall()]
        conn2.close()

        for row in pending_disputes:
            try:
                ae_staff = database.get_staff_for_escalation_level(
                    1, ward=row.get("ward",""), zone=row.get("division_name","")
                )
                msg = (
                    f"⚠️ SITE VISIT REQUIRED: {row['report_id']} "
                    f"Ward {row.get('ward','')} — citizen disputed work. "
                    f"You must visit site and force-close or reopen."
                )
                for staff in ae_staff:
                    phone = staff.get("phone","") or staff.get("mobile","")
                    if phone:
                        try:
                            from notifications import send_sms
                            send_sms(phone, msg)
                        except Exception:
                            pass
            except Exception as e:
                log.error(f"[citizen_review] dispute_reminder_failed report_id={row.get('report_id')} error={str(e)[:80]}")

    except Exception as e:
        log.error(f"[citizen_review] engine_error: {str(e)[:200]}")
        try:
            conn.close()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════════
# ANNUAL BACKUP
# ═══════════════════════════════════════════════════════════════════════════════

def run_annual_backup():
    """
    Runs on Jan 1 every year.
    Exports all resolved/closed complaints from the previous year
    to data/resolved_YYYY.csv as a permanent offline backup.
    """
    now = _now_ist_dt()

    # Only run on Jan 1
    if now.month != 1 or now.day != 1:
        return

    prev_year = now.year - 1
    log.info(f"[annual_backup] starting backup for year {prev_year}")

    conn = database.get_conn(); c = conn.cursor()
    try:
        if database.USE_POSTGRES:
            c.execute(database._q("""
                SELECT * FROM reports
                WHERE status IN ('resolved','closed')
                AND EXTRACT(YEAR FROM submitted_at::timestamp) = ?
            """), (prev_year,))
        else:
            c.execute("""
                SELECT * FROM reports
                WHERE status IN ('resolved','closed')
                AND strftime('%Y', submitted_at) = ?
            """, (str(prev_year),))

        rows = [dict(r) for r in c.fetchall()]
        conn.close()

        import csv, os, pathlib
        path = pathlib.Path(f"data/resolved_{prev_year}.csv")
        os.makedirs("data", exist_ok=True)

        with open(path, "w", newline="", encoding="utf-8") as f:
            if rows:
                writer = csv.DictWriter(f, fieldnames=rows[0].keys(), extrasaction="ignore")
                writer.writeheader()
                writer.writerows(rows)

        log.info(f"[annual_backup] written {len(rows)} records to {path}")

    except Exception as e:
        log.error(f"[annual_backup] failed: {str(e)[:200]}")
        try: conn.close()
        except Exception: pass