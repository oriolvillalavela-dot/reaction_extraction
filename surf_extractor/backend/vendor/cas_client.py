"""
CAS SciFinder client — vendored from ChemConverter.

Requires env vars (read via .env or environment):
  CAS_CLIENT_ID, CAS_CLIENT_SECRET, CAS_TOKEN_URL, CAS_SERVER
"""

import json
import re
import time
from pathlib import Path
from typing import Dict, Optional, List
import threading
from html import unescape

import requests
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

CAS_RN_RE = re.compile(r"\b\d{2,7}-\d{2}-\d\b")
INCHIKEY_RE = re.compile(r"\b[A-Z]{14}-[A-Z]{10}-[A-Z]\b")
HTML_TAG_RE = re.compile(r"<[^>]+>")

# ----------------- helpers -----------------

def _strip_html(s: Optional[str]) -> Optional[str]:
    if not isinstance(s, str):
        return s
    s2 = unescape(s)
    s3 = HTML_TAG_RE.sub("", s2)
    return re.sub(r"\s{2,}", " ", s3).strip()

def _deep_strings(o):
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for v in o.values():
            yield from _deep_strings(v)
    elif isinstance(o, list):
        for it in o:
            yield from _deep_strings(it)

def _first(items):
    return items[0] if isinstance(items, list) and items else None

def _get_nested(d, paths: List[str]):
    def dig(obj, path):
        cur = obj
        for p in path.split("."):
            if isinstance(cur, list):
                cur = _first(cur)
            if cur is None:
                return None
            if isinstance(cur, dict):
                cur = cur.get(p)
            else:
                return None
        return cur
    for p in paths:
        v = dig(d, p)
        if v is not None:
            return v
    return None

def _normalize_inchi(v: str) -> str:
    return re.sub(r"^\s*inchi=\s*", "", v, flags=re.I)

def _find_cas_anywhere(rec):
    for path in [
        "casRegistryNumber", "casRN", "rn",
        "identifiers.casRegistryNumber", "identifiers.cas", "identifiers.casRN",
        "registryNumber", "registryNumbers", "identifiers.registryNumbers",
        "ids.cas", "ids.casRegistryNumber", "ids.registryNumber",
        "substance.casRegistryNumber", "substance.casRN",
        "substance.registryNumber", "substance.registryNumbers",
    ]:
        v = _get_nested(rec, [path])
        if isinstance(v, list):
            for s in v:
                if isinstance(s, str) and CAS_RN_RE.search(s):
                    return CAS_RN_RE.search(s).group(0)
        elif isinstance(v, str):
            m = CAS_RN_RE.search(v)
            if m:
                return m.group(0)
    for s in _deep_strings(rec):
        m = CAS_RN_RE.search(s)
        if m:
            return m.group(0)
    return None

def _find_inchikey_anywhere(rec):
    for path in [
        "structures.inchiKey", "structures.InChIKey", "structures.standardInchiKey",
        "identifiers.inchiKey", "identifiers.InChIKey", "identifiers.standardInchiKey",
        "ids.inchiKey", "ids.InChIKey",
        "substance.structures.inchiKey", "substance.identifiers.inchiKey",
    ]:
        v = _get_nested(rec, [path])
        if isinstance(v, list):
            for s in v:
                if isinstance(s, str):
                    m = INCHIKEY_RE.search(s)
                    if m:
                        return m.group(0).upper()
        elif isinstance(v, str):
            m = INCHIKEY_RE.search(v)
            if m:
                return m.group(0).upper()
    for s in _deep_strings(rec):
        m = INCHIKEY_RE.search(s)
        if m:
            return m.group(0).upper()
    return None

def _pick_name(rec):
    for path in [
        "names.iupac", "iupacName", "preferredName", "name", "displayName", "title",
        "substance.names.iupac", "substance.iupacName", "substance.preferredName",
    ]:
        v = _get_nested(rec, [path])
        if isinstance(v, list) and v:
            return _strip_html(v[0])
        if isinstance(v, str) and v.strip():
            return _strip_html(v)
    syn = _get_nested(rec, ["synonyms"]) or _get_nested(rec, ["substance.synonyms"])
    if isinstance(syn, list) and syn:
        return _strip_html(syn[0])
    return None

def _pick_smiles(rec):
    for path in [
        "structures.isomericSmiles", "isomericSmiles", "structures.canonicalSmiles",
        "canonicalSmiles", "smiles", "kekuleSmiles",
        "substance.structures.canonicalSmiles", "substance.canonicalSmiles",
    ]:
        v = _get_nested(rec, [path])
        if isinstance(v, list) and v:
            return v[0]
        if isinstance(v, str) and v.strip():
            return v
    return None

def _pick_inchi(rec):
    for path in ["structures.inchi", "inchi", "InChI", "substance.structures.inchi"]:
        v = _get_nested(rec, [path])
        if isinstance(v, list) and v:
            return v[0]
        if isinstance(v, str) and v.strip():
            return v
    return None

def _pick_mf(rec) -> Optional[str]:
    candidates = [
        "molecularFormula", "formula",
        "substance.molecularFormula", "substance.formula",
        "properties.molecularFormula", "props.molecularFormula",
        "physchem.molecularFormula",
    ]
    v = _get_nested(rec, candidates)
    if isinstance(v, str) and v.strip():
        return v.strip()
    return None

def _pick_mw(rec) -> Optional[float]:
    candidates = [
        "molecularWeight", "exactMass", "mw",
        "substance.molecularWeight", "properties.molecularWeight",
        "props.molecularWeight", "physchem.molecularWeight",
    ]
    v = _get_nested(rec, candidates)
    try:
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str) and v.strip():
            m = re.search(r"[-+]?\d*\.?\d+", v.replace(",", ""))
            if m:
                return float(m.group(0))
    except Exception:
        return None
    return None

def _compute_inchi_from_smiles(smiles: str) -> Optional[str]:
    try:
        from rdkit import Chem
        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            return None
        return Chem.MolToInchi(mol)
    except Exception:
        return None

def _compute_inchikey_from_smiles(smiles: str):
    try:
        from rdkit import Chem
        mol = Chem.MolFromSmiles(smiles)
        if not mol:
            return None
        return Chem.MolToInchiKey(mol)
    except Exception:
        return None

def _compute_inchikey_from_inchi(inchi: str):
    try:
        from rdkit import Chem
        s = inchi if inchi.upper().startswith("INCHI=") else f"InChI={inchi}"
        mol = Chem.MolFromInchi(s)
        if not mol:
            return None
        return Chem.MolToInchiKey(mol)
    except Exception:
        return None

def _compute_mf_mw_from_smiles(smiles: str):
    try:
        from rdkit import Chem
        from rdkit.Chem import rdMolDescriptors, Descriptors
        mol = Chem.MolFromSmiles(smiles)
        if not mol:
            return None, None
        mf = rdMolDescriptors.CalcMolFormula(mol)
        mw = float(Descriptors.MolWt(mol))
        return mf, mw
    except Exception:
        return None, None


# ----------------- Settings -----------------

class CASSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    client_id: str = Field("", alias="CAS_CLIENT_ID")
    client_secret: str = Field("", alias="CAS_CLIENT_SECRET")
    token_url: str = Field("", alias="CAS_TOKEN_URL")
    server: str = Field("", alias="CAS_SERVER")
    grant_type: str = "client_credentials"
    scope: str = ""
    verify_ssl: bool = True
    cache_file: str = ".cas_token_cache.json"
    rate_limit_per_sec: float = 1.0
    debug: bool = False


# ----------------- Auth -----------------

class CASAuth:
    def __init__(self, settings: CASSettings):
        self.s = settings

    def _request_token(self) -> dict:
        data = {
            "grant_type": self.s.grant_type,
            "client_id": self.s.client_id,
            "client_secret": self.s.client_secret,
            "scope": self.s.scope,
        }
        r = requests.post(self.s.token_url, data=data, verify=self.s.verify_ssl)
        r.raise_for_status()
        tok = r.json()
        tok["expiration_date"] = int(time.time() + int(tok.get("expires_in", 3600)))
        try:
            Path(self.s.cache_file).write_text(json.dumps(tok), encoding="utf-8")
        except Exception:
            pass
        return tok

    def get_token(self) -> dict:
        p = Path(self.s.cache_file)
        if p.exists():
            try:
                tok = json.loads(p.read_text(encoding="utf-8"))
                if time.time() < tok.get("expiration_date", 0) - 30:
                    return tok
            except Exception:
                pass
        return self._request_token()

# ----------------- Rate limiter -----------------

class _RateLimiter:
    """Thread-safe limiter enforcing a minimum interval between request starts."""
    def __init__(self, rate_per_sec: float = 1.0):
        try:
            rate = float(rate_per_sec)
        except Exception:
            rate = 1.0
        self.min_interval = 0.0 if rate <= 0 else 1.0 / rate
        self._next_time = 0.0
        self._cond = threading.Condition()

    def wait(self):
        if self.min_interval <= 0:
            return
        with self._cond:
            while True:
                now = time.monotonic()
                if now >= self._next_time:
                    self._next_time = now + self.min_interval
                    self._cond.notify(1)
                    return
                self._cond.wait(timeout=self._next_time - now)

# ----------------- HTTP -----------------

class RequestHandler:
    def __init__(self, settings: CASSettings, auth: CASAuth):
        self.s = settings
        self.auth = auth
        self.session = requests.Session()
        self._limiter = _RateLimiter(self.s.rate_limit_per_sec)

    def _update_headers(self):
        tok = self.auth.get_token()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Authorization": f"{tok.get('token_type','Bearer')} {tok['access_token']}"
        })

    def _mask_bearer(self, headers: dict) -> dict:
        masked = dict(headers)
        if "Authorization" in masked:
            masked["Authorization"] = re.sub(
                r"(Bearer\s+)([A-Za-z0-9\-_\.]+)", r"\1***REDACTED***",
                masked["Authorization"]
            )
        return masked

    def _dump(self, kind: str, payload: dict):
        if not self.s.debug:
            return
        dbg = Path(".cas_debug"); dbg.mkdir(exist_ok=True)
        ts = int(time.time() * 1000)
        (dbg / f"{ts}_{kind}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def post(self, url: str, json_body: Optional[dict] = None) -> dict:
        self._update_headers()
        self._limiter.wait()
        r = self.session.post(url, data=json.dumps(json_body or {}), verify=self.s.verify_ssl)
        snap = {
            "request": {"method": "POST", "url": url, "json": json_body or {},
                        "headers": self._mask_bearer(dict(self.session.headers))},
            "status_code": r.status_code,
            "text": r.text[:4000],
        }
        try:
            j = r.json(); snap["json"] = j
        except Exception:
            j = None
        self._dump("response_post", snap)
        r.raise_for_status()
        return j or {}

    def get(self, url: str, params: Optional[dict] = None) -> dict:
        self._update_headers()
        self._limiter.wait()
        r = self.session.get(url, params=params, verify=self.s.verify_ssl)
        snap = {
            "request": {"method": "GET", "url": url, "params": params or {},
                        "headers": self._mask_bearer(dict(self.session.headers))},
            "status_code": r.status_code,
            "text": r.text[:4000],
        }
        try:
            j = r.json(); snap["json"] = j
        except Exception:
            j = None
        self._dump("response_get", snap)
        r.raise_for_status()
        return j or {}

# ----------------- Models & client -----------------

class BaseSearchRequest(BaseModel):
    offset: int = Field(0, ge=0)
    length: int = Field(25, ge=1, le=100)
    echo: str = Field("false", alias="echo")

    @field_validator("echo")
    def _echo_bool(cls, v: str) -> str:
        lv = (v or "").lower()
        if lv not in ("true", "false"):
            raise ValueError("echo must be 'true' or 'false'")
        return lv

    model_config = {"use_enum_values": True, "populate_by_name": True}


class SubstanceSearchRequest(BaseSearchRequest):
    query: Optional[str] = Field(None, alias="q")
    structure: Optional[str] = Field(None, alias="str")
    structure_mode: str = Field("drw", alias="strMode")
    structure_highlight: str = Field("false", alias="strHighlight")
    uri_only: str = Field("false", alias="uriOnly")
    facet: str = Field("false", alias="facet")

    @field_validator("structure_highlight", "uri_only", "facet")
    def _bools(cls, v: str) -> str:
        lv = (v or "").lower()
        if lv not in ("true", "false"):
            raise ValueError("must be 'true' or 'false'")
        return lv


class CASClient:
    SUBSTANCES_SEARCH = "substances"
    SUBSTANCE_DETAILS = "substances/{uri}"

    def __init__(self):
        self.settings = CASSettings()
        self.auth = CASAuth(self.settings)
        self.http = RequestHandler(self.settings, self.auth)
        self._server = self.settings.server.rstrip("/")

    def _details(self, uri: Optional[str]) -> dict:
        if not uri:
            return {}
        url = f"{self._server}/{self.SUBSTANCE_DETAILS.format(uri=requests.utils.quote(uri, safe=''))}"
        return self.http.get(url)

    def _search_substances(self, params: SubstanceSearchRequest):
        url = f"{self._server}/{self.SUBSTANCES_SEARCH}"
        data = self.http.post(url, json_body=params.model_dump(by_alias=True, exclude_defaults=True))
        return data.get("substances") or data.get("results") or []

    def lookup_by_smiles(self, smiles: str, full: bool = False, wide: bool = False, mf_mw: bool = False) -> Dict:
        query_inchi = _compute_inchi_from_smiles(smiles)
        recs = self._search_substances(SubstanceSearchRequest(**{
            "str": smiles, "strMode": "drw", "length": 20, "echo": "false", "uriOnly": "false"
        }))
        if not recs:
            recs = self._search_substances(SubstanceSearchRequest(**{
                "str": smiles, "strMode": "sub", "length": 20, "echo": "false", "uriOnly": "false"
            }))
        if not recs:
            return {}

        if query_inchi:
            primary = [r for r in recs if (r.get('inchi') == query_inchi)]
            if not primary:
                try:
                    q_ik = _compute_inchikey_from_inchi(query_inchi) or _compute_inchikey_from_smiles(smiles)
                except Exception:
                    q_ik = None
                if q_ik:
                    conn = q_ik.split('-')[0]
                    primary = [r for r in recs if ((r.get('inchikey') or '').startswith(conn) or
                                                   (r.get('InChIKey') or '').startswith(conn))]
            recs = primary or recs

        best = max(recs, key=lambda r: int(r.get("suppliersCount", 0)))
        details = self._details(_get_nested(best, ["uri"]))
        return self._extract_fields(details, fallback_smiles=smiles, include_full=full, include_wide=wide, include_mf_mw=mf_mw)

    def lookup_by_cas(self, rn: str, full: bool = False, wide: bool = False, mf_mw: bool = False) -> Dict:
        q = f'rn:"{rn}"'
        recs = self._search_substances(SubstanceSearchRequest(**{"q": q, "length": 1, "echo": "false", "uriOnly": "false"}))
        if not recs:
            return {}
        details = self._details(_get_nested(recs[0], ["uri"]))
        out = self._extract_fields(details, include_full=full, include_wide=wide, include_mf_mw=mf_mw)
        out["cas"] = out.get("cas") or rn
        return out

    def lookup_by_name(self, name: str, full: bool = False, wide: bool = False, mf_mw: bool = False) -> Dict:
        recs = self._search_substances(SubstanceSearchRequest(**{"q": name, "length": 1, "echo": "false", "uriOnly": "false"}))
        if not recs:
            return {}
        details = self._details(_get_nested(recs[0], ["uri"]))
        return self._extract_fields(details, include_full=full, include_wide=wide, include_mf_mw=mf_mw)

    def _extract_fields(self, details: dict, fallback_smiles: Optional[str] = None,
                        include_full: bool = False, include_wide: bool = False,
                        include_mf_mw: bool = False) -> Dict:
        rec = details.get("substance", details) if details else {}
        out = {
            "cas": _find_cas_anywhere(rec),
            "name": _pick_name(rec),
            "smiles": _pick_smiles(rec) or fallback_smiles,
        }

        if include_full or include_wide:
            raw_inchi = _pick_inchi(rec)
            if not raw_inchi and out.get("smiles"):
                raw_inchi = _compute_inchi_from_smiles(out["smiles"])
            if raw_inchi:
                out["inchi"] = _normalize_inchi(raw_inchi)
            ik = _find_inchikey_anywhere(rec)
            if not ik and out.get("inchi"):
                ik = _compute_inchikey_from_inchi(out["inchi"])
            if not ik and out.get("smiles"):
                ik = _compute_inchikey_from_smiles(out["smiles"])
            if ik:
                out["inchikey"] = ik

        if include_mf_mw:
            mf = _pick_mf(rec)
            mw = _pick_mw(rec)
            if (mf is None or mw is None) and out.get("smiles"):
                cmf, cmw = _compute_mf_mw_from_smiles(out["smiles"])
                mf = mf or cmf
                mw = mw or cmw
            if mf: out["molecular_formula"] = mf
            if mw: out["molecular_weight"] = round(float(mw), 4)

        for k, v in list(out.items()):
            if isinstance(v, str):
                out[k] = _strip_html(v)
            if isinstance(v, list):
                out[k] = v[0] if v else None

        return {k: v for k, v in out.items() if v}
