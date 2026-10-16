# -*- coding: utf-8 -*-
"""
Geração da apresentação executiva (PPTX) do Painel de Resultados.

Lê os dados já prontos em painel_dados/dados_brownfield_rsi.xlsx (a mesma
base que alimenta o painel estático diario_brownfield_rsi.html) e monta um
dicionário com tudo que as slides precisam. A geração do .pptx em si fica em
montar_apresentacao_pptx() (usa python-pptx).

Fluxo pensado pro botão do ADM: cada clique relê o xlsx do zero, então o
deck sempre reflete a última atualização carregada no servidor -- não tem
cache nem estado guardado entre uma geração e outra.
"""
import os
from datetime import datetime

import openpyxl

MESES_ABREV = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"]

# Paleta "Cherry Bold" (vermelho Claro) -- usada tanto aqui (pra decidir cor
# condicional dos números) quanto no gerador do pptx.
COR_PRIMARIA = "990011"       # vermelho Claro
COR_SECUNDARIA = "2F3C7E"     # azul-marinho (contraste/segundo grupo)
COR_POSITIVO = "1D7A46"       # verde (delta positivo)
COR_NEGATIVO = "990011"       # vermelho (delta negativo)
COR_NEUTRA = "6B6B6B"


class DadosIndisponiveisError(Exception):
    """Levantado quando o xlsx do painel não existe ou está incompleto."""


def _ler_abas(caminho_xlsx):
    if not os.path.exists(caminho_xlsx):
        raise DadosIndisponiveisError(
            f"Base de dados do painel ({caminho_xlsx}) não encontrada. "
            f"Suba o arquivo dados_brownfield_rsi.xlsx antes de gerar a apresentação."
        )
    wb = openpyxl.load_workbook(caminho_xlsx, data_only=True)
    abas = {}
    for nome in wb.sheetnames:
        ws = wb[nome]
        linhas = list(ws.iter_rows(values_only=True))
        if not linhas:
            abas[nome] = []
            continue
        cabecalho = linhas[0]
        abas[nome] = [dict(zip(cabecalho, linha)) for linha in linhas[1:]]
    return abas


def _mes_mais_recente(linhas_tendencia):
    """O 'mês vigente' é o maior mes_num presente na aba Tendencia."""
    return max((l["mes_num"] for l in linhas_tendencia), default=datetime.now().month)


def _card_tendencia(linhas_tendencia, tipo, regional, canal, mes_num):
    """Acha a linha de Tendencia (tipo/regional/canal/mês) e monta o card
    pronto pra slide: real do mês, tendência de fechamento, acumulado
    jan-mês e o progresso em dias úteis."""
    linha = next(
        (l for l in linhas_tendencia
         if l["tipo"] == tipo and l["regional"] == regional and l["canal"] == canal and l["mes_num"] == mes_num),
        None,
    )
    if linha is None:
        return None

    acumulado_real = sum(
        l["realizado"] for l in linhas_tendencia
        if l["tipo"] == tipo and l["regional"] == regional and l["canal"] == canal and l["mes_num"] <= mes_num
    )
    acumulado_forecast = sum(
        (l["forecast_mensal"] or 0) for l in linhas_tendencia
        if l["tipo"] == tipo and l["regional"] == regional and l["canal"] == canal and l["mes_num"] <= mes_num
    )
    delta_acumulado = (
        (acumulado_real / acumulado_forecast - 1) if acumulado_forecast else None
    )

    return {
        "mes": linha["mes"],
        "real_mes": linha["realizado"],
        "meta_proporcional": linha["meta_proporcional"],
        "delta_real_vs_meta": linha["delta_real_vs_meta_prop"],
        "tendencia_fechamento": linha["tendencia"],
        "forecast_mensal": linha["forecast_mensal"],
        "delta_tendencia_vs_forecast": linha["delta_tendencia_vs_forecast"],
        "acumulado_real": acumulado_real,
        "acumulado_forecast": acumulado_forecast,
        "delta_acumulado": delta_acumulado,
        "dias_uteis_decorridos": linha["dias_uteis_decorridos"],
        "dias_uteis_totais": linha["dias_uteis_totais"],
    }


def montar_resumo(linhas_tendencia, regional, canal, mes_num, titulo, subtitulo_filtro):
    """Monta o par de cards (Geração + Execução) de uma das 3 slides de
    resumo (RSI / Célula Local / Brasil)."""
    return {
        "titulo": titulo,
        "subtitulo_filtro": subtitulo_filtro,
        "geracao": _card_tendencia(linhas_tendencia, "Geracao", regional, canal, mes_num),
        "execucao": _card_tendencia(linhas_tendencia, "Execucao", regional, canal, mes_num),
    }


def montar_tabela_canal(linhas_tendencia, tipo, regional, mes_num):
    """Tabela 'canal x mês' (linhas do exemplo: OS Geradas/Executadas por
    canal, Brasil ou RSI), com TOTAL, tendência e forecast do mês vigente."""
    canais_ordem = [
        "AGENTE AUTORIZADO", "CÉLULA LOCAL", "DAC", "E-COMMERCE", "LOJA PRÓPRIA",
        "NÃO IDENTIFICADO", "OUTROS", "PORTA A PORTA", "QUALIDADE REGIONAL",
        "TLV ATIVO", "TLV RECEPTIVO", "TOTAL",
    ]
    linhas_regional = [l for l in linhas_tendencia if l["tipo"] == tipo and l["regional"] == regional]
    canais_presentes = {l["canal"] for l in linhas_regional}

    linhas_saida = []
    for canal in canais_ordem:
        if canal not in canais_presentes:
            continue
        por_mes = {l["mes_num"]: l["realizado"] for l in linhas_regional if l["canal"] == canal}
        linha_mes_vigente = next((l for l in linhas_regional if l["canal"] == canal and l["mes_num"] == mes_num), None)
        total_ano = sum(por_mes.get(m, 0) or 0 for m in range(1, mes_num + 1))
        linhas_saida.append({
            "canal": canal,
            "valores_por_mes": [por_mes.get(m) for m in range(1, mes_num + 1)],
            "total": total_ano,
            "tendencia_mes_vigente": linha_mes_vigente["tendencia"] if linha_mes_vigente else None,
            "forecast_mes_vigente": linha_mes_vigente["forecast_mensal"] if linha_mes_vigente else None,
            "delta_tendencia_forecast": linha_mes_vigente["delta_tendencia_vs_forecast"] if linha_mes_vigente else None,
            "destaque": canal == "TOTAL",
        })
    return {
        "meses": MESES_ABREV[:mes_num],
        "linhas": linhas_saida,
    }


def montar_quebra_mensal_agrupamento(linhas_mensal, linhas_agrupamento, mes_num):
    """Monta o gráfico empilhado (% por agrupamento e mês, com o total do
    mês) + a tabela 'por agrupamento' das slides de Quebra de Agenda."""
    meses_presentes = sorted({l["mes_num"] for l in linhas_mensal if l["mes_num"] <= mes_num})
    total_por_mes = {l["mes_num"]: l["pct_quebra"] for l in linhas_mensal}

    agrupamentos = sorted({l["agrupamento"] for l in linhas_agrupamento})
    series = []
    for agr in agrupamentos:
        valores = []
        for m in meses_presentes:
            linha = next((l for l in linhas_agrupamento if l["agrupamento"] == agr and l["mes_num"] == m), None)
            valores.append(round((linha["pct"] * 100), 2) if linha else 0)
        series.append({"nome": agr.title(), "valores": valores})

    tabela = []
    for agr in agrupamentos:
        valores_pct = []
        contagem_total_ano = 0
        for m in meses_presentes:
            linha = next((l for l in linhas_agrupamento if l["agrupamento"] == agr and l["mes_num"] == m), None)
            valores_pct.append(round(linha["pct"] * 100, 1) if linha else None)
            if linha:
                contagem_total_ano += linha["contagem"]
        media = sum(v for v in valores_pct if v is not None) / max(1, len([v for v in valores_pct if v is not None]))
        var_ultimo_vs_penultimo = None
        if len(valores_pct) >= 2 and valores_pct[-1] is not None and valores_pct[-2] is not None:
            var_ultimo_vs_penultimo = round(valores_pct[-1] - valores_pct[-2], 1)
        tabela.append({
            "agrupamento": agr.title(),
            "valores_pct": valores_pct,
            "total_ano": contagem_total_ano,
            "media_pct": round(media, 1),
            "variacao_pp": var_ultimo_vs_penultimo,
        })
    tabela.sort(key=lambda r: r["media_pct"], reverse=True)

    return {
        "meses": [MESES_ABREV[m - 1] for m in meses_presentes],
        "total_mensal_pct": [round((total_por_mes.get(m, 0) or 0) * 100, 1) for m in meses_presentes],
        "series": series,
        "tabela": tabela,
    }


def montar_quebra_ddd(linhas_ddd_mensal, mes_num):
    """Tabela '% quebra por DDD e mês' da última slide de quebra."""
    ddds = sorted({l["ddd"] for l in linhas_ddd_mensal})
    meses_presentes = sorted({l["mes_num"] for l in linhas_ddd_mensal if l["mes_num"] <= mes_num})
    linhas = []
    for ddd in ddds:
        valores = []
        for m in meses_presentes:
            linha = next((l for l in linhas_ddd_mensal if l["ddd"] == ddd and l["mes_num"] == m), None)
            valores.append(round(linha["pct_quebra"] * 100, 1) if linha else None)
        media = sum(v for v in valores if v is not None) / max(1, len([v for v in valores if v is not None]))
        linhas.append({"ddd": ddd, "valores_pct": valores, "media_pct": round(media, 1)})
    linhas.sort(key=lambda r: r["media_pct"], reverse=True)
    return {
        "meses": [MESES_ABREV[m - 1] for m in meses_presentes],
        "linhas": linhas,
    }


def gerar_diagnosticos(resumo_rsi, resumo_celula, resumo_brasil, quebra_rsi, quebra_ddd):
    """Analisa os números já calculados e devolve uma lista curta de
    diagnósticos + recomendações -- tudo derivado dos dados (sem texto
    fixo), no espírito da seção 'Impactos' do exemplo."""
    pontos = []

    def _fmt_pct(v):
        return f"{v * 100:+.1f}%".replace(".", ",") if v is not None else "n/d"

    exec_rsi = resumo_rsi["execucao"]
    if exec_rsi and exec_rsi["delta_tendencia_vs_forecast"] is not None:
        if exec_rsi["delta_tendencia_vs_forecast"] < -0.05:
            pontos.append({
                "diagnostico": (
                    f"Execução RSI com tendência de fechar {_fmt_pct(exec_rsi['delta_tendencia_vs_forecast'])} "
                    f"abaixo do forecast de {exec_rsi['mes']} "
                    f"({exec_rsi['dias_uteis_decorridos']}/{exec_rsi['dias_uteis_totais']} dias úteis decorridos)."
                ),
                "recomendacao": "Reforçar acompanhamento diário de execução por regional/canal para reverter a tendência antes do fechamento do mês.",
            })
        elif exec_rsi["delta_tendencia_vs_forecast"] > 0.05:
            pontos.append({
                "diagnostico": (
                    f"Execução RSI acima do forecast em {exec_rsi['mes']} "
                    f"({_fmt_pct(exec_rsi['delta_tendencia_vs_forecast'])} de tendência sobre o forecast)."
                ),
                "recomendacao": "Manter o ritmo atual e monitorar se a geração acompanha o volume de execução.",
            })

    gera_rsi = resumo_rsi["geracao"]
    if gera_rsi and gera_rsi["delta_acumulado"] is not None and gera_rsi["delta_acumulado"] < -0.05:
        pontos.append({
            "diagnostico": (
                f"Geração RSI acumulada no ano {_fmt_pct(gera_rsi['delta_acumulado'])} abaixo do forecast acumulado."
            ),
            "recomendacao": "Investigar canais com maior desvio (ver tabela de canais) e priorizar ações de captação nesses canais.",
        })

    if quebra_rsi["tabela"]:
        pior = quebra_rsi["tabela"][0]
        pontos.append({
            "diagnostico": (
                f"'{pior['agrupamento']}' é o principal motivo de quebra de agenda na RSI no ano "
                f"(média de {str(pior['media_pct']).replace('.', ',')}% dos agendamentos)."
            ),
            "recomendacao": f"Priorizar plano de ação específico para '{pior['agrupamento']}', maior alavanca de redução de quebra.",
        })
        # Motivo com maior variação (piora) no último mês
        pioras = [l for l in quebra_rsi["tabela"] if l["variacao_pp"] and l["variacao_pp"] > 0]
        if pioras:
            maior_piora = max(pioras, key=lambda l: l["variacao_pp"])
            if maior_piora["agrupamento"] != pior["agrupamento"]:
                pontos.append({
                    "diagnostico": (
                        f"'{maior_piora['agrupamento']}' foi o agrupamento que mais piorou no último mês "
                        f"(+{str(maior_piora['variacao_pp']).replace('.', ',')} p.p. frente ao mês anterior)."
                    ),
                    "recomendacao": "Validar se a piora é pontual (evento isolado) ou tendência, antes de redesenhar o plano de ação.",
                })

    if quebra_ddd["linhas"]:
        pior_ddd = quebra_ddd["linhas"][0]
        pontos.append({
            "diagnostico": f"DDD {pior_ddd['ddd']} é o de maior quebra média no período ({str(pior_ddd['media_pct']).replace('.', ',')}%).",
            "recomendacao": f"Direcionar apoio operacional prioritário ao DDD {pior_ddd['ddd']} (agenda, infraestrutura e efetivo local).",
        })

    return pontos[:6]


def montar_dados_apresentacao(caminho_xlsx):
    """Ponto de entrada: lê o xlsx e devolve TODO o conteúdo já calculado
    pras slides, pronto pra passar pro gerador do pptx."""
    abas = _ler_abas(caminho_xlsx)
    tendencia = abas.get("Tendencia", [])
    if not tendencia:
        raise DadosIndisponiveisError("Aba 'Tendencia' vazia ou ausente no xlsx do painel.")

    mes_num = _mes_mais_recente(tendencia)
    mes_nome = MESES_ABREV[mes_num - 1]

    resumo_rsi = montar_resumo(tendencia, "RSI", "TOTAL", mes_num, "Migrações RSI — Resumo", "Regional: RSI · Canal: Todos")
    resumo_celula = montar_resumo(tendencia, "TODAS", "CÉLULA LOCAL", mes_num, "Célula Local — Resumo", "Canal: Célula Local · Brasil")
    resumo_brasil = montar_resumo(tendencia, "TODAS", "TOTAL", mes_num, "Regionais — Resumo", "Regional: Todas · Canal: Todos")

    tabela_canal_brasil_geracao = montar_tabela_canal(tendencia, "Geracao", "TODAS", mes_num)
    tabela_canal_brasil_execucao = montar_tabela_canal(tendencia, "Execucao", "TODAS", mes_num)
    tabela_canal_rsi_geracao = montar_tabela_canal(tendencia, "Geracao", "RSI", mes_num)
    tabela_canal_rsi_execucao = montar_tabela_canal(tendencia, "Execucao", "RSI", mes_num)

    quebra_rsi = montar_quebra_mensal_agrupamento(
        abas.get("Quebra_RSI_Mensal", []), abas.get("Quebra_RSI_Agrupamento", []), mes_num,
    )
    quebra_celula = montar_quebra_mensal_agrupamento(
        abas.get("Quebra_Celula_Mensal", []), abas.get("Quebra_Celula_Agrupamento", []), mes_num,
    )
    quebra_ddd = montar_quebra_ddd(abas.get("Quebra_DDD_Mensal", []), mes_num)

    diagnosticos = gerar_diagnosticos(resumo_rsi, resumo_celula, resumo_brasil, quebra_rsi, quebra_ddd)

    # "Dados até": a data da última atualização da base (mtime do xlsx), não
    # o momento em que o ADM clicou pra gerar o PPT -- é o que aparece nas
    # slides como referência de corte dos números.
    dados_ate = datetime.fromtimestamp(os.path.getmtime(caminho_xlsx))

    return {
        "gerado_em": datetime.now(),
        "dados_ate": dados_ate,
        "mes_num": mes_num,
        "mes_nome": mes_nome,
        "dias_uteis_decorridos": resumo_rsi["execucao"]["dias_uteis_decorridos"] if resumo_rsi["execucao"] else None,
        "dias_uteis_totais": resumo_rsi["execucao"]["dias_uteis_totais"] if resumo_rsi["execucao"] else None,
        "resumo_rsi": resumo_rsi,
        "resumo_celula": resumo_celula,
        "resumo_brasil": resumo_brasil,
        "tabela_canal_brasil_geracao": tabela_canal_brasil_geracao,
        "tabela_canal_brasil_execucao": tabela_canal_brasil_execucao,
        "tabela_canal_rsi_geracao": tabela_canal_rsi_geracao,
        "tabela_canal_rsi_execucao": tabela_canal_rsi_execucao,
        "quebra_rsi": quebra_rsi,
        "quebra_celula": quebra_celula,
        "quebra_ddd": quebra_ddd,
        "diagnosticos": diagnosticos,
    }
