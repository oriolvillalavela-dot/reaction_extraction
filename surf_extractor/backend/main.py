"""
SURF Extractor – FastAPI Application

Endpoints:
  POST /extract          – Upload PDFs + instructions, returns job_id immediately
  GET  /status/{job_id}  – Poll job progress
  GET  /download/{job_id} – Download the generated SURF TSV file
  GET  /health           – Health check (also pings PortKey gateway)
"""

from __future__ import annotations

import asyncio
import logging
import os

# Load .env before anything else reads environment variables
from dotenv import load_dotenv
load_dotenv()
import uuid
import tempfile
from pathlib import Path
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import httpx

from backend.models import JobProgress, JobStatus
from backend.agents.coordinator import CoordinatorAgent

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Persistent output directory for generated SURF files
# ---------------------------------------------------------------------------
OUTPUTS_DIR = Path(__file__).resolve().parents[1] / "outputs"
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# In-memory job registry (suitable for single-worker deployments)
# For production, replace with Redis or a database.
# ---------------------------------------------------------------------------
_jobs: dict[str, dict] = {}   # job_id → {"progress": JobProgress, "tsv_path": str|None}


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("SURF Extractor starting up…")
    yield
    logger.info("SURF Extractor shutting down.")


app = FastAPI(
    title="SURF Extractor",
    description=(
        "Multi-agent chemical reaction data extractor. "
        "Parses scientific PDFs with MERMaid, extracts SURF data with Gemini 2.5 Pro "
        "via PortKey/Galileo gateway, resolves chemical identifiers via ChemConverter."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Helper: run the coordinator in a background thread (it's synchronous I/O)
# ---------------------------------------------------------------------------

def _run_coordinator(
    job_id: str,
    main_pdf: str,
    si_pdf: Optional[str],
    user_instructions: str,
    use_visualheist: bool,
    large_model: bool,
):
    """Runs in a thread pool executor; updates job registry throughout."""

    def on_status(status: str, message: str):
        _jobs[job_id]["progress"].status = JobStatus(status) if status in JobStatus._value2member_map_ else JobStatus.EXTRACTING
        _jobs[job_id]["progress"].step = status
        _jobs[job_id]["progress"].message = message

    try:
        coordinator = CoordinatorAgent()
        tsv_content, issues = coordinator.run(
            main_pdf_path=main_pdf,
            si_pdf_path=si_pdf,
            user_instructions=user_instructions,
            use_visualheist=use_visualheist,
            large_model=large_model,
            on_status=on_status,
        )

        # Save TSV to disk
        tsv_path = OUTPUTS_DIR / f"{job_id}.tsv"
        tsv_path.write_text(tsv_content, encoding="utf-8")

        _jobs[job_id]["tsv_path"] = str(tsv_path)
        _jobs[job_id]["progress"].status = JobStatus.DONE
        _jobs[job_id]["progress"].message = f"Extraction complete. {len(issues)} review note(s)."
        _jobs[job_id]["progress"].download_url = f"/download/{job_id}"
        _jobs[job_id]["progress"].error = "; ".join(issues) if issues else None

    except Exception as exc:
        logger.error("Job %s failed: %s", job_id, exc, exc_info=True)
        _jobs[job_id]["progress"].status = JobStatus.FAILED
        _jobs[job_id]["progress"].error = str(exc)
        _jobs[job_id]["progress"].message = "Pipeline failed – see error for details."


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    """Health check. Attempts to reach the PortKey/Galileo gateway."""
    gateway_url = os.getenv("PORTKEY_HEALTH_URL", "https://us.aigw.galileo.roche.com/v1/health")
    gateway_ok = False
    gateway_msg = "not checked"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(gateway_url)
            gateway_ok = resp.status_code < 400
            gateway_msg = f"HTTP {resp.status_code}"
    except Exception as exc:
        gateway_msg = str(exc)

    return {
        "status": "ok",
        "gateway": {"url": gateway_url, "reachable": gateway_ok, "detail": gateway_msg},
    }


@app.post("/extract", response_model=JobProgress, status_code=202)
async def extract(
    main_pdf: UploadFile = File(..., description="Main publication PDF"),
    si_pdf: Optional[UploadFile] = File(None, description="Supplementary Information PDF (optional)"),
    user_instructions: str = Form(default="", description="Custom extraction rules / overrides"),
    use_visualheist: bool = Form(default=True, description="Run VisualHeist image extraction"),
    large_model: bool = Form(default=False, description="Use large VisualHeist model"),
):
    """
    Upload PDFs and start the multi-agent extraction pipeline.

    Returns a job_id immediately. Poll /status/{job_id} for progress.
    """
    job_id = str(uuid.uuid4())
    logger.info("New extraction job: %s", job_id)

    # Persist uploaded files to a temporary directory that outlives this request
    tmp_dir = tempfile.mkdtemp(prefix=f"surf_{job_id}_")

    main_pdf_path = Path(tmp_dir) / "main.pdf"
    main_pdf_path.write_bytes(await main_pdf.read())

    si_pdf_path: Optional[str] = None
    if si_pdf and si_pdf.filename:
        si_path = Path(tmp_dir) / "si.pdf"
        si_path.write_bytes(await si_pdf.read())
        si_pdf_path = str(si_path)

    # Register job
    _jobs[job_id] = {
        "progress": JobProgress(
            job_id=job_id,
            status=JobStatus.PENDING,
            step="queued",
            message="Job queued. Pipeline will start shortly.",
        ),
        "tsv_path": None,
        "tmp_dir": tmp_dir,
    }

    # Launch pipeline in a background thread (non-blocking)
    loop = asyncio.get_event_loop()
    loop.run_in_executor(
        None,  # default ThreadPoolExecutor
        _run_coordinator,
        job_id,
        str(main_pdf_path),
        si_pdf_path,
        user_instructions,
        use_visualheist,
        large_model,
    )

    return _jobs[job_id]["progress"]


@app.get("/status/{job_id}", response_model=JobProgress)
async def job_status(job_id: str):
    """Poll the status of an extraction job."""
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    return job["progress"]


@app.get("/download/{job_id}")
async def download(job_id: str):
    """Download the SURF TSV file once the job is complete."""
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    if job["progress"].status != JobStatus.DONE:
        raise HTTPException(
            status_code=409,
            detail=f"Job is not done yet. Current status: {job['progress'].status}.",
        )
    tsv_path = job.get("tsv_path")
    if not tsv_path or not Path(tsv_path).exists():
        raise HTTPException(status_code=500, detail="Output file not found on server.")

    return FileResponse(
        path=tsv_path,
        media_type="text/tab-separated-values",
        filename=f"surf_extraction_{job_id}.tsv",
    )


# ---------------------------------------------------------------------------
# Serve the frontend static files
# ---------------------------------------------------------------------------
_FRONTEND_DIR = Path(__file__).resolve().parents[1] / "frontend"
if _FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIR), html=True), name="frontend")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        reload=os.getenv("DEV", "false").lower() == "true",
    )
