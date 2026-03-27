"""
QA Reviewer Agent: count-aware validator.

Responsibilities:
  1. Compare len(extracted_rows) against the Parser's expected row count.
  2. Identify EXACTLY which table entries are missing by matching
     rxn_id patterns against the set of (table_id, entry_id) pairs the
     Parser reported.
  3. Run lightweight local checks (CAS format, yield type vocabulary,
     numeric fields) without an LLM call.
  4. Optionally run an LLM review pass for semantic correctness
     (hallucinated CAS/SMILES, inconsistent units).

Returns a QAResult with:
  - rows          : (possibly corrected) row list
  - issues        : human-readable list of detected problems
  - missing_entry_ids : ["T1_E3", "T2_E7", …] — keys the Coordinator uses
                        to look up the original TableRow and re-extract
  - count_ok      : True only when all entries are accounted for
"""

from __future__ import annotations
import json
import re
import logging
from typing import Optional

from backend.agents.base_agent import BaseAgent
from backend.models import ParsedTable, QAResult

logger = logging.getLogger("agents.QAReviewerAgent")

# ---------------------------------------------------------------------------
# Accepted yield-type vocabulary (lower-cased for comparison)
# ---------------------------------------------------------------------------
_ACCEPTED_YIELD_TYPES = {
    "isolated", "nmr yield", "lcms yield", "hplc yield",
    "gc yield", "crude yield", "not reported", "",
}

# Pattern a valid CAS number must match
_CAS_PATTERN = re.compile(r"^\d{2,7}-\d{2}-\d$")

# Pattern to extract T<n>_E<id> from an rxn_id
_RXN_ID_TE_PATTERN = re.compile(r"(T\d+)_(E[\w]+)")

# ---------------------------------------------------------------------------
# LLM review prompt (only called when issues are found in local checks)
# ---------------------------------------------------------------------------
_LLM_REVIEW_SYSTEM = """\
You are a meticulous chemical data quality reviewer.

You will receive:
  A) An abbreviated view of the original publication text.
  B) A JSON array of extracted SURF reaction rows.

Your job — check and correct the following:
1. Every _cas and _smiles field must be either:
   - A value EXPLICITLY stated in the publication text, OR
   - The placeholder "PENDING_CONVERSION"
   If a CAS/SMILES was inferred or generated, replace it with "PENDING_CONVERSION".

2. Every product_N_yieldtype must be one of:
   isolated | NMR yield | LCMS yield | HPLC yield | GC yield | crude yield | not reported
   Normalise deviations.

3. temperature_deg_c, time_h, scale_mol, concentration_mol_l, wavelength_nm
   must contain only numeric values (or "" / "not reported").

4. rxn_id must follow the pattern: <lastname>_<year>_T<n>_E<id>

Return ONLY the corrected JSON array. No text outside the array. No markdown fences.
"""


class QAReviewerAgent(BaseAgent):
    """
    Validates extracted SURF rows and identifies missing entries for re-extraction.
    """

    def __init__(self, client=None, run_llm_review: bool = True):
        super().__init__(client=client, name="QAReviewerAgent")
        self.run_llm_review = run_llm_review

    def run(
        self,
        rows: list[dict],
        expected_count: int,
        parsed_tables: list[ParsedTable],
        main_text: str = "",
        si_text: str = "",
    ) -> QAResult:
        """
        Full QA pass.

        Args:
            rows:           Extracted SURF row dicts.
            expected_count: Total rows the Parser found (ground truth).
            parsed_tables:  ParsedTable list (used to identify missing entries).
            main_text:      Publication main text (for LLM review context).
            si_text:        SI text (for LLM review context).

        Returns:
            QAResult with missing_entry_ids populated for re-extraction.
        """
        issues: list[str] = []

        # ---------------------------------------------------------------
        # 1. Count check
        # ---------------------------------------------------------------
        actual = len(rows)
        count_ok = actual == expected_count
        if not count_ok:
            issues.append(
                f"Count mismatch: Parser expected {expected_count} rows, "
                f"got {actual} after extraction."
            )

        # ---------------------------------------------------------------
        # 2. Missing entry identification
        # ---------------------------------------------------------------
        missing_ids = self._find_missing_entries(rows, parsed_tables)
        if missing_ids:
            issues.append(
                f"Missing entries ({len(missing_ids)}): {', '.join(missing_ids[:20])}"
                + (" …" if len(missing_ids) > 20 else "")
            )

        # ---------------------------------------------------------------
        # 3. Local quality checks (no LLM needed)
        # ---------------------------------------------------------------
        local_issues = self._local_checks(rows)
        issues.extend(local_issues)

        # ---------------------------------------------------------------
        # 4. Optional LLM review pass (only when issues detected)
        # ---------------------------------------------------------------
        corrected_rows = rows
        if self.run_llm_review and (local_issues or not count_ok):
            corrected_rows = self._llm_review(rows, main_text, si_text)
            if not corrected_rows:
                issues.append("LLM review returned unparseable JSON – keeping previous output.")
                corrected_rows = rows

        # ---------------------------------------------------------------
        # Result
        # ---------------------------------------------------------------
        logger.info(
            "QA complete: %d/%d rows, %d missing, %d issues.",
            actual, expected_count, len(missing_ids), len(issues),
        )

        return QAResult(
            rows=corrected_rows,
            issues=issues,
            missing_entry_ids=missing_ids,
            count_ok=count_ok and not missing_ids,
        )

    # ------------------------------------------------------------------
    # Missing entry detection
    # ------------------------------------------------------------------

    def _find_missing_entries(
        self, rows: list[dict], parsed_tables: list[ParsedTable]
    ) -> list[str]:
        """
        Build the set of (table_id, entry_id) pairs the Parser reported,
        compare against what the extracted rxn_ids claim, and return the
        difference as sorted "T<n>_E<id>" strings.
        """
        if not parsed_tables:
            return []          # no structured tables → nothing to compare against

        # Expected: from the parser
        expected: set[str] = set()
        for table in parsed_tables:
            for row in table.rows:
                expected.add(f"{row.table_id}_{row.entry_id}")

        # Extracted: parse T<n>_E<id> out of each rxn_id
        extracted: set[str] = set()
        for row in rows:
            rxn_id = row.get("rxn_id", "")
            m = _RXN_ID_TE_PATTERN.search(rxn_id)
            if m:
                extracted.add(f"{m.group(1)}_{m.group(2)}")

        missing = sorted(expected - extracted)
        if missing:
            logger.warning("Missing entries: %s", missing)
        return missing

    # ------------------------------------------------------------------
    # Local rule checks
    # ------------------------------------------------------------------

    def _local_checks(self, rows: list[dict]) -> list[str]:
        """Lightweight, deterministic checks that do not require an LLM."""
        issues: list[str] = []

        for i, row in enumerate(rows):
            prefix = f"Row {i + 1} (rxn_id={row.get('rxn_id', '?')})"

            # CAS format
            for key, val in row.items():
                if not key.endswith("_cas"):
                    continue
                if val in ("PENDING_CONVERSION", "not reported", "", None):
                    continue
                if not _CAS_PATTERN.match(str(val)):
                    issues.append(f"{prefix}: suspicious CAS in '{key}': {val!r}")

            # Yield type vocabulary
            for key, val in row.items():
                if not key.endswith("_yieldtype"):
                    continue
                if str(val).lower() not in _ACCEPTED_YIELD_TYPES:
                    issues.append(f"{prefix}: unrecognised yieldtype in '{key}': {val!r}")

            # Numeric fields
            for key in (
                "temperature_deg_c", "time_h", "scale_mol",
                "concentration_mol_l", "wavelength_nm",
            ):
                val = str(row.get(key, "")).strip()
                if not val or val.lower() in ("not reported", ""):
                    continue
                clean = re.sub(r"[~<>≈±\s]", "", val)
                # Handle ranges like "20-25"
                clean = clean.split("-")[0].split("–")[0]
                try:
                    float(clean)
                except ValueError:
                    issues.append(f"{prefix}: non-numeric value in '{key}': {val!r}")

            # rxn_id format
            rxn_id = row.get("rxn_id", "")
            if rxn_id and not re.match(r"^[a-z]+_\d{4}_T\d+_E[\w]+$", rxn_id):
                issues.append(f"{prefix}: rxn_id format unexpected: {rxn_id!r}")

        return issues

    # ------------------------------------------------------------------
    # LLM review pass
    # ------------------------------------------------------------------

    def _llm_review(
        self, rows: list[dict], main_text: str, si_text: str
    ) -> Optional[list[dict]]:
        """Run an LLM correction pass when local checks found issues."""
        combined_text = (main_text[:15000] + "\n\n" + si_text[:8000]).strip()
        user_msg = (
            f"=== ORIGINAL TEXT (abbreviated) ===\n{combined_text}\n\n"
            f"=== DRAFT SURF DATA (JSON) ===\n"
            f"{json.dumps(rows, indent=2)[:24000]}\n\n"
            "Return the corrected JSON array now."
        )
        try:
            raw = self._chat(
                system=_LLM_REVIEW_SYSTEM,
                user=user_msg,
                max_tokens=8192,
                temperature=0.0,
            )
            return self._parse_json_array(raw)
        except Exception as exc:
            logger.warning("LLM review call failed: %s", exc)
            return None

    @staticmethod
    def _parse_json_array(raw: str) -> Optional[list[dict]]:
        cleaned = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`").strip()
        match = re.search(r"\[.*\]", cleaned, re.DOTALL)
        if match:
            cleaned = match.group(0)
        try:
            data = json.loads(cleaned)
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                return [data]
        except json.JSONDecodeError:
            pass
        return None
