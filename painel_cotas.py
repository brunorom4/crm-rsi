"""
painel_cotas.py

Funções de dados do Painel de Cotas -- sem nenhuma dependência do Flask
nem do banco do CRM (crm.db). É só leitura/validação do CSV consolidado
e montagem das estruturas que o template usa. As rotas em si (login,
upload, render_template) ficam no app.py, seguindo o mesmo padrão do
Painel de Resultados (Brownfield).
"""

import os
import json
import shutil
import sqlite3
import hashlib
import unicodedata
from datetime import datetime

import pandas as pd

# Mapa prefixo do Bucket -> nome de cidade legível. Isto é só o valor
# SEMENTE (seed) inicial -- o mapa real, editável pelo ADM na página de
# config, fica persistido em mapa_cidades.json (ver carregar_mapa_cidades /
# salvar_mapa_cidades mais abaixo). Prefixos novos que aparecerem num CSV
# importado (cidade nova na extração) entram automaticamente na tela de
# config pra você preencher o nome, em vez de eu adivinhar errado.
MAPA_CIDADES_PADRAO = {
    "CAS": "Campinas",
    "STS": "Santos",
    "RIB": "Ribeirão Preto",
    "SJC": "São José dos Campos",
    "SOR": "Sorocaba",
}

# Classes conhecidas do Painel de Cotas (chave -> rótulo), mesmas chaves
# usadas no CLASSE_CHECKBOX_MAP do 1_download_cotas.py.
CLASSES_DISPONIVEIS = [
    ("1", "Classe 1"),
    ("1pme", "Classe 1 (PME)"),
    ("3", "Classe 3"),
    ("7", "Classe 7"),
    ("15", "Classe 15"),
    ("4", "Classe 4"),
    ("5", "Classe 5"),
    ("5pme", "Classe 5 (PME)"),
    ("5pmesla", "Classe 5 (PME SLA)"),
    ("8", "Classe 8"),
    ("9", "Classe 9"),
    ("10", "Classe 10"),
    ("12", "Classe 12"),
    ("14", "Classe 14"),
]

CONFIG_EXTRACAO_PADRAO = {
    "cidades": ["CAMPINAS", "SANTOS", "RIBEIRAO PRETO", "SAO JOSE DOS CAMPOS", "SOROCABA"],
    "classes": ["1", "15"],
    # cidades mostradas nos cards de resumo (nomes "bonitos", como aparecem
    # no painel) -- independente da lista de extração acima, que pode
    # crescer sem que os cards mudem.
    "cidades_cards": ["Campinas", "Santos", "Ribeirão Preto", "São José dos Campos", "Sorocaba"],
}

ORDEM_JANELA = ["08:00 - 12:00", "12:00 - 15:00", "15:00 - 18:00"]

# horário de início/fim de cada janela, pra calcular a próxima janela
# aberta a partir de agora
JANELA_INICIO = {"08:00 - 12:00": (8, 0), "12:00 - 15:00": (12, 0), "15:00 - 18:00": (15, 0)}
JANELA_FIM = {"08:00 - 12:00": (12, 0), "12:00 - 15:00": (15, 0), "15:00 - 18:00": (18, 0)}

COLUNAS_OBRIGATORIAS = [
    "Bucket", "Classe", "Data", "Janela", "% Utilizado", "Status Janela",
    "Atividades Disp.",
]


def nome_cidade(bucket: str, mapa: dict = None) -> str:
    """Traduz o prefixo do Bucket pro nome de cidade, usando `mapa` (o
    mapa configurável). Se o prefixo não estiver mapeado ainda, volta uma
    versão só limpa do prefixo (não o Bucket inteiro) -- legível, mas
    deixa claro que ainda falta cadastrar o nome de verdade."""
    if mapa is None:
        mapa = MAPA_CIDADES_PADRAO
    prefixo = bucket.split("_")[0]
    if prefixo in mapa:
        return mapa[prefixo]
    return f"{prefixo} (sem nome cadastrado)"


def prefixos_do_csv(caminho_csv: str) -> list:
    """Lista os prefixos de Bucket distintos que aparecem no CSV atual,
    em ordem alfabética -- usado pra montar a tabela de mapeamento de
    cidades na página de config."""
    if not os.path.exists(caminho_csv):
        return []
    try:
        df = pd.read_csv(caminho_csv, usecols=["Bucket"])
        prefixos = sorted({b.split("_")[0] for b in df["Bucket"].dropna().unique()})
        return prefixos
    except Exception:
        return []


def classes_do_csv(caminho_csv: str) -> list:
    """Valores distintos da coluna Classe do CSV atual ("CLASSE1",
    "CLASSE15", ...), em ordem -- pro combo de filtro do histórico. Lê só
    essa coluna (antes a rota carregava e consolidava o arquivo inteiro
    só pra tirar essa lista, o que custava quase 1s por acesso)."""
    if not os.path.exists(caminho_csv):
        return []
    try:
        df = pd.read_csv(caminho_csv, usecols=["Classe"])
        return sorted(df["Classe"].dropna().unique())
    except Exception:
        return []


def carregar_mapa_cidades(caminho_json: str) -> dict:
    """Mapa persistido prefixo -> cidade. Nunca lança exceção; se não
    existir ou estiver corrompido, volta o padrão (as 5 originais)."""
    if not os.path.exists(caminho_json):
        return dict(MAPA_CIDADES_PADRAO)
    try:
        with open(caminho_json, "r", encoding="utf-8") as f:
            mapa = json.load(f)
        return mapa if isinstance(mapa, dict) else dict(MAPA_CIDADES_PADRAO)
    except Exception:
        return dict(MAPA_CIDADES_PADRAO)


def salvar_mapa_cidades(caminho_json: str, mapa: dict) -> dict:
    pasta = os.path.dirname(caminho_json)
    if pasta:
        os.makedirs(pasta, exist_ok=True)
    with open(caminho_json, "w", encoding="utf-8") as f:
        json.dump(mapa, f, ensure_ascii=False, indent=2)
    return mapa


def _status_combinado(serie_status: pd.Series) -> str:
    """Quando uma cidade tem mais de uma área (ex: Guarujá = GUA_Area_01 +
    GUA_Area_02) e ambas caem no mesmo dia/janela/classe, considera Aberta
    se PELO MENOS UMA área estiver aberta -- ainda dá pra agendar ali."""
    aberta = serie_status.astype(str).str.strip().str.lower().eq("aberta").any()
    return "Aberta" if aberta else "Fechada"


def _agregar_areas(df: pd.DataFrame) -> pd.DataFrame:
    """Consolida linhas de áreas diferentes da MESMA cidade (já traduzida
    pelo prefixo) no mesmo dia/janela/classe: status combinado (any-aberta)
    e Atividades Disp. somadas (representa a capacidade total da cidade
    naquele horário, não só de uma área). Pra cidade com bucket único
    (o caso comum), isso não muda nada -- cada grupo já tem 1 linha só.

    Otimização (2026-10-06): antes o status era combinado chamando
    _status_combinado grupo a grupo (função Python por grupo), o que
    custava ~0,8s por arquivo e era o grosso da lentidão do histórico.
    Agora o "está aberta?" é calculado uma vez pra coluna inteira e o
    groupby só tira o máximo (True se qualquer área estiver aberta) --
    mesmo resultado, ordem de colunas idêntica, ~50x mais rápido."""
    chaves = ["Cidade", "Classe", "Data", "Janela", "JanelaOrdem"]
    aux = df.assign(
        _aberta=df["Status Janela"].astype(str).str.strip().str.lower().eq("aberta")
    )
    agregado = aux.groupby(chaves, as_index=False).agg(**{
        "_aberta": ("_aberta", "max"),
        "Atividades Disp.": ("Atividades Disp.", "sum"),
    })
    status = agregado.pop("_aberta").map({True: "Aberta", False: "Fechada"})
    agregado.insert(len(chaves), "Status Janela", status)
    return agregado


def carregar_dados(caminho_csv: str, mapa_cidades: dict = None, data_minima: str = None):
    """Lê o CSV consolidado. Retorna (df, erro) -- nunca lança exceção
    pra fora, sempre volta um erro legível se algo der errado.

    `data_minima` (opcional, formato "YYYY-MM-DD"): quando informado,
    descarta linhas com Data anterior a ela -- usado pela tela principal
    pra não arrastar dias que já passaram (ver portal_painel_cotas em
    app.py, pedido do Bruno, 2026-09-22: a linha do tempo só crescia, e
    a busca pela próxima janela aberta tinha que varrer tudo). Deixe
    None (padrão) quando o chamador precisa do histórico completo --
    é o caso de montar_historico_mensal, que relê fotos passadas do
    histórico e teria os dados todos descartados se isso filtrasse."""
    if not os.path.exists(caminho_csv):
        return None, None
    try:
        df = pd.read_csv(caminho_csv)
        faltando = [c for c in COLUNAS_OBRIGATORIAS if c not in df.columns]
        if faltando:
            return None, f"Arquivo importado está com colunas faltando: {', '.join(faltando)}"
        df["Cidade"] = df["Bucket"].apply(lambda b: nome_cidade(b, mapa_cidades))
        df["JanelaOrdem"] = df["Janela"].apply(
            lambda j: ORDEM_JANELA.index(j) if j in ORDEM_JANELA else 99
        )
        if data_minima:
            # comparação de string funciona pq a Data já vem no formato
            # ISO "YYYY-MM-DD" (ordena igual a data de verdade)
            df = df[df["Data"] >= data_minima]
        df = _agregar_areas(df)
        return df, None
    except Exception as e:
        return None, f"Não foi possível ler o arquivo importado: {e}"


def montar_cards_proxima_janela(df, agora=None, cidades_filtro=None):
    """Um card por cidade; dentro, uma linha por classe mostrando a
    PRÓXIMA janela aberta (Status Janela == 'Aberta' e que ainda não
    terminou), quantas horas faltam a partir de agora, e quantas
    atividades ainda cabem nela (Atividades Disp.).

    `cidades_filtro`, se informado, restringe os cards a essas cidades
    (comparação sem diferenciar maiúsculas/minúsculas) -- é a lista
    configurável de "cidades nos cards", separada da lista de extração.
    Cidades do filtro que não aparecem no CSV são simplesmente ignoradas.
    """
    if agora is None:
        agora = datetime.now()

    if cidades_filtro:
        permitidas = {c.strip().lower() for c in cidades_filtro if c.strip()}
        df = df[df["Cidade"].str.lower().isin(permitidas)]

    cards = []
    for cidade, grupo in df.groupby("Cidade"):
        classes_info = []
        for classe, sub in grupo.groupby("Classe"):
            candidatos = []
            for _, linha in sub.iterrows():
                try:
                    data_base = datetime.strptime(linha["Data"], "%Y-%m-%d")
                except (ValueError, TypeError):
                    continue
                inicio_h, inicio_m = JANELA_INICIO.get(linha["Janela"], (0, 0))
                fim_h, fim_m = JANELA_FIM.get(linha["Janela"], (23, 59))
                inicio_dt = data_base.replace(hour=inicio_h, minute=inicio_m)
                fim_dt = data_base.replace(hour=fim_h, minute=fim_m)
                aberta = linha["Status Janela"].strip().lower() == "aberta"
                if aberta and fim_dt > agora:
                    candidatos.append((inicio_dt, linha))

            classe_label = classe.replace("CLASSE", "Classe ")
            if candidatos:
                candidatos.sort(key=lambda t: t[0])
                inicio_dt, linha = candidatos[0]
                horas_ate = max(0.0, (inicio_dt - agora).total_seconds() / 3600)
                classes_info.append({
                    "classe": classe_label,
                    "tem_janela": True,
                    "data_fmt": inicio_dt.strftime("%d/%m"),
                    "janela": linha["Janela"],
                    "horas_ate": round(horas_ate, 1),
                    "ativ_disp": int(linha["Atividades Disp."]),
                })
            else:
                classes_info.append({"classe": classe_label, "tem_janela": False})

        classes_info.sort(key=lambda c: c["classe"])
        cards.append({"cidade": cidade, "classes": classes_info})

    cards.sort(key=lambda c: c["cidade"])
    return cards


def montar_heatmap(df):
    """Estrutura pra tabela detalhada: cidade > classe > (data, janela) -> célula."""
    heatmaps = []
    for cidade, grupo in df.groupby("Cidade"):
        colunas = sorted(
            grupo[["Data", "Janela", "JanelaOrdem"]].drop_duplicates().itertuples(index=False),
            key=lambda t: (t.Data, t.JanelaOrdem),
        )
        colunas_labels = [f"{c.Data} {c.Janela}" for c in colunas]

        linhas = []
        for classe, subgrupo in grupo.groupby("Classe"):
            celulas = []
            for c in colunas:
                match = subgrupo[(subgrupo["Data"] == c.Data) & (subgrupo["Janela"] == c.Janela)]
                if match.empty:
                    celulas.append(None)
                else:
                    linha = match.iloc[0]
                    celulas.append({
                        "status": linha["Status Janela"],
                        "aberta": linha["Status Janela"].strip().lower() == "aberta",
                        "ativ_disp": int(linha["Atividades Disp."]),
                    })
            linhas.append({"classe": classe.replace("CLASSE", "Classe "), "celulas": celulas})

        heatmaps.append({"cidade": cidade, "colunas": colunas_labels, "linhas": linhas})

    heatmaps.sort(key=lambda h: h["cidade"])
    return heatmaps


def salvar_upload_cotas(arquivo_flask, pasta: str, nome_arquivo_final: str):
    """Salva o CSV enviado pela caixa de upload, com backup do anterior e
    validação antes de assumir como arquivo atual. `arquivo_flask` é o
    FileStorage do request.files. Retorna (ok: bool, mensagem: str)."""
    if not arquivo_flask or arquivo_flask.filename == "":
        return False, "Nenhum arquivo selecionado."

    if not arquivo_flask.filename.lower().endswith(".csv"):
        return False, "Envie um arquivo .csv."

    os.makedirs(pasta, exist_ok=True)
    caminho_atual = os.path.join(pasta, nome_arquivo_final)
    caminho_temp = caminho_atual + ".tmp"

    if os.path.exists(caminho_atual):
        backup = os.path.join(pasta, f"backup_{datetime.now():%Y%m%d_%H%M%S}.csv")
        shutil.copy2(caminho_atual, backup)

    arquivo_flask.save(caminho_temp)

    try:
        df_validacao = pd.read_csv(caminho_temp)
        faltando = [c for c in COLUNAS_OBRIGATORIAS if c not in df_validacao.columns]
        if faltando:
            raise ValueError(f"colunas faltando: {', '.join(faltando)}")
    except Exception as e:
        os.remove(caminho_temp)
        return False, f"Arquivo inválido, não foi importado: {e}"

    os.replace(caminho_temp, caminho_atual)
    return True, "Arquivo importado com sucesso."


def carregar_config_extracao(caminho_json: str) -> dict:
    """Última configuração salva (cidades + classes + cidades_cards). Se
    não existir ou estiver corrompida, volta o padrão -- nunca lança
    exceção. Configs salvas antes do campo cidades_cards existir ganham
    o padrão dele automaticamente (retrocompatível)."""
    if not os.path.exists(caminho_json):
        return dict(CONFIG_EXTRACAO_PADRAO)
    try:
        with open(caminho_json, "r", encoding="utf-8") as f:
            config = json.load(f)
        if not config.get("cidades") or not config.get("classes"):
            return dict(CONFIG_EXTRACAO_PADRAO)
        if not config.get("cidades_cards"):
            config["cidades_cards"] = list(CONFIG_EXTRACAO_PADRAO["cidades_cards"])
        return config
    except Exception:
        return dict(CONFIG_EXTRACAO_PADRAO)


def salvar_config_extracao(caminho_json: str, cidades: list, classes: list, cidades_cards: list) -> dict:
    pasta = os.path.dirname(caminho_json)
    if pasta:
        os.makedirs(pasta, exist_ok=True)
    config = {"cidades": cidades, "classes": classes, "cidades_cards": cidades_cards}
    with open(caminho_json, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    return config


# ==========================
# HISTÓRICO (ACUMULADO) -- pedido do Bruno, 2026-09-21: um painel
# executivo que olha pra trás no mês inteiro, não só a foto de agora.
#
# Até aqui o Painel de Cotas só guardava UM arquivo atual
# (merged-painel-cotas.csv) + backups de segurança (backup_*.csv, só
# pra poder reverter um upload ruim) que podem ser apagados a qualquer
# momento -- nada disso serve como histórico de verdade. A partir de
# agora, TODO upload aceito também é copiado pra painel_cotas_dados/
# historico/AAAA-MM-DD_HHMMSS.csv (ver arquivar_snapshot_cotas,
# chamada em portal_painel_cotas_upload) -- essa pasta é o que alimenta
# a visão de histórico abaixo. Como isso começou a ser gravado agora,
# o "mês" só vai ficar completo com o tempo -- não há como reconstruir
# retroativamente fotos que nunca foram guardadas.
# ==========================

LIMIAR_ALONGAMENTO_HORAS = 24

# Sentinela pro caso de não existir NENHUMA janela aberta visível pra
# aquela cidade/classe na foto do dia (fora do horizonte baixado, ou
# tudo fechado) -- é o pior caso possível, pior que qualquer valor
# numérico de horas, mas não dá pra somar/tirar média com ele
# diretamente (por isso fica de fora das médias, e conta à parte como
# "dias sem janela visível" -- ver montar_historico_mensal).
SEM_JANELA_VISIVEL = float("inf")


def _horas_ate_por_grupo(df, agora):
    """Mesma lógica de 'próxima janela aberta' de montar_cards_proxima_janela,
    mas devolve TODAS as combinações (Cidade, Classe) do df (não só as do
    filtro de cards), usando `agora` como referência -- pra poder rodar
    sobre uma foto antiga (histórico), não só o instante atual.

    Retorna {(cidade, classe): horas_ate}. horas_ate é um float >= 0, ou
    SEM_JANELA_VISIVEL se nenhuma janela aberta foi encontrada nos dados
    daquela foto pra esse grupo.

    Otimização (2026-10-06): mesma regra de antes, mas calculada pra
    coluna inteira de uma vez (sem iterrows linha a linha)."""
    if df.empty:
        return {}
    datas = pd.to_datetime(df["Data"], format="%Y-%m-%d", errors="coerce")
    min_inicio = df["Janela"].map(lambda j: JANELA_INICIO.get(j, (0, 0))[0] * 60 + JANELA_INICIO.get(j, (0, 0))[1])
    min_fim = df["Janela"].map(lambda j: JANELA_FIM.get(j, (23, 59))[0] * 60 + JANELA_FIM.get(j, (23, 59))[1])
    inicio_dt = datas + pd.to_timedelta(min_inicio, unit="m")
    fim_dt = datas + pd.to_timedelta(min_fim, unit="m")
    aberta = df["Status Janela"].astype(str).str.strip().str.lower().eq("aberta")
    candidato = aberta & datas.notna() & (fim_dt > pd.Timestamp(agora))

    aux = pd.DataFrame({
        "Cidade": df["Cidade"], "Classe": df["Classe"],
        "inicio": inicio_dt.where(candidato),  # NaT onde não é candidato
    })
    resultado = {}
    for (cidade, classe), primeiro in aux.groupby(["Cidade", "Classe"])["inicio"].min().items():
        if pd.isna(primeiro):
            resultado[(cidade, classe)] = SEM_JANELA_VISIVEL
        else:
            segundos = (primeiro.to_pydatetime() - agora).total_seconds()
            resultado[(cidade, classe)] = round(max(0.0, segundos / 3600), 1)
    return resultado


def arquivar_snapshot_cotas(pasta_historico: str, caminho_csv_atual: str):
    """Copia o CSV recém-importado (já validado por salvar_upload_cotas)
    pra pasta de histórico, com nome datado (AAAA-MM-DD_HHMMSS.csv) --
    é isso que alimenta a visão de histórico/acumulado. Chamada uma vez
    por upload aceito. Não mexe nos backup_*.csv de segurança, que
    continuam existindo só pra poder reverter um upload ruim."""
    os.makedirs(pasta_historico, exist_ok=True)
    destino = os.path.join(pasta_historico, f"{datetime.now():%Y-%m-%d_%H%M%S}.csv")
    shutil.copy2(caminho_csv_atual, destino)
    return destino


def _listar_snapshots(pasta_historico: str) -> list:
    """[(datetime da foto, caminho)], mais antiga primeiro. Só reconhece
    arquivos com o nome exato gravado por arquivar_snapshot_cotas
    (AAAA-MM-DD_HHMMSS.csv) -- qualquer outro arquivo na pasta é
    ignorado silenciosamente (nunca quebra o painel)."""
    if not os.path.isdir(pasta_historico):
        return []
    achados = []
    for nome in os.listdir(pasta_historico):
        if not nome.endswith(".csv"):
            continue
        try:
            dt = datetime.strptime(nome[:-4], "%Y-%m-%d_%H%M%S")
        except ValueError:
            continue
        achados.append((dt, os.path.join(pasta_historico, nome)))
    achados.sort(key=lambda t: t[0])
    return achados


def _ultimo_snapshot_por_dia(pasta_historico: str, data_inicio=None, data_fim=None) -> list:
    """Reduz o histórico a UMA foto por dia -- a mais recente daquele dia,
    ou a única, se só tiver uma (pedido do Bruno: "olhe pra cada dia e
    veja a última foto do dia, ou a foto que houver se tiver só uma").
    data_inicio/data_fim são objetos `date`, opcionais. Retorna
    [(date, datetime da foto, caminho)], ordenado por data crescente."""
    por_dia = {}
    for dt, caminho in _listar_snapshots(pasta_historico):
        d = dt.date()
        if data_inicio and d < data_inicio:
            continue
        if data_fim and d > data_fim:
            continue
        por_dia[d] = (dt, caminho)  # a lista já vem ordenada crescente -- a última atribuição vence
    return [(d, dt, caminho) for d, (dt, caminho) in sorted(por_dia.items())]


# ==========================
# RESUMO DO HISTÓRICO (tabela incremental) -- pedido do Bruno, 2026-10-06
# ==========================
# Antes, cada acesso à página de histórico relia e recalculava TODAS as
# fotos do período (uma por dia), o que crescia a cada upload. Agora cada
# foto é resumida UMA vez só -- uma linha por (foto, cidade, classe) com
# as horas até a próxima janela aberta -- num SQLite próprio do painel
# (painel_cotas_dados/historico_resumo.db), totalmente separado do crm.db.
#
# Como se mantém atualizado, sem passo manual:
#   - no upload, a foto nova é resumida logo depois de arquivada;
#   - em todo acesso ao histórico, sincronizar_resumo_historico confere a
#     pasta: foto que ainda não está no resumo (ex: primeira vez, ou um
#     upload em que o resumo falhou) é processada na hora; foto apagada
#     da pasta sai do resumo; foto sobrescrita (tamanho/data mudou) é
#     refeita;
#   - se o mapa de cidades (mapa_cidades.json) mudar, ou a regra de
#     cálculo mudar (VERSAO_RESUMO), o resumo é refeito do zero
#     automaticamente no próximo acesso (o nome da cidade e a junção de
#     áreas dependem do mapa).
# As fotos CSV continuam sendo a fonte de verdade: dá pra apagar o .db a
# qualquer momento que ele é reconstruído sozinho no próximo acesso.
VERSAO_RESUMO = "1"


def _assinatura_resumo(mapa_cidades: dict) -> str:
    base = json.dumps({"v": VERSAO_RESUMO, "mapa": mapa_cidades or {}}, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(base.encode("utf-8")).hexdigest()


def _conectar_resumo(caminho_db: str):
    pasta = os.path.dirname(caminho_db)
    if pasta:
        os.makedirs(pasta, exist_ok=True)
    conn = sqlite3.connect(caminho_db, timeout=30)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS meta (
            chave TEXT PRIMARY KEY,
            valor TEXT
        );
        CREATE TABLE IF NOT EXISTS fotos (
            arquivo   TEXT PRIMARY KEY,   -- AAAA-MM-DD_HHMMSS.csv
            data      TEXT NOT NULL,      -- AAAA-MM-DD (dia da foto)
            dt_foto   TEXT NOT NULL,      -- AAAA-MM-DD HH:MM:SS
            tem_dados INTEGER NOT NULL,   -- 0 = arquivo ilegível/inválido
            tamanho   INTEGER,
            mtime     REAL
        );
        CREATE INDEX IF NOT EXISTS idx_fotos_data ON fotos(data);
        CREATE TABLE IF NOT EXISTS resumo (
            arquivo    TEXT NOT NULL,
            cidade     TEXT NOT NULL,
            classe     TEXT NOT NULL,
            horas_ate  REAL,              -- NULL quando sem_janela = 1
            sem_janela INTEGER NOT NULL,
            PRIMARY KEY (arquivo, cidade, classe)
        );
    """)
    return conn


def _resumir_foto(caminho_csv: str, dt_foto: datetime, mapa_cidades: dict):
    """Lê UMA foto e devolve (tem_dados, {(cidade, classe): horas_ate}).
    É exatamente o cálculo que montar_historico_mensal fazia por foto."""
    df, _erro = carregar_dados(caminho_csv, mapa_cidades=mapa_cidades)
    if df is None:
        return False, {}
    return True, _horas_ate_por_grupo(df, dt_foto)


def sincronizar_resumo_historico(pasta_historico: str, caminho_db: str, mapa_cidades: dict) -> int:
    """Deixa o resumo em dia com a pasta de fotos, processando só o que
    falta. Retorna quantas fotos foram (re)processadas -- 0 no caso
    normal, 1 logo depois de um upload."""
    snapshots = _listar_snapshots(pasta_historico)
    conn = _conectar_resumo(caminho_db)
    processadas = 0
    try:
        assinatura = _assinatura_resumo(mapa_cidades)
        linha = conn.execute("SELECT valor FROM meta WHERE chave = 'assinatura'").fetchone()
        if not linha or linha[0] != assinatura:
            with conn:
                conn.execute("DELETE FROM resumo")
                conn.execute("DELETE FROM fotos")
                conn.execute("INSERT OR REPLACE INTO meta (chave, valor) VALUES ('assinatura', ?)", (assinatura,))

        conhecidas = {
            arq: (tam, mt) for arq, tam, mt in conn.execute("SELECT arquivo, tamanho, mtime FROM fotos")
        }
        no_disco = {}
        for dt, caminho in snapshots:
            try:
                st = os.stat(caminho)
            except OSError:
                continue
            no_disco[os.path.basename(caminho)] = (dt, caminho, st.st_size, st.st_mtime)

        removidas = [arq for arq in conhecidas if arq not in no_disco]
        if removidas:
            with conn:
                conn.executemany("DELETE FROM resumo WHERE arquivo = ?", [(a,) for a in removidas])
                conn.executemany("DELETE FROM fotos WHERE arquivo = ?", [(a,) for a in removidas])

        for arq, (dt, caminho, tamanho, mtime) in sorted(no_disco.items()):
            if conhecidas.get(arq) == (tamanho, mtime):
                continue  # já resumida e não mudou
            tem_dados, horas_por_grupo = _resumir_foto(caminho, dt, mapa_cidades)
            linhas = [
                (arq, cidade, classe,
                 None if horas == SEM_JANELA_VISIVEL else float(horas),
                 1 if horas == SEM_JANELA_VISIVEL else 0)
                for (cidade, classe), horas in horas_por_grupo.items()
            ]
            # uma transação por foto: se o processo cair no meio, o que já
            # foi feito fica salvo e o resto é retomado no próximo acesso
            with conn:
                conn.execute("DELETE FROM resumo WHERE arquivo = ?", (arq,))
                conn.execute(
                    "INSERT OR REPLACE INTO fotos (arquivo, data, dt_foto, tem_dados, tamanho, mtime) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (arq, dt.strftime("%Y-%m-%d"), dt.strftime("%Y-%m-%d %H:%M:%S"),
                     1 if tem_dados else 0, tamanho, mtime),
                )
                conn.executemany(
                    "INSERT OR REPLACE INTO resumo (arquivo, cidade, classe, horas_ate, sem_janela) "
                    "VALUES (?, ?, ?, ?, ?)",
                    linhas,
                )
            processadas += 1
    finally:
        conn.close()
    return processadas


def _ler_resumo(caminho_db: str, arquivos: list) -> dict:
    """{arquivo: (tem_dados, {(cidade, classe): horas_ate})} só das fotos
    pedidas (as do período filtrado)."""
    if not arquivos:
        return {}
    conn = _conectar_resumo(caminho_db)
    try:
        marcadores = ",".join("?" for _ in arquivos)
        resultado = {}
        for arq, tem_dados in conn.execute(
            f"SELECT arquivo, tem_dados FROM fotos WHERE arquivo IN ({marcadores})", arquivos
        ):
            resultado[arq] = (bool(tem_dados), {})
        for arq, cidade, classe, horas, sem_janela in conn.execute(
            f"SELECT arquivo, cidade, classe, horas_ate, sem_janela FROM resumo WHERE arquivo IN ({marcadores})",
            arquivos,
        ):
            if arq in resultado:
                resultado[arq][1][(cidade, classe)] = SEM_JANELA_VISIVEL if sem_janela else horas
        return resultado
    finally:
        conn.close()


def formatar_alongamento(horas) -> str:
    """Formata um valor de horas_ate pra exibição -- trata a sentinela
    SEM_JANELA_VISIVEL com um rótulo, não com "inf horas"."""
    if horas is None:
        return "—"
    if horas == SEM_JANELA_VISIVEL:
        return "Sem janela"
    if horas < 24:
        return f"{horas:g}h"
    dias = horas / 24
    return f"{horas:g}h ({dias:.1f}d)"


def _normalizar_nome_cidade(txt: str) -> str:
    """Maiúsculo, sem acento, sem apóstrofo, espaços colapsados -- pra
    comparar nome de cidade sem depender de acentuação/pontuação baterem
    exatamente (mesma ideia do 1_download_cotas.py, duplicada aqui pra
    não criar dependência entre os dois módulos)."""
    txt = txt.strip().upper().replace("'", " ").replace("’", " ")
    txt = unicodedata.normalize("NFKD", txt)
    txt = "".join(c for c in txt if not unicodedata.combining(c))
    return " ".join(txt.split())


def _resolver_ddd(cidade: str, cidade_para_ddd: dict):
    """Acha o DDD de `cidade` em cidade_para_ddd, cobrindo o caso de nome
    combinado tipo 'Americana / Santa Bárbara D'Oeste' -- quando duas
    cidades compartilham o mesmo bucket de extração e o mapa de cidades
    (config, ver nome_cidade) passa a devolver as duas juntas num nome
    só, que não bate mais com nenhuma linha isolada da tabela
    cidades_ddd (correção Bruno, 2026-09-25: até então isso fazia o
    histórico perder o DDD dessas cidades, caindo em "?").

    Tenta o nome inteiro primeiro; se não achar, tenta cada pedaço
    separado por '/' e usa o DDD do primeiro que bater -- cidades que
    compartilham bucket normalmente compartilham DDD também, então
    qualquer uma das partes resolve. Retorna None se não achar nada."""
    mapa_normalizado = {_normalizar_nome_cidade(k): v for k, v in cidade_para_ddd.items()}
    achado = mapa_normalizado.get(_normalizar_nome_cidade(cidade))
    if achado is not None:
        return achado
    for parte in cidade.split("/"):
        parte = parte.strip()
        if not parte:
            continue
        achado = mapa_normalizado.get(_normalizar_nome_cidade(parte))
        if achado is not None:
            return achado
    return None


def montar_historico_mensal(
    pasta_historico: str, mapa_cidades: dict, cidade_para_ddd: dict,
    data_inicio=None, data_fim=None,
    ddd_filtro=None, cidade_filtro=None, classe_filtro=None,
    caminho_resumo: str = None,
) -> dict:
    """Monta a visão de histórico/acumulado do Painel de Cotas: pra cada
    dia (última foto do dia, ou a única), calcula quantas horas faltam
    até a próxima janela aberta -- o "alongamento da agenda" -- por
    cidade e por DDD, e resume isso pro mapa do estado e pras tabelas de
    linha do tempo. `cidade_para_ddd` é {NOME CIDADE EM MAIÚSCULA: ddd},
    injetado de fora (vem da tabela cidades_ddd do crm.db) -- esse
    módulo continua sem depender do crm.db diretamente, só recebe o
    mapa já pronto (mesmo padrão de mapa_cidades).

    Onde uma cidade tem mais de uma classe, o valor do dia pra ela é o
    PIOR caso (maior horas_ate entre as classes) -- é isso que evidencia
    o problema de agenda mais esticada; o mesmo vale pra agregar cidades
    dentro de um DDD. Cidades sem DDD cadastrado em cidade_para_ddd
    entram como ddd "?" (aparecem na tabela por cidade normalmente, só
    ficam de fora da tabela por DDD e do mapa).

    Retorna um dict com: sem_historico, dias (metadados de cada foto
    usada), por_cidade_dia, por_ddd_dia, cidades (resumo por cidade pro
    mapa/tabela), ddds (resumo por DDD), analise (lista de frases).

    Desde 2026-10-06 os números de cada foto vêm da tabela de resumo
    (`caminho_resumo`, ver sincronizar_resumo_historico) em vez de reler
    os CSVs a cada acesso. Se o resumo falhar por qualquer motivo, cai
    no cálculo direto do arquivo -- mais lento, mas a página nunca quebra.
    O filtro de datas continua sendo aplicado aqui, igual antes."""
    if caminho_resumo is None:
        caminho_resumo = os.path.join(
            os.path.dirname(os.path.abspath(pasta_historico)), "historico_resumo.db"
        )
    snapshots = _ultimo_snapshot_por_dia(pasta_historico, data_inicio, data_fim)
    if not snapshots:
        return {
            "sem_historico": True, "dias": [], "por_cidade_dia": {}, "por_ddd_dia": {},
            "cidades": [], "ddds": [], "analise": [],
        }

    cidade_filtro_norm = {_normalizar_nome_cidade(c) for c in cidade_filtro} if cidade_filtro else None
    ddd_filtro_norm = {str(d) for d in ddd_filtro} if ddd_filtro else None
    classe_filtro_norm = set(classe_filtro) if classe_filtro else None

    dias_meta = []
    por_cidade_dia = {}   # {cidade: {data_iso: horas_ate}}
    por_ddd_dia = {}      # {ddd: {data_iso: horas_ate}}

    try:
        sincronizar_resumo_historico(pasta_historico, caminho_resumo, mapa_cidades)
        resumo = _ler_resumo(caminho_resumo, [os.path.basename(c) for _, _, c in snapshots])
    except Exception:
        resumo = {}

    for data_dia, dt_foto, caminho in snapshots:
        data_iso = data_dia.strftime("%Y-%m-%d")
        item = resumo.get(os.path.basename(caminho))
        if item is None:  # fora do resumo (falha no .db) -> calcula direto do arquivo
            item = _resumir_foto(caminho, dt_foto, mapa_cidades)
        tem_dados, horas_por_grupo = item
        if not tem_dados:
            dias_meta.append({"data": data_iso, "hora_foto": dt_foto.strftime("%H:%M"), "tem_dados": False})
            continue
        if classe_filtro_norm:
            horas_por_grupo = {k: v for k, v in horas_por_grupo.items() if k[1] in classe_filtro_norm}
        dias_meta.append({"data": data_iso, "hora_foto": dt_foto.strftime("%H:%M"), "tem_dados": True})
        if not horas_por_grupo:
            continue

        pior_por_cidade_no_dia = {}
        for (cidade, classe), horas in horas_por_grupo.items():
            partes_cidade = {_normalizar_nome_cidade(p) for p in ([cidade] + cidade.split("/"))}
            if cidade_filtro_norm and not (partes_cidade & cidade_filtro_norm):
                continue
            ddd = _resolver_ddd(cidade, cidade_para_ddd)
            if ddd_filtro_norm and (ddd is None or str(ddd) not in ddd_filtro_norm):
                continue
            atual = pior_por_cidade_no_dia.get(cidade)
            pior_por_cidade_no_dia[cidade] = horas if atual is None else max(atual, horas)

        for cidade, horas in pior_por_cidade_no_dia.items():
            por_cidade_dia.setdefault(cidade, {})[data_iso] = horas
            ddd = _resolver_ddd(cidade, cidade_para_ddd)
            chave_ddd = str(ddd) if ddd is not None else "?"
            atual_ddd = por_ddd_dia.setdefault(chave_ddd, {}).get(data_iso)
            por_ddd_dia[chave_ddd][data_iso] = horas if atual_ddd is None else max(atual_ddd, horas)

    # ---- resumo por cidade (mapa + tabela) ----
    cidades_resumo = []
    for cidade, valores in por_cidade_dia.items():
        dias_com_dado = [(d, h) for d, h in valores.items()]
        finitos_acima = [h - LIMIAR_ALONGAMENTO_HORAS for _, h in dias_com_dado
                          if h != SEM_JANELA_VISIVEL and h > LIMIAR_ALONGAMENTO_HORAS]
        dias_sem_janela = sum(1 for _, h in dias_com_dado if h == SEM_JANELA_VISIVEL)
        ocorrencias = len(finitos_acima) + dias_sem_janela
        total_dias = len(dias_com_dado)
        ultima_data = max(valores.keys()) if valores else None
        cidades_resumo.append({
            "cidade": cidade,
            "ddd": _resolver_ddd(cidade, cidade_para_ddd) or "?",
            "total_dias": total_dias,
            "ocorrencias_alongamento": ocorrencias,
            "dias_sem_janela": dias_sem_janela,
            "pct_dias_alongado": round(100 * ocorrencias / total_dias, 0) if total_dias else 0,
            "soma_horas_acima_limiar": round(sum(finitos_acima), 1),
            "media_horas_acima_limiar": round(sum(finitos_acima) / len(finitos_acima), 1) if finitos_acima else 0,
            "ultima_leitura": formatar_alongamento(valores.get(ultima_data)) if ultima_data else "—",
        })
    cidades_resumo.sort(key=lambda c: (-c["ocorrencias_alongamento"], -c["soma_horas_acima_limiar"]))

    # ---- resumo por DDD (tabela) ----
    ddds_resumo = []
    for ddd, valores in por_ddd_dia.items():
        finitos_acima = [h - LIMIAR_ALONGAMENTO_HORAS for h in valores.values()
                          if h != SEM_JANELA_VISIVEL and h > LIMIAR_ALONGAMENTO_HORAS]
        dias_sem_janela = sum(1 for h in valores.values() if h == SEM_JANELA_VISIVEL)
        ddds_resumo.append({
            "ddd": ddd,
            "total_dias": len(valores),
            "ocorrencias_alongamento": len(finitos_acima) + dias_sem_janela,
            "dias_sem_janela": dias_sem_janela,
            "soma_horas_acima_limiar": round(sum(finitos_acima), 1),
        })
    ddds_resumo.sort(key=lambda d: (-d["ocorrencias_alongamento"], -d["soma_horas_acima_limiar"]))

    analise = _gerar_analise_historico(cidades_resumo, dias_meta)

    return {
        "sem_historico": False,
        "dias": dias_meta,
        "por_cidade_dia": por_cidade_dia,
        "por_ddd_dia": por_ddd_dia,
        "cidades": cidades_resumo,
        "ddds": ddds_resumo,
        "analise": analise,
    }


def _gerar_analise_historico(cidades_resumo: list, dias_meta: list) -> list:
    """Leitura automática, em frases prontas, do que o painel mostra --
    pedido do Bruno: "uma análise também sobre a leitura do painel, com
    apontamentos de desvios e onde a agenda se manteve estável". Regras
    simples, sem inventar causa -- só descreve o que os números mostram."""
    frases = []
    dias_com_dado = [d for d in dias_meta if d["tem_dados"]]
    if not dias_com_dado:
        return ["Nenhuma foto com dados válidos no período filtrado."]

    frases.append(
        f"Período com {len(dias_com_dado)} foto(s) de dia(s) distintos "
        f"(de {dias_com_dado[0]['data']} a {dias_com_dado[-1]['data']})."
    )

    com_ocorrencia = [c for c in cidades_resumo if c["ocorrencias_alongamento"] > 0]
    estaveis = [c for c in cidades_resumo if c["ocorrencias_alongamento"] == 0]

    if com_ocorrencia:
        pior = com_ocorrencia[0]
        frases.append(
            f"Maior desvio: {pior['cidade']} (DDD {pior['ddd']}) com agenda esticada acima de "
            f"{LIMIAR_ALONGAMENTO_HORAS}h em {pior['ocorrencias_alongamento']} de {pior['total_dias']} "
            f"dia(s) analisado(s)"
            + (f", incluindo {pior['dias_sem_janela']} dia(s) sem nenhuma janela aberta visível" if pior["dias_sem_janela"] else "")
            + "."
        )
        if len(com_ocorrencia) > 1:
            outras = ", ".join(f"{c['cidade']} ({c['ocorrencias_alongamento']}x)" for c in com_ocorrencia[1:4])
            frases.append(f"Também com ocorrências no período: {outras}.")
    else:
        frases.append(f"Nenhuma cidade ficou acima de {LIMIAR_ALONGAMENTO_HORAS}h de alongamento no período.")

    if estaveis:
        nomes = ", ".join(c["cidade"] for c in estaveis[:6])
        sufixo = "..." if len(estaveis) > 6 else ""
        frases.append(f"Agenda estável (sem nenhum dia acima de {LIMIAR_ALONGAMENTO_HORAS}h) em: {nomes}{sufixo}.")

    return frases


def gerar_config_cotas_py(cidades: list, classes: list) -> str:
    """Gera o conteúdo do config_cotas.py: colocado na mesma pasta do
    1_download_cotas.py, é importado automaticamente por ele e sobrescreve
    CIDADES/CLASSES -- sem precisar editar o script principal."""
    linhas_cidades = ",\n    ".join(f'"{c}"' for c in cidades)
    linhas_classes = ", ".join(f'"{c}"' for c in classes)
    return (
        '"""\n'
        "config_cotas.py -- gerado pela pagina de configuracao do Painel\n"
        "de Cotas no CRM (ADM). Coloque este arquivo na MESMA PASTA do\n"
        "1_download_cotas.py -- ele e importado automaticamente e\n"
        "sobrescreve CIDADES/CLASSES.\n"
        f"Gerado em: {datetime.now():%d/%m/%Y %H:%M}\n"
        '"""\n\n'
        "CIDADES = [\n"
        f"    {linhas_cidades}\n"
        "]\n\n"
        f"CLASSES = [{linhas_classes}]\n"
    )
