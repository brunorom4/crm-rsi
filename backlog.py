"""
Módulo Tratamento de Backlog -- pedido do Bruno, 2026-09-19.

Módulo separado, com banco de dados próprio (backlog.db), que NÃO se
comunica operacionalmente com o CRM principal, com UMA exceção combinada
explicitamente com o Bruno: o fechamento "AGENDADO" grava uma linha real na
tabela `atividades` do crm.db (tabulacao='Agendado'), pra contar de verdade
como agendamento nos indicadores do CRM. Todo o resto (fila, distribuição,
histórico, Aberto Chamado/Retorno) vive só aqui dentro.

Login, permissão ("backlog_tratar") e troca de senha são reaproveitados do
app.py principal -- importados dentro de cada rota (não no topo do
arquivo), pra evitar import circular, já que app.py importa esse módulo
pra registrar o blueprint.

Resumo das regras de negócio (confirmadas com o Bruno):
- Importação: entra todo contrato com AGING em ("AGENDA NO PASSADO",
  "SEM AGENDA-NUNCA AGENDADA", "SEM AGENDA-REAGENDADA"), independente da
  DATA_ABERTURA_OS -- inclusive sem data nenhuma. Qualquer outro AGING
  (AGENDA NO FUTURO, AGENDA NO DIA) é "não elegível".
  Chave de deduplicação/reconciliação: CD_OPERADORA + CD_CONTRATO.

  Histórico: de 2026-09-19 a 2026-10-19 valeu também um corte D-7 (só
  entrava contrato aberto há pelo menos 7 dias) e os sem data nunca
  entravam. Os dois foram derrubados a pedido do Bruno em 2026-10-19.
  Atenção: "sem data" aqui inclui data que o _parse_data_ddmmaa não
  consegue ler (ele só aceita DD/MM/AA) -- antes essas linhas eram
  descartadas em silêncio; agora entram e ficam visíveis na fila.
- Ordenação da fila, em três faixas (2026-10-19, pedido do Bruno):
    1ª) contratos com data ANTERIOR ao mês corrente, do mais antigo pro
        mais novo;
    2ª) contratos SEM data;
    3ª) contratos com data DENTRO do mês corrente, do mais antigo pro
        mais novo.
  A fronteira é o 1º dia do mês atual, recalculada a cada consulta -- em
  novembro vira "até outubro -> sem data -> novembro", sem precisar mexer
  no código. A ordem do mais antigo pro mais novo dentro de cada faixa é
  de 2026-09-30 (antes era do mais recente pro mais antigo). Dentro da
  mesma data, prioridade pros vendedores ANA CLAUDIA PIRES e
  TATIANE CRISTINA SABINO CALTRAN (mesmo padrão de prioridade "período
  primeiro, vendedor depois" já usado na Confirmação de Agenda).
- Reimportação (por contrato já existente no banco, chave CD_OPERADORA+
  CD_CONTRATO), contrato que está em AGUARDANDO_RETORNO (Aberto Chamado ou
  Retorno) nunca é tocado -- "permanece como está". Pra todos os outros:
    * Volta elegível (passado ou sem agenda) -> atualiza os dados e
      reenfileira (status=PENDENTE), não importa se já tinha sido tratado
      antes (reabre).
    * Volta não elegível (futuro ou no dia) -> marca RESOLVIDO, sai da
      fila.
    * Não volta na planilha -> marca RESOLVIDO, sai da fila.
  Contrato novo (não existia) só entra se vier elegível.
- `ultimo_fechamento` guarda o último fechamento real dado por um operador
  (AGENDADO/CANCELADO/JA_CANCELADO/CONTRATO_CONECTADO/ABERTO_CHAMADO/
  RETORNO) -- não é sobrescrito por uma resolução automática de
  reimportação se o contrato nunca tinha sido tratado por ninguém.
"""
import sqlite3
import os
from datetime import datetime, timedelta
from flask import Blueprint, render_template, request, redirect, send_file
import pandas as pd
import io

PASTA_BASE_BACKLOG = os.path.dirname(os.path.abspath(__file__))
NOME_BANCO_BACKLOG = "backlog.db"

# AGING elegível pra entrar/permanecer na fila do Backlog -- qualquer outro
# valor (AGENDA NO FUTURO, AGENDA NO DIA) é tratado como não elegível.
AGING_ELEGIVEL = {"AGENDA NO PASSADO", "SEM AGENDA-NUNCA AGENDADA", "SEM AGENDA-REAGENDADA"}

# Mesmo critério de prioridade de vendedor já usado na Confirmação de
# Agenda -- só entra como desempate, nunca fura a ordenação principal.
VENDEDORES_PRIORITARIOS = {"ANA CLAUDIA PIRES", "TATIANE CRISTINA SABINO CALTRAN"}

FECHAMENTOS_TERMINAIS = ["AGENDADO", "CANCELADO", "JA_CANCELADO", "CONTRATO_CONECTADO"]
FECHAMENTOS_RETORNO = ["ABERTO_CHAMADO", "RETORNO"]
FECHAMENTOS_VALIDOS = FECHAMENTOS_TERMINAIS + FECHAMENTOS_RETORNO

LABELS_FECHAMENTO = {
    "AGENDADO": "Agendado",
    "CANCELADO": "Cancelado",
    "JA_CANCELADO": "Já Cancelado",
    "CONTRATO_CONECTADO": "Contrato Conectado",
    "ABERTO_CHAMADO": "Aberto Chamado",
    "RETORNO": "Retorno",
    "RESOLVIDO_REIMPORTACAO": "Resolvido (reimportação)",
    "REENFILEIRADO_REIMPORTACAO": "Reenfileirado (reimportação)",
}

bp_backlog = Blueprint("backlog", __name__, template_folder="templates")


def conectar_backlog():
    conn = sqlite3.connect(os.path.join(PASTA_BASE_BACKLOG, NOME_BANCO_BACKLOG))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _garantir_banco_backlog():
    conn = conectar_backlog()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS contratos_backlog (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cd_operadora TEXT NOT NULL,
            cd_contrato TEXT NOT NULL,
            ddd TEXT,
            cidade TEXT,
            produto_de TEXT,
            produto_para TEXT,
            data_agendamento TEXT,
            periodo TEXT,
            aging TEXT,
            data_abertura_os TEXT,
            vendedor TEXT,
            prioridade_vendedor INTEGER DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'PENDENTE',
            ultimo_fechamento TEXT,
            operador_atual TEXT,
            data_distribuicao TEXT,
            retorno_em TEXT,
            data_entrada_fila TEXT NOT NULL,
            data_primeira_importacao TEXT NOT NULL,
            data_ultima_importacao TEXT NOT NULL,
            criado_em TEXT,
            atualizado_em TEXT,
            UNIQUE(cd_operadora, cd_contrato)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_backlog_fila ON contratos_backlog(status, data_abertura_os)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tratamentos_backlog (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contrato_backlog_id INTEGER,
            cd_operadora TEXT,
            cd_contrato TEXT,
            operador TEXT,
            fechamento TEXT,
            observacao TEXT,
            nova_data_agendamento TEXT,
            novo_periodo TEXT,
            novo_resp_cliente TEXT,
            novo_flag_mdu INTEGER,
            retorno_em TEXT,
            data_tratamento TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_backlog_hist ON tratamentos_backlog(cd_operadora, cd_contrato)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS importacoes_backlog (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario TEXT,
            nome_arquivo TEXT,
            total_linhas INTEGER,
            total_elegiveis INTEGER,
            novos INTEGER,
            reenfileirados INTEGER,
            resolvidos INTEGER,
            sem_ddd INTEGER,
            data_hora TEXT
        )
    """)
    conn.commit()
    conn.close()


_garantir_banco_backlog()


def _parse_data_ddmmaa(valor):
    """Converte 'DD/MM/AA' pra 'YYYY-MM-DD'. None se vazio/inválido."""
    if not valor or not isinstance(valor, str) or not valor.strip():
        return None
    try:
        return datetime.strptime(valor.strip(), "%d/%m/%y").strftime("%Y-%m-%d")
    except ValueError:
        return None


def processar_importacao_backlog(caminho_arquivo, caminho_crm_db, usuario_nome):
    """Lê a planilha de backlog, resolve DDD via crm.db (mesmas tabelas
    cidades_operadora/cidades_ddd já usadas pela Confirmação de Agenda), e
    aplica a reconciliação completa (ver regras no cabeçalho do arquivo).
    Chave de deduplicação/atualização: CD_OPERADORA + CD_CONTRATO."""
    df = pd.read_excel(caminho_arquivo)
    colunas_necessarias = [
        "CD_OPERADORA", "CD_CONTRATO", "NM_CIDADE", "PRODUTO_DE", "PRODUTO_PARA",
        "DATA_AGENDAMENTO", "PERIODO", "AGING", "DATA_ABERTURA_OS", "DS_VENDEDOR",
    ]
    faltando = [c for c in colunas_necessarias if c not in df.columns]
    if faltando:
        return {"erro": f"Coluna(s) não encontrada(s) na planilha: {', '.join(faltando)}"}

    # dedupe na própria planilha (mesma chave composta), mantém a 1ª ocorrência
    df["_cd_operadora_str"] = df["CD_OPERADORA"].apply(
        lambda v: str(int(v)) if pd.notna(v) else None
    )
    df["_cd_contrato_str"] = df["CD_CONTRATO"].apply(
        lambda v: str(v).strip() if pd.notna(v) else None
    )
    df = df.drop_duplicates(subset=["_cd_operadora_str", "_cd_contrato_str"], keep="first")

    conn_crm = sqlite3.connect(caminho_crm_db)
    mapa_cidade = {r[0]: r[1] for r in conn_crm.execute("SELECT cod_operadora, cidade FROM cidades_operadora").fetchall()}
    mapa_ddd = {r[0]: r[1] for r in conn_crm.execute("SELECT cidade, ddd FROM cidades_ddd").fetchall()}
    conn_crm.close()

    hoje_str = datetime.now().strftime("%Y-%m-%d")
    agora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = conectar_backlog()

    # chaves vistas nessa importação (pra saber quem NÃO voltou)
    chaves_vistas = {}  # (cd_operadora, cd_contrato) -> aging (normalizado)
    novos = reenfileirados = resolvidos = sem_ddd = total_elegiveis = 0

    for _idx, linha in df.iterrows():
        cd_operadora = linha["_cd_operadora_str"]
        cd_contrato = linha["_cd_contrato_str"]
        if not cd_operadora or not cd_contrato:
            continue

        aging = str(linha["AGING"]).strip().upper() if pd.notna(linha["AGING"]) else ""
        data_abertura_os = _parse_data_ddmmaa(
            str(linha["DATA_ABERTURA_OS"]) if pd.notna(linha["DATA_ABERTURA_OS"]) else None
        )
        chaves_vistas[(cd_operadora, cd_contrato)] = aging
        # Elegibilidade é só o AGING (2026-10-19). O corte D-7 e a exigência
        # de ter data saíram daqui -- contrato sem data entra e cai na 2ª
        # faixa da fila (ver ordenação no cabeçalho do arquivo).
        elegivel = aging in AGING_ELEGIVEL

        existente = conn.execute(
            "SELECT * FROM contratos_backlog WHERE cd_operadora=? AND cd_contrato=?",
            (cd_operadora, cd_contrato)
        ).fetchone()

        if existente and existente["status"] == "AGUARDANDO_RETORNO":
            continue  # Aberto Chamado / Retorno: permanece como está, não mexe

        if elegivel:
            total_elegiveis += 1
            cidade = mapa_cidade.get(cd_operadora)
            ddd = mapa_ddd.get(cidade) if cidade else None
            if not ddd:
                sem_ddd += 1
                if not existente:
                    continue  # sem DDD e é novo -- não dá pra distribuir, não entra

            data_agendamento = _parse_data_ddmmaa(
                str(linha["DATA_AGENDAMENTO"]) if pd.notna(linha["DATA_AGENDAMENTO"]) else None
            )
            periodo = str(linha["PERIODO"]) if pd.notna(linha["PERIODO"]) else ""
            produto_de = str(linha["PRODUTO_DE"]) if pd.notna(linha["PRODUTO_DE"]) else ""
            produto_para = str(linha["PRODUTO_PARA"]) if pd.notna(linha["PRODUTO_PARA"]) else ""
            vendedor = str(linha["DS_VENDEDOR"]).strip() if pd.notna(linha["DS_VENDEDOR"]) else ""
            prioridade_vendedor = 1 if vendedor.upper() in VENDEDORES_PRIORITARIOS else 0

            if not existente:
                conn.execute("""
                    INSERT INTO contratos_backlog
                        (cd_operadora, cd_contrato, ddd, cidade, produto_de, produto_para,
                         data_agendamento, periodo, aging, data_abertura_os, vendedor,
                         prioridade_vendedor, status, data_entrada_fila,
                         data_primeira_importacao, data_ultima_importacao, criado_em, atualizado_em)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDENTE', ?, ?, ?, ?, ?)
                """, (cd_operadora, cd_contrato, ddd, cidade, produto_de, produto_para,
                      data_agendamento, periodo, aging, data_abertura_os, vendedor,
                      prioridade_vendedor, hoje_str, hoje_str, hoje_str, agora_str, agora_str))
                novos += 1
            else:
                reabrindo = existente["status"] != "PENDENTE"
                conn.execute("""
                    UPDATE contratos_backlog SET
                        ddd=COALESCE(?, ddd), cidade=COALESCE(?, cidade), produto_de=?, produto_para=?,
                        data_agendamento=?, periodo=?, aging=?, data_abertura_os=?, vendedor=?,
                        prioridade_vendedor=?, status='PENDENTE', operador_atual=NULL,
                        data_distribuicao=NULL, retorno_em=NULL,
                        data_ultima_importacao=?, atualizado_em=?
                    WHERE id=?
                """, (ddd, cidade, produto_de, produto_para, data_agendamento, periodo, aging,
                      data_abertura_os, vendedor, prioridade_vendedor, hoje_str, agora_str,
                      existente["id"]))
                if reabrindo:
                    conn.execute("""
                        INSERT INTO tratamentos_backlog
                            (contrato_backlog_id, cd_operadora, cd_contrato, operador, fechamento,
                             observacao, data_tratamento)
                        VALUES (?, ?, ?, ?, 'REENFILEIRADO_REIMPORTACAO', ?, ?)
                    """, (existente["id"], cd_operadora, cd_contrato, "SISTEMA (reimportação)",
                          f"Voltou na planilha com AGING '{aging}' -- reenfileirado.", agora_str))
                reenfileirados += 1
        else:
            # não elegível (AGING fora da lista -- desde 2026-10-19 esse é o
            # único motivo possível): se já existia aqui dentro (e não está
            # aguardando retorno, já filtrado acima), resolve e tira da fila.
            if existente:
                motivo = "AGING não elegível"
                novo_ultimo_fechamento = existente["ultimo_fechamento"] or "RESOLVIDO_REIMPORTACAO"
                if existente["status"] != "RESOLVIDO":
                    conn.execute("""
                        INSERT INTO tratamentos_backlog
                            (contrato_backlog_id, cd_operadora, cd_contrato, operador, fechamento,
                             observacao, data_tratamento)
                        VALUES (?, ?, ?, ?, 'RESOLVIDO_REIMPORTACAO', ?, ?)
                    """, (existente["id"], cd_operadora, cd_contrato, "SISTEMA (reimportação)",
                          f"Voltou na planilha com AGING '{aging}' ({motivo}) -- resolvido.", agora_str))
                    resolvidos += 1
                conn.execute("""
                    UPDATE contratos_backlog SET status='RESOLVIDO', ultimo_fechamento=?,
                        operador_atual=NULL, data_distribuicao=NULL, retorno_em=NULL,
                        data_ultima_importacao=?, atualizado_em=?
                    WHERE id=?
                """, (novo_ultimo_fechamento, hoje_str, agora_str, existente["id"]))
            # se não existia e não é elegível: nunca entrou, ignora de vez.

    # quem estava na fila (qualquer status exceto AGUARDANDO_RETORNO/RESOLVIDO)
    # e NÃO voltou nessa planilha -> resolve.
    pendentes_no_banco = conn.execute("""
        SELECT * FROM contratos_backlog WHERE status NOT IN ('AGUARDANDO_RETORNO', 'RESOLVIDO')
    """).fetchall()
    for c in pendentes_no_banco:
        chave = (c["cd_operadora"], c["cd_contrato"])
        if chave in chaves_vistas:
            continue  # já tratado no loop acima (voltou, elegível ou não)
        novo_ultimo_fechamento = c["ultimo_fechamento"] or "RESOLVIDO_REIMPORTACAO"
        conn.execute("""
            INSERT INTO tratamentos_backlog
                (contrato_backlog_id, cd_operadora, cd_contrato, operador, fechamento,
                 observacao, data_tratamento)
            VALUES (?, ?, ?, ?, 'RESOLVIDO_REIMPORTACAO', ?, ?)
        """, (c["id"], c["cd_operadora"], c["cd_contrato"], "SISTEMA (reimportação)",
              "Não voltou na importação mais recente -- resolvido.", agora_str))
        conn.execute("""
            UPDATE contratos_backlog SET status='RESOLVIDO', ultimo_fechamento=?,
                operador_atual=NULL, data_distribuicao=NULL, retorno_em=NULL, atualizado_em=?
            WHERE id=?
        """, (novo_ultimo_fechamento, agora_str, c["id"]))
        resolvidos += 1

    nome_arquivo = getattr(caminho_arquivo, "filename", None) or os.path.basename(str(caminho_arquivo))
    conn.execute("""
        INSERT INTO importacoes_backlog
            (usuario, nome_arquivo, total_linhas, total_elegiveis, novos, reenfileirados,
             resolvidos, sem_ddd, data_hora)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (usuario_nome, nome_arquivo, len(df), total_elegiveis, novos,
          reenfileirados, resolvidos, sem_ddd, agora_str))

    conn.commit()
    conn.close()
    return {
        "total_linhas": len(df), "total_elegiveis": total_elegiveis, "novos": novos,
        "reenfileirados": reenfileirados, "resolvidos": resolvidos, "sem_ddd": sem_ddd,
    }


def acordar_retornos_vencidos():
    """Contrato em Aberto Chamado/Retorno cuja data/hora marcada já passou
    volta pra fila (PENDENTE) -- checado ao vivo, sempre que alguém abre o
    Painel ou pede o próximo contrato pra tratar (não depende de tarefa
    agendada rodando à parte)."""
    agora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = conectar_backlog()
    cursor = conn.execute("""
        UPDATE contratos_backlog SET status='PENDENTE', operador_atual=NULL,
            data_distribuicao=NULL, retorno_em=NULL, atualizado_em=?
        WHERE status='AGUARDANDO_RETORNO' AND retorno_em IS NOT NULL AND retorno_em <= ?
    """, (agora_str, agora_str))
    total = cursor.rowcount
    conn.commit()
    conn.close()
    return total


def _primeiro_dia_mes_atual():
    """Fronteira das faixas da fila. Recalculada a cada chamada, então a
    regra anda sozinha na virada do mês."""
    return datetime.now().date().replace(day=1).strftime("%Y-%m-%d")


def buscar_proximo_backlog(operador_nome):
    """Mesmo padrão de reivindicação com retry-loop (evita corrida entre dois
    operadores pegando o mesmo contrato).

    Ordenação em três faixas (2026-10-19): meses anteriores ao atual ->
    sem data -> mês atual. Dentro de cada faixa, DATA_ABERTURA_OS do mais
    ANTIGO pro mais novo (2026-09-30), com prioridade de vendedor como
    desempate dentro da mesma data."""
    acordar_retornos_vencidos()
    conn = conectar_backlog()

    atual = conn.execute(
        "SELECT * FROM contratos_backlog WHERE status='EM_ANDAMENTO' AND operador_atual=? "
        "ORDER BY data_distribuicao DESC LIMIT 1",
        (operador_nome,)
    ).fetchone()
    if atual:
        conn.close()
        return atual

    agora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    inicio_mes = _primeiro_dia_mes_atual()
    ids_ja_tentados = []
    for _tentativa in range(5):
        marcador = ",".join("?" * len(ids_ja_tentados)) if ids_ja_tentados else "-1"
        # O CASE define a faixa (0 = meses anteriores, 1 = sem data, 2 = mês
        # atual). O "?" dele vem DEPOIS dos ids do NOT IN na lista de
        # parâmetros -- a ordem dos ? no SQL é o que manda.
        candidato = conn.execute(f"""
            SELECT * FROM contratos_backlog
            WHERE status='PENDENTE' AND id NOT IN ({marcador})
            ORDER BY
                CASE
                    WHEN data_abertura_os IS NULL THEN 1
                    WHEN data_abertura_os < ? THEN 0
                    ELSE 2
                END,
                data_abertura_os ASC, prioridade_vendedor DESC, id ASC
            LIMIT 1
        """, ids_ja_tentados + [inicio_mes]).fetchone()

        if not candidato:
            conn.close()
            return None

        cursor = conn.execute(
            "UPDATE contratos_backlog SET status='EM_ANDAMENTO', operador_atual=?, data_distribuicao=? "
            "WHERE id=? AND status='PENDENTE'",
            (operador_nome, agora_str, candidato["id"])
        )
        if cursor.rowcount == 0:
            conn.rollback()
            ids_ja_tentados.append(candidato["id"])
            continue
        conn.commit()
        contrato = conn.execute("SELECT * FROM contratos_backlog WHERE id=?", (candidato["id"],)).fetchone()
        conn.close()
        return contrato

    conn.close()
    return None


def _intervalo_mes_atual():
    hoje = datetime.now().date()
    primeiro = hoje.replace(day=1)
    if hoje.month == 12:
        proximo_mes = hoje.replace(year=hoje.year + 1, month=1, day=1)
    else:
        proximo_mes = hoje.replace(month=hoje.month + 1, day=1)
    ultimo = proximo_mes - timedelta(days=1)
    return primeiro.strftime("%Y-%m-%d"), ultimo.strftime("%Y-%m-%d")


def calcular_resumo_backlog(conn):
    """KPI do topo do Painel: total pendente (fila ativa apta a tratar --
    PENDENTE + EM_ANDAMENTO + AGUARDANDO_RETORNO conta como "ainda em
    trâmite", mas o pedido foi específico: pendente = aging passado/sem
    agenda ainda não fechado -- então PENDENTE + EM_ANDAMENTO), quantidade
    já tratada (fechamentos reais dados por operador, não resoluções
    automáticas de reimportação) e % tratado."""
    acordar_retornos_vencidos()
    pendente = conn.execute(
        "SELECT COUNT(*) c FROM contratos_backlog WHERE status IN ('PENDENTE', 'EM_ANDAMENTO')"
    ).fetchone()["c"]
    tratado = conn.execute(
        "SELECT COUNT(DISTINCT contrato_backlog_id) c FROM tratamentos_backlog "
        "WHERE fechamento IN ({})".format(",".join("?" * len(FECHAMENTOS_VALIDOS))),
        FECHAMENTOS_VALIDOS
    ).fetchone()["c"]
    total = pendente + tratado
    pct = round(tratado / total * 100, 1) if total else 0.0
    return {"total_pendente": pendente, "total_tratado": tratado, "total": total, "pct_tratado": pct}


def calcular_painel_por_ddd(conn):
    """Mesma quebra do resumo, aberta por DDD."""
    linhas_pendente = conn.execute("""
        SELECT ddd, COUNT(*) c FROM contratos_backlog
        WHERE status IN ('PENDENTE', 'EM_ANDAMENTO') GROUP BY ddd
    """).fetchall()
    linhas_tratado = conn.execute(f"""
        SELECT cb.ddd ddd, COUNT(DISTINCT t.contrato_backlog_id) c
        FROM tratamentos_backlog t
        JOIN contratos_backlog cb ON cb.id = t.contrato_backlog_id
        WHERE t.fechamento IN ({",".join("?" * len(FECHAMENTOS_VALIDOS))})
        GROUP BY cb.ddd
    """, FECHAMENTOS_VALIDOS).fetchall()

    por_ddd = {}
    for l in linhas_pendente:
        por_ddd.setdefault(l["ddd"] or "—", {"pendente": 0, "tratado": 0})["pendente"] = l["c"]
    for l in linhas_tratado:
        por_ddd.setdefault(l["ddd"] or "—", {"pendente": 0, "tratado": 0})["tratado"] = l["c"]

    saida = []
    for ddd, d in por_ddd.items():
        total = d["pendente"] + d["tratado"]
        pct = round(d["tratado"] / total * 100, 1) if total else 0.0
        saida.append({"ddd": ddd, "pendente": d["pendente"], "tratado": d["tratado"], "total": total, "pct": pct})
    saida.sort(key=lambda x: (x["ddd"] == "—", x["ddd"]))
    return saida


def calcular_evolucao_backlog(conn, data_inicio, data_fim):
    """Série diária pro gráfico: barra = tratados no dia (data_tratamento
    dos fechamentos reais), linha (2º eixo) = total pendente acumulado
    (contratos com data_entrada_fila <= dia e ainda sem fechamento real até
    o fim daquele dia)."""
    dias = []
    d = datetime.strptime(data_inicio, "%Y-%m-%d").date()
    fim = datetime.strptime(data_fim, "%Y-%m-%d").date()
    while d <= fim:
        dias.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)

    tratados_por_dia = {r["dia"]: r["c"] for r in conn.execute(f"""
        SELECT date(data_tratamento) dia, COUNT(DISTINCT contrato_backlog_id) c
        FROM tratamentos_backlog
        WHERE fechamento IN ({",".join("?" * len(FECHAMENTOS_VALIDOS))})
        GROUP BY date(data_tratamento)
    """, FECHAMENTOS_VALIDOS).fetchall()}

    todos_contratos = conn.execute(
        "SELECT id, data_entrada_fila FROM contratos_backlog"
    ).fetchall()
    primeiro_fechamento = {r["contrato_backlog_id"]: r["dia"] for r in conn.execute(f"""
        SELECT contrato_backlog_id, MIN(date(data_tratamento)) dia
        FROM tratamentos_backlog
        WHERE fechamento IN ({",".join("?" * len(FECHAMENTOS_VALIDOS))})
        GROUP BY contrato_backlog_id
    """, FECHAMENTOS_VALIDOS).fetchall()}

    serie_tratados = []
    serie_pendente = []
    for dia in dias:
        serie_tratados.append(tratados_por_dia.get(dia, 0))
        pendente_no_dia = 0
        for c in todos_contratos:
            if c["data_entrada_fila"] and c["data_entrada_fila"] <= dia:
                fechado_em = primeiro_fechamento.get(c["id"])
                if not fechado_em or fechado_em > dia:
                    pendente_no_dia += 1
        serie_pendente.append(pendente_no_dia)

    return {"dias": dias, "tratados": serie_tratados, "pendente": serie_pendente}


# ==========================
# ROTAS
# ==========================

@bp_backlog.route("/backlog")
def backlog_painel():
    from app import usuario_atual
    usuario = usuario_atual()
    if not usuario:
        return redirect("/login")

    data_inicio_default, data_fim_default = _intervalo_mes_atual()
    data_inicio = request.args.get("data_inicio") or data_inicio_default
    data_fim = request.args.get("data_fim") or data_fim_default

    conn = conectar_backlog()
    resumo = calcular_resumo_backlog(conn)
    painel_ddd = calcular_painel_por_ddd(conn)
    evolucao = calcular_evolucao_backlog(conn, data_inicio, data_fim)
    conn.close()

    return render_template(
        "BacklogPainel.html", resumo=resumo, painel_ddd=painel_ddd, evolucao=evolucao,
        data_inicio=data_inicio, data_fim=data_fim, usuario_backlog=usuario
    )


@bp_backlog.route("/backlog/atender")
def backlog_atender():
    from app import usuario_atual
    usuario = usuario_atual()
    if not usuario:
        return redirect("/login")

    contrato = buscar_proximo_backlog(usuario["nome"])
    historico = []
    if contrato:
        conn = conectar_backlog()
        historico = conn.execute(
            "SELECT * FROM tratamentos_backlog WHERE contrato_backlog_id=? ORDER BY id DESC",
            (contrato["id"],)
        ).fetchall()
        conn.close()

    return render_template(
        "BacklogTratamento.html", contrato=contrato, historico=historico,
        fechamentos=FECHAMENTOS_VALIDOS, labels_fechamento=LABELS_FECHAMENTO, usuario_backlog=usuario
    )


def _salvar_tratamento_backlog(contrato, usuario, form):
    """Compartilhado entre /backlog/tratar (fila) e /backlog/buscar/tratar
    (revisão manual) -- grava o histórico (nunca sobrescreve), o novo
    status/ultimo_fechamento, e (só pra AGENDADO) a atividade real no
    crm.db."""
    fechamento = form.get("fechamento")
    if fechamento not in FECHAMENTOS_VALIDOS:
        return False

    observacao = (form.get("observacao") or "").strip()
    agora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    nova_data_agendamento = novo_periodo = novo_resp_cliente = None
    novo_flag_mdu = 0
    retorno_em = None

    if fechamento == "AGENDADO":
        nova_data_agendamento = (form.get("nova_data_agendamento") or "").strip() or None
        novo_periodo = (form.get("novo_periodo") or "").strip() or None
        novo_resp_cliente = (form.get("novo_resp_cliente") or "").strip() or None
        novo_flag_mdu = 1 if form.get("novo_flag_mdu") else 0
        if not nova_data_agendamento or not novo_periodo:
            return False
    elif fechamento in FECHAMENTOS_RETORNO:
        retorno_data = (form.get("retorno_data") or "").strip()
        retorno_hora = (form.get("retorno_hora") or "08:00").strip() or "08:00"
        if not retorno_data:
            return False
        retorno_em = f"{retorno_data} {retorno_hora}:00"

    conn = conectar_backlog()
    conn.execute("""
        INSERT INTO tratamentos_backlog
            (contrato_backlog_id, cd_operadora, cd_contrato, operador, fechamento, observacao,
             nova_data_agendamento, novo_periodo, novo_resp_cliente, novo_flag_mdu, retorno_em,
             data_tratamento)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (contrato["id"], contrato["cd_operadora"], contrato["cd_contrato"], usuario["nome"],
          fechamento, observacao, nova_data_agendamento, novo_periodo, novo_resp_cliente,
          novo_flag_mdu, retorno_em, agora_str))

    if fechamento in FECHAMENTOS_RETORNO:
        conn.execute("""
            UPDATE contratos_backlog SET status='AGUARDANDO_RETORNO', ultimo_fechamento=?,
                operador_atual=NULL, retorno_em=?, atualizado_em=? WHERE id=?
        """, (fechamento, retorno_em, agora_str, contrato["id"]))
    else:
        conn.execute("""
            UPDATE contratos_backlog SET status='RESOLVIDO', ultimo_fechamento=?,
                operador_atual=NULL, atualizado_em=? WHERE id=?
        """, (fechamento, agora_str, contrato["id"]))

    conn.commit()
    conn.close()

    if fechamento == "AGENDADO":
        _gravar_atividade_crm_agendado(contrato, usuario, nova_data_agendamento, novo_periodo,
                                        novo_resp_cliente, novo_flag_mdu, observacao, agora_str)
    return True


def _gravar_atividade_crm_agendado(contrato, usuario, nova_data_agendamento, novo_periodo,
                                    novo_resp_cliente, novo_flag_mdu, observacao, agora_str):
    """Grava uma linha real em atividades do crm.db pro fechamento AGENDADO
    contar de verdade como agendamento no CRM -- confirmado com o Bruno,
    2026-09-19. tentativa=1 e minutos_efetivos=NULL (contrato de backlog
    não vem de ligação/mailing, não tem tentativa nem tempo de tela reais
    -- também confirmado com o Bruno).

    id_mailing (pedido do Bruno, 2026-09-20): o mesmo número de contrato
    pode existir em mais de uma linha de mailing (cidades diferentes) --
    sem vincular a linha certa, essa atividade de Backlog aparecia
    misturada no histórico de QUALQUER contrato com esse número, cidade
    nenhuma garantida (ver _garantir_coluna_id_mailing_atividades_followup,
    em app.py). Resolve por contrato+cidade (contratos_backlog já tem a
    cidade) -- fica NULL se não achar nenhuma linha de mailing com essa
    combinação (contrato de backlog que nunca existiu no mailing
    principal, caso comum)."""
    from app import conectar as conectar_crm
    observacao_completa = f"[Backlog] {observacao}".strip() if observacao else "[Backlog]"
    conn_crm = conectar_crm()
    linha_mailing = conn_crm.execute(
        "SELECT id FROM mailing WHERE contrato=? AND cidade=?",
        (contrato["cd_contrato"], contrato["cidade"])
    ).fetchone()
    id_mailing = linha_mailing["id"] if linha_mailing else None
    conn_crm.execute("""
        INSERT INTO atividades (contrato, id_mailing, operador, tentativa, tabulacao, canal_contato, observacao,
                                 data_inicio, data_fim, status, data_agendamento, janela_agendamento,
                                 resp_cliente, flag_mdu, minutos_efetivos)
        VALUES (?, ?, ?, 1, 'Agendado', NULL, ?, ?, ?, 'CONCLUIDO', ?, ?, ?, ?, NULL)
    """, (contrato["cd_contrato"], id_mailing, usuario["nome"], observacao_completa, agora_str, agora_str,
          nova_data_agendamento, novo_periodo, novo_resp_cliente, novo_flag_mdu))
    conn_crm.commit()
    conn_crm.close()


@bp_backlog.route("/backlog/tratar", methods=["POST"])
def backlog_tratar():
    from app import usuario_atual
    usuario = usuario_atual()
    if not usuario:
        return redirect("/login")

    id_contrato = request.form.get("id_contrato")
    conn = conectar_backlog()
    contrato = conn.execute(
        "SELECT * FROM contratos_backlog WHERE id=? AND operador_atual=? AND status='EM_ANDAMENTO'",
        (id_contrato, usuario["nome"])
    ).fetchone()
    conn.close()
    if not contrato:
        return redirect("/backlog/atender")

    _salvar_tratamento_backlog(contrato, usuario, request.form)
    return redirect("/backlog/atender")


@bp_backlog.route("/backlog/buscar")
def backlog_buscar():
    from app import usuario_atual
    usuario = usuario_atual()
    if not usuario:
        return redirect("/login")

    cd_contrato = (request.args.get("contrato") or "").strip()
    contrato = None
    historico = []
    buscou = False

    if cd_contrato:
        buscou = True
        conn = conectar_backlog()
        contrato = conn.execute(
            "SELECT * FROM contratos_backlog WHERE cd_contrato=? ORDER BY id DESC LIMIT 1",
            (cd_contrato,)
        ).fetchone()
        if contrato:
            historico = conn.execute(
                "SELECT * FROM tratamentos_backlog WHERE contrato_backlog_id=? ORDER BY id DESC",
                (contrato["id"],)
            ).fetchall()
        conn.close()

    return render_template(
        "BacklogBuscar.html", contrato=contrato, historico=historico, buscou=buscou,
        cd_contrato=cd_contrato, fechamentos=FECHAMENTOS_VALIDOS, labels_fechamento=LABELS_FECHAMENTO,
        usuario_backlog=usuario
    )


@bp_backlog.route("/backlog/buscar/tratar", methods=["POST"])
def backlog_buscar_tratar():
    from app import usuario_atual
    usuario = usuario_atual()
    if not usuario:
        return redirect("/login")

    id_contrato = request.form.get("id_contrato")
    conn = conectar_backlog()
    contrato = conn.execute("SELECT * FROM contratos_backlog WHERE id=?", (id_contrato,)).fetchone()
    conn.close()
    if not contrato:
        return redirect("/backlog/buscar")

    _salvar_tratamento_backlog(contrato, usuario, request.form)
    return redirect(f"/backlog/buscar?contrato={contrato['cd_contrato']}")


@bp_backlog.route("/backlog/extracao")
def backlog_extracao():
    from app import usuario_atual
    usuario = usuario_atual()
    if not usuario or usuario["perfil"] != "ADM":
        return "Acesso restrito.", 403

    data_inicio_default, data_fim_default = _intervalo_mes_atual()
    data_de = request.args.get("data_de") or data_inicio_default
    data_ate = request.args.get("data_ate") or data_fim_default
    operador_filtro = request.args.get("operador") or ""
    fechamento_filtro = request.args.get("fechamento") or ""

    conn = conectar_backlog()
    condicoes = ["date(t.data_tratamento) BETWEEN ? AND ?", "t.fechamento != 'REENFILEIRADO_REIMPORTACAO'"]
    params = [data_de, data_ate]
    if operador_filtro:
        condicoes.append("t.operador = ?")
        params.append(operador_filtro)
    if fechamento_filtro:
        condicoes.append("t.fechamento = ?")
        params.append(fechamento_filtro)

    tratamentos = conn.execute(f"""
        SELECT t.*, c.ddd, c.cidade, c.produto_de, c.produto_para, c.vendedor
        FROM tratamentos_backlog t
        LEFT JOIN contratos_backlog c ON c.id = t.contrato_backlog_id
        WHERE {" AND ".join(condicoes)}
        ORDER BY t.data_tratamento DESC
    """, params).fetchall()
    operadores_distintos = [r["operador"] for r in conn.execute(
        "SELECT DISTINCT operador FROM tratamentos_backlog "
        "WHERE fechamento != 'REENFILEIRADO_REIMPORTACAO' AND operador != 'SISTEMA (reimportação)' "
        "ORDER BY operador"
    ).fetchall()]
    conn.close()

    if request.args.get("exportar"):
        registros = [{
            "Contrato": t["cd_contrato"], "Operadora": t["cd_operadora"], "DDD": t["ddd"],
            "Cidade": t["cidade"], "Produto De": t["produto_de"], "Produto Para": t["produto_para"],
            "Vendedor": t["vendedor"], "Fechamento": LABELS_FECHAMENTO.get(t["fechamento"], t["fechamento"]),
            "Nova Data (Agendado)": t["nova_data_agendamento"], "Novo Período": t["novo_periodo"],
            "Resp. Cliente": t["novo_resp_cliente"], "MDU": t["novo_flag_mdu"],
            "Retorno em": t["retorno_em"], "Observação": t["observacao"],
            "Operador": t["operador"], "Data Tratamento": t["data_tratamento"],
        } for t in tratamentos]
        df = pd.DataFrame(registros)
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Backlog")
        buffer.seek(0)
        return send_file(
            buffer, as_attachment=True,
            download_name=f"backlog_{data_de}_a_{data_ate}.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

    return render_template(
        "BacklogExtracao.html", tratamentos=tratamentos, data_de=data_de, data_ate=data_ate,
        operador_filtro=operador_filtro, fechamento_filtro=fechamento_filtro,
        operadores_distintos=operadores_distintos, fechamentos=FECHAMENTOS_VALIDOS,
        labels_fechamento=LABELS_FECHAMENTO, usuario_backlog=usuario
    )
