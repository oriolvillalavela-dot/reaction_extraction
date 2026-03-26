"""
Formatter Agent: compiles resolved SURF rows into the strict tab-separated CSV format.
"""

from __future__ import annotations
import csv
import io
import logging
from backend.models import SURF_COLUMNS

logger = logging.getLogger(__name__)


class FormatterAgent:
    """Converts a list of SURF row dicts to a tab-separated file content string."""

    def run(self, rows: list[dict]) -> str:
        """
        Convert rows to SURF tab-separated format.

        Returns the TSV file content as a string (UTF-8).
        """
        if not rows:
            logger.warning("FormatterAgent received empty rows – returning header-only TSV.")
            return "\t".join(SURF_COLUMNS) + "\n"

        # Discover any extra compound columns dynamically
        # (e.g. reagent_3, catalyst_2 not in the base schema)
        extra_cols = []
        for row in rows:
            for key in row:
                if key not in SURF_COLUMNS and key not in extra_cols:
                    extra_cols.append(key)

        all_columns = SURF_COLUMNS + extra_cols

        buf = io.StringIO()
        writer = csv.DictWriter(
            buf,
            fieldnames=all_columns,
            delimiter="\t",
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()

        for row in rows:
            # Sanitise cell values: strip newlines/tabs that would break TSV
            clean_row = {}
            for col in all_columns:
                val = str(row.get(col, "")).replace("\t", " ").replace("\n", " ").replace("\r", "")
                clean_row[col] = val
            writer.writerow(clean_row)

        tsv_content = buf.getvalue()
        logger.info(
            "Formatter produced %d reaction rows, %d columns.",
            len(rows), len(all_columns),
        )
        return tsv_content
