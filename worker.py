import os
import logging
from redis import Redis
from rq import Worker, Queue
from severity import analyse_severity
import database

log = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL")
if not REDIS_URL:
    raise RuntimeError("REDIS_URL is required to run worker.py")

redis_conn = Redis.from_url(REDIS_URL)
q = Queue(connection=redis_conn)

def process_severity(report_id, file_key, damage_type):
    try:
        result = analyse_severity(file_key, damage_type)
        database.update_report_severity(
            report_id,
            result.get("severity", "medium"),
            result.get("severity_details", ""),
            result.get("estimated_cost", ""),
            result.get("urgency", "")
        )
        log.info(f"Severity processed for {report_id}: {result.get('severity')}")
    except Exception as e:
        log.error(f"process_severity failed for {report_id}: {e}")
        raise  # re-raise so rq marks job as failed

if __name__ == "__main__":
    worker = Worker([q], connection=redis_conn)
    worker.work()
