# src/fetch_online_sources.py
"""
Etapa 3: Download oficial online (fail-loud, zero dados sinteticos) das bases complementares:
  1. Comex Stat / MDIC (NCMs 2805.30, 2846.10, 2846.90 e 8505.11 — 2015-2026)
  2. Biomas do Brasil (IBGE 1:250.000 / espelho oficial IPEA geobr)
  3. Unidades de Conservacao (MMA CNUC / ICMBio / espelho oficial IPEA geobr)
  4. Terras Indigenas (FUNAI GeoServer WFS / espelho oficial IPEA geobr)

Registra URL, status, SHA-256, cobertura e timestamp em `data/raw/download_manifest.json`.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import sys
import urllib3
import zipfile
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import geopandas as gpd
import pandas as pd
import pyogrio
import requests

from src.config import DATA_PROCESSED, DATA_RAW, RAW_BASEMAPS_DIR, SIRGAS_2000_GEO

IBGE_UF_URL = (
    "https://geoftp.ibge.gov.br/organizacao_do_territorio/malhas_territoriais/"
    "malhas_municipais/municipio_2025/Brasil/BR_UF_2025.zip"
)
VLIZ_EEZ_URL = (
    "https://geo.vliz.be/geoserver/MarineRegions/ows"
    "?service=WFS&version=1.0.0&request=GetFeature"
    "&typeName=MarineRegions:eez&cql_filter=iso_sov1=%27BRA%27&outputFormat=application/json"
)
NE_COUNTRIES_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/"
    "geojson/ne_110m_admin_0_countries.geojson"
)

logging.basicConfig(level=logging.INFO, format="[FETCH ONLINE] %(levelname)s: %(message)s")
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

MANIFEST_PATH = DATA_RAW / "download_manifest.json"
ANM_RAW_METADATA_PATH = DATA_RAW / "anm_metadata.json"
_PT_MONTHS_SHORT = {
    1: "jan", 2: "fev", 3: "mar", 4: "abr", 5: "mai", 6: "jun",
    7: "jul", 8: "ago", 9: "set", 10: "out", 11: "nov", 12: "dez",
}
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TerrasRarasBrasil-DataPaper/1.0",
    "Accept": "application/json, text/csv, application/octet-stream, */*",
}

TARGET_SH6 = ("280530", "284610", "284690", "850511")
NCM_GROUP_MAP = {
    "280530": "Metais de Terras Raras, Escândio e Ítrio (2805.30)",
    "284610": "Compostos de Cério (2846.10)",
    "284690": "Outros Compostos de ETR / Ítrio / Escândio (2846.90)",
    "850511": "Ímãs Permanentes Metálicos / NdFeB (8505.11)",
}


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _download_bytes(url: str, timeout: int = 90) -> tuple[bytes, bool]:
    """Tenta baixar URL com TLS verificado; para hosts .gov.br com ICP-Brasil, permite fallback verify=False."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        return r.content, True
    except requests.exceptions.SSLError:
        if ".gov.br" in url:
            logging.warning("SSLError em host .gov.br (%s); tentando novamente com verify=False.", url)
            r = requests.get(url, headers=HEADERS, timeout=timeout, verify=False)
            r.raise_for_status()
            return r.content, False
        raise


def _download_to_file(url: str, dest: Path, timeout: int = 180) -> dict[str, Any]:
    """Faz download streaming para disco e retorna metadados para o manifesto."""
    verify_ssl = True
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout, stream=True)
        r.raise_for_status()
    except requests.exceptions.SSLError:
        if ".gov.br" in url:
            logging.warning("SSLError em %s; fallback verify=False para host .gov.br.", url)
            verify_ssl = False
            r = requests.get(url, headers=HEADERS, timeout=timeout, stream=True, verify=False)
            r.raise_for_status()
        else:
            raise

    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as f:
        for chunk in r.iter_content(chunk_size=1024 * 512):
            if chunk:
                f.write(chunk)

    return {
        "url": url,
        "file": str(dest.relative_to(DATA_RAW.parent.parent)),
        "size_bytes": dest.stat().st_size,
        "sha256": _sha256_file(dest),
        "ssl_verified": verify_ssl,
        "downloaded_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def _fetch_geobr_metadata() -> pd.DataFrame:
    """Consulta a tabela oficial de metadados do pacote IPEA geobr."""
    meta_urls = [
        "https://www.ipea.gov.br/geobr/metadata/metadata_1.7.0_gpkg.csv",
        "https://www.ipea.gov.br/geobr/metadata/metadata_gpkg.csv",
        "https://raw.githubusercontent.com/ipeaGIT/geobr/master/metadata/metadata_1.7.0_gpkg.csv",
    ]
    for u in meta_urls:
        try:
            content, _ = _download_bytes(u, timeout=30)
            df = pd.read_csv(io.BytesIO(content))
            if not df.empty and "geo" in df.columns:
                logging.info("Metadata oficial IPEA/geobr carregado de %s (%d linhas).", u, len(df))
                return df
        except Exception as exc:
            logging.debug("Falha ao ler metadata geobr %s: %s", u, exc)
    return pd.DataFrame()


def _geobr_urls_for(meta: pd.DataFrame, geo_key: str) -> list[str]:
    if meta.empty or "geo" not in meta.columns or "download_path" not in meta.columns:
        return []
    sub = meta[meta["geo"].astype(str).str.strip() == geo_key].copy()
    if sub.empty:
        return []
    if "year" in sub.columns:
        sub["year_num"] = pd.to_numeric(sub["year"], errors="coerce").fillna(0)
        sub = sub.sort_values("year_num", ascending=False)
    return [str(u).strip() for u in sub["download_path"].dropna().tolist()]


def _read_vector_from_zip_or_file(path: Path) -> tuple[gpd.GeoDataFrame, Path]:
    with open(path, "rb") as f:
        head = f.read(32)
    if head.startswith(b"PK\x03\x04"):
        zip_path = path.with_suffix(".zip")
        if path != zip_path:
            path.replace(zip_path)
            path = zip_path
        with zipfile.ZipFile(path, "r") as zf:
            names = zf.namelist()
            shps = [n for n in names if n.lower().endswith(".shp")]
            gpkgs = [n for n in names if n.lower().endswith(".gpkg")]
            geojsons = [n for n in names if n.lower().endswith((".geojson", ".json"))]
            target = shps[0] if shps else (gpkgs[0] if gpkgs else (geojsons[0] if geojsons else None))
            if target is None:
                raise RuntimeError(f"Nenhum .shp/.gpkg/.geojson encontrado em {path}")
            return gpd.read_file(f"zip://{path}!{target}"), path
    if head.startswith(b"SQLite format 3"):
        gpkg_path = path.with_suffix(".gpkg")
        if path != gpkg_path:
            path.replace(gpkg_path)
            path = gpkg_path
        return gpd.read_file(path), path
    if head.lstrip().startswith((b"{", b"[")):
        json_path = path.with_suffix(".geojson")
        if path != json_path:
            path.replace(json_path)
            path = json_path
        return gpd.read_file(path), path
    return gpd.read_file(path), path


def fetch_comex_stat() -> dict[str, Any]:
    """
    Baixa dados oficiais de comercio exterior (2015-2026) do Comex Stat / MDIC.
    Estrategia A: API JSON oficial (`api-comexstat.mdic.gov.br/general`).
    Estrategia B: CSVs anuais oficiais (`balanca.economia.gov.br/balanca/bd/comexstat-bd/ncm/`).
    """
    logging.info("Iniciando coleta oficial Comex Stat / MDIC para SH6 %s...", TARGET_SH6)
    records: list[dict[str, Any]] = []
    api_url = "https://api-comexstat.mdic.gov.br/general"
    api_ok = True
    years = list(range(2015, 2027))

    # Testar API oficial por ano/fluxo (evita timeouts e limites de pagina)
    try:
        for flow in ("import", "export"):
            for yr in years:
                payload = {
                    "flow": flow,
                    "monthDetail": False,
                    "period": {"from": f"{yr}-01", "to": f"{yr}-12"},
                    "filters": [{"filter": "heading", "values": ["2805", "2846", "8505"]}],
                    "details": ["ncm", "country", "state"],
                    "metrics": ["metricFOB", "metricKG"],
                }
                try:
                    resp = requests.post(api_url, json=payload, headers=HEADERS, timeout=60, verify=False)
                    if resp.status_code == 400:
                        # Fallback de filtro para sh6 caso heading nao seja aceito
                        payload["filters"] = [{"filter": "ncm", "values": [
                            "28053000", "28053010", "28053090",
                            "28461010", "28461090", "28469010", "28469020", "28469030", "28469090",
                            "85051100",
                        ]}]
                        resp = requests.post(api_url, json=payload, headers=HEADERS, timeout=60, verify=False)
                    resp.raise_for_status()
                    data_json = resp.json()
                    rows = data_json.get("data", {}).get("list", []) if isinstance(data_json, dict) else []
                    for r in rows:
                        ncm_code = str(r.get("coNcm") or r.get("ncm") or "").strip().zfill(8)
                        if not ncm_code.startswith(TARGET_SH6):
                            continue
                        sh6 = ncm_code[:6]
                        records.append({
                            "year": int(r.get("year") or yr),
                            "flow": "Importação" if flow == "import" else "Exportação",
                            "ncm": ncm_code,
                            "sh6": sh6,
                            "ncm_desc": str(r.get("noNcmpt") or r.get("ncmDesc") or r.get("ncm") or "").strip(),
                            "ncm_group": NCM_GROUP_MAP.get(sh6, sh6),
                            "country": str(r.get("noPais") or r.get("country") or "Não Informado").strip(),
                            "uf": str(r.get("sgUf") or r.get("state") or "").strip(),
                            "fob_usd": float(r.get("metricFOB") or 0.0),
                            "net_kg": float(r.get("metricKG") or 0.0),
                        })
                except Exception as yr_exc:
                    if yr == 2026:
                        logging.info("Ano 2026 ainda indisponivel/parcial na API Comex Stat (%s).", yr_exc)
                    else:
                        raise
    except Exception as api_exc:
        logging.warning("API Comex Stat indisponivel ou recusou consulta (%s). Acionando fallback oficial CSV MDIC...", api_exc)
        api_ok = False
        records = []

    if api_ok and len(records) > 20:
        df = pd.DataFrame(records)
        raw_cache = DATA_RAW / "comexstat_api_2015_2026.json"
        df.to_json(raw_cache, orient="records", force_ascii=False, indent=2)
        out_pq = DATA_PROCESSED / "comex_ree_magnets.parquet"
        df.to_parquet(out_pq, index=False)
        logging.info("Comex Stat API salvo (%d registros, anos %s-%s) -> %s", len(df), df["year"].min(), df["year"].max(), out_pq)
        return {
            "source": "MDIC / Comex Stat API (api-comexstat.mdic.gov.br)",
            "file": str(raw_cache.relative_to(DATA_RAW.parent.parent)),
            "records": int(len(df)),
            "years": sorted(df["year"].unique().tolist()),
            "sha256": _sha256_file(raw_cache),
            "downloaded_at_utc": datetime.now(timezone.utc).isoformat(),
        }

    # Estrategia B: CSVs oficiais mensais consolidados por ano do MDIC
    base_csv_url = "https://balanca.economia.gov.br/balanca/bd/comexstat-bd"
    pais_bytes, _ = _download_bytes(f"{base_csv_url}/tabelas/PAIS.csv", timeout=60)
    ncm_bytes, _ = _download_bytes(f"{base_csv_url}/tabelas/NCM.csv", timeout=60)
    df_pais = pd.read_csv(io.BytesIO(pais_bytes), sep=";", encoding="latin1", dtype=str)
    df_ncm = pd.read_csv(io.BytesIO(ncm_bytes), sep=";", encoding="latin1", dtype=str)
    pais_map = dict(zip(df_pais["CO_PAIS"].str.strip(), df_pais["NO_PAIS"].str.strip()))
    ncm_map = dict(zip(df_ncm["CO_NCM"].str.strip().str.zfill(8), df_ncm["NO_NCM_POR"].str.strip()))

    csv_frames = []
    downloaded_years = []
    for yr in years:
        for prefix, flow_pt in (("IMP", "Importação"), ("EXP", "Exportação")):
            url = f"{base_csv_url}/ncm/{prefix}_{yr}.csv"
            try:
                r = requests.get(url, headers=HEADERS, timeout=180, stream=True, verify=False)
                if r.status_code == 404 and yr == 2026:
                    continue
                r.raise_for_status()
                chunks = []
                for ch in pd.read_csv(r.raw, sep=";", dtype={"CO_NCM": str, "CO_PAIS": str, "SG_UF_NCM": str}, chunksize=250_000):
                    ch["CO_NCM"] = ch["CO_NCM"].astype(str).str.strip().str.zfill(8)
                    sub = ch[ch["CO_NCM"].str.startswith(TARGET_SH6)].copy()
                    if not sub.empty:
                        sub["flow"] = flow_pt
                        chunks.append(sub)
                if chunks:
                    df_yr = pd.concat(chunks, ignore_index=True)
                    csv_frames.append(df_yr)
                    downloaded_years.append(f"{prefix}_{yr}")
                    logging.info("Comex Stat CSV %s_%d: %d linhas filtradas.", prefix, yr, len(df_yr))
            except Exception as exc:
                if yr == 2026:
                    logging.info("CSV %s_%d ainda nao publicado (%s).", prefix, yr, exc)
                else:
                    raise RuntimeError(f"Falha ao baixar CSV oficial MDIC {url}: {exc}") from exc

    if not csv_frames:
        raise RuntimeError("Nenhuma serie Comex Stat / MDIC foi obtida de API ou CSVs oficiais.")

    df_all = pd.concat(csv_frames, ignore_index=True)
    df_all["year"] = pd.to_numeric(df_all["CO_ANO"], errors="coerce").astype(int)
    df_all["ncm"] = df_all["CO_NCM"].astype(str).str.strip().str.zfill(8)
    df_all["sh6"] = df_all["ncm"].str[:6]
    df_all["ncm_desc"] = df_all["ncm"].map(ncm_map).fillna(df_all["ncm"])
    df_all["ncm_group"] = df_all["sh6"].map(NCM_GROUP_MAP).fillna(df_all["sh6"])
    df_all["country"] = df_all["CO_PAIS"].astype(str).str.strip().map(pais_map).fillna("Não Informado")
    df_all["uf"] = df_all["SG_UF_NCM"].astype(str).str.strip()
    df_all["fob_usd"] = pd.to_numeric(df_all["VL_FOB"], errors="coerce").fillna(0.0)
    df_all["net_kg"] = pd.to_numeric(df_all["KG_LIQUIDO"], errors="coerce").fillna(0.0)

    agg = (
        df_all.groupby(["year", "flow", "ncm", "sh6", "ncm_desc", "ncm_group", "country", "uf"], as_index=False)
        [["fob_usd", "net_kg"]]
        .sum()
    )
    raw_csv = DATA_RAW / "comexstat_filtered_2015_2026.csv"
    agg.to_csv(raw_csv, index=False, encoding="utf-8-sig")
    out_pq = DATA_PROCESSED / "comex_ree_magnets.parquet"
    agg.to_parquet(out_pq, index=False)
    logging.info("Comex Stat CSV oficial consolidado (%d linhas agregadas) -> %s", len(agg), out_pq)
    return {
        "source": "MDIC / Comex Stat Bulk CSV (balanca.economia.gov.br)",
        "file": str(raw_csv.relative_to(DATA_RAW.parent.parent)),
        "records": int(len(agg)),
        "years": sorted(agg["year"].unique().tolist()),
        "sha256": _sha256_file(raw_csv),
        "downloaded_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def fetch_vector_layer(
    name: str,
    candidate_urls: list[str],
    dest_filename: str,
    out_parquet: Path,
    expected_min_rows: int,
) -> dict[str, Any]:
    """Tenta baixar camada vetorial oficial de uma lista ordenada de URLs (fail-loud se todas falharem)."""
    dest = DATA_RAW / dest_filename
    last_err: Exception | None = None

    for url in candidate_urls:
        try:
            logging.info("[%s] Tentando download oficial: %s", name, url)
            info = _download_to_file(url, dest, timeout=240)
            if info["size_bytes"] < 5_000:
                raise RuntimeError(f"Arquivo baixado suspeito/pequeno ({info['size_bytes']} bytes)")
            gdf, actual_dest = _read_vector_from_zip_or_file(dest)
            info["file"] = str(actual_dest.relative_to(DATA_RAW.parent.parent))
            if len(gdf) < expected_min_rows:
                raise RuntimeError(f"Contagem abaixo do minimo ({len(gdf)} < {expected_min_rows})")
            if gdf.crs is None:
                gdf = gdf.set_crs(SIRGAS_2000_GEO)
            else:
                gdf = gdf.to_crs(SIRGAS_2000_GEO)
            gdf["geometry"] = gdf.geometry.make_valid()
            gdf = gdf[~gdf.geometry.is_empty & gdf.geometry.notna()].copy()
            gdf.to_parquet(out_parquet)
            info["layer"] = name
            info["rows"] = int(len(gdf))
            info["columns"] = [str(c) for c in gdf.columns if c != "geometry"]
            logging.info("[%s] OK: %d feicoes salvas em %s", name, len(gdf), out_parquet)
            return info
        except Exception as exc:
            last_err = exc
            logging.warning("[%s] Falha em %s: %s", name, url, exc)

    raise RuntimeError(f"[{name}] FALHA CRÍTICA: todas as fontes oficiais falharam. Último erro: {last_err}")


def _format_pt_month_labels(dt: datetime) -> tuple[str, str]:
    mon = _PT_MONTHS_SHORT[dt.month]
    abnt = "maio" if mon == "mai" else f"{mon}."
    return f"{mon}/{dt.year}", f"{abnt} {dt.year}"


def fetch_anm_shapefile(dest_dir: Path | None = None) -> dict[str, Any]:
    """Baixa e extrai o shapefile consolidado BRASIL.zip mais recente da ANM/SIGMINE."""
    target_dir = dest_dir or (DATA_RAW / "anm")
    target_dir.mkdir(parents=True, exist_ok=True)
    zip_dest = target_dir / "BRASIL.zip"

    candidate_urls = [
        "https://dadosabertos.anm.gov.br/SIGMINE/PROCESSOS_MINERARIOS/BRASIL.zip",
        "https://app.anm.gov.br/dadosabertos/SIGMINE/PROCESSOS_MINERARIOS/BRASIL.zip",
    ]
    last_err: Exception | None = None

    for url in candidate_urls:
        try:
            logging.info("[ANM SIGMINE] Baixando shapefile oficial: %s", url)
            verify_ssl = True
            try:
                r = requests.get(url, headers=HEADERS, timeout=300, stream=True)
                r.raise_for_status()
            except requests.exceptions.SSLError:
                verify_ssl = False
                logging.warning("[ANM SIGMINE] SSLError em %s; tentando verify=False para host .gov.br.", url)
                r = requests.get(url, headers=HEADERS, timeout=300, stream=True, verify=False)
                r.raise_for_status()

            http_last_mod = r.headers.get("Last-Modified")
            final_url = str(r.url)
            with open(zip_dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)

            zip_size = zip_dest.stat().st_size
            if zip_size < 50_000_000:
                raise RuntimeError(f"Arquivo BRASIL.zip suspeito/incompleto ({zip_size} bytes < 50 MB)")
            with open(zip_dest, "rb") as f:
                if f.read(4) != b"PK\x03\x04":
                    raise RuntimeError("Cabecalho ZIP invalido em BRASIL.zip")

            zip_sha256 = _sha256_file(zip_dest)
            shp_zip_dt: datetime | None = None
            extracted_files: list[str] = []
            with zipfile.ZipFile(zip_dest, "r") as zf:
                members = [m for m in zf.infolist() if not m.is_dir()]
                brasil_members = [m for m in members if Path(m.filename).name.upper().startswith("BRASIL.")]
                if not brasil_members:
                    raise RuntimeError("BRASIL.shp nao encontrado dentro de BRASIL.zip")
                for m in brasil_members:
                    base_name = Path(m.filename).name
                    out_p = target_dir / base_name
                    with zf.open(m, "r") as src_f, open(out_p, "wb") as dst_f:
                        while True:
                            buf = src_f.read(1024 * 1024)
                            if not buf:
                                break
                            dst_f.write(buf)
                    extracted_files.append(base_name)
                    if base_name.upper() == "BRASIL.SHP":
                        shp_zip_dt = datetime(*m.date_time, tzinfo=timezone.utc)

            shp_path = target_dir / "BRASIL.shp"
            if not shp_path.exists():
                raise RuntimeError("Extracao de BRASIL.shp falhou")

            http_dt: datetime | None = None
            if http_last_mod:
                try:
                    http_dt = parsedate_to_datetime(http_last_mod)
                    if http_dt.tzinfo is None:
                        http_dt = http_dt.replace(tzinfo=timezone.utc)
                except Exception:
                    http_dt = None

            dl_dt = datetime.now(timezone.utc)
            ref_dt = http_dt or shp_zip_dt or dl_dt
            snapshot_label, snapshot_abnt = _format_pt_month_labels(ref_dt)
            info_shp = pyogrio.read_info(shp_path)
            total_anm_rows = int(info_shp.get("features", 0))
            if total_anm_rows < 200_000:
                raise RuntimeError(f"Contagem inesperadamente baixa no shapefile ANM: {total_anm_rows}")

            meta = {
                "source": "ANM / SIGMINE (BRASIL.zip)",
                "url": url,
                "final_url": final_url,
                "ssl_verified": verify_ssl,
                "file_zip": str(zip_dest.relative_to(DATA_RAW.parent.parent)),
                "file_shp": str(shp_path.relative_to(DATA_RAW.parent.parent)),
                "extracted_files": sorted(extracted_files),
                "zip_size_bytes": int(zip_size),
                "shp_size_bytes": int(shp_path.stat().st_size),
                "sha256": zip_sha256,
                "last_modified_http": http_last_mod,
                "shp_zip_date_utc": shp_zip_dt.isoformat() if shp_zip_dt else None,
                "downloaded_at_utc": dl_dt.isoformat(),
                "total_anm_rows": total_anm_rows,
                "snapshot_date_iso": ref_dt.date().isoformat(),
                "snapshot_label": snapshot_label,
                "snapshot_abnt": snapshot_abnt,
            }
            with open(ANM_RAW_METADATA_PATH, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
            logging.info(
                "[ANM SIGMINE] OK: %s registros | corte %s | %.1f MB ZIP -> %s",
                f"{total_anm_rows:,}", snapshot_label, zip_size / 1e6, shp_path,
            )
            return meta
        except Exception as exc:
            last_err = exc
            logging.warning("[ANM SIGMINE] Falha em %s: %s", url, exc)

    raise RuntimeError(f"[ANM SIGMINE] FALHA CRÍTICA: todas as URLs oficiais falharam. Último erro: {last_err}")


def _update_manifest(updates: dict[str, Any]) -> None:
    existing: dict[str, Any] = {"generated_at_utc": datetime.now(timezone.utc).isoformat(), "sources": {}}
    if MANIFEST_PATH.exists():
        try:
            existing = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    existing["generated_at_utc"] = datetime.now(timezone.utc).isoformat()
    existing.setdefault("sources", {}).update(updates)
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    logging.info("Manifesto completo de downloads salvo em %s", MANIFEST_PATH)


def fetch_basemaps_raw() -> dict[str, Any]:
    """Baixa as malhas de referencia (IBGE UF 2025, Amazonia Azul VLIZ e Natural Earth 110m)."""
    RAW_BASEMAPS_DIR.mkdir(parents=True, exist_ok=True)
    return {
        "ibge_uf_2025": _download_to_file(IBGE_UF_URL, RAW_BASEMAPS_DIR / "BR_UF_2025.zip", timeout=180),
        "brazil_eez": _download_to_file(VLIZ_EEZ_URL, RAW_BASEMAPS_DIR / "brazil_eez.geojson", timeout=120),
        "ne_110m_countries": _download_to_file(NE_COUNTRIES_URL, RAW_BASEMAPS_DIR / "ne_110m_countries.geojson", timeout=90),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Download oficial das bases online do projeto terras_raras_brasil.")
    parser.add_argument("--only-anm", action="store_true", help="Baixa apenas o shapefile atualizado da ANM/SIGMINE.")
    parser.add_argument("--include-socioenv", action="store_true", help="Tambem baixa Biomas IBGE, UCs CNUC e TIs FUNAI.")
    args = parser.parse_args()

    if args.only_anm:
        anm_meta = fetch_anm_shapefile()
        _update_manifest({"anm_sigmine": anm_meta})
        return

    new_sources: dict[str, Any] = {}

    # 0. ANM / SIGMINE Shapefile Consolidado
    new_sources["anm_sigmine"] = fetch_anm_shapefile()

    # 1. Basemaps IBGE UF 2025, Marine Regions EEZ e Natural Earth
    new_sources.update(fetch_basemaps_raw())

    # 2. Comex Stat (Fig. 12)
    new_sources["comex_stat"] = fetch_comex_stat()

    if not args.include_socioenv:
        _update_manifest(new_sources)
        logging.info("Coleta publicada concluida (ANM + basemaps + Comex Stat). Use --include-socioenv para Biomas/UCs/TIs.")
        return

    meta_geobr = _fetch_geobr_metadata()

    # 3. Biomas IBGE 1:250.000
    biome_urls = [
        "https://geoftp.ibge.gov.br/informacoes_ambientais/estudos_ambientais/biomas/vetores/Biomas_250mil.zip",
        *_geobr_urls_for(meta_geobr, "biomes"),
    ]
    new_sources["biomes"] = fetch_vector_layer(
        name="Biomas IBGE 1:250.000",
        candidate_urls=biome_urls,
        dest_filename="biomas_ibge_250mil.bin",
        out_parquet=DATA_PROCESSED / "socioenv_biomes.parquet",
        expected_min_rows=6,
    )

    # 4. Unidades de Conservacao (CNUC/MMA via geobr + ICMBio Federal GeoServer)
    uc_urls = [
        *_geobr_urls_for(meta_geobr, "conservation_units"),
        "https://geoservicos.inde.gov.br/geoserver/ICMBio/ows?service=WFS&version=1.0.0&request=GetFeature&typeName=ICMBio:limiteucsfederais_a&outputFormat=application/json",
    ]
    new_sources["conservation_units"] = fetch_vector_layer(
        name="Unidades de Conservação (CNUC/ICMBio)",
        candidate_urls=uc_urls,
        dest_filename="ucs_cnuc_icmbio.bin",
        out_parquet=DATA_PROCESSED / "socioenv_ucs.parquet",
        expected_min_rows=200,
    )

    # 5. Terras Indigenas (FUNAI GeoServer WFS + espelho oficial geobr)
    ti_urls = [
        "https://geoserver.funai.gov.br/geoserver/Funai/ows?service=WFS&version=1.0.0&request=GetFeature&typeName=Funai:tis_poligonais&outputFormat=application/json",
        "https://geoserver.funai.gov.br/geoserver/Funai/ows?service=WFS&version=1.0.0&request=GetFeature&typeName=Funai:tis_poligonais&outputFormat=SHAPE-ZIP",
        *_geobr_urls_for(meta_geobr, "indigenous_land"),
    ]
    new_sources["indigenous_lands"] = fetch_vector_layer(
        name="Terras Indígenas (FUNAI)",
        candidate_urls=ti_urls,
        dest_filename="tis_funai.bin",
        out_parquet=DATA_PROCESSED / "socioenv_tis.parquet",
        expected_min_rows=400,
    )

    _update_manifest(new_sources)


if __name__ == "__main__":
    main()
