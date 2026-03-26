from typing import Optional

def iupac_to_kekule_smiles(name: str) -> Optional[str]:
    """
    Convert IUPAC name -> SMILES preserving stereochemistry.
    Strategy:
      1) Ask CIRpy for StdInChI (preferred; InChI carries stereochemistry).
      2) Build an RDKit Mol from InChI and export isomeric SMILES (with @/@@).
      3) If CIRpy InChI fails, fall back to CIRpy SMILES -> RDKit isomeric SMILES.
    Returns None on failure.
    """
    try:
        import cirpy
        from rdkit import Chem

        # Try to get an InChI first (stereo-safe)
        inchi = cirpy.resolve(name, 'stdinchi') or cirpy.resolve(name, 'inchi')
        if inchi:
            s = inchi if inchi.upper().startswith('INCHI=') else f'InChI={inchi}'
            mol = Chem.MolFromInchi(s)
            if mol:
                # isomericSmiles=True ensures @/@@ for stereocenters
                return Chem.MolToSmiles(mol, isomericSmiles=True)

        # Fallback: CIRpy SMILES -> RDKit mol -> isomeric SMILES (best effort)
        smiles = cirpy.resolve(name, 'smiles')
        if not smiles:
            return None
        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            return smiles
        return Chem.MolToSmiles(mol, isomericSmiles=True)
    except Exception:
        return None
