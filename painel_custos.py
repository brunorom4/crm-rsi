"""
Painel de Custos da Esteira de Migração -- pedido do Bruno, 2026-10-16.
Visão exclusiva do ADM (/admin/custos).

Base: planilha "Histórico Esteira de Migrações x Custo" (jan/2023 a jul/2026,
este último marcado como projeção). Os valores da planilha entram como
semente na primeira subida; daqui pra frente tudo é editado na própria tela.

Modelo de custo (uma fórmula só, que reproduz as duas fases da planilha):

    custo_total = assistentes x custo_hc_assistente
                + (assistentes + analistas + supervisores) x custo_maquina
                + analistas x custo_analista
                + supervisores x custo_supervisor

  - Até jun/2025: só assistentes -> (Custo HC + Máquina) x Assistentes.
  - De jul/2025: + 1 analista (R$ 9.460,16) e máquina para ele.
  - De jun/2026: + 1 supervisor (R$ 16.091,78) e máquina para ele.

Migrações do mês (regra combinada com o Bruno):
  1. ajuste manual do ADM, se houver;
  2. senão o número da planilha (histórico);
  3. senão as execuções importadas no CRM (execucoes_os, pelo mês da baixa).
A tela mostra a fonte de cada mês e, ao lado, sempre o número do CRM -- a
planilha e o CRM batem até jun/2025 e, a partir de jul/2025, a planilha
aparece deslocada um mês em relação ao CRM (achado de 2026-10-16; o Bruno
decidiu manter a planilha como histórico, com ajuste manual disponível).

Mês novo nasce com os custos e o quadro do mês anterior (editáveis).
"""
import os
import sqlite3
from datetime import datetime, date

from flask import Blueprint, render_template, request, redirect

bp_painel_custos = Blueprint("painel_custos", __name__)

MIN_MIGRACOES_COMPARACAO = 20

PASTA_BASE_CUSTOS = os.path.dirname(os.path.abspath(__file__))

# Semente: planilha Historico_Esteira_de_Migracoes_x_Custo.xlsx (2026-10-16).
# (mes, assistentes, analistas, supervisores, custo_hc, custo_maquina, custo_analista,
#  custo_supervisor, migracoes, observacao, projecao)
_HC1, _HC2, _MAQ, _AN, _SUP = 4118.67, 6847.0, 500.0, 9460.16, 16091.78
_ASSIST = [6, 6, 6, 6, 6, 6, 7, 7, 10, 10, 10, 10,           # 2023
           10, 10, 10, 10, 10, 10, 10, 10, 10, 12, 12, 12,   # 2024
           12, 14, 16, 16, 17, 17, 16, 16, 16, 16, 16, 16,   # 2025
           16, 19, 19, 19, 19, 23, 25]                       # 2026 (jan-jul)
_MIGR = [5, 97, 62, 107, 224, 255, 242, 355, 476, 669, 491, 590,
         564, 434, 427, 547, 632, 463, 498, 629, 543, 466, 516, 412,
         390, 395, 469, 656, 538, 667, 640, 561, 609, 407, 135, 139,
         70, 728, 930, 1042, 916, 932, 1200]
_OBS = {"2025-10": "Migração paralisada parcialmente para apoio ao Comercial",
        "2025-11": "Migração paralisada para apoio ao Comercial",
        "2025-12": "Migração paralisada para apoio ao Comercial",
        "2026-01": "Migração paralisada para apoio ao Comercial"}


def _semente():
    linhas = []
    for i, (assist, migr) in enumerate(zip(_ASSIST, _MIGR)):
        ano, mes = 2023 + i // 12, i % 12 + 1
        chave = f"{ano}-{mes:02d}"
        fase2 = chave >= "2025-07"
        fase3 = chave >= "2026-06"
        linhas.append((
            chave, assist, 1 if fase2 else 0, 1 if fase3 else 0,
            _HC2 if fase2 else _HC1, _MAQ, _AN if fase2 else 0.0, _SUP if fase3 else 0.0,
            migr, _OBS.get(chave), 1 if chave == "2026-07" else 0,
        ))
    return linhas


def _conectar():
    conn = sqlite3.connect(os.path.join(PASTA_BASE_CUSTOS, "crm.db"))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def garantir_tabelas_custos():
    conn = _conectar()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS custos_mensais (
            mes TEXT PRIMARY KEY,               -- YYYY-MM
            assistentes INTEGER DEFAULT 0,
            analistas INTEGER DEFAULT 0,
            supervisores INTEGER DEFAULT 0,
            custo_hc_assistente REAL DEFAULT 0,
            custo_maquina REAL DEFAULT 0,       -- por pessoa (assistente, analista, supervisor)
            custo_analista REAL DEFAULT 0,      -- por analista
            custo_supervisor REAL DEFAULT 0,    -- por supervisor
            migracoes_planilha INTEGER,         -- histórico da planilha (semente)
            migracoes_ajuste INTEGER,           -- ajuste manual do ADM (vence tudo)
            observacao TEXT,
            projecao INTEGER DEFAULT 0,
            atualizado_por TEXT,
            atualizado_em TEXT
        );
        CREATE TABLE IF NOT EXISTS custos_config (
            chave TEXT PRIMARY KEY,
            valor TEXT
        );
    """)
    vazio = conn.execute("SELECT COUNT(*) FROM custos_mensais").fetchone()[0] == 0
    if vazio:
        conn.executemany("""
            INSERT INTO custos_mensais (mes, assistentes, analistas, supervisores, custo_hc_assistente,
                custo_maquina, custo_analista, custo_supervisor, migracoes_planilha, observacao, projecao,
                atualizado_por, atualizado_em)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'planilha', ?)
        """, [l + (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),) for l in _semente()])
    conn.commit()
    conn.close()


garantir_tabelas_custos()


# ==========================
# CÁLCULO
# ==========================

def custo_total(m):
    a, an, s = m["assistentes"] or 0, m["analistas"] or 0, m["supervisores"] or 0
    return (a * (m["custo_hc_assistente"] or 0)
            + (a + an + s) * (m["custo_maquina"] or 0)
            + an * (m["custo_analista"] or 0)
            + s * (m["custo_supervisor"] or 0))


def composicao(m):
    a, an, s = m["assistentes"] or 0, m["analistas"] or 0, m["supervisores"] or 0
    return {
        "hc": a * (m["custo_hc_assistente"] or 0),
        "maquina": (a + an + s) * (m["custo_maquina"] or 0),
        "analista": an * (m["custo_analista"] or 0),
        "supervisor": s * (m["custo_supervisor"] or 0),
    }


def _proximo_mes(chave):
    ano, mes = int(chave[:4]), int(chave[5:7])
    return f"{ano + (mes == 12)}-{(mes % 12) + 1:02d}"


def garantir_meses_ate(conn, ate_chave):
    """Cria os meses que faltam até `ate_chave`, copiando quadro e custos do
    mês anterior (migrações em branco -> CRM)."""
    ultimo = conn.execute("SELECT * FROM custos_mensais ORDER BY mes DESC LIMIT 1").fetchone()
    if not ultimo:
        return
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    chave, base = ultimo["mes"], ultimo
    while chave < ate_chave:
        chave = _proximo_mes(chave)
        conn.execute("""
            INSERT OR IGNORE INTO custos_mensais (mes, assistentes, analistas, supervisores,
                custo_hc_assistente, custo_maquina, custo_analista, custo_supervisor, projecao,
                atualizado_por, atualizado_em)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 'automático (cópia do mês anterior)', ?)
        """, (chave, base["assistentes"], base["analistas"], base["supervisores"],
              base["custo_hc_assistente"], base["custo_maquina"], base["custo_analista"],
              base["custo_supervisor"], agora))
    conn.commit()


def migracoes_crm(conn):
    """{YYYY-MM: execuções importadas no CRM (execucoes_os) pelo mês da baixa}."""
    try:
        return {r[0]: r[1] for r in conn.execute(
            "SELECT substr(dh_baixa, 1, 7), COUNT(*) FROM execucoes_os WHERE dh_baixa IS NOT NULL GROUP BY 1")}
    except sqlite3.OperationalError:
        return {}


def _dias_uteis(ano, mes, ate_dia=None):
    from calendar import monthrange
    ultimo = monthrange(ano, mes)[1]
    fim = min(ate_dia, ultimo) if ate_dia else ultimo
    return sum(1 for d in range(1, fim + 1) if date(ano, mes, d).weekday() < 5)


def montar_painel(conn, hoje=None):
    hoje = hoje or date.today()
    mes_atual = hoje.strftime("%Y-%m")
    fim_ano = f"{hoje.year}-12"
    garantir_meses_ate(conn, fim_ano)

    meta_txt = conn.execute("SELECT valor FROM custos_config WHERE chave='meta_custo_unitario'").fetchone()
    meta = float(meta_txt["valor"]) if meta_txt and meta_txt["valor"] else None
    crm = migracoes_crm(conn)

    meses = []
    for m in conn.execute("SELECT * FROM custos_mensais ORDER BY mes"):
        total = custo_total(m)
        if m["migracoes_ajuste"] is not None:
            migr, fonte = m["migracoes_ajuste"], "Ajuste ADM"
        elif m["migracoes_planilha"] is not None:
            migr, fonte = m["migracoes_planilha"], "Planilha"
        elif m["mes"] in crm:
            migr, fonte = crm[m["mes"]], "CRM"
        else:
            migr, fonte = None, "—"
        futuro = m["mes"] > mes_atual
        em_andamento = m["mes"] == mes_atual
        unit = round(total / migr, 2) if migr else None
        pessoas = (m["assistentes"] or 0)
        meses.append({
            **dict(m),
            "custo_total": round(total, 2),
            "composicao": {k: round(v, 2) for k, v in composicao(m).items()},
            "migracoes": migr,
            "fonte": fonte,
            "migracoes_crm": crm.get(m["mes"]),
            "custo_unitario": unit,
            "produtividade": round(migr / pessoas, 1) if migr and pessoas else None,
            "futuro": futuro,
            "em_andamento": em_andamento,
            "projecao": bool(m["projecao"]),
            "rotulo": f"{m['mes'][5:7]}/{m['mes'][2:4]}",
        })

    # Meses "fechados" = passados, com migração, que não são projeção nem o mês corrente
    fechados = [x for x in meses if x["migracoes"] and not x["futuro"] and not x["em_andamento"] and not x["projecao"]]
    analise = _analises(meses, fechados, meta, hoje)
    anos = _resumo_anual(fechados)
    return {"meses": meses, "fechados": fechados, "meta": meta, "analise": analise, "anos": anos,
            "mes_atual": mes_atual, "crm": crm}


def _media(valores):
    valores = [v for v in valores if v is not None]
    return sum(valores) / len(valores) if valores else None


def _analises(meses, fechados, meta, hoje):
    a = {}
    if not fechados:
        return a
    tot_custo = sum(x["custo_total"] for x in fechados)
    tot_migr = sum(x["migracoes"] for x in fechados)
    a["custo_acumulado"] = tot_custo
    a["migracoes_acumuladas"] = tot_migr
    a["unitario_geral"] = tot_custo / tot_migr if tot_migr else None

    ult = fechados[-1]
    a["ultimo"] = ult
    ult12 = fechados[-12:]
    a["unitario_12m"] = (sum(x["custo_total"] for x in ult12) / sum(x["migracoes"] for x in ult12))
    # Melhor/pior ignoram meses com produção ínfima (ex.: jan/2023, início da
    # operação, 5 migrações -> R$ 5.542/migração), que distorcem a comparação.
    comparaveis = [x for x in fechados if x["migracoes"] >= MIN_MIGRACOES_COMPARACAO] or fechados
    a["melhor"] = min(comparaveis, key=lambda x: x["custo_unitario"])
    a["pior"] = max(comparaveis, key=lambda x: x["custo_unitario"])
    a["fora_comparacao"] = [x for x in fechados if x["migracoes"] < MIN_MIGRACOES_COMPARACAO]

    if len(fechados) >= 6:
        ult3, ant3 = fechados[-3:], fechados[-6:-3]
        u3 = sum(x["custo_total"] for x in ult3) / sum(x["migracoes"] for x in ult3)
        a3 = sum(x["custo_total"] for x in ant3) / sum(x["migracoes"] for x in ant3)
        a["tendencia"] = {"ult3": u3, "ant3": a3, "variacao": (u3 / a3 - 1) * 100 if a3 else None}

    # Ponto de equilíbrio com o quadro do mês corrente (ou do último cadastrado)
    ref = next((x for x in meses if x["em_andamento"]), None) or meses[-1]
    a["quadro_atual"] = ref
    if meta:
        a["migracoes_para_meta"] = int(-(-ref["custo_total"] // meta))  # teto

    # Mês corrente: projeção linear por dias úteis
    if ref["em_andamento"] and ref["migracoes_crm"] is not None:
        feitos = ref["migracoes_crm"]
        du_passados = _dias_uteis(hoje.year, hoje.month, hoje.day)
        du_total = _dias_uteis(hoje.year, hoje.month)
        proj = round(feitos / du_passados * du_total) if du_passados else None
        a["mes_corrente"] = {
            "feitos": feitos, "du_passados": du_passados, "du_total": du_total, "projecao": proj,
            "unitario_projetado": ref["custo_total"] / proj if proj else None,
        }

    # Planilha x CRM: meses com divergência relevante (> 5%)
    div = [x for x in meses if x["migracoes_planilha"] is not None and x["migracoes_crm"] is not None
           and x["migracoes_planilha"] and abs(x["migracoes_crm"] - x["migracoes_planilha"]) / x["migracoes_planilha"] > 0.05]
    a["divergencias"] = div
    a["paralisados"] = [x for x in fechados if x.get("observacao")]
    return a


def _resumo_anual(fechados):
    anos = {}
    for x in fechados:
        ano = x["mes"][:4]
        r = anos.setdefault(ano, {"ano": ano, "custo": 0.0, "migracoes": 0, "meses": 0, "assistentes": 0})
        r["custo"] += x["custo_total"]
        r["migracoes"] += x["migracoes"]
        r["meses"] += 1
        r["assistentes"] += x["assistentes"] or 0
    lista = []
    anterior = None
    for ano in sorted(anos):
        r = anos[ano]
        r["unitario"] = r["custo"] / r["migracoes"] if r["migracoes"] else None
        r["hc_medio"] = r["assistentes"] / r["meses"] if r["meses"] else None
        r["migr_mes"] = r["migracoes"] / r["meses"] if r["meses"] else None
        r["var_unitario"] = ((r["unitario"] / anterior["unitario"] - 1) * 100
                             if anterior and anterior["unitario"] and r["unitario"] else None)
        lista.append(r)
        anterior = r
    return lista


# ==========================
# ROTAS
# ==========================

def _num(valor, inteiro=False):
    txt = (valor or "").strip().replace("R$", "").replace(" ", "")
    if not txt:
        return None
    if "," in txt:
        txt = txt.replace(".", "").replace(",", ".")
    try:
        return int(float(txt)) if inteiro else float(txt)
    except ValueError:
        return None


@bp_painel_custos.route("/admin/custos")
def painel_custos():
    from app import login_required, perfil_required

    @login_required
    @perfil_required("ADM")
    def _tela():
        conn = _conectar()
        dados = montar_painel(conn)
        conn.close()
        return render_template("PainelCustos.html", **dados,
                               ok=request.args.get("ok"), erro=request.args.get("erro"))
    return _tela()


@bp_painel_custos.route("/admin/custos/mes", methods=["POST"])
def salvar_mes():
    from app import login_required, perfil_required, usuario_atual

    @login_required
    @perfil_required("ADM")
    def _salvar():
        import urllib.parse
        mes = (request.form.get("mes") or "").strip()
        if len(mes) != 7 or mes[4] != "-":
            return redirect("/admin/custos?erro=" + urllib.parse.quote("Mês inválido."))
        campos = {
            "assistentes": _num(request.form.get("assistentes"), True) or 0,
            "analistas": _num(request.form.get("analistas"), True) or 0,
            "supervisores": _num(request.form.get("supervisores"), True) or 0,
            "custo_hc_assistente": _num(request.form.get("custo_hc_assistente")) or 0,
            "custo_maquina": _num(request.form.get("custo_maquina")) or 0,
            "custo_analista": _num(request.form.get("custo_analista")) or 0,
            "custo_supervisor": _num(request.form.get("custo_supervisor")) or 0,
            "migracoes_ajuste": _num(request.form.get("migracoes_ajuste"), True),
            "observacao": (request.form.get("observacao") or "").strip() or None,
            "projecao": 1 if request.form.get("projecao") else 0,
        }
        conn = _conectar()
        existe = conn.execute("SELECT 1 FROM custos_mensais WHERE mes=?", (mes,)).fetchone()
        agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if not existe:
            conn.execute("INSERT INTO custos_mensais (mes) VALUES (?)", (mes,))
        conn.execute(f"""
            UPDATE custos_mensais SET {', '.join(f'{k}=?' for k in campos)}, atualizado_por=?, atualizado_em=?
            WHERE mes=?
        """, list(campos.values()) + [usuario_atual()["nome"], agora, mes])
        if request.form.get("aplicar_seguintes"):
            # Propaga quadro e custos (não migrações/observação) para os meses seguintes
            conn.execute("""
                UPDATE custos_mensais SET assistentes=?, analistas=?, supervisores=?, custo_hc_assistente=?,
                    custo_maquina=?, custo_analista=?, custo_supervisor=?, atualizado_por=?, atualizado_em=?
                WHERE mes > ?
            """, (campos["assistentes"], campos["analistas"], campos["supervisores"], campos["custo_hc_assistente"],
                  campos["custo_maquina"], campos["custo_analista"], campos["custo_supervisor"],
                  usuario_atual()["nome"], agora, mes))
        conn.commit()
        conn.close()
        return redirect("/admin/custos?ok=" + urllib.parse.quote(f"Mês {mes[5:7]}/{mes[:4]} salvo.") + "#tabela-meses")
    return _salvar()


@bp_painel_custos.route("/admin/custos/meta", methods=["POST"])
def salvar_meta():
    from app import login_required, perfil_required

    @login_required
    @perfil_required("ADM")
    def _salvar():
        import urllib.parse
        valor = _num(request.form.get("meta"))
        conn = _conectar()
        conn.execute("INSERT INTO custos_config (chave, valor) VALUES ('meta_custo_unitario', ?) "
                     "ON CONFLICT(chave) DO UPDATE SET valor=excluded.valor",
                     (str(valor) if valor else None,))
        conn.commit()
        conn.close()
        return redirect("/admin/custos?ok=" + urllib.parse.quote("Meta salva."))
    return _salvar()
