"""
Read-only wrapper for the ChemConverter module.

ChemConverter directory is treated as an external black-box dependency.
Do NOT modify any files inside /workspaces/workspaces/ChemConverter/.
"""

import sys
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Inject ChemConverter directory into sys.path so its modules resolve.
# ---------------------------------------------------------------------------
_CHEMCONV_ROOT = Path(__file__).resolve().parents[3] / "ChemConverter"

if str(_CHEMCONV_ROOT) not in sys.path:
    sys.path.insert(0, str(_CHEMCONV_ROOT))


# ---------------------------------------------------------------------------
# Lazy-import helpers
# ---------------------------------------------------------------------------

_cas_client_instance = None
_cas_client_error: Optional[str] = None


def _get_cas_client():
    """Return a singleton CASClient, or None if unavailable."""
    global _cas_client_instance, _cas_client_error
    if _cas_client_instance is not None:
        return _cas_client_instance
    if _cas_client_error is not None:
        return None
    try:
        from cas_client import CASClient  # noqa: PLC0415
        _cas_client_instance = CASClient()
        logger.info("CASClient initialized successfully.")
        return _cas_client_instance
    except Exception as exc:
        _cas_client_error = str(exc)
        logger.warning("CASClient unavailable: %s", exc)
        return None


def _iupac_to_smiles(name: str) -> Optional[str]:
    try:
        from converters import iupac_to_kekule_smiles  # noqa: PLC0415
        return iupac_to_kekule_smiles(name)
    except Exception as exc:
        logger.warning("iupac_to_kekule_smiles failed for '%s': %s", name, exc)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve_compound(name: str) -> dict:
    """
    Given a chemical compound name (as it appears in the text), attempt to
    resolve CAS number and SMILES using ChemConverter tools.

    Resolution strategy:
      1. CASClient.lookup_by_name()  → gets CAS + SMILES from CAS SciFinder
      2. iupac_to_kekule_smiles()    → fallback SMILES via CIRpy + RDKit
      3. If both fail, return "PENDING_CONVERSION" placeholders.

    Returns:
        {
            "name": str,
            "cas": str | None,
            "smiles": str | None,
            "resolved": bool
        }
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

        # Try to also get CAS from the resolved SMILES
        if client is not None:
            try:
                info = client.lookup_by_smiles(smiles)
                if info:
                    result["cas"] = info.get("cas")
            except Exception:
                pass

    return result


def resolve_compounds_batch(names: list[str]) -> dict[str, dict]:
    """
    Resolve a list of compound names. Returns a dict keyed by name.
    Skips empty / already-resolved entries.
    """
    results = {}
    unique = list(dict.fromkeys(n for n in names if n and n != "PENDING_CONVERSION"))
    for name in unique:
        results[name] = resolve_compound(name)
    return results
