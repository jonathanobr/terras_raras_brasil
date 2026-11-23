# tests/test_terras_raras_integrity.py
"""
Suite automatizada de integridade ontologica, curada, funcional e cartografica
do repositorio publicado `terras_raras_brasil`.
Compativel com clone limpo (GitHub Actions CI) e execucao local.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pytest
from PIL import Image

from src import build_figures, etl_local_sources, fetch_online_sources, run_all
from src.config import (
    DATA_CURATED,
    FIGURES_PNG_DIR,
    PROJECT_ROOT,
    STRICT_REE_SUBSTANCES,
    classify_anm_maturity_group,
    classify_anm_stage,
    fmt_int_ptbr,
    fmt_ptbr,
    normalize_text,
)

PUBLISHED_FIGURE_STEMS = [
    "fig_01a_mapa_nacional_etr_estrito",
    "fig_01b_mapa_nacional_etr_com_coprodutos",
    "fig_06b_zoom_concessoes_lavra_serra_verde_minacu",
    "fig_12_comercio_exterior_comex_stat",
]


def test_anchor_projects_curated_integrity():
    anc_path = DATA_CURATED / "anchor_projects_and_midstream.json"
    assert anc_path.exists()
    data = json.loads(anc_path.read_text(encoding="utf-8"))
    projects = data["projects"]
    assert len(projects) == 17
    ids = {p["id"] for p in projects}
    assert "PRJ-01" in ids and "MID-01" in ids
    for p in projects:
        assert -34.5 <= float(p["latitude"]) <= 6.0
        assert -74.5 <= float(p["longitude"]) <= -28.0
        assert p["project_name"] and p["geology_type"]


def test_ree_ontology_and_helpers():
    assert "TERRAS RARAS" in STRICT_REE_SUBSTANCES
    assert "MONAZITA" in STRICT_REE_SUBSTANCES
    assert "CITRINO" not in STRICT_REE_SUBSTANCES

    assert normalize_text("Minério de Cério") == "MINERIO DE CERIO"
    assert classify_anm_stage("CONCESSÃO DE LAVRA") == "Concessão de Lavra"
    assert classify_anm_maturity_group("Concessão de Lavra") == "Lavra Ativa / Concessão"
    assert classify_anm_stage("AUTORIZAÇÃO DE PESQUISA") == "Autorização de Pesquisa"
    assert fmt_ptbr(5031201.82, 1) == "5.031.201,8"
    assert fmt_int_ptbr(3138) == "3.138"

    assert fetch_online_sources.TARGET_SH6 == ("280530", "284610", "284690", "850511")
    assert "ibge.gov.br" in fetch_online_sources.IBGE_UF_URL
    assert "vliz.be" in fetch_online_sources.VLIZ_EEZ_URL
    assert "natural-earth" in fetch_online_sources.NE_COUNTRIES_URL


@pytest.mark.parametrize("stem", PUBLISHED_FIGURE_STEMS)
def test_published_4_figures_600dpi(stem: str):
    png = FIGURES_PNG_DIR / f"{stem}_600dpi.png"
    assert png.exists(), f"Figura publicada ausente: {png}"
    assert png.stat().st_size > 50_000
    with Image.open(png) as im:
        w, h = im.size
        dpi = im.info.get("dpi", (0, 0))
        assert dpi[0] >= 599 and dpi[1] >= 599
        assert 0.55 <= (w / max(h, 1)) <= 2.45
        arr = np.asarray(im.convert("RGB"), dtype=np.float32)
        assert float(arr[::16, ::16].var()) > 100.0


def test_repo_metadata_licenses_and_modules():
    for rel in (
        "LICENSE",
        "output/LICENSE",
        "NOTICE.md",
        "CITATION.cff",
        "README.md",
        "pyproject.toml",
        "requirements.txt",
    ):
        p = PROJECT_ROOT / rel
        assert p.exists(), f"Arquivo institucional ausente: {p}"
        assert p.stat().st_size > 50

    assert "MIT License" in (PROJECT_ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "CC BY 4.0" in (PROJECT_ROOT / "output" / "LICENSE").read_text(encoding="utf-8")

    readme_txt = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    assert "C:\\Users\\jonat" not in readme_txt
    assert "OneDrive" not in readme_txt
    assert "../recursos" not in readme_txt
    for stem in PUBLISHED_FIGURE_STEMS:
        assert f"{stem}_600dpi.png" in readme_txt

    assert build_figures.PUBLISHED_FIGURES == ["01a", "01b", "06b", "12"]
    assert callable(etl_local_sources.build_anm_strict_and_coproducts)
    assert callable(run_all.main)
