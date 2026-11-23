# src/run_all.py
"""
Orquestrador CLI do projeto `terras_raras_brasil`.
Uso:
  python src/run_all.py [--skip-download] [--only-figures] [--figures 01a 01b 06b 12] [--png-only]
"""

from __future__ import annotations

import argparse
import importlib
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import build_figures, etl_local_sources, fetch_online_sources

logging.basicConfig(level=logging.INFO, format="[RUN ALL] %(levelname)s: %(message)s")


def main() -> None:
    parser = argparse.ArgumentParser(description="Pipeline Terras Raras no Brasil (Figuras 1a, 1b, 6b, 12 a 600 DPI).")
    parser.add_argument("--skip-download", action="store_true", help="Pula download online usando bases locais em data/raw/ e data/processed/.")
    parser.add_argument("--only-figures", action="store_true", help="Gera apenas as figuras 600 DPI.")
    parser.add_argument("--figures", nargs="+", default=build_figures.PUBLISHED_FIGURES, help="IDs das figuras (padrao: 01a 01b 06b 12).")
    parser.add_argument("--png-only", action="store_true", help="Salva apenas PNG 600 DPI (sem SVG).")
    parser.add_argument("--full", action="store_true", help="Executa pipeline local estendido (requer modulos locais opcionais).")
    args = parser.parse_args()

    if args.only_figures:
        build_figures.main(figures=args.figures, png_only=args.png_only)
        return

    if args.full:
        try:
            curated = importlib.import_module("src.build_curated_layers")
            tables = importlib.import_module("src.build_tables")
            dp = importlib.import_module("src.build_data_paper")
            audit = importlib.import_module("src.audit_pipeline")
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Modo --full requer os modulos locais completos (socioambiental, tabelas e Data Paper) nao publicados no repositorio enxuto."
            ) from exc
        if not args.skip_download:
            fetch_online_sources.main()
        etl_local_sources.main()
        curated.main()
        tables.main()
        build_figures.main(figures=list(build_figures.FIGURE_BUILDERS.keys()), png_only=args.png_only)
        dp.main()
        audit.main()
        return

    if not args.skip_download:
        logging.info("Etapa 1/3: Download oficial das fontes online (ANM, IBGE UF, VLIZ ZEE, Natural Earth, Comex Stat)...")
        orig_argv = sys.argv
        try:
            sys.argv = [orig_argv[0]]
            fetch_online_sources.main()
        finally:
            sys.argv = orig_argv
    else:
        logging.info("Etapa 1/3: Pulando download online (--skip-download).")

    logging.info("Etapa 2/3: ETL geoespacial ANM/SIGMINE (Cadastro ANM + Coprodutos) e basemaps...")
    etl_local_sources.build_basemaps()
    etl_local_sources.build_anm_strict_and_coproducts()

    logging.info("Etapa 3/3: Geracao das figuras selecionadas (600 DPI)...")
    build_figures.main(figures=args.figures, png_only=args.png_only)
    logging.info("Pipeline terras_raras_brasil concluido com sucesso!")


if __name__ == "__main__":
    main()
