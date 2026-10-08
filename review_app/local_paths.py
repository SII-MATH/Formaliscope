"""Shared project-local storage layout; production may supply its own data dir."""
import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def local_root(project_root=None):
    return Path(project_root or PROJECT_ROOT) / '.formaliscope'


def runtime_dir():
    return Path(os.environ['REVIEW_DATA_DIR']) if 'REVIEW_DATA_DIR' in os.environ else local_root() / 'runtime'
