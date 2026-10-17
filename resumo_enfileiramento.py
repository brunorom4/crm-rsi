"""
Resumo do Enfileiramento -- subpágina do Dashboard, pedido do Bruno em
2026-10-17. Mostra, por bloco de prioridade, em que pé está a base:

    Tratados          CONCLUIDO
    Enfileirado livre ENFILEIRADO que a distribuição pode entregar
    Enf. bloq. distr. ENFILEIRADO em cidade bloqueada ou contrato na
                      blacklist (continua ENFILEIRADO, mas a fila de
                      followup pula -- ver buscar_proximo_cliente)
    Enf. bloq. GED    BLOQUEADO_GED cujo status anterior era ENFILEIRADO
                      (ged_bloqueios ativo) -- os dois cenários de
                      bloqueio ficam separados, como pedido
    Pendentes         PENDENTE (ainda não tratado neste ciclo)
    Outros            em atendimento agora, GPON, GED de pendentes

E, dos enfileirados (livres + bloqueados), quantos são IMPRODUTIVOS
REABERTOS JÁ TENTADOS DE NOVO: o contrato teve uma tabulação "Improdutivo",
foi reaberto pela reimportação (regra de 2026-08-25) e recebeu pelo menos
uma tratativa depois disso -- é o que o colocou de volta em ENFILEIRADO.

Blocos de prioridade: >95 · 90–95 · 80–89 · <80 · sem prioridade
(prioridade vazia ou 0, confirmado com o Bruno).
"""
import os
import sqlite3

from flask import Blueprint, render_template

bp_resumo_enfileiramento = Blueprint("resumo_enfileiramento", __name__)

PASTA_BASE_RE = os.path.dirname(os.path.abspath(__file__))

BLOCOS = [">95", "90–95", "80–89", "<80", "Sem prioridade"]
SEGMENTOS = [
    ("tratados", "Tratados"),
    ("enf_livre", "Enfileirado livre"),
    ("enf_bloq_distr", "Enfileirado · bloqueio de distribuição"),
    ("enf_bloq_ged", "Enfileirado · bloqueado no GED"),
    ("pendentes", "Pendentes"),
    ("outros", "Outros"),
]

SQL_BLOCO = """CASE
    WHEN m.prioridade IS NULL OR m.prioridade = '' OR m.prioridade = 0 THEN 4
    WHEN m.prioridade > 95 THEN 0
    WHEN m.prioridade >= 90 THEN 1
    WHEN m.prioridade >= 80 THEN 2
    ELSE 3 END"""


def _garantir_indice():
    """Sem índice por id_mailing, cruzar o histórico de tratativas com a fila
    levava minutos na base real. Criado uma vez, na importação do módulo."""
    try:
        conn = sqlite3.connect(os.path.join(PASTA_BASE_RE, "crm.db"), timeout=30)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_atividades_id_mailing ON atividades (id_mailing)")
        conn.commit()
        conn.close()
    except sqlite3.Error as erro:
        print(f"[resumo_enfileiramento] índice não criado: {erro}")


_garantir_indice()


def _tem_tabela(conn, nome):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (nome,)).fetchone() is not None


def calcular_resumo(conn):
    ged = _tem_tabela(conn, "ged_bloqueios")
    ged_join = ("LEFT JOIN (SELECT id_mailing, MAX(status_anterior) AS status_anterior FROM ged_bloqueios "
                "WHERE ativo=1 GROUP BY id_mailing) g ON g.id_mailing = m.id") if ged else ""
    ged_ant = "g.status_anterior" if ged else "NULL"

    linhas = conn.execute(f"""
        SELECT {SQL_BLOCO} AS bloco,
               CASE
                 WHEN m.status = 'CONCLUIDO' THEN 'tratados'
                 WHEN m.status = 'ENFILEIRADO' AND (
                        m.cidade IN (SELECT cidade FROM cidades_bloqueio_distribuicao WHERE bloqueado=1)
                     OR m.contrato IN (SELECT contrato FROM contratos_blacklist)) THEN 'enf_bloq_distr'
                 WHEN m.status = 'ENFILEIRADO' THEN 'enf_livre'
                 WHEN m.status = 'BLOQUEADO_GED' AND {ged_ant} = 'ENFILEIRADO' THEN 'enf_bloq_ged'
                 WHEN m.status = 'PENDENTE' THEN 'pendentes'
                 ELSE 'outros'
               END AS segmento,
               COUNT(*) AS qtd
        FROM mailing m
        {ged_join}
        GROUP BY 1, 2
    """).fetchall()

    tabela = [{"bloco": b, "total": 0, "reabertos": 0, **{k: 0 for k, _ in SEGMENTOS}} for b in BLOCOS]
    for bloco, segmento, qtd in linhas:
        tabela[bloco][segmento] += qtd
        tabela[bloco]["total"] += qtd

    # Improdutivos reabertos já tentados de novo, entre os enfileirados.
    # Histórico do contrato: por id_mailing (gravado desde 2026-09-20) e, nas
    # tratativas antigas sem id_mailing, pelo número do contrato.
    reabertos = conn.execute(f"""
        WITH fila AS (
            SELECT m.id, m.contrato, {SQL_BLOCO} AS bloco
            FROM mailing m {ged_join}
            WHERE m.status = 'ENFILEIRADO' OR (m.status = 'BLOQUEADO_GED' AND {ged_ant} = 'ENFILEIRADO')
        ),
        hist AS (
            SELECT f.id, a.id AS id_ativ, a.tabulacao FROM fila f JOIN atividades a ON a.id_mailing = f.id
            UNION ALL
            SELECT f.id, a.id, a.tabulacao FROM fila f JOIN atividades a
                   ON a.id_mailing IS NULL AND a.contrato = f.contrato
        ),
        resumo AS (
            SELECT id, MAX(CASE WHEN tabulacao = 'Improdutivo' THEN id_ativ END) AS ultimo_improdutivo,
                   MAX(id_ativ) AS ultima
            FROM hist GROUP BY id
        )
        SELECT f.bloco, COUNT(*) FROM fila f JOIN resumo r ON r.id = f.id
        WHERE r.ultimo_improdutivo IS NOT NULL AND r.ultima > r.ultimo_improdutivo
        GROUP BY f.bloco
    """).fetchall()
    for bloco, qtd in reabertos:
        tabela[bloco]["reabertos"] += qtd

    totais = {"bloco": "Total", "total": sum(l["total"] for l in tabela),
              "reabertos": sum(l["reabertos"] for l in tabela),
              **{k: sum(l[k] for l in tabela) for k, _ in SEGMENTOS}}
    for linha in tabela + [totais]:
        linha["enfileirados"] = linha["enf_livre"] + linha["enf_bloq_distr"] + linha["enf_bloq_ged"]
    return tabela, totais


@bp_resumo_enfileiramento.route("/dashboard/enfileiramento")
def resumo_enfileiramento():
    from app import login_required, permissao_required, conectar

    @login_required
    @permissao_required("dashboard_equipe")
    def _tela():
        conn = conectar()
        tabela, totais = calcular_resumo(conn)
        conn.close()
        return render_template("ResumoEnfileiramento.html", tabela=tabela, totais=totais, segmentos=SEGMENTOS)
    return _tela()
