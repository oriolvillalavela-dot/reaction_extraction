"""
Chemical resolution wrapper — uses vendored ChemConverter code in backend/vendor/.

Resolution strategy per compound:
  1. CASClient.lookup_by_name()  → CAS SciFinder (requires CAS_* env vars)
  2. iupac_to_kekule_smiles()    → CIRpy + RDKit fallback
  3. Both fail                   → leave as "PENDING_CONVERSION"
"""

from __future__ import annotations
import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy-import helpers (thread-safe singleton for CASClient)
# ---------------------------------------------------------------------------

_cas_client_instance = None
_cas_client_error: Optional[str] = None
_cas_client_lock = threading.Lock()


def _get_cas_client():
    """Return a singleton CASClient, or None if unavailable (thread-safe)."""
    global _cas_client_instance, _cas_client_error
    # Fast path — no lock once settled
    if _cas_client_instance is not None:
        return _cas_client_instance
    if _cas_client_error is not None:
        return None
    with _cas_client_lock:
        # Double-checked locking
        if _cas_client_instance is not None:
            return _cas_client_instance
        if _cas_client_error is not None:
            return None
        try:
            from backend.vendor.cas_client import CASClient
            _cas_client_instance = CASClient()
            logger.info("CASClient initialized successfully.")
            return _cas_client_instance
        except Exception as exc:
            _cas_client_error = str(exc)
            logger.warning("CASClient unavailable: %s", exc)
            return None


def _iupac_to_smiles(name: str) -> Optional[str]:
    try:
        from backend.vendor.converters import iupac_to_kekule_smiles
        result = iupac_to_kekule_smiles(name)
        if result:
            return result
    except Exception:
        pass

    # Direct CIRpy fallback (no RDKit required)
    try:
        import cirpy
        smiles = cirpy.resolve(name, "smiles")
        return smiles or None
    except Exception as exc:
        logger.warning("CIRpy resolution failed for '%s': %s", name, exc)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve_compound(name: str) -> dict:
    """
    Resolve a compound name to CAS number and SMILES.

    Returns:
        {"name": str, "cas": str | None, "smiles": str | None, "resolved": bool}
    """
    result = {"name": name, "cas": None, "smiles": None, "resolved": False}

    if not name or name.strip() in ("", "PENDING_CONVERSION", "not reported"):
        return result

    # Step 1: CAS SciFinder lookup
    client = _get_cas_client()
    if client is not None:
        try:
            info = client.lookup_by_name(name)
            if info:
                result["cas"] = info.get("cas")
                result["smiles"] = info.get("smiles")
                if result["cas"] or result["smiles"]:
                    result["resolved"] = True
                    logger.debug("Resolved '%s' via CASClient: cas=%s", name, result["cas"])
                    return result
        except Exception as exc:
            logger.warning("CASClient.lookup_by_name failed for '%s': %s", name, exc)

    # Step 2: IUPAC → SMILES via CIRpy
    smiles = _iupac_to_smiles(name)
    if smiles:
        result["smiles"] = smiles
        result["resolved"] = True
        logger.debug("Resolved '%s' via CIRpy: smiles=%s", name, smiles)

        if client is not None:
            try:
                info = client.lookup_by_smiles(smiles)
                if info:
                    result["cas"] = info.get("cas")
            except Exception:
                pass

    return result


def resolve_compounds_batch(names: list[str]) -> dict[str, dict]:
    """Resolve a list of compound names. Returns a dict keyed by name."""
    results = {}
    unique = list(dict.fromkeys(n for n in names if n and n != "PENDING_CONVERSION"))
    for name in unique:
        results[name] = resolve_compound(name)
    return results
