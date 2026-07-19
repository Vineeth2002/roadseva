"""
storage.py — RoadSeva Photo Storage Module
===========================================
Uploads photos to Cloudinary and returns a URL string.
Replaces base64 photo_data stored directly in the database.

Why Cloudinary instead of base64 in DB:
  - 1000 complaints × 2MB photo = 2.7GB in PostgreSQL — unsustainable
  - Cloudinary free tier: 25GB storage, 25GB bandwidth/month
  - Photos served via CDN — faster for citizens tracking complaints
  - DB stays lean — only URL strings stored (varchar ~200 chars)

Environment variables required:
  CLOUDINARY_CLOUD_NAME  — your Cloudinary cloud name
  CLOUDINARY_API_KEY     — your Cloudinary API key
  CLOUDINARY_API_SECRET  — your Cloudinary API secret

Fallback behaviour:
  If Cloudinary is not configured, upload_photo() returns None.
  main.py then falls back to base64 storage in photo_data column.
  This means the system works correctly with OR without Cloudinary.

Usage in main.py /submit:
  photo_url = upload_photo(clean_bytes, report_id, ext)
  if photo_url:
      # store URL in photo_url column
  else:
      # fall back to base64 in photo_data column
"""

import os
import io
import logging

log = logging.getLogger("roadseva.storage")


def _is_configured() -> bool:
    return bool(
        os.getenv("CLOUDINARY_CLOUD_NAME") and
        os.getenv("CLOUDINARY_API_KEY") and
        os.getenv("CLOUDINARY_API_SECRET")
    )


def _get_config() -> dict:
    return {
        "cloud_name": os.getenv("CLOUDINARY_CLOUD_NAME", ""),
        "api_key":    os.getenv("CLOUDINARY_API_KEY", ""),
        "api_secret": os.getenv("CLOUDINARY_API_SECRET", ""),
    }


def upload_photo(photo_bytes: bytes, report_id: str, ext: str = "jpeg") -> str | None:
    """
    Upload a photo to Cloudinary.

    Args:
        photo_bytes: Clean, re-encoded image bytes (already inspected by deep_inspect_photo)
        report_id:   GVMC complaint ID used as the public_id in Cloudinary
        ext:         File extension (jpeg / png / webp)

    Returns:
        Secure URL string on success (e.g. https://res.cloudinary.com/...)
        None on failure or if Cloudinary is not configured

    Never raises — storage failure must not crash the complaint submission flow.
    """
    if not _is_configured():
        log.warning("[storage] Cloudinary not configured — falling back to base64. "
                    "Set CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET.")
        return None

    try:
        import cloudinary
        import cloudinary.uploader

        cfg = _get_config()
        cloudinary.config(
            cloud_name=cfg["cloud_name"],
            api_key=cfg["api_key"],
            api_secret=cfg["api_secret"],
            secure=True,
        )

        # Use report_id as public_id so photos are traceable
        # Folder: roadseva/complaints/GVMC-2606-LTT02E
        public_id = f"roadseva/complaints/{report_id}"

        result = cloudinary.uploader.upload(
            io.BytesIO(photo_bytes),
            public_id=public_id,
            resource_type="image",
            format=ext,
            overwrite=True,
            # Transformation: auto quality, auto format for faster delivery
            transformation=[
                {"quality": "auto", "fetch_format": "auto"}
            ],
            # Tags for management
            tags=["roadseva", "complaint", "road_damage"],
        )

        url = result.get("secure_url", "")
        if url:
            log.info(f"[storage] Uploaded {report_id} → {url[:60]}...")
            return url
        else:
            log.error(f"[storage] Cloudinary returned no URL for {report_id}")
            return None

    except ImportError:
        log.warning("[storage] cloudinary package not installed. Run: pip install cloudinary")
        return None
    except Exception as e:
        log.error(f"[storage] Upload failed for {report_id}: {type(e).__name__}: {str(e)[:200]}")
        return None


def upload_work_done_photo(photo_bytes: bytes, report_id: str, ext: str = "jpeg") -> str | None:
    """
    Upload field engineer's work-done (after) photo to Cloudinary.
    Stored separately from citizen complaint photo for before/after comparison.

    Returns: Secure URL or None
    """
    if not _is_configured():
        return None

    try:
        import cloudinary
        import cloudinary.uploader

        cfg = _get_config()
        cloudinary.config(
            cloud_name=cfg["cloud_name"],
            api_key=cfg["api_key"],
            api_secret=cfg["api_secret"],
            secure=True,
        )

        public_id = f"roadseva/work_done/{report_id}_after"

        result = cloudinary.uploader.upload(
            io.BytesIO(photo_bytes),
            public_id=public_id,
            resource_type="image",
            format=ext,
            overwrite=True,
            transformation=[{"quality": "auto", "fetch_format": "auto"}],
            tags=["roadseva", "work_done", "after_photo"],
        )

        url = result.get("secure_url", "")
        if url:
            log.info(f"[storage] Work-done photo uploaded {report_id} → {url[:60]}...")
            return url
        return None

    except Exception as e:
        log.error(f"[storage] Work-done upload failed {report_id}: {str(e)[:200]}")
        return None


def delete_photo(report_id: str) -> bool:
    """
    Delete a complaint photo from Cloudinary.
    Used only by admin — complaints cannot be deleted per civic record rules,
    but photos may need to be removed for privacy compliance.

    Returns True on success, False on failure.
    """
    if not _is_configured():
        return False

    try:
        import cloudinary
        import cloudinary.uploader

        cfg = _get_config()
        cloudinary.config(
            cloud_name=cfg["cloud_name"],
            api_key=cfg["api_key"],
            api_secret=cfg["api_secret"],
            secure=True,
        )

        result = cloudinary.uploader.destroy(
            f"roadseva/complaints/{report_id}",
            resource_type="image",
        )
        success = result.get("result") == "ok"
        if success:
            log.info(f"[storage] Deleted photo for {report_id}")
        else:
            log.warning(f"[storage] Could not delete photo for {report_id}: {result}")
        return success

    except Exception as e:
        log.error(f"[storage] Delete failed for {report_id}: {str(e)[:200]}")
        return False