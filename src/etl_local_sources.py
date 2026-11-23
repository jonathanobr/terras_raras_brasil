# src/etl_local_sources.py
"""
Etapa 2: ETL de fontes locais a partir de `recursos/data/`:
  1. Basemaps oficiais (IBGE UF 2025, Amazonia Azul / ZEE, paises sul-americanos).
  2. Poligonos reais da ANM (`BRASIL.shp`):
     - Camada Estrita (`anm_ree_polygons.parquet`): SUBS in {TERRAS RARAS, MONAZITA, MINERIO DE CERIO}.
     - Camada Segregada de Coprodutos (`anm_coproduct_polygons.parquet`): Nb/Pirocloro, Sn/Cassiterita-Xenotima,
       Bauxita/Argila de Pocos de Caldas e Areias Pesadas Monaziticas.
  3. Estatisticas globais do USGS Mineral Commodity Summaries 2026 (`mcs2026_ree_global.parquet`).
"""

from __future__ import annotations

import json
import logging
import re
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
from shapely.geometry import box

from src.config import (
    ANM_RAW_DIR,
    ANM_RAW_METADATA_PATH,
    ANM_SNAPSHOT_META_PATH,
    BASEMAPS_PROCESSED,
    DATA_PROCESSED,
    IBGE_ALBERS_CRS,
    PROJECT_ROOT,
    SIRGAS_2000_GEO,
    STRICT_REE_SUBSTANCES,
    UF_NAMES,
    UF_TO_REGION,
    WGS84_GEO,
    classify_anm_maturity_group,
    classify_anm_stage,
    normalize_text,
    resolve_anm_shp,
    resolve_brazil_eez_source,
    resolve_ibge_uf_source,
    resolve_mcs_csv_source,
    resolve_ne_countries_source,
)

logging.basicConfig(level=logging.INFO, format="[ETL LOCAL] %(levelname)s: %(message)s")

LOCAL_ANM_SHP_PATH = ANM_RAW_DIR / "BRASIL.shp"

BRICS_PLUS_SET = {
    "Brazil", "Russia", "India", "China", "South Africa",
    "Egypt", "Ethiopia", "Iran", "Saudi Arabia", "United Arab Emirates", "Indonesia",
}
GLOBAL_NORTH_SET = {
    "United States", "Canada", "Australia", "Greenland", "Japan", "New Zealand",
    "United Kingdom", "France", "Germany", "Italy", "Spain", "Portugal",
    "Netherlands", "Belgium", "Switzerland", "Austria", "Sweden", "Norway",
    "Denmark", "Finland", "Ireland", "Iceland", "Luxembourg", "Poland",
    "Ukraine", "Korea, Republic of", "South Korea", "Israel",
}


def build_basemaps() -> None:
    """Converte e valida os basemaps oficiais em GeoParquet."""
    uf_src = resolve_ibge_uf_source()
    if uf_src.suffix.lower() == ".zip":
        with zipfile.ZipFile(uf_src, "r") as zf:
            shps = [n for n in zf.namelist() if n.lower().endswith(".shp")]
            if not shps:
                raise FileNotFoundError(f"Nenhum .shp encontrado dentro de {uf_src}")
            gdf_uf = gpd.read_file(f"zip://{uf_src}!{shps[0]}")
    else:
        gdf_uf = gpd.read_file(uf_src)
    if gdf_uf.crs is None:
        gdf_uf = gdf_uf.set_crs(SIRGAS_2000_GEO)
    else:
        gdf_uf = gdf_uf.to_crs(SIRGAS_2000_GEO)
    gdf_uf["geometry"] = gdf_uf.geometry.make_valid()
    gdf_uf = gdf_uf.rename(columns={
        "SIGLA_UF": "uf",
        "NM_UF": "uf_name",
        "NM_REGIAO": "region",
        "AREA_KM2": "area_ibge_km2",
    })
    gdf_uf["uf"] = gdf_uf["uf"].astype(str).str.strip().str.upper()
    gdf_uf = gdf_uf[["CD_UF", "uf", "uf_name", "region", "area_ibge_km2", "geometry"]].copy()
    uf_out = BASEMAPS_PROCESSED / "brazil_states_ibge2025.parquet"
    gdf_uf.to_parquet(uf_out)
    logging.info("Basemap IBGE UF 2025 salvo (%d UFs): %s", len(gdf_uf), uf_out)

    eez_src = resolve_brazil_eez_source()
    gdf_eez = gpd.read_file(eez_src)
    if gdf_eez.crs is None:
        gdf_eez = gdf_eez.set_crs(WGS84_GEO)
    gdf_eez = gdf_eez.to_crs(SIRGAS_2000_GEO)
    gdf_eez["geometry"] = gdf_eez.geometry.make_valid()
    eez_out = BASEMAPS_PROCESSED / "brazil_eez.parquet"
    gdf_eez.to_parquet(eez_out)
    logging.info("Basemap Amazonia Azul (ZEE) salvo (%d feicoes): %s", len(gdf_eez), eez_out)

    ne_src = resolve_ne_countries_source()
    gdf_world = gpd.read_file(ne_src)
    if gdf_world.crs is None:
        gdf_world = gdf_world.set_crs(WGS84_GEO)
    clip_poly = box(-115.0, -60.0, -15.0, 28.0)
    gdf_world["geometry"] = gdf_world.geometry.make_valid().intersection(clip_poly)
    gdf_world = gdf_world[~gdf_world.geometry.is_empty & gdf_world.geometry.notna()].copy()
    gdf_world = gdf_world.to_crs(SIRGAS_2000_GEO)
    sa_out = BASEMAPS_PROCESSED / "south_america_countries.parquet"
    gdf_world.to_parquet(sa_out)
    logging.info("Basemap America do Sul clipado salvo (%d feicoes): %s", len(gdf_world), sa_out)


def _enrich_anm_columns(gdf: gpd.GeoDataFrame, dataset_layer: str) -> gpd.GeoDataFrame:
    """Padroniza colunas, geometrias validas, centroides WGS84 e areas em Albers."""
    if gdf.crs is None:
        gdf = gdf.set_crs(SIRGAS_2000_GEO)
    else:
        gdf = gdf.to_crs(SIRGAS_2000_GEO)

    gdf = gdf.copy()
    gdf["geometry"] = gdf.geometry.make_valid()
    gdf = gdf[~gdf.geometry.is_empty & gdf.geometry.notna()].copy()

    # Calcular area geodetica/equivalente real em IBGE Albers (ha)
    gdf_albers = gdf.to_crs(IBGE_ALBERS_CRS)
    gdf["area_albers_ha"] = (gdf_albers.geometry.area / 10_000.0).round(4)

    # Centroides em WGS84 (calculados em Albers e reprojetados para precisao metrica)
    cent_wgs = gpd.GeoSeries(gdf_albers.geometry.centroid, crs=IBGE_ALBERS_CRS).to_crs(WGS84_GEO)
    gdf["longitude"] = cent_wgs.x.round(6)
    gdf["latitude"] = cent_wgs.y.round(6)

    # Filtro estrito de bounding box nacional + ZEE
    in_bounds = (
        (gdf["latitude"] >= -34.5) & (gdf["latitude"] <= 6.0) &
        (gdf["longitude"] >= -74.5) & (gdf["longitude"] <= -28.0)
    )
    dropped = int((~in_bounds).sum())
    if dropped > 0:
        logging.warning("Descartados %d poligonos fora do perimetro Brasil+ZEE.", dropped)
        gdf = gdf[in_bounds].copy()

    gdf["processo"] = gdf["PROCESSO"].astype(str).str.strip()
    gdf["numero"] = pd.to_numeric(gdf["NUMERO"], errors="coerce").astype("Int64")
    gdf["first_year"] = pd.to_numeric(gdf["ANO"], errors="coerce").astype("Int64")
    gdf["area_ha"] = pd.to_numeric(gdf["AREA_HA"], errors="coerce").fillna(0.0).round(4)
    gdf["anm_id"] = gdf["ID"].astype(str).str.strip()
    gdf["fase"] = gdf["FASE"].astype(str).str.strip()
    gdf["ult_evento"] = gdf["ULT_EVENTO"].astype(str).str.strip()
    gdf["operator_raw"] = gdf["NOME"].astype(str).str.strip()
    gdf["operator_norm"] = gdf["operator_raw"].apply(normalize_text)
    gdf["substance_raw"] = gdf["SUBS"].astype(str).str.strip()
    gdf["substance_norm"] = gdf["substance_raw"].apply(normalize_text)
    gdf["uso"] = gdf["USO"].astype(str).str.strip()
    gdf["uf"] = gdf["UF"].astype(str).str.strip().str.upper()
    gdf["uf_name"] = gdf["uf"].map(UF_NAMES).fillna(gdf["uf"])
    gdf["region"] = gdf["uf"].map(UF_TO_REGION).fillna("Não Informada")

    # Ano do ultimo evento regulatorio
    ev_yr = gdf["ult_evento"].str.extract(r"EM \d{2}/\d{2}/(19\d{2}|20\d{2})")[0]
    ev_yr_fallback = gdf["ult_evento"].str.extract(r"\b(19\d{2}|20\d{2})\b")[0]
    gdf["ult_evento_year"] = pd.to_numeric(ev_yr.fillna(ev_yr_fallback), errors="coerce").astype("Int64")

    gdf["stage_tier"] = gdf["fase"].apply(classify_anm_stage)
    gdf["maturity_group"] = gdf["stage_tier"].apply(classify_anm_maturity_group)
    gdf["dataset_layer"] = dataset_layer

    keep_cols = [
        "anm_id", "processo", "numero", "first_year", "ult_evento_year",
        "uf", "uf_name", "region", "fase", "stage_tier", "maturity_group",
        "substance_raw", "substance_norm", "uso", "operator_raw", "operator_norm",
        "area_ha", "area_albers_ha", "latitude", "longitude", "ult_evento",
        "dataset_layer",
    ]
    extra_cols = [c for c in ["coproduct_chain", "coproduct_complex"] if c in gdf.columns]
    return gdf[keep_cols + extra_cols + ["geometry"]].reset_index(drop=True)


def build_anm_strict_and_coproducts() -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Extrai separadamente os poligonos base do Cadastro ANM de ETR e os complexos de coprodutos."""
    shp_path = resolve_anm_shp()

    logging.info("Lendo atributos de %s...", shp_path)
    df_attr = pyogrio.read_dataframe(shp_path, read_geometry=False)
    total_anm_rows = int(len(df_attr))
    df_attr["_row_idx"] = np.arange(total_anm_rows)
    subs_norm = df_attr["SUBS"].apply(normalize_text)
    nome_norm = df_attr["NOME"].apply(normalize_text)
    fase_norm = df_attr["FASE"].apply(normalize_text)
    uf_norm = df_attr["UF"].astype(str).str.strip().str.upper()

    # 1. Camada Principal do Cadastro ANM (Terras Raras)
    strict_mask = subs_norm.isin(STRICT_REE_SUBSTANCES)
    strict_indices = df_attr.loc[strict_mask, "_row_idx"].tolist()
    logging.info("Processos do Cadastro ANM identificados no DBF: %d", len(strict_indices))

    # Carregar geometrias por fids (leitura seletiva ultra-rapida)
    gdf_strict_raw = pyogrio.read_dataframe(shp_path, fids=strict_indices)
    gdf_strict = _enrich_anm_columns(gdf_strict_raw, dataset_layer="anm_strict")
    strict_out = DATA_PROCESSED / "anm_ree_polygons.parquet"
    gdf_strict.to_parquet(strict_out)
    logging.info(
        "Cadastro ANM salvo: %d registros | %.1f ha declarados | %.1f ha Albers -> %s",
        len(gdf_strict), gdf_strict["area_ha"].sum(), gdf_strict["area_albers_ha"].sum(), strict_out,
    )

    # 2. Camada Segregada de Coprodutos / Substancias Legadas Associadas
    coprod_specs: list[tuple[pd.Series, str, str]] = [
        (
            nome_norm.str.contains("VIRIDIS MINERACAO", na=False) & subs_norm.isin({"BAUXITA", "MINERIO DE ALUMINIO"}),
            "Bauxita / Argila (Poços de Caldas)",
            "Colossus / Poços de Caldas (Viridis — títulos Bauxita/Al)",
        ),
        (
            nome_norm.str.contains("METEORIC CALDEIRA", na=False) & subs_norm.isin({"BAUXITA", "MINERIO DE ALUMINIO"}),
            "Bauxita / Argila (Poços de Caldas)",
            "Caldeira / Poços de Caldas (Meteoric — títulos Al)",
        ),
        (
            nome_norm.str.contains("MINERACAO TABOCA", na=False) & (uf_norm == "AM") & subs_norm.isin({"ESTANHO", "CASSITERITA", "MINERIO DE ESTANHO"}),
            "Estanho / Cassiterita–Xenotima (Pitinga)",
            "Pitinga (Mineração Taboca / CNMC — Sn/Nb/Ta/ETR)",
        ),
        (
            nome_norm.str.contains("COMPANHIA DE PESQUISA DE RECURSOS MINERAIS", na=False) & (uf_norm == "AM") & subs_norm.str.contains("NIOBIO", na=False),
            "Nióbio / Pirocloro e Fosfato (Carbonatitos)",
            "Morro dos Seis Lagos (SGB-CPRM — Nb/ETR)",
        ),
        (
            nome_norm.str.contains("COMPANHIA BRASILEIRA DE METALURGIA E MINERACAO", na=False) & (uf_norm == "MG"),
            "Nióbio / Pirocloro e Fosfato (Carbonatitos)",
            "Araxá (CBMM — Complexo Barreiro Nb/ETR)",
        ),
        (
            nome_norm.str.contains("DESENVOLVIMENTO ECONOMICO DE MINAS GERAIS|CODEMIG", na=False) & (uf_norm == "MG") & subs_norm.isin({"APATITA", "FOSFATO", "FOSFORITA (O)"}),
            "Nióbio / Pirocloro e Fosfato (Carbonatitos)",
            "Araxá / Patos de Minas (CODEMIG — Apatita/Fosfato)",
        ),
        (
            nome_norm.str.contains("ARAXA MINERACAO E METALURGIA|ARAXA METALS", na=False) & (uf_norm == "MG") & subs_norm.isin({"MINERIO DE NIOBIO", "NIOBIO", "FOSFATO"}),
            "Nióbio / Pirocloro e Fosfato (Carbonatitos)",
            "Araxá (St George / Araxá Metals — Nb/Fosfato)",
        ),
        (
            nome_norm.str.contains("CMOC BRASIL MINERACAO", na=False) & uf_norm.isin({"GO", "MG"}) & subs_norm.isin({"MINERIO DE NIOBIO", "NIOBIO", "FOSFATO"}),
            "Nióbio / Pirocloro e Fosfato (Carbonatitos)",
            "Catalão I & II / Ouvidor (CMOC — Nb/Fosfato)",
        ),
        (
            nome_norm.str.contains("MOSAIC FERTILIZANTES", na=False) & uf_norm.isin({"MG", "GO"}) & subs_norm.isin({"NIOBIO", "PIROCLORO", "MINERIO DE NIOBIO", "TITANIO", "MINERIO DE TITANIO"}),
            "Nióbio / Pirocloro e Fosfato (Carbonatitos)",
            "Tapira / Araxá / Catalão (Mosaic — Nb/Ti/Fosfato)",
        ),
        (
            nome_norm.str.contains("ENERGY FUELS BRAZIL", na=False) & (uf_norm == "BA"),
            "Titânio / Areias Pesadas Monazíticas",
            "Bahia Project (Energy Fuels — Ilmenita/Ti/Monazita)",
        ),
        (
            nome_norm.str.contains("INDUSTRIAS NUCLEARES DO BRASIL", na=False) & uf_norm.isin({"RJ", "ES"}) & subs_norm.isin({"ILMENITA", "MINERIO DE TITANIO"}),
            "Titânio / Areias Pesadas Monazíticas",
            "Buena / São Francisco de Itabapoana (INB — Monazita/Ilmenita)",
        ),
        (
            nome_norm.str.contains("TRONOX PIGMENTOS", na=False) & (uf_norm == "PB") & subs_norm.isin({"ILMENITA"}),
            "Titânio / Areias Pesadas Monazíticas",
            "Mataraca (Tronox — Areias Pesadas)",
        ),
        (
            nome_norm.str.contains("RIO GRANDE MINERACAO", na=False) & (uf_norm == "RS") & subs_norm.isin({"ILMENITA", "TITANIO", "MINERIO DE TITANIO"}) & fase_norm.str.contains("CONCESSAO DE LAVRA|REQUERIMENTO DE LAVRA", na=False),
            "Titânio / Areias Pesadas Monazíticas",
            "Retiro / Bujuru / São José do Norte (Rio Grande Mineração — Ti/Monazita)",
        ),
    ]

    # Atribuir cada registro no maximo a um complexo (prioridade pela ordem da lista) e excluir estritos
    assigned_chain = pd.Series(index=df_attr.index, dtype=object)
    assigned_complex = pd.Series(index=df_attr.index, dtype=object)
    for mask, chain_name, complex_name in coprod_specs:
        eligible = mask & (~strict_mask) & assigned_chain.isna()
        assigned_chain.loc[eligible] = chain_name
        assigned_complex.loc[eligible] = complex_name

    coprod_idx = df_attr.index[assigned_chain.notna()].tolist()
    gdf_coprod_raw = pyogrio.read_dataframe(shp_path, fids=coprod_idx)
    gdf_coprod_raw["coproduct_chain"] = assigned_chain.loc[coprod_idx].values
    gdf_coprod_raw["coproduct_complex"] = assigned_complex.loc[coprod_idx].values
    gdf_coprod = _enrich_anm_columns(gdf_coprod_raw, dataset_layer="coproduct_associated")

    coprod_out = DATA_PROCESSED / "anm_coproduct_polygons.parquet"
    gdf_coprod.to_parquet(coprod_out)
    logging.info(
        "ANM Coprodutos salvo: %d registros | %.1f ha declarados -> %s",
        len(gdf_coprod), gdf_coprod["area_ha"].sum(), coprod_out,
    )

    # Gravar metadados dinamicos do snapshot ANM ativo
    raw_meta = {}
    if shp_path == LOCAL_ANM_SHP_PATH and ANM_RAW_METADATA_PATH.exists():
        try:
            raw_meta = json.loads(ANM_RAW_METADATA_PATH.read_text(encoding="utf-8"))
        except Exception:
            raw_meta = {}

    shp_mtime = datetime.fromtimestamp(shp_path.stat().st_mtime, tz=timezone.utc)
    pt_months = {1: "jan", 2: "fev", 3: "mar", 4: "abr", 5: "mai", 6: "jun", 7: "jul", 8: "ago", 9: "set", 10: "out", 11: "nov", 12: "dez"}
    fallback_mon = pt_months[shp_mtime.month]
    fallback_abnt = "maio" if fallback_mon == "mai" else f"{fallback_mon}."
    try:
        rel_shp = shp_path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        rel_shp = shp_path.name
    snap_meta = {
        "shp_path": rel_shp,
        "origin": "local_raw_anm" if shp_path == LOCAL_ANM_SHP_PATH else "recursos_fallback",
        "snapshot_label": raw_meta.get("snapshot_label", f"{fallback_mon}/{shp_mtime.year}"),
        "snapshot_abnt": raw_meta.get("snapshot_abnt", f"{fallback_abnt} {shp_mtime.year}"),
        "snapshot_date_iso": raw_meta.get("snapshot_date_iso", shp_mtime.date().isoformat()),
        "shp_mtime_utc": shp_mtime.isoformat(),
        "last_modified_http": raw_meta.get("last_modified_http"),
        "downloaded_at_utc": raw_meta.get("downloaded_at_utc"),
        "sha256_zip": raw_meta.get("sha256"),
        "url": raw_meta.get("url"),
        "total_anm_rows": int(total_anm_rows),
        "strict_count": int(len(gdf_strict)),
        "strict_gross_ha": round(float(gdf_strict["area_ha"].sum()), 2),
        "strict_albers_ha": round(float(gdf_strict["area_albers_ha"].sum()), 2),
        "coproduct_count": int(len(gdf_coprod)),
        "coproduct_gross_ha": round(float(gdf_coprod["area_ha"].sum()), 2),
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    ANM_SNAPSHOT_META_PATH.write_text(json.dumps(snap_meta, ensure_ascii=False, indent=2), encoding="utf-8")
    logging.info("Metadados dinamicos do snapshot ANM gravados em %s", ANM_SNAPSHOT_META_PATH)
    return gdf_strict, gdf_coprod


def _parse_usgs_num(val) -> float | None:
    if val is None or pd.isna(val):
        return None
    s = str(val).strip().replace(",", "").replace(" ", "").replace(">", "").replace("<", "").replace("~", "")
    if s in {"", "-", "—", "NA", "W", "s", "Small"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _classify_geoblock(country: str) -> str:
    if country in BRICS_PLUS_SET:
        return "BRICS+"
    if country in GLOBAL_NORTH_SET:
        return "Norte Global"
    if country in {"Other countries", "World total"}:
        return "Outros USGS"
    return "Demais Sul Global"


def build_mcs2026_global() -> pd.DataFrame | None:
    """Processa USGS MCS 2026 (capitulo RARE EARTHS) se o CSV local estiver disponivel."""
    mcs_path = resolve_mcs_csv_source()
    if mcs_path is None:
        logging.info("MCS2026_Commodities_Data.csv ausente — pulando ETL global USGS MCS 2026 (nao exigido pelas figuras 1a, 1b, 6b, 12).")
        return None

    raw = pd.read_csv(mcs_path, low_memory=False, encoding="latin1")
    re_world = raw[
        (raw["MCS chapter"].astype(str).str.strip().str.upper() == "RARE EARTHS") &
        (raw["Section"].astype(str).str.contains("World Mine Production and Reserves", case=False, na=False))
    ].copy()

    re_world["Country"] = re_world["Country"].astype(str).str.strip()
    re_world["val_num"] = re_world["Value"].apply(_parse_usgs_num)
    re_world["Year_int"] = pd.to_numeric(re_world["Year"], errors="coerce").astype("Int64")

    prod_2024 = re_world[(re_world["Statistics"] == "Production") & (re_world["Year_int"] == 2024)].set_index("Country")["val_num"]
    prod_2025 = re_world[(re_world["Statistics"] == "Production") & (re_world["Year_int"] == 2025)].set_index("Country")["val_num"]
    res_2025 = re_world[(re_world["Statistics"] == "Reserves") & (re_world["Year_int"] == 2025)].set_index("Country")["val_num"]
    res_raw_str = re_world[(re_world["Statistics"] == "Reserves") & (re_world["Year_int"] == 2025)].set_index("Country")["Value"]

    countries = [c for c in re_world["Country"].unique() if c != "World total"]
    world_prod_2024 = float(prod_2024.get("World total", np.nan))
    world_prod_2025 = float(prod_2025.get("World total", np.nan))
    reported_res_sum = float(res_2025.drop(labels=["World total"], errors="ignore").dropna().sum())
    world_res_label = str(res_raw_str.get("World total", ">85,000,000"))

    country_pt_map = {
        "China": "China",
        "United States": "Estados Unidos",
        "Australia": "Austrália",
        "Burma": "Mianmar",
        "Thailand": "Tailândia",
        "India": "Índia",
        "Madagascar": "Madagascar",
        "Russia": "Rússia",
        "Brazil": "Brasil",
        "Nigeria": "Nigéria",
        "Vietnam": "Vietnã",
        "Malaysia": "Malásia",
        "Canada": "Canadá",
        "Greenland": "Groenlândia",
        "South Africa": "África do Sul",
        "Tanzania": "Tanzânia",
        "Other countries": "Outros países",
    }

    rows = []
    for c in countries:
        p24 = prod_2024.get(c)
        p25 = prod_2025.get(c)
        r25 = res_2025.get(c)
        p24_val = float(p24) if pd.notna(p24) else 0.0
        p25_val = float(p25) if pd.notna(p25) else 0.0
        r25_val = float(r25) if pd.notna(r25) else np.nan
        var_pct = ((p25_val - p24_val) / p24_val * 100.0) if p24_val > 0 else np.nan
        rp_years = (r25_val / p25_val) if (pd.notna(r25_val) and p25_val > 0) else np.nan

        rows.append({
            "country": c,
            "country_pt": country_pt_map.get(c, c),
            "geoblock": _classify_geoblock(c),
            "prod_2024_t": p24_val,
            "prod_2025_t": p25_val,
            "prod_2025_share_pct": round(p25_val / world_prod_2025 * 100.0, 3) if world_prod_2025 else 0.0,
            "prod_yoy_pct": round(var_pct, 2) if pd.notna(var_pct) else np.nan,
            "reserves_2025_t": r25_val,
            "reserves_2025_kt": round(r25_val / 1000.0, 2) if pd.notna(r25_val) else np.nan,
            "reserves_share_reported_pct": round(r25_val / reported_res_sum * 100.0, 3) if pd.notna(r25_val) else 0.0,
            "rp_ratio_years": round(rp_years, 1) if pd.notna(rp_years) else np.nan,
            "world_prod_2024_t": world_prod_2024,
            "world_prod_2025_t": world_prod_2025,
            "world_res_reported_sum_t": reported_res_sum,
            "world_res_usgs_label": world_res_label,
        })

    df_mcs = pd.DataFrame(rows).sort_values(["reserves_2025_t", "prod_2025_t"], ascending=[False, False], na_position="last").reset_index(drop=True)
    out_path = DATA_PROCESSED / "mcs2026_ree_global.parquet"
    df_mcs.to_parquet(out_path, index=False)
    logging.info(
        "MCS 2026 ETR salvo (%d paises | World Prod 2025: %.0f t | Soma Reservas Reportadas: %.2f Mt | Label USGS: %s) -> %s",
        len(df_mcs), world_prod_2025, reported_res_sum / 1e6, world_res_label, out_path,
    )
    return df_mcs


def main() -> None:
    build_basemaps()
    build_anm_strict_and_coproducts()
    build_mcs2026_global()


if __name__ == "__main__":
    main()
