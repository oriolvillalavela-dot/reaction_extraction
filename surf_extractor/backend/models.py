"""
Pydantic models shared across the application.
"""

from __future__ import annotations
from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    PENDING = "pending"
    PARSING = "parsing"
    EXTRACTING = "extracting"
    REVIEWING = "reviewing"
    RESOLVING = "resolving"
    FORMATTING = "formatting"
    DONE = "done"
    FAILED = "failed"


class JobProgress(BaseModel):
    job_id: str
    status: JobStatus
    step: str = ""
    message: str = ""
    error: Optional[str] = None
    download_url: Optional[str] = None


class ExtractionRequest(BaseModel):
    user_instructions: str = Field(default="", description="User-specific extraction rules/overrides")
    use_visualheist: bool = Field(default=True, description="Whether to run VisualHeist image extraction")
    large_model: bool = Field(default=False, description="Use the large VisualHeist model")


class SURFRow(BaseModel):
    """Represents a single reaction row in the SURF format."""
    rxn_id: str = ""
    source_id: str = ""
    source_type: str = ""
    rxn_date: str = ""
    rxn_type: str = ""
    rxn_name: str = ""
    rxn_tech: str = ""
    temperature_deg_c: str = ""
    time_h: str = ""
    atmosphere: str = ""
    stirring_shaking: str = ""
    scale_mol: str = ""
    concentration_mol_l: str = ""
    wavelength_nm: str = ""
    startingmat_1_name: str = ""
    startingmat_1_cas: str = ""
    startingmat_1_smiles: str = ""
    startingmat_1_eq: str = ""
    reagent_1_name: str = ""
    reagent_1_cas: str = ""
    reagent_1_smiles: str = ""
    reagent_1_eq: str = ""
    reagent_2_name: str = ""
    reagent_2_cas: str = ""
    reagent_2_smiles: str = ""
    reagent_2_eq: str = ""
    catalyst_1_name: str = ""
    catalyst_1_cas: str = ""
    catalyst_1_smiles: str = ""
    catalyst_1_eq: str = ""
    ligand_1_name: str = ""
    ligand_1_cas: str = ""
    ligand_1_smiles: str = ""
    ligand_1_eq: str = ""
    additive_1_name: str = ""
    additive_1_cas: str = ""
    additive_1_smiles: str = ""
    additive_1_eq: str = ""
    solvent_1_name: str = ""
    solvent_1_cas: str = ""
    solvent_1_smiles: str = ""
    solvent_1_fraction: str = ""
    product_1_name: str = ""
    product_1_cas: str = ""
    product_1_smiles: str = ""
    product_1_yield: str = ""
    product_1_yieldtype: str = ""
    product_1_ms: str = ""
    product_1_nmr: str = ""
    product_2_name: str = ""
    product_2_cas: str = ""
    product_2_smiles: str = ""
    product_2_yield: str = ""
    product_2_yieldtype: str = ""
    product_2_ms: str = ""
    product_2_nmr: str = ""
    procedure: str = ""
    comment: str = ""

    class Config:
        extra = "allow"   # Allow dynamic additional compound columns


# SURF canonical column order
SURF_COLUMNS = [
    "rxn_id", "source_id", "source_type", "rxn_date", "rxn_type", "rxn_name",
    "rxn_tech", "temperature_deg_c", "time_h", "atmosphere", "stirring_shaking",
    "scale_mol", "concentration_mol_l", "wavelength_nm",
    "startingmat_1_name", "startingmat_1_cas", "startingmat_1_smiles", "startingmat_1_eq",
    "reagent_1_name", "reagent_1_cas", "reagent_1_smiles", "reagent_1_eq",
    "reagent_2_name", "reagent_2_cas", "reagent_2_smiles", "reagent_2_eq",
    "catalyst_1_name", "catalyst_1_cas", "catalyst_1_smiles", "catalyst_1_eq",
    "ligand_1_name", "ligand_1_cas", "ligand_1_smiles", "ligand_1_eq",
    "additive_1_name", "additive_1_cas", "additive_1_smiles", "additive_1_eq",
    "solvent_1_name", "solvent_1_cas", "solvent_1_smiles", "solvent_1_fraction",
    "product_1_name", "product_1_cas", "product_1_smiles", "product_1_yield", "product_1_yieldtype",
    "product_1_ms", "product_1_nmr",
    "product_2_name", "product_2_cas", "product_2_smiles", "product_2_yield", "product_2_yieldtype",
    "product_2_ms", "product_2_nmr",
    "procedure", "comment",
]
