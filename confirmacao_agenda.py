"""
Módulo Confirmação de Agenda -- pedido do Bruno, 2026-09-16.

Módulo separado, com banco de dados próprio (confirmacao_agenda.db), que
NÃO se comunica operacionalmente com o CRM principal (mailing/atividades/
followup do crm.db não são tocados por nada aqui). A única vez que esse
módulo lê o crm.db é na IMPORTAÇÃO, pra resolver CD_OPERADORA -> cidade ->
DDD usando as tabelas cidades_operadora e cidades_ddd que já existem lá --
depois disso, o DDD fica gravado direto no banco desse módulo, sem precisar
consultar o crm.db de novo.

Login, permissão ("confirmacao_agenda_tratar") e troca de senha são
reaproveitados do app.py principal -- importados no fim desse arquivo,
depois que tudo já estiver definido lá (evita import circular, já que
app.py importa esse módulo pra registrar o blueprint).
"""
import sqlite3
import os
import calendar
from datetime import datetime, timedelta
from flask import Blueprint, render_template, request, redirect, send_file
import pandas as pd
import io

PASTA_BASE_CONF = os.path.dirname(os.path.abspath(__file__))
NOME_BANCO_CONF = "confirmacao_agenda.db"

FECHAMENTOS_VALIDOS = [
    "AGENDA CONFIRMADA", "SEM CONTATO", "JÁ EXECUTADA", "REAGENDADA",
    "LIMPAR AGENDA/CANCELAR", "JÁ CANCELADO", "JÁ CONFIRMADA",
]

# Vendedores com prioridade absoluta na distribuição -- único critério de
# prioridade dessa fila (pedido do Bruno, 2026-09-16). Os demais seguem a
# ordem original da planilha (ordem_planilha).
VENDEDORES_PRIORITARIOS = {"ANA CLAUDIA PIRES", "TATIANE CRISTINA SABINO CALTRAN"}

bp_confirmacao_agenda = Blueprint(
    "confirmacao_agenda", __name__, template_folder="templates"
)


def conectar_conf():
    conn = sqlite3.connect(os.path.join(PASTA_BASE_CONF, NOME_BANCO_CONF))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _garantir_banco_confirmacao_agenda():
    conn = conectar_conf()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS contratos_confirmacao (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cd_contrato TEXT UNIQUE NOT NULL,
            cd_operadora TEXT,
            ddd TEXT,
            cidade TEXT,
            produto_de TEXT,
            produto_para TEXT,
            data_agendamento TEXT,
            periodo TEXT,
            ds_vendedor TEXT,
            prioridade_vendedor INTEGER DEFAULT 0,
            status TEXT DEFAULT 'PENDENTE',
            operador_atual TEXT,
            data_distribuicao TEXT,
            ordem_planilha INTEGER,
            criado_em TEXT,
            atualizado_em TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_conf_fila ON contratos_confirmacao(ddd, status, data_agendamento)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tratamentos_confirmacao (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cd_contrato TEXT,
            operador TEXT,
            fechamento TEXT,
            nova_data_agendamento TEXT,
            novo_periodo TEXT,
            observacao TEXT,
            data_tratamento TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cidades_bloqueadas_conf (
            cidade TEXT PRIMARY KEY,
            motivo TEXT,
            bloqueado_por TEXT,
            bloqueado_em TEXT
        )
    """)
    conn.commit()
    conn.close()


def _garantir_historico_bloqueio_cidade():
    """Log histórico de quando cada cidade foi bloqueada/desbloqueada --
    pedido do Bruno, 2026-09-19, pra poder reconstruir com precisão se uma
    cidade estava bloqueada num dia PASSADO (o flag em cidades_bloqueadas_conf
    só sabe o estado ATUAL). Faz backfill uma única vez: cidades que já
    estavam bloqueadas antes desse histórico existir ganham um registro
    BLOQUEAR retroativo, usando a data que já tinha em bloqueado_em."""
    conn = conectar_conf()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS historico_bloqueio_cidade (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cidade TEXT NOT NULL,
            acao TEXT NOT NULL,
            usuario TEXT,
            data_hora TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hist_bloqueio_cidade ON historico_bloqueio_cidade(cidade, data_hora)")

    tem_historico = conn.execute("SELECT COUNT(*) c FROM historico_bloqueio_cidade").fetchone()["c"]
    if tem_historico == 0:
        bloqueadas = conn.execute("SELECT cidade, bloqueado_por, bloqueado_em FROM cidades_bloqueadas_conf").fetchall()
        for b in bloqueadas:
            conn.execute("""
                INSERT INTO historico_bloqueio_cidade (cidade, acao, usuario, data_hora)
                VALUES (?, 'BLOQUEAR', ?, ?)
            """, (b["cidade"], b["bloqueado_por"], b["bloqueado_em"] or datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

    conn.commit()
    conn.close()


_garantir_banco_confirmacao_agenda()
_garantir_historico_bloqueio_cidade()


def datas_alvo_hoje(agora=None):
    """Datas (YYYY-MM-DD) elegíveis pra distribuição hoje, em ORDEM de
    prioridade. Normalmente só D+1 (segunda a quinta) -- na sexta, primeiro
    tenta sábado, só cai pra próxima segunda quando o sábado acabar."""
    agora = agora or datetime.now()
    dia_semana = agora.weekday()  # 0=segunda ... 4=sexta, 5=sábado, 6=domingo
    hoje = agora.date()
    if dia_semana == 4:  # sexta
        sabado = hoje + timedelta(days=1)
        proxima_segunda = hoje + timedelta(days=3)
        return [sabado.strftime("%Y-%m-%d"), proxima_segunda.strftime("%Y-%m-%d")]
    if dia_semana <= 3:  # segunda a quinta
        return [(hoje + timedelta(days=1)).strftime("%Y-%m-%d")]
    return []  # sábado/domingo -- não há distribuição definida pra esses dias


def _parse_data_ddmmaa(valor):
    """Converte 'DD/MM/AA' pra 'YYYY-MM-DD'. None se vazio/inválido --
    contratos sem data ou com data quebrada são ignorados na importação
    (pedido do Bruno)."""
    if not valor or not isinstance(valor, str) or not valor.strip():
        return None
    try:
        return datetime.strptime(valor.strip(), "%d/%m/%y").strftime("%Y-%m-%d")
    except ValueError:
        return None


def processar_importacao_confirmacao(caminho_arquivo, caminho_crm_db):
    """Lê a planilha, remove duplicados (mesmo CD_CONTRATO), resolve DDD via
    crm.db (cidades_operadora + cidades_ddd), ignora datas passadas/vazias, e
    aplica a regra de atualização: só atualiza a data se a nova vier MAIS NO
    FUTURO que a já cadastrada (senão mantém como está); reabre contrato já
    tratado se a nova data avançar. Nunca mexe em contrato EM_ANDAMENTO
    (alguém pode estar tratando ele nesse exato momento)."""
    df = pd.read_excel(caminho_arquivo)
    colunas_necessarias = ["CD_OPERADORA", "CD_CONTRATO", "NM_CIDADE", "PRODUTO_DE",
                            "PRODUTO_PARA", "DATA_AGENDAMENTO", "PERIODO", "DS_VENDEDOR"]
    faltando = [c for c in colunas_necessarias if c not in df.columns]
    if faltando:
        return {"erro": f"Coluna(s) não encontrada(s) na planilha: {', '.join(faltando)}"}

    df = df.drop_duplicates(subset=["CD_CONTRATO"], keep="first")

    conn_crm = sqlite3.connect(caminho_crm_db)
    mapa_cidade = {r[0]: r[1] for r in conn_crm.execute("SELECT cod_operadora, cidade FROM cidades_operadora").fetchall()}
    mapa_ddd = {r[0]: r[1] for r in conn_crm.execute("SELECT cidade, ddd FROM cidades_ddd").fetchall()}
    conn_crm.close()

    hoje_str = datetime.now().strftime("%Y-%m-%d")
    agora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = conectar_conf()
    novos = atualizados = ignorados = sem_ddd = em_andamento_preservados = 0

    for idx, linha in df.iterrows():
        cd_contrato = str(linha["CD_CONTRATO"]).strip()
        data_txt = str(linha["DATA_AGENDAMENTO"]) if pd.notna(linha["DATA_AGENDAMENTO"]) else None
        data_agendamento = _parse_data_ddmmaa(data_txt)

        if not data_agendamento or data_agendamento < hoje_str:
            ignorados += 1
            continue

        cd_operadora = str(int(linha["CD_OPERADORA"])) if pd.notna(linha["CD_OPERADORA"]) else None
        cidade = mapa_cidade.get(cd_operadora)
        ddd = mapa_ddd.get(cidade) if cidade else None
        if not ddd:
            sem_ddd += 1
            continue

        ds_vendedor = str(linha["DS_VENDEDOR"]).strip() if pd.notna(linha["DS_VENDEDOR"]) else ""
        prioridade_vendedor = 1 if ds_vendedor.upper() in VENDEDORES_PRIORITARIOS else 0
        produto_de = str(linha["PRODUTO_DE"]) if pd.notna(linha["PRODUTO_DE"]) else ""
        produto_para = str(linha["PRODUTO_PARA"]) if pd.notna(linha["PRODUTO_PARA"]) else ""
        periodo = str(linha["PERIODO"]) if pd.notna(linha["PERIODO"]) else ""

        existente = conn.execute("SELECT * FROM contratos_confirmacao WHERE cd_contrato=?", (cd_contrato,)).fetchone()

        if not existente:
            conn.execute("""
                INSERT INTO contratos_confirmacao
                    (cd_contrato, cd_operadora, ddd, cidade, produto_de, produto_para,
                     data_agendamento, periodo, ds_vendedor, prioridade_vendedor,
                     status, ordem_planilha, criado_em, atualizado_em)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDENTE', ?, ?, ?)
            """, (cd_contrato, cd_operadora, ddd, cidade, produto_de, produto_para,
                  data_agendamento, periodo, ds_vendedor, prioridade_vendedor,
                  idx, agora_str, agora_str))
            novos += 1
        elif existente["status"] == "EM_ANDAMENTO":
            em_andamento_preservados += 1  # alguém pode estar tratando agora -- não mexe
        elif data_agendamento > existente["data_agendamento"]:
            conn.execute("""
                UPDATE contratos_confirmacao SET
                    cd_operadora=?, ddd=?, cidade=?, produto_de=?, produto_para=?,
                    data_agendamento=?, periodo=?, ds_vendedor=?, prioridade_vendedor=?,
                    status='PENDENTE', operador_atual=NULL, data_distribuicao=NULL,
                    ordem_planilha=?, atualizado_em=?
                WHERE cd_contrato=?
            """, (cd_operadora, ddd, cidade, produto_de, produto_para,
                  data_agendamento, periodo, ds_vendedor, prioridade_vendedor,
                  idx, agora_str, cd_contrato))
            atualizados += 1
        else:
            ignorados += 1  # já existia e a data não avançou -- mantém como está

    conn.commit()
    conn.close()
    return {
        "novos": novos, "atualizados": atualizados, "ignorados": ignorados,
        "sem_ddd": sem_ddd, "em_andamento_preservados": em_andamento_preservados,
    }


def buscar_proximo_confirmacao(operador_nome, ddd_escolhido):
    """Mesmo padrão de reivindicação com retry do CRM principal (evita dois
    operadores pegando o mesmo contrato numa corrida). Ordem de prioridade
    dentro da data-alvo (pedido do Bruno, 2026-09-18): 1º período do
    agendamento (08h-12h, depois 12h-15h, depois 15h-18h), 2º vendedor
    prioritário, 3º ordem original da planilha -- tudo isso só dentro da
    data mais prioritária disponível (na sexta, sábado esgota antes de
    olhar a próxima segunda)."""
    conn = conectar_conf()

    datas_alvo = datas_alvo_hoje()

    # Só retoma um EM_ANDAMENTO existente se ele ainda for válido pra hoje --
    # mesmo DDD escolhido e data dentro da janela-alvo de hoje. Sem essa
    # checagem, um contrato distribuído num dia anterior e nunca tratado
    # (operador saiu, trocou de DDD, etc.) ficava "grudado" no operador e era
    # entregue de novo pra ele independente do DDD/data pedidos -- foi o bug
    # relatado (contrato de 18/09 sendo entregue numa tela que já mostrava
    # 19/09 e 21/09 como datas válidas).
    atual = conn.execute("""
        SELECT * FROM contratos_confirmacao WHERE status='EM_ANDAMENTO' AND operador_atual=?
        ORDER BY data_distribuicao DESC LIMIT 1
    """, (operador_nome,)).fetchone()
    if atual and atual["ddd"] == ddd_escolhido and atual["data_agendamento"] in datas_alvo:
        conn.close()
        return atual

    if not datas_alvo:
        conn.close()
        return None

    agora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ids_ja_tentados = []
    for _tentativa in range(5):
        candidato = None
        for data_alvo in datas_alvo:
            marcador = ",".join("?" * len(ids_ja_tentados)) if ids_ja_tentados else "-1"
            candidato = conn.execute(f"""
                SELECT * FROM contratos_confirmacao
                WHERE status='PENDENTE' AND ddd=? AND data_agendamento=?
                  AND id NOT IN ({marcador})
                  AND cidade NOT IN (SELECT cidade FROM cidades_bloqueadas_conf)
                ORDER BY
                    CASE
                        WHEN periodo LIKE '08%' THEN 1
                        WHEN periodo LIKE '12%' THEN 2
                        WHEN periodo LIKE '15%' THEN 3
                        ELSE 4
                    END ASC,
                    prioridade_vendedor DESC,
                    ordem_planilha ASC
                LIMIT 1
            """, (ddd_escolhido, data_alvo, *ids_ja_tentados)).fetchone()
            if candidato:
                break  # achou na data mais prioritária, não olha a próxima

        if not candidato:
            conn.close()
            return None

        cursor = conn.execute(
            "UPDATE contratos_confirmacao SET status='EM_ANDAMENTO', operador_atual=?, data_distribuicao=? WHERE id=? AND status='PENDENTE'",
            (operador_nome, agora_str, candidato["id"])
        )
        if cursor.rowcount == 0:
            conn.rollback()
            ids_ja_tentados.append(candidato["id"])
            continue
        conn.commit()
        cliente = conn.execute("SELECT * FROM contratos_confirmacao WHERE id=?", (candidato["id"],)).fetchone()
        conn.close()
        return cliente

    conn.close()
    return None


def _mes_atual_intervalo():
    """Primeiro e último dia do mês corrente (YYYY-MM-DD) -- filtro padrão
    do painel quando o usuário não escolhe um período."""
    hoje = datetime.now().date()
    primeiro = hoje.replace(day=1)
    ultimo_dia = calendar.monthrange(hoje.year, hoje.month)[1]
    ultimo = hoje.replace(day=ultimo_dia)
    return primeiro.strftime("%Y-%m-%d"), ultimo.strftime("%Y-%m-%d")


def _data_disponibilizacao(data_agendamento_str):
    """Data em que um contrato com essa data_agendamento ENTROU na janela de
    distribuição (ficou disponível pra um operador tratar) -- pedido do
    Bruno, 2026-09-19: o painel deve refletir o que aconteceu num dia (o que
    foi enfileirado, tratado, bloqueado naquele dia), não a data agendada do
    cliente. Usa a mesma regra de datas_alvo_hoje(), de trás pra frente:
      - se a data agendada cai numa SEGUNDA, foi liberada na SEXTA anterior
        (regra da sexta: sábado e a segunda seguinte juntos no mesmo dia);
      - senão, foi liberada no dia anterior (regra D+1 normal).
    Datas agendadas caindo num DOMINGO nunca são cobertas por nenhuma regra
    de distribuição vigente (nem D+1 nem a da sexta alcançam domingo) --
    retorna None nesse caso raro/anômalo, e o contrato simplesmente não
    aparece em nenhum filtro de "enfileirado nessa data"."""
    d = datetime.strptime(data_agendamento_str, "%Y-%m-%d").date()
    if d.weekday() == 6:  # domingo -- nunca é data-alvo de distribuição
        return None
    if d.weekday() == 0:  # segunda -- liberada na sexta anterior (D-3)
        disponivel = d - timedelta(days=3)
    else:
        disponivel = d - timedelta(days=1)
    return disponivel.strftime("%Y-%m-%d")


def _carregar_historico_bloqueio(conn):
    """Histórico de bloqueio/desbloqueio de cada cidade, agrupado e em
    ordem cronológica -- usado pra saber se uma cidade estava bloqueada num
    dia PASSADO específico (o estado atual em cidades_bloqueadas_conf não
    basta pra isso)."""
    linhas = conn.execute("""
        SELECT cidade, acao, data_hora FROM historico_bloqueio_cidade ORDER BY cidade, data_hora
    """).fetchall()
    por_cidade = {}
    for l in linhas:
        por_cidade.setdefault(l["cidade"], []).append((l["data_hora"], l["acao"]))
    return por_cidade


def _estava_bloqueada(historico_por_cidade, cidade, data_str):
    """True se, no fim do dia `data_str`, a cidade estava bloqueada --
    olhando o último evento de bloqueio/desbloqueio até aquele momento."""
    eventos = historico_por_cidade.get(cidade)
    if not eventos:
        return False
    limite = f"{data_str} 23:59:59"
    estado = False
    for data_hora, acao in eventos:
        if data_hora > limite:
            break
        estado = (acao == "BLOQUEAR")
    return estado


def _contratos_disponiveis_no_periodo(conn, data_inicio, data_fim):
    """Todos os contratos cujo "dia de referência" cai dentro do período
    filtrado -- independente da data_agendamento em si. Não dá pra fazer
    isso num WHERE de SQL porque esse dia é calculado, não é uma coluna do
    banco -- com a base nesse tamanho (milhares de linhas), filtrar em
    Python depois de trazer tudo é simples e rápido o bastante.

    O "dia de referência" muda conforme o status (achado numa checagem do
    Bruno, 2026-09-19):
    -- Se já está TRATADO, usa o dia real do fechamento (atualizado_em).
       Necessário porque um fechamento REAGENDADA reescreve a
       data_agendamento pra data nova que o cliente pediu -- calcular a
       disponibilização a partir dela jogaria o contrato pro período ERRADO
       (ele já foi fechado no dia em que estava na fila, não no dia da nova
       data solicitada, que nem chegou a reabrir de verdade -- só reabre de
       fato numa reimportação futura).
    -- Senão (PENDENTE, EM_ANDAMENTO, NAO_TRATADO), a data_agendamento ainda
       não foi tocada, então usa _data_disponibilizacao normalmente."""
    historico = _carregar_historico_bloqueio(conn)
    linhas = conn.execute(
        "SELECT ddd, cidade, status, data_agendamento, atualizado_em FROM contratos_confirmacao"
    ).fetchall()

    saida = []
    for l in linhas:
        if l["status"] == "TRATADO":
            dia_referencia = (l["atualizado_em"] or "")[:10] or None
        else:
            dia_referencia = _data_disponibilizacao(l["data_agendamento"])

        if not dia_referencia or not (data_inicio <= dia_referencia <= data_fim):
            continue
        saida.append({
            "ddd": l["ddd"],
            "status": l["status"],
            "bloqueada": _estava_bloqueada(historico, l["cidade"], dia_referencia),
        })
    return saida


def calcular_resumo_confirmacao(contratos):
    """Totalizador do topo do painel (pedido do Bruno, 2026-09-19): total de
    contratos ENFILEIRADOS no período (ficaram disponíveis pra tratar,
    qualquer status), quantos estavam em cidade bloqueada naquele dia,
    quantos estão PENDENTE e aptos a tratar (fora de bloqueio), quantos já
    foram TRATADO (a qualquer momento), e o % tratado sobre o total."""
    total_fila = len(contratos)
    total_bloqueadas = sum(1 for c in contratos if c["bloqueada"])
    total_pendente = sum(1 for c in contratos if c["status"] == "PENDENTE" and not c["bloqueada"])
    total_tratado = sum(1 for c in contratos if c["status"] == "TRATADO")
    pct_tratado = round(total_tratado / total_fila * 100, 1) if total_fila else 0.0

    return {
        "total_fila": total_fila,
        "total_bloqueadas": total_bloqueadas,
        "total_pendente": total_pendente,
        "total_tratado": total_tratado,
        "pct_tratado": pct_tratado,
    }


def calcular_painel_confirmacao(contratos):
    """Tabela do painel aberta por DDD -- mesmo recorte do totalizador do
    topo (total enfileirado, bloqueadas, pendente apto a tratar, tratado,
    %), sobre a mesma lista de contratos já filtrada pelo período (pedido
    do Bruno, 2026-09-19)."""
    por_ddd = {}
    for c in contratos:
        d = por_ddd.setdefault(c["ddd"], {"total": 0, "bloqueadas": 0, "pendente": 0, "tratado": 0})
        d["total"] += 1
        if c["bloqueada"]:
            d["bloqueadas"] += 1
        if c["status"] == "PENDENTE" and not c["bloqueada"]:
            d["pendente"] += 1
        if c["status"] == "TRATADO":
            d["tratado"] += 1

    def _chave_ordenacao(item):
        ddd = item[0]
        return int(ddd) if ddd and ddd.isdigit() else 9999

    saida = []
    for ddd, d in sorted(por_ddd.items(), key=_chave_ordenacao):
        pct = round(d["tratado"] / d["total"] * 100, 1) if d["total"] else 0.0
        saida.append({
            "ddd": ddd, "total": d["total"], "bloqueadas": d["bloqueadas"],
            "pendente": d["pendente"], "tratado": d["tratado"], "pct": pct,
        })
    return saida


@bp_confirmacao_agenda.route("/confirmacao-agenda")
def confirmacao_agenda_painel():
    from app import usuario_atual
    usuario = usuario_atual()
    if not usuario:
        return redirect("/login")

    padrao_inicio, padrao_fim = _mes_atual_intervalo()
    data_inicio = (request.args.get("data_inicio") or padrao_inicio).strip()
    data_fim = (request.args.get("data_fim") or padrao_fim).strip()

    conn = conectar_conf()
    contratos = _contratos_disponiveis_no_periodo(conn, data_inicio, data_fim)
    resumo = calcular_resumo_confirmacao(contratos)
    painel = calcular_painel_confirmacao(contratos)
    conn.close()
    return render_template(
        "ConfirmacaoAgendaPainel.html", resumo=resumo, painel=painel,
        data_inicio=data_inicio, data_fim=data_fim, usuario_conf=usuario
    )


@bp_confirmacao_agenda.route("/confirmacao-agenda/atender")
def confirmacao_agenda_atender():
    from app import usuario_atual
    usuario = usuario_atual()
    if not usuario:
        return redirect("/login")

    ddd_escolhido = request.args.get("ddd")
    conn = conectar_conf()
    ddds_disponiveis = [r["ddd"] for r in conn.execute(
        "SELECT DISTINCT ddd FROM contratos_confirmacao WHERE status='PENDENTE' ORDER BY ddd"
    ).fetchall()]
    conn.close()

    if not ddd_escolhido:
        return render_template("ConfirmacaoAgendaEscolherDDD.html", ddds_disponiveis=ddds_disponiveis, usuario_conf=usuario)

    contrato = buscar_proximo_confirmacao(usuario["nome"], ddd_escolhido)
    return render_template("ConfirmacaoAgendaAtender.html", contrato=contrato, ddd_escolhido=ddd_escolhido,
                            ddds_disponiveis=ddds_disponiveis, fechamentos=FECHAMENTOS_VALIDOS, usuario_conf=usuario)


@bp_confirmacao_agenda.route("/confirmacao-agenda/tratar", methods=["POST"])
def confirmacao_agenda_tratar():
    from app import usuario_atual
    usuario = usuario_atual()
    if not usuario:
        return redirect("/login")

    id_contrato = request.form.get("id_contrato")
    ddd_escolhido = request.form.get("ddd_escolhido")
    fechamento = request.form.get("fechamento")
    nova_data_br = (request.form.get("nova_data_agendamento") or "").strip()
    novo_periodo = (request.form.get("novo_periodo") or "").strip()
    observacao = (request.form.get("observacao") or "").strip()

    if fechamento not in FECHAMENTOS_VALIDOS:
        return redirect(f"/confirmacao-agenda/atender?ddd={ddd_escolhido}")

    conn = conectar_conf()
    contrato = conn.execute(
        "SELECT * FROM contratos_confirmacao WHERE id=? AND operador_atual=? AND status='EM_ANDAMENTO'",
        (id_contrato, usuario["nome"])
    ).fetchone()
    if not contrato:
        conn.close()
        return redirect(f"/confirmacao-agenda/atender?ddd={ddd_escolhido}")

    agora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # nova_data_br vem do <input type="date"> (formato YYYY-MM-DD nativo)
    nova_data_final = nova_data_br if (fechamento == "REAGENDADA" and nova_data_br) else None

    conn.execute("""
        INSERT INTO tratamentos_confirmacao
            (cd_contrato, operador, fechamento, nova_data_agendamento, novo_periodo, observacao, data_tratamento)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (contrato["cd_contrato"], usuario["nome"], fechamento, nova_data_final, novo_periodo or None, observacao, agora_str))

    if fechamento == "REAGENDADA" and nova_data_final:
        conn.execute("""
            UPDATE contratos_confirmacao SET status='TRATADO', data_agendamento=?, periodo=?,
                atualizado_em=? WHERE id=?
        """, (nova_data_final, novo_periodo or contrato["periodo"], agora_str, id_contrato))
    else:
        conn.execute(
            "UPDATE contratos_confirmacao SET status='TRATADO', atualizado_em=? WHERE id=?",
            (agora_str, id_contrato)
        )
    conn.commit()
    conn.close()
    return redirect(f"/confirmacao-agenda/atender?ddd={ddd_escolhido}")


@bp_confirmacao_agenda.route("/confirmacao-agenda/extracao")
def confirmacao_agenda_extracao():
    from app import usuario_atual
    usuario = usuario_atual()
    if not usuario or usuario["perfil"] != "ADM":
        return "Acesso restrito.", 403

    data_de = request.args.get("data_de") or (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    data_ate = request.args.get("data_ate") or datetime.now().strftime("%Y-%m-%d")
    fechamento_filtro = request.args.get("fechamento") or ""
    operador_filtro = request.args.get("operador") or ""

    conn = conectar_conf()
    condicoes = ["date(t.data_tratamento) BETWEEN ? AND ?"]
    params = [data_de, data_ate]
    if fechamento_filtro:
        condicoes.append("t.fechamento = ?")
        params.append(fechamento_filtro)
    if operador_filtro:
        condicoes.append("t.operador = ?")
        params.append(operador_filtro)

    tratamentos = conn.execute(f"""
        SELECT t.*, c.ddd, c.cidade, c.produto_de, c.produto_para
        FROM tratamentos_confirmacao t
        LEFT JOIN contratos_confirmacao c ON c.cd_contrato = t.cd_contrato
        WHERE {" AND ".join(condicoes)}
        ORDER BY t.data_tratamento DESC
    """, params).fetchall()
    operadores_distintos = [r["operador"] for r in conn.execute(
        "SELECT DISTINCT operador FROM tratamentos_confirmacao ORDER BY operador"
    ).fetchall()]
    conn.close()

    if request.args.get("exportar"):
        registros = [{
            "Contrato": t["cd_contrato"], "DDD": t["ddd"], "Cidade": t["cidade"],
            "Produto De": t["produto_de"], "Produto Para": t["produto_para"],
            "Fechamento": t["fechamento"], "Nova Data (se Reagendado)": t["nova_data_agendamento"],
            "Novo Período": t["novo_periodo"], "Observação": t["observacao"],
            "Operador": t["operador"], "Data Tratamento": t["data_tratamento"],
        } for t in tratamentos]
        df = pd.DataFrame(registros)
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Confirmação de Agenda")
        buffer.seek(0)
        return send_file(
            buffer, as_attachment=True,
            download_name=f"confirmacao_agenda_{data_de}_a_{data_ate}.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

    return render_template("ConfirmacaoAgendaExtracao.html", tratamentos=tratamentos, data_de=data_de, data_ate=data_ate,
                            fechamento_filtro=fechamento_filtro, operador_filtro=operador_filtro,
                            fechamentos=FECHAMENTOS_VALIDOS, operadores_distintos=operadores_distintos, usuario_conf=usuario)


@bp_confirmacao_agenda.route("/confirmacao-agenda/cidades-bloqueadas")
def confirmacao_agenda_cidades_bloqueadas():
    """Subpágina do ADM pra bloquear cidades que não podem ser tratadas por
    algum motivo operacional (ex: obra, sinistro, restrição temporária) --
    pedido do Bruno, 2026-09-17. Bloqueio persiste até o ADM desbloquear --
    não expira sozinho, não depende de data.

    Lista TODAS as cidades da base (as que já têm algum contrato importado)
    com um flag de bloqueio cada, salvando tudo de uma vez com um botão só
    -- pedido do Bruno, 2026-09-19 (antes era um combobox pra bloquear uma
    cidade por vez)."""
    from app import usuario_atual
    usuario = usuario_atual()
    if not usuario or usuario["perfil"] != "ADM":
        return "Acesso restrito.", 403

    conn = conectar_conf()
    todas_cidades = [r["cidade"] for r in conn.execute(
        "SELECT DISTINCT cidade FROM contratos_confirmacao WHERE cidade IS NOT NULL AND cidade != '' ORDER BY cidade"
    ).fetchall()]
    bloqueadas_atuais = {r["cidade"] for r in conn.execute(
        "SELECT cidade FROM cidades_bloqueadas_conf"
    ).fetchall()}
    conn.close()

    cidades = [{"cidade": c, "bloqueada": c in bloqueadas_atuais} for c in todas_cidades]
    return render_template(
        "ConfirmacaoAgendaCidadesBloqueadas.html", cidades=cidades, usuario_conf=usuario
    )


@bp_confirmacao_agenda.route("/confirmacao-agenda/cidades-bloqueadas/salvar", methods=["POST"])
def confirmacao_agenda_salvar_cidades_bloqueadas():
    """Aplica de uma vez só o estado de todos os flags marcados/desmarcados
    na tela -- cidade marcada e que ainda não estava bloqueada entra em
    cidades_bloqueadas_conf; cidade desmarcada e que estava bloqueada sai.
    Cada mudança também grava um evento em historico_bloqueio_cidade
    (pedido do Bruno, 2026-09-19), pra dar pro painel saber se uma cidade
    estava bloqueada num dia passado específico -- o flag em
    cidades_bloqueadas_conf sozinho só sabe o estado ATUAL."""
    from app import usuario_atual
    usuario = usuario_atual()
    if not usuario or usuario["perfil"] != "ADM":
        return "Acesso restrito.", 403

    marcadas = set(request.form.getlist("cidades"))
    agora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = conectar_conf()
    bloqueadas_atuais = {r["cidade"] for r in conn.execute(
        "SELECT cidade FROM cidades_bloqueadas_conf"
    ).fetchall()}

    para_bloquear = marcadas - bloqueadas_atuais
    para_desbloquear = bloqueadas_atuais - marcadas

    for cidade in para_bloquear:
        conn.execute("""
            INSERT INTO cidades_bloqueadas_conf (cidade, motivo, bloqueado_por, bloqueado_em)
            VALUES (?, NULL, ?, ?)
            ON CONFLICT(cidade) DO NOTHING
        """, (cidade, usuario["nome"], agora_str))
        conn.execute("""
            INSERT INTO historico_bloqueio_cidade (cidade, acao, usuario, data_hora)
            VALUES (?, 'BLOQUEAR', ?, ?)
        """, (cidade, usuario["nome"], agora_str))
    for cidade in para_desbloquear:
        conn.execute("DELETE FROM cidades_bloqueadas_conf WHERE cidade=?", (cidade,))
        conn.execute("""
            INSERT INTO historico_bloqueio_cidade (cidade, acao, usuario, data_hora)
            VALUES (?, 'DESBLOQUEAR', ?, ?)
        """, (cidade, usuario["nome"], agora_str))

    conn.commit()
    conn.close()
    return redirect("/confirmacao-agenda/cidades-bloqueadas")


@bp_confirmacao_agenda.route("/confirmacao-agenda/buscar")
def confirmacao_agenda_buscar():
    """Busca um contrato específico (qualquer status) pra rever o
    tratamento -- pedido do Bruno, 2026-09-17: cliente pode ligar de volta
    e precisar mudar o que já foi decidido. Mesma lógica do Buscar
    Contrato do CRM principal: acha pelo número, mostra o histórico e
    permite tratar de novo, não importa o status atual."""
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
        conn = conectar_conf()
        contrato = conn.execute(
            "SELECT * FROM contratos_confirmacao WHERE cd_contrato=?", (cd_contrato,)
        ).fetchone()
        if contrato:
            historico = conn.execute(
                "SELECT * FROM tratamentos_confirmacao WHERE cd_contrato=? ORDER BY id DESC", (cd_contrato,)
            ).fetchall()
        conn.close()

    return render_template(
        "ConfirmacaoAgendaBuscar.html", contrato=contrato, historico=historico,
        buscou=buscou, cd_contrato=cd_contrato, fechamentos=FECHAMENTOS_VALIDOS, usuario_conf=usuario
    )


@bp_confirmacao_agenda.route("/confirmacao-agenda/buscar/tratar", methods=["POST"])
def confirmacao_agenda_buscar_tratar():
    """Salva um novo tratamento pra um contrato achado pela busca -- ao
    contrário de /confirmacao-agenda/tratar (que só aceita quem está
    EM_ANDAMENTO com esse operador, vindo da fila), aqui aceita qualquer
    status, porque é uma revisão manual, não a distribuição normal."""
    from app import usuario_atual
    usuario = usuario_atual()
    if not usuario:
        return redirect("/login")

    id_contrato = request.form.get("id_contrato")
    fechamento = request.form.get("fechamento")
    nova_data_br = (request.form.get("nova_data_agendamento") or "").strip()
    novo_periodo = (request.form.get("novo_periodo") or "").strip()
    observacao = (request.form.get("observacao") or "").strip()

    conn = conectar_conf()
    contrato = conn.execute("SELECT * FROM contratos_confirmacao WHERE id=?", (id_contrato,)).fetchone()
    if not contrato or fechamento not in FECHAMENTOS_VALIDOS:
        conn.close()
        return redirect(f"/confirmacao-agenda/buscar?contrato={request.form.get('cd_contrato', '')}")

    agora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    nova_data_final = nova_data_br if (fechamento == "REAGENDADA" and nova_data_br) else None

    conn.execute("""
        INSERT INTO tratamentos_confirmacao
            (cd_contrato, operador, fechamento, nova_data_agendamento, novo_periodo, observacao, data_tratamento)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (contrato["cd_contrato"], usuario["nome"], fechamento, nova_data_final, novo_periodo or None, observacao, agora_str))

    if fechamento == "REAGENDADA" and nova_data_final:
        conn.execute("""
            UPDATE contratos_confirmacao SET status='TRATADO', data_agendamento=?, periodo=?,
                operador_atual=NULL, atualizado_em=? WHERE id=?
        """, (nova_data_final, novo_periodo or contrato["periodo"], agora_str, id_contrato))
    else:
        conn.execute(
            "UPDATE contratos_confirmacao SET status='TRATADO', operador_atual=NULL, atualizado_em=? WHERE id=?",
            (agora_str, id_contrato)
        )
    conn.commit()
    conn.close()
    return redirect(f"/confirmacao-agenda/buscar?contrato={contrato['cd_contrato']}")


def marcar_nao_tratados_do_dia():
    """Roda 1x por dia via Scheduled Task (ver
    tarefa_confirmacao_nao_tratado.py): qualquer contrato PENDENTE cujas
    datas-alvo de hoje já passaram (ninguém tratou a tempo) vira
    NAO_TRATADO. Cobre TODAS as datas-alvo do dia, não só a principal --
    importante na sexta, onde sábado E a próxima segunda são trabalhados no
    mesmo dia (se sobrou alguma das duas sem tratar, as duas contam).

    Além disso, faz uma varredura de contratos PENDENTE ou EM_ANDAMENTO cuja
    data_agendamento já ficou no passado (antes de hoje) e continuam sem
    tratamento -- cobre o caso de um contrato que ficou "preso" EM_ANDAMENTO
    com um operador que não terminou de tratar a tempo (ele some da tela do
    operador e libera o registro, em vez de ficar reaparecendo indefinidamente
    pra ele em qualquer DDD/data que ele escolher)."""
    conn = conectar_conf()
    total = 0

    datas_alvo = datas_alvo_hoje()
    for data_alvo in datas_alvo:
        cursor = conn.execute(
            "UPDATE contratos_confirmacao SET status='NAO_TRATADO' WHERE status='PENDENTE' AND data_agendamento=?",
            (data_alvo,)
        )
        total += cursor.rowcount

    hoje_str = datetime.now().strftime("%Y-%m-%d")
    cursor = conn.execute(
        "UPDATE contratos_confirmacao SET status='NAO_TRATADO' WHERE status IN ('PENDENTE','EM_ANDAMENTO') AND data_agendamento < ?",
        (hoje_str,)
    )
    total += cursor.rowcount

    conn.commit()
    conn.close()
    return total
