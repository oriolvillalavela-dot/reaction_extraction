# SURF Extractor

**Multi-Agent Chemical Reaction Data Extractor**

Extracts chemical reaction data from scientific publication PDFs (Main Paper + Supplementary Information) and structures it into the strict SURF tab-separated format using a five-agent AI pipeline powered by **Gemini 2.5 Pro** via the **Roche Galileo / PortKey** gateway.

---

## Architecture

```
User uploads PDFs + instructions
         │
         ▼
┌─────────────────────────────────────────────────────────┐
│                   Coordinator Agent                     │
│                                                         │
│  Step 1 ──► PDF Parser (MERMaid)                        │
│              VisualHeist (tables/figures as images)     │
│              pdfplumber (full text extraction)          │
│                                                         │
│  Step 2 ──► Extraction Agent                            │
│              Gemini 2.5 Pro via PortKey/Galileo         │
│              Uses core SURF extraction prompt           │
│                                                         │
│  Step 3 ──► Reviewer / Critic Agent                     │
│              Hallucination check (CAS/SMILES)           │
│              Format & vocabulary validation             │
│                                                         │
│  Step 4 ──► Chemical Resolution Agent                   │
│              ChemConverter: CASClient + CIRpy           │
│              Resolves PENDING_CONVERSION entries        │
│                                                         │
│  Step 5 ──► Formatter Agent                             │
│              SURF tab-separated CSV output              │
└─────────────────────────────────────────────────────────┘
         │
         ▼
    SURF .tsv file download
```

### External Modules (read-only)

| Module | Location | Role |
|--------|----------|------|
| **MERMaid** | `../MERMaid/` | VisualHeist (PDF → cropped table/figure images) |
| **ChemConverter** | `../ChemConverter/` | CASClient (CAS/SMILES lookup) + CIRpy converters |

> These folders are **never modified**. They are imported as black-box tools.

---

## Project Structure

```
surf_extractor/
├── backend/
│   ├── __init__.py
│   ├── main.py                  # FastAPI app (endpoints, job management)
│   ├── models.py                # Pydantic models, SURF column list
│   ├── portkey_client.py        # PortKey/Galileo HTTP client (RCN + WAF fallback)
│   ├── agents/
│   │   ├── base_agent.py        # Shared LLM call helpers
│   │   ├── coordinator.py       # Orchestration pipeline
│   │   ├── extraction_agent.py  # SURF extraction via Gemini
│   │   ├── reviewer_agent.py    # Critic / hallucination guard
│   │   ├── chem_resolver_agent.py  # ChemConverter integration
│   │   └── formatter_agent.py   # TSV output
│   └── integrations/
│       ├── mermaid_wrapper.py   # MERMaid VisualHeist + text extraction
│       └── chemconv_wrapper.py  # ChemConverter CASClient + CIRpy
├── frontend/
│   └── index.html               # Single-page UI (served by FastAPI)
├── outputs/                     # Generated SURF TSV files (auto-created)
├── requirements.txt
├── .env.example
└── run.sh
```

---

## Prerequisites

- **Python 3.10+**
- **pip**
- (Optional) **poppler-utils** for VisualHeist PDF-to-image conversion:
  - Ubuntu/Debian: `sudo apt-get install poppler-utils`
  - macOS: `brew install poppler`
- Access to the **Roche Galileo AI Gateway** (API key)
- (Optional) CAS SciFinder API credentials for ChemConverter

---

## Setup

### 1. Clone / navigate

```bash
cd /path/to/workspaces
# The surf_extractor, MERMaid, and ChemConverter directories must be siblings:
ls
# ChemConverter/   MERMaid/   surf_extractor/
```

### 2. Create a virtual environment

```bash
cd surf_extractor
python3 -m venv .venv
source .venv/bin/activate          # Linux/macOS
# .venv\Scripts\activate           # Windows
```

### 3. Install core dependencies

```bash
pip install -r requirements.txt
```

#### Install MERMaid (VisualHeist — optional but recommended)

```bash
pip install -e ../MERMaid
# Heavy ML stack – only needed if use_visualheist=true
# Requires CUDA or CPU-only torch
```

#### Install ChemConverter dependencies

```bash
pip install -e ../ChemConverter   # if it has a setup.py/pyproject.toml
# or manually:
pip install cirpy rdkit pydantic-settings
```

### 4. Configure environment

```bash
cp .env.example .env
# Edit .env with your credentials:
```

```dotenv
# Required – Galileo/PortKey gateway API key
PORTKEY_API_KEY=your_api_key_here

# Optional – CAS SciFinder OAuth (for ChemConverter resolution)
CAS_CLIENT_ID=your_cas_client_id
CAS_CLIENT_SECRET=your_cas_client_secret
CAS_TOKEN_URL=https://your-cas-oauth-endpoint/token
CAS_SERVER=https://your-cas-api-server
CAS_SCOPE=openid
```

### 5. Run the application

```bash
# Using the convenience script:
./run.sh

# Or directly:
source .env
uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
```

The app will be available at **http://localhost:8000**

---

## Usage

1. Open **http://localhost:8000** in your browser.
2. Upload the **Main Paper PDF** (required).
3. Upload the **Supplementary Information PDF** (optional but strongly recommended).
4. Enter any **custom extraction instructions** (e.g., yield type preferences, DOI location hints).
5. Toggle **VisualHeist** on/off (image extraction – slower but more accurate for table-heavy papers).
6. Click **Extract SURF Data** and monitor the five-step pipeline in real time.
7. Once complete, click **Download SURF File (.tsv)** to get the result.

### API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/health` | Health check + gateway ping |
| `POST` | `/extract` | Start extraction (multipart form) |
| `GET`  | `/status/{job_id}` | Poll job progress |
| `GET`  | `/download/{job_id}` | Download SURF TSV |
| `GET`  | `/docs` | Interactive API docs (Swagger UI) |

---

## SURF Output Format

The output is a **tab-separated file** with one reaction per row. Key columns:

| Column | Description |
|--------|-------------|
| `rxn_id` | `<author>_<year>_T<n>_E<n>` |
| `source_id` | DOI |
| `rxn_type` | e.g., `C(sp2)-Br / C(sp3)-Br` |
| `rxn_tech` | e.g., `photochemistry`, `metallophotoredox` |
| `startingmat_1_name` | Compound name as in text |
| `startingmat_1_cas` | CAS number (or `PENDING_CONVERSION`) |
| `startingmat_1_smiles` | SMILES (or `PENDING_CONVERSION`) |
| `product_1_yield` | Yield value |
| `product_1_yieldtype` | `isolated`, `NMR yield`, `LCMS yield`, etc. |
| `procedure` | SI page reference |

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| `VisualHeist not available` | Install MERMaid deps or uncheck "Use VisualHeist" |
| `CASClient unavailable` | Set CAS env vars in `.env`; CAS/SMILES will stay as `PENDING_CONVERSION` |
| `All PortKey endpoints failed` | Check `PORTKEY_API_KEY` and network access to Galileo gateway |
| `Job stuck at parsing` | Large PDFs take time; increase `PORTKEY_TIMEOUT` in `.env` |
| Empty extraction result | Add more specific instructions in the instructions box |

---

## PortKey / Galileo Gateway

The app sends all LLM requests to:

- **Primary (RCN):** `https://us.aigw.galileo.roche.com/v1`
- **Fallback (WAF):** `https://waf-us.aigw.galileo.roche.com/v1`
- **Health:** `https://us.aigw.galileo.roche.com/v1/health`
- **Model:** `gemini-2.5-pro`

The client automatically falls back to the WAF endpoint if the RCN endpoint is unreachable.
