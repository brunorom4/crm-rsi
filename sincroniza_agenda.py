"""
Sincronização dos agendamentos com a planilha de Confirmação de Agenda --
pedido do Bruno, 2026-10-16.

Problema: o cliente (ou o próprio campo) reagenda a visita por outros canais
-- site, app, WhatsApp da Claro, técnico após quebra -- sem passar pelo time.
A tela Agendados por Data continuava mostrando a data antiga, e o relatório
enviado pra área técnica saía errado.

Solução: a planilha de Confirmação de Agenda (exportação da agenda,
importada todo dia em Importação de Dados) já traz a data e o período
ATUAIS de cada OS. No momento da importação, cada agendamento nosso com data
de hoje em diante é comparado com a planilha (chave contrato + cidade, a
cidade da planilha vem de CD_OPERADORA via cidades_operadora):

- Data ou período diferente -> REAGENDADO FORA DO CRM: atualiza data e janela
  na própria linha do Agendado (mesmo mecanismo da remarcação feita pelo
  operador: nada conta em métrica, observações do time intactas) e grava o
  antes/depois em ajustes_agendamento com operador "CLIENTE / OUTRO CANAL",
  que aparece no histórico do contrato como "Ajuste de Agendamento". Se a
  planilha traz DT_REAG / USER_REAG / MOTIVO_REAG, vão junto na observação
  (muitas vezes o motivo mostra que foi o campo: chuva, cliente ausente...).
- TRAVA: se alguém do time mexeu no agendamento NO DIA DA IMPORTAÇÃO, NÃO
  sobrescreve -- a planilha pode ter sido extraída antes da mudança e
  desfaria um ajuste correto. Fica listado como "protegido"; a planilha do
  dia seguinte resolve. Mudança feita em dia anterior é considerada já
  refletida na base, e a planilha prevalece. (DT_RELATORIO NÃO é usado: o
  Bruno confirmou em 2026-10-16 que é só uma marcação do banco da extração,
  sem relação operacional com o agendamento.)
- Agendado futuro que NÃO aparece na planilha -> só listado como alerta
  (possível cancelamento ou execução antecipada); nada é alterado.

Não se cruza com o banco da Confirmação (confirmacao_agenda.db): a
importação dele só guarda data que AVANÇA, então antecipações se perdem, e
não dá pra saber quem veio na planilha do dia.

O resultado de cada sincronização fica em sinc_agenda_execucoes /
sinc_agenda_itens e aparece em /agendados/reagendamentos-externos.
"""
import os
import re
import sqlite3
from datetime import datetime

import pandas as pd
from flask import Blueprint, render_template, request, redirect

bp_sincroniza_agenda = Blueprint("sincroniza_agenda", __name__)

PASTA_BASE_SINC = os.path.dirname(os.path.abspath(__file__))
OPERADOR_EXTERNO = "CLIENTE / OUTRO CANAL"


def _conectar():
    conn = sqlite3.connect(os.path.join(PASTA_BASE_SINC, "crm.db"))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def garantir_tabelas_sinc():
    conn = _conectar()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sinc_agenda_execucoes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            executado_em TEXT,
            executado_por TEXT,
            arquivo TEXT,
            linhas_planilha INTEGER,
            agendados_avaliados INTEGER,
            na_planilha INTEGER,
            ajustados INTEGER,
            protegidos INTEGER,
            ausentes INTEGER
        );
        CREATE TABLE IF NOT EXISTS sinc_agenda_itens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            id_execucao INTEGER,
            tipo TEXT,                -- AJUSTADO / PROTEGIDO / AUSENTE
            atividade_id INTEGER,
            contrato TEXT,
            cidade TEXT,
            operador TEXT,
            data_anterior TEXT,
            janela_anterior TEXT,
            data_planilha TEXT,
            janela_planilha TEXT,
            ultimo_ajuste_crm TEXT,
            dt_relatorio TEXT,
            dt_reag TEXT,
            user_reag TEXT,
            motivo_reag TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_sinc_itens_exec ON sinc_agenda_itens(id_execucao, tipo);
    """)
    conn.commit()
    conn.close()


garantir_tabelas_sinc()


# ==========================
# CONVERSÕES
# ==========================

def periodo_para_janela(periodo):
    """'08:00 - 12:00' -> '08h as 12h' (formato das janelas do CRM).
    Aceita variações como '12:00 15:00'. None se não reconhecer."""
    m = re.match(r"\s*(\d{1,2})[:h]\d{0,2}\s*-?\s*(?:as|às|a)?\s*(\d{1,2})[:h]?\d{0,2}", str(periodo or ""))
    if not m:
        return None
    return f"{int(m.group(1)):02d}h as {int(m.group(2)):02d}h"


def _data_planilha(valor):
    """DATA_AGENDAMENTO / DT_RELATORIO / DT_REAG: 'DD/MM/AA' (texto) ou data
    nativa do Excel. Devolve 'YYYY-MM-DD' ou None."""
    if valor is None or (isinstance(valor, float) and pd.isna(valor)):
        return None
    if isinstance(valor, (datetime, pd.Timestamp)):
        return valor.strftime("%Y-%m-%d")
    texto = str(valor).strip()
    for formato in ("%d/%m/%y", "%d/%m/%Y", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(texto, formato).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _texto(valor):
    if valor is None or (isinstance(valor, float) and pd.isna(valor)):
        return None
    t = str(valor).strip()
    return t or None


def _fmt(data_iso):
    try:
        return datetime.strptime(data_iso, "%Y-%m-%d").strftime("%d/%m/%y")
    except Exception:
        return data_iso or "—"


# ==========================
# SINCRONIZAÇÃO
# ==========================

def sincronizar_com_planilha(conn, df, usuario_nome, nome_arquivo=None, agora=None):
    """Compara os agendamentos nossos (último status 'Agendado', data >= hoje)
    com a planilha da Confirmação de Agenda e aplica os reagendamentos
    externos. Devolve o dict de contagens. Grava e faz commit."""
    agora = agora or datetime.now()
    hoje = agora.strftime("%Y-%m-%d")
    agora_str = agora.strftime("%Y-%m-%d %H:%M:%S")

    necessarias = ["CD_OPERADORA", "CD_CONTRATO", "DATA_AGENDAMENTO", "PERIODO"]
    if any(c not in df.columns for c in necessarias):
        return None  # planilha fora do padrão -- a importação principal já reclama

    mapa_cidade = {str(r["cod_operadora"]).strip(): r["cidade"]
                   for r in conn.execute("SELECT cod_operadora, cidade FROM cidades_operadora")}

    planilha = {}
    for _, linha in df.drop_duplicates(subset=["CD_CONTRATO"], keep="first").iterrows():
        try:
            cod_op = str(int(float(linha["CD_OPERADORA"])))
            contrato = str(int(float(linha["CD_CONTRATO"])))
        except (TypeError, ValueError):
            continue
        cidade = mapa_cidade.get(cod_op)
        data = _data_planilha(linha["DATA_AGENDAMENTO"])
        if not cidade or not data:
            continue
        planilha[(contrato, cidade)] = {
            "data": data,
            "janela": periodo_para_janela(linha["PERIODO"]),
            "dt_reag": _data_planilha(linha.get("DT_REAG")),
            "user_reag": _texto(linha.get("USER_REAG")),
            "motivo_reag": _texto(linha.get("MOTIVO_REAG")),
        }

    agendados = conn.execute("""
        SELECT a.*, m.cidade,
               (SELECT MAX(j.data_hora) FROM ajustes_agendamento j WHERE j.atividade_id = a.id) AS ultimo_ajuste
        FROM atividades a
        JOIN mailing m ON (m.id = a.id_mailing OR (a.id_mailing IS NULL AND m.contrato = a.contrato))
        WHERE a.id IN (SELECT MAX(id) FROM atividades GROUP BY contrato)
          AND a.tabulacao = 'Agendado'
          AND a.data_agendamento >= ?
    """, (hoje,)).fetchall()

    cur = conn.execute("""
        INSERT INTO sinc_agenda_execucoes (executado_em, executado_por, arquivo, linhas_planilha, agendados_avaliados)
        VALUES (?, ?, ?, ?, ?)
    """, (agora_str, usuario_nome, nome_arquivo, len(df), len(agendados)))
    id_exec = cur.lastrowid

    cont = {"avaliados": len(agendados), "na_planilha": 0, "ajustados": 0, "protegidos": 0, "ausentes": 0}
    vistos = set()
    for a in agendados:
        if a["id"] in vistos:  # contrato repetido em duas linhas de mailing -- conta uma vez
            continue
        vistos.add(a["id"])
        chave = (str(a["contrato"]).strip().lstrip("0"), a["cidade"])
        info = planilha.get(chave) or planilha.get((str(a["contrato"]).strip(), a["cidade"]))
        ultimo_crm = max(x for x in (a["data_fim"] or "", a["ultimo_ajuste"] or ""))
        base_item = {
            "id_execucao": id_exec, "atividade_id": a["id"], "contrato": a["contrato"], "cidade": a["cidade"],
            "operador": a["operador"], "data_anterior": a["data_agendamento"],
            "janela_anterior": a["janela_agendamento"], "ultimo_ajuste_crm": ultimo_crm or None,
        }
        if not info:
            cont["ausentes"] += 1
            _gravar_item(conn, {**base_item, "tipo": "AUSENTE"})
            continue
        cont["na_planilha"] += 1
        janela_nova = info["janela"] or a["janela_agendamento"]
        if info["data"] == a["data_agendamento"] and janela_nova == a["janela_agendamento"]:
            continue

        item = {**base_item, "data_planilha": info["data"], "janela_planilha": janela_nova,
                "dt_reag": info["dt_reag"],
                "user_reag": info["user_reag"], "motivo_reag": info["motivo_reag"]}

        if ultimo_crm and ultimo_crm[:10] >= hoje:
            cont["protegidos"] += 1
            _gravar_item(conn, {**item, "tipo": "PROTEGIDO"})
            continue

        observacao = f"Reagendado fora do CRM (cliente/outro canal) -- base de confirmação de agenda importada em {_fmt(hoje)}"
        extras = []
        if info["motivo_reag"]:
            extras.append(f"motivo: {info['motivo_reag']}")
        if info["user_reag"]:
            extras.append(f"usuário: {info['user_reag']}")
        if info["dt_reag"]:
            extras.append(f"em {_fmt(info['dt_reag'])}")
        if extras:
            observacao += " (" + ", ".join(extras) + ")"

        conn.execute("UPDATE atividades SET data_agendamento=?, janela_agendamento=? WHERE id=?",
                     (info["data"], janela_nova, a["id"]))
        conn.execute("""
            INSERT INTO ajustes_agendamento (atividade_id, contrato, operador, data_hora,
                data_agendamento_anterior, janela_anterior, resp_cliente_anterior,
                data_agendamento_nova, janela_nova, resp_cliente_novo, observacao)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (a["id"], a["contrato"], OPERADOR_EXTERNO, agora_str,
              a["data_agendamento"], a["janela_agendamento"], a["resp_cliente"],
              info["data"], janela_nova, a["resp_cliente"], observacao))
        cont["ajustados"] += 1
        _gravar_item(conn, {**item, "tipo": "AJUSTADO"})

    conn.execute("""
        UPDATE sinc_agenda_execucoes SET na_planilha=?, ajustados=?, protegidos=?, ausentes=? WHERE id=?
    """, (cont["na_planilha"], cont["ajustados"], cont["protegidos"], cont["ausentes"], id_exec))
    conn.commit()
    cont["id_execucao"] = id_exec
    return cont


def _gravar_item(conn, item):
    campos = ["id_execucao", "tipo", "atividade_id", "contrato", "cidade", "operador", "data_anterior",
              "janela_anterior", "data_planilha", "janela_planilha", "ultimo_ajuste_crm", "dt_relatorio",
              "dt_reag", "user_reag", "motivo_reag"]
    conn.execute(
        f"INSERT INTO sinc_agenda_itens ({', '.join(campos)}) VALUES ({', '.join('?' * len(campos))})",
        [item.get(c) for c in campos],
    )


def ids_reagendados_externamente(conn, ids_atividades):
    """Conjunto de atividade_id cujo ÚLTIMO ajuste veio de fora do CRM --
    usado pra marcar a linha em Agendados por Data."""
    ids = list(ids_atividades)
    if not ids:
        return set()
    marc = ",".join("?" * len(ids))
    return {r["atividade_id"] for r in conn.execute(f"""
        SELECT j.atividade_id FROM ajustes_agendamento j
        WHERE j.atividade_id IN ({marc})
          AND j.id = (SELECT MAX(j2.id) FROM ajustes_agendamento j2 WHERE j2.atividade_id = j.atividade_id)
          AND j.operador = ?
    """, ids + [OPERADOR_EXTERNO])}


# ==========================
# TELA
# ==========================

@bp_sincroniza_agenda.route("/agendados/reagendamentos-externos")
def tela_reagendamentos_externos():
    from app import login_required, permissao_required

    @login_required
    @permissao_required("agendados_data")
    def _tela():
        conn = _conectar()
        id_pedido = request.args.get("execucao", type=int)
        execucoes = conn.execute("SELECT * FROM sinc_agenda_execucoes ORDER BY id DESC LIMIT 30").fetchall()
        atual = None
        if execucoes:
            atual = next((e for e in execucoes if e["id"] == id_pedido), execucoes[0])
        itens = {"AJUSTADO": [], "PROTEGIDO": [], "AUSENTE": []}
        if atual:
            for i in conn.execute("""
                SELECT * FROM sinc_agenda_itens WHERE id_execucao=?
                ORDER BY tipo, COALESCE(data_planilha, data_anterior), contrato
            """, (atual["id"],)):
                itens.setdefault(i["tipo"], []).append(i)
        conn.close()
        return render_template("ReagendamentosExternos.html", execucoes=execucoes, atual=atual, itens=itens)
    return _tela()
