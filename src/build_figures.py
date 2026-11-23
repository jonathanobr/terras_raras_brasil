# src/build_figures.py
"""
Etapa 6: Geracao do Atlas Expandido de 17 Figuras a 600 DPI (PNG + SVG)
seguindo rigorosamente as diretrizes cartograficas e editoriais do projeto `recursos`:
  - Projecao equivalente IBGE Albers (SIRGAS 2000 / GRS80) em todos os mapas
  - Inclusao compulsoria da Amazonia Azul (ZEE) nos mapas nacionais
  - Eixos geograficos em graus (°W/°S), seta Norte e barra de escala em km
  - Rodapes padronizados em 4 niveis com quebra automatica (wrap_width=115)
  - Titulos visuais sobrios sem prefixo numerico rigido (`FIGURA XX:`)
  - Escala linear rigorosa em todos os graficos estatisticos
"""

from __future__ import annotations

import argparse
from functools import cached_property
import json
import logging
import sys
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import geopandas as gpd
import matplotlib.lines as mlines
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from mpl_toolkits.axes_grid1.inset_locator import inset_axes
import numpy as np
import pandas as pd
import shapely

from src.config import (
    BASEMAPS_PROCESSED,
    BIOME_COLORS,
    BLOCK_COLORS,
    CAPITAL_ORIGIN_COLORS,
    COPRODUCT_CHAIN_COLORS,
    DATA_CURATED,
    DATA_PROCESSED,
    GEOLOGY_COLORS,
    IBGE_ALBERS_CRS,
    STAGE_COLORS,
    STAGE_ORDER,
    UF_TO_REGION,
    add_cartographic_footer,
    add_figure_footer,
    fmt_int_ptbr,
    fmt_ptbr,
    format_map_axes_and_scalebar,
    get_anm_snapshot_label,
    save_figure_dual,
    setup_editorial_style,
)

logging.basicConfig(level=logging.INFO, format="[BUILD FIGURES] %(levelname)s: %(message)s")


def _clean_albers(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    g = gdf.to_crs(IBGE_ALBERS_CRS).copy()
    g["geometry"] = shapely.force_2d(g.geometry).make_valid()
    return g[~g.geometry.is_empty & g.geometry.notna()].copy()


class AtlasData:
    """Container compartilhado com carregamento sob demanda para renderizacao seletiva ou integral."""

    def __init__(self, save_svg: bool = True) -> None:
        self.save_svg = save_svg

    @cached_property
    def states_alb(self) -> gpd.GeoDataFrame:
        return _clean_albers(gpd.read_parquet(BASEMAPS_PROCESSED / "brazil_states_ibge2025.parquet"))

    @cached_property
    def eez_alb(self) -> gpd.GeoDataFrame:
        return _clean_albers(gpd.read_parquet(BASEMAPS_PROCESSED / "brazil_eez.parquet"))

    @cached_property
    def sa_alb(self) -> gpd.GeoDataFrame:
        return _clean_albers(gpd.read_parquet(BASEMAPS_PROCESSED / "south_america_countries.parquet"))

    @cached_property
    def anm_alb(self) -> gpd.GeoDataFrame:
        enriched = DATA_PROCESSED / "anm_ree_enriched.parquet"
        poly = DATA_PROCESSED / "anm_ree_polygons.parquet"
        src_p = enriched if enriched.exists() else poly
        return _clean_albers(gpd.read_parquet(src_p))

    @cached_property
    def cop_alb(self) -> gpd.GeoDataFrame:
        enriched = DATA_PROCESSED / "anm_coproduct_enriched.parquet"
        poly = DATA_PROCESSED / "anm_coproduct_polygons.parquet"
        src_p = enriched if enriched.exists() else poly
        return _clean_albers(gpd.read_parquet(src_p))

    @cached_property
    def biomes_alb(self) -> gpd.GeoDataFrame:
        return _clean_albers(gpd.read_parquet(DATA_PROCESSED / "socioenv_biomes.parquet"))

    @cached_property
    def ucs_alb(self) -> gpd.GeoDataFrame:
        return _clean_albers(gpd.read_parquet(DATA_PROCESSED / "socioenv_ucs.parquet"))

    @cached_property
    def tis_alb(self) -> gpd.GeoDataFrame:
        return _clean_albers(gpd.read_parquet(DATA_PROCESSED / "socioenv_tis.parquet"))

    @cached_property
    def mcs(self) -> pd.DataFrame:
        return pd.read_parquet(DATA_PROCESSED / "mcs2026_ree_global.parquet")

    @cached_property
    def comex(self) -> pd.DataFrame:
        return pd.read_parquet(DATA_PROCESSED / "comex_ree_magnets.parquet")

    @cached_property
    def uf_net(self) -> pd.DataFrame:
        return pd.read_parquet(DATA_PROCESSED / "uf_net_footprint.parquet")

    @cached_property
    def socio_sum(self) -> pd.DataFrame:
        return pd.read_parquet(DATA_PROCESSED / "socioenv_net_footprint_summary.parquet")

    @cached_property
    def anchors_alb(self) -> gpd.GeoDataFrame:
        with open(DATA_CURATED / "anchor_projects_and_midstream.json", "r", encoding="utf-8") as f:
            df_anc = pd.DataFrame(json.load(f)["projects"])
        return gpd.GeoDataFrame(
            df_anc,
            geometry=gpd.points_from_xy(df_anc["longitude"], df_anc["latitude"]),
            crs="EPSG:4326",
        ).to_crs(IBGE_ALBERS_CRS)


def _brazil_national_bounds(data: AtlasData, margin: float = 0.05) -> tuple[tuple[float, float], tuple[float, float]]:
    """Calcula limites dinamicos unindo Brasil continental + Amazonia Azul (sem cortes no AC ou ZEE)."""
    sb = data.states_alb.total_bounds
    eb = data.eez_alb.total_bounds
    xmin = min(sb[0], eb[0])
    ymin = min(sb[1], eb[1])
    xmax = min(max(sb[2], eb[2]), 7_550_000.0)  # evita compressao por ilhas oceanicas remotas
    ymax = max(sb[3], eb[3])
    dx = xmax - xmin
    dy = ymax - ymin
    return (xmin - dx * margin, xmax + dx * margin), (ymin - dy * margin, ymax + dy * margin)


def _plot_national_basemap(ax, data: AtlasData, show_biomes: bool = False) -> None:
    """Desenha basemap sul-americano, UFs IBGE 2025 e Amazonia Azul."""
    xlim, ylim = _brazil_national_bounds(data)
    data.sa_alb.plot(ax=ax, color="#F1F5F9", edgecolor="#CBD5E1", linewidth=0.5, zorder=1)
    if show_biomes:
        for bname, bcolor in BIOME_COLORS.items():
            sub_b = data.biomes_alb[data.biomes_alb["Bioma"] == bname]
            if not sub_b.empty:
                sub_b.plot(ax=ax, color=bcolor, edgecolor="none", alpha=0.80, zorder=2)
        data.states_alb.plot(ax=ax, color="none", edgecolor="#64748B", linewidth=0.6, zorder=3)
    else:
        data.states_alb.plot(ax=ax, color="#FAFAFA", edgecolor="#94A3B8", linewidth=0.6, zorder=2)

    data.eez_alb.plot(
        ax=ax, color="#E0F2FE", edgecolor="#0284C7",
        linewidth=0.85, linestyle="--", alpha=0.45, zorder=2,
    )
    ax.set_xlim(*xlim)
    ax.set_ylim(*ylim)
    ax.grid(True, linestyle=":", linewidth=0.4, color="#CBD5E1", alpha=0.65, zorder=0)


def _plot_regional_basemap(
    ax,
    data: AtlasData,
    focus_ufs: list[str],
    margin: float = 0.08,
    show_socioenv: bool = True,
    candidates_km: list[int] | None = None,
    show_crs_box: bool = True,
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Desenha recorte estadual/regional focado com contexto de estados vizinhos e UCs/TIs."""
    focus_gdf = data.states_alb[data.states_alb["uf"].isin(focus_ufs)]
    b = focus_gdf.total_bounds
    dx = b[2] - b[0]
    dy = b[3] - b[1]
    xlim = (b[0] - dx * margin, b[2] + dx * margin)
    ylim = (b[1] - dy * margin, b[3] + dy * margin)

    data.sa_alb.cx[xlim[0]:xlim[1], ylim[0]:ylim[1]].plot(ax=ax, color="#F1F5F9", edgecolor="#CBD5E1", linewidth=0.5, zorder=1)
    data.states_alb.cx[xlim[0]:xlim[1], ylim[0]:ylim[1]].plot(ax=ax, color="#F8FAFC", edgecolor="#CBD5E1", linewidth=0.55, zorder=2)
    focus_gdf.plot(ax=ax, color="#FFFFFF", edgecolor="#334155", linewidth=1.05, zorder=3)

    if show_socioenv:
        ucs_pi = data.ucs_alb[data.ucs_alb["group"] == "PI"].cx[xlim[0]:xlim[1], ylim[0]:ylim[1]]
        ucs_us = data.ucs_alb[data.ucs_alb["group"] == "US"].cx[xlim[0]:xlim[1], ylim[0]:ylim[1]]
        tis_sub = data.tis_alb.cx[xlim[0]:xlim[1], ylim[0]:ylim[1]]
        if not ucs_us.empty:
            ucs_us.plot(ax=ax, color="#DCFCE7", edgecolor="#16A34A", linewidth=0.45, alpha=0.55, zorder=4)
        if not ucs_pi.empty:
            ucs_pi.plot(ax=ax, color="#86EFAC", edgecolor="#15803D", linewidth=0.6, alpha=0.65, zorder=5)
        if not tis_sub.empty:
            tis_sub.plot(ax=ax, color="#FEF08A", edgecolor="#CA8A04", linewidth=0.7, alpha=0.65, zorder=5)

    # Contorno da UF focal por cima de UCs/TIs para nitidez cartografica
    focus_gdf.boundary.plot(ax=ax, color="#1E293B", linewidth=1.1, zorder=8)

    # Rótulos discretos das UFs no fundo
    for _, st in data.states_alb.cx[xlim[0]:xlim[1], ylim[0]:ylim[1]].iterrows():
        c = st.geometry.representative_point()
        if xlim[0] < c.x < xlim[1] and ylim[0] < c.y < ylim[1]:
            is_focus = st["uf"] in focus_ufs
            ax.text(
                c.x, c.y, st["uf"],
                fontsize=8.5 if is_focus else 7.0,
                fontweight="bold" if is_focus else "normal",
                color="#94A3B8" if not is_focus else "#475569",
                alpha=0.55, ha="center", va="center", zorder=4,
            )

    ax.set_xlim(*xlim)
    ax.set_ylim(*ylim)
    ax.grid(True, linestyle=":", linewidth=0.4, color="#CBD5E1", alpha=0.65, zorder=0)
    format_map_axes_and_scalebar(
        ax, IBGE_ALBERS_CRS, scale_pos="bottom_right",
        candidates_km=candidates_km, show_crs_box=show_crs_box,
    )
    return xlim, ylim


def _plot_anm_by_stage(ax, gdf: gpd.GeoDataFrame, base_z: int = 8) -> None:
    """Plota poligonos ANM com Z-order crescente por maturidade regulatoria."""
    order_bottom_to_top = list(reversed(STAGE_ORDER))
    for i, stg in enumerate(order_bottom_to_top):
        sub = gdf[gdf["stage_tier"] == stg]
        if sub.empty:
            continue
        is_mature = stg in {"Concessão de Lavra", "Requerimento de Lavra", "Direito de Requerer a Lavra"}
        lw = 1.15 if stg == "Concessão de Lavra" else (0.75 if is_mature else 0.35)
        alpha = 0.92 if is_mature else 0.72
        sub.plot(
            ax=ax,
            color=STAGE_COLORS[stg],
            edgecolor="#0F172A" if is_mature else STAGE_COLORS[stg],
            linewidth=lw,
            alpha=alpha,
            zorder=base_z + i,
        )


def _callout(ax, x: float, y: float, dx: float, dy: float, text: str, color: str = "#0F172A", fontsize: float = 7.2, shrink_b: float = 2.0) -> None:
    """Anotacao cartografica com caixa branca sem colisao."""
    ax.annotate(
        text,
        xy=(x, y),
        xytext=(x + dx, y + dy),
        fontsize=fontsize,
        fontweight="bold",
        color=color,
        ha="left" if dx >= 0 else "right",
        va="center",
        bbox=dict(boxstyle="round,pad=0.22", facecolor="white", edgecolor=color, linewidth=0.65, alpha=0.92),
        arrowprops=dict(arrowstyle="-|>", color=color, lw=0.75, shrinkA=0, shrinkB=shrink_b),
        zorder=24,
    )


def _highlight_concession_polo(ax, data: AtlasData, ring_size: float = 175.0) -> tuple[float, float] | None:
    """Desenha anel vazado sobre as Concessões de Lavra para destacar sem ocluir os polígonos."""
    conc = data.anm_alb[data.anm_alb["stage_tier"] == "Concessão de Lavra"]
    if conc.empty:
        return None
    c = conc.geometry.union_all().centroid
    ax.scatter(
        [c.x], [c.y],
        s=ring_size,
        facecolors="none",
        edgecolors="#991B1B",
        linewidths=1.7,
        zorder=23,
    )
    return float(c.x), float(c.y)


# ==============================================================================
# FIGURA 01a — Mapa Nacional Cadastro ANM (SEM Coprodutos)
# ==============================================================================
def build_fig_01a(data: AtlasData) -> None:
    fig, ax = plt.subplots(figsize=(11.5, 11.4))
    _plot_national_basemap(ax, data, show_biomes=False)
    _plot_anm_by_stage(ax, data.anm_alb, base_z=8)

    anm_label = get_anm_snapshot_label()
    n_anm_s = fmt_int_ptbr(len(data.anm_alb))
    gross_ha_s = fmt_int_ptbr(data.anm_alb["area_ha"].sum())
    conc_cnt = int((data.anm_alb["stage_tier"] == "Concessão de Lavra").sum())

    # Destacar centroides dos projetos ancora e LabFabITR (PRJ-01 em anel vazado para nao ocluir concessoes)
    anc_strict = data.anchors_alb[data.anchors_alb["id"].isin(["PRJ-01", "PRJ-02", "PRJ-05", "PRJ-06", "PRJ-07", "PRJ-09", "MID-01"])]
    sv_center = _highlight_concession_polo(ax, data, ring_size=190.0)
    for _, row in anc_strict.iterrows():
        if row["id"] == "PRJ-01" and sv_center is not None:
            continue
        is_mid = row["id"] == "MID-01"
        ax.scatter(
            row.geometry.x, row.geometry.y,
            s=85 if is_mid else 52,
            marker="D" if is_mid else "*",
            c="#0F172A" if is_mid else "#FEF08A",
            edgecolors="#FFFFFF" if is_mid else "#0F172A",
            linewidths=0.8,
            zorder=22,
        )

    # Callouts desobstruidos (folga vertical sobre a legenda elevada)
    offs = {
        "PRJ-07": (-550_000, 320_000, "Ema / BCM (AM)\n1,07 Bt @ 732 ppm"),
        "PRJ-01": (-750_000, 260_000, f"Serra Verde / Pela Ema (GO)\n{conc_cnt} Concessões em Lavra"),
        "PRJ-02": (60_000, 470_000, "Carina / Aclara (GO)\nFS 170,8 Mt @ 1.745 ppm"),
        "PRJ-05": (460_000, 40_000, "Monte Alto / BRE (BA)\n4,4 Mt @ 9,87% TREO"),
        "PRJ-06": (420_000, -140_000, "Caladão / Axel (MG)\n708,6 Mt @ 1.443 ppm"),
        "PRJ-09": (-780_000, -180_000, "Araxá / St George (MG)\n111,2 Mt @ 3,57% TREO"),
        "MID-01": (440_000, -230_000, "LabFabITR (Lagoa Santa-MG)\nÍmãs NdFeB (100 t/ano proj.)"),
    }
    for _, row in anc_strict.iterrows():
        if row["id"] in offs:
            dx, dy, lbl = offs[row["id"]]
            if row["id"] == "PRJ-01" and sv_center is not None:
                _callout(ax, sv_center[0], sv_center[1], dx, dy, lbl, color="#7F1D1D", fontsize=7.0, shrink_b=8.5)
            else:
                _callout(ax, row.geometry.x, row.geometry.y, dx, dy, lbl, color="#0F172A", fontsize=7.0)

    format_map_axes_and_scalebar(ax, IBGE_ALBERS_CRS, scale_pos="bottom_right", candidates_km=[250, 500, 1000], show_crs_box=True)

    # Legenda no Atlantico Sul (deslocada acima da barra de escala e do quadro Sistema de Referencia)
    handles = []
    for stg in STAGE_ORDER:
        cnt = int((data.anm_alb["stage_tier"] == stg).sum())
        handles.append(mpatches.Patch(color=STAGE_COLORS[stg], label=f"{stg} ({fmt_int_ptbr(cnt)})"))
    handles.append(mlines.Line2D([], [], color="#0F172A", marker="*", linestyle="None", markersize=9, markerfacecolor="#FEF08A", label="Projeto Âncora (Cadastro ANM)"))
    handles.append(mlines.Line2D([], [], color="none", marker="o", linestyle="None", markersize=8.5, markerfacecolor="none", markeredgecolor="#991B1B", markeredgewidth=1.6, label="Polo em Lavra (Minaçu-GO; ver zoom)"))
    handles.append(mlines.Line2D([], [], color="#FFFFFF", marker="D", linestyle="None", markersize=7, markerfacecolor="#0F172A", label="Ativo Midstream (LabFabITR)"))
    handles.append(mpatches.Patch(facecolor="#E0F2FE", edgecolor="#0284C7", linestyle="--", label="Amazônia Azul (ZEE)"))

    leg = ax.legend(
        handles=handles,
        title=f"Estágio Regulatório ANM (Total: {n_anm_s})",
        title_fontsize=8.2,
        fontsize=7.3,
        loc="lower right",
        bbox_to_anchor=(0.99, 0.145),
        frameon=True,
        facecolor="white",
        edgecolor="#94A3B8",
        framealpha=0.97,
    )
    leg.set_zorder(30)
    ax.set_title("Cadastro Minerário de Terras Raras no Brasil por Estágio Regulatório", fontsize=13.5, fontweight="bold", pad=12, color="#0F172A")
    add_cartographic_footer(
        ax,
        "TERRAS RARAS NO BRASIL — CADASTRO ANM",
        f"Distribuição espacial dos {n_anm_s} processos ativos em TERRAS RARAS, MONAZITA e MINÉRIO DE CÉRIO (área bruta: {gross_ha_s} ha; sem coprodutos).",
        f"ANM / SIGMINE (BRASIL.shp, {anm_label}); IBGE UF 2025; Marine Regions (ZEE Amazônia Azul); JORC / NI 43-101.",
        "",
        "Exclui títulos sob outras substâncias primárias (Poços de Caldas Al, Pitinga Sn, Araxá/Catalão Nb, Areias Pesadas Ti); ver variação com coprodutos.",
    )
    save_figure_dual(fig, "fig_01a_mapa_nacional_etr_estrito", save_svg=data.save_svg)


# ==============================================================================
# FIGURA 01b — Mapa Nacional COM Coprodutos e Tipologia Geológica
# ==============================================================================
def build_fig_01b(data: AtlasData) -> None:
    fig, ax = plt.subplots(figsize=(11.5, 11.4))
    _plot_national_basemap(ax, data, show_biomes=False)

    anm_label = get_anm_snapshot_label()
    n_anm_s = fmt_int_ptbr(len(data.anm_alb))
    n_cop_s = fmt_int_ptbr(len(data.cop_alb))
    cop_ha_s = fmt_int_ptbr(data.cop_alb["area_ha"].sum())
    pit_kha_s = fmt_ptbr(
        data.cop_alb[data.cop_alb["coproduct_chain"].str.contains("Estanho", na=False)]["area_ha"].sum() / 1000.0, 1
    )

    # Cadastro ANM em camada base discreta
    data.anm_alb.plot(ax=ax, color="#93C5FD", edgecolor="#2563EB", linewidth=0.28, alpha=0.58, zorder=7)

    # Coprodutos destacados por cadeia
    for chain, color in COPRODUCT_CHAIN_COLORS.items():
        sub = data.cop_alb[data.cop_alb["coproduct_chain"] == chain]
        if not sub.empty:
            sub.plot(ax=ax, color=color, edgecolor="#0F172A", linewidth=0.6, alpha=0.88, zorder=12)

    # Projetos ancora por tipologia geologica
    marker_map = {
        "Argila Iônica": "o",
        "Carbonatito / Alcalino": "s",
        "Granito / Xenotima": "^",
        "Areia Pesada / Monazita": "v",
        "Midstream / Ímãs": "D",
    }
    for gtype, gcol in GEOLOGY_COLORS.items():
        sub_a = data.anchors_alb[data.anchors_alb["geology_type"] == gtype]
        if not sub_a.empty:
            ax.scatter(
                sub_a.geometry.x, sub_a.geometry.y,
                c=gcol, marker=marker_map[gtype],
                s=65, edgecolors="white", linewidths=0.85, zorder=22,
            )

    offs = {
        "PRJ-12": (-160_000, 340_000, "Seis Lagos (AM)\n2,89 Bt laterito Nb-ETR (SGB)"),
        "PRJ-13": (420_000, 140_000, f"Pitinga / Taboca (AM)\nSn-Nb-Ta + Xenotima ({pit_kha_s} mil ha)"),
        "PRJ-01": (-740_000, 220_000, "Serra Verde (GO)"),
        "PRJ-11": (320_000, -40_000, "Catalão / CMOC (GO)\nCarbonatito Nb/P/ETR"),
        "PRJ-03": (-750_000, -80_000, "Poços de Caldas (MG/SP)\nCaldeira (Meteoric) & Colossus (Viridis)"),
        "PRJ-09": (-780_000, 120_000, "Araxá (MG)\nCBMM & St George (Nb/ETR)"),
        "PRJ-14": (340_000, 110_000, "Bahia Project (BA)\nEnergy Fuels Monazita"),
        "PRJ-15": (260_000, 150_000, "Buena / INB (RJ)\nAreia Monazítica"),
        "PRJ-16": (-520_000, 90_000, "Retiro / RGM (RS)\nIlmenita/Monazita"),
    }
    for _, row in data.anchors_alb.iterrows():
        if row["id"] in offs:
            dx, dy, lbl = offs[row["id"]]
            _callout(ax, row.geometry.x, row.geometry.y, dx, dy, lbl, color="#0F172A", fontsize=6.9)

    format_map_axes_and_scalebar(ax, IBGE_ALBERS_CRS, scale_pos="bottom_right", candidates_km=[250, 500, 1000], show_crs_box=True)

    handles = [
        mpatches.Patch(facecolor="#93C5FD", edgecolor="#2563EB", label=f"Cadastro ANM ETR ({n_anm_s})"),
    ]
    for ch, col in COPRODUCT_CHAIN_COLORS.items():
        cnt = int((data.cop_alb["coproduct_chain"] == ch).sum())
        handles.append(mpatches.Patch(facecolor=col, edgecolor="#0F172A", label=f"Coproduto: {ch} ({cnt})"))
    for gtype, gcol in GEOLOGY_COLORS.items():
        handles.append(mlines.Line2D([], [], color="white", marker=marker_map[gtype], markersize=7.5, markerfacecolor=gcol, label=f"Âncora: {gtype}"))

    leg = ax.legend(
        handles=handles,
        title="Escopo (ANM + Coprodutos + Tipologia)",
        title_fontsize=7.3,
        fontsize=6.2,
        labelspacing=0.28,
        borderpad=0.35,
        handletextpad=0.5,
        loc="lower right",
        bbox_to_anchor=(0.99, 0.125),
        frameon=True, facecolor="white", edgecolor="#94A3B8", framealpha=0.97,
    )
    leg.set_zorder(30)
    ax.set_title("Terras Raras no Brasil: Cadastro ANM, Coprodutos Associados e Tipologia Geológica", fontsize=13.0, fontweight="bold", pad=12, color="#0F172A")
    add_cartographic_footer(
        ax,
        "TERRAS RARAS NO BRASIL — CADASTRO ANM + COPRODUTOS",
        f"Integração dos {n_anm_s} títulos de ETR com {n_cop_s} títulos de coprodutos/legados ({cop_ha_s} ha brutos; {pit_kha_s} mil ha em Pitinga/AM).",
        f"ANM / SIGMINE ({anm_label}); SGB-CPRM; CETEM; Relatórios JORC/NI 43-101; IBGE UF 2025; Marine Regions.",
        "",
        "Diferencia ontologicamente ativos declarados como ETR na ANM daqueles titulados sob substâncias hospedeiras primárias (Al, Sn, Nb, P, Ti).",
    )
    save_figure_dual(fig, "fig_01b_mapa_nacional_etr_com_coprodutos", save_svg=data.save_svg)


# ==============================================================================
# FIGURA 02 — Mapa Socioambiental (Biomas, UCs, TIs + Inset Seis Lagos)
# ==============================================================================
def build_fig_02(data: AtlasData) -> None:
    fig, ax = plt.subplots(figsize=(12.2, 11.4))
    _plot_national_basemap(ax, data, show_biomes=True)

    anm_label = get_anm_snapshot_label()
    n_anm_s = fmt_int_ptbr(len(data.anm_alb))
    n_cop_s = fmt_int_ptbr(len(data.cop_alb))

    ucs_pi = data.ucs_alb[data.ucs_alb["group"] == "PI"]
    ucs_us = data.ucs_alb[data.ucs_alb["group"] == "US"]
    ucs_us.plot(ax=ax, color="#86EFAC", edgecolor="#16A34A", linewidth=0.35, alpha=0.55, zorder=5)
    ucs_pi.plot(ax=ax, color="#15803D", edgecolor="#064E3B", linewidth=0.45, alpha=0.70, zorder=6)
    data.tis_alb.plot(ax=ax, color="#FACC15", edgecolor="#A16207", linewidth=0.45, alpha=0.68, zorder=7)

    # Processos do Cadastro ANM e coprodutos em destaque vermelho/purpura
    data.anm_alb.plot(ax=ax, color="#DC2626", edgecolor="#7F1D1D", linewidth=0.35, alpha=0.82, zorder=12)
    data.cop_alb.plot(ax=ax, color="#7C3AED", edgecolor="#3B0764", linewidth=0.55, alpha=0.85, zorder=13)

    format_map_axes_and_scalebar(ax, IBGE_ALBERS_CRS, scale_pos="bottom_right", candidates_km=[250, 500, 1000], show_crs_box=True)

    # Inset Zoom: Morro dos Seis Lagos / Cabeça do Cachorro (AM) no espaco oceanico/andino livre a sudoeste
    ax_ins = inset_axes(ax, width="31%", height="29%", loc="lower left", bbox_to_anchor=(0.02, 0.06, 1, 1), bbox_transform=ax.transAxes)
    sl_row = data.cop_alb[data.cop_alb["processo"] == "803778/1975"]
    if not sl_row.empty:
        sb = sl_row.total_bounds
        pad = 95_000.0
        ix0, iy0, ix1, iy1 = sb[0] - pad, sb[1] - pad, sb[2] + pad, sb[3] + pad
        data.sa_alb.plot(ax=ax_ins, color="#F1F5F9", edgecolor="#94A3B8", linewidth=0.5, zorder=1)
        data.biomes_alb.cx[ix0:ix1, iy0:iy1].plot(ax=ax_ins, color="#D1FAE5", edgecolor="none", zorder=2)
        ucs_pi.cx[ix0:ix1, iy0:iy1].plot(ax=ax_ins, color="#15803D", edgecolor="#064E3B", linewidth=0.6, alpha=0.65, zorder=4)
        data.tis_alb.cx[ix0:ix1, iy0:iy1].plot(ax=ax_ins, color="#FACC15", edgecolor="#A16207", linewidth=0.6, alpha=0.55, zorder=3)
        sl_row.plot(ax=ax_ins, color="#7C3AED", edgecolor="#FFFFFF", linewidth=1.2, alpha=0.95, zorder=8)
        ax_ins.set_xlim(ix0, ix1)
        ax_ins.set_ylim(iy0, iy1)
        ax_ins.set_xticks([])
        ax_ins.set_yticks([])
        ax_ins.set_xlabel("")
        ax_ins.set_ylabel("")
        ax_ins.set_title("Zoom: Seis Lagos (AM)\nPARNA Pico da Neblina × TI Yanomami", fontsize=7.4, fontweight="bold", pad=6, color="#0F172A")
        for sp in ax_ins.spines.values():
            sp.set_edgecolor("#7C3AED")
            sp.set_linewidth(1.3)
        sl_pt = sl_row.geometry.iloc[0].centroid
        ax.annotate(
            "", xy=(sl_pt.x, sl_pt.y), xytext=(0.12, 0.35),
            xycoords="data", textcoords=ax.transAxes,
            arrowprops=dict(arrowstyle="-|>", color="#7C3AED", lw=1.1, ls="--"),
            zorder=24,
        )

    handles = [mpatches.Patch(color=BIOME_COLORS[b], label=f"Bioma {b}") for b in BIOME_COLORS]
    handles.extend([
        mpatches.Patch(color="#15803D", label="UC Proteção Integral (CNUC)"),
        mpatches.Patch(color="#86EFAC", label="UC Uso Sustentável (CNUC)"),
        mpatches.Patch(color="#FACC15", label="Terra Indígena (FUNAI)"),
        mpatches.Patch(color="#DC2626", label=f"Cadastro ANM ETR ({n_anm_s})"),
        mpatches.Patch(color="#7C3AED", label=f"ANM Coproduto Associado ({n_cop_s})"),
    ])
    leg = ax.legend(
        handles=handles,
        title="Malhas Socioambientais e Títulos ANM",
        title_fontsize=7.8, fontsize=6.8,
        loc="lower right", bbox_to_anchor=(0.99, 0.145),
        frameon=True, facecolor="white", edgecolor="#94A3B8", framealpha=0.97,
    )
    leg.set_zorder(30)
    ax.set_title("Terras Raras no Brasil: Sobreposição com Biomas, Unidades de Conservação e Terras Indígenas", fontsize=12.6, fontweight="bold", pad=12, color="#0F172A")
    add_cartographic_footer(
        ax,
        "TERRAS RARAS NO BRASIL — INTERFACE SOCIOAMBIENTAL",
        "Cruzamento espacial dos polígonos ANM e coprodutos com Biomas IBGE, UCs e TIs (ver Tabela T08 para área bruta vs. pegada líquida dissolvida).",
        f"ANM/SIGMINE ({anm_label}); IBGE Biomas 1:250k (2019); CNUC/MMA via IPEA geobr (set/2019); FUNAI via geobr (mar/2021).",
        "",
        "Títulos ANM em pesquisa ou requerimento não implicam autorização de supressão vegetal nem licença ambiental.",
    )
    save_figure_dual(fig, "fig_02_mapa_socioambiental_biomas_ucs_tis")


# ==============================================================================
# FIGURA 03 — Multi-Zoom dos 3 Grandes Polos (BA, GO–TO, MG)
# ==============================================================================
def build_fig_03(data: AtlasData) -> None:
    fig, axes = plt.subplots(1, 3, figsize=(17.5, 9.4))
    fig.subplots_adjust(top=0.80, bottom=0.14, wspace=0.22)

    anm_label = get_anm_snapshot_label()
    cnt_ba = int((data.anm_alb["uf"] == "BA").sum())
    cnt_goto = int(data.anm_alb["uf"].isin(["GO", "TO"]).sum())
    cnt_mg = int((data.anm_alb["uf"] == "MG").sum())
    conc_goto = int((data.anm_alb[data.anm_alb["uf"].isin(["GO", "TO"])]["stage_tier"] == "Concessão de Lavra").sum())
    pct_polos = (cnt_ba + cnt_goto + cnt_mg) / len(data.anm_alb) * 100.0

    panels = [
        (axes[0], ["BA"], f"Polo Bahia ({fmt_int_ptbr(cnt_ba)} proc. + Bahia HMS)", [50, 100, 200], [("PRJ-05", 55_000, 45_000, "Monte Alto / BRE"), ("PRJ-14", -90_000, 35_000, "Bahia HMS / Energy Fuels")]),
        (axes[1], ["GO", "TO"], f"Arco Goiás–Tocantins ({fmt_int_ptbr(cnt_goto)} proc. + Catalão)", [100, 200, 300], [("PRJ-01", -160_000, 95_000, f"Serra Verde ({conc_goto} Concessões)"), ("PRJ-02", 80_000, 50_000, "Carina / Aclara"), ("PRJ-08", 130_000, -25_000, "PCH / Ultra"), ("PRJ-11", 70_000, -45_000, "Catalão / CMOC")]),
        (axes[2], ["MG"], f"Polo Minas Gerais ({fmt_int_ptbr(cnt_mg)} proc. + Araxá/Caldas)", [100, 200, 300], [("PRJ-03", -160_000, -70_000, "Caldeira / Meteoric"), ("PRJ-04", 75_000, 40_000, "Colossus / Viridis"), ("PRJ-09", -120_000, 120_000, "Araxá (CBMM & St George)"), ("PRJ-06", 70_000, 60_000, "Caladão / Axel"), ("MID-01", 80_000, -75_000, "LabFabITR")]),
    ]
    for ax, ufs, title, c_km, annots in panels:
        xlim, ylim = _plot_regional_basemap(
            ax, data, ufs, margin=0.09, show_socioenv=True,
            candidates_km=c_km, show_crs_box=(ufs == ["MG"]),
        )
        _plot_anm_by_stage(ax, data.anm_alb.cx[xlim[0]:xlim[1], ylim[0]:ylim[1]], base_z=10)
        sub_cop = data.cop_alb.cx[xlim[0]:xlim[1], ylim[0]:ylim[1]]
        if not sub_cop.empty:
            sub_cop.plot(ax=ax, color="#7C3AED", edgecolor="#3B0764", linewidth=0.45, alpha=0.68, zorder=9)
        sv_ring = None
        for pid, dx, dy, lbl in annots:
            if pid == "PRJ-01":
                sv_ring = _highlight_concession_polo(ax, data, ring_size=165.0)
                if sv_ring is not None:
                    _callout(ax, sv_ring[0], sv_ring[1], dx, dy, lbl, color="#7F1D1D", fontsize=6.8, shrink_b=8.0)
                    continue
            row = data.anchors_alb[data.anchors_alb["id"] == pid]
            if not row.empty:
                r = row.iloc[0]
                ax.scatter(r.geometry.x, r.geometry.y, s=55, c="#FEF08A", edgecolors="#0F172A", marker="*", zorder=22)
                _callout(ax, r.geometry.x, r.geometry.y, dx, dy, lbl, fontsize=6.8)
        ax.set_title(title, fontsize=10.5, fontweight="bold", pad=10, color="#0F172A")

    handles = [mpatches.Patch(color=STAGE_COLORS[s], label=s) for s in STAGE_ORDER]
    handles.append(mpatches.Patch(color="#7C3AED", label="Coprodutos Associados"))
    handles.append(mpatches.Patch(color="#86EFAC", label="UC Proteção Integral"))
    handles.append(mpatches.Patch(color="#FEF08A", label="Terra Indígena"))
    handles.append(mlines.Line2D([], [], color="none", marker="o", linestyle="None", markersize=8.0, markerfacecolor="none", markeredgecolor="#991B1B", markeredgewidth=1.6, label="Polo em Lavra (Minaçu-GO)"))
    fig.legend(
        handles=handles, loc="upper center", bbox_to_anchor=(0.5, 0.915),
        ncol=4, fontsize=7.6, frameon=True, facecolor="#F8FAFC", edgecolor="#CBD5E1",
    )
    fig.suptitle("Polos Regionais de Terras Raras no Brasil: Polígonos ANM, Coprodutos e Projetos Âncora", fontsize=13.8, fontweight="bold", y=0.975, color="#0F172A")
    add_figure_footer(
        fig,
        "TERRAS RARAS NO BRASIL — POLOS BA, GO-TO E MG",
        f"Detalhamento em escala sub-regional dos três principais polos detentores de {fmt_ptbr(pct_polos, 1)}% dos processos do Cadastro ANM.",
        f"ANM / SIGMINE ({anm_label}); IBGE UF 2025; CNUC/MMA; FUNAI; JORC/NI 43-101.",
        "",
        "Polígonos roxos destacam complexos de coprodutos (Bauxita/Al em Poços de Caldas, Nb/P em Araxá/Catalão e Ilmenita na BA).",
    )
    save_figure_dual(fig, "fig_03_mapa_multi_zoom_polos_ba_go_mg")


# ==============================================================================
# FIGURA 04 — Mapa Geopolítica do Capital Controlador
# ==============================================================================
def build_fig_04(data: AtlasData) -> None:
    fig, ax = plt.subplots(figsize=(11.5, 11.4))
    _plot_national_basemap(ax, data, show_biomes=False)
    anm_label = get_anm_snapshot_label()

    plot_order = [
        "Titular Local / SPE Não Classificada",
        "Brasil — Privado",
        "Brasil — Estatal",
        "Outro Estrangeiro Verificado",
        "Canadá / Chile",
        "Austrália",
        "EUA / Reino Unido",
        "China",
    ]
    combined = pd.concat([data.anm_alb, data.cop_alb], ignore_index=True)
    for i, orig in enumerate(plot_order):
        sub = combined[combined["origin_country"] == orig]
        if not sub.empty:
            col_hex = CAPITAL_ORIGIN_COLORS.get(orig, "#94A3B8")
            sub.plot(
                ax=ax,
                color=col_hex,
                edgecolor=col_hex,
                linewidth=0.25 if orig != "Titular Local / SPE Não Classificada" else 0.15,
                alpha=0.88 if orig != "Titular Local / SPE Não Classificada" else 0.65,
                zorder=8 + i,
            )

    format_map_axes_and_scalebar(ax, IBGE_ALBERS_CRS, scale_pos="bottom_right", candidates_km=[250, 500, 1000], show_crs_box=True)

    handles = []
    legend_origins = [
        "Austrália", "EUA / Reino Unido", "Canadá / Chile", "China",
        "Outro Estrangeiro Verificado", "Brasil — Privado", "Brasil — Estatal", "Titular Local / SPE Não Classificada",
    ]
    for orig in legend_origins:
        sub_s = data.anm_alb[data.anm_alb["origin_country"] == orig]
        sub_c = data.cop_alb[data.cop_alb["origin_country"] == orig]
        tot_ha = float(sub_s["area_ha"].sum() + sub_c["area_ha"].sum()) / 1000.0
        if orig == "Outro Estrangeiro Verificado" and tot_ha < 0.05:
            continue
        handles.append(mpatches.Patch(color=CAPITAL_ORIGIN_COLORS[orig], label=f"{orig} ({fmt_ptbr(tot_ha, 1)} mil ha)"))

    leg = ax.legend(
        handles=handles,
        title="Origem do Capital Controlador (Cadastro ANM + Coprodutos)",
        title_fontsize=8.0, fontsize=7.1,
        loc="lower right", bbox_to_anchor=(0.99, 0.145),
        frameon=True, facecolor="white", edgecolor="#94A3B8", framealpha=0.97,
    )
    leg.set_zorder(30)
    ax.set_title("Controle Societário e Origem Geopolítica do Capital nos Títulos de Terras Raras e Coprodutos", fontsize=12.6, fontweight="bold", pad=12, color="#0F172A")
    add_cartographic_footer(
        ax,
        "TERRAS RARAS NO BRASIL — GEOPOLÍTICA DO CAPITAL",
        "Classificação dos titulares ANM (Top 45 + 100% dos maduros + coprodutos) por país/bloco controlador (inclui Serra Verde/USA Rare Earth e Taboca/CMOC).",
        f"ANM / SIGMINE ({anm_label}); Registros ASX, TSX, SEC, CNPJ e comunicados corporativos (auditoria nov/2026).",
        "",
        "SPEs sem controlador estrangeiro comprovado permanecem classificadas como 'Titular Local / SPE Não Classificada'.",
    )
    save_figure_dual(fig, "fig_04_mapa_geopolitica_capital_e_coprodutos")


# ==============================================================================
# FIGURAS 05 a 08 — Recortes Estaduais Dedicados (BA, GO–TO, MG, AM)
# ==============================================================================
def build_fig_05_to_08(data: AtlasData) -> None:
    anm_label = get_anm_snapshot_label()
    ba_df = data.anm_alb[data.anm_alb["uf"] == "BA"]
    ba_cop_n = int((data.cop_alb["uf"] == "BA").sum())
    goto_df = data.anm_alb[data.anm_alb["uf"].isin(["GO", "TO"])]
    conc_goto = int((goto_df["stage_tier"] == "Concessão de Lavra").sum())
    mg_df = data.anm_alb[data.anm_alb["uf"] == "MG"]
    mg_cop_n = int((data.cop_alb["uf"] == "MG").sum())
    am_df = data.anm_alb[data.anm_alb["uf"] == "AM"]
    am_cop_kha = float(data.cop_alb[data.cop_alb["uf"] == "AM"]["area_ha"].sum()) / 1000.0

    specs = [
        (
            "fig_05_recorte_polo_bahia", ["BA"], "Bahia: Cadastro ANM, Coprodutos de Areias Pesadas e Projetos Âncora",
            f"{fmt_int_ptbr(len(ba_df))} processos no Cadastro ANM ({fmt_ptbr(ba_df['area_ha'].sum()/1e6, 2)} Mha) + {ba_cop_n} títulos de Ilmenita/Titânio/Monazita (Energy Fuels).",
            [("PRJ-05", 130_000, 60_000, "Monte Alto / Rocha da Rocha\n(BRE — 4,4 Mt @ 9,87% TREO)"), ("PRJ-14", -250_000, 140_000, "Bahia HMS Project\n(Energy Fuels — Monazita)")],
            [50, 100, 200], "lower left", 0.08,
            "Concentração no cinturão granulítico/Rocha da Rocha (Jequié–Ubaíra) e litoral extremo sul; títulos em pesquisa não implicam autorização ambiental.",
        ),
        (
            "fig_06_recorte_polo_goias_tocantins", ["GO", "TO"], "Arco Goiás–Tocantins: Concessões em Lavra, Argilas Iônicas e Carbonatitos",
            f"{fmt_int_ptbr(len(goto_df))} processos no Cadastro ANM ({fmt_ptbr(goto_df['area_ha'].sum()/1e6, 2)} Mha) com as únicas {conc_goto} Concessões de Lavra ativas (Serra Verde/GO) + Catalão (CMOC).",
            [("PRJ-01", -240_000, 95_000, f"Serra Verde / Pela Ema\n({conc_goto} Concessões de Lavra)"), ("PRJ-02", 120_000, 65_000, "Carina / Aclara\n(FS 170,8 Mt @ 1.745 ppm)"), ("PRJ-08", 135_000, -55_000, "PCH / Ultra-Appia\n(Iporá — 52,8 Mt)"), ("PRJ-11", 110_000, -70_000, "Catalão I & II / CMOC\n(Coproduto Nb/Fosfato)")],
            [100, 200, 300], "upper left", 0.13,
            "Minaçu (GO) concentra 100% das Concessões de Lavra ativas no Cadastro ANM; Catalão/Ouvidor aparece na camada de coprodutos Nb/P.",
        ),
        (
            "fig_07_recorte_polo_minas_gerais", ["MG"], "Minas Gerais: Poços de Caldas, Complexo Barreiro (Araxá), Jequitinhonha e LabFabITR",
            f"{fmt_int_ptbr(len(mg_df))} processos no Cadastro ANM ({fmt_int_ptbr(mg_df['area_ha'].sum()/1000.0)} mil ha) + {mg_cop_n} títulos coprodutos (Al/Bauxita Viridis & Meteoric; Nb/P CBMM, St George, Mosaic).",
            [("PRJ-03", -230_000, -110_000, "Caldeira / Meteoric\n(1,63 Bt @ 2.317 ppm)"), ("PRJ-04", 120_000, -75_000, "Colossus / Viridis\n(493 Mt @ 2.508 ppm)"), ("PRJ-09", -240_000, 110_000, "Araxá / St George & CBMM\n(Carbonatito Nb + ETR)"), ("PRJ-06", 90_000, 80_000, "Caladão / Axel REE\n(708,6 Mt @ 1.443 ppm)"), ("MID-01", 110_000, 55_000, "LabFabITR / CIT SENAI\n(Lagoa Santa — Ímãs)")],
            [100, 200, 300], "lower right", 0.08,
            "Em Poços de Caldas, parte relevante dos ativos da Viridis e da Meteoric permanece titulada sob Bauxita/Alumínio (camada de coprodutos).",
        ),
        (
            "fig_08_recorte_polo_amazonas", ["AM"], "Amazonas: Fronteira de Argilas Iônicas (Apuí) vs. Gigantes Polimetálicos (Pitinga e Seis Lagos)",
            f"Contraste entre {fmt_int_ptbr(len(am_df))} processos no Cadastro ANM no AM ({fmt_ptbr(am_df['area_ha'].sum()/1000.0, 1)} mil ha) e {fmt_ptbr(am_cop_kha, 1)} mil ha em Pitinga (Taboca/CNMC) e Seis Lagos (SGB-CPRM).",
            [("PRJ-07", -360_000, 140_000, "Ema / BCM (Apuí)\n1,07 Bt @ 732 ppm TREO"), ("PRJ-13", -180_000, -120_000, "Pitinga / Taboca-CNMC\n194,9 mil ha (Sn + Xenotima)"), ("PRJ-12", 180_000, -120_000, "Morro dos Seis Lagos / SGB\n2,89 Bt (PARNA + TI Yanomami)")],
            [150, 300, 500], "lower left", 0.11,
            "Em Seis Lagos (AM), há sobreposição integral com o PARNA Pico da Neblina e a TI Yanomami; Pitinga é lavrada sob cassiterita/estanho.",
        ),
    ]

    for stem, ufs, title, sub_txt, annots, c_km, leg_loc, margin_v, caveat_txt in specs:
        fig, ax = plt.subplots(figsize=(11.2, 10.6))
        xlim, ylim = _plot_regional_basemap(ax, data, ufs, margin=margin_v, show_socioenv=True, candidates_km=c_km, show_crs_box=True)
        _plot_anm_by_stage(ax, data.anm_alb.cx[xlim[0]:xlim[1], ylim[0]:ylim[1]], base_z=10)
        for ch, col in COPRODUCT_CHAIN_COLORS.items():
            sub_c = data.cop_alb[data.cop_alb["coproduct_chain"] == ch].cx[xlim[0]:xlim[1], ylim[0]:ylim[1]]
            if not sub_c.empty:
                sub_c.plot(ax=ax, color=col, edgecolor="#0F172A", linewidth=0.55, alpha=0.82, zorder=9)

        for pid, dx, dy, lbl in annots:
            if pid == "PRJ-01":
                sv_ring = _highlight_concession_polo(ax, data, ring_size=190.0)
                if sv_ring is not None:
                    _callout(ax, sv_ring[0], sv_ring[1], dx, dy, lbl, color="#7F1D1D", fontsize=7.0, shrink_b=8.5)
                    continue
            r = data.anchors_alb[data.anchors_alb["id"] == pid]
            if not r.empty:
                pt = r.iloc[0]
                ax.scatter(pt.geometry.x, pt.geometry.y, s=80, c="#FEF08A", edgecolors="#0F172A", marker="*", zorder=22)
                _callout(ax, pt.geometry.x, pt.geometry.y, dx, dy, lbl, fontsize=7.0)

        handles = [mpatches.Patch(color=STAGE_COLORS[s], label=s) for s in STAGE_ORDER]
        handles.append(mpatches.Patch(color="#7C3AED", label="Coproduto: Nb/P Carbonatito"))
        if "GO" in ufs:
            handles.append(mlines.Line2D([], [], color="none", marker="o", linestyle="None", markersize=8.0, markerfacecolor="none", markeredgecolor="#991B1B", markeredgewidth=1.6, label="Polo em Lavra (ver fig_06b)"))
        if "MG" in ufs:
            handles.append(mpatches.Patch(color=COPRODUCT_CHAIN_COLORS["Bauxita / Argila (Poços de Caldas)"], label="Coproduto: Bauxita/Al Poços de Caldas"))
        if "AM" in ufs:
            handles.append(mpatches.Patch(color=COPRODUCT_CHAIN_COLORS["Estanho / Cassiterita–Xenotima (Pitinga)"], label="Coproduto: Sn/Xenotima Pitinga"))
        if "BA" in ufs:
            handles.append(mpatches.Patch(color=COPRODUCT_CHAIN_COLORS["Titânio / Areias Pesadas Monazíticas"], label="Coproduto: Ti/Monazita"))
        handles.extend([
            mpatches.Patch(color="#86EFAC", label="UC Proteção Integral"),
            mpatches.Patch(color="#DCFCE7", label="UC Uso Sustentável"),
            mpatches.Patch(color="#FEF08A", label="Terra Indígena (FUNAI)"),
        ])
        anchor_map = {"lower left": (0.015, 0.02), "upper left": (0.015, 0.985), "lower right": (0.985, 0.15)}
        leg = ax.legend(
            handles=handles, fontsize=6.8, loc=leg_loc, bbox_to_anchor=anchor_map[leg_loc],
            frameon=True, facecolor="white", edgecolor="#94A3B8", framealpha=0.97,
        )
        leg.set_zorder(30)
        ax.set_title(title, fontsize=12.4, fontweight="bold", pad=12, color="#0F172A")
        add_cartographic_footer(
            ax,
            f"RECORTE ESTADUAL — {'/'.join(ufs)}",
            sub_txt,
            f"ANM / SIGMINE ({anm_label}); IBGE UF 2025; CNUC/MMA (set/2019); FUNAI (mar/2021); JORC/NI 43-101/SGB.",
            "",
            caveat_txt,
        )
        save_figure_dual(fig, stem)


# ==============================================================================
# FIGURA 06b — Zoom Distrital: Concessões de Lavra em Minaçu–GO (Serra Verde)
# ==============================================================================
def build_fig_06b(data: AtlasData) -> None:
    anm_label = get_anm_snapshot_label()
    sv_mat = data.anm_alb[
        (data.anm_alb["uf"] == "GO")
        & (data.anm_alb["operator_raw"].str.contains("SERRA VERDE", case=False, na=False))
        & (data.anm_alb["stage_tier"].isin(["Concessão de Lavra", "Requerimento de Lavra"]))
    ].copy()
    if sv_mat.empty:
        logging.warning("Sem processos maduros da Serra Verde em GO para fig_06b.")
        return

    b = sv_mat.total_bounds
    xlim = (b[0] - 8_500.0, b[2] + 8_500.0)
    ylim = (b[1] - 7_500.0, b[3] + 6_500.0)
    win_w_km = (xlim[1] - xlim[0]) / 1000.0
    win_h_km = (ylim[1] - ylim[0]) / 1000.0

    fig, ax = plt.subplots(figsize=(11.6, 10.6))
    ax.set_facecolor("#FEFCE8")

    near = data.anm_alb.cx[xlim[0]:xlim[1], ylim[0]:ylim[1]].copy()
    is_sv = near["operator_raw"].str.contains("SERRA VERDE", case=False, na=False)
    is_mature = near["stage_tier"].isin(["Concessão de Lavra", "Requerimento de Lavra"])

    other_bg = near[~is_sv & ~is_mature]
    sv_res = near[is_sv & ~is_mature]
    sv_req = near[is_sv & (near["stage_tier"] == "Requerimento de Lavra")]
    sv_conc = near[is_sv & (near["stage_tier"] == "Concessão de Lavra")]

    if not other_bg.empty:
        other_bg.plot(ax=ax, color="#CBD5E1", edgecolor="#64748B", linewidth=0.55, alpha=0.65, zorder=6)
    if not sv_res.empty:
        sv_res.plot(ax=ax, color="#93C5FD", edgecolor="#1D4ED8", linewidth=0.75, alpha=0.68, zorder=8)
    if not sv_req.empty:
        sv_req.plot(ax=ax, color=STAGE_COLORS["Requerimento de Lavra"], edgecolor="#0F172A", linewidth=1.45, alpha=0.92, zorder=14)
    if not sv_conc.empty:
        sv_conc.plot(ax=ax, color=STAGE_COLORS["Concessão de Lavra"], edgecolor="#0F172A", linewidth=1.6, alpha=0.92, zorder=16)

    # Rótulos individuais de processo e área em todos os polígonos maduros
    callout_specs = {
        "861429/2010": (-2_200.0, 1_200.0, "#9A3412", " (Req. Lavra)"),
        "861694/2010": (2_200.0, 800.0, "#7F1D1D", ""),
        "860262/2011": (2_200.0, -700.0, "#7F1D1D", ""),
    }
    for _, row in sv_mat.iterrows():
        proc = str(row["processo"])
        area_s = fmt_int_ptbr(float(row["area_ha"]))
        pt = row.geometry.representative_point()
        if proc in callout_specs:
            dx, dy, col_hex, suffix = callout_specs[proc]
            _callout(ax, pt.x, pt.y, dx, dy, f"{proc}\n{area_s} ha{suffix}", color=col_hex, fontsize=6.8)
        else:
            ax.text(
                pt.x, pt.y, f"{proc}\n{area_s} ha",
                ha="center", va="center", fontsize=6.7, fontweight="bold", color="#7F1D1D",
                bbox=dict(boxstyle="round,pad=0.18", facecolor="white", edgecolor="#991B1B", linewidth=0.75, alpha=0.93),
                zorder=25,
            )

    ax.set_xlim(*xlim)
    ax.set_ylim(*ylim)
    ax.set_aspect("equal")
    ax.grid(True, linestyle=":", linewidth=0.45, color="#CBD5E1", alpha=0.70, zorder=0)
    format_map_axes_and_scalebar(
        ax, IBGE_ALBERS_CRS, scale_pos="bottom_right", candidates_km=[2, 5, 10], show_crs_box=True,
    )

    # Inset localizador em Goiás (canto inferior esquerdo)
    ax_ins = inset_axes(
        ax, width="23%", height="26%", loc="lower left",
        bbox_to_anchor=(0.015, 0.02, 1, 1), bbox_transform=ax.transAxes,
    )
    ax_ins.set_zorder(28)
    go_gdf = data.states_alb[data.states_alb["uf"] == "GO"]
    gb = go_gdf.total_bounds
    gdx, gdy = gb[2] - gb[0], gb[3] - gb[1]
    ix0, ix1 = gb[0] - gdx * 0.12, gb[2] + gdx * 0.12
    iy0, iy1 = gb[1] - gdy * 0.10, gb[3] + gdy * 0.10
    data.states_alb.cx[ix0:ix1, iy0:iy1].plot(ax=ax_ins, color="#F1F5F9", edgecolor="#CBD5E1", linewidth=0.45, zorder=1)
    go_gdf.plot(ax=ax_ins, color="#FFFFFF", edgecolor="#1E293B", linewidth=1.0, zorder=2)
    sv_pt = sv_conc.geometry.union_all().centroid if not sv_conc.empty else sv_mat.geometry.union_all().centroid
    ax_ins.scatter([sv_pt.x], [sv_pt.y], s=55, c="#B91C1C", marker="s", edgecolors="white", linewidths=0.7, zorder=5)
    ax_ins.set_xlim(ix0, ix1)
    ax_ins.set_ylim(iy0, iy1)
    ax_ins.set_xticks([])
    ax_ins.set_yticks([])
    ax_ins.set_xlabel("")
    ax_ins.set_ylabel("")
    ax_ins.set_title("Localização: Minaçu (GO)", fontsize=7.2, fontweight="bold", pad=4, color="#0F172A")
    for sp in ax_ins.spines.values():
        sp.set_edgecolor("#991B1B")
        sp.set_linewidth(1.1)

    conc_n = len(sv_conc)
    conc_ha_round = fmt_int_ptbr(float(sv_conc["area_ha"].sum()))
    conc_ha_dec = fmt_ptbr(float(sv_conc["area_ha"].sum()), 1)
    req_n = len(sv_req)
    req_ha_round = fmt_int_ptbr(float(sv_req["area_ha"].sum()))
    req_ha_dec = fmt_ptbr(float(sv_req["area_ha"].sum()), 1)

    handles = [
        mpatches.Patch(facecolor=STAGE_COLORS["Concessão de Lavra"], edgecolor="#0F172A", linewidth=1.0, label=f"Concessão de Lavra — Serra Verde ({conc_n} | {conc_ha_round} ha)"),
        mpatches.Patch(facecolor=STAGE_COLORS["Requerimento de Lavra"], edgecolor="#0F172A", linewidth=1.0, label=f"Requerimento de Lavra — Serra Verde ({req_n} | {req_ha_round} ha)"),
        mpatches.Patch(facecolor="#93C5FD", edgecolor="#1D4ED8", linewidth=0.7, label=f"Pesquisa / Outros — Serra Verde (entorno: {len(sv_res)})"),
        mpatches.Patch(facecolor="#CBD5E1", edgecolor="#64748B", linewidth=0.6, label=f"Pesquisa / Outros — Terceiros (entorno: {len(other_bg)})"),
    ]
    leg = ax.legend(
        handles=handles,
        title="Polígonos ANM no Distrito Minaçu (GO)",
        title_fontsize=7.8, fontsize=6.9,
        loc="upper left", bbox_to_anchor=(0.015, 0.985),
        frameon=True, facecolor="white", edgecolor="#94A3B8", framealpha=0.97,
    )
    leg.set_zorder(30)

    ax.set_title(
        f"Zoom Distrital — Minaçu (GO): As {conc_n} Concessões de Lavra de Terras Raras (Serra Verde / Pela Ema)",
        fontsize=12.4, fontweight="bold", pad=12, color="#0F172A",
    )
    add_cartographic_footer(
        ax,
        "ZOOM DISTRITAL — MINAÇU (GO) / SERRA VERDE",
        f"Detalhamento em escala distrital ({fmt_ptbr(win_w_km, 1)} × {fmt_ptbr(win_h_km, 1)} km) das {conc_n} Concessões de Lavra ({conc_ha_dec} ha) + {req_n} Requerimento de Lavra ({req_ha_dec} ha) e {len(near)} processos no entorno.",
        f"ANM / SIGMINE (BRASIL.shp, {anm_label}); IBGE UF 2025; Biomas IBGE 1:250k.",
        "",
        "100% das Concessões de Lavra ativas no Cadastro ANM de Terras Raras concentram-se neste bloco contíguo em Minaçu (GO); polígonos do entorno são títulos em fase de pesquisa/requerimento.",
    )
    save_figure_dual(fig, "fig_06b_zoom_concessoes_lavra_serra_verde_minacu", save_svg=data.save_svg)


# ==============================================================================
# FIGURA 09 — Paradoxo Global USGS MCS 2026 (Reservas vs Produção)
# ==============================================================================
def build_fig_09(data: AtlasData) -> None:
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(15.4, 7.2), gridspec_kw={"width_ratios": [1.35, 1.0]})

    # Painel Esquerdo: Top 12 paises por max(res_share, prod_share) em escala linear estrita
    m = data.mcs[data.mcs["country"] != "Other countries"].copy()
    m["max_s"] = m[["reserves_share_reported_pct", "prod_2025_share_pct"]].max(axis=1)
    top = m.sort_values("max_s", ascending=True).tail(12)

    y = np.arange(len(top))
    h = 0.36
    b1 = ax1.barh(y + h / 2, top["reserves_share_reported_pct"], height=h, color="#0D9488", label="% Reservas 2025 (Base 85,29 Mt)")
    b2 = ax1.barh(y - h / 2, top["prod_2025_share_pct"], height=h, color="#DC2626", label="% Produção Mina 2025 (390 kt)")
    ax1.set_yticks(y)
    ax1.set_yticklabels(top["country_pt"], fontsize=9.0, fontweight="bold")
    ax1.set_xlim(0, 82)
    ax1.set_xlabel("Participação Global (%) — Escala Linear", fontsize=9.5, fontweight="bold")
    ax1.set_title("Assimetria entre Estoques Geológicos (Reservas) e Fluxo Produtivo", fontsize=11.0, fontweight="bold", pad=10)
    ax1.legend(loc="lower right", fontsize=8.2, frameon=True, facecolor="white")

    for r1, r2 in zip(b1, b2):
        w1 = r1.get_width()
        w2 = r2.get_width()
        if w1 > 0.2:
            ax1.text(w1 + 0.8, r1.get_y() + r1.get_height() / 2, f"{w1:.1f}%", va="center", fontsize=7.6, color="#0F766E", fontweight="bold")
        if w2 > 0.2:
            ax1.text(w2 + 0.8, r2.get_y() + r2.get_height() / 2, f"{w2:.1f}%", va="center", fontsize=7.6, color="#B91C1C", fontweight="bold")

    # Destaque textual para o Brasil
    br_idx = list(top["country_pt"]).index("Brasil")
    ax1.annotate(
        "Brasil: 2º em reservas (24,6%)\nmas 0,51% da produção (2.000 t)",
        xy=(22.0, br_idx), xytext=(41, br_idx - 1.15),
        fontsize=8.0, fontweight="bold", color="#0F172A",
        bbox=dict(boxstyle="round,pad=0.3", facecolor="#FEF3C7", edgecolor="#D97706"),
        arrowprops=dict(arrowstyle="-|>", color="#D97706", lw=1.0),
    )

    # Painel Direito: Barras 100% empilhadas por bloco geopolitico
    blocks = ["BRICS+", "Demais Sul Global", "Norte Global", "Outros USGS"]
    w_prod = float(data.mcs["world_prod_2025_t"].iloc[0])
    w_res = float(data.mcs["world_res_reported_sum_t"].iloc[0])
    res_blk = {b: float(data.mcs[data.mcs["geoblock"] == b]["reserves_2025_t"].sum()) / w_res * 100.0 for b in blocks}
    prod_blk = {b: float(data.mcs[data.mcs["geoblock"] == b]["prod_2025_t"].sum()) / w_prod * 100.0 for b in blocks}

    cats = ["Produção Mina\n2025 (390 kt)", "Reservas Mundiais\n2025 (85,29 Mt*)"]
    left_cum = np.zeros(2)
    for b in blocks:
        vals = np.array([prod_blk[b], res_blk[b]])
        bars = ax2.barh([0, 1], vals, left=left_cum, height=0.45, color=BLOCK_COLORS[b], edgecolor="white", linewidth=0.8, label=b)
        for i, v in enumerate(vals):
            if v >= 4.5:
                fs = 7.4 if v < 9.0 else 8.8
                ax2.text(left_cum[i] + v / 2, i, f"{v:.1f}%", ha="center", va="center", color="white" if b != "Outros USGS" else "#0F172A", fontsize=fs, fontweight="bold")
        left_cum += vals

    ax2.set_yticks([0, 1])
    ax2.set_yticklabels(cats, fontsize=9.5, fontweight="bold")
    ax2.set_xlim(0, 100)
    ax2.set_xlabel("Participação por Bloco Geopolítico (%)", fontsize=9.5, fontweight="bold")
    ax2.set_title("Distribuição Geopolítica (BRICS+, Sul e Norte Global)", fontsize=11.0, fontweight="bold", pad=10)
    ax2.legend(loc="upper center", bbox_to_anchor=(0.5, -0.14), ncol=2, fontsize=8.2, frameon=True, facecolor="white")

    fig.suptitle("Terras Raras na Economia Global: Reservas, Produção e Blocos Geopolíticos (USGS MCS 2026)", fontsize=13.5, fontweight="bold", y=0.98, color="#0F172A")
    fig.subplots_adjust(bottom=0.22, wspace=0.25)
    add_figure_footer(
        fig,
        "PANORAMA GLOBAL USGS MCS 2026",
        "Segregação entre estoque de reservas geológicas (2025) e fluxo anual de produção mineral (2025) em escala linear estrita; blocos fecham em 100,0%.",
        "USGS Mineral Commodity Summaries 2026 (capítulo Rare Earths, dados 2024–2025 em equivalente óxido REO).",
        "",
        "*Total mundial de reservas reportado pelo USGS como '>85 Mt' pois Índia, Mianmar, Madagascar, Nigéria e Tailândia constam como NA (soma dos reportados: 85,29 Mt).",
    )
    save_figure_dual(fig, "fig_09_paradoxo_global_mcs2026")


# ==============================================================================
# FIGURA 10 — Corrida Regulatória ANM (1970–2026), Funil e Top 12 UFs
# ==============================================================================
def build_fig_10(data: AtlasData) -> None:
    fig = plt.figure(figsize=(15.5, 8.8))
    fig.subplots_adjust(bottom=0.14)
    gs = fig.add_gridspec(2, 2, height_ratios=[1.05, 1.0], hspace=0.38, wspace=0.28)
    ax_ts = fig.add_subplot(gs[0, :])
    ax_fun = fig.add_subplot(gs[1, 0])
    ax_uf = fig.add_subplot(gs[1, 1])

    anm_label = get_anm_snapshot_label()
    anm = data.anm_alb.copy()
    anm["yr_plot"] = anm["first_year"].fillna(1970).astype(int).clip(lower=1970)
    ts = anm.groupby("yr_plot").agg(procs=("processo", "count"), area_kha=("area_ha", lambda s: s.sum() / 1000.0)).reindex(range(1970, 2027), fill_value=0)

    ax_ts.bar(ts.index, ts["procs"], color=["#DC2626" if y >= 2023 else "#2563EB" for y in ts.index], alpha=0.85, label="Nº Processos Protocolados/Ano")
    ax_ts.set_ylabel("Novos Processos ANM / Ano", fontsize=9.2, fontweight="bold", color="#1E3A8A")
    ax_ts.set_xlim(1969, 2027)
    ax_ts2 = ax_ts.twinx()
    ax_ts2.plot(ts.index, ts["area_kha"], color="#0F172A", linewidth=1.8, marker="o", markersize=2.6, label="Área Requerida (mil ha/ano)")
    ax_ts2.set_ylabel("Área Requerida (mil ha / ano)", fontsize=9.2, fontweight="bold", color="#0F172A")
    ax_ts2.grid(False)
    ax_ts.set_title("Dinâmica Temporal dos Protocolos ANM de Terras Raras (1970–2026): A Corrida Pós-2023", fontsize=11.0, fontweight="bold")
    boom_cnt = int(anm[anm["first_year"] >= 2023].shape[0])
    boom_pct_s = fmt_ptbr(boom_cnt / len(anm) * 100.0, 1)
    ax_ts.annotate(
        f"Boom 2023–2026: {fmt_int_ptbr(boom_cnt)} processos\n({boom_pct_s}% do cadastro histórico)",
        xy=(2024, float(ts.loc[2024, "procs"])), xytext=(2004, float(ts["procs"].max()) * 0.78),
        fontsize=8.4, fontweight="bold", color="#991B1B",
        bbox=dict(boxstyle="round,pad=0.3", facecolor="#FEF2F2", edgecolor="#DC2626"),
        arrowprops=dict(arrowstyle="-|>", color="#DC2626", lw=1.1),
    )

    # Funil Regulatorio
    st_counts = anm["stage_tier"].value_counts().reindex(STAGE_ORDER, fill_value=0)
    y_f = np.arange(len(STAGE_ORDER))
    ax_fun.barh(y_f, st_counts.values, color=[STAGE_COLORS[s] for s in STAGE_ORDER], height=0.58)
    ax_fun.set_yticks(y_f)
    ax_fun.set_yticklabels(["Concessão\nde Lavra", "Requerimento\nde Lavra", "Dir. Requerer\na Lavra", "Autorização\nde Pesquisa", "Req. Pesquisa /\nDisp. / Outros"], fontsize=8.3, fontweight="bold")
    ax_fun.invert_yaxis()
    ax_fun.set_xlim(0, max(st_counts.values) * 1.33)
    ax_fun.set_xlabel("Nº Processos (Escala Linear)", fontsize=8.8, fontweight="bold")
    ax_fun.set_title("Funil Regulatório ANM (Maturidade Mineral)", fontsize=10.5, fontweight="bold")
    for idx, val in enumerate(st_counts.values):
        ax_fun.text(val + 35, idx, f"{fmt_int_ptbr(val)} ({val/len(anm)*100:.2f}%)", va="center", fontsize=8.0, fontweight="bold")

    # Top 12 UFs: Bruta vs Liquida
    uf_top = data.uf_net.sort_values("gross_area_ha", ascending=False).head(12).iloc[::-1]
    yu = np.arange(len(uf_top))
    ax_uf.barh(yu + 0.18, uf_top["gross_area_ha"] / 1000.0, height=0.35, color="#2563EB", label="Área Bruta Titulada")
    ax_uf.barh(yu - 0.18, uf_top["net_dissolved_ha"] / 1000.0, height=0.35, color="#0D9488", label="Pegada Líquida Dissolvida")
    ax_uf.set_yticks(yu)
    ax_uf.set_yticklabels(uf_top["uf"], fontsize=8.5, fontweight="bold")
    ax_uf.set_xlabel("Área (mil ha)", fontsize=8.8, fontweight="bold")
    ax_uf.set_title("Top 12 UFs: Área Bruta vs. Pegada Dissolvida", fontsize=10.5, fontweight="bold")
    ax_uf.legend(loc="lower right", fontsize=7.6)

    conc_cnt = int(st_counts.loc["Concessão de Lavra"])
    conc_pct_s = fmt_ptbr(conc_cnt / len(anm) * 100.0, 2)
    fig.suptitle("Corrida Exploratória, Funil Regulatório e Concentração Estadual das Terras Raras no Brasil", fontsize=13.5, fontweight="bold", y=0.98, color="#0F172A")
    add_figure_footer(
        fig,
        "DINÂMICA TEMPORAL E FUNIL REGULATÓRIO ANM",
        "Evolução anual de protocolos (ANO), conversão pesquisa→lavra e concentração estadual em área bruta vs. pegada líquida dissolvida (union_all em Albers).",
        f"ANM / SIGMINE (BRASIL.shp, {anm_label}; {fmt_int_ptbr(len(anm))} processos no Cadastro ANM).",
        "",
        f"Apenas {conc_cnt} processos ({conc_pct_s}% do total, todos da Serra Verde/GO) atingiram o estágio de Concessão de Lavra.",
    )
    save_figure_dual(fig, "fig_10_corrida_regulatoria_e_funil_anm")


# ==============================================================================
# FIGURA 11 — Concentração Societária e Origem do Capital
# ==============================================================================
def build_fig_11(data: AtlasData) -> None:
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(15.5, 7.6), gridspec_kw={"width_ratios": [1.45, 1.0]})
    anm_label = get_anm_snapshot_label()
    anm = data.anm_alb
    tot_ha = float(anm["area_ha"].sum())

    op_g = anm.groupby(["operator_raw", "origin_country"], as_index=False).agg(area_ha=("area_ha", "sum"), procs=("processo", "count")).sort_values("area_ha", ascending=False)
    top20 = op_g.head(20).iloc[::-1]
    y = np.arange(len(top20))
    colors = [CAPITAL_ORIGIN_COLORS.get(o, "#94A3B8") for o in top20["origin_country"]]
    ax1.barh(y, top20["area_ha"] / 1000.0, color=colors, height=0.68)
    ax1.set_yticks(y)
    ax1.set_yticklabels([n[:36] + ("..." if len(n) > 36 else "") for n in top20["operator_raw"]], fontsize=7.6, fontweight="bold")
    ax1.set_xlabel("Área Titulada Bruta (mil ha)", fontsize=9.2, fontweight="bold")
    ax1.set_xlim(0, top20["area_ha"].max() / 1000.0 * 1.32)
    ax1.set_title("Top 20 Titulares ANM por Área e Origem do Capital", fontsize=11.0, fontweight="bold")
    for idx, (_, r) in enumerate(top20.iterrows()):
        ax1.text(r["area_ha"] / 1000.0 + 4, idx, f"{r['area_ha']/1000.0:.1f} mil ha ({r['procs']})", va="center", fontsize=7.1)

    sh = op_g["area_ha"] / tot_ha * 100.0
    cr4, cr10, cr20 = sh.head(4).sum(), sh.head(10).sum(), sh.head(20).sum()
    top45_cov = sh.head(45).sum()
    hhi = (sh ** 2).sum()
    ax1.text(
        0.97, 0.06,
        f"Concentração ({fmt_int_ptbr(len(op_g))} titulares)\nCR4: {cr4:.1f}% | CR10: {cr10:.1f}%\nCR20: {cr20:.1f}% | HHI: {hhi:.0f}",
        transform=ax1.transAxes, ha="right", va="bottom", fontsize=8.0, fontweight="bold",
        bbox=dict(boxstyle="round,pad=0.35", facecolor="#F8FAFC", edgecolor="#64748B"),
    )

    # Painel Direito: Participacao por Pais / Origem do Capital + Tiers
    orig_g = anm.groupby("origin_country", as_index=False)["area_ha"].sum().sort_values("area_ha", ascending=True)
    y2 = np.arange(len(orig_g))
    ax2.barh(y2, orig_g["area_ha"] / tot_ha * 100.0, color=[CAPITAL_ORIGIN_COLORS.get(o, "#94A3B8") for o in orig_g["origin_country"]], height=0.58)
    ax2.set_yticks(y2)
    ax2.set_yticklabels(["Titular Local /\nSPE Não Classificada" if o == "Titular Local / SPE Não Classificada" else o for o in orig_g["origin_country"]], fontsize=8.2, fontweight="bold")
    ax2.set_xlim(0, max(82.0, orig_g["area_ha"].max() / tot_ha * 100.0 * 1.65))
    ax2.set_xlabel(f"% da Área Bruta Nacional ({fmt_ptbr(tot_ha / 1e6, 2)} Mha)", fontsize=9.2, fontweight="bold")
    ax2.set_title("Distribuição por Origem Verificada do Capital", fontsize=11.0, fontweight="bold")
    for idx, (_, r) in enumerate(orig_g.iterrows()):
        pct = r["area_ha"] / tot_ha * 100.0
        ax2.text(pct + 0.8, idx, f"{pct:.1f}% ({r['area_ha']/1000:.0f} mil ha)", va="center", fontsize=7.8, fontweight="bold")

    fig.suptitle("Estrutura Societária, Concentração de Área e Origem do Capital em Terras Raras (Cadastro ANM)", fontsize=13.5, fontweight="bold", y=0.98, color="#0F172A")
    fig.subplots_adjust(bottom=0.14, wspace=0.52)
    add_figure_footer(
        fig,
        "CONTROLE SOCIETÁRIO E CONCENTRAÇÃO",
        f"Auditoria de controle econômico para os Top 45 titulares ({fmt_ptbr(top45_cov, 1)}% da área) e 100% dos processos maduros ({fmt_int_ptbr(len(anm))} processos em escala linear).",
        f"ANM / SIGMINE ({anm_label}); Registros ASX, TSX, SEC, CNPJ e comunicados corporativos (auditoria nov/2026).",
        "",
        "Títulos abaixo do Top 45 e SPEs sem controlador estrangeiro publicamente verificável permanecem rotulados como 'Titular Local / SPE Não Classificada'.",
    )
    save_figure_dual(fig, "fig_11_concentracao_societaria_e_capital")


# ==============================================================================
# FIGURA 12 — Comércio Exterior Comex Stat / MDIC
# ==============================================================================
def build_fig_12(data: AtlasData) -> None:
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(15.4, 7.2))
    cx = data.comex.copy()

    # Painel Esquerdo: Exportacao vs Importacao FOB (2015-2026* parcial)
    yr_fob = cx.groupby(["year", "flow"])["fob_usd"].sum().unstack(fill_value=0.0) / 1e6
    years = sorted(yr_fob.index)
    x = np.arange(len(years))
    w = 0.38
    exp_vals = [float(yr_fob.loc[y].get("Exportação", 0.0)) for y in years]
    imp_vals = [float(yr_fob.loc[y].get("Importação", 0.0)) for y in years]

    ax1.bar(x - w / 2, exp_vals, width=w, color=["#86EFAC" if y == 2026 else "#059669" for y in years], edgecolor="#065F46", label="Exportação FOB (US$ mi)")
    ax1.bar(x + w / 2, imp_vals, width=w, color=["#FCA5A5" if y == 2026 else "#DC2626" for y in years], edgecolor="#7F1D1D", label="Importação FOB (US$ mi)")
    ax1.set_xticks(x)
    ax1.set_xticklabels(["2026*" if y == 2026 else str(y) for y in years], fontsize=8.2, rotation=25)
    ax1.set_ylabel("Valor FOB (US$ Milhões)", fontsize=9.2, fontweight="bold")
    ax1.set_title("Balança Comercial: ETR (Cap. 28) + Ímãs Permanentes (8505.11)", fontsize=10.8, fontweight="bold")
    ax1.legend(loc="upper left", fontsize=8.2)
    if 2025 in years:
        i25 = years.index(2025)
        ax1.annotate(
            f"2025: US$ {exp_vals[i25]:.1f} mi\n(MREC NCM 2846.90.90)",
            xy=(i25 - w / 2, exp_vals[i25]), xytext=(i25 - 4.2, exp_vals[i25] + 10.5),
            fontsize=7.8, fontweight="bold", color="#065F46",
            bbox=dict(boxstyle="round,pad=0.25", facecolor="#ECFDF5", edgecolor="#059669"),
            arrowprops=dict(arrowstyle="-|>", color="#059669"),
        )

    # Painel Direito: Composicao das Importacoes (Imas 8505.11 vs Cap 28) + % China
    imp = cx[cx["flow"] == "Importação"]
    m_yr = imp[imp["sh6"] == "850511"].groupby("year")["fob_usd"].sum().reindex(years, fill_value=0.0) / 1e6
    r_yr = imp[imp["sh6"] != "850511"].groupby("year")["fob_usd"].sum().reindex(years, fill_value=0.0) / 1e6
    tot_yr = m_yr + r_yr
    chn_yr = imp[imp["country"].str.contains("China", case=False, na=False)].groupby("year")["fob_usd"].sum().reindex(years, fill_value=0.0) / 1e6
    chn_pct = (chn_yr / tot_yr.replace(0, np.nan) * 100.0).fillna(0.0)

    ax2.bar(x, m_yr.values, width=0.58, color="#1E3A8A", label="Ímãs Permanentes Metálicos (8505.11)")
    ax2.bar(x, r_yr.values, bottom=m_yr.values, width=0.58, color="#D97706", label="Compostos e Metais de ETR (2805.30/2846)")
    ax2.set_xticks(x)
    ax2.set_xticklabels(["2026*" if y == 2026 else str(y) for y in years], fontsize=8.2, rotation=25)
    ax2.set_ylabel("Importações FOB (US$ Milhões)", fontsize=9.2, fontweight="bold")
    ax2.set_title("Hiato Downstream: Importação de Ímãs vs. Dependência da China", fontsize=10.8, fontweight="bold")

    ax2b = ax2.twinx()
    ax2b.plot(x, chn_pct.values, color="#DC2626", marker="o", linewidth=2.0, label="% China nas Importações")
    ax2b.set_ylim(0, 105)
    ax2b.set_ylabel("% China no Total Importado", fontsize=9.0, fontweight="bold", color="#DC2626")
    ax2b.grid(False)

    h1, l1 = ax2.get_legend_handles_labels()
    h2, l2 = ax2b.get_legend_handles_labels()
    ax2.legend(h1 + h2, l1 + l2, loc="upper left", fontsize=7.6, frameon=True, facecolor="white")

    fig.suptitle("Comércio Exterior Brasileiro de Terras Raras e Ímãs Permanentes (Comex Stat/MDIC, 2015–2026*)", fontsize=13.2, fontweight="bold", y=0.98, color="#0F172A")
    fig.subplots_adjust(bottom=0.15, wspace=0.28)
    add_figure_footer(
        fig,
        "COMÉRCIO EXTERIOR — COMEX STAT / MDIC",
        "Fluxos anuais FOB (US$ milhões, escalas lineares) para NCMs 2805.30, 2846.10, 2846.90 e 8505.11.",
        "MDIC / Comex Stat API (api-comexstat.mdic.gov.br, consulta nov/2026).",
        "",
        "*2026 corresponde ao acumulado parcial divulgado até nov/2026 (barras claras). A NCM 8505.11 responde por 83% das importações acumuladas no período.",
    )
    save_figure_dual(fig, "fig_12_comercio_exterior_comex_stat", save_svg=data.save_svg)


# ==============================================================================
# FIGURA 13 — Matriz Geológica (Teor vs Contido) e Cadeia de Valor
# ==============================================================================
def build_fig_13(data: AtlasData) -> None:
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(15.5, 7.5), gridspec_kw={"width_ratios": [1.15, 1.05]})
    anc = data.anchors_alb.copy()
    quant = anc.dropna(subset=["resource_mt", "grade_ppm_treo"]).copy()
    quant["contained_treo_kt"] = quant["resource_mt"] * quant["grade_ppm_treo"] / 1000.0
    quant = quant.sort_values("contained_treo_kt", ascending=True)

    yq = np.arange(len(quant))
    cols = [GEOLOGY_COLORS.get(g, "#64748B") for g in quant["geology_type"]]
    ax1.barh(yq, quant["contained_treo_kt"], color=cols, height=0.62)
    ax1.set_yticks(yq)
    ax1.set_yticklabels([f"{r['project_name']} ({r['uf']})" for _, r in quant.iterrows()], fontsize=8.4, fontweight="bold")
    ax1.set_xlim(0, 6850)
    ax1.set_xlabel("TREO Contido Reportado (kt REO) — Escala Linear", fontsize=9.0, fontweight="bold")
    ax1.set_title("Projetos com Recursos Publicados: TREO Contido & Teor", fontsize=10.8, fontweight="bold")
    for idx, (_, r) in enumerate(quant.iterrows()):
        grade_lbl = f"{r['grade_ppm_treo']/10000:.2f}%".replace(".", ",") if r["grade_ppm_treo"] >= 10_000 else f"{r['grade_ppm_treo']:,.0f} ppm".replace(",", ".")
        mt_lbl = f"{r['resource_mt']:.1f}".replace(".", ",") if r["resource_mt"] < 10 else f"{r['resource_mt']:,.0f}".replace(",", ".")
        ax1.text(r["contained_treo_kt"] + 85, idx, f"{r['contained_treo_kt']:,.0f} kt ({mt_lbl} Mt @ {grade_lbl})".replace(",", "."), va="center", fontsize=7.3, fontweight="bold")
    leg_g = [mpatches.Patch(color=GEOLOGY_COLORS[g], label=g) for g in ["Argila Iônica", "Carbonatito / Alcalino"]]
    ax1.legend(handles=leg_g, loc="lower right", fontsize=8.0)

    # Painel Direito: Matriz de Maturidade dos Elos da Cadeia no Brasil
    stages = ["1. Extração &\nConcentração", "2. Lixiviação /\nMREC", "3. Separação\nde Óxidos", "4. Metais\ne Ligas", "5. Ímãs\nNdFeB"]
    players = [
        ("Serra Verde / Pela Ema (GO)", [2, 2, 0, 0, 0]),
        ("Aclara / Carina (GO)", [1, 1, 0, 0, 0]),
        ("Meteoric / Caldeira (MG)", [1, 1, 0, 0, 0]),
        ("Viridis / Colossus (MG)", [1, 1, 1, 0, 0]),
        ("BRE / Monte Alto (BA)", [1, 1, 1, 0, 0]),
        ("St George / Araxá (MG)", [1, 1, 1, 0, 0]),
        ("Energy Fuels / Bahia (BA)", [1, 0, 0, 0, 0]),
        ("LabFabITR / CIT SENAI (MG)", [0, 0, 0, 2, 2]),
    ]
    mat = np.array([p[1] for p in players])
    cmap_vals = {0: "#F1F5F9", 1: "#FDE68A", 2: "#16A34A"}
    lbl_vals = {0: "Ausente", 1: "Projeto / Piloto", 2: "Ativo / Comiss."}

    ax2.set_xlim(-0.5, len(stages) - 0.5)
    ax2.set_ylim(-0.5, len(players) - 0.5)
    for r_i in range(len(players)):
        for c_j in range(len(stages)):
            val = int(mat[r_i, c_j])
            rect = mpatches.Rectangle((c_j - 0.44, r_i - 0.38), 0.88, 0.76, facecolor=cmap_vals[val], edgecolor="#CBD5E1", linewidth=0.8)
            ax2.add_patch(rect)
            ax2.text(c_j, r_i, lbl_vals[val], ha="center", va="center", fontsize=7.0, fontweight="bold" if val > 0 else "normal", color="white" if val == 2 else "#334155")

    ax2.set_xticks(range(len(stages)))
    ax2.set_xticklabels(stages, fontsize=8.0, fontweight="bold")
    ax2.set_yticks(range(len(players)))
    ax2.set_yticklabels([p[0] for p in players], fontsize=8.0, fontweight="bold")
    ax2.invert_yaxis()
    ax2.grid(False)
    ax2.set_title("Cobertura dos Elos da Cadeia Produtiva (Gargalo Midstream)", fontsize=10.8, fontweight="bold")

    fig.suptitle("Tipologia Geológica, Recursos Publicados e Maturidade da Cadeia de Terras Raras no Brasil", fontsize=13.2, fontweight="bold", y=0.98, color="#0F172A")
    fig.subplots_adjust(bottom=0.14, wspace=0.52)
    add_figure_footer(
        fig,
        "TIPOLOGIA GEOLÓGICA E CADEIA PRODUTIVA",
        "Comparação de TREO contido (JORC / NI 43-101, escala linear em kt REO) e posicionamento por elo da cadeia mine-to-magnet.",
        "Relatórios JORC (2012) e NI 43-101 (Meteoric, Viridis, BRE, Aclara, St George, BCM, Appia/Ultra); Serra Verde; FIEMG/LabFabITR.",
        "",
        "Recursos/teores heterogêneos não são somáveis como reservas homogêneas; evidencia hiato no elo 3 (separação comercial) e elos 4–5 (LabFabITR, 100 t/ano proj.).",
    )
    save_figure_dual(fig, "fig_13_matriz_tipologia_geologica_e_cadeia")


# ==============================================================================
# FIGURA 14 — Sobreposição Socioambiental (Biomas, UCs e TIs)
# ==============================================================================
def build_fig_14(data: AtlasData) -> None:
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(15.4, 7.2))
    s = data.socio_sum.copy()
    anm_label = get_anm_snapshot_label()

    # Painel Esquerdo: Biomas IBGE (Bruta vs Liquida)
    bio_s = s[(s["scope"] == "Cadastro ANM (Terras Raras)") & (s["dimension"] == "Bioma IBGE")].sort_values("net_overlap_ha", ascending=True)
    yb = np.arange(len(bio_s))
    ax1.barh(yb + 0.18, bio_s["gross_overlap_ha"] / 1000.0, height=0.35, color="#2563EB", label="Área Bruta Sobreposição")
    ax1.barh(yb - 0.18, bio_s["net_overlap_ha"] / 1000.0, height=0.35, color="#0D9488", label="Pegada Líquida Dissolvida")
    ax1.set_yticks(yb)
    ax1.set_yticklabels(bio_s["category"], fontsize=8.8, fontweight="bold")
    ax1.set_xlabel("Área (mil ha) — Escala Linear", fontsize=9.2, fontweight="bold")
    ax1.set_xlim(0, bio_s["gross_overlap_ha"].max() / 1000.0 * 1.38)
    ax1.set_title("Distribuição por Bioma IBGE (Cadastro ANM)", fontsize=11.0, fontweight="bold")
    ax1.legend(loc="lower right", fontsize=8.0)
    for idx, (_, r) in enumerate(bio_s.iterrows()):
        ax1.text(r["gross_overlap_ha"] / 1000.0 + 30, idx + 0.16, f"{r['gross_overlap_ha']/1000:.1f}k ({r['process_count']} proc.)", va="center", fontsize=7.4)

    # Painel Direito: UCs e TIs (Pegada Liquida Dissolvida: Cadastro ANM vs + Coprodutos)
    cats = ["UCs Proteção Integral (PI)", "UCs Uso Sustentável (US)", "Total UCs (União PI + US)", "Total Terras Indígenas (Todas as Fases)"]
    sub_est = s[(s["scope"] == "Cadastro ANM (Terras Raras)") & (s["category"].isin(cats))].set_index("category").reindex(cats)
    sub_comb = s[(s["scope"] == "Cadastro ANM + Coprodutos") & (s["category"].isin(cats))].set_index("category").reindex(cats)

    yc = np.arange(len(cats))
    ax2.barh(yc + 0.18, sub_est["net_overlap_ha"] / 1000.0, height=0.35, color="#DC2626", label="Cadastro ANM (Pegada Líquida)")
    ax2.barh(yc - 0.18, sub_comb["net_overlap_ha"] / 1000.0, height=0.35, color="#7C3AED", label="Cadastro ANM + Coprodutos (Pegada Líquida)")
    ax2.set_yticks(yc)
    ax2.set_yticklabels(["UCs Proteção\nIntegral (PI)", "UCs Uso\nSustentável (US)", "Total UCs\n(União PI+US)", "Terras Indígenas\n(FUNAI)"], fontsize=8.6, fontweight="bold")
    ax2.invert_yaxis()
    ax2.set_xlabel("Sobreposição Líquida Dissolvida (mil ha)", fontsize=9.2, fontweight="bold")
    ax2.set_xlim(0, max(sub_comb["net_overlap_ha"].max(), 1) / 1000.0 * 1.32)
    ax2.set_title("Sobreposição Líquida em UCs e TIs: Cadastro ANM vs. + Coprodutos", fontsize=11.0, fontweight="bold")
    ax2.legend(loc="lower right", fontsize=8.0)
    for idx in range(len(cats)):
        ve = float(sub_est["net_overlap_ha"].iloc[idx]) / 1000.0
        vc = float(sub_comb["net_overlap_ha"].iloc[idx]) / 1000.0
        ax2.text(ve + 4, idx + 0.18, f"{ve:.1f} mil ha", va="center", fontsize=7.4, color="#991B1B", fontweight="bold")
        ax2.text(vc + 4, idx - 0.18, f"{vc:.1f} mil ha", va="center", fontsize=7.4, color="#5B21B6", fontweight="bold")

    fig.suptitle("Perfil Socioambiental das Terras Raras no Brasil: Biomas, UCs e Terras Indígenas", fontsize=13.5, fontweight="bold", y=0.98, color="#0F172A")
    fig.subplots_adjust(bottom=0.14, wspace=0.32)
    add_figure_footer(
        fig,
        "PERFIL SOCIOAMBIENTAL — BIOMAS, UCS E TIS",
        "Quantificação em IBGE Albers da área bruta e pegada territorial líquida dissolvida (ver Tabela T08 para desagregação por fase jurídica de Terra Indígena).",
        f"ANM / SIGMINE ({anm_label}); IBGE Biomas 1:250k (2019); CNUC/MMA (set/2019); FUNAI (mar/2021).",
        "",
        "O acréscimo de coprodutos eleva expressivamente a interface com Amazônia, UCs e TIs devido a Seis Lagos (PARNA Pico da Neblina/TI Yanomami) e Pitinga.",
    )
    save_figure_dual(fig, "fig_14_sobreposicao_socioambiental_biomas")


# ==============================================================================
# FIGURA 15 — Análise de Sensibilidade: Cadastro ANM vs. + Coprodutos
# ==============================================================================
def build_fig_15(data: AtlasData) -> None:
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(15.4, 8.2))
    anm_label = get_anm_snapshot_label()
    s_uf = data.anm_alb.groupby("uf")["area_ha"].sum() / 1000.0
    c_uf = data.cop_alb.groupby("uf")["area_ha"].sum() / 1000.0
    ufs_all = sorted(set(s_uf.index).union(c_uf.index))
    df_uf = pd.DataFrame({
        "uf": ufs_all,
        "base_anm": [float(s_uf.get(u, 0.0)) for u in ufs_all],
        "coproduto": [float(c_uf.get(u, 0.0)) for u in ufs_all],
    })
    df_uf["total"] = df_uf["base_anm"] + df_uf["coproduto"]
    top_uf = df_uf.sort_values("total", ascending=True).tail(12)

    y = np.arange(len(top_uf))
    ax1.barh(y, top_uf["base_anm"], color="#2563EB", height=0.6, label="Cadastro ANM (TERRAS RARAS / MONAZITA / CÉRIO)")
    ax1.barh(y, top_uf["coproduto"], left=top_uf["base_anm"], color="#7C3AED", height=0.6, label="Coprodutos Associados (Al, Sn, Nb, P, Ti)")
    ax1.set_yticks(y)
    ax1.set_yticklabels(top_uf["uf"], fontsize=8.8, fontweight="bold")
    ax1.set_xlabel("Área Bruta Titulada (mil ha)", fontsize=9.2, fontweight="bold")
    ax1.set_xlim(0, top_uf["total"].max() * 1.48)
    ax1.set_title("Sensibilidade por UF: Cadastro ANM vs. + Coprodutos", fontsize=11.0, fontweight="bold")
    ax1.legend(loc="lower right", fontsize=7.8)
    for idx, (_, r) in enumerate(top_uf.iterrows()):
        extra = f" (+{r['coproduto']:.1f}k)" if r["coproduto"] > 0.1 else ""
        ax1.text(r["total"] + 20, idx, f"{r['total']:.1f}k{extra}", va="center", fontsize=7.5, fontweight="bold")

    # Painel Direito: Composicao por Estagio Regulatorio (% de Processos)
    st_est = data.anm_alb["stage_tier"].value_counts(normalize=True).reindex(STAGE_ORDER, fill_value=0.0) * 100.0
    st_cop = data.cop_alb["stage_tier"].value_counts(normalize=True).reindex(STAGE_ORDER, fill_value=0.0) * 100.0
    left_c = np.zeros(2)
    for stg in STAGE_ORDER:
        vals = np.array([float(st_est.loc[stg]), float(st_cop.loc[stg])])
        ax2.barh([0, 1], vals, left=left_c, height=0.42, color=STAGE_COLORS[stg], edgecolor="white", label=stg)
        for i, v in enumerate(vals):
            if v >= 5.0:
                ax2.text(left_c[i] + v / 2, i, f"{v:.1f}%", ha="center", va="center", color="white", fontsize=8.4, fontweight="bold")
        left_c += vals
    ax2.set_yticks([0, 1])
    ax2.set_yticklabels([f"Cadastro ANM\n({fmt_int_ptbr(len(data.anm_alb))} proc.)", f"Coprodutos Associados\n({fmt_int_ptbr(len(data.cop_alb))} proc.)"], fontsize=9.0, fontweight="bold")
    ax2.set_xlim(0, 100)
    ax2.set_xlabel("Distribuição por Estágio Regulatório (% dos Títulos)", fontsize=9.2, fontweight="bold")
    ax2.set_title("Maturidade Regulatória: Cadastro ANM vs. Coprodutos", fontsize=11.0, fontweight="bold")
    ax2.legend(loc="upper center", bbox_to_anchor=(0.5, -0.14), ncol=2, fontsize=7.6, frameon=True, facecolor="white")

    conc_pct_s = fmt_ptbr(float(st_est.loc["Concessão de Lavra"]), 2)
    aut_pct_s = fmt_ptbr(float(st_est.loc["Autorização de Pesquisa"]), 1)
    fig.suptitle("Análise de Sensibilidade Cadastral: Cadastro ANM vs. Escopo Ampliado com Coprodutos", fontsize=13.2, fontweight="bold", y=0.97, color="#0F172A")
    fig.subplots_adjust(bottom=0.26, wspace=0.42)
    add_figure_footer(
        fig,
        "SENSIBILIDADE CADASTRAL — CADASTRO ANM VS. COPRODUTOS",
        "Impacto territorial e regulatório da inclusão de títulos hospedeiros primários de Nb, Sn, P, Ti e Al/Bauxita (escala linear, mil ha brutos).",
        f"ANM / SIGMINE ({anm_label}); Curadoria de complexos de coprodutos (Tabela T05).",
        "",
        f"Enquanto o Cadastro ANM exibe {conc_pct_s}% em Concessão de Lavra ({aut_pct_s}% em Autorização de Pesquisa), os coprodutos concentram títulos maduros herdados de Sn, Nb e Ti.",
    )
    save_figure_dual(fig, "fig_15_comparacao_estrito_vs_coprodutos")


FIGURE_BUILDERS = {
    "01a": build_fig_01a,
    "01b": build_fig_01b,
    "02": build_fig_02,
    "03": build_fig_03,
    "04": build_fig_04,
    "05_08": build_fig_05_to_08,
    "06b": build_fig_06b,
    "09": build_fig_09,
    "10": build_fig_10,
    "11": build_fig_11,
    "12": build_fig_12,
    "13": build_fig_13,
    "14": build_fig_14,
    "15": build_fig_15,
}
PUBLISHED_FIGURES = ["01a", "01b", "06b", "12"]


def main(figures: list[str] | None = None, png_only: bool = False) -> None:
    setup_editorial_style()
    if figures is None:
        has_full = (
            (DATA_PROCESSED / "socioenv_ucs.parquet").exists()
            and (DATA_PROCESSED / "mcs2026_ree_global.parquet").exists()
            and (DATA_PROCESSED / "uf_net_footprint.parquet").exists()
        )
        selected = list(FIGURE_BUILDERS.keys()) if has_full else PUBLISHED_FIGURES
    else:
        selected = figures

    logging.info("Renderizando figuras selecionadas (%s | png_only=%s)...", selected, png_only)
    data = AtlasData(save_svg=not png_only)
    for name in selected:
        fn = FIGURE_BUILDERS[name]
        logging.info("Renderizando %s...", name)
        fn(data)
    logging.info("Geracao de figuras concluida com sucesso!")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gera mapas e graficos 600 DPI de terras_raras_brasil.")
    parser.add_argument("--figures", nargs="+", choices=list(FIGURE_BUILDERS.keys()), help="Chaves das figuras a gerar (padrao: 01a 01b 06b 12 em clone enxuto).")
    parser.add_argument("--all", action="store_true", help="Gera todas as 17 figuras do atlas completo.")
    parser.add_argument("--png-only", action="store_true", help="Salva apenas PNG 600 DPI (sem SVG).")
    args = parser.parse_args()
    figs = list(FIGURE_BUILDERS.keys()) if args.all else args.figures
    main(figures=figs, png_only=args.png_only)
