"""
Coordinator Agent: orchestrates the full multi-agent SURF extraction workflow.

Pipeline:
  1. Parser Tool (MERMaid)         – extract text + images from PDFs
  2. Extraction Agent              – draft SURF data from parsed content
  3. Reviewer / Critic Agent       – validate and correct the draft
  4. Chemical Resolution Agent     – resolve PENDING_CONVERSION entries
  5. Formatter Agent               – produce final SURF TSV
"""

from __future__ import annotations
import logging
import tempfile
from pathlib import Path
from typing import Callable, Optional

from backend.integrations.mermaid_wrapper import parse_pdfs
from backend.agents.extraction_agent import ExtractionAgent
from backend.agents.reviewer_agent import ReviewerAgent
from backend.agents.chem_resolver_agent import ChemResolverAgent
from backend.agents.formatter_agent import FormatterAgent
from backend.portkey_client import get_client

logger = logging.getLogger(__name__)

StatusCallback = Callable[[str, str], None]   # (status, message)


class CoordinatorAgent:
    """
    Top-level orchestrator for the SURF extraction multi-agent workflow.
    """

    def __init__(self, client=None):
        self.client = client or get_client()
        self.extraction_agent = ExtractionAgent(client=self.client)
        self.reviewer_agent = ReviewerAgent(client=self.client)
        self.chem_resolver = ChemResolverAgent()
        self.formatter = FormatterAgent()

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

        Args:
            main_pdf_path:      Path to the main publication PDF.
            si_pdf_path:        Path to the supplementary information PDF (optional).
            user_instructions:  User-provided extraction rules/overrides.
            use_visualheist:    Whether to run VisualHeist image extraction.
            large_model:        Use the large VisualHeist model.
            on_status:          Optional callback(status_key, message) for progress updates.

        Returns:
            (tsv_content: str, issues: list[str])
        """
        def _emit(status: str, msg: str):
            logger.info("[Coordinator] %s: %s", status, msg)
            if on_status:
                on_status(status, msg)

        # ------------------------------------------------------------------ #
        # Step 1: Parse PDFs
        # ------------------------------------------------------------------ #
        _emit("parsing", "Extracting text and images from PDFs (MERMaid)…")

        with tempfile.TemporaryDirectory(prefix="surf_images_") as img_dir:
            parsed = parse_pdfs(
                main_pdf_path=main_pdf_path,
                si_pdf_path=si_pdf_path,
                image_output_dir=img_dir,
                use_visualheist=use_visualheist,
                large_model=large_model,
            )

            main_text = parsed["main_text"]
            si_text = parsed["si_text"]
            images = parsed["images"]

            _emit(
                "parsing",
                f"Parsed: {len(main_text)} chars (main), {len(si_text)} chars (SI), "
                f"{len(images)} images extracted.",
            )

            # ------------------------------------------------------------------ #
            # Step 2: Extraction Agent
            # ------------------------------------------------------------------ #
            _emit("extracting", "Running Extraction Agent – chunked pass over full text…")

            extracted_rows = self.extraction_agent.run(
                main_text=main_text,
                si_text=si_text,
                images=images,
                user_instructions=user_instructions,
            )
            _emit("extracting", f"Extraction complete: {len(extracted_rows)} reaction rows found.")

            if not extracted_rows:
                return "", ["Extraction Agent returned no reaction data."]

            # ------------------------------------------------------------------ #
            # Step 3: Reviewer / Critic Agent
            # ------------------------------------------------------------------ #
            _emit("reviewing", "Running Reviewer Agent (validation & correction)…")

            reviewed_rows, issues = self.reviewer_agent.run(
                extracted_rows=extracted_rows,
                main_text=main_text,
                si_text=si_text,
            )
            if issues:
                _emit("reviewing", f"Reviewer found {len(issues)} issue(s): " + "; ".join(issues[:3]))
            else:
                _emit("reviewing", "Reviewer: all checks passed.")

            # ------------------------------------------------------------------ #
            # Step 4: Chemical Resolution Agent
            # ------------------------------------------------------------------ #
            _emit("resolving", "Running Chemical Resolution Agent (ChemConverter)…")

            resolved_rows = self.chem_resolver.run(reviewed_rows)
            _emit("resolving", f"Chemical resolution complete for {len(resolved_rows)} rows.")

            # ------------------------------------------------------------------ #
            # Step 5: Formatter Agent
            # ------------------------------------------------------------------ #
            _emit("formatting", "Formatting results as SURF tab-separated CSV…")
            tsv_content = self.formatter.run(resolved_rows)
            _emit("formatting", f"SURF file generated: {len(tsv_content)} bytes.")

        return tsv_content, issues
