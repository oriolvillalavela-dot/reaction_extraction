import os
from typing import Optional

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from converters import iupac_to_kekule_smiles
from cas_client import CASClient

app = FastAPI(title="IUPAC/SMILES/CAS Converter API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ResolveRequest(BaseModel):
    inputType: str 
    value: str
    fullConversion: bool = False

class ResolveResult(BaseModel):
    input: str
    name: Optional[str] = None
    smiles: Optional[str] = None
    cas: Optional[str] = None
    inchi: Optional[str] = None
    inchikey: Optional[str] = None
    error: Optional[str] = None

cas_client = CASClient()

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/resolve", response_model=ResolveResult)
def resolve(req: ResolveRequest):
    result = ResolveResult(input=req.value)
    try:
        itype = req.inputType.lower().strip()
        if itype == "iupac":
            smiles = iupac_to_kekule_smiles(req.value)
            if not smiles:
                result.error = "Could not resolve IUPAC to SMILES."
                return result
            result.smiles = smiles
            info = cas_client.lookup_by_smiles(smiles, full=req.fullConversion)
        elif itype == "smiles":
            result.smiles = req.value
            info = cas_client.lookup_by_smiles(req.value, full=req.fullConversion)
        elif itype == "cas":
            result.cas = req.value
            info = cas_client.lookup_by_cas(req.value, full=req.fullConversion)
        else:
            raise HTTPException(status_code=400, detail="Invalid inputType. Use one of: iupac, smiles, cas.")
        if info:
            result.name = info.get("name") or info.get("iupacName") or result.name
            result.cas = info.get("cas") or result.cas
            result.smiles = info.get("smiles") or result.smiles
            if req.fullConversion:
                result.inchi = info.get("inchi")
                result.inchikey = info.get("inchikey") or info.get("inchiKey")
    except Exception as e:
        result.error = str(e)
    return result

static_dir = Path(__file__).resolve().parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir), html=False), name="static")

    @app.get("/")
    def root():
        index_path = static_dir / "index.html"
        if index_path.exists():
            return FileResponse(index_path, media_type="text/html")
        return RedirectResponse(url="/docs", status_code=307)

    @app.get("/favicon.ico")
    def favicon():
        ico = static_dir / "favicon.svg"
        if ico.exists():
            return FileResponse(ico, media_type="image/svg+xml")
        return Response(status_code=204)
else:
    @app.get("/")
    def root():
        return RedirectResponse(url="/docs", status_code=307)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
