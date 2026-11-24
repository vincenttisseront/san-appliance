"""Emplacements live, conf et data. Aucun n'est choisi tout seul."""

import os
from pathlib import Path


class SanPaths:
    def __init__(self, root=None):
        base = Path(root if root is not None else os.environ.get("SAN_ROOT", ""))
        # Un root vide désigne la racine du système. Un root de test est un répertoire.
        if str(base) in ("", "/"):
            self.root = Path("/")
        else:
            self.root = base
        self.etc = self.root / "etc" / "san"
        self.run = self.root / "run" / "san"
        self.conf = self.root / "var" / "lib" / "san" / "conf"
        self.data = self.root / "var" / "lib" / "san" / "data"
        self.choice = self.etc / "chosen.json"

    def ensure_runtime(self):
        self.etc.mkdir(parents=True, exist_ok=True)
        self.run.mkdir(parents=True, exist_ok=True)
        os.chmod(self.etc, 0o755)
        os.chmod(self.run, 0o755)
