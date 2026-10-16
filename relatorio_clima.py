"""
Módulo Relatório de Clima -- pedido do Bruno, 2026-09-17.

Reaproveita a coleta de previsão do tempo que já existe no crm.db
(tabela previsao_tempo, atualizada diariamente pela
tarefa_atualizar_previsao_tempo.py) e o mapeamento cidade->DDD
(cidades_ddd) -- esse módulo só LÊ esses dados, nunca escreve nada no
crm.db. Duas saídas: página web (acessível pelo Portal, com permissão
própria) e um resumo diário por Telegram (via
tarefa_relatorio_clima_telegram.py).
"""
import sqlite3
import os
from datetime import datetime, timedelta
from flask import Blueprint, render_template

PASTA_BASE_CLIMA = os.path.dirname(os.path.abspath(__file__))

# As 6 cidades que o Bruno pediu pra destacar com detalhamento completo dos
# 7 dias -- o resto das cidades entra só no resumo agregado por DDD.
CIDADES_DESTAQUE = [
    "CAMPINAS", "RIBEIRAO PRETO", "SANTOS", "SOROCABA",
    "SAO JOSE DOS CAMPOS", "SAO JOSE DO RIO PRETO",
]

bp_relatorio_clima = Blueprint("relatorio_clima", __name__, template_folder="templates")


def _conectar_crm():
    conn = sqlite3.connect(os.path.join(PASTA_BASE_CLIMA, "crm.db"))
    conn.row_factory = sqlite3.Row
    return conn


def montar_relatorio_clima():
    """Monta a estrutura de dado usada tanto pela página web quanto pelo
    Telegram -- uma função só, pra nunca ter os dois divergindo."""
    conn = _conectar_crm()
    hoje_str = datetime.now().strftime("%Y-%m-%d")
    fim_str = (datetime.now() + timedelta(days=6)).strftime("%Y-%m-%d")

    linhas = conn.execute("""
        SELECT p.cidade, p.data, p.temp_min, p.temp_max, p.chance_chuva, p.condicao, c.ddd
        FROM previsao_tempo p
        LEFT JOIN cidades_ddd c ON c.cidade = p.cidade
        WHERE p.data BETWEEN ? AND ?
        ORDER BY p.data
    """, (hoje_str, fim_str)).fetchall()
    conn.close()

    # Agrupamento por DDD, por dia -- média de chance de chuva e faixa de
    # temperatura entre as cidades daquele DDD.
    por_ddd = {}
    for l in linhas:
        ddd = l["ddd"] or "Não mapeado"
        grupo = por_ddd.setdefault(ddd, {})
        dia = grupo.setdefault(l["data"], {"chuvas": [], "temp_mins": [], "temp_maxs": [], "cidades": 0})
        dia["chuvas"].append(l["chance_chuva"])
        dia["temp_mins"].append(l["temp_min"])
        dia["temp_maxs"].append(l["temp_max"])
        dia["cidades"] += 1

    ddd_resumo = []
    for ddd, dias in sorted(por_ddd.items()):
        dias_lista = []
        for data, d in sorted(dias.items()):
            dias_lista.append({
                "data": data,
                "chance_chuva_media": round(sum(d["chuvas"]) / len(d["chuvas"])) if d["chuvas"] else 0,
                "temp_min": round(min(d["temp_mins"])) if d["temp_mins"] else None,
                "temp_max": round(max(d["temp_maxs"])) if d["temp_maxs"] else None,
                "cidades": d["cidades"],
            })
        pico_chuva = max(dias_lista, key=lambda x: x["chance_chuva_media"]) if dias_lista else None
        ddd_resumo.append({"ddd": ddd, "dias": dias_lista, "pico_chuva": pico_chuva})

    # As 6 cidades de destaque, com o detalhamento completo dos 7 dias.
    cidades_resumo = []
    for cidade in CIDADES_DESTAQUE:
        dias_cidade = [l for l in linhas if l["cidade"] == cidade]
        if not dias_cidade:
            cidades_resumo.append({"cidade": cidade, "sem_dados": True})
            continue
        dias_lista = [{
            "data": l["data"], "temp_min": round(l["temp_min"]), "temp_max": round(l["temp_max"]),
            "chance_chuva": l["chance_chuva"], "condicao": l["condicao"],
        } for l in sorted(dias_cidade, key=lambda x: x["data"])]
        cidades_resumo.append({
            "cidade": cidade, "sem_dados": False, "ddd": dias_cidade[0]["ddd"], "dias": dias_lista,
        })

    # Resumo gerencial: classifica cada dia (média de chuva entre TODOS os
    # DDDs) como favorável pro trabalho de campo ou indicando foco em MDU
    # (prédios) -- pedido do Bruno, 2026-09-17: a operação depende de sol
    # pra trabalho externo, dia de chuva pede foco em prédios. Limiar de
    # 50% de chance média de chuva pra virar recomendação de MDU -- é uma
    # escolha razoável (não veio um número exato no pedido); ajusto fácil
    # se quiser outro corte.
    LIMIAR_CHUVA_MDU = 50
    dias_geral = {}
    for grupo in ddd_resumo:
        for d in grupo["dias"]:
            dias_geral.setdefault(d["data"], []).append(d["chance_chuva_media"])

    resumo_dias = []
    for data in sorted(dias_geral.keys()):
        media_geral = round(sum(dias_geral[data]) / len(dias_geral[data]))
        resumo_dias.append({
            "data": data,
            "chance_chuva_media": media_geral,
            "recomendacao": "MDU" if media_geral >= LIMIAR_CHUVA_MDU else "CAMPO",
        })

    # DDDs mais afetados na semana (maior média de chuva ao longo dos 7
    # dias) -- ajuda a direcionar o foco de MDU geograficamente, não só
    # por dia.
    ddds_ordenados_por_chuva = sorted(
        ddd_resumo,
        key=lambda g: sum(d["chance_chuva_media"] for d in g["dias"]) / len(g["dias"]) if g["dias"] else 0,
        reverse=True,
    )
    ddds_mais_afetados = [{
        "ddd": g["ddd"],
        "media_semana": round(sum(d["chance_chuva_media"] for d in g["dias"]) / len(g["dias"])) if g["dias"] else 0,
    } for g in ddds_ordenados_por_chuva[:3]]

    dias_favoraveis = sum(1 for d in resumo_dias if d["recomendacao"] == "CAMPO")
    dias_mdu = len(resumo_dias) - dias_favoraveis

    resumo_gerencial = {
        "dias": resumo_dias,
        "ddds_mais_afetados": ddds_mais_afetados,
        "dias_favoraveis": dias_favoraveis,
        "dias_mdu": dias_mdu,
    }

    return {
        "gerado_em": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "periodo_inicio": hoje_str, "periodo_fim": fim_str,
        "resumo_gerencial": resumo_gerencial,
        "por_ddd": ddd_resumo, "cidades_destaque": cidades_resumo,
    }


def _formatar_data_curta(data_iso):
    """'2026-09-18' -> '18/09'"""
    try:
        return datetime.strptime(data_iso, "%Y-%m-%d").strftime("%d/%m")
    except (ValueError, TypeError):
        return data_iso or "—"


def montar_texto_telegram(relatorio):
    """Relatório completo formatado pra Telegram (Markdown) -- resumo
    gerencial com recomendação de campo x MDU no topo, resumo por DDD
    (pico de chuva da semana) + detalhamento dia a dia das 6 cidades de
    destaque."""
    rg = relatorio["resumo_gerencial"]
    linhas = [
        "🌤️ *Relatório de Clima — Próximos 7 Dias*",
        f"_Período: {_formatar_data_curta(relatorio['periodo_inicio'])} a {_formatar_data_curta(relatorio['periodo_fim'])}_",
        "",
        "*📋 Resumo Gerencial — Recomendação de Atuação*",
        f"{rg['dias_favoraveis']} dia(s) favorável(is) pra campo · {rg['dias_mdu']} dia(s) com foco em MDU",
        "",
    ]
    for d in rg["dias"]:
        icone = "☀️ Campo" if d["recomendacao"] == "CAMPO" else "🏢 Foco MDU"
        linhas.append(f"  {_formatar_data_curta(d['data'])}: {icone} ({d['chance_chuva_media']}% chuva média)")
    if rg["ddds_mais_afetados"]:
        linhas.append("")
        linhas.append("*Regiões mais afetadas na semana:*")
        for a in rg["ddds_mais_afetados"]:
            linhas.append(f"  DDD {a['ddd']}: {a['media_semana']}% de chuva média na semana")
    linhas.append("")
    linhas.append("*Resumo por DDD* (pico de chance de chuva na semana):")
    for grupo in relatorio["por_ddd"]:
        pico = grupo["pico_chuva"]
        if pico:
            linhas.append(f"• DDD {grupo['ddd']}: {pico['chance_chuva_media']}% em {_formatar_data_curta(pico['data'])} ({pico['temp_min']}°-{pico['temp_max']}°C)")
    linhas.append("")
    linhas.append("*Cidades em destaque — detalhamento dos 7 dias:*")
    for c in relatorio["cidades_destaque"]:
        linhas.append("")
        if c.get("sem_dados"):
            linhas.append(f"📍 *{c['cidade']}* — sem previsão disponível")
            continue
        linhas.append(f"📍 *{c['cidade']}* (DDD {c['ddd']})")
        for d in c["dias"]:
            linhas.append(f"  {_formatar_data_curta(d['data'])}: {d['condicao']}, {d['temp_min']}°-{d['temp_max']}°C, {d['chance_chuva']}% chuva")

    return "\n".join(linhas)


@bp_relatorio_clima.route("/relatorio-clima")
def relatorio_clima_pagina():
    from app import usuario_atual, perfil_pode
    usuario = usuario_atual()
    if not usuario:
        from flask import redirect
        return redirect("/login")
    if not perfil_pode(usuario["perfil"], "relatorio_clima"):
        return "Acesso restrito.", 403

    relatorio = montar_relatorio_clima()
    return render_template("RelatorioClima.html", relatorio=relatorio, usuario_conf=usuario)
