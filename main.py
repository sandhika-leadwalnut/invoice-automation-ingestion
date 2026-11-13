import os
import logging
from fastapi import FastAPI
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Import after load_dotenv ensures variables are loaded
from app.workflow import poll_gmail_for_invoices

app = FastAPI(title="Ingestion Service")
scheduler = BackgroundScheduler()

from datetime import datetime

@app.on_event("startup")
def startup_event():
    poll_interval = int(os.getenv("POLL_INTERVAL_SECONDS", 60))
    scheduler.add_job(
        poll_gmail_for_invoices,
        'interval',
        seconds=poll_interval,
        id='poll_invoices',
        next_run_time=datetime.now()
    )

    # Vendors who upload rather than email. Only scheduled when folders are
    # configured, so an empty DRIVE_INBOX_FOLDERS costs nothing.
    from app.drive_inbox import poll_drive_for_invoices, configured_folders
    folders = configured_folders()
    if folders:
        scheduler.add_job(
            poll_drive_for_invoices,
            'interval',
            seconds=poll_interval,
            id='poll_drive_inbox',
            next_run_time=datetime.now(),
        )

    scheduler.start()
    logger.info(f"Started polling Gmail every {poll_interval} seconds")
    if folders:
        logger.info(
            "Also polling %d Drive folder(s) every %ds: %s",
            len(folders), poll_interval,
            ", ".join(f.get("vendor") or f["folder_id"] for f in folders),
        )
    else:
        logger.info("No Drive inbox folders configured (DRIVE_INBOX_FOLDERS)")


@app.on_event("shutdown")
def shutdown_event():
    scheduler.shutdown()
    logger.info("Scheduler shut down")

@app.get("/health")
def health_check():
    return {"status": "ok"}
