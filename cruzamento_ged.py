"""
Módulo Cruzamento com a Base GED -- pedido do Bruno, 2026-10-16.

Toda segunda-feira o Bruno extrai a base de endereços completa (outro
projeto, automação própria) e chega numa lista enxuta com o status
comercial de cada HP/contrato:

    COD_OPERADORA;CONTRATO;HP;COD_IMOVEL;STATUS_COMERCIAL;NODE;CIDADE

Essa lista (CSV com ";" -- ou o .zip dela, que cai de 110 MB pra 15 MB)
sobe aqui e é cruzada com a fila do CRM. Regras confirmadas com o Bruno:

- Chave: CONTRATO + CIDADE (mesmo critério da importação do mailing -- o
  mesmo número de contrato existe em cidades diferentes).
- VENDA LIBERADA -> continua apto a ser distribuído.
- VENDA BLOQUEADA, VENDA NAO LIBERADA ou contrato NÃO ENCONTRADO no GED
  -> sai da fila e entra no relatório de acompanhamento:
    * PENDENTE -> sai.
    * ENFILEIRADO de 2ª/3ª tentativa -> sai.
    * ENFILEIRADO com RETORNO agendado com o cliente -> FICA com o
      operador, mas a tela do contrato mostra um aviso pra ele conferir o
      status no sistema antes de fechar o agendamento.
    * EM_ANDAMENTO (alguém com o contrato na tela agora) -> não é mexido;
      se continuar bloqueado, sai no cruzamento seguinte.
- Quem saiu e na semana seguinte aparece como VENDA LIBERADA volta
  sozinho pro status de antes (PENDENTE, ou ENFILEIRADO com o followup do
  mesmo operador -- o followup nunca é apagado, só fica invisível pra
  distribuição enquanto o contrato está BLOQUEADO_GED, porque a fila de
  followup exige m.status='ENFILEIRADO').
- Upload/aplicação: só ADM. Relatório: permissão "relatorio_ged" na
  matriz de Permissões (ADM sempre tem; demais perfis desligados por
  padrão).

Como sai da fila: status novo no mailing, BLOQUEADO_GED (mesmo padrão do
AGUARDANDO_ANALISE_GPON). As duas consultas de distribuição só olham
PENDENTE/ENFILEIRADO, então nenhuma linha de buscar_proximo_cliente
precisou mudar. O status de antes fica guardado em ged_bloqueios.

Duas etapas, de propósito: o upload só SIMULA (mostra quantos saem,
quantos voltam, quais cidades não vieram no arquivo); nada muda na fila
até o ADM clicar em Aplicar. Um arquivo errado ou truncado (ex: só metade
das cidades) tiraria milhares de contratos da fila de uma vez -- a
simulação deixa isso visível antes. Cidade da fila que não aparece em
NENHUMA linha do arquivo nem é avaliada (fica como está).

Mesmo padrão dos outros blueprints: login/permissão importados do app.py
dentro de cada rota, pra evitar import circular.
"""
import csv
import io
import json
import os
import re
import sqlite3
import unicodedata
import zipfile
from datetime import datetime

from flask import Blueprint, render_template, request, redirect, send_file

bp_cruzamento_ged = Blueprint("cruzamento_ged", __name__)

PASTA_BASE_GED = os.path.dirname(os.path.abspath(__file__))

STATUS_LIBERADA = "VENDA LIBERADA"
STATUS_NAO_ENCONTRADO = "NAO ENCONTRADO NO GED"
STATUS_FILA = ("PENDENTE", "ENFILEIRADO")
STATUS_BLOQUEADO = "BLOQUEADO_GED"

# Quantos cruzamentos guardam o detalhe linha a linha (ged_avaliacao).
# O resumo de cada cruzamento (ged_cruzamentos) e o histórico de
# bloqueios (ged_bloqueios) ficam pra sempre.
CRUZAMENTOS_COM_DETALHE = 12

COLUNAS_OBRIGATORIAS = ("CONTRATO", "CIDADE", "STATUS_COMERCIAL")

ROTULO_ACAO = {
    "MANTEM": "Continua na fila",
    "SAI": "Sai da fila",
    "SINALIZA": "Fica com o operador (Retorno agendado) + aviso",
    "VOLTA": "Volta pra fila",
    "CONTINUA": "Continua fora da fila",
    "NAO_AVALIADO": "Não avaliado (cidade fora do arquivo)",
}


def _conectar():
    conn = sqlite3.connect(os.path.join(PASTA_BASE_GED, "crm.db"))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def garantir_tabelas_ged():
    """Auto-migração idempotente -- só cria o que ainda não existe."""
    conn = _conectar()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS ged_cruzamentos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            arquivo TEXT,
            data_base TEXT,
            enviado_por TEXT,
            enviado_em TEXT,
            situacao TEXT,              -- SIMULADO / APLICADO / DESCARTADO
            aplicado_por TEXT,
            aplicado_em TEXT,
            linhas_lidas INTEGER,
            linhas_com_contrato INTEGER,
            cidades_arquivo TEXT,       -- JSON
            cidades_fora TEXT,          -- JSON: cidades da fila que não vieram no arquivo
            resumo TEXT                 -- JSON: contagens por ação / status
        );
        CREATE TABLE IF NOT EXISTS ged_avaliacao (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            id_cruzamento INTEGER,
            id_mailing INTEGER,
            contrato TEXT,
            cidade TEXT,
            status_mailing TEXT,
            status_ged TEXT,
            node_ged TEXT,
            acao TEXT,
            operador_followup TEXT,
            tipo_followup TEXT,
            data_retorno TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_ged_avaliacao_cruz ON ged_avaliacao(id_cruzamento, acao);
        CREATE TABLE IF NOT EXISTS ged_bloqueios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            id_mailing INTEGER,
            contrato TEXT,
            cidade TEXT,
            status_anterior TEXT,
            motivo TEXT,
            operador_followup TEXT,
            id_cruzamento_saida INTEGER,
            saiu_em TEXT,
            id_cruzamento_ultimo INTEGER,
            ativo INTEGER DEFAULT 1,
            encerramento TEXT,          -- VOLTOU_FILA / TRATADO_FORA
            id_cruzamento_encerramento INTEGER,
            encerrado_em TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_ged_bloqueios_ativo ON ged_bloqueios(ativo, id_mailing);
        CREATE TABLE IF NOT EXISTS ged_status_contrato (
            id_mailing INTEGER PRIMARY KEY,
            contrato TEXT,
            cidade TEXT,
            status_ged TEXT,
            id_cruzamento INTEGER,
            data_base TEXT,
            atualizado_em TEXT
        );
    """)
    conn.commit()
    conn.close()


garantir_tabelas_ged()


# ==========================
# NORMALIZAÇÃO
# ==========================

def _norm_contrato(valor):
    s = str(valor or "").strip()
    if s.endswith(".0"):
        s = s[:-2]
    s = s.lstrip("0")
    return s


def _norm_cidade(valor):
    s = unicodedata.normalize("NFKD", str(valor or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", s).strip().upper()


def _norm_status(valor):
    s = unicodedata.normalize("NFKD", str(valor or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", s).strip().upper()


def _data_base_do_nome(nome_arquivo):
    """relatorio_colunas_selecionadas_20261014.zip -> 2026-10-14."""
    m = re.search(r"(20\d{2})(\d{2})(\d{2})", nome_arquivo or "")
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).strftime("%Y-%m-%d")
    except ValueError:
        return None


# ==========================
# LEITURA DO ARQUIVO
# ==========================

def _abrir_texto(arquivo_storage):
    """Devolve um stream de texto do CSV -- direto, ou de dentro do .zip
    (primeiro .csv que encontrar). Lido em fluxo: o CSV inteiro (1,8 mi de
    linhas) nunca é carregado na memória de uma vez."""
    nome = (arquivo_storage.filename or "").lower()
    if nome.endswith(".zip"):
        zf = zipfile.ZipFile(arquivo_storage.stream)
        membros = [n for n in zf.namelist() if n.lower().endswith(".csv") and not n.startswith("__MACOSX")]
        if not membros:
            raise ValueError("O .zip não tem nenhum arquivo .csv dentro.")
        bruto = zf.open(membros[0])
    elif nome.endswith(".csv") or nome.endswith(".txt"):
        bruto = arquivo_storage.stream
    else:
        raise ValueError("Envie o .zip ou o .csv da base GED.")
    return io.TextIOWrapper(bruto, encoding="utf-8-sig", errors="replace", newline="")


def ler_base_ged(arquivo_storage, chaves_interesse):
    """Lê a base GED guardando só as linhas cuja chave (contrato, cidade)
    está em `chaves_interesse` -- de 1,8 mi de linhas sobram uns poucos
    milhares. Devolve (status_por_chave, node_por_chave, estatisticas)."""
    texto = _abrir_texto(arquivo_storage)
    primeira = texto.readline()
    if not primeira:
        raise ValueError("Arquivo vazio.")
    delimitador = ";" if primeira.count(";") >= primeira.count(",") else ","
    cabecalho = [c.strip().strip('"').upper() for c in primeira.rstrip("\r\n").split(delimitador)]
    faltando = [c for c in COLUNAS_OBRIGATORIAS if c not in cabecalho]
    if faltando:
        raise ValueError("O arquivo está sem a(s) coluna(s): " + ", ".join(faltando)
                         + ". Cabeçalho encontrado: " + ", ".join(cabecalho))
    i_contrato = cabecalho.index("CONTRATO")
    i_cidade = cabecalho.index("CIDADE")
    i_status = cabecalho.index("STATUS_COMERCIAL")
    i_node = cabecalho.index("NODE") if "NODE" in cabecalho else None
    maior_indice = max(i for i in (i_contrato, i_cidade, i_status, i_node) if i is not None)

    status_por_chave = {}
    node_por_chave = {}
    cidades_arquivo = set()
    status_arquivo = {}
    linhas = com_contrato = 0
    cidade_cache = {}

    for linha in csv.reader(texto, delimiter=delimitador):
        if len(linha) <= maior_indice:
            continue
        linhas += 1
        cidade_bruta = linha[i_cidade]
        cidade = cidade_cache.get(cidade_bruta)
        if cidade is None:
            cidade = cidade_cache[cidade_bruta] = _norm_cidade(cidade_bruta)
        cidades_arquivo.add(cidade)
        contrato = _norm_contrato(linha[i_contrato])
        if not contrato:
            continue
        com_contrato += 1
        status = _norm_status(linha[i_status]) or "SEM STATUS"
        status_arquivo[status] = status_arquivo.get(status, 0) + 1
        chave = (contrato, cidade)
        if chave in chaves_interesse:
            status_por_chave.setdefault(chave, set()).add(status)
            if i_node is not None and chave not in node_por_chave:
                node_por_chave[chave] = linha[i_node].strip()

    if linhas == 0:
        raise ValueError("Nenhuma linha de dados encontrada no arquivo.")
    return status_por_chave, node_por_chave, {
        "linhas_lidas": linhas,
        "linhas_com_contrato": com_contrato,
        "cidades_arquivo": sorted(cidades_arquivo),
        "status_arquivo": status_arquivo,
    }


def _status_consolidado(conjunto):
    """O mesmo contrato pode vir em mais de um HP (1,3 mil casos na base de
    14/10, nenhum deles na fila com status divergente). Se QUALQUER HP
    estiver liberado, o contrato é atendível -> liberado. Senão, bloqueada
    vence não liberada (só pra ter um motivo determinístico)."""
    if not conjunto:
        return STATUS_NAO_ENCONTRADO
    if STATUS_LIBERADA in conjunto:
        return STATUS_LIBERADA
    if "VENDA BLOQUEADA" in conjunto:
        return "VENDA BLOQUEADA"
    return sorted(conjunto)[0]


# ==========================
# SIMULAÇÃO E APLICAÇÃO
# ==========================

def _followups_pendentes(conn, ids_mailing_por_contrato):
    """id_mailing -> (operador, tipo, data_retorno) do followup PENDENTE,
    RETORNO tendo precedência sobre FOLLOWUP comum. Followup antigo sem
    id_mailing cai pro contrato (mesmo critério do resto do app)."""
    resultado = {}
    for f in conn.execute("""
        SELECT id_mailing, contrato, operador, tipo, data_retorno FROM followup
        WHERE status='PENDENTE' ORDER BY id
    """):
        if f["id_mailing"] is not None:
            alvos = [f["id_mailing"]]
        else:
            alvos = ids_mailing_por_contrato.get(_norm_contrato(f["contrato"]), [])
        for id_m in alvos:
            atual = resultado.get(id_m)
            if atual is None or (atual[1] != "RETORNO" and f["tipo"] == "RETORNO") or \
                    (atual[1] == f["tipo"]):
                resultado[id_m] = (f["operador"], f["tipo"] or "FOLLOWUP", f["data_retorno"])
    return resultado


def simular_cruzamento(conn, arquivo_storage, usuario_nome):
    """Lê o arquivo, avalia cada contrato da fila e grava a simulação.
    Não mexe em nenhum status do mailing."""
    escopo = conn.execute(f"""
        SELECT id, contrato, cidade, status FROM mailing
        WHERE status IN ('PENDENTE', 'ENFILEIRADO', '{STATUS_BLOQUEADO}')
    """).fetchall()
    chaves = {}
    ids_por_contrato = {}
    for m in escopo:
        chave = (_norm_contrato(m["contrato"]), _norm_cidade(m["cidade"]))
        chaves.setdefault(chave, []).append(m)
        ids_por_contrato.setdefault(chave[0], []).append(m["id"])

    status_por_chave, node_por_chave, est = ler_base_ged(arquivo_storage, set(chaves))
    cidades_arquivo = set(est["cidades_arquivo"])
    followups = _followups_pendentes(conn, ids_por_contrato)

    agora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data_base = _data_base_do_nome(arquivo_storage.filename) or datetime.now().strftime("%Y-%m-%d")

    # Simulação anterior que ninguém aplicou fica descartada -- só a última vale.
    conn.execute("UPDATE ged_cruzamentos SET situacao='DESCARTADO' WHERE situacao='SIMULADO'")
    cur = conn.execute("""
        INSERT INTO ged_cruzamentos (arquivo, data_base, enviado_por, enviado_em, situacao,
                                     linhas_lidas, linhas_com_contrato, cidades_arquivo)
        VALUES (?, ?, ?, ?, 'SIMULADO', ?, ?, ?)
    """, (arquivo_storage.filename, data_base, usuario_nome, agora_str,
          est["linhas_lidas"], est["linhas_com_contrato"], json.dumps(est["cidades_arquivo"])))
    id_cruz = cur.lastrowid

    linhas = []
    cidades_fora = {}
    for chave, registros in chaves.items():
        contrato_n, cidade_n = chave
        for m in registros:
            fu = followups.get(m["id"], (None, None, None))
            if cidade_n not in cidades_arquivo:
                acao, status_ged = "NAO_AVALIADO", None
                if m["status"] in STATUS_FILA:
                    cidades_fora[m["cidade"]] = cidades_fora.get(m["cidade"], 0) + 1
            else:
                status_ged = _status_consolidado(status_por_chave.get(chave))
                liberada = status_ged == STATUS_LIBERADA
                if m["status"] == STATUS_BLOQUEADO:
                    acao = "VOLTA" if liberada else "CONTINUA"
                elif liberada:
                    acao = "MANTEM"
                elif m["status"] == "ENFILEIRADO" and fu[1] == "RETORNO":
                    acao = "SINALIZA"
                else:
                    acao = "SAI"
            linhas.append((id_cruz, m["id"], m["contrato"], m["cidade"], m["status"], status_ged,
                           node_por_chave.get(chave), acao, fu[0], fu[1], fu[2]))

    conn.executemany("""
        INSERT INTO ged_avaliacao (id_cruzamento, id_mailing, contrato, cidade, status_mailing,
                                   status_ged, node_ged, acao, operador_followup, tipo_followup, data_retorno)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, linhas)

    resumo = {
        "status_arquivo": est["status_arquivo"],
        "simulado": _contar_acoes(conn, id_cruz),
    }
    conn.execute("UPDATE ged_cruzamentos SET cidades_fora=?, resumo=? WHERE id=?",
                 (json.dumps(cidades_fora, ensure_ascii=False), json.dumps(resumo, ensure_ascii=False), id_cruz))
    _limpar_detalhe_antigo(conn)
    conn.commit()
    return id_cruz


def _contar_acoes(conn, id_cruz):
    """{'por_acao': {...}, 'por_status_acao': {'PENDENTE|VENDA BLOQUEADA|SAI': n, ...}}"""
    por_acao = {}
    por_status = {}
    for r in conn.execute("""
        SELECT acao, status_mailing, COALESCE(status_ged, '-') AS sg, COUNT(*) AS n
        FROM ged_avaliacao WHERE id_cruzamento=? GROUP BY 1, 2, 3
    """, (id_cruz,)):
        por_acao[r["acao"]] = por_acao.get(r["acao"], 0) + r["n"]
        por_status[f"{r['status_mailing']}|{r['sg']}|{r['acao']}"] = r["n"]
    return {"por_acao": por_acao, "por_status_acao": por_status}


def _limpar_detalhe_antigo(conn):
    manter = [r["id"] for r in conn.execute(
        "SELECT id FROM ged_cruzamentos ORDER BY id DESC LIMIT ?", (CRUZAMENTOS_COM_DETALHE,))]
    if manter:
        marc = ",".join("?" * len(manter))
        conn.execute(f"DELETE FROM ged_avaliacao WHERE id_cruzamento NOT IN ({marc})", manter)


def aplicar_cruzamento(conn, id_cruz, usuario_nome):
    """Aplica a simulação. Cada mudança confere se o contrato ainda está no
    mesmo status da simulação (UPDATE ... WHERE status=?) -- se alguém
    tratou o contrato nesse meio tempo, ele é pulado, nunca atropelado."""
    cruz = conn.execute("SELECT * FROM ged_cruzamentos WHERE id=?", (id_cruz,)).fetchone()
    if not cruz or cruz["situacao"] != "SIMULADO":
        return None
    agora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    real = {"saiu": 0, "voltou": 0, "continua": 0, "sinalizado": 0, "mantido": 0,
            "pulado": 0, "tratado_fora": 0}

    ativos = {b["id_mailing"]: b for b in conn.execute(
        "SELECT * FROM ged_bloqueios WHERE ativo=1")}

    for av in conn.execute(
            "SELECT * FROM ged_avaliacao WHERE id_cruzamento=? AND acao != 'NAO_AVALIADO'",
            (id_cruz,)).fetchall():
        acao = av["acao"]
        if acao == "SAI":
            cur = conn.execute(f"UPDATE mailing SET status='{STATUS_BLOQUEADO}' WHERE id=? AND status=?",
                               (av["id_mailing"], av["status_mailing"]))
            if cur.rowcount:
                conn.execute("""
                    INSERT INTO ged_bloqueios (id_mailing, contrato, cidade, status_anterior, motivo,
                        operador_followup, id_cruzamento_saida, saiu_em, id_cruzamento_ultimo, ativo)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """, (av["id_mailing"], av["contrato"], av["cidade"], av["status_mailing"],
                      av["status_ged"], av["operador_followup"], id_cruz, agora_str, id_cruz))
                real["saiu"] += 1
            else:
                real["pulado"] += 1
        elif acao == "VOLTA":
            b = ativos.get(av["id_mailing"])
            anterior = b["status_anterior"] if b and b["status_anterior"] in STATUS_FILA else "PENDENTE"
            cur = conn.execute(f"UPDATE mailing SET status=? WHERE id=? AND status='{STATUS_BLOQUEADO}'",
                               (anterior, av["id_mailing"]))
            if cur.rowcount:
                real["voltou"] += 1
            else:
                real["pulado"] += 1
            if b:
                conn.execute("""
                    UPDATE ged_bloqueios SET ativo=0, encerramento='VOLTOU_FILA',
                        id_cruzamento_encerramento=?, encerrado_em=?, id_cruzamento_ultimo=?
                    WHERE id=?
                """, (id_cruz, agora_str, id_cruz, b["id"]))
        elif acao == "CONTINUA":
            b = ativos.get(av["id_mailing"])
            if b:
                conn.execute("UPDATE ged_bloqueios SET motivo=?, id_cruzamento_ultimo=? WHERE id=?",
                             (av["status_ged"], id_cruz, b["id"]))
            real["continua"] += 1
        elif acao == "SINALIZA":
            real["sinalizado"] += 1
        elif acao == "MANTEM":
            real["mantido"] += 1

        conn.execute("""
            INSERT INTO ged_status_contrato (id_mailing, contrato, cidade, status_ged, id_cruzamento, data_base, atualizado_em)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id_mailing) DO UPDATE SET status_ged=excluded.status_ged,
                id_cruzamento=excluded.id_cruzamento, data_base=excluded.data_base,
                atualizado_em=excluded.atualizado_em
        """, (av["id_mailing"], av["contrato"], av["cidade"], av["status_ged"], id_cruz,
              cruz["data_base"], agora_str))

    # Bloqueio ativo cujo contrato já não está mais BLOQUEADO_GED: alguém
    # tratou por outro caminho (Buscar Contrato, Nova Tarefa, transferência)
    # -- encerra o bloqueio pra não ficar no relatório como se ainda estivesse fora.
    cur = conn.execute(f"""
        UPDATE ged_bloqueios SET ativo=0, encerramento='TRATADO_FORA',
            id_cruzamento_encerramento=?, encerrado_em=?
        WHERE ativo=1 AND id_mailing NOT IN (SELECT id FROM mailing WHERE status='{STATUS_BLOQUEADO}')
    """, (id_cruz, agora_str))
    real["tratado_fora"] = cur.rowcount

    resumo = json.loads(cruz["resumo"] or "{}")
    resumo["aplicado"] = real
    conn.execute("""
        UPDATE ged_cruzamentos SET situacao='APLICADO', aplicado_por=?, aplicado_em=?, resumo=? WHERE id=?
    """, (usuario_nome, agora_str, json.dumps(resumo, ensure_ascii=False), id_cruz))
    conn.commit()
    return real


# ==========================
# CONSULTAS DO RELATÓRIO
# ==========================

def _bloqueios_ativos(conn, cidade="", motivo=""):
    filtros, params = ["b.ativo=1"], []
    if cidade:
        filtros.append("b.cidade=?")
        params.append(cidade)
    if motivo:
        filtros.append("b.motivo=?")
        params.append(motivo)
    return conn.execute(f"""
        SELECT b.*, m.cliente, m.node, m.prioridade, m.ultimo_operador, m.status AS status_atual,
               CAST(julianday('now','localtime') - julianday(b.saiu_em) AS INTEGER) AS dias_fora
        FROM ged_bloqueios b
        LEFT JOIN mailing m ON m.id = b.id_mailing
        WHERE {' AND '.join(filtros)}
        ORDER BY b.cidade, b.saiu_em, b.contrato
    """, params).fetchall()


def _retornos_sinalizados(conn, id_cruz):
    if not id_cruz:
        return []
    return conn.execute("""
        SELECT a.*, m.cliente, m.status AS status_atual
        FROM ged_avaliacao a
        LEFT JOIN mailing m ON m.id = a.id_mailing
        WHERE a.id_cruzamento=? AND a.acao='SINALIZA'
        ORDER BY a.data_retorno, a.contrato
    """, (id_cruz,)).fetchall()


def ged_alerta(id_mailing):
    """Usado pelas telas de atendimento (Cliente.html / AtendimentoAvulso.html):
    devolve o status GED do contrato se ele NÃO estiver liberado no último
    cruzamento aplicado, ou None. Nunca derruba a tela -- qualquer erro
    (tabela ainda não criada, banco ocupado) vira None."""
    if not id_mailing:
        return None
    try:
        conn = _conectar()
        linha = conn.execute(
            "SELECT status_ged, data_base FROM ged_status_contrato WHERE id_mailing=?", (id_mailing,)
        ).fetchone()
        conn.close()
    except Exception:
        return None
    if not linha or not linha["status_ged"] or linha["status_ged"] == STATUS_LIBERADA:
        return None
    data_base = linha["data_base"]
    try:
        data_fmt = datetime.strptime(data_base, "%Y-%m-%d").strftime("%d/%m/%Y")
    except Exception:
        data_fmt = data_base or "—"
    return {"status": linha["status_ged"], "data_base": data_fmt}


@bp_cruzamento_ged.app_template_global("ged_alerta")
def _ged_alerta_template(id_mailing):
    return ged_alerta(id_mailing)


def _carregar(cruz):
    if not cruz:
        return None
    d = dict(cruz)
    d["resumo_obj"] = json.loads(d.get("resumo") or "{}")
    d["cidades_fora_obj"] = json.loads(d.get("cidades_fora") or "{}")
    d["cidades_arquivo_obj"] = json.loads(d.get("cidades_arquivo") or "[]")
    return d


# ==========================
# ROTAS
# ==========================

@bp_cruzamento_ged.route("/ged")
def ged_painel():
    from app import login_required, permissao_required

    @login_required
    @permissao_required("relatorio_ged")
    def _painel():
        from app import usuario_atual
        usuario = usuario_atual()
        eh_adm = usuario["perfil"] == "ADM"
        cidade = request.args.get("cidade") or ""
        motivo = request.args.get("motivo") or ""

        conn = _conectar()
        simulacao = _carregar(conn.execute(
            "SELECT * FROM ged_cruzamentos WHERE situacao='SIMULADO' ORDER BY id DESC LIMIT 1").fetchone()) if eh_adm else None
        ultimo = _carregar(conn.execute(
            "SELECT * FROM ged_cruzamentos WHERE situacao='APLICADO' ORDER BY id DESC LIMIT 1").fetchone())
        historico = [_carregar(c) for c in conn.execute(
            "SELECT * FROM ged_cruzamentos WHERE situacao='APLICADO' ORDER BY id DESC LIMIT 20")]
        bloqueados = _bloqueios_ativos(conn, cidade, motivo)
        todos_ativos = _bloqueios_ativos(conn)
        sinalizados = _retornos_sinalizados(conn, ultimo["id"] if ultimo else None)
        fila_atual = conn.execute(
            "SELECT COUNT(*) FROM mailing WHERE status IN ('PENDENTE','ENFILEIRADO')").fetchone()[0]

        por_cidade = {}
        por_motivo = {}
        for b in todos_ativos:
            c = por_cidade.setdefault(b["cidade"], {"total": 0})
            c["total"] += 1
            c[b["motivo"]] = c.get(b["motivo"], 0) + 1
            por_motivo[b["motivo"]] = por_motivo.get(b["motivo"], 0) + 1
        conn.close()

        dias_desde = None
        if ultimo and ultimo.get("aplicado_em"):
            dias_desde = (datetime.now() - datetime.strptime(ultimo["aplicado_em"], "%Y-%m-%d %H:%M:%S")).days

        return render_template(
            "GedCruzamento.html",
            eh_adm=eh_adm, simulacao=simulacao, ultimo=ultimo, historico=historico,
            bloqueados=bloqueados, total_ativos=len(todos_ativos), sinalizados=sinalizados,
            por_cidade=sorted(por_cidade.items(), key=lambda x: -x[1]["total"]),
            por_motivo=por_motivo, cidades_filtro=sorted(por_cidade), motivos_filtro=sorted(por_motivo),
            cidade=cidade, motivo=motivo, fila_atual=fila_atual, dias_desde=dias_desde,
            rotulo_acao=ROTULO_ACAO, erro=request.args.get("erro"), ok=request.args.get("ok"),
        )
    return _painel()


@bp_cruzamento_ged.route("/ged/upload", methods=["POST"])
def ged_upload():
    from app import login_required, perfil_required

    @login_required
    @perfil_required("ADM")
    def _upload():
        from app import usuario_atual
        import urllib.parse
        arquivo = request.files.get("arquivo")
        if not arquivo or not arquivo.filename:
            return redirect("/ged?erro=" + urllib.parse.quote("Nenhum arquivo selecionado."))
        conn = _conectar()
        try:
            simular_cruzamento(conn, arquivo, usuario_atual()["nome"])
        except (ValueError, zipfile.BadZipFile, UnicodeDecodeError, csv.Error) as erro:
            conn.rollback()
            conn.close()
            return redirect("/ged?erro=" + urllib.parse.quote(f"Não consegui ler o arquivo: {erro}"))
        conn.close()
        return redirect("/ged#simulacao")
    return _upload()


@bp_cruzamento_ged.route("/ged/aplicar/<int:id_cruz>", methods=["POST"])
def ged_aplicar(id_cruz):
    from app import login_required, perfil_required

    @login_required
    @perfil_required("ADM")
    def _aplicar():
        from app import usuario_atual
        import urllib.parse
        conn = _conectar()
        real = aplicar_cruzamento(conn, id_cruz, usuario_atual()["nome"])
        conn.close()
        if real is None:
            return redirect("/ged?erro=" + urllib.parse.quote("Essa simulação não está mais disponível (já aplicada ou descartada)."))
        msg = (f"Cruzamento aplicado: {real['saiu']} saíram da fila, {real['voltou']} voltaram, "
               f"{real['sinalizado']} Retornos sinalizados, {real['continua']} continuam fora")
        if real["pulado"]:
            msg += f", {real['pulado']} pulados (mudaram de status depois da simulação)"
        if real["tratado_fora"]:
            msg += f", {real['tratado_fora']} bloqueios encerrados (tratados por outro caminho)"
        return redirect("/ged?ok=" + urllib.parse.quote(msg + "."))
    return _aplicar()


@bp_cruzamento_ged.route("/ged/descartar/<int:id_cruz>", methods=["POST"])
def ged_descartar(id_cruz):
    from app import login_required, perfil_required

    @login_required
    @perfil_required("ADM")
    def _descartar():
        conn = _conectar()
        conn.execute("UPDATE ged_cruzamentos SET situacao='DESCARTADO' WHERE id=? AND situacao='SIMULADO'", (id_cruz,))
        conn.commit()
        conn.close()
        return redirect("/ged")
    return _descartar()


@bp_cruzamento_ged.route("/ged/extracao")
def ged_extracao():
    from app import login_required, permissao_required

    @login_required
    @permissao_required("relatorio_ged")
    def _extracao():
        import pandas as pd
        conn = _conectar()
        ultimo = conn.execute(
            "SELECT * FROM ged_cruzamentos WHERE situacao='APLICADO' ORDER BY id DESC LIMIT 1").fetchone()
        bloq = [{
            "Contrato": b["contrato"], "Cidade": b["cidade"], "Node": b["node"], "Cliente": b["cliente"],
            "Motivo (status GED)": b["motivo"], "Status antes de sair": b["status_anterior"],
            "Operador do followup": b["operador_followup"], "Último operador": b["ultimo_operador"],
            "Prioridade": b["prioridade"], "Saiu da fila em": b["saiu_em"], "Dias fora": b["dias_fora"],
        } for b in _bloqueios_ativos(conn)]
        sinal = [{
            "Contrato": s["contrato"], "Cidade": s["cidade"], "Cliente": s["cliente"],
            "Status GED": s["status_ged"], "Operador": s["operador_followup"],
            "Retorno agendado para": s["data_retorno"], "Status atual no CRM": s["status_atual"],
        } for s in _retornos_sinalizados(conn, ultimo["id"] if ultimo else None)]
        hist = [{
            "Data da base": c["data_base"], "Arquivo": c["arquivo"], "Aplicado por": c["aplicado_por"],
            "Aplicado em": c["aplicado_em"], "Linhas no arquivo": c["linhas_lidas"],
            **{k.replace("_", " ").capitalize(): v for k, v in json.loads(c["resumo"] or "{}").get("aplicado", {}).items()},
        } for c in conn.execute("SELECT * FROM ged_cruzamentos WHERE situacao='APLICADO' ORDER BY id DESC")]
        conn.close()

        saida = io.BytesIO()
        with pd.ExcelWriter(saida, engine="openpyxl") as writer:
            pd.DataFrame(bloq or [{"Contrato": "Nenhum contrato fora da fila"}]).to_excel(writer, index=False, sheet_name="Fora da fila")
            pd.DataFrame(sinal or [{"Contrato": "Nenhum Retorno sinalizado"}]).to_excel(writer, index=False, sheet_name="Retornos sinalizados")
            pd.DataFrame(hist or [{"Data da base": "Nenhum cruzamento aplicado"}]).to_excel(writer, index=False, sheet_name="Histórico")
        saida.seek(0)
        return send_file(
            saida, as_attachment=True,
            download_name=f"cruzamento_ged_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    return _extracao()
