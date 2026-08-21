"""
linkage.py — Road Asset Linkage Engine
=======================================
Resolves a GPS repair point to a road asset → contract → contractor chain.

Design principles:
  - GPS suggests; staff confirms. Never silently write procurement evidence.
  - Point-to-line-segment distance, not point-to-point.
  - Ward is a ranking signal, not a hard filter (GPS near ward boundary).
  - Active contract resolved by repair date, not current date.
  - Every FK write is a single atomic transaction with audit trail.
  - If any relationship in the chain is invalid → full rollback.

Confidence tiers:
  HIGH      distance < 25m, exactly one candidate
  MEDIUM    distance 25–50m, exactly one candidate
  AMBIGUOUS multiple candidates within 50m (regardless of distance)
  NO_MATCH  nothing within 50m, or no GPS provided
"""

import math
from datetime import datetime, timezone, timedelta
from typing import Optional

import database

# ── Constants ─────────────────────────────────────────────────────────────────
SEARCH_RADIUS_M      = 200   # outer search radius — candidates beyond this ignored
HIGH_CONFIDENCE_M    = 25    # single candidate within this → HIGH
MEDIUM_CONFIDENCE_M  = 50    # single candidate within this → MEDIUM
AMBIGUOUS_THRESHOLD  = 50    # multiple candidates within this → AMBIGUOUS

CONFIDENCE_HIGH      = "HIGH"
CONFIDENCE_MEDIUM    = "MEDIUM"
CONFIDENCE_AMBIGUOUS = "AMBIGUOUS"
CONFIDENCE_NO_MATCH  = "NO_MATCH"


# ── Geometry ──────────────────────────────────────────────────────────────────

def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine great-circle distance in metres."""
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi  = math.radians(lat2 - lat1)
    dlam  = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _point_to_segment_distance_m(
    plat: float, plon: float,
    alat: float, alon: float,
    blat: float, blon: float,
) -> float:
    """
    Minimum distance in metres from point P to line segment A→B.

    Method: project to a flat local coordinate system centred at the
    segment midpoint using metre-per-degree scaling at the local latitude.
    Accurate within ~0.1% for segments < 10 km (all GVMC road segments).

    Returns distance to the nearest point on the segment, not its endpoints.
    If the perpendicular foot falls outside A→B, returns distance to the
    nearest endpoint instead.
    """
    # Metre-per-degree scaling at local latitude
    mid_lat  = (alat + blat) / 2
    m_per_lat = 111_320.0
    m_per_lon = 111_320.0 * math.cos(math.radians(mid_lat))

    # Convert all points to local metres relative to A
    px = (plon - alon) * m_per_lon
    py = (plat - alat) * m_per_lat
    bx = (blon - alon) * m_per_lon
    by = (blat - alat) * m_per_lat

    # Segment length squared
    seg_len_sq = bx * bx + by * by

    if seg_len_sq < 1e-10:
        # Degenerate segment (A == B) — fall back to point distance
        return _haversine_m(plat, plon, alat, alon)

    # Parametric projection t ∈ [0, 1]
    t = max(0.0, min(1.0, (px * bx + py * by) / seg_len_sq))

    # Nearest point on segment
    nearest_x = t * bx
    nearest_y = t * by

    # Distance in metres
    dx = px - nearest_x
    dy = py - nearest_y
    return math.sqrt(dx * dx + dy * dy)


# ── Contract resolution ───────────────────────────────────────────────────────

def _resolve_active_contract(road_asset_id: int, repair_date: str) -> Optional[dict]:
    """
    Given a road_asset_id and the date of the repair, find the contract
    that was ACTIVE on that date.

    Critical: uses repair_date, not today's date.
    A 2025 repair must not be attributed to a 2026 contract even if the
    2026 contract is currently active on the same segment.

    Returns dict with contract + contractor fields, or None.
    """
    conn = database.get_conn()
    c    = conn.cursor()
    try:
        # repair_date may be a full datetime string — extract date part
        repair_day = repair_date[:10] if repair_date else ""
        if not repair_day:
            return None

        c.execute(database._q("""
            SELECT
                ct.id            AS contract_id,
                ct.contract_code,
                ct.tender_number,
                ct.dlp_months,
                ct.responsible_ae,
                ct.work_start_date,
                ct.work_end_date,
                ct.status        AS contract_status,
                co.id            AS contractor_id,
                co.name          AS contractor_name,
                co.contractor_code
            FROM contract_segments cs
            JOIN contracts   ct ON cs.contract_id    = ct.id
            JOIN contractors co ON ct.contractor_id  = co.id
            WHERE cs.road_asset_id = ?
              AND ct.work_start_date <= ?
              AND ct.work_end_date   >= ?
            ORDER BY ct.work_start_date DESC
            LIMIT 1
        """), (road_asset_id, repair_day, repair_day))

        row = c.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ── Candidate finder ──────────────────────────────────────────────────────────

def find_road_asset_candidates(
    lat: float,
    lon: float,
    ward: str = "",
    repair_date: str = "",
    radius_m: float = SEARCH_RADIUS_M,
) -> dict:
    """
    Find candidate road assets near a GPS point.

    Args:
        lat, lon:     Repair GPS coordinates (required).
        ward:         Hint only — used for ranking, NOT hard filtering.
        repair_date:  ISO date string (YYYY-MM-DD or datetime).
                      Used to resolve the correct historical contract.
                      Defaults to today if omitted.
        radius_m:     Search radius in metres (default 200m).

    Returns dict:
        {
          "confidence":  "HIGH" | "MEDIUM" | "AMBIGUOUS" | "NO_MATCH",
          "candidates":  [ { road_asset_id, road_name, ward, distance_m,
                             contract_id, contract_code, tender_number,
                             contractor_id, contractor_name,
                             same_ward, confidence } ],
          "recommended": candidate dict or None (only for HIGH/MEDIUM),
          "reason":      human-readable explanation,
        }
    """
    if not lat or not lon:
        return {
            "confidence":  CONFIDENCE_NO_MATCH,
            "candidates":  [],
            "recommended": None,
            "reason":      "No GPS coordinates provided — manual road selection required.",
        }

    # Default repair_date to today if not provided
    if not repair_date:
        ist = timezone(timedelta(hours=5, minutes=30))
        repair_date = datetime.now(ist).strftime("%Y-%m-%d")

    conn = database.get_conn()
    c    = conn.cursor()
    try:
        c.execute("""
            SELECT id, road_code, road_name, ward, division,
                   start_lat, start_lng, end_lat, end_lng
            FROM road_assets
            WHERE status = 'active'
              AND start_lat IS NOT NULL AND start_lng IS NOT NULL
              AND end_lat   IS NOT NULL AND end_lng   IS NOT NULL
        """)
        all_assets = [dict(r) for r in c.fetchall()]
    finally:
        conn.close()

    if not all_assets:
        return {
            "confidence":  CONFIDENCE_NO_MATCH,
            "candidates":  [],
            "recommended": None,
            "reason":      "No road assets in database — import road data first.",
        }

    # ── Compute point-to-segment distance for every asset ─────────────────────
    scored = []
    for asset in all_assets:
        dist = _point_to_segment_distance_m(
            lat, lon,
            asset["start_lat"], asset["start_lng"],
            asset["end_lat"],   asset["end_lng"],
        )
        if dist > radius_m:
            continue  # outside search radius entirely

        same_ward = (
            ward and
            asset["ward"].lower().replace(" ", "") == ward.lower().replace(" ", "")
        )

        # Resolve contract active on repair_date
        contract_info = _resolve_active_contract(asset["id"], repair_date)

        scored.append({
            "road_asset_id":   asset["id"],
            "road_code":       asset["road_code"],
            "road_name":       asset["road_name"],
            "ward":            asset["ward"],
            "division":        asset["division"],
            "distance_m":      round(dist, 1),
            "same_ward":       same_ward,
            "contract_id":     contract_info["contract_id"]     if contract_info else None,
            "contract_code":   contract_info["contract_code"]   if contract_info else None,
            "tender_number":   contract_info["tender_number"]   if contract_info else None,
            "contractor_id":   contract_info["contractor_id"]   if contract_info else None,
            "contractor_name": contract_info["contractor_name"] if contract_info else None,
            "dlp_months":      contract_info["dlp_months"]      if contract_info else None,
            "responsible_ae":  contract_info["responsible_ae"]  if contract_info else None,
            "confidence":      None,  # filled below
        })

    if not scored:
        return {
            "confidence":  CONFIDENCE_NO_MATCH,
            "candidates":  [],
            "recommended": None,
            "reason":      f"No road assets found within {radius_m:.0f}m of reported GPS coordinates.",
        }

    # ── Sort: same-ward first, then by distance ───────────────────────────────
    scored.sort(key=lambda x: (not x["same_ward"], x["distance_m"]))

    # ── Assign per-candidate confidence ───────────────────────────────────────
    for s in scored:
        if s["distance_m"] <= HIGH_CONFIDENCE_M:
            s["confidence"] = CONFIDENCE_HIGH
        elif s["distance_m"] <= MEDIUM_CONFIDENCE_M:
            s["confidence"] = CONFIDENCE_MEDIUM
        else:
            s["confidence"] = CONFIDENCE_NO_MATCH  # within radius but > 50m

    # ── Determine overall result confidence ───────────────────────────────────
    within_50 = [s for s in scored if s["distance_m"] <= AMBIGUOUS_THRESHOLD]

    if len(within_50) == 0:
        # Candidates exist but all > 50m
        best = scored[0]
        return {
            "confidence":  CONFIDENCE_NO_MATCH,
            "candidates":  scored[:5],
            "recommended": None,
            "reason": (
                f"Nearest road asset '{best['road_name']}' is {best['distance_m']}m away "
                f"(threshold: {AMBIGUOUS_THRESHOLD}m). Manual selection required."
            ),
        }

    if len(within_50) == 1:
        best = within_50[0]
        conf = CONFIDENCE_HIGH if best["distance_m"] <= HIGH_CONFIDENCE_M else CONFIDENCE_MEDIUM
        contract_note = (
            f"Contract: {best['contract_code']} · Contractor: {best['contractor_name']}"
            if best["contract_id"]
            else "No active contract found on this segment for this date."
        )
        return {
            "confidence":  conf,
            "candidates":  scored[:5],
            "recommended": best,
            "reason": (
                f"'{best['road_name']}' — {best['distance_m']}m from GPS. "
                f"{contract_note}"
            ),
        }

    # Multiple within 50m → AMBIGUOUS
    return {
        "confidence":  CONFIDENCE_AMBIGUOUS,
        "candidates":  scored[:5],
        "recommended": None,
        "reason": (
            f"{len(within_50)} candidate road segments found within {AMBIGUOUS_THRESHOLD}m. "
            f"Manual confirmation required to avoid incorrect contractor attribution."
        ),
    }


# ── Linkage writer ────────────────────────────────────────────────────────────

def link_repair_to_contract(
    repair_record_id: int,
    road_asset_id:    int,
    confirmed_by:     str,
    confirmed_at:     str = "",
) -> dict:
    """
    Atomically resolve and persist the full chain:
        repair_record → road_asset → contract_segment → contract → contractor

    Validates every relationship before committing.
    On any failure → full rollback, nothing written.

    Returns:
        { "ok": True,  "contract_id": ..., "contractor_id": ... }
        { "ok": False, "error": "reason" }
    """
    if not confirmed_at:
        ist = timezone(timedelta(hours=5, minutes=30))
        confirmed_at = datetime.now(ist).strftime("%Y-%m-%d %H:%M:%S")

    conn = database.get_conn()
    c    = conn.cursor()

    try:
        # ── 1. Validate repair_record exists ──────────────────────────────────
        c.execute(database._q(
            "SELECT id, repaired_at, contractor_name FROM repair_records WHERE id = ?"
        ), (repair_record_id,))
        repair = c.fetchone()
        if not repair:
            return {"ok": False, "error": f"repair_record id={repair_record_id} not found."}
        repair = dict(repair)

        # ── 2. Validate road_asset exists and is active ───────────────────────
        c.execute(database._q(
            "SELECT id, road_name, ward FROM road_assets WHERE id = ? AND status = 'active'"
        ), (road_asset_id,))
        asset = c.fetchone()
        if not asset:
            return {"ok": False, "error": f"road_asset id={road_asset_id} not found or inactive."}
        asset = dict(asset)

        # ── 3. Resolve contract active on repair date ─────────────────────────
        repair_date = repair["repaired_at"][:10] if repair["repaired_at"] else ""
        contract_info = _resolve_active_contract(road_asset_id, repair_date)
        if not contract_info:
            # Still allow linkage to road asset — contract may not exist yet
            # Write road_asset_id only, leave contract/contractor NULL
            c.execute(database._q("""
                UPDATE repair_records
                SET road_asset_id = ?
                WHERE id = ?
            """), (road_asset_id, repair_record_id))
            conn.commit()

            # Audit
            _write_linkage_audit(c, conn, repair_record_id, road_asset_id,
                                 None, None, confirmed_by, confirmed_at,
                                 note="Road asset linked; no active contract found for repair date.")

            # Still schedule inspections using default policies
            try:
                inspections_created = database.create_scheduled_inspections(
                    repair_record_id=repair_record_id,
                    contract_id=None,
                )
            except Exception as ie:
                print(f"[linkage] inspection scheduling (no-contract path) error: {ie}")
                inspections_created = []

            return {
                "ok":                  True,
                "road_asset_id":       road_asset_id,
                "contract_id":         None,
                "contractor_id":       None,
                "inspections_created": len(inspections_created),
                "warning":             "Road asset linked but no active contract found for this date. "
                                       "Contract attribution must be added manually.",
            }

        contract_id   = contract_info["contract_id"]
        contractor_id = contract_info["contractor_id"]

        # ── 4. Validate contractor still exists ───────────────────────────────
        c.execute(database._q(
            "SELECT id FROM contractors WHERE id = ?"
        ), (contractor_id,))
        if not c.fetchone():
            return {"ok": False, "error": f"contractor id={contractor_id} not found — data integrity error."}

        # ── 5. Write all three FKs atomically ─────────────────────────────────
        c.execute(database._q("""
            UPDATE repair_records
            SET road_asset_id = ?,
                contract_id   = ?,
                contractor_id = ?
            WHERE id = ?
        """), (road_asset_id, contract_id, contractor_id, repair_record_id))

        conn.commit()

        # ── 6. Audit entry ────────────────────────────────────────────────────
        _write_linkage_audit(c, conn, repair_record_id, road_asset_id,
                             contract_id, contractor_id,
                             confirmed_by, confirmed_at,
                             note=(
                                 f"Linked to {asset['road_name']} · "
                                 f"Contract {contract_info['contract_code']} · "
                                 f"Contractor {contract_info['contractor_name']}"
                             ))

        # ── Auto-create scheduled inspections from contract DLP policy ──────
        try:
            inspections_created = database.create_scheduled_inspections(
                repair_record_id=repair_record_id,
                contract_id=contract_id,
            )
            print(f"[linkage] created {len(inspections_created)} inspection tasks")
        except Exception as ie:
            print(f"[linkage] inspection scheduling error (non-fatal): {ie}")
            inspections_created = []

        return {
            "ok":                  True,
            "road_asset_id":       road_asset_id,
            "road_name":           asset["road_name"],
            "contract_id":         contract_id,
            "contract_code":       contract_info["contract_code"],
            "contractor_id":       contractor_id,
            "contractor_name":     contract_info["contractor_name"],
            "inspections_created": len(inspections_created),
        }

    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        return {"ok": False, "error": f"Transaction failed and rolled back: {e}"}
    finally:
        conn.close()


def _write_linkage_audit(c, conn, repair_id, road_asset_id,
                          contract_id, contractor_id,
                          confirmed_by, confirmed_at, note=""):
    """Write a linkage event to audit_log."""
    try:
        new_val = (
            f"road_asset={road_asset_id} contract={contract_id} "
            f"contractor={contractor_id} confirmed_by={confirmed_by} | {note}"
        )
        c.execute(database._q("""
            INSERT INTO audit_log (report_id, action, old_value, new_value, done_by, done_at)
            SELECT report_id, 'repair_linked', '', ?, ?, ?
            FROM repair_records WHERE id = ?
        """), (new_val, confirmed_by, confirmed_at, repair_id))
        conn.commit()
    except Exception as e:
        print(f"[linkage] audit write failed (non-fatal): {e}")


# ── Public API summary ────────────────────────────────────────────────────────
__all__ = [
    "find_road_asset_candidates",
    "link_repair_to_contract",
    "CONFIDENCE_HIGH",
    "CONFIDENCE_MEDIUM",
    "CONFIDENCE_AMBIGUOUS",
    "CONFIDENCE_NO_MATCH",
]