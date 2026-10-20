"""
Relatório gerencial por e-mail -- pedido do Bruno, 2026-10-19.

Substitui o relatório que morreu com a migração pra web (dependia do Power
Automate lendo uma pasta). Módulo separado: não importa o app.py, conversa
direto com o crm.db, e roda como Scheduled Task.

A DIVISÃO DIÁRIO / SEMANAL É PROPOSITAL

  O diário mostra os números do time e só o que fugiu do padrão. Se nada
  fugiu, ele diz isso e acaba -- relatório que às vezes é curto é relatório
  que continua sendo lido.

  O que NÃO entra no diário: conversão por pessoa. Medido em 2026-10-19
  sobre os dados reais, a conversão individual de um dia tem desvio típico
  de 20 p.p. e amplitude típica de 71 p.p. entre o melhor e o pior dia da
  MESMA pessoa -- com ~13 contatos por pessoa por dia, um agendamento a
  mais move 8 pontos. Apontar "fulano caiu" em cima disso é apontar ruído.
  Conversão por pessoa fica no semanal, onde cada um acumula 60-75 contatos.

  No diário ficam os desvios de VOLUME, que são reais e geralmente apontam
  ausência, problema de fila ou algo operacional.

  O SEMANAL responde o que um dia não comporta: o que é desempenho de
  gente, o que é característica de cidade e o que é efeito de configuração
  nossa. Ele ajusta a conversão de cada pessoa pelo mix de filas que ela
  trabalhou -- sem isso o ranking premia quem pega retorno de follow-up e
  pune quem abre contato frio, que convertem 58% e 6%.

COMO USAR

  Prévia em arquivo (não envia nada):
      python3.10 relatorio_gerencial.py --diario --preview previa.html
      python3.10 relatorio_gerencial.py --semanal --preview previa.html

  Período específico:
      python3.10 relatorio_gerencial.py --diario --data 2026-10-16 --preview p.html
      python3.10 relatorio_gerencial.py --semanal --semana 2026-10-14 --preview p.html
      (--semana aceita qualquer dia da semana desejada)

  Envio de verdade (é o que as Scheduled Tasks rodam):
      python3.10 relatorio_gerencial.py --diario --enviar
      python3.10 relatorio_gerencial.py --semanal --enviar

  Sem --semana, o semanal usa a última semana seg-dom fechada. A tarefa
  agendada para segunda de manhã pega a semana que acabou de terminar.

CONFIGURAÇÃO DO E-MAIL

  Reaproveita o que já existe no banco (tabela configuracoes): email_smtp_host,
  email_smtp_porta, email_smtp_usuario, email_smtp_senha, email_remetente.
  Os destinatários saem de relatorio_email_destinatarios (ativo=1).

  Pro Gmail: host smtp.gmail.com, porta 587, usuário é o e-mail completo e a
  senha é uma SENHA DE APP (gerada em myaccount.google.com, exige verificação
  em duas etapas). A senha normal da conta não funciona.
"""
import os
import sys
import sqlite3
import smtplib
import statistics
from email.message import EmailMessage
from datetime import datetime, timedelta, date
from collections import defaultdict

PASTA_BASE = os.path.dirname(os.path.abspath(__file__))
CAMINHO_CRM_DB = os.path.join(PASTA_BASE, "crm.db")
CAMINHO_BACKLOG_DB = os.path.join(PASTA_BASE, "backlog.db")

# Tabulações que NÃO encerram o contato -- são reenfileiramento, não tratativa
# concluída. Ficam fora da conta de "contatos efetivos".
TABULACOES_SEM_CONTATO = ("2ª Tentativa", "3ª Tentativa", "Improdutivo")

DIAS_HISTORICO = 30          # janela pra calcular o padrão de cada pessoa
MIN_DIAS_HISTORICO = 10      # abaixo disso não dá pra dizer o que é padrão
LIMIAR_DESVIO = 2.5          # quantos desvios robustos pra virar "variação relevante"
CHUVA_ALTA = 61              # % de chance a partir da qual o efeito aparece nos dados

# Medido em 2026-10-19: em dia de chuva alta a execução cai de 78,6% pra
# 70,6%. Metade do estrago não é rotulado como CHUVA -- aparece em
# AGENDAMENTO, MOVIMENTAÇÕES e TUBULAÇÃO (deslocamento de equipe).
PERDA_ESPERADA_CHUVA_PP = 8


def conectar(caminho=None):
    conn = sqlite3.connect(caminho or CAMINHO_CRM_DB)
    conn.row_factory = sqlite3.Row
    return conn


def dias_uteis_anteriores(ate, quantos):
    """Lista de dias úteis (seg-sex) terminando em `ate`, do mais antigo
    pro mais novo. Feriado não é tratado -- aparece como dia de volume
    baixo, o que o cálculo robusto absorve."""
    dias, d = [], ate
    while len(dias) < quantos:
        if d.weekday() < 5:
            dias.append(d)
        d -= timedelta(days=1)
    return list(reversed(dias))


def numeros_do_dia(conn, dia):
    """Números do time no dia. Tudo em cima de atividades CONCLUIDO, que é
    a mesma base usada pelo ranking do CRM.

    Os COALESCE não são decoração: num dia sem nenhuma atividade (domingo,
    feriado, ou a madrugada antes da primeira tabulação) o SUM do SQLite
    devolve NULL, não zero -- e aí qualquer conta adiante quebra.
    """
    marcadores = ",".join("?" * len(TABULACOES_SEM_CONTATO))
    linha = conn.execute(f"""
        SELECT COUNT(*) atividades,
               COALESCE(SUM(CASE WHEN tabulacao NOT IN ({marcadores}) THEN 1 ELSE 0 END), 0) contatos,
               COALESCE(SUM(CASE WHEN tabulacao='Agendado' THEN 1 ELSE 0 END), 0) agendados,
               COUNT(DISTINCT operador) pessoas
        FROM atividades
        WHERE status='CONCLUIDO' AND date(data_fim)=?
    """, list(TABULACOES_SEM_CONTATO) + [dia.isoformat()]).fetchone()
    d = dict(linha)
    d["conversao"] = (100.0 * d["agendados"] / d["contatos"]) if d["contatos"] else None
    return d


def media_mesmo_dia_semana(conn, dia, quantas=4):
    """Base de comparação: as últimas N ocorrências do mesmo dia da semana.
    Segunda compara com segunda -- o volume varia muito por dia da semana e
    comparar com 'ontem' daria falso alarme toda segunda."""
    amostras, d = [], dia - timedelta(days=7)
    while len(amostras) < quantas:
        n = numeros_do_dia(conn, d)
        if n["atividades"]:
            amostras.append(n)
        d -= timedelta(days=7)
        if (dia - d).days > 70:
            break
    if not amostras:
        return None
    conversoes = [a["conversao"] for a in amostras if a["conversao"] is not None]
    return {
        "atividades": statistics.mean(a["atividades"] for a in amostras),
        "contatos": statistics.mean(a["contatos"] for a in amostras),
        "agendados": statistics.mean(a["agendados"] for a in amostras),
        "conversao": statistics.mean(conversoes) if conversoes else None,
        "n": len(amostras),
    }


ABREV_DIA = ["SEG", "TER", "QUA", "QUI", "SEX", "SAB", "DOM"]


def quem_esta_fora(conn, dia):
    """Quem não era esperado trabalhando no dia, e por quê.

    Sem isso, quem está de férias vira "variação relevante" todo santo dia
    e o relatório morre de tanto alarme falso. Três fontes, nessa ordem de
    especificidade: ausência registrada na data, período de férias da
    escala, e o dia da semana não estar na escala da pessoa.
    """
    fora = {}
    nomes = {r["id"]: r["nome"] for r in conn.execute("SELECT id, nome FROM usuarios").fetchall()}

    for linha in conn.execute(
            "SELECT usuario_id, motivo FROM ausencias WHERE data=?", (dia.isoformat(),)).fetchall():
        nome = nomes.get(linha["usuario_id"])
        if nome:
            fora[nome] = linha["motivo"] or "ausência"

    for linha in conn.execute("""
            SELECT usuario_id, dias_trabalho, ferias_inicio, ferias_fim FROM escalas""").fetchall():
        nome = nomes.get(linha["usuario_id"])
        if not nome or nome in fora:
            continue
        ini, fim = linha["ferias_inicio"], linha["ferias_fim"]
        if ini and fim and ini <= dia.isoformat() <= fim:
            fora[nome] = "Férias"
        elif linha["dias_trabalho"] and ABREV_DIA[dia.weekday()] not in linha["dias_trabalho"]:
            fora[nome] = "fora da escala"
    return fora


def variacoes_de_volume(conn, dia):
    """Quem fez volume fora do próprio padrão hoje.

    Compara cada pessoa com ELA MESMA nos últimos dias úteis, usando mediana
    e desvio absoluto mediano (MAD) em vez de média e desvio padrão -- com
    série curta, um único dia atípico contamina a média e esconde o desvio
    seguinte. O MAD não se deixa puxar por um ponto solto.
    """
    inicio = (dia - timedelta(days=DIAS_HISTORICO * 2)).isoformat()
    linhas = conn.execute("""
        SELECT operador, date(data_fim) d, COUNT(*) atividades
        FROM atividades
        WHERE status='CONCLUIDO' AND date(data_fim) BETWEEN ? AND ?
        GROUP BY operador, d
    """, (inicio, dia.isoformat())).fetchall()

    por_pessoa = defaultdict(dict)
    for linha in linhas:
        por_pessoa[linha["operador"]][linha["d"]] = linha["atividades"]

    uteis = dias_uteis_anteriores(dia - timedelta(days=1), DIAS_HISTORICO)
    fora = quem_esta_fora(conn, dia)
    achados = []
    for pessoa, serie in por_pessoa.items():
        hoje = serie.get(dia.isoformat(), 0)
        # Fez zero e estava de férias/ausente/fora de escala: isso não é
        # variação, é o esperado. Sai do relatório.
        if hoje == 0 and pessoa in fora:
            continue
        historico = [serie.get(u.isoformat(), 0) for u in uteis if u.isoformat() in serie]
        if len(historico) < MIN_DIAS_HISTORICO:
            continue
        mediana = statistics.median(historico)
        desvios = [abs(x - mediana) for x in historico]
        mad = statistics.median(desvios)
        # MAD zero (pessoa muito regular) tornaria qualquer variação infinita;
        # o piso de 3 evita apontar ruído de 1-2 atividades como desvio.
        escala = max(mad * 1.4826, 3.0)
        z = (hoje - mediana) / escala
        if abs(z) >= LIMIAR_DESVIO:
            achados.append({
                "pessoa": pessoa, "hoje": hoje, "normal": mediana,
                "z": z, "sentido": "acima" if z > 0 else "abaixo",
                # Trabalhou mesmo estando marcado como fora: o número é real,
                # mas o contexto explica, então vai junto em vez de virar dúvida.
                "contexto": fora.get(pessoa),
            })
    achados.sort(key=lambda a: -abs(a["z"]))
    return achados


def situacao_da_fila(conn):
    fila = dict(conn.execute("""
        SELECT SUM(CASE WHEN status='PENDENTE' THEN 1 ELSE 0 END) pendentes,
               SUM(CASE WHEN status='ENFILEIRADO' THEN 1 ELSE 0 END) enfileirados,
               SUM(CASE WHEN status='EM_ANDAMENTO' THEN 1 ELSE 0 END) em_andamento
        FROM mailing
    """).fetchone())
    fila["backlog"] = None
    if os.path.exists(CAMINHO_BACKLOG_DB):
        try:
            cb = conectar(CAMINHO_BACKLOG_DB)
            fila["backlog"] = cb.execute(
                "SELECT COUNT(*) c FROM contratos_backlog WHERE status IN ('PENDENTE','EM_ANDAMENTO')"
            ).fetchone()["c"]
            cb.close()
        except Exception:
            pass
    return fila


def risco_de_chuva(conn, dia):
    """Cidades com chuva alta prevista pro próximo dia útil, cruzadas com as
    agendas já marcadas lá. É a parte acionável do relatório: a previsão
    existe com dias de antecedência, e o efeito medido é do mesmo dia."""
    alvo = dia + timedelta(days=1)
    while alvo.weekday() >= 5:
        alvo += timedelta(days=1)
    previsao = conn.execute(
        "SELECT cidade, chance_chuva, condicao FROM previsao_tempo WHERE data=? AND chance_chuva>=?",
        (alvo.isoformat(), CHUVA_ALTA)
    ).fetchall()
    if not previsao:
        return {"data": alvo, "cidades": []}

    agendas = dict(conn.execute("""
        SELECT m.cidade, COUNT(*) n FROM atividades a
        JOIN mailing m ON m.id = a.id_mailing
        WHERE a.tabulacao='Agendado' AND a.data_agendamento=? GROUP BY m.cidade
    """, (alvo.isoformat(),)).fetchall())

    cidades = [{
        "cidade": p["cidade"], "chance": p["chance_chuva"],
        "condicao": p["condicao"], "agendas": agendas.get(p["cidade"], 0),
    } for p in previsao]
    cidades.sort(key=lambda c: (-c["agendas"], -c["chance"]))
    return {"data": alvo, "cidades": cidades}


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------
ESTILO = """
body{margin:0;padding:24px;background:#f4f5f9;font-family:Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#111b2e}
.wrap{max-width:680px;margin:0 auto}
.card{background:#fff;border-radius:10px;padding:20px 22px;margin-bottom:14px;box-shadow:0 1px 3px rgba(15,23,42,.12)}
h1{font-size:19px;margin:0 0 2px}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.6px;color:#6b707b;margin:0 0 14px}
.sub{color:#6b707b;font-size:13px;margin:0 0 18px}
.num{display:inline-block;margin:0 28px 10px 0}
.num b{display:block;font-size:26px;line-height:1.1}
.num span{font-size:12px;color:#6b707b}
.cmp{font-size:12px;color:#6b707b}
.up{color:#187a46}.down{color:#da291c}
table{width:100%;border-collapse:collapse;font-size:13px}
td,th{padding:7px 4px;text-align:left;border-bottom:1px solid #eceef3}
th{font-size:11px;text-transform:uppercase;color:#6b707b;letter-spacing:.4px}
td.r,th.r{text-align:right}
.ok{color:#187a46;font-size:14px;margin:0}
.nota{font-size:12px;color:#6b707b;margin:12px 0 0;line-height:1.5}
.chuva td:first-child{font-weight:600}
.quad{width:100%;border-collapse:separate;border-spacing:8px;table-layout:fixed}
.quad td{border:1px solid #e2e5ec;border-radius:8px;padding:11px 12px;vertical-align:top;border-bottom:1px solid #e2e5ec}
.quad .tit{font-size:11px;text-transform:uppercase;letter-spacing:.4px;font-weight:700;margin:0 0 7px}
.quad .item{font-size:12px;line-height:1.55;color:#2a3246}
.quad .vazio{font-size:12px;color:#9aa0ab;font-style:italic}
.q-bom{background:#f1f9f4}.q-bom .tit{color:#186b40}
.q-atencao{background:#fdf4f3}.q-atencao .tit{color:#a8251b}
.q-neutro{background:#f7f8fb}.q-neutro .tit{color:#5b6376}
.eixos{font-size:11px;color:#8a909c;margin:2px 0 10px}
.destaque{font-size:14px;line-height:1.65;margin:0 0 10px}
.destaque b{color:#111b2e}
.tag{display:inline-block;font-size:10px;padding:1px 6px;border-radius:9px;background:#eceef3;color:#5b6376;margin-left:5px;vertical-align:1px}
.tag-bl{background:#fdeceb;color:#a8251b}
"""


def _cmp(atual, media, unidade="", inverter=False):
    if atual is None or media is None or not media:
        return "—"
    dif = atual - media
    pct = 100.0 * dif / media if media else 0
    bom = (dif < 0) if inverter else (dif > 0)
    classe = "up" if bom else "down"
    return f'<span class="{classe}">{dif:+.0f}{unidade} ({pct:+.0f}%)</span>'


def _cmp_pp(atual, media):
    """Comparação em pontos percentuais -- conversão não se compara em %
    de variação, se compara em p.p. Aqui o n do time é grande (centenas de
    contatos), então o número é estável o bastante pra mostrar."""
    if atual is None or media is None:
        return "—"
    dif = atual - media
    classe = "up" if dif >= 0 else "down"
    # Troca o separador decimal só no número: aplicar replace na string
    # inteira transformaria "p.p." em "p,p,".
    numero = f"{dif:+.1f}".replace(".", ",")
    return f'<span class="{classe}">{numero} p.p.</span>'


def montar_html_diario(dados):
    d, cmp_, var, fila, chuva = (dados["dia"], dados["comparacao"], dados["variacoes"],
                                 dados["fila"], dados["chuva"])
    n = dados["numeros"]
    conv = f'{n["conversao"]:.1f}%'.replace(".", ",") if n["conversao"] is not None else "—"

    if cmp_:
        linha_cmp = (f'<p class="cmp">Contra a média das últimas {cmp_["n"]} '
                     f'{["segundas","terças","quartas","quintas","sextas","sábados","domingos"][d.weekday()]}: '
                     f'atividades {_cmp(n["atividades"], cmp_["atividades"])} · '
                     f'contatos {_cmp(n["contatos"], cmp_["contatos"])} · '
                     f'agendados {_cmp(n["agendados"], cmp_["agendados"])} · '
                     f'conversão {_cmp_pp(n["conversao"], cmp_["conversao"])}</p>')
    else:
        linha_cmp = ""

    if var:
        linhas = "".join(
            f'<tr><td>{v["pessoa"].title()}'
            + (f'<br><span style="font-size:11px;color:#6b707b">{v["contexto"]}</span>' if v["contexto"] else "")
            + f'</td><td class="r">{v["hoje"]}</td>'
            f'<td class="r">{v["normal"]:.0f}</td>'
            f'<td class="r"><span class="{"up" if v["sentido"]=="acima" else "down"}">'
            f'{v["hoje"] - v["normal"]:+.0f}</span></td></tr>'
            for v in var)
        bloco_var = (f'<table><tr><th>Pessoa</th><th class="r">Hoje</th>'
                     f'<th class="r">Normal</th><th class="r">Dif.</th></tr>{linhas}</table>'
                     f'<p class="nota">"Normal" é a mediana dos últimos {DIAS_HISTORICO} dias úteis '
                     f'da própria pessoa. Só entra quem passou de {LIMIAR_DESVIO} desvios do próprio padrão. '
                     f'Quem estava de férias ou fora de escala e não produziu não aparece aqui.</p>')
    else:
        bloco_var = '<p class="ok">Nenhuma variação relevante hoje.</p>'

    if chuva["cidades"]:
        linhas_ch = "".join(
            f'<tr><td>{c["cidade"].title()}</td><td class="r">{c["chance"]}%</td>'
            f'<td class="r">{c["agendas"] or "—"}</td></tr>'
            for c in chuva["cidades"][:10])
        com_agenda = sum(c["agendas"] for c in chuva["cidades"])
        bloco_chuva = (
            f'<table class="chuva"><tr><th>Cidade</th><th class="r">Chuva</th>'
            f'<th class="r">Agendas</th></tr>{linhas_ch}</table>'
            f'<p class="nota">Em dia de chuva alta a execução cai cerca de '
            f'{PERDA_ESPERADA_CHUVA_PP} pontos — e metade disso não aparece rotulado como chuva, '
            f'vem como agendamento, movimentação de agenda e tubulação. '
            f'{"São " + str(com_agenda) + " agendas marcadas nessas cidades." if com_agenda else ""}</p>')
    else:
        bloco_chuva = '<p class="ok">Sem previsão de chuva alta nas cidades atendidas.</p>'

    fila_txt = (f'Pendentes {fila["pendentes"]:,} · Enfileirados {fila["enfileirados"]:,} · '
                f'Em andamento {fila["em_andamento"]:,}').replace(",", ".")
    if fila["backlog"] is not None:
        fila_txt += f' · Backlog {fila["backlog"]:,}'.replace(",", ".")

    # Dia sem nenhuma atividade: domingo, feriado, ou a tarefa rodou antes da
    # primeira tabulação do dia. Apontar desvio aqui produziria uma lista com
    # o time inteiro "muito abaixo do normal" -- alarme falso puro. Melhor
    # dizer em uma linha que não houve movimento e manter fila e chuva, que
    # continuam valendo.
    if not n["atividades"]:
        return f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<style>{ESTILO}</style></head><body><div class="wrap">
<div class="card">
  <h1>CRM RSI — {d.strftime('%d/%m/%Y')}</h1>
  <p class="sub">Sem atividade registrada neste dia.</p>
  <p style="margin:0;font-size:14px">{fila_txt}</p>
</div>
<div class="card"><h2>Chuva prevista — {chuva["data"].strftime('%d/%m')}</h2>{bloco_chuva}</div>
</div></body></html>"""

    return f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<style>{ESTILO}</style></head><body><div class="wrap">
<div class="card">
  <h1>CRM RSI — {d.strftime('%d/%m/%Y')}</h1>
  <p class="sub">{(str(len(var)) + " variações relevantes") if len(var) != 1 else "1 variação relevante"} · {n["pessoas"]} pessoas ativas</p>
  <div class="num"><b>{n["atividades"]}</b><span>atividades</span></div>
  <div class="num"><b>{n["contatos"]}</b><span>contatos</span></div>
  <div class="num"><b>{n["agendados"]}</b><span>agendados</span></div>
  <div class="num"><b>{conv}</b><span>conversão</span></div>
  {linha_cmp}
</div>
<div class="card"><h2>Variações relevantes</h2>{bloco_var}</div>
<div class="card"><h2>Chuva prevista — {chuva["data"].strftime('%d/%m')}</h2>{bloco_chuva}</div>
<div class="card"><h2>Fila</h2><p style="margin:0;font-size:14px">{fila_txt}</p></div>
<p class="nota" style="text-align:center">Conversão por pessoa sai no relatório semanal —
num único dia ela oscila demais pra significar alguma coisa.</p>
</div></body></html>"""


def montar_diario(conn, dia):
    numeros = numeros_do_dia(conn, dia)
    # Dia parado: não calcula desvio nenhum. Todo mundo apareceria como
    # "muito abaixo", e o assunto do e-mail viria com uma dúzia de pontos
    # de atenção que não existem.
    parado = not numeros["atividades"]
    return {
        "dia": dia,
        "numeros": numeros,
        "comparacao": None if parado else media_mesmo_dia_semana(conn, dia),
        "variacoes": [] if parado else variacoes_de_volume(conn, dia),
        "fila": situacao_da_fila(conn),
        "chuva": risco_de_chuva(conn, dia),
    }


# ---------------------------------------------------------------------------
# SEMANAL
# ---------------------------------------------------------------------------
# O semanal responde o que o diário não pode: o que é desempenho de gente, o
# que é característica de cidade, e o que é efeito de configuração nossa.
#
# TRÊS DECISÕES DE DESENHO QUE VALEM EXPLICAÇÃO (medidas em 2026-10-19 sobre
# 4 semanas de dados reais):
#
# 1) CONVERSÃO POR PESSOA É AJUSTADA PELO MIX. A conversão depende quase toda
#    de qual fila a pessoa trabalhou: primeira abordagem converte 7,8%,
#    retorno de follow-up converte 65,8%. Quem faz mais abordagem fria tem
#    conversão baixa por construção. A correlação entre a conversão crua e a
#    conversão que o mix sozinho já explica é 0,62 -- 38% do ranking é só a
#    fila. Ranquear pelo número cru premia quem pega retorno e pune quem
#    abre contato. Então a coluna que ordena é a DIFERENÇA contra o esperado
#    pelo próprio mix.
#
# 2) QUEBRA NÃO ENTRA NO EIXO DAS PESSOAS. Testadas as 28 pessoas contra a
#    base do time (21,6%), só uma passou de 2 desvios -- e para o lado bom.
#    Com 28 comparações o esperado por azar é 1,3. Não há sinal: a quebra de
#    uma agenda não depende de quem agendou. Por cidade ela discrimina forte
#    (Araraquara 39%, Americana 35%, Araçatuba 3,4%), e é lá que ela fica.
#
# 3) QUEBRA TEM ATRASO DE IMPORTAÇÃO. A quebras_agenda é alimentada por
#    importação manual: em out/2026 as cargas foram 13/10 (até 30/09) e
#    19/10 (até 15/10). Na segunda-feira a semana que acabou ainda não tem
#    desfecho nenhum. Por isso os blocos de quebra usam a última semana com
#    cobertura suficiente, e o relatório diz qual semana é -- em vez de
#    mostrar 0% de quebra e passar a impressão de semana perfeita.
MIN_CONTATOS_PESSOA = 25     # abaixo disso a conversão da semana não diz nada
MIN_AGENDAS_CIDADE = 20      # idem para a quebra da cidade
MIN_CONTATOS_CIDADE = 30     # idem para a conversão da cidade
COBERTURA_MINIMA_QUEBRA = 0.70
SEMANAS_TENDENCIA = 6
SEMANAS_JANELA_CIDADE = 4    # quebra de cidade muda devagar; 1 semana é ruído
MIN_CONTATOS_BASE_TENTATIVA = 30  # abaixo disso usa a base de 4 semanas

NOME_TENTATIVA = {1: "1ª abordagem", 2: "retorno", 3: "retorno tardio"}

# Retentativas de quem NÃO atendeu: a tentativa anterior do mesmo contrato
# terminou em Improdutivo. É só nesse caso que "mudar o horário" quer dizer
# alguma coisa -- em quem já atendeu, o horário da próxima ligação é o que o
# cliente pediu, não escolha nossa.
#
# De propósito NÃO se mede aqui o intervalo entre tentativas: ele cresce
# sozinho conforme a base envelhece (em agosto era impossível ter intervalo
# de 25 dias porque o sistema tinha acabado de subir), e viraria um
# "follow-up piorando" que o dado não sustenta.
SQL_RETENTATIVAS = """
WITH t AS (
  SELECT contrato, data_inicio, tabulacao,
         LAG(data_inicio) OVER (PARTITION BY contrato ORDER BY data_inicio) ant,
         LAG(tabulacao)   OVER (PARTITION BY contrato ORDER BY data_inicio) ant_tab,
         CASE WHEN ABS(CAST(strftime('%H', data_inicio) AS INT)
                     - CAST(strftime('%H', LAG(data_inicio) OVER
                            (PARTITION BY contrato ORDER BY data_inicio)) AS INT)) >= 2
              THEN 1 ELSE 0 END trocou_faixa
  FROM atividades
  WHERE status='CONCLUIDO' AND data_inicio IS NOT NULL
)
"""
SQL_SO_RETENTATIVA = "ant IS NOT NULL AND ant_tab='Improdutivo'"


def semana_de(dia):
    """(segunda, domingo) da semana que contém `dia`."""
    ini = dia - timedelta(days=dia.weekday())
    return ini, ini + timedelta(days=6)


def semana_fechada_antes(dia):
    """A última semana seg-dom inteiramente concluída antes de `dia`."""
    ini_atual, _ = semana_de(dia)
    fim = ini_atual - timedelta(days=1)
    return semana_de(fim)


def _taxas_por_tentativa(conn, ini, fim):
    """Conversão base de cada tipo de fila no período. É o denominador justo:
    comparar alguém de abordagem fria com alguém de retorno sem isso é
    comparar coisas diferentes."""
    marcadores = ",".join("?" * len(TABULACOES_SEM_CONTATO))
    linhas = conn.execute(f"""
        SELECT tentativa,
               COALESCE(SUM(CASE WHEN tabulacao NOT IN ({marcadores}) THEN 1 ELSE 0 END), 0) ct,
               COALESCE(SUM(CASE WHEN tabulacao='Agendado' THEN 1 ELSE 0 END), 0) ag
        FROM atividades
        WHERE status='CONCLUIDO' AND date(data_fim) BETWEEN ? AND ?
        GROUP BY tentativa
    """, list(TABULACOES_SEM_CONTATO) + [ini.isoformat(), fim.isoformat()]).fetchall()
    return {l["tentativa"]: {"ct": l["ct"], "ag": l["ag"]} for l in linhas}


def desempenho_por_pessoa(conn, ini, fim):
    """Conversão de cada pessoa contra o que o mix de filas dela já explica.

    O `z` sai da variância binomial do mix (soma por fila), não de um desvio
    histórico: com 25-140 contatos por semana, uma diferença de 10 pontos
    pode ser sorte. Só o que passa de 2 desvios é apontado como diferença
    real -- o resto é mostrado sem destaque, para o número existir sem virar
    acusação.
    """
    marcadores = ",".join("?" * len(TABULACOES_SEM_CONTATO))
    linhas = conn.execute(f"""
        SELECT operador, tentativa,
               COALESCE(SUM(CASE WHEN tabulacao NOT IN ({marcadores}) THEN 1 ELSE 0 END), 0) ct,
               COALESCE(SUM(CASE WHEN tabulacao='Agendado' THEN 1 ELSE 0 END), 0) ag
        FROM atividades
        WHERE status='CONCLUIDO' AND date(data_fim) BETWEEN ? AND ?
        GROUP BY operador, tentativa
    """, list(TABULACOES_SEM_CONTATO) + [ini.isoformat(), fim.isoformat()]).fetchall()
    if not linhas:
        return []

    base_semana = _taxas_por_tentativa(conn, ini, fim)
    # Fila com pouco volume na semana usa a base de 4 semanas, que é estável.
    base_longa = _taxas_por_tentativa(conn, ini - timedelta(days=21), fim)
    taxa = {}
    for tent in set(list(base_semana) + list(base_longa)):
        fonte = base_semana.get(tent, {"ct": 0, "ag": 0})
        if fonte["ct"] < MIN_CONTATOS_BASE_TENTATIVA:
            fonte = base_longa.get(tent, fonte)
        taxa[tent] = (fonte["ag"] / fonte["ct"]) if fonte["ct"] else 0.0

    por_pessoa = defaultdict(dict)
    for l in linhas:
        por_pessoa[l["operador"]][l["tentativa"]] = (l["ct"], l["ag"])

    resultado = []
    for pessoa, filas in por_pessoa.items():
        total = sum(ct for ct, _ in filas.values())
        agendados = sum(ag for _, ag in filas.values())
        if total < MIN_CONTATOS_PESSOA:
            continue
        esperados = sum(ct * taxa.get(t, 0.0) for t, (ct, _) in filas.items())
        variancia = sum(ct * taxa.get(t, 0.0) * (1 - taxa.get(t, 0.0))
                        for t, (ct, _) in filas.items())
        erro_pp = (100.0 * (variancia ** 0.5) / total) if total else 0.0
        conv = 100.0 * agendados / total
        esperado = 100.0 * esperados / total
        dif = conv - esperado
        resultado.append({
            "pessoa": pessoa, "contatos": total, "agendados": agendados,
            "conversao": conv, "esperado": esperado, "diferenca": dif,
            "erro_pp": erro_pp,
            "z": (dif / erro_pp) if erro_pp else 0.0,
            # Fatia de abordagem fria: é o contexto que explica a conversão
            # baixa de quem abre contato.
            "pct_fria": 100.0 * filas.get(1, (0, 0))[0] / total,
        })
    resultado.sort(key=lambda r: -r["diferenca"])
    return resultado


def cobertura_quebra(conn, ini, fim):
    """Quanto das agendas da semana já tem desfecho importado."""
    linha = conn.execute("""
        SELECT COUNT(*) agendas,
               COALESCE(SUM(CASE WHEN q.id IS NOT NULL THEN 1 ELSE 0 END), 0) com_desfecho
        FROM atividades a
        LEFT JOIN quebras_agenda q
               ON q.contrato = a.contrato AND q.dat_agenda = a.data_agendamento
        WHERE a.tabulacao='Agendado' AND a.status='CONCLUIDO'
          AND a.data_agendamento BETWEEN ? AND ?
    """, (ini.isoformat(), fim.isoformat())).fetchone()
    agendas = linha["agendas"]
    return {
        "agendas": agendas, "com_desfecho": linha["com_desfecho"],
        "cobertura": (linha["com_desfecho"] / agendas) if agendas else 0.0,
    }


def ultima_semana_com_desfecho(conn, antes_de, limite=8):
    """Anda para trás até achar uma semana cujas agendas já tenham desfecho.

    Sem isso o relatório de segunda-feira mostraria 0% de quebra na semana
    que acabou -- não porque foi perfeita, mas porque o arquivo de quebra
    ainda não foi importado.
    """
    ini, fim = semana_fechada_antes(antes_de)
    for _ in range(limite):
        cob = cobertura_quebra(conn, ini, fim)
        if cob["agendas"] and cob["cobertura"] >= COBERTURA_MINIMA_QUEBRA:
            return ini, fim, cob
        ini, fim = ini - timedelta(days=7), fim - timedelta(days=7)
    return None, None, None


def _z_proporcao(parte, total, base):
    if not total or base in (0, 1):
        return 0.0
    erro = (base * (1 - base) / total) ** 0.5
    return ((parte / total) - base) / erro if erro else 0.0


def desfecho_por_cidade(conn, ini, fim):
    """Execuções e quebra por cidade, nas agendas que o time marcou.

    A cidade vem da quebras_agenda, não do mailing: as duas grafam diferente
    ("Campinas" x "CAMPINAS", com e sem acento) e usar a da própria tabela de
    desfecho dispensa normalizar nome na consulta.
    """
    linhas = conn.execute("""
        SELECT q.cidade,
               COALESCE(SUM(CASE WHEN q.tipo_tratamento='SEM QUEBRA DE AGENDA' THEN 1 ELSE 0 END), 0) executadas,
               COALESCE(SUM(CASE WHEN q.tipo_tratamento='COM QUEBRA DE AGENDA' THEN 1 ELSE 0 END), 0) quebradas
        FROM atividades a
        JOIN quebras_agenda q
          ON q.contrato = a.contrato AND q.dat_agenda = a.data_agendamento
        WHERE a.tabulacao='Agendado' AND a.status='CONCLUIDO'
          AND a.data_agendamento BETWEEN ? AND ?
        GROUP BY q.cidade
    """, (ini.isoformat(), fim.isoformat())).fetchall()

    total = sum(l["executadas"] + l["quebradas"] for l in linhas)
    quebras = sum(l["quebradas"] for l in linhas)
    base = (quebras / total) if total else 0.0

    cidades = []
    for l in linhas:
        n = l["executadas"] + l["quebradas"]
        if n < MIN_AGENDAS_CIDADE:
            continue
        cidades.append({
            "cidade": l["cidade"], "agendas": n, "executadas": l["executadas"],
            "quebra": 100.0 * l["quebradas"] / n,
            "z": _z_proporcao(l["quebradas"], n, base),
        })
    cidades.sort(key=lambda c: -c["executadas"])
    return {"base": 100.0 * base, "total": total, "cidades": cidades,
            "fora_do_corte": len(linhas) - len(cidades)}


def rendimento_liquido(conn, ini_sem, fim_sem, desfecho):
    """Conversão x (1 - quebra) por cidade.

    A conversão sozinha engana: cidade que converte muito e quebra muito
    entrega menos que cidade mediana nas duas pontas. Esse é o número que
    decide se uma cidade vale a fila.

    Os dois lados usam a mesma janela de 4 semanas, deslocada para onde o
    desfecho já existe. Numa semana só, cidade bloqueada aparece com 1 ou 2
    contatos e conversão de 0% ou 100% -- número que não significa nada e
    que, num relatório, vira decisão errada.
    """
    marcadores = ",".join("?" * len(TABULACOES_SEM_CONTATO))
    conv = {}
    for l in conn.execute(f"""
            SELECT m.cidade,
                   COALESCE(SUM(CASE WHEN a.tabulacao NOT IN ({marcadores}) THEN 1 ELSE 0 END), 0) ct,
                   COALESCE(SUM(CASE WHEN a.tabulacao='Agendado' THEN 1 ELSE 0 END), 0) ag
            FROM atividades a JOIN mailing m ON m.id = a.id_mailing
            WHERE a.status='CONCLUIDO' AND date(a.data_fim) BETWEEN ? AND ?
            GROUP BY m.cidade
        """, list(TABULACOES_SEM_CONTATO) + [ini_sem.isoformat(), fim_sem.isoformat()]).fetchall():
        if l["ct"] >= MIN_CONTATOS_CIDADE:
            conv[_chave_cidade(l["cidade"])] = {
                "contatos": l["ct"], "conversao": 100.0 * l["ag"] / l["ct"]}

    bloqueadas = {_chave_cidade(r["cidade"]) for r in conn.execute(
        "SELECT cidade FROM cidades_bloqueio_distribuicao WHERE bloqueado=1").fetchall()}

    saida = []
    for c in (desfecho["cidades"] if desfecho else []):
        chave = _chave_cidade(c["cidade"])
        if chave not in conv:
            continue
        taxa = conv[chave]["conversao"]
        saida.append({
            "cidade": c["cidade"], "conversao": taxa, "quebra": c["quebra"],
            "liquido": taxa * (1 - c["quebra"] / 100.0),
            "contatos": conv[chave]["contatos"], "agendas": c["agendas"],
            "bloqueada": chave in bloqueadas,
        })
    saida.sort(key=lambda c: -c["liquido"])
    return saida


def _chave_cidade(nome):
    """Normaliza para cruzar mailing.cidade ('SAO JOSE DOS CAMPOS') com
    quebras_agenda.cidade ('São José dos Campos')."""
    import unicodedata
    if not nome:
        return ""
    texto = unicodedata.normalize("NFKD", str(nome))
    texto = "".join(ch for ch in texto if not unicodedata.combining(ch))
    return texto.upper().replace("'", " ").replace("-", " ").strip()


def funil_da_semana(conn, ini, fim):
    """Onde o contato vira agenda -- e onde ele morre.

    A separação por tentativa não é detalhe técnico: 1 é abordagem fria,
    2 e 3 são retorno de follow-up. São dois negócios diferentes dentro do
    mesmo time, com conversão quase dez vezes diferente.
    """
    marcadores = ",".join("?" * len(TABULACOES_SEM_CONTATO))
    l = conn.execute(f"""
        SELECT
          COALESCE(SUM(CASE WHEN tentativa=1 AND tabulacao NOT IN ({marcadores}) THEN 1 ELSE 0 END), 0) fria,
          COALESCE(SUM(CASE WHEN tentativa=1 AND tabulacao='Agendado' THEN 1 ELSE 0 END), 0) fria_ag,
          COALESCE(SUM(CASE WHEN tentativa>1 AND tabulacao NOT IN ({marcadores}) THEN 1 ELSE 0 END), 0) retorno,
          COALESCE(SUM(CASE WHEN tentativa>1 AND tabulacao='Agendado' THEN 1 ELSE 0 END), 0) retorno_ag,
          COALESCE(SUM(CASE WHEN tabulacao='Improdutivo' THEN 1 ELSE 0 END), 0) improdutivo,
          COALESCE(SUM(CASE WHEN tabulacao LIKE 'Recusado%' THEN 1 ELSE 0 END), 0) recusado,
          COALESCE(SUM(CASE WHEN tabulacao='Cliente Já Migrado' THEN 1 ELSE 0 END), 0) ja_migrado
        FROM atividades
        WHERE status='CONCLUIDO' AND date(data_fim) BETWEEN ? AND ?
    """, list(TABULACOES_SEM_CONTATO) * 2 + [ini.isoformat(), fim.isoformat()]).fetchone()
    d = dict(l)
    d["conv_fria"] = (100.0 * d["fria_ag"] / d["fria"]) if d["fria"] else None
    d["conv_retorno"] = (100.0 * d["retorno_ag"] / d["retorno"]) if d["retorno"] else None
    total_ag = d["fria_ag"] + d["retorno_ag"]
    d["agendados"] = total_ag
    d["pct_do_retorno"] = (100.0 * d["retorno_ag"] / total_ag) if total_ag else None
    return d


def indicadores_de_processo(conn, fim, semanas=SEMANAS_TENDENCIA):
    """Semana a semana, os indicadores que uma mudança de configuração move.

    É o que permite responder "o prazo de follow-up de 30h melhorou o quê?".
    O CRM ainda não guarda histórico de alteração de configuração, então o
    efeito é lido pelo indicador, não pela data da mudança -- se a série vira
    numa semana, foi ali. (Vale gravar esse histórico, do mesmo jeito que o
    bloqueio de cidade passou a ser gravado.)

    ALTERNÂNCIA é a fatia de retentativas feitas em faixa de horário
    diferente da tentativa anterior (2h ou mais de diferença). Ligar sempre
    no mesmo horário para quem não atende é desperdiçar tentativa.

    A coluna ESPERADO separa mix de desempenho. A conversão do time cai
    sozinha quando a semana tem mais abordagem fria, sem ninguém ter
    piorado. Sem essa coluna, toda semana de carga nova de mailing parece
    queda de produtividade.
    """
    inicio = fim - timedelta(days=7 * semanas - 1)
    marcadores = ",".join("?" * len(TABULACOES_SEM_CONTATO))
    conversao = {r["sem"]: r for r in conn.execute(f"""
        SELECT strftime('%Y-%W', data_fim) sem,
               COALESCE(SUM(CASE WHEN tabulacao NOT IN ({marcadores}) THEN 1 ELSE 0 END), 0) ct,
               COALESCE(SUM(CASE WHEN tabulacao='Agendado' THEN 1 ELSE 0 END), 0) ag,
               COALESCE(SUM(CASE WHEN tentativa=1 AND tabulacao NOT IN ({marcadores}) THEN 1 ELSE 0 END), 0) fria
        FROM atividades
        WHERE status='CONCLUIDO' AND date(data_fim) BETWEEN ? AND ?
        GROUP BY 1
    """, list(TABULACOES_SEM_CONTATO) * 2 + [inicio.isoformat(), fim.isoformat()]).fetchall()}

    # Taxas de referência de cada fila no período inteiro da série: é contra
    # elas que se mede quanto da variação semanal é só mudança de mix.
    ref = _taxas_por_tentativa(conn, inicio, fim)
    ct_fria = ref.get(1, {"ct": 0, "ag": 0})
    taxa_fria = (ct_fria["ag"] / ct_fria["ct"]) if ct_fria["ct"] else 0.0
    ag_ret = sum(v["ag"] for t, v in ref.items() if t != 1)
    ct_ret = sum(v["ct"] for t, v in ref.items() if t != 1)
    taxa_ret = (ag_ret / ct_ret) if ct_ret else 0.0

    alternancia = {r["sem"]: r for r in conn.execute(f"""
        {SQL_RETENTATIVAS}
        SELECT strftime('%Y-%W', data_inicio) sem, COUNT(*) pares,
               COALESCE(SUM(trocou_faixa), 0) trocou
        FROM t
        WHERE {SQL_SO_RETENTATIVA} AND date(data_inicio) BETWEEN ? AND ?
        GROUP BY 1
    """, (inicio.isoformat(), fim.isoformat())).fetchall()}

    linhas = []
    d = inicio - timedelta(days=inicio.weekday())
    while d <= fim:
        chave = d.strftime("%Y-%W")
        c = conversao.get(chave)
        a = alternancia.get(chave)
        pares = a["pares"] if a else 0
        conv = esperado = None
        if c and c["ct"]:
            conv = 100.0 * c["ag"] / c["ct"]
            fria, retorno = c["fria"], c["ct"] - c["fria"]
            esperado = 100.0 * (fria * taxa_fria + retorno * taxa_ret) / c["ct"]
        linhas.append({
            "inicio": d,
            "contatos": c["ct"] if c else 0,
            "conversao": conv,
            "esperado": esperado,
            "diferenca": (conv - esperado) if conv is not None else None,
            "pct_fria": (100.0 * c["fria"] / c["ct"]) if c and c["ct"] else None,
            "alternancia": (100.0 * a["trocou"] / pares) if pares else None,
            "retentativas": pares,
        })
        d += timedelta(days=7)
    return linhas


def efeito_da_alternancia(conn, ini, fim):
    """Trocar a faixa de horário na retentativa aumenta a chance de falar com
    o cliente? Mede nas duas pontas, no mesmo período.

    Medido em 2026-10-19 sobre 4 semanas: 83,5% contra 68,8%, diferença de
    14,7 pontos com 6,5 desvios. É o que transforma alternância de impressão
    em indicador com retorno conhecido.
    """
    linhas = conn.execute(f"""
        {SQL_RETENTATIVAS}
        SELECT trocou_faixa, COUNT(*) n,
               COALESCE(SUM(CASE WHEN tabulacao<>'Improdutivo' THEN 1 ELSE 0 END), 0) falou
        FROM t
        WHERE {SQL_SO_RETENTATIVA} AND date(data_inicio) BETWEEN ? AND ?
        GROUP BY trocou_faixa
    """, (ini.isoformat(), fim.isoformat())).fetchall()
    dados = {l["trocou_faixa"]: (l["n"], l["falou"]) for l in linhas}
    n_troca, f_troca = dados.get(1, (0, 0))
    n_mesmo, f_mesmo = dados.get(0, (0, 0))
    if n_troca < 30 or n_mesmo < 30:
        return None
    p_troca, p_mesmo = f_troca / n_troca, f_mesmo / n_mesmo
    erro = (p_troca * (1 - p_troca) / n_troca + p_mesmo * (1 - p_mesmo) / n_mesmo) ** 0.5
    return {
        "trocou": 100.0 * p_troca, "n_trocou": n_troca,
        "mesmo": 100.0 * p_mesmo, "n_mesmo": n_mesmo,
        "diferenca": 100.0 * (p_troca - p_mesmo),
        "z": ((p_troca - p_mesmo) / erro) if erro else 0.0,
    }


def bloqueios_no_periodo(conn, ini, fim):
    """O que mudou na distribuição por cidade na semana.

    Bloqueio é válvula de capacidade de execução na ponta -- técnico
    disponível na classe 15 --, não tratamento de quebra. Aqui ele aparece
    como contexto: mudou a fila, então mudou a comparação.
    """
    try:
        linhas = conn.execute("""
            SELECT cidade, bloqueado, bloqueado_antes, alterado_em, alterado_por, motivo
            FROM cidades_bloqueio_historico
            WHERE date(alterado_em) BETWEEN ? AND ?
              AND bloqueado_antes IS NOT NULL
            ORDER BY alterado_em
        """, (ini.isoformat(), fim.isoformat())).fetchall()
    except sqlite3.OperationalError:
        # Tabela só existe depois do app.py novo carregar uma vez.
        return None
    return [dict(l) for l in linhas]


def montar_semanal(conn, ini, fim):
    # Os blocos de cidade usam 4 semanas terminando na última semana com
    # desfecho importado -- numa semana só, cidade pouco movimentada entra
    # com 2 agendas e 50% de quebra.
    ini_q, fim_q, cob = ultima_semana_com_desfecho(conn, ini + timedelta(days=7))
    if ini_q:
        ini_j = ini_q - timedelta(days=7 * (SEMANAS_JANELA_CIDADE - 1))
        desfecho = desfecho_por_cidade(conn, ini_j, fim_q)
        liquido = rendimento_liquido(conn, ini_j, fim_q, desfecho)
    else:
        ini_j = desfecho = None
        liquido = []
    return {
        "inicio": ini, "fim": fim,
        "numeros": _totais_periodo(conn, ini, fim),
        "anterior": _totais_periodo(conn, ini - timedelta(days=7), fim - timedelta(days=7)),
        "pessoas": desempenho_por_pessoa(conn, ini, fim),
        "funil": funil_da_semana(conn, ini, fim),
        "cobertura_semana": cobertura_quebra(conn, ini, fim),
        "semana_quebra": (ini_q, fim_q) if ini_q else None,
        "janela_cidade": (ini_j, fim_q) if ini_q else None,
        "cobertura_quebra": cob,
        "desfecho": desfecho,
        "liquido": liquido,
        "processo": indicadores_de_processo(conn, fim),
        "alternancia": efeito_da_alternancia(
            conn, fim - timedelta(days=7 * SEMANAS_JANELA_CIDADE - 1), fim),
        "bloqueios": bloqueios_no_periodo(conn, ini, fim),
    }


def _totais_periodo(conn, ini, fim):
    marcadores = ",".join("?" * len(TABULACOES_SEM_CONTATO))
    l = conn.execute(f"""
        SELECT COUNT(*) atividades,
               COALESCE(SUM(CASE WHEN tabulacao NOT IN ({marcadores}) THEN 1 ELSE 0 END), 0) contatos,
               COALESCE(SUM(CASE WHEN tabulacao='Agendado' THEN 1 ELSE 0 END), 0) agendados,
               COUNT(DISTINCT operador) pessoas
        FROM atividades
        WHERE status='CONCLUIDO' AND date(data_fim) BETWEEN ? AND ?
    """, list(TABULACOES_SEM_CONTATO) + [ini.isoformat(), fim.isoformat()]).fetchone()
    d = dict(l)
    d["conversao"] = (100.0 * d["agendados"] / d["contatos"]) if d["contatos"] else None
    return d


# ---------------------------------------------------------------------------
# HTML do semanal
# ---------------------------------------------------------------------------
# O quadrante é desenhado como tabela 2x2, não como gráfico de pontos. Dois
# motivos: o Gmail remove `position:absolute`, que é o que um gráfico de
# dispersão em HTML precisa; e com 25 nomes os pontos viram uma nuvem
# ilegível. A grade nomeada diz a mesma coisa e sobrevive a qualquer cliente
# de e-mail.
Z_RELEVANTE = 2.0


def _grade_quadrante(titulo_x, titulo_y, celulas, vazio="ninguém nesta faixa"):
    """celulas: 4 dicts (titulo, classe, itens, discretos) na ordem
    cima-esquerda, cima-direita, baixo-esquerda, baixo-direita.

    `itens` são os casos cuja diferença é grande o bastante para não ser
    sorte; `discretos` são os que caíram no quadrante mas estão dentro da
    margem -- aparecem em linha única e apagada, para o quadrante não
    apontar o dedo para ruído."""
    def cel(c):
        partes = [f'<div class="item">{i}</div>' for i in c["itens"]]
        if c.get("discretos"):
            partes.append('<div class="vazio">dentro da margem: '
                          + ", ".join(c["discretos"]) + "</div>")
        return (f'<td class="{c["classe"]}"><p class="tit">{c["titulo"]}</p>'
                + ("".join(partes) or f'<div class="vazio">{vazio}</div>') + "</td>")
    return (f'<p class="eixos">Horizontal: {titulo_x} · Vertical: {titulo_y}</p>'
            f'<table class="quad"><tr>{cel(celulas[0])}{cel(celulas[1])}</tr>'
            f'<tr>{cel(celulas[2])}{cel(celulas[3])}</tr></table>')


def _num_br(valor, casas=1, sufixo=""):
    if valor is None:
        return "—"
    return f"{valor:.{casas}f}".replace(".", ",") + sufixo


def leitura_da_semana(d):
    """As frases do topo. Cada uma só existe se o dado a sustenta -- nada de
    frase de encher linguiça quando a semana não teve nada demais.

    A ordem é a da pergunta que o leitor faz primeiro: o time rendeu?, de
    onde veio o resultado?, quem destoou?, onde está a perda?
    """
    frases = []
    n, ant = d["numeros"], d["anterior"]
    if not n["contatos"]:
        return ["Sem contatos registrados nesta semana — não há o que ler."]
    processo = [l for l in d["processo"] if l["conversao"] is not None]

    # 1. O time rendeu acima ou abaixo do que o mix da semana explicava?
    if processo:
        atual = processo[-1]
        if atual["diferenca"] is not None:
            lado = "acima" if atual["diferenca"] >= 0 else "abaixo"
            frase = (f'A conversão do time foi <b>{_num_br(atual["conversao"], 1, "%")}</b>, '
                     f'{_num_br(abs(atual["diferenca"]), 1)} p.p. {lado} do que o mix da '
                     f'semana explicava ({_num_br(atual["pct_fria"], 0, "%")} de primeira '
                     f'abordagem).')
            if len(processo) >= 2 and processo[-2]["diferenca"] is not None:
                delta = atual["diferenca"] - processo[-2]["diferenca"]
                if abs(delta) >= 3:
                    frase += (f' Contra a semana anterior isso é uma virada de '
                              f'{_num_br(delta, 1)} p.p. já descontado o mix.')
            frases.append(frase)
    elif n["conversao"] is not None:
        frases.append(f'Conversão do time em {_num_br(n["conversao"], 1, "%")}.')

    # 2. De onde veio o volume.
    f = d["funil"]
    if f["pct_do_retorno"] is not None:
        frases.append(
            f'<b>{_num_br(f["pct_do_retorno"], 0, "%")} das {f["agendados"]} agendas '
            f'vieram do retorno de follow-up</b> — a primeira abordagem converteu '
            f'{_num_br(f["conv_fria"], 1, "%")} contra {_num_br(f["conv_retorno"], 1, "%")} '
            f'do retorno.')

    # 3. Quem destoou -- só quem passou da margem.
    destaques = [p for p in d["pessoas"] if abs(p["z"]) >= Z_RELEVANTE]
    acima = [p for p in destaques if p["diferenca"] > 0]
    abaixo = [p for p in destaques if p["diferenca"] < 0]
    if acima or abaixo:
        partes = []
        if acima:
            melhor = max(acima, key=lambda p: p["diferenca"])
            partes.append(f'<b>{melhor["pessoa"].title()}</b> '
                          f'{_num_br(melhor["diferenca"], 1)} p.p. acima do esperado pelo '
                          f'mix dela'
                          + (f' (e mais {len(acima) - 1} acima)' if len(acima) > 1 else ''))
        if abaixo:
            pior = min(abaixo, key=lambda p: p["diferenca"])
            partes.append(f'<b>{pior["pessoa"].title()}</b> '
                          f'{_num_br(pior["diferenca"], 1)} p.p. abaixo'
                          + (f' (e mais {len(abaixo) - 1} abaixo)' if len(abaixo) > 1 else ''))
        frases.append("Fora da margem estatística: " + "; ".join(partes) + ".")
    elif d["pessoas"]:
        frases.append(f'Nenhuma das {len(d["pessoas"])} pessoas no corte ficou fora da '
                      f'margem estatística — a diferença entre elas, nesta semana, é ruído.')

    # 4. Onde a execução se perde.
    if d["desfecho"] and d["desfecho"]["cidades"]:
        criticas = [c for c in d["desfecho"]["cidades"] if c["z"] >= Z_RELEVANTE]
        if criticas:
            pior = max(criticas, key=lambda c: c["quebra"])
            perdidas = round(pior["agendas"] * pior["quebra"] / 100)
            frases.append(
                f'<b>{pior["cidade"]}</b> quebra {_num_br(pior["quebra"], 1, "%")} contra '
                f'{_num_br(d["desfecho"]["base"], 1, "%")} da base — {perdidas} agendas '
                f'perdidas no período. Quebra é frente de qualidade e infraestrutura, '
                f'não motivo de bloqueio.')

    # 5. Alternância, quando há evidência.
    a = d.get("alternancia")
    if a and abs(a["z"]) >= Z_RELEVANTE:
        frases.append(
            f'Na retentativa de quem não atendeu, trocar a faixa de horário fez falar com '
            f'o cliente em <b>{_num_br(a["trocou"], 1, "%")}</b> contra '
            f'{_num_br(a["mesmo"], 1, "%")} de quem repetiu o horário '
            f'({_num_br(a["diferenca"], 1)} p.p., {a["n_trocou"] + a["n_mesmo"]} '
            f'retentativas).')

    return frases


def _bloco_pessoas(pessoas):
    if not pessoas:
        return ('<p class="ok">Sem ninguém com volume suficiente na semana '
                f'(mínimo de {MIN_CONTATOS_PESSOA} contatos).</p>')

    linhas = "".join(
        f'<tr><td>{p["pessoa"].title()}'
        f'{"<span class=\"tag\">" + _num_br(p["pct_fria"], 0, "% fria") + "</span>" if p["pct_fria"] >= 60 else ""}'
        f'</td><td class="r">{p["contatos"]}</td>'
        f'<td class="r">{_num_br(p["conversao"], 1, "%")}</td>'
        f'<td class="r">{_num_br(p["esperado"], 1, "%")}</td>'
        f'<td class="r"><span class="{"up" if p["diferenca"] >= 0 else "down"}">'
        f'{_num_br(p["diferenca"], 1)}</span>'
        f'{" <b>*</b>" if abs(p["z"]) >= Z_RELEVANTE else ""}</td></tr>'
        for p in pessoas)

    destaques = [p for p in pessoas if abs(p["z"]) >= Z_RELEVANTE]
    return (
        f'<table><tr><th>Pessoa</th><th class="r">Contatos</th>'
        f'<th class="r">Conversão</th><th class="r">Esperado</th>'
        f'<th class="r">Dif. (p.p.)</th></tr>{linhas}</table>'
        f'<p class="nota"><b>Esperado</b> é a conversão que o mix de filas da própria '
        f'pessoa já explica: 1ª abordagem e retorno de follow-up convertem em patamares '
        f'muito diferentes, então quem abre contato tem conversão baixa por construção. '
        f'A coluna que ordena é a diferença. O <b>*</b> marca as {len(destaques)} '
        f'diferença(s) grande(s) o bastante para não ser sorte da semana — as outras '
        f'estão dentro da margem e não devem ser tratadas como desempenho.</p>')


def _quadrante_pessoas(pessoas):
    if len(pessoas) < 4:
        return ""
    corte = statistics.median(p["agendados"] for p in pessoas)

    def nome(p):
        partes = p["pessoa"].title().split()
        return f'{partes[0]} {partes[-1][:1]}.' if len(partes) > 1 else partes[0]

    def celula(titulo, classe, filtro):
        dentro = [p for p in pessoas if filtro(p)]
        relevantes = [p for p in dentro if abs(p["z"]) >= Z_RELEVANTE]
        margem = [p for p in dentro if abs(p["z"]) < Z_RELEVANTE]
        # Cobrar de quem está dentro da margem é cobrar ruído: a cor de
        # alerta só aparece se há alguém realmente fora dela.
        if classe == "q-atencao" and not relevantes:
            classe = "q-neutro"
        return {
            "titulo": titulo, "classe": classe,
            "itens": [f'<b>{nome(p)}</b> — {p["agendados"]} agendas, '
                      f'{_num_br(p["diferenca"], 1)} p.p.' for p in relevantes],
            "discretos": [nome(p) for p in margem],
        }

    return _grade_quadrante(
        "agendas da semana", "conversão contra o esperado pelo mix",
        [
            celula("Referência", "q-bom",
                   lambda p: p["diferenca"] >= 0 and p["agendados"] >= corte),
            celula("Converte bem, volume baixo", "q-neutro",
                   lambda p: p["diferenca"] >= 0 and p["agendados"] < corte),
            celula("Volume com conversão abaixo", "q-neutro",
                   lambda p: p["diferenca"] < 0 and p["agendados"] >= corte),
            celula("Prioridade de apoio", "q-atencao",
                   lambda p: p["diferenca"] < 0 and p["agendados"] < corte),
        ])


def _quadrante_cidades(desfecho):
    cidades = desfecho["cidades"]
    if len(cidades) < 4:
        return ""
    corte = statistics.median(c["executadas"] for c in cidades)
    base = desfecho["base"]

    def celula(titulo, classe, filtro):
        dentro = [c for c in cidades if filtro(c)]
        relevantes = [c for c in dentro if abs(c["z"]) >= Z_RELEVANTE]
        margem = [c for c in dentro if abs(c["z"]) < Z_RELEVANTE]
        if classe == "q-atencao" and not relevantes:
            classe = "q-neutro"
        return {
            "titulo": titulo, "classe": classe,
            "itens": [f'<b>{c["cidade"]}</b> — {c["executadas"]} execuções, '
                      f'{_num_br(c["quebra"], 1, "%")} de quebra' for c in relevantes],
            "discretos": [c["cidade"] for c in margem],
        }

    return _grade_quadrante(
        "execuções no período", f'quebra contra a base de {_num_br(base, 1, "%")}',
        [
            celula("Referência", "q-bom",
                   lambda c: c["quebra"] <= base and c["executadas"] >= corte),
            celula("Consistente, volume baixo", "q-neutro",
                   lambda c: c["quebra"] <= base and c["executadas"] < corte),
            celula("Volume com perda", "q-atencao",
                   lambda c: c["quebra"] > base and c["executadas"] >= corte),
            celula("Prioridade de apoio", "q-atencao",
                   lambda c: c["quebra"] > base and c["executadas"] < corte),
        ],
        vazio="nenhuma cidade nesta faixa")


def _bloco_liquido(liquido, janela=None):
    if not liquido:
        return ('<p class="ok">Sem cidade com volume suficiente nas duas pontas '
                f'(mínimo de {MIN_CONTATOS_CIDADE} contatos e {MIN_AGENDAS_CIDADE} '
                'agendas no período).</p>')
    cabecalho = ""
    if janela:
        cabecalho = (f'<p class="nota" style="margin:0 0 12px">Mesma janela de '
                     f'{janela[0].strftime("%d/%m")} a {janela[1].strftime("%d/%m")} nas '
                     f'duas colunas — numa semana só, cidade bloqueada entra com dois '
                     f'contatos e 100% de conversão.</p>')
    linhas = "".join(
        f'<tr><td>{c["cidade"]}'
        f'{"<span class=\"tag tag-bl\">bloqueada</span>" if c["bloqueada"] else ""}</td>'
        f'<td class="r">{_num_br(c["conversao"], 1, "%")}</td>'
        f'<td class="r">{_num_br(c["quebra"], 1, "%")}</td>'
        f'<td class="r"><b>{_num_br(c["liquido"], 1, "%")}</b></td></tr>'
        for c in liquido)
    return (cabecalho
            + f'<table><tr><th>Cidade</th><th class="r">Conversão</th>'
            f'<th class="r">Quebra</th><th class="r">Líquido</th></tr>{linhas}</table>'
            f'<p class="nota"><b>Líquido</b> é conversão × (1 − quebra): de cada 100 '
            f'contatos, quantos viram instalação feita. Conversão alta com quebra alta '
            f'entrega menos que cidade mediana nas duas pontas — é o número que diz se '
            f'a cidade vale a fila. Quebra é assunto de qualidade e infraestrutura, '
            f'não motivo de bloqueio: bloqueio responde a capacidade de execução na ponta.</p>')


def _bloco_funil(f):
    total = f["fria"] + f["retorno"]
    if not total:
        return '<p class="ok">Sem contatos na semana.</p>'
    outros = total - f["agendados"] - f["recusado"] - f["ja_migrado"]
    perdas = (f'<tr><td>Recusas</td><td class="r">{f["recusado"]}</td>'
              f'<td class="r">{_num_br(100.0 * f["recusado"] / total, 0, "%")}</td></tr>'
              f'<tr><td>Cliente já migrado</td><td class="r">{f["ja_migrado"]}</td>'
              f'<td class="r">{_num_br(100.0 * f["ja_migrado"] / total, 0, "%")}</td></tr>'
              f'<tr><td>Outros desfechos (contrato desabilitado, sem GPON…)</td>'
              f'<td class="r">{outros}</td>'
              f'<td class="r">{_num_br(100.0 * outros / total, 0, "%")}</td></tr>')
    return (
        f'<table><tr><th>Etapa</th><th class="r">Contatos</th>'
        f'<th class="r">Agendas</th><th class="r">Conversão</th></tr>'
        f'<tr><td>1ª abordagem</td><td class="r">{f["fria"]}</td>'
        f'<td class="r">{f["fria_ag"]}</td>'
        f'<td class="r">{_num_br(f["conv_fria"], 1, "%")}</td></tr>'
        f'<tr><td>Retorno de follow-up</td><td class="r">{f["retorno"]}</td>'
        f'<td class="r">{f["retorno_ag"]}</td>'
        f'<td class="r">{_num_br(f["conv_retorno"], 1, "%")}</td></tr>'
        f'</table>'
        f'<p class="nota"><b>{_num_br(f["pct_do_retorno"], 0, "%")} das agendas da semana '
        f'vieram do retorno</b>, não do primeiro contato. O follow-up é onde o resultado '
        f'acontece — é por isso que mexer no prazo de retorno move o número do time '
        f'inteiro.</p>'
        f'<table style="margin-top:6px"><tr><th>Desfecho dos {total} contatos</th>'
        f'<th class="r">Qtd.</th><th class="r">Fatia</th></tr>{perdas}</table>'
        f'<p class="nota">Fora desses contatos houve <b>{f["improdutivo"]} tentativas sem '
        f'contato</b> (improdutivo: não falou com o cliente). Não entram na conversão '
        f'porque não houve conversa — entram no custo de ligação.</p>')


def _bloco_processo(linhas):
    if not linhas:
        return ""
    corpo = "".join(
        f'<tr><td>{l["inicio"].strftime("%d/%m")}</td>'
        f'<td class="r">{l["contatos"] or "—"}</td>'
        f'<td class="r">{_num_br(l["pct_fria"], 0, "%")}</td>'
        f'<td class="r">{_num_br(l["conversao"], 1, "%")}</td>'
        f'<td class="r">{_num_br(l["esperado"], 1, "%")}</td>'
        + (f'<td class="r"><span class="{"up" if l["diferenca"] >= 0 else "down"}">'
           f'{_num_br(l["diferenca"], 1)}</span></td>' if l["diferenca"] is not None
           else '<td class="r">—</td>')
        + f'<td class="r">{_num_br(l["alternancia"], 0, "%")}</td></tr>'
        for l in linhas)
    return (f'<table><tr><th>Semana</th><th class="r">Contatos</th><th class="r">Fria</th>'
            f'<th class="r">Conversão</th><th class="r">Esperado</th>'
            f'<th class="r">Dif.</th><th class="r">Altern.</th></tr>{corpo}</table>'
            f'<p class="nota"><b>Fria</b> é a fatia de primeira abordagem na semana. '
            f'<b>Esperado</b> é a conversão que esse mix sozinho produziria — semana com '
            f'mais abordagem fria converte menos sem ninguém ter piorado, e é a '
            f'<b>diferença</b> que mostra o desempenho de verdade.</p>'
            f'<p class="nota"><b>Alternância</b> é a fatia de retentativas feitas em faixa '
            f'de horário diferente da anterior (2h ou mais). Insistir no mesmo horário com '
            f'quem não atende é gastar tentativa à toa. A série existe para responder o que '
            f'cada mudança de configuração provocou — se ela virar numa semana, foi ali. '
            f'O CRM ainda não grava histórico de alteração de configuração; vale gravar, '
            f'como o bloqueio de cidade passou a gravar.</p>')


def _bloco_bloqueios(bloqueios):
    if bloqueios is None:
        return ('<p class="nota" style="margin:0">O histórico de bloqueio começa a ser '
                'gravado a partir da primeira carga do app.py novo.</p>')
    if not bloqueios:
        return '<p class="ok">Nenhuma cidade entrou ou saiu da distribuição nesta semana.</p>'
    linhas = "".join(
        f'<tr><td>{b["cidade"].title()}</td>'
        f'<td>{"bloqueada" if b["bloqueado"] else "liberada"}</td>'
        f'<td>{str(b["alterado_em"])[:16]}</td>'
        f'<td>{(b["alterado_por"] or "").title()}</td></tr>'
        for b in bloqueios)
    return (f'<table><tr><th>Cidade</th><th>Virou</th><th>Quando</th>'
            f'<th>Quem</th></tr>{linhas}</table>'
            f'<p class="nota">Bloquear cidade muda a composição da fila, então muda a '
            f'comparação da semana: uma queda de conversão do time depois de bloquear '
            f'cidade que convertia bem é efeito de mix, não de desempenho.</p>')


def montar_html_semanal(d):
    ini, fim, n, ant = d["inicio"], d["fim"], d["numeros"], d["anterior"]
    periodo = f'{ini.strftime("%d/%m")} a {fim.strftime("%d/%m/%Y")}'
    conv = _num_br(n["conversao"], 1, "%")

    if d["semana_quebra"]:
        ij, fq = d["janela_cidade"]
        aviso_quebra = (
            f'<p class="nota" style="margin:0 0 12px">Execução e quebra referentes às '
            f'{SEMANAS_JANELA_CIDADE} semanas de <b>{ij.strftime("%d/%m")} a '
            f'{fq.strftime("%d/%m")}</b> ({d["desfecho"]["total"]} agendas). A semana de '
            f'{ini.strftime("%d/%m")} ainda tem só '
            f'{_num_br(100 * d["cobertura_semana"]["cobertura"], 0, "%")} de desfecho '
            f'importado — a importação de quebra é manual e chega depois. '
            f'Cidade com menos de {MIN_AGENDAS_CIDADE} agendas no período fica de fora '
            f'({d["desfecho"]["fora_do_corte"]} cidades).</p>')
        bloco_cid = _quadrante_cidades(d["desfecho"]) or (
            '<p class="ok">Sem cidade com agendas suficientes no período.</p>')
    else:
        aviso_quebra = ""
        bloco_cid = ('<p class="nota" style="margin:0">Nenhuma das últimas 8 semanas tem '
                     'desfecho de agenda importado. Sem isso não há execução nem quebra '
                     'para medir — vale importar o arquivo de quebra.</p>')

    quad_pessoas = _quadrante_pessoas(d["pessoas"])

    return f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<style>{ESTILO}</style></head><body><div class="wrap">
<div class="card">
  <h1>CRM RSI — semana de {periodo}</h1>
  <p class="sub">{n["pessoas"]} pessoas com atividade</p>
  <div class="num"><b>{n["atividades"]}</b><span>atividades</span></div>
  <div class="num"><b>{n["contatos"]}</b><span>contatos</span></div>
  <div class="num"><b>{n["agendados"]}</b><span>agendados</span></div>
  <div class="num"><b>{conv}</b><span>conversão</span></div>
  <p class="cmp">Contra a semana anterior:
     contatos {_cmp(n["contatos"], ant["contatos"])} ·
     agendados {_cmp(n["agendados"], ant["agendados"])} ·
     conversão {_cmp_pp(n["conversao"], ant["conversao"])}</p>
</div>
<div class="card"><h2>Leitura da semana</h2>
  {"".join(f'<p class="destaque">{frase}</p>' for frase in leitura_da_semana(d))}
</div>
<div class="card"><h2>De onde vêm as agendas</h2>{_bloco_funil(d["funil"])}</div>
<div class="card"><h2>Desempenho por pessoa</h2>{_bloco_pessoas(d["pessoas"])}</div>
{f'<div class="card"><h2>Quadrante do time</h2>{quad_pessoas}</div>' if quad_pessoas else ''}
<div class="card"><h2>Quadrante das cidades</h2>{aviso_quebra}{bloco_cid}</div>
<div class="card"><h2>Rendimento líquido por cidade</h2>{_bloco_liquido(d["liquido"], d["janela_cidade"])}</div>
<div class="card"><h2>Indicadores de processo</h2>{_bloco_processo(d["processo"])}</div>
<div class="card"><h2>Mudanças de distribuição na semana</h2>{_bloco_bloqueios(d["bloqueios"])}</div>
<p class="nota" style="text-align:center">Quebra de agenda não aparece por pessoa:
testadas as diferenças individuais contra a base do time, nenhuma passa do que o
azar explica. Ela é característica de cidade e de infraestrutura.</p>
</div></body></html>"""


# ---------------------------------------------------------------------------
# Envio
# ---------------------------------------------------------------------------
def enviar_email(conn, assunto, corpo_html):
    cfg = conn.execute("""
        SELECT email_smtp_host, email_smtp_porta, email_smtp_usuario,
               email_smtp_senha, email_remetente FROM configuracoes WHERE id=1
    """).fetchone()
    if not cfg or not cfg["email_smtp_host"] or not cfg["email_smtp_usuario"]:
        return False, ("SMTP não configurado. Preencha host, porta, usuário e senha "
                       "na tela de configuração de e-mail do CRM.")
    destinos = [r["email"] for r in conn.execute(
        "SELECT email FROM relatorio_email_destinatarios WHERE ativo=1").fetchall()]
    if not destinos:
        return False, "Nenhum destinatário ativo em relatorio_email_destinatarios."

    msg = EmailMessage()
    msg["Subject"] = assunto
    msg["From"] = cfg["email_remetente"] or cfg["email_smtp_usuario"]
    msg["To"] = ", ".join(destinos)
    msg.set_content("Seu leitor de e-mail não exibe HTML. O relatório está na versão HTML.")
    msg.add_alternative(corpo_html, subtype="html")

    try:
        porta = int(cfg["email_smtp_porta"] or 587)
        with smtplib.SMTP(cfg["email_smtp_host"], porta, timeout=60) as smtp:
            smtp.starttls()
            smtp.login(cfg["email_smtp_usuario"], cfg["email_smtp_senha"] or "")
            smtp.send_message(msg)
    except Exception as erro:
        return False, f"Falha ao enviar: {erro}"
    return True, f"Enviado para {', '.join(destinos)}"


USO = """Uso:
  python3.10 relatorio_gerencial.py --diario  [--data AAAA-MM-DD]   [--preview arq.html | --enviar]
  python3.10 relatorio_gerencial.py --semanal [--semana AAAA-MM-DD] [--preview arq.html | --enviar]

  --data    dia do relatório diário (padrão: hoje)
  --semana  qualquer dia da semana desejada (padrão: a última semana fechada)
"""


def _opcao(args, nome):
    if nome not in args:
        return None
    try:
        return args[args.index(nome) + 1]
    except IndexError:
        sys.exit(f"ERRO: {nome} precisa vir seguido de uma data AAAA-MM-DD.")


def _entregar(conn, args, html, assunto, resumo):
    """--preview grava arquivo, --enviar manda e-mail, sem nada imprime o HTML."""
    caminho = _opcao(args, "--preview")
    if caminho:
        with open(caminho, "w", encoding="utf-8") as arq:
            arq.write(html)
        print(f"Prévia gravada em {caminho}")
        print(f"Assunto seria: {assunto}")
        print(resumo)
        return True
    if "--enviar" in args:
        ok, detalhe = enviar_email(conn, assunto, html)
        print(("OK: " if ok else "ERRO: ") + detalhe)
        return ok
    print(html)
    return True


def main():
    args = sys.argv[1:]
    diario, semanal = "--diario" in args, "--semanal" in args
    if diario == semanal:
        sys.exit(("ERRO: escolha --diario OU --semanal.\n\n" if diario else "") + USO)

    conn = conectar()

    if diario:
        texto = _opcao(args, "--data")
        dia = date.fromisoformat(texto) if texto else date.today()
        dados = montar_diario(conn, dia)
        html = montar_html_diario(dados)
        n, qtd = dados["numeros"], len(dados["variacoes"])
        if not n["atividades"]:
            assunto = f"CRM RSI · {dia.strftime('%d/%m')} · sem atividade no dia"
        else:
            assunto = (f"CRM RSI · {dia.strftime('%d/%m')} · "
                       + (f"{qtd} ponto{'s' if qtd != 1 else ''} de atenção"
                          if qtd else "tudo dentro do normal"))
        resumo = (f"Números: {n['atividades']} atividades, {n['contatos']} contatos, "
                  f"{n['agendados']} agendados | {qtd} variação(ões)")
    else:
        texto = _opcao(args, "--semana")
        referencia = date.fromisoformat(texto) if texto else date.today()
        ini, fim = (semana_de(referencia) if texto
                    else semana_fechada_antes(referencia))
        dados = montar_semanal(conn, ini, fim)
        html = montar_html_semanal(dados)
        n = dados["numeros"]
        destaques = sum(1 for p in dados["pessoas"] if abs(p["z"]) >= Z_RELEVANTE)
        assunto = (f"CRM RSI · semana {ini.strftime('%d/%m')}-{fim.strftime('%d/%m')} · "
                   f"{n['agendados']} agendas, conversão {_num_br(n['conversao'], 1, '%')}")
        sq = dados["semana_quebra"]
        resumo = (f"Números: {n['contatos']} contatos, {n['agendados']} agendados | "
                  f"{len(dados['pessoas'])} pessoas no corte, {destaques} com diferença "
                  f"relevante | desfecho de agenda: "
                  + (f"semana de {sq[0].strftime('%d/%m')}" if sq else "sem cobertura"))

    ok = _entregar(conn, args, html, assunto, resumo)
    conn.close()
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
