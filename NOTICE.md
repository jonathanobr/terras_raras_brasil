# Notice & Third-Party Data Attribution (`terras_raras_brasil`)

Este repositório adota **licenciamento dual**:
- **Código-fonte (`src/`, `tests/`):** Licença **MIT** ([LICENSE](LICENSE)).
- **Mapas e figuras publicados (`output/figures/png/`):** Licença **Creative Commons Attribution 4.0 International (CC BY 4.0)** ([output/LICENSE](output/LICENSE)).

Nenhum direito autoral é reivindicado sobre os dados primários públicos consumidos pelo pipeline, cujas fontes oficiais e termos seguem atribuídos abaixo:

1. **ANM / SIGMINE — Processos Minerários Ativos (`BRASIL.shp`)**
   - Órgão: Agência Nacional de Mineração (ANM), Ministério de Minas e Energia (Brasil).
   - Fonte: `https://dadosabertos.anm.gov.br/SIGMINE/PROCESSOS_MINERARIOS/BRASIL.zip`
   - Regime: Dados Abertos Governamentais do Brasil (Lei nº 12.527/2011; Decreto nº 8.777/2016).

2. **MDIC / Comex Stat — Estatísticas de Comércio Exterior (`2805.30`, `2846.10`, `2846.90`, `8505.11`)**
   - Órgão: Ministério do Desenvolvimento, Indústria, Comércio e Serviços (MDIC, Brasil).
   - Fonte: `https://api-comexstat.mdic.gov.br/general` e `https://balanca.economia.gov.br/balanca/bd/comexstat-bd`
   - Regime: Dados públicos oficiais brasileiros com atribuição de fonte ao MDIC.

3. **IBGE — Malha Territorial Estadual (`BR_UF_2025`)**
   - Órgão: Instituto Brasileiro de Geografia e Estatística (IBGE).
   - Fonte: `https://geoftp.ibge.gov.br/organizacao_do_territorio/malhas_territoriais/malhas_municipais/municipio_2025/Brasil/BR_UF_2025.zip`
   - Regime: Dados públicos oficiais brasileiros com atribuição de fonte ao IBGE.

4. **Marine Regions / VLIZ — Zona Econômica Exclusiva Brasileira (Amazônia Azul)**
   - Fonte: Flanders Marine Institute (VLIZ), *Maritime Boundaries Geodatabase*.
   - Serviço WFS: `https://geo.vliz.be/geoserver/MarineRegions/ows`
   - Licença upstream: **CC BY 4.0** (compatível com os mapas publicados sob CC BY 4.0 mediante citação explícita).

5. **Natural Earth — Limites Nacionais (`ne_110m_admin_0_countries`)**
   - Fonte: Natural Earth (`https://www.naturalearthdata.com/`).
   - Regime: Domínio Público.

6. **Projetos Âncora e Ativos Midstream (`data/curated/anchor_projects_and_midstream.json`)**
   - Compilação factual elaborada a partir de relatórios técnicos públicos (JORC 2012, NI 43-101, SGB-CPRM, CETEM, FIEMG/SENAI, ANM e comunicados societários públicos ASX/TSX/NYSE).
