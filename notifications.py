"""
notifications.py — RoadSeva Citizen Notification System
=========================================================
Blocker 6 fix: System was completely silent to citizens after submission.

Handles:
  - Email via Resend API (immediate, zero cost)
  - SMS via MSG91 (when API key configured)
  - WhatsApp via MSG91 (future)

Usage in main.py:
  from notifications import notify_citizen

  notify_citizen(
      phone  = "9XXXXXXXXX",
      email  = "citizen@gmail.com",
      name   = "Ravi Kumar",
      event  = "submitted",       # or assigned/resolved/review_request
      report_id = "GVMC-2601-XYZ",
      ward   = "Ward 22 - Seethammadhara",
  )

BUGS FIXED (this pass):
  N1: notify_complaint_submitted/assigned/resolved/notify_dispute_raised
      accepted **kw but never forwarded name/email to notify_citizen() —
      citizen name always showed as "Citizen" and email notifications
      never fired through these wrappers. Fixed: wrappers now accept and
      forward name/email explicitly.
  N2: _format_message() silently swallowed KeyError and returned the raw
      unformatted template (citizen would see literal "{report_id}" text
      in an SMS). Fixed: now logs a warning when a template key is missing,
      still returns the unformatted template as a safe fallback so the
      message at least gets delivered.

NOT YET FIXED (tracked in database_py_pending_fixes.md — cross-file, needs
main.py Step 4 rewrite, not fixable inside this file alone):
  N3: notify_citizen() is never actually called anywhere in current main.py.
      Even with this file fully correct, zero citizen SMS/email will be
      sent until main.py wires in calls at: /submit (event=submitted),
      WAS auto-assignment (event=assigned), work-done-upload (event=resolved,
      needs create_citizen_review_token() called first), and citizen_review_post
      dispute path (event=dispute_received).
  N4: create_citizen_review_token() exists in database.py but has no caller —
      the citizen satisfaction review link is never generated or sent.
  N5: watchdog.py builds its own inline SLA message strings instead of using
      a shared template here — no Telugu version exists for staff-facing SLA
      messages. This is a design choice (add staff SLA templates here?), not
      a bug — flagged for a decision, not forced into this file.
"""

import os
import logging

log = logging.getLogger("roadseva.notifications")

# ── Event message templates ────────────────────────────────────────────────────
# English + Telugu for each event type

MESSAGES = {
    "submitted": {
        "en": (
            "Your road damage complaint has been registered.\n"
            "Reference: {report_id}\n"
            "Ward: {ward}\n"
            "Track status: roadseva.onrender.com/track\n"
            "— GVMC RoadSeva"
        ),
        "te": (
            "మీ రోడ్డు నష్టం ఫిర్యాదు నమోదు చేయబడింది.\n"
            "సూచన నంబర్: {report_id}\n"
            "వార్డు: {ward}\n"
            "స్థితి తెలుసుకోండి: roadseva.onrender.com/track\n"
            "— GVMC RoadSeva"
        ),
        "subject": "Complaint Registered — {report_id} | GVMC RoadSeva",
    },
    "assigned": {
        "en": (
            "Your complaint {report_id} has been assigned to a field officer.\n"
            "Ward: {ward}\n"
            "Expected resolution: within 48 hours.\n"
            "Track: roadseva.onrender.com/track\n"
            "— GVMC RoadSeva"
        ),
        "te": (
            "మీ ఫిర్యాదు {report_id} ఫీల్డ్ అధికారికి అప్పగించబడింది.\n"
            "పని 48 గంటల లోపు పూర్తవుతుంది.\n"
            "— GVMC RoadSeva"
        ),
        "subject": "Complaint Assigned — {report_id} | GVMC RoadSeva",
    },
    "resolved": {
        "en": (
            "The road work for your complaint {report_id} has been completed.\n"
            "Ward: {ward}\n\n"
            "Was the work done properly? Please confirm:\n"
            "{review_url}\n\n"
            "Your response helps us improve. Takes 5 seconds.\n"
            "— GVMC RoadSeva"
        ),
        "te": (
            "మీ ఫిర్యాదు {report_id} పని పూర్తైంది.\n\n"
            "పని సరిగా జరిగిందా? దయచేసి నిర్ధారించండి:\n"
            "{review_url}\n"
            "— GVMC RoadSeva"
        ),
        "subject": "Work Completed — Please Confirm | {report_id} | GVMC RoadSeva",
    },
    "review_request": {
        "en": (
            "Was the road fixed at your location?\n"
            "Complaint: {report_id} | Ward: {ward}\n\n"
            "Confirm here (takes 5 seconds):\n"
            "{review_url}\n\n"
            "No response within 72 hours = automatically marked complete.\n"
            "— GVMC RoadSeva"
        ),
        "te": (
            "మీ ఫిర్యాదు {report_id} పని పూర్తైందా?\n"
            "నిర్ధారించండి: {review_url}\n"
            "72 గంటల్లో జవాబు లేకుంటే స్వయంచాలకంగా మూసివేయబడుతుంది.\n"
            "— GVMC RoadSeva"
        ),
        "subject": "Did We Fix Your Road? | {report_id} | GVMC RoadSeva",
    },
    "dispute_received": {
        "en": (
            "We've received your feedback that the road work was incomplete.\n"
            "Complaint: {report_id}\n"
            "A senior officer will review and take action within 48 hours.\n"
            "— GVMC RoadSeva"
        ),
        "te": (
            "మీ అభిప్రాయం అందుకున్నాం. పని అసంపూర్ణంగా ఉందని నమోదైంది.\n"
            "48 గంటల్లో సీనియర్ అధికారి చర్య తీసుకుంటారు.\n"
            "— GVMC RoadSeva"
        ),
        "subject": "Feedback Received — Under Review | {report_id} | GVMC RoadSeva",
    },
}


def _build_review_url(report_id: str) -> str:
    base = os.getenv("APP_BASE_URL", "https://roadseva.onrender.com")
    return f"{base}/citizen-review/{report_id}"


def _format_message(template: str, **kwargs) -> str:
    """
    N2 FIX: log a warning instead of silently swallowing missing template
    keys. Still returns the unformatted template as a safe fallback so the
    message gets delivered rather than crashing the notify call entirely.
    """
    try:
        return template.format(**kwargs)
    except KeyError as e:
        log.warning(f"[notify] template missing key {e} — sending unformatted template")
        return template


# ── SMS via MSG91 ──────────────────────────────────────────────────────────────

def _send_sms(phone: str, message: str) -> bool:
    api_key      = os.getenv("MSG91_API_KEY", "")
    sender_id    = os.getenv("MSG91_SENDER_ID", "RODSVA")
    template_id  = os.getenv("MSG91_TEMPLATE_ID", "")

    if not api_key:
        log.info(f"[sms] MSG91_API_KEY not set — SMS skipped for {phone[-4:] if phone else ''}")
        return False

    if not phone or len(phone) < 10:
        return False

    # Normalise phone — remove +91, spaces, dashes
    phone = phone.strip().replace(" ", "").replace("-", "")
    if phone.startswith("+91"):
        phone = phone[3:]
    elif phone.startswith("91") and len(phone) == 12:
        phone = phone[2:]

    try:
        import urllib.request, json, urllib.parse

        payload = {
            "sender":    sender_id,
            "route":     "4",
            "country":   "91",
            "sms": [{
                "message": message[:160],
                "to":      [f"91{phone}"],
            }]
        }
        if template_id:
            payload["template_id"] = template_id

        data = json.dumps(payload).encode("utf-8")
        req  = urllib.request.Request(
            "https://api.msg91.com/api/v2/sendsms",
            data=data,
            headers={
                "Content-Type":  "application/json",
                "authkey":       api_key,
            },
            method="POST"
        )
        resp = urllib.request.urlopen(req, timeout=8)
        body = resp.read().decode("utf-8")
        result = json.loads(body)

        if result.get("type") == "success":
            log.info(f"[sms] sent to {phone[-4:]} — event ok")
            return True
        else:
            log.warning(f"[sms] MSG91 returned: {result}")
            return False

    except Exception as e:
        log.error(f"[sms] failed for {phone[-4:] if phone else ''}: {type(e).__name__}: {e}")
        return False


# ── Email via Resend ──────────────────────────────────────────────────────────

def _send_email(email: str, subject: str, body_text: str,
                report_id: str = "", review_url: str = "") -> bool:
    api_key = os.getenv("RESEND_API_KEY", "")

    if not api_key:
        log.info(f"[email] RESEND_API_KEY not set — email skipped")
        return False

    if not email or "@" not in email:
        return False

    try:
        import resend
        resend.api_key = api_key

        html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f0f4f8;font-family:Arial,sans-serif;">
<div style="max-width:560px;margin:32px auto;background:white;border-radius:12px;
     overflow:hidden;box-shadow:0 4px 20px rgba(0,0,0,0.08);">

  <div style="background:#1a6b3c;padding:20px 28px;">
    <div style="color:white;font-size:18px;font-weight:bold;">
      🛣️ RoadSeva · GVMC Visakhapatnam
    </div>
  </div>

  <div style="padding:28px;">
    <div style="font-size:15px;color:#333;line-height:1.8;white-space:pre-wrap;">{body_text}</div>

    {f'''<div style="margin-top:24px;text-align:center;">
      <a href="{review_url}"
         style="display:inline-block;background:#1a6b3c;color:white;
                padding:14px 32px;border-radius:8px;text-decoration:none;
                font-weight:700;font-size:15px;">
        ✅ Was the road fixed? Click to confirm
      </a>
    </div>''' if review_url else ''}
  </div>

  <div style="background:#f8f8f8;border-top:1px solid #eee;
       padding:14px 28px;text-align:center;">
    <div style="color:#aaa;font-size:11px;">
      Greater Visakhapatnam Municipal Corporation · RoadSeva
      {f'<br>Reference: {report_id}' if report_id else ''}
    </div>
  </div>
</div>
</body></html>"""

        resend.Emails.send({
            "from":    "RoadSeva GVMC <onboarding@resend.dev>",
            "to":      [email],
            "subject": subject,
            "html":    html,
        })
        log.info(f"[email] sent to {email[:4]}*** — {subject[:40]}")
        return True

    except ImportError:
        log.warning("[email] resend package not installed — pip install resend")
        return False
    except Exception as e:
        log.error(f"[email] failed: {type(e).__name__}: {e}")
        return False


# ── Main notify function ───────────────────────────────────────────────────────

def notify_citizen(
    event:     str,
    report_id: str,
    name:      str  = "",
    phone:     str  = "",
    email:     str  = "",
    ward:      str  = "",
    lang:      str  = "en",
) -> dict:
    """
    Send notification to citizen for a given event.

    Events: submitted | assigned | resolved | review_request |
            dispute_received

    Returns: {"sms": bool, "email": bool}
    """
    if event not in MESSAGES:
        log.warning(f"[notify] unknown event: {event}")
        return {"sms": False, "email": False}

    if not phone and not email:
        return {"sms": False, "email": False}

    template_set = MESSAGES[event]
    review_url   = _build_review_url(report_id) if event in ("resolved", "review_request") else ""

    # Build message in preferred language (fallback to English)
    lang_key  = lang if lang in ("en", "te") else "en"
    body_text = _format_message(
        template_set[lang_key],
        report_id  = report_id,
        name       = name or "Citizen",
        ward       = ward,
        review_url = review_url,
    )
    subject = _format_message(
        template_set.get("subject", "GVMC RoadSeva Update"),
        report_id = report_id,
    )

    sms_sent   = _send_sms(phone, body_text)   if phone else False
    email_sent = _send_email(email, subject, body_text,
                             report_id, review_url) if email else False

    log.info(
        f"[notify] event={event} report={report_id} "
        f"sms={'✅' if sms_sent else '⬜'} "
        f"email={'✅' if email_sent else '⬜'}"
    )
    return {"sms": sms_sent, "email": email_sent}


# ── Convenience wrappers for main.py imports ──────────────────────────────────
# N1 FIX: all wrappers now accept and forward name/email instead of dropping
# them into **kw — citizen name will display correctly and email notifications
# will actually fire through these wrappers when callers supply email.

def notify_complaint_submitted(phone, report_id, ward, damage_type="",
                                name="", email="", **kw):
    return notify_citizen("submitted", report_id, phone=phone, email=email,
                          name=name, ward=ward)

def notify_complaint_assigned(phone, report_id, ward="", name="", email="", **kw):
    return notify_citizen("assigned", report_id, phone=phone, email=email,
                          name=name, ward=ward)

def notify_complaint_resolved(phone, report_id, ward="", name="", email="", **kw):
    return notify_citizen("resolved", report_id, phone=phone, email=email,
                          name=name, ward=ward)

def notify_dispute_raised(phone, report_id, name="", email="", **kw):
    return notify_citizen("dispute_received", report_id, phone=phone,
                          email=email, name=name)

# ── Direct SMS wrapper for watchdog.py ────────────────────────────────────────
def send_sms(phone: str, message: str) -> bool:
    """Public wrapper — called by watchdog.py for SLA alerts."""
    return _send_sms(phone, message)