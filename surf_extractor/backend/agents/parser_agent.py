"""
Parser Agent: structural document understanding.

Responsibilities:
  1. Extract reaction tables from the Main Paper PDF using PyMuPDF find_tables().
     Each table is converted to a list of TableRow objects with raw cell dicts.
  2. Count the exact number of expected reactions per table and in total.
  3. Extract the General Procedures section from the SI text so the Scientist
     Agent can establish baseline parameters in a single dedicated LLM call.
  4. Extract source metadata (DOI, first-author lastname, year) for rxn_id generation.

Falls back gracefully when find_tables() is unavailable (old PyMuPDF) or when
no reaction tables are detected (tables are figures/images), returning an empty
tables list so the Coordinator can use the text-chunk fallback path.
"""

from __future__ import annotations
import re
import logging
from typing import Optional

from backend.models import TableRow, ParsedTable, ParsedDocument

logger = logging.getLogger("agents.ParserAgent")

# ---------------------------------------------------------------------------
# Heuristics for reaction table detection
# ---------------------------------------------------------------------------

_REACTION_KEYWORDS = {
    "entry", "yield", "catalyst", "ligand", "base", "solvent", "reagent",
    "temperature", "temp", "time", "conversion", "ee", "er", "dr",
    "loading", "equiv", "equivalents", "product", "substrate",
    "photocatalyst", "oxidant", "reductant", "additive", "selectivity",
    "t (h)", "t(h)", "isolated", "t (°c)", "smiles", "cas",
}
# A table must have at least this many keyword hits in its headers
_MIN_KEYWORD_HITS = 2

# ---------------------------------------------------------------------------
# Heuristics for entry-ID column names
# ---------------------------------------------------------------------------
_ENTRY_COLUMN_NAMES = {"entry", "#", "no.", "no", "run", "exp", "experiment"}

# ---------------------------------------------------------------------------
# General Procedure section-header patterns
# ---------------------------------------------------------------------------
_GP_HEADER_PATTERN = re.compile(
    r"(?:general\s+procedure[s]?|standard\s+condition[s]?|typical\s+procedure|"
    r"general\s+method|general\s+protocol|standard\s+protocol|"
    r"general\s+experimental)",
    re.IGNORECASE,
)
_GP_WINDOW = 3000          # chars to capture after each GP header
_GP_FALLBACK_WINDOW = 4000  # chars from SI start if no GP header found

# ---------------------------------------------------------------------------
# Source-info extraction patterns
# ---------------------------------------------------------------------------
_DOI_PATTERN = re.compile(r"10\.\d{4,}/[\w.\-/;()+:]+")
_YEAR_PATTERN = re.compile(r"\b(20\d{2}|19\d{2})\b")
_AUTHOR_PATTERN = re.compile(
    r"([A-Z][a-z]{1,20}),\s+[A-Z][a-z]?\."  # "Smith, J." or "Smith, John"
)


class ParserAgent:
    """
    Structural PDF parser.  Returns a ParsedDocument containing:
    - structured ParsedTable objects with TableRow lists
    - the General Procedures text for the Scientist's baseline step
    - source metadata for rxn_id generation
    - raw texts + images (passed through from MERMaid wrapper)
    """

    def run(
        self,
        main_pdf_path: str,
        si_pdf_path: Optional[str],
        main_text: str,
        si_text: str,
        images: list[dict],
    ) -> ParsedDocument:
        tables = self._extract_tables(main_pdf_path)

        # If the main paper produced no tables, try the SI
        if not tables and si_pdf_path:
            tables = self._extract_tables(si_pdf_path)

        general_procedures = self._extract_general_procedures(si_text)

        total = sum(t.expected_count for t in tables)
        logger.info(
            "ParserAgent: %d reaction table(s) found, %d rows total. GP: %d chars.",
            len(tables), total, len(general_procedures),
        )

        return ParsedDocument(
            main_text=main_text,
            si_text=si_text,
            general_procedures=general_procedures,
            tables=tables,
            images=images,
        )

    # ------------------------------------------------------------------
    # Table extraction
    # ------------------------------------------------------------------

    def _extract_tables(self, pdf_path: str) -> list[ParsedTable]:
        """Use PyMuPDF find_tables() to extract all reaction tables from a PDF."""
        try:
            import fitz  # PyMuPDF
        except ImportError:
            logger.warning("PyMuPDF (fitz) not installed – skipping structural table parse.")
            return []

        tables: list[ParsedTable] = []
        table_counter = 0

        try:
            doc = fitz.open(pdf_path)
        except Exception as exc:
            logger.error("Cannot open PDF %s: %s", pdf_path, exc)
            return []

        for page_num, page in enumerate(doc):
            try:
                tab_finder = page.find_tables()
            except AttributeError:
                # find_tables() requires PyMuPDF >= 1.23; fall back silently
                logger.warning(
                    "page.find_tables() unavailable (PyMuPDF < 1.23) – "
                    "install a newer version for structural table parsing."
                )
                doc.close()
                return []
            except Exception as exc:
                logger.debug("find_tables() error on page %d: %s", page_num + 1, exc)
                continue

            for tab in tab_finder.tables:
                try:
                    parsed = self._parse_fitz_table(tab, table_counter + 1)
                    if parsed is not None:
                        table_counter += 1
                        tables.append(parsed)
                except Exception as exc:
                    logger.debug("Table parse error (page %d): %s", page_num + 1, exc)

        doc.close()
        return tables

    def _parse_fitz_table(self, tab, candidate_id: int) -> Optional[ParsedTable]:
        """Convert a single PyMuPDF Table object into a ParsedTable, or None if rejected."""
        raw = tab.extract()          # list[list[str | None]]
        if not raw or len(raw) < 2:  # need at least header + 1 data row
            return None

        # First row = headers; normalise None/whitespace
        headers = [str(h or "").strip() for h in raw[0]]
        if not self._is_reaction_table(headers):
            return None

        table_id = f"T{candidate_id}"
        rows: list[TableRow] = []

        for row_idx, raw_row in enumerate(raw[1:]):
            cells = {
                headers[i]: str(raw_row[i] or "").strip()
                for i in range(min(len(headers), len(raw_row)))
            }
            entry_id = self._extract_entry_id(headers, cells, row_idx)
            # Skip blank rows (all cells empty)
            if not any(cells.values()):
                continue
            rows.append(TableRow(
                table_id=table_id,
                entry_id=entry_id,
                row_index=row_idx,
                raw_cells=cells,
            ))

        if not rows:
            return None

        return ParsedTable(
            table_id=table_id,
            caption="",          # caption extraction is not reliable from find_tables
            headers=headers,
            rows=rows,
        )

    def _is_reaction_table(self, headers: list[str]) -> bool:
        """Return True if ≥ _MIN_KEYWORD_HITS of the headers match reaction keywords."""
        hits = sum(
            1 for h in headers
            if any(kw in h.lower() for kw in _REACTION_KEYWORDS)
        )
        return hits >= _MIN_KEYWORD_HITS

    def _extract_entry_id(
        self, headers: list[str], cells: dict[str, str], row_idx: int
    ) -> str:
        """
        Try to read the entry identifier from an 'Entry'/'#'/… column.
        Falls back to 1-based row index.
        """
        for h in headers:
            if h.lower() in _ENTRY_COLUMN_NAMES:
                val = cells.get(h, "").strip()
                if val:
                    # Normalise: strip trailing dots, spaces
                    val = val.rstrip(". ")
                    return f"E{val}"
        return f"E{row_idx + 1}"

    # ------------------------------------------------------------------
    # General Procedures extraction
    # ------------------------------------------------------------------

    def _extract_general_procedures(self, si_text: str) -> str:
        """
        Locate and extract GP sections from the SI text.
        Returns a concatenated string of all GP sections found.
        Falls back to the first _GP_FALLBACK_WINDOW chars of the SI.
        """
        if not si_text:
            return ""

        segments: list[str] = []
        for match in _GP_HEADER_PATTERN.finditer(si_text):
            start = match.start()
            segment = si_text[start: start + _GP_WINDOW]
            segments.append(segment)

        if segments:
            return "\n\n---\n\n".join(segments)

        # Fallback: assume GP is at the beginning of the SI
        logger.debug("No explicit GP header found in SI; using first %d chars.", _GP_FALLBACK_WINDOW)
        return si_text[:_GP_FALLBACK_WINDOW]

    # ------------------------------------------------------------------
    # Source metadata extraction (static utility — used by coordinator)
    # ------------------------------------------------------------------

    @staticmethod
    def extract_source_info(main_text: str) -> dict[str, str]:
        """
        Heuristically extract DOI, publication year, and first-author last name
        from the main paper text.  Used by the Scientist to build rxn_ids.
        """
        # DOI – scan the full text
        doi_match = _DOI_PATTERN.search(main_text)
        doi = doi_match.group(0).rstrip(".,)") if doi_match else "unknown"

        # Year – first 4-digit year in the first 1000 chars (most likely pub year)
        year_match = _YEAR_PATTERN.search(main_text[:1000])
        year = year_match.group(1) if year_match else "2024"

        # First author last name – look for "Lastname, F." pattern
        author_match = _AUTHOR_PATTERN.search(main_text[:2000])
        if author_match:
            lastname = author_match.group(1).lower()
        else:
            # Fall back: first capitalised word of 4+ letters (often the author)
            words = re.findall(r"\b[A-Z][a-z]{3,}\b", main_text[:500])
            lastname = words[0].lower() if words else "unknown"

        return {"doi": doi, "year": year, "lastname": lastname}
