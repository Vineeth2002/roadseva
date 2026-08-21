"""
main.py — RoadSeva Application Entry Point

KEY CHANGES vs original:
  HARD ROLE WALLS on every route
  SCOPED ASSIGNMENT — officer sees only HIS engineers
  assigned_officer saved on every assign action
  Field engineer pages — no citizen phone, no full admin controls
  ENQUIRY EMAIL via Resend API

NEW (routing brain + SLA watchdog + hierarchy):
  start_watchdog() / stop_watchdog() in lifespan
  /submitted shows division_name the complaint was routed to
  /api/stats scoped for all new roles (was/ae/zonal/field_engineer)
  /complete-inspection logs AI training label on every closure
  /citizen-review/<report_id> — citizen satisfaction response
  /disputed-reviews — before/after photo comparison for AE review
  /work-done-upload — WAS submits work completion photo
  /export/training-data — admin-only AI dataset export
  /triage — grievance officer GPS-failed complaint queue
  /assign-ward — triage officer assigns ward
  /flag-incorrect-ward — WAS flags wrong ward
  /approve-ward-flag — AE approves/rejects ward reassignment
  ROLE_HOME + ROLE_LABELS extended for all 9 GVMC roles
  /field accepts was role (Ward Amenities Secretary)
  /staff accepts ae role (Assistant Engineer)
  /commissioner accepts zonal_commissioner role

FIXES APPLIED (FIX 1–10):
  FIX 1  — /submit: ward now optional (Form default=""), location_text accepted
  FIX 2  — /submit: notify_citizen() called after submission
  FIX 3  — /submit: division_name looked up even when ward is empty
  FIX 4  — ROLE_HOME includes grievance_officer and triage_officer → /triage
  FIX 5  — ROLE_LABELS includes grievance_officer and triage_officer
  FIX 6  — /work-done-upload: notify_citizen() called after resolution
  FIX 7  — /flag-incorrect-ward route added (WAS flags wrong ward)
  FIX 8  — /approve-ward-flag route added (AE approves/rejects ward flag)
  FIX 9  — /submit: check_rate_limit("submit") enforced before processing
           (rate limit existed in security.py but was never called here)
  FIX 10 — /work-done-upload: review token passed into notify_citizen review_url
           (token was generated but discarded — citizen received broken link)
"""

import os, io, csv, base64, urllib.parse
import permissions
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager

#from attrs import inspect  # not in requirements.txt, never used

from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse
from fastapi.templating import Jinja2Templates

import database
from wards import WARD_NAMES
from permissions import ROLE_HOME, ROLE_LABELS

from security import (
    sanitize_input, sanitize_filename, deep_inspect_photo,
    csrf_token_citizen, verify_csrf_citizen,
    generate_csrf_token, verify_csrf_token,
    get_real_ip,
    check_rate_limit,
    SecurityGatewayMiddleware,
    RequestSizeLimitMiddleware,
    SecurityHeadersMiddleware,
)

from watchdog import start_watchdog, stop_watchdog

# Notifications — gracefully no-op if not configured
try:
    from notifications import notify_citizen
    _NOTIFICATIONS_AVAILABLE = True
except ImportError:
    _NOTIFICATIONS_AVAILABLE = False
    def notify_citizen(*a, **kw): return {"sms": False, "email": False}


COOKIE_NAME    = "session_token"
COOKIE_MAX_AGE = 8 * 3600
WARRANTY_DAYS  = 180

@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    try:
        database.cleanup_expired_sessions()
    except Exception as e:
        print(f"[startup] cleanup_expired_sessions skipped: {e}")
    try:
        from severity import retry_pending_severity
        # NOTE: On Render's ephemeral filesystem, uploads/ is wiped on each deploy.
        # retry_pending_severity() will find no files and silently no-op.
        # This is expected behaviour — photos are stored in Cloudinary for production.
        # This call only has effect in local dev where uploads/ persists.
        retry_pending_severity()
    except Exception:
        pass
    # ── Phase 1: Contract Intelligence schema + demo data ─────────────────────
    try:
        database.init_contract_intelligence_schema()
        database.seed_demo_contract_data()
    except Exception as e:
        print(f"[startup] contract schema init skipped: {e}")
    start_watchdog()
    yield
    stop_watchdog()


app = FastAPI(lifespan=lifespan)

app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(SecurityGatewayMiddleware)
app.add_middleware(RequestSizeLimitMiddleware)

try:
    from logging_middleware import RequestContextMiddleware
    app.add_middleware(RequestContextMiddleware)
except ImportError:
    pass

import traceback
from starlette.middleware.base import BaseHTTPMiddleware

class ErrorAlertMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        try:
            response = await call_next(request)
            if response.status_code == 500:
                _send_error_alert(f"500 Error\nURL: {request.url}")
            return response
        except Exception as exc:
            tb = traceback.format_exc()
            _send_error_alert(f"Exception\nURL: {request.url}\n{type(exc).__name__}: {exc}\n{tb[-600:]}")
            raise

def _send_error_alert(message):
    token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        import urllib.request, json
        data = json.dumps({"chat_id": chat_id, "text": f"🚨 RoadSeva\n{message}"[:4000]}).encode()
        req  = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=data, headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass

app.add_middleware(ErrorAlertMiddleware)

templates = Jinja2Templates(directory="templates")
os.makedirs("uploads", exist_ok=True)
os.makedirs("data", exist_ok=True)


# ── SESSION HELPERS ────────────────────────────────────────────────────────────

def set_session_cookie(response, token):
    response.set_cookie(key=COOKIE_NAME, value=token, httponly=True,
        secure=os.getenv("ENVIRONMENT","production")=="production",
        samesite="lax", max_age=COOKIE_MAX_AGE)

def require_login(request):
    token = request.cookies.get(COOKIE_NAME, "")
    if not token: return None, False
    staff = database.get_staff_by_token(token)
    if not staff: return None, False
    return staff, bool(staff.get("must_change_password"))

def require_login_fc(request):
    return require_login(request)

def _now_ist():
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).strftime("%d %b %Y, %I:%M %p")

def _grade_from_score(score):
    if score >= 90: return {"grade":"A","color":"#22c55e","label":"Excellent"}
    if score >= 75: return {"grade":"B","color":"#84cc16","label":"Good"}
    if score >= 60: return {"grade":"C","color":"#eab308","label":"Average"}
    if score >= 40: return {"grade":"D","color":"#f97316","label":"Poor"}
    return              {"grade":"F","color":"#ef4444", "label":"Critical"}

def _build_rqi_data(events):
    total_repairs  = len(set(e.get("original_report_id","") for e in events)) if events else 0
    total_breaches = len(events)
    city_score     = round((1-total_breaches/max(total_repairs,1))*100) if total_repairs > 0 else 100
    city_grade     = _grade_from_score(city_score)
    dur_days = [e.get("days_since_repair",0) for e in events if e.get("days_since_repair")]
    avg_dur  = round(sum(dur_days)/len(dur_days)) if dur_days else 0
    ward_map = {}
    for ev in events:
        w = ev.get("ward") or "Unknown"
        if w not in ward_map: ward_map[w] = {"ward":w,"total_repairs":0,"breaches":0,"days":[]}
        ward_map[w]["breaches"] += 1; ward_map[w]["total_repairs"] += 1
        if ev.get("days_since_repair"): ward_map[w]["days"].append(ev["days_since_repair"])
    ward_list = []
    for w, d in ward_map.items():
        score = round((1-d["breaches"]/max(d["total_repairs"],1))*100)
        g = _grade_from_score(score)
        ward_list.append({"ward":w,"score":score,"grade":g["grade"],
            "grade_color":g["color"],"total_repairs":d["total_repairs"],
            "breaches":d["breaches"],
            "avg_durability":round(sum(d["days"])/len(d["days"])) if d["days"] else 0})
    ward_list.sort(key=lambda x: -x["breaches"])
    eng_map = {}
    for ev in events:
        name = ev.get("contractor_name") or "Unknown"
        if name not in eng_map: eng_map[name] = {"name":name,"total_repairs":0,"breaches":0}
        eng_map[name]["breaches"] += 1; eng_map[name]["total_repairs"] += 1
    eng_list = []
    for name, d in eng_map.items():
        score = round((1-d["breaches"]/max(d["total_repairs"],1))*100)
        g = _grade_from_score(score)
        eng_list.append({"name":name,"score":score,"grade":g["grade"],
            "grade_color":g["color"],"total_repairs":d["total_repairs"],"breaches":d["breaches"]})
    eng_list.sort(key=lambda x: -x["breaches"])
    dmg_map = {}
    for ev in events:
        dt = ev.get("damage_type") or "Unknown"
        if dt not in dmg_map: dmg_map[dt] = {"damage_type":dt,"total":0,"breached":0}
        dmg_map[dt]["breached"] += 1; dmg_map[dt]["total"] += 1
    damage_breach = sorted(dmg_map.values(), key=lambda x: -x["breached"])
    monthly_map = {}
    for ev in events:
        month = (ev.get("flagged_at") or "")[:7]
        if not month: continue
        if month not in monthly_map: monthly_map[month] = {"month":month,"repairs":0,"breaches":0}
        monthly_map[month]["repairs"] += 1; monthly_map[month]["breaches"] += 1
    monthly = sorted(monthly_map.values(), key=lambda x: x["month"])
    recent_events = [{"breach_detected_at":ev.get("flagged_at",""),
        "days_elapsed":ev.get("days_since_repair",0),
        "distance_meters":round(ev.get("distance_m",0),1),
        "repaired_by":ev.get("contractor_name") or "—",
        "warranty_days":WARRANTY_DAYS, **ev} for ev in events[:20]]
    return {"warranty_days":WARRANTY_DAYS,"city_score":city_score,
            "city_grade":city_grade,"total_repairs":total_repairs,
            "total_breaches":total_breaches,"active_under_warranty":0,
            "avg_durability_days":avg_dur,"wards":ward_list,
            "engineers":eng_list,"damage_breach":damage_breach,
            "monthly":monthly,"recent_events":recent_events}


# ── ENQUIRY EMAIL ──────────────────────────────────────────────────────────────

def _send_enquiry_email(fname, lname, email, org, etype, msg):
    resend_key = os.getenv("RESEND_API_KEY", "")
    if not resend_key:
        print("[enquiry] RESEND_API_KEY not set")
        return False
    try:
        import resend
        resend.api_key = resend_key
        resend.Emails.send({
            "from":     "RoadSeva Enquiries <onboarding@resend.dev>",
            "to":       ["ainimireddyvineeth25@gmail.com"],
            "reply_to": email,
            "subject":  f"New Enquiry — {etype or 'General'} | {org or fname}",
            "html": f"""<!DOCTYPE html><html><body style="font-family:Arial,sans-serif;background:#f0f4f8;padding:20px">
<div style="max-width:600px;margin:auto;background:white;border-radius:12px;overflow:hidden">
  <div style="background:#1a6b3c;padding:24px 28px;color:white;font-size:20px;font-weight:bold">📬 New RoadSeva Enquiry</div>
  <div style="padding:24px 28px">
    <p><b>Name:</b> {fname} {lname}</p>
    <p><b>Email:</b> <a href="mailto:{email}">{email}</a></p>
    <p><b>Org:</b> {org or '—'}</p>
    <p><b>Type:</b> {etype or '—'}</p>
    <div style="background:#fafafa;border-left:4px solid #f47920;padding:16px;margin-top:16px">
      <b>Message:</b><br><br>{msg}
    </div>
  </div>
</div></body></html>"""
        })
        return True
    except Exception as e:
        print(f"[enquiry] error: {e}")
        return False


# ── HOME ───────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(request, "landing.html", {})

@app.get("/about", response_class=HTMLResponse)
async def about(request: Request):
    staff, _ = require_login(request)
    return templates.TemplateResponse(request, "about.html", {"staff": staff})

@app.post("/enquiry")
async def enquiry_post(request: Request):
    try:
        data  = await request.json()
        fname = data.get("fname","").strip()
        lname = data.get("lname","").strip()
        email = data.get("email","").strip()
        org   = data.get("org","").strip()
        etype = data.get("type","").strip()
        msg   = data.get("msg","").strip()
        if not fname or not email or not msg:
            return JSONResponse({"ok": False, "error": "Name, email and message are required."})
        key = f"enquiry_{database.now().replace(' ','_').replace(':','-')}_{email[:30]}"
        database.set_system_setting(key, f"{fname} {lname} | {email} | {org} | {etype} | {msg[:500]}")
        _send_enquiry_email(fname, lname, email, org, etype, msg)
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": "Server error."})


# ── CITIZEN ────────────────────────────────────────────────────────────────────

@app.get("/citizen", response_class=HTMLResponse)
async def citizen(request: Request):
    return templates.TemplateResponse(request, "citizen.html", {"csrf": csrf_token_citizen(), "wards": WARD_NAMES})


@app.post("/submit", response_class=HTMLResponse)
async def submit(
    request: Request,
    citizen_name:     str = Form(...),
    citizen_phone:    str = Form(...),
    citizen_email:    str = Form(default=""),
    ward:             str = Form(default=""),
    location_text:    str = Form(default=""),
    damage_type:      str = Form(...),
    description:      str = Form(default=""),
    photo:            UploadFile = File(default=None),
    latitude:         str = Form(default=""),
    longitude:        str = Form(default=""),
    csrf:             str = Form(default=""),
    compressed_photo: str = Form(default=""),
):
    # FIX 9 — enforce /submit rate limit (was defined in security.py but never called)
    ip = get_real_ip(request)
    ok, reason, retry = check_rate_limit("submit", ip)
    if not ok:
        return templates.TemplateResponse(request, "citizen.html", {"error": reason,
            "csrf": csrf_token_citizen(), "wards": WARD_NAMES})

    if not verify_csrf_citizen(csrf):
        return templates.TemplateResponse(request, "citizen.html", {"error": "Session expired. Please try again.",
            "csrf": csrf_token_citizen(), "wards": WARD_NAMES})

    citizen_name, _  = sanitize_input(citizen_name)
    citizen_phone, _ = sanitize_input(citizen_phone)
    citizen_email, _ = sanitize_input(citizen_email)
    ward, _          = sanitize_input(ward)
    location_text, _ = sanitize_input(location_text)
    damage_type, _   = sanitize_input(damage_type)
    description, _   = sanitize_input(description)

    effective_description = description or location_text

    if compressed_photo and compressed_photo.startswith("data:"):
        try:
            header, b64data = compressed_photo.split(",", 1)
            ext         = header.split("/")[1].split(";")[0]
            clean_bytes = base64.b64decode(b64data)
            filename    = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_compressed.{ext}"
            file_path   = os.path.join("uploads", filename)
            os.makedirs("uploads", exist_ok=True)
            with open(file_path, "wb") as f: f.write(clean_bytes)
        except Exception as e:
            print(f"[submit] compressed photo error: {e}")
            clean_bytes = b""; ext = "jpg"; file_path = ""
    elif photo and photo.filename:
        photo_bytes = await photo.read()
        safe_filename, _ = sanitize_filename(photo.filename or "upload.jpg")
        inspect = deep_inspect_photo(photo_bytes, safe_filename)
        if not inspect["safe"]:
            return templates.TemplateResponse(request, "citizen.html", {"error": inspect["error"],
                "csrf": csrf_token_citizen(), "wards": WARD_NAMES})
        clean_bytes = inspect["clean_bytes"]; ext = inspect["ext"]
        filename    = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{safe_filename}"
        file_path   = os.path.join("uploads", filename)
        os.makedirs("uploads", exist_ok=True)
        with open(file_path, "wb") as f: f.write(clean_bytes)
    else:
        return templates.TemplateResponse(request, "citizen.html", {"error": "Please upload a photo of the damage.",
            "csrf": csrf_token_citizen(), "wards": WARD_NAMES})

    try:    lat = float(latitude) if latitude else None
    except: lat = None
    try:    lng = float(longitude) if longitude else None
    except: lng = None

    photo_data = f"data:image/{ext};base64,{base64.b64encode(clean_bytes).decode()}"

    report_id = database.add_report(
        "GVMC", ward, damage_type, effective_description, file_path,
        citizen_name, citizen_phone, citizen_email, lat, lng,
        severity="unknown", photo_data=photo_data,
        location_text=location_text,
    )

    division_name = database.get_division_by_ward(ward) if ward else ""

    try:
        notify_citizen(
            event     = "submitted",
            report_id = report_id,
            name      = citizen_name,
            phone     = citizen_phone,
            email     = citizen_email,
            ward      = ward or "Pending assignment",

        )
    except Exception as e:
        print(f"[submit] notify_citizen error: {e}")

    def _run_severity(rid, fpath, dtype):
        try:
            from severity import analyse_severity
            result = analyse_severity(fpath, dtype)
            if result["severity"] != "unknown":
                database.update_report_severity(rid, result["severity"],
                    result.get("severity_details",""), result.get("estimated_cost",""),
                    result.get("urgency",""))
        except Exception as e:
            print(f"[submit] AI severity error: {e}")

    import threading
    threading.Thread(target=_run_severity, args=(report_id, file_path, damage_type),
                     daemon=True).start()

    rqi_breach = False
    if lat and lng:
        rqi_breach = database.check_rqi_breach(report_id, ward, damage_type, lat, lng)
    duplicate = {"found": False}
    try:
        duplicate = database.check_duplicate_complaint(report_id, damage_type, lat, lng, ward=ward)
    except Exception:
        pass

    params = urllib.parse.urlencode({
        "id":      report_id,
        "name":    citizen_name,
        "severity":"unknown",
        "rqi":     "1" if rqi_breach else "0",
        "dup_id":  duplicate.get("report_id",""),
        "dup_dist":str(duplicate.get("distance_m","")),
        "division":division_name,
    })
    return RedirectResponse(url=f"/submitted?{params}", status_code=303)


@app.get("/submitted", response_class=HTMLResponse)
async def submitted_get(request: Request,
    id: str="", name: str="", severity: str="unknown",
    rqi: str="0", dup_id: str="", dup_dist: str="", division: str=""):
    if not id:
        return RedirectResponse(url="/citizen", status_code=303)
    return templates.TemplateResponse(request, "submitted.html", {"report_id": id, "citizen_name": name,
        "severity": severity, "rqi_breach": rqi=="1",
        "duplicate_id": dup_id, "duplicate_dist": dup_dist,
        "division_name": division})


# ── CITIZEN REVIEW ─────────────────────────────────────────────────────────────

@app.get("/citizen-review/{report_id}", response_class=HTMLResponse)
async def citizen_review_get(request: Request, report_id: str, token: str = ""):
    if not token:
        return HTMLResponse(
            "<h2>Invalid or missing review link. Please use the link sent to your phone.</h2>",
            status_code=404)
    review = database.get_review_by_token(token)
    if not review or review.get("rid") != report_id:
        return HTMLResponse(
            "<h2>This review link is invalid or has already been used.</h2>",
            status_code=404)
    return templates.TemplateResponse(request, "citizen_review.html", {"report_id": report_id,
        "token": token,
        "ward": review.get("ward",""),
        "damage_type": review.get("damage_type",""),
        "csrf": csrf_token_citizen(),
    })


@app.post("/citizen-review/{report_id}", response_class=HTMLResponse)
async def citizen_review_post(request: Request, report_id: str,
    satisfied: str = Form(...),
    note:      str = Form(default=""),
    token:     str = Form(default=""),
    csrf:      str = Form(default="")):
    if not verify_csrf_citizen(csrf):
        return HTMLResponse("<h2>Session expired. Please use the original link.</h2>")
    if not token:
        return HTMLResponse("<h2>Invalid review link.</h2>", status_code=404)

    is_satisfied = satisfied.lower() in ("yes", "1", "true", "satisfied")
    note, _ = sanitize_input(note)

    ok, result = database.submit_citizen_review(token, is_satisfied, note)
    if not ok:
        return HTMLResponse(f"<h2>{result}</h2>", status_code=400)

    report = database.get_report_by_id(report_id)

    if result == "closed":
        return templates.TemplateResponse(request, "citizen_review_thanks.html", {"satisfied": True, "force_closed": False,
            "report_id": report_id,
            "message": "Thank you for confirming. Your feedback helps us serve Visakhapatnam better."
        })

    try:
        if report:
            notify_citizen(event="dispute_received", report_id=report_id,
                name=report.get("citizen_name",""), phone=report.get("citizen_phone",""),
                email=report.get("citizen_email",""))
    except Exception as e:
        print(f"[citizen_review] notify_citizen error: {e}")

    return templates.TemplateResponse(request, "citizen_review_thanks.html", {"satisfied": False, "force_closed": False,
        "report_id": report_id,
        "message": "Thank you for letting us know. A senior officer will review the work photos and take action within 48 hours."
    })

# ── TRIAGE DASHBOARD ──────────────────────────────────────────────────────────

@app.get("/triage", response_class=HTMLResponse)
async def triage_dashboard(request: Request):
    staff, mc = require_login_fc(request)
    if not staff: return RedirectResponse("/login", status_code=302)
    if mc: return RedirectResponse("/change-password?forced=1", status_code=302)

    if not permissions.check_role(staff, *permissions.TRIAGE_ROLES, *permissions.COMMISSIONER_ROLES):
        return permissions.redirect_home(staff)

    reports = database.get_pending_triage_reports(limit=100)
    token   = request.cookies.get(COOKIE_NAME, "")
    return templates.TemplateResponse(request, "triage.html", {"staff": staff,
        "reports": reports,
        "csrf": generate_csrf_token(token),
        "wards": WARD_NAMES,
    })

@app.post("/assign-ward", response_class=HTMLResponse)
async def assign_ward(request: Request,
    report_id:   str = Form(...),
    ward:        str = Form(...),
    triage_note: str = Form(default=""),
    csrf:        str = Form(default="")):
    staff, mc = require_login_fc(request)
    if not staff: return RedirectResponse("/login", status_code=302)
    token = request.cookies.get(COOKIE_NAME, "")
    if not verify_csrf_token(token, csrf):
        return RedirectResponse("/triage?error=csrf", status_code=302)

    if not permissions.check_role(staff, *permissions.TRIAGE_ROLES, *permissions.COMMISSIONER_ROLES):
        return permissions.redirect_home(staff)

    ward, _ = sanitize_input(ward)
    ok, msg = database.assign_ward_from_triage(report_id, ward, staff["name"])

    if ok and triage_note:
        triage_note, _ = sanitize_input(triage_note)
        database.add_comment(report_id, f"🗺️ Triage: {triage_note}", staff["name"])

    return RedirectResponse("/triage", status_code=302)


# ── FIX 7 — WAS flags incorrect ward ──────────────────────────────────────────

@app.post("/flag-incorrect-ward", response_class=HTMLResponse)
async def flag_incorrect_ward(request: Request,
    report_id:      str = Form(...),
    requested_ward: str = Form(...),
    reason:         str = Form(default=""),
    csrf:           str = Form(default="")):
    staff, mc = require_login_fc(request)
    if not staff: return RedirectResponse("/login", status_code=302)
    token = request.cookies.get(COOKIE_NAME, "")
    if not verify_csrf_token(token, csrf):
        return RedirectResponse("/field?error=csrf", status_code=302)

    if not permissions.check_role(staff, *permissions.FIELD_ROLES, "admin"):
        return permissions.redirect_home(staff)

    requested_ward, _ = sanitize_input(requested_ward)
    reason, _         = sanitize_input(reason)

    ok, msg = database.flag_incorrect_ward(
        report_id, requested_ward, reason, staff["name"]
    )

    if ok:
        database.add_comment(report_id,
            f"🚩 Ward flag submitted by {staff['name']}: requested reassignment to {requested_ward}. Reason: {reason or 'Not specified'}",
            staff["name"])

    return RedirectResponse("/field", status_code=302)


# ── FIX 8 — AE approves or rejects ward flag ──────────────────────────────────

@app.post("/approve-ward-flag", response_class=HTMLResponse)
async def approve_ward_flag(request: Request,
    report_id: str = Form(...),
    decision:  str = Form(...),
    reason:    str = Form(default=""),
    csrf:      str = Form(default="")):
    staff, mc = require_login_fc(request)
    if not staff: return RedirectResponse("/login", status_code=302)
    token = request.cookies.get(COOKIE_NAME, "")
    if not verify_csrf_token(token, csrf):
        return RedirectResponse("/staff?error=csrf", status_code=302)

    if not permissions.check_role(staff, "ae", "admin", "commissioner", "zonal_commissioner"):
        return permissions.redirect_home(staff)

    reason, _ = sanitize_input(reason)
    approved  = decision == "approve"

    ok, msg = database.approve_ward_reassignment(
        report_id, approved, staff["name"], reason
    )

    if ok:
        action = "✅ Ward reassignment approved" if approved else "❌ Ward flag rejected"
        database.add_comment(report_id,
            f"{action} by {staff['name']}. {reason or ''}".strip(),
            staff["name"])

    referer   = request.headers.get("referer", "/staff")
    safe_next = referer if referer and ("/staff" in referer or "/commissioner" in referer) else "/staff"
    return RedirectResponse(safe_next, status_code=302)


# ── DISPUTED REVIEWS ──────────────────────────────────────────────────────────

@app.get("/disputed-reviews", response_class=HTMLResponse)
async def disputed_reviews(request: Request):
    staff, mc = require_login_fc(request)
    if not staff: return RedirectResponse("/login", status_code=302)
    if mc: return RedirectResponse("/change-password?forced=1", status_code=302)

    if not permissions.check_role(staff, "commissioner", "admin", "zonal_commissioner", "ae"):
        return permissions.redirect_home(staff)

    division = staff.get("zone","") if staff["role"] in ("ae","zonal_commissioner") else None
    reports  = database.get_disputed_reports_for_review(division_name=division)
    token    = request.cookies.get(COOKIE_NAME,"")

    return templates.TemplateResponse(request, "disputed_reviews.html", {"staff": staff,
        "reports": reports, "csrf": generate_csrf_token(token)})

@app.post("/resolve-dispute", response_class=HTMLResponse)
async def resolve_dispute(request: Request,
    report_id: str = Form(...),
    decision:  str = Form(...),
    note:      str = Form(default=""),
    csrf:      str = Form(default="")):
    staff, mc = require_login_fc(request)
    if not staff: return RedirectResponse("/login", status_code=302)
    token = request.cookies.get(COOKIE_NAME,"")
    if not verify_csrf_token(token, csrf):
        return RedirectResponse("/disputed-reviews?error=csrf", status_code=302)

    if not permissions.check_role(staff, "commissioner", "admin", "zonal_commissioner", "ae"):
        return permissions.redirect_home(staff)

    note, _ = sanitize_input(note)

    if decision == "reopen":
        database.update_report_status(report_id, "assigned", staff["name"])
        database.add_comment(report_id,
            f"⚠️ Dispute reviewed by {staff['name']} — Reopened for re-inspection. {note}",
            staff["name"])
        database.save_citizen_review(report_id, False, "Reopened by head after photo review")
    else:
        database.update_report_status(report_id, "closed", staff["name"])
        database.add_comment(report_id,
            f"✅ Dispute reviewed by {staff['name']} — Closed as complete. {note}",
            staff["name"])

    return RedirectResponse("/disputed-reviews", status_code=302)


# ── WORK DONE PHOTO UPLOAD ─────────────────────────────────────────────────────

@app.post("/work-done-upload", response_class=HTMLResponse)
async def work_done_upload(request: Request,
    report_id:       str = Form(...),
    work_done_photo: UploadFile = File(...),
    work_done_lat:   str = Form(default=""),
    work_done_lng:   str = Form(default=""),
    work_note:       str = Form(default=""),
    csrf:            str = Form(default="")):
    staff, mc = require_login_fc(request)
    if not staff: return RedirectResponse("/login", status_code=302)
    token = request.cookies.get(COOKIE_NAME,"")
    if not verify_csrf_token(token, csrf):
        return RedirectResponse("/field?error=csrf", status_code=302)

    if not permissions.check_role(staff, *permissions.FIELD_ROLES):
        return permissions.redirect_home(staff)

    try:
        photo_bytes = await work_done_photo.read()
        safe_fn, _  = sanitize_filename(work_done_photo.filename or "work_done.jpg")
        inspect     = deep_inspect_photo(photo_bytes, safe_fn)
        if not inspect["safe"]:
            return RedirectResponse(f"/field?error=Photo+rejected:+{inspect['error'][:50]}",
                                    status_code=302)
        try:
            from storage import upload_work_done_photo
            cloud_url = upload_work_done_photo(inspect['clean_bytes'], report_id, inspect['ext'])
            photo_data = cloud_url if cloud_url else f"data:image/{inspect['ext']};base64,{base64.b64encode(inspect['clean_bytes']).decode()}"
        except Exception as e:
            print(f"[work_done] Cloudinary error: {e}")
            photo_data = f"data:image/{inspect['ext']};base64,{base64.b64encode(inspect['clean_bytes']).decode()}"
        lat = None; lng = None
        try:
            if work_done_lat and work_done_lng:
                lat = float(work_done_lat)
                lng = float(work_done_lng)
        except (ValueError, TypeError):
            pass

        database.mark_work_done(
            report_id,
            photo_data,
            staff["name"],
            work_done_lat = lat,
            work_done_lng = lng,
        )
        if work_note:
            work_note, _ = sanitize_input(work_note)
            database.add_comment(report_id, f"🔨 Work done: {work_note}", staff["name"])
        database.add_comment(report_id,
            f"✅ Work completed by {staff['name']}. Photo uploaded. Pending citizen verification.",
            staff["name"])

        # FIX 6 + FIX 10 — notify citizen and pass token into review URL
        try:
            report = database.get_report_by_id(report_id)
            if report and report.get("citizen_phone"):
                # FIX 10: token was generated but discarded before.
                # Now we pass it explicitly so review URL is token-gated.
                review_token = database.create_citizen_review_token(report_id)
                base_url     = os.getenv("APP_BASE_URL", "https://roadseva.onrender.com")
                review_url   = f"{base_url}/citizen-review/{report_id}?token={review_token}"

                notify_citizen(
                    event     = "resolved",
                    report_id = report_id,
                    name      = report.get("citizen_name",""),
                    phone     = report.get("citizen_phone",""),
                    email     = report.get("citizen_email",""),
                    ward      = report.get("ward",""),
                    review_url = review_url,
                )
        except Exception as e:
            print(f"[work_done] notify_citizen error: {e}")

    except Exception as e:
        print(f"[work_done] error: {e}")
        return RedirectResponse("/field?error=Upload+failed", status_code=302)

    return RedirectResponse("/field", status_code=302)


# ── TRACK ──────────────────────────────────────────────────────────────────────

@app.get("/track", response_class=HTMLResponse)
async def track_get(request: Request,
    report_id:   str = "",
    ref:         str = "",
    phone:       str = "",
    search_type: str = "id"):
    staff, _ = require_login(request)

    report_id = report_id or ref

    if report_id:
        query = report_id.strip().upper()
        r = database.get_report_by_id(query)
        if r:
            return templates.TemplateResponse(request, "track.html", {"searched": True, "reports": [r],
                "query": query, "error": "", "is_staff": bool(staff),
                "search_type": "id", "from_archive": False})
        archived = database.get_archived_report(query)
        if archived:
            return templates.TemplateResponse(request, "track.html", {"searched": True, "reports": [archived],
                "query": query, "error": "", "is_staff": bool(staff),
                "search_type": "id", "from_archive": True})
        return templates.TemplateResponse(request, "track.html", {"searched": True, "reports": [],
            "query": query, "error": "Complaint not found.", "is_staff": bool(staff),
            "search_type": "id", "from_archive": False})

    if phone and staff:
        reports = database.get_archived_reports_by_phone(phone.strip())
        return templates.TemplateResponse(request, "track.html", {"searched": True, "reports": reports,
            "query": phone.strip(), "error": "", "is_staff": bool(staff),
            "search_type": "phone", "from_archive": False})

    return templates.TemplateResponse(request, "track.html", {"searched": False, "reports": [],
        "query": "", "error": "", "is_staff": bool(staff),
        "search_type": "id", "from_archive": False})

@app.post("/track", response_class=HTMLResponse)
async def track_post(request: Request,
    report_id:   str = Form(default=""),
    phone:       str = Form(default=""),
    search_type: str = Form(default="id")):
    staff, _ = require_login(request)
    ip = get_real_ip(request)

    ok, reason, retry = check_rate_limit("track", ip)
    if not ok:
        return templates.TemplateResponse(request, "track.html", {"searched": False, "reports": [],
            "query": "", "search_type": search_type,
            "is_staff": bool(staff),
            "error": "Too many lookups. Please wait before trying again."})

    reports = []; query = ""; error = ""; from_archive = False
    if search_type == "phone" and staff:
        query = phone.strip()
        if query:
            reports = database.get_archived_reports_by_phone(query)
        else:
            error = "Please enter a phone number"
    else:
        query = report_id.strip().upper()
        if query:
            r = database.get_report_by_id(query)
            if r:
                reports = [r]
            else:
                archived = database.get_archived_report(query)
                if archived:
                    reports      = [archived]
                    from_archive = True
                else:
                    error = f"No complaint found with ID: {query}"
        else:
            error = "Please enter a grievance reference number"

    return templates.TemplateResponse(request, "track.html", {"searched": True, "reports": reports,
        "query": query, "error": error,
        "is_staff": bool(staff), "search_type": search_type,
        "from_archive": from_archive})


# ── LOGIN / LOGOUT ─────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_get(request: Request):
    staff, _ = require_login(request)
    if staff:
        return RedirectResponse(ROLE_HOME.get(staff["role"],"/staff"), status_code=302)
    return templates.TemplateResponse(request, "login.html", {"error": ""})

@app.post("/login", response_class=HTMLResponse)
async def login_post(request: Request,
    username: str = Form(...), password: str = Form(...)):
    ip       = get_real_ip(request)
    username = username.strip().lower()
    staff, err = database.authenticate_staff(username, password, ip)
    if not staff:
        return templates.TemplateResponse(request, "login.html", {"error": err})
    token = database.create_session(staff["id"])
    dest  = "/change-password?forced=1" if staff.get("must_change_password") \
            else ROLE_HOME.get(staff["role"],"/staff")
    response = RedirectResponse(dest, status_code=302)
    set_session_cookie(response, token)
    return response

@app.get("/logout")
async def logout(request: Request):
    token = request.cookies.get(COOKIE_NAME,"")
    if token: database.delete_session(token)
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie(COOKIE_NAME)
    return response


# ── CHANGE PASSWORD ────────────────────────────────────────────────────────────

@app.get("/change-password", response_class=HTMLResponse)
async def change_password_get(request: Request, forced: str="0"):
    staff, mc = require_login_fc(request)
    if not staff: return RedirectResponse("/login", status_code=302)
    token = request.cookies.get(COOKIE_NAME,"")
    return templates.TemplateResponse(request, "change_password.html", {"staff": staff,
        "is_forced": mc or forced=="1",
        "csrf": generate_csrf_token(token), "error": "", "message": ""})

@app.post("/change-password", response_class=HTMLResponse)
async def change_password_post(request: Request,
    current_password: str = Form(...),
    new_password:     str = Form(...),
    confirm_password: str = Form(...),
    forced:           str = Form(default="0"),
    csrf:             str = Form(default="")):
    staff, mc = require_login_fc(request)
    if not staff: return RedirectResponse("/login", status_code=302)
    token = request.cookies.get(COOKIE_NAME,"")
    if not verify_csrf_token(token, csrf):
        return templates.TemplateResponse(request, "change_password.html", {"staff": staff, "is_forced": mc or forced=="1",
            "csrf": generate_csrf_token(token),
            "error": "Session expired. Please try again.", "message": ""})
    if new_password != confirm_password:
        return templates.TemplateResponse(request, "change_password.html", {"staff": staff, "is_forced": mc or forced=="1",
            "csrf": generate_csrf_token(token),
            "error": "New passwords do not match.", "message": ""})
    ok, err = database.change_password(staff["id"], current_password, new_password)
    if not ok:
        return templates.TemplateResponse(request, "change_password.html", {"staff": staff, "is_forced": mc or forced=="1",
            "csrf": generate_csrf_token(token), "error": err, "message": ""})
    return RedirectResponse(ROLE_HOME.get(staff["role"],"/staff"), status_code=302)


# ── STAFF DASHBOARD ────────────────────────────────────────────────────────────

@app.get("/staff", response_class=HTMLResponse)
async def staff_dashboard(request: Request,
    status: str="", ward: str="", severity: str="",
    search: str="", damage_type: str="", page: int=1, page_size: int=50):
    staff, mc = require_login_fc(request)
    if not staff:  return RedirectResponse("/login", status_code=302)
    if mc:         return RedirectResponse("/change-password?forced=1", status_code=302)

    if permissions.deny_role(staff, *permissions.FIELD_ROLES):
        return RedirectResponse("/field", status_code=302)
    if permissions.deny_role(staff, *permissions.TRIAGE_ROLES):
        return RedirectResponse("/triage", status_code=302)

    page_size  = page_size if page_size in (10,20,50,100) else 50
    offset     = (page-1) * page_size
    eff_status = status if status else "active_only"

    reports = database.get_reports_for_role(staff, status=eff_status,
        ward=ward or None, severity=severity or None,
        search=search or None, damage_type=damage_type or None,
        limit=page_size, offset=offset)

    all_reports  = database.get_reports_for_role(staff, limit=10000)
    all_audits   = database.get_all_audits_grouped()
    all_comments = database.get_all_comments_grouped()

    if staff["role"] == "ae":
        officers = database.get_engineers_under_by_name(staff["name"])
    else:
        officers = database.get_active_officers()

    ward_list  = sorted(set(r.get("ward","") for r in all_reports if r.get("ward")))
    token      = request.cookies.get(COOKIE_NAME,"")
    sla_cutoff = (datetime.now()-timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")

    return templates.TemplateResponse(request, "staff.html", {"staff": staff, "reports": reports,
        "all_reports": all_reports, "all_audits": all_audits,
        "all_comments": all_comments, "officers": officers,
        "engineers": officers, "ward_list": ward_list,
        "filter_status": status, "filter_ward": ward,
        "filter_severity": severity, "filter_search": search,
        "filter_damage": damage_type,
        "page": page, "csrf": generate_csrf_token(token),
        "ward_names": WARD_NAMES, "showing_active": not status,
        "sla_cutoff": sla_cutoff, "page_size": page_size,
        "role_labels": ROLE_LABELS,
        "stat_open":       sum(1 for r in all_reports if r.get("status")=="open"),
        "stat_assigned":   sum(1 for r in all_reports if r.get("status")=="assigned"),
        "stat_inspecting": sum(1 for r in all_reports if r.get("status")=="inspecting"),
        "stat_inspected":  sum(1 for r in all_reports if r.get("status")=="inspected"),
        "stat_resolved":   sum(1 for r in all_reports if r.get("status") in ("resolved","closed")),
        "stat_total":      len(all_reports)})

# ── UPDATE STATUS ──────────────────────────────────────────────────────────────

@app.post("/update-status", response_class=HTMLResponse)
async def update_status(request: Request,
    report_id:       str = Form(...),
    new_status:      str = Form(...),
    csrf:            str = Form(default=""),
    contractor_name: str = Form(default=""),
    road_asset_id:   str = Form(default=""),
    next_url:        str = Form(default="")):
    staff, mc = require_login_fc(request)
    if not staff: return RedirectResponse("/login", status_code=302)
    token = request.cookies.get(COOKIE_NAME,"")
    if not verify_csrf_token(token, csrf):
        return RedirectResponse("/staff?error=csrf", status_code=302)

    if staff["role"] == "viewer":
        return RedirectResponse("/staff", status_code=302)
    if permissions.deny_role(staff, *permissions.FIELD_ROLES) and new_status in ("resolved","closed","open","assigned"):
        return RedirectResponse("/field", status_code=302)
    if staff["role"] == "ae":
        report = database.get_report_by_id(report_id)
        if report and new_status == "resolved" and report.get("status") != "inspected":
            return RedirectResponse(
                "/staff?error=Cannot+mark+resolved+until+field+engineer+submits+site+report",
                status_code=302)

    # ── Contractor attribution guard ──────────────────────────────────────────
    # A repair closure without contractor attribution permanently loses
    # durability and procurement data. Enforce before writing status.
    contractor_name = contractor_name.strip() if contractor_name else ""
    if new_status in ("resolved", "closed"):
        if not contractor_name:
            # Block closure — return error, do NOT update status
            safe_next = next_url if next_url and next_url.startswith("/staff") else "/staff"
            return RedirectResponse(
                f"{safe_next}?error=Contractor+name+is+required+before+marking+work+done.+Enter+contractor+name+or+'GVMC+Direct'+for+municipal+works.",
                status_code=302,
            )

    database.update_report_status(report_id, new_status, staff["name"])

    if new_status in ("resolved", "closed") and contractor_name:
        try:
            r = database.get_report_by_id(report_id)
            if r and r.get("latitude") and r.get("longitude"):
                repair_rec_id = database.add_repair_record(
                    report_id, contractor_name,
                    float(r["latitude"]), float(r["longitude"]), staff["name"]
                )
                # ── Link to road asset chain if staff confirmed one ────────────
                # road_asset_id is submitted as a hidden field by the closure modal
                if repair_rec_id and road_asset_id:
                    try:
                        from linkage import link_repair_to_contract
                        link_result = link_repair_to_contract(
                            repair_record_id=repair_rec_id,
                            road_asset_id=int(road_asset_id),
                            confirmed_by=staff["name"],
                        )
                        if not link_result.get("ok"):
                            print(f"[linkage] link failed: {link_result.get('error')}")
                    except Exception as le:
                        print(f"[linkage] link_repair_to_contract error: {le}")
        except Exception as e:
            print(f"[rqi] repair record error: {e}")

    if permissions.deny_role(staff, *permissions.FIELD_ROLES):
        return RedirectResponse("/field", status_code=302)
    safe_next = next_url if next_url and next_url.startswith("/staff") else "/staff"
    return RedirectResponse(safe_next, status_code=302)


# ── ASSIGN REPORT ──────────────────────────────────────────────────────────────

@app.post("/assign-report", response_class=HTMLResponse)
async def assign_report(request: Request,
    report_id:   str = Form(...),
    assigned_to: str = Form(...),
    csrf:        str = Form(default="")):
    staff, mc = require_login_fc(request)
    if not staff: return RedirectResponse("/login", status_code=302)
    token = request.cookies.get(COOKIE_NAME,"")
    if not verify_csrf_token(token, csrf):
        return RedirectResponse("/staff?error=csrf", status_code=302)

    if not permissions.check_role(staff, "admin", "commissioner", "zonal_commissioner", "ae"):
        return permissions.redirect_home(staff)

    ok, msg = database.assign_report(report_id, assigned_to, staff["name"],
                            assigned_officer=staff["name"])
    referer   = request.headers.get("referer","/staff")
    safe_next = referer if referer and "/staff" in referer else "/staff"
    if not ok:
        sep = "&" if "?" in safe_next else "?"
        return RedirectResponse(f"{safe_next}{sep}error={urllib.parse.quote(msg)}", status_code=302)
    return RedirectResponse(safe_next, status_code=302)

# ── ADD COMMENT ────────────────────────────────────────────────────────────────

@app.post("/add-comment", response_class=HTMLResponse)
async def add_comment(request: Request,
    report_id: str = Form(...),
    comment:   str = Form(...),
    csrf:      str = Form(default="")):
    staff, mc = require_login_fc(request)
    if not staff: return RedirectResponse("/login", status_code=302)
    token = request.cookies.get(COOKIE_NAME,"")
    if not verify_csrf_token(token, csrf):
        return RedirectResponse(ROLE_HOME.get(staff["role"],"/staff"), status_code=302)
    comment, _ = sanitize_input(comment)
    database.add_comment(report_id, comment, staff["name"])
    referer = request.headers.get("referer","")
    if "/field" in referer:
        return RedirectResponse("/field", status_code=302)
    if "/triage" in referer:
        return RedirectResponse("/triage", status_code=302)
    return RedirectResponse("/staff", status_code=302)


# ── FIELD DASHBOARD ────────────────────────────────────────────────────────────

@app.get("/field", response_class=HTMLResponse)
async def field_dashboard(request: Request):
    staff, mc = require_login_fc(request)
    if not staff: return RedirectResponse("/login", status_code=302)
    if mc: return RedirectResponse("/change-password?forced=1", status_code=302)

    if not permissions.check_role(staff, *permissions.FIELD_ROLES):
        return permissions.redirect_home(staff)

    all_active = database.get_all_reports(
        status="active_only", assigned_to=staff["name"], limit=200)
    active = [r for r in all_active if r["status"] in ("assigned","inspecting","inspected")]

    sev_order = {"critical":0,"high":1,"medium":2,"low":3,"unknown":4}
    active.sort(key=lambda r: sev_order.get(r.get("severity","unknown"),4))

    token        = request.cookies.get(COOKIE_NAME,"")
    sla_cutoff   = (datetime.now()-timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    all_comments = database.get_all_comments_grouped()

    pending_flags = database.get_pending_ward_flags()
    my_flags = [f for f in pending_flags
                if f.get("ward_flag_by") == staff["name"]]

    # Pending inspection tasks for this officer
    pending_inspections = database.get_pending_inspections_for_officer(staff["name"])

    return templates.TemplateResponse(request, "field.html", {"staff": staff, "reports": active,
        "csrf": generate_csrf_token(token),
        "sla_cutoff": sla_cutoff, "all_comments": all_comments,
        "ward_names": WARD_NAMES,
        "pending_flags": my_flags,
        "role_labels": ROLE_LABELS,
        "pending_inspections": pending_inspections,
    })



# ── SUBMIT INSPECTION RESULT ──────────────────────────────────────────────────

@app.post("/submit-inspection", response_class=HTMLResponse)
async def submit_inspection(request: Request,
    inspection_id:   str  = Form(...),
    condition_score: int  = Form(...),
    condition_notes: str  = Form(default=""),
    inspect_lat:     str  = Form(default=""),
    inspect_lng:     str  = Form(default=""),
    photo:           UploadFile = File(default=None),
    csrf:            str  = Form(default="")):
    staff, mc = require_login_fc(request)
    if not staff: return RedirectResponse("/login", status_code=302)
    token = request.cookies.get(COOKIE_NAME, "")
    if not verify_csrf_token(token, csrf):
        return RedirectResponse("/field?error=csrf", status_code=302)
    if not permissions.check_role(staff, *permissions.FIELD_ROLES):
        return permissions.redirect_home(staff)

    # Handle photo upload
    photo_data = ""
    if photo and photo.filename:
        try:
            raw = await photo.read()
            if len(raw) > 0:
                from PIL import Image
                import io
                img = Image.open(io.BytesIO(raw))
                img.thumbnail((1200, 1200))
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=75)
                import base64
                photo_data = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
        except Exception as e:
            print(f"[inspection] photo error: {e}")

    flat = float(inspect_lat) if inspect_lat else None
    flng = float(inspect_lng) if inspect_lng else None

    result = database.submit_inspection_result(
        inspection_id   = int(inspection_id),
        condition_score = condition_score,
        condition_notes = condition_notes,
        photo_data      = photo_data,
        inspect_lat     = flat,
        inspect_lng     = flng,
        recorded_by     = staff["name"],
    )

    if not result.get("ok"):
        return RedirectResponse(
            f"/field?error=Inspection+error:+{result.get('error','unknown')[:60]}",
            status_code=302)

    if result.get("breach_candidate"):
        return RedirectResponse(
            "/field?notice=Inspection+submitted.+Low+score+flagged+as+warranty+breach+candidate+for+commissioner+review.",
            status_code=302)

    return RedirectResponse("/field?notice=Inspection+submitted+successfully.", status_code=302)

@app.get("/route", response_class=HTMLResponse)
async def route_page(request: Request):
    staff, mc = require_login_fc(request)
    if not staff: return RedirectResponse("/login", status_code=302)
    return RedirectResponse("/field", status_code=302)

# ── START INSPECTION ───────────────────────────────────────────────────────────

@app.post("/start-inspection", response_class=HTMLResponse)
async def start_inspection(request: Request,
    report_id: str = Form(...), csrf: str = Form(default="")):
    staff, mc = require_login_fc(request)
    if not staff: return RedirectResponse("/login", status_code=302)
    if not permissions.check_role(staff, *permissions.FIELD_ROLES):
        return permissions.redirect_home(staff)
    token = request.cookies.get(COOKIE_NAME,"")
    if not verify_csrf_token(token, csrf):
        return RedirectResponse("/field", status_code=302)
    database.update_report_status(report_id, "inspecting", staff["name"])
    return RedirectResponse("/field", status_code=302)


# ── WAS VERIFY DAMAGE — STEP 2 (before photo + damage type correction) ─────────
# WAS is physically on site. Corrects damage type, uploads before photo.
# This is the primary AI training point — ground truth from a field officer.

@app.post("/was-verify-damage", response_class=HTMLResponse)
async def was_verify_damage_post(
    request:              Request,
    report_id:            str        = Form(...),
    verified_damage_type: str        = Form(...),
    site_condition:       str        = Form(...),
    site_photo:           UploadFile = File(...),
    inspection_notes:     str        = Form(default=""),
    verify_lat:           str        = Form(default=""),
    verify_lng:           str        = Form(default=""),
    csrf:                 str        = Form(default=""),
):
    staff, mc = require_login_fc(request)
    if not staff: return RedirectResponse("/login", status_code=302)
    if not permissions.check_role(staff, *permissions.FIELD_ROLES):
        return permissions.redirect_home(staff)
    token = request.cookies.get(COOKIE_NAME,"")
    if not verify_csrf_token(token, csrf):
        return RedirectResponse("/field?error=Session+expired", status_code=302)

    report = database.get_report_by_id(report_id)
    if not report:
        return RedirectResponse("/field?error=Report+not+found", status_code=302)
    if report.get("status") != "inspecting":
        return RedirectResponse("/field?error=Complaint+not+in+inspecting+status", status_code=302)

    # Read and validate photo
    raw = await site_photo.read()
    if not raw:
        return RedirectResponse("/field?error=Before+photo+is+required", status_code=302)

    try:
        safe_fn, _ = sanitize_filename(site_photo.filename or "before.jpg")
        inspect    = deep_inspect_photo(raw, safe_fn)
        if not inspect["safe"]:
            return RedirectResponse(
                f"/field?error=Photo+rejected:+{inspect['error'][:50]}", status_code=302
            )
    except ValueError as e:
        return RedirectResponse(
            f"/field?error={urllib.parse.quote(str(e))}", status_code=302
        )

    # Upload to Cloudinary or base64 fallback
    photo_data = ""
    try:
        from storage import upload_to_cloudinary
        cloud_url  = upload_to_cloudinary(inspect["clean_bytes"], report_id + "_before")
        photo_data = cloud_url if cloud_url else (
            f"data:image/{inspect['ext']};base64,"
            + base64.b64encode(inspect["clean_bytes"]).decode()
        )
    except Exception as e:
        print(f"[was_verify] Cloudinary error: {e}")
        photo_data = (
            f"data:image/{inspect['ext']};base64,"
            + base64.b64encode(inspect["clean_bytes"]).decode()
        )

    # Parse GPS coordinates
    lat = None; lng = None
    try:
        if verify_lat and verify_lng:
            lat = float(verify_lat)
            lng = float(verify_lng)
    except (ValueError, TypeError):
        pass

    ok = database.was_verify_damage(
        report_id            = report_id,
        verified_damage_type = verified_damage_type,
        site_condition       = site_condition,
        before_photo_data    = photo_data,
        verified_by          = staff["name"],
        notes                = inspection_notes,
        verify_lat           = lat,
        verify_lng           = lng,
    )

    if not ok:
        return RedirectResponse("/field?error=Verification+failed", status_code=302)

    return RedirectResponse("/field?verified=1", status_code=302)


# ── COMPLETE INSPECTION (legacy — kept for backward compatibility) ──────────────
# This route is no longer called from field.html.
# WAS now uses /was-verify-damage (step 2) and /work-done-upload (step 3).
# Kept here so existing bookmarks or old form submissions don't 404.

@app.post("/complete-inspection", response_class=HTMLResponse)
async def complete_inspection(request: Request,
    report_id:        str = Form(...),
    damage_confirmed: str = Form(default="yes"),
    action_taken:     str = Form(default=""),
    inspection_note:  str = Form(default=""),
    csrf:             str = Form(default="")):
    staff, mc = require_login_fc(request)
    if not staff: return RedirectResponse("/login", status_code=302)
    token = request.cookies.get(COOKIE_NAME,"")
    if not verify_csrf_token(token, csrf):
        return RedirectResponse("/field", status_code=302)

    new_status = "inspected" if damage_confirmed == "yes" else "open"
    database.update_report_status(report_id, new_status, staff["name"])

    note_parts = []
    if action_taken:    note_parts.append(f"Action: {action_taken}")
    if inspection_note: note_parts.append(inspection_note)
    if damage_confirmed == "no": note_parts.append("Damage not found on site.")
    if note_parts:
        database.add_comment(report_id, " | ".join(note_parts), staff["name"])

    if damage_confirmed == "yes":
        try:
            report = database.get_report_by_id(report_id)
            if report:
                database.save_training_sample(
                    report_id            = report_id,
                    ward                 = report.get("ward",""),
                    citizen_damage_type  = report.get("damage_type",""),
                    verified_damage_type = action_taken or report.get("damage_type",""),
                    severity             = report.get("severity","unknown"),
                    site_condition       = "same",
                    verified_by          = staff["name"],
                    verified_at          = database.now(),
                    photo_data           = "",
                    is_override          = False,
                )
        except Exception as e:
            print(f"[training] complete-inspection error: {e}")

    return RedirectResponse("/field", status_code=302)

# ── REJECT INSPECTION ──────────────────────────────────────────────────────────

@app.post("/reject-inspection", response_class=HTMLResponse)
async def reject_inspection(request: Request,
    report_id:   str = Form(...),
    reject_note: str = Form(default=""),
    csrf:        str = Form(default="")):
    staff, mc = require_login_fc(request)
    if not staff: return RedirectResponse("/login", status_code=302)
    if not permissions.check_role(staff, *permissions.FIELD_ROLES):
        return permissions.redirect_home(staff)
    token = request.cookies.get(COOKIE_NAME,"")
    if not verify_csrf_token(token, csrf):
        return RedirectResponse("/field", status_code=302)
    note = reject_note.strip() or "No damage found at this location."
    database.update_report_status(report_id, "open", staff["name"])
    database.add_comment(report_id,
        f"⚠️ SITE VISIT: Damage not found. Engineer: {staff['name']}. Note: {note}",
        staff["name"])
    return RedirectResponse("/field", status_code=302)


# ── VERIFY INSPECTION ──────────────────────────────────────────────────────────

@app.post("/verify-inspection", response_class=HTMLResponse)
async def verify_inspection(request: Request,
    report_id:            str = Form(...),
    verified_damage_type: str = Form(...),
    site_condition:       str = Form(default="same"),
    inspection_notes:     str = Form(default=""),
    site_photo:           UploadFile = File(default=None),
    override_reason:      str = Form(default=""),
    csrf:                 str = Form(default="")):
    staff, mc = require_login_fc(request)
    if not staff: return RedirectResponse("/login", status_code=302)
    token = request.cookies.get(COOKIE_NAME,"")
    if not verify_csrf_token(token, csrf):
        return RedirectResponse("/field", status_code=302)

    report = database.get_report_by_id(report_id)
    if not report:
        return RedirectResponse("/field?error=Report+not+found", status_code=302)

    is_assigned_engineer = (report.get("assigned_to") == staff["name"])
    is_admin = staff["role"] == "admin"
    is_override = False

    if not is_assigned_engineer and not is_admin:
        parent_name = database.get_parent_name_for_engineer(report.get("assigned_to", ""))
        if staff["name"] != parent_name:
            return RedirectResponse("/field?error=Only+the+assigned+engineer+or+their+supervisor+can+submit+this", status_code=302)
        if int(report.get("escalation_level", 0) or 0) < 1:
            return RedirectResponse("/field?error=SLA+not+yet+breached+—+wait+or+contact+the+assigned+engineer", status_code=302)
        if not override_reason.strip():
            return RedirectResponse("/field?error=Override+reason+required", status_code=302)
        is_override = True

    if not site_photo or not site_photo.filename:
        return RedirectResponse("/field?error=Site+photo+required", status_code=302)

    site_photo_data = ""
    try:
        site_bytes = await site_photo.read()
        safe_fn, _ = sanitize_filename(site_photo.filename)
        insp       = deep_inspect_photo(site_bytes, safe_fn)
        if not insp["safe"]:
            return RedirectResponse("/field?error=Photo+rejected", status_code=302)
        site_photo_data = f"data:image/{insp['ext']};base64,{base64.b64encode(insp['clean_bytes']).decode()}"
    except Exception as e:
        print(f"[verify] site photo error: {e}")

    database.save_inspection_verification(report_id, verified_damage_type,
        site_condition, site_photo_data, staff["name"], inspection_notes,
        is_override=is_override, override_reason=override_reason)
    database.update_report_status(report_id, "inspected", staff["name"])

    if not permissions.check_role(staff, "ae", "admin", "commissioner",
                                  "zonal_commissioner", *permissions.FIELD_ROLES):
        return permissions.redirect_home(staff)

    referer = request.headers.get("referer","")
    return RedirectResponse("/staff" if "/staff" in referer else "/field", status_code=302)


# ── COMMISSIONER DASHBOARD ─────────────────────────────────────────────────────

@app.get("/commissioner", response_class=HTMLResponse)
async def commissioner(request: Request):
    staff, mc = require_login_fc(request)
    if not staff: return RedirectResponse("/login", status_code=302)
    if mc: return RedirectResponse("/change-password?forced=1", status_code=302)

    if not permissions.check_role(staff, *permissions.COMMISSIONER_ROLES):
        return permissions.redirect_home(staff)

    ward_filter = None
    if staff["role"] == "zonal_commissioner":
        from wards import get_wards_for_zone
        zone = staff.get("zone","")
        ward_filter = get_wards_for_zone(zone) if zone else []

    data = database.get_commissioner_data(ward_filter=ward_filter)

    from watchdog import get_sla_dashboard_data
    inspection_stats = database.get_inspection_stats_for_commissioner()
    return templates.TemplateResponse(request, "commissioner.html", {"staff": staff,
        "data":             data,
        "sla":              get_sla_dashboard_data(),
        "inspection_stats": inspection_stats,
    })


# ── API STATS ──────────────────────────────────────────────────────────────────

@app.get("/api/linkage-candidates")
async def api_linkage_candidates(request: Request,
    lat:  str = "",
    lng:  str = "",
    ward: str = ""):
    """
    Returns road asset candidates for a given GPS point.
    Called by the closure modal JS before staff confirms.
    Staff confirms/overrides — result is never auto-applied.
    """
    staff, _ = require_login(request)
    if not staff:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        from linkage import find_road_asset_candidates
        flat = float(lat) if lat else None
        flng = float(lng) if lng else None
        result = find_road_asset_candidates(flat, flng, ward=ward)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({
            "error": str(e),
            "confidence": "NO_MATCH",
            "candidates": [],
            "recommended": None,
            "reason": "Linkage lookup failed — manual selection required."
        })


@app.get("/api/stats")
async def api_stats(request: Request):
    staff, _ = require_login(request)
    if not staff: return {"error": "unauthorized"}

    role     = staff.get("role","")
    division = staff.get("zone","")

    if role in permissions.FIELD_ROLES:
        return database.get_live_stats_for_role(staff["name"], role)
    elif role in ("ae","zonal_commissioner") and division:
        return database.get_live_stats_for_role(staff["name"], role, division=division)
    return database.get_live_stats()

# ── ANALYTICS ─────────────────────────────────────────────────────────────────

@app.get("/analytics", response_class=HTMLResponse)
async def analytics(request: Request):
    staff, mc = require_login_fc(request)
    if not staff: return RedirectResponse("/login", status_code=302)
    if mc: return RedirectResponse("/change-password?forced=1", status_code=302)
    if permissions.deny_role(staff, *permissions.FIELD_ROLES):
        return RedirectResponse("/field", status_code=302)
    data = database.get_analytics_data()
    return templates.TemplateResponse(request, "analytics.html", {"staff": staff, "data": data, "now_ist": _now_ist()})

# ── MAP ────────────────────────────────────────────────────────────────────────

@app.get("/map", response_class=HTMLResponse)
async def map_page(request: Request):
    staff, mc = require_login_fc(request)
    if not staff: return RedirectResponse("/login", status_code=302)
    if mc: return RedirectResponse("/change-password?forced=1", status_code=302)
    if permissions.deny_role(staff, *permissions.FIELD_ROLES):
        return RedirectResponse("/field", status_code=302)
    reports     = database.get_all_reports(limit=500)
    geo_reports = [r for r in reports if r.get("latitude") and r.get("longitude")]
    return templates.TemplateResponse(request, "map.html", {"staff": staff, "reports": geo_reports})

# ── EXPORT CSV ─────────────────────────────────────────────────────────────────

@app.get("/export")
async def export_csv(request: Request):
    staff, mc = require_login_fc(request)
    if not staff: return RedirectResponse("/login", status_code=302)
    if permissions.deny_role(staff, *permissions.FIELD_ROLES):
        return RedirectResponse("/field", status_code=302)
    reports = database.get_all_reports(limit=10000)
    output  = io.StringIO()
    writer  = csv.writer(output)
    writer.writerow(["Report ID","Ward","Division","Damage Type","Verified Damage Type",
        "Status","Severity","Assigned To","Assigned Officer","Submitted At",
        "Updated At","Citizen Name","Latitude","Longitude","Description",
        "Location Text","Intake Channel","Site Condition","Verified By","Verified At",
        "Escalation Level","Escalation Count","Dispute Count","Citizen Review"])
    for r in reports:
        writer.writerow([
            r.get("report_id",""),        r.get("ward",""),
            r.get("division_name",""),    r.get("damage_type",""),
            r.get("verified_damage_type",""), r.get("status",""),
            r.get("severity",""),         r.get("assigned_to",""),
            r.get("assigned_officer",""), r.get("submitted_at",""),
            r.get("updated_at",""),       r.get("citizen_name",""),
            r.get("latitude",""),         r.get("longitude",""),
            r.get("description",""),      r.get("location_text",""),
            r.get("intake_channel",""),   r.get("site_condition",""),
            r.get("verified_by",""),      r.get("verified_at",""),
            r.get("escalation_level",0),  r.get("escalation_count",0),
            r.get("dispute_count",0),     r.get("citizen_satisfied",""),
        ])
    output.seek(0)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=roadseva_export_{ts}.csv"})

# ── EXPORT TRAINING DATA ───────────────────────────────────────────────────────

@app.get("/export/training-data")
async def export_training_data(request: Request):
    staff, mc = require_login_fc(request)
    if not staff or staff["role"] != "admin":
        return RedirectResponse("/login", status_code=302)
    stats = database.get_training_stats()
    path  = database.TRAINING_CSV
    if not os.path.exists(path):
        return HTMLResponse(
            f"<p>No training data yet. {stats['total']} samples collected so far.</p>",
            status_code=404)
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return StreamingResponse(iter([content]), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=roadseva_training_{ts}.csv"})

# ── ADMIN PANEL ────────────────────────────────────────────────────────────────

@app.get("/admin", response_class=HTMLResponse)
async def admin_get(request: Request):
    staff, mc = require_login_fc(request)
    if not staff: return RedirectResponse("/login", status_code=302)
    if mc: return RedirectResponse("/change-password?forced=1", status_code=302)
    if staff["role"] != "admin":
        return RedirectResponse(ROLE_HOME.get(staff["role"],"/staff"), status_code=302)
    all_staff = database.get_all_staff()
    token     = request.cookies.get(COOKIE_NAME,"")
    return templates.TemplateResponse(request, "admin.html", {"staff": staff, "all_staff": all_staff,
        "csrf": generate_csrf_token(token), "message": "", "error": ""})

@app.post("/admin/add-staff", response_class=HTMLResponse)
async def admin_add_staff(request: Request,
    name: str=Form(...), username: str=Form(...),
    password: str=Form(...), role: str=Form(...),
    zone: str=Form(default=""), division: str=Form(default=""),
    ward_list: str=Form(default=""),
    phone: str=Form(default=""), csrf: str=Form(default="")):
    staff, mc = require_login_fc(request)
    if not staff or staff["role"] != "admin":
        return RedirectResponse("/login", status_code=302)
    token = request.cookies.get(COOKIE_NAME,"")
    if not verify_csrf_token(token, csrf):
        return RedirectResponse("/admin", status_code=302)
    name, _     = sanitize_input(name)
    username    = username.strip().lower()
    zone, _     = sanitize_input(zone)
    division, _ = sanitize_input(division)
    ward_list, _= sanitize_input(ward_list)
    phone, _    = sanitize_input(phone)
    ok, result = database.add_staff(name, username, password, role, staff["name"],
                                     zone=zone, division=division, ward_list=ward_list, phone=phone)
    all_staff  = database.get_all_staff()
    return templates.TemplateResponse(request, "admin.html", {"staff": staff, "all_staff": all_staff,
        "csrf": generate_csrf_token(token),
        "message": f"Account created. Temp password: {result}" if ok else "",
        "error": result if not ok else ""})

@app.post("/admin/toggle-staff", response_class=HTMLResponse)
async def admin_toggle_staff(request: Request,
    staff_id: int=Form(...), csrf: str=Form(default="")):
    staff, mc = require_login_fc(request)
    if not staff or staff["role"] != "admin":
        return RedirectResponse("/login", status_code=302)
    token = request.cookies.get(COOKIE_NAME,"")
    if not verify_csrf_token(token, csrf):
        return RedirectResponse("/admin", status_code=302)
    database.toggle_staff_active(staff_id, staff["name"])
    return RedirectResponse("/admin", status_code=302)

# ── TEAM MANAGEMENT ────────────────────────────────────────────────────────────

@app.get("/team", response_class=HTMLResponse)
async def team_get(request: Request, message: str="", error: str=""):
    staff, mc = require_login_fc(request)
    if not staff: return RedirectResponse("/login", status_code=302)
    if mc: return RedirectResponse("/change-password?forced=1", status_code=302)
    if not permissions.check_role(staff, "commissioner", "zonal_commissioner", "ae", "admin"):
        return permissions.redirect_home(staff)
    team_members = database.get_team_members(
        staff["role"], staff["username"], staff.get("id"))
    creatable    = database.ROLE_CAN_CREATE.get(staff["role"], set())
    token        = request.cookies.get(COOKIE_NAME,"")
    return templates.TemplateResponse(request, "team.html", {"staff": staff, "team_members": team_members,
        "creatable_roles": sorted(creatable), "role_labels": ROLE_LABELS,
        "csrf": generate_csrf_token(token), "message": message, "error": error})

@app.post("/team/add-member", response_class=HTMLResponse)
async def team_add_member(request: Request,
    name:          str = Form(...),
    username:      str = Form(...),
    role:          str = Form(...),
    temp_password: str = Form(...),
    zone:          str = Form(default=""),
    division:      str = Form(default=""),
    ward_list:     str = Form(default=""),
    phone:         str = Form(default=""),
    supervised_by: int = Form(default=0),
    csrf:          str = Form(default="")):
    staff, mc = require_login_fc(request)
    if not staff: return RedirectResponse("/login", status_code=302)
    token   = request.cookies.get(COOKIE_NAME,"")
    if not verify_csrf_token(token, csrf):
        return RedirectResponse("/team?error=Session+expired", status_code=302)
    allowed = database.ROLE_CAN_CREATE.get(staff["role"], set())
    if role not in allowed:
        return RedirectResponse("/team?error=Not+allowed", status_code=302)
    name, _      = sanitize_input(name)
    username     = username.strip().lower()
    zone, _      = sanitize_input(zone)
    division, _  = sanitize_input(division)
    ward_list, _ = sanitize_input(ward_list)
    phone, _     = sanitize_input(phone)
    sup_id = staff["id"] if staff["role"] == "ae" else (supervised_by or None)
    ok, result = database.add_staff(name, username, temp_password, role,
                                     staff["name"], must_change=True,
                                     supervised_by=sup_id,
                                     zone=zone, division=division, ward_list=ward_list, phone=phone)
    if ok:
        msg = urllib.parse.quote(f"Account created for {name}. Temp password: {temp_password}")
        return RedirectResponse(f"/team?message={msg}", status_code=302)
    err = urllib.parse.quote(str(result))
    return RedirectResponse(f"/team?error={err}", status_code=302)

@app.post("/team/reset-password", response_class=HTMLResponse)
async def team_reset_password(request: Request,
    staff_id: int=Form(...), csrf: str=Form(default="")):
    staff, mc = require_login_fc(request)
    if not staff: return RedirectResponse("/login", status_code=302)
    token = request.cookies.get(COOKIE_NAME,"")
    if not verify_csrf_token(token, csrf):
        return RedirectResponse("/team?error=Session+expired", status_code=302)
    ok, new_pass = database.reset_staff_password(staff_id, staff["name"])
    if ok:
        msg = urllib.parse.quote(f"Password reset. New temp password: {new_pass}")
        return RedirectResponse(f"/team?message={msg}", status_code=302)
    err = urllib.parse.quote(str(new_pass))
    return RedirectResponse(f"/team?error={err}", status_code=302)

@app.post("/team/toggle", response_class=HTMLResponse)
async def team_toggle(request: Request,
    staff_id: int=Form(...), csrf: str=Form(default="")):
    staff, mc = require_login_fc(request)
    if not staff: return RedirectResponse("/login", status_code=302)
    token = request.cookies.get(COOKIE_NAME,"")
    if not verify_csrf_token(token, csrf):
        return RedirectResponse("/team?error=Session+expired", status_code=302)
    ok, result = database.toggle_staff_active(staff_id, staff["name"])
    if ok:
        return RedirectResponse(f"/team?message=Account+{result}", status_code=302)
    return RedirectResponse(f"/team?error={result}", status_code=302)

# ── STAFF LOG ──────────────────────────────────────────────────────────────────

@app.get("/staff-log", response_class=HTMLResponse)
async def staff_log_get(request: Request):
    staff, mc = require_login_fc(request)
    if not staff: return RedirectResponse("/login", status_code=302)
    if mc: return RedirectResponse("/change-password?forced=1", status_code=302)
    if not permissions.check_role(staff, "admin", "commissioner", "zonal_commissioner", "ae"):
        return permissions.redirect_home(staff)
    token = request.cookies.get(COOKIE_NAME,"")
    return templates.TemplateResponse(request, "staff_log.html", {"staff": staff,
        "csrf": generate_csrf_token(token), "wards": WARD_NAMES,
        "message": "", "error": "", "report_id": ""})

@app.post("/staff-log", response_class=HTMLResponse)
async def staff_log_post(request: Request,
    citizen_name:   str = Form(default=""),
    citizen_phone:  str = Form(default=""),
    ward:           str = Form(...),
    damage_type:    str = Form(...),
    description:    str = Form(default=""),
    latitude:       str = Form(default=""),
    longitude:      str = Form(default=""),
    intake_channel: str = Form(default="phone"),
    intake_ref:     str = Form(default=""),
    csrf:           str = Form(default="")):
    staff, mc = require_login_fc(request)
    if not staff: return RedirectResponse("/login", status_code=302)
    if not permissions.check_role(staff, "admin", "commissioner", "zonal_commissioner", "ae"):
        return permissions.redirect_home(staff)
    token = request.cookies.get(COOKIE_NAME,"")
    if not verify_csrf_token(token, csrf):
        return RedirectResponse("/staff-log?error=Session+expired", status_code=302)
    citizen_name, _  = sanitize_input(citizen_name)
    citizen_phone, _ = sanitize_input(citizen_phone)
    ward, _          = sanitize_input(ward)
    damage_type, _   = sanitize_input(damage_type)
    description, _   = sanitize_input(description)
    intake_ref, _    = sanitize_input(intake_ref)
    try:    lat = float(latitude) if latitude else None
    except: lat = None
    try:    lng = float(longitude) if longitude else None
    except: lng = None
    report_id = database.add_report("GVMC", ward, damage_type, description, "",
        citizen_name, citizen_phone, "", lat, lng,
        severity="unknown", photo_data="",
        intake_channel=intake_channel, intake_ref=intake_ref)
    return templates.TemplateResponse(request, "staff_log.html", {"staff": staff,
        "csrf": generate_csrf_token(token), "wards": WARD_NAMES,
        "message": "Complaint logged successfully.", "error": "",
        "report_id": report_id})

# ── RQI ────────────────────────────────────────────────────────────────────────

@app.get("/rqi", response_class=HTMLResponse)
async def rqi_page(request: Request):
    staff, mc = require_login_fc(request)
    if not staff: return RedirectResponse("/login", status_code=302)
    if mc: return RedirectResponse("/change-password?forced=1", status_code=302)
    if not permissions.check_role(staff, *permissions.COMMISSIONER_ROLES):
        return permissions.redirect_home(staff)
    rqi_raw = database.get_rqi_data()
    database.mark_rqi_seen()
    data = _build_rqi_data(rqi_raw["events"])
    return templates.TemplateResponse(request, "rqi.html", {"staff": staff, "data": data})


# ── BREACH REVIEW ─────────────────────────────────────────────────────────────

@app.get("/breach-review", response_class=HTMLResponse)
async def breach_review_page(request: Request):
    staff, mc = require_login_fc(request)
    if not staff: return RedirectResponse("/login", status_code=302)
    if mc: return RedirectResponse("/change-password?forced=1", status_code=302)
    if not permissions.check_role(staff, *permissions.COMMISSIONER_ROLES):
        return permissions.redirect_home(staff)
    token    = request.cookies.get(COOKIE_NAME, "")
    candidates = database.get_breach_candidates()
    perf_summary = database.get_contractor_performance_summary()
    return templates.TemplateResponse(request, "breach_review.html", {
        "staff":       staff,
        "candidates":  candidates,
        "summary":     perf_summary,
        "csrf":        generate_csrf_token(token),
    })


@app.post("/breach-review/verify", response_class=HTMLResponse)
async def breach_verify(request: Request,
    event_id: int = Form(...),
    verdict:  str = Form(...),
    notes:    str = Form(default=""),
    csrf:     str = Form(default="")):
    staff, mc = require_login_fc(request)
    if not staff: return RedirectResponse("/login", status_code=302)
    token = request.cookies.get(COOKIE_NAME, "")
    if not verify_csrf_token(token, csrf):
        return RedirectResponse("/breach-review?error=csrf", status_code=302)
    if not permissions.check_role(staff, *permissions.COMMISSIONER_ROLES):
        return permissions.redirect_home(staff)
    result = database.verify_breach(event_id, verdict, staff["name"], notes)
    if not result.get("ok"):
        return RedirectResponse(
            f"/breach-review?error={result.get('error','unknown')[:80]}",
            status_code=302)
    msg = "Breach+verified+and+added+to+procurement+record." if verdict == "verified_recurrence"           else "Candidate+dismissed+and+reason+recorded."
    return RedirectResponse(f"/breach-review?notice={msg}", status_code=302)


# ── CONTRACT INTELLIGENCE ──────────────────────────────────────────────────────

@app.get("/contracts", response_class=HTMLResponse)
async def contracts_page(request: Request):
    staff, mc = require_login_fc(request)
    if not staff: return RedirectResponse("/login", status_code=302)
    if mc: return RedirectResponse("/change-password?forced=1", status_code=302)
    if not permissions.check_role(staff, *permissions.COMMISSIONER_ROLES):
        return permissions.redirect_home(staff)
    summary     = database.get_contract_intelligence_summary()
    contracts   = database.get_contracts()
    contractors = database.get_contractors()
    road_assets = database.get_road_assets()
    performance = database.get_contractor_performance()
    return templates.TemplateResponse(request, "contracts.html", {
        "staff":        staff,
        "summary":      summary,
        "contracts":    contracts,
        "contractors":  contractors,
        "road_assets":  road_assets,
        "performance":  performance,
    })

# ── WARDS ──────────────────────────────────────────────────────────────────────

@app.get("/wards", response_class=HTMLResponse)
async def wards_page(request: Request):
    staff, _ = require_login(request)
    raw = database.get_all_ward_stats()
    raw.sort(key=lambda x: -x["resolution_rate"])
    wards = [{"ward":w["ward"],"total":w["total"],"resolved":w["resolved"],
               "open":w["open_count"],"rate":w["resolution_rate"],
               "avg_days":0,"rank":i+1}
             for i,w in enumerate(raw)]
    return templates.TemplateResponse(request, "wards.html", {"staff": staff,
        "wards": wards, "total_wards": len(wards)})

@app.get("/ward/{ward_name:path}", response_class=HTMLResponse)
async def ward_public(request: Request, ward_name: str):
    ward_name = urllib.parse.unquote(ward_name)
    data      = database.get_ward_stats(ward_name)
    staff, _  = require_login(request)
    return templates.TemplateResponse(request, "ward_public.html", {"staff": staff, "data": data, "ward": ward_name})

# ── ACCOUNT LOG ────────────────────────────────────────────────────────────────

@app.get("/account-log", response_class=HTMLResponse)
async def account_log(request: Request):
    staff, mc = require_login_fc(request)
    if not staff: return RedirectResponse("/login", status_code=302)
    if mc: return RedirectResponse("/change-password?forced=1", status_code=302)
    if not permissions.check_role(staff, "admin", "commissioner"):
        return permissions.redirect_home(staff)
    log = database.get_staff_audit_log(200)
    return templates.TemplateResponse(request, "account_log.html", {"staff": staff, "log": log})

# ── CREDENTIAL CARD ────────────────────────────────────────────────────────────

@app.get("/credential-card/{staff_id}", response_class=HTMLResponse)
async def credential_card(request: Request, staff_id: int):
    staff, mc = require_login_fc(request)
    if not staff: return RedirectResponse("/login", status_code=302)
    if not permissions.check_role(staff, "admin", "commissioner", "zonal_commissioner", "ae"):
        return permissions.redirect_home(staff)
    target = database.get_staff_by_id(staff_id)
    if not target: return RedirectResponse("/team", status_code=302)
    return templates.TemplateResponse(request, "credential_card.html", {"staff": staff, "target": target,
        "name": target["name"], "username": target["username"],
        "role": ROLE_LABELS.get(target["role"], target["role"]),
        "org":  database.get_system_setting("org_name") or "GVMC",
        "created_by": target.get("created_by") or staff["name"],
        "issued_at":  (target.get("created_at") or database.now())[:10],
        "temp_password": "", "first_run": "0"})

# ── SETUP WIZARD ───────────────────────────────────────────────────────────────

@app.get("/setup", response_class=HTMLResponse)
async def setup_get(request: Request):
    if database.is_setup_complete():
        return RedirectResponse("/login", status_code=302)
    token = request.cookies.get(COOKIE_NAME,"")
    return templates.TemplateResponse(request, "setup.html", {"csrf": generate_csrf_token(token) if token else csrf_token_citizen(),
        "org": database.get_system_setting("org_name") or "",
        "error": "", "message": ""})

@app.post("/setup", response_class=HTMLResponse)
async def setup_post(request: Request,
    org_name:              str = Form(default="Greater Visakhapatnam Municipal Corporation"),
    commissioner_name:     str = Form(...),
    commissioner_username: str = Form(...),
    commissioner_password: str = Form(...),
    csrf:                  str = Form(default="")):
    if database.is_setup_complete():
        return RedirectResponse("/login", status_code=302)
    token = request.cookies.get(COOKIE_NAME,"")
    def render_error(msg):
        return templates.TemplateResponse(request, "setup.html", {"csrf": generate_csrf_token(token) if token else csrf_token_citizen(),
            "org": org_name, "error": msg, "message": ""})
    commissioner_name, _     = sanitize_input(commissioner_name)
    commissioner_username, _ = sanitize_input(commissioner_username.strip().lower())
    org_name, _              = sanitize_input(org_name)
    ok, err = database.validate_password_strength(commissioner_password)
    if not ok: return render_error(err)
    ok, result = database.setup_first_commissioner(
        commissioner_name, commissioner_username, commissioner_password, org_name)
    if not ok: return render_error(result)
    new_staff = database.get_staff_by_username(commissioner_username)
    if not new_staff: return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(request, "credential_card.html", {"staff": {"name": "Setup Wizard", "role": "admin"},
        "target": new_staff, "name": commissioner_name,
        "username": commissioner_username, "role": "Commissioner",
        "org": org_name, "created_by": "IT Setup Wizard",
        "issued_at": database.now()[:10],
        "temp_password": commissioner_password, "first_run": "1"})

# ── PRIVACY / HEALTH ───────────────────────────────────────────────────────────

@app.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request):
    return templates.TemplateResponse(request, "privacy.html", {})

@app.get("/health")
async def health():
    return {"status": "ok", "service": "roadseva"}

@app.get("/health/live")
async def health_live():
    return {"status": "ok"}

@app.get("/health/watchdog")
async def health_watchdog():
    from watchdog import _scheduler, get_sla_dashboard_data

    status = {
        "scheduler_running": False,
        "jobs":              [],
        "sla_breached_now":  0,
        "sla_warning_now":   0,
        "last_check":        database.now(),
    }

    if _scheduler and _scheduler.running:
        status["scheduler_running"] = True
        for job in _scheduler.get_jobs():
            status["jobs"].append({
                "id":       job.id,
                "next_run": str(job.next_run_time) if job.next_run_time else "unknown",
            })

    try:
        sla = get_sla_dashboard_data()
        status["sla_breached_now"]  = sla.get("sla_breached_now", 0)
        status["sla_warning_now"]   = sla.get("sla_warning_now", 0)
        status["total_escalations"] = sla.get("total_escalations", 0)
    except Exception as e:
        status["sla_error"] = str(e)

    if not status["scheduler_running"]:
        _send_error_alert(
            "⚠️ RoadSeva Watchdog DOWN\n"
            "APScheduler stopped — SLA escalations not running."
        )
        return JSONResponse(status_code=503, content={
            "status": "error",
            "detail": "Watchdog scheduler not running",
            **status
        })

    return {"status": "ok", **status}

@app.get("/health/ready")
async def health_ready():
    try:
        conn = database.get_conn(); conn.close()
        return {"status": "ready"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}

@app.get("/export/resolved-backup")
async def export_resolved_backup(request: Request):
    staff, mc = require_login_fc(request)
    if not staff or staff["role"] not in ("admin","commissioner"):
        return RedirectResponse("/login", status_code=302)
    backup_path = database._get_csv_backup_path()
    if not os.path.exists(backup_path):
        return HTMLResponse("<p>No resolved complaints backup yet.</p>", status_code=404)
    with open(backup_path, "r", encoding="utf-8") as f:
        content = f.read()
    yr = datetime.now(timezone(timedelta(hours=5,minutes=30))).year
    return StreamingResponse(iter([content]), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=resolved_{yr}.csv"})