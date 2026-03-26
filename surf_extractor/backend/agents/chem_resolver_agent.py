"""
Chemical Resolution Agent.

Iterates over all compound fields in the reviewed SURF rows, identifies those
with "PENDING_CONVERSION" CAS or SMILES, and attempts to resolve them using
the ChemConverter (CASClient + CIRpy) black-box module.

Runs resolution concurrently using a thread pool to avoid blocking the event loop.
"""

from __future__ import annotations
import logging
import concurrent.futures
from backend.integrations.chemconv_wrapper import resolve_compounds_batch

logger = logging.getLogger(__name__)

# Compound-role prefixes that carry CAS/SMILES fields in the SURF schema
COMPOUND_ROLE_PREFIXES = [
    "startingmat_1", "startingmat_2",
    "reagent_1", "reagent_2", "reagent_3",
    "catalyst_1", "catalyst_2",
    "ligand_1", "ligand_2",
    "additive_1", "additive_2",
    "solvent_1", "solvent_2",
    "product_1", "product_2",
]


class ChemResolverAgent:
    """
    Resolves PENDING_CONVERSION CAS / SMILES entries for all compounds in
    the validated SURF rows using ChemConverter (read-only black-box).
    """

    def __init__(self, max_workers: int = 4):
        self.max_workers = max_workers
        self.logger = logging.getLogger("agents.ChemResolverAgent")

    def run(self, rows: list[dict]) -> list[dict]:
        """
        Resolves all PENDING_CONVERSION entries in the rows.
        Returns updated rows.
        """
        if not rows:
            return rows

        # Collect all names that need resolution
        names_to_resolve: set[str] = set()
        for row in rows:
            for prefix in COMPOUND_ROLE_PREFIXES:
                name_key = f"{prefix}_name"
                cas_key = f"{prefix}_cas"
                smiles_key = f"{prefix}_smiles"

                name = str(row.get(name_key, "")).strip()
                cas = str(row.get(cas_key, "")).strip()
                smiles = str(row.get(smiles_key, "")).strip()

                if name and (cas == "PENDING_CONVERSION" or smiles == "PENDING_CONVERSION"):
                    names_to_resolve.add(name)

        if not names_to_resolve:
            self.logger.info("No PENDING_CONVERSION entries found – skipping resolution.")
            return rows

        self.logger.info("Resolving %d unique compound names…", len(names_to_resolve))

        # Resolve concurrently
        resolved = self._resolve_batch(list(names_to_resolve))

        # Patch the rows
        patched = []
        for row in rows:
            row = dict(row)  # shallow copy to avoid mutating input
            for prefix in COMPOUND_ROLE_PREFIXES:
                name_key = f"{prefix}_name"
                cas_key = f"{prefix}_cas"
                smiles_key = f"{prefix}_smiles"

                name = str(row.get(name_key, "")).strip()
                if not name or name not in resolved:
                    continue

                info = resolved[name]
                if not info.get("resolved"):
                    continue  # leave as PENDING_CONVERSION

                # Only overwrite PENDING_CONVERSION slots
                if str(row.get(cas_key, "")).strip() == "PENDING_CONVERSION":
                    row[cas_key] = info.get("cas") or "PENDING_CONVERSION"

                if str(row.get(smiles_key, "")).strip() == "PENDING_CONVERSION":
                    row[smiles_key] = info.get("smiles") or "PENDING_CONVERSION"

            patched.append(row)

        resolved_count = sum(1 for v in resolved.values() if v.get("resolved"))
        self.logger.info(
            "Resolution complete: %d/%d compounds resolved.",
            resolved_count, len(names_to_resolve),
        )
        return patched

    def _resolve_batch(self, names: list[str]) -> dict[str, dict]:
        """Run resolution in a thread pool (ChemConverter is synchronous I/O)."""
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                # Split into chunks and submit
                chunk_size = max(1, len(names) // self.max_workers)
                futures = {}
                for i in range(0, len(names), chunk_size):
                    chunk = names[i:i + chunk_size]
                    fut = executor.submit(resolve_compounds_batch, chunk)
                    futures[fut] = chunk

                results: dict[str, dict] = {}
                for fut in concurrent.futures.as_completed(futures):
                    try:
                        results.update(fut.result())
                    except Exception as exc:
                        self.logger.warning("Batch resolution failed: %s", exc)

            return results
        except Exception as exc:
            self.logger.error("Thread pool resolution failed: %s", exc, exc_info=True)
            # Fallback: sequential
            return resolve_compounds_batch(names)
