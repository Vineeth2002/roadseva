import os
import json
import base64
from pathlib import Path


def analyse_severity(photo_path: str, damage_type: str) -> dict:
    """
    Analyse road damage photo using Groq Llama 4 Scout vision.
    FIX: text variable initialised before try block — no NameError in JSONDecodeError handler.
    """
    text = ""
    try:
        from groq import Groq

        api_key = os.getenv("GROQ_API_KEY", "")
        if not api_key:
            return _default_result("Groq API key not configured")

        photo_file = Path(photo_path)
        if not photo_file.exists():
            return _default_result("Photo not found")

        with open(photo_path, "rb") as f:
            image_bytes = f.read()
        image_b64 = base64.b64encode(image_bytes).decode("utf-8")

        suffix = photo_file.suffix.lower()
        mime_map = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png",  ".webp": "image/webp"
        }
        mime_type = mime_map.get(suffix, "image/jpeg")

        client = Groq(api_key=api_key)

        prompt = f"""You are an expert road infrastructure inspector for Indian municipal roads.
Analyse this road damage photo carefully.
Damage type reported by citizen: {damage_type}

Respond ONLY with a valid JSON object. No explanation, no markdown, no extra text.
Use exactly this format:

{{
  "severity": "high",
  "damage_confirmed": true,
  "damage_description": "Large pothole approximately 2 feet wide with water accumulation",
  "estimated_size": "2ft x 1.5ft",
  "accident_risk": "high",
  "urgency": "Repair within 48 hours",
  "recommended_action": "Immediate patching required before next rainfall"
}}

Rules:
- severity must be exactly one of: critical, high, medium, low
- accident_risk must be exactly one of: high, medium, low
- If photo is unclear or not a road damage photo, set severity to unknown and damage_confirmed to false
- Do NOT include cost estimates — engineer will calculate on site
- Be specific about what you see in the photo"""

        response = client.chat.completions.create(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{image_b64}"
                            }
                        },
                        {
                            "type": "text",
                            "text": prompt
                        }
                    ]
                }
            ],
            temperature=0.1,
            max_tokens=500
        )

        text = response.choices[0].message.content.strip()

        # Clean markdown fences if present
        if "```" in text:
            parts = text.split("```")
            for part in parts:
                if "{" in part:
                    text = part
                    if text.startswith("json"):
                        text = text[4:]
                    break
        text = text.strip()

        result = json.loads(text)
        severity = result.get("severity", "unknown").lower()
        description = result.get("damage_description", "")

        return {
            "severity":         severity,
            "severity_details": description,
            "estimated_cost":   "",
            "urgency":          result.get("urgency", ""),
            "accident_risk":    result.get("accident_risk", ""),
            "recommended_action": result.get("recommended_action", ""),
            "estimated_size":   result.get("estimated_size", ""),
            "damage_confirmed": result.get("damage_confirmed", True)
        }

    except json.JSONDecodeError as e:
        print(f"JSON parse error: {e} — text was: {text[:200]}")
        return _default_result(f"Could not parse AI response: {text[:100]}")
    except Exception as e:
        print(f"Groq severity analysis error: {e}")
        return _default_result(str(e)[:200])


def _default_result(reason: str = "") -> dict:
    return {
        "severity":           "unknown",
        "severity_details":   reason,
        "estimated_cost":     "",
        "urgency":            "",
        "accident_risk":      "",
        "recommended_action": "",
        "estimated_size":     "",
        "damage_confirmed":   False
    }

def retry_pending_severity():
    """
    Reprocesses reports stuck at severity='unknown' due to Groq rate limits
    or transient API failures at submission time.

    FIX (this session): originally re-read photo_path from local disk —
    but Render wipes the uploads/ folder on every deploy, so this silently
    did nothing on every startup. Now reads photo_data (base64) from the
    database instead, which survives restarts, and writes it to a temp
    file just for the duration of the analyse_severity() call.
    """
    import database
    import tempfile
    try:
        conn = database.get_conn()
        if database.USE_POSTGRES:
            rows = conn.execute("""
                SELECT report_id, photo_data, damage_type
                FROM reports
                WHERE severity = 'unknown'
                  AND CAST(submitted_at AS TIMESTAMPTZ) >= NOW() - INTERVAL '2 days'
                  AND photo_data != ''
                ORDER BY submitted_at DESC
                LIMIT 20
            """).fetchall()
        else:
            rows = conn.execute("""
                SELECT report_id, photo_data, damage_type
                FROM reports
                WHERE severity = 'unknown'
                  AND submitted_at >= date('now', '-2 days')
                  AND photo_data != ''
                ORDER BY submitted_at DESC
                LIMIT 20
            """).fetchall()
        conn.close()
        if not rows:
            return
        print(f"[severity] Retrying AI analysis for {len(rows)} pending report(s)...")
        for r in rows:
            tmp_path = None
            try:
                photo_data = r["photo_data"] or ""
                if not photo_data or not photo_data.startswith("data:"):
                    continue
                header, b64data = photo_data.split(",", 1)
                ext = header.split("/")[1].split(";")[0] or "jpg"
                image_bytes = base64.b64decode(b64data)

                with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
                    tmp.write(image_bytes)
                    tmp_path = tmp.name

                result = analyse_severity(tmp_path, r["damage_type"] or "Road Damage")
                if result["severity"] != "unknown":
                    database.update_report_severity(
                        r["report_id"],
                        result["severity"],
                        result.get("severity_details", ""),
                        result.get("estimated_cost", ""),
                        result.get("urgency", "")
                    )
                    print(f"[severity] {r['report_id']} → {result['severity']}")
            except Exception as e:
                print(f"[severity] retry failed for {r['report_id']}: {e}")
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    os.remove(tmp_path)
    except Exception as e:
        print(f"[severity] retry_pending_severity error: {e}")