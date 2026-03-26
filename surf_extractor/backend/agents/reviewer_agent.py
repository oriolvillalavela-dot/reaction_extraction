"""
Reviewer / Critic Agent: validates the Extraction Agent's output.

Checks:
  1. No CAS numbers or SMILES are hallucinated (must be PENDING_CONVERSION if not in text).
  2. Yield types are from an accepted vocabulary.
  3. Numeric fields contain plausible values.
  4. Required string fields are not empty without justification.

Returns corrected data and a list of issues found.
"""

from __future__ import annotations
import json
import re
import logging
from backend.agents.base_agent import BaseAgent

logger = logging.getLogger(__name__)

REVIEWER_SYSTEM_PROMPT = """\
You are a meticulous chemical data quality reviewer.

You will receive:
  A) The original publication text (abbreviated).
  B) A JSON array of extracted reaction rows (draft SURF data).

Your job:
1. Check that every _cas and _smiles field is either:
   - A value EXPLICITLY found in the original text, OR
   - The placeholder "PENDING_CONVERSION".
   If a CAS / SMILES appears to be inferred or generated, replace it with "PENDING_CONVERSION".

2. Check that product_N_yieldtype values are one of:
   isolated, NMR yield, LCMS yield, HPLC yield, GC yield, crude yield, not reported.
   Normalise deviations.

3. Check that temperature_deg_c, time_h, scale_mol, concentration_mol_l, wavelength_nm
   contain only numeric values (or empty / "not reported").

4. Verify rxn_id follows the pattern: <lastname>_<year>_T<n>_E<n>.

5. Return the corrected JSON array ONLY.
   Do NOT include any explanation text outside the JSON array.
   Do NOT wrap in markdown code fences.
"""


class ReviewerAgent(BaseAgent):
    """
    Validates and corrects the extraction agent's output.
    """

    MAX_REVIEW_ROUNDS = 2

    def __init__(self, client=None):
        super().__init__(client=client, name="ReviewerAgent")

    def run(
        self,
        extracted_rows: list[dict],
        main_text: str,
        si_text: str,
    ) -> tuple[list[dict], list[str]]:
        """
        Runs the review loop.

        Returns:
            (corrected_rows, issues_list)
        """
        if not extracted_rows:
            return [], ["ExtractionAgent returned no data."]

        issues: list[str] = []
        rows = extracted_rows
        combined_text = (main_text[:20000] + "\n\n" + si_text[:10000]).strip()

        for round_num in range(1, self.MAX_REVIEW_ROUNDS + 1):
            self.logger.info("Review round %d/%d…", round_num, self.MAX_REVIEW_ROUNDS)

            user_msg = f"""\
=== ORIGINAL TEXT (abbreviated) ===
{combined_text}

=== DRAFT SURF DATA (JSON) ===
{json.dumps(rows, indent=2)[:30000]}

Please review and return the corrected JSON array.
"""
            raw = self._chat(
                system=REVIEWER_SYSTEM_PROMPT,
                user=user_msg,
                max_tokens=8192,
                temperature=0.0,
            )
            corrected = self._parse_json(raw)
            if corrected:
                rows = corrected
                issues_this_round = self._local_checks(rows)
                issues.extend([f"Round {round_num}: {i}" for i in issues_this_round])
                if not issues_this_round:
                    self.logger.info("All checks passed after round %d.", round_num)
                    break
            else:
                issues.append(f"Round {round_num}: LLM returned unparseable JSON – keeping previous output.")
                break

        return rows, issues

    def _parse_json(self, raw: str) -> list[dict]:
        cleaned = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("```").strip()
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
        return []

    def _local_checks(self, rows: list[dict]) -> list[str]:
        """Lightweight rule checks that don't need an LLM call."""
        issues = []
        cas_pattern = re.compile(r"^\d{2,7}-\d{2}-\d$")
        accepted_yield_types = {
            "isolated", "nmr yield", "lcms yield", "hplc yield",
            "gc yield", "crude yield", "not reported", "",
        }

        for i, row in enumerate(rows):
            prefix = f"Row {i+1} (rxn_id={row.get('rxn_id','')})"

            # Check CAS fields
            for key, val in row.items():
                if key.endswith("_cas") and val not in ("PENDING_CONVERSION", "not reported", ""):
                    if not cas_pattern.match(str(val)):
                        issues.append(f"{prefix}: suspicious CAS value in '{key}': {val!r}")

            # Check yield type fields
            for key, val in row.items():
                if key.endswith("_yieldtype") and val.lower() not in accepted_yield_types:
                    issues.append(f"{prefix}: unrecognised yieldtype in '{key}': {val!r}")

            # Check numeric fields
            for key in ["temperature_deg_c", "time_h", "scale_mol", "concentration_mol_l", "wavelength_nm"]:
                val = str(row.get(key, "")).strip()
                if val and val.lower() not in ("not reported", ""):
                    try:
                        float(val.replace("~", "").replace("<", "").replace(">", ""))
                    except ValueError:
                        issues.append(f"{prefix}: non-numeric value in '{key}': {val!r}")

        return issues
