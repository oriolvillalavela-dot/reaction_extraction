"""
Extraction Agent: produces a draft SURF data payload from parsed PDF content.

Uses the canonical SURF extraction prompt with PortKey → gemini-2.5-pro.
"""

from __future__ import annotations
import json
import re
import logging
from typing import Optional
from backend.agents.base_agent import BaseAgent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Core extraction system prompt
# ---------------------------------------------------------------------------

EXTRACTION_SYSTEM_PROMPT = """\
You are an expert chemical data extractor. Your task is to extract chemical reaction data from \
the provided text and tables parsed from a scientific publication (Main Paper + Supplementary Information).

Goal: Extract all relevant reaction data to fill a SURF format template.

CRITICAL RULE: Do not attempt to convert compound names into SMILES or CAS numbers unless the \
original text explicitly provides them. Use the placeholder "PENDING_CONVERSION" in their place. \
Focus ONLY on accurately extracting the compound names as they appear in the text.

Data Fields Required:
If a value is not provided in the publication, leave it empty ("") or mark it as "not reported".

- rxn_id: format macmillan_year_Tnº_Enº (e.g. macmillan_2024_T1_E1)
- source_id: DOI of the paper
- source_type: e.g., publication, patent
- rxn_date: date of reaction if present, otherwise date of publication
- rxn_type: chemical reaction type (e.g., C(sp2)-Br / C(sp3)-Br)
- rxn_name: name given to the reaction (e.g., formation of X)
- rxn_tech: e.g., photochemistry, metallophotoredox, electrochemistry
- temperature_deg_c: temperature of the reaction
- time_h: duration of the reaction in hours
- atmosphere: e.g., air, N2, argon
- stirring_shaking: shaking/stirring presence and speed if written
- scale_mol: quantity of mol of the limitant reactant
- concentration_mol_l: exact number, or calculate (mol limitant / volume solvent)
- wavelength_nm: irradiation wavelength if photochemistry
- startingmat_1_name: chemical name
- startingmat_1_cas: extract ONLY if in text, else "PENDING_CONVERSION"
- startingmat_1_smiles: extract ONLY if in text, else "PENDING_CONVERSION"
- startingmat_1_eq: equivalents
- reagent_N_name / reagent_N_cas / reagent_N_smiles / reagent_N_eq (N=1,2,...)
- catalyst_N_name / catalyst_N_cas / catalyst_N_smiles / catalyst_N_eq
- ligand_N_name / ligand_N_cas / ligand_N_smiles / ligand_N_eq
- additive_N_name / additive_N_cas / additive_N_smiles / additive_N_eq
- solvent_N_name / solvent_N_cas / solvent_N_smiles / solvent_N_fraction
- product_N_name / product_N_cas / product_N_smiles / product_N_yield / product_N_yieldtype / product_N_ms / product_N_nmr
- procedure: page of the SI where the procedure is found
- comment: any relevant observation for that specific reaction

OUTPUT FORMAT: Respond with a valid JSON array of objects, one per reaction.
Each object has the field names listed above as keys.
Example:
[
  {
    "rxn_id": "smith_2024_T1_E1",
    "source_id": "10.1234/example",
    "source_type": "publication",
    ...
    "startingmat_1_name": "benzaldehyde",
    "startingmat_1_cas": "PENDING_CONVERSION",
    "startingmat_1_smiles": "PENDING_CONVERSION",
    ...
  }
]

Do NOT include any text outside the JSON array. Do NOT wrap it in markdown code fences.
"""


class ExtractionAgent(BaseAgent):
    """
    Drafts SURF reaction data from parsed PDF content.
    """

    def __init__(self, client=None):
        super().__init__(client=client, name="ExtractionAgent")

    def run(
        self,
        main_text: str,
        si_text: str,
        images: list[dict],
        user_instructions: str = "",
    ) -> list[dict]:
        """
        Runs the extraction on provided text and images.

        Returns a list of reaction dicts (one per reaction row).
        """
        user_section = f"User Specific Instructions / Overrides:\n{user_instructions}\n\n" if user_instructions.strip() else ""

        user_message = f"""{user_section}\
=== MAIN PAPER TEXT ===
{main_text[:60000]}

=== SUPPLEMENTARY INFORMATION TEXT ===
{si_text[:40000]}

Please extract all chemical reactions from the above content and return a JSON array \
following the SURF format described in the system prompt.
"""

        # Use vision if images are present (cap at 20 to stay within context limits)
        capped_images = images[:20]

        # max_tokens=16384: gives Gemini 2.5 Pro enough room for output while
        # staying within the Galileo gateway's server-side timeout.
        # (65536 causes 504; 8192 truncates JSON mid-object)
        if capped_images:
            raw = self._chat_with_images(
                system=EXTRACTION_SYSTEM_PROMPT,
                text=user_message,
                images=capped_images,
                max_tokens=16384,
                temperature=0.0,
            )
        else:
            raw = self._chat(
                system=EXTRACTION_SYSTEM_PROMPT,
                user=user_message,
                max_tokens=16384,
                temperature=0.0,
            )

        return self._parse_json_response(raw)

    def _parse_json_response(self, raw: str) -> list[dict]:
        """Extract JSON array from LLM response, tolerating markdown fences and truncation."""
        # Strip markdown code fences if present
        cleaned = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`").strip()

        # Find the outermost JSON array start
        start = cleaned.find("[")
        if start == -1:
            self.logger.error("No JSON array found in response.\nRaw (first 2000 chars):\n%s", raw[:2000])
            return []
        cleaned = cleaned[start:]

        # Try parsing as-is first
        try:
            data = json.loads(cleaned)
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                return [data]
        except json.JSONDecodeError as exc:
            self.logger.warning("JSON parse failed (%s) – attempting truncation recovery…", exc)

        # Recovery: extract all complete {...} objects from the array
        results = []
        depth = 0
        obj_start = None
        i = 0
        in_string = False
        escape_next = False
        while i < len(cleaned):
            ch = cleaned[i]
            if escape_next:
                escape_next = False
            elif ch == "\\" and in_string:
                escape_next = True
            elif ch == '"':
                in_string = not in_string
            elif not in_string:
                if ch == "{":
                    if depth == 0:
                        obj_start = i
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0 and obj_start is not None:
                        try:
                            obj = json.loads(cleaned[obj_start:i + 1])
                            if isinstance(obj, dict):
                                results.append(obj)
                        except json.JSONDecodeError:
                            pass
                        obj_start = None
            i += 1

        if results:
            self.logger.info("Truncation recovery extracted %d reaction(s).", len(results))
        else:
            self.logger.error("JSON parse failed completely.\nRaw (first 2000 chars):\n%s", raw[:2000])

        return results
