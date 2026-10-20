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

COMO USAR

  Prévia em arquivo (não envia nada):
      python3.10 relatorio_gerencial.py --diario --preview previa.html

  Prévia de um dia específico:
      python3.10 relatorio_gerencial.py --diario --data 2026-10-16 --preview previa.html

  Envio de verdade (é o que a Scheduled Task roda):
      python3.10 relatorio_gerencial.py --diario --enviar

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
    a mesma base usada pelo ranking do CRM."""
    marcadores = ",".join("?" * len(TABULACOES_SEM_CONTATO))
    linha = conn.execute(f"""
        SELECT COUNT(*) atividades,
               SUM(CASE WHEN tabulacao NOT IN ({marcadores}) THEN 1 ELSE 0 END) contatos,
               SUM(CASE WHEN tabulacao='Agendado' THEN 1 ELSE 0 END) agendados,
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
    return {
        "atividades": statistics.mean(a["atividades"] for a in amostras),
        "contatos": statistics.mean(a["contatos"] for a in amostras),
        "agendados": statistics.mean(a["agendados"] for a in amostras),
        "conversao": statistics.mean(a["conversao"] for a in amostras if a["conversao"] is not None),
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
"""


def _cmp(atual, media, unidade="", inverter=False):
    if media is None or not media:
        return ""
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
    return {
        "dia": dia,
        "numeros": numeros_do_dia(conn, dia),
        "comparacao": media_mesmo_dia_semana(conn, dia),
        "variacoes": variacoes_de_volume(conn, dia),
        "fila": situacao_da_fila(conn),
        "chuva": risco_de_chuva(conn, dia),
    }


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


def main():
    args = sys.argv[1:]
    if "--diario" not in args:
        sys.exit("Use --diario (o --semanal ainda não existe).\n"
                 "Ex: python3.10 relatorio_gerencial.py --diario --preview previa.html")

    if "--data" in args:
        dia = date.fromisoformat(args[args.index("--data") + 1])
    else:
        dia = date.today()

    conn = conectar()
    dados = montar_diario(conn, dia)
    html = montar_html_diario(dados)
    n = dados["numeros"]
    qtd = len(dados["variacoes"])
    assunto = (f"CRM RSI · {dia.strftime('%d/%m')} · "
               + (f"{qtd} ponto{'s' if qtd != 1 else ''} de atenção" if qtd else "tudo dentro do normal"))

    if "--preview" in args:
        caminho = args[args.index("--preview") + 1]
        with open(caminho, "w", encoding="utf-8") as arq:
            arq.write(html)
        print(f"Prévia gravada em {caminho}")
        print(f"Assunto seria: {assunto}")
        print(f"Números: {n['atividades']} atividades, {n['contatos']} contatos, "
              f"{n['agendados']} agendados | {qtd} variação(ões)")
    elif "--enviar" in args:
        ok, detalhe = enviar_email(conn, assunto, html)
        print(("OK: " if ok else "ERRO: ") + detalhe)
        if not ok:
            conn.close()
            sys.exit(1)
    else:
        print(html)
    conn.close()


if __name__ == "__main__":
    main()
