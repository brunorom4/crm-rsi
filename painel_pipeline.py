"""
Painel de Pipeline -- pedido do Bruno, 2026-10-20.

Duas telas: /pipeline (cada um vê o seu) e /pipeline/equipe (consolidado do
supervisor). Não é placar: não há ranking por conversão crua, e ninguém é
comparado com colega nome a nome na tela do operador.

POR QUE NÃO É PLACAR -- as medições que desenharam esta tela
(feitas em 2026-10-19/20 sobre 4 a 6 semanas de dados reais):

1) Conversão individual de UM DIA é ruído: desvio típico de 20 p.p. e
   amplitude de 71 p.p. entre o melhor e o pior dia da MESMA pessoa. Com
   ~13 contatos por dia, um agendamento a mais move 8 pontos. Por isso a
   evolução aqui é semanal, nunca diária.

2) Ranking por conversão crua é injusto: 1ª abordagem converte ~8% e
   retorno de follow-up ~66%, então quem abre contato aparece no fim da
   lista fazendo o trabalho mais difícil. A correlação entre a conversão
   crua e a que o mix de filas sozinho explica é 0,62 -- 38% do ranking é
   só qual fila a pessoa trabalhou. Aqui a conversão é sempre comparada
   com o ESPERADO PELO MIX da própria pessoa.

3) Quebra de agenda não entra na tela do operador: testadas as 28 pessoas
   contra a base do time, nenhuma difere além do que o acaso explica. Não
   depende de quem agendou -- é característica de cidade e infraestrutura.

O QUE ENTRA, ENTÃO: o pipeline real (o que está na mão da pessoa agora), o
funil dela, e ALAVANCAS -- comportamentos que ela controla e cujo retorno
foi medido nesta base. Cada alavanca mostra o número dela, o do time e o
ganho medido, e termina numa ação concreta.
"""
import math
import sqlite3
from datetime import datetime, timedelta, date

from flask import Blueprint, render_template, request, redirect

bp_painel_pipeline = Blueprint("painel_pipeline", __name__)

TABULACOES_SEM_CONTATO = ("2ª Tentativa", "3ª Tentativa", "Improdutivo")

# Pontos de corte vindos das medições. Ver GANHOS logo abaixo.
HORA_FIM_MANHA = 13          # manhã = antes das 13h
HORA_INICIO_RETORNO = 10     # retentativa antes disso rende muito menos
PRAZO_RETORNO_HORAS = 24     # retorno de follow-up dentro de um dia
SEMANAS_PADRAO = 6
MIN_CONTATOS_ALAVANCA = 20   # abaixo disso o percentual da pessoa é ruído

# Ganhos medidos em 2026-10-20 sobre 6 semanas. Ficam explícitos aqui, e
# não embutidos no texto da tela, porque precisam ser REMEDIDOS de tempos
# em tempos -- número de estudo envelhece e vira folclore.
GANHOS = {
    "retorno_cedo": {
        "titulo": "Retentativa antes das 10h",
        "metrica": "falar com o cliente",
        "ruim": 22.7, "bom": 50.3,
        "acao": "Deixe as retentativas para depois das 10h — o melhor "
                "intervalo é das 12h às 17h. Antes das 10h menos da metade "
                "atende.",
        "medido": "25 desvios, dentro da mesma faixa de prioridade",
    },
    "fria_tarde": {
        "titulo": "1ª abordagem à tarde",
        "metrica": "conversão",
        "ruim": 6.6, "bom": 12.8,
        "acao": "Concentre a primeira abordagem na manhã. Ela converte o "
                "dobro antes das 13h, e não é por causa do lead: a "
                "prioridade média é igual de manhã e à tarde.",
        "medido": "6 desvios, efeito mantido dentro de cada faixa de prioridade",
    },
    "followup_atrasado": {
        "titulo": "Follow-up devolvido com mais de um dia",
        "metrica": "conversão",
        "ruim": 29.7, "bom": 51.2,
        "acao": "Volte no cliente dentro do primeiro dia. Passando de 24h a "
                "conversão cai; acima de três dias, cai pela metade.",
        "medido": "11.754 retornos; parte do efeito pode ser do cliente "
                  "difícil, não só do atraso",
    },
    "mesmo_horario": {
        "titulo": "Retentativa no mesmo horário",
        "metrica": "falar com o cliente",
        "ruim": 68.8, "bom": 83.5,
        "acao": "Se não atendeu, tente em outra faixa de horário. Repetir o "
                "mesmo horário com quem não atende é gastar tentativa.",
        "medido": "6,5 desvios em 1.633 retentativas",
    },
}


# ---------------------------------------------------------------------------
# Consultas
# ---------------------------------------------------------------------------
def _marcadores(valores):
    return ",".join("?" * len(valores))


def _semana_atual(hoje):
    ini = hoje - timedelta(days=hoje.weekday())
    return ini, ini + timedelta(days=6)


def pipeline_agora(conn, operador, hoje):
    """O que está na mão da pessoa NESTE momento.

    É a parte que serve todo dia, diferente de qualquer indicador: o que
    vence hoje, o que atrasou, o que foi agendado e o que já teve desfecho.
    """
    hoje_str = hoje.isoformat()
    ini_sem, fim_sem = _semana_atual(hoje)

    fup = conn.execute("""
        SELECT
          COALESCE(SUM(CASE WHEN date(data_retorno) = ? THEN 1 ELSE 0 END), 0) hoje,
          COALESCE(SUM(CASE WHEN date(data_retorno) < ? THEN 1 ELSE 0 END), 0) atrasados,
          COALESCE(SUM(CASE WHEN date(data_retorno) > ? THEN 1 ELSE 0 END), 0) futuros
        FROM followup
        WHERE status='PENDENTE' AND operador = ?
    """, (hoje_str, hoje_str, hoje_str, operador)).fetchone()

    # Atrasado há mais tempo: é o que o benchmark de pipeline chama de
    # "sinal de risco por recência" -- o que está parado há mais tempo é o
    # que mais provavelmente se perde.
    mais_antigo = conn.execute("""
        SELECT MIN(date(data_retorno)) d FROM followup
        WHERE status='PENDENTE' AND operador = ? AND date(data_retorno) < ?
    """, (operador, hoje_str)).fetchone()["d"]
    dias_parado = None
    if mais_antigo:
        try:
            dias_parado = (hoje - date.fromisoformat(mais_antigo)).days
        except ValueError:
            dias_parado = None

    agendas = conn.execute("""
        SELECT COUNT(*) n FROM atividades
        WHERE tabulacao='Agendado' AND status='CONCLUIDO' AND operador = ?
          AND data_agendamento BETWEEN ? AND ?
    """, (operador, ini_sem.isoformat(), fim_sem.isoformat())).fetchone()["n"]

    # Desfecho das agendas da pessoa nas últimas 8 semanas: a importação da
    # quebra é manual e chega com atraso, então uma janela curta mostraria
    # zero execução e passaria impressão errada.
    desfecho = conn.execute("""
        SELECT
          COALESCE(SUM(CASE WHEN q.tipo_tratamento='SEM QUEBRA DE AGENDA' THEN 1 ELSE 0 END), 0) executadas,
          COALESCE(SUM(CASE WHEN q.tipo_tratamento='COM QUEBRA DE AGENDA' THEN 1 ELSE 0 END), 0) quebradas
        FROM atividades a
        JOIN quebras_agenda q
          ON q.contrato = a.contrato AND q.dat_agenda = a.data_agendamento
        WHERE a.tabulacao='Agendado' AND a.status='CONCLUIDO' AND a.operador = ?
          AND a.data_agendamento BETWEEN ? AND ?
    """, (operador, (hoje - timedelta(days=56)).isoformat(), hoje_str)).fetchone()

    em_maos = conn.execute("""
        SELECT COUNT(*) n FROM mailing
        WHERE operador = ? AND status IN ('EM_ANDAMENTO','ENFILEIRADO')
    """, (operador,)).fetchone()["n"]

    return {
        "followups_hoje": fup["hoje"], "followups_atrasados": fup["atrasados"],
        "followups_futuros": fup["futuros"], "dias_parado": dias_parado,
        "agendas_semana": agendas, "em_maos": em_maos,
        "executadas": desfecho["executadas"], "quebradas": desfecho["quebradas"],
        "inicio_semana": ini_sem, "fim_semana": fim_sem,
    }


def funil(conn, ini, fim, operador=None):
    """Entrou, falou, converteu -- e, nas agendas, executou ou quebrou."""
    onde = "AND a.operador = ?" if operador else ""
    extra = [operador] if operador else []
    m = _marcadores(TABULACOES_SEM_CONTATO)
    linha = conn.execute(f"""
        SELECT COUNT(*) tentativas,
          COALESCE(SUM(CASE WHEN a.tabulacao<>'Improdutivo' THEN 1 ELSE 0 END), 0) falou,
          COALESCE(SUM(CASE WHEN a.tabulacao NOT IN ({m}) THEN 1 ELSE 0 END), 0) contatos,
          COALESCE(SUM(CASE WHEN a.tabulacao='Agendado' THEN 1 ELSE 0 END), 0) agendados,
          COALESCE(SUM(CASE WHEN a.tentativa=1 AND a.tabulacao NOT IN ({m}) THEN 1 ELSE 0 END), 0) fria,
          COALESCE(SUM(CASE WHEN a.tentativa=1 AND a.tabulacao='Agendado' THEN 1 ELSE 0 END), 0) fria_ag,
          COALESCE(SUM(CASE WHEN a.tentativa>1 AND a.tabulacao NOT IN ({m}) THEN 1 ELSE 0 END), 0) retorno,
          COALESCE(SUM(CASE WHEN a.tentativa>1 AND a.tabulacao='Agendado' THEN 1 ELSE 0 END), 0) retorno_ag,
          COALESCE(SUM(CASE WHEN a.tabulacao LIKE 'Recusado%' THEN 1 ELSE 0 END), 0) recusado,
          COALESCE(SUM(CASE WHEN a.tabulacao='Cliente Já Migrado' THEN 1 ELSE 0 END), 0) ja_migrado
        FROM atividades a
        WHERE a.status='CONCLUIDO' AND date(a.data_fim) BETWEEN ? AND ? {onde}
    """, list(TABULACOES_SEM_CONTATO) * 3 + [ini.isoformat(), fim.isoformat()] + extra).fetchone()
    d = dict(linha)
    d["conversao"] = (100.0 * d["agendados"] / d["contatos"]) if d["contatos"] else None
    d["taxa_falou"] = (100.0 * d["falou"] / d["tentativas"]) if d["tentativas"] else None
    d["conv_fria"] = (100.0 * d["fria_ag"] / d["fria"]) if d["fria"] else None
    d["conv_retorno"] = (100.0 * d["retorno_ag"] / d["retorno"]) if d["retorno"] else None
    d["pct_do_retorno"] = (100.0 * d["retorno_ag"] / d["agendados"]) if d["agendados"] else None
    return d


def _percentuais_alavanca(conn, ini, fim, operador=None):
    """Os quatro comportamentos, em percentual, para uma pessoa ou o time.

    Devolve também o denominador de cada um: percentual sobre 3 tentativas
    não significa nada, e a tela esconde o que está abaixo do mínimo.
    """
    onde = "AND a.operador = ?" if operador else ""
    extra = [operador] if operador else []
    p = [ini.isoformat(), fim.isoformat()]

    r = conn.execute(f"""
        SELECT
          COALESCE(SUM(CASE WHEN a.tentativa>1 THEN 1 ELSE 0 END), 0) retentativas,
          COALESCE(SUM(CASE WHEN a.tentativa>1
                   AND CAST(strftime('%H', a.data_inicio) AS INT) < {HORA_INICIO_RETORNO}
                   THEN 1 ELSE 0 END), 0) retentativas_cedo,
          COALESCE(SUM(CASE WHEN a.tentativa=1 THEN 1 ELSE 0 END), 0) frias,
          COALESCE(SUM(CASE WHEN a.tentativa=1
                   AND CAST(strftime('%H', a.data_inicio) AS INT) >= {HORA_FIM_MANHA}
                   THEN 1 ELSE 0 END), 0) frias_tarde
        FROM atividades a
        WHERE a.status='CONCLUIDO' AND a.data_inicio IS NOT NULL
          AND date(a.data_fim) BETWEEN ? AND ? {onde}
    """, p + extra).fetchone()

    # Alternância: só retentativa de quem NÃO atendeu. Em quem já atendeu, o
    # horário da próxima ligação é o que o cliente pediu, não escolha nossa.
    alt = conn.execute(f"""
        WITH t AS (
          SELECT a.operador, a.data_inicio, a.data_fim,
                 LAG(a.data_inicio) OVER (PARTITION BY a.contrato ORDER BY a.data_inicio) ant,
                 LAG(a.tabulacao)   OVER (PARTITION BY a.contrato ORDER BY a.data_inicio) ant_tab
          FROM atividades a
          WHERE a.status='CONCLUIDO' AND a.data_inicio IS NOT NULL
        )
        SELECT COUNT(*) n,
          COALESCE(SUM(CASE WHEN ABS(CAST(strftime('%H', data_inicio) AS INT)
                                   - CAST(strftime('%H', ant) AS INT)) < 2
                       THEN 1 ELSE 0 END), 0) mesmo_horario
        FROM t
        WHERE ant IS NOT NULL AND ant_tab='Improdutivo'
          AND date(data_fim) BETWEEN ? AND ?
          {"AND operador = ?" if operador else ""}
    """, p + extra).fetchone()

    fup = conn.execute(f"""
        WITH r AS (
          SELECT f.operador, f.data_retorno,
                 (SELECT MIN(a.data_inicio) FROM atividades a
                   WHERE a.id_mailing = f.id_mailing AND a.data_inicio > f.data_retorno
                     AND a.status='CONCLUIDO') ligou
          FROM followup f
          WHERE f.status='TRATADO' AND date(f.data_retorno) BETWEEN ? AND ?
        )
        SELECT COUNT(*) n,
          COALESCE(SUM(CASE WHEN (julianday(ligou)-julianday(data_retorno))*24
                                 > {PRAZO_RETORNO_HORAS} THEN 1 ELSE 0 END), 0) atrasados
        FROM r WHERE ligou IS NOT NULL
          {"AND operador = ?" if operador else ""}
    """, p + extra).fetchone()

    def pct(parte, total):
        return (100.0 * parte / total) if total else None

    return {
        "retorno_cedo": {"pct": pct(r["retentativas_cedo"], r["retentativas"]),
                         "n": r["retentativas"]},
        "fria_tarde": {"pct": pct(r["frias_tarde"], r["frias"]), "n": r["frias"]},
        "mesmo_horario": {"pct": pct(alt["mesmo_horario"], alt["n"]), "n": alt["n"]},
        "followup_atrasado": {"pct": pct(fup["atrasados"], fup["n"]), "n": fup["n"]},
    }


def alavancas(conn, ini, fim, operador):
    """O número da pessoa, o do time e a ação. Em todas elas, MENOR é melhor
    -- são os quatro comportamentos que custam resultado."""
    meus = _percentuais_alavanca(conn, ini, fim, operador)
    time = _percentuais_alavanca(conn, ini, fim, None)

    saida = []
    for chave, info in GANHOS.items():
        meu, tm = meus[chave], time[chave]
        if meu["pct"] is None or meu["n"] < MIN_CONTATOS_ALAVANCA:
            continue
        diferenca = meu["pct"] - (tm["pct"] or 0)
        saida.append({
            "chave": chave, "titulo": info["titulo"], "metrica": info["metrica"],
            "meu": meu["pct"], "n": meu["n"], "time": tm["pct"],
            "diferenca": diferenca,
            "ruim": info["ruim"], "bom": info["bom"],
            "ganho": info["bom"] - info["ruim"],
            "acao": info["acao"], "medido": info["medido"],
            # "acima do time" sozinho não basta: três alavancas marcadas como
            # prioritárias ao mesmo tempo não orientam ninguém.
            "acima_do_time": diferenca > 5,
            "prioritaria": False,
        })
    saida.sort(key=lambda a: -a["diferenca"])
    # Uma só prioritária: a maior folga contra o time, e só se a folga for
    # grande o bastante pra valer uma conversa.
    if saida and saida[0]["acima_do_time"]:
        saida[0]["prioritaria"] = True
    return saida


def evolucao_semanal(conn, fim, semanas=SEMANAS_PADRAO, operador=None):
    """Conversão semanal contra o esperado pelo mix de filas da pessoa.

    A margem vem da variância binomial do próprio mix: é ela que diz se a
    variação da semana significa alguma coisa. Sem isso, a pessoa lê como
    queda o que é oscilação normal.
    """
    inicio = fim - timedelta(days=7 * semanas - 1)
    inicio -= timedelta(days=inicio.weekday())
    m = _marcadores(TABULACOES_SEM_CONTATO)

    base = conn.execute(f"""
        SELECT a.tentativa,
          COALESCE(SUM(CASE WHEN a.tabulacao NOT IN ({m}) THEN 1 ELSE 0 END), 0) ct,
          COALESCE(SUM(CASE WHEN a.tabulacao='Agendado' THEN 1 ELSE 0 END), 0) ag
        FROM atividades a
        WHERE a.status='CONCLUIDO' AND date(a.data_fim) BETWEEN ? AND ?
        GROUP BY a.tentativa
    """, list(TABULACOES_SEM_CONTATO) + [inicio.isoformat(), fim.isoformat()]).fetchall()
    taxa = {l["tentativa"]: (l["ag"] / l["ct"]) if l["ct"] else 0.0 for l in base}

    onde = "AND a.operador = ?" if operador else ""
    extra = [operador] if operador else []
    linhas = conn.execute(f"""
        SELECT strftime('%Y-%W', a.data_fim) sem, a.tentativa,
          COALESCE(SUM(CASE WHEN a.tabulacao NOT IN ({m}) THEN 1 ELSE 0 END), 0) ct,
          COALESCE(SUM(CASE WHEN a.tabulacao='Agendado' THEN 1 ELSE 0 END), 0) ag
        FROM atividades a
        WHERE a.status='CONCLUIDO' AND date(a.data_fim) BETWEEN ? AND ? {onde}
        GROUP BY sem, a.tentativa
    """, list(TABULACOES_SEM_CONTATO) + [inicio.isoformat(), fim.isoformat()] + extra).fetchall()

    por_semana = {}
    for l in linhas:
        por_semana.setdefault(l["sem"], []).append(l)

    saida = []
    d = inicio
    while d <= fim:
        chave = d.strftime("%Y-%W")
        filas = por_semana.get(chave, [])
        total = sum(f["ct"] for f in filas)
        agendados = sum(f["ag"] for f in filas)
        if total:
            esperado = sum(f["ct"] * taxa.get(f["tentativa"], 0.0) for f in filas) / total
            variancia = sum(f["ct"] * taxa.get(f["tentativa"], 0.0)
                            * (1 - taxa.get(f["tentativa"], 0.0)) for f in filas)
            margem = 100.0 * 2 * math.sqrt(variancia) / total
            saida.append({
                "inicio": d, "contatos": total, "agendados": agendados,
                "conversao": 100.0 * agendados / total,
                "esperado": 100.0 * esperado, "margem": margem,
                "diferenca": 100.0 * agendados / total - 100.0 * esperado,
            })
        else:
            saida.append({"inicio": d, "contatos": 0, "agendados": 0, "conversao": None,
                          "esperado": None, "margem": None, "diferenca": None})
        d += timedelta(days=7)
    return saida


def dados_operador(conn, operador, hoje=None, semanas=SEMANAS_PADRAO):
    hoje = hoje or datetime.now().date()
    ini = hoje - timedelta(days=7 * semanas - 1)
    return {
        "operador": operador, "hoje": hoje, "semanas": semanas,
        "inicio": ini, "fim": hoje,
        "pipeline": pipeline_agora(conn, operador, hoje),
        "funil": funil(conn, ini, hoje, operador),
        "funil_time": funil(conn, ini, hoje, None),
        "alavancas": alavancas(conn, ini, hoje, operador),
        "evolucao": evolucao_semanal(conn, hoje, semanas, operador),
    }


def dados_equipe(conn, hoje=None, semanas=SEMANAS_PADRAO):
    """Consolidado do supervisor: funil do time, alavancas por pessoa e o
    quadrante de desempenho (conversão ajustada x volume)."""
    hoje = hoje or datetime.now().date()
    ini = hoje - timedelta(days=7 * semanas - 1)

    pessoas = []
    nomes = [r["operador"] for r in conn.execute("""
        SELECT DISTINCT operador FROM atividades
        WHERE status='CONCLUIDO' AND date(data_fim) BETWEEN ? AND ?
          AND operador IS NOT NULL
    """, (ini.isoformat(), hoje.isoformat())).fetchall()]

    time_pct = _percentuais_alavanca(conn, ini, hoje, None)
    for nome in nomes:
        f = funil(conn, ini, hoje, nome)
        if f["contatos"] < MIN_CONTATOS_ALAVANCA:
            continue
        ev = [e for e in evolucao_semanal(conn, hoje, semanas, nome)
              if e["conversao"] is not None]
        pct = _percentuais_alavanca(conn, ini, hoje, nome)
        # Diferença acumulada do período, não a média das semanas: semana
        # curta pesaria igual a semana cheia.
        total_ct = sum(e["contatos"] for e in ev)
        dif = (sum(e["diferenca"] * e["contatos"] for e in ev) / total_ct) if total_ct else 0.0
        pessoas.append({
            "operador": nome, "funil": f, "alavancas": pct,
            "diferenca": dif, "agendados": f["agendados"], "contatos": f["contatos"],
        })

    pessoas.sort(key=lambda p: -p["diferenca"])
    return {
        "hoje": hoje, "inicio": ini, "fim": hoje, "semanas": semanas,
        "minimo": MIN_CONTATOS_ALAVANCA,
        "funil": funil(conn, ini, hoje, None),
        "evolucao": evolucao_semanal(conn, hoje, semanas, None),
        "pessoas": pessoas,
        "time_alavancas": time_pct,
        "ganhos": GANHOS,
    }


# ---------------------------------------------------------------------------
# Rotas -- login/permissão importados aqui dentro, pra evitar import circular
# (mesmo padrão dos outros blueprints do projeto).
# ---------------------------------------------------------------------------
@bp_painel_pipeline.route("/pipeline")
def pipeline_pessoal():
    from app import login_required, usuario_atual, conectar

    @login_required
    def interno():
        usuario = usuario_atual()
        # O supervisor pode abrir o painel de alguém pelo consolidado; o
        # operador só enxerga o próprio, independente do que vier na URL.
        alvo = usuario["nome"]
        pedido = (request.args.get("pessoa") or "").strip()
        if pedido and usuario["perfil"] in ("ADM", "SUPERVISOR"):
            alvo = pedido
        conn = conectar()
        try:
            dados = dados_operador(conn, alvo)
        finally:
            conn.close()
        return render_template("PainelPipeline.html", d=dados, usuario=usuario,
                               eh_outro=(alvo != usuario["nome"]))
    return interno()


@bp_painel_pipeline.route("/pipeline/equipe")
def pipeline_equipe():
    from app import login_required, perfil_required, usuario_atual, conectar

    @login_required
    @perfil_required("ADM", "SUPERVISOR")
    def interno():
        conn = conectar()
        try:
            dados = dados_equipe(conn)
        finally:
            conn.close()
        return render_template("PainelPipelineEquipe.html", d=dados,
                               usuario=usuario_atual())
    return interno()
