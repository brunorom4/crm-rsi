# -*- coding: utf-8 -*-
"""
Monta o arquivo .pptx da apresentação executiva de resultados, a partir do
dicionário produzido por gerar_apresentacao.montar_dados_apresentacao().

Uso:
    from gerar_apresentacao import montar_dados_apresentacao
    from gerar_apresentacao_pptx import montar_apresentacao_pptx

    dados = montar_dados_apresentacao("painel_dados/dados_brownfield_rsi.xlsx")
    montar_apresentacao_pptx(dados, "saida.pptx")
"""
import os

from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.enum.shapes import MSO_SHAPE
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION, XL_TICK_LABEL_POSITION, XL_LABEL_POSITION
from pptx.oxml.ns import qn

# ---------------------------------------------------------------------------
# Paleta oficial da marca Claro (vermelho/preto/branco -- manual de marca)
# + variações de grafite/cinza pra dar contraste e diferenciação nos
# gráficos sem fugir da identidade. Visual "fibra ótica": fundo escuro com
# feixes de luz vermelhos na capa, acento discreto nas slides de conteúdo.
# Tipografia seguindo o padrão de fontes seguras (renderiza igual no
# PowerPoint real e no LibreOffice usado pra QA).
# ---------------------------------------------------------------------------
VERMELHO = RGBColor(0xDA, 0x29, 0x1C)       # vermelho oficial Claro
VERMELHO_ESCURO = RGBColor(0x8C, 0x19, 0x13)
VERMELHO_CLARO = RGBColor(0xF2, 0xA2, 0x9C)
PRETO = RGBColor(0x0A, 0x09, 0x09)
GRAFITE = RGBColor(0x1E, 0x1E, 0x1E)
GRAFITE_MEDIO = RGBColor(0x3A, 0x3A, 0x3A)
OFF_WHITE = RGBColor(0xF7, 0xF7, 0xF8)
CINZA_ESCURO = RGBColor(0x24, 0x24, 0x24)
CINZA_MEDIO = RGBColor(0x6B, 0x6B, 0x6B)
CINZA_CLARO = RGBColor(0xE2, 0xE2, 0xE3)
BRANCO = RGBColor(0xFF, 0xFF, 0xFF)
VERDE = RGBColor(0x1D, 0x7A, 0x46)
VERMELHO_CLARO_BG = RGBColor(0xFC, 0xE7, 0xE5)

FONTE_TITULO = "Arial"
FONTE_CORPO = "Calibri"

# Família vermelho/preto/cinza -- mantém a identidade Claro mesmo com várias
# séries no gráfico de quebra de agenda.
PALETA_GRAFICO = [
    "DA291C", "1E1E1E", "F2A29C", "6B6B6B", "8C1913", "B9B9B9", "4A4A4A",
]

LARGURA = Inches(13.333)
ALTURA = Inches(7.5)
MARGEM = Inches(0.5)

PASTA_ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pptx_assets")
IMG_CAPA_FUNDO = os.path.join(PASTA_ASSETS, "capa_fundo.jpg")
IMG_CANTO_FIBRA = os.path.join(PASTA_ASSETS, "canto_fibra.png")


def _luminancia(rgbcolor):
    r, g, b = rgbcolor[0] / 255, rgbcolor[1] / 255, rgbcolor[2] / 255
    return 0.299 * r + 0.587 * g + 0.114 * b


def _cor_texto_sobre(rgbcolor):
    """Branco ou grafite, o que tiver mais contraste sobre a cor de fundo."""
    return BRANCO if _luminancia(rgbcolor) < 0.55 else GRAFITE


def _fmt_int(n):
    if n is None:
        return "—"
    return f"{round(n):,}".replace(",", ".")


def _fmt_pct(v, casas=1):
    if v is None:
        return "n/d"
    return f"{v * 100:+.{casas}f}%".replace(".", ",")


def _fmt_pct_abs(v, casas=1):
    if v is None:
        return "n/d"
    return f"{v:.{casas}f}%".replace(".", ",")


def _cor_delta(v):
    if v is None:
        return CINZA_MEDIO
    return VERDE if v >= 0 else VERMELHO


def _set_background(slide, cor):
    fundo = slide.background
    fundo.fill.solid()
    fundo.fill.fore_color.rgb = cor


def _add_textbox(slide, left, top, width, height, texto, tamanho=14, cor=CINZA_ESCURO,
                  negrito=False, fonte=FONTE_CORPO, alinhamento=PP_ALIGN.LEFT, italico=False,
                  espacamento=1.0, anchor=None):
    caixa = slide.shapes.add_textbox(left, top, width, height)
    tf = caixa.text_frame
    tf.word_wrap = True
    tf.margin_left = 0
    tf.margin_right = 0
    tf.margin_top = 0
    tf.margin_bottom = 0
    if anchor is not None:
        tf.vertical_anchor = anchor
    p = tf.paragraphs[0]
    p.alignment = alinhamento
    p.line_spacing = espacamento
    run = p.add_run()
    run.text = texto
    run.font.size = Pt(tamanho)
    run.font.bold = negrito
    run.font.italic = italico
    run.font.name = fonte
    run.font.color.rgb = cor
    return caixa


def _add_rect(slide, left, top, width, height, cor_preenchimento, cor_borda=None, arredondado=False, sombra=False):
    forma = slide.shapes.add_shape(
        MSO_SHAPE.ROUNDED_RECTANGLE if arredondado else MSO_SHAPE.RECTANGLE,
        left, top, width, height,
    )
    if arredondado:
        try:
            forma.adjustments[0] = 0.06
        except Exception:
            pass
    forma.fill.solid()
    forma.fill.fore_color.rgb = cor_preenchimento
    if cor_borda is None:
        forma.line.fill.background()
    else:
        forma.line.color.rgb = cor_borda
        forma.line.width = Pt(0.75)
    forma.shadow.inherit = False
    if sombra:
        sp = forma.shadow._element  # noqa: SLF001 -- pptx não expõe API de sombra "solta"
    return forma


def _rodape(slide, dados, numero_pagina, titulo_secao=""):
    _add_textbox(
        slide, MARGEM, ALTURA - Inches(0.38), Inches(8), Inches(0.3),
        f"Migração Fibra RSI · Dados até {dados['dados_ate'].strftime('%d/%m')} · {titulo_secao}",
        tamanho=9, cor=CINZA_MEDIO, fonte=FONTE_CORPO,
    )
    _add_textbox(
        slide, LARGURA - Inches(0.9), ALTURA - Inches(0.38), Inches(0.5), Inches(0.3),
        str(numero_pagina), tamanho=9, cor=CINZA_MEDIO, alinhamento=PP_ALIGN.RIGHT,
    )


def _acento_canto(slide, tamanho=Inches(2.6)):
    """Acento discreto de fibra ótica no canto inferior direito -- reforça
    o tema visual sem competir com o conteúdo (fica atrás de tudo, e o
    canto ali é sempre vazio: fora da área de tabelas/gráficos/rodapé)."""
    if os.path.exists(IMG_CANTO_FIBRA):
        slide.shapes.add_picture(
            IMG_CANTO_FIBRA, LARGURA - tamanho + Inches(0.15), ALTURA - tamanho + Inches(0.15),
            width=tamanho, height=tamanho,
        )


def _cabecalho_slide(slide, titulo, subtitulo=""):
    _add_textbox(slide, MARGEM, Inches(0.32), Inches(10.5), Inches(0.55), titulo,
                 tamanho=28, negrito=True, cor=CINZA_ESCURO, fonte=FONTE_TITULO)
    if subtitulo:
        _add_textbox(slide, MARGEM, Inches(0.86), Inches(10.5), Inches(0.35), subtitulo,
                     tamanho=13, cor=CINZA_MEDIO, fonte=FONTE_CORPO, italico=True)


# ---------------------------------------------------------------------------
# Slide 1 — Capa
# ---------------------------------------------------------------------------
def _slide_capa(prs, dados):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _set_background(slide, PRETO)

    if os.path.exists(IMG_CAPA_FUNDO):
        slide.shapes.add_picture(IMG_CAPA_FUNDO, 0, 0, width=LARGURA, height=ALTURA)

    # selo "CLARO" discreto, canto superior esquerdo -- reforça a marca sem
    # depender de um arquivo de logo externo (que eu não tenho aqui).
    _add_textbox(slide, Inches(0.7), Inches(0.5), Inches(3), Inches(0.4), "CLARO",
                 tamanho=16, negrito=True, cor=BRANCO, fonte=FONTE_TITULO, espacamento=1.0)

    _add_textbox(slide, Inches(0.7), Inches(2.7), Inches(11.8), Inches(1.3),
                 "Migração Fibra — Resultados RSI", tamanho=46, negrito=True, cor=BRANCO, fonte=FONTE_TITULO)
    _add_textbox(slide, Inches(0.7), Inches(3.75), Inches(11), Inches(0.6),
                 f"Geração, Execução e Quebra de Agenda · Jan–{dados['mes_nome']}/{dados['dados_ate'].year}",
                 tamanho=18, cor=VERMELHO_CLARO, fonte=FONTE_CORPO)

    _add_textbox(slide, Inches(0.7), ALTURA - Inches(1.05), Inches(9), Inches(0.6),
                 f"Dados até {dados['dados_ate'].strftime('%d/%m/%Y')}  ·  "
                 f"{dados['dias_uteis_decorridos']}/{dados['dias_uteis_totais']} dias úteis do mês",
                 tamanho=14, cor=BRANCO, fonte=FONTE_CORPO)
    return slide


# ---------------------------------------------------------------------------
# Slides 2-4 — Resumo (cards Geração/Execução)
# ---------------------------------------------------------------------------
def _tile_metrica(slide, left, top, width, height, rotulo, valor, delta=None, delta_rotulo=None, cor_valor=CINZA_ESCURO):
    _add_rect(slide, left, top, width, height, OFF_WHITE, arredondado=True)
    _add_textbox(slide, left + Inches(0.15), top + Inches(0.12), width - Inches(0.3), Inches(0.3),
                 rotulo.upper(), tamanho=9.5, negrito=True, cor=CINZA_MEDIO, fonte=FONTE_CORPO)
    _add_textbox(slide, left + Inches(0.15), top + Inches(0.42), width - Inches(0.3), Inches(0.55),
                 valor, tamanho=24, negrito=True, cor=cor_valor, fonte=FONTE_TITULO)
    if delta is not None:
        _add_textbox(slide, left + Inches(0.15), top + height - Inches(0.34), width - Inches(0.3), Inches(0.28),
                     f"{_fmt_pct(delta)} {delta_rotulo or ''}".strip(), tamanho=10.5, negrito=True,
                     cor=_cor_delta(delta), fonte=FONTE_CORPO)


def _bloco_geracao_execucao(slide, top, card, rotulo_secao, cor_secao):
    bolinha = slide.shapes.add_shape(MSO_SHAPE.OVAL, MARGEM, top + Inches(0.03), Inches(0.16), Inches(0.16))
    bolinha.fill.solid()
    bolinha.fill.fore_color.rgb = cor_secao
    bolinha.line.fill.background()
    bolinha.shadow.inherit = False
    _add_textbox(slide, MARGEM + Inches(0.28), top, Inches(4), Inches(0.35), rotulo_secao,
                 tamanho=15, negrito=True, cor=cor_secao, fonte=FONTE_TITULO)

    largura_tile = Inches(2.95)
    espaco = Inches(0.2)
    altura_tile = Inches(1.5)
    top_tiles = top + Inches(0.42)

    if card is None:
        _add_textbox(slide, MARGEM + Inches(0.25), top_tiles, Inches(9), Inches(0.4),
                     "Sem dados suficientes para este recorte no mês vigente.", tamanho=12, cor=CINZA_MEDIO)
        return

    _tile_metrica(
        slide, MARGEM + Inches(0.25), top_tiles, largura_tile, altura_tile,
        f"Real ({card['mes']})", _fmt_int(card["real_mes"]),
        delta=card["delta_real_vs_meta"], delta_rotulo="vs. meta do período",
    )
    _tile_metrica(
        slide, MARGEM + Inches(0.25) + (largura_tile + espaco), top_tiles, largura_tile, altura_tile,
        "Tendência (fim do mês)", _fmt_int(card["tendencia_fechamento"]),
        delta=card["delta_tendencia_vs_forecast"], delta_rotulo="vs. forecast",
    )
    _tile_metrica(
        slide, MARGEM + Inches(0.25) + 2 * (largura_tile + espaco), top_tiles, largura_tile, altura_tile,
        "Acumulado Jan–" + card["mes"], _fmt_int(card["acumulado_real"]),
        delta=card["delta_acumulado"], delta_rotulo="vs. forecast acum.",
    )
    _tile_metrica(
        slide, MARGEM + Inches(0.25) + 3 * (largura_tile + espaco), top_tiles, largura_tile, altura_tile,
        "Progresso do mês", f"{card['dias_uteis_decorridos']}/{card['dias_uteis_totais']} dias úteis",
        cor_valor=GRAFITE,
    )


def _slide_resumo(prs, dados, chave, numero_pagina):
    resumo = dados[chave]
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _set_background(slide, BRANCO)
    _acento_canto(slide)
    _cabecalho_slide(slide, resumo["titulo"], resumo["subtitulo_filtro"])

    _bloco_geracao_execucao(slide, Inches(1.55), resumo["geracao"], "OS Geradas", VERMELHO)
    _bloco_geracao_execucao(slide, Inches(3.75), resumo["execucao"], "OS Executadas", GRAFITE)

    _rodape(slide, dados, numero_pagina, resumo["titulo"])
    return slide


# ---------------------------------------------------------------------------
# Slides 5-6 — Tabelas de canal
# ---------------------------------------------------------------------------
def _slide_tabela_canal(prs, dados, titulo, subtitulo, tabela_geracao, tabela_execucao, numero_pagina):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _set_background(slide, BRANCO)
    _cabecalho_slide(slide, titulo, subtitulo)

    _add_textbox(slide, MARGEM, Inches(1.28), Inches(6), Inches(0.28), "OS GERADAS",
                 tamanho=13, negrito=True, cor=VERMELHO, fonte=FONTE_TITULO)
    _tabela_canal(slide, MARGEM, Inches(1.58), LARGURA - 2 * MARGEM, Inches(2.55), tabela_geracao)

    _add_textbox(slide, MARGEM, Inches(4.18), Inches(6), Inches(0.28), "OS EXECUTADAS",
                 tamanho=13, negrito=True, cor=GRAFITE, fonte=FONTE_TITULO)
    _tabela_canal(slide, MARGEM, Inches(4.48), LARGURA - 2 * MARGEM, Inches(2.55), tabela_execucao)

    _rodape(slide, dados, numero_pagina, titulo)
    return slide


def _sombrear_celula(celula, cor):
    celula.fill.solid()
    celula.fill.fore_color.rgb = cor


def _texto_celula(celula, texto, tamanho=8.5, negrito=False, cor=CINZA_ESCURO, alinhamento=PP_ALIGN.RIGHT):
    celula.margin_left = Inches(0.04)
    celula.margin_right = Inches(0.04)
    celula.margin_top = Inches(0.02)
    celula.margin_bottom = Inches(0.02)
    celula.vertical_anchor = MSO_ANCHOR.MIDDLE
    tf = celula.text_frame
    tf.word_wrap = False
    p = tf.paragraphs[0]
    p.alignment = alinhamento
    run = p.add_run() if not p.runs else p.runs[0]
    run.text = texto
    run.font.size = Pt(tamanho)
    run.font.bold = negrito
    run.font.name = FONTE_CORPO
    run.font.color.rgb = cor


def _tabela_canal(slide, left, top, width, height, tabela):
    meses = tabela["meses"]
    colunas = ["Canal"] + meses + ["Total", "Tend.", "Forecast", "Δ Tend."]
    n_linhas = len(tabela["linhas"]) + 1
    n_colunas = len(colunas)

    grafico = slide.shapes.add_table(n_linhas, n_colunas, left, top, width, height)
    tabela_gfx = grafico.table
    tabela_gfx.first_row = False
    tabela_gfx.horz_banding = False

    # add_table() só usa "height" pra distribuir a altura inicial das linhas
    # -- o LibreOffice/PowerPoint re-expande cada linha pro mínimo exigido
    # pela fonte, ignorando esse total. Fixando a altura de cada linha aqui
    # é o jeito de garantir que a tabela toda caiba no espaço reservado.
    altura_linha = int(height / n_linhas)
    for linha_tabela in tabela_gfx.rows:
        linha_tabela.height = altura_linha

    largura_canal = Inches(1.7)
    largura_mes = (width - largura_canal - Inches(2.7)) / max(1, len(meses))
    tabela_gfx.columns[0].width = int(largura_canal)
    for i in range(len(meses)):
        tabela_gfx.columns[1 + i].width = int(largura_mes)
    for i, w in enumerate([Inches(0.75), Inches(0.65), Inches(0.75), Inches(0.55)]):
        tabela_gfx.columns[1 + len(meses) + i].width = int(w)

    for j, titulo_col in enumerate(colunas):
        cel = tabela_gfx.cell(0, j)
        _sombrear_celula(cel, GRAFITE)
        _texto_celula(cel, titulo_col, tamanho=8.5, negrito=True, cor=BRANCO,
                      alinhamento=PP_ALIGN.LEFT if j == 0 else PP_ALIGN.RIGHT)

    for i, linha in enumerate(tabela["linhas"], start=1):
        destaque = linha.get("destaque")
        cor_fundo = RGBColor(0xF3, 0xE7, 0xE8) if destaque else (OFF_WHITE if i % 2 == 0 else BRANCO)
        cel = tabela_gfx.cell(i, 0)
        _sombrear_celula(cel, cor_fundo)
        _texto_celula(cel, linha["canal"].title(), tamanho=8.5, negrito=bool(destaque), alinhamento=PP_ALIGN.LEFT)

        for j, valor in enumerate(linha["valores_por_mes"]):
            cel = tabela_gfx.cell(i, 1 + j)
            _sombrear_celula(cel, cor_fundo)
            _texto_celula(cel, _fmt_int(valor) if valor is not None else "—", tamanho=8.5, negrito=bool(destaque))

        cel = tabela_gfx.cell(i, 1 + len(meses))
        _sombrear_celula(cel, cor_fundo)
        _texto_celula(cel, _fmt_int(linha["total"]), tamanho=8.5, negrito=True)

        cel = tabela_gfx.cell(i, 2 + len(meses))
        _sombrear_celula(cel, cor_fundo)
        _texto_celula(cel, _fmt_int(linha["tendencia_mes_vigente"]), tamanho=8.5)

        cel = tabela_gfx.cell(i, 3 + len(meses))
        _sombrear_celula(cel, cor_fundo)
        _texto_celula(cel, _fmt_int(linha["forecast_mes_vigente"]), tamanho=8.5)

        cel = tabela_gfx.cell(i, 4 + len(meses))
        _sombrear_celula(cel, cor_fundo)
        _texto_celula(cel, _fmt_pct(linha["delta_tendencia_forecast"], casas=0), tamanho=8.5, negrito=True,
                      cor=_cor_delta(linha["delta_tendencia_forecast"]))


# ---------------------------------------------------------------------------
# Slides 7-8 — Quebra de agenda (gráfico + tabela)
# ---------------------------------------------------------------------------
def _grafico_quebra(slide, left, top, width, height, quebra):
    # Agrega em "top 6 + Outros" pra não poluir o gráfico com 10 séries.
    tabela_ordenada = quebra["tabela"]
    nomes_top = [l["agrupamento"] for l in tabela_ordenada[:6]]
    dados_grafico = CategoryChartData()
    dados_grafico.categories = quebra["meses"]

    serie_por_nome = {s["nome"]: s["valores"] for s in quebra["series"]}
    for nome in nomes_top:
        dados_grafico.add_series(nome, serie_por_nome.get(nome, [0] * len(quebra["meses"])))

    outros_nomes = [n for n in serie_por_nome if n not in nomes_top]
    if outros_nomes:
        soma_outros = [
            round(sum(serie_por_nome[n][i] for n in outros_nomes), 2)
            for i in range(len(quebra["meses"]))
        ]
        dados_grafico.add_series("Outros", soma_outros)

    grafico_shape = slide.shapes.add_chart(
        XL_CHART_TYPE.COLUMN_STACKED, left, top, width, height, dados_grafico,
    )
    chart = grafico_shape.chart
    chart.has_title = True
    chart.chart_title.text_frame.text = "% de quebra por agrupamento (barras empilhadas)"
    chart.chart_title.text_frame.paragraphs[0].runs[0].font.size = Pt(11)
    chart.chart_title.text_frame.paragraphs[0].runs[0].font.bold = True
    chart.chart_title.text_frame.paragraphs[0].runs[0].font.color.rgb = CINZA_ESCURO

    chart.has_legend = True
    chart.legend.position = XL_LEGEND_POSITION.BOTTOM
    chart.legend.include_in_layout = False
    chart.legend.font.size = Pt(8.5)

    # Desliga os rótulos "automáticos" (valor cru, sem formatação) em todo
    # o gráfico -- só mostramos rótulo onde explicitamente montamos um
    # abaixo, senão o LibreOffice/PowerPoint preenche com o valor bruto
    # (ex.: "1.08" em vez de "1%") nos pontos que a gente queria omitir.
    plot = chart.plots[0]
    plot.has_data_labels = True
    rotulos_padrao = plot.data_labels
    rotulos_padrao.show_value = False
    rotulos_padrao.show_percentage = False
    rotulos_padrao.show_category_name = False
    rotulos_padrao.show_series_name = False
    rotulos_padrao.show_legend_key = False

    limiar_rotulo = 3.0  # abaixo disso o número não cabe no pedaço da barra -- omite
    for i, serie in enumerate(plot.series):
        cor_serie = RGBColor.from_string(PALETA_GRAFICO[i % len(PALETA_GRAFICO)])
        serie.format.fill.solid()
        serie.format.fill.fore_color.rgb = cor_serie
        cor_rotulo = _cor_texto_sobre(cor_serie)

        valores = list(serie.values)
        for j, ponto in enumerate(serie.points):
            valor = valores[j] if j < len(valores) else None
            if valor is None or valor < limiar_rotulo:
                continue  # sem override aqui = herda o padrão do plot (tudo False, some)
            rotulo = ponto.data_label
            rotulo.has_text_frame = True
            tf_rotulo = rotulo.text_frame
            tf_rotulo.word_wrap = False
            p_rotulo = tf_rotulo.paragraphs[0]
            run_rotulo = p_rotulo.add_run()
            run_rotulo.text = f"{valor:.0f}%".replace(".", ",")
            run_rotulo.font.size = Pt(7.5)
            run_rotulo.font.bold = True
            run_rotulo.font.color.rgb = cor_rotulo
            run_rotulo.font.name = FONTE_CORPO
            rotulo.position = XL_LABEL_POSITION.CENTER

    cat_axis = chart.category_axis
    cat_axis.tick_labels.font.size = Pt(9)
    cat_axis.format.line.color.rgb = CINZA_CLARO

    val_axis = chart.value_axis
    val_axis.tick_labels.font.size = Pt(8.5)
    val_axis.has_major_gridlines = True
    val_axis.major_gridlines.format.line.color.rgb = CINZA_CLARO
    val_axis.major_gridlines.format.line.width = Pt(0.5)
    return grafico_shape


def _tabela_quebra_agrupamento(slide, left, top, width, height, quebra, max_linhas=7):
    meses = quebra["meses"]
    colunas = ["Agrupamento"] + meses + ["Média", "Var. p.p."]
    linhas_dados = quebra["tabela"][:max_linhas]
    n_linhas = len(linhas_dados) + 1
    n_colunas = len(colunas)

    grafico = slide.shapes.add_table(n_linhas, n_colunas, left, top, width, height)
    tabela_gfx = grafico.table
    tabela_gfx.first_row = False
    tabela_gfx.horz_banding = False

    altura_linha = int(height / n_linhas)
    for linha_tabela in tabela_gfx.rows:
        linha_tabela.height = altura_linha

    largura_nome = Inches(1.55)
    largura_mes = (width - largura_nome - Inches(1.2)) / max(1, len(meses))
    tabela_gfx.columns[0].width = int(largura_nome)
    for i in range(len(meses)):
        tabela_gfx.columns[1 + i].width = int(largura_mes)
    tabela_gfx.columns[1 + len(meses)].width = int(Inches(0.65))
    tabela_gfx.columns[2 + len(meses)].width = int(Inches(0.55))

    for j, titulo_col in enumerate(colunas):
        cel = tabela_gfx.cell(0, j)
        _sombrear_celula(cel, VERMELHO)
        _texto_celula(cel, titulo_col, tamanho=8, negrito=True, cor=BRANCO,
                      alinhamento=PP_ALIGN.LEFT if j == 0 else PP_ALIGN.RIGHT)

    for i, linha in enumerate(linhas_dados, start=1):
        cor_fundo = OFF_WHITE if i % 2 == 0 else BRANCO
        cel = tabela_gfx.cell(i, 0)
        _sombrear_celula(cel, cor_fundo)
        _texto_celula(cel, linha["agrupamento"], tamanho=8, alinhamento=PP_ALIGN.LEFT)

        for j, valor in enumerate(linha["valores_pct"]):
            cel = tabela_gfx.cell(i, 1 + j)
            _sombrear_celula(cel, cor_fundo)
            _texto_celula(cel, _fmt_pct_abs(valor) if valor is not None else "—", tamanho=8)

        cel = tabela_gfx.cell(i, 1 + len(meses))
        _sombrear_celula(cel, cor_fundo)
        _texto_celula(cel, _fmt_pct_abs(linha["media_pct"]), tamanho=8, negrito=True)

        cel = tabela_gfx.cell(i, 2 + len(meses))
        _sombrear_celula(cel, cor_fundo)
        var = linha["variacao_pp"]
        cor_var = VERMELHO if (var or 0) > 0 else (VERDE if (var or 0) < 0 else CINZA_MEDIO)
        _texto_celula(cel, (f"{var:+.1f}".replace(".", ",") if var is not None else "—"), tamanho=8,
                      negrito=True, cor=cor_var)


def _slide_quebra(prs, dados, titulo, subtitulo, quebra, numero_pagina):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _set_background(slide, BRANCO)
    _cabecalho_slide(slide, titulo, subtitulo)

    _grafico_quebra(slide, MARGEM, Inches(1.35), LARGURA - 2 * MARGEM, Inches(2.9), quebra)

    _add_textbox(slide, MARGEM, Inches(4.4), Inches(8), Inches(0.3),
                 "Por agrupamento (ordenado pela média do período)", tamanho=11, negrito=True, cor=CINZA_ESCURO)
    _tabela_quebra_agrupamento(slide, MARGEM, Inches(4.72), LARGURA - 2 * MARGEM, Inches(2.2), quebra)

    _rodape(slide, dados, numero_pagina, titulo)
    return slide


# ---------------------------------------------------------------------------
# Slide 9 — Quebra por DDD
# ---------------------------------------------------------------------------
def _slide_quebra_ddd(prs, dados, numero_pagina):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _set_background(slide, BRANCO)
    _acento_canto(slide, tamanho=Inches(1.6))
    _cabecalho_slide(slide, "Quebra de Agenda — por DDD",
                     f"% de quebra por DDD e mês · Jan–{dados['mes_nome']}")

    quebra = dados["quebra_ddd"]
    meses = quebra["meses"]
    colunas = ["DDD"] + meses + ["Média"]
    linhas_dados = quebra["linhas"]
    n_linhas = len(linhas_dados) + 1
    n_colunas = len(colunas)

    width = LARGURA - 2 * MARGEM
    height = Inches(4.6)
    grafico = slide.shapes.add_table(n_linhas, n_colunas, MARGEM, Inches(1.55), width, height)
    tabela_gfx = grafico.table
    tabela_gfx.first_row = False
    tabela_gfx.horz_banding = False

    altura_linha = int(height / n_linhas)
    for linha_tabela in tabela_gfx.rows:
        linha_tabela.height = altura_linha

    largura_ddd = Inches(0.9)
    largura_mes = (width - largura_ddd - Inches(0.9)) / max(1, len(meses))
    tabela_gfx.columns[0].width = int(largura_ddd)
    for i in range(len(meses)):
        tabela_gfx.columns[1 + i].width = int(largura_mes)
    tabela_gfx.columns[1 + len(meses)].width = int(Inches(0.9))

    for j, titulo_col in enumerate(colunas):
        cel = tabela_gfx.cell(0, j)
        _sombrear_celula(cel, GRAFITE)
        _texto_celula(cel, titulo_col, tamanho=11, negrito=True, cor=BRANCO,
                      alinhamento=PP_ALIGN.LEFT if j == 0 else PP_ALIGN.RIGHT)

    for i, linha in enumerate(linhas_dados, start=1):
        destaque = i == 1  # pior DDD do período, já ordenado desc
        cor_fundo = VERMELHO_CLARO_BG if destaque else (OFF_WHITE if i % 2 == 0 else BRANCO)
        cel = tabela_gfx.cell(i, 0)
        _sombrear_celula(cel, cor_fundo)
        _texto_celula(cel, linha["ddd"], tamanho=11, negrito=destaque, alinhamento=PP_ALIGN.LEFT)

        for j, valor in enumerate(linha["valores_pct"]):
            cel = tabela_gfx.cell(i, 1 + j)
            _sombrear_celula(cel, cor_fundo)
            _texto_celula(cel, _fmt_pct_abs(valor) if valor is not None else "—", tamanho=11)

        cel = tabela_gfx.cell(i, 1 + len(meses))
        _sombrear_celula(cel, cor_fundo)
        _texto_celula(cel, _fmt_pct_abs(linha["media_pct"]), tamanho=11, negrito=True,
                      cor=VERMELHO if destaque else CINZA_ESCURO)

    _rodape(slide, dados, numero_pagina, "Quebra de Agenda — por DDD")
    return slide


# ---------------------------------------------------------------------------
# Slide final — Diagnósticos e Recomendações
# ---------------------------------------------------------------------------
def _slide_diagnosticos(prs, dados, numero_pagina):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _set_background(slide, BRANCO)
    _acento_canto(slide)
    _cabecalho_slide(slide, "Diagnósticos e Recomendações",
                     f"Leitura automática dos indicadores · Jan–{dados['mes_nome']}")

    pontos = dados["diagnosticos"]
    if not pontos:
        _add_textbox(slide, MARGEM, Inches(1.8), Inches(11), Inches(0.5),
                     "Sem desvios relevantes identificados nos indicadores deste período.",
                     tamanho=14, cor=CINZA_MEDIO)
        _rodape(slide, dados, numero_pagina, "Diagnósticos e Recomendações")
        return slide

    top = Inches(1.35)
    altura_card = Inches(0.92)
    espaco = Inches(0.12)
    for indice, ponto in enumerate(pontos, start=1):
        _add_rect(slide, MARGEM, top, LARGURA - 2 * MARGEM, altura_card, OFF_WHITE, arredondado=True)

        selo = slide.shapes.add_shape(MSO_SHAPE.OVAL, MARGEM + Inches(0.2), top + Inches(0.24), Inches(0.44), Inches(0.44))
        selo.fill.solid()
        selo.fill.fore_color.rgb = VERMELHO
        selo.line.fill.background()
        selo.shadow.inherit = False
        selo.text_frame.margin_left = 0
        selo.text_frame.margin_right = 0
        selo.text_frame.word_wrap = False
        p_selo = selo.text_frame.paragraphs[0]
        p_selo.alignment = PP_ALIGN.CENTER
        run_selo = p_selo.add_run()
        run_selo.text = str(indice)
        run_selo.font.size = Pt(16)
        run_selo.font.bold = True
        run_selo.font.color.rgb = BRANCO
        run_selo.font.name = FONTE_TITULO

        _add_textbox(slide, MARGEM + Inches(0.85), top + Inches(0.1), LARGURA - 2 * MARGEM - Inches(1.1), Inches(0.4),
                     ponto["diagnostico"], tamanho=12.5, negrito=True, cor=CINZA_ESCURO, fonte=FONTE_CORPO)
        _add_textbox(slide, MARGEM + Inches(0.85), top + Inches(0.52), LARGURA - 2 * MARGEM - Inches(1.1), Inches(0.4),
                     "Recomendação: " + ponto["recomendacao"], tamanho=11, cor=GRAFITE, fonte=FONTE_CORPO, italico=True)
        top += altura_card + espaco

    _rodape(slide, dados, numero_pagina, "Diagnósticos e Recomendações")
    return slide


# ---------------------------------------------------------------------------
# Montagem final
# ---------------------------------------------------------------------------
def montar_apresentacao_pptx(dados, caminho_saida):
    prs = Presentation()
    prs.slide_width = LARGURA
    prs.slide_height = ALTURA

    _slide_capa(prs, dados)
    _slide_resumo(prs, dados, "resumo_rsi", 2)
    _slide_resumo(prs, dados, "resumo_celula", 3)
    _slide_resumo(prs, dados, "resumo_brasil", 4)

    _slide_tabela_canal(
        prs, dados, "Canais — Regionais (Brasil)", "OS Geradas e Executadas por canal · Todas as regionais",
        dados["tabela_canal_brasil_geracao"], dados["tabela_canal_brasil_execucao"], 5,
    )
    _slide_tabela_canal(
        prs, dados, "Canais — RSI", "OS Geradas e Executadas por canal · Regional RSI",
        dados["tabela_canal_rsi_geracao"], dados["tabela_canal_rsi_execucao"], 6,
    )

    _slide_quebra(prs, dados, "Quebra de Agenda — RSI",
                  f"Regional RSI · Jan–{dados['mes_nome']}/{dados['dados_ate'].year}", dados["quebra_rsi"], 7)
    _slide_quebra(prs, dados, "Quebra de Agenda — Célula Local",
                  f"Canal Célula Local · Jan–{dados['mes_nome']}/{dados['dados_ate'].year}", dados["quebra_celula"], 8)
    _slide_quebra_ddd(prs, dados, 9)

    _slide_diagnosticos(prs, dados, 10)

    prs.save(caminho_saida)
    return caminho_saida
