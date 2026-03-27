# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Overview

This is a multi-module chemistry research platform with three interconnected projects:

- **ChemConverter** — FastAPI service for converting between IUPAC names, SMILES, CAS numbers, and InChI strings
- **MERMaid** — Pipeline for extracting chemical reactions from scientific PDFs using vision-language models
- **surf_extractor** — Five-agent AI pipeline that outputs structured SURF tab-separated reaction data from PDFs

## Running the Services

### ChemConverter
```bash
cd ChemConverter
pip install -r requirements.txt
uvicorn app:app --reload --port 8000
```
Required env vars: `CAS_CLIENT_ID`, `CAS_CLIENT_SECRET`, `CAS_TOKEN_URL`, `CAS_SERVER`

### SURF Extractor
```bash
cd surf_extractor
pip install -r requirements.txt
uvicorn backend.main:app --reload --port 8001
```
Required env var: `PORTKEY_API_KEY` (for Roche Galileo AI Gateway)

### MERMaid
```bash
cd MERMaid
pip install -e ".[full]"          # or specific extras: visualheist, dataraider, kgwizard
python scripts/run_mermaid.py     # full pipeline
# Or individual stages:
python scripts/run_visualheist.py
python scripts/run_dataraider.py
python scripts/run_kgwizard.py
# Web UI:
bash launch_webapp.sh             # Streamlit
uvicorn webapp.fastapi_app:app --reload
```
Required env var: `OPENAI_API_KEY`

## Architecture

### Data Flow (SURF Extractor — the top-level orchestrator)
```
User PDFs + Instructions
    ↓ POST /extract
[Coordinator Agent]  (backend/agents/coordinator.py)
    ├─ MERMaid Wrapper → pdfplumber/PyMuPDF text + VisualHeist images
    ├─ Extraction Agent → Gemini 2.5 Pro (via PortKey/Galileo) → draft SURF rows
    ├─ Reviewer Agent → Gemini validation, hallucination checks
    ├─ Chem Resolver Agent → ChemConverter API for PENDING_CONVERSION entries
    └─ Formatter Agent → TSV serialization
    ↓ GET /download/{job_id}
SURF .tsv file
```

### Key Architectural Decisions

**Gateway fallback**: `portkey_client.py` tries the RCN endpoint first, falls back to WAF endpoint on failure. Both require `PORTKEY_API_KEY`.

**In-memory job registry**: `main.py` stores jobs in a dict — fine for single-worker; would need Redis for multi-worker production.

**Background thread execution**: Coordinator runs in a thread pool (`asyncio.get_event_loop().run_in_executor`) to avoid blocking FastAPI's event loop.

**Chunked extraction**: Large papers are split into chunks to avoid LLM token limits (see `extraction_agent.py`).

**Lazy ML imports**: `mermaid_wrapper.py` imports VisualHeist/PyTorch lazily, allowing text-only mode without heavy ML dependencies.

**ChemConverter resolution chain**: `converters.py` tries RDKit → CIRpy → CAS API in sequence for SMILES resolution.

### Module Integration
SURF Extractor treats MERMaid and ChemConverter as black-box dependencies:
- `surf_extractor/backend/integrations/mermaid_wrapper.py` — MERMaid integration
- `surf_extractor/backend/integrations/chemconv_wrapper.py` — ChemConverter integration

### SURF Output Schema
Defined in `surf_extractor/backend/models.py` as `SURFRow` — 56 Pydantic fields covering rxn_id, source_id, reactants, solvents, catalysts, products, yields, conditions, etc.

## Key Files

| File | Purpose |
|------|---------|
| `surf_extractor/backend/main.py` | FastAPI endpoints + job registry |
| `surf_extractor/backend/agents/coordinator.py` | 5-step pipeline orchestrator |
| `surf_extractor/backend/agents/extraction_agent.py` | Gemini LLM extraction calls |
| `surf_extractor/backend/portkey_client.py` | PortKey/Galileo HTTP client with RCN→WAF fallback |
| `surf_extractor/backend/models.py` | Pydantic models including `SURFRow` schema |
| `ChemConverter/app.py` | FastAPI `/resolve` endpoint |
| `ChemConverter/cas_client.py` | CAS OAuth2 client with token caching + rate limiting |
| `MERMaid/src/visualheist/methods_visualheist.py` | PDF → cropped table/figure images |

## Environment Variables

Create `.env` files (excluded from git) in each project directory:

```
# ChemConverter/.env
CAS_CLIENT_ID=...
CAS_CLIENT_SECRET=...
CAS_TOKEN_URL=...
CAS_SERVER=...

# surf_extractor/.env
PORTKEY_API_KEY=...

# MERMaid/.env
OPENAI_API_KEY=...
```

All projects use `python-dotenv` with `load_dotenv()` at startup.

## Testing

No automated test suite. `ChemConverter/test_rate_limit.py` is a manual verification script for rate limiting behavior.
