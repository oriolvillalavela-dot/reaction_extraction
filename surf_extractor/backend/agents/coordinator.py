"""
Coordinator (Orchestrator): central controller for the SURF extraction pipeline.

New multi-agent architecture:
  1. Parser Agent        – structural PDF understanding (tables, GP, metadata)
  2. Scientist Agent     – Phase 1: establish_baseline() from GP
                         – Phase 2: process_chunk() per small row batch (3–5 rows)
  3. QA Reviewer Agent   – count reconciliation + validation
                         – re-extraction loop for missing entries
  4. Chem Resolver Agent – resolve PENDING_CONVERSION CAS/SMILES
  5. Formatter Agent     – produce the final SURF tab-separated file

STRUCTURED PATH (preferred)
  When the Parser finds reaction tables with identifiable rows:
    for each table:
      for each chunk of 3–5 rows:
        rows += scientist.process_chunk(chunk, baseline, table, source_info)
    qa_result = qa_reviewer.run(rows, expected_count, tables)
    while qa_result.missing_entry_ids and retries < MAX_QA_RETRIES:
      re-extract each missing entry individually and re-run QA

TEXT-CHUNK FALLBACK PATH
  When no structured tables are found (scanned PDFs, image tables):
    Split combined text into overlapping 24KB windows
    for each window:
      rows += scientist.process_text_chunk(window, baseline, source_info)
    QA runs without count check (expected_count=0)

The external interface (run() signature and return type) is unchanged so that
main.py requires no edits.
"""

from __future__ import annotations
import logging
import re
import tempfile
from pathlib import Path
from typing import Callable, Optional

from backend.integrations.mermaid_wrapper import parse_pdfs
from backend.agents.parser_agent import ParserAgent
from backend.agents.scientist_agent import ScientistAgent
from backend.agents.qa_reviewer_agent import QAReviewerAgent
from backend.agents.chem_resolver_agent import ChemResolverAgent
from backend.agents.formatter_agent import FormatterAgent
from backend.models import ParsedDocument, ParsedTable, TableRow
from backend.portkey_client import get_client

logger = logging.getLogger(__name__)

StatusCallback = Callable[[str, str], None]   # (status_key, message)

# ---------------------------------------------------------------------------
# Tunable constants
# ---------------------------------------------------------------------------

CHUNK_SIZE      = 4     # table rows per Scientist prompt
MAX_QA_RETRIES  = 2     # re-extraction passes after the first QA check

# Text-chunk fallback constants (mirrors old ExtractionAgent)
_TEXT_CHUNK_SIZE    = 24_000  # chars
_TEXT_CHUNK_OVERLAP =  2_000  # chars


class CoordinatorAgent:
    """
    Top-level orchestrator for the SURF extraction multi-agent workflow.
    Public API is identical to the previous version.
    """

    def __init__(self, client=None):
        self.client       = client or get_client()
        self.parser       = ParserAgent()
        self.scientist    = ScientistAgent(client=self.client)
        self.qa_reviewer  = QAReviewerAgent(client=self.client)
        self.chem_resolver = ChemResolverAgent()
        self.formatter    = FormatterAgent()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(
        self,
        main_pdf_path: str,
        si_pdf_path: Optional[str],
        user_instructions: str = "",
        use_visualheist: bool = True,
        large_model: bool = False,
        on_status: Optional[StatusCallback] = None,
    ) -> tuple[str, list[str]]:
        """
        Execute the full pipeline.

        Returns:
            (tsv_content: str, issues: list[str])
        """
        def _emit(status: str, msg: str):
            logger.info("[Coordinator] %s: %s", status, msg)
            if on_status:
                on_status(status, msg)

        # ------------------------------------------------------------------ #
        # Step 1 – Raw PDF parse (text + images via MERMaid)
        # ------------------------------------------------------------------ #
        _emit("parsing", "Extracting text and images from PDFs (MERMaid)…")

        with tempfile.TemporaryDirectory(prefix="surf_images_") as img_dir:
            raw_parsed = parse_pdfs(
                main_pdf_path=main_pdf_path,
                si_pdf_path=si_pdf_path,
                image_output_dir=img_dir,
                use_visualheist=use_visualheist,
                large_model=large_model,
            )

            main_text = raw_parsed["main_text"]
            si_text   = raw_parsed["si_text"]
            images    = raw_parsed["images"]

            _emit(
                "parsing",
                f"MERMaid: {len(main_text):,} chars (main), "
                f"{len(si_text):,} chars (SI), {len(images)} images.",
            )

            # ------------------------------------------------------------------ #
            # Step 2 – Structural parse (Parser Agent)
            # ------------------------------------------------------------------ #
            _emit("parsing", "Parser Agent: detecting reaction tables and General Procedures…")

            parsed: ParsedDocument = self.parser.run(
                main_pdf_path=main_pdf_path,
                si_pdf_path=si_pdf_path,
                main_text=main_text,
                si_text=si_text,
                images=images,
            )

            table_summary = (
                f"{len(parsed.tables)} table(s) found, "
                f"{parsed.total_expected_reactions} rows expected."
                if parsed.tables else "No structured tables detected – will use text-chunk fallback."
            )
            _emit("parsing", f"Parser complete: {table_summary}")

            # ------------------------------------------------------------------ #
            # Step 3 – Source metadata + Baseline (Scientist Phase 1)
            # ------------------------------------------------------------------ #
            _emit("extracting", "Scientist Agent: reading General Procedures to establish baseline…")

            source_info = ParserAgent.extract_source_info(main_text)
            logger.info(
                "Source info: doi=%s  author=%s  year=%s",
                source_info["doi"], source_info["lastname"], source_info["year"],
            )

            baseline = self.scientist.establish_baseline(
                general_procedures=parsed.general_procedures,
                main_text=main_text,
            )
            _emit(
                "extracting",
                f"Baseline established: {sum(1 for v in baseline.values() if v)} non-empty fields.",
            )

            # ------------------------------------------------------------------ #
            # Step 4 – Extraction (Scientist Phase 2)
            # ------------------------------------------------------------------ #
            all_rows: list[dict] = []

            if parsed.tables:
                all_rows = self._structured_extraction(
                    parsed=parsed,
                    baseline=baseline,
                    source_info=source_info,
                    user_instructions=user_instructions,
                    emit=_emit,
                )
            else:
                all_rows = self._text_chunk_extraction(
                    main_text=main_text,
                    si_text=si_text,
                    images=images,
                    baseline=baseline,
                    source_info=source_info,
                    user_instructions=user_instructions,
                    emit=_emit,
                )

            if not all_rows:
                return "", ["Extraction produced no reaction rows."]

            # ------------------------------------------------------------------ #
            # Step 5 – QA Review + targeted re-extraction loop
            # ------------------------------------------------------------------ #
            _emit("reviewing", f"QA Reviewer: validating {len(all_rows)} rows…")

            qa_result = self.qa_reviewer.run(
                rows=all_rows,
                expected_count=parsed.total_expected_reactions,
                parsed_tables=parsed.tables,
                main_text=main_text,
                si_text=si_text,
            )
            all_rows = qa_result.rows

            # Re-extraction loop for missing entries
            retry = 0
            while qa_result.missing_entry_ids and retry < MAX_QA_RETRIES:
                retry += 1
                missing = qa_result.missing_entry_ids
                _emit(
                    "reviewing",
                    f"QA pass {retry}/{MAX_QA_RETRIES}: "
                    f"re-extracting {len(missing)} missing entr{'y' if len(missing)==1 else 'ies'} "
                    f"({', '.join(missing[:6])}{'…' if len(missing)>6 else ''})…",
                )

                reextracted = self._reextract_missing(
                    missing_keys=missing,
                    parsed=parsed,
                    baseline=baseline,
                    source_info=source_info,
                    user_instructions=user_instructions,
                )

                if reextracted:
                    # Merge: avoid duplicates by rxn_id
                    existing_ids = {r.get("rxn_id", "") for r in all_rows}
                    added = 0
                    for r in reextracted:
                        rid = r.get("rxn_id", "")
                        if rid not in existing_ids:
                            all_rows.append(r)
                            existing_ids.add(rid)
                            added += 1
                    logger.info("Re-extraction: +%d new row(s).", added)

                # Re-run QA with the augmented list
                qa_result = self.qa_reviewer.run(
                    rows=all_rows,
                    expected_count=parsed.total_expected_reactions,
                    parsed_tables=parsed.tables,
                    main_text=main_text,
                    si_text=si_text,
                )
                all_rows = qa_result.rows

            if qa_result.missing_entry_ids:
                _emit(
                    "reviewing",
                    f"QA: {len(qa_result.missing_entry_ids)} entr(ies) still missing after "
                    f"{MAX_QA_RETRIES} retries – proceeding with {len(all_rows)} rows.",
                )
            else:
                _emit("reviewing", f"QA: all {len(all_rows)} rows validated. ✓")

            # ------------------------------------------------------------------ #
            # Step 6 – Chemical Resolution
            # ------------------------------------------------------------------ #
            _emit("resolving", "Chemical Resolution Agent (ChemConverter)…")

            resolved_rows = self.chem_resolver.run(all_rows)
            _emit("resolving", f"Chemical resolution complete for {len(resolved_rows)} rows.")

            # ------------------------------------------------------------------ #
            # Step 7 – Formatter
            # ------------------------------------------------------------------ #
            _emit("formatting", "Formatting results as SURF tab-separated CSV…")
            tsv_content = self.formatter.run(resolved_rows)
            _emit("formatting", f"SURF file generated: {len(tsv_content):,} bytes.")

        return tsv_content, qa_result.issues

    # ------------------------------------------------------------------
    # Structured extraction path
    # ------------------------------------------------------------------

    def _structured_extraction(
        self,
        parsed: ParsedDocument,
        baseline: dict,
        source_info: dict,
        user_instructions: str,
        emit: Callable,
    ) -> list[dict]:
        """
        Paginated loop: iterate over each ParsedTable in CHUNK_SIZE batches.
        """
        all_rows: list[dict] = []

        for table in parsed.tables:
            expected = table.expected_count
            emit(
                "extracting",
                f"Scientist Agent: processing {table.table_id} "
                f"({expected} entries, chunks of {CHUNK_SIZE})…",
            )

            table_rows: list[dict] = []
            row_chunks = _chunk_list(table.rows, CHUNK_SIZE)

            for chunk_idx, chunk in enumerate(row_chunks):
                logger.info(
                    "  %s chunk %d/%d: entries %s",
                    table.table_id,
                    chunk_idx + 1,
                    len(row_chunks),
                    [r.entry_id for r in chunk],
                )
                extracted = self.scientist.process_chunk(
                    rows=chunk,
                    baseline=baseline,
                    table=table,
                    source_info=source_info,
                    user_instructions=user_instructions,
                )
                table_rows.extend(extracted)

            emit(
                "extracting",
                f"{table.table_id}: extracted {len(table_rows)}/{expected} rows.",
            )
            all_rows.extend(table_rows)

        return all_rows

    # ------------------------------------------------------------------
    # Text-chunk fallback path
    # ------------------------------------------------------------------

    def _text_chunk_extraction(
        self,
        main_text: str,
        si_text: str,
        images: list[dict],
        baseline: dict,
        source_info: dict,
        user_instructions: str,
        emit: Callable,
    ) -> list[dict]:
        """
        No structured tables found: fall back to overlapping text windows.
        """
        combined = (
            f"=== MAIN PAPER TEXT ===\n{main_text}\n\n"
            f"=== SUPPLEMENTARY INFORMATION TEXT ===\n{si_text}"
        )
        chunks = _make_text_chunks(combined, _TEXT_CHUNK_SIZE, _TEXT_CHUNK_OVERLAP)

        emit(
            "extracting",
            f"Text-chunk fallback: {len(chunks)} chunks "
            f"(~{_TEXT_CHUNK_SIZE:,} chars each)…",
        )

        all_rows: list[dict] = []
        seen_ids: set[str] = set()

        for idx, chunk in enumerate(chunks):
            chunk_images = images[:10] if idx == 0 else []
            extracted = self.scientist.process_text_chunk(
                text_chunk=chunk,
                baseline=baseline,
                source_info=source_info,
                chunk_index=idx,
                total_chunks=len(chunks),
                images=chunk_images,
                user_instructions=user_instructions,
            )
            new = 0
            for row in extracted:
                rxn_id = row.get("rxn_id", "").strip()
                if rxn_id and rxn_id in seen_ids:
                    continue
                if rxn_id:
                    seen_ids.add(rxn_id)
                all_rows.append(row)
                new += 1
            logger.info(
                "Text chunk %d/%d: %d new row(s) (total %d).",
                idx + 1, len(chunks), new, len(all_rows),
            )

        return all_rows

    # ------------------------------------------------------------------
    # Targeted re-extraction of missing entries
    # ------------------------------------------------------------------

    def _reextract_missing(
        self,
        missing_keys: list[str],
        parsed: ParsedDocument,
        baseline: dict,
        source_info: dict,
        user_instructions: str,
    ) -> list[dict]:
        """
        For each "T<n>_E<id>" key, locate the original TableRow and call
        the Scientist with a single-row chunk.
        """
        results: list[dict] = []
        # Build lookup: "T1_E3" -> (ParsedTable, TableRow)
        table_map: dict[str, ParsedTable] = {t.table_id: t for t in parsed.tables}

        for key in missing_keys:
            table_id, entry_id = _split_entry_key(key)
            table = table_map.get(table_id)
            if not table:
                logger.warning("Re-extract: table %s not found in parsed doc.", table_id)
                continue

            row = next((r for r in table.rows if r.entry_id == entry_id), None)
            if not row:
                logger.warning(
                    "Re-extract: entry %s not found in %s.", entry_id, table_id
                )
                continue

            logger.info("Re-extracting %s %s…", table_id, entry_id)
            extracted = self.scientist.process_chunk(
                rows=[row],
                baseline=baseline,
                table=table,
                source_info=source_info,
                user_instructions=user_instructions,
            )
            results.extend(extracted)

        return results


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _chunk_list(lst: list, size: int) -> list[list]:
    """Split lst into sub-lists of at most `size` elements."""
    return [lst[i: i + size] for i in range(0, len(lst), size)]


def _make_text_chunks(text: str, chunk_size: int, overlap: int) -> list[str]:
    """Split text into overlapping windows of chunk_size chars."""
    if len(text) <= chunk_size:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start += chunk_size - overlap
    return chunks


def _split_entry_key(key: str) -> tuple[str, str]:
    """
    Split "T1_E3" → ("T1", "E3").
    Handles entries like "T1_E1a".
    """
    match = re.match(r"(T\d+)_(E[\w]+)", key)
    if match:
        return match.group(1), match.group(2)
    # Fallback: split on first underscore
    parts = key.split("_", 1)
    return (parts[0], parts[1]) if len(parts) == 2 else (key, "")
