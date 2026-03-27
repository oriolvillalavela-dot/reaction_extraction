"""
Scientist Agent: chemical domain expert and data translator.

Two-phase operation:
  Phase 1 – establish_baseline()
    Reads the General Procedures section once and extracts DEFAULT reaction
    parameters (temperature, time, atmosphere, catalyst loading, solvent, …).
    Returns a flat dict that serves as the inherited "baseline" for all rows.

  Phase 2 – process_chunk()
    Receives a small batch of structured TableRow objects (3–5 rows) plus the
    baseline.  For each row it:
      - Inherits all baseline fields
      - Overrides ONLY fields explicitly changed in the table row
      - Performs unit conversions (mol% → eq, min → h, etc.)
      - Sets every _cas and _smiles to "PENDING_CONVERSION"
      - Generates a correctly-formatted rxn_id
    Returns a list of fully-populated SURF row dicts.

  Fallback – process_text_chunk()
    Used when no structured tables were parsed (scanned PDFs, image tables).
    Behaves like the original ExtractionAgent's chunked approach but still
    enforces strict JSON output and inherits the baseline.

All LLM calls use strict JSON-only output enforced through both the system
prompt and response_format={"type": "json_object"} (passed via the `extra`
kwarg to PortKeyClient.chat()).
"""

from __future__ import annotations
import json
import re
import logging
from typing import Optional

from backend.agents.base_agent import BaseAgent
from backend.models import TableRow, ParsedTable, SURF_COLUMNS

logger = logging.getLogger("agents.ScientistAgent")

_MAX_TOKENS_BASELINE = 4096
_MAX_TOKENS_CHUNK    = 8192
_MAX_TOKENS_TEXT     = 16384

# ---------------------------------------------------------------------------
# Accepted yield-type vocabulary (used in prompt constraints)
# ---------------------------------------------------------------------------
_YIELD_TYPES = "isolated | NMR yield | LCMS yield | HPLC yield | GC yield | crude yield | not reported"

# ---------------------------------------------------------------------------
# Compact SURF field list for prompts (abbreviated for readability)
# ---------------------------------------------------------------------------
_SURF_FIELDS_BRIEF = ", ".join(SURF_COLUMNS)

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

BASELINE_SYSTEM = f"""\
You are an expert synthetic chemist. You will be given the "General Procedures" \
section from the Supplementary Information of a chemistry paper.

Your task: extract the DEFAULT reaction parameters that apply to ALL reactions \
described in the paper unless a specific table entry explicitly overrides them.

Return ONLY a single valid JSON object (no markdown, no prose) with exactly these keys \
(use an empty string "" for any value not mentioned):

{{
  "rxn_tech": "",
  "temperature_deg_c": "",
  "time_h": "",
  "atmosphere": "",
  "stirring_shaking": "",
  "scale_mol": "",
  "concentration_mol_l": "",
  "wavelength_nm": "",
  "startingmat_1_name": "",
  "startingmat_1_eq": "",
  "reagent_1_name": "", "reagent_1_eq": "",
  "reagent_2_name": "", "reagent_2_eq": "",
  "catalyst_1_name": "", "catalyst_1_eq": "",
  "ligand_1_name": "", "ligand_1_eq": "",
  "additive_1_name": "", "additive_1_eq": "",
  "solvent_1_name": "", "solvent_1_fraction": "",
  "solvent_2_name": "", "solvent_2_fraction": "",
  "product_1_yieldtype": "",
  "rxn_type": "",
  "rxn_name": "",
  "source_type": "publication"
}}

UNIT CONVERSION RULES:
- mol%  → equivalents: divide by 100.  "5 mol%" → "0.05";  "0.5 mol%" → "0.005"
- minutes → hours: divide by 60.  "30 min" → "0.5";  "90 min" → "1.5"
- °C values: keep as plain number.  "25 °C" → "25"
- Equivalents written as "x equiv." → keep as plain number "x"

Return ONLY the JSON object.  No text before or after.
"""

CHUNK_SYSTEM = f"""\
You are an expert synthetic chemist converting raw table rows from a chemistry \
paper into structured SURF-format reaction entries.

You will receive:
  1. BASELINE – default parameters from the General Procedure (a JSON object).
  2. TABLE CONTEXT – table ID, caption, and column headers with their chemical meaning.
  3. SOURCE INFO – DOI, author, year, and rxn_id naming convention.
  4. RAW ROWS – a numbered list of table rows (header → cell value).

For EACH row in RAW ROWS:
  a) Start from the BASELINE and copy all its non-empty values into the new row.
  b) OVERRIDE only the fields that the table row explicitly specifies.
  c) Apply unit conversions (same rules as below).
  d) Assign ALL _cas and _smiles fields the value "PENDING_CONVERSION".
     NEVER guess, infer, or hallucinate CAS numbers or SMILES strings.
  e) Use "not reported" for values that are genuinely absent from text and baseline.
  f) Set rxn_id using the pattern: {{lastname}}_{{year}}_{{table_id}}_{{entry_id}}
     Example: smith_2024_T1_E3

UNIT CONVERSION RULES:
- mol%  → eq: divide by 100.  "5 mol%" → "0.05";  "10 mol%" → "0.1"
- minutes → hours: divide by 60.  "30 min" → "0.5"
- "x equiv." / "x eq" → keep plain number "x"
- Yield written as "45%" → keep as "45" (no % sign)

YIELD TYPE VOCABULARY (product_N_yieldtype): {_YIELD_TYPES}

SURF FIELD LIST: {_SURF_FIELDS_BRIEF}

Return ONLY a valid JSON object with this exact structure:
{{
  "reactions": [
    {{
      "rxn_id": "...",
      "source_id": "...",
      ... (all applicable SURF fields) ...
    }}
  ]
}}

Rules:
- Include EVERY row from RAW ROWS (do not skip any).
- Do NOT include explanatory text outside the JSON.
- Do NOT use markdown code fences.
- If a column header has a unit qualifier (e.g. "Yield (%)"), strip the qualifier
  when writing the value ("45" not "45%").
"""

TEXT_CHUNK_SYSTEM = f"""\
You are an expert chemical data extractor. Extract ALL chemical reactions from \
the provided text chunk and convert them to SURF-format entries.

BASELINE PARAMETERS (inherit these unless the text specifies otherwise):
{{baseline_json}}

RULES:
- Set ALL _cas and _smiles fields to "PENDING_CONVERSION" — never hallucinate them.
- Use "not reported" for missing values.
- Apply unit conversions: mol% → eq (÷100), minutes → hours (÷60).
- Yield type vocabulary: {_YIELD_TYPES}
- rxn_id format: {{lastname}}_{{year}}_T{{table_num}}_E{{entry_num}}

Return ONLY a valid JSON object:
{{
  "reactions": [
    {{ ... all SURF fields ... }}
  ]
}}
No text outside the JSON.  No markdown code fences.
"""


class ScientistAgent(BaseAgent):
    """Chemical domain expert: baseline extraction + structured row processing."""

    def __init__(self, client=None):
        super().__init__(client=client, name="ScientistAgent")

    # ------------------------------------------------------------------
    # Phase 1: Baseline from General Procedures
    # ------------------------------------------------------------------

    def establish_baseline(
        self, general_procedures: str, main_text: str = ""
    ) -> dict:
        """
        Read the General Procedures section and return a flat dict of default
        SURF field values.  Falls back to an empty dict on LLM failure.
        """
        if not general_procedures.strip():
            logger.info("No GP text available – using empty baseline.")
            return {}

        context = general_procedures[:6000]  # keep prompt tight
        if main_text:
            # Append the first 1000 chars of main text for reaction type/name hints
            context += f"\n\n=== MAIN PAPER CONTEXT ===\n{main_text[:1000]}"

        try:
            raw = self._chat(
                system=BASELINE_SYSTEM,
                user=f"=== GENERAL PROCEDURES ===\n{context}",
                max_tokens=_MAX_TOKENS_BASELINE,
                temperature=0.0,
                extra={"response_format": {"type": "json_object"}},
            )
            baseline = self._parse_json_object(raw)
            if baseline:
                logger.info(
                    "Baseline established: %d non-empty fields.",
                    sum(1 for v in baseline.values() if v),
                )
                return baseline
        except Exception as exc:
            logger.warning("Baseline LLM call failed: %s – using empty baseline.", exc)

        return {}

    # ------------------------------------------------------------------
    # Phase 2a: Structured chunk processing (preferred path)
    # ------------------------------------------------------------------

    def process_chunk(
        self,
        rows: list[TableRow],
        baseline: dict,
        table: ParsedTable,
        source_info: dict,
        user_instructions: str = "",
    ) -> list[dict]:
        """
        Process a small batch (3–5) of TableRow objects.

        Args:
            rows:             The TableRow batch to process.
            baseline:         Flat dict of default SURF values from the GP.
            table:            The ParsedTable these rows belong to (for context).
            source_info:      {"lastname": str, "year": str, "doi": str}
            user_instructions: Free-text overrides from the user.

        Returns:
            A list of SURF row dicts, one per input row.
        """
        if not rows:
            return []

        lastname = source_info.get("lastname", "unknown")
        year     = source_info.get("year", "2024")
        doi      = source_info.get("doi", "unknown")

        # Build the "RAW ROWS" block
        rows_text = self._format_rows_for_prompt(rows)

        instructions_block = (
            f"\nUSER INSTRUCTIONS / OVERRIDES:\n{user_instructions}\n"
            if user_instructions.strip() else ""
        )

        user_msg = f"""\
BASELINE (inherit non-empty values into every row):
{json.dumps({k: v for k, v in baseline.items() if v}, indent=2)}

TABLE CONTEXT:
  Table ID  : {table.table_id}
  Caption   : {table.caption or "(not available)"}
  Headers   : {table.headers}

SOURCE INFO:
  DOI       : {doi}
  Author    : {lastname}
  Year      : {year}
  rxn_id pattern: {lastname}_{year}_{table.table_id}_<entry_id>
{instructions_block}
RAW ROWS (process ALL of them):
{rows_text}

Generate one SURF reaction entry per row. Return the JSON object now.
"""

        try:
            raw = self._chat(
                system=CHUNK_SYSTEM,
                user=user_msg,
                max_tokens=_MAX_TOKENS_CHUNK,
                temperature=0.0,
                extra={"response_format": {"type": "json_object"}},
            )
            return self._extract_reactions(raw)
        except Exception as exc:
            logger.error(
                "Chunk LLM call failed for %s rows %s: %s",
                table.table_id, [r.entry_id for r in rows], exc,
            )
            return []

    # ------------------------------------------------------------------
    # Phase 2b: Text-chunk fallback (for image tables / no structure)
    # ------------------------------------------------------------------

    def process_text_chunk(
        self,
        text_chunk: str,
        baseline: dict,
        source_info: dict,
        chunk_index: int = 0,
        total_chunks: int = 1,
        images: Optional[list[dict]] = None,
        user_instructions: str = "",
    ) -> list[dict]:
        """
        Fallback: process a raw text chunk when no structured tables are available.
        Behaves like the old ExtractionAgent but with baseline inheritance and
        strict JSON output.
        """
        lastname = source_info.get("lastname", "unknown")
        year     = source_info.get("year", "2024")
        doi      = source_info.get("doi", "unknown")

        baseline_json = json.dumps(
            {k: v for k, v in baseline.items() if v}, indent=2
        )
        system = TEXT_CHUNK_SYSTEM.replace(
            "{baseline_json}", baseline_json
        ).replace("{lastname}", lastname).replace("{year}", year)

        instructions_block = (
            f"USER INSTRUCTIONS:\n{user_instructions}\n\n"
            if user_instructions.strip() else ""
        )

        user_msg = (
            f"{instructions_block}"
            f"SOURCE DOI: {doi}\n\n"
            f"[Document chunk {chunk_index + 1} of {total_chunks}]\n\n"
            f"{text_chunk}\n\n"
            "Extract ALL chemical reactions from this chunk. "
            "Return the JSON object now."
        )

        try:
            if images:
                raw = self._chat_with_images(
                    system=system,
                    text=user_msg,
                    images=images,
                    max_tokens=_MAX_TOKENS_TEXT,
                    temperature=0.0,
                )
            else:
                raw = self._chat(
                    system=system,
                    user=user_msg,
                    max_tokens=_MAX_TOKENS_TEXT,
                    temperature=0.0,
                    extra={"response_format": {"type": "json_object"}},
                )
            return self._extract_reactions(raw)
        except Exception as exc:
            logger.error("Text-chunk LLM call failed (chunk %d): %s", chunk_index + 1, exc)
            return []

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_rows_for_prompt(rows: list[TableRow]) -> str:
        """Render table rows as a numbered list of header→value pairs for the prompt."""
        lines = []
        for r in rows:
            cell_str = " | ".join(f"{k}: {v}" for k, v in r.raw_cells.items() if v)
            lines.append(f"  {r.entry_id}: {cell_str}")
        return "\n".join(lines)

    def _extract_reactions(self, raw: str) -> list[dict]:
        """
        Parse the LLM response.  Expects {"reactions": [...]}.
        Falls back to scanning for any complete {...} objects.
        """
        cleaned = self._strip_fences(raw)

        # Primary: try full parse
        try:
            data = json.loads(cleaned)
            if isinstance(data, dict) and "reactions" in data:
                rxns = data["reactions"]
                if isinstance(rxns, list):
                    return rxns
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass

        # Secondary: look for {"reactions": [...]} embedded in text
        match = re.search(r'"reactions"\s*:\s*(\[.*?\])', cleaned, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass

        # Tertiary: extract all complete {...} objects (truncation recovery)
        return self._extract_objects(cleaned)

    def _parse_json_object(self, raw: str) -> dict:
        """Parse a single JSON object from the LLM response."""
        cleaned = self._strip_fences(raw)
        start = cleaned.find("{")
        if start == -1:
            return {}
        cleaned = cleaned[start:]
        try:
            data = json.loads(cleaned)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
        return {}

    @staticmethod
    def _strip_fences(text: str) -> str:
        return re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`").strip()

    @staticmethod
    def _extract_objects(text: str) -> list[dict]:
        """Walk the string character-by-character and collect complete {...} objects."""
        results: list[dict] = []
        depth = 0
        obj_start = None
        in_string = False
        escape_next = False
        for i, ch in enumerate(text):
            if escape_next:
                escape_next = False
                continue
            if ch == "\\" and in_string:
                escape_next = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                if depth == 0:
                    obj_start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and obj_start is not None:
                    try:
                        obj = json.loads(text[obj_start: i + 1])
                        if isinstance(obj, dict) and "rxn_id" in obj:
                            results.append(obj)
                    except json.JSONDecodeError:
                        pass
                    obj_start = None
        if results:
            logger.info("Truncation recovery extracted %d reaction(s).", len(results))
        return results


# ---------------------------------------------------------------------------
# Extend BaseAgent._chat to accept the `extra` kwarg
# ---------------------------------------------------------------------------
# Monkey-patch _chat to forward `extra` to the PortKeyClient.
# This avoids modifying base_agent.py while still supporting response_format.

_orig_base_chat = BaseAgent._chat  # type: ignore[attr-defined]


def _patched_chat(
    self,
    system: str,
    user: str,
    max_tokens: int = 8192,
    temperature: float = 0.0,
    extra: Optional[dict] = None,
) -> str:
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    self.logger.info("[%s] Calling LLM (%d user chars)…", self.name, len(user))
    result = self.client.chat(
        messages, max_tokens=max_tokens, temperature=temperature, extra=extra
    )
    self.logger.info("[%s] LLM responded (%d chars).", self.name, len(result))
    return result


# Apply only if not already patched
if not getattr(BaseAgent, "_chat_extra_patched", False):
    BaseAgent._chat = _patched_chat          # type: ignore[method-assign]
    BaseAgent._chat_extra_patched = True     # type: ignore[attr-defined]
