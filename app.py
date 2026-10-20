# Em rede corporativa (com inspeção de tráfego HTTPS, ex: proxy da empresa),
# o Windows já confia no certificado interno, mas o Python usa uma lista de
# certificados própria e separada -- isso faz chamadas HTTPS (como pro bot do
# Telegram) falharem com erro de SSL mesmo com tudo certo. Aqui a gente faz o
# Python usar a mesma lista de confiança do Windows, resolvendo isso. Precisa
# vir antes de QUALQUER outro import, pra garantir que pegue tudo.
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass  # se o pacote não estiver instalado, segue com o comportamento padrão do Python

from flask import Flask, render_template, request, redirect, session, send_file, send_from_directory, jsonify, flash
import sqlite3
import json
import math
import re
import unicodedata
import pandas as pd
import io
from datetime import datetime, timedelta, time
import os
import sys
import secrets
import threading
import time as tempo_espera
from functools import wraps
from werkzeug.utils import secure_filename
from werkzeug.security import check_password_hash, generate_password_hash
from painel_cotas import (
    carregar_dados, montar_cards_proxima_janela, montar_heatmap, salvar_upload_cotas,
    carregar_config_extracao, salvar_config_extracao, gerar_config_cotas_py, CLASSES_DISPONIVEIS,
    carregar_mapa_cidades, salvar_mapa_cidades, prefixos_do_csv,
    arquivar_snapshot_cotas, montar_historico_mensal, formatar_alongamento,
    LIMIAR_ALONGAMENTO_HORAS, sincronizar_resumo_historico, classes_do_csv,
)

# O servidor do PythonAnywhere roda no fuso UTC, mas o app inteiro sempre
# assumiu horário de Brasília (era rodado localmente, no Brasil, com o
# relógio do próprio Windows já certo). Sem isso, TODO datetime.now() do
# código -- login/logout, pausas, relatório do Telegram, aniversário,
# checagem de horário de envio -- vem 3h adiantado (Brasília é UTC-3).
# Em vez de mudar cada datetime.now() espalhado pelo código (são dezenas),
# fixa o fuso do próprio processo logo na subida -- precisa vir antes de
# QUALQUER datetime.now() ser chamado, inclusive os que rodam na importação
# do módulo (ex: a auto-migração do aniversário, mais abaixo).
os.environ["TZ"] = "America/Sao_Paulo"
try:
    tempo_espera.tzset()
except AttributeError:
    pass  # tzset() só existe em Unix -- no Windows local (Anaconda) o relógio do SO já é o certo, não precisa disso

# root_path ancorado na pasta do próprio app.py (pedido do Bruno, 2026-09-25,
# junto com o ambiente espelho): sem isso, o Flask('CRM RSI') procurava
# templates/ e static/ na pasta de onde o processo foi iniciado (cwd), e o
# espelho -- que roda no mesmo processo da produção -- acabaria mostrando os
# templates da produção. Em produção não muda nada (cwd já é esta pasta).
app = Flask('CRM RSI', root_path=os.path.dirname(os.path.abspath(__file__)))


def obter_versao_sistema(conn=None):
    """Versão exibida no rodapé do login e no link da sidebar -- é sempre a
    versão mais recente cadastrada em releases_sistema (editável pelo ADM em
    /notas-de-versao), não mais um texto fixo no código. "0.0" é só um
    fallback pra não quebrar a tela antes da migração rodar."""
    conn_proprio = conn or conectar()
    linha = conn_proprio.execute(
        "SELECT versao FROM releases_sistema ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not conn:
        conn_proprio.close()
    return linha["versao"] if linha else "0.0"


app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')  # absoluto: ver comentário do root_path
# SECRET_KEY (corrigido 2026-08-29, pedido do Bruno -- auditoria de segurança):
# antes tinha um valor-padrão fixo no código ("troque-esta-chave-em-producao")
# que valia pra QUALQUER instalação que esquecesse de configurar a variável de
# ambiente -- quem conhecesse esse valor (é literalmente público, está no
# histórico do projeto) conseguia forjar cookie de sessão de qualquer usuário.
# Agora: se CRM_SECRET_KEY não estiver configurada, gera uma chave aleatória
# nova a cada início do processo (nunca mais o valor fixo) e avisa bem alto no
# log -- sessões abertas antes de um reload nesse cenário são invalidadas,
# o que é justamente o comportamento que queremos (força a pessoa a notar e
# configurar a variável de ambiente de verdade, em vez de rodar
# silenciosamente insegura pra sempre). Em produção (PythonAnywhere), configure
# CRM_SECRET_KEY na aba Web -> "Environment variables" (ou no WSGI, ver
# GUIA_DEPLOY_PYTHONANYWHERE.md) -- com ela configurada nada muda pro usuário.
_secret_key_env = os.environ.get("CRM_SECRET_KEY")
if _secret_key_env:
    app.secret_key = _secret_key_env
else:
    app.secret_key = secrets.token_hex(32)
    print(
        "AVISO DE SEGURANCA: variavel de ambiente CRM_SECRET_KEY nao configurada -- "
        "usando uma chave aleatoria gerada so pra este processo (sessoes serao "
        "invalidadas a cada reload/restart ate isso ser configurado). "
        "Configure CRM_SECRET_KEY na aba Web -> Environment variables do PythonAnywhere.",
        file=sys.stderr,
    )

# Token compartilhado com as Edge Functions do Supabase (webhook-telegram e o
# cron de envio diário), pra elas conseguirem chamar as rotas /interno/* daqui
# do CRM (agora que ele tem endereço público) sem que qualquer um na internet
# consiga chamar essas rotas também. Não é senha de gente -- é só pra
# servidor-a-servidor, por isso fica fixo aqui em vez de pedir login.
TOKEN_RELATORIO_API = os.environ.get("RELATORIO_API_TOKEN", "7871d81fd02655c13f202454787b6c655b90358ec305a9a16cd4653e92459e52")

# ==========================
# REGRAS PADRÃO DE PAUSA (usadas só se o ADM nunca configurou nada em
# Tratamento/Pausas -- ver obter_limites_pausa(), que lê de "configuracoes")
# ==========================
PAUSA_MANHA_LIMITE_PADRAO = time(10, 30)
PAUSA_MANHA_INICIO_PADRAO = time(6, 0)
ALMOCO_JANELA_INICIO_PADRAO = time(11, 30)
ALMOCO_LIMITE_PADRAO = time(13, 0)
PAUSA_TARDE_LIMITE_PADRAO = time(16, 30)
PAUSA_TARDE_INICIO_PADRAO = time(13, 0)

# Tempo de inatividade até um contrato "EM_ANDAMENTO" ser liberado de volta pra fila
INATIVIDADE_LIMITE = timedelta(hours=24)

DIAS_SEMANA = ["SEG", "TER", "QUA", "QUI", "SEX", "SAB", "DOM"]  # weekday(): 0=Segunda

# Ancora o caminho do banco na pasta do próprio app.py, não na pasta de onde o
# processo foi chamado -- sem isso, "crm.db" (caminho relativo) resolve
# certinho quando servido via WSGI (que já roda de dentro da pasta do
# projeto), mas quebra quando chamado de outro lugar, como uma Scheduled
# Task do PythonAnywhere: o comando "python3.11 /home/crmrsi/CRM_RSI/
# tarefa_encerramento_automatico.py" roda com o diretório de trabalho sendo
# a pasta pessoal (/home/crmrsi/), não o CRM_RSI -- então "crm.db" criava
# (silenciosamente, o sqlite3 não erra "arquivo não existe", só cria um novo
# vazio) um banco novo e vazio ali, sem nenhuma tabela, e a primeira
# migração que faz SELECT numa tabela existente quebrava com "no such
# table" (bug encontrado pelo Bruno, 2026-09-09, no log da Task de
# encerramento automático).
PASTA_BASE = os.path.dirname(os.path.abspath(__file__))


def conectar():
    conn = sqlite3.connect(os.path.join(PASTA_BASE, "crm.db"))
    conn.row_factory = sqlite3.Row
    # WAL: permite leitura simultânea enquanto alguém grava, em vez de travar tudo
    # numa fila só. Funciona igual no Windows e no Linux — não precisa de nada
    # diferente por sistema operacional.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    # Se o banco estiver ocupado por outra escrita no exato milissegundo, espera
    # até 5s em vez de estourar erro "database is locked" na hora.
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


# ==========================
# LOGIN / SESSÃO
# ==========================

def usuario_atual():
    """Retorna o usuário logado (dict-like) a partir da sessão, ou None."""
    usuario_id = session.get("usuario_id")
    if not usuario_id:
        return None
    conn = conectar()
    usuario = conn.execute("SELECT * FROM usuarios WHERE id=? AND ativo=1", (usuario_id,)).fetchone()
    conn.close()
    return usuario


# Rotas que o OPERADOR pode acessar mesmo sem ter iniciado o tratamento hoje
# ("/portal" cobre também "/portal/ferramenta" e "/portal/painel" -- o portal
# em si (e o painel de resultados) tem que abrir a qualquer hora, mesmo antes
# do turno começar; só o botão "Abrir a Ferramenta" respeita o horário)
ROTAS_LIVRES_SEM_TURNO = ("/pausas", "/turno/entrar", "/turno/sair", "/trocar-senha", "/logout", "/perfil/salvar", "/portal", "/ajuda")


def login_required(f):
    @wraps(f)
    def decorado(*args, **kwargs):
        if not session.get("usuario_id"):
            return redirect("/login")
        usuario = usuario_atual()
        if not usuario:
            # usuário foi desativado ou removido: encerra a sessão
            session.clear()
            return redirect("/login")
        if usuario["precisa_trocar_senha"] and request.path not in ("/trocar-senha", "/logout"):
            return redirect("/trocar-senha")
        if usuario["perfil"] == "OPERADOR" and not usuario["precisa_trocar_senha"]:
            if not request.path.startswith(ROTAS_LIVRES_SEM_TURNO):
                conn = conectar()
                sessao_hoje = conn.execute(
                    "SELECT 1 FROM sessoes WHERE usuario_id=? AND date(login_em)=?",
                    (usuario["id"], hoje_str())
                ).fetchone()
                conn.close()
                if not sessao_hoje:
                    return redirect("/pausas")
        return f(*args, **kwargs)
    return decorado


def perfil_required(*perfis_permitidos):
    def decorator(f):
        @wraps(f)
        def decorado(*args, **kwargs):
            usuario = usuario_atual()
            if not usuario or usuario["perfil"] not in perfis_permitidos:
                return "Acesso restrito a: " + ", ".join(perfis_permitidos), 403
            return f(*args, **kwargs)
        return decorado
    return decorator


def perfil_pode(perfil, modulo):
    """Consulta a matriz de permissões (tela Permissões). ADM sempre tem acesso
    a tudo e nem entra na tabela — isso é garantido aqui, não no banco."""
    if perfil == "ADM":
        return True
    conn = conectar()
    linha = conn.execute(
        "SELECT permitido FROM permissoes WHERE perfil=? AND modulo=?", (perfil, modulo)
    ).fetchone()
    conn.close()
    return bool(linha and linha["permitido"])


def permissao_required(modulo):
    def decorator(f):
        @wraps(f)
        def decorado(*args, **kwargs):
            usuario = usuario_atual()
            if not usuario or not perfil_pode(usuario["perfil"], modulo):
                return f"Seu perfil não tem permissão para acessar '{modulo}'.", 403
            return f(*args, **kwargs)
        return decorado
    return decorator


@app.template_global("cod_operadora_da_cidade")
def cod_operadora_da_cidade(cidade):
    """Código de operadora de uma cidade (de-para em Cidades x Operadora) --
    pedido do Bruno, 2026-10-16, pra mostrar na tela de atendimento. Se a
    cidade tiver mais de um código cadastrado, mostra todos; '—' se nenhum."""
    if not cidade:
        return "—"
    try:
        conn = conectar()
        codigos = [r["cod_operadora"] for r in conn.execute(
            "SELECT cod_operadora FROM cidades_operadora WHERE UPPER(TRIM(cidade)) = UPPER(TRIM(?)) ORDER BY cod_operadora",
            (cidade,))]
        conn.close()
    except Exception:
        return "—"
    return ", ".join(codigos) if codigos else "—"


@app.context_processor
def injetar_usuario_logado():
    usuario = usuario_atual()

    if usuario:
        verificar_aniversarios_se_necessario()

    def pode(capacidade):
        return bool(usuario) and perfil_pode(usuario["perfil"], capacidade)

    precisa_iniciar_tratamento = False
    precisa_encerrar_tratamento = False
    if usuario and usuario["perfil"] == "OPERADOR":
        conn = conectar()
        hoje = datetime.now().strftime("%Y-%m-%d")
        sessao_hoje = conn.execute("""
            SELECT * FROM sessoes WHERE usuario_id=? AND date(login_em)=?
            ORDER BY id DESC LIMIT 1
        """, (usuario["id"], hoje)).fetchone()

        if not sessao_hoje:
            precisa_iniciar_tratamento = True
        elif (sessao_hoje["logout_em"] is None and obter_modo_trabalho(conn) == "ATIVO"
              and datetime.now().time() >= time(17, 48) and encerramento_automatico_ligado(conn)):
            precisa_encerrar_tratamento = True
        conn.close()

    tem_noticia_nao_lida = False
    tem_monitoria_pendente = False
    tem_quebra_pendente = False
    tem_transferencia_pendente = False
    sunny_link = None
    if usuario:
        conn = conectar()
        linha = conn.execute("""
            SELECT COUNT(*) c FROM noticias n
            WHERE n.ativo = 1
              AND NOT EXISTS (SELECT 1 FROM noticias_leituras l WHERE l.noticia_id = n.id AND l.usuario_id = ?)
        """, (usuario["id"],)).fetchone()
        tem_noticia_nao_lida = linha["c"] > 0

        linha2 = conn.execute("""
            SELECT COUNT(*) c FROM monitorias WHERE operador_id=? AND aceite_status='PENDENTE'
        """, (usuario["id"],)).fetchone()
        tem_monitoria_pendente = linha2["c"] > 0

        if perfil_pode(usuario["perfil"], "tratamento_quebra"):
            tem_quebra_pendente = contador_quebras_pendentes(conn, usuario["nome"]) > 0

        if usuario["perfil"] in ("SUPERVISOR", "ADM"):
            linha4 = conn.execute("SELECT COUNT(*) c FROM transferencias WHERE status='PENDENTE'").fetchone()
            tem_transferencia_pendente = linha4["c"] > 0

        linha3 = conn.execute("SELECT sunny_link FROM configuracoes WHERE id=1").fetchone()
        sunny_link = linha3["sunny_link"] if linha3 else None
        conn.close()

    return {
        "usuario_logado": usuario,
        "pode": pode,
        "sunny_link": sunny_link,
        "precisa_iniciar_tratamento": precisa_iniciar_tratamento,
        "precisa_encerrar_tratamento": precisa_encerrar_tratamento,
        "tem_noticia_nao_lida": tem_noticia_nao_lida,
        "tem_monitoria_pendente": tem_monitoria_pendente,
        "tem_quebra_pendente": tem_quebra_pendente,
        "tem_transferencia_pendente": tem_transferencia_pendente,
        "versao_sistema": obter_versao_sistema(),
    }


@app.template_filter("exibir_perfil")
def exibir_perfil(perfil):
    """Só pra exibição na tela — o valor gravado no banco/lógica não muda."""
    if perfil == "OPERADOR":
        return "ASSISTENTE"
    if perfil == "ANALISTA_II":
        return "ANALISTA II"
    if perfil == "ASSISTENTE_CLARO":
        return "ASSISTENTE CLARO"
    if perfil == "OPERADOR_CONF":
        return "CONFIRMAÇÃO DE AGENDA"
    if perfil == "VISUALIZACAO":
        return "VISUALIZAÇÃO"
    return perfil


@app.route("/login", methods=["GET", "POST"])
def login():
    erro = None
    if request.method == "POST":
        login_informado = (request.form.get("login") or "").strip().lower()
        senha_informada = request.form.get("senha") or ""

        conn = conectar()
        usuario = conn.execute(
            "SELECT * FROM usuarios WHERE login=? AND ativo=1", (login_informado,)
        ).fetchone()
        conn.close()

        if usuario and check_password_hash(usuario["senha"], senha_informada):
            # A regra de "só 5 min antes do início da escala" não bloqueia mais o
            # login em si -- o portal (nova página pós-login) pode ser aberto a
            # qualquer hora. Quem respeita o horário agora é o botão "Abrir a
            # Ferramenta" (ver /portal/ferramenta), e o /turno/entrar como
            # segunda trava (pra quem tentar pular o portal via URL direta).
            conn = conectar()
            session["usuario_id"] = usuario["id"]
            conn.execute("UPDATE usuarios SET ultimo_ip=? WHERE id=?", (request.remote_addr, usuario["id"]))
            conn.commit()
            conn.close()
            return redirect("/portal")
        erro = "Login ou senha inválidos."

    return render_template("Login.html", erro=erro)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


# ==========================
# PORTAL (página pós-login) + PAINEL DE RESULTADOS
# ==========================

# Pasta com os 2 arquivos do painel: o HTML (entregue por mim, não mexe) e a
# planilha de dados (o Bruno sobe/atualiza periodicamente pelo Files do
# PythonAnywhere -- sempre com esse nome exato, sobrescrevendo a anterior).
PASTA_PAINEL = os.path.join(PASTA_BASE, "painel_dados")  # absoluto (espelho, 2026-09-25)
os.makedirs(PASTA_PAINEL, exist_ok=True)

ARQUIVO_PAINEL_HTML = "diario_brownfield_rsi.html"
ARQUIVO_PAINEL_DADOS = "dados_brownfield_rsi.xlsx"

# Perfis que podem ver o Painel de Resultados (o novo perfil "Visualização" só
# serve pra isso -- não tem acesso à ferramenta de atendimento).
PERFIS_PAINEL = ("ADM", "SUPERVISOR", "ANALISTA_II", "VISUALIZACAO")


@app.route("/portal")
@login_required
def portal():
    usuario = usuario_atual()
    erro_ferramenta = session.pop("portal_erro_ferramenta", None)
    return render_template(
        "Portal.html",
        erro_ferramenta=erro_ferramenta,
        pode_painel=usuario["perfil"] in PERFIS_PAINEL,
        # OPERADOR_CONF fica de fora daqui -- o botão "Abrir a Ferramenta"
        # sempre manda pra "/" (Dashboard principal), que esse perfil não
        # acessa (não tem permissão de dashboard, de propósito). O trabalho
        # dele inteiro é a Confirmação de Agenda, que tem botão próprio
        # logo abaixo -- pedido do Bruno, 2026-09-16.
        pode_ferramenta=usuario["perfil"] not in ("VISUALIZACAO", "OPERADOR_CONF"),
        pode_confirmacao_agenda=perfil_pode(usuario["perfil"], "confirmacao_agenda_tratar"),
        pode_relatorio_clima=perfil_pode(usuario["perfil"], "relatorio_clima"),
        pode_backlog=perfil_pode(usuario["perfil"], "backlog_tratar"),
    )


@app.route("/ajuda")
@login_required
@permissao_required("ajuda")
def ajuda():
    """Manual explicando cada tela do sistema. Reconstruído em 2026-09-02 a
    pedido do Bruno: antes era acessível a todo mundo sem trava nenhuma,
    agora passa pela matriz de permissões (grupo "Outros" -> "Ajuda") --
    fica fechado por padrão pra todo perfil que não é ADM até ele liberar
    manualmente pela tela de Permissões. O próprio template também esconde
    seções conforme o perfil de quem está olhando."""
    usuario = usuario_atual()
    return render_template("Ajuda.html", perfil=usuario["perfil"])


@app.route("/portal/ferramenta")
@login_required
def portal_ferramenta():
    """Gatilho único pro botão 'Abrir a Ferramenta' -- é aqui que a regra de
    só liberar 5 min antes do início da escala (perfil Operador) é checada.
    O /turno/entrar tem a mesma checagem como segunda trava, pra quem tentar
    pular direto pra lá digitando a URL."""
    usuario = usuario_atual()
    if usuario["perfil"] in ("VISUALIZACAO", "OPERADOR_CONF"):
        return redirect("/portal")
    conn = conectar()
    bloqueado, motivo = verificar_bloqueio_login(conn, usuario)
    conn.close()
    if bloqueado:
        session["portal_erro_ferramenta"] = motivo
        return redirect("/portal")
    return redirect("/")


@app.route("/portal/painel")
@login_required
@perfil_required(*PERFIS_PAINEL)
def portal_painel():
    if not os.path.exists(os.path.join(PASTA_PAINEL, ARQUIVO_PAINEL_HTML)):
        return "Painel ainda não foi publicado neste servidor. Fale com o ADM.", 404
    return send_from_directory(PASTA_PAINEL, ARQUIVO_PAINEL_HTML)


@app.route("/portal/painel/" + ARQUIVO_PAINEL_DADOS)
@login_required
@perfil_required(*PERFIS_PAINEL)
def portal_painel_dados():
    if not os.path.exists(os.path.join(PASTA_PAINEL, ARQUIVO_PAINEL_DADOS)):
        return (
            f"Base de dados do painel ainda não foi carregada. Suba o arquivo "
            f"{ARQUIVO_PAINEL_DADOS} na pasta {PASTA_PAINEL} pelo Files do PythonAnywhere.",
            404,
        )
    return send_from_directory(
        PASTA_PAINEL, ARQUIVO_PAINEL_DADOS,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ==========================
# PAINEL DE COTAS (COP)
# ==========================
# Pasta isolada -- não tem nenhuma relação com o crm.db nem com o
# Painel de Resultados acima. O Bruno importa o CSV consolidado pela
# própria tela (caixa de upload), não pelo Files do PythonAnywhere.
# Liberado pra todos os perfis por enquanto (sem perfil_required); se
# precisar restringir depois, é só adicionar @perfil_required(...).
PASTA_PAINEL_COTAS = os.path.join(PASTA_BASE, "painel_cotas_dados")  # absoluto (espelho, 2026-09-25)
os.makedirs(PASTA_PAINEL_COTAS, exist_ok=True)
ARQUIVO_PAINEL_COTAS_CSV = "merged-painel-cotas.csv"
ARQUIVO_MAPA_CIDADES = os.path.join(PASTA_PAINEL_COTAS, "mapa_cidades.json")
# Pasta de histórico (uma foto por upload aceito) que alimenta a visão
# executiva/acumulada -- ver arquivar_snapshot_cotas em painel_cotas.py
# e a rota portal_painel_cotas_historico abaixo. Pedido do Bruno, 2026-09-21.
PASTA_HISTORICO_COTAS = os.path.join(PASTA_PAINEL_COTAS, "historico")
# Resumo incremental do histórico (uma linha por foto/cidade/classe) --
# SQLite próprio do painel, sem relação com o crm.db. Atualizado a cada
# upload e conferido a cada acesso; pode ser apagado sem perda, é
# reconstruído sozinho a partir das fotos. Pedido do Bruno, 2026-10-06.
ARQUIVO_RESUMO_HISTORICO_COTAS = os.path.join(PASTA_PAINEL_COTAS, "historico_resumo.db")

# Coordenadas aproximadas (lat, lon) das cidades do Painel de Cotas.
# NÃO É USADO PELO MAPA da visão de histórico desde 2026-09-21 -- o
# mapa passou a usar a malha de municípios de verdade
# (static/geo/sp_municipios.json), casando por NOME, não por
# coordenada. Deixado aqui porque ainda é um dado útil (lat/lon por
# cidade) caso apareça outro uso; se nada mais referenciar isso daqui
# a um tempo, pode remover.
COORDENADAS_CIDADES_COTAS = {
    "AMERICANA": (-22.7375, -47.3317), "APARECIDA": (-22.8474, -45.2308),
    "ARACATUBA": (-21.2089, -50.4328), "ARARAQUARA": (-21.7845, -48.1780),
    "ARARAS": (-22.3573, -47.3838), "BAURU": (-22.3147, -49.0606),
    "BOTUCATU": (-22.8858, -48.4450), "CACAPAVA": (-23.0989, -45.7078),
    "CACHOEIRA PAULISTA": (-22.6656, -45.0092), "CAMPINAS": (-22.9099, -47.0626),
    "CAPIVARI": (-22.9942, -47.5078), "COSMOPOLIS": (-22.6408, -47.1953),
    "CRUZEIRO": (-22.5722, -44.9647), "CUBATAO": (-23.8950, -46.4253),
    "ELIAS FAUSTO": (-23.0433, -47.3792), "FRANCA": (-20.5386, -47.4008),
    "GUARUJA": (-23.9931, -46.2564), "HORTOLANDIA": (-22.8583, -47.2200),
    "INDAIATUBA": (-23.0908, -47.2181), "ITAPETININGA": (-23.5917, -48.0533),
    "JACAREI": (-23.3053, -45.9658), "JAU": (-22.2936, -48.5578),
    "LIMEIRA": (-22.5647, -47.4017), "LORENA": (-22.7300, -45.1197),
    "MARILIA": (-22.2139, -49.9458), "MOGI GUACU": (-22.3672, -46.9425),
    "MOGI MIRIM": (-22.4319, -46.9578), "MONTE MOR": (-22.9469, -47.3175),
    "PAULINIA": (-22.7614, -47.1542), "PINDAMONHANGABA": (-22.9239, -45.4614),
    "PIRACICABA": (-22.7253, -47.6492), "PORTO FELIZ": (-23.2072, -47.5250),
    "PRAIA GRANDE": (-24.0058, -46.4025), "PRESIDENTE PRUDENTE": (-22.1256, -51.3889),
    "RAFARD": (-23.0083, -47.5372), "RIBEIRAO PRETO": (-21.1775, -47.8103),
    "RIO CLARO": (-22.4111, -47.5614), "SANTA BARBARA D OESTE": (-22.7550, -47.4142),
    "SANTOS": (-23.9608, -46.3339), "SAO CARLOS": (-22.0175, -47.8908),
    "SAO JOSE DO RIO PRETO": (-20.8197, -49.3794), "SAO JOSE DOS CAMPOS": (-23.1794, -45.8869),
    "SAO VICENTE": (-23.9631, -46.3919), "SERTAOZINHO": (-21.1378, -48.0264),
    "SOROCABA": (-23.5015, -47.4526), "SUMARE": (-22.8219, -47.2669),
    "TAUBATE": (-23.0264, -45.5553), "VALINHOS": (-22.9714, -46.9967),
    "VINHEDO": (-23.0300, -46.9758), "VOTORANTIM": (-23.5467, -47.4386),
}


@app.route("/portal/painel-cotas")
@login_required
def portal_painel_cotas():
    caminho = os.path.join(PASTA_PAINEL_COTAS, ARQUIVO_PAINEL_COTAS_CSV)
    mapa_cidades = carregar_mapa_cidades(ARQUIVO_MAPA_CIDADES)
    # Tela principal só mostra de hoje em diante -- dia que já passou não
    # ajuda a achar a próxima janela aberta, só faz a varredura demorar
    # mais a cada dia que a agenda cresce. O histórico completo continua
    # intacto em /portal/painel-cotas/historico (lê os snapshots à parte).
    hoje_str = datetime.now().strftime("%Y-%m-%d")
    df, erro = carregar_dados(caminho, mapa_cidades=mapa_cidades, data_minima=hoje_str)

    ultima_importacao = None
    if os.path.exists(caminho):
        ultima_importacao = datetime.fromtimestamp(os.path.getmtime(caminho))

    cards, heatmaps = [], []
    if df is not None:
        config = carregar_config_extracao(ARQUIVO_CONFIG_COTAS)
        cards = montar_cards_proxima_janela(df, cidades_filtro=config.get("cidades_cards"))
        heatmaps = montar_heatmap(df)

    return render_template(
        "PainelCotas.html",
        cards=cards,
        heatmaps=heatmaps,
        erro=erro,
        ultima_importacao=ultima_importacao,
        tem_dados=df is not None,
    )


@app.route("/portal/painel-cotas/upload", methods=["POST"])
@login_required
def portal_painel_cotas_upload():
    arquivo = request.files.get("arquivo")
    ok, mensagem = salvar_upload_cotas(arquivo, PASTA_PAINEL_COTAS, ARQUIVO_PAINEL_COTAS_CSV)
    if ok:
        # Guarda essa foto no histórico pra alimentar a visão executiva
        # (/portal/painel-cotas/historico) -- pedido do Bruno, 2026-09-21.
        # Best-effort: se der algum erro de disco aqui, não quebra o
        # upload em si (a foto atual já foi salva com sucesso).
        try:
            arquivar_snapshot_cotas(
                PASTA_HISTORICO_COTAS,
                os.path.join(PASTA_PAINEL_COTAS, ARQUIVO_PAINEL_COTAS_CSV),
            )
        except Exception:
            pass
        # Já deixa a foto nova resumida na tabela do histórico, pra página
        # de histórico não ter que processar nada no próximo acesso.
        # Também best-effort: se falhar, a própria página completa depois.
        try:
            sincronizar_resumo_historico(
                PASTA_HISTORICO_COTAS, ARQUIVO_RESUMO_HISTORICO_COTAS,
                carregar_mapa_cidades(ARQUIVO_MAPA_CIDADES),
            )
        except Exception:
            pass
    flash(mensagem, "sucesso" if ok else "erro")
    return redirect("/portal/painel-cotas")


def _obter_mapa_cidade_ddd():
    """Cidade (maiúscula) -> DDD, a partir da tabela cidades_ddd do
    crm.db -- já existe e é mantida pro módulo de Confirmação de Agenda
    (ver confirmacao_agenda.py). Reaproveitada aqui só como um dict
    pronto, sem o Painel de Cotas depender do crm.db diretamente (mesmo
    padrão de injeção já usado pra mapa_cidades)."""
    conn = conectar()
    try:
        return {r["cidade"]: r["ddd"] for r in conn.execute("SELECT cidade, ddd FROM cidades_ddd")}
    finally:
        conn.close()


@app.route("/portal/painel-cotas/historico")
@login_required
def portal_painel_cotas_historico():
    hoje = datetime.now().date()
    data_inicio_str = request.args.get("data_inicio") or hoje.replace(day=1).strftime("%Y-%m-%d")
    data_fim_str = request.args.get("data_fim") or hoje.strftime("%Y-%m-%d")
    try:
        data_inicio = datetime.strptime(data_inicio_str, "%Y-%m-%d").date()
        data_fim = datetime.strptime(data_fim_str, "%Y-%m-%d").date()
    except ValueError:
        data_inicio, data_fim = hoje.replace(day=1), hoje

    # Filtros agora são combobox de seleção única (pedido do Bruno,
    # 2026-09-21) -- "" ou ausente = "Todos", vira lista vazia (sem
    # filtro) pra montar_historico_mensal.
    ddd_sel = (request.args.get("ddd") or "").strip()
    cidade_sel = (request.args.get("cidade") or "").strip()
    classe_sel = (request.args.get("classe") or "").strip()
    ddd_filtro = [ddd_sel] if ddd_sel else []
    cidade_filtro = [cidade_sel] if cidade_sel else []
    classe_filtro = [classe_sel] if classe_sel else []

    mapa_cidades = carregar_mapa_cidades(ARQUIVO_MAPA_CIDADES)
    cidade_para_ddd = _obter_mapa_cidade_ddd()

    resultado = montar_historico_mensal(
        PASTA_HISTORICO_COTAS, mapa_cidades, cidade_para_ddd,
        data_inicio=data_inicio, data_fim=data_fim,
        ddd_filtro=ddd_filtro or None, cidade_filtro=cidade_filtro or None,
        classe_filtro=classe_filtro or None,
        caminho_resumo=ARQUIVO_RESUMO_HISTORICO_COTAS,
    )

    # Dados do mapa: o mapa em si agora é a malha de municípios de SP
    # de verdade (static/geo/sp_municipios.json, IBGE via geodata-br),
    # pintada no navegador -- sem tile externo, sem relevo, sem malha
    # rodoviária (pedido do Bruno, 2026-09-21: "menos colorido, focado
    # nas fronteiras entre cidades"). Aqui só entregamos os valores por
    # cidade; o casamento com o polígono é por nome, no JS (normaliza
    # acento/caixa dos dois lados -- ver template). Não depende mais de
    # ter coordenada cadastrada em COORDENADAS_CIDADES_COTAS.
    pontos_mapa = []
    maior_soma = max((c["soma_horas_acima_limiar"] for c in resultado["cidades"]), default=0) or 1
    for c in resultado["cidades"]:
        pontos_mapa.append({
            **c,
            "intensidade": round(c["soma_horas_acima_limiar"] / maior_soma, 3) if c["soma_horas_acima_limiar"] else 0,
        })

    # Listas pros filtros (todas as opções conhecidas, não só as que
    # aparecem no período filtrado, pra não "sumir" opção do combo
    # conforme o usuário filtra).
    todas_cidades = sorted(cidade_para_ddd.keys())
    todos_ddds = sorted({str(d) for d in cidade_para_ddd.values()}, key=lambda d: int(d))

    # Classes disponíveis pro filtro: derivadas dos valores REAIS que
    # aparecem no CSV atual do painel ("CLASSE1", "CLASSE15", ...), não
    # da lista fixa CLASSES_DISPONIVEIS (que usa outras chaves --
    # "1", "1pme" -- pensadas pro download, não pro filtro daqui).
    # Isso também corrige o combo mostrando a tupla Python inteira
    # (ex: "('1', 'Classe 1')") em vez do nome -- bug apontado pelo
    # Bruno, 2026-09-21.
    # (2026-10-06: lê só a coluna Classe, sem consolidar o arquivo todo)
    valores_classe = classes_do_csv(os.path.join(PASTA_PAINEL_COTAS, ARQUIVO_PAINEL_COTAS_CSV))
    classes_disponiveis = [(v, v.replace("CLASSE", "Classe ")) for v in valores_classe]

    return render_template(
        "PainelCotasHistorico.html",
        resultado=resultado,
        pontos_mapa=pontos_mapa,
        data_inicio=data_inicio_str, data_fim=data_fim_str,
        ddd_sel=ddd_sel, cidade_sel=cidade_sel, classe_sel=classe_sel,
        todas_cidades=todas_cidades, todos_ddds=todos_ddds,
        classes_disponiveis=classes_disponiveis,
        limiar_horas=LIMIAR_ALONGAMENTO_HORAS,
        formatar_alongamento=formatar_alongamento,
    )


# Sub-página de configuração da extração (cidades/classes) -- só ADM.
# Fica isolada dentro do Painel de Cotas, sem nenhuma relação com o
# CRM de Migração nem com o crm.db.
ARQUIVO_CONFIG_COTAS = os.path.join(PASTA_PAINEL_COTAS, "config_extracao.json")


@app.route("/portal/painel-cotas/config", methods=["GET"])
@login_required
@perfil_required("ADM")
def portal_painel_cotas_config():
    config = carregar_config_extracao(ARQUIVO_CONFIG_COTAS)

    caminho_csv = os.path.join(PASTA_PAINEL_COTAS, ARQUIVO_PAINEL_COTAS_CSV)
    mapa_atual = carregar_mapa_cidades(ARQUIVO_MAPA_CIDADES)
    prefixos = sorted(set(prefixos_do_csv(caminho_csv)) | set(mapa_atual.keys()))
    prefixos_e_nomes = [(p, mapa_atual.get(p, "")) for p in prefixos]

    return render_template(
        "PainelCotasConfig.html",
        cidades_salvas="\n".join(config["cidades"]),
        classes_salvas=set(config["classes"]),
        classes_disponiveis=CLASSES_DISPONIVEIS,
        cidades_cards_salvas="\n".join(config["cidades_cards"]),
        prefixos_e_nomes=prefixos_e_nomes,
    )


@app.route("/portal/painel-cotas/config", methods=["POST"])
@login_required
@perfil_required("ADM")
def portal_painel_cotas_config_salvar():
    cidades_raw = request.form.get("cidades", "")
    cidades = [c.strip().upper() for c in cidades_raw.splitlines() if c.strip()]
    classes = request.form.getlist("classes")
    cidades_cards_raw = request.form.get("cidades_cards", "")
    cidades_cards = [c.strip() for c in cidades_cards_raw.splitlines() if c.strip()]

    if not cidades:
        flash("Informe pelo menos uma cidade.", "erro")
        return redirect("/portal/painel-cotas/config")
    if not classes:
        flash("Selecione pelo menos uma classe.", "erro")
        return redirect("/portal/painel-cotas/config")
    if not cidades_cards:
        flash("Informe pelo menos uma cidade para os cards.", "erro")
        return redirect("/portal/painel-cotas/config")

    salvar_config_extracao(ARQUIVO_CONFIG_COTAS, cidades, classes, cidades_cards)

    conteudo = gerar_config_cotas_py(cidades, classes)
    return send_file(
        io.BytesIO(conteudo.encode("utf-8")),
        mimetype="text/x-python",
        as_attachment=True,
        download_name="config_cotas.py",
    )


@app.route("/portal/painel-cotas/config/mapa-cidades", methods=["POST"])
@login_required
@perfil_required("ADM")
def portal_painel_cotas_config_mapa_cidades():
    mapa_atual = carregar_mapa_cidades(ARQUIVO_MAPA_CIDADES)
    atualizados = 0
    for campo, valor in request.form.items():
        if not campo.startswith("cidade_"):
            continue
        prefixo = campo[len("cidade_"):]
        valor = valor.strip()
        if valor:  # campo em branco = mantém o que já tinha, não apaga
            mapa_atual[prefixo] = valor
            atualizados += 1
    salvar_mapa_cidades(ARQUIVO_MAPA_CIDADES, mapa_atual)
    flash(f"{atualizados} nome(s) de cidade salvo(s).", "sucesso")
    return redirect("/portal/painel-cotas/config")


@app.route("/notas-de-versao")
@login_required
def notas_de_versao():
    conn = conectar()
    releases = conn.execute("SELECT * FROM releases_sistema ORDER BY id DESC").fetchall()
    conn.close()
    return render_template("NotasDeVersao.html", releases=releases)


@app.route("/notas-de-versao/adicionar", methods=["POST"])
@login_required
@perfil_required("ADM")
def notas_de_versao_adicionar():
    versao = (request.form.get("versao") or "").strip()
    data = (request.form.get("data") or "").strip()
    descricao = (request.form.get("descricao") or "").strip()

    if not versao or not data or not descricao:
        return redirect("/notas-de-versao?erro=Preencha versão, data e descrição.")

    conn = conectar()
    conn.execute(
        "INSERT INTO releases_sistema (versao, data, descricao) VALUES (?, ?, ?)",
        (versao, data, descricao)
    )
    conn.commit()
    conn.close()
    return redirect("/notas-de-versao")


@app.route("/notas-de-versao/<int:release_id>/editar", methods=["POST"])
@login_required
@perfil_required("ADM")
def notas_de_versao_editar(release_id):
    """Edição de uma nota de versão já registrada -- pedido do Bruno,
    2026-09-11, só ADM (mesma trava de quem pode adicionar)."""
    versao = (request.form.get("versao") or "").strip()
    data = (request.form.get("data") or "").strip()
    descricao = (request.form.get("descricao") or "").strip()

    if not versao or not data or not descricao:
        return redirect("/notas-de-versao?erro=Preencha versão, data e descrição.")

    conn = conectar()
    conn.execute(
        "UPDATE releases_sistema SET versao=?, data=?, descricao=? WHERE id=?",
        (versao, data, descricao, release_id)
    )
    conn.commit()
    conn.close()
    return redirect("/notas-de-versao")


@app.route("/diario-bordo")
@login_required
@permissao_required("diario_bordo")
def diario_bordo():
    """Registro de eventos operacionais -- pedido do Bruno, 2026-09-10.
    Filtro de data (por padrão, últimos 30 dias) e de classificação."""
    data_fim = request.args.get("data_fim") or hoje_str()
    data_inicio = request.args.get("data_inicio") or (datetime.strptime(data_fim, "%Y-%m-%d") - timedelta(days=30)).strftime("%Y-%m-%d")
    classificacao_filtro = request.args.get("classificacao") or ""

    conn = conectar()
    condicoes = ["data_evento BETWEEN ? AND ?"]
    params = [data_inicio, data_fim]
    if classificacao_filtro:
        condicoes.append("classificacao = ?")
        params.append(classificacao_filtro)

    eventos = conn.execute(f"""
        SELECT * FROM diario_bordo WHERE {" AND ".join(condicoes)}
        ORDER BY data_evento DESC, data_hora_inicio DESC
    """, params).fetchall()

    classificacoes_ativas = [c["nome"] for c in conn.execute(
        "SELECT nome FROM diario_bordo_classificacoes WHERE ativo=1 ORDER BY nome"
    ).fetchall()]
    classificacoes_todas = conn.execute(
        "SELECT * FROM diario_bordo_classificacoes ORDER BY nome"
    ).fetchall()
    conn.close()

    return render_template("DiarioBordo.html", eventos=eventos, data_inicio=data_inicio, data_fim=data_fim,
                            classificacao_filtro=classificacao_filtro, classificacoes_ativas=classificacoes_ativas,
                            classificacoes_todas=classificacoes_todas)


def _calcular_duracao_minutos(inicio_str, fim_str):
    """Retorna (duracao_em_minutos, erro). Erro preenchido se fim vier antes
    do início (duração negativa não é permitida -- pedido do Bruno,
    2026-09-10) ou se alguma data vier mal formatada."""
    try:
        inicio_dt = datetime.strptime(inicio_str, "%Y-%m-%dT%H:%M")
        fim_dt = datetime.strptime(fim_str, "%Y-%m-%dT%H:%M")
    except (ValueError, TypeError):
        return None, "Data/hora de início ou fim inválida."
    if fim_dt < inicio_dt:
        return None, "A data/hora de fim não pode ser antes do início (duração negativa não é permitida)."
    duracao = int((fim_dt - inicio_dt).total_seconds() // 60)
    return duracao, None


@app.route("/diario-bordo/adicionar", methods=["POST"])
@login_required
@permissao_required("diario_bordo")
def diario_bordo_adicionar():
    usuario = usuario_atual()
    data_evento = (request.form.get("data_evento") or "").strip()
    classificacao = (request.form.get("classificacao") or "").strip()
    detalhamento = (request.form.get("detalhamento") or "").strip()
    inicio_str = (request.form.get("data_hora_inicio") or "").strip()
    fim_str = (request.form.get("data_hora_fim") or "").strip()

    if not data_evento or not classificacao or not detalhamento or not inicio_str or not fim_str:
        return redirect("/diario-bordo?erro=" + "Preencha todos os campos.")

    conn = conectar()
    classificacoes_validas = {c["nome"] for c in conn.execute(
        "SELECT nome FROM diario_bordo_classificacoes WHERE ativo=1"
    ).fetchall()}
    if classificacao not in classificacoes_validas:
        conn.close()
        return redirect("/diario-bordo?erro=" + "Classificação inválida.")

    duracao, erro = _calcular_duracao_minutos(inicio_str, fim_str)
    if erro:
        conn.close()
        return redirect("/diario-bordo?erro=" + erro)

    conn.execute("""
        INSERT INTO diario_bordo (data_evento, classificacao, detalhamento, data_hora_inicio, data_hora_fim,
                                   duracao_minutos, criado_por, criado_em)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (data_evento, classificacao, detalhamento, inicio_str.replace("T", " ") + ":00",
          fim_str.replace("T", " ") + ":00", duracao, usuario["nome"], datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()
    return redirect("/diario-bordo")


@app.route("/diario-bordo/exportar")
@login_required
@permissao_required("diario_bordo")
def diario_bordo_exportar():
    data_fim = request.args.get("data_fim") or hoje_str()
    data_inicio = request.args.get("data_inicio") or (datetime.strptime(data_fim, "%Y-%m-%d") - timedelta(days=30)).strftime("%Y-%m-%d")
    classificacao_filtro = request.args.get("classificacao") or ""

    conn = conectar()
    condicoes = ["data_evento BETWEEN ? AND ?"]
    params = [data_inicio, data_fim]
    if classificacao_filtro:
        condicoes.append("classificacao = ?")
        params.append(classificacao_filtro)

    eventos = conn.execute(f"""
        SELECT * FROM diario_bordo WHERE {" AND ".join(condicoes)}
        ORDER BY data_evento DESC, data_hora_inicio DESC
    """, params).fetchall()
    conn.close()

    registros = [{
        "Data do Evento": e["data_evento"],
        "Classificação": e["classificacao"],
        "Detalhamento": e["detalhamento"],
        "Início": e["data_hora_inicio"],
        "Fim": e["data_hora_fim"],
        "Duração (min)": e["duracao_minutos"],
        "Criado Por": e["criado_por"],
        "Criado Em": e["criado_em"],
    } for e in eventos]

    df = pd.DataFrame(registros)
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Diário de Bordo")
    buffer.seek(0)

    nome_arquivo = f"diario_bordo_{data_inicio}_a_{data_fim}.xlsx"
    return send_file(
        buffer, as_attachment=True, download_name=nome_arquivo,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


@app.route("/admin/diario-bordo/classificacoes/adicionar", methods=["POST"])
@login_required
@perfil_required("ADM")
def admin_diario_bordo_classificacoes_adicionar():
    nome = (request.form.get("nome") or "").strip().upper()
    if not nome:
        return redirect("/diario-bordo")
    conn = conectar()
    try:
        conn.execute("INSERT INTO diario_bordo_classificacoes (nome, ativo) VALUES (?, 1)", (nome,))
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    conn.close()
    return redirect("/diario-bordo")


@app.route("/admin/diario-bordo/classificacoes/<int:classificacao_id>/alternar", methods=["POST"])
@login_required
@perfil_required("ADM")
def admin_diario_bordo_classificacoes_alternar(classificacao_id):
    conn = conectar()
    conn.execute("UPDATE diario_bordo_classificacoes SET ativo = 1 - ativo WHERE id=?", (classificacao_id,))
    conn.commit()
    conn.close()
    return redirect("/diario-bordo")


@app.route("/trocar-senha", methods=["GET", "POST"])
@login_required
def trocar_senha():
    usuario = usuario_atual()
    erro = None

    if request.method == "POST":
        senha_atual = request.form.get("senha_atual") or ""
        nova_senha = request.form.get("nova_senha") or ""
        confirmar_senha = request.form.get("confirmar_senha") or ""

        if not check_password_hash(usuario["senha"], senha_atual):
            erro = "Senha atual incorreta."
        elif len(nova_senha) < 4:
            erro = "A nova senha precisa ter pelo menos 4 caracteres."
        elif nova_senha != confirmar_senha:
            erro = "As senhas não coincidem."
        else:
            conn = conectar()
            conn.execute(
                "UPDATE usuarios SET senha=?, precisa_trocar_senha=0 WHERE id=?",
                (generate_password_hash(nova_senha), usuario["id"])
            )
            conn.commit()
            conn.close()
            if usuario["perfil"] in ("VISUALIZACAO", "OPERADOR_CONF"):
                return redirect("/portal")
            return redirect("/")

    return render_template(
        "TrocarSenha.html", erro=erro, obrigatorio=bool(usuario["precisa_trocar_senha"])
    )


@app.route("/perfil/salvar", methods=["POST"])
@login_required
def perfil_salvar():
    usuario = usuario_atual()
    conn = conectar()

    # Só mexe em celular/ramal se esse formulário específico enviou esses campos
    # (o formulário de trocar foto, por exemplo, não manda -- e não pode apagar os outros dois)
    if "celular" in request.form or "ramal" in request.form:
        celular = (request.form.get("celular") or "").strip()
        ramal = (request.form.get("ramal") or "").strip()
        conn.execute(
            "UPDATE usuarios SET celular=?, ramal=? WHERE id=?",
            (celular, ramal, usuario["id"])
        )

    foto = request.files.get("foto")
    if foto and foto.filename:
        extensao = foto.filename.rsplit(".", 1)[-1].lower()
        if extensao in ("png", "jpg", "jpeg", "gif", "webp"):
            nome_arquivo = secure_filename(f"{usuario['nome']}_{usuario['id']}.{extensao}")
            os.makedirs(os.path.join(PASTA_BASE, "static", "fotos_perfil"), exist_ok=True)
            foto.save(os.path.join(PASTA_BASE, "static", "fotos_perfil", nome_arquivo))
            conn.execute(
                "UPDATE usuarios SET foto=? WHERE id=?",
                (f"fotos_perfil/{nome_arquivo}", usuario["id"])
            )

    conn.commit()
    conn.close()
    return redirect(request.referrer or "/")


def obter_modo_trabalho(conn):
    linha = conn.execute("SELECT modo_trabalho FROM configuracoes WHERE id=1").fetchone()
    return linha["modo_trabalho"] if linha else "ATIVO"


def _followup_pendente_do_operador(conn, id_mailing, contrato, operador_nome):
    """True se essa linha específica de mailing (id_mailing) tem um followup
    (2ª/3ª Tentativa ou Retorno) ainda PENDENTE reservado pra esse operador
    -- usado nas rotinas que liberam um EM_ANDAMENTO de volta pra fila
    (timeout de 24h, encerramento forçado) pra decidir se o contrato some
    pra fila geral (PENDENTE, sem dono) ou continua reservado só pra ele
    (ENFILEIRADO, mesma trava que buscar_proximo_cliente já usa pra
    followup). Pedido do Bruno, 2026-09-19: followup reservado fica pro
    mesmo operador pra sempre -- só abre pra outra pessoa se ela entrar de
    férias/afastamento ou for desligada (isso já é o comportamento do
    passo 2 abaixo, que filtra por usuário ativo/ausente; o que faltava
    era não perder essa reserva quando o contrato ficava parado
    EM_ANDAMENTO até expirar).

    Casa por id_mailing quando o followup já tem essa referência (gravado a
    partir de 2026-09-20); followup antigo sem id_mailing (id_mailing IS
    NULL) cai no contrato só, mesmo comportamento de antes -- ver
    _garantir_coluna_id_mailing_atividades_followup pro motivo (o mesmo
    número de contrato pode existir em mais de uma cidade/linha de
    mailing)."""
    linha = conn.execute(
        "SELECT 1 FROM followup WHERE status='PENDENTE' AND operador=? AND (id_mailing=? OR (id_mailing IS NULL AND contrato=?)) LIMIT 1",
        (operador_nome, id_mailing, contrato)
    ).fetchone()
    return bool(linha)


def buscar_proximo_cliente(usuario_id, operador_nome):
    conn = conectar()
    agora = datetime.now()
    agora_str = agora.strftime("%Y-%m-%d %H:%M:%S")

    # 1) Se esse operador já tem um contrato EM_ANDAMENTO, continua nele —
    #    SEMPRE, pra sempre, enquanto ele não for tratado. Não existe mais
    #    timeout de inatividade aqui (pedido do Bruno, 2026-09-21: um
    #    contrato distribuído fica atrelado à mesma pessoa pra sempre,
    #    ponto -- as únicas exceções são a pessoa entrar em Férias/
    #    Afastamento, tratadas por _liberar_em_andamento_por_ausencia,
    #    chamada na hora em que a ausência de hoje é registrada em
    #    Supervisão -- ver supervisao_ausencia -- ou ser desligada,
    #    tratada por _encerrar_sessao_e_redistribuir_tarefas). Antes,
    #    passadas 24h sem o operador pedir o próximo contrato, o sistema
    #    soltava esse EM_ANDAMENTO de volta pra fila sozinho -- só que
    #    isso frequentemente devolvia PENDENTE puro (sem follow-up
    #    registrado ainda, ex: contrato recém distribuído que a pessoa
    #    nem abriu), abrindo pra qualquer um da fila geral pegar um
    #    contrato que já tinha "dono". Reportado pelo Bruno através de
    #    reclamações recorrentes da equipe (contratos de um operador
    #    caindo pra outro, ex: Leticia recebendo contratos que eram da
    #    Melissa/João Edson, 2026-09-21) -- a causa real não era o timeout
    #    em si (ele respeitava follow-up quando existia um), mas o Bruno
    #    decidiu remover de vez esse mecanismo de qualquer forma: contrato
    #    parado na tela da pessoa não deve nunca "vazar" pra fila geral
    #    sozinho, só por inatividade.
    atual = conn.execute("""
        SELECT * FROM mailing WHERE status='EM_ANDAMENTO' AND operador=?
        ORDER BY data_distribuicao DESC LIMIT 1
    """, (operador_nome,)).fetchone()

    if atual:
        conn.close()
        return atual

    # No modo RECEPTIVO não existe distribuição automática — o operador sempre
    # busca o contrato manualmente pela tela de Buscar Contrato.
    modo_atual = obter_modo_trabalho(conn)
    if modo_atual == "RECEPTIVO":
        conn.close()
        return None

    # No modo ATIVO, depois das 17h48 o sistema para de atribuir tarefa NOVA
    # (quem já está em andamento termina normal) -- só quando o encerramento
    # automático de jornada (18h30) estiver ligado; se tiver desligado, a
    # regra também fica desligada, pra não travar quem decidiu trabalhar até
    # mais tarde por conta própria.
    if modo_atual == "ATIVO" and agora.time() >= time(17, 48) and encerramento_automatico_ligado(conn):
        conn.close()
        return None

    # 2) Followups vencidos (retorno agendado ou 2ª/3ª tentativa) do próprio operador
    #    -- já respeitando cidades pausadas pelo ADM (falta de técnico, clima, etc.)
    #
    #    IMPORTANTE: exige m.status='ENFILEIRADO' -- sem isso, um followup antigo
    #    (ex: da 2ª Tentativa) reaparecia aqui mesmo depois do contrato já ter sido
    #    fechado por outro caminho nesse meio tempo (ex: Agendado pela tela Buscar
    #    Contrato), porque essa consulta nunca olhava o status atual do mailing --
    #    só o followup, que ninguém cancelava. Resultado: o contrato "ressuscitava"
    #    na fila quando o prazo do followup antigo vencia, mesmo já concluído.
    #
    #    A reserva (passo 3) é feita com "UPDATE ... WHERE status='PENDENTE'" e
    #    checando quantas linhas foram alteradas -- sem isso, duas pessoas pedindo
    #    contrato ao mesmo tempo podiam ambas "ver" o mesmo PENDENTE antes de
    #    qualquer uma reservar, e as duas acabavam com o mesmo contrato na tela
    #    (bug real, encontrado em 02/09/2026 -- contratos indo pra duas pessoas
    #    ao mesmo tempo). Por isso agora tenta algumas vezes: se perder a corrida
    #    pra outra pessoa num contrato, tenta o próximo da fila, em vez de deixar
    #    as duas passarem.
    ids_ja_tentados = []
    hoje_str_followup = agora.strftime("%Y-%m-%d")
    for _tentativa_reserva in range(5):
        # Ordenado por tipo primeiro (Retorno antes de followup comum -- um
        # Retorno é compromisso firme com o cliente numa data/hora
        # específica, enquanto 2ª/3ª Tentativa é só "tenta de novo depois",
        # sem compromisso; pedido do Bruno, 2026-09-10), e por data de
        # vencimento dentro de cada tipo (mais antigo primeiro). Antes
        # sempre priorizava o followup da PRÓPRIA pessoa, não importa a
        # idade, e só olhava followup de outra pessoa (ou "geral", sem
        # dono) se sobrasse zero followup próprio. Isso fazia followup sem
        # dono (ou de gente que nunca mais pede próximo) ficar parado pra
        # sempre, porque sempre existe algum followup mais recente de
        # alguém competindo e vencendo por ser "da própria pessoa" (achado
        # numa extração do Bruno, 2026-09-09: contrato parado desde 01/09).
        # Sem a camada, o próprio followup ainda tende a vencer na maioria
        # das vezes (raramente fica velho, já que a pessoa está sempre
        # trabalhando), mas um item muito mais antigo -- seu ou de
        # qualquer outra pessoa -- não fica mais esquecido.
        # JOIN por id_mailing quando o followup já tem essa referência
        # (gravado a partir de 2026-09-20); followup antigo sem id_mailing
        # cai de volta pro contrato só (mesmo comportamento de antes -- só
        # ambíguo pros poucos casos de contrato duplicado em cidade
        # diferente que já existiam ANTES dessa correção, ver
        # _garantir_coluna_id_mailing_atividades_followup). m.id vai junto
        # no SELECT pra resolver a linha de mailing certa depois, em vez de
        # buscar de novo por "WHERE contrato=?" (que reintroduziria a mesma
        # ambiguidade na hora de pegar o cliente).
        fila = conn.execute("""
            SELECT f.contrato, m.id AS id_mailing_resolvido FROM followup f
            JOIN mailing m ON (f.id_mailing = m.id OR (f.id_mailing IS NULL AND m.contrato = f.contrato))
            WHERE f.status = 'PENDENTE' AND f.data_retorno <= ?
              AND (
                    f.operador = ?
                    OR f.operador IS NULL
                    OR NOT EXISTS (SELECT 1 FROM usuarios u WHERE u.nome = f.operador AND u.ativo = 1)
                    OR EXISTS (
                        SELECT 1 FROM ausencias a JOIN usuarios u ON u.id = a.usuario_id
                        WHERE u.nome = f.operador AND a.data = ? AND a.motivo IN ('Férias', 'Afastamento')
                    )
              )
              AND m.status = 'ENFILEIRADO'
              AND m.cidade NOT IN (SELECT cidade FROM cidades_bloqueio_distribuicao WHERE bloqueado=1)
              AND m.contrato NOT IN (SELECT contrato FROM contratos_blacklist)
            ORDER BY (CASE WHEN f.tipo = 'RETORNO' THEN 0 ELSE 1 END), f.data_retorno ASC LIMIT 1
        """, (agora_str, operador_nome, hoje_str_followup)).fetchone()

        cliente = None
        veio_do_followup = False
        if fila:
            cliente = conn.execute(
                "SELECT * FROM mailing WHERE id = ?", (fila["id_mailing_resolvido"],)
            ).fetchone()
            veio_do_followup = True

        # 3) Se não tiver followup vencido (ou se ele já foi tentado e perdeu a
        #    corrida), pega um PENDENTE novo — respeitando reservas exclusivas
        #    (reservado_para, criadas pela tela de Nova Tarefa, que sempre vencem
        #    -- é atribuição explícita, não uma preferência) e a prioridade
        #    cadastrada no mailing (maior primeiro).
        #
        #    "ultimo_operador" (devolver pro mesmo atendente que teve o contrato
        #    antes, liberado por inatividade ou reabertura) é só DESEMPATE entre
        #    prioridades IGUAIS -- não vence a prioridade em si. Antes vencia
        #    sempre, não importa quão baixa fosse: um contrato prioridade 1 ligado
        #    a mim como ultimo_operador enterrava um prioridade 99 de outra pessoa
        #    pra sempre (bug real encontrado pelo Bruno, 2026-09-11 -- mesma classe
        #    do bug de followup corrigido antes, só que com prioridade em vez de
        #    idade).
        if not cliente or cliente["id"] in ids_ja_tentados:
            veio_do_followup = False
            marcador = ",".join("?" * len(ids_ja_tentados)) if ids_ja_tentados else "-1"
            cliente = conn.execute(f"""
                SELECT * FROM mailing WHERE status = 'PENDENTE'
                  AND (reservado_para IS NULL OR reservado_para = ?)
                  AND cidade NOT IN (SELECT cidade FROM cidades_bloqueio_distribuicao WHERE bloqueado=1)
                  AND contrato NOT IN (SELECT contrato FROM contratos_blacklist)
                  AND id NOT IN ({marcador})
                ORDER BY
                    (CASE WHEN reservado_para = ? THEN 0 ELSE 1 END),
                    prioridade DESC,
                    (CASE WHEN ultimo_operador = ? THEN 0 ELSE 1 END),
                    id
                LIMIT 1
            """, (operador_nome, *ids_ja_tentados, operador_nome, operador_nome)).fetchone()

        if not cliente:
            break  # não sobrou mais nada disponível pra tentar

        # Candidato vindo do followup está com status ENFILEIRADO (nunca
        # PENDENTE) -- a reivindicação precisa checar o status certo pra cada
        # origem, senão falha sempre em silêncio pra followup e o contrato
        # nunca é reivindicado de volta (bug encontrado em 2026-09-04:
        # followups vencidos nunca retornavam pro operador, mesmo depois de
        # passar da regra de 12h -- a correção da condição de corrida, feita
        # antes, tinha travado só em 'PENDENTE', sem considerar essa origem).
        status_esperado = "ENFILEIRADO" if veio_do_followup else "PENDENTE"
        expira = agora + INATIVIDADE_LIMITE
        cursor = conn.execute(f"""
            UPDATE mailing SET status='EM_ANDAMENTO', operador=?, data_distribuicao=?, data_expiracao=?, reservado_para=NULL
            WHERE id=? AND status='{status_esperado}'
        """, (operador_nome, agora_str, expira.strftime("%Y-%m-%d %H:%M:%S"), cliente["id"]))

        if cursor.rowcount == 0:
            # Alguém reservou esse contrato entre a hora que a gente leu e a
            # hora que tentou reservar -- perdeu a corrida, tenta o próximo.
            conn.rollback()
            ids_ja_tentados.append(cliente["id"])
            continue

        # Só marca o followup de origem como tratado aqui quando o contrato
        # NÃO veio dele (limpeza de followup órfão/inconsistente, mesmo
        # comportamento de antes). Quando veio do followup, a reserva
        # continua valendo até um fechamento de verdade -- pega o contrato
        # não é a mesma coisa que tratar ele; marcar como TRATADO só no
        # ato de PEGAR era o que fazia a reserva se perder se a pessoa não
        # fechasse na hora (o contrato caía pra PENDENTE puro quando
        # EM_ANDAMENTO expirava, e qualquer outro operador podia pegar --
        # bug real reportado pelo Bruno, 2026-09-19, contratos 1537100 e
        # 401027863: Cristiane/Ana Carolina tabularam 2ª Tentativa, o
        # followup venceu e voltou certinho pra elas, mas como nenhuma das
        # duas fechou na hora, ele expirou sem dono e foi pro Guilherme).
        # registrar_tratamento() é quem marca como TRATADO de verdade, no
        # fechamento real (tanto pela fila quanto por Buscar Contrato).
        followup_dono_original = None
        if veio_do_followup:
            linha_followup_origem = conn.execute(
                "SELECT operador FROM followup WHERE status='PENDENTE' AND (id_mailing=? OR (id_mailing IS NULL AND contrato=?)) ORDER BY id DESC LIMIT 1",
                (cliente["id"], cliente["contrato"])
            ).fetchone()
            followup_dono_original = linha_followup_origem["operador"] if linha_followup_origem else None
        else:
            # Escopado por id_mailing (ou legado sem id_mailing) -- pra não
            # marcar como tratado o followup de OUTRA linha de mailing com o
            # mesmo número de contrato em cidade diferente (pedido do Bruno,
            # 2026-09-20).
            conn.execute("""
                UPDATE followup SET status='TRATADO'
                WHERE status='PENDENTE' AND (id_mailing=? OR (id_mailing IS NULL AND contrato=?))
            """, (cliente["id"], cliente["contrato"]))

        # Log de diagnóstico (ver _garantir_tabela_log_distribuicao) -- não
        # afeta a distribuição em si, só registra o caminho seguido.
        if veio_do_followup:
            origem_log = "followup_proprio" if followup_dono_original == operador_nome else "followup_outro"
        else:
            origem_log = "fila_geral"
        conn.execute("""
            INSERT INTO log_distribuicao (contrato, operador_recebeu, origem, followup_dono_original, data_hora)
            VALUES (?, ?, ?, ?, ?)
        """, (cliente["contrato"], operador_nome, origem_log, followup_dono_original, agora_str))

        conn.commit()
        cliente = conn.execute("SELECT * FROM mailing WHERE id=?", (cliente["id"],)).fetchone()
        conn.close()
        return cliente

    conn.close()
    return None


def buscar_proximo_gpon(analista_nome):
    """Mesma lógica de fila do atendimento normal, mas pra fila separada de
    análise de GPON Indisponível (status próprio, não se mistura com a fila
    dos operadores)."""
    conn = conectar()
    agora = datetime.now()
    agora_str = agora.strftime("%Y-%m-%d %H:%M:%S")

    # Se esse analista já está com um em análise, continua nele (mesma trava de 24h)
    atual = conn.execute("""
        SELECT * FROM mailing WHERE status='EM_ANALISE_GPON' AND operador=?
        ORDER BY data_distribuicao DESC LIMIT 1
    """, (analista_nome,)).fetchone()

    if atual:
        expira = datetime.strptime(atual["data_expiracao"], "%Y-%m-%d %H:%M:%S")
        if agora <= expira:
            nova_expira = agora + INATIVIDADE_LIMITE
            conn.execute("UPDATE mailing SET data_expiracao=? WHERE id=?",
                         (nova_expira.strftime("%Y-%m-%d %H:%M:%S"), atual["id"]))
            conn.commit()
            atual = conn.execute("SELECT * FROM mailing WHERE id=?", (atual["id"],)).fetchone()
            conn.close()
            return atual
        else:
            conn.execute("UPDATE mailing SET status='AGUARDANDO_ANALISE_GPON', operador=NULL WHERE id=?", (atual["id"],))
            conn.commit()

    # A reserva é feita com "UPDATE ... WHERE status='AGUARDANDO_ANALISE_GPON'" e
    # checando quantas linhas foram alteradas -- mesma correção de corrida aplicada
    # em buscar_proximo_cliente, pro mesmo bug não acontecer aqui também.
    ids_ja_tentados = []
    for _tentativa_reserva in range(5):
        marcador = ",".join("?" * len(ids_ja_tentados)) if ids_ja_tentados else "-1"
        cliente = conn.execute(f"""
            SELECT * FROM mailing WHERE status='AGUARDANDO_ANALISE_GPON'
              AND (data_retorno_gpon IS NULL OR data_retorno_gpon <= ?)
              AND id NOT IN ({marcador})
            ORDER BY (CASE WHEN data_retorno_gpon IS NULL THEN 1 ELSE 0 END), data_retorno_gpon ASC, id
            LIMIT 1
        """, (agora_str, *ids_ja_tentados)).fetchone()

        if not cliente:
            break

        expira = agora + INATIVIDADE_LIMITE
        cursor = conn.execute("""
            UPDATE mailing SET status='EM_ANALISE_GPON', operador=?, data_distribuicao=?, data_expiracao=?
            WHERE id=? AND status='AGUARDANDO_ANALISE_GPON'
        """, (analista_nome, agora_str, expira.strftime("%Y-%m-%d %H:%M:%S"), cliente["id"]))

        if cursor.rowcount == 0:
            conn.rollback()
            ids_ja_tentados.append(cliente["id"])
            continue

        conn.commit()
        cliente = conn.execute("SELECT * FROM mailing WHERE id=?", (cliente["id"],)).fetchone()
        conn.close()
        return cliente

    conn.close()
    return None


def quem_tabulou_gpon(conn, contrato):
    """Nome do operador que tabulou GPON Indisponível mais recentemente pra esse
    contrato -- é pra onde o item volta se o HP foi liberado mas não deu pra agendar."""
    linha = conn.execute("""
        SELECT operador FROM atividades WHERE contrato=? AND tabulacao='GPON Indisponível'
        ORDER BY id DESC LIMIT 1
    """, (contrato,)).fetchone()
    return linha["operador"] if linha else None


def operador_disponivel_hoje(conn, nome_operador):
    """Confirma se o operador ainda está ativo no cadastro (não foi
    desligado) e não está de Férias/Afastamento hoje -- usado pra decidir
    se um contrato volta reservado pra ele especificamente ou se fica livre
    pra distribuição normal.

    De propósito, NÃO conta ausência do dia a dia (Falta Justificada,
    Treinamento, Feriado, etc.) como indisponibilidade -- só Férias,
    Afastamento ou desligamento contam de verdade. Motivo (pedido do
    Bruno, 2026-09-10, revertendo uma ampliação de um dia antes): isso
    deveria ser excessão, não regra -- contar falta pontual faria contrato
    started por uma pessoa cair pra outra com frequência (o "dono" natural
    do followup só muda de mãos numa ausência longa/permanente, nunca por
    uma falta de um dia)."""
    if not nome_operador:
        return False
    usuario_row = conn.execute("SELECT ativo FROM usuarios WHERE nome=?", (nome_operador,)).fetchone()
    if not usuario_row or not usuario_row["ativo"]:
        return False
    hoje = datetime.now().strftime("%Y-%m-%d")
    ausencia = conn.execute("""
        SELECT a.id FROM ausencias a JOIN usuarios u ON u.id = a.usuario_id
        WHERE u.nome = ? AND a.data = ? AND a.motivo IN ('Férias', 'Afastamento')
    """, (nome_operador, hoje)).fetchone()
    return ausencia is None


# ==========================
# ESCALA / PAUSAS - HELPERS
# ==========================

def hoje_str():
    return datetime.now().strftime("%Y-%m-%d")


def obter_escala(conn, usuario_id):
    return conn.execute("SELECT * FROM escalas WHERE usuario_id=?", (usuario_id,)).fetchone()


def dia_trabalho(escala, agora):
    if not escala:
        return False
    dia_abrev = DIAS_SEMANA[agora.weekday()]
    return dia_abrev in (escala["dias_trabalho"] or "").split(",")


def dentro_da_escala(escala, agora):
    if not dia_trabalho(escala, agora):
        return False
    h_ini = datetime.strptime(escala["hora_inicio"], "%H:%M").time()
    h_fim = datetime.strptime(escala["hora_fim"], "%H:%M").time()
    return h_ini <= agora.time() <= h_fim


def pausas_do_dia(conn, usuario_id, data=None):
    data = data or hoje_str()
    return conn.execute(
        "SELECT * FROM pausas WHERE usuario_id=? AND data=? ORDER BY id", (usuario_id, data)
    ).fetchall()


def pausa_aberta(conn, usuario_id):
    return conn.execute(
        "SELECT * FROM pausas WHERE usuario_id=? AND data=? AND fim_real IS NULL",
        (usuario_id, hoje_str())
    ).fetchone()


def ja_fez(pausas, tipo):
    return any(p["tipo"] == tipo for p in pausas)


# ==========================
# TRATAMENTO/PAUSAS -- duração, origem do encerramento e timeline gráfica
# (pedido do Bruno, 2026-09-20: coluna de tempo total, filtro de data no
# histórico, saber se o logout foi manual/automático/supervisão, e uma
# linha do tempo gráfica do dia -- ver rota pausas() e Pausas.html)
# ==========================

_CORES_TIMELINE = {
    "SEM_SESSAO": ("#e9ecef", "Sem sessão"),
    "TRABALHO": ("#0d6efd", "Em atendimento"),
    "PAUSA_MANHA": ("#fd7e14", "Pausa manhã"),
    "ALMOCO": ("#198754", "Almoço"),
    "PAUSA_TARDE": ("#6f42c1", "Pausa tarde"),
    "BANHEIRO": ("#ffc107", "Pausa banheiro"),
}


def _formatar_duracao(inicio_str, fim_str, data_consulta, agora=None):
    """Formata 'Xh Ymin' (ou só 'Ymin' se < 1h) entre dois timestamps
    'YYYY-MM-DD HH:MM:SS'. Se fim_str for None: usa "agora" como fim SÓ se
    data_consulta for hoje (sessão/pausa ainda aberta de verdade); num dia
    passado sem fim registrado (não deveria acontecer, o encerramento
    automático fecha tudo todo dia, mas é defensivo) não dá pra saber
    quanto durou -- retorna '—' em vez de inventar uma duração até agora
    que não faz sentido pra um dia que já passou."""
    if not inicio_str:
        return "—"
    agora = agora or datetime.now()
    try:
        inicio = datetime.strptime(inicio_str, "%Y-%m-%d %H:%M:%S")
        if fim_str:
            fim = datetime.strptime(fim_str, "%Y-%m-%d %H:%M:%S")
        elif data_consulta == hoje_str():
            fim = agora
        else:
            return "—"
    except ValueError:
        return "—"
    minutos_totais = max(0, int((fim - inicio).total_seconds() // 60))
    horas, minutos = divmod(minutos_totais, 60)
    return f"{horas}h {minutos}min" if horas else f"{minutos}min"


def _rotulo_encerramento(encerrado_por, nome_proprio):
    """Traduz o valor cru gravado em sessoes.encerrado_por (ver
    _garantir_coluna_encerrado_por_sessoes) pro texto que aparece na tela."""
    if not encerrado_por:
        return "—"
    if encerrado_por == "AUTOMATICO":
        return "Automático (18h30)"
    if encerrado_por == "DESLIGAMENTO":
        return "Desligamento"
    if encerrado_por.startswith("SUPERVISOR:"):
        return f"Supervisão ({encerrado_por.split(':', 1)[1]})"
    return "Manual"


def _montar_timeline_dia(jornadas, pausas, data_consulta):
    """Monta os segmentos coloridos da linha do tempo gráfica do dia
    consultado -- lista de dicts {esquerda_pct, largura_pct, cor, label,
    detalhe} prontos pra virar <div style="left:X%; width:Y%"> no template,
    mais os marcadores de hora embaixo da barra. Retorna None se não tiver
    nenhuma jornada nesse dia (nada pra desenhar).

    Camadas (a de cima ganha, quando períodos se sobrepõem -- pausa sempre
    "recorta" o trabalho, nunca o contrário): TRABALHO (azul) por baixo, uma
    pra cada jornada; PAUSA_* por cima, uma pra cada pausa."""
    if not jornadas:
        return None

    agora = datetime.now()
    eh_hoje = data_consulta == hoje_str()

    def minuto_do_dia(timestamp_str):
        dt = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
        return dt.hour * 60 + dt.minute + dt.second / 60

    minuto_agora = (agora.hour * 60 + agora.minute) if eh_hoje else None

    # Camada 0: trabalho (uma faixa por jornada; sem logout ainda -> vai até
    # agora se for hoje, senão até o fim do dia -- defensivo, não deveria
    # acontecer numa data passada).
    faixas_trabalho = []
    limites = []
    for j in jornadas:
        inicio = minuto_do_dia(j["login_em"])
        fim = minuto_do_dia(j["logout_em"]) if j["logout_em"] else (minuto_agora if eh_hoje else 1440)
        fim = max(fim, inicio)
        faixas_trabalho.append((inicio, fim))
        limites.extend([inicio, fim])

    # Camada 1: pausas (mesma lógica de fim em aberto).
    faixas_pausa = []
    for p in pausas:
        if not p["inicio"]:
            continue
        inicio = minuto_do_dia(p["inicio"])
        fim = minuto_do_dia(p["fim_real"]) if p["fim_real"] else (minuto_agora if eh_hoje else 1440)
        fim = max(fim, inicio)
        faixas_pausa.append((inicio, fim, p["tipo"]))
        limites.extend([inicio, fim])

    if not limites:
        return None

    # Janela de exibição: do início da primeira jornada até o fim da
    # última (ou "agora"), com 30min de folga de cada lado, arredondado pra
    # hora cheia -- pra sempre sobrar uma margem cinza mostrando que a
    # pessoa "ainda não tinha logado"/"já tinha saído", sem cortar nada.
    janela_inicio = max(0, (min(limites) - 30) // 60 * 60)
    janela_fim = min(1440, -(-(max(limites) + 30) // 60) * 60)  # teto arredondado pra hora
    if janela_fim <= janela_inicio:
        janela_fim = janela_inicio + 60
    duracao_janela = janela_fim - janela_inicio

    def cor_ativa(meio):
        for inicio, fim, tipo in faixas_pausa:
            if inicio <= meio < fim:
                return tipo
        for inicio, fim in faixas_trabalho:
            if inicio <= meio < fim:
                return "TRABALHO"
        return "SEM_SESSAO"

    # Pontos de corte (todo início/fim de qualquer faixa, mais as bordas da
    # janela) -- entre dois pontos consecutivos, o tipo ativo não muda.
    pontos = {janela_inicio, janela_fim}
    for inicio, fim in faixas_trabalho:
        pontos.add(max(janela_inicio, min(janela_fim, inicio)))
        pontos.add(max(janela_inicio, min(janela_fim, fim)))
    for inicio, fim, _tipo in faixas_pausa:
        pontos.add(max(janela_inicio, min(janela_fim, inicio)))
        pontos.add(max(janela_inicio, min(janela_fim, fim)))
    pontos = sorted(pontos)

    segmentos_brutos = []
    for t1, t2 in zip(pontos, pontos[1:]):
        if t2 <= t1:
            continue
        tipo = cor_ativa((t1 + t2) / 2)
        segmentos_brutos.append((t1, t2, tipo))

    # Funde segmentos vizinhos do mesmo tipo (evita uma faixa cinza
    # picotada em várias tirinhas de 1min por causa dos pontos de corte).
    segmentos_fundidos = []
    for t1, t2, tipo in segmentos_brutos:
        if segmentos_fundidos and segmentos_fundidos[-1][2] == tipo and segmentos_fundidos[-1][1] == t1:
            segmentos_fundidos[-1] = (segmentos_fundidos[-1][0], t2, tipo)
        else:
            segmentos_fundidos.append((t1, t2, tipo))

    def hhmm(minuto):
        return f"{int(minuto // 60):02d}:{int(minuto % 60):02d}"

    segmentos = []
    for t1, t2, tipo in segmentos_fundidos:
        cor, label = _CORES_TIMELINE[tipo]
        segmentos.append({
            "esquerda_pct": (t1 - janela_inicio) / duracao_janela * 100,
            "largura_pct": (t2 - t1) / duracao_janela * 100,
            "cor": cor,
            "label": label,
            "detalhe": f"{label} ({hhmm(t1)} – {hhmm(t2)})",
        })

    # Marcadores de hora embaixo da barra -- de hora em hora se a janela for
    # curta (<= 8h), de 2 em 2h se for mais longa, pra não amontoar.
    passo = 60 if duracao_janela <= 480 else 120
    marcadores = []
    marca = janela_inicio
    while marca <= janela_fim:
        marcadores.append({
            "esquerda_pct": (marca - janela_inicio) / duracao_janela * 100,
            "label": hhmm(marca),
        })
        marca += passo

    return {"segmentos": segmentos, "marcadores": marcadores}


def obter_duracoes(conn):
    """Duração (timedelta) da pausa curta, do almoço e da pausa banheiro,
    configuráveis pelo ADM na tela Tratamento/Pausas. Cai nos valores padrão
    se a config estiver vazia."""
    linha = conn.execute(
        "SELECT duracao_pausa_min, duracao_almoco_min, duracao_banheiro_min FROM configuracoes WHERE id=1"
    ).fetchone()
    pausa_min = (linha["duracao_pausa_min"] if linha and linha["duracao_pausa_min"] else 10)
    almoco_min = (linha["duracao_almoco_min"] if linha and linha["duracao_almoco_min"] else 60)
    banheiro_min = (linha["duracao_banheiro_min"] if linha and linha["duracao_banheiro_min"] else 10)
    return timedelta(minutes=pausa_min), timedelta(minutes=almoco_min), timedelta(minutes=banheiro_min)


def _garantir_colunas_limite_pausa():
    """Auto-migração leve (mesmo padrão de _garantir_coluna_data_cadastro): garante
    que 'configuracoes' tem as 4 colunas de horário-limite de pausa, sem precisar de
    script nem console -- só trocar o app.py e dar Reload já aplica. Fica NULL até o
    ADM salvar algo em Tratamento/Pausas; enquanto for NULL, obter_limites_pausa()
    cai nos valores padrão da empresa (*_PADRAO, lá em cima)."""
    conn = conectar()
    for coluna in ("limite_pausa_manha", "limite_almoco_inicio", "limite_almoco_fim", "limite_pausa_tarde",
                   "limite_pausa_manha_inicio", "limite_pausa_tarde_inicio"):
        try:
            conn.execute(f"ALTER TABLE configuracoes ADD COLUMN {coluna} TEXT")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # coluna já existe -- migração já rodou antes
    conn.close()


_garantir_colunas_limite_pausa()


def _garantir_coluna_prazo_followup():
    """Auto-migração leve (mesmo padrão de _garantir_coluna_data_cadastro): garante
    que 'configuracoes' tem a coluna do prazo (em horas) do follow-up automático de
    2ª/3ª Tentativa, sem precisar de script nem console -- só trocar o app.py e dar
    Reload já aplica. Fica NULL até o ADM salvar algo em Tratamento/Pausas; enquanto
    for NULL, obter_prazo_followup() cai no padrão (PRAZO_FOLLOWUP_HORAS_PADRAO)."""
    conn = conectar()
    try:
        conn.execute("ALTER TABLE configuracoes ADD COLUMN prazo_followup_horas INTEGER")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # coluna já existe -- migração já rodou antes
    conn.close()


_garantir_coluna_prazo_followup()

PRAZO_FOLLOWUP_HORAS_PADRAO = 12  # antes era fixo em 4h no código; agora configurável pelo ADM


def _garantir_coluna_minutos_efetivos():
    """Auto-migração leve (mesmo padrão de _garantir_coluna_prazo_followup):
    garante que 'atividades' tem a coluna 'minutos_efetivos', usada pro
    "tempo efetivo de tela" (pedido do Bruno em 2026-08-26 -- desconta pausa e
    tempo deslogado do tempo médio de atendimento). Fica NULL pra qualquer
    atividade gravada antes dessa migração (não dá pra recalcular retroativo
    com precisão -- o Dashboard/Qualidade caem de volta no cálculo antigo,
    relógio corrido, só pra essas linhas antigas; toda atividade nova a partir
    daqui já grava o valor certo)."""
    conn = conectar()
    try:
        conn.execute("ALTER TABLE atividades ADD COLUMN minutos_efetivos REAL")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # coluna já existe -- migração já rodou antes
    conn.close()


_garantir_coluna_minutos_efetivos()


def obter_prazo_followup(conn):
    """Prazo (em horas) até um follow-up de 2ª/3ª Tentativa voltar pra fila,
    configurável pelo ADM em Tratamento/Pausas. Cai no padrão se o ADM nunca
    configurou nada (coluna NULL) ou se o valor salvo for inválido."""
    linha = conn.execute("SELECT prazo_followup_horas FROM configuracoes WHERE id=1").fetchone()
    valor = linha["prazo_followup_horas"] if linha else None
    if valor is None:
        return PRAZO_FOLLOWUP_HORAS_PADRAO
    try:
        valor = int(valor)
        return valor if valor > 0 else PRAZO_FOLLOWUP_HORAS_PADRAO
    except (TypeError, ValueError):
        return PRAZO_FOLLOWUP_HORAS_PADRAO


def obter_limites_pausa(conn):
    """Horários (início e fim) de cada pausa, configuráveis pelo ADM em
    Tratamento/Pausas. Cai nos valores padrão da empresa se o ADM nunca
    configurou nada (coluna NULL) ou se o valor salvo estiver num formato
    inválido. Cada pausa só fica disponível dentro da própria janela -- sem
    isso (antes de 2026-09-04), manhã e tarde só tinham horário de FIM,
    ficando disponíveis o dia inteiro até vencer e se sobrepondo uma com a
    outra."""
    linha = conn.execute("""
        SELECT limite_pausa_manha, limite_pausa_manha_inicio, limite_almoco_inicio, limite_almoco_fim,
               limite_pausa_tarde_inicio, limite_pausa_tarde
        FROM configuracoes WHERE id=1
    """).fetchone()

    def _parse(valor, padrao):
        if not valor:
            return padrao
        try:
            return datetime.strptime(valor, "%H:%M").time()
        except ValueError:
            return padrao

    manha_inicio = _parse(linha["limite_pausa_manha_inicio"] if linha else None, PAUSA_MANHA_INICIO_PADRAO)
    pausa_manha = _parse(linha["limite_pausa_manha"] if linha else None, PAUSA_MANHA_LIMITE_PADRAO)
    almoco_inicio = _parse(linha["limite_almoco_inicio"] if linha else None, ALMOCO_JANELA_INICIO_PADRAO)
    almoco_fim = _parse(linha["limite_almoco_fim"] if linha else None, ALMOCO_LIMITE_PADRAO)
    tarde_inicio = _parse(linha["limite_pausa_tarde_inicio"] if linha else None, PAUSA_TARDE_INICIO_PADRAO)
    pausa_tarde = _parse(linha["limite_pausa_tarde"] if linha else None, PAUSA_TARDE_LIMITE_PADRAO)
    return manha_inicio, pausa_manha, almoco_inicio, almoco_fim, tarde_inicio, pausa_tarde


def _garantir_coluna_data_entrada_fila():
    """Auto-migração leve (mesmo padrão de _garantir_coluna_prazo_followup):
    garante que 'mailing' tem a coluna 'data_entrada_fila', usada pro alerta
    de "lead envelhecendo" (pedido do Bruno, 2026-08-27 -- quanto tempo um
    contrato fica esperando o primeiro contato, ou parado numa 2ª/3ª
    Tentativa). Marca a última vez que o contrato entrou no estado PENDENTE
    (aguardando alguém pegar) -- todo lugar do código que muda um contrato
    pra PENDENTE (import novo, reabertura de Improdutivo, timeout de 24h,
    Desligar/Excluir, transferência aprovada, Nova Tarefa) grava esse
    timestamp de novo, então a idade sempre reflete a entrada mais recente na
    fila, não a criação original do registro."""
    conn = conectar()
    try:
        conn.execute("ALTER TABLE mailing ADD COLUMN data_entrada_fila TEXT")
        conn.commit()
        # Backfill: contratos PENDENTE que já existiam antes dessa migração
        # nascem com a data de HOJE, não uma data reconstruída -- mesmo
        # cuidado já usado em usuarios.data_cadastro (evita que a própria
        # subida dessa migração jogue todo o backlog acumulado direto pro
        # alerta de "envelhecido").
        agora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE mailing SET data_entrada_fila=? WHERE status='PENDENTE' AND data_entrada_fila IS NULL",
            (agora_str,)
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass  # coluna já existe -- migração já rodou antes
    conn.close()


_garantir_coluna_data_entrada_fila()


def _garantir_colunas_envelhecimento_lead():
    """Auto-migração leve: garante que 'configuracoes' tem as 2 colunas dos
    limites do alerta de "lead envelhecendo", configuráveis pelo ADM em
    Tratamento/Pausas. Ficam NULL até o ADM salvar algo; enquanto NULL,
    obter_limites_envelhecimento() cai nos padrões combinados com o Bruno em
    2026-08-27."""
    conn = conectar()
    for coluna in ("limite_lead_nunca_contatado_dias", "limite_followup_travado_horas"):
        try:
            conn.execute(f"ALTER TABLE configuracoes ADD COLUMN {coluna} INTEGER")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # coluna já existe -- migração já rodou antes
    conn.close()


_garantir_colunas_envelhecimento_lead()

LIMITE_LEAD_NUNCA_CONTATADO_DIAS_PADRAO = 15  # combinado com o Bruno, 2026-08-27
LIMITE_FOLLOWUP_TRAVADO_HORAS_PADRAO = 48  # combinado com o Bruno, 2026-08-27


def obter_limites_envelhecimento(conn):
    """Limites (dias / horas) do alerta de "lead envelhecendo", configuráveis
    pelo ADM em Tratamento/Pausas. Cai nos padrões combinados com o Bruno se o
    ADM nunca configurou nada (coluna NULL) ou se o valor salvo for inválido."""
    linha = conn.execute("""
        SELECT limite_lead_nunca_contatado_dias, limite_followup_travado_horas
        FROM configuracoes WHERE id=1
    """).fetchone()

    def _parse(valor, padrao):
        if valor is None:
            return padrao
        try:
            valor = int(valor)
            return valor if valor > 0 else padrao
        except (TypeError, ValueError):
            return padrao

    dias_nunca_contatado = _parse(linha["limite_lead_nunca_contatado_dias"] if linha else None, LIMITE_LEAD_NUNCA_CONTATADO_DIAS_PADRAO)
    horas_followup = _parse(linha["limite_followup_travado_horas"] if linha else None, LIMITE_FOLLOWUP_TRAVADO_HORAS_PADRAO)
    return dias_nunca_contatado, horas_followup


def calcular_leads_envelhecidos(conn):
    """Alerta de "lead envelhecendo" (pedido do Bruno, 2026-08-27, a partir de
    um gap levantado na opinião de negócio): quanto tempo, em média, um
    contrato fica esperando o primeiro contato (status PENDENTE) ou parado
    numa 2ª/3ª Tentativa (follow-up já vencido, contrato ainda ENFILEIRADO).
    Usa só dado que já existe hoje (mailing/followup) -- não antecipa os
    relatórios futuros de execução em campo/quebra de agenda combinados com o
    Bruno na mesma conversa; quando esses entrarem, este indicador ganha
    companhia, não substituição."""
    dias_nunca_contatado, horas_followup = obter_limites_envelhecimento(conn)
    agora = datetime.now()

    # -- Nunca contatado: status PENDENTE, idade = agora - data_entrada_fila.
    pendentes = conn.execute("""
        SELECT data_entrada_fila FROM mailing
        WHERE status='PENDENTE' AND data_entrada_fila IS NOT NULL
    """).fetchall()
    idades_horas = []
    for linha in pendentes:
        try:
            entrada = datetime.strptime(linha["data_entrada_fila"], "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            continue
        idades_horas.append((agora - entrada).total_seconds() / 3600)

    limite_pendente_horas = dias_nunca_contatado * 24
    qtd_pendente_envelhecido = sum(1 for h in idades_horas if h > limite_pendente_horas)
    media_dias_pendente = (sum(idades_horas) / len(idades_horas) / 24) if idades_horas else 0.0

    # -- Parado em follow-up: followup ainda PENDENTE, com data_retorno já no
    #    passado, e o contrato ainda ENFILEIRADO -- mesma trava usada em
    #    buscar_proximo_cliente(), pra não contar followup obsoleto de um
    #    contrato que já foi fechado por outro caminho nesse meio-tempo.
    agora_str = agora.strftime("%Y-%m-%d %H:%M:%S")
    followups_vencidos = conn.execute("""
        SELECT f.data_retorno FROM followup f
        JOIN mailing m ON m.contrato = f.contrato
        WHERE f.status='PENDENTE' AND m.status='ENFILEIRADO' AND f.data_retorno <= ?
    """, (agora_str,)).fetchall()
    atrasos_horas = []
    for linha in followups_vencidos:
        try:
            retorno = datetime.strptime(linha["data_retorno"], "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            continue
        atrasos_horas.append((agora - retorno).total_seconds() / 3600)

    qtd_followup_travado = sum(1 for h in atrasos_horas if h > horas_followup)
    media_horas_atraso_followup = (sum(atrasos_horas) / len(atrasos_horas)) if atrasos_horas else 0.0

    return {
        "qtd_pendente_envelhecido": qtd_pendente_envelhecido,
        "media_dias_pendente": round(media_dias_pendente, 1),
        "limite_pendente_dias": dias_nunca_contatado,
        "qtd_followup_travado": qtd_followup_travado,
        "media_horas_atraso_followup": round(media_horas_atraso_followup, 1),
        "limite_followup_horas": horas_followup,
    }


# ==========================
# EXECUÇÕES DE OS (pedido do Bruno, 2026-08-28 -- é a "companhia" do alerta de
# lead envelhecendo, acima: aquele mostrava a fila DENTRO da ferramenta; isso
# aqui mostra o que de fato aconteceu EM CAMPO, vindo do CRM oficial da
# empresa. Todo dia o Bruno extrai de lá as OS's do tipo "MUDANCA DE PACOTE"
# baixadas (executadas) e sobe esse CSV aqui (D-1) em /admin/importacao.
#
# Chave de identidade escolhida com o Bruno: cod_operadora (código da
# cidade no sistema deles, ex: 052=Campinas) + contrato + data de baixa --
# NÃO o número da OS (achamos, no arquivo de exemplo, pares de OS com o
# mesmo número mas contrato/data totalmente diferentes -- não é estável o
# bastante pra chave). A data de baixa entra na chave pra não perder
# execuções DISTINTAS do mesmo contrato em datas diferentes (contrato pode
# passar por mais de uma migração de pacote ao longo do tempo).
#
# Atribuição ao operador é por LOGIN (CD_LOGIN_ABERT_ORDEM_SERVICO, com
# fallback pra CD_LOGIN_ABERTURA_SOLICITACAO) -- existe em toda OS, não
# depende de o contrato ter sido agendado na nossa ferramenta. Contrato
# "solto" (sem contrato correspondente em mailing/atividades) ainda soma no
# resultado geral, só não aparece linkado a um agendamento específico.
# ==========================

def _garantir_tabelas_execucoes_os():
    """Auto-migração (mesmo padrão de sempre -- CREATE TABLE IF NOT EXISTS,
    roda 1x a cada subida)."""
    conn = conectar()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cidades_operadora (
            cod_operadora TEXT PRIMARY KEY,
            cidade TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS execucoes_os (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cod_operadora TEXT NOT NULL,
            contrato TEXT NOT NULL,
            dh_baixa TEXT NOT NULL,
            nr_ordem_servico TEXT,
            login_abertura TEXT,
            nome_abertura_os TEXT,
            tipo_os TEXT,
            data_importacao TEXT,
            UNIQUE(cod_operadora, contrato, dh_baixa)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_execucoes_os_contrato ON execucoes_os(contrato)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_execucoes_os_login ON execucoes_os(login_abertura)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_execucoes_os_dh_baixa ON execucoes_os(dh_baixa)")
    # Faltava índice em cod_operadora -- é exatamente a coluna que
    # admin_cidades_operadora() agrupa (GROUP BY) pra montar "Ainda sem
    # cidade"; sem índice, essa consulta varre a tabela inteira toda vez que
    # a tela é aberta (2026-08-28, investigando relato do Bruno de que
    # /admin/cidades-operadora não carregava -- pode ser isso batendo no
    # limite de CPU do plano PythonAnywhere conforme execucoes_os cresce).
    conn.execute("CREATE INDEX IF NOT EXISTS idx_execucoes_os_cod_operadora ON execucoes_os(cod_operadora)")
    # Seed dos códigos que o Bruno já passou de cabeça (2026-08-28) -- INSERT
    # OR IGNORE pra não sobrescrever se ele já tiver editado algum pela tela
    # de Admin. O resto ele completa aos poucos por lá, conforme for lembrando.
    for cod_operadora, cidade in (("52", "CAMPINAS"), ("7", "SOROCABA"), ("162", "SAO JOSE DOS CAMPOS"), ("4", "SANTOS")):
        conn.execute("INSERT OR IGNORE INTO cidades_operadora (cod_operadora, cidade) VALUES (?, ?)", (cod_operadora, cidade))
    conn.commit()
    conn.close()


_garantir_tabelas_execucoes_os()


# ==========================
# QUEBRA DE AGENDA -- pedido pelo Bruno 2026-08-29. Métrica: total de OS
# marcadas "COM QUEBRA DE AGENDA" / TOTAL DE OS -- EXPURGADA CONTA no total
# (corrigido em 2026-08-29, mesmo dia, 2º ajuste: eu tinha excluído EXPURGADA
# do total numa primeira versão, o Bruno corrigiu -- "não precisa
# desconsiderar expurgado, ela conta também"; só não entra no numerador
# porque seu tipo_tratamento é diferente de 'COM QUEBRA DE AGENDA'). Fonte
# oficial: planilha "amostra_qualificada" (analisada e escolhida em cima da
# alternativa em CSV que o Bruno também mandou -- menos linhas pra agosto,
# datas fora do mês, OS duplicada por reagendamento -- ver documento técnico
# do projeto). Reenviada pelo Bruno TODO DIA com o acumulado do mês inteiro
# (não é um delta) -- upsert por cod_os, então reimportar não duplica; a
# fila "D-1" de tratamento filtra por DAT_AGENDA = ontem dentro do arquivo
# reimportado, sem precisar de diff de verdade.
#
# Segmentação Célula Local x Regional (3ª e definitiva versão, 2026-08-29):
# NEM cidade/operadora NEM canal DAC servem como critério -- o Bruno
# explicou que todos os canais (DAC incluso) atuam em TODAS as cidades, e
# cidade sozinha também não isola o time dele. O critério certo é por
# CONTRATO: cruzar os contratos que NÓS agendamos de fato na ferramenta
# (atividades.tabulacao='Agendado', join com mailing pra pegar a cidade)
# contra os contratos da planilha de quebra. Como o mesmo número de
# contrato pode existir em cidades diferentes, a chave de cruzamento é
# composta -- cidade + contrato (normalizando a cidade: a planilha vem tipo
# "São José dos Campos" e o nosso mailing vem "SAO JOSE DOS CAMPOS", sem
# acento e maiúsculo, então a comparação remove acento/caixa dos dois lados
# -- ver _normalizar_cidade_quebra() / _chave_cidade_contrato()). Quando o
# contrato+cidade da quebra bate com um contrato que nós agendamos, o
# canal_venda gravado vira literalmente "CELULA LOCAL" (sobrescrevendo o
# nome original do canal); os demais mantêm o DSC_CANAL_VENDA original
# intacto, incluindo quando é "DAC" de outro time (não é reclassificado).
# Ver classificar_segmento_quebra() e contratos_agendados_por_nos().
# A métrica em si é calculada por canal de venda: Célula Local é um canal
# entre os outros, e a soma de TODOS os canais (Célula Local incluída) é a
# quebra da Regional -- por isso o indicador do Dashboard usa a base
# inteira (resumo(None)/bucketizar(None)) na perna Regional, não um filtro
# de segmento "REGIONAL" exclusivo (isso também resolve o "Regional sempre
# zerado": o export diário do Bruno só cobre as cidades do time dele, então
# filtrar por um segmento "REGIONAL" nunca achava nada). Ver
# calcular_indicador_quebra_totais(). A tabela "por segmento" da página de
# resultado (/quebra/resultado) continua mostrando Célula Local x Regional
# como uma visão mutuamente exclusiva -- isso não muda, é uma tabela
# informativa diferente do indicador do Dashboard.
#
# Histórico do dia (documentando pra não confundir quem ler depois): 1ª
# versão usou COD_OPERADORA/cidades_operadora; 2ª versão trocou pra
# DSC_CANAL_VENDA_SUBGRUPO=='DAC' (o Bruno apontou que DAC não é exclusivo
# do time dele); esta 3ª versão é a que ficou -- por contrato mesmo,
# cruzando com o que a gente realmente agendou.
# ==========================

def _garantir_tabelas_quebra_agenda():
    conn = conectar()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS quebras_agenda (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cod_os TEXT UNIQUE NOT NULL,
            contrato TEXT,
            cod_operadora TEXT,
            cidade TEXT,
            segmento TEXT NOT NULL,
            canal_venda TEXT,
            tipo_tratamento TEXT NOT NULL,
            responsavel_quebra TEXT,
            agrupamento_quebra TEXT,
            sub_agrupamento TEXT,
            dat_agenda TEXT,
            tecnico TEXT,
            motivo_reagendamento TEXT,
            vinculado_mailing INTEGER NOT NULL DEFAULT 0,
            data_importacao TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS quebras_tratamento (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            quebra_id INTEGER NOT NULL,
            operador TEXT NOT NULL,
            tabulacao TEXT NOT NULL,
            submotivo TEXT,
            observacao TEXT,
            data_tratamento TEXT NOT NULL,
            FOREIGN KEY (quebra_id) REFERENCES quebras_agenda(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_quebras_agenda_dat_agenda ON quebras_agenda(dat_agenda)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_quebras_agenda_segmento ON quebras_agenda(segmento)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_quebras_agenda_contrato ON quebras_agenda(contrato)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_quebras_tratamento_quebra_id ON quebras_tratamento(quebra_id)")
    conn.commit()
    conn.close()


_garantir_tabelas_quebra_agenda()


def _garantir_tabela_bloqueio_historico():
    """Histórico das decisões de bloqueio de cidade -- pedido do Bruno,
    2026-10-19.

    A tabela cidades_bloqueio_distribuicao guarda só o estado ATUAL: a tela
    de salvar regrava as 36 cidades de uma vez, então o atualizado_em de
    todas fica igual e não dá pra saber quando uma cidade entrou ou saiu do
    bloqueio. Sem isso não há como avaliar depois se o bloqueio funcionou
    como válvula de capacidade (a agenda voltou a encurtar?) nem o que ele
    custou em conversão.

    Aqui só entra o que REALMENTE mudou -- uma linha por cidade por
    alteração de estado, não a cada clique em salvar.
    """
    conn = conectar()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cidades_bloqueio_historico (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cidade TEXT NOT NULL,
            bloqueado INTEGER NOT NULL,
            bloqueado_antes INTEGER,
            motivo TEXT,
            alterado_em TEXT NOT NULL,
            alterado_por TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bloqueio_hist_cidade ON cidades_bloqueio_historico(cidade, alterado_em)")

    # Marco zero: sem uma linha de partida, a primeira mudança futura não
    # tem "antes" com data conhecida. Grava o estado de hoje uma única vez.
    ja_tem = conn.execute("SELECT COUNT(*) c FROM cidades_bloqueio_historico").fetchone()["c"]
    if not ja_tem:
        agora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for linha in conn.execute("SELECT cidade, bloqueado FROM cidades_bloqueio_distribuicao").fetchall():
            conn.execute("""
                INSERT INTO cidades_bloqueio_historico
                    (cidade, bloqueado, bloqueado_antes, motivo, alterado_em, alterado_por)
                VALUES (?, ?, NULL, 'estado inicial registrado', ?, 'SISTEMA')
            """, (linha["cidade"], linha["bloqueado"], agora_str))
    conn.commit()
    conn.close()


_garantir_tabela_bloqueio_historico()


def _garantir_coluna_canal_venda_original_quebra():
    """Auto-migração leve: guarda o DSC_CANAL_VENDA original da planilha
    mesmo quando a linha é reclassificada como "CELULA LOCAL" -- pedido do
    Bruno, 2026-10-16, junto com a regra de data da quebra. Antes o canal
    original era sobrescrito e perdido; se a classificação mudar depois
    (recálculo do segmento), dá pra devolver o canal certo."""
    conn = conectar()
    try:
        conn.execute("ALTER TABLE quebras_agenda ADD COLUMN canal_venda_original TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # coluna já existe
    conn.close()


_garantir_coluna_canal_venda_original_quebra()


def _garantir_coluna_motivo_reagendamento_quebra():
    """Auto-migração leve (mesmo padrão de _garantir_coluna_meta_execucoes):
    coluna motivo_reagendamento em quebras_agenda, pra guardar o texto da
    coluna MOTIVO_REAGENDAMENTO da planilha de origem e mostrar pro
    assistente na tela de tratamento da quebra -- pedido do Bruno, 2026-08-29.
    Só precisa pra bancos que já existiam antes dessa mudança; banco novo já
    nasce com a coluna (ver CREATE TABLE em _garantir_tabelas_quebra_agenda)."""
    conn = conectar()
    try:
        conn.execute("ALTER TABLE quebras_agenda ADD COLUMN motivo_reagendamento TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # coluna já existe -- migração já rodou antes
    conn.close()


_garantir_coluna_motivo_reagendamento_quebra()


def _quebras_do_periodo(conn, data_inicio, data_fim, operador_nome=None):
    """Linhas de quebras_agenda no período. EXPURGADA conta no total (pedido
    do Bruno, 2026-08-29 -- reverte a exclusão anterior); só não entra no
    numerador de "com quebra" porque seu tipo_tratamento é diferente de
    'COM QUEBRA DE AGENDA'. Se operador_nome for passado, restringe às
    quebras de contratos que esse operador especificamente agendou (visão
    pessoal) -- cruza por contrato com atividades.tabulacao='Agendado'."""
    if operador_nome:
        return conn.execute("""
            SELECT dat_agenda, tipo_tratamento, segmento FROM quebras_agenda
            WHERE dat_agenda BETWEEN ? AND ?
              AND contrato IN (SELECT DISTINCT contrato FROM atividades WHERE tabulacao='Agendado' AND operador=?)
        """, (data_inicio, data_fim, operador_nome)).fetchall()
    return conn.execute("""
        SELECT dat_agenda, tipo_tratamento, segmento FROM quebras_agenda
        WHERE dat_agenda BETWEEN ? AND ?
    """, (data_inicio, data_fim)).fetchall()


def calcular_indicador_quebra_totais(conn, data_inicio, data_fim, operador_nome=None):
    """Cards do indicador: total de OS / com quebra / taxa (métrica é sempre
    COM QUEBRA / TOTAL DE OS, EXPURGADA inclusa no total). Visão de equipe
    devolve Célula Local e Regional lado a lado; visão pessoal devolve só o
    resultado do próprio operador. Corrigido em 2026-08-29 a pedido do
    Bruno: "Regional" NÃO é o complemento exclusivo de Célula Local -- é a
    base INTEIRA (todo mundo, Célula Local incluída), por isso usa
    resumo(None) em vez de filtrar segmento=='REGIONAL' (que ficava sempre
    zerado em produção, já que o export diário só cobre as cidades do time
    do Bruno)."""
    linhas = _quebras_do_periodo(conn, data_inicio, data_fim, operador_nome)

    def resumo(filtro_segmento):
        total = sum(1 for l in linhas if filtro_segmento is None or l["segmento"] == filtro_segmento)
        com_quebra = sum(
            1 for l in linhas
            if (filtro_segmento is None or l["segmento"] == filtro_segmento) and l["tipo_tratamento"] == "COM QUEBRA DE AGENDA"
        )
        taxa = round(com_quebra / total * 100, 1) if total else None
        return {"total": total, "com_quebra": com_quebra, "taxa": taxa}

    if operador_nome:
        return {"pessoal": resumo(None)}
    return {"celula_local": resumo("CELULA LOCAL"), "regional": resumo(None)}


GRANULARIDADES_QUEBRA = {
    "dia": (lambda d: d, lambda d: f"{d[8:10]}/{d[5:7]}"),
    "mes": (lambda d: d[:7], lambda d: f"{d[5:7]}/{d[:4]}"),
    "ano": (lambda d: d[:4], lambda d: d[:4]),
}


def calcular_serie_quebra(conn, data_inicio, data_fim, granularidade, operador_nome=None):
    """Série da taxa de quebra pro gráfico de linha, com granularidade
    ajustável (dia/mês/ano). Visão de equipe traz 2 linhas (Célula Local x
    Regional); visão pessoal traz só a linha do próprio operador. "Regional"
    é a base inteira (todo mundo, Célula Local incluída), não o complemento
    dela -- mesma correção de calcular_indicador_quebra_totais."""
    if granularidade not in GRANULARIDADES_QUEBRA:
        granularidade = "dia"
    chave_bucket, formatar_label = GRANULARIDADES_QUEBRA[granularidade]

    linhas = _quebras_do_periodo(conn, data_inicio, data_fim, operador_nome)

    def bucketizar(filtro_segmento):
        buckets = {}
        for l in linhas:
            if filtro_segmento is not None and l["segmento"] != filtro_segmento:
                continue
            chave = chave_bucket(l["dat_agenda"])
            b = buckets.setdefault(chave, {"total": 0, "com_quebra": 0})
            b["total"] += 1
            if l["tipo_tratamento"] == "COM QUEBRA DE AGENDA":
                b["com_quebra"] += 1
        return buckets

    def taxa_de(buckets, chave):
        b = buckets.get(chave)
        if not b or not b["total"]:
            return 0
        return round(b["com_quebra"] / b["total"] * 100, 1)

    if operador_nome:
        buckets = bucketizar(None)
        # As chaves de bucket (ISO: "2026-08-27" / "2026-08" / "2026") ordenam
        # cronologicamente como string -- só formata pro label DEPOIS de
        # ordenar, senão "01/09" vem antes de "27/08" na ordenação alfabética.
        chaves_ordenadas = sorted(buckets.keys())
        return {
            "labels": [formatar_label(k) for k in chaves_ordenadas],
            "pessoal": [taxa_de(buckets, k) for k in chaves_ordenadas],
        }

    buckets_local = bucketizar("CELULA LOCAL")
    buckets_regional = bucketizar(None)
    chaves_ordenadas = sorted(set(buckets_local.keys()) | set(buckets_regional.keys()))
    return {
        "labels": [formatar_label(k) for k in chaves_ordenadas],
        "celula_local": [taxa_de(buckets_local, k) for k in chaves_ordenadas],
        "regional": [taxa_de(buckets_regional, k) for k in chaves_ordenadas],
    }


def calcular_resultado_quebra(conn, data_inicio, data_fim, canal=None):
    """Página /quebra/resultado (pedida pelo Bruno, 2026-08-29): "todas as
    visões que der pra gerar" -- geral, por segmento, por cidade, por canal
    de venda, por responsável e por agrupamento da quebra. EXPURGADA conta
    no total (mesma regra da métrica oficial, corrigida em 2026-08-29 -- só
    não entra no numerador de "com quebra")."""
    params = [data_inicio, data_fim]
    filtro_canal_sql = ""
    if canal:
        filtro_canal_sql = " AND canal_venda = ?"
        params.append(canal)

    linhas = conn.execute(f"""
        SELECT segmento, cidade, canal_venda, responsavel_quebra, agrupamento_quebra, tipo_tratamento
        FROM quebras_agenda
        WHERE dat_agenda BETWEEN ? AND ? {filtro_canal_sql}
    """, params).fetchall()

    total_geral = len(linhas)
    com_quebra_geral = sum(1 for l in linhas if l["tipo_tratamento"] == "COM QUEBRA DE AGENDA")

    def agrupar_por(chave_fn, filtrar_so_com_quebra=False, taxa_sobre_total_quebras=False):
        """taxa_sobre_total_quebras=False (padrão, usado em segmento/cidade/canal):
        Taxa = taxa de quebra DENTRO do grupo (com_quebra do grupo / total do
        grupo) -- só faz sentido quando o grupo tem OS com e sem quebra.
        taxa_sobre_total_quebras=True (agrupamento/responsável, pedido do
        Bruno em 2026-08-29): essas duas visões só listam OS que JÁ tiveram
        quebra (com_quebra == total sempre, taxa dentro do grupo sempre
        99999%) -- então aqui "Taxa" vira a PARTICIPAÇÃO do grupo no total de
        quebras do período (com_quebra do grupo / total de quebras geral),
        ex: 1000 quebras no total, 100 são de "AGENDAMENTO" -> 10%."""
        grupos = {}
        for l in linhas:
            if filtrar_so_com_quebra and l["tipo_tratamento"] != "COM QUEBRA DE AGENDA":
                continue
            chave = chave_fn(l) or "Não informado"
            g = grupos.setdefault(chave, {"total": 0, "com_quebra": 0})
            g["total"] += 1
            if l["tipo_tratamento"] == "COM QUEBRA DE AGENDA":
                g["com_quebra"] += 1
        linhas_saida = []
        for chave, g in grupos.items():
            if taxa_sobre_total_quebras:
                taxa = round(g["com_quebra"] / com_quebra_geral * 100, 1) if com_quebra_geral else 0
            else:
                taxa = round(g["com_quebra"] / g["total"] * 100, 1) if g["total"] else 0
            linhas_saida.append({"chave": chave, "total": g["total"], "com_quebra": g["com_quebra"], "taxa": taxa})
        linhas_saida.sort(key=lambda x: x["com_quebra"], reverse=True)
        return linhas_saida

    canais_disponiveis = sorted({l["canal_venda"] for l in conn.execute(
        "SELECT DISTINCT canal_venda FROM quebras_agenda WHERE canal_venda IS NOT NULL"
    ).fetchall() if l["canal_venda"]})

    return {
        "geral": {
            "total": total_geral, "com_quebra": com_quebra_geral,
            "taxa": round(com_quebra_geral / total_geral * 100, 1) if total_geral else None,
        },
        "por_segmento": agrupar_por(lambda l: l["segmento"]),
        "por_cidade": agrupar_por(lambda l: l["cidade"]),
        "por_canal": agrupar_por(lambda l: l["canal_venda"]),
        "por_responsavel": agrupar_por(lambda l: l["responsavel_quebra"], filtrar_so_com_quebra=True, taxa_sobre_total_quebras=True),
        "por_agrupamento": agrupar_por(lambda l: l["agrupamento_quebra"], filtrar_so_com_quebra=True, taxa_sobre_total_quebras=True),
        "canais_disponiveis": canais_disponiveis,
    }


SUBMOTIVOS_CANCELAMENTO_QUEBRA = (
    "Tubulação Obstruída", "Mudança de Endereço", "Desiste da Migração", "Sem contato", "Outro",
)


def _sql_dono_quebra(alias):
    """Subconsulta SQL que devolve o DONO de uma quebra (alias = tabela de
    quebras_agenda na consulta externa) -- pedido do Bruno, 2026-10-16.

    Regra: a quebra só é de alguém do time se aconteceu NO DIA ou DEPOIS do
    dia em que essa pessoa agendou o contrato (date(atividades.data_fim) <=
    dat_agenda). E, se mais de uma pessoa agendou o mesmo contrato, o dono
    é quem fez o agendamento MAIS RECENTE até a data da quebra.

    Por quê: a base de quebra é o acumulado do mês inteiro, de todos os
    canais. Antes o cruzamento era só por contrato -- quem agendou o
    contrato alguma vez recebia QUALQUER quebra dele, inclusive quebras
    anteriores ao nosso agendamento, de outro atendimento/canal (78 pares
    assim no backup de 16/10). Comparar com a data exata agendada foi
    descartado: cortaria ~170 quebras legítimas em que a data da OS no campo
    difere da que agendamos (antecipação de agenda, etc.)."""
    return f"""(SELECT a_dono.operador FROM atividades a_dono
                WHERE a_dono.tabulacao = 'Agendado'
                  AND a_dono.contrato = {alias}.contrato
                  AND date(a_dono.data_fim) <= {alias}.dat_agenda
                ORDER BY a_dono.data_fim DESC, a_dono.id DESC LIMIT 1)"""


def dono_da_quebra(conn, quebra):
    """Mesmo critério de _sql_dono_quebra, pra uma quebra já carregada."""
    linha = conn.execute("""
        SELECT operador FROM atividades
        WHERE tabulacao = 'Agendado' AND contrato = ? AND date(data_fim) <= ?
        ORDER BY data_fim DESC, id DESC LIMIT 1
    """, (quebra["contrato"], quebra["dat_agenda"])).fetchone()
    return linha["operador"] if linha else None


def quebras_pendentes_tratamento(conn, operador_nome, status_filtro="PENDENTE", data_inicio=None,
                                   data_fim=None, contrato_filtro=None, motivo_filtro=None):
    """Fila de quebras que esse operador precisa tratar -- pedido do Bruno,
    2026-08-29, reescrita em 2026-09-08 depois de um bug encontrado: a
    versão anterior só mostrava quebras de D-1 (dia anterior a hoje),
    fazendo qualquer pendência mais antiga que isso ficar invisível pra
    sempre pro operador (mesmo aparecendo certo pro ADM/Supervisor na tela
    de Gestão de Quebra, que olha o período inteiro). Agora mostra TODAS as
    pendentes por padrão, com filtro de data/status/contrato/motivo pra
    quem quiser refinar.

    Só entra quem: (1) teve quebra de verdade (tipo_tratamento='COM QUEBRA
    DE AGENDA' -- OS sem quebra ou EXPURGADA não precisam de tratativa
    nenhuma), (2) esse operador é o DONO da quebra -- agendou o contrato
    até a data da quebra, e foi o agendamento mais recente até ela (ver
    _sql_dono_quebra; regra de 2026-10-16 -- antes bastava ter agendado o
    contrato alguma vez, de qualquer data).

    status_filtro: "PENDENTE" (default, sem tratativa registrada ainda),
    "CONCLUIDO" (já tratada) ou "TODOS".

    Ordenado priorizando contrato de produto Dados (produto_virtua
    preenchido no mailing) primeiro -- TV e Fone não são prioritários,
    pedido do Bruno."""
    condicoes = [
        "q.tipo_tratamento = 'COM QUEBRA DE AGENDA'",
        # pré-filtro barato (índice por operador) antes da subconsulta do dono
        "q.contrato IN (SELECT DISTINCT contrato FROM atividades WHERE tabulacao='Agendado' AND operador=?)",
        f"{_sql_dono_quebra('q')} = ?",
    ]
    params = [operador_nome, operador_nome]

    if status_filtro == "PENDENTE":
        condicoes.append("q.id NOT IN (SELECT quebra_id FROM quebras_tratamento)")
    elif status_filtro == "CONCLUIDO":
        condicoes.append("q.id IN (SELECT quebra_id FROM quebras_tratamento)")
    # "TODOS" não filtra por status nenhum

    if data_inicio:
        condicoes.append("q.dat_agenda >= ?")
        params.append(data_inicio)
    if data_fim:
        condicoes.append("q.dat_agenda <= ?")
        params.append(data_fim)
    if contrato_filtro:
        condicoes.append("q.contrato LIKE ?")
        params.append(f"%{contrato_filtro}%")
    if motivo_filtro:
        condicoes.append("q.motivo_reagendamento = ?")
        params.append(motivo_filtro)

    where_sql = " AND ".join(condicoes)
    return conn.execute(f"""
        SELECT q.* FROM quebras_agenda q
        LEFT JOIN mailing m ON m.contrato = q.contrato
        WHERE {where_sql}
        ORDER BY
            (CASE WHEN m.produto_virtua IS NOT NULL AND m.produto_virtua != '' THEN 0 ELSE 1 END),
            q.dat_agenda ASC
    """, params).fetchall()


def contador_quebras_pendentes(conn, operador_nome):
    return len(quebras_pendentes_tratamento(conn, operador_nome))


def calcular_fila_gestao_quebra(conn, data_inicio, data_fim, operador=None, contrato=None, motivo=None):
    """Tela de gestão /gestao/quebra (pedida pelo Bruno, 2026-08-29 -- 2ª
    rodada de ajustes da tela de quebra): visão do Supervisor/ADM de quem
    (qual Assistente) tem demanda de quebra pra tratar e o volume já
    tratado, com o detalhe de cada quebra individual. Mesmo cruzamento "é
    uma quebra desse operador" já usado em quebras_pendentes_tratamento
    (por contrato, via atividades.tabulacao='Agendado') -- só que aqui olha
    um período inteiro (não só D-1) e cobre pendentes E já tratadas, pra dar
    o volume completo, não só a fila do dia."""
    operadores = conn.execute("""
        SELECT nome FROM usuarios
        WHERE perfil='OPERADOR' AND ativo=1 AND COALESCE(excluido,0)=0
        ORDER BY nome
    """).fetchall()
    nomes_operadores = [o["nome"] for o in operadores]
    if operador:
        nomes_operadores = [n for n in nomes_operadores if n == operador]

    filtro_extra_sql = ""
    filtro_extra_params = []
    if contrato:
        filtro_extra_sql += " AND qa.contrato = ?"
        filtro_extra_params.append(contrato)
    if motivo:
        filtro_extra_sql += " AND qa.motivo_reagendamento = ?"
        filtro_extra_params.append(motivo)

    por_operador = []
    for nome in nomes_operadores:
        linhas = conn.execute(f"""
            SELECT qa.*, qt.tabulacao AS trat_tabulacao, qt.submotivo AS trat_submotivo,
                   qt.observacao AS trat_observacao, qt.data_tratamento AS trat_data
            FROM quebras_agenda qa
            LEFT JOIN quebras_tratamento qt ON qt.quebra_id = qa.id
            WHERE qa.tipo_tratamento = 'COM QUEBRA DE AGENDA'
              AND qa.dat_agenda BETWEEN ? AND ?
              AND qa.contrato IN (SELECT DISTINCT contrato FROM atividades WHERE tabulacao='Agendado' AND operador=?)
              AND {_sql_dono_quebra('qa')} = ?
              {filtro_extra_sql}
            ORDER BY qa.dat_agenda DESC, qa.cod_os
        """, [data_inicio, data_fim, nome, nome] + filtro_extra_params).fetchall()

        if not linhas:
            continue

        quebras = []
        pendentes = 0
        tratadas = 0
        for l in linhas:
            foi_tratada = l["trat_tabulacao"] is not None
            if foi_tratada:
                tratadas += 1
            else:
                pendentes += 1
            quebras.append({
                "id": l["id"],
                "cod_os": l["cod_os"],
                "contrato": l["contrato"],
                "cidade": l["cidade"] or "—",
                "dat_agenda": l["dat_agenda"],
                "motivo_reagendamento": l["motivo_reagendamento"] or "—",
                "agrupamento_quebra": l["agrupamento_quebra"] or "—",
                "tratada": foi_tratada,
                "tratamento_tabulacao": l["trat_tabulacao"],
                "tratamento_submotivo": l["trat_submotivo"],
                "tratamento_observacao": l["trat_observacao"],
                "tratamento_data": l["trat_data"],
            })

        por_operador.append({
            "operador": nome,
            "total": len(quebras),
            "pendentes": pendentes,
            "tratadas": tratadas,
            "taxa_tratamento": round(tratadas / len(quebras) * 100, 1) if quebras else 0,
            "quebras": quebras,
        })

    # Quem tem mais pendência primeiro -- é a gestão que mais precisa de
    # atenção agora; empate desempata pelo volume total.
    por_operador.sort(key=lambda o: (-o["pendentes"], -o["total"]))

    total_geral = sum(o["total"] for o in por_operador)
    pendentes_geral = sum(o["pendentes"] for o in por_operador)
    tratadas_geral = sum(o["tratadas"] for o in por_operador)

    motivos_disponiveis = sorted({l["motivo_reagendamento"] for l in conn.execute(
        "SELECT DISTINCT motivo_reagendamento FROM quebras_agenda WHERE motivo_reagendamento IS NOT NULL"
    ).fetchall() if l["motivo_reagendamento"]})

    return {
        "por_operador": por_operador,
        "geral": {
            "total": total_geral,
            "pendentes": pendentes_geral,
            "tratadas": tratadas_geral,
            "taxa_tratamento": round(tratadas_geral / total_geral * 100, 1) if total_geral else None,
            "operadores_com_pendencia": sum(1 for o in por_operador if o["pendentes"] > 0),
        },
        "operadores_disponiveis": [o["nome"] for o in operadores],
        "motivos_disponiveis": motivos_disponiveis,
    }


def _extrair_ddd(telefone):
    """DDD = 2 primeiros dígitos do telefone (formato salvo no mailing é só
    números, sem parênteses/traço -- ex: '19991342877'). None se o campo
    estiver vazio/curto demais pra ter DDD."""
    if not telefone:
        return None
    digitos = "".join(ch for ch in telefone if ch.isdigit())
    if len(digitos) < 2:
        return None
    return digitos[:2]


def _extrair_velocidade_produto_virtua(texto):
    """Extrai a velocidade (em Mbps) do texto livre de produto_virtua --
    cobre GIGA/G (x1000), K (kilobits, /1000) e M/MEGA (Mbps direto).
    Cobertura testada: 417 de 419 valores distintos reais da base
    (2026-09-11) -- os 2 que sobram não têm nenhuma unidade de velocidade
    no texto (ex: "SUSPENSAO TEMPORARIA VIRTUA")."""
    if not texto:
        return None
    texto_upper = texto.upper()
    if "GIGA" in texto_upper:
        m = re.search(r"(\d+)\s*GIGA", texto_upper)
        return (int(m.group(1)) if m else 1) * 1000
    m = re.search(r"\b(\d+)\s*G\b", texto_upper)
    if m:
        return int(m.group(1)) * 1000
    m = re.search(r"(\d+)\s*K\b", texto_upper)
    if m:
        return int(m.group(1)) / 1000
    m = re.search(r"(\d+)\s*MEGA|\b(\d+)\s*M\b", texto_upper)
    if m:
        return int(m.group(1) or m.group(2))
    return None


def _rotulo_velocidade(mbps):
    if mbps is None:
        return "Não identificado"
    if mbps >= 1000:
        giga = mbps / 1000
        return f"{giga:.0f}GB" if giga == int(giga) else f"{giga:.1f}GB"
    if mbps < 1:
        return f"{int(mbps * 1000)}KB"
    return f"{int(mbps)}M"


def _titulo_evento_atividade(tabulacao):
    """Ícone + texto amigável pra cada tipo de tabulação -- GPON usa a mesma
    tabela atividades (registrar_tratamento), então já entra automaticamente
    sem precisar de fonte separada."""
    if not tabulacao:
        return "📝", "Tratamento"
    if tabulacao == "Agendado":
        return "📅", "Agendado"
    if tabulacao.startswith("Recusado"):
        return "❌", tabulacao
    if tabulacao in ("2ª Tentativa", "3ª Tentativa"):
        return "📞", tabulacao
    if tabulacao == "Retorno":
        return "🕐", "Retorno agendado"
    if tabulacao == "Improdutivo":
        return "🚫", "Improdutivo"
    if tabulacao == "Contrato Desconectado":
        return "🔌", "Contrato Desconectado"
    if "GPON" in tabulacao.upper() or "HP liberado" in tabulacao:
        return "📡", f"GPON: {tabulacao}"
    if tabulacao in ("Mantido bloqueado", "Solicitado bloqueio"):
        return "🔒", tabulacao
    return "📝", tabulacao


def montar_timeline_contrato(conn, contrato):
    """Histórico Unificado de Interações (Timeline do Cliente) -- pedido do
    Bruno, 2026-09-11: junta tudo que já existia espalhado (atividades,
    transferências, quebras de agenda) numa lista só, em ordem cronológica
    (mais recente primeiro), pra não precisar olhar em vários lugares pra
    reconstruir a história de um contrato. GPON entra automaticamente,
    porque usa a mesma tabela atividades (registrar_tratamento) que o
    atendimento normal."""
    eventos = []

    for a in conn.execute("""
        SELECT operador, tabulacao, observacao, data_inicio, data_fim,
               data_agendamento, janela_agendamento, resp_cliente
        FROM atividades WHERE contrato=? ORDER BY id
    """, (contrato,)).fetchall():
        icone, titulo = _titulo_evento_atividade(a["tabulacao"])
        detalhe = a["observacao"] or ""
        if a["tabulacao"] == "Agendado" and a["data_agendamento"]:
            detalhe = f"Agendado pra {a['data_agendamento']} ({a['janela_agendamento'] or 'sem janela'}) -- {a['resp_cliente'] or ''}. {detalhe}".strip()
        eventos.append({
            "data_hora": a["data_fim"] or a["data_inicio"],
            "icone": icone, "titulo": titulo, "detalhe": detalhe.strip(),
            "operador": a["operador"], "cor": "#0C2F4C",
        })

    for t in conn.execute("""
        SELECT solicitante, dono_atual, destino, justificativa, status,
               data_solicitacao, data_resposta, respondido_por
        FROM transferencias WHERE contrato=? ORDER BY id
    """, (contrato,)).fetchall():
        rotulo_status = {"APROVADO": "aprovada", "REJEITADO": "rejeitada", "PENDENTE": "pendente"}.get(t["status"], t["status"])
        detalhe = f"{t['dono_atual'] or t['solicitante']} → {t['destino']}"
        if t["justificativa"]:
            detalhe += f". Motivo: {t['justificativa']}"
        if t["status"] != "PENDENTE" and t["respondido_por"]:
            detalhe += f". Respondido por {t['respondido_por']}"
        eventos.append({
            "data_hora": t["data_resposta"] or t["data_solicitacao"],
            "icone": "🔄", "titulo": f"Transferência {rotulo_status}", "detalhe": detalhe,
            "operador": t["solicitante"], "cor": "#7A2E8C",
        })

    for q in conn.execute("""
        SELECT qt.operador, qt.tabulacao, qt.submotivo, qt.observacao, qt.data_tratamento
        FROM quebras_tratamento qt
        JOIN quebras_agenda qa ON qa.id = qt.quebra_id
        WHERE qa.contrato=? ORDER BY qt.id
    """, (contrato,)).fetchall():
        detalhe = q["tabulacao"] or ""
        if q["submotivo"]:
            detalhe += f" — {q['submotivo']}"
        if q["observacao"]:
            detalhe += f". {q['observacao']}"
        eventos.append({
            "data_hora": q["data_tratamento"],
            "icone": "📋", "titulo": "Quebra de agenda tratada", "detalhe": detalhe.strip(),
            "operador": q["operador"], "cor": "#F2A900",
        })

    for h in conn.execute("""
        SELECT criado_por, atribuido_para, tipo, data_criacao
        FROM historico_tarefas_manuais WHERE contrato=? ORDER BY id
    """, (contrato,)).fetchall():
        eventos.append({
            "data_hora": h["data_criacao"],
            "icone": "📥", "titulo": "Entrou via Nova Tarefa", "detalhe": f"Criado por {h['criado_por']}, atribuído a {h['atribuido_para']}",
            "operador": h["criado_por"], "cor": "#4C8C4A",
        })

    for imp in conn.execute("""
        SELECT importado_por, importado_em
        FROM historico_importacao_mailing WHERE contrato=? ORDER BY id
    """, (contrato,)).fetchall():
        eventos.append({
            "data_hora": imp["importado_em"],
            "icone": "📥", "titulo": "Entrou via importação de mailing", "detalhe": "",
            "operador": imp["importado_por"], "cor": "#4C8C4A",
        })

    eventos = [e for e in eventos if e["data_hora"]]
    eventos.sort(key=lambda e: e["data_hora"], reverse=True)
    return eventos


def calcular_mailing_refinado(conn):
    """Detalhamento da Lista de Trabalho (pedida pelo Bruno, 2026-08-29,
    reformulada em 2026-09-11): volume por cidade, node (agrupado por DDD >
    Cidade > Node), DDD (via tabela cidades_ddd, não mais extraído do
    telefone) e produto Virtua (agrupado por faixa de velocidade) --
    subpágina do Dashboard, mesma trava de visibilidade da visão de equipe.

    Removido em 2026-09-11 a pedido do Bruno: "saturação" (percentual
    tratado) em todas as visões, e o agrupamento "por produto" (combo
    TV/Internet/Voz)."""
    linhas = conn.execute("""
        SELECT cidade, node, status, produto_virtua
        FROM mailing
    """).fetchall()

    cidade_para_ddd = {
        l["cidade"]: l["ddd"] for l in conn.execute("SELECT cidade, ddd FROM cidades_ddd").fetchall()
    }

    def agrupar_total(chave_fn, com_tratado=True):
        grupos = {}
        for l in linhas:
            chave = chave_fn(l) or "Não informado"
            g = grupos.setdefault(chave, {"total": 0, "tratado": 0})
            g["total"] += 1
            if com_tratado and l["status"] != "PENDENTE":
                g["tratado"] += 1
        saida = []
        for chave, g in grupos.items():
            item = {"chave": chave, "total": g["total"]}
            if com_tratado:
                item["tratado"] = g["tratado"]
            saida.append(item)
        saida.sort(key=lambda x: x["total"], reverse=True)
        return saida

    total_geral = len(linhas)
    tratado_geral = sum(1 for l in linhas if l["status"] != "PENDENTE")

    por_cidade = agrupar_total(lambda l: l["cidade"])
    por_ddd = agrupar_total(
        lambda l: f"DDD {cidade_para_ddd[l['cidade']]}" if l["cidade"] in cidade_para_ddd else "DDD não mapeado"
    )

    # Por node, agrupado hierarquicamente: DDD > Cidade > Node (com "+" pra
    # expandir cada nível na tela) -- pedido do Bruno, 2026-09-11.
    hierarquia = {}
    for l in linhas:
        cidade = l["cidade"] or "Não informado"
        ddd_num = cidade_para_ddd.get(cidade)
        ddd_label = f"DDD {ddd_num}" if ddd_num else "DDD não mapeado"
        node = l["node"] or "Não informado"
        tratado = l["status"] != "PENDENTE"

        grupo_ddd = hierarquia.setdefault(ddd_label, {"total": 0, "tratado": 0, "cidades": {}})
        grupo_ddd["total"] += 1
        grupo_ddd["tratado"] += tratado

        grupo_cidade = grupo_ddd["cidades"].setdefault(cidade, {"total": 0, "tratado": 0, "nodes": {}})
        grupo_cidade["total"] += 1
        grupo_cidade["tratado"] += tratado

        no = grupo_cidade["nodes"].setdefault(node, {"total": 0, "tratado": 0})
        no["total"] += 1
        no["tratado"] += tratado

    por_node = []
    for ddd_label, grupo_ddd in sorted(hierarquia.items(), key=lambda x: x[1]["total"], reverse=True):
        cidades_saida = []
        for cidade, grupo_cidade in sorted(grupo_ddd["cidades"].items(), key=lambda x: x[1]["total"], reverse=True):
            nodes_saida = sorted(
                [{"chave": n, "total": v["total"], "tratado": v["tratado"]} for n, v in grupo_cidade["nodes"].items()],
                key=lambda x: x["total"], reverse=True,
            )
            cidades_saida.append({
                "chave": cidade, "total": grupo_cidade["total"], "tratado": grupo_cidade["tratado"],
                "nodes": nodes_saida,
            })
        por_node.append({
            "chave": ddd_label, "total": grupo_ddd["total"], "tratado": grupo_ddd["tratado"],
            "cidades": cidades_saida,
        })

    # Por produto Virtua, agrupado por faixa de velocidade -- substitui o
    # antigo agrupamento "por produto" (combo TV/Internet/Voz).
    por_produto_virtua = agrupar_total(
        lambda l: _rotulo_velocidade(_extrair_velocidade_produto_virtua(l["produto_virtua"]))
    )

    return {
        "geral": {"total": total_geral, "tratado": tratado_geral},
        "por_cidade": por_cidade,
        "por_node": por_node,
        "por_ddd": por_ddd,
        "por_produto_virtua": por_produto_virtua,
    }


def _garantir_coluna_meta_execucoes():
    """Auto-migração leve (mesmo padrão de meta_agendados_mes): objetivo
    mensal de execuções, configurável pelo ADM em Modo de Trabalho, usado nos
    velocímetros do Dashboard -- pedido do Bruno, 2026-08-28."""
    conn = conectar()
    try:
        conn.execute("ALTER TABLE configuracoes ADD COLUMN meta_execucoes_mes INTEGER DEFAULT 100")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # coluna já existe -- migração já rodou antes
    conn.close()


_garantir_coluna_meta_execucoes()


def _garantir_coluna_transferencia_auto_aprovar():
    """Auto-migração leve (mesmo padrão acima): flag de contingência que,
    quando ligada, pula a aprovação manual de transferência de contrato --
    pedido do Bruno, 2026-09-04, pra usar em momento de pico/instabilidade."""
    conn = conectar()
    try:
        conn.execute("ALTER TABLE configuracoes ADD COLUMN transferencia_auto_aprovar INTEGER DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    conn.close()


_garantir_coluna_transferencia_auto_aprovar()


def _garantir_tabela_historico_tarefas_manuais():
    """Auto-migração: registra quem criou cada tarefa pela tela Nova Tarefa
    (manual ou importação em planilha pela mesma tela), pra quem e quando --
    pedido do Bruno, 2026-09-08, depois de um caso onde não dava pra saber
    quem tinha criado uma tarefa com dado errado (contrato = login de
    alguém, por engano)."""
    conn = conectar()
    try:
        conn.execute("""
            CREATE TABLE historico_tarefas_manuais (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contrato TEXT,
                cidade TEXT,
                criado_por TEXT,
                atribuido_para TEXT,
                tipo TEXT,
                data_criacao TEXT
            )
        """)
        conn.commit()
    except sqlite3.OperationalError:
        pass  # tabela já existe -- migração já rodou antes
    conn.close()


_garantir_tabela_historico_tarefas_manuais()


def _garantir_tabela_motivos_ausencia_customizados():
    """Auto-migração: motivos de ausência extras que o ADM pode cadastrar
    (além dos 5 fixos: Falta Justificada, Injustificada, Treinamento,
    Férias, Afastamento) -- pedido do Bruno, 2026-09-08, depois de um
    feriado que não tinha motivo cadastrado pra usar."""
    conn = conectar()
    try:
        conn.execute("""
            CREATE TABLE motivos_ausencia_customizados (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT UNIQUE NOT NULL,
                ativo INTEGER DEFAULT 1,
                criado_por TEXT,
                criado_em TEXT
            )
        """)
        conn.commit()
    except sqlite3.OperationalError:
        pass
    conn.close()


_garantir_tabela_motivos_ausencia_customizados()


def _garantir_coluna_tipo_followup():
    """Auto-migração: distingue 'RETORNO' (o cliente pediu pra ser chamado
    numa data/hora específica -- compromisso firme) de 'FOLLOWUP' (2ª/3ª
    Tentativa -- tentativa de novo depois de um prazo, sem compromisso com o
    cliente). Antes as duas caíam na mesma tabela sem distinção nenhuma,
    ordenadas só por data -- o Bruno esclareceu em 2026-09-10 que Retorno
    tem prioridade sobre followup comum (o followup pode esperar o Retorno
    ser distribuído primeiro), não é só "o mais antigo primeiro" entre os
    dois tipos."""
    conn = conectar()
    try:
        conn.execute("ALTER TABLE followup ADD COLUMN tipo TEXT DEFAULT 'FOLLOWUP'")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    conn.close()


_garantir_coluna_tipo_followup()


def _garantir_coluna_id_mailing_atividades_followup():
    """Auto-migração (pedido do Bruno, 2026-09-20): `atividades` e `followup`
    guardavam só o número do contrato (texto), sem distinguir DE QUAL linha
    de `mailing` aquela tratativa/reserva era. Isso não fazia diferença
    enquanto contrato era único -- mas não é: o mesmo número de contrato já
    existe em cidades diferentes de propósito (Nova Tarefa dedupa por
    contrato+cidade, não só contrato -- ver _processar_nova_tarefa), com 5
    casos reais confirmados na base (ex: 232849156 em Araraquara E
    Aracatuba). Sem uma referência direta à linha de mailing, qualquer
    JOIN/WHERE por só `contrato` misturava o histórico e a reserva de
    followup das DUAS linhas como se fosse uma coisa só -- caso real
    encontrado: histórico do contrato 232849156 misturando tratativas de
    duas operadoras diferentes, uma pra cada cidade, na mesma linha do tempo
    (achado por um operador, registrado na observação de um followup:
    "Cliente de quebra, cidade está errada, cliente é de Araraquara").

    id_mailing fica NULL pra linhas antigas ambíguas (não dá pra saber
    retroativamente qual das duas era) -- mas é populado a partir de agora
    em toda gravação nova (registrar_tratamento, GPON, Backlog), e
    retroativamente já preenchido abaixo pros casos NÃO ambíguos (contrato
    que só existe numa linha de mailing -- a grande maioria)."""
    conn = conectar()
    try:
        conn.execute("ALTER TABLE atividades ADD COLUMN id_mailing INTEGER")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE followup ADD COLUMN id_mailing INTEGER")
    except sqlite3.OperationalError:
        pass
    conn.commit()

    # Backfill: só quando o contrato é INEQUÍVOCO (existe em uma única linha
    # de mailing) -- pros 5 casos ambíguos reais (e qualquer futuro caso
    # igual), id_mailing fica NULL de propósito, e as consultas tratam
    # NULL como "linha antiga, não dá pra saber" (mesmo comportamento de
    # hoje pra esses poucos casos, sem piorar nem arriscar adivinhar errado).
    conn.execute("""
        UPDATE atividades SET id_mailing = (
            SELECT m.id FROM mailing m WHERE m.contrato = atividades.contrato
        )
        WHERE id_mailing IS NULL
          AND (SELECT COUNT(*) FROM mailing m WHERE m.contrato = atividades.contrato) = 1
    """)
    conn.execute("""
        UPDATE followup SET id_mailing = (
            SELECT m.id FROM mailing m WHERE m.contrato = followup.contrato
        )
        WHERE id_mailing IS NULL
          AND (SELECT COUNT(*) FROM mailing m WHERE m.contrato = followup.contrato) = 1
    """)
    conn.commit()
    conn.close()


_garantir_coluna_id_mailing_atividades_followup()


def _garantir_coluna_encerrado_por_sessoes():
    """Auto-migração (pedido do Bruno, 2026-09-20): a tela Tratamento/Pausas
    não dizia se o fim de uma jornada foi a pessoa mesma clicando "Encerrar
    Tratamento" (/turno/sair), o encerramento automático das 18h30
    (encerrar_jornadas_esquecidas -- roda sozinho, ver _rotina_encerramento_automatico)
    ou um supervisor/ADM encerrando manualmente por ela (/supervisao/encerrar_atendimento).
    Guarda quem/o quê encerrou em texto -- o nome de quem clicou (própria pessoa
    ou supervisor) ou o literal 'AUTOMATICO'. NULL pra sessões antigas (de antes
    dessa coluna existir) e enquanto a sessão está aberta (logout_em também NULL)."""
    conn = conectar()
    try:
        conn.execute("ALTER TABLE sessoes ADD COLUMN encerrado_por TEXT")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()


_garantir_coluna_encerrado_por_sessoes()


def _garantir_tabela_log_distribuicao():
    """Log leve de diagnóstico: registra o caminho exato que cada contrato
    seguiu ao ser distribuído (followup próprio, followup de outra pessoa
    liberado por ausência, ou fila geral por prioridade) -- pedido do Bruno,
    2026-09-10, depois de dois casos (9977889 e 236078308) onde um contrato
    de followup de alguém disponível foi parar com outra pessoa, sem
    conseguir reproduzir o motivo em teste nenhum (sequencial ou concorrência
    real simulada). Serve só pra diagnosticar se acontecer de novo -- não
    afeta a lógica de distribuição em nada."""
    conn = conectar()
    try:
        conn.execute("""
            CREATE TABLE log_distribuicao (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contrato TEXT,
                operador_recebeu TEXT,
                origem TEXT,
                followup_dono_original TEXT,
                data_hora TEXT
            )
        """)
        conn.commit()
    except sqlite3.OperationalError:
        pass
    conn.close()


_garantir_tabela_log_distribuicao()


def _garantir_tabela_blacklist_contratos():
    """Lista de contratos bloqueados pra distribuição automática -- pedido do
    Bruno, 2026-09-20. O ADM cadastra o contrato aqui (tela "Modo de
    Trabalho") e ele para de ser oferecido em buscar_proximo_cliente
    (tanto pela fila geral quanto por followup vencido), até ser removido
    da lista. Contrato já em andamento com alguém não é interrompido --
    o bloqueio só impede uma NOVA distribuição."""
    conn = conectar()
    try:
        conn.execute("""
            CREATE TABLE contratos_blacklist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contrato TEXT UNIQUE,
                motivo TEXT,
                criado_por TEXT,
                criado_em TEXT
            )
        """)
        conn.commit()
    except sqlite3.OperationalError:
        pass
    conn.close()


_garantir_tabela_blacklist_contratos()


def obter_motivos_ausencia_customizados(conn, so_ativos=True):
    sql = "SELECT * FROM motivos_ausencia_customizados"
    if so_ativos:
        sql += " WHERE ativo=1"
    sql += " ORDER BY nome"
    return conn.execute(sql).fetchall()


def calcular_execucoes(conn, filtro_data_sql, params, limite_ranking=15):
    """Totais e ranking de OS executadas no período (coluna dh_baixa da
    execucoes_os). filtro_data_sql segue o mesmo padrão usado em
    calcular_produtividade/calcular_ranking (ex: "dh_baixa = ?" ou
    "strftime('%Y-%m', dh_baixa) = ?"), mas referenciando a coluna dh_baixa."""
    total = conn.execute(
        f"SELECT COUNT(*) c FROM execucoes_os WHERE {filtro_data_sql}", params
    ).fetchone()["c"]

    ranking = conn.execute(f"""
        SELECT u.nome as operador, COUNT(*) as qtd
        FROM execucoes_os e
        JOIN usuarios u ON lower(u.login) = e.login_abertura
        WHERE {filtro_data_sql}
        GROUP BY u.id ORDER BY qtd DESC LIMIT {int(limite_ranking)}
    """, params).fetchall()

    vinculadas = conn.execute(f"""
        SELECT COUNT(*) c FROM execucoes_os e
        JOIN usuarios u ON lower(u.login) = e.login_abertura
        WHERE {filtro_data_sql}
    """, params).fetchone()["c"]

    return {
        "total": total,
        "vinculadas": vinculadas,
        "nao_vinculadas": total - vinculadas,
        "ranking": ranking,
    }


def calcular_execucoes_mes_operador(conn, mes_referencia, operador_nome=None):
    """Total de execuções no mês pro operador dado (por login), ou da equipe
    inteira se operador_nome for None -- usado pra base da projeção mensal de
    Execuções na visão pessoal do Dashboard (2026-08-28), já que
    calcular_execucoes() por si só não filtra por pessoa."""
    if operador_nome:
        linha = conn.execute("""
            SELECT COUNT(*) c FROM execucoes_os e
            JOIN usuarios u ON lower(u.login) = e.login_abertura
            WHERE strftime('%Y-%m', e.dh_baixa) = ? AND u.nome = ?
        """, (mes_referencia, operador_nome)).fetchone()
    else:
        linha = conn.execute(
            "SELECT COUNT(*) c FROM execucoes_os WHERE strftime('%Y-%m', dh_baixa) = ?",
            (mes_referencia,)
        ).fetchone()
    return linha["c"]


def iniciar_pausa(conn, usuario_id, tipo, agora, automatica):
    duracao_pausa, duracao_almoco, duracao_banheiro = obter_duracoes(conn)
    if tipo == "ALMOCO":
        duracao = duracao_almoco
    elif tipo == "BANHEIRO":
        duracao = duracao_banheiro
    else:
        duracao = duracao_pausa
    fim_previsto = agora + duracao
    conn.execute("""
        INSERT INTO pausas (usuario_id, tipo, data, inicio, fim_previsto, fim_real, automatica)
        VALUES (?, ?, ?, ?, ?, NULL, ?)
    """, (usuario_id, tipo, hoje_str(), agora.strftime("%Y-%m-%d %H:%M:%S"),
          fim_previsto.strftime("%Y-%m-%d %H:%M:%S"), 1 if automatica else 0))
    conn.commit()


def verificar_pausa_obrigatoria(conn, usuario_id, agora):
    """Retorna o tipo de pausa que precisa ser disparada automaticamente agora, ou None."""
    if not pausa_automatica_ligada(conn):
        return None
    pausas = pausas_do_dia(conn, usuario_id)
    hora_atual = agora.time()
    _manha_inicio, limite_manha, _almoco_inicio, limite_almoco, _tarde_inicio, limite_tarde = obter_limites_pausa(conn)

    if not ja_fez(pausas, "PAUSA_MANHA") and hora_atual >= limite_manha:
        return "PAUSA_MANHA"
    if not ja_fez(pausas, "ALMOCO") and hora_atual >= limite_almoco:
        return "ALMOCO"
    if not ja_fez(pausas, "PAUSA_TARDE") and hora_atual >= limite_tarde:
        return "PAUSA_TARDE"
    return None


def bloquear_por_pausa(conn, usuario_id, agora):
    """Verifica pausa aberta ou obrigatória. Se algo bloquear o atendimento, insere/retorna a pausa ativa."""
    aberta = pausa_aberta(conn, usuario_id)
    if aberta:
        return aberta

    tipo_obrigatorio = verificar_pausa_obrigatoria(conn, usuario_id, agora)
    if tipo_obrigatorio:
        iniciar_pausa(conn, usuario_id, tipo_obrigatorio, agora, automatica=True)
        return pausa_aberta(conn, usuario_id)

    return None


# ==========================
# MONITORIA DE QUALIDADE
# ==========================

PERGUNTAS_MONITORIA = [
    ("q1_abordagem", "Abordagem", "Utilizou script padrão?"),
    ("q2_agilidade", "Agilidade", "Executa com agilidade a interação com o cliente? (CRM / NET SMS / NET Sales / Whatsapp / Ligação)"),
    ("q3_cordialidade", "Cordialidade", "Utiliza linguagem adequada com o cliente?"),
    ("q4_oferta", "Oferta", "Apresentou corretamente a oferta adequada ao cliente?"),
    ("q5_fechamento", "Fechamento", "Conduziu para o fechamento? (Contornou as objeções do cliente?)"),
    ("q6_contato", "Contato", "Tentou todas as formas de contato? (Whatsapp, Telefone, Email)"),
    ("q7_tabulacao", "Tabulação", "Tabulou corretamente no CRM e NET SMS?"),
]


def calcular_nota_monitoria(respostas):
    """respostas: dict com as 7 chaves de PERGUNTAS_MONITORIA, valor 1 (Sim) ou 0 (Não).
    Cada Sim vale 1/7 da nota final (100%)."""
    total_sim = sum(1 for chave, _, _ in PERGUNTAS_MONITORIA if respostas.get(chave) == 1)
    return round((total_sim / len(PERGUNTAS_MONITORIA)) * 100, 1)


def calcular_metricas_operador(conn, usuario_id, nome_usuario, data_inicio, data_fim):
    """Métricas de desempenho de um operador num período (usadas na tela de
    monitoria e na comparação com a média geral)."""
    tratados_total = conn.execute("""
        SELECT COUNT(*) c FROM atividades
        WHERE operador=? AND date(data_inicio) BETWEEN ? AND ?
    """, (nome_usuario, data_inicio, data_fim)).fetchone()["c"]

    agendados_total = conn.execute("""
        SELECT COUNT(*) c FROM atividades
        WHERE operador=? AND tabulacao='Agendado' AND date(data_inicio) BETWEEN ? AND ?
    """, (nome_usuario, data_inicio, data_fim)).fetchone()["c"]

    conversao_pct = round((agendados_total / tratados_total) * 100, 1) if tratados_total else 0.0

    # "Tempo efetivo de tela" (2026-08-26): usa minutos_efetivos (já descontado
    # pausa/logout) quando disponível; cai no cálculo antigo (relógio corrido)
    # só pra atividades gravadas antes dessa migração (minutos_efetivos NULL).
    tempo_medio_min = 0.0
    linhas_tempo = conn.execute("""
        SELECT data_inicio, data_fim, minutos_efetivos FROM atividades
        WHERE operador=? AND date(data_inicio) BETWEEN ? AND ? AND data_fim IS NOT NULL
    """, (nome_usuario, data_inicio, data_fim)).fetchall()
    if linhas_tempo:
        duracoes = []
        for linha in linhas_tempo:
            if linha["minutos_efetivos"] is not None:
                duracoes.append(linha["minutos_efetivos"])
                continue
            try:
                ini = datetime.strptime(linha["data_inicio"], "%Y-%m-%d %H:%M:%S")
                fim = datetime.strptime(linha["data_fim"], "%Y-%m-%d %H:%M:%S")
                duracoes.append((fim - ini).total_seconds() / 60)
            except (ValueError, TypeError):
                continue
        if duracoes:
            tempo_medio_min = round(sum(duracoes) / len(duracoes), 1)

    pausas_excedidas = conn.execute("""
        SELECT COUNT(*) c FROM pausas
        WHERE usuario_id=? AND data BETWEEN ? AND ?
          AND fim_real IS NOT NULL AND fim_real > fim_previsto
    """, (usuario_id, data_inicio, data_fim)).fetchone()["c"]

    faltas = conn.execute("""
        SELECT COUNT(*) c FROM ausencias WHERE usuario_id=? AND data BETWEEN ? AND ?
    """, (usuario_id, data_inicio, data_fim)).fetchone()["c"]

    # Atrasos: dias em que a pessoa era escalada, tinha login registrado, mas
    # logou depois do horário de início da própria escala.
    escala = conn.execute("SELECT dias_trabalho, hora_inicio, ferias_inicio, ferias_fim FROM escalas WHERE usuario_id=?", (usuario_id,)).fetchone()
    atrasos = 0
    saidas_antecipadas = 0
    sessoes_periodo = conn.execute("""
        SELECT login_em, logout_em FROM sessoes WHERE usuario_id=? AND date(login_em) BETWEEN ? AND ?
    """, (usuario_id, data_inicio, data_fim)).fetchall()

    hora_inicio_escala = None
    if escala and escala["hora_inicio"]:
        try:
            hora_inicio_escala = datetime.strptime(escala["hora_inicio"], "%H:%M").time()
        except ValueError:
            hora_inicio_escala = None

    for s in sessoes_periodo:
        try:
            login_dt = datetime.strptime(s["login_em"], "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            continue
        if hora_inicio_escala and login_dt.time() > hora_inicio_escala:
            atrasos += 1
        if s["logout_em"]:
            try:
                logout_dt = datetime.strptime(s["logout_em"], "%Y-%m-%d %H:%M:%S")
                if logout_dt.time() < time(17, 45):
                    saidas_antecipadas += 1
            except (ValueError, TypeError):
                pass

    # Volume de agendamentos por mês (agrupado direto via SQL)
    linhas_mes = conn.execute("""
        SELECT strftime('%Y-%m', data_inicio) AS mes, COUNT(*) c
        FROM atividades
        WHERE operador=? AND tabulacao='Agendado' AND date(data_inicio) BETWEEN ? AND ?
        GROUP BY mes ORDER BY mes
    """, (nome_usuario, data_inicio, data_fim)).fetchall()
    agendamentos_por_mes = [{"mes": linha["mes"], "qtd": linha["c"]} for linha in linhas_mes]

    return {
        "conversao_pct": conversao_pct,
        "tratados_total": tratados_total,
        "agendados_total": agendados_total,
        "tempo_medio_min": tempo_medio_min,
        "pausas_excedidas": pausas_excedidas,
        "faltas": faltas,
        "atrasos": atrasos,
        "saidas_antecipadas": saidas_antecipadas,
        "agendamentos_por_mes": agendamentos_por_mes,
    }


def calcular_media_geral_metricas(conn, data_inicio, data_fim):
    """Média das métricas acima entre TODOS os operadores ativos, pra comparação."""
    operadores = conn.execute("SELECT id, nome FROM usuarios WHERE ativo=1 AND perfil IN ('OPERADOR','ANALISTA')").fetchall()
    if not operadores:
        return None

    acumulado = {
        "conversao_pct": [], "tempo_medio_min": [], "pausas_excedidas": [],
        "faltas": [], "atrasos": [], "saidas_antecipadas": [],
    }
    for op in operadores:
        m = calcular_metricas_operador(conn, op["id"], op["nome"], data_inicio, data_fim)
        for chave in acumulado:
            acumulado[chave].append(m[chave])

    def media(lista):
        return round(sum(lista) / len(lista), 1) if lista else 0.0

    return {chave: media(valores) for chave, valores in acumulado.items()}


def calcular_grafico_escala_presenca(conn, data_inicio_str, data_fim_str):
    """Pra cada dia do período: quantos operadores estavam escalados, quantos
    contam como ausentes (falta registrada OU deveria ter logado e não logou)
    e quantos sobraram como presentes. Um dia só conta como "escalado" pra
    alguém se for igual ou posterior ao cadastro dela no sistema
    (usuarios.data_cadastro) -- mesma trava já aplicada em
    _pendencias_ausencia() (2026-08-27), aqui corrigida de vez depois de um
    diretor notar, olhando o gráfico pela primeira vez, que o vermelho
    (ausentes) aparecia quase uniforme o mês inteiro: sem essa trava, escala
    configurada antes de a pessoa existir no CRM inflava o "ausente" de todo
    mundo em qualquer dia anterior ao próprio cadastro, dando a impressão
    errada de um problema real de absenteísmo."""
    escalas_usuarios = conn.execute("""
        SELECT e.usuario_id, e.dias_trabalho, e.ferias_inicio, e.ferias_fim, e.hora_inicio, u.data_cadastro
        FROM escalas e
        JOIN usuarios u ON u.id = e.usuario_id
        WHERE u.ativo = 1
    """).fetchall()
    hora_inicio_por_usuario = {e["usuario_id"]: e["hora_inicio"] for e in escalas_usuarios}

    ausencias_por_dia = {}
    for linha in conn.execute("""
        SELECT data, usuario_id FROM ausencias WHERE data BETWEEN ? AND ?
    """, (data_inicio_str, data_fim_str)).fetchall():
        ausencias_por_dia.setdefault(linha["data"], set()).add(linha["usuario_id"])

    # Quem teve pelo menos 1 login registrado em cada dia do período
    logins_por_dia = {}
    for linha in conn.execute("""
        SELECT date(login_em) as dia, usuario_id FROM sessoes WHERE date(login_em) BETWEEN ? AND ?
    """, (data_inicio_str, data_fim_str)).fetchall():
        logins_por_dia.setdefault(linha["dia"], set()).add(linha["usuario_id"])

    hoje_valor = hoje_str()
    agora = datetime.now()

    feriados_coletivos = {l["data"] for l in conn.execute(
        "SELECT data FROM calendario_feriados WHERE data BETWEEN ? AND ?", (data_inicio_str, data_fim_str)
    ).fetchall()}

    data_inicio = datetime.strptime(data_inicio_str, "%Y-%m-%d")
    data_fim = datetime.strptime(data_fim_str, "%Y-%m-%d")
    if (data_fim - data_inicio).days > 366:
        data_fim = data_inicio + timedelta(days=366)

    dias, escalados_serie, presentes_serie, ausentes_serie = [], [], [], []
    dia_atual = data_inicio
    while dia_atual <= data_fim:
        dia_str = dia_atual.strftime("%Y-%m-%d")
        abrev = DIAS_SEMANA[dia_atual.weekday()]

        # Feriado/folga coletiva: ninguém conta como escalado nesse dia --
        # não é que "todo mundo faltou", é que não tinha expediente. O dia
        # some do cálculo de absenteísmo (0 escalados = 0% contribuindo pra
        # média do período), em vez de contar 100% de ausência.
        if dia_str in feriados_coletivos:
            dias.append(dia_atual.strftime("%d/%m"))
            escalados_serie.append(0)
            presentes_serie.append(0)
            ausentes_serie.append(0)
            dia_atual += timedelta(days=1)
            continue

        escalados_hoje = set()
        for e in escalas_usuarios:
            dias_trabalho = (e["dias_trabalho"] or "").split(",")
            if abrev not in dias_trabalho:
                continue
            if e["ferias_inicio"] and e["ferias_fim"] and e["ferias_inicio"] <= dia_str <= e["ferias_fim"]:
                continue
            cadastro_data = (e["data_cadastro"] or "")[:10]
            if cadastro_data and dia_str < cadastro_data:
                continue
            escalados_hoje.add(e["usuario_id"])

        logaram_hoje = logins_por_dia.get(dia_str, set())
        ausentes_com_falta = ausencias_por_dia.get(dia_str, set()) & escalados_hoje
        nao_logaram = escalados_hoje - logaram_hoje

        if dia_str < hoje_valor:
            # Dia já passou: quem era escalado e nunca logou também conta como
            # ausente, mesmo sem falta formalmente registrada.
            ausentes_hoje = ausentes_com_falta | nao_logaram
        elif dia_str == hoje_valor:
            # Hoje: só conta como ausente quem já passou do próprio horário de
            # início da escala e ainda não logou (não marca "ausente" cedo demais).
            ainda_nao_chegou_no_horario = set()
            for usuario_id in nao_logaram:
                hora_inicio_str = hora_inicio_por_usuario.get(usuario_id)
                try:
                    hora_inicio_pessoa = datetime.strptime(hora_inicio_str, "%H:%M").time()
                except (ValueError, TypeError):
                    hora_inicio_pessoa = time(0, 0)
                if agora.time() >= hora_inicio_pessoa:
                    ainda_nao_chegou_no_horario.add(usuario_id)
            ausentes_hoje = ausentes_com_falta | ainda_nao_chegou_no_horario
        else:
            # Dia futuro: não dá pra saber ainda, considera só falta já registrada
            ausentes_hoje = ausentes_com_falta

        presentes_hoje = escalados_hoje - ausentes_hoje

        dias.append(dia_atual.strftime("%d/%m"))
        escalados_serie.append(len(escalados_hoje))
        presentes_serie.append(len(presentes_hoje))
        ausentes_serie.append(len(ausentes_hoje))

        dia_atual += timedelta(days=1)

    # Absenteísmo diário (%) -- pedido do Bruno, 2026-09-10: uma linha no
    # próprio gráfico com ausentes/escalados de cada dia, mais o agregado do
    # período inteiro (não é a média dos % diários -- é ausentes totais
    # dividido por escalados totais do período, que é o jeito correto de
    # calcular taxa quando o tamanho do "escalados" varia dia a dia).
    absenteismo_dia_pct = [
        round((ausentes_serie[i] / escalados_serie[i]) * 100, 1) if escalados_serie[i] else 0.0
        for i in range(len(dias))
    ]
    total_escalados_periodo = sum(escalados_serie)
    total_ausentes_periodo = sum(ausentes_serie)
    absenteismo_periodo_pct = round((total_ausentes_periodo / total_escalados_periodo) * 100, 1) if total_escalados_periodo else 0.0

    return dias, escalados_serie, presentes_serie, ausentes_serie, absenteismo_dia_pct, absenteismo_periodo_pct


PERFIS_HC_ATIVO = ("OPERADOR",)


def calcular_hc_ativo_serie(conn, data_inicio_str, data_fim_str):
    """Gráfico "HC Ativo" pedido pelo Bruno (2026-08-29): pra cada dia do
    período, quantas pessoas da operação (perfis em PERFIS_HC_ATIVO -- só
    Assistente/OPERADOR, restrito em 2026-08-29 a pedido do Bruno: não conta
    mais Analista/Analista_II/Supervisor, nem ADM/VISUALIZAÇÃO) estavam
    ativas (linha REAL), mais barras de contratações/desligamentos (evento
    no dia = usuarios.data_cadastro / data de saída) e férias/afastamento
    (ausencias.motivo, contagem de quantas pessoas estão de férias/afastadas
    naquele dia especificamente, não só no 1º dia -- também restrito aos
    mesmos perfis de PERFIS_HC_ATIVO).

    Data de saída considera tanto Excluir (usuarios.excluido_em) quanto
    Desligar simples sem excluir (usuarios.desativado_em) -- bug real
    encontrado pelo Bruno em 2026-09-10: antes só considerava Excluir, então
    desligar sem excluir o cadastro (o caso comum) nunca refletia no
    gráfico. "Desistência" foi removida (não existe mais essa situação na
    ferramenta). Adicionado também "HC Total" (real + férias + afastamento),
    pra comparar o quadro completo da equipe com quem está de fato
    disponível hoje."""
    usuarios_rel = conn.execute(f"""
        SELECT id, data_cadastro, excluido, excluido_em, desativado_em FROM usuarios
        WHERE perfil IN ({",".join("?" * len(PERFIS_HC_ATIVO))})
    """, PERFIS_HC_ATIVO).fetchall()
    ids_rel = {u["id"] for u in usuarios_rel}

    ferias_por_dia = {}
    afastamento_por_dia = {}
    for linha in conn.execute("""
        SELECT data, motivo, usuario_id FROM ausencias
        WHERE data BETWEEN ? AND ? AND motivo IN ('Férias', 'Afastamento')
    """, (data_inicio_str, data_fim_str)).fetchall():
        if linha["usuario_id"] not in ids_rel:
            continue  # só considera Assistente (OPERADOR) na conta, mesma regra da linha REAL
        alvo = ferias_por_dia if linha["motivo"] == "Férias" else afastamento_por_dia
        alvo.setdefault(linha["data"], set()).add(linha["usuario_id"])

    # Férias também podem ser cadastradas direto na tela de Escalas
    # (escalas.ferias_inicio/ferias_fim), sem passar pelo fluxo de
    # ausências do Supervisor -- são dois caminhos diferentes pro mesmo
    # conceito. Bug real encontrado pelo Bruno, 2026-09-10 (Larissa de
    # férias, cadastrada só via Escalas, não aparecia no gráfico)."""
    for linha in conn.execute("""
        SELECT usuario_id, ferias_inicio, ferias_fim FROM escalas
        WHERE ferias_inicio IS NOT NULL AND ferias_fim IS NOT NULL
    """).fetchall():
        if linha["usuario_id"] not in ids_rel:
            continue
        inicio_efetivo = max(linha["ferias_inicio"], data_inicio_str)
        fim_efetivo = min(linha["ferias_fim"], data_fim_str)
        dia_ferias = datetime.strptime(inicio_efetivo, "%Y-%m-%d") if inicio_efetivo <= fim_efetivo else None
        fim_ferias_dt = datetime.strptime(fim_efetivo, "%Y-%m-%d") if dia_ferias else None
        while dia_ferias and dia_ferias <= fim_ferias_dt:
            ferias_por_dia.setdefault(dia_ferias.strftime("%Y-%m-%d"), set()).add(linha["usuario_id"])
            dia_ferias += timedelta(days=1)

    data_inicio = datetime.strptime(data_inicio_str, "%Y-%m-%d")
    data_fim = datetime.strptime(data_fim_str, "%Y-%m-%d")
    if (data_fim - data_inicio).days > 366:
        data_fim = data_inicio + timedelta(days=366)

    dias, serie_real, serie_contratacoes, serie_desligamentos = [], [], [], []
    serie_ferias, serie_afastamento, serie_hc_total = [], [], []
    dia_atual = data_inicio
    while dia_atual <= data_fim:
        dia_str = dia_atual.strftime("%Y-%m-%d")
        ativos_hoje = 0
        contratacoes_hoje = 0
        desligamentos_hoje = 0
        for u in usuarios_rel:
            cadastro_data = (u["data_cadastro"] or "")[:10]
            # Data de saída: excluído por completo (excluido_em) OU só
            # desligado sem excluir (desativado_em) -- o que existir; se
            # tiver os dois (raro, mas possível se desligou e excluiu
            # depois), usa o mais antigo, que é a saída real da operação.
            saidas_possiveis = [d[:10] for d in (u["excluido_em"], u["desativado_em"]) if d]
            desligado_data = min(saidas_possiveis) if saidas_possiveis else ""
            if cadastro_data and cadastro_data == dia_str:
                contratacoes_hoje += 1
            if desligado_data and desligado_data == dia_str:
                desligamentos_hoje += 1
            if cadastro_data and cadastro_data <= dia_str and (not desligado_data or desligado_data >= dia_str):
                ativos_hoje += 1

        dias.append(dia_atual.strftime("%d/%m"))
        serie_real.append(ativos_hoje)
        serie_contratacoes.append(contratacoes_hoje)
        serie_desligamentos.append(desligamentos_hoje)
        qtd_ferias = len(ferias_por_dia.get(dia_str, ()))
        qtd_afastamento = len(afastamento_por_dia.get(dia_str, ()))
        serie_ferias.append(qtd_ferias)
        serie_afastamento.append(qtd_afastamento)
        # HC Total = quem está trabalhando hoje + quem está de férias ou
        # afastado -- pra comparar o "quadro completo" (contando quem devia
        # estar na equipe mas está numa ausência de longo prazo) com o HC
        # Real (só quem está efetivamente disponível hoje). Pedido do
        # Bruno, 2026-09-10.
        serie_hc_total.append(ativos_hoje + qtd_ferias + qtd_afastamento)

        dia_atual += timedelta(days=1)

    return {
        "dias": dias,
        "real": serie_real,
        "contratacoes": serie_contratacoes,
        "desligamentos": serie_desligamentos,
        "ferias": serie_ferias,
        "afastamento": serie_afastamento,
        "hc_total": serie_hc_total,
    }


# ==========================
# RELATÓRIO / BOT DO TELEGRAM
# ==========================

HORA_INICIO_PADRAO = time(8, 0)
HORA_FIM_PADRAO = time(17, 48)


# Feriados nacionais fixos (não inclui os móveis -- Carnaval, Sexta-Feira
# Santa, Corpus Christi -- que mudam de data todo ano e precisariam de
# cálculo à parte; cobre a maioria dos feriados do calendário mesmo assim).
# Usado pra decidir se hoje é dia útil (ex: pausar postagem automática de
# aniversário em fim de semana/feriado -- pedido do Bruno, 2026-09-04).
FERIADOS_NACIONAIS_FIXOS = {
    "01-01",  # Confraternização Universal
    "04-21",  # Tiradentes
    "05-01",  # Dia do Trabalho
    "09-07",  # Independência
    "10-12",  # Nossa Senhora Aparecida
    "11-02",  # Finados
    "11-15",  # Proclamação da República
    "11-20",  # Consciência Negra
    "12-25",  # Natal
}


def eh_dia_util(data):
    """Segunda a sexta e fora da lista de feriados fixos (ver
    FERIADOS_NACIONAIS_FIXOS). Não cobre feriado móvel nem feriado municipal
    -- suficiente pro caso de uso atual (pausar postagem automática de
    aniversário em dia não-útil)."""
    if data.weekday() >= 5:  # 5=Sáb, 6=Dom
        return False
    return data.strftime("%m-%d") not in FERIADOS_NACIONAIS_FIXOS


def dias_uteis_no_periodo(data_inicio, data_fim, max_weekday=4):
    """Conta dias úteis entre duas datas, incluindo ambas as pontas. Padrão
    Seg-Sex (max_weekday=4); passe max_weekday=5 pra contar Seg-Sáb -- usado
    na projeção de Execuções em campo (2026-08-28), já que equipe de campo
    costuma trabalhar aos sábados também, diferente do time de agendamento."""
    dias = 0
    atual = data_inicio
    while atual <= data_fim:
        if atual.weekday() <= max_weekday:  # 0=Seg ... 4=Sex, 5=Sáb
            dias += 1
        atual += timedelta(days=1)
    return dias


def calcular_ranking(conn, filtro_data_sql, params, agendados_apenas=False, limite=10, agrupar_por="operador"):
    """Ranking de fechamentos (ou só agendamentos), agrupado por operador OU por
    tipo de tabulação, no período dado."""
    coluna = "tabulacao" if agrupar_por == "tabulacao" else "operador"
    filtro_extra = " AND tabulacao='Agendado'" if agendados_apenas else ""
    linhas = conn.execute(f"""
        SELECT {coluna} as chave, COUNT(*) as qtd FROM atividades
        WHERE {filtro_data_sql} AND status='CONCLUIDO'{filtro_extra}
        GROUP BY {coluna} ORDER BY qtd DESC LIMIT {int(limite)}
    """, params).fetchall()
    total = conn.execute(f"""
        SELECT COUNT(*) c FROM atividades WHERE {filtro_data_sql} AND status='CONCLUIDO'{filtro_extra}
    """, params).fetchone()["c"]
    return linhas, total


def calcular_projecao_dia(total_hoje, agora=None):
    agora = agora or datetime.now()
    inicio = datetime.combine(agora.date(), HORA_INICIO_PADRAO)
    fim = datetime.combine(agora.date(), HORA_FIM_PADRAO)
    agora_limitado = min(max(agora, inicio), fim)
    minutos_decorridos = (agora_limitado - inicio).total_seconds() / 60
    minutos_totais = (fim - inicio).total_seconds() / 60
    if minutos_decorridos <= 0:
        return None
    pct = minutos_decorridos / minutos_totais
    return round(total_hoje / pct)


def calcular_projecao_mes(total_mes, hoje=None, max_weekday=4):
    """Projeção linear do mês em curso, proporcional a dias úteis já decorridos
    vs. dias úteis totais do mês. max_weekday segue o mesmo padrão de
    dias_uteis_no_periodo (4=Seg-Sex, 5=Seg-Sáb)."""
    hoje = hoje or datetime.now().date()
    inicio_mes = hoje.replace(day=1)
    if hoje.month == 12:
        proximo_mes = hoje.replace(year=hoje.year + 1, month=1, day=1)
    else:
        proximo_mes = hoje.replace(month=hoje.month + 1, day=1)
    fim_mes = proximo_mes - timedelta(days=1)

    uteis_decorridos = dias_uteis_no_periodo(inicio_mes, hoje, max_weekday)
    uteis_totais = dias_uteis_no_periodo(inicio_mes, fim_mes, max_weekday)
    if uteis_decorridos <= 0:
        return None
    return round(total_mes / uteis_decorridos * uteis_totais)


def variacao_pct(atual, anterior):
    if not anterior:
        return None
    return round((atual - anterior) / anterior * 100, 1)


def montar_bloco_ranking(titulo, linhas, total, comparacao_total, projecao, emoji_pos="🥇🥈🥉", eh_pessoa=True):
    texto = f"*{titulo}* — total: {total}"
    var = variacao_pct(total, comparacao_total)
    if var is not None:
        seta = "🔺" if var >= 0 else "🔻"
        texto += f" ({seta}{abs(var)}% vs período anterior: {comparacao_total})"
    if projecao is not None:
        texto += f"\n📈 Projeção: ~{projecao}"
    texto += "\n"
    if linhas:
        for i, linha in enumerate(linhas, start=1):
            marcador = emoji_pos[i - 1] if i <= 3 else f"{i}."
            rotulo = linha["chave"] or "—"
            if eh_pessoa:
                rotulo = rotulo.title()
            texto += f"{marcador} {rotulo} — {linha['qtd']}\n"
    else:
        texto += "_Sem registros no período._\n"
    return texto


def calcular_dados_relatorio(conn):
    """Calcula todos os números do relatório (fechamentos, agendamentos,
    situação da fila) -- independente de como vai ser exibido depois
    (Telegram, e-mail, etc)."""
    agora = datetime.now()
    hoje = agora.strftime("%Y-%m-%d")
    ontem = (agora - timedelta(days=1)).strftime("%Y-%m-%d")

    inicio_mes_atual = agora.replace(day=1).strftime("%Y-%m-%d")
    dia_do_mes = agora.day
    mes_anterior_ref = (agora.replace(day=1) - timedelta(days=1))
    inicio_mes_anterior = mes_anterior_ref.replace(day=1).strftime("%Y-%m-%d")
    try:
        fim_mesmo_periodo_mes_anterior = mes_anterior_ref.replace(day=dia_do_mes).strftime("%Y-%m-%d")
    except ValueError:
        fim_mesmo_periodo_mes_anterior = mes_anterior_ref.strftime("%Y-%m-%d")

    rank_fech_hoje, total_fech_hoje = calcular_ranking(conn, "date(data_fim) = ?", [hoje], agrupar_por="tabulacao")
    _, total_fech_ontem = calcular_ranking(conn, "date(data_fim) = ?", [ontem], agrupar_por="tabulacao")
    rank_fech_mes, total_fech_mes = calcular_ranking(conn, "date(data_fim) BETWEEN ? AND ?", [inicio_mes_atual, hoje], agrupar_por="tabulacao")
    _, total_fech_mes_ant = calcular_ranking(conn, "date(data_fim) BETWEEN ? AND ?", [inicio_mes_anterior, fim_mesmo_periodo_mes_anterior], agrupar_por="tabulacao")

    rank_agend_hoje, total_agend_hoje = calcular_ranking(conn, "date(data_fim) = ?", [hoje], agendados_apenas=True)
    _, total_agend_ontem = calcular_ranking(conn, "date(data_fim) = ?", [ontem], agendados_apenas=True)
    rank_agend_mes, total_agend_mes = calcular_ranking(conn, "date(data_fim) BETWEEN ? AND ?", [inicio_mes_atual, hoje], agendados_apenas=True)
    _, total_agend_mes_ant = calcular_ranking(conn, "date(data_fim) BETWEEN ? AND ?", [inicio_mes_anterior, fim_mesmo_periodo_mes_anterior], agendados_apenas=True)

    pendentes = conn.execute("SELECT COUNT(*) c FROM mailing WHERE status='PENDENTE'").fetchone()["c"]
    andamento = conn.execute("SELECT COUNT(*) c FROM mailing WHERE status='EM_ANDAMENTO'").fetchone()["c"]
    enfileirados = conn.execute("SELECT COUNT(*) c FROM mailing WHERE status='ENFILEIRADO'").fetchone()["c"]
    concluidos = conn.execute("SELECT COUNT(*) c FROM mailing WHERE status='CONCLUIDO'").fetchone()["c"]

    return {
        "agora": agora,
        "fechamentos": {
            "hoje": {"rank": rank_fech_hoje, "total": total_fech_hoje, "comparacao": total_fech_ontem,
                     "projecao": calcular_projecao_dia(total_fech_hoje, agora)},
            "mes": {"rank": rank_fech_mes, "total": total_fech_mes, "comparacao": total_fech_mes_ant,
                    "projecao": calcular_projecao_mes(total_fech_mes, agora.date())},
        },
        "agendamentos": {
            "hoje": {"rank": rank_agend_hoje, "total": total_agend_hoje, "comparacao": total_agend_ontem,
                     "projecao": calcular_projecao_dia(total_agend_hoje, agora)},
            "mes": {"rank": rank_agend_mes, "total": total_agend_mes, "comparacao": total_agend_mes_ant,
                    "projecao": calcular_projecao_mes(total_agend_mes, agora.date())},
        },
        "situacao": {"pendentes": pendentes, "andamento": andamento, "enfileirados": enfileirados, "concluidos": concluidos},
    }


def montar_relatorio_telegram(dados):
    agora = dados["agora"]
    texto = f"📊 *CRM RSI — Relatório de {agora.strftime('%d/%m/%Y')} às {agora.strftime('%H:%M')}*\n\n"

    texto += "🏆 *RANKING DE FECHAMENTOS*\n\n"
    texto += montar_bloco_ranking("Hoje", dados["fechamentos"]["hoje"]["rank"], dados["fechamentos"]["hoje"]["total"],
                                   dados["fechamentos"]["hoje"]["comparacao"], dados["fechamentos"]["hoje"]["projecao"], eh_pessoa=False)
    texto += "\n"
    texto += montar_bloco_ranking("Mês", dados["fechamentos"]["mes"]["rank"], dados["fechamentos"]["mes"]["total"],
                                   dados["fechamentos"]["mes"]["comparacao"], dados["fechamentos"]["mes"]["projecao"], eh_pessoa=False)

    texto += "\n📅 *RANKING DE AGENDAMENTOS*\n\n"
    texto += montar_bloco_ranking("Hoje", dados["agendamentos"]["hoje"]["rank"], dados["agendamentos"]["hoje"]["total"],
                                   dados["agendamentos"]["hoje"]["comparacao"], dados["agendamentos"]["hoje"]["projecao"])
    texto += "\n"
    texto += montar_bloco_ranking("Mês", dados["agendamentos"]["mes"]["rank"], dados["agendamentos"]["mes"]["total"],
                                   dados["agendamentos"]["mes"]["comparacao"], dados["agendamentos"]["mes"]["projecao"])

    s = dados["situacao"]
    texto += "\n📋 *SITUAÇÃO ATUAL DA FILA*\n"
    texto += f"Pendentes: {s['pendentes']}\n"
    texto += f"Em andamento: {s['andamento']}\n"
    texto += f"Enfileirados (follow-up): {s['enfileirados']}\n"
    texto += f"Concluídos: {s['concluidos']}\n"

    return texto


def _bloco_ranking_html(titulo, bloco, cor_destaque, eh_pessoa=True):
    var = variacao_pct(bloco["total"], bloco["comparacao"])
    var_html = ""
    if var is not None:
        cor_var = "#1E8E4A" if var >= 0 else "#E4002B"
        seta = "▲" if var >= 0 else "▼"
        var_html = f'<span style="color:{cor_var}; font-weight:600;">{seta} {abs(var)}% vs período anterior ({bloco["comparacao"]})</span><br>'

    linhas_html = ""
    if bloco["rank"]:
        for i, linha in enumerate(bloco["rank"], start=1):
            rotulo = linha["chave"] or "—"
            if eh_pessoa:
                rotulo = rotulo.title()
            linhas_html += f"""
            <tr>
                <td style="padding:6px 10px; color:#6E6E76; font-size:13px; width:28px;">{i}º</td>
                <td style="padding:6px 10px; font-size:14px;">{rotulo}</td>
                <td style="padding:6px 10px; font-size:14px; font-weight:700; text-align:right;">{linha['qtd']}</td>
            </tr>"""
    else:
        linhas_html = '<tr><td colspan="3" style="padding:8px 10px; color:#6E6E76; font-size:13px;">Sem registros no período.</td></tr>'

    projecao_html = ""
    if bloco["projecao"] is not None:
        projecao_html = f'<span style="font-size:12.5px; color:#6E6E76;">📈 Projeção: ~{bloco["projecao"]}</span>'

    return f"""
    <table cellpadding="0" cellspacing="0" border="0" width="100%" style="margin-bottom:18px;">
        <tr>
            <td style="border-left:4px solid {cor_destaque}; padding-left:10px;">
                <strong style="font-size:15px;">{titulo}</strong> — total: <strong>{bloco['total']}</strong><br>
                {var_html}{projecao_html}
            </td>
        </tr>
        <tr>
            <td>
                <table cellpadding="0" cellspacing="0" border="0" width="100%" style="margin-top:6px;">{linhas_html}</table>
            </td>
        </tr>
    </table>"""


def montar_relatorio_email_html(dados):
    agora = dados["agora"]
    s = dados["situacao"]

    return f"""
    <table cellpadding="0" cellspacing="0" border="0" width="100%" style="max-width:620px; margin:0 auto; font-family: Arial, Helvetica, sans-serif; color:#14141A;">
        <tr>
            <td bgcolor="#A2001D" style="background-color:#A2001D; padding:22px 24px; color:#ffffff;">
                <span style="font-size:11px; letter-spacing:.08em; text-transform:uppercase; color:#f3c9d0;">CRM RSI &mdash; Relatório</span><br>
                <span style="font-size:20px; font-weight:800; color:#ffffff;">{agora.strftime('%d/%m/%Y')} às {agora.strftime('%H:%M')}</span>
            </td>
        </tr>
        <tr><td style="padding-top:20px;"></td></tr>
        <tr>
            <td>
                <h2 style="font-size:16px; margin-bottom:6px;">🏆 Ranking de Fechamentos</h2>
                {_bloco_ranking_html("Hoje", dados["fechamentos"]["hoje"], "#E4002B", eh_pessoa=False)}
                {_bloco_ranking_html("Mês", dados["fechamentos"]["mes"], "#A2001D", eh_pessoa=False)}

                <h2 style="font-size:16px; margin-bottom:6px; margin-top:22px;">📅 Ranking de Agendamentos</h2>
                {_bloco_ranking_html("Hoje", dados["agendamentos"]["hoje"], "#1D4ED8")}
                {_bloco_ranking_html("Mês", dados["agendamentos"]["mes"], "#1D4ED8")}

                <h2 style="font-size:16px; margin-bottom:10px; margin-top:22px;">📋 Situação Atual da Fila</h2>
                <table cellpadding="0" cellspacing="0" border="0" width="100%">
                    <tr bgcolor="#F3F3F1" style="background-color:#F3F3F1;">
                        <td style="padding:10px 12px;">Pendentes</td>
                        <td style="padding:10px 12px; font-weight:700; text-align:right;">{s['pendentes']}</td>
                    </tr>
                    <tr><td style="padding:10px 12px;">Em andamento</td><td style="padding:10px 12px; font-weight:700; text-align:right;">{s['andamento']}</td></tr>
                    <tr bgcolor="#F3F3F1" style="background-color:#F3F3F1;">
                        <td style="padding:10px 12px;">Enfileirados (follow-up)</td>
                        <td style="padding:10px 12px; font-weight:700; text-align:right;">{s['enfileirados']}</td>
                    </tr>
                    <tr><td style="padding:10px 12px;">Concluídos</td><td style="padding:10px 12px; font-weight:700; text-align:right;">{s['concluidos']}</td></tr>
                </table>

                <p style="font-size:11px; color:#6E6E76; margin-top:24px; text-align:center;">
                    CRM RSI — Migração Claro Fibra · relatório automático
                </p>
            </td>
        </tr>
    </table>"""


def chamar_funcao_supabase(project_url, anon_key, nome_funcao, payload):
    """Chama uma Edge Function do Supabase via POST. Retorna (sucesso, resultado_ou_erro)."""
    import urllib.request
    import urllib.error
    import json as json_lib

    if not project_url or not anon_key:
        return False, "Configure a URL do projeto e a Anon Key do Supabase antes de usar."

    # Corrige sozinho os enganos mais comuns de copiar a URL errada do painel
    # do Supabase (ex: colar a URL da API REST em vez da URL base do projeto)
    project_url = project_url.strip()
    for sufixo in ("/rest/v1/", "/rest/v1", "/functions/v1/", "/functions/v1"):
        if project_url.endswith(sufixo):
            project_url = project_url[: -len(sufixo)]
            break
    project_url = project_url.rstrip("/")

    url = f"{project_url.rstrip('/')}/functions/v1/{nome_funcao}"
    try:
        req = urllib.request.Request(
            url,
            data=json_lib.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {anon_key}"},
        )
        with urllib.request.urlopen(req, timeout=15) as resposta:
            corpo = json_lib.loads(resposta.read().decode("utf-8"))
            return True, corpo
    except urllib.error.HTTPError as erro:
        try:
            corpo_erro = erro.read().decode("utf-8", errors="replace")[:400]
        except Exception:
            corpo_erro = str(erro)
        return False, corpo_erro
    except Exception as erro:
        return False, str(erro)


def enviar_relatorio_telegram_via_supabase(project_url, anon_key, texto):
    """Manda o relatório pro Telegram através da Edge Function do Supabase.
    Retorna (sucesso, mensagem_erro)."""
    sucesso, resultado = chamar_funcao_supabase(project_url, anon_key, "enviar-relatorio", {"texto": texto})
    if not sucesso:
        return False, resultado
    if isinstance(resultado, dict) and resultado.get("ok"):
        return True, None
    erro = resultado.get("erro") or resultado.get("description") if isinstance(resultado, dict) else str(resultado)
    return False, erro or "erro desconhecido"


def salvar_relatorio_para_consulta_via_supabase(project_url, anon_key, texto):
    """Guarda a versão mais recente do relatório no Supabase (não manda pro
    Telegram) -- é isso que o comando /status do bot vai ler depois quando
    alguém consultar durante o dia. Retorna (sucesso, mensagem_erro)."""
    sucesso, resultado = chamar_funcao_supabase(project_url, anon_key, "salvar-relatorio", {"texto": texto})
    if not sucesso:
        return False, resultado
    if isinstance(resultado, dict) and resultado.get("ok"):
        return True, None
    erro = resultado.get("erro") if isinstance(resultado, dict) else str(resultado)
    return False, erro or "erro desconhecido"


def descobrir_chat_id_via_supabase(project_url, anon_key):
    """Chama a Edge Function de descoberta de chat_id. Retorna (sucesso, chats_ou_erro)."""
    sucesso, resultado = chamar_funcao_supabase(project_url, anon_key, "descobrir-chat-id", {})
    if not sucesso:
        return False, resultado
    if isinstance(resultado, dict) and resultado.get("ok"):
        return True, resultado.get("chats", {})
    erro = resultado.get("erro") if isinstance(resultado, dict) else str(resultado)
    return False, erro or "erro desconhecido"


def escrever_arquivo_relatorio(pasta_destino, corpo_html):
    """Escreve o relatório em HTML numa pasta local (pensada pra ser uma pasta
    sincronizada do OneDrive). O CRM não fala com a Microsoft em nenhum momento
    aqui -- quem sobe o arquivo é o próprio OneDrive, e quem manda o e-mail
    depois é um fluxo do Power Automate configurado por fora. Retorna
    (sucesso, mensagem_erro_ou_caminho)."""
    if not pasta_destino:
        return False, "Configure a pasta de destino antes de gerar o relatório."
    if not os.path.isdir(pasta_destino):
        return False, f"A pasta '{pasta_destino}' não existe ou não está acessível nesse computador."

    nome_arquivo = f"relatorio_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}.html"
    caminho_completo = os.path.join(pasta_destino, nome_arquivo)
    try:
        with open(caminho_completo, "w", encoding="utf-8") as arquivo:
            arquivo.write(corpo_html)
        return True, caminho_completo
    except Exception as erro:
        return False, str(erro)



def enviar_mensagem_telegram(token, chat_id, texto):
    """Envia uma mensagem via API do Telegram. Retorna (sucesso, mensagem_erro)."""
    import urllib.request
    import urllib.parse
    import urllib.error
    import json as json_lib

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    dados = urllib.parse.urlencode({
        "chat_id": chat_id, "text": texto, "parse_mode": "Markdown",
    }).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=dados, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resposta:
            corpo = json_lib.loads(resposta.read().decode("utf-8"))
            if corpo.get("ok"):
                return True, None
            return False, corpo.get("description", "erro desconhecido")
    except urllib.error.HTTPError as erro:
        try:
            corpo_erro = erro.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            corpo_erro = ""
        eh_resposta_do_telegram = False
        try:
            json_lib.loads(corpo_erro)
            eh_resposta_do_telegram = True
        except (ValueError, TypeError):
            pass
        if eh_resposta_do_telegram:
            return False, f"Telegram respondeu HTTP {erro.code}: {corpo_erro}"
        return False, (
            f"HTTP {erro.code} -- a resposta não veio em formato do Telegram (provavelmente é a rede da "
            f"empresa bloqueando o acesso a api.telegram.org pra esse programa, não o Telegram recusando). "
            f"Detalhe técnico: {corpo_erro[:150]}"
        )
    except Exception as erro:
        return False, str(erro)


def telegram_configurado(conn):
    linha = conn.execute("SELECT telegram_bot_token, telegram_chat_id FROM configuracoes WHERE id=1").fetchone()
    return bool(linha and linha["telegram_bot_token"] and linha["telegram_chat_id"])


def _rotina_email_envio_automatico():
    """Roda em segundo plano -- todo dia, no horário e nos dias da semana
    configurados, gera o arquivo do relatório sozinho na pasta configurada
    (só uma vez por dia)."""
    while True:
        try:
            conn = conectar()
            config = conn.execute("""
                SELECT email_pasta_destino, email_envio_automatico_ativo, email_horario_envio,
                       email_ultimo_envio_data, email_dias_semana
                FROM configuracoes WHERE id=1
            """).fetchone()
            if config and config["email_pasta_destino"] and (
                config["email_envio_automatico_ativo"] is None or config["email_envio_automatico_ativo"]
            ):
                agora = datetime.now()
                hoje_str_valor = agora.strftime("%Y-%m-%d")
                abrev_hoje = DIAS_SEMANA[agora.weekday()]
                dias_configurados = (config["email_dias_semana"] or "SEG,TER,QUA,QUI,SEX").split(",")

                horario_configurado = config["email_horario_envio"] or "18:00"
                try:
                    hora_alvo = datetime.strptime(horario_configurado, "%H:%M").time()
                except ValueError:
                    hora_alvo = time(18, 0)

                ja_enviou_hoje = config["email_ultimo_envio_data"] == hoje_str_valor
                if abrev_hoje in dias_configurados and agora.time() >= hora_alvo and not ja_enviou_hoje:
                    dados = calcular_dados_relatorio(conn)
                    corpo_html = montar_relatorio_email_html(dados)
                    sucesso, _ = escrever_arquivo_relatorio(config["email_pasta_destino"], corpo_html)
                    if sucesso:
                        conn.execute("UPDATE configuracoes SET email_ultimo_envio_data=? WHERE id=1", (hoje_str_valor,))
                        conn.commit()
            conn.close()
        except Exception as erro:
            print(f"Erro na rotina de geração automática do relatório: {erro}")
        tempo_espera.sleep(60)


def _rotina_telegram_supabase_envio_automatico():
    """Roda em segundo plano -- no horário e dias da semana configurados,
    manda o relatório pro Telegram através da Edge Function do Supabase
    (só uma vez por dia)."""
    while True:
        try:
            conn = conectar()
            config = conn.execute("""
                SELECT supabase_project_url, supabase_anon_key, supabase_telegram_ativo,
                       supabase_telegram_horario_envio, supabase_telegram_dias_semana, supabase_telegram_ultimo_envio_data
                FROM configuracoes WHERE id=1
            """).fetchone()
            if config and config["supabase_project_url"] and config["supabase_anon_key"] and config["supabase_telegram_ativo"]:
                agora = datetime.now()
                hoje_str_valor = agora.strftime("%Y-%m-%d")
                abrev_hoje = DIAS_SEMANA[agora.weekday()]
                dias_configurados = (config["supabase_telegram_dias_semana"] or "SEG,TER,QUA,QUI,SEX").split(",")

                horario_configurado = config["supabase_telegram_horario_envio"] or "18:00"
                try:
                    hora_alvo = datetime.strptime(horario_configurado, "%H:%M").time()
                except ValueError:
                    hora_alvo = time(18, 0)

                ja_enviou_hoje = config["supabase_telegram_ultimo_envio_data"] == hoje_str_valor
                if abrev_hoje in dias_configurados and agora.time() >= hora_alvo and not ja_enviou_hoje:
                    dados = calcular_dados_relatorio(conn)
                    texto = montar_relatorio_telegram(dados)
                    sucesso, _ = enviar_relatorio_telegram_via_supabase(config["supabase_project_url"], config["supabase_anon_key"], texto)
                    if sucesso:
                        conn.execute("UPDATE configuracoes SET supabase_telegram_ultimo_envio_data=? WHERE id=1", (hoje_str_valor,))
                        conn.commit()
            conn.close()
        except Exception as erro:
            print(f"Erro na rotina de envio automático do Telegram via Supabase: {erro}")
        tempo_espera.sleep(60)


def _rotina_supabase_consulta_periodica():
    """Roda em segundo plano -- de hora em hora, das 8h às 18h, guarda a
    versão mais recente do relatório no Supabase, pra permitir consulta via
    /status no Telegram a qualquer momento do dia (sem precisar do CRM
    "acordado" na hora exata da pergunta)."""
    while True:
        try:
            conn = conectar()
            config = conn.execute("""
                SELECT supabase_project_url, supabase_anon_key, supabase_consulta_ativa, supabase_consulta_ultima_atualizacao
                FROM configuracoes WHERE id=1
            """).fetchone()
            if config and config["supabase_project_url"] and config["supabase_anon_key"] and (
                config["supabase_consulta_ativa"] is None or config["supabase_consulta_ativa"]
            ):
                agora = datetime.now()
                dentro_do_horario = time(8, 0) <= agora.time() < time(18, 0)
                marca_hora_atual = agora.strftime("%Y-%m-%d %H:00")
                ja_atualizou_essa_hora = config["supabase_consulta_ultima_atualizacao"] == marca_hora_atual

                if dentro_do_horario and not ja_atualizou_essa_hora:
                    dados = calcular_dados_relatorio(conn)
                    texto = montar_relatorio_telegram(dados)
                    sucesso, _ = salvar_relatorio_para_consulta_via_supabase(
                        config["supabase_project_url"], config["supabase_anon_key"], texto
                    )
                    if sucesso:
                        conn.execute(
                            "UPDATE configuracoes SET supabase_consulta_ultima_atualizacao=? WHERE id=1",
                            (marca_hora_atual,)
                        )
                        conn.commit()
            conn.close()
        except Exception as erro:
            print(f"Erro na rotina de consulta periódica via Supabase: {erro}")
        tempo_espera.sleep(60)



def calcular_produtividade(conn, filtro_data_sql, params_data, operador_nome=None):
    """Retorna (motivos[], finalizados, agendados, conversao_pct) pro filtro de data e operador dados."""
    filtro_operador = " AND operador = ?" if operador_nome else ""
    params = list(params_data) + ([operador_nome] if operador_nome else [])

    motivos = conn.execute(f"""
        SELECT tabulacao, COUNT(*) as qtd FROM atividades
        WHERE {filtro_data_sql}{filtro_operador}
        GROUP BY tabulacao ORDER BY qtd DESC
    """, params).fetchall()

    finalizados = conn.execute(f"""
        SELECT COUNT(*) FROM atividades WHERE {filtro_data_sql}{filtro_operador} AND status='CONCLUIDO'
    """, params).fetchone()[0]

    agendados = conn.execute(f"""
        SELECT COUNT(*) FROM atividades WHERE {filtro_data_sql}{filtro_operador} AND status='CONCLUIDO' AND tabulacao='Agendado'
    """, params).fetchone()[0]

    conversao = round((agendados / finalizados * 100), 1) if finalizados else 0.0
    return motivos, finalizados, agendados, conversao


LIMITE_DIAS_SERIE_AGENDADO_EXECUTADO = 180  # trava de sanidade -- nunca desenha mais que ~6 meses de uma vez


def calcular_serie_agendado_executado(conn, data_inicio_str, data_fim_str, operador_nome=None):
    """Série DIÁRIA de volume de Agendado (concluído nesse dia, mesmo critério
    data_fim usado no resto do Dashboard) x Execuções em campo (dh_baixa
    nesse dia) -- substituiu a "tendência semanal de conversão" a pedido do
    Bruno em 2026-08-28, junto com a chegada da visão de execuções: agora dá
    pra comparar direto volume agendado x volume executado por dia, sem
    passar por conversão de tabulação (que só media agendamento). Zero-
    preenche dias sem nenhum registro, mesmo espírito da função anterior.
    Quando operador_nome é passado (visão pessoal), filtra os dois lados:
    agendados pela coluna `operador`, execuções pelo login do operador (mesmo
    JOIN de calcular_execucoes)."""
    dt_inicio = datetime.strptime(data_inicio_str, "%Y-%m-%d")
    dt_fim = datetime.strptime(data_fim_str, "%Y-%m-%d")
    if dt_fim < dt_inicio:
        dt_inicio, dt_fim = dt_fim, dt_inicio
    if (dt_fim - dt_inicio).days > LIMITE_DIAS_SERIE_AGENDADO_EXECUTADO:
        dt_fim = dt_inicio + timedelta(days=LIMITE_DIAS_SERIE_AGENDADO_EXECUTADO)
    data_inicio_str = dt_inicio.strftime("%Y-%m-%d")
    data_fim_str = dt_fim.strftime("%Y-%m-%d")

    filtro_operador_agendados = " AND operador = ?" if operador_nome else ""
    params_agendados = [data_inicio_str, data_fim_str] + ([operador_nome] if operador_nome else [])
    linhas_agendados = conn.execute(f"""
        SELECT date(data_fim) as dia, COUNT(*) as qtd FROM atividades
        WHERE date(data_fim) BETWEEN ? AND ? {filtro_operador_agendados}
              AND status='CONCLUIDO' AND tabulacao='Agendado'
        GROUP BY dia
    """, params_agendados).fetchall()
    mapa_agendados = {l["dia"]: l["qtd"] for l in linhas_agendados}

    if operador_nome:
        linhas_execucoes = conn.execute("""
            SELECT date(e.dh_baixa) as dia, COUNT(*) as qtd
            FROM execucoes_os e JOIN usuarios u ON lower(u.login) = e.login_abertura
            WHERE date(e.dh_baixa) BETWEEN ? AND ? AND u.nome = ?
            GROUP BY dia
        """, [data_inicio_str, data_fim_str, operador_nome]).fetchall()
    else:
        linhas_execucoes = conn.execute("""
            SELECT date(dh_baixa) as dia, COUNT(*) as qtd FROM execucoes_os
            WHERE date(dh_baixa) BETWEEN ? AND ?
            GROUP BY dia
        """, [data_inicio_str, data_fim_str]).fetchall()
    mapa_execucoes = {l["dia"]: l["qtd"] for l in linhas_execucoes}

    labels, dias_serie, serie_agendados, serie_execucoes = [], [], [], []
    dia_atual = dt_inicio
    while dia_atual <= dt_fim:
        chave = dia_atual.strftime("%Y-%m-%d")
        dias_serie.append(chave)
        labels.append(f"{chave[8:10]}/{chave[5:7]}")
        serie_agendados.append(mapa_agendados.get(chave, 0))
        serie_execucoes.append(mapa_execucoes.get(chave, 0))
        dia_atual += timedelta(days=1)

    return labels, serie_agendados, serie_execucoes


@app.route("/")
@login_required
def dashboard():
    usuario = usuario_atual()
    if usuario["perfil"] in ("VISUALIZACAO", "OPERADOR_CONF"):
        # VISUALIZACAO só existe pro Painel de Resultados, OPERADOR_CONF só
        # existe pra Confirmação de Agenda -- nenhum dos dois tem acesso ao
        # Dashboard principal, de propósito (ver /portal). Bug real
        # encontrado pelo Bruno, 2026-09-16: sem essa checagem, quem cai
        # aqui (ex: depois de trocar a senha) esbarra num erro de acesso
        # negado em vez de ser mandado de volta pro portal.
        return redirect("/portal")
    # Qual visão do Dashboard a pessoa vê agora é configurável pelo ADM em
    # /admin/permissoes (sub-permissões "dashboard_equipe" e "dashboard_pessoal",
    # reestruturado em 2026-08-26 -- antes era hardcoded por perfil, sem passar
    # pela matriz nem ter qualquer bloqueio de acesso). Quando o perfil tem as
    # duas (ex: ADM, sempre por bypass), a visão de equipe prevalece.
    if perfil_pode(usuario["perfil"], "dashboard_equipe"):
        escopo_pessoal = False
    elif perfil_pode(usuario["perfil"], "dashboard_pessoal"):
        escopo_pessoal = True
    else:
        return "Você não tem acesso a nenhuma visão do Dashboard. Fale com o ADM.", 403
    data_escolhida = request.args.get("data") or datetime.now().strftime("%Y-%m-%d")
    mes_referencia = data_escolhida[:7]

    conn = conectar()
    pendentes = conn.execute("SELECT COUNT(*) FROM mailing WHERE status='PENDENTE'").fetchone()[0]
    andamento = conn.execute("SELECT COUNT(*) FROM mailing WHERE status='EM_ANDAMENTO'").fetchone()[0]
    enfileirados = conn.execute("SELECT COUNT(*) FROM mailing WHERE status='ENFILEIRADO'").fetchone()[0]
    concluidos = conn.execute("SELECT COUNT(*) FROM mailing WHERE status='CONCLUIDO'").fetchone()[0]

    por_pessoa = None
    meus_atendimentos = None
    operador_filtro = usuario["nome"] if escopo_pessoal else None

    motivos, finalizados_dia, agendados_dia, conversao_dia = calcular_produtividade(
        conn, "date(data_fim) = ?", [data_escolhida], operador_filtro
    )
    motivos_mes, finalizados_mes, agendados_mes, conversao_mes = calcular_produtividade(
        conn, "strftime('%Y-%m', data_fim) = ?", [mes_referencia], operador_filtro
    )

    if escopo_pessoal:
        # "Tempo efetivo de tela" (2026-08-26): usa minutos_efetivos (já desconta
        # pausa/logout) quando a atividade foi gravada depois dessa migração;
        # cai no cálculo antigo (relógio corrido) só pra atividades históricas
        # de antes dela, que ficaram com minutos_efetivos NULL.
        meus_atendimentos = conn.execute("""
            SELECT contrato, tabulacao, data_inicio, data_fim,
                   COALESCE(minutos_efetivos, (julianday(data_fim) - julianday(data_inicio)) * 24 * 60) as minutos
            FROM atividades
            WHERE operador = ? AND date(data_fim) = ?
            ORDER BY data_fim
        """, (usuario["nome"], data_escolhida)).fetchall()
    else:
        por_pessoa = conn.execute("""
            SELECT a.operador, u.id as usuario_id, COUNT(*) as qtd,
                   AVG(COALESCE(a.minutos_efetivos, (julianday(a.data_fim) - julianday(a.data_inicio)) * 24 * 60)) as media_minutos
            FROM atividades a
            LEFT JOIN usuarios u ON u.nome = a.operador
            WHERE date(a.data_fim) = ?
            GROUP BY a.operador
            ORDER BY qtd DESC
        """, (data_escolhida,)).fetchall()

    total_motivos = sum(m["qtd"] for m in motivos) or 1
    total_motivos_mes = sum(m["qtd"] for m in motivos_mes) or 1

    grafico_dias = grafico_escalados = grafico_presentes = grafico_ausentes = None
    grafico_absenteismo_dia = absenteismo_periodo_pct = None
    hoje_str_dashboard = datetime.now().strftime("%Y-%m-%d")
    grafico_data_inicio = request.args.get("grafico_inicio") or datetime.now().replace(day=1).strftime("%Y-%m-%d")
    grafico_data_fim = request.args.get("grafico_fim") or hoje_str_dashboard
    # HC Ativo e Escalados x Presentes olham pra trás, não faz sentido mostrar
    # "futuro" vazio no gráfico -- trava o fim em hoje (D0), mesmo que alguém
    # digite uma data futura direto na URL ou no filtro (pedido do Bruno, 2026-09-04).
    if grafico_data_fim > hoje_str_dashboard:
        grafico_data_fim = hoje_str_dashboard
    envelhecimento = None
    hc_ativo = None

    # Execuções em campo (OS's baixadas no CRM oficial -- pedido do Bruno,
    # 2026-08-28, ver seção "EXECUÇÕES DE OS"). Mostra "ontem" (não "hoje")
    # porque a importação é sempre D-1 -- o Bruno sobe hoje o que foi
    # executado ontem, então "hoje" aqui sempre daria zero logo depois do
    # upload.
    #
    # Calculado sempre (não só na visão de equipe) desde 2026-09-04, a pedido
    # do Bruno: o velocímetro de execuções é um resultado coletivo do time,
    # mas o Operador também pode ver ele no dashboard pessoal dele -- só o
    # resto do bloco de equipe (mapa de calor, HC Ativo, envelhecimento etc.)
    # continua exclusivo da visão de equipe.
    ontem_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    execucoes_ontem = calcular_execucoes(conn, "dh_baixa = ?", [ontem_str])
    execucoes_mes = calcular_execucoes(conn, "strftime('%Y-%m', dh_baixa) = ?", [mes_referencia])
    agendados_ontem = None

    if not escopo_pessoal:
        (grafico_dias, grafico_escalados, grafico_presentes, grafico_ausentes,
         grafico_absenteismo_dia, absenteismo_periodo_pct) = calcular_grafico_escala_presenca(
            conn, grafico_data_inicio, grafico_data_fim
        )
        # HC Ativo (pedido pelo Bruno, 2026-08-29) -- mesmo período do
        # gráfico de escalados/presença acima, pra não precisar de mais um
        # filtro de data na tela.
        hc_ativo = calcular_hc_ativo_serie(conn, grafico_data_inicio, grafico_data_fim)
        # Alerta de "lead envelhecendo" (pedido do Bruno, 2026-08-27) -- visão
        # de fila é sempre coletiva (um contrato PENDENTE não tem operador
        # ainda), então só faz sentido na visão de equipe, igual o gráfico
        # de escala/presença acima.
        envelhecimento = calcular_leads_envelhecidos(conn)

        # Compara com o total de "Agendado" do mesmo dia/mês -- é uma
        # comparação de VOLUME (mesmo espírito do conversao_pct que a tela já
        # usa pra finalizados/agendados), não um pareamento agendamento a
        # agendamento (isso é o que a coluna "Executado?" do Relatório de
        # Agendados faz, contrato a contrato). Só faz sentido na visão de
        # equipe (compara com agendados_mes, que também é só de equipe).
        _, _, agendados_ontem, _ = calcular_produtividade(conn, "date(data_fim) = ?", [ontem_str], None)
        execucoes_ontem["pct_vs_agendados"] = round(execucoes_ontem["total"] / agendados_ontem * 100) if agendados_ontem else None
        execucoes_mes["pct_vs_agendados"] = round(execucoes_mes["total"] / agendados_mes * 100) if agendados_mes else None

    # Gráfico de linhas Agendado x Executado por dia (substituiu a "tendência
    # semanal de conversão" a pedido do Bruno, 2026-08-28) -- disponível nas
    # duas visões: time inteiro na visão de equipe, só a própria série na
    # visão pessoal (reaproveita o mesmo operador_filtro de produtividade
    # dia/mês acima). Padrão do mês corrente, com filtro de período próprio.
    serie_data_inicio = request.args.get("serie_inicio") or datetime.now().replace(day=1).strftime("%Y-%m-%d")
    serie_data_fim = request.args.get("serie_fim") or datetime.now().strftime("%Y-%m-%d")
    serie_labels, serie_agendados, serie_execucoes = calcular_serie_agendado_executado(
        conn, serie_data_inicio, serie_data_fim, operador_filtro
    )

    # Cards de projeção do mês corrente (pedido do Bruno, 2026-08-28), ao lado
    # do gráfico acima -- sempre sobre o mês REAL em curso (independe do
    # filtro do gráfico ou da data escolhida pra Produtividade), pessoal na
    # visão do operador / da equipe inteira na visão ADM-Supervisor. Cálculo
    # proporcional a dias úteis já decorridos vs. totais do mês: Agendados
    # considera Seg-Sex (time de agendamento não trabalha fim de semana),
    # Execuções considera Seg-Sáb (equipe de campo trabalha sábado também).
    mes_corrente_real = datetime.now().strftime("%Y-%m")
    _, _, agendados_mes_corrente, _ = calcular_produtividade(
        conn, "strftime('%Y-%m', data_fim) = ?", [mes_corrente_real], operador_filtro
    )
    execucoes_mes_corrente = calcular_execucoes_mes_operador(conn, mes_corrente_real, operador_filtro)
    projecao_agendados_mes = calcular_projecao_mes(agendados_mes_corrente, max_weekday=4)
    projecao_execucoes_mes = calcular_projecao_mes(execucoes_mes_corrente, max_weekday=5)

    # Indicador de quebra de agenda (pedido do Bruno, 2026-08-29) -- cards +
    # gráfico de linha logo abaixo do gráfico Agendados x Executados, com
    # granularidade ajustável (dia/mês/ano) e filtro de data próprio. Visão
    # pessoal do Operador traz só o resultado dele (cruzado por contrato
    # agendado por ele); visão de equipe traz Célula Local x Regional.
    quebra_granularidade = request.args.get("quebra_granularidade") or "dia"
    quebra_data_inicio = request.args.get("quebra_inicio") or datetime.now().replace(day=1).strftime("%Y-%m-%d")
    quebra_data_fim = request.args.get("quebra_fim") or datetime.now().strftime("%Y-%m-%d")
    quebra_indicador = calcular_indicador_quebra_totais(conn, quebra_data_inicio, quebra_data_fim, operador_filtro)
    quebra_serie = calcular_serie_quebra(conn, quebra_data_inicio, quebra_data_fim, quebra_granularidade, operador_filtro)

    meta_agendados_mes_exibida = percentual_meta_mes = percentual_meta_dia = meta_agendados_dia = None
    linha_meta = conn.execute("SELECT meta_agendados_mes FROM configuracoes WHERE id=1").fetchone()
    meta_agendados_mes_exibida = (linha_meta["meta_agendados_mes"] if linha_meta and linha_meta["meta_agendados_mes"] else 100)

    # Os gauges são sempre uma visão COLETIVA (objetivo do time inteiro),
    # mesmo na tela de quem só vê os próprios números no resto do Dashboard --
    # por isso recalcula sem o filtro por operador, não reaproveita
    # agendados_dia/agendados_mes (que no escopo pessoal são só da pessoa).
    if escopo_pessoal:
        _, _, agendados_dia_time, _ = calcular_produtividade(conn, "date(data_fim) = ?", [data_escolhida], None)
        _, _, agendados_mes_time, _ = calcular_produtividade(conn, "strftime('%Y-%m', data_fim) = ?", [mes_referencia], None)
    else:
        agendados_dia_time = agendados_dia
        agendados_mes_time = agendados_mes

    percentual_meta_mes = round(agendados_mes_time / meta_agendados_mes_exibida * 100) if meta_agendados_mes_exibida else 0

    inicio_mes_atual = (agora_dashboard := datetime.now()).replace(day=1)
    if agora_dashboard.month == 12:
        fim_mes_atual = agora_dashboard.replace(year=agora_dashboard.year + 1, month=1, day=1) - timedelta(days=1)
    else:
        fim_mes_atual = agora_dashboard.replace(month=agora_dashboard.month + 1, day=1) - timedelta(days=1)
    dias_uteis_mes_inteiro = dias_uteis_no_periodo(inicio_mes_atual.date(), fim_mes_atual.date())
    meta_agendados_dia = round(meta_agendados_mes_exibida / dias_uteis_mes_inteiro, 1) if dias_uteis_mes_inteiro else 0
    percentual_meta_dia = round(agendados_dia_time / meta_agendados_dia * 100) if meta_agendados_dia else 0

    # Velocímetros de Execuções (pedido do Bruno, 2026-08-28, mesmo padrão dos
    # de Agendados acima). Calculado sempre desde 2026-09-04 -- é resultado
    # coletivo do time, mas o Operador também pode ver no dashboard pessoal.
    linha_meta_execucoes = conn.execute("SELECT meta_execucoes_mes FROM configuracoes WHERE id=1").fetchone()
    meta_execucoes_mes = (linha_meta_execucoes["meta_execucoes_mes"] if linha_meta_execucoes and linha_meta_execucoes["meta_execucoes_mes"] else 100)
    meta_execucoes_dia = round(meta_execucoes_mes / dias_uteis_mes_inteiro, 1) if dias_uteis_mes_inteiro else 0
    percentual_meta_execucoes_dia = round(execucoes_ontem["total"] / meta_execucoes_dia * 100) if meta_execucoes_dia else 0
    percentual_meta_execucoes_mes = round(execucoes_mes["total"] / meta_execucoes_mes * 100) if meta_execucoes_mes else 0

    mapa_calor_cidades = None
    if not escopo_pessoal:
        fim_mapa = datetime.now().date()
        inicio_mapa = fim_mapa - timedelta(days=13)
        dias_mapa = [(inicio_mapa + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(14)]

        linhas_mapa = conn.execute("""
            SELECT m.cidade, date(a.data_fim) as dia, COUNT(*) as qtd
            FROM atividades a
            JOIN mailing m ON m.contrato = a.contrato
            WHERE date(a.data_fim) BETWEEN ? AND ? AND m.cidade IS NOT NULL AND m.cidade != ''
                  AND a.tabulacao = 'Agendado'
            GROUP BY m.cidade, dia
        """, (dias_mapa[0], dias_mapa[-1])).fetchall()

        valores_por_cidade = {}
        for linha in linhas_mapa:
            valores_por_cidade.setdefault(linha["cidade"], {})[linha["dia"]] = linha["qtd"]

        # Ordem INVERTIDA de propósito (2026-10-16): o heatmap do ApexCharts
        # desenha a primeira série embaixo -- com a lista em A->Z, a tela
        # mostrava Z no topo. Mandando Z->A, a tela lê de A (topo) a Z.
        mapa_calor_cidades = [
            {"name": cidade, "data": [{"x": dia[8:10] + "/" + dia[5:7], "y": valores.get(dia, 0)} for dia in dias_mapa]}
            for cidade, valores in sorted(valores_por_cidade.items(), reverse=True)
        ]

    # Cards da fila e funil do mês (pedido do Bruno, 2026-10-17).
    # "Outros" fecha a conta da base: em análise GPON, bloqueados no GED etc.
    outros_status = conn.execute(
        "SELECT COUNT(*) FROM mailing WHERE status NOT IN ('PENDENTE','EM_ANDAMENTO','ENFILEIRADO','CONCLUIDO')"
    ).fetchone()[0]
    # Em rota hoje: mesma regra do relatório Agendados por Data (última
    # tratativa do contrato = Agendado para hoje).
    em_rota_hoje = conn.execute("""
        SELECT COUNT(*) FROM atividades a
        WHERE a.id IN (SELECT MAX(id) FROM atividades GROUP BY contrato)
          AND a.tabulacao = 'Agendado' AND a.data_agendamento = ?
    """, (hoje_str_dashboard,)).fetchone()[0]
    conversao_exec_agend = round(execucoes_mes["total"] / agendados_mes_time * 100) if agendados_mes_time else None

    conn.close()
    return render_template(
        "Dashboard.html",
        outros_status=outros_status,
        em_rota_hoje=em_rota_hoje,
        hoje_str_dashboard=hoje_str_dashboard,
        conversao_exec_agend=conversao_exec_agend,
        pendentes=pendentes,
        andamento=andamento,
        enfileirados=enfileirados,
        concluidos=concluidos,
        data_escolhida=data_escolhida,
        mes_referencia=mes_referencia,
        motivos=motivos,
        total_motivos=total_motivos,
        motivos_mes=motivos_mes,
        total_motivos_mes=total_motivos_mes,
        finalizados_dia=finalizados_dia,
        agendados_dia=agendados_dia,
        conversao_dia=conversao_dia,
        finalizados_mes=finalizados_mes,
        agendados_mes=agendados_mes,
        conversao_mes=conversao_mes,
        meta_agendados_mes=meta_agendados_mes_exibida,
        meta_agendados_dia=meta_agendados_dia,
        percentual_meta_mes=percentual_meta_mes,
        percentual_meta_dia=percentual_meta_dia,
        agendados_dia_time=agendados_dia_time,
        agendados_mes_time=agendados_mes_time,
        mapa_calor_cidades=mapa_calor_cidades,
        por_pessoa=por_pessoa,
        meus_atendimentos=meus_atendimentos,
        escopo_pessoal=escopo_pessoal,
        grafico_dias=grafico_dias,
        grafico_escalados=grafico_escalados,
        grafico_presentes=grafico_presentes,
        grafico_ausentes=grafico_ausentes,
        grafico_absenteismo_dia=grafico_absenteismo_dia,
        absenteismo_periodo_pct=absenteismo_periodo_pct,
        grafico_data_inicio=grafico_data_inicio,
        grafico_data_fim=grafico_data_fim,
        hc_ativo=hc_ativo,
        envelhecimento=envelhecimento,
        execucoes_ontem=execucoes_ontem,
        execucoes_mes=execucoes_mes,
        ontem_str=ontem_str,
        agendados_ontem=agendados_ontem,
        meta_execucoes_mes=meta_execucoes_mes,
        meta_execucoes_dia=meta_execucoes_dia,
        percentual_meta_execucoes_dia=percentual_meta_execucoes_dia,
        percentual_meta_execucoes_mes=percentual_meta_execucoes_mes,
        serie_labels=serie_labels,
        serie_agendados=serie_agendados,
        serie_execucoes=serie_execucoes,
        serie_data_inicio=serie_data_inicio,
        serie_data_fim=serie_data_fim,
        projecao_agendados_mes=projecao_agendados_mes,
        projecao_execucoes_mes=projecao_execucoes_mes,
        mes_corrente_real=mes_corrente_real,
        quebra_indicador=quebra_indicador,
        quebra_serie=quebra_serie,
        quebra_granularidade=quebra_granularidade,
        quebra_data_inicio=quebra_data_inicio,
        quebra_data_fim=quebra_data_fim,
    )


@app.route("/dashboard/pessoa/<int:usuario_id>")
@login_required
@permissao_required("dashboard_equipe")  # mesma sub-permissão que libera a visão de equipe do Dashboard (2026-08-26) -- quem vê a tabela "Tratamento por pessoa" com o botão "Detalhes" consegue abrir o detalhe
def dashboard_pessoa(usuario_id):
    data_escolhida = request.args.get("data") or datetime.now().strftime("%Y-%m-%d")

    conn = conectar()
    usuario = conn.execute("SELECT * FROM usuarios WHERE id=?", (usuario_id,)).fetchone()
    if not usuario:
        conn.close()
        return "Usuário não encontrado.", 404

    atendimentos = conn.execute("""
        SELECT contrato, tabulacao, data_inicio, data_fim,
               COALESCE(minutos_efetivos, (julianday(data_fim) - julianday(data_inicio)) * 24 * 60) as minutos
        FROM atividades
        WHERE operador = ? AND date(data_fim) = ?
        ORDER BY data_fim
    """, (usuario["nome"], data_escolhida)).fetchall()
    conn.close()

    return render_template(
        "DashboardPessoa.html",
        usuario=usuario,
        data_escolhida=data_escolhida,
        atendimentos=atendimentos,
    )


@app.route("/cliente")
@login_required
@permissao_required("atendimento")
def cliente():
    usuario = usuario_atual()
    conn = conectar()
    if usuario["perfil"] == "OPERADOR":
        pausa = bloquear_por_pausa(conn, usuario["id"], datetime.now())
        if pausa:
            conn.close()
            return redirect("/pausas")

    cliente = buscar_proximo_cliente(usuario["id"], usuario["nome"])
    if not cliente:
        modo = obter_modo_trabalho(conn)
        conn.close()
        if modo == "RECEPTIVO":
            return redirect("/cliente/buscar")
        return "Sem clientes disponíveis no momento. Aguarde os retornos ou a carga de novo mailing."

    # Escopado por id_mailing (ou legado sem id_mailing) e ordenado por id
    # (insertion order real) em vez de data_inicio (que pode repetir entre
    # tratativas -- ver _garantir_coluna_id_mailing_atividades_followup e o
    # comentário em _mesclar_historico_com_ajustes).
    historico_atividades = conn.execute("""
        SELECT * FROM atividades
        WHERE (id_mailing=? OR (id_mailing IS NULL AND contrato=?))
        ORDER BY id DESC
    """, (cliente["id"], cliente["contrato"])).fetchall()
    ajustes_agendamento = conn.execute("""
        SELECT ag.* FROM ajustes_agendamento ag
        JOIN atividades a ON a.id = ag.atividade_id
        WHERE ag.contrato=? AND (a.id_mailing=? OR (a.id_mailing IS NULL AND a.contrato=?))
        ORDER BY ag.data_hora DESC
    """, (cliente["contrato"], cliente["id"], cliente["contrato"])).fetchall()
    historico = _mesclar_historico_com_ajustes(historico_atividades, ajustes_agendamento)
    scripts_atendimento = [dict(s) for s in conn.execute(
        "SELECT * FROM scripts_atendimento ORDER BY id"
    ).fetchall()]
    _abrir_visita_tela(conn, usuario["id"], cliente["contrato"])
    conn.close()

    texto_agendamento = session.pop("texto_agendamento_pendente", None)

    return render_template("Cliente.html", cliente=cliente, historico=historico,
                            texto_agendamento=texto_agendamento,
                            scripts_atendimento=scripts_atendimento)


@app.route("/cadastro")
@login_required
@perfil_required("SUPERVISOR", "ADM")
def cadastro():
    return render_template("Cadastro.html")


@app.route("/salvar_cadastro", methods=["POST"])
@login_required
@perfil_required("SUPERVISOR", "ADM")
def salvar_cadastro():
    nome = request.form.get("cliente")
    contrato = request.form.get("contrato")
    cidade = request.form.get("cidade")
    node = request.form.get("node")
    codigo_imovel = request.form.get("codigo_imovel")
    telefone_celular = request.form.get("telefone_celular")
    email = request.form.get("email")

    conn = conectar()
    agora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("""
        INSERT INTO mailing (cliente, contrato, cidade, node, codigo_imovel, telefone_celular, email, status, tentativa, data_entrada_fila)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDENTE', 1, ?)
    """, (nome, contrato, cidade, node, codigo_imovel, telefone_celular, email, agora_str))
    conn.commit()
    conn.close()

    return redirect("/")


def gerar_texto_agendamento(resp_cliente, data_agendamento, janela_agendamento, observacao, operador):
    """Texto padrão de ocorrência pós-agendamento, mesclando os dados do formulário
    com o modelo fixo definido pela operação."""
    try:
        data_formatada = datetime.strptime(data_agendamento, "%Y-%m-%d").strftime("%d/%m/%Y")
    except (ValueError, TypeError):
        data_formatada = data_agendamento or ""

    return (
        f"Realizado agendamento de Mudança de Pacote HFC X GPON.\n"
        f"Em contato com o Sr(a). {resp_cliente}, ficou agendado para {data_formatada}, "
        f"período {janela_agendamento}.\n"
        f"Observações: {observacao}\n"
        f"{operador} / Silmara CONTIERI / Esteira de Migração RSI"
    )


def _somar_intervalos_dentro_janela(intervalos, inicio_janela, fim_janela):
    """Soma em minutos o tempo coberto por uma lista de intervalos (inicio, fim
    -- datetimes; fim pode ser None quando o intervalo ainda está aberto/em
    curso), recortados pra dentro de [inicio_janela, fim_janela]. Funde
    intervalos sobrepostos antes de somar, pra nunca contar duas vezes uma
    sobreposição (ex: duas sessões que por algum motivo se cruzam)."""
    recortados = []
    for ini, fim in intervalos:
        if fim is None:
            fim = fim_janela  # ainda aberto (sem logout/fim_real) -- considera até o fim da janela
        ini_r = max(ini, inicio_janela)
        fim_r = min(fim, fim_janela)
        if fim_r > ini_r:
            recortados.append((ini_r, fim_r))
    if not recortados:
        return 0.0
    recortados.sort()
    fundidos = [recortados[0]]
    for ini, fim in recortados[1:]:
        ultimo_ini, ultimo_fim = fundidos[-1]
        if ini <= ultimo_fim:
            fundidos[-1] = (ultimo_ini, max(ultimo_fim, fim))
        else:
            fundidos.append((ini, fim))
    return sum((fim - ini).total_seconds() for ini, fim in fundidos) / 60


def calcular_minutos_efetivos(conn, operador_nome, inicio_str, fim_str):
    """"Tempo efetivo de tela" (pedido do Bruno, 2026-08-26): quanto tempo,
    dentro da janela [inicio_str, fim_str] de um atendimento, o operador
    esteve de fato logado E não em pausa -- ou seja, tempo de relógio menos
    pausa menos período deslogado. Calculado como:
        (tempo coberto por sessões de login dentro da janela)
      - (tempo coberto por pausas dentro da janela)
    Matematicamente seguro sem checar sobreposição entre os dois: uma pausa só
    existe enquanto a pessoa está logada (nunca é criada fora de uma sessão),
    então o tempo de pausa é sempre um subconjunto do tempo de sessão -- a
    subtração nunca "desconta" tempo que não tinha sido somado antes.
    Devolve None (não 0) quando não dá pra calcular com confiança -- usuário
    não encontrado pelo nome, datas inválidas, ou NENHUMA sessão cobrindo a
    janela (típico de dado histórico de antes dessa tabela existir, ou de
    atendimento avulso sem controle de ponto) -- quem chama cai de volta
    pro cálculo antigo (relógio corrido) nesse caso, pra nunca travar o fluxo
    nem gravar um "0 minutos" enganoso."""
    linha_usuario = conn.execute("SELECT id FROM usuarios WHERE nome=?", (operador_nome,)).fetchone()
    if not linha_usuario:
        return None
    usuario_id = linha_usuario["id"]
    try:
        inicio_dt = datetime.strptime(inicio_str, "%Y-%m-%d %H:%M:%S")
        fim_dt = datetime.strptime(fim_str, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    if fim_dt <= inicio_dt:
        return 0.0

    sessoes_rows = conn.execute("""
        SELECT login_em, logout_em FROM sessoes
        WHERE usuario_id=? AND login_em <= ? AND (logout_em IS NULL OR logout_em >= ?)
    """, (usuario_id, fim_str, inicio_str)).fetchall()
    intervalos_sessao = [
        (datetime.strptime(r["login_em"], "%Y-%m-%d %H:%M:%S"),
         datetime.strptime(r["logout_em"], "%Y-%m-%d %H:%M:%S") if r["logout_em"] else None)
        for r in sessoes_rows
    ]
    minutos_logado = _somar_intervalos_dentro_janela(intervalos_sessao, inicio_dt, fim_dt)
    if minutos_logado <= 0:
        return None  # sem sessão cobrindo a janela -- não dá pra calcular, cai pro cálculo antigo

    pausas_rows = conn.execute("""
        SELECT inicio, fim_real FROM pausas
        WHERE usuario_id=? AND inicio <= ? AND (fim_real IS NULL OR fim_real >= ?)
    """, (usuario_id, fim_str, inicio_str)).fetchall()
    intervalos_pausa = [
        (datetime.strptime(r["inicio"], "%Y-%m-%d %H:%M:%S"),
         datetime.strptime(r["fim_real"], "%Y-%m-%d %H:%M:%S") if r["fim_real"] else None)
        for r in pausas_rows
    ]
    minutos_pausa = _somar_intervalos_dentro_janela(intervalos_pausa, inicio_dt, fim_dt)

    return max(minutos_logado - minutos_pausa, 0.0)


# ==========================
# VISITAS DE TELA (tempo efetivo de tela, versão precisa) -- 2026-08-26
# ==========================
# A pedido do Bruno: em vez de só inferir o tempo efetivo por sessão de
# login/pausa (que pode "contar de novo" um período que já tinha sido tempo
# de trabalho de uma tratativa anterior do mesmo contrato -- ver o achado do
# "3 min vira 6 min" quando um follow-up é retratado por Buscar Contrato),
# a tela de Atendimento e a de Buscar Contrato agora abrem uma "visita"
# (entrada=agora) quando são abertas pra um contrato, e essa visita é fechada
# (saida=agora) na primeira saída EFETIVA que o sistema perceber: salvar a
# tabulação, entrar em pausa, ou navegar pra qualquer outro menu do sistema.
# O tempo efetivo de uma tabulação passa a ser a SOMA das visitas fechadas
# daquele contrato desde a tabulação anterior (ou desde a distribuição, se
# for a primeira) -- medindo presença na tela de verdade, não mais inferida.
#
# Saídas incertas (fechar a aba, cair a internet, o computador hibernar) não
# são tratadas de forma especial, a pedido do Bruno -- a visita fica em
# aberto até a próxima coisa que o sistema perceber (mesmo que demore); se
# nunca mais acontecer nada, ela nunca fecha e nunca entra em soma nenhuma
# (só visita com 'saida' preenchida conta pro tempo efetivo).
#
# Cobre só as telas de Atendimento e Buscar Contrato (as que o Bruno pediu).
# A fila de GPON Indisponível continua no cálculo por sessão/pausa
# (calcular_minutos_efetivos, acima) -- não foi pedido e é uma tela à parte.

def _garantir_tabela_visitas_tela():
    """Auto-migração (mesmo padrão de sempre -- CREATE TABLE IF NOT EXISTS,
    roda 1x a cada subida, não precisa de script nem console)."""
    conn = conectar()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS visitas_tela (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario_id INTEGER NOT NULL,
            contrato TEXT NOT NULL,
            entrada TEXT NOT NULL,
            saida TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_visitas_tela_usuario_aberta ON visitas_tela(usuario_id, saida)")
    conn.commit()
    conn.close()


_garantir_tabela_visitas_tela()


def _fechar_visita_tela_aberta(conn, usuario_id, agora_str=None):
    """Fecha (saida=agora) a visita de tela em aberto do usuário, se existir.
    Idempotente -- não faz nada se não houver nenhuma aberta agora."""
    agora_str = agora_str or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "UPDATE visitas_tela SET saida=? WHERE usuario_id=? AND saida IS NULL",
        (agora_str, usuario_id)
    )


def _abrir_visita_tela(conn, usuario_id, contrato):
    """Abre uma visita de tela pro contrato indicado, chamada quando a tela
    de Atendimento ou Buscar Contrato é aberta. Se já havia uma visita aberta
    pro MESMO contrato, não faz nada (evita abrir de novo só por causa de um
    F5/reload da mesma tela). Se havia uma aberta pra um contrato DIFERENTE,
    fecha ela antes (a pessoa saiu daquele e entrou nesse -- cobre trocar de
    contrato sem passar pelo fechamento explícito de outra rota)."""
    agora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    aberta = conn.execute(
        "SELECT id, contrato FROM visitas_tela WHERE usuario_id=? AND saida IS NULL",
        (usuario_id,)
    ).fetchone()
    if aberta and aberta["contrato"] == contrato:
        return  # já está com essa mesma tela aberta -- não duplica
    if aberta:
        conn.execute("UPDATE visitas_tela SET saida=? WHERE id=?", (agora_str, aberta["id"]))
    conn.execute(
        "INSERT INTO visitas_tela (usuario_id, contrato, entrada) VALUES (?, ?, ?)",
        (usuario_id, contrato, agora_str)
    )
    conn.commit()


# Rotas que ABREM/mantêm a visita de tela sozinhas (dentro da própria rota,
# porque só ali dá pra saber qual contrato está sendo aberto) -- todo o
# resto conta como "saída efetiva" e fecha qualquer visita em aberto, sem
# precisar instrumentar rota por rota.
def _rota_abre_visita_tela(caminho):
    if caminho == "/cliente":
        return True
    if caminho.startswith("/atendimento/") and not caminho.endswith("/tratar"):
        return True  # GET /atendimento/<id> (abrir Buscar Contrato) -- a versão
        # POST .../tratar (Salvar) É uma saída efetiva, por isso a exclusão acima
    return False


@app.before_request
def _fechar_visita_tela_ao_sair():
    """Fecha a visita de tela em aberto (se houver) em QUALQUER requisição
    que não seja abrir a própria tela de Atendimento/Buscar Contrato nem um
    arquivo estático -- cobre salvar a tabulação, entrar em pausa e navegar
    pra qualquer outro menu, tudo de uma vez, sem precisar mexer em cada rota
    uma por uma. Não falha o request se der algum problema aqui (best-effort:
    contagem de tempo não pode nunca travar o uso do sistema)."""
    if request.path.startswith("/static/"):
        return
    usuario_id = session.get("usuario_id")
    if not usuario_id:
        return
    if _rota_abre_visita_tela(request.path):
        return
    try:
        conn = conectar()
        _fechar_visita_tela_aberta(conn, usuario_id)
        conn.commit()
        conn.close()
    except Exception:
        pass


def _fim_ultima_atividade(conn, contrato):
    """data_fim da última tabulação já registrada pra esse contrato (antes da
    que está sendo gravada agora), ou None se essa for a primeira. Usado como
    início da janela de cálculo do tempo efetivo -- pra cada tratativa medir
    só o tempo desde que ELA MESMA começou, nunca reincluindo o período de
    uma tratativa anterior (o mesmo achado do "3 min vira 6 min" quando um
    follow-up é retratado por Buscar Contrato, sem passar pela fila)."""
    linha = conn.execute(
        "SELECT data_fim FROM atividades WHERE contrato=? ORDER BY id DESC LIMIT 1",
        (contrato,)
    ).fetchone()
    return linha["data_fim"] if linha and linha["data_fim"] else None


def calcular_minutos_por_visitas_tela(conn, contrato, desde_str, ate_str):
    """Soma (em minutos) das visitas de tela FECHADAS desse contrato dentro
    da janela [desde_str, ate_str] -- fonte de verdade nova e mais precisa do
    tempo efetivo, pras tabulações feitas por Atendimento/Buscar Contrato.
    Devolve None quando não há nenhuma visita fechada cobrindo a janela
    (contrato tratado antes dessa migração, tabulação feita por um caminho
    não instrumentado como GPON, ou visita ainda em aberto por uma saída
    incerta) -- quem chama cai pro cálculo por sessão/pausa nesse caso."""
    try:
        inicio_dt = datetime.strptime(desde_str, "%Y-%m-%d %H:%M:%S")
        fim_dt = datetime.strptime(ate_str, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    if fim_dt <= inicio_dt:
        return 0.0
    linhas = conn.execute("""
        SELECT entrada, saida FROM visitas_tela
        WHERE contrato=? AND saida IS NOT NULL AND entrada <= ? AND saida >= ?
    """, (contrato, ate_str, desde_str)).fetchall()
    if not linhas:
        return None
    intervalos = [
        (datetime.strptime(l["entrada"], "%Y-%m-%d %H:%M:%S"), datetime.strptime(l["saida"], "%Y-%m-%d %H:%M:%S"))
        for l in linhas
    ]
    return _somar_intervalos_dentro_janela(intervalos, inicio_dt, fim_dt)


# ==========================
# REMARCAÇÃO DE AGENDAMENTO JÁ FEITO (Bruno, 2026-08-27)
# ==========================
# Achado: reenviar "Agendado" com data/janela novas pra um contrato que já
# está CONCLUIDO/Agendado (o jeito que a própria tela de Buscar Contrato
# instrui pra remarcar) inseria uma tratativa NOVA em `atividades` -- o que
# duplicava a contagem de "Agendados" no Dashboard/Qualidade/Telegram e ainda
# atribuía o crédito ao operador/dia de quem fez o AJUSTE, não de quem
# realmente agendou (e na data em que o ajuste foi feito, não a data original
# -- ou pior, na data velha da distribuição original do contrato, corrompendo
# retroativamente o relatório daquele dia).
#
# Corrigido separando os dois casos: se o contrato já está CONCLUIDO com a
# ÚLTIMA tabulação sendo "Agendado" e chega um novo "Agendado", é só uma
# REMARCAÇÃO (cliente ligou pedindo pra mudar data/janela) -- não conta como
# atendimento nem agendamento novo em nenhuma métrica de produtividade: só
# atualiza os campos de data/janela/responsável na própria linha já existente
# (preservando operador/data originais, que são a fonte de verdade das
# métricas) e guarda o antes/depois numa tabela separada
# (`ajustes_agendamento`), só pra ficar visível no histórico da tela de
# Atendimento -- com transparência de que houve um ajuste, sem contaminar
# nenhuma contagem. Continua contando normal (tratativa nova) qualquer outra
# situação -- inclusive um "Agendado" genuíno, a primeira vez que o contrato
# recebe essa tabulação.

def _garantir_tabela_ajustes_agendamento():
    """Auto-migração (mesmo padrão de sempre -- CREATE TABLE IF NOT EXISTS,
    roda 1x a cada subida, não precisa de script nem console)."""
    conn = conectar()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ajustes_agendamento (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            atividade_id INTEGER NOT NULL,
            contrato TEXT NOT NULL,
            operador TEXT NOT NULL,
            data_hora TEXT NOT NULL,
            data_agendamento_anterior TEXT,
            janela_anterior TEXT,
            resp_cliente_anterior TEXT,
            data_agendamento_nova TEXT,
            janela_nova TEXT,
            resp_cliente_novo TEXT,
            observacao TEXT
        )
    """)
    conn.commit()
    conn.close()


_garantir_tabela_ajustes_agendamento()


def _eh_apenas_remarcacao_de_agendamento(conn, id_cliente, contrato, resultado):
    """True quando esse 'Agendado' que está chegando é só um AJUSTE de um
    agendamento que já existe (contrato já CONCLUIDO, última tabulação já era
    Agendado) -- não um agendamento genuíno novo. Devolve também a linha de
    `atividades` que deve ser atualizada em vez de uma nova ser inserida."""
    if resultado != "Agendado":
        return False, None
    linha_mailing = conn.execute("SELECT status FROM mailing WHERE id=?", (id_cliente,)).fetchone()
    if not linha_mailing or linha_mailing["status"] != "CONCLUIDO":
        return False, None
    # Escopado por id_mailing (ou legado sem id_mailing) -- senão a "última
    # atividade" podia vir de OUTRA linha de mailing com o mesmo número de
    # contrato em cidade diferente (pedido do Bruno, 2026-09-20).
    ultima_atividade = conn.execute(
        "SELECT * FROM atividades WHERE (id_mailing=? OR (id_mailing IS NULL AND contrato=?)) ORDER BY id DESC LIMIT 1",
        (id_cliente, contrato)
    ).fetchone()
    if ultima_atividade is None or ultima_atividade["tabulacao"] != "Agendado":
        return False, None
    return True, ultima_atividade


def _registrar_remarcacao_de_agendamento(conn, atividade_existente, id_cliente, operador_logado, observacao):
    """Aplica só o ajuste de data/janela/responsável na linha de 'Agendado' já
    existente (sem inserir uma tratativa nova) e guarda o antes/depois em
    `ajustes_agendamento` pra ficar visível no histórico da tela de
    Atendimento -- sem contar como agendamento/atendimento novo em nenhuma
    métrica (Dashboard, Qualidade, Telegram, Extração), já que a linha
    contada continua a mesma de sempre, só com os campos operacionais
    (data/janela/responsável) atualizados."""
    agora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    resp_cliente_novo = request.form.get('resp_cliente', '')
    data_agendamento_novo = request.form.get('data_agendamento', '')
    janela_agendamento_novo = request.form.get('janela_agendamento', '')
    flag_mdu_novo = 1 if request.form.get('flag_mdu') else 0

    conn.execute("""
        UPDATE atividades SET data_agendamento=?, janela_agendamento=?, resp_cliente=?, flag_mdu=?
        WHERE id=?
    """, (data_agendamento_novo, janela_agendamento_novo, resp_cliente_novo, flag_mdu_novo,
          atividade_existente["id"]))

    conn.execute("""
        INSERT INTO ajustes_agendamento (atividade_id, contrato, operador, data_hora,
            data_agendamento_anterior, janela_anterior, resp_cliente_anterior,
            data_agendamento_nova, janela_nova, resp_cliente_novo, observacao)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (atividade_existente["id"], atividade_existente["contrato"], operador_logado, agora_str,
          atividade_existente["data_agendamento"], atividade_existente["janela_agendamento"],
          atividade_existente["resp_cliente"],
          data_agendamento_novo, janela_agendamento_novo, resp_cliente_novo, observacao))

    # Só quem tocou no contrato por último muda -- status/tentativa continuam
    # intocados (segue CONCLUIDO, como já estava antes do ajuste).
    conn.execute("UPDATE mailing SET operador=? WHERE id=?", (operador_logado, id_cliente))
    conn.commit()


def _mesclar_historico_com_ajustes(atividades_rows, ajustes_rows):
    """Junta as tratativas normais (`atividades`) com os ajustes de
    agendamento (`ajustes_agendamento`, remarcações que não contam como
    atendimento novo) numa lista só, ordenada por data -- pra aparecer tudo
    junto na linha do tempo da tela de Atendimento, com transparência de que
    houve uma remarcação, sem misturar os dois tipos em nenhuma métrica de
    produtividade (essa mistura acontece só aqui, na exibição).

    Ordena por data_fim (quando a tratativa realmente TERMINOU), não por
    data_inicio: data_inicio vem de mailing.data_distribuicao, que só
    atualiza numa distribuição de verdade pela fila -- retratar um contrato
    já CONCLUIDO pela tela Buscar Contrato não passa por ali, então várias
    tratativas seguidas nascem com o mesmo data_inicio (às vezes idêntico a
    uma tratativa bem mais antiga) e a ordenação por data_inicio embaralhava
    a ordem real (bug real encontrado pelo Bruno, 2026-09-20, contrato
    8183133 -- duas tratativas feitas hoje apareciam antes de uma de
    9 dias atrás, empatadas na mesma data_inicio). data_fim é sempre o
    momento exato em que aquela tratativa específica foi salva -- não
    repete."""
    linhas = []
    for a in atividades_rows:
        linhas.append({
            "data_ordenacao": a["data_fim"] or a["data_inicio"] or "",
            "data_inicio": a["data_inicio"],
            "operador": a["operador"],
            "tabulacao": a["tabulacao"],
            "observacao": a["observacao"],
        })
    for aj in ajustes_rows:
        texto = (f"Agendamento remarcado: {aj['data_agendamento_anterior']} ({aj['janela_anterior']}) "
                 f"→ {aj['data_agendamento_nova']} ({aj['janela_nova']})")
        if aj["observacao"]:
            texto += f" | Obs: {aj['observacao']}"
        linhas.append({
            "data_ordenacao": aj["data_hora"] or "",
            "data_inicio": aj["data_hora"],
            "operador": aj["operador"],
            "tabulacao": "Ajuste de Agendamento",
            "observacao": texto,
        })
    linhas.sort(key=lambda x: x["data_ordenacao"], reverse=True)
    return linhas


def registrar_tratamento(conn, id_cliente, contrato, tentativa_atual, operador_logado, resultado, observacao):
    """Grava uma tabulação (fechamento ou follow-up) pro contrato indicado.
    Compartilhada entre o fluxo normal (/salvar) e a busca de contrato (/atendimento/<id>/tratar)."""
    # Trava de segurança (a tela já desabilita essa opção antes da hora, isso aqui
    # é só reforço contra alguém tentando forçar via requisição direta)
    if resultado == "Improdutivo" and tentativa_atual != 3:
        return

    # Remarcação de um agendamento já feito (ver seção "REMARCAÇÃO DE
    # AGENDAMENTO JÁ FEITO" acima) -- não é uma tratativa nova, é só um
    # ajuste; trata à parte e sai, sem inserir linha nova em `atividades`.
    eh_remarcacao, atividade_existente = _eh_apenas_remarcacao_de_agendamento(conn, id_cliente, contrato, resultado)
    if eh_remarcacao:
        _registrar_remarcacao_de_agendamento(conn, atividade_existente, id_cliente, operador_logado, observacao)
        return

    agora_dt = datetime.now()
    agora_str = agora_dt.strftime("%Y-%m-%d %H:%M:%S")

    # Momento em que esse atendimento realmente começou (quando o contrato foi aberto na tela),
    # usado pra medir o tempo de tratamento no Dashboard. Se não tiver (ex: contrato PENDENTE
    # tratado direto pela busca, sem passar pela fila), usa agora mesmo como início.
    linha_mailing = conn.execute("SELECT data_distribuicao FROM mailing WHERE id=?", (id_cliente,)).fetchone()
    inicio_atendimento = (linha_mailing["data_distribuicao"] if linha_mailing and linha_mailing["data_distribuicao"] else agora_str)

    status_mailing = 'CONCLUIDO'
    data_retorno = None
    tipo_followup = None

    detalhes = []
    data_agendamento_col = None
    janela_agendamento_col = None
    resp_cliente_col = None
    flag_mdu_col = None
    if resultado == "Agendado":
        resp_cliente_col = request.form.get('resp_cliente', '')
        data_agendamento_col = request.form.get('data_agendamento', '')
        janela_agendamento_col = request.form.get('janela_agendamento', '')
        flag_mdu_col = 1 if request.form.get('flag_mdu') else 0
        # Trava de segurança (o calendário na tela já bloqueia data passada
        # visualmente, isso aqui é só reforço contra alguém tentando forçar
        # via requisição direta) -- pedido do Bruno, 2026-09-11.
        if data_agendamento_col and data_agendamento_col < agora_dt.strftime("%Y-%m-%d"):
            return
        detalhes.append(f"Responsável: {resp_cliente_col}")
        detalhes.append(f"Data: {data_agendamento_col}")
        detalhes.append(f"Janela: {janela_agendamento_col}")
        detalhes.append(f"MDU: {'Sim' if flag_mdu_col else 'Não'}")
    elif resultado == "Improdutivo":
        detalhes.append(f"Email enviado: {'Sim' if request.form.get('flag_email') else 'Não'}")

    observacao_completa = " | ".join(detalhes)
    if observacao:
        observacao_completa = f"{observacao_completa} | Obs: {observacao}" if observacao_completa else observacao

    if resultado in ["2ª Tentativa", "3ª Tentativa"]:
        status_mailing = 'ENFILEIRADO'
        prazo_horas = obter_prazo_followup(conn)
        data_retorno = (agora_dt + timedelta(hours=prazo_horas)).strftime("%Y-%m-%d %H:%M:%S")
        tipo_followup = 'FOLLOWUP'
        tentativa_atual += 1
    elif resultado == "Retorno":
        status_mailing = 'ENFILEIRADO'
        data_ret = request.form.get("retorno_data")
        hora_ret = request.form.get("retorno_hora")
        data_retorno = f"{data_ret} {hora_ret}:00"
        tipo_followup = 'RETORNO'
    elif resultado == "GPON Indisponível":
        status_mailing = 'AGUARDANDO_ANALISE_GPON'

    if resultado == "GPON Indisponível" and 'evidencia' in request.files:
        file = request.files['evidencia']
        if file.filename != '':
            filename = secure_filename(f"{id_cliente}_{file.filename}")
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
            extra = f"Evidência: {filename}"
            observacao_completa = f"{observacao_completa} | {extra}" if observacao_completa else extra

    # Qualquer followup pendente antigo desse contrato (de uma tabulação anterior,
    # ex: 2ª/3ª Tentativa ou Retorno que ainda não tinha vencido) fica obsoleto
    # assim que uma nova tabulação é registrada agora -- marca como tratado pra
    # nunca mais "ressuscitar" esse contrato na fila depois. Reforça, não
    # substitui, a trava por m.status='ENFILEIRADO' em buscar_proximo_cliente.
    # Escopado por id_mailing quando dá (id_mailing=id_cliente OU linha antiga
    # sem id_mailing) -- pra não marcar como tratado o followup de OUTRA linha
    # de mailing com o mesmo número de contrato em cidade diferente (pedido do
    # Bruno, 2026-09-20 -- ver _garantir_coluna_id_mailing_atividades_followup).
    conn.execute("""
        UPDATE followup SET status='TRATADO'
        WHERE contrato=? AND status='PENDENTE' AND (id_mailing=? OR id_mailing IS NULL)
    """, (contrato, id_cliente))

    # "Tempo efetivo de tela" (pedido do Bruno, 2026-08-26): a janela de
    # cálculo é desde o FIM da tratativa anterior desse contrato (não desde a
    # distribuição original) -- pra nunca reincluir o período de uma
    # tratativa já contada antes, no caso de o follow-up ser retratado por
    # Buscar Contrato em vez de voltar pela fila. Prioridade: (1) soma das
    # visitas de tela fechadas nessa janela (mais preciso, mede presença real
    # na tela); (2) se não achar nenhuma visita, cai pro cálculo por
    # sessão/pausa de sempre; (3) se nenhum dos dois der pra calcular, fica
    # NULL e o Dashboard/Qualidade caem no cálculo antigo (relógio corrido)
    # só nessa linha.
    inicio_janela_efetivo = _fim_ultima_atividade(conn, contrato) or inicio_atendimento
    minutos_efetivos = calcular_minutos_por_visitas_tela(conn, contrato, inicio_janela_efetivo, agora_str)
    if minutos_efetivos is None:
        minutos_efetivos = calcular_minutos_efetivos(conn, operador_logado, inicio_janela_efetivo, agora_str)

    cursor = conn.execute("""
        INSERT INTO atividades (contrato, id_mailing, operador, tentativa, tabulacao, canal_contato, observacao, data_inicio, data_fim, status,
                                 data_agendamento, janela_agendamento, resp_cliente, flag_mdu, minutos_efetivos)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (contrato, id_cliente, operador_logado, tentativa_atual, resultado, None, observacao_completa,
          inicio_atendimento, agora_str, status_mailing,
          data_agendamento_col, janela_agendamento_col, resp_cliente_col, flag_mdu_col, minutos_efetivos))
    id_atividade = cursor.lastrowid

    if resultado == "Improdutivo":
        conn.execute("""
            INSERT INTO checklist_improdutivo (id_atividade, celular, residencial, comercial, email)
            VALUES (?, 0, 0, 0, ?)
        """, (id_atividade, 1 if request.form.get('flag_email') else 0))

    if data_retorno:
        conn.execute("""
            INSERT INTO followup (contrato, id_mailing, operador, data_retorno, observacao, status, tipo)
            VALUES (?, ?, ?, ?, ?, 'PENDENTE', ?)
        """, (contrato, id_cliente, operador_logado, data_retorno, observacao, tipo_followup))

    conn.execute("""
        UPDATE mailing SET status=?, tentativa=?, operador=? WHERE id=?
    """, (status_mailing, tentativa_atual, operador_logado, id_cliente))

    # Marca o momento exato em que entrou na fila do GPON -- usado só pra
    # ordenar a tabela de tratamento por ordem de chegada (mais antigo
    # primeiro). Separado do UPDATE acima de propósito, pra não mexer no
    # comportamento de data_entrada_fila das outras tabulações (pedido do
    # Bruno, 2026-09-04).
    if resultado == "GPON Indisponível":
        conn.execute("UPDATE mailing SET data_entrada_fila=? WHERE id=?", (agora_str, id_cliente))

    conn.commit()


# ==========================
# RELATÓRIO DE AGENDADOS POR DATA
# ==========================

@app.route("/relatorios/agendados")
@login_required
@permissao_required("agendados_data")
def relatorio_agendados():
    data_escolhida = request.args.get("data") or (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    conn = conectar()
    resultados = conn.execute("""
        SELECT a.id AS atividade_id, m.cidade, a.contrato, a.data_agendamento, a.janela_agendamento,
               a.operador AS tratado_por, a.observacao
        FROM atividades a
        JOIN mailing m ON m.contrato = a.contrato
        WHERE a.id IN (
            SELECT MAX(id) FROM atividades GROUP BY contrato
        )
        AND a.tabulacao = 'Agendado'
        AND a.data_agendamento = ?
        ORDER BY a.janela_agendamento, a.contrato
    """, (data_escolhida,)).fetchall()
    # Linhas cujo último ajuste veio de fora do CRM (sincronização com a
    # Confirmação de Agenda, 2026-10-16) ganham a marcação "Reagendado fora do CRM".
    from sincroniza_agenda import ids_reagendados_externamente
    reagendados_fora = ids_reagendados_externamente(conn, [r["atividade_id"] for r in resultados])
    conn.close()

    return render_template("RelatorioAgendados.html", data_escolhida=data_escolhida, resultados=resultados,
                           reagendados_fora=reagendados_fora)


# ==========================
# AGENDADO x EXECUTADO -- tela em /atendimento/agendado_executado, pedida
# pelo Bruno em 2026-08-28: acompanhamento pro Operador (só o próprio, filtro
# de data, padrão mês corrente) e pro Supervisor (equipe inteira, com filtro
# de operador). Mesmo pareamento contrato a contrato já usado no Relatório de
# Agendados (execucoes_os por contrato, primeira baixa a partir da data
# agendada), só que numa janela de datas em vez de um dia só, e paginado
# (mesmo padrão de Histórico de Tratamento, ver seção de paginação acima).
# ==========================

@app.route("/atendimento/agendado_executado")
@login_required
@permissao_required("agendado_executado")
def agendado_executado():
    usuario = usuario_atual()
    agora_tela = datetime.now()
    data_de = request.args.get("data_de") or agora_tela.replace(day=1).strftime("%Y-%m-%d")
    data_ate = request.args.get("data_ate") or agora_tela.strftime("%Y-%m-%d")

    # Operador só acompanha o próprio -- ignora qualquer "operador" que venha
    # na querystring (defesa contra URL manipulada). Quem tem visão de
    # equipe (Supervisor, e ADM por bypass) escolhe livremente, "Todos" por padrão.
    eh_operador = usuario["perfil"] == "OPERADOR"
    operador_filtro = usuario["nome"] if eh_operador else (request.args.get("operador") or "")

    conn = conectar()
    operadores = []
    if not eh_operador:
        operadores = [l["operador"] for l in conn.execute("""
            SELECT DISTINCT operador FROM atividades
            WHERE tabulacao='Agendado' AND operador IS NOT NULL AND operador != '' ORDER BY operador
        """).fetchall()]

    # O MAX(id) que dedupe "qual Agendado vale pra esse contrato" precisa ser
    # calculado DENTRO do próprio período filtrado, não globalmente -- do
    # contrário um contrato agendado dentro do período mas remarcado de novo
    # DEPOIS (pra uma data fora da janela, ex: mês seguinte) desaparecia da
    # contagem inteira, porque o Agendado "vigente" (o mais recente de todos
    # os tempos) já não caía mais no filtro de data. Bug real encontrado pelo
    # Bruno em 2026-08-28 (tela mostrando 11 de 35 agendados no mês).
    #
    # Mas isso sozinho reabria outro problema (achado pelo Bruno em
    # 2026-09-10): se o contrato foi agendado dentro do período e DEPOIS
    # (id maior, tabulação diferente) foi cancelado ou alterado pra outro
    # fechamento qualquer, esse "Agendado" antigo continuava contando --
    # porque o subquery só olha dentro de tabulacao='Agendado', nunca
    # verifica se algo mais novo (de qualquer tipo) veio por cima depois.
    #
    # A correção certa precisa distinguir duas coisas que parecem iguais
    # mas não são: REMARCAÇÃO (uma tabulação Agendado mais nova, só que
    # com outra data -- ainda é um compromisso válido, só que pra outro
    # período; é exatamente o caso que o fix de 28/08 queria preservar) x
    # CANCELAMENTO (uma tabulação mais nova de QUALQUER outro tipo -- não
    # tem mais agendamento nenhum valendo). A condição abaixo só exclui no
    # segundo caso: exclui se existir algo mais novo pro mesmo contrato que
    # NÃO seja Agendado; se o mais novo também for Agendado (remarcação),
    # continua contando normalmente no período original.
    sub_max_id = "SELECT MAX(id) FROM atividades WHERE tabulacao='Agendado' AND data_agendamento BETWEEN ? AND ?"
    parametros_sub = [data_de, data_ate]
    if operador_filtro:
        sub_max_id += " AND operador = ?"
        parametros_sub.append(operador_filtro)
    sub_max_id += " GROUP BY contrato"

    filtro_sql = f"""
        WHERE a.id IN ({sub_max_id})
          AND a.data_agendamento BETWEEN ? AND ?
          AND NOT EXISTS (
              SELECT 1 FROM atividades a3
              WHERE a3.contrato = a.contrato AND a3.id > a.id AND a3.tabulacao != 'Agendado'
          )
    """
    parametros = parametros_sub + [data_de, data_ate]
    if operador_filtro:
        filtro_sql += " AND a.operador = ?"
        parametros.append(operador_filtro)

    total = conn.execute(f"""
        SELECT COUNT(*) c FROM atividades a JOIN mailing m ON m.contrato = a.contrato {filtro_sql}
    """, parametros).fetchone()["c"]
    total_executados = conn.execute(f"""
        SELECT COUNT(*) c FROM atividades a JOIN mailing m ON m.contrato = a.contrato {filtro_sql}
        AND EXISTS (SELECT 1 FROM execucoes_os e WHERE e.contrato = a.contrato AND e.dh_baixa >= a.data_agendamento)
    """, parametros).fetchone()["c"]

    pagina, total_paginas, offset = _paginar(request.args, total)

    linhas = conn.execute(f"""
        SELECT m.cidade, a.contrato, a.operador AS tratado_por, a.data_agendamento, a.janela_agendamento,
               (SELECT MIN(e.dh_baixa) FROM execucoes_os e
                WHERE e.contrato = a.contrato AND e.dh_baixa >= a.data_agendamento) AS dh_execucao,
               (SELECT qa.motivo_reagendamento FROM quebras_agenda qa
                WHERE qa.contrato = a.contrato AND qa.dat_agenda <= a.data_agendamento
                ORDER BY qa.id DESC LIMIT 1) AS quebra_motivo,
               (SELECT qt.tabulacao FROM quebras_agenda qa
                JOIN quebras_tratamento qt ON qt.quebra_id = qa.id
                WHERE qa.contrato = a.contrato AND qa.dat_agenda <= a.data_agendamento
                ORDER BY qt.id DESC LIMIT 1) AS quebra_tabulacao
        FROM atividades a
        JOIN mailing m ON m.contrato = a.contrato
        {filtro_sql}
        ORDER BY a.data_agendamento DESC, a.contrato
        LIMIT ? OFFSET ?
    """, parametros + [POR_PAGINA_PADRAO, offset]).fetchall()
    conn.close()

    def status_visual(l):
        """Combina execução + quebra de agenda numa categoria visual só --
        pedido do Bruno, 2026-09-11: "Ainda não" virou "Em Rota", e agora
        cruza com o tratamento de quebra. Em Rota (cinza) = sem quebra
        registrada OU quebra reagendada (voltou pra rota); Parado na Quebra
        (amarelo claro) = quebra existe mas ainda não foi tratada;
        Cancelado na Quebra (vermelho suave) = quebra tratada com
        resultado diferente de Reagendado; Executado (verde) = já tem OS
        de execução batida."""
        # "classe" (2026-10-16): a tela pinta a linha por classe CSS, com
        # variante própria pro modo escuro -- antes as cores vinham inline e,
        # no escuro, o texto claro do tema ficava em cima do fundo pastel.
        if l["dh_execucao"]:
            return {"rotulo": "Executado", "classe": "executado", "cor": "#D4EDDA", "cor_texto": "#155724"}
        if not l["quebra_motivo"]:
            return {"rotulo": "Em Rota", "classe": "em-rota", "cor": "#E9ECEF", "cor_texto": "#495057"}
        if not l["quebra_tabulacao"]:
            return {"rotulo": "Parado na Quebra", "classe": "parado", "cor": "#FFF3CD", "cor_texto": "#856404", "detalhe": l["quebra_motivo"]}
        if l["quebra_tabulacao"] == "Reagendado":
            return {"rotulo": "Em Rota", "classe": "em-rota", "cor": "#E9ECEF", "cor_texto": "#495057", "detalhe": "Reagendado após quebra"}
        return {"rotulo": "Cancelado na Quebra", "classe": "cancelado", "cor": "#F8D7DA", "cor_texto": "#842029", "detalhe": l["quebra_tabulacao"]}

    registros = [{
        "cidade": l["cidade"] or "—",
        "contrato": l["contrato"],
        "operador": l["tratado_por"],
        "data_agendamento": l["data_agendamento"],
        "janela_agendamento": l["janela_agendamento"],
        "dh_execucao": l["dh_execucao"],
        "categoria": "executado" if l["dh_execucao"] else "pendente",
        "visual": status_visual(l),
    } for l in linhas]

    return render_template(
        "AgendadoExecutado.html",
        registros=registros, total=total,
        total_executados=total_executados, total_pendentes=total - total_executados,
        pct_executado=round(total_executados / total * 100) if total else None,
        operadores=operadores, eh_operador=eh_operador,
        data_de=data_de, data_ate=data_ate, operador_filtro=operador_filtro,
        pagina=pagina, total_paginas=total_paginas, paginas_exibir=_paginas_para_exibir(pagina, total_paginas),
        querystring_sem_pagina=_querystring_sem_pagina(request.args),
    )


@app.route("/salvar", methods=["POST"])
@login_required
@permissao_required("atendimento")
def salvar():
    usuario = usuario_atual()
    operador_logado = usuario["nome"]

    id_cliente = request.form.get("id_cliente")
    resultado = request.form.get("resultado")
    observacao = request.form.get("observacao") or ""

    conn = conectar()
    cliente_atual = conn.execute(
        "SELECT contrato, tentativa, operador, status FROM mailing WHERE id=?", (id_cliente,)
    ).fetchone()

    if not cliente_atual or cliente_atual["operador"] != operador_logado or cliente_atual["status"] != "EM_ANDAMENTO":
        conn.close()
        return redirect("/cliente")  # contrato não é mais desse operador (expirou/foi tratado em outra aba)

    contrato = cliente_atual["contrato"]
    tentativa_atual = cliente_atual["tentativa"] or 1

    registrar_tratamento(conn, id_cliente, contrato, tentativa_atual, operador_logado, resultado, observacao)

    if resultado == "Agendado":
        session["texto_agendamento_pendente"] = gerar_texto_agendamento(
            request.form.get("resp_cliente", ""),
            request.form.get("data_agendamento", ""),
            request.form.get("janela_agendamento", ""),
            observacao,
            operador_logado,
        )

    # Verifica se, ao terminar esse atendimento, o operador já estourou algum limite de pausa
    if usuario["perfil"] == "OPERADOR":
        pausa_obrigatoria = verificar_pausa_obrigatoria(conn, usuario["id"], datetime.now())
        if pausa_obrigatoria:
            iniciar_pausa(conn, usuario["id"], pausa_obrigatoria, datetime.now(), automatica=True)
            conn.close()
            return redirect("/pausas")

    conn.close()
    return redirect("/cliente")


# ==========================
# FILA DE ANÁLISE DE GPON INDISPONÍVEL
# ==========================

def contadores_fila_gpon(conn, data_inicio=None, data_fim=None):
    """aguardando/em_analise são o estado ATUAL da fila (não fazem sentido
    filtrados por data -- é o que está pendente agora). Os demais respeitam o
    período escolhido."""
    aguardando = conn.execute("SELECT COUNT(*) c FROM mailing WHERE status='AGUARDANDO_ANALISE_GPON'").fetchone()["c"]
    em_analise = conn.execute("SELECT COUNT(*) c FROM mailing WHERE status='EM_ANALISE_GPON'").fetchone()["c"]
    follow_up_pendente = conn.execute("""
        SELECT COUNT(*) c FROM mailing WHERE status='AGUARDANDO_ANALISE_GPON' AND data_retorno_gpon IS NOT NULL
    """).fetchone()["c"]

    if not data_inicio:
        data_inicio = hoje_str()
    if not data_fim:
        data_fim = hoje_str()

    analisado_periodo = conn.execute("""
        SELECT COUNT(*) c FROM atividades
        WHERE date(data_fim) BETWEEN ? AND ? AND tabulacao LIKE 'Analisado%'
    """, (data_inicio, data_fim)).fetchone()["c"]

    hp_liberado = conn.execute("""
        SELECT COUNT(*) c FROM atividades
        WHERE date(data_fim) BETWEEN ? AND ? AND tabulacao = 'Analisado (HP liberado) - Não foi possível agendar'
    """, (data_inicio, data_fim)).fetchone()["c"]

    mantido_bloqueado = conn.execute("""
        SELECT COUNT(*) c FROM atividades
        WHERE date(data_fim) BETWEEN ? AND ? AND tabulacao = 'Analisado (Não possui GPON) Manter bloqueado'
    """, (data_inicio, data_fim)).fetchone()["c"]

    solicitado_bloqueio = conn.execute("""
        SELECT COUNT(*) c FROM atividades
        WHERE date(data_fim) BETWEEN ? AND ? AND tabulacao = 'Analisado (Não possui GPON) Solicitado Bloqueio'
    """, (data_inicio, data_fim)).fetchone()["c"]

    # "Agendado" via GPON -- contratos que passaram por GPON Indisponível em
    # algum momento e tiveram um Agendado registrado dentro do período
    agendado_via_gpon = conn.execute("""
        SELECT COUNT(*) c FROM atividades a
        WHERE a.tabulacao='Agendado' AND date(a.data_fim) BETWEEN ? AND ?
          AND EXISTS (SELECT 1 FROM atividades g WHERE g.contrato=a.contrato AND g.tabulacao='GPON Indisponível')
    """, (data_inicio, data_fim)).fetchone()["c"]

    return {
        "aguardando": aguardando, "em_analise": em_analise, "follow_up_pendente": follow_up_pendente,
        "analisado_periodo": analisado_periodo, "hp_liberado": hp_liberado,
        "mantido_bloqueado": mantido_bloqueado, "solicitado_bloqueio": solicitado_bloqueio,
        "agendado_via_gpon": agendado_via_gpon,
    }


@app.route("/uploads/<path:nome_arquivo>")
@login_required
def servir_upload(nome_arquivo):
    """Serve arquivos da pasta uploads/ (evidências de GPON, fotos de perfil
    etc.) -- exige login, sem exigir permissão específica de módulo, já que é
    usado em vários contextos diferentes. send_from_directory já protege
    contra path traversal sozinho."""
    return send_from_directory(app.config['UPLOAD_FOLDER'], nome_arquivo)


def extrair_nome_evidencia_gpon(observacao):
    """A evidência de GPON Indisponível é salva como texto dentro da
    observação (\"Evidência: nome_do_arquivo.png\"), não numa coluna própria
    -- essa função extrai só o nome do arquivo dali, pra montar o link de
    baixar/ver (pedido do Bruno, 2026-09-04)."""
    if not observacao or "Evidência: " not in observacao:
        return None
    trecho = observacao.split("Evidência: ", 1)[1]
    nome_arquivo = trecho.split(" | ")[0].strip()
    return nome_arquivo or None


def evidencia_gpon_existe(nome_arquivo):
    """Diz se a imagem da evidência ainda está no disco -- pedido do Bruno,
    2026-10-19.

    A tarefa_limpar_imagens_gpon.py apaga as evidências com mais de 30 dias,
    mas o nome do arquivo continua escrito na observação da atividade pra
    sempre (é assim que o CRM guarda a referência). Sem essa checagem, a tela
    mostraria um link "Ver print" que leva a um 404. Com ela, o link só
    aparece clicável quando o arquivo existe; apagado, vira texto estático.

    A conferência de pasta é proposital: o nome chega de um campo de texto
    livre (a observação), então não dá pra confiar que nunca vai ter "..".
    """
    if not nome_arquivo:
        return False
    pasta = os.path.realpath(app.config['UPLOAD_FOLDER'])
    caminho = os.path.realpath(os.path.join(pasta, nome_arquivo))
    if not caminho.startswith(pasta + os.sep):
        return False
    return os.path.isfile(caminho)


app.jinja_env.globals["extrair_nome_evidencia_gpon"] = extrair_nome_evidencia_gpon
app.jinja_env.globals["evidencia_gpon_existe"] = evidencia_gpon_existe


@app.route("/gpon")
@login_required
def gpon():
    usuario = usuario_atual()
    tem_fila = perfil_pode(usuario["perfil"], "gpon_fila")
    tem_tratamento = perfil_pode(usuario["perfil"], "gpon_tratamento")
    if not (tem_fila or tem_tratamento):
        return "Acesso restrito.", 403

    conn = conectar()

    data_fim = request.args.get("data_fim") or hoje_str()
    data_inicio = request.args.get("data_inicio") or (datetime.strptime(data_fim, "%Y-%m-%d") - timedelta(days=30)).strftime("%Y-%m-%d")
    contadores = contadores_fila_gpon(conn, data_inicio, data_fim)

    # Reescrito em 2026-09-04, a pedido do Bruno: a tela agora é SEMPRE
    # dashboard (cards, iguais pro ADM/Supervisor/Analista) + tabela da fila
    # embaixo -- sem mais fluxo automático de "pega o próximo sozinho"
    # (buscar_proximo_gpon deixou de ser chamado aqui; a fila é sempre
    # tratada escolhendo da tabela, com o botão Tratar/Reabrir).
    status_filtro = request.args.get("status") or "ABERTOS"
    contrato_filtro = (request.args.get("contrato") or "").strip()

    # "CONCLUIDO" no mailing NÃO é exclusivo do GPON -- é o mesmo status usado
    # por qualquer fechamento do sistema inteiro (Atendimento normal também
    # usa). Pra não trazer contrato nenhum que nunca passou pelo GPON, exige
    # que a ÚLTIMA tabulação registrada seja uma das de conclusão do GPON
    # (bug encontrado pelo Bruno, 2026-09-04). AGUARDANDO_ANALISE_GPON e
    # EM_ANALISE_GPON já são exclusivos do GPON, não precisam dessa checagem.
    TABULACOES_CONCLUSAO_GPON = (
        "Analisado e agendado",
        "Analisado (HP liberado) - Não foi possível agendar",
        "Analisado (Não possui GPON) Manter bloqueado",
        "Analisado (Não possui GPON) Solicitado Bloqueio",
    )
    filtro_concluido_sql = """
        m.status = 'CONCLUIDO' AND (
            SELECT tabulacao FROM atividades WHERE contrato = m.contrato ORDER BY id DESC LIMIT 1
        ) IN ({})
    """.format(",".join("?" * len(TABULACOES_CONCLUSAO_GPON)))

    if status_filtro == "ABERTOS":
        filtro_status_sql = "m.status IN ('AGUARDANDO_ANALISE_GPON', 'EM_ANALISE_GPON')"
        params_status = []
    elif status_filtro == "CONCLUIDO":
        filtro_status_sql = filtro_concluido_sql
        params_status = list(TABULACOES_CONCLUSAO_GPON)
    else:
        filtro_status_sql = f"(m.status IN ('AGUARDANDO_ANALISE_GPON', 'EM_ANALISE_GPON') OR ({filtro_concluido_sql}))"
        params_status = list(TABULACOES_CONCLUSAO_GPON)

    params = params_status + [data_inicio, data_fim]
    filtro_contrato_sql = ""
    if contrato_filtro:
        filtro_contrato_sql = "AND m.contrato LIKE ?"
        params.append(f"%{contrato_filtro}%")

    fila = conn.execute(f"""
        SELECT m.* FROM mailing m
        WHERE {filtro_status_sql}
          AND date(COALESCE(m.data_entrada_fila, m.data_distribuicao)) BETWEEN ? AND ?
          {filtro_contrato_sql}
        ORDER BY m.data_entrada_fila ASC
    """, params).fetchall()

    conn.close()
    texto_agendamento = session.pop("texto_agendamento_pendente", None)
    return render_template("Gpon.html", pode_tratar=False, pode_tratar_da_fila=tem_tratamento,
                            cliente=None, historico=None, fila=fila,
                            contadores=contadores, data_inicio=data_inicio, data_fim=data_fim,
                            status_filtro=status_filtro, contrato_filtro=contrato_filtro,
                            texto_agendamento=texto_agendamento)


@app.route("/gpon/tratar/<int:id_mailing>")
@login_required
def gpon_tratar_pagina(id_mailing):
    """Subpágina de tratamento de UM contrato específico -- dados, histórico
    (do atendimento e da própria análise de GPON, tudo na mesma 'atividades'),
    link do print, e o formulário de tabulação. Só abre se esse contrato
    estiver EM_ANALISE_GPON com essa pessoa como operador (ou seja, precisa
    ter clicado Tratar/Reabrir primeiro -- ver gpon_tratar_especifico)."""
    usuario = usuario_atual()
    if not perfil_pode(usuario["perfil"], "gpon_tratamento"):
        return "Acesso restrito.", 403

    conn = conectar()
    cliente = conn.execute(
        "SELECT * FROM mailing WHERE id=? AND status='EM_ANALISE_GPON' AND operador=?",
        (id_mailing, usuario["nome"])
    ).fetchone()
    if not cliente:
        conn.close()
        return redirect("/gpon")

    historico = conn.execute(
        "SELECT * FROM atividades WHERE (id_mailing=? OR (id_mailing IS NULL AND contrato=?)) ORDER BY id DESC",
        (cliente["id"], cliente["contrato"])
    ).fetchall()
    contadores = contadores_fila_gpon(conn, hoje_str(), hoje_str())
    conn.close()
    texto_agendamento = session.pop("texto_agendamento_pendente", None)
    return render_template("Gpon.html", pode_tratar=True, cliente=cliente, historico=historico, fila=None,
                            contadores=contadores, data_inicio=hoje_str(), data_fim=hoje_str(),
                            texto_agendamento=texto_agendamento)


@app.route("/gpon/tratar_especifico/<int:id_mailing>", methods=["POST"])
@login_required
def gpon_tratar_especifico(id_mailing):
    """Deixa quem tem gpon_tratamento reivindicar um contrato ESPECÍFICO da
    fila (Tratar ou Reabrir) -- reserva de forma atômica (WHERE status=... na
    mesma query). Fila compartilhada: qualquer analista pode reivindicar,
    mesmo um contrato que já esteja EM_ANALISE_GPON com outra pessoa, ou já
    CONCLUIDO (reabrir) -- confirmado com o Bruno em 2026-09-04."""
    usuario = usuario_atual()
    if not perfil_pode(usuario["perfil"], "gpon_tratamento"):
        return "Acesso restrito.", 403

    conn = conectar()
    agora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    expira = datetime.now() + INATIVIDADE_LIMITE
    cursor = conn.execute("""
        UPDATE mailing SET status='EM_ANALISE_GPON', operador=?, data_distribuicao=?, data_expiracao=?
        WHERE id=? AND status IN ('AGUARDANDO_ANALISE_GPON', 'EM_ANALISE_GPON', 'CONCLUIDO')
    """, (usuario["nome"], agora_str, expira.strftime("%Y-%m-%d %H:%M:%S"), id_mailing))
    conn.commit()
    conn.close()

    if cursor.rowcount == 0:
        return redirect("/gpon?erro=" + "Não encontrei esse contrato pra reivindicar.")
    return redirect(f"/gpon/tratar/{id_mailing}")


@app.route("/gpon/proprio")
@login_required
def gpon_proprio():
    """Mostra a tela de tratamento (mesmo formato do 'próximo automático')
    pro contrato que o analista já está com EM_ANALISE_GPON agora -- usada
    depois de reivindicar um específico da fila (ver gpon_tratar_especifico),
    já que buscar_proximo_gpon só busca AGUARDANDO (nunca acha o que já está
    em análise com o próprio usuário)."""
    usuario = usuario_atual()
    if not perfil_pode(usuario["perfil"], "gpon_tratamento"):
        return "Acesso restrito.", 403
    conn = conectar()
    cliente = conn.execute(
        "SELECT * FROM mailing WHERE operador=? AND status='EM_ANALISE_GPON' ORDER BY data_distribuicao DESC LIMIT 1",
        (usuario["nome"],)
    ).fetchone()
    historico = None
    if cliente:
        historico = conn.execute(
            "SELECT * FROM atividades WHERE (id_mailing=? OR (id_mailing IS NULL AND contrato=?)) ORDER BY id DESC",
            (cliente["id"], cliente["contrato"])
        ).fetchall()
    contadores = contadores_fila_gpon(conn)
    conn.close()
    return render_template("Gpon.html", pode_tratar=True, cliente=cliente, historico=historico, fila=None,
                            contadores=contadores, data_inicio=hoje_str(), data_fim=hoje_str())


@app.route("/gpon/detalhe/<int:id_mailing>")
@login_required
def gpon_detalhe(id_mailing):
    usuario = usuario_atual()
    if not perfil_pode(usuario["perfil"], "gpon_fila"):
        return "Acesso restrito.", 403
    conn = conectar()
    cliente = conn.execute("SELECT * FROM mailing WHERE id=?", (id_mailing,)).fetchone()
    if not cliente:
        conn.close()
        return redirect("/gpon")
    historico = conn.execute(
        "SELECT * FROM atividades WHERE (id_mailing=? OR (id_mailing IS NULL AND contrato=?)) ORDER BY id DESC",
        (cliente["id"], cliente["contrato"])
    ).fetchall()
    contadores = contadores_fila_gpon(conn)
    conn.close()
    return render_template("Gpon.html", pode_tratar=False, cliente=cliente, historico=historico, fila=None,
                            modo_detalhe=True, contadores=contadores, data_inicio=hoje_str(), data_fim=hoje_str())


@app.route("/gpon/salvar", methods=["POST"])
@login_required
def gpon_salvar():
    usuario = usuario_atual()
    if not perfil_pode(usuario["perfil"], "gpon_tratamento"):
        return "Você não tem a permissão de Tratamento em GPON Indisponível.", 403

    id_cliente = request.form.get("id_cliente")
    motivo = request.form.get("motivo")
    observacao = (request.form.get("observacao") or "").strip()
    analista_logado = usuario["nome"]

    conn = conectar()
    cliente_atual = conn.execute(
        "SELECT * FROM mailing WHERE id=? AND status='EM_ANALISE_GPON' AND operador=?",
        (id_cliente, analista_logado)
    ).fetchone()
    if not cliente_atual:
        conn.close()
        return redirect("/gpon")

    contrato = cliente_atual["contrato"]
    tentativa_atual = cliente_atual["tentativa"] or 1
    agora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    inicio_atendimento = cliente_atual["data_distribuicao"] or agora_str

    if motivo == "Analisado e agendado":
        registrar_tratamento(conn, id_cliente, contrato, tentativa_atual, analista_logado, "Agendado", observacao)
        session["texto_agendamento_pendente"] = gerar_texto_agendamento(
            request.form.get("resp_cliente", ""), request.form.get("data_agendamento", ""),
            request.form.get("janela_agendamento", ""), observacao, analista_logado,
        )

    # "Tempo efetivo de tela" tb vale pras tabulações de GPON, que entram na
    # mesma tabela 'atividades' e caem nas mesmas médias do Dashboard/Qualidade.
    # GPON não tem visita de tela instrumentada (só Atendimento/Buscar
    # Contrato, ver seção "VISITAS DE TELA" acima) -- usa direto o cálculo por
    # sessão/pausa, já bounded ao fim da tratativa anterior desse contrato.
    inicio_janela_gpon = _fim_ultima_atividade(conn, contrato) or inicio_atendimento
    minutos_efetivos_gpon = calcular_minutos_efetivos(conn, analista_logado, inicio_janela_gpon, agora_str)

    if motivo == "Analisado (HP liberado) - Não foi possível agendar":
        operador_original = quem_tabulou_gpon(conn, contrato)
        # Só reserva de volta pro operador original se ele ainda estiver ativo
        # e não estiver de férias/afastado hoje -- senão o contrato ficaria
        # "reservado" pra sempre pra alguém que nunca mais vai buscar fila
        # (reservado_para trava a distribuição normal pra qualquer outro).
        # Nesse caso, cai como reservado_para=NULL, entrando na distribuição
        # normal pra quem estiver disponível (pedido do Bruno, 2026-09-04).
        destino_reserva = operador_original if operador_disponivel_hoje(conn, operador_original) else None
        texto = "Analisado pela Esteira GPON: HP liberado, mas não foi possível agendar."
        obs_final = f"{texto} | Obs: {observacao}" if observacao else texto
        conn.execute("""
            INSERT INTO atividades (contrato, id_mailing, operador, tentativa, tabulacao, observacao, data_inicio, data_fim, status, minutos_efetivos)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDENTE', ?)
        """, (contrato, id_cliente, analista_logado, tentativa_atual, motivo, obs_final, inicio_atendimento, agora_str, minutos_efetivos_gpon))
        conn.execute(
            "UPDATE mailing SET status='PENDENTE', operador=NULL, reservado_para=?, data_entrada_fila=? WHERE id=?",
            (destino_reserva, agora_str, id_cliente)
        )

    elif motivo in ("Analisado (Não possui GPON) Manter bloqueado", "Analisado (Não possui GPON) Solicitado Bloqueio"):
        conn.execute("""
            INSERT INTO atividades (contrato, id_mailing, operador, tentativa, tabulacao, observacao, data_inicio, data_fim, status, minutos_efetivos)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'CONCLUIDO', ?)
        """, (contrato, id_cliente, analista_logado, tentativa_atual, motivo, observacao, inicio_atendimento, agora_str, minutos_efetivos_gpon))
        conn.execute("UPDATE mailing SET status='CONCLUIDO', operador=? WHERE id=?", (analista_logado, id_cliente))

    elif motivo == "Análise Pendente":
        # Reescrito em 2026-09-04, a pedido do Bruno: mantém o contrato "em
        # andamento" (EM_ANALISE_GPON), não devolve pra "aguardando" -- fica
        # visível na tabela com o botão Tratar disponível (fila
        # compartilhada, qualquer analista continua depois). Antes disso
        # exigia data/hora de retorno e escondia o contrato até vencer; não
        # tem mais essa exigência -- os campos de data ficam só como anotação
        # opcional, se a analista quiser registrar uma previsão.
        data_ret = request.form.get("retorno_data")
        hora_ret = request.form.get("retorno_hora")
        if data_ret and hora_ret:
            texto = f"Análise pendente, previsão de retorno para {data_ret} às {hora_ret}."
        else:
            texto = "Análise pendente."
        obs_final = f"{texto} | Obs: {observacao}" if observacao else texto
        conn.execute("""
            INSERT INTO atividades (contrato, id_mailing, operador, tentativa, tabulacao, observacao, data_inicio, data_fim, status, minutos_efetivos)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'EM_ANALISE_GPON', ?)
        """, (contrato, id_cliente, analista_logado, tentativa_atual, motivo, obs_final, inicio_atendimento, agora_str, minutos_efetivos_gpon))
        conn.execute(
            "UPDATE mailing SET status='EM_ANALISE_GPON', operador=? WHERE id=?",
            (analista_logado, id_cliente)
        )

    conn.commit()
    conn.close()
    return redirect("/gpon")


# ==========================
# BUSCA DE CONTRATO (continuidade de atendimento / editar agendamento)
# ==========================

@app.route("/cliente/buscar")
@login_required
@permissao_required("buscar_contrato")
def cliente_buscar():
    termo = (request.args.get("q") or "").strip()
    resultados = []
    if termo:
        conn = conectar()
        resultados = conn.execute("""
            SELECT id, contrato, cliente, cidade, status, operador FROM mailing
            WHERE contrato LIKE ? OR cliente LIKE ?
            ORDER BY id LIMIT 30
        """, (f"%{termo}%", f"%{termo}%")).fetchall()
        conn.close()
    return render_template("BuscarContrato.html", termo=termo, resultados=resultados)


@app.route("/atendimento/<int:mailing_id>")
@login_required
@permissao_required("buscar_contrato")
def atendimento_avulso(mailing_id):
    usuario = usuario_atual()
    conn = conectar()

    # Mesma trava de pausa que a fila normal (/cliente) já tinha -- sem isso,
    # dava pra continuar tratando contrato por aqui mesmo com uma pausa
    # obrigatória vencida, só porque esse caminho (Buscar Contrato) não passava
    # por essa checagem.
    if usuario["perfil"] == "OPERADOR":
        pausa = bloquear_por_pausa(conn, usuario["id"], datetime.now())
        if pausa:
            conn.close()
            return redirect("/pausas")

    cliente = conn.execute("SELECT * FROM mailing WHERE id=?", (mailing_id,)).fetchone()
    if not cliente:
        conn.close()
        return "Contrato não encontrado.", 404
    # Escopado por id_mailing (ou legado sem id_mailing) e ordenado por id
    # (insertion order real) em vez de data_inicio (que pode repetir entre
    # tratativas -- ver _garantir_coluna_id_mailing_atividades_followup e o
    # comentário em _mesclar_historico_com_ajustes).
    historico_atividades = conn.execute("""
        SELECT * FROM atividades
        WHERE (id_mailing=? OR (id_mailing IS NULL AND contrato=?))
        ORDER BY id DESC
    """, (cliente["id"], cliente["contrato"])).fetchall()
    ajustes_agendamento = conn.execute("""
        SELECT ag.* FROM ajustes_agendamento ag
        JOIN atividades a ON a.id = ag.atividade_id
        WHERE ag.contrato=? AND (a.id_mailing=? OR (a.id_mailing IS NULL AND a.contrato=?))
        ORDER BY ag.data_hora DESC
    """, (cliente["contrato"], cliente["id"], cliente["contrato"])).fetchall()
    historico = _mesclar_historico_com_ajustes(historico_atividades, ajustes_agendamento)
    _abrir_visita_tela(conn, usuario["id"], cliente["contrato"])
    scripts_atendimento = [dict(s) for s in conn.execute(
        "SELECT * FROM scripts_atendimento ORDER BY id"
    ).fetchall()]
    conn.close()
    texto_agendamento = session.pop("texto_agendamento_pendente", None)
    return render_template("AtendimentoAvulso.html", cliente=cliente, historico=historico,
                            texto_agendamento=texto_agendamento, scripts_atendimento=scripts_atendimento)


@app.route("/atendimento/<int:mailing_id>/tratar", methods=["POST"])
@login_required
@permissao_required("atendimento")
def atendimento_avulso_tratar(mailing_id):
    usuario = usuario_atual()
    operador_logado = usuario["nome"]
    resultado = request.form.get("resultado")
    observacao = request.form.get("observacao") or ""

    conn = conectar()
    cliente_atual = conn.execute("SELECT contrato, tentativa FROM mailing WHERE id=?", (mailing_id,)).fetchone()
    if not cliente_atual:
        conn.close()
        return "Contrato não encontrado.", 404

    contrato = cliente_atual["contrato"]
    tentativa_atual = cliente_atual["tentativa"] or 1

    registrar_tratamento(conn, mailing_id, contrato, tentativa_atual, operador_logado, resultado, observacao)

    # Mesmo texto padrão de ocorrência que a fila normal (/salvar) já gera pro
    # agendamento -- faltava aqui, por isso o modal não aparecia quando o
    # agendamento era feito por Buscar Contrato em vez da fila normal.
    if resultado == "Agendado":
        session["texto_agendamento_pendente"] = gerar_texto_agendamento(
            request.form.get("resp_cliente", ""),
            request.form.get("data_agendamento", ""),
            request.form.get("janela_agendamento", ""),
            observacao,
            operador_logado,
        )

    # Mesma checagem de pausa obrigatória que a fila normal (/salvar) já tinha
    # depois de registrar um fechamento -- faltava aqui, por isso um fechamento
    # feito por Buscar Contrato nunca disparava a pausa automática.
    if usuario["perfil"] == "OPERADOR":
        pausa_obrigatoria = verificar_pausa_obrigatoria(conn, usuario["id"], datetime.now())
        if pausa_obrigatoria:
            iniciar_pausa(conn, usuario["id"], pausa_obrigatoria, datetime.now(), automatica=True)
            conn.close()
            return redirect("/pausas")

    conn.close()
    return redirect(f"/atendimento/{mailing_id}")


# ==========================
# TURNO (login/logout simulado, sem senha por enquanto)
# ==========================

@app.route("/turno/entrar")
@login_required
@permissao_required("pausas")
def turno_entrar():
    usuario = usuario_atual()
    conn = conectar()
    # Segunda trava da regra "só entra 5 min antes da escala" -- a primeira é
    # o botão "Abrir a Ferramenta" no /portal (que nem chega a mandar pra cá se
    # bloqueado); essa aqui é só rede de segurança contra quem tentar pular o
    # portal digitando a URL direto.
    bloqueado, motivo = verificar_bloqueio_login(conn, usuario)
    if bloqueado:
        conn.close()
        session["portal_erro_ferramenta"] = motivo
        return redirect("/portal")
    aberta = conn.execute(
        "SELECT id FROM sessoes WHERE usuario_id=? AND logout_em IS NULL", (usuario["id"],)
    ).fetchone()
    if not aberta:
        conn.execute(
            "INSERT INTO sessoes (usuario_id, login_em, logout_em) VALUES (?, ?, NULL)",
            (usuario["id"], datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        conn.commit()
    conn.close()
    return redirect("/")


def encerramento_automatico_ligado(conn):
    linha = conn.execute("SELECT encerramento_automatico_ativo FROM configuracoes WHERE id=1").fetchone()
    return linha is None or linha["encerramento_automatico_ativo"] is None or bool(linha["encerramento_automatico_ativo"])


def bloqueio_login_ligado(conn):
    linha = conn.execute("SELECT bloqueio_login_ativo FROM configuracoes WHERE id=1").fetchone()
    return linha is None or linha["bloqueio_login_ativo"] is None or bool(linha["bloqueio_login_ativo"])


def pausa_automatica_ligada(conn):
    linha = conn.execute("SELECT pausa_automatica_ativa FROM configuracoes WHERE id=1").fetchone()
    return linha is None or linha["pausa_automatica_ativa"] is None or bool(linha["pausa_automatica_ativa"])


TOLERANCIA_LOGIN = timedelta(minutes=5)


def verificar_bloqueio_login(conn, usuario):
    """Só se aplica ao perfil Operador. Bloqueia login fora do dia escalado, ou
    mais de 5 minutos antes do horário de início da escala. Retorna (bloqueado, motivo)."""
    if usuario["perfil"] != "OPERADOR":
        return False, None
    if not bloqueio_login_ligado(conn):
        return False, None

    escala = conn.execute("SELECT dias_trabalho, hora_inicio, ferias_inicio, ferias_fim FROM escalas WHERE usuario_id=?", (usuario["id"],)).fetchone()
    if not escala or not escala["dias_trabalho"] or not escala["hora_inicio"]:
        return False, None  # sem escala cadastrada -- não dá pra bloquear por falta de referência

    agora = datetime.now()
    hoje_str_valor = agora.strftime("%Y-%m-%d")

    if escala["ferias_inicio"] and escala["ferias_fim"] and escala["ferias_inicio"] <= hoje_str_valor <= escala["ferias_fim"]:
        return True, "Você está de férias nesse período."

    abrev_hoje = DIAS_SEMANA[agora.weekday()]
    dias_trabalho = (escala["dias_trabalho"] or "").split(",")
    if abrev_hoje not in dias_trabalho:
        return True, "Hoje não é um dia da sua escala de trabalho."

    try:
        hora_inicio = datetime.strptime(escala["hora_inicio"], "%H:%M").time()
    except ValueError:
        return False, None

    limite_liberado = (datetime.combine(agora.date(), hora_inicio) - TOLERANCIA_LOGIN).time()
    if agora.time() < limite_liberado:
        return True, f"Seu horário de início é {escala['hora_inicio']}. O acesso libera a partir de {limite_liberado.strftime('%H:%M')}."

    return False, None


def encerrar_atendimento_forcado(conn, usuario_id, agora_str=None, encerrado_por="AUTOMATICO"):
    """Encerra o atendimento de UM usuário: fecha qualquer pausa em aberto
    primeiro (senão fica sem fim_real pra sempre no banco), e só depois fecha
    a sessão. Usada tanto pelo encerramento automático das 18h30 quanto pelo
    botão manual do Supervisor. Não commita -- quem chama decide quando.

    encerrado_por (pedido do Bruno, 2026-09-20, pra tela Tratamento/Pausas
    mostrar a origem do fim de cada jornada): 'AUTOMATICO' por padrão (o
    caminho mais comum, a rotina das 18h30 nunca passa esse argumento), ou o
    nome de quem clicou o botão manual do Supervisor -- ver
    supervisao_encerrar_atendimento.

    IMPORTANTE: fecha a sessão/pausa aberta INDEPENDENTE de qual dia ela foi
    aberta (sem filtrar por date(login_em)=hoje) -- isso é de propósito.
    Filtrar só por "hoje" fazia sentido pro encerramento automático (que só
    olha sessões do próprio dia), mas quebrava o botão manual do Supervisor:
    se alguém ficou com sessão aberta desde o dia anterior (esqueceu, ou por
    causa de alguma instabilidade), o botão "Encerrar atendimento" não
    conseguia fechar nada, porque a condição de data nunca batia (bug
    encontrado em 2026-09-04).

    Também libera de volta pra fila qualquer contrato que a pessoa estava
    com EM_ANDAMENTO -- sem isso, o contrato ficava preso até vencer
    sozinho (quase 24h), mesmo com a pessoa já sem sessão aberta (mesmo
    problema identificado em 2026-09-04: vários followups "parados" porque o
    operador tinha um EM_ANDAMENTO órfão bloqueando). Contrato que veio de
    um followup (2ª/3ª Tentativa ou Retorno) ainda pendente pra ela volta
    reservado (ENFILEIRADO), não pra fila geral -- mesma regra de
    buscar_proximo_cliente, pedido do Bruno, 2026-09-19 (esse encerramento
    forçado roda toda tarde às 18h30, então era um dos jeitos mais comuns
    de um followup reservado perder o dono sem ninguém perceber)."""
    agora_str = agora_str or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("""
        UPDATE pausas SET fim_real=?
        WHERE usuario_id=? AND fim_real IS NULL
    """, (agora_str, usuario_id))
    conn.execute("""
        UPDATE sessoes SET logout_em=?, encerrado_por=?
        WHERE usuario_id=? AND logout_em IS NULL
    """, (agora_str, encerrado_por, usuario_id))

    usuario_row = conn.execute("SELECT nome FROM usuarios WHERE id=?", (usuario_id,)).fetchone()
    if usuario_row:
        nome = usuario_row["nome"]

        # ids de mailing protegidos por um followup pendente desse operador.
        # Calculado em Python (não numa subquery correlacionada por
        # contrato) pra resolver certo tanto followup novo (já com
        # id_mailing, a partir de 2026-09-20) quanto followup antigo
        # (id_mailing NULL, cai pro contrato) sem duplicar linha via JOIN
        # quando o contrato existe em mais de uma cidade -- mesmo motivo de
        # _garantir_coluna_id_mailing_atividades_followup.
        protegidos = set()
        for linha_f in conn.execute(
            "SELECT id_mailing, contrato FROM followup WHERE operador=? AND status='PENDENTE'", (nome,)
        ):
            if linha_f["id_mailing"] is not None:
                protegidos.add(linha_f["id_mailing"])
            else:
                for m in conn.execute("SELECT id FROM mailing WHERE contrato=?", (linha_f["contrato"],)):
                    protegidos.add(m["id"])

        marcador = ",".join("?" * len(protegidos)) if protegidos else "-1"
        conn.execute(f"""
            UPDATE mailing SET status='ENFILEIRADO', operador=NULL, ultimo_operador=?,
                   data_distribuicao=NULL, data_expiracao=NULL, data_entrada_fila=?
            WHERE operador=? AND status='EM_ANDAMENTO' AND id IN ({marcador})
        """, (nome, agora_str, nome, *protegidos))
        conn.execute(f"""
            UPDATE mailing SET status='PENDENTE', operador=NULL, ultimo_operador=?,
                   data_distribuicao=NULL, data_expiracao=NULL, data_entrada_fila=?
            WHERE operador=? AND status='EM_ANDAMENTO' AND id NOT IN ({marcador})
        """, (nome, agora_str, nome, *protegidos))


def _liberar_em_andamento_por_ausencia(conn, usuario_id, agora_str=None):
    """Libera de volta pra fila qualquer contrato que esse usuário esteja
    com EM_ANDAMENTO agora -- chamada quando uma ausência de HOJE
    (Férias ou Afastamento) é registrada pra ele em Supervisão (ver
    supervisao_ausencia). Pedido do Bruno, 2026-09-21, junto com a
    remoção do timeout de 24h em buscar_proximo_cliente: sem timeout,
    um contrato distribuído fica com a mesma pessoa pra sempre até ela
    tratar -- as únicas exceções viraram Férias/Afastamento (aqui) e
    Desligamento (_encerrar_sessao_e_redistribuir_tarefas). Sem essa
    liberação explícita, marcar alguém como Férias não tiraria da mão
    dela um contrato que já estava aberto na tela, porque não existe
    mais nenhum outro gatilho (nem o de 24h, nem o encerramento das
    18h30, que só fecha quem tem sessão aberta hoje) que solte isso.

    Mesma regra de sempre pra decidir o destino: se o contrato ainda tem
    um follow-up (2ª/3ª Tentativa ou Retorno) PENDENTE reservado pra essa
    pessoa, volta ENFILEIRADO (a reserva do follow-up continua valendo,
    só abre pra outra pessoa quando o passo 2 de buscar_proximo_cliente
    checar a ausência de novo); senão, volta PENDENTE puro, pra fila
    geral. Não mexe em sessão/pausa -- só o vínculo do contrato mesmo.
    Não commita -- quem chama decide quando."""
    agora_str = agora_str or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    usuario_row = conn.execute("SELECT nome FROM usuarios WHERE id=?", (usuario_id,)).fetchone()
    if not usuario_row:
        return
    nome = usuario_row["nome"]

    protegidos = set()
    for linha_f in conn.execute(
        "SELECT id_mailing, contrato FROM followup WHERE operador=? AND status='PENDENTE'", (nome,)
    ):
        if linha_f["id_mailing"] is not None:
            protegidos.add(linha_f["id_mailing"])
        else:
            for m in conn.execute("SELECT id FROM mailing WHERE contrato=?", (linha_f["contrato"],)):
                protegidos.add(m["id"])

    marcador = ",".join("?" * len(protegidos)) if protegidos else "-1"
    conn.execute(f"""
        UPDATE mailing SET status='ENFILEIRADO', operador=NULL, ultimo_operador=?,
               data_distribuicao=NULL, data_expiracao=NULL, data_entrada_fila=?
        WHERE operador=? AND status='EM_ANDAMENTO' AND id IN ({marcador})
    """, (nome, agora_str, nome, *protegidos))
    conn.execute(f"""
        UPDATE mailing SET status='PENDENTE', operador=NULL, ultimo_operador=?,
               data_distribuicao=NULL, data_expiracao=NULL, data_entrada_fila=?
        WHERE operador=? AND status='EM_ANDAMENTO' AND id NOT IN ({marcador})
    """, (nome, agora_str, nome, *protegidos))


def encerrar_jornadas_esquecidas():
    """Encerra (logout_em) a sessão de todo mundo que esqueceu de bater saída --
    roda sozinho todo dia às 18h30 (ver _rotina_encerramento_automatico), a
    menos que o ADM tenha desligado essa função na tela de Pausas. Fecha
    qualquer pausa em aberto de cada pessoa antes de fechar a sessão dela.

    Só se aplica a Operador e Analista -- Analista II, Supervisor e ADM
    ficam de fora (pedido do Bruno, 2026-09-09: esses perfis não têm
    jornada fixa de call center do mesmo jeito, faz sentido eles decidirem
    a própria saída).

    Manda um relatório simples pelo Telegram sempre que roda (mesmo sem
    ninguém esquecido -- confirma que a Task rodou), pedido do Bruno,
    2026-09-10 -- via a mesma Edge Function do Supabase que o relatório
    diário já usa (não fala direto com api.telegram.org -- a rede da
    empresa bloqueia isso; corrigido em 2026-09-10 depois do Bruno testar e
    a mensagem não chegar, porque a primeira versão tentava falar direto
    com o Telegram em vez de passar pelo Supabase). Só manda se o Supabase
    já estiver configurado; se não estiver, só fecha as sessões mesmo e não
    manda nada."""
    conn = conectar()
    if not encerramento_automatico_ligado(conn):
        conn.close()
        print("Encerramento automático de jornada está desligado (config do ADM) -- pulando.")
        return

    agora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    hoje = datetime.now().strftime("%Y-%m-%d")

    usuarios_com_sessao_aberta = conn.execute("""
        SELECT DISTINCT s.usuario_id, u.nome FROM sessoes s
        JOIN usuarios u ON u.id = s.usuario_id
        WHERE s.logout_em IS NULL AND date(s.login_em)=? AND u.perfil IN ('OPERADOR', 'ANALISTA')
        ORDER BY u.nome
    """, (hoje,)).fetchall()

    for linha in usuarios_com_sessao_aberta:
        encerrar_atendimento_forcado(conn, linha["usuario_id"], agora_str)

    conn.commit()
    fechadas = len(usuarios_com_sessao_aberta)
    if fechadas:
        print(f"[{agora_str}] Encerramento automático de jornada: {fechadas} sessão(ões) fechada(s).")
    else:
        print(f"[{agora_str}] Encerramento automático de jornada: nenhuma sessão esquecida hoje.")

    config = conn.execute("SELECT supabase_project_url, supabase_anon_key FROM configuracoes WHERE id=1").fetchone()
    if config and config["supabase_project_url"] and config["supabase_anon_key"]:
        if fechadas:
            nomes = "\n".join(f"- {u['nome']}" for u in usuarios_com_sessao_aberta)
            texto = (
                f"🔒 *Encerramento automático de jornada*\n"
                f"{fechadas} sessão(ões) esquecida(s) fechada(s) hoje:\n\n{nomes}"
            )
        else:
            texto = "🔒 *Encerramento automático de jornada*\nNenhuma sessão esquecida hoje -- tudo certo."
        sucesso, erro = enviar_relatorio_telegram_via_supabase(config["supabase_project_url"], config["supabase_anon_key"], texto)
        if not sucesso:
            print(f"Aviso: não consegui enviar o relatório pro Telegram -- {erro}")
    conn.close()


def _rotina_encerramento_automatico():
    """Roda em segundo plano enquanto o servidor estiver de pé, verificando a
    cada minuto se já passou das 18h30 -- e só dispara uma vez por dia."""
    ultima_execucao = None
    while True:
        agora = datetime.now()
        if agora.time() >= time(18, 30) and ultima_execucao != agora.date():
            try:
                encerrar_jornadas_esquecidas()
            except Exception as erro:
                print(f"Erro no encerramento automático de jornada: {erro}")
            ultima_execucao = agora.date()
        tempo_espera.sleep(60)


def _garantir_coluna_data_cadastro():
    """Auto-migração leve: garante que a coluna data_cadastro existe em
    usuarios, sem precisar de script separado nem acesso a console -- só
    trocar o app.py e dar Reload já resolve (roda 1x por subida do
    processo; se a coluna já existir, o ALTER TABLE falha com "duplicate
    column name" e a gente ignora, então é seguro rodar de novo a cada
    reload). Quem já existia no sistema antes dessa migração recebe a
    data de hoje como cadastro -- isso é de propósito: evita que
    aniversários já passados esse ano, de gente que já está no sistema
    há tempos, sejam postados retroativamente de uma vez só (foi
    exatamente o que aconteceu no primeiro deploy da checagem de
    aniversário atrasado, antes dessa correção). Cadastro feito a partir
    de agora (tela de Importação de usuários) já grava a data real."""
    conn = conectar()
    try:
        conn.execute("ALTER TABLE usuarios ADD COLUMN data_cadastro TEXT")
        conn.execute(
            "UPDATE usuarios SET data_cadastro=? WHERE data_cadastro IS NULL",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),)
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass  # coluna já existe -- migração já rodou antes
    finally:
        conn.close()


_garantir_coluna_data_cadastro()


def _garantir_colunas_exclusao_usuario():
    """Auto-migração leve (mesmo padrão de _garantir_coluna_data_cadastro): garante
    as colunas usadas pela Exclusão de usuário (ADM, tela de Supervisão) --
    diferente de Desligar/Inativar (que só marca ativo=0 e deixa a pessoa visível,
    cinza, na lista, com opção de Reativar), Excluir marca excluido=1 e some com o
    nome da lista de vez, sem apagar nenhum histórico de tratamentos/atividades
    (não mexe em nenhuma outra tabela, só em 'usuarios'). excluido_em/excluido_por
    ficam só de registro (não são usados pra nenhuma regra hoje)."""
    conn = conectar()
    for coluna, tipo in (("excluido", "INTEGER DEFAULT 0"), ("excluido_em", "TEXT"), ("excluido_por", "TEXT")):
        try:
            conn.execute(f"ALTER TABLE usuarios ADD COLUMN {coluna} {tipo}")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # coluna já existe -- migração já rodou antes
    conn.close()


_garantir_colunas_exclusao_usuario()


def postar_aniversariantes_do_dia():
    """Cria um post em Informações pra cada pessoa que já fez aniversário
    este ano e ainda não teve isso postado (não só quem faz aniversário
    exatamente hoje -- se ninguém abriu o sistema no dia certo, a pessoa
    ainda é pega na próxima vez que alguém entrar, com o texto ajustado pra
    não dizer "hoje" quando já passou; nunca duplica por causa do controle
    ultimo_aniversario_postado_ano). Só considera um aniversário "atrasado"
    pra recuperar se ele caiu DEPOIS da pessoa já estar cadastrada no
    sistema (data_cadastro) -- assim ninguém recebe um "parabéns atrasado"
    de um aniversário que já tinha passado antes mesmo de a pessoa entrar
    no sistema."""
    conn = conectar()
    hoje = datetime.now()

    # Fim de semana/feriado: não posta nada hoje -- a pessoa é pega
    # automaticamente no próximo dia útil (a lógica de "atrasado" abaixo já
    # cuida disso sozinha, sem precisar de mais nada aqui). Pedido do Bruno,
    # 2026-09-04.
    if not eh_dia_util(hoje):
        conn.close()
        return

    mes_dia_hoje = hoje.strftime("%m-%d")
    ano_atual = hoje.strftime("%Y")

    aniversariantes = conn.execute("""
        SELECT id, nome, data_cadastro, strftime('%m-%d', data_nascimento) AS mes_dia
        FROM usuarios
        WHERE ativo=1 AND (divulgar_aniversario IS NULL OR divulgar_aniversario=1)
          AND data_nascimento IS NOT NULL
          AND strftime('%m-%d', data_nascimento) <= ?
          AND (ultimo_aniversario_postado_ano IS NULL OR ultimo_aniversario_postado_ano != ?)
    """, (mes_dia_hoje, ano_atual)).fetchall()

    for pessoa in aniversariantes:
        mes_dia = pessoa["mes_dia"]
        aniversario_este_ano = f"{ano_atual}-{mes_dia}"

        # data_cadastro pode ter hora junto ("YYYY-MM-DD HH:MM:SS") -- compara
        # só a parte da data. Se por algum motivo não tiver cadastro
        # registrado (não deveria acontecer, a migração acima cobre todo
        # mundo), não bloqueia -- trata como elegível.
        cadastro = pessoa["data_cadastro"]
        cadastro_data = cadastro[:10] if cadastro else None
        if cadastro_data and aniversario_este_ano < cadastro_data:
            continue  # aniversário já tinha passado antes da pessoa ser cadastrada

        primeiro_nome = pessoa["nome"].split()[0].title()
        if mes_dia == mes_dia_hoje:
            titulo = f"🎉 Feliz aniversário, {primeiro_nome}!"
            conteudo_html = (
                f"<p>Hoje é o aniversário de <strong>{pessoa['nome'].title()}</strong>! "
                f"Bora deixar o dia dela(e) ainda melhor — parabéns! 🎂🎈</p>"
            )
        else:
            mes_str, dia_str = mes_dia.split("-")
            data_formatada = f"{dia_str}/{mes_str}/{ano_atual}"
            titulo = f"🎉 Parabéns (atrasado), {primeiro_nome}!"
            conteudo_html = (
                f"<p>Foi aniversário de <strong>{pessoa['nome'].title()}</strong> em "
                f"{data_formatada} e não queríamos deixar passar em branco — parabéns! 🎂🎈</p>"
            )
        conn.execute("""
            INSERT INTO noticias (titulo, conteudo_html, autor, data_publicacao, ativo)
            VALUES (?, ?, 'Equipe Migração', ?, 1)
        """, (titulo, conteudo_html, hoje.strftime("%Y-%m-%d %H:%M:%S")))
        conn.execute("UPDATE usuarios SET ultimo_aniversario_postado_ano=? WHERE id=?", (ano_atual, pessoa["id"]))

    conn.commit()
    conn.close()


def _rotina_aniversarios():
    """Roda em segundo plano, checando de hora em hora se é aniversário de
    alguém hoje (o controle de "já postou esse ano" evita duplicar).
    Só dispara quando o app roda direto (`python app.py`, uso local no
    Anaconda) -- ver verificar_aniversarios_se_necessario() pra hospedado."""
    while True:
        try:
            postar_aniversariantes_do_dia()
        except Exception as erro:
            print(f"Erro na rotina de aniversários: {erro}")
        tempo_espera.sleep(300)


_ULTIMA_VERIFICACAO_ANIVERSARIOS = None


def verificar_aniversarios_se_necessario():
    """Substitui a rotina em thread pra quando o app roda hospedado (WSGI no
    PythonAnywhere): o bloco `if __name__ == "__main__":` que inicia as
    threads nunca executa sob WSGI, então nada disparava a postagem de
    aniversário lá. Em vez de depender de thread/Scheduled Task, aproveita
    a primeira requisição de qualquer pessoa logada no dia pra rodar a
    checagem -- no máximo uma vez por dia por processo (variável em memória,
    não precisa de coluna nova no banco). postar_aniversariantes_do_dia() já
    é segura de chamar de novo: só posta uma vez por pessoa por ano, graças
    ao controle ultimo_aniversario_postado_ano -- por isso não tem problema
    se rodar em mais de um processo/worker ao mesmo tempo."""
    global _ULTIMA_VERIFICACAO_ANIVERSARIOS
    hoje = datetime.now().date()
    if _ULTIMA_VERIFICACAO_ANIVERSARIOS == hoje:
        return
    _ULTIMA_VERIFICACAO_ANIVERSARIOS = hoje
    try:
        postar_aniversariantes_do_dia()
    except Exception as erro:
        print(f"Erro ao verificar aniversários (checagem por requisição): {erro}")


# Removido em 2026-09-10, a pedido do Bruno: a checagem de encerramento
# automático por requisição (verificar_encerramento_automatico_se_necessario)
# foi tirada de propósito. Ela existia como reforço porque, no dia em que
# essa área foi corrigida, a Scheduled Task do PythonAnywhere ainda não
# estava configurada -- agora que está (Daily, 21:30 UTC = 18h30 de
# Brasília, rodando tarefa_encerramento_automatico.py), ter as duas coisas
# rodando causava confusão (ficava sem saber qual delas realmente fechou
# uma sessão esquecida, e a checagem por requisição podia disparar mais
# cedo que o esperado, dependendo de quem acessasse o sistema e quando).
# Só a Task cuida disso agora -- é a única fonte, prevista e verificável.


@app.route("/turno/sair")
@login_required
@permissao_required("pausas")
def turno_sair():
    usuario = usuario_atual()
    conn = conectar()

    pausa_aberta = conn.execute("""
        SELECT * FROM pausas WHERE usuario_id=? AND data=date('now') AND fim_real IS NULL
        ORDER BY id DESC LIMIT 1
    """, (usuario["id"],)).fetchone()
    if pausa_aberta:
        conn.close()
        return redirect("/pausas?erro_pausa_aberta=1")

    conn.execute("""
        UPDATE sessoes SET logout_em=?, encerrado_por=? WHERE usuario_id=? AND logout_em IS NULL
    """, (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), usuario["nome"], usuario["id"]))
    conn.commit()
    conn.close()
    return redirect("/")


# ==========================
# PAUSAS
# ==========================

@app.route("/pausas")
@login_required
@permissao_required("pausas")
def pausas():
    usuario = usuario_atual()

    # Data consultada no histórico/timeline (pedido do Bruno, 2026-09-20) --
    # sempre HOJE por padrão, e só aceita "YYYY-MM-DD" válido (qualquer coisa
    # fora disso -- vazio, formato quebrado, tentativa de injeção -- cai pra
    # hoje, silenciosamente, sem quebrar a tela). Isso NUNCA afeta os botões
    # de ação (Iniciar Tratamento, pausas, etc.) nem os limites de horário
    # abaixo -- esses continuam sempre olhando o HOJE real, é só a área de
    # histórico/gráfico que muda com o filtro.
    data_consulta = (request.args.get("data") or "").strip()
    try:
        datetime.strptime(data_consulta, "%Y-%m-%d")
    except ValueError:
        data_consulta = hoje_str()

    conn = conectar()
    agora = datetime.now()
    aberta = pausa_aberta(conn, usuario["id"])
    historico_hoje = pausas_do_dia(conn, usuario["id"])  # sempre hoje -- usado só nos "pode_*" abaixo
    turno_aberto = conn.execute(
        "SELECT * FROM sessoes WHERE usuario_id=? AND logout_em IS NULL", (usuario["id"],)
    ).fetchone()

    # Jornada(s) e pausas do DIA CONSULTADO (história completa, respeitando o
    # filtro) -- a tela só mostrava "Em tratamento desde X" embutido enquanto
    # o turno estava aberto, e sumia assim que a pessoa encerrava o
    # tratamento, sem nenhum registro visível de quando começou/terminou.
    # Pode ter mais de uma jornada no mesmo dia (se a pessoa encerrou e
    # iniciou de novo). Pedido do Bruno, 2026-09-20.
    jornadas_consulta = conn.execute(
        "SELECT * FROM sessoes WHERE usuario_id=? AND date(login_em)=? ORDER BY id",
        (usuario["id"], data_consulta)
    ).fetchall()
    pausas_consulta = pausas_do_dia(conn, usuario["id"], data_consulta)
    conn.close()

    # Monta as linhas já com duração calculada e (pras jornadas) a origem do
    # encerramento traduzida -- feito aqui, não no template, porque envolve
    # parsing de data/hora (mais claro em Python que em Jinja).
    jornadas_consulta_exibicao = [{
        "login_em": j["login_em"],
        "logout_em": j["logout_em"],
        "duracao": _formatar_duracao(j["login_em"], j["logout_em"], data_consulta, agora),
        "origem_logout": _rotulo_encerramento(j["encerrado_por"], usuario["nome"]),
    } for j in jornadas_consulta]
    pausas_consulta_exibicao = [{
        "tipo": p["tipo"],
        "inicio": p["inicio"],
        "fim_previsto": p["fim_previsto"],
        "fim_real": p["fim_real"],
        "automatica": p["automatica"],
        "duracao": _formatar_duracao(p["inicio"], p["fim_real"], data_consulta, agora),
    } for p in pausas_consulta]
    timeline_dia = _montar_timeline_dia(jornadas_consulta, pausas_consulta, data_consulta)

    hora_atual = agora.time()
    hora_inicio_turno = None
    if turno_aberto and turno_aberto["login_em"]:
        hora_inicio_turno = datetime.strptime(turno_aberto["login_em"], "%Y-%m-%d %H:%M:%S").time()

    conn = conectar()
    limite_manha_inicio, limite_manha, almoco_inicio, limite_almoco, limite_tarde_inicio, limite_tarde = obter_limites_pausa(conn)

    pode_manha = (
        (limite_manha_inicio <= hora_atual < limite_manha)
        and not ja_fez(historico_hoje, "PAUSA_MANHA")
        and (hora_inicio_turno is None or hora_inicio_turno < time(10, 0))
    )
    pode_almoco = (
        (almoco_inicio <= hora_atual < limite_almoco)
        and not ja_fez(historico_hoje, "ALMOCO")
        and (hora_inicio_turno is None or hora_inicio_turno < time(13, 0))
    )
    pode_tarde = (
        (limite_tarde_inicio <= hora_atual < limite_tarde)
        and not ja_fez(historico_hoje, "PAUSA_TARDE")
    )
    pode_banheiro = True  # sem restrição de horário, e sem limite de vezes por dia

    duracao_pausa, duracao_almoco, duracao_banheiro = obter_duracoes(conn)
    encerramento_ativo = encerramento_automatico_ligado(conn)
    bloqueio_login_ativo_valor = bloqueio_login_ligado(conn)
    pausa_automatica_ativa_valor = pausa_automatica_ligada(conn)
    prazo_followup_horas = obter_prazo_followup(conn)
    limite_lead_nunca_contatado_dias, limite_followup_travado_horas = obter_limites_envelhecimento(conn)
    conn.close()

    return render_template(
        "Pausas.html",
        aberta=aberta,
        data_consulta=data_consulta,
        eh_hoje_consulta=(data_consulta == hoje_str()),
        jornadas_consulta=jornadas_consulta_exibicao,
        historico=pausas_consulta_exibicao,
        timeline_dia=timeline_dia,
        pode_manha=pode_manha,
        pode_almoco=pode_almoco,
        pode_tarde=pode_tarde,
        pode_banheiro=pode_banheiro,
        turno_aberto=turno_aberto,
        duracao_pausa_min=int(duracao_pausa.total_seconds() // 60),
        duracao_almoco_min=int(duracao_almoco.total_seconds() // 60),
        duracao_banheiro_min=int(duracao_banheiro.total_seconds() // 60),
        encerramento_ativo=encerramento_ativo,
        bloqueio_login_ativo=bloqueio_login_ativo_valor,
        pausa_automatica_ativa=pausa_automatica_ativa_valor,
        limite_pausa_manha_inicio=limite_manha_inicio.strftime("%H:%M"),
        limite_pausa_manha=limite_manha.strftime("%H:%M"),
        limite_almoco_inicio=almoco_inicio.strftime("%H:%M"),
        limite_almoco_fim=limite_almoco.strftime("%H:%M"),
        limite_pausa_tarde_inicio=limite_tarde_inicio.strftime("%H:%M"),
        limite_pausa_tarde=limite_tarde.strftime("%H:%M"),
        prazo_followup_horas=prazo_followup_horas,
        limite_lead_nunca_contatado_dias=limite_lead_nunca_contatado_dias,
        limite_followup_travado_horas=limite_followup_travado_horas,
    )


@app.route("/pausas/prazo_followup/salvar", methods=["POST"])
@login_required
@perfil_required("ADM")
def pausas_prazo_followup_salvar():
    try:
        prazo_horas = int(request.form.get("prazo_followup_horas", PRAZO_FOLLOWUP_HORAS_PADRAO))
        if prazo_horas <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return redirect("/pausas?erro_prazo_followup=1")

    conn = conectar()
    conn.execute("UPDATE configuracoes SET prazo_followup_horas=? WHERE id=1", (prazo_horas,))
    conn.commit()
    conn.close()
    return redirect("/pausas")


@app.route("/pausas/envelhecimento/salvar", methods=["POST"])
@login_required
@perfil_required("ADM")
def pausas_envelhecimento_salvar():
    """Salva os 2 limites do alerta de "lead envelhecendo" (Dashboard, pedido
    do Bruno em 2026-08-27) -- mesmo padrão de validação já usado em
    pausas_prazo_followup_salvar."""
    try:
        dias_nunca_contatado = int(request.form.get("limite_lead_nunca_contatado_dias", LIMITE_LEAD_NUNCA_CONTATADO_DIAS_PADRAO))
        horas_followup = int(request.form.get("limite_followup_travado_horas", LIMITE_FOLLOWUP_TRAVADO_HORAS_PADRAO))
        if dias_nunca_contatado <= 0 or horas_followup <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return redirect("/pausas?erro_envelhecimento=1")

    conn = conectar()
    conn.execute(
        "UPDATE configuracoes SET limite_lead_nunca_contatado_dias=?, limite_followup_travado_horas=? WHERE id=1",
        (dias_nunca_contatado, horas_followup)
    )
    conn.commit()
    conn.close()
    return redirect("/pausas")


@app.route("/pausas/encerramento_automatico/alternar", methods=["POST"])
@login_required
@perfil_required("ADM")
def pausas_encerramento_automatico_alternar():
    conn = conectar()
    conn.execute("""
        UPDATE configuracoes SET encerramento_automatico_ativo = 1 - COALESCE(encerramento_automatico_ativo, 1)
        WHERE id=1
    """)
    conn.commit()
    conn.close()
    return redirect("/pausas")


@app.route("/pausas/bloqueio_login/alternar", methods=["POST"])
@login_required
@perfil_required("ADM")
def pausas_bloqueio_login_alternar():
    conn = conectar()
    conn.execute("""
        UPDATE configuracoes SET bloqueio_login_ativo = 1 - COALESCE(bloqueio_login_ativo, 1)
        WHERE id=1
    """)
    conn.commit()
    conn.close()
    return redirect("/pausas")


@app.route("/pausas/pausa_automatica/alternar", methods=["POST"])
@login_required
@perfil_required("ADM")
def pausas_pausa_automatica_alternar():
    conn = conectar()
    conn.execute("""
        UPDATE configuracoes SET pausa_automatica_ativa = 1 - COALESCE(pausa_automatica_ativa, 1)
        WHERE id=1
    """)
    conn.commit()
    conn.close()
    return redirect("/pausas")


@app.route("/pausas/duracao/salvar", methods=["POST"])
@login_required
@perfil_required("ADM")
def pausas_duracao_salvar():
    try:
        pausa_min = max(1, int(request.form.get("duracao_pausa_min", 10)))
        almoco_min = max(1, int(request.form.get("duracao_almoco_min", 60)))
        banheiro_min = max(1, int(request.form.get("duracao_banheiro_min", 10)))
    except (TypeError, ValueError):
        return redirect("/pausas")

    conn = conectar()
    conn.execute(
        "UPDATE configuracoes SET duracao_pausa_min=?, duracao_almoco_min=?, duracao_banheiro_min=? WHERE id=1",
        (pausa_min, almoco_min, banheiro_min)
    )
    conn.commit()
    conn.close()
    return redirect("/pausas")


@app.route("/pausas/limites/salvar", methods=["POST"])
@login_required
@perfil_required("ADM")
def pausas_limites_salvar():
    def _valido(valor):
        try:
            datetime.strptime(valor, "%H:%M")
            return True
        except (TypeError, ValueError):
            return False

    campos = {
        "limite_pausa_manha_inicio": request.form.get("limite_pausa_manha_inicio", ""),
        "limite_pausa_manha": request.form.get("limite_pausa_manha", ""),
        "limite_almoco_inicio": request.form.get("limite_almoco_inicio", ""),
        "limite_almoco_fim": request.form.get("limite_almoco_fim", ""),
        "limite_pausa_tarde_inicio": request.form.get("limite_pausa_tarde_inicio", ""),
        "limite_pausa_tarde": request.form.get("limite_pausa_tarde", ""),
    }
    if not all(_valido(v) for v in campos.values()):
        return redirect("/pausas?erro_limites=1")

    conn = conectar()
    conn.execute("""
        UPDATE configuracoes
        SET limite_pausa_manha_inicio=?, limite_pausa_manha=?, limite_almoco_inicio=?, limite_almoco_fim=?,
            limite_pausa_tarde_inicio=?, limite_pausa_tarde=?
        WHERE id=1
    """, (campos["limite_pausa_manha_inicio"], campos["limite_pausa_manha"], campos["limite_almoco_inicio"],
          campos["limite_almoco_fim"], campos["limite_pausa_tarde_inicio"], campos["limite_pausa_tarde"]))
    conn.commit()
    conn.close()
    return redirect("/pausas")


@app.route("/pausas/iniciar", methods=["POST"])
@login_required
@permissao_required("pausas")
def pausas_iniciar():
    usuario = usuario_atual()
    tipo = request.form.get("tipo")
    conn = conectar()
    if not pausa_aberta(conn, usuario["id"]) and tipo in ("PAUSA_MANHA", "ALMOCO", "PAUSA_TARDE", "BANHEIRO"):
        iniciar_pausa(conn, usuario["id"], tipo, datetime.now(), automatica=False)
    conn.close()
    return redirect("/pausas")


@app.route("/pausas/finalizar", methods=["POST"])
@login_required
@permissao_required("pausas")
def pausas_finalizar():
    usuario = usuario_atual()
    conn = conectar()
    aberta = pausa_aberta(conn, usuario["id"])
    if aberta:
        conn.execute(
            "UPDATE pausas SET fim_real=? WHERE id=?",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), aberta["id"])
        )
        conn.commit()
    conn.close()
    return redirect("/pausas")


# ==========================
# SUPERVISÃO
# ==========================

# Registro de falta (2026-08-27, a pedido do Bruno): até aqui só dava pra
# registrar a falta de alguém no PRÓPRIO dia (o botão só aparecia quando o
# status calculado na hora já era "Ausente", e a rota gravava a data como
# hoje, sem receber nenhuma data) -- se o supervisor esquecesse, no dia
# seguinte não tinha mais por onde fazer esse registro. Duas mudanças: o
# registro de falta passou a aceitar uma data (ou um intervalo de datas, pra
# cobrir de uma vez um atestado de vários dias) escolhida pelo supervisor, com
# uma janela retroativa limitada (não trava data futura -- útil quando o
# operador já avisa hoje que vai faltar nos próximos dias); e a tela de
# Supervisão passou a mostrar sozinha quem ficou "pendente" de registro (foi
# escalado, não logou e não tem falta registrada), pra não depender da
# memória do supervisor.
LIMITE_DIAS_RETROATIVOS_AUSENCIA = 30  # não deixa registrar falta de mais de 30 dias atrás
LIMITE_DIAS_INTERVALO_AUSENCIA = 60  # trava de sanidade: no máximo 60 dias num único registro (evita erro de digitação criar um intervalo absurdo)


def em_ferias(escala, agora):
    if not escala or not escala["ferias_inicio"] or not escala["ferias_fim"]:
        return False
    hoje = agora.strftime("%Y-%m-%d")
    return escala["ferias_inicio"] <= hoje <= escala["ferias_fim"]


def status_operador(conn, usuario, agora):
    if not usuario["ativo"]:
        return {
            "usuario": usuario,
            "status": "Desligado",
            "cor": "cinza",
            "logado": False,
            "deveria": False,
            "pausa_excedida": False,
            "ausente": False,
        }

    escala = obter_escala(conn, usuario["id"])

    if em_ferias(escala, agora):
        return {
            "usuario": usuario,
            "status": f"Férias (até {escala['ferias_fim']})",
            "cor": "azul",
            "logado": False,
            "deveria": False,
            "pausa_excedida": False,
            "ausente": False,
        }

    deveria = dentro_da_escala(escala, agora)

    sessao_aberta = conn.execute(
        "SELECT * FROM sessoes WHERE usuario_id=? AND logout_em IS NULL", (usuario["id"],)
    ).fetchone()
    logado = sessao_aberta is not None

    ausente = False
    if logado and deveria:
        status = "Logado (normal)"
        cor = "verde"
    elif not logado and deveria:
        status = "Ausente (deveria estar logado)"
        cor = "vermelho"
        ausente = True
    elif logado and not deveria:
        status = "Logado fora da escala"
        cor = "amarelo"
    else:
        status = "Fora de escala / Folga"
        cor = "cinza"

    aberta = pausa_aberta(conn, usuario["id"])
    pausa_excedida = False
    if aberta:
        fim_previsto = datetime.strptime(aberta["fim_previsto"], "%Y-%m-%d %H:%M:%S")
        if agora > fim_previsto:
            pausa_excedida = True
            status = f"Pausa excedida ({aberta['tipo']})"
            cor = "vermelho"
        else:
            status = f"Em pausa ({aberta['tipo']})"
            cor = "azul"

    return {
        "usuario": usuario,
        "status": status,
        "cor": cor,
        "logado": logado,
        "deveria": deveria,
        "ausente": ausente,
        "pausa_excedida": pausa_excedida,
    }


def _pendencias_ausencia(conn, data_de, data_ate):
    """Dias no intervalo [data_de, data_ate] (nunca inclui hoje, que ainda
    está em andamento -- quem chama já deve travar isso) em que alguém era
    escalado, não tem nenhum login registrado naquele dia e também não tem
    falta registrada -- ou seja, ficou sem nenhum tipo de registro. Mesma
    lógica já usada em calcular_grafico_escala_presenca() (o gráfico de
    escala x presença do Dashboard), só que aqui devolve cada pendência
    individual (pessoa + dia) em vez de só a contagem por dia. Um dia só conta
    como pendência se for IGUAL OU POSTERIOR ao cadastro da pessoa no sistema
    (usuarios.data_cadastro) -- escala configurada antes de a pessoa
    existir/começar a ser rastreada no CRM não é falta nenhuma, só ruído
    (foi exatamente o que inundou a lista durante o período de testes). O
    próprio dia do cadastro CONTA (2026-08-27: corrigido depois de um caso
    real -- cadastro em lote de operador novo feito à tarde, no mesmo dia
    em que a escala já previa presença de manhã; excluir esse dia também
    escondia uma pendência real do próprio dia em que a pessoa começou)."""
    escalas_usuarios = conn.execute("""
        SELECT e.usuario_id, u.nome, u.data_cadastro, e.dias_trabalho, e.ferias_inicio, e.ferias_fim
        FROM escalas e JOIN usuarios u ON u.id = e.usuario_id
        WHERE u.ativo = 1 AND COALESCE(u.excluido, 0) = 0
    """).fetchall()
    if not escalas_usuarios:
        return []

    ausencias_por_dia = {}
    for l in conn.execute(
        "SELECT data, usuario_id FROM ausencias WHERE data BETWEEN ? AND ?", (data_de, data_ate)
    ).fetchall():
        ausencias_por_dia.setdefault(l["data"], set()).add(l["usuario_id"])

    logins_por_dia = {}
    for l in conn.execute(
        "SELECT date(login_em) as dia, usuario_id FROM sessoes WHERE date(login_em) BETWEEN ? AND ?",
        (data_de, data_ate)
    ).fetchall():
        logins_por_dia.setdefault(l["dia"], set()).add(l["usuario_id"])

    pendencias = []
    dia_atual = datetime.strptime(data_de, "%Y-%m-%d")
    dia_final = datetime.strptime(data_ate, "%Y-%m-%d")
    while dia_atual <= dia_final:
        dia_str = dia_atual.strftime("%Y-%m-%d")
        abrev = DIAS_SEMANA[dia_atual.weekday()]
        ausentes_no_dia = ausencias_por_dia.get(dia_str, set())
        logaram_no_dia = logins_por_dia.get(dia_str, set())
        for e in escalas_usuarios:
            if abrev not in (e["dias_trabalho"] or "").split(","):
                continue
            if e["ferias_inicio"] and e["ferias_fim"] and e["ferias_inicio"] <= dia_str <= e["ferias_fim"]:
                continue
            cadastro_data = (e["data_cadastro"] or "")[:10]
            if cadastro_data and dia_str < cadastro_data:
                continue
            if e["usuario_id"] in logaram_no_dia or e["usuario_id"] in ausentes_no_dia:
                continue
            pendencias.append({"usuario_id": e["usuario_id"], "nome": e["nome"], "data": dia_str})
        dia_atual += timedelta(days=1)

    pendencias.sort(key=lambda p: p["data"], reverse=True)
    return pendencias


@app.route("/supervisao")
@login_required
@permissao_required("supervisao")
def supervisao():
    conn = conectar()
    agora = datetime.now()
    hoje = agora.strftime("%Y-%m-%d")
    usuarios = conn.execute(
        "SELECT * FROM usuarios WHERE perfil IN ('OPERADOR', 'ANALISTA') AND COALESCE(excluido,0)=0 ORDER BY ativo DESC, nome"
    ).fetchall()
    status_list = [status_operador(conn, u, agora) for u in usuarios]

    ausencias_hoje = {}
    for a in conn.execute("SELECT * FROM ausencias WHERE data=? ORDER BY id DESC", (hoje,)).fetchall():
        if a["usuario_id"] not in ausencias_hoje:
            ausencias_hoje[a["usuario_id"]] = a

    conn.close()

    # Pra pré-preencher o modal de edição com o que já está registrado hoje
    # (em vez de abrir em branco por cima de um registro existente).
    ausencias_hoje_js = {
        str(usuario_id): {
            "motivo": a["motivo"] or "",
            "submotivo": a["submotivo"] or "",
            "observacao": a["observacao"] or "",
        }
        for usuario_id, a in ausencias_hoje.items()
    }

    # Contagens do resumo (separadas por status real, não só pela cor --
    # vermelho cobre "Ausente" e "Pausa excedida" ao mesmo tempo, e azul cobre
    # "Em pausa" e "Férias" -- por isso checa o texto do status, não a cor)
    resumo = {
        "em_atendimento": sum(1 for s in status_list if s["status"].startswith("Logado (normal)")),
        "ausentes": sum(1 for s in status_list if s["status"].startswith("Ausente")),
        "pausa_excedida": sum(1 for s in status_list if s["status"].startswith("Pausa excedida")),
        "em_pausa": sum(1 for s in status_list if s["status"].startswith("Em pausa")),
        "ferias": sum(1 for s in status_list if s["status"].startswith("Férias")),
        "fora_escala": sum(1 for s in status_list if s["status"].startswith("Logado fora da escala")),
        "folga": sum(1 for s in status_list if s["status"].startswith("Fora de escala / Folga")),
    }

    data_minima_ausencia = (agora - timedelta(days=LIMITE_DIAS_RETROATIVOS_AUSENCIA)).strftime("%Y-%m-%d")
    conn_motivos = conectar()
    motivos_ausencia_extras = [m["nome"] for m in obter_motivos_ausencia_customizados(conn_motivos)]
    conn_motivos.close()

    return render_template(
        "Supervisao.html", status_list=status_list, usuario_logado_id=session.get("usuario_id"),
        ausencias_hoje=ausencias_hoje, ausencias_hoje_js=ausencias_hoje_js, hoje=hoje,
        data_minima_ausencia=data_minima_ausencia, motivos_ausencia_extras=motivos_ausencia_extras,
        resumo=resumo,
    )


@app.route("/supervisao/faltas")
@login_required
@permissao_required("supervisao")
def supervisao_faltas():
    """Página própria (2026-08-27, a pedido do Bruno) pra gerenciar pendências
    de registro de falta -- tirada de dentro da tela de Supervisão (que é de
    status em tempo real) porque a lista de pendências, sem filtro, virou uma
    enxurrada de registros durante o período de testes (escalas configuradas
    antes de haver uso real do sistema). Aqui o supervisor escolhe o período
    que quer revisar, em vez de ver a janela inteira de uma vez."""
    hoje_dt = datetime.now()
    ontem = (hoje_dt - timedelta(days=1)).strftime("%Y-%m-%d")
    data_minima = (hoje_dt - timedelta(days=LIMITE_DIAS_RETROATIVOS_AUSENCIA)).strftime("%Y-%m-%d")

    data_de = request.args.get("data_de") or ontem
    data_ate = request.args.get("data_ate") or ontem
    # Trava pros mesmos limites que o registro em si aceita -- nunca teria
    # como agir numa pendência fora dessa janela mesmo.
    if data_de < data_minima:
        data_de = data_minima
    if data_ate > ontem:
        data_ate = ontem
    if data_de > data_ate:
        data_de = data_ate

    conn = conectar()
    pendencias = _pendencias_ausencia(conn, data_de, data_ate)
    motivos_ausencia_extras = [m["nome"] for m in obter_motivos_ausencia_customizados(conn)]
    conn.close()

    return render_template(
        "SupervisaoFaltas.html", pendencias=pendencias, total=len(pendencias),
        data_de=data_de, data_ate=data_ate, data_minima=data_minima, data_maxima=ontem,
        motivos_ausencia_extras=motivos_ausencia_extras,
    )


@app.route("/supervisao/ausencia/<int:usuario_id>", methods=["POST"])
@login_required
@permissao_required("supervisao")
def supervisao_ausencia(usuario_id):
    supervisor = usuario_atual()
    motivo = request.form.get("motivo")
    submotivo = request.form.get("submotivo") or None
    observacao = request.form.get("observacao") or ""
    data_de = request.form.get("data_de") or datetime.now().strftime("%Y-%m-%d")
    data_ate = request.form.get("data_ate") or data_de
    voltar_para = request.form.get("voltar_para") or "/supervisao"

    conn_checagem = conectar()
    motivos_customizados_ativos = {m["nome"] for m in obter_motivos_ausencia_customizados(conn_checagem)}
    conn_checagem.close()
    if motivo not in ("Falta Justificada", "Injustificada", "Treinamento", "Férias", "Afastamento") and motivo not in motivos_customizados_ativos:
        return "Motivo inválido.", 400
    if motivo != "Falta Justificada":
        submotivo = None
    if submotivo == "Outros" and not observacao.strip():
        return "Observação é obrigatória quando o submotivo é 'Outros'.", 400

    try:
        dt_de = datetime.strptime(data_de, "%Y-%m-%d")
        dt_ate = datetime.strptime(data_ate, "%Y-%m-%d")
    except ValueError:
        return "Data inválida.", 400
    if dt_de > dt_ate:
        return "A data 'De' não pode ser depois da data 'Até'.", 400
    if (dt_ate - dt_de).days + 1 > LIMITE_DIAS_INTERVALO_AUSENCIA:
        return f"Intervalo muito longo (máximo {LIMITE_DIAS_INTERVALO_AUSENCIA} dias por registro).", 400
    limite_retroativo = datetime.now() - timedelta(days=LIMITE_DIAS_RETROATIVOS_AUSENCIA)
    if dt_de.date() < limite_retroativo.date():
        return (
            f"Não é possível registrar falta de mais de {LIMITE_DIAS_RETROATIVOS_AUSENCIA} dias atrás.",
            400,
        )

    conn = conectar()
    alvo = conn.execute("SELECT data_cadastro FROM usuarios WHERE id=?", (usuario_id,)).fetchone()
    # A falta só existe pra ser registrada depois (ou no próprio dia) de a
    # pessoa existir no sistema -- escala configurada antes do cadastro não
    # conta como falta (pedido do Bruno, 2026-08-27, pra cortar o ruído do
    # período de testes). O próprio dia do cadastro é aceito: corrigido no
    # mesmo dia depois de um caso real em que o cadastro em lote de um
    # operador novo foi feito à tarde, no mesmo dia em que a escala já
    # previa presença de manhã -- excluir o dia do cadastro escondia uma
    # falta real do primeiro dia da pessoa.
    cadastro_data = (alvo["data_cadastro"] or "")[:10] if alvo else ""
    if cadastro_data and data_de < cadastro_data:
        conn.close()
        return (
            f"Não é possível registrar falta anterior ao cadastro da pessoa "
            f"no sistema ({cadastro_data[8:10]}/{cadastro_data[5:7]}/{cadastro_data[0:4]}).",
            400,
        )

    agora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    dia_atual = dt_de
    while dia_atual <= dt_ate:
        dia_str = dia_atual.strftime("%Y-%m-%d")
        # Apaga qualquer registro anterior desse dia antes de gravar o novo --
        # sem isso, "editar" uma falta já registrada só empilhava outra linha
        # em cima (a tela sempre mostrava a mais recente, mas contagens que
        # somam ausências por COUNT(*) ficavam infladas com o registro duplicado).
        conn.execute("DELETE FROM ausencias WHERE usuario_id=? AND data=?", (usuario_id, dia_str))
        conn.execute("""
            INSERT INTO ausencias (usuario_id, data, motivo, submotivo, observacao, registrado_por, data_registro)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (usuario_id, dia_str, motivo, submotivo, observacao, supervisor["nome"], agora_str))
        dia_atual += timedelta(days=1)

    # Se a ausência registrada (Férias ou Afastamento) cobre o dia de HOJE,
    # solta na hora qualquer contrato que a pessoa esteja com EM_ANDAMENTO --
    # ver _liberar_em_andamento_por_ausencia. Sem timeout de 24h (removido
    # em buscar_proximo_cliente, pedido do Bruno 2026-09-21), essa é a
    # única forma de esse contrato sair da mão dela nesse caso -- registrar
    # a falta sozinho não soltava nada antes disso.
    hoje_str = datetime.now().strftime("%Y-%m-%d")
    if motivo in ("Férias", "Afastamento") and data_de <= hoje_str <= data_ate:
        _liberar_em_andamento_por_ausencia(conn, usuario_id, agora_str)

    conn.commit()
    conn.close()
    return redirect(voltar_para)


# ==========================
# FALTAS LANÇADAS -- editar / excluir (pedido do Bruno, 2026-10-06)
# ==========================
# Caso real que motivou: supervisora lançou Férias pra pessoa errada e não
# havia como desfazer pela tela -- o modal da Supervisão só edita o dia de
# HOJE e só da própria pessoa. Aqui o supervisor vê os lançamentos já
# gravados (passados, hoje e futuros -- Férias costuma ir pra frente) e pode
# editar (inclusive trocar a pessoa) ou excluir, no período inteiro do
# lançamento ou só em parte dele. Mesma trava de 30 dias pra trás do registro.
# Toda alteração fica em ausencias_ajustes (antes/depois + quem + quando).

def _garantir_tabela_ausencias_ajustes():
    """Auto-migração: trilha de auditoria das edições/exclusões de falta."""
    conn = conectar()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ausencias_ajustes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                acao TEXT,               -- 'EDITAR' ou 'EXCLUIR'
                ausencia_id INTEGER,
                data TEXT,               -- dia da falta afetado
                antes TEXT,              -- JSON do registro antes
                depois TEXT,             -- JSON do registro depois (NULL na exclusão)
                feito_por TEXT,
                feito_em TEXT
            )
        """)
        conn.commit()
    except sqlite3.OperationalError:
        pass
    conn.close()


_garantir_tabela_ausencias_ajustes()

MOTIVOS_AUSENCIA_FIXOS = ("Falta Justificada", "Injustificada", "Treinamento", "Férias", "Afastamento")


def _agrupar_lancamentos_ausencia(linhas):
    """Junta as linhas de `ausencias` (uma por dia) no lançamento que as
    gerou: mesma pessoa + mesmo envio (registrado_por/data_registro) + mesmo
    motivo/submotivo/observação, em dias consecutivos. Um lançamento de
    Férias de 15 dias aparece como 1 linha só, não 15."""
    def chave(l):
        return (l["usuario_id"], l["registrado_por"] or "", l["data_registro"] or "",
                l["motivo"] or "", l["submotivo"] or "", l["observacao"] or "")

    grupos = []
    atual = None
    for l in sorted(linhas, key=lambda l: (chave(l), l["data"])):
        dia = datetime.strptime(l["data"], "%Y-%m-%d")
        if atual and atual["_chave"] == chave(l) and (dia - atual["_ultimo"]).days == 1:
            atual["ids"].append(l["id"])
            atual["data_ate"] = l["data"]
            atual["_ultimo"] = dia
            atual["dias"] += 1
            continue
        atual = {
            "_chave": chave(l), "_ultimo": dia, "ids": [l["id"]], "dias": 1,
            "usuario_id": l["usuario_id"], "nome": l["nome"] or f"(usuário {l['usuario_id']})",
            "data_de": l["data"], "data_ate": l["data"], "motivo": l["motivo"] or "",
            "submotivo": l["submotivo"] or "", "observacao": l["observacao"] or "",
            "registrado_por": l["registrado_por"] or "", "data_registro": l["data_registro"] or "",
        }
        grupos.append(atual)
    for g in grupos:
        g.pop("_chave"); g.pop("_ultimo")
        g["ids_str"] = ",".join(str(i) for i in g["ids"])
    grupos.sort(key=lambda g: (g["data_de"], g["nome"]), reverse=True)
    return grupos


def _ausencia_como_dict(linha):
    return {k: linha[k] for k in ("id", "usuario_id", "data", "motivo", "submotivo", "observacao",
                                  "registrado_por", "data_registro")}


def _carregar_linhas_ajuste_ausencia(conn):
    """Lê do form os ids do lançamento + o recorte De/Até e devolve
    (linhas_afetadas, erro). O recorte precisa estar dentro do lançamento e
    respeitar o limite retroativo."""
    try:
        ids = [int(i) for i in (request.form.get("ids") or "").split(",") if i.strip()]
    except ValueError:
        return None, "Lançamento inválido."
    if not ids:
        return None, "Lançamento inválido."
    data_de = request.form.get("data_de") or ""
    data_ate = request.form.get("data_ate") or data_de
    try:
        dt_de = datetime.strptime(data_de, "%Y-%m-%d")
        datetime.strptime(data_ate, "%Y-%m-%d")
    except ValueError:
        return None, "Data inválida."
    if data_de > data_ate:
        return None, "A data 'De' não pode ser depois da data 'Até'."
    limite = (datetime.now() - timedelta(days=LIMITE_DIAS_RETROATIVOS_AUSENCIA)).date()
    if dt_de.date() < limite:
        return None, (f"Não é possível alterar falta de mais de {LIMITE_DIAS_RETROATIVOS_AUSENCIA} "
                      f"dias atrás.")

    marcadores = ",".join("?" * len(ids))
    todas = conn.execute(f"SELECT * FROM ausencias WHERE id IN ({marcadores})", ids).fetchall()
    if len(todas) != len(ids):
        return None, "Esse lançamento foi alterado por outra pessoa enquanto a tela estava aberta. Atualize a página."
    if data_de < min(l["data"] for l in todas) or data_ate > max(l["data"] for l in todas):
        return None, "O período escolhido precisa estar dentro do período do lançamento."
    afetadas = [l for l in todas if data_de <= l["data"] <= data_ate]
    if not afetadas:
        return None, "Nenhum dia do lançamento dentro do período escolhido."
    return afetadas, None


def _voltar_faltas_lancadas(msg=None, erro=None):
    destino = request.form.get("voltar_para") or "/supervisao/faltas-lancadas"
    if not destino.startswith("/supervisao/faltas-lancadas"):
        destino = "/supervisao/faltas-lancadas"
    if msg:
        flash(msg, "success")
    if erro:
        flash(erro, "danger")
    return redirect(destino)


@app.route("/supervisao/faltas-lancadas")
@login_required
@permissao_required("supervisao")
def supervisao_faltas_lancadas():
    agora = datetime.now()
    hoje = agora.strftime("%Y-%m-%d")
    data_minima = (agora - timedelta(days=LIMITE_DIAS_RETROATIVOS_AUSENCIA)).strftime("%Y-%m-%d")
    data_de = request.args.get("data_de") or (agora - timedelta(days=7)).strftime("%Y-%m-%d")
    data_ate = request.args.get("data_ate") or (agora + timedelta(days=60)).strftime("%Y-%m-%d")
    usuario_filtro = request.args.get("usuario_id", type=int)
    if data_de < data_minima:
        data_de = data_minima
    if data_de > data_ate:
        data_ate = data_de

    conn = conectar()
    filtro_sql, params = "", [data_de, data_ate]
    if usuario_filtro:
        filtro_sql, params = " AND a.usuario_id = ?", params + [usuario_filtro]
    linhas = conn.execute(f"""
        SELECT a.*, u.nome FROM ausencias a LEFT JOIN usuarios u ON u.id = a.usuario_id
        WHERE a.data BETWEEN ? AND ?{filtro_sql}
    """, params).fetchall()
    lancamentos = _agrupar_lancamentos_ausencia(linhas)

    # Pessoas que podem receber falta (mesmo universo da tela de Supervisão)
    pessoas = conn.execute("""
        SELECT id, nome, data_cadastro FROM usuarios
        WHERE perfil IN ('OPERADOR', 'ANALISTA') AND COALESCE(excluido,0)=0 ORDER BY ativo DESC, nome
    """).fetchall()

    ajustes = []
    for a in conn.execute("SELECT * FROM ausencias_ajustes ORDER BY id DESC LIMIT 300").fetchall():
        antes = json.loads(a["antes"]) if a["antes"] else {}
        depois = json.loads(a["depois"]) if a["depois"] else None
        ajustes.append({"acao": a["acao"], "data": a["data"], "antes": antes, "depois": depois,
                        "feito_por": a["feito_por"], "feito_em": a["feito_em"]})
    nomes = {p["id"]: p["nome"] for p in conn.execute("SELECT id, nome FROM usuarios").fetchall()}
    motivos_ausencia_extras = [m["nome"] for m in obter_motivos_ausencia_customizados(conn)]
    conn.close()

    # Junta os ajustes do mesmo clique (mesma ação/quem/quando/pessoa/motivo)
    # numa linha só, com o período, pra o histórico não virar uma lista de dias.
    historico = []
    for a in ajustes:
        antes, depois = a["antes"], a["depois"]
        resumo_antes = f"{nomes.get(antes.get('usuario_id'), '?')} · {antes.get('motivo') or ''}"
        if antes.get("submotivo"):
            resumo_antes += f" ({antes['submotivo']})"
        resumo_depois = None
        if depois:
            resumo_depois = f"{nomes.get(depois.get('usuario_id'), '?')} · {depois.get('motivo') or ''}"
            if depois.get("submotivo"):
                resumo_depois += f" ({depois['submotivo']})"
        chave = (a["acao"], a["feito_por"], a["feito_em"], resumo_antes, resumo_depois)
        if historico and historico[-1]["_chave"] == chave:
            historico[-1]["data_de"] = min(historico[-1]["data_de"], a["data"])
            historico[-1]["data_ate"] = max(historico[-1]["data_ate"], a["data"])
            historico[-1]["dias"] += 1
            continue
        historico.append({"_chave": chave, "acao": a["acao"], "feito_por": a["feito_por"],
                          "feito_em": a["feito_em"], "antes": resumo_antes, "depois": resumo_depois,
                          "data_de": a["data"], "data_ate": a["data"], "dias": 1})
    historico = historico[:30]

    return render_template(
        "SupervisaoFaltasLancadas.html", lancamentos=lancamentos, pessoas=pessoas,
        data_de=data_de, data_ate=data_ate, data_minima=data_minima, hoje=hoje,
        usuario_filtro=usuario_filtro, motivos_ausencia_extras=motivos_ausencia_extras,
        historico=historico,
    )


@app.route("/supervisao/faltas-lancadas/excluir", methods=["POST"])
@login_required
@permissao_required("supervisao")
def supervisao_faltas_lancadas_excluir():
    supervisor = usuario_atual()
    conn = conectar()
    afetadas, erro = _carregar_linhas_ajuste_ausencia(conn)
    if erro:
        conn.close()
        return _voltar_faltas_lancadas(erro=erro)
    agora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for l in afetadas:
        conn.execute("""
            INSERT INTO ausencias_ajustes (acao, ausencia_id, data, antes, depois, feito_por, feito_em)
            VALUES ('EXCLUIR', ?, ?, ?, NULL, ?, ?)
        """, (l["id"], l["data"], json.dumps(_ausencia_como_dict(l), ensure_ascii=False),
              supervisor["nome"], agora_str))
        conn.execute("DELETE FROM ausencias WHERE id=?", (l["id"],))
    conn.commit()
    conn.close()
    n = len(afetadas)
    return _voltar_faltas_lancadas(msg=f"{n} dia{'s' if n != 1 else ''} de falta excluído{'s' if n != 1 else ''}.")


@app.route("/supervisao/faltas-lancadas/editar", methods=["POST"])
@login_required
@permissao_required("supervisao")
def supervisao_faltas_lancadas_editar():
    supervisor = usuario_atual()
    motivo = request.form.get("motivo")
    submotivo = request.form.get("submotivo") or None
    observacao = request.form.get("observacao") or ""
    novo_usuario_id = request.form.get("usuario_id", type=int)

    conn = conectar()
    motivos_customizados_ativos = {m["nome"] for m in obter_motivos_ausencia_customizados(conn)}
    if motivo not in MOTIVOS_AUSENCIA_FIXOS and motivo not in motivos_customizados_ativos:
        conn.close()
        return _voltar_faltas_lancadas(erro="Motivo inválido.")
    if motivo != "Falta Justificada":
        submotivo = None
    if submotivo == "Outros" and not observacao.strip():
        conn.close()
        return _voltar_faltas_lancadas(erro="Observação é obrigatória quando o submotivo é 'Outros'.")

    afetadas, erro = _carregar_linhas_ajuste_ausencia(conn)
    if erro:
        conn.close()
        return _voltar_faltas_lancadas(erro=erro)

    alvo = conn.execute(
        "SELECT id, nome, data_cadastro FROM usuarios WHERE id=? AND COALESCE(excluido,0)=0",
        (novo_usuario_id,)
    ).fetchone()
    if not alvo:
        conn.close()
        return _voltar_faltas_lancadas(erro="Pessoa inválida.")
    # Mesma regra do registro: não existe falta antes do cadastro da pessoa
    cadastro_data = (alvo["data_cadastro"] or "")[:10]
    primeiro_dia = min(l["data"] for l in afetadas)
    if cadastro_data and primeiro_dia < cadastro_data:
        conn.close()
        return _voltar_faltas_lancadas(erro=(
            f"{alvo['nome']} foi cadastrada no sistema em "
            f"{cadastro_data[8:10]}/{cadastro_data[5:7]}/{cadastro_data[0:4]} -- não dá pra lançar falta antes disso."
        ))

    agora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for l in afetadas:
        # Trocando de pessoa: se a pessoa nova já tinha falta nesse dia, ela é
        # substituída (mesma regra do registro normal -- uma falta por pessoa
        # por dia), e a substituição também vai pra auditoria.
        if alvo["id"] != l["usuario_id"]:
            for antiga in conn.execute(
                "SELECT * FROM ausencias WHERE usuario_id=? AND data=? AND id<>?", (alvo["id"], l["data"], l["id"])
            ).fetchall():
                conn.execute("""
                    INSERT INTO ausencias_ajustes (acao, ausencia_id, data, antes, depois, feito_por, feito_em)
                    VALUES ('EXCLUIR', ?, ?, ?, NULL, ?, ?)
                """, (antiga["id"], antiga["data"], json.dumps(_ausencia_como_dict(antiga), ensure_ascii=False),
                      supervisor["nome"], agora_str))
                conn.execute("DELETE FROM ausencias WHERE id=?", (antiga["id"],))
        depois = {
            "id": l["id"], "usuario_id": alvo["id"], "data": l["data"], "motivo": motivo,
            "submotivo": submotivo, "observacao": observacao,
            "registrado_por": supervisor["nome"], "data_registro": agora_str,
        }
        conn.execute("""
            INSERT INTO ausencias_ajustes (acao, ausencia_id, data, antes, depois, feito_por, feito_em)
            VALUES ('EDITAR', ?, ?, ?, ?, ?, ?)
        """, (l["id"], l["data"], json.dumps(_ausencia_como_dict(l), ensure_ascii=False),
              json.dumps(depois, ensure_ascii=False), supervisor["nome"], agora_str))
        conn.execute("""
            UPDATE ausencias SET usuario_id=?, motivo=?, submotivo=?, observacao=?, registrado_por=?, data_registro=?
            WHERE id=?
        """, (alvo["id"], motivo, submotivo, observacao, supervisor["nome"], agora_str, l["id"]))

    # Mesma regra do registro: Férias/Afastamento cobrindo HOJE solta na hora
    # o que a pessoa (a nova, se trocou) estiver com EM_ANDAMENTO.
    hoje_str = datetime.now().strftime("%Y-%m-%d")
    if motivo in ("Férias", "Afastamento") and any(l["data"] == hoje_str for l in afetadas):
        _liberar_em_andamento_por_ausencia(conn, alvo["id"], agora_str)

    conn.commit()
    conn.close()
    n = len(afetadas)
    return _voltar_faltas_lancadas(msg=f"{n} dia{'s' if n != 1 else ''} de falta atualizado{'s' if n != 1 else ''}.")


def _encerrar_sessao_e_redistribuir_tarefas(conn, alvo, agora_str):
    """Passo comum entre Desligar e Excluir: encerra turno/pausa em aberto e
    devolve as tarefas em andamento (mailing + followups) dessa pessoa pra
    fila geral. Não mexe em nenhum histórico (atividades, monitorias, etc.)
    -- só solta o que ainda estava em aberto na hora."""
    usuario_id = alvo["id"]
    conn.execute("UPDATE sessoes SET logout_em=?, encerrado_por='DESLIGAMENTO' WHERE usuario_id=? AND logout_em IS NULL", (agora_str, usuario_id))
    conn.execute("UPDATE pausas SET fim_real=? WHERE usuario_id=? AND fim_real IS NULL", (agora_str, usuario_id))

    # Redireciona as tarefas em andamento dessa pessoa de volta pra fila, guardando quem tratava
    # (se ela for reativada -- caso de Desligar --, a preferência de retorno em
    # buscar_proximo_cliente ainda funciona; no caso de Excluir não há reativação,
    # mas o contrato precisa voltar pra fila do mesmo jeito)
    conn.execute("""
        UPDATE mailing SET status='PENDENTE', operador=NULL, ultimo_operador=?, data_entrada_fila=?
        WHERE status='EM_ANDAMENTO' AND operador=?
    """, (alvo["nome"], agora_str, alvo["nome"]))

    # Solta os followups dela pra fila geral (qualquer operador pode puxar agora)
    conn.execute("""
        UPDATE followup SET operador=NULL
        WHERE status='PENDENTE' AND operador=?
    """, (alvo["nome"],))


@app.route("/supervisao/desligar/<int:usuario_id>", methods=["POST"])
@login_required
@permissao_required("supervisao")
def supervisao_desligar(usuario_id):
    usuario_logado = usuario_atual()
    if usuario_id == usuario_logado["id"]:
        return "Você não pode desligar o próprio usuário.", 400

    conn = conectar()
    alvo = conn.execute("SELECT * FROM usuarios WHERE id=?", (usuario_id,)).fetchone()
    if not alvo:
        conn.close()
        return redirect("/supervisao")

    agora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("UPDATE usuarios SET ativo=0, desativado_em=? WHERE id=?", (agora_str, usuario_id))
    _encerrar_sessao_e_redistribuir_tarefas(conn, alvo, agora_str)

    conn.commit()
    conn.close()
    return redirect("/supervisao")


@app.route("/supervisao/excluir/<int:usuario_id>", methods=["POST"])
@login_required
@perfil_required("ADM")
def supervisao_excluir(usuario_id):
    """Diferente de Desligar (que só marca ativo=0 e mantém a pessoa visível,
    cinza, na lista, com opção de Reativar), Excluir -- só pro ADM -- some com o
    nome da pessoa da tela de Supervisão de vez (sem opção de reativar por ali).
    Não apaga nenhum histórico: a linha em 'usuarios' continua existindo (só com
    excluido=1), então todo o histórico de atividades/monitorias/etc. que
    referencia essa pessoa continua íntegro e consultável normalmente. As
    tarefas em andamento dela são redistribuídas de volta pra fila geral, igual
    ao Desligar."""
    solicitante = usuario_atual()
    if usuario_id == solicitante["id"]:
        return "Você não pode excluir o próprio usuário.", 400

    conn = conectar()
    alvo = conn.execute("SELECT * FROM usuarios WHERE id=?", (usuario_id,)).fetchone()
    if not alvo:
        conn.close()
        return redirect("/supervisao")

    agora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "UPDATE usuarios SET ativo=0, excluido=1, excluido_em=?, excluido_por=? WHERE id=?",
        (agora_str, solicitante["nome"], usuario_id)
    )
    _encerrar_sessao_e_redistribuir_tarefas(conn, alvo, agora_str)

    conn.commit()
    conn.close()
    return redirect("/supervisao")


@app.route("/supervisao/reativar/<int:usuario_id>", methods=["POST"])
@login_required
@permissao_required("supervisao")
def supervisao_reativar(usuario_id):
    conn = conectar()
    conn.execute("UPDATE usuarios SET ativo=1, desativado_em=NULL WHERE id=?", (usuario_id,))
    conn.commit()
    conn.close()
    return redirect("/supervisao")


@app.route("/supervisao/encerrar_atendimento/<int:usuario_id>", methods=["POST"])
@login_required
@permissao_required("supervisao")
def supervisao_encerrar_atendimento(usuario_id):
    """Pro Supervisor/Adm encerrarem manualmente o atendimento de alguém que
    foi embora mais cedo e esqueceu logado -- sem precisar esperar até 18h30."""
    usuario_supervisor = usuario_atual()
    conn = conectar()
    encerrar_atendimento_forcado(conn, usuario_id, encerrado_por=f"SUPERVISOR:{usuario_supervisor['nome']}")
    conn.commit()
    conn.close()
    return redirect("/supervisao")


@app.route("/supervisao/resetar_senha/<int:usuario_id>", methods=["POST"])
@login_required
@permissao_required("supervisao")
def supervisao_resetar_senha(usuario_id):
    solicitante = usuario_atual()
    conn = conectar()
    alvo = conn.execute("SELECT * FROM usuarios WHERE id=?", (usuario_id,)).fetchone()
    if not alvo:
        conn.close()
        return redirect("/supervisao")

    # Supervisor reseta Operador/Assistente, Analista, Analista II e Visualização.
    # ADM, além desses, também reseta a senha de outro Supervisor (Supervisor não
    # reseta a senha de outro Supervisor -- é perfil de mesmo nível).
    perfis_permitidos = ["OPERADOR", "ANALISTA", "ANALISTA_II", "VISUALIZACAO", "ASSISTENTE_CLARO", "OPERADOR_CONF"]
    if solicitante["perfil"] == "ADM":
        perfis_permitidos.append("SUPERVISOR")

    if alvo["perfil"] not in perfis_permitidos:
        conn.close()
        return "Você não tem permissão para resetar a senha desse perfil.", 400

    nova_senha_hash = generate_password_hash(alvo["login"])
    conn.execute(
        "UPDATE usuarios SET senha=?, precisa_trocar_senha=1 WHERE id=?",
        (nova_senha_hash, usuario_id)
    )
    conn.commit()
    conn.close()
    return redirect("/supervisao")


@app.route("/supervisao/aniversario/<int:usuario_id>/alternar", methods=["POST"])
@login_required
@permissao_required("supervisao")
def supervisao_aniversario_alternar(usuario_id):
    conn = conectar()
    conn.execute("UPDATE usuarios SET divulgar_aniversario = 1 - COALESCE(divulgar_aniversario, 1) WHERE id=?", (usuario_id,))
    conn.commit()
    conn.close()
    return redirect("/supervisao")


@app.route("/supervisao/verificar_aniversariantes", methods=["POST"])
@login_required
@permissao_required("supervisao")
def supervisao_verificar_aniversariantes():
    postar_aniversariantes_do_dia()
    return redirect("/supervisao?aniversarios_verificados=1")


NOMES_PAUSA_TIMELINE = {
    "PAUSA_MANHA": "Pausa Manhã",
    "ALMOCO": "Almoço",
    "PAUSA_TARDE": "Pausa Tarde",
    "BANHEIRO": "Pausa Banheiro",
}


def _hora_hm(valor):
    return valor[11:16] if valor else "-"


def _duracao_hm(inicio_str, fim_str):
    if not inicio_str or not fim_str:
        return "-"
    try:
        inicio = datetime.strptime(inicio_str, "%Y-%m-%d %H:%M:%S")
        fim = datetime.strptime(fim_str, "%Y-%m-%d %H:%M:%S")
        minutos = int((fim - inicio).total_seconds() // 60)
        if minutos < 0:
            return "-"
        if minutos < 60:
            return f"{minutos:02d} min"
        horas, resto = divmod(minutos, 60)
        return f"{horas}h {resto:02d}min"
    except (ValueError, TypeError):
        return "-"


@app.route("/supervisao/timeline/<int:usuario_id>")
@login_required
@permissao_required("supervisao")
def supervisao_timeline(usuario_id):
    conn = conectar()
    usuario = conn.execute("SELECT * FROM usuarios WHERE id=?", (usuario_id,)).fetchone()
    hoje = hoje_str()

    linhas_jornada = []

    sessoes = conn.execute(
        "SELECT login_em, logout_em FROM sessoes WHERE usuario_id=? AND date(login_em)=? ORDER BY login_em",
        (usuario_id, hoje)
    ).fetchall()
    for s in sessoes:
        linhas_jornada.append({
            "acao": "Login",
            "inicio": _hora_hm(s["login_em"]),
            "fim": _hora_hm(s["logout_em"]),
            "duracao": _duracao_hm(s["login_em"], s["logout_em"]),
            "ordem": s["login_em"] or "",
        })

    pausas_evt = conn.execute(
        "SELECT tipo, inicio, fim_real FROM pausas WHERE usuario_id=? AND data=? ORDER BY inicio",
        (usuario_id, hoje)
    ).fetchall()
    for p in pausas_evt:
        linhas_jornada.append({
            "acao": NOMES_PAUSA_TIMELINE.get(p["tipo"], p["tipo"]),
            "inicio": _hora_hm(p["inicio"]),
            "fim": _hora_hm(p["fim_real"]),
            "duracao": _duracao_hm(p["inicio"], p["fim_real"]),
            "ordem": p["inicio"] or "",
        })

    linhas_jornada.sort(key=lambda linha: linha["ordem"])

    atividades_evt = conn.execute(
        "SELECT data_inicio, tabulacao, contrato FROM atividades WHERE operador=? AND date(data_inicio)=? ORDER BY data_inicio",
        (usuario["nome"], hoje)
    ).fetchall()
    linhas_atendimento = [{
        "horario": _hora_hm(a["data_inicio"]),
        "tabulacao": a["tabulacao"],
        "contrato": a["contrato"],
    } for a in atividades_evt]

    conn.close()
    return render_template(
        "Timeline.html", usuario=usuario,
        linhas_jornada=linhas_jornada, linhas_atendimento=linhas_atendimento,
    )


# ==========================
# ESCALAS (cadastro de escala do operador)
# ==========================

@app.route("/escalas")
@login_required
@permissao_required("escalas")
def escalas():
    conn = conectar()
    usuarios = conn.execute("SELECT * FROM usuarios WHERE ativo=1 ORDER BY nome").fetchall()
    escalas_atuais = {e["usuario_id"]: e for e in conn.execute("SELECT * FROM escalas").fetchall()}
    feriados = conn.execute("SELECT * FROM calendario_feriados ORDER BY data DESC").fetchall()
    conn.close()
    return render_template("Escalas.html", usuarios=usuarios, escalas=escalas_atuais, dias_semana=DIAS_SEMANA,
                            feriados=feriados)


@app.route("/escalas/feriados/adicionar", methods=["POST"])
@login_required
@perfil_required("ADM")
def escalas_feriados_adicionar():
    """Marca um dia como feriado/folga coletiva -- zera 'escalados' pro dia
    inteiro no gráfico de Escalados x Presentes x Ausentes, pra não contar
    como ausência no absenteísmo (pedido do Bruno, 2026-09-11)."""
    usuario = usuario_atual()
    data = (request.form.get("data") or "").strip()
    tipo = (request.form.get("tipo") or "Feriado").strip()
    descricao = (request.form.get("descricao") or "").strip()
    if not data:
        return redirect("/escalas")
    conn = conectar()
    try:
        conn.execute(
            "INSERT INTO calendario_feriados (data, tipo, descricao, criado_por, criado_em) VALUES (?, ?, ?, ?, ?)",
            (data, tipo, descricao, usuario["nome"], datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        conn.commit()
    except sqlite3.IntegrityError:
        pass  # já existe um feriado cadastrado nessa data
    conn.close()
    return redirect("/escalas")


@app.route("/escalas/feriados/<int:feriado_id>/remover", methods=["POST"])
@login_required
@perfil_required("ADM")
def escalas_feriados_remover(feriado_id):
    conn = conectar()
    conn.execute("DELETE FROM calendario_feriados WHERE id=?", (feriado_id,))
    conn.commit()
    conn.close()
    return redirect("/escalas")


@app.route("/escalas/aplicar_padrao_em_massa", methods=["POST"])
@login_required
@perfil_required("ADM")
def escalas_aplicar_padrao_em_massa():
    """Aplica Seg-Sex 08h-17h48 pra todo Operador ativo que AINDA NÃO tem
    escala salva no banco (o checkbox pré-marcado na tela é só um preenchimento
    sugerido -- só vira real depois de salvo)."""
    conn = conectar()
    sem_escala = conn.execute("""
        SELECT u.id FROM usuarios u
        LEFT JOIN escalas e ON e.usuario_id = u.id
        WHERE u.ativo = 1 AND u.perfil = 'OPERADOR' AND e.id IS NULL
    """).fetchall()

    for u in sem_escala:
        conn.execute("""
            INSERT INTO escalas (usuario_id, dias_trabalho, hora_inicio, hora_fim)
            VALUES (?, 'SEG,TER,QUA,QUI,SEX', '08:00', '17:48')
        """, (u["id"],))

    conn.commit()
    conn.close()
    return redirect("/escalas")


@app.route("/escalas/salvar", methods=["POST"])
@login_required
@permissao_required("escalas")
def escalas_salvar():
    usuario_id = request.form.get("usuario_id")
    dias = request.form.getlist("dias")
    hora_inicio = request.form.get("hora_inicio")
    hora_fim = request.form.get("hora_fim")

    conn = conectar()
    existente = conn.execute("SELECT id FROM escalas WHERE usuario_id=?", (usuario_id,)).fetchone()
    if existente:
        conn.execute(
            "UPDATE escalas SET dias_trabalho=?, hora_inicio=?, hora_fim=? WHERE usuario_id=?",
            (",".join(dias), hora_inicio, hora_fim, usuario_id)
        )
    else:
        conn.execute(
            "INSERT INTO escalas (usuario_id, dias_trabalho, hora_inicio, hora_fim) VALUES (?, ?, ?, ?)",
            (usuario_id, ",".join(dias), hora_inicio, hora_fim)
        )
    conn.commit()
    conn.close()
    return redirect("/escalas")


@app.route("/escalas/ferias/salvar", methods=["POST"])
@login_required
@permissao_required("escalas")
def escalas_ferias_salvar():
    usuario_id = request.form.get("usuario_id")
    ferias_inicio = request.form.get("ferias_inicio") or None
    ferias_fim = request.form.get("ferias_fim") or None

    conn = conectar()
    existente = conn.execute("SELECT id FROM escalas WHERE usuario_id=?", (usuario_id,)).fetchone()
    if existente:
        conn.execute(
            "UPDATE escalas SET ferias_inicio=?, ferias_fim=? WHERE usuario_id=?",
            (ferias_inicio, ferias_fim, usuario_id)
        )
    else:
        conn.execute(
            "INSERT INTO escalas (usuario_id, ferias_inicio, ferias_fim) VALUES (?, ?, ?)",
            (usuario_id, ferias_inicio, ferias_fim)
        )
    conn.commit()
    conn.close()
    return redirect("/escalas")


@app.route("/escalas/ferias/limpar", methods=["POST"])
@login_required
@permissao_required("escalas")
def escalas_ferias_limpar():
    usuario_id = request.form.get("usuario_id")
    conn = conectar()
    conn.execute("UPDATE escalas SET ferias_inicio=NULL, ferias_fim=NULL WHERE usuario_id=?", (usuario_id,))
    conn.commit()
    conn.close()
    return redirect("/escalas")


# ==========================
# NOVA TAREFA (criação manual via upload de planilha) + TRANSFERÊNCIA
# ==========================

def _limpo_texto(valor):
    import math
    if valor is None:
        return ""
    if isinstance(valor, float) and math.isnan(valor):
        return ""
    texto = str(valor).strip()
    return "" if texto.lower() in ("nan", "none") else texto


def _limpo_numero(valor):
    import math
    if valor is None or valor == "":
        return 0.0
    try:
        valor = float(valor)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if math.isnan(valor) else valor


def _limpa_prioridade(valor):
    """Prioridade vai de 0 (menor) a 100 (maior). Fora disso ou ausente vira 0."""
    numero = int(round(_limpo_numero(valor)))
    return max(0, min(100, numero))


def _registrar_historico_tarefa_manual(conn, contrato, cidade, criado_por, atribuido_para, tipo):
    conn.execute("""
        INSERT INTO historico_tarefas_manuais (contrato, cidade, criado_por, atribuido_para, tipo, data_criacao)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (contrato, cidade, criado_por, atribuido_para, tipo, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))


def _processar_nova_tarefa(conn, usuario_nome, usuarios_ativos, dados_linha, justificativa):
    """Processa uma única tarefa (contrato/cidade/atribuição + dados opcionais).
    Retorna (tipo, mensagem) onde tipo é 'direto', 'transferencia' ou 'erro'."""
    contrato = _limpo_texto(dados_linha.get("contrato", ""))
    cidade = _limpo_texto(dados_linha.get("cidade", ""))
    atribuicao_login = _limpo_texto(dados_linha.get("atribuicao", "")).lower()

    if not contrato or not cidade or not atribuicao_login:
        return "erro", "Contrato, cidade e atribuição são obrigatórios."

    # Só número no contrato -- pedido do Bruno, 2026-09-10, depois de um caso
    # real onde alguém digitou o próprio login (ex: "z629225") no campo de
    # contrato por engano, criando uma tarefa com dado sem sentido. Também
    # remove zero a esquerda (ex: "007776570" -> "7776570"), pra não ter duas
    # linhas diferentes pro mesmo contrato só por causa de formatação.
    if not contrato.isdigit():
        return "erro", f"Contrato \"{contrato}\" inválido -- só é permitido número."
    contrato = str(int(contrato))

    if atribuicao_login not in usuarios_ativos:
        return "erro", f"Login de atribuição '{atribuicao_login}' não existe ou está inativo."

    destino_nome = usuarios_ativos[atribuicao_login]

    existente = conn.execute(
        "SELECT * FROM mailing WHERE contrato=? AND cidade=?", (contrato, cidade)
    ).fetchone()

    if not existente:
        conn.execute("""
            INSERT INTO mailing (
                contrato, cliente, cidade, node, endereco, complemento,
                telefone_celular, telefone_residencial, telefone_comercial, email, codigo_imovel,
                produto_tv, valor_tv, valor_ponto_adicional, produto_virtua, valor_virtua,
                produto_voip, valor_voip, valor_total,
                status, tentativa, reservado_para, prioridade, data_entrada_fila
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDENTE', 1, ?, ?, ?)
        """, (
            contrato, _limpo_texto(dados_linha.get("cliente", "")), cidade, _limpo_texto(dados_linha.get("node", "")),
            _limpo_texto(dados_linha.get("endereco", "")), _limpo_texto(dados_linha.get("complemento", "")),
            _limpo_texto(dados_linha.get("telefone_celular", "")), _limpo_texto(dados_linha.get("telefone_residencial", "")),
            _limpo_texto(dados_linha.get("telefone_comercial", "")), _limpo_texto(dados_linha.get("email", "")),
            _limpo_texto(dados_linha.get("codigo_imovel", "")),
            _limpo_texto(dados_linha.get("produto_tv", "")), _limpo_numero(dados_linha.get("valor_tv", 0)),
            _limpo_numero(dados_linha.get("valor_ponto_adicional", 0)),
            _limpo_texto(dados_linha.get("produto_virtua", "")), _limpo_numero(dados_linha.get("valor_virtua", 0)),
            _limpo_texto(dados_linha.get("produto_voip", "")), _limpo_numero(dados_linha.get("valor_voip", 0)),
            _limpo_numero(dados_linha.get("valor_total", 0)),
            destino_nome,
            _limpa_prioridade(dados_linha.get("prioridade", 0)),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ))
        _registrar_historico_tarefa_manual(conn, contrato, cidade, usuario_nome, destino_nome, "novo")
        return "direto", f"{contrato} ({cidade}) → {destino_nome}"

    elif not existente["operador"]:
        conn.execute("UPDATE mailing SET reservado_para=? WHERE id=?", (destino_nome, existente["id"]))
        _registrar_historico_tarefa_manual(conn, contrato, cidade, usuario_nome, destino_nome, "reserva")
        return "direto", f"{contrato} ({cidade}) → {destino_nome}"

    else:
        agora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Modo de contingência (pedido do Bruno, 2026-09-04): quando ligado
        # em Modo de Trabalho, pula a fila de aprovação -- a transferência já
        # nasce aprovada e o contrato já libera/reserva na hora, útil em
        # momento de pico/instabilidade onde esperar aprovação manual atrasa
        # demais. Registra do mesmo jeito na tabela (auditoria), só que já
        # como APROVADO, sem precisar de ninguém confirmar depois.
        linha_config = conn.execute("SELECT transferencia_auto_aprovar FROM configuracoes WHERE id=1").fetchone()
        auto_aprovar = bool(linha_config and linha_config["transferencia_auto_aprovar"])

        if auto_aprovar:
            conn.execute("""
                UPDATE mailing SET status='PENDENTE', operador=NULL, ultimo_operador=?, reservado_para=?, data_entrada_fila=?
                WHERE id=?
            """, (existente["operador"], destino_nome, agora_str, existente["id"]))
            conn.execute("""
                INSERT INTO transferencias (contrato, solicitante, dono_atual, destino, justificativa, status, data_solicitacao, respondido_por, data_resposta)
                VALUES (?, ?, ?, ?, ?, 'APROVADO', ?, 'Sistema (contingência)', ?)
            """, (contrato, usuario_nome, existente["operador"], destino_nome, justificativa, agora_str, agora_str))
            _registrar_historico_tarefa_manual(conn, contrato, cidade, usuario_nome, destino_nome, "transferencia_auto")
            return "direto", f"{contrato} ({cidade}): {existente['operador']} → {destino_nome} (auto-aprovado, modo contingência)"

        conn.execute("""
            INSERT INTO transferencias (contrato, solicitante, dono_atual, destino, justificativa, status, data_solicitacao)
            VALUES (?, ?, ?, ?, ?, 'PENDENTE', ?)
        """, (contrato, usuario_nome, existente["operador"], destino_nome, justificativa, agora_str))
        _registrar_historico_tarefa_manual(conn, contrato, cidade, usuario_nome, destino_nome, "transferencia_pendente")
        return "transferencia", f"{contrato} ({cidade}): {existente['operador']} → {destino_nome}"


@app.route("/tarefas/nova")
@login_required
@permissao_required("nova_tarefa")
def tarefas_nova():
    conn = conectar()
    cidades = conn.execute("SELECT * FROM cidades WHERE ativo=1 ORDER BY nome").fetchall()
    usuarios = conn.execute("SELECT * FROM usuarios WHERE ativo=1 ORDER BY nome").fetchall()
    conn.close()
    return render_template("TarefaNova.html", cidades=cidades, usuarios=usuarios)


@app.route("/tarefas/manual", methods=["POST"])
@login_required
@permissao_required("nova_tarefa")
def tarefas_manual():
    usuario = usuario_atual()
    conn = conectar()
    usuarios_ativos = {u["login"]: u["nome"] for u in conn.execute("SELECT login, nome FROM usuarios WHERE ativo=1").fetchall()}

    dados_linha = {
        "contrato": request.form.get("contrato", ""),
        "cidade": request.form.get("cidade", ""),
        "atribuicao": request.form.get("atribuicao", ""),
        "cliente": request.form.get("cliente", ""),
        "node": request.form.get("node", ""),
        "endereco": request.form.get("endereco", ""),
        "complemento": request.form.get("complemento", ""),
        "telefone_celular": request.form.get("telefone_celular", ""),
        "telefone_residencial": request.form.get("telefone_residencial", ""),
        "telefone_comercial": request.form.get("telefone_comercial", ""),
        "email": request.form.get("email", ""),
        "codigo_imovel": request.form.get("codigo_imovel", ""),
        "produto_tv": request.form.get("produto_tv", ""),
        "valor_tv": request.form.get("valor_tv", 0),
        "valor_ponto_adicional": request.form.get("valor_ponto_adicional", 0),
        "produto_virtua": request.form.get("produto_virtua", ""),
        "valor_virtua": request.form.get("valor_virtua", 0),
        "produto_voip": request.form.get("produto_voip", ""),
        "valor_voip": request.form.get("valor_voip", 0),
        "valor_total": request.form.get("valor_total", 0),
        "prioridade": request.form.get("prioridade", 0),
    }
    justificativa = request.form.get("justificativa", "")

    tipo, mensagem = _processar_nova_tarefa(conn, usuario["nome"], usuarios_ativos, dados_linha, justificativa)
    conn.commit()
    conn.close()

    return render_template(
        "TarefaResultado.html",
        atribuidos_direto=[mensagem] if tipo == "direto" else [],
        transferencias_solicitadas=[mensagem] if tipo == "transferencia" else [],
        erros=[mensagem] if tipo == "erro" else [],
    )


@app.route("/tarefas/importar", methods=["POST"])
@login_required
@permissao_required("nova_tarefa")
def tarefas_importar():
    usuario = usuario_atual()
    if usuario["perfil"] == "OPERADOR":
        return "Seu perfil não pode criar tarefas via planilha.", 403
    justificativa_padrao = request.form.get("justificativa") or ""
    arquivo = request.files.get("arquivo")

    if not arquivo or arquivo.filename == "":
        return render_template("TarefaResultado.html", erro="Nenhum arquivo selecionado.")

    try:
        if arquivo.filename.lower().endswith(".csv"):
            df = pd.read_csv(arquivo)
        else:
            df = pd.read_excel(arquivo)
    except Exception as erro:
        return render_template("TarefaResultado.html", erro=f"Não consegui ler o arquivo: {erro}")

    conn = conectar()

    atribuidos_direto = []
    transferencias_solicitadas = []
    erros = []

    # Mapa de logins válidos -> nome (pra validar a coluna de atribuição)
    usuarios_ativos = {u["login"]: u["nome"] for u in conn.execute("SELECT login, nome FROM usuarios WHERE ativo=1").fetchall()}

    for idx, linha in df.iterrows():
        dados_linha = {
            "contrato": linha.get("contrato", linha.get("Contrato", "")),
            "cidade": linha.get("cidade", linha.get("Cidade", "")),
            "atribuicao": linha.get("atribuicao", linha.get("Atribuicao", linha.get("Atribuição", ""))),
            "cliente": linha.get("cliente", ""),
            "node": linha.get("node", ""),
            "endereco": linha.get("endereco", ""),
            "complemento": linha.get("complemento", ""),
            "telefone_celular": linha.get("telefone_celular", ""),
            "telefone_residencial": linha.get("telefone_residencial", ""),
            "telefone_comercial": linha.get("telefone_comercial", ""),
            "email": linha.get("email", ""),
            "codigo_imovel": linha.get("codigo_imovel", ""),
            "produto_tv": linha.get("produto_tv", ""),
            "valor_tv": linha.get("valor_tv", 0),
            "valor_ponto_adicional": linha.get("valor_ponto_adicional", 0),
            "produto_virtua": linha.get("produto_virtua", ""),
            "valor_virtua": linha.get("valor_virtua", 0),
            "produto_voip": linha.get("produto_voip", ""),
            "valor_voip": linha.get("valor_voip", 0),
            "valor_total": linha.get("valor_total", 0),
            "prioridade": linha.get("prioridade", 0),
        }

        tipo, mensagem = _processar_nova_tarefa(conn, usuario["nome"], usuarios_ativos, dados_linha, justificativa_padrao)
        mensagem_com_linha = mensagem if tipo != "erro" else f"Linha {idx + 2}: {mensagem}"

        if tipo == "direto":
            atribuidos_direto.append(mensagem_com_linha)
        elif tipo == "transferencia":
            transferencias_solicitadas.append(mensagem_com_linha)
        else:
            erros.append(mensagem_com_linha)

    conn.commit()
    conn.close()

    return render_template(
        "TarefaResultado.html",
        atribuidos_direto=atribuidos_direto,
        transferencias_solicitadas=transferencias_solicitadas,
        erros=erros,
    )


@app.route("/transferencias")
@login_required
@permissao_required("transferencias")
def transferencias():
    conn = conectar()
    pendentes = conn.execute(
        "SELECT * FROM transferencias WHERE status='PENDENTE' ORDER BY data_solicitacao"
    ).fetchall()
    conn.close()
    return render_template("Transferencias.html", pendentes=pendentes)


@app.route("/transferencias/<int:transf_id>/aprovar", methods=["POST"])
@login_required
@permissao_required("transferencias")
def transferencias_aprovar(transf_id):
    supervisor = usuario_atual()
    conn = conectar()
    agora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    transf = conn.execute("SELECT * FROM transferencias WHERE id=? AND status='PENDENTE'", (transf_id,)).fetchone()
    if transf:
        # Libera o contrato do dono atual e reserva pro destino aprovado
        conn.execute("""
            UPDATE mailing SET status='PENDENTE', operador=NULL, ultimo_operador=?, reservado_para=?, data_entrada_fila=?
            WHERE contrato=?
        """, (transf["dono_atual"], transf["destino"], agora_str, transf["contrato"]))

        conn.execute("""
            UPDATE transferencias SET status='APROVADO', respondido_por=?, data_resposta=?
            WHERE id=?
        """, (supervisor["nome"], agora_str, transf_id))

        conn.commit()
    conn.close()
    return redirect("/transferencias")


@app.route("/transferencias/<int:transf_id>/rejeitar", methods=["POST"])
@login_required
@permissao_required("transferencias")
def transferencias_rejeitar(transf_id):
    supervisor = usuario_atual()
    conn = conectar()
    agora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("""
        UPDATE transferencias SET status='REJEITADO', respondido_por=?, data_resposta=?
        WHERE id=? AND status='PENDENTE'
    """, (supervisor["nome"], agora_str, transf_id))
    conn.commit()
    conn.close()
    return redirect("/transferencias")


@app.route("/admin/motivos-ausencia/adicionar", methods=["POST"])
@login_required
@perfil_required("ADM")
def admin_motivos_ausencia_adicionar():
    """Deixa o ADM cadastrar motivo de ausência extra (ex: Feriado) sem
    precisar de mim pra mexer no código -- pedido do Bruno, 2026-09-08."""
    usuario = usuario_atual()
    nome = (request.form.get("nome") or "").strip()
    if not nome:
        return redirect("/admin/modo")
    conn = conectar()
    try:
        conn.execute(
            "INSERT INTO motivos_ausencia_customizados (nome, ativo, criado_por, criado_em) VALUES (?, 1, ?, ?)",
            (nome, usuario["nome"], datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        conn.commit()
    except sqlite3.IntegrityError:
        pass  # já existe um motivo com esse nome -- ignora silenciosamente
    conn.close()
    return redirect("/admin/modo")


@app.route("/admin/motivos-ausencia/<int:motivo_id>/alternar", methods=["POST"])
@login_required
@perfil_required("ADM")
def admin_motivos_ausencia_alternar(motivo_id):
    """Ativa/desativa um motivo customizado -- desativar não apaga o
    histórico de faltas já registradas com esse motivo, só tira ele da
    lista de opções pra novos registros."""
    conn = conectar()
    conn.execute("UPDATE motivos_ausencia_customizados SET ativo = 1 - ativo WHERE id=?", (motivo_id,))
    conn.commit()
    conn.close()
    return redirect("/admin/modo")


@app.route("/admin/historico-tarefas")
@login_required
@perfil_required("ADM")
def admin_historico_tarefas():
    """Histórico de tudo que foi criado pela tela Nova Tarefa (manual ou
    importação em planilha pela mesma tela) -- quem criou, pra quem
    atribuiu, contrato, cidade e quando. Pedido do Bruno, 2026-09-08, depois
    do caso do contrato criado com o login de alguém no lugar do número."""
    conn = conectar()
    data_fim = request.args.get("data_fim") or hoje_str()
    data_inicio = request.args.get("data_inicio") or (datetime.strptime(data_fim, "%Y-%m-%d") - timedelta(days=30)).strftime("%Y-%m-%d")

    historico = conn.execute("""
        SELECT * FROM historico_tarefas_manuais
        WHERE date(data_criacao) BETWEEN ? AND ?
        ORDER BY id DESC
    """, (data_inicio, data_fim)).fetchall()
    conn.close()
    return render_template("HistoricoTarefas.html", historico=historico, data_inicio=data_inicio, data_fim=data_fim)


@app.route("/admin/backup")
@login_required
@perfil_required("ADM")
def admin_backup():
    """Baixa um zip com tudo (código, templates, banco) -- pra rodar ANTES
    de subir qualquer versão nova. Pedido pelo Bruno em 2026-09-04, depois
    do incidente de corrupção de banco/regressão de tela do dia anterior.

    Também salva uma cópia dentro do próprio servidor (pasta backups/), pra
    o restore poder ser feito 100% pelo console Bash (ver
    restaurar_backup.py) sem depender do site estar no ar -- se o site
    quebrar, o botão quebra junto, então o restore não pode morar dentro da
    aplicação (pedido do Bruno, 2026-09-04)."""
    import zipfile

    buffer = io.BytesIO()
    pasta_raiz = os.path.dirname(os.path.abspath(__file__))

    # Pastas que não fazem sentido levar pro backup (cache, e a própria pasta
    # de backups -- senão cada backup incluiria todos os anteriores dentro
    # dele, crescendo pra sempre).
    pastas_ignoradas = {"__pycache__", ".git", "flask_session", "backups"}

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zip_arquivo:
        for pasta_atual, subpastas, arquivos in os.walk(pasta_raiz):
            subpastas[:] = [p for p in subpastas if p not in pastas_ignoradas]
            for nome_arquivo in arquivos:
                if nome_arquivo.endswith(".pyc"):
                    continue
                caminho_completo = os.path.join(pasta_atual, nome_arquivo)
                caminho_relativo = os.path.relpath(caminho_completo, pasta_raiz)
                zip_arquivo.write(caminho_completo, caminho_relativo)

    buffer.seek(0)
    nome_zip = f"backup_crm_rsi_{datetime.now().strftime('%Y%m%d_%H%M')}.zip"

    # Salva a cópia local, e mantém só os 10 mais recentes (apaga o resto)
    pasta_backups = os.path.join(pasta_raiz, "backups")
    os.makedirs(pasta_backups, exist_ok=True)
    with open(os.path.join(pasta_backups, nome_zip), "wb") as arquivo_local:
        arquivo_local.write(buffer.getvalue())

    backups_existentes = sorted(
        [f for f in os.listdir(pasta_backups) if f.startswith("backup_crm_rsi_") and f.endswith(".zip")],
        reverse=True
    )
    for antigo in backups_existentes[10:]:
        os.remove(os.path.join(pasta_backups, antigo))

    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name=nome_zip, mimetype="application/zip")


@app.route("/admin/modo")
@login_required
@permissao_required("modo_trabalho")
def admin_modo():
    conn = conectar()
    modo_atual = obter_modo_trabalho(conn)

    cidades_mailing = [c["cidade"] for c in conn.execute(
        "SELECT DISTINCT cidade FROM mailing WHERE cidade IS NOT NULL AND cidade != '' ORDER BY cidade"
    ).fetchall()]
    bloqueios = {
        b["cidade"]: b for b in conn.execute("SELECT * FROM cidades_bloqueio_distribuicao").fetchall()
    }
    meta_agendados_mes = conn.execute("SELECT meta_agendados_mes FROM configuracoes WHERE id=1").fetchone()
    meta_agendados_mes = meta_agendados_mes["meta_agendados_mes"] if meta_agendados_mes and meta_agendados_mes["meta_agendados_mes"] else 100
    meta_execucoes_mes = conn.execute("SELECT meta_execucoes_mes FROM configuracoes WHERE id=1").fetchone()
    meta_execucoes_mes = meta_execucoes_mes["meta_execucoes_mes"] if meta_execucoes_mes and meta_execucoes_mes["meta_execucoes_mes"] else 100
    transferencia_auto_aprovar = conn.execute("SELECT transferencia_auto_aprovar FROM configuracoes WHERE id=1").fetchone()
    transferencia_auto_aprovar = bool(transferencia_auto_aprovar and transferencia_auto_aprovar["transferencia_auto_aprovar"])
    motivos_ausencia_customizados = obter_motivos_ausencia_customizados(conn, so_ativos=False)
    blacklist_contratos = conn.execute("SELECT * FROM contratos_blacklist ORDER BY criado_em DESC").fetchall()
    conn.close()
    return render_template("AdminModo.html", modo_atual=modo_atual, cidades_mailing=cidades_mailing, bloqueios=bloqueios,
                            meta_agendados_mes=meta_agendados_mes, meta_execucoes_mes=meta_execucoes_mes,
                            transferencia_auto_aprovar=transferencia_auto_aprovar,
                            blacklist_contratos=blacklist_contratos,
                            motivos_ausencia_customizados=motivos_ausencia_customizados)


@app.route("/admin/modo/transferencia_auto_aprovar/alternar", methods=["POST"])
@login_required
@permissao_required("modo_trabalho")
def admin_modo_transferencia_auto_aprovar_alternar():
    """Liga/desliga o modo de contingência de transferência (pedido do
    Bruno, 2026-09-04) -- ver a lógica de auto-aprovação em
    _processar_nova_tarefa, onde a transferência é criada."""
    conn = conectar()
    atual = conn.execute("SELECT transferencia_auto_aprovar FROM configuracoes WHERE id=1").fetchone()
    novo_valor = 0 if (atual and atual["transferencia_auto_aprovar"]) else 1
    conn.execute("UPDATE configuracoes SET transferencia_auto_aprovar=? WHERE id=1", (novo_valor,))
    conn.commit()
    conn.close()
    return redirect("/admin/modo")


@app.route("/admin/modo/salvar", methods=["POST"])
@login_required
@permissao_required("modo_trabalho")
def admin_modo_salvar():
    novo_modo = request.form.get("modo_trabalho")
    if novo_modo in ("ATIVO", "RECEPTIVO"):
        conn = conectar()
        conn.execute("UPDATE configuracoes SET modo_trabalho=? WHERE id=1", (novo_modo,))
        conn.commit()
        conn.close()
    return redirect("/admin/modo")


@app.route("/admin/meta_agendados/salvar", methods=["POST"])
@login_required
@perfil_required("ADM")
def admin_meta_agendados_salvar():
    try:
        meta = int(request.form.get("meta_agendados_mes") or 0)
    except ValueError:
        meta = 0
    conn = conectar()
    conn.execute("UPDATE configuracoes SET meta_agendados_mes=? WHERE id=1", (meta,))
    conn.commit()
    conn.close()
    return redirect("/admin/modo")


@app.route("/admin/meta_execucoes/salvar", methods=["POST"])
@login_required
@perfil_required("ADM")
def admin_meta_execucoes_salvar():
    try:
        meta = int(request.form.get("meta_execucoes_mes") or 0)
    except ValueError:
        meta = 0
    conn = conectar()
    conn.execute("UPDATE configuracoes SET meta_execucoes_mes=? WHERE id=1", (meta,))
    conn.commit()
    conn.close()
    return redirect("/admin/modo")


@app.route("/admin/cidades_bloqueio/salvar", methods=["POST"])
@login_required
@perfil_required("ADM")
def admin_cidades_bloqueio_salvar():
    cidades_marcadas = set(request.form.getlist("cidade_bloqueada"))
    todas_as_cidades = request.form.getlist("todas_cidades")
    usuario = usuario_atual()
    agora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = conectar()
    # Estado antes de gravar: a tela manda TODAS as cidades a cada salvamento,
    # então sem comparar não dá pra saber qual delas realmente mudou -- e o
    # histórico ficaria com 36 linhas iguais por clique (2026-10-19).
    estado_anterior = {
        linha["cidade"]: linha["bloqueado"]
        for linha in conn.execute("SELECT cidade, bloqueado FROM cidades_bloqueio_distribuicao").fetchall()
    }
    for cidade in todas_as_cidades:
        bloqueado = 1 if cidade in cidades_marcadas else 0
        antes = estado_anterior.get(cidade)
        if antes != bloqueado:
            conn.execute("""
                INSERT INTO cidades_bloqueio_historico
                    (cidade, bloqueado, bloqueado_antes, alterado_em, alterado_por)
                VALUES (?, ?, ?, ?, ?)
            """, (cidade, bloqueado, antes, agora_str, usuario["nome"]))
        conn.execute("""
            INSERT INTO cidades_bloqueio_distribuicao (cidade, bloqueado, atualizado_em, atualizado_por)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(cidade) DO UPDATE SET bloqueado=excluded.bloqueado, atualizado_em=excluded.atualizado_em, atualizado_por=excluded.atualizado_por
        """, (cidade, bloqueado, agora_str, usuario["nome"]))
    conn.commit()
    conn.close()
    return redirect("/admin/modo")


@app.route("/admin/blacklist/adicionar", methods=["POST"])
@login_required
@perfil_required("ADM")
def admin_blacklist_adicionar():
    """Adiciona um ou mais contratos na blacklist de distribuição --
    aceita colar vários de uma vez (um por linha, ou separados por vírgula
    ou espaço), pedido do Bruno, 2026-09-20. Contrato que já está na lista
    é ignorado silenciosamente (não duplica nem sobrescreve o motivo
    original)."""
    bruto = (request.form.get("contratos") or "").strip()
    motivo = (request.form.get("motivo") or "").strip() or None
    usuario = usuario_atual()
    agora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    contratos = set()
    for pedaco in re.split(r"[,\s]+", bruto):
        pedaco = pedaco.strip()
        if pedaco:
            contratos.add(pedaco)

    if contratos:
        conn = conectar()
        for contrato in contratos:
            conn.execute("""
                INSERT OR IGNORE INTO contratos_blacklist (contrato, motivo, criado_por, criado_em)
                VALUES (?, ?, ?, ?)
            """, (contrato, motivo, usuario["nome"], agora_str))
        conn.commit()
        conn.close()
    return redirect("/admin/modo")


@app.route("/admin/blacklist/<int:blacklist_id>/remover", methods=["POST"])
@login_required
@perfil_required("ADM")
def admin_blacklist_remover(blacklist_id):
    conn = conectar()
    conn.execute("DELETE FROM contratos_blacklist WHERE id=?", (blacklist_id,))
    conn.commit()
    conn.close()
    return redirect("/admin/modo")


@app.route("/admin/sunny/salvar", methods=["POST"])
@login_required
@perfil_required("ADM")
def admin_sunny_salvar():
    novo_link = (request.form.get("sunny_link") or "").strip()
    conn = conectar()
    conn.execute("UPDATE configuracoes SET sunny_link=? WHERE id=1", (novo_link,))
    conn.commit()
    conn.close()
    return redirect("/admin/modo")


# ---------------------------------------------------------------------------
# Relatório por e-mail
# ---------------------------------------------------------------------------
# Esta tela era do tempo do Power Automate: o CRM escrevia um HTML numa pasta
# do OneDrive e um fluxo externo mandava o e-mail. Com a migração para a nuvem
# a pasta deixou de existir e a tela ficou sem função.
#
# Agora ela configura o envio direto por SMTP, que é o que o
# relatorio_gerencial.py usa. O módulo é importado DENTRO das rotas, não no
# topo do arquivo: assim um erro nele não impede o site de subir.
#
# A SENHA NUNCA VOLTA PARA O HTML. O formulário mostra só o tamanho dela, e o
# campo de senha em branco significa "mantém a que está gravada". Devolver a
# senha no value do input a deixaria no código-fonte da página.
@app.route("/admin/relatorio_email")
@login_required
@perfil_required("ADM")
def admin_relatorio_email():
    conn = conectar()
    config = conn.execute("""
        SELECT email_smtp_host, email_smtp_porta, email_smtp_usuario, email_smtp_senha,
               email_remetente, email_envio_automatico_ativo, email_dias_semana,
               email_ultimo_envio_data
        FROM configuracoes WHERE id=1
    """).fetchone()
    destinatarios = conn.execute(
        "SELECT id, email, ativo FROM relatorio_email_destinatarios ORDER BY email"
    ).fetchall()
    conn.close()
    dias_marcados = (config["email_dias_semana"] or "SEG,TER,QUA,QUI,SEX").split(",") if config else []
    senha_gravada = len(config["email_smtp_senha"] or "") if config else 0
    return render_template("AdminRelatorioEmail.html", config=config,
                           destinatarios=destinatarios, dias_marcados=dias_marcados,
                           dias_semana=DIAS_SEMANA, data_hoje=hoje_str(),
                           senha_gravada=senha_gravada)


@app.route("/admin/relatorio_email/salvar", methods=["POST"])
@login_required
@perfil_required("ADM")
def admin_relatorio_email_salvar():
    host = (request.form.get("email_smtp_host") or "").strip()
    usuario = (request.form.get("email_smtp_usuario") or "").strip()
    remetente = (request.form.get("email_remetente") or "").strip()
    senha = request.form.get("email_smtp_senha") or ""
    ativo = 1 if request.form.get("email_envio_automatico_ativo") else 0
    dias_texto = ",".join(request.form.getlist("email_dias_semana"))
    try:
        porta = int((request.form.get("email_smtp_porta") or "587").strip())
    except ValueError:
        porta = 587

    conn = conectar()
    # email_pasta_destino vai a vazio de propósito: é o que desliga a rotina
    # de fundo que ainda escrevia o arquivo do Power Automate.
    conn.execute("""
        UPDATE configuracoes SET email_smtp_host=?, email_smtp_porta=?, email_smtp_usuario=?,
               email_remetente=?, email_envio_automatico_ativo=?, email_dias_semana=?,
               email_pasta_destino=''
        WHERE id=1
    """, (host, porta, usuario, remetente, ativo, dias_texto))
    # Senha em branco mantém a gravada -- é o que permite salvar os outros
    # campos sem precisar redigitar a senha de app toda vez.
    if senha.strip():
        conn.execute("UPDATE configuracoes SET email_smtp_senha=? WHERE id=1",
                     (senha.replace(" ", ""),))
    conn.commit()
    conn.close()
    return redirect("/admin/relatorio_email?salvo=1")


@app.route("/admin/relatorio_email/destinatario", methods=["POST"])
@login_required
@perfil_required("ADM")
def admin_relatorio_email_destinatario():
    acao = request.form.get("acao")
    conn = conectar()
    if acao == "adicionar":
        email = (request.form.get("email") or "").strip()
        if "@" in email:
            conn.execute("INSERT OR IGNORE INTO relatorio_email_destinatarios(email, ativo)"
                         " VALUES (?, 1)", (email,))
            conn.execute("UPDATE relatorio_email_destinatarios SET ativo=1 WHERE email=?",
                         (email,))
    elif acao == "alternar":
        conn.execute("UPDATE relatorio_email_destinatarios SET ativo = 1 - ativo WHERE id=?",
                     (request.form.get("id"),))
    elif acao == "remover":
        conn.execute("DELETE FROM relatorio_email_destinatarios WHERE id=?",
                     (request.form.get("id"),))
    conn.commit()
    conn.close()
    return redirect("/admin/relatorio_email")


@app.route("/admin/relatorio_email/enviar_teste", methods=["POST"])
@login_required
@perfil_required("ADM")
def admin_relatorio_email_enviar_teste():
    import urllib.parse
    conn = conectar()
    try:
        import relatorio_gerencial
        corpo = ("<p style=\"font-family:sans-serif\">Teste de envio do CRM RSI.<br>"
                 "Se esta mensagem chegou, os relatórios diário e semanal estão prontos "
                 "para ser agendados.</p>")
        ok, detalhe = relatorio_gerencial.enviar_email(conn, "CRM RSI — teste de envio", corpo)
    except Exception as erro:
        ok, detalhe = False, str(erro)
    finally:
        conn.close()
    chave = "enviado" if ok else "erro_envio"
    return redirect(f"/admin/relatorio_email?{chave}=" + urllib.parse.quote(detalhe))


@app.route("/admin/relatorio_email/enviar_agora", methods=["POST"])
@login_required
@perfil_required("ADM")
def admin_relatorio_email_enviar_agora():
    import urllib.parse
    tipo = request.form.get("tipo", "diario")
    conn = conectar()
    try:
        assunto, html = _montar_relatorio_gerencial(conn, tipo)
        import relatorio_gerencial
        ok, detalhe = relatorio_gerencial.enviar_email(conn, assunto, html)
    except Exception as erro:
        ok, detalhe = False, str(erro)
    finally:
        conn.close()
    chave = "enviado" if ok else "erro_envio"
    return redirect(f"/admin/relatorio_email?{chave}=" + urllib.parse.quote(detalhe))


@app.route("/admin/relatorio_email/previa")
@login_required
@perfil_required("ADM")
def admin_relatorio_email_previa():
    """Mostra o relatório na tela, sem enviar nada. É o jeito de conferir o
    conteúdo antes de mandar para alguém."""
    tipo = request.args.get("tipo", "diario")
    conn = conectar()
    try:
        _, html = _montar_relatorio_gerencial(conn, tipo)
    except Exception as erro:
        html = (f"<p style=\"font-family:sans-serif;color:#b00\">Não consegui montar a "
                f"prévia: {erro}</p>")
    finally:
        conn.close()
    return html


def _montar_relatorio_gerencial(conn, tipo):
    """Monta o diário de hoje ou o semanal da última semana fechada.

    Importa o módulo aqui dentro, e não no topo do app.py, para que um erro
    no relatório nunca impeça o site inteiro de subir.
    """
    import relatorio_gerencial
    if tipo == "semanal":
        hoje = datetime.now().date()
        inicio, fim = relatorio_gerencial.semana_fechada_antes(hoje)
        dados = relatorio_gerencial.montar_semanal(conn, inicio, fim)
        html = relatorio_gerencial.montar_html_semanal(dados)
        assunto = (f"CRM RSI · semana {inicio.strftime('%d/%m')}-{fim.strftime('%d/%m')} · "
                   f"{dados['numeros']['agendados']} agendas")
        return assunto, html
    dia = datetime.now().date()
    dados = relatorio_gerencial.montar_diario(conn, dia)
    html = relatorio_gerencial.montar_html_diario(dados)
    qtd = len(dados["variacoes"])
    if not dados["numeros"]["atividades"]:
        assunto = f"CRM RSI · {dia.strftime('%d/%m')} · sem atividade no dia"
    else:
        assunto = (f"CRM RSI · {dia.strftime('%d/%m')} · "
                   + (f"{qtd} ponto{'s' if qtd != 1 else ''} de atenção"
                      if qtd else "tudo dentro do normal"))
    return assunto, html


@app.route("/admin/relatorio_telegram")
@login_required
@perfil_required("ADM")
def admin_relatorio_telegram():
    conn = conectar()
    config = conn.execute("""
        SELECT supabase_project_url, supabase_anon_key, supabase_telegram_ativo,
               supabase_telegram_horario_envio, supabase_telegram_dias_semana, supabase_telegram_ultimo_envio_data
        FROM configuracoes WHERE id=1
    """).fetchone()
    conn.close()
    dias_marcados = (config["supabase_telegram_dias_semana"] or "SEG,TER,QUA,QUI,SEX").split(",") if config else []
    chat_ids_encontrados = session.pop("chat_ids_encontrados", None)
    return render_template("AdminRelatorioTelegram.html", config=config, dias_marcados=dias_marcados,
                            dias_semana=DIAS_SEMANA, data_hoje=hoje_str(), chat_ids_encontrados=chat_ids_encontrados)


@app.route("/admin/relatorio_telegram/salvar", methods=["POST"])
@login_required
@perfil_required("ADM")
def admin_relatorio_telegram_salvar():
    project_url = (request.form.get("supabase_project_url") or "").strip()
    anon_key = (request.form.get("supabase_anon_key") or "").strip()
    horario = (request.form.get("supabase_telegram_horario_envio") or "18:00").strip()
    ativo = 1 if request.form.get("supabase_telegram_ativo") else 0
    dias_marcados = request.form.getlist("supabase_telegram_dias_semana")
    dias_texto = ",".join(dias_marcados) if dias_marcados else ""

    conn = conectar()
    if anon_key:
        conn.execute("""
            UPDATE configuracoes SET supabase_project_url=?, supabase_anon_key=?, supabase_telegram_horario_envio=?,
                   supabase_telegram_ativo=?, supabase_telegram_dias_semana=?
            WHERE id=1
        """, (project_url, anon_key, horario, ativo, dias_texto))
    else:
        conn.execute("""
            UPDATE configuracoes SET supabase_project_url=?, supabase_telegram_horario_envio=?,
                   supabase_telegram_ativo=?, supabase_telegram_dias_semana=?
            WHERE id=1
        """, (project_url, horario, ativo, dias_texto))
    conn.commit()
    conn.close()
    return redirect("/admin/relatorio_telegram")


@app.route("/admin/relatorio_telegram/liberar_hoje", methods=["POST"])
@login_required
@perfil_required("ADM")
def admin_relatorio_telegram_liberar_hoje():
    conn = conectar()
    conn.execute("UPDATE configuracoes SET supabase_telegram_ultimo_envio_data=NULL WHERE id=1")
    conn.commit()
    conn.close()
    return redirect("/admin/relatorio_telegram?liberado=1")


@app.route("/admin/relatorio_telegram/descobrir_chat_id", methods=["POST"])
@login_required
@perfil_required("ADM")
def admin_relatorio_telegram_descobrir_chat_id():
    import urllib.parse
    project_url = (request.form.get("supabase_project_url") or "").strip()
    anon_key = (request.form.get("supabase_anon_key") or "").strip()

    conn_temp = conectar()
    if not anon_key:
        salvo = conn_temp.execute("SELECT supabase_anon_key FROM configuracoes WHERE id=1").fetchone()
        anon_key = salvo["supabase_anon_key"] if salvo else None
    conn_temp.close()

    sucesso, resultado = descobrir_chat_id_via_supabase(project_url, anon_key)
    if not sucesso:
        return redirect("/admin/relatorio_telegram?erro_envio=" + urllib.parse.quote(str(resultado)))
    if not resultado:
        return redirect("/admin/relatorio_telegram?erro_envio=" + urllib.parse.quote(
            "Nenhuma mensagem encontrada. Manda uma mensagem pro bot (ou no grupo) e tenta de novo."))

    conn = conectar()
    conn.execute("UPDATE configuracoes SET supabase_project_url=? WHERE id=1", (project_url,))
    if anon_key:
        conn.execute("UPDATE configuracoes SET supabase_anon_key=? WHERE id=1", (anon_key,))
    conn.commit()
    conn.close()

    session["chat_ids_encontrados"] = resultado
    return redirect("/admin/relatorio_telegram")


@app.route("/admin/relatorio_telegram/enviar_teste", methods=["POST"])
@login_required
@perfil_required("ADM")
def admin_relatorio_telegram_enviar_teste():
    import urllib.parse
    conn = conectar()
    config = conn.execute("SELECT supabase_project_url, supabase_anon_key FROM configuracoes WHERE id=1").fetchone()
    if not config or not config["supabase_project_url"] or not config["supabase_anon_key"]:
        conn.close()
        return redirect("/admin/relatorio_telegram?erro_envio=" + urllib.parse.quote("Configure a URL do projeto e a Anon Key antes de testar."))

    dados = calcular_dados_relatorio(conn)
    texto = montar_relatorio_telegram(dados)
    sucesso, erro = enviar_relatorio_telegram_via_supabase(config["supabase_project_url"], config["supabase_anon_key"], texto)
    conn.close()
    if sucesso:
        return redirect("/admin/relatorio_telegram?enviado=1")
    return redirect("/admin/relatorio_telegram?erro_envio=" + urllib.parse.quote(str(erro)))


@app.route("/admin/relatorio_telegram/atualizar_cache_agora", methods=["POST"])
@login_required
@perfil_required("ADM")
def admin_relatorio_telegram_atualizar_cache_agora():
    import urllib.parse
    conn = conectar()
    config = conn.execute("SELECT supabase_project_url, supabase_anon_key FROM configuracoes WHERE id=1").fetchone()
    if not config or not config["supabase_project_url"] or not config["supabase_anon_key"]:
        conn.close()
        return redirect("/admin/relatorio_telegram?erro_envio=" + urllib.parse.quote("Configure a URL do projeto e a Anon Key antes de atualizar."))

    dados = calcular_dados_relatorio(conn)
    texto = montar_relatorio_telegram(dados)
    sucesso, erro = salvar_relatorio_para_consulta_via_supabase(config["supabase_project_url"], config["supabase_anon_key"], texto)
    conn.close()
    if sucesso:
        return redirect("/admin/relatorio_telegram?atualizado=1")
    return redirect("/admin/relatorio_telegram?erro_envio=" + urllib.parse.quote(str(erro)))


def _autorizado_rota_interna():
    """Confere o token compartilhado com as Edge Functions do Supabase.
    Aceita tanto no header (X-Token-Relatorio) quanto na query string
    (?token=...), porque chamar via net.http_get do pg_cron é mais simples
    passando na URL."""
    token_recebido = request.headers.get("X-Token-Relatorio") or request.args.get("token")
    return token_recebido == TOKEN_RELATORIO_API


@app.route("/interno/relatorio_telegram")
def interno_relatorio_telegram():
    """Devolve o relatório calculado NA HORA -- chamada pela Edge Function
    webhook-telegram toda vez que alguém manda /status no Telegram. Substitui
    o cache de hora em hora (relatorio_cache no Supabase): agora que o CRM
    tem endereço público, não precisa mais de cache, é sempre o dado atual.
    Não usa @login_required porque quem chama é a Edge Function, não uma
    pessoa logada no navegador -- a proteção aqui é o token."""
    if not _autorizado_rota_interna():
        return jsonify({"ok": False, "erro": "não autorizado"}), 403
    conn = conectar()
    dados = calcular_dados_relatorio(conn)
    texto = montar_relatorio_telegram(dados)
    conn.close()
    return jsonify({"ok": True, "texto": texto})


@app.route("/interno/verificar_envio_diario", methods=["GET", "POST"])
def interno_verificar_envio_diario():
    """Roda a mesma checagem que antes vivia só na thread
    _rotina_telegram_supabase_envio_automatico (dispara o envio pro Telegram
    uma vez por dia, no horário/dias configurados em /admin/relatorio_telegram
    -- idempotente via supabase_telegram_ultimo_envio_data, então não manda
    duas vezes no mesmo dia mesmo se chamada várias vezes). Pensada pra ser
    chamada periodicamente (a cada 10-15min) por um Cron Job do Supabase
    (pg_cron + pg_net), que substitui a thread -- ela não roda hospedado
    (WSGI), então sem esse heartbeat externo o envio automático nunca
    disparava sozinho."""
    if not _autorizado_rota_interna():
        return jsonify({"ok": False, "erro": "não autorizado"}), 403

    # Mesmo pulso alimenta o Painel de Saúde do Sistema (2026-10-17): foto da
    # fila, alertas e limpeza de histórico. Nunca derruba o envio do relatório.
    try:
        from saude_sistema import pulso
        pulso(origem="supabase")
    except Exception as erro_pulso:
        print(f"[saude_sistema] pulso não executado: {erro_pulso}", file=sys.stderr)

    conn = conectar()
    config = conn.execute("""
        SELECT supabase_project_url, supabase_anon_key, supabase_telegram_ativo,
               supabase_telegram_horario_envio, supabase_telegram_dias_semana, supabase_telegram_ultimo_envio_data
        FROM configuracoes WHERE id=1
    """).fetchone()

    if not (config and config["supabase_project_url"] and config["supabase_anon_key"] and config["supabase_telegram_ativo"]):
        conn.close()
        return jsonify({"ok": True, "enviado": False, "motivo": "desativado ou não configurado"})

    agora = datetime.now()
    hoje_str_valor = agora.strftime("%Y-%m-%d")
    abrev_hoje = DIAS_SEMANA[agora.weekday()]
    dias_configurados = (config["supabase_telegram_dias_semana"] or "SEG,TER,QUA,QUI,SEX").split(",")

    horario_configurado = config["supabase_telegram_horario_envio"] or "18:00"
    try:
        hora_alvo = datetime.strptime(horario_configurado, "%H:%M").time()
    except ValueError:
        hora_alvo = time(18, 0)

    ja_enviou_hoje = config["supabase_telegram_ultimo_envio_data"] == hoje_str_valor
    if abrev_hoje not in dias_configurados or agora.time() < hora_alvo or ja_enviou_hoje:
        conn.close()
        return jsonify({"ok": True, "enviado": False})

    dados = calcular_dados_relatorio(conn)
    texto = montar_relatorio_telegram(dados)
    sucesso, erro = enviar_relatorio_telegram_via_supabase(config["supabase_project_url"], config["supabase_anon_key"], texto)
    if sucesso:
        conn.execute("UPDATE configuracoes SET supabase_telegram_ultimo_envio_data=? WHERE id=1", (hoje_str_valor,))
        conn.commit()
    conn.close()
    return jsonify({"ok": bool(sucesso), "enviado": bool(sucesso), "erro": erro})


EXTRACOES_SUBITENS = (
    "extracao_pontualidade", "extracao_tratamentos", "extracao_gpon",
    "extracao_ausencias", "extracao_usuarios", "extracao_followup_atrasado",
    "extracao_quebra",
)


@app.route("/admin/extracao")
@login_required
def admin_extracao():
    # Página junta TODAS as extrações num menu só, cada uma com sua própria
    # sub-permissão (ver EXTRACOES_SUBITENS, reestruturado em 2026-08-26 --
    # antes eram duas permissões guarda-chuva: login/logout/pausas hardcoded
    # ADM/SUPERVISOR, e as outras 4 todas atrás da mesma permissão "extracao").
    # Cada card decide sozinho se aparece; aqui só barra quem não tem acesso a
    # NENHUMA das sub-permissões.
    usuario = usuario_atual()
    if not any(perfil_pode(usuario["perfil"], chave) for chave in EXTRACOES_SUBITENS):
        return "Acesso restrito.", 403
    hoje = datetime.now().strftime("%Y-%m-%d")
    return render_template("AdminExtracao.html", hoje=hoje)


@app.route("/admin/extracao/followup-atrasado")
@login_required
@permissao_required("extracao_followup_atrasado")
def admin_extracao_followup_atrasado():
    """Extração dos contratos parados em followup vencido -- pedido do
    Bruno, 2026-09-09, depois de investigar 147 contratos travados (boa
    parte porque o operador responsável estava ausente no dia). Traz quem
    tá atribuído, se essa pessoa está ausente hoje, e o histórico da última
    tabulação, pra facilitar decidir o que fazer com cada um."""
    horas = request.args.get("horas", "48")
    try:
        horas = int(horas)
    except ValueError:
        horas = 48

    conn = conectar()
    limite_str = (datetime.now() - timedelta(hours=horas)).strftime("%Y-%m-%d %H:%M:%S")
    linhas = conn.execute("""
        SELECT f.contrato, f.operador, f.data_retorno, m.status as status_mailing, m.cidade,
            (SELECT a.tabulacao FROM atividades a WHERE a.contrato = f.contrato ORDER BY a.id DESC LIMIT 1) as ultima_tabulacao,
            (SELECT a.data_inicio FROM atividades a WHERE a.contrato = f.contrato ORDER BY a.id DESC LIMIT 1) as ultima_data_inicio,
            (SELECT a.data_fim FROM atividades a WHERE a.contrato = f.contrato ORDER BY a.id DESC LIMIT 1) as ultima_data_fim
        FROM followup f
        JOIN mailing m ON m.contrato = f.contrato
        WHERE f.status = 'PENDENTE' AND f.data_retorno <= ? AND m.status = 'ENFILEIRADO'
        ORDER BY f.data_retorno ASC
    """, (limite_str,)).fetchall()

    hoje = datetime.now().strftime("%Y-%m-%d")
    registros = []
    for linha in linhas:
        ausente_hoje = "—"
        if linha["operador"]:
            ausencia = conn.execute("""
                SELECT motivo FROM ausencias a JOIN usuarios u ON u.id = a.usuario_id
                WHERE u.nome = ? AND a.data = ?
            """, (linha["operador"], hoje)).fetchone()
            ausente_hoje = ausencia["motivo"] if ausencia else "Não"

        registros.append({
            "Contrato": linha["contrato"],
            "Cidade": linha["cidade"],
            "Pessoa Atribuída": linha["operador"] or "(ninguém -- geral)",
            "Ausente Hoje": ausente_hoje,
            "Status Atual": linha["status_mailing"],
            "Última Tabulação": linha["ultima_tabulacao"],
            "Data Início (última tratativa)": linha["ultima_data_inicio"],
            "Data Fim (última tratativa)": linha["ultima_data_fim"],
            "Followup Vencido Em": linha["data_retorno"],
        })
    conn.close()

    df = pd.DataFrame(registros)
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Followup Atrasado")
    buffer.seek(0)

    nome_arquivo = f"extracao_followup_atrasado_{horas}h_{hoje}.xlsx"
    return send_file(
        buffer,
        as_attachment=True,
        download_name=nome_arquivo,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


@app.route("/admin/extracao/gerar")
@login_required
@permissao_required("extracao_tratamentos")
def admin_extracao_gerar():
    data_inicio = request.args.get("data_inicio")
    data_fim = request.args.get("data_fim")
    if not data_inicio or not data_fim:
        return "Informe data de início e fim.", 400

    conn = conectar()
    linhas = conn.execute("""
        SELECT m.*,
            (SELECT MAX(a.data_fim) FROM atividades a WHERE a.contrato = m.contrato) as ultima_data_fim,
            (SELECT a.tabulacao FROM atividades a WHERE a.contrato = m.contrato ORDER BY a.id DESC LIMIT 1) as ultima_tabulacao,
            (SELECT a.observacao FROM atividades a WHERE a.contrato = m.contrato ORDER BY a.id DESC LIMIT 1) as ultima_observacao
        FROM mailing m
        WHERE m.status IN ('EM_ANDAMENTO', 'CONCLUIDO')
    """).fetchall()
    conn.close()

    registros = []
    for linha in linhas:
        # Data de referência do tratamento: pra concluído é quando foi finalizado,
        # pra em andamento é quando foi distribuído ao operador (ainda em curso)
        if linha["status"] == "CONCLUIDO" and linha["ultima_data_fim"]:
            data_ref_completa = linha["ultima_data_fim"]
        else:
            data_ref_completa = linha["data_distribuicao"] or ""
        data_ref = data_ref_completa[:10]

        if not (data_inicio <= data_ref <= data_fim):
            continue

        registros.append({
            "Contrato": linha["contrato"],
            "Cliente": linha["cliente"],
            "Cidade": linha["cidade"],
            "Status": linha["status"],
            "Operador": linha["operador"],
            "Tentativa": linha["tentativa"],
            "Última Tabulação": linha["ultima_tabulacao"],
            "Última Observação": linha["ultima_observacao"],
            "Data do Tratamento": data_ref_completa,
            "Data Distribuição": linha["data_distribuicao"],
            "Produto TV": linha["produto_tv"],
            "Valor TV": linha["valor_tv"],
            "Produto Internet": linha["produto_virtua"],
            "Valor Internet": linha["valor_virtua"],
            "Produto VOIP": linha["produto_voip"],
            "Valor VOIP": linha["valor_voip"],
            "Valor Total": linha["valor_total"],
        })

    df = pd.DataFrame(registros)
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Tratamentos")
    buffer.seek(0)

    nome_arquivo = f"extracao_tratamentos_{data_inicio}_a_{data_fim}.xlsx"
    return send_file(
        buffer,
        as_attachment=True,
        download_name=nome_arquivo,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/admin/extracao/tratativas")
@login_required
@permissao_required("extracao_tratamentos")
def admin_extracao_tratativas():
    """TODAS as tratativas do período, uma linha por tratativa (atividade) --
    pedido do Bruno, 2026-10-16. A Extração de Tratamentos (/gerar) traz uma
    linha por CONTRATO e só os em andamento/concluídos, então 2ª/3ª
    tentativa, retorno etc. de quem ainda está na fila ficavam de fora.
    Data de referência: quando a tratativa foi salva (data_fim). Inclui uma
    aba de resumo (pessoa x tabulação) e os ajustes de agendamento do período
    (remarcações, que não são tratativas novas e por isso não contam no total)."""
    data_inicio = request.args.get("data_inicio")
    data_fim = request.args.get("data_fim")
    if not data_inicio or not data_fim:
        return "Informe data de início e fim.", 400

    conn = conectar()
    linhas = conn.execute("""
        SELECT a.*, m.cliente, m.cidade, m.node, m.produto_virtua, m.valor_virtua, m.valor_total,
               m.status AS status_atual
        FROM atividades a
        LEFT JOIN mailing m ON (m.id = a.id_mailing OR (a.id_mailing IS NULL AND m.contrato = a.contrato))
        WHERE date(a.data_fim) BETWEEN ? AND ?
        GROUP BY a.id
        ORDER BY a.data_fim
    """, (data_inicio, data_fim)).fetchall()
    ajustes = conn.execute("""
        SELECT * FROM ajustes_agendamento WHERE date(data_hora) BETWEEN ? AND ? ORDER BY data_hora
    """, (data_inicio, data_fim)).fetchall()
    mapa_cod = {}
    for r in conn.execute("SELECT cidade, cod_operadora FROM cidades_operadora ORDER BY cod_operadora"):
        chave = (r["cidade"] or "").strip().upper()
        mapa_cod[chave] = f"{mapa_cod[chave]}, {r['cod_operadora']}" if chave in mapa_cod else r["cod_operadora"]
    conn.close()

    registros = [{
        "Data/Hora da Tratativa": l["data_fim"],
        "Início": l["data_inicio"],
        "Minutos efetivos": round(l["minutos_efetivos"], 1) if l["minutos_efetivos"] is not None else None,
        "Operador": l["operador"],
        "Contrato": l["contrato"],
        "Cliente": l["cliente"],
        "Cidade": l["cidade"],
        "Cód. Operadora": mapa_cod.get((l["cidade"] or "").strip().upper()),
        "Node": l["node"],
        "Tentativa": l["tentativa"],
        "Tabulação": l["tabulacao"],
        "Canal de Contato": l["canal_contato"],
        "Data Agendada": l["data_agendamento"],
        "Janela": l["janela_agendamento"],
        "Responsável (cliente)": l["resp_cliente"],
        "Observação": l["observacao"],
        "Status Atual do Contrato": l["status_atual"],
        "Produto Internet": l["produto_virtua"],
        "Valor Internet": l["valor_virtua"],
        "Valor Total": l["valor_total"],
    } for l in linhas]

    df = pd.DataFrame(registros)
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        if df.empty:
            pd.DataFrame([{"Aviso": "Nenhuma tratativa no período."}]).to_excel(writer, index=False, sheet_name="Tratativas")
        else:
            df.to_excel(writer, index=False, sheet_name="Tratativas")
            resumo = pd.crosstab(df["Operador"].fillna("—"), df["Tabulação"].fillna("—"), margins=True, margins_name="Total")
            resumo.to_excel(writer, sheet_name="Resumo por pessoa")
        pd.DataFrame([{
            "Data/Hora": j["data_hora"], "Feito por": j["operador"], "Contrato": j["contrato"],
            "Data anterior": j["data_agendamento_anterior"], "Janela anterior": j["janela_anterior"],
            "Data nova": j["data_agendamento_nova"], "Janela nova": j["janela_nova"], "Observação": j["observacao"],
        } for j in ajustes] or [{"Aviso": "Nenhum ajuste de agendamento no período."}]).to_excel(
            writer, index=False, sheet_name="Ajustes de agendamento")
    buffer.seek(0)
    return send_file(
        buffer, as_attachment=True,
        download_name=f"extracao_todas_tratativas_{data_inicio}_a_{data_fim}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/admin/extracao/quebra")
@login_required
@permissao_required("extracao_quebra")
def admin_extracao_quebra():
    """Extração COMPLETA da quebra de agenda -- pedido do Bruno, 2026-10-16,
    pra supervisora analisar. Uma linha por OS da base de quebra no período
    (data da agenda), com todas as colunas importadas, o Assistente DONO da
    quebra (mesma regra da fila -- _sql_dono_quebra: agendou até a data da
    quebra, agendamento mais recente), a tratativa registrada e a primeira
    execução do contrato a partir da data da quebra. Abas de resumo por
    Assistente e por motivo."""
    data_inicio = request.args.get("data_inicio")
    data_fim = request.args.get("data_fim")
    so_com_quebra = request.args.get("so_com_quebra") == "1"
    if not data_inicio or not data_fim:
        return "Informe data de início e fim.", 400

    filtro_tipo = "AND q.tipo_tratamento = 'COM QUEBRA DE AGENDA'" if so_com_quebra else ""
    conn = conectar()
    linhas = conn.execute(f"""
        SELECT q.*,
               {_sql_dono_quebra('q')} AS dono,
               qt.operador AS trat_operador, qt.tabulacao AS trat_tabulacao, qt.submotivo AS trat_submotivo,
               qt.observacao AS trat_observacao, qt.data_tratamento AS trat_data,
               (SELECT MIN(e.dh_baixa) FROM execucoes_os e
                 WHERE e.contrato = q.contrato AND e.dh_baixa >= q.dat_agenda) AS execucao_depois
        FROM quebras_agenda q
        LEFT JOIN quebras_tratamento qt ON qt.quebra_id = q.id
        WHERE q.dat_agenda BETWEEN ? AND ? {filtro_tipo}
        ORDER BY q.dat_agenda, q.cidade, q.contrato
    """, (data_inicio, data_fim)).fetchall()
    conn.close()

    def situacao(l):
        if l["tipo_tratamento"] != "COM QUEBRA DE AGENDA":
            return "Não se aplica"
        if l["trat_tabulacao"]:
            return "Tratada"
        return "Pendente" if l["dono"] else "Sem dono no time"

    registros = []
    for l in linhas:
        dias_ate_tratar = None
        if l["trat_data"] and l["dat_agenda"]:
            try:
                dias_ate_tratar = (datetime.strptime(l["trat_data"][:10], "%Y-%m-%d")
                                   - datetime.strptime(l["dat_agenda"][:10], "%Y-%m-%d")).days
            except ValueError:
                pass
        registros.append({
            "Data da Agenda": l["dat_agenda"],
            "Cód. OS": l["cod_os"],
            "Contrato": l["contrato"],
            "Cód. Operadora": l["cod_operadora"],
            "Cidade": l["cidade"],
            "Segmento": l["segmento"],
            "Canal de Venda": l["canal_venda"],
            "Canal de Venda (original)": l["canal_venda_original"],
            "Tipo": l["tipo_tratamento"],
            "Responsável pela Quebra": l["responsavel_quebra"],
            "Agrupamento": l["agrupamento_quebra"],
            "Sub-agrupamento": l["sub_agrupamento"],
            "Motivo do Reagendamento": l["motivo_reagendamento"],
            "Técnico": l["tecnico"],
            "Assistente (dono da quebra)": l["dono"],
            "Situação": situacao(l),
            "Tratada por": l["trat_operador"],
            "Tratativa": l["trat_tabulacao"],
            "Submotivo": l["trat_submotivo"],
            "Observação da Tratativa": l["trat_observacao"],
            "Data da Tratativa": l["trat_data"],
            "Dias até tratar": dias_ate_tratar,
            "Executado depois da quebra em": l["execucao_depois"],
            "Importado em": l["data_importacao"],
        })

    df = pd.DataFrame(registros)
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        if df.empty:
            pd.DataFrame([{"Aviso": "Nenhuma OS de quebra no período."}]).to_excel(writer, index=False, sheet_name="Quebras")
        else:
            df.to_excel(writer, index=False, sheet_name="Quebras")
            com = df[df["Tipo"] == "COM QUEBRA DE AGENDA"].copy()
            if not com.empty:
                com["Assistente"] = com["Assistente (dono da quebra)"].fillna("(sem dono no time)")
                resumo = com.groupby("Assistente").agg(
                    Quebras=("Cód. OS", "count"),
                    Tratadas=("Tratativa", lambda x: x.notna().sum()),
                    Reagendadas=("Tratativa", lambda x: (x == "Reagendado").sum()),
                    Canceladas=("Tratativa", lambda x: (x == "Cancelado").sum()),
                    Executadas_depois=("Executado depois da quebra em", lambda x: x.notna().sum()),
                    Media_dias_ate_tratar=("Dias até tratar", "mean"),
                ).reset_index()
                resumo["Pendentes"] = resumo["Quebras"] - resumo["Tratadas"]
                resumo["Media_dias_ate_tratar"] = resumo["Media_dias_ate_tratar"].round(1)
                resumo.rename(columns={"Executadas_depois": "Executadas depois",
                                       "Media_dias_ate_tratar": "Média de dias até tratar"}).to_excel(
                    writer, index=False, sheet_name="Resumo por Assistente")
                motivos = com.groupby(["Agrupamento", "Motivo do Reagendamento"], dropna=False).size().reset_index(name="Quebras")
                motivos.sort_values("Quebras", ascending=False).to_excel(writer, index=False, sheet_name="Por motivo")
    buffer.seek(0)
    sufixo = "_com_quebra" if so_com_quebra else ""
    return send_file(
        buffer, as_attachment=True,
        download_name=f"extracao_quebra{sufixo}_{data_inicio}_a_{data_fim}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/admin/extracao/gpon")
@login_required
@permissao_required("extracao_gpon")
def admin_extracao_gpon():
    data_inicio = request.args.get("data_inicio")
    data_fim = request.args.get("data_fim")
    if not data_inicio or not data_fim:
        return "Informe data de início e fim.", 400

    conn = conectar()
    linhas = conn.execute("""
        SELECT a.contrato, a.operador AS analista, a.tabulacao, a.observacao, a.data_fim,
               m.cliente, m.cidade
        FROM atividades a
        JOIN mailing m ON m.contrato = a.contrato
        WHERE a.tabulacao LIKE 'Analisado%' OR a.tabulacao = 'Análise Pendente'
        ORDER BY a.data_fim DESC
    """).fetchall()
    conn.close()

    registros = []
    for linha in linhas:
        data_ref = (linha["data_fim"] or "")[:10]
        if not (data_inicio <= data_ref <= data_fim):
            continue
        registros.append({
            "Contrato": linha["contrato"],
            "Cliente": linha["cliente"],
            "Cidade": linha["cidade"],
            "Analista": linha["analista"],
            "Resultado da análise": linha["tabulacao"],
            "Observação": linha["observacao"],
            "Data": linha["data_fim"],
        })

    df = pd.DataFrame(registros)
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Tratamento GPON")
    buffer.seek(0)

    nome_arquivo = f"extracao_tratamento_gpon_{data_inicio}_a_{data_fim}.xlsx"
    return send_file(
        buffer,
        as_attachment=True,
        download_name=nome_arquivo,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/admin/extracao/ausencias")
@login_required
@permissao_required("extracao_ausencias")
def admin_extracao_ausencias():
    data_inicio = request.args.get("data_inicio")
    data_fim = request.args.get("data_fim")
    if not data_inicio or not data_fim:
        return "Informe data de início e fim.", 400

    conn = conectar()
    linhas = conn.execute("""
        SELECT a.*, u.nome as nome_usuario, u.perfil as perfil_usuario
        FROM ausencias a
        LEFT JOIN usuarios u ON u.id = a.usuario_id
        WHERE a.data BETWEEN ? AND ?
        ORDER BY a.data, u.nome
    """, (data_inicio, data_fim)).fetchall()
    conn.close()

    registros = [{
        "Nome": linha["nome_usuario"],
        "Perfil": linha["perfil_usuario"],
        "Data da Falta": linha["data"],
        "Motivo": linha["motivo"],
        "Submotivo": linha["submotivo"] or "",
        "Observação": linha["observacao"] or "",
        "Registrado por": linha["registrado_por"],
        "Registrado em": linha["data_registro"],
    } for linha in linhas]

    df = pd.DataFrame(registros)
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Ausencias")
    buffer.seek(0)

    nome_arquivo = f"extracao_ausencias_{data_inicio}_a_{data_fim}.xlsx"
    return send_file(
        buffer,
        as_attachment=True,
        download_name=nome_arquivo,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/admin/extracao/usuarios")
@login_required
@permissao_required("extracao_usuarios")
def admin_extracao_usuarios():
    conn = conectar()
    linhas = conn.execute("""
        SELECT nome, login, email, perfil, celular, ramal, ativo, ultimo_ip
        FROM usuarios ORDER BY nome
    """).fetchall()
    conn.close()

    registros = [{
        "Nome": linha["nome"],
        "Login": linha["login"],
        "E-mail": linha["email"] or "",
        "Perfil": exibir_perfil(linha["perfil"]),
        "Celular": linha["celular"] or "",
        "Ramal": linha["ramal"] or "",
        "Ativo": "Sim" if linha["ativo"] else "Não",
        "Último IP": linha["ultimo_ip"] or "",
    } for linha in linhas]

    df = pd.DataFrame(registros)
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Usuarios")
    buffer.seek(0)

    nome_arquivo = f"extracao_usuarios_{datetime.now().strftime('%Y-%m-%d')}.xlsx"
    return send_file(
        buffer,
        as_attachment=True,
        download_name=nome_arquivo,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )



# ==========================
# PAGINAÇÃO (pedido do Bruno, 2026-08-28: telas de listagem grande não devem
# carregar tudo de uma vez -- 100 registros por página, com navegação
# numerada). Compartilhado entre Histórico de Tratamento e Agendado x
# Executado (abaixo), e qualquer listagem futura que precisar do mesmo padrão.
# ==========================

POR_PAGINA_PADRAO = 100


def _paginar(request_args, total, por_pagina=POR_PAGINA_PADRAO):
    """A partir do parâmetro 'pagina' da querystring e do total de registros
    que bateram o filtro, devolve (pagina_atual, total_paginas, offset) --
    offset já pronto pra usar em LIMIT/OFFSET na consulta. 'pagina' fora do
    intervalo válido (0, negativo, além do total, não numérico) é
    silenciosamente ajustado pro mais próximo válido, não dá erro 400."""
    try:
        pagina = int(request_args.get("pagina") or 1)
    except (TypeError, ValueError):
        pagina = 1
    total_paginas = max(1, math.ceil(total / por_pagina)) if total else 1
    pagina = max(1, min(pagina, total_paginas))
    offset = (pagina - 1) * por_pagina
    return pagina, total_paginas, offset


def _paginas_para_exibir(atual, total, janela=2):
    """Lista de números de página pra navegação (sempre inclui a 1ª, a
    última, e uma janela em volta da atual) -- None representa uma
    reticência ("..."), pra não poluir a tela quando há muitas páginas."""
    if total <= 0:
        return []
    paginas = {1, total, atual}
    for i in range(atual - janela, atual + janela + 1):
        if 1 <= i <= total:
            paginas.add(i)
    paginas_ordenadas = sorted(paginas)
    resultado = []
    anterior = None
    for p in paginas_ordenadas:
        if anterior is not None and p - anterior > 1:
            resultado.append(None)
        resultado.append(p)
        anterior = p
    return resultado


def _querystring_sem_pagina(request_args):
    """Querystring atual (filtros da tela) sem o parâmetro 'pagina' -- usada
    nos links de navegação de página, que só precisam trocar esse parâmetro
    sem perder os filtros já aplicados."""
    import urllib.parse
    partes = [(k, v) for k, v in request_args.items() if k != "pagina" and v not in (None, "")]
    return urllib.parse.urlencode(partes)


# ==========================
# HISTÓRICO DE TRATAMENTO (Gestão) -- tela em /gestao/historico_tratamento,
# pedida pelo Bruno em 2026-08-27: visão pro supervisor acompanhar o que cada
# pessoa andou tratando, com filtro por operador/data/tabulação (diferente da
# Extração de Tratamentos, que é um Excel por contrato -- aqui é uma tabela na
# tela, uma linha por tratativa registrada em `atividades`).
# ==========================

def _historico_tratamento_categoria(status, tabulacao):
    """Classifica cada linha em 3 categorias (pra cor da linha e pro rótulo de
    Status): 'Agendado' é sempre a própria tabulação (mesmo sendo um
    CONCLUIDO por baixo -- é a categoria que o Bruno pediu pra destacar em
    verde); os demais CONCLUIDO (Recusado, Contrato Desabilitado, Improdutivo,
    Contrato Coletivo etc.) ficam cinza; qualquer outro status (ENFILEIRADO --
    2ª/3ª Tentativa ou Retorno --, AGUARDANDO_ANALISE_GPON, PENDENTE da fila de
    GPON) significa que o caso ainda não fechou -- fica em vermelho claro
    como "em andamento"."""
    if tabulacao == "Agendado":
        return "agendado", "Agendado"
    if status == "CONCLUIDO":
        return "concluido", "Concluído"
    rotulos = {
        "ENFILEIRADO": "Em andamento (aguardando retorno)",
        "AGUARDANDO_ANALISE_GPON": "Em andamento (aguardando análise GPON)",
        "PENDENTE": "Em andamento (fila GPON)",
        "BLOQUEADO_GED": "Fora da fila (venda não liberada no GED)",
    }
    return "andamento", rotulos.get(status, status or "—")


@app.route("/gestao/historico_tratamento")
@login_required
@permissao_required("historico_tratamento")
def gestao_historico_tratamento():
    hoje = datetime.now().strftime("%Y-%m-%d")
    data_de = request.args.get("data_de") or hoje
    data_ate = request.args.get("data_ate") or hoje
    operador_filtro = request.args.get("operador") or ""
    tabulacao_filtro = request.args.get("tabulacao") or ""

    conn = conectar()
    operadores = [l["operador"] for l in conn.execute("""
        SELECT DISTINCT operador FROM atividades
        WHERE operador IS NOT NULL AND operador != '' ORDER BY operador
    """).fetchall()]
    tabulacoes = [l["tabulacao"] for l in conn.execute("""
        SELECT DISTINCT tabulacao FROM atividades
        WHERE tabulacao IS NOT NULL AND tabulacao != '' ORDER BY tabulacao
    """).fetchall()]

    # Filtra por data_fim (quando a tratativa foi concluída), não data_inicio
    # (quando começou) -- a pedido do Bruno em 2026-08-27, pra bater com o
    # mesmo critério de "data" já usado no resto do app (Dashboard etc. também
    # agrupam por data_fim, não data_inicio -- ver calcular_ranking()).
    filtro_sql = " WHERE date(a.data_fim) BETWEEN ? AND ?"
    parametros = [data_de, data_ate]
    if operador_filtro:
        filtro_sql += " AND a.operador = ?"
        parametros.append(operador_filtro)
    if tabulacao_filtro:
        filtro_sql += " AND a.tabulacao = ?"
        parametros.append(tabulacao_filtro)

    total = conn.execute(f"SELECT COUNT(*) c FROM atividades a{filtro_sql}", parametros).fetchone()["c"]
    pagina, total_paginas, offset = _paginar(request.args, total)

    linhas = conn.execute(f"""
        SELECT a.operador, a.contrato, m.cidade, a.tabulacao, a.data_inicio, a.data_fim, a.status
        FROM atividades a
        LEFT JOIN mailing m ON m.contrato = a.contrato
        {filtro_sql}
        ORDER BY a.data_fim DESC
        LIMIT ? OFFSET ?
    """, parametros + [POR_PAGINA_PADRAO, offset]).fetchall()
    conn.close()

    registros = []
    for l in linhas:
        categoria, status_label = _historico_tratamento_categoria(l["status"], l["tabulacao"])
        registros.append({
            "operador": l["operador"],
            "contrato": l["contrato"],
            "cidade": l["cidade"] or "—",
            "tabulacao": l["tabulacao"],
            "data_inicio": l["data_inicio"],
            "data_fim": l["data_fim"],
            "status_label": status_label,
            "categoria": categoria,
        })

    return render_template(
        "HistoricoTratamento.html",
        registros=registros, total=total,
        operadores=operadores, tabulacoes=tabulacoes,
        data_de=data_de, data_ate=data_ate,
        operador_filtro=operador_filtro, tabulacao_filtro=tabulacao_filtro,
        pagina=pagina, total_paginas=total_paginas, paginas_exibir=_paginas_para_exibir(pagina, total_paginas),
        querystring_sem_pagina=_querystring_sem_pagina(request.args),
    )


# ==========================
# IMPORTAÇÃO DE PLANILHAS (Mailing e Usuários) -- tela em /admin/importacao
# Reaproveita a mesma lógica dos scripts importar_mailing.py e importar_usuarios.py,
# mas lendo o arquivo direto do upload do navegador (sem precisar subir pela aba
# Files e rodar script pelo console Bash toda vez).
# ==========================

def _imp_limpo_texto(valor):
    """Converte célula da planilha em texto seguro, tratando NaN/None como string vazia."""
    if valor is None:
        return ""
    if isinstance(valor, float) and math.isnan(valor):
        return ""
    texto = str(valor).strip()
    return "" if texto.lower() == "nan" else texto


def _imp_limpo_telefone(valor):
    """Como _imp_limpo_texto, mas remove o '.0' que aparece quando a coluna vira float por causa de células vazias."""
    if valor is None:
        return ""
    if isinstance(valor, float):
        if math.isnan(valor):
            return ""
        if valor.is_integer():
            return str(int(valor))
        return str(valor)
    texto = str(valor).strip()
    return "" if texto.lower() == "nan" else texto


def _imp_limpo_numero(valor):
    """Converte célula da planilha em número seguro, tratando NaN/None/inválido como 0.0."""
    if valor is None:
        return 0.0
    try:
        valor = float(valor)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if math.isnan(valor) else valor


def _imp_limpa_prioridade(valor):
    """Prioridade vai de 0 (menor) a 100 (maior). Qualquer coisa fora disso ou ausente vira 0."""
    numero = _imp_limpo_numero(valor)
    numero = int(round(numero))
    return max(0, min(100, numero))


def _imp_pegar_coluna(linha, *nomes_possiveis):
    """Tenta achar o valor testando várias variações de nome de coluna (com
    espaço, com underscore, maiúsculo/minúsculo) -- assim não trava se a
    planilha usar uma grafia um pouco diferente da esperada."""
    for nome in nomes_possiveis:
        if nome in linha and pd.notna(linha[nome]):
            return linha[nome]
    chaves_normalizadas = {str(k).strip().lower().replace(" ", "_").replace("-", "_"): k for k in linha.index}
    for nome in nomes_possiveis:
        chave_normalizada = nome.strip().lower().replace(" ", "_").replace("-", "_")
        if chave_normalizada in chaves_normalizadas:
            valor = linha[chaves_normalizadas[chave_normalizada]]
            if pd.notna(valor):
                return valor
    return None


def _imp_converter_data_nascimento(valor):
    """Aceita DD/MM/AAAA (texto ou já como data do Excel) e devolve AAAA-MM-DD, ou None se vier vazio/inválido."""
    texto = _imp_limpo_texto(valor)
    if not texto:
        return None
    if hasattr(valor, "strftime"):  # já veio como data/timestamp do pandas
        return valor.strftime("%Y-%m-%d")
    for formato in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(texto, formato).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


COLUNAS_OBRIGATORIAS_MAILING = [
    "NUM_CONTRATO", "NOME_ASSINANTE", "NOME_CIDADE", "NODE",
    "CELULAR", "TEL_RES", "TEL_COM", "E_MAIL", "COD_IMOVEL",
    "PRODUTO_VIRTUA", "VALOR_VIRTUA", "VALOR_TOTAL_COBRANCA",
]

PERFIS_VALIDOS_IMPORTACAO = {"OPERADOR", "SUPERVISOR", "ADM", "ANALISTA", "ANALISTA_II", "VISUALIZACAO", "ASSISTENTE_CLARO", "OPERADOR_CONF"}


@app.route("/admin/importacao")
@login_required
@perfil_required("ADM")
def admin_importacao():
    return render_template("AdminImportacao.html", colunas_mailing=COLUNAS_OBRIGATORIAS_MAILING)


@app.route("/admin/importacao/mailing", methods=["POST"])
@login_required
@perfil_required("ADM")
def admin_importacao_mailing():
    import urllib.parse
    usuario = usuario_atual()
    arquivo = request.files.get("arquivo")
    if not arquivo or arquivo.filename == "":
        return redirect("/admin/importacao?erro_mailing=" + urllib.parse.quote("Nenhum arquivo selecionado."))

    try:
        df = pd.read_csv(arquivo) if arquivo.filename.lower().endswith(".csv") else pd.read_excel(arquivo)
    except Exception as erro:
        return redirect("/admin/importacao?erro_mailing=" + urllib.parse.quote(f"Não consegui ler o arquivo: {erro}"))

    faltando = [c for c in COLUNAS_OBRIGATORIAS_MAILING if c not in df.columns]
    if faltando:
        return redirect("/admin/importacao?erro_mailing=" + urllib.parse.quote(
            "A planilha está sem a(s) coluna(s): " + ", ".join(faltando)))

    conn = conectar()
    novos = atualizados = reabertos = ignorados = erros = 0
    chaves_da_planilha = set()  # (contrato, cidade) -- ver comentário na chave composta abaixo
    agora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for _, linha in df.iterrows():
        try:
            contrato = _imp_limpo_texto(linha["NUM_CONTRATO"])
            if not contrato:
                ignorados += 1
                continue  # sem número de contrato não dá pra identificar o registro

            valor_virtua = _imp_limpo_numero(linha["VALOR_VIRTUA"])
            valor_total = _imp_limpo_numero(linha["VALOR_TOTAL_COBRANCA"])
            prioridade = _imp_limpa_prioridade(linha.get("PRIORIDADE", 0))

            # TV, ponto adicional e VOIP não vêm da planilha: ficam em branco/zerados
            # na criação e são preenchidos manualmente depois -- por isso não entram
            # no UPDATE, pra reimportação não sobrescrever o que já foi preenchido.
            dados = {
                "contrato": contrato,
                "cliente": _imp_limpo_texto(linha["NOME_ASSINANTE"]),
                "cidade": _imp_limpo_texto(linha["NOME_CIDADE"]),
                "node": _imp_limpo_texto(linha["NODE"]),
                "endereco": _imp_limpo_texto(linha.get("ENDERECO", "")),
                "complemento": _imp_limpo_texto(linha.get("COMPLEMENTO", "")),
                "telefone_celular": _imp_limpo_telefone(linha["CELULAR"]),
                "telefone_residencial": _imp_limpo_telefone(linha["TEL_RES"]),
                "telefone_comercial": _imp_limpo_telefone(linha["TEL_COM"]),
                "email": _imp_limpo_texto(linha["E_MAIL"]),
                "codigo_imovel": _imp_limpo_texto(linha["COD_IMOVEL"]),
                "produto_virtua": _imp_limpo_texto(linha["PRODUTO_VIRTUA"]),
                "valor_virtua": valor_virtua,
                "valor_total": valor_total,
                "prioridade": prioridade,
                "data_entrada_fila": agora_str,
            }
            chaves_da_planilha.add((contrato, dados["cidade"]))

            # Chave composta contrato+cidade (não só contrato): o mesmo número
            # de contrato pode existir em cidades diferentes (5 casos reais
            # confirmados na base, ex: 232849156 em Araraquara E Aracatuba --
            # ambos criados legitimamente por Nova Tarefa, que já dedupa por
            # contrato+cidade). Casar só por contrato aqui fazia esse UPDATE
            # atingir a linha ERRADA (ou as duas de uma vez, já que UPDATE
            # afeta toda linha que bate no WHERE): reimportar a planilha
            # sobrescrevia cliente/telefone/endereço da linha da outra cidade
            # com os dados de quem não tem nada a ver -- comprovado com
            # 232849156 (Bruno, 2026-09-20). Mesmo critério já usado em
            # _processar_nova_tarefa (Nova Tarefa) e em _chave_cidade_contrato
            # (Quebra de Agenda).
            existente = conn.execute(
                "SELECT id, status, operador FROM mailing WHERE contrato=? AND cidade=?",
                (contrato, dados["cidade"])
            ).fetchone()

            # Exceção: contrato CONCLUIDO cuja última tabulação registrada foi
            # "Improdutivo" volta pra fila (status PENDENTE) na reimportação --
            # pedido do Bruno (2026-08-25). Não distribui de volta pra mesma
            # pessoa: só solta pra fila geral, com prioridade nova e tentativa
            # reiniciada (novo ciclo de distribuição), guardando quem tratou da
            # última vez em ultimo_operador (mesmo critério de desempate usado
            # em toda reabertura de fila) e sem apagar nenhum histórico em
            # atividades/checklist_improdutivo.
            reabrir = False
            if existente and existente["status"] == "CONCLUIDO":
                ultima_atividade = conn.execute(
                    "SELECT tabulacao FROM atividades WHERE id_mailing=? ORDER BY id DESC LIMIT 1",
                    (existente["id"],)
                ).fetchone()
                if ultima_atividade and ultima_atividade["tabulacao"] == "Improdutivo":
                    reabrir = True

            if existente and reabrir:
                dados["ultimo_operador"] = existente["operador"]
                conn.execute("""
                    UPDATE mailing SET
                        cliente=:cliente, cidade=:cidade, node=:node,
                        endereco=:endereco, complemento=:complemento,
                        telefone_celular=:telefone_celular, telefone_residencial=:telefone_residencial,
                        telefone_comercial=:telefone_comercial, email=:email, codigo_imovel=:codigo_imovel,
                        produto_virtua=:produto_virtua, valor_virtua=:valor_virtua, valor_total=:valor_total,
                        prioridade=:prioridade,
                        status='PENDENTE', tentativa=1, operador=NULL, reservado_para=NULL,
                        ultimo_operador=:ultimo_operador, data_distribuicao=NULL, data_expiracao=NULL,
                        data_entrada_fila=:data_entrada_fila
                    WHERE id=:id_existente
                """, {**dados, "id_existente": existente["id"]})
                reabertos += 1
            elif existente:
                # Upsert: atualiza os dados "informativos". NUNCA mexe em
                # status/tentativa/operador/datas -- pra não zerar o andamento
                # de um atendimento que já está em curso ou enfileirado.
                # Escopado por id (não mais só contrato) pelo mesmo motivo
                # acima -- atinge só a linha certa.
                conn.execute("""
                    UPDATE mailing SET
                        cliente=:cliente, cidade=:cidade, node=:node,
                        endereco=:endereco, complemento=:complemento,
                        telefone_celular=:telefone_celular, telefone_residencial=:telefone_residencial,
                        telefone_comercial=:telefone_comercial, email=:email, codigo_imovel=:codigo_imovel,
                        produto_virtua=:produto_virtua, valor_virtua=:valor_virtua, valor_total=:valor_total,
                        prioridade=:prioridade
                    WHERE id=:id_existente
                """, {**dados, "id_existente": existente["id"]})
                atualizados += 1
            else:
                conn.execute("""
                    INSERT INTO mailing (
                        contrato, cliente, cidade, node, endereco, complemento,
                        telefone_celular, telefone_residencial, telefone_comercial,
                        email, codigo_imovel,
                        produto_tv, valor_tv, valor_ponto_adicional,
                        produto_virtua, valor_virtua,
                        produto_voip, valor_voip, valor_total,
                        status, tentativa, prioridade, data_entrada_fila
                    ) VALUES (
                        :contrato, :cliente, :cidade, :node, :endereco, :complemento,
                        :telefone_celular, :telefone_residencial, :telefone_comercial,
                        :email, :codigo_imovel,
                        '', 0, 0,
                        :produto_virtua, :valor_virtua,
                        '', 0, :valor_total,
                        'PENDENTE', 1, :prioridade, :data_entrada_fila
                    )
                """, dados)
                novos += 1
                conn.execute(
                    "INSERT INTO historico_importacao_mailing (contrato, importado_por, importado_em) VALUES (?, ?, ?)",
                    (contrato, usuario["nome"], datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                )
        except Exception:
            erros += 1

    # Contrato ainda não tratado (PENDENTE ou ENFILEIRADO) que estava na
    # ferramenta mas NÃO voltou nessa importação nova -- provavelmente saiu
    # da base de origem (não é mais um lead válido pra insistir). Em vez de
    # excluir (perderia o histórico se ele voltar um dia), zera a
    # prioridade -- ele desce pro fim da fila sozinho, sem nunca ser
    # distribuído enquanto tiver qualquer outro com prioridade > 0. Pedido
    # do Bruno, 2026-09-11.
    # Chave composta (contrato, cidade) aqui também -- por contrato só, uma
    # linha PENDENTE de uma cidade "sumiria" da lista de zerar só por outra
    # cidade com o mesmo número de contrato ainda estar na planilha nova.
    conn.execute("DROP TABLE IF EXISTS temp_contratos_planilha")
    conn.execute("CREATE TEMP TABLE temp_contratos_planilha (contrato TEXT, cidade TEXT, PRIMARY KEY (contrato, cidade))")
    conn.executemany(
        "INSERT OR IGNORE INTO temp_contratos_planilha (contrato, cidade) VALUES (?, ?)",
        list(chaves_da_planilha)
    )
    cursor_zerados = conn.execute("""
        UPDATE mailing SET prioridade = 0
        WHERE status IN ('PENDENTE', 'ENFILEIRADO')
          AND prioridade != 0
          AND (contrato, cidade) NOT IN (SELECT contrato, cidade FROM temp_contratos_planilha)
    """)
    zerados = cursor_zerados.rowcount
    conn.execute("DROP TABLE temp_contratos_planilha")

    conn.commit()
    conn.close()

    return redirect(
        f"/admin/importacao?mailing_novos={novos}&mailing_atualizados={atualizados}"
        f"&mailing_reabertos={reabertos}"
        f"&mailing_ignorados={ignorados}&mailing_erros={erros}&mailing_zerados={zerados}"
    )


@app.route("/admin/importacao/confirmacao-agenda", methods=["POST"])
@login_required
@perfil_required("ADM")
def admin_importacao_confirmacao_agenda():
    """Importação do módulo Confirmação de Agenda -- delega toda a lógica
    pesada (dedupe, resolução de DDD, regra de atualização de data) pra
    processar_importacao_confirmacao() em confirmacao_agenda.py. Essa rota
    só cuida do upload e de mostrar o resultado -- pedido do Bruno,
    2026-09-16, mínimo de lógica nova aqui no app.py."""
    import urllib.parse
    from confirmacao_agenda import processar_importacao_confirmacao

    arquivo = request.files.get("arquivo")
    if not arquivo or arquivo.filename == "":
        return redirect("/admin/importacao?erro_conf=" + urllib.parse.quote("Nenhum arquivo selecionado."))

    # Lido uma vez só em memória: a mesma planilha alimenta a Confirmação de
    # Agenda E a sincronização dos agendamentos (reagendamentos fora do CRM,
    # 2026-10-16 -- ver sincroniza_agenda.py).
    conteudo = arquivo.read()
    try:
        resultado = processar_importacao_confirmacao(io.BytesIO(conteudo), os.path.join(PASTA_BASE, "crm.db"))
    except Exception as erro:
        return redirect("/admin/importacao?erro_conf=" + urllib.parse.quote(f"Não consegui processar o arquivo: {erro}"))

    if "erro" in resultado:
        return redirect("/admin/importacao?erro_conf=" + urllib.parse.quote(resultado["erro"]))

    sinc_params = ""
    try:
        from sincroniza_agenda import sincronizar_com_planilha
        conn_sinc = conectar()
        sinc = sincronizar_com_planilha(conn_sinc, pd.read_excel(io.BytesIO(conteudo)),
                                        usuario_atual()["nome"], arquivo.filename)
        conn_sinc.close()
        if sinc:
            sinc_params = (f"&sinc_ajustados={sinc['ajustados']}&sinc_protegidos={sinc['protegidos']}"
                           f"&sinc_ausentes={sinc['ausentes']}")
    except Exception as erro:  # a importação da Confirmação já foi gravada -- não perde por causa disso
        print(f"[sincroniza_agenda] falhou: {erro}", file=sys.stderr)
        sinc_params = "&sinc_erro=" + urllib.parse.quote(str(erro)[:200])

    return redirect(
        f"/admin/importacao?conf_novos={resultado['novos']}&conf_atualizados={resultado['atualizados']}"
        f"&conf_ignorados={resultado['ignorados']}&conf_sem_ddd={resultado['sem_ddd']}"
        f"&conf_em_andamento={resultado['em_andamento_preservados']}" + sinc_params
    )


@app.route("/admin/importacao/backlog", methods=["POST"])
@login_required
@perfil_required("ADM")
def admin_importacao_backlog():
    """Importação do módulo Tratamento de Backlog -- delega toda a lógica
    pesada (dedupe por CD_OPERADORA+CD_CONTRATO, filtro de AGING, resolução
    de DDD, reconciliação de reimportação) pra processar_importacao_backlog()
    em backlog.py. Essa rota só cuida do upload e de mostrar o resultado --
    pedido do Bruno, 2026-09-19, mínimo de lógica nova aqui no app.py."""
    import urllib.parse
    from backlog import processar_importacao_backlog

    arquivo = request.files.get("arquivo")
    if not arquivo or arquivo.filename == "":
        return redirect("/admin/importacao?erro_backlog=" + urllib.parse.quote("Nenhum arquivo selecionado."))

    try:
        resultado = processar_importacao_backlog(arquivo, os.path.join(PASTA_BASE, "crm.db"), usuario_atual()["nome"])
    except Exception as erro:
        return redirect("/admin/importacao?erro_backlog=" + urllib.parse.quote(f"Não consegui processar o arquivo: {erro}"))

    if "erro" in resultado:
        return redirect("/admin/importacao?erro_backlog=" + urllib.parse.quote(resultado["erro"]))

    return redirect(
        f"/admin/importacao?backlog_novos={resultado['novos']}&backlog_reenfileirados={resultado['reenfileirados']}"
        f"&backlog_resolvidos={resultado['resolvidos']}&backlog_sem_ddd={resultado['sem_ddd']}"
        f"&backlog_elegiveis={resultado['total_elegiveis']}"
    )


@app.route("/admin/importacao/usuarios", methods=["POST"])
@login_required
@perfil_required("ADM")
def admin_importacao_usuarios():
    import urllib.parse
    arquivo = request.files.get("arquivo")
    if not arquivo or arquivo.filename == "":
        return redirect("/admin/importacao?erro_usuarios=" + urllib.parse.quote("Nenhum arquivo selecionado."))

    try:
        df = pd.read_csv(arquivo) if arquivo.filename.lower().endswith(".csv") else pd.read_excel(arquivo)
    except Exception as erro:
        return redirect("/admin/importacao?erro_usuarios=" + urllib.parse.quote(f"Não consegui ler o arquivo: {erro}"))

    conn = conectar()
    conn.execute("PRAGMA busy_timeout=10000")
    novos = atualizados = ignorados = reativados = 0

    for _, linha in df.iterrows():
        nome = _imp_limpo_texto(_imp_pegar_coluna(linha, "Nome"))
        login = _imp_limpo_texto(_imp_pegar_coluna(linha, "Login")).lower()
        email = _imp_limpo_texto(_imp_pegar_coluna(linha, "Email"))
        perfil = _imp_limpo_texto(_imp_pegar_coluna(linha, "Perfil")).upper().replace(" ", "_")
        perfil = perfil.replace("Ç", "C").replace("Ã", "A")  # tolera "Visualização" digitado com acento
        data_nascimento = _imp_converter_data_nascimento(
            _imp_pegar_coluna(linha, "Data Nascimento", "data_nascimento", "Data de Nascimento")
        )

        if not login or not nome:
            ignorados += 1
            continue

        if perfil not in PERFIS_VALIDOS_IMPORTACAO:
            perfil = "OPERADOR"

        existente = conn.execute(
            "SELECT id, COALESCE(excluido,0) as excluido FROM usuarios WHERE login=?", (login,)
        ).fetchone()

        if existente and existente["excluido"]:
            # Login já existiu antes mas foi Excluído (ver Supervisão) -- reimportar
            # "recria" a pessoa: reativa a conta, limpa a marca de exclusão e volta
            # com senha inicial = login (igual conta nova), já que a senha antiga não
            # é reaproveitável depois de excluída. Histórico dela não foi mexido pela
            # exclusão, então nem precisa ser tocado aqui.
            senha_inicial = generate_password_hash(login)
            conn.execute(
                "UPDATE usuarios SET nome=?, email=?, perfil=?, data_nascimento=COALESCE(?, data_nascimento), "
                "ativo=1, excluido=0, excluido_em=NULL, excluido_por=NULL, senha=?, precisa_trocar_senha=1 "
                "WHERE login=?",
                (nome, email, perfil, data_nascimento, senha_inicial, login)
            )
            reativados += 1
        elif existente:
            # Nunca mexe na senha de quem já existe (e já está ativo/não excluído).
            conn.execute(
                "UPDATE usuarios SET nome=?, email=?, perfil=?, data_nascimento=COALESCE(?, data_nascimento) WHERE login=?",
                (nome, email, perfil, data_nascimento, login)
            )
            atualizados += 1
        else:
            # Senha inicial = o próprio login; a pessoa troca no primeiro acesso.
            senha_inicial = generate_password_hash(login)
            conn.execute(
                "INSERT INTO usuarios (nome, login, senha, email, perfil, ativo, precisa_trocar_senha, data_nascimento, data_cadastro) "
                "VALUES (?, ?, ?, ?, ?, 1, 1, ?, ?)",
                (nome, login, senha_inicial, email, perfil, data_nascimento, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            )
            novos += 1

    conn.commit()
    conn.close()

    return redirect(
        f"/admin/importacao?usuarios_novos={novos}&usuarios_atualizados={atualizados}"
        f"&usuarios_reativados={reativados}&usuarios_ignorados={ignorados}"
    )


def _normalizar_cod_operadora(valor):
    """Tira zero à esquerda pra '052' e '52' virarem a mesma chave ('52') --
    o Bruno às vezes lembra o código com zero, o CSV do CRM oficial vem sem."""
    texto = _imp_limpo_texto(valor)
    if not texto:
        return None
    try:
        return str(int(texto))
    except ValueError:
        return texto.upper() or None


def _imp_converter_data_os_curta(valor):
    """DH_BAIXA_ORDEM_SERVICO vem como DD/MM/AA (ano com 2 dígitos, sem hora,
    apesar do nome da coluna) -- converte pra AAAA-MM-DD. Aceita DD/MM/AAAA
    também, por segurança."""
    texto = _imp_limpo_texto(valor)
    if not texto:
        return None
    if hasattr(valor, "strftime"):  # já veio como data/timestamp do pandas
        return valor.strftime("%Y-%m-%d")
    for formato in ("%d/%m/%y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(texto, formato).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


COLUNAS_OBRIGATORIAS_EXECUCOES = ["NR_CONTRATO", "CD_OPERADORA", "DH_BAIXA_ORDEM_SERVICO", "NR_ORDEM_SERVICO"]


@app.route("/admin/importacao/execucoes", methods=["POST"])
@login_required
@perfil_required("ADM")
def admin_importacao_execucoes():
    """Importa o CSV de OS's executadas (baixadas) que o Bruno extrai todo dia
    do CRM oficial da empresa -- ver comentário no topo da seção "EXECUÇÕES DE
    OS", acima de calcular_execucoes(). Upsert por (cod_operadora, contrato,
    dh_baixa): reimportar o mesmo dia (ou dias que se sobrepõem) não duplica."""
    import urllib.parse
    arquivo = request.files.get("arquivo")
    if not arquivo or arquivo.filename == "":
        return redirect("/admin/importacao?erro_execucoes=" + urllib.parse.quote("Nenhum arquivo selecionado."))

    try:
        if arquivo.filename.lower().endswith(".csv"):
            # O export vem em ISO-8859-1 (latin1), não UTF-8 -- acento quebra
            # sem isso. dtype=str em tudo pra não deixar contrato/OS/código de
            # operadora virarem float e ganharem ".0" quando alguma célula
            # vazia força o pandas a promover a coluna inteira pra float.
            try:
                df = pd.read_csv(arquivo, encoding="latin1", dtype=str)
            except UnicodeDecodeError:
                arquivo.stream.seek(0)
                df = pd.read_csv(arquivo, encoding="utf-8", dtype=str)
        else:
            df = pd.read_excel(arquivo, dtype=str)
    except Exception as erro:
        return redirect("/admin/importacao?erro_execucoes=" + urllib.parse.quote(f"Não consegui ler o arquivo: {erro}"))

    faltando = [c for c in COLUNAS_OBRIGATORIAS_EXECUCOES if c not in df.columns]
    if faltando:
        return redirect("/admin/importacao?erro_execucoes=" + urllib.parse.quote(
            "A planilha está sem a(s) coluna(s): " + ", ".join(faltando)))

    conn = conectar()
    agora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    novos = atualizados = ignorados = erros = 0

    for _, linha in df.iterrows():
        try:
            contrato = _imp_limpo_texto(linha.get("NR_CONTRATO"))
            cod_operadora = _normalizar_cod_operadora(linha.get("CD_OPERADORA"))
            dh_baixa = _imp_converter_data_os_curta(linha.get("DH_BAIXA_ORDEM_SERVICO"))

            if not contrato or not cod_operadora or not dh_baixa:
                # Sem um dos 3 não dá pra montar a chave (cod_operadora +
                # contrato + dh_baixa) -- ex: OS ainda não baixada (execução
                # de verdade só conta quando tem data de baixa).
                ignorados += 1
                continue

            login_abertura = _imp_limpo_texto(_imp_pegar_coluna(linha, "CD_LOGIN_ABERT_ORDEM_SERVICO")).lower()
            if not login_abertura:
                login_abertura = _imp_limpo_texto(_imp_pegar_coluna(linha, "CD_LOGIN_ABERTURA_SOLICITACAO")).lower()

            dados = {
                "cod_operadora": cod_operadora,
                "contrato": contrato,
                "dh_baixa": dh_baixa,
                "nr_ordem_servico": _imp_limpo_texto(linha.get("NR_ORDEM_SERVICO")),
                "login_abertura": login_abertura or None,
                "nome_abertura_os": _imp_limpo_texto(_imp_pegar_coluna(linha, "NM_USUA_ABERTURA_ORDEM_SERVICO")),
                "tipo_os": _imp_limpo_texto(linha.get("NM_TIPO_ORDEM_SERVICO")),
                "data_importacao": agora_str,
            }

            existente = conn.execute(
                "SELECT id FROM execucoes_os WHERE cod_operadora=:cod_operadora AND contrato=:contrato AND dh_baixa=:dh_baixa",
                dados
            ).fetchone()

            if existente:
                conn.execute("""
                    UPDATE execucoes_os SET
                        nr_ordem_servico=:nr_ordem_servico, login_abertura=:login_abertura,
                        nome_abertura_os=:nome_abertura_os, tipo_os=:tipo_os, data_importacao=:data_importacao
                    WHERE cod_operadora=:cod_operadora AND contrato=:contrato AND dh_baixa=:dh_baixa
                """, dados)
                atualizados += 1
            else:
                conn.execute("""
                    INSERT INTO execucoes_os (
                        cod_operadora, contrato, dh_baixa, nr_ordem_servico,
                        login_abertura, nome_abertura_os, tipo_os, data_importacao
                    ) VALUES (
                        :cod_operadora, :contrato, :dh_baixa, :nr_ordem_servico,
                        :login_abertura, :nome_abertura_os, :tipo_os, :data_importacao
                    )
                """, dados)
                novos += 1
        except Exception:
            erros += 1

    conn.commit()
    conn.close()

    return redirect(
        f"/admin/importacao?execucoes_novos={novos}&execucoes_atualizados={atualizados}"
        f"&execucoes_ignorados={ignorados}&execucoes_erros={erros}"
    )


COLUNAS_OBRIGATORIAS_QUEBRA = ["COD_OS", "CONTRATO", "COD_OPERADORA", "TIPO_TRATAMENTO", "DAT_AGENDA"]


def _imp_converter_data_quebra(valor):
    """DAT_AGENDA na planilha amostra_qualificada vem como data/hora nativa
    do Excel (pandas já entrega datetime); se um dia vier como texto (ex:
    reenvio em CSV), tenta os formatos mais comuns."""
    if valor is None:
        return None
    if hasattr(valor, "strftime"):
        return valor.strftime("%Y-%m-%d")
    texto = _imp_limpo_texto(valor)
    if not texto:
        return None
    for formato in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(texto, formato).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _imp_cod_os(valor):
    """COD_OS/NUM_WO às vezes vira float por causa de célula vazia na
    coluna -- tira o '.0' que sobra."""
    texto = _imp_limpo_texto(valor)
    if texto.endswith(".0"):
        texto = texto[:-2]
    return texto or None


def _normalizar_cidade_quebra(cidade):
    """Remove acento e padroniza caixa alta pra cruzar a cidade da planilha
    de quebra (título, com acento -- ex: "São José dos Campos") com a
    cidade do nosso mailing (maiúsculo, sem acento -- ex: "SAO JOSE DOS
    CAMPOS")."""
    if not cidade:
        return ""
    sem_acento = unicodedata.normalize("NFKD", str(cidade)).encode("ascii", "ignore").decode("ascii")
    return sem_acento.strip().upper()


def _chave_cidade_contrato(cidade, contrato):
    """Chave composta cidade+contrato (pedido do Bruno, 2026-08-29, 3º
    ajuste do dia): usar só CONTRATO pra cruzar "é nosso ou não" dá erro,
    porque o mesmo número de contrato pode existir em cidades diferentes --
    concatenando cidade+contrato não tem ambiguidade."""
    return f"{_normalizar_cidade_quebra(cidade)}|{(contrato or '').strip()}"


def remover_quebras_duplicadas(conn):
    """Remove duplicata de mesmo contrato + mesma dat_agenda em
    quebras_agenda (cod_os diferente, mas o Bruno confirmou em 2026-09-08
    que isso não deveria contar como dois registros -- só uma tratativa por
    contrato+data). Mantém a linha de MAIOR cod_os (mais recente) de cada
    grupo; se a linha removida já tinha uma tratativa registrada, transfere
    pra linha que ficou, em vez de simplesmente descartar o histórico.
    Chamada automaticamente no fim de toda importação de quebra."""
    duplicados = conn.execute("""
        SELECT contrato, dat_agenda FROM quebras_agenda
        WHERE contrato IS NOT NULL AND contrato != '' AND dat_agenda IS NOT NULL
        GROUP BY contrato, dat_agenda HAVING COUNT(*) > 1
    """).fetchall()

    total_removidas = 0
    for grupo in duplicados:
        linhas = conn.execute(
            "SELECT id FROM quebras_agenda WHERE contrato=? AND dat_agenda=? ORDER BY cod_os DESC",
            (grupo["contrato"], grupo["dat_agenda"])
        ).fetchall()
        id_manter = linhas[0]["id"]
        for linha_remover in linhas[1:]:
            id_remover = linha_remover["id"]
            conn.execute("UPDATE quebras_tratamento SET quebra_id=? WHERE quebra_id=?", (id_manter, id_remover))
            conn.execute("DELETE FROM quebras_agenda WHERE id=?", (id_remover,))
            total_removidas += 1
    return total_removidas


def contratos_agendados_por_nos(conn):
    # Desde 2026-10-16 devolve {chave cidade|contrato: dia do PRIMEIRO
    # agendamento nosso} -- a quebra só é Célula Local se aconteceu nesse
    # dia ou depois (ver classificar_segmento_quebra).
    """Conjunto de chaves cidade-contrato dos contratos que NÓS agendamos de
    fato na ferramenta (atividades.tabulacao='Agendado', cruzado com mailing
    só pra pegar a cidade -- atividades não tem coluna cidade própria).
    Critério definitivo de "nossos contratos" pra segmentação Célula Local x
    Regional da quebra de agenda (2026-08-29, 3ª e última versão desse
    critério no dia -- ver comentário no topo da seção QUEBRA DE AGENDA):
    nem cidade/operadora nem canal DAC servem, porque todo canal atua em
    toda cidade; o que importa é se o contrato foi agendado por nós mesmo."""
    linhas = conn.execute("""
        SELECT m.cidade, a.contrato, MIN(date(a.data_fim)) AS primeiro_agendamento
        FROM atividades a
        JOIN mailing m ON m.contrato = a.contrato
        WHERE a.tabulacao = 'Agendado'
        GROUP BY m.cidade, a.contrato
    """).fetchall()
    resultado = {}
    for l in linhas:
        chave = _chave_cidade_contrato(l["cidade"], l["contrato"])
        dia = l["primeiro_agendamento"]
        if chave not in resultado or (dia and (resultado[chave] is None or dia < resultado[chave])):
            resultado[chave] = dia
    return resultado


def classificar_segmento_quebra(chave_cidade_contrato, nossos_agendamentos, dat_agenda=None):
    """Célula Local = a chave cidade+contrato dessa OS de quebra bate com um
    contrato que nós agendamos de fato na ferramenta (nossos_agendamentos,
    de contratos_agendados_por_nos()); qualquer outro caso conta como
    Regional. O retorno aqui só define o rótulo da linha (segmento) e se o
    canal_venda vai ser reclassificado pra "CELULA LOCAL" na importação; o
    indicador "Regional" no Dashboard NÃO filtra por esse campo (é a base
    inteira -- ver calcular_indicador_quebra_totais / calcular_serie_quebra)."""
    # Regra de data (2026-10-16): quebra ANTERIOR ao nosso primeiro
    # agendamento daquele contrato é legado de outro atendimento/canal --
    # conta como Regional, não como Célula Local.
    if chave_cidade_contrato not in nossos_agendamentos:
        return "REGIONAL"
    primeiro = nossos_agendamentos[chave_cidade_contrato]
    if dat_agenda and primeiro and dat_agenda < primeiro:
        return "REGIONAL"
    return "CELULA LOCAL"


@app.route("/admin/importacao/quebra", methods=["POST"])
@login_required
@perfil_required("ADM")
def admin_importacao_quebra():
    """Importa a planilha "amostra_qualificada" de quebra de agenda,
    reenviada pelo Bruno todo dia com o acumulado do mês inteiro. Upsert por
    cod_os -- reimportar o mês inteiro de novo não duplica, só atualiza o
    que mudou (ex: uma OS que ganhou tratamento/baixa desde ontem)."""
    import urllib.parse
    arquivo = request.files.get("arquivo")
    if not arquivo or arquivo.filename == "":
        return redirect("/admin/importacao?erro_quebra=" + urllib.parse.quote("Nenhum arquivo selecionado."))

    try:
        if arquivo.filename.lower().endswith(".csv"):
            try:
                df = pd.read_csv(arquivo, encoding="latin1")
            except UnicodeDecodeError:
                arquivo.stream.seek(0)
                df = pd.read_csv(arquivo, encoding="utf-8")
        else:
            df = pd.read_excel(arquivo)
    except Exception as erro:
        return redirect("/admin/importacao?erro_quebra=" + urllib.parse.quote(f"Não consegui ler o arquivo: {erro}"))

    faltando = [c for c in COLUNAS_OBRIGATORIAS_QUEBRA if c not in df.columns]
    if faltando:
        return redirect("/admin/importacao?erro_quebra=" + urllib.parse.quote(
            "A planilha está sem a(s) coluna(s): " + ", ".join(faltando)))

    conn = conectar()
    agora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Calculado uma vez só, fora do loop (a planilha tem milhares de linhas)
    # -- conjunto dos contratos que nós agendamos de fato na ferramenta,
    # pra classificar Célula Local x Regional por cidade+contrato.
    nossos_agendamentos = contratos_agendados_por_nos(conn)
    novos = atualizados = ignorados = erros = 0

    for _, linha in df.iterrows():
        try:
            cod_os = _imp_cod_os(linha.get("COD_OS"))
            cod_operadora = _normalizar_cod_operadora(linha.get("COD_OPERADORA"))
            tipo_tratamento = _imp_limpo_texto(linha.get("TIPO_TRATAMENTO")).upper() or None
            dat_agenda = _imp_converter_data_quebra(linha.get("DAT_AGENDA"))

            if not cod_os or not tipo_tratamento or not dat_agenda:
                ignorados += 1
                continue

            contrato = _imp_cod_os(linha.get("CONTRATO"))
            cidade = _imp_limpo_texto(_imp_pegar_coluna(linha, "DSC_MUNICIPIO")) or None
            canal_venda_original = _imp_limpo_texto(_imp_pegar_coluna(linha, "DSC_CANAL_VENDA")) or None
            chave = _chave_cidade_contrato(cidade, contrato)
            segmento = classificar_segmento_quebra(chave, nossos_agendamentos, dat_agenda)
            # Só as OS que batem (cidade+contrato) com um contrato que NÓS
            # agendamos na ferramenta ficam com o canal_venda gravado como
            # "CELULA LOCAL" (sobrescreve o nome original) -- pedido do
            # Bruno, 2026-08-29. As demais mantêm o canal original tal como
            # veio na planilha -- inclusive quando esse canal é "DAC" (canal
            # usado também por outros times, em todas as cidades, então
            # precisa continuar aparecendo como DAC).
            canal_venda = "CELULA LOCAL" if segmento == "CELULA LOCAL" else canal_venda_original
            vinculado = 0
            if contrato:
                existe_no_mailing = conn.execute(
                    "SELECT 1 FROM mailing WHERE contrato=? UNION SELECT 1 FROM atividades WHERE contrato=? LIMIT 1",
                    (contrato, contrato)
                ).fetchone()
                vinculado = 1 if existe_no_mailing else 0

            dados = {
                "cod_os": cod_os,
                "contrato": contrato,
                "cod_operadora": cod_operadora,
                "cidade": cidade,
                "segmento": segmento,
                "canal_venda": canal_venda,
                "canal_venda_original": canal_venda_original,
                "tipo_tratamento": tipo_tratamento,
                "responsavel_quebra": _imp_limpo_texto(_imp_pegar_coluna(linha, "RESPONSAVEL_QUEBRA")) or None,
                "agrupamento_quebra": _imp_limpo_texto(_imp_pegar_coluna(linha, "AGRUPAMENTO_QUEBRA")) or None,
                "sub_agrupamento": _imp_limpo_texto(_imp_pegar_coluna(linha, "SUB_AGRUPAMENTO_SC")) or None,
                "dat_agenda": dat_agenda,
                "tecnico": _imp_limpo_texto(_imp_pegar_coluna(linha, "LOGIN_TEC")) or None,
                "motivo_reagendamento": _imp_limpo_texto(_imp_pegar_coluna(linha, "MOTIVO_REAGENDAMENTO")) or None,
                "vinculado_mailing": vinculado,
                "data_importacao": agora_str,
            }

            existente = conn.execute("SELECT id FROM quebras_agenda WHERE cod_os=:cod_os", dados).fetchone()
            if existente:
                conn.execute("""
                    UPDATE quebras_agenda SET
                        contrato=:contrato, cod_operadora=:cod_operadora, cidade=:cidade, segmento=:segmento,
                        canal_venda=:canal_venda, canal_venda_original=:canal_venda_original,
                        tipo_tratamento=:tipo_tratamento,
                        responsavel_quebra=:responsavel_quebra, agrupamento_quebra=:agrupamento_quebra,
                        sub_agrupamento=:sub_agrupamento, dat_agenda=:dat_agenda, tecnico=:tecnico,
                        motivo_reagendamento=:motivo_reagendamento,
                        vinculado_mailing=:vinculado_mailing, data_importacao=:data_importacao
                    WHERE cod_os=:cod_os
                """, dados)
                atualizados += 1
            else:
                conn.execute("""
                    INSERT INTO quebras_agenda (
                        cod_os, contrato, cod_operadora, cidade, segmento, canal_venda, canal_venda_original,
                        tipo_tratamento, responsavel_quebra, agrupamento_quebra, sub_agrupamento, dat_agenda, tecnico,
                        motivo_reagendamento, vinculado_mailing, data_importacao
                    ) VALUES (
                        :cod_os, :contrato, :cod_operadora, :cidade, :segmento, :canal_venda, :canal_venda_original,
                        :tipo_tratamento, :responsavel_quebra, :agrupamento_quebra, :sub_agrupamento, :dat_agenda, :tecnico,
                        :motivo_reagendamento, :vinculado_mailing, :data_importacao
                    )
                """, dados)
                novos += 1
        except Exception:
            erros += 1

    duplicadas_removidas = remover_quebras_duplicadas(conn)
    conn.commit()
    conn.close()

    return redirect(
        f"/admin/importacao?quebra_novos={novos}&quebra_atualizados={atualizados}"
        f"&quebra_ignorados={ignorados}&quebra_erros={erros}&quebra_duplicadas_removidas={duplicadas_removidas}"
    )


@app.route("/quebra/resultado")
@login_required
@permissao_required("dashboard_equipe")
def quebra_resultado():
    """Página com o resultado da quebra de agenda em todas as visões
    (geral, por segmento Célula Local x Regional, cidade, canal, responsável,
    agrupamento), com filtro de período e canal -- pedido do Bruno em
    2026-08-29. Mesma trava de acesso do Dashboard em visão de equipe
    (dashboard_equipe): quem só tem dashboard_pessoal não vê o resultado
    agregado de todo mundo aqui."""
    data_inicio = request.args.get("data_inicio") or datetime.now().replace(day=1).strftime("%Y-%m-%d")
    data_fim = request.args.get("data_fim") or datetime.now().strftime("%Y-%m-%d")
    canal = request.args.get("canal") or None
    if canal == "Todos":
        canal = None

    conn = conectar()
    resultado = calcular_resultado_quebra(conn, data_inicio, data_fim, canal)
    conn.close()

    return render_template(
        "QuebraResultado.html",
        resultado=resultado,
        data_inicio=data_inicio,
        data_fim=data_fim,
        canal=canal,
    )


@app.route("/quebra/tratamento")
@login_required
@permissao_required("tratamento_quebra")
def quebra_tratamento():
    """Fila de quebras que o operador logado precisa tratar -- pedido do
    Bruno, 2026-08-29 (reescrita em 2026-09-08 pra mostrar todas as
    pendentes, não só D-1 -- ver quebras_pendentes_tratamento). Só as
    próprias (contratos que ele mesmo agendou)."""
    usuario = usuario_atual()
    status_filtro = request.args.get("status") or "PENDENTE"
    data_inicio = request.args.get("data_inicio") or None
    data_fim = request.args.get("data_fim") or None
    contrato_filtro = (request.args.get("contrato") or "").strip() or None
    motivo_filtro = request.args.get("motivo") or None

    conn = conectar()
    pendentes = quebras_pendentes_tratamento(
        conn, usuario["nome"], status_filtro=status_filtro, data_inicio=data_inicio,
        data_fim=data_fim, contrato_filtro=contrato_filtro, motivo_filtro=motivo_filtro
    )
    motivos_disponiveis = [r["motivo_reagendamento"] for r in conn.execute(
        "SELECT DISTINCT motivo_reagendamento FROM quebras_agenda WHERE motivo_reagendamento IS NOT NULL ORDER BY motivo_reagendamento"
    ).fetchall()]
    tratadas_ids = {r["quebra_id"] for r in conn.execute("SELECT quebra_id FROM quebras_tratamento").fetchall()}
    conn.close()
    return render_template("QuebraTratamento.html", pendentes=pendentes, status_filtro=status_filtro,
                            data_inicio=data_inicio, data_fim=data_fim, contrato_filtro=contrato_filtro,
                            motivo_filtro=motivo_filtro, motivos_disponiveis=motivos_disponiveis,
                            tratadas_ids=tratadas_ids)


@app.route("/quebra/tratamento/<int:quebra_id>")
@login_required
@permissao_required("tratamento_quebra")
def quebra_tratamento_form(quebra_id):
    usuario = usuario_atual()
    conn = conectar()
    quebra = conn.execute("SELECT * FROM quebras_agenda WHERE id=?", (quebra_id,)).fetchone()
    if not quebra:
        conn.close()
        return "Quebra não encontrada.", 404
    # Trava de segurança: só o DONO da quebra pode tratar (mesmo critério
    # da fila -- ver _sql_dono_quebra).
    eh_do_operador = dono_da_quebra(conn, quebra) == usuario["nome"]
    ja_tratada = conn.execute("SELECT 1 FROM quebras_tratamento WHERE quebra_id=?", (quebra_id,)).fetchone()
    conn.close()
    if not eh_do_operador:
        return "Essa quebra não é sua: ela aconteceu antes do seu agendamento ou o contrato foi agendado depois por outra pessoa.", 403
    if ja_tratada:
        return redirect("/quebra/tratamento")
    return render_template("QuebraTratamentoForm.html", quebra=quebra,
                            submotivos=SUBMOTIVOS_CANCELAMENTO_QUEBRA)


@app.route("/quebra/tratamento/<int:quebra_id>/salvar", methods=["POST"])
@login_required
@permissao_required("tratamento_quebra")
def quebra_tratamento_salvar(quebra_id):
    usuario = usuario_atual()
    operador_logado = usuario["nome"]
    tabulacao = request.form.get("tabulacao")
    observacao = request.form.get("observacao") or ""

    conn = conectar()
    quebra = conn.execute("SELECT * FROM quebras_agenda WHERE id=?", (quebra_id,)).fetchone()
    if not quebra:
        conn.close()
        return "Quebra não encontrada.", 404
    eh_do_operador = dono_da_quebra(conn, quebra) == usuario["nome"]
    if not eh_do_operador:
        conn.close()
        return "Essa quebra não é sua: ela aconteceu antes do seu agendamento ou o contrato foi agendado depois por outra pessoa.", 403
    ja_tratada = conn.execute("SELECT 1 FROM quebras_tratamento WHERE quebra_id=?", (quebra_id,)).fetchone()
    if ja_tratada:
        conn.close()
        return redirect("/quebra/tratamento")

    submotivo = None
    if tabulacao == "Reagendado":
        # Mesmo formulário e mesma gravação de um agendamento normal --
        # pedido do Bruno: "carregar formulário igual ao agendamento normal
        # e gravar no banco da mesma forma para carregar no agendados por
        # data". Reaproveita registrar_tratamento(), a mesma função usada
        # por /salvar e /atendimento/<id>/tratar -- se o contrato já tiver
        # esse agendamento como última tabulação (o caso normal aqui, já que
        # é justamente o agendamento que quebrou), ela detecta sozinha que é
        # uma remarcação e atualiza a data/janela na própria linha (sem
        # mudar dat_agenda da quebra original), guardando o antes/depois em
        # ajustes_agendamento -- exatamente "não mudar a data de agendamento
        # original, só carregar novas informações no histórico".
        cliente_mailing = conn.execute(
            "SELECT id, tentativa FROM mailing WHERE contrato=?", (quebra["contrato"],)
        ).fetchone()
        id_cliente = cliente_mailing["id"] if cliente_mailing else None
        tentativa_atual = (cliente_mailing["tentativa"] if cliente_mailing else 1) or 1
        registrar_tratamento(conn, id_cliente, quebra["contrato"], tentativa_atual, operador_logado, "Agendado", observacao)
    elif tabulacao == "Cancelado":
        submotivo = request.form.get("submotivo") or None
        if submotivo not in SUBMOTIVOS_CANCELAMENTO_QUEBRA:
            conn.close()
            return "Submotivo inválido.", 400
        if submotivo == "Outro" and not observacao.strip():
            conn.close()
            return "Justificativa é obrigatória pra submotivo 'Outro'.", 400
    else:
        conn.close()
        return "Tabulação inválida.", 400

    conn.execute("""
        INSERT INTO quebras_tratamento (quebra_id, operador, tabulacao, submotivo, observacao, data_tratamento)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (quebra_id, operador_logado, tabulacao, submotivo, observacao, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()
    return redirect("/quebra/tratamento")


@app.route("/gestao/quebra")
@login_required
@permissao_required("quebra_gestao")
def gestao_quebra():
    """Fila de demandas de quebra pra gestão (Supervisor/ADM) acompanhar --
    pedido do Bruno, 2026-08-29 (2ª rodada de ajustes da tela de quebra):
    cards de resumo + tabela por Assistente (pendentes x já tratadas), com
    detalhe expansível ("+") de cada quebra individual daquela pessoa --
    mesmo padrão visual já usado no histórico de monitorias (Qualidade)."""
    hoje = datetime.now().strftime("%Y-%m-%d")
    data_inicio = request.args.get("data_inicio") or datetime.now().replace(day=1).strftime("%Y-%m-%d")
    data_fim = request.args.get("data_fim") or hoje
    operador_filtro = request.args.get("operador") or ""
    contrato_filtro = request.args.get("contrato") or ""
    motivo_filtro = request.args.get("motivo") or ""

    conn = conectar()
    resultado = calcular_fila_gestao_quebra(
        conn, data_inicio, data_fim,
        operador=operador_filtro or None,
        contrato=contrato_filtro or None,
        motivo=motivo_filtro or None,
    )
    conn.close()

    return render_template(
        "GestaoQuebra.html",
        resultado=resultado,
        data_inicio=data_inicio, data_fim=data_fim,
        operador_filtro=operador_filtro, contrato_filtro=contrato_filtro, motivo_filtro=motivo_filtro,
    )


@app.route("/contrato/<contrato>/timeline")
@login_required
@permissao_required("atendimento")
def contrato_timeline(contrato):
    """Histórico Unificado de Interações -- pedido do Bruno, 2026-09-11."""
    conn = conectar()
    cliente = conn.execute("SELECT contrato, cliente, cidade FROM mailing WHERE contrato=?", (contrato,)).fetchone()
    eventos = montar_timeline_contrato(conn, contrato)
    conn.close()
    return render_template("TimelineCliente.html", contrato=contrato, cliente=cliente, eventos=eventos)


@app.route("/dashboard/mailing")
@login_required
@permissao_required("dashboard_equipe")
def dashboard_mailing():
    """Detalhamento da Lista de Trabalho (pedida pelo Bruno, 2026-08-29,
    reformulada em 2026-09-11): volume por cidade, node (DDD > Cidade >
    Node) e produto Virtua por velocidade -- subpágina do Dashboard, mesma
    trava de visibilidade da visão de equipe."""
    conn = conectar()
    mailing_refinado = calcular_mailing_refinado(conn)
    conn.close()
    return render_template("MailingRefinado.html", mailing_refinado=mailing_refinado)


@app.route("/admin/cidades-operadora")
@login_required
@perfil_required("ADM")
def admin_cidades_operadora():
    conn = conectar()
    mapeados = conn.execute("SELECT cod_operadora, cidade FROM cidades_operadora ORDER BY cidade").fetchall()
    nao_mapeados = conn.execute("""
        SELECT e.cod_operadora, COUNT(*) as qtd
        FROM execucoes_os e
        WHERE NOT EXISTS (SELECT 1 FROM cidades_operadora c WHERE c.cod_operadora = e.cod_operadora)
        GROUP BY e.cod_operadora ORDER BY qtd DESC
    """).fetchall()
    conn.close()
    return render_template("AdminCidadesOperadora.html", mapeados=mapeados, nao_mapeados=nao_mapeados)


@app.route("/admin/cidades-operadora/salvar", methods=["POST"])
@login_required
@perfil_required("ADM")
def admin_cidades_operadora_salvar():
    cod_operadora = _normalizar_cod_operadora(request.form.get("cod_operadora"))
    cidade = (request.form.get("cidade") or "").strip().upper()
    if cod_operadora and cidade:
        conn = conectar()
        conn.execute("""
            INSERT INTO cidades_operadora (cod_operadora, cidade) VALUES (?, ?)
            ON CONFLICT(cod_operadora) DO UPDATE SET cidade=excluded.cidade
        """, (cod_operadora, cidade))
        conn.commit()
        conn.close()
    return redirect("/admin/cidades-operadora")


@app.route("/admin/cidades-operadora/excluir/<cod_operadora>", methods=["POST"])
@login_required
@perfil_required("ADM")
def admin_cidades_operadora_excluir(cod_operadora):
    conn = conectar()
    conn.execute("DELETE FROM cidades_operadora WHERE cod_operadora=?", (cod_operadora,))
    conn.commit()
    conn.close()
    return redirect("/admin/cidades-operadora")


@app.route("/extracao/pontualidade")
@login_required
@permissao_required("extracao_pontualidade")
def extracao_pontualidade():
    hoje = datetime.now().strftime("%Y-%m-%d")
    return render_template("ExtracaoPontualidade.html", hoje=hoje)


@app.route("/extracao/pontualidade/gerar")
@login_required
@permissao_required("extracao_pontualidade")
def extracao_pontualidade_gerar():
    data_inicio = request.args.get("data_inicio")
    data_fim = request.args.get("data_fim")
    if not data_inicio or not data_fim:
        return "Informe data de início e fim.", 400

    conn = conectar()
    sessoes_linhas = conn.execute("""
        SELECT s.*, u.nome as nome_usuario, u.perfil as perfil_usuario
        FROM sessoes s
        JOIN usuarios u ON u.id = s.usuario_id
        WHERE date(s.login_em) BETWEEN ? AND ?
        ORDER BY s.login_em
    """, (data_inicio, data_fim)).fetchall()

    pausas_linhas = conn.execute("""
        SELECT p.*, u.nome as nome_usuario, u.perfil as perfil_usuario
        FROM pausas p
        JOIN usuarios u ON u.id = p.usuario_id
        WHERE p.data BETWEEN ? AND ?
        ORDER BY p.data, p.inicio
    """, (data_inicio, data_fim)).fetchall()
    conn.close()

    def duracao_formatada(inicio_str, fim_str):
        if not inicio_str or not fim_str:
            return "Em aberto"
        try:
            inicio_dt = datetime.strptime(inicio_str, "%Y-%m-%d %H:%M:%S")
            fim_dt = datetime.strptime(fim_str, "%Y-%m-%d %H:%M:%S")
            minutos = int((fim_dt - inicio_dt).total_seconds() // 60)
            horas, resto = divmod(minutos, 60)
            return f"{horas}h{resto:02d}min" if horas else f"{resto}min"
        except (ValueError, TypeError):
            return ""

    registros_sessoes = [{
        "Nome": linha["nome_usuario"],
        "Perfil": exibir_perfil(linha["perfil_usuario"]),
        "Login em": linha["login_em"],
        "Logout em": linha["logout_em"] or "Em aberto",
        "Duração": duracao_formatada(linha["login_em"], linha["logout_em"]),
    } for linha in sessoes_linhas]

    registros_pausas = [{
        "Nome": linha["nome_usuario"],
        "Perfil": exibir_perfil(linha["perfil_usuario"]),
        "Tipo": linha["tipo"],
        "Data": linha["data"],
        "Início": linha["inicio"],
        "Fim previsto": linha["fim_previsto"],
        "Fim real": linha["fim_real"] or "Em aberto",
        "Duração": duracao_formatada(linha["inicio"], linha["fim_real"]),
        "Origem": "Automática" if linha["automatica"] else "Manual",
    } for linha in pausas_linhas]

    df_sessoes = pd.DataFrame(registros_sessoes)
    df_pausas = pd.DataFrame(registros_pausas)
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df_sessoes.to_excel(writer, index=False, sheet_name="Login e Logout")
        df_pausas.to_excel(writer, index=False, sheet_name="Pausas")
    buffer.seek(0)

    nome_arquivo = f"extracao_pontualidade_{data_inicio}_a_{data_fim}.xlsx"
    return send_file(
        buffer,
        as_attachment=True,
        download_name=nome_arquivo,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def _ip_na_rede_local():
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# Agrupado por seção -- mesmo agrupamento que a pessoa já vê no menu lateral
# (Operação / Gestão / Administração, ver layout.html), pra facilitar achar o
# módulo certo. Cada item é um dict {"chave", "label", "desc", "subitens"}.
# "subitens" é None pra módulo de capacidade única (um só checkbox por perfil,
# igual antes), ou uma lista de (chave, label, desc) quando o módulo esconde
# mais de uma capacidade que faz sentido liberar separadamente por cargo
# (reestruturado em 2026-08-26, a pedido do Bruno -- ex: Dashboard tinha "visão
# pessoal" e "visão de equipe" sempre decididas só pelo perfil, sem passar pela
# matriz; GPON tinha "Tratamento" e "Visualização da fila" misturados numa
# permissão só; Extração tinha 5 relatórios diferentes atrás da mesma
# permissão). Toda chave (de item simples OU de subitem) é a mesma usada em
# permissao_required()/pode() no resto do código -- não mexer nela sem também
# atualizar os decoradores/checagens que a usam.
GRUPOS_PERMISSAO = [
    ("Atendimento (Operação)", "bi-telephone-fill", [
        {"chave": "dashboard", "label": "Dashboard", "desc": "Tela inicial com indicadores de produtividade.",
         "subitens": [
             ("dashboard_pessoal", "Visão pessoal", "Meus indicadores e atendimentos do dia/mês (a visão que Operador/Analista sempre viram)."),
             ("dashboard_equipe", "Visão de equipe", "Indicadores da equipe inteira, tabela por pessoa, mapa de calor e o botão \"Detalhes\" de cada pessoa."),
         ]},
        {"chave": "atendimento", "label": "Atendimento", "desc": "Fila normal de atendimento (contratos distribuídos automaticamente).", "subitens": None},
        {"chave": "buscar_contrato", "label": "Buscar Contrato", "desc": "Buscar e tratar um contrato específico fora da fila.", "subitens": None},
        {"chave": "agendados_data", "label": "Agendados por Data", "desc": "Relatório de agendamentos por data.", "subitens": None},
        {"chave": "agendado_executado", "label": "Agendado x Executado", "desc": "Acompanhamento das próprias execuções em campo (Operador) ou de toda a equipe (Supervisor).", "subitens": None},
        {"chave": "tratamento_quebra", "label": "Tratamento de Quebra", "desc": "Tratar as quebras de agenda do dia anterior (D-1) dos próprios contratos: reagendar ou cancelar.", "subitens": None},
        {"chave": "nova_tarefa", "label": "Nova Tarefa", "desc": "Cadastrar uma tarefa avulsa.", "subitens": None},
        {"chave": "tratamento_gpon", "label": "Tratamento GPON Indisponível", "desc": "Fila de contratos aguardando análise de GPON.",
         "subitens": [
             ("gpon_tratamento", "Tratamento", "Trabalhar a fila: buscar o próximo contrato pendente e registrar o resultado da análise."),
             ("gpon_fila", "Visualização da fila", "Ver a fila consolidada (quem está aguardando/em análise) e abrir o detalhe de um contrato, sem tratar."),
         ]},
        {"chave": "qualidade", "label": "Qualidade (Monitoria)", "desc": "Monitorias de qualidade do atendimento.",
         "subitens": [
             ("qualidade_propria", "Ver próprio histórico", "Ver as próprias monitorias recebidas e dar aceite nelas."),
             ("qualidade_gestao", "Gestão da equipe", "Ver as monitorias de todo mundo, os gráficos de dispersão/evolução, e criar novas monitorias."),
         ]},
        {"chave": "pausas", "label": "Tratamento / Pausas", "desc": "Abrir/fechar turno e tirar pausas.", "subitens": None},
    ]),
    ("Gestão de equipe", "bi-people-fill", [
        {"chave": "supervisao", "label": "Supervisão", "desc": "Visão da equipe: status, desligar/excluir usuário, resetar senha.", "subitens": None},
        {"chave": "escalas", "label": "Escalas", "desc": "Cadastro das escalas de trabalho da equipe.", "subitens": None},
        {"chave": "transferencias", "label": "Transferências", "desc": "Transferir contratos entre operadores.", "subitens": None},
        {"chave": "extracao", "label": "Extração", "desc": "Extrações de relatórios em Excel.",
         "subitens": [
             ("extracao_pontualidade", "Login, Logout e Pausas", "Histórico de início/fim de tratamento e de pausas de todo mundo."),
             ("extracao_tratamentos", "Tratamentos (mailing)", "Contratos já atribuídos (em andamento + finalizados) no período."),
             ("extracao_gpon", "Tratamento GPON", "Resultado das análises da fila de GPON Indisponível no período."),
             ("extracao_ausencias", "Ausências", "Registros de falta (justificada ou não) no período."),
             ("extracao_usuarios", "Usuários", "Dados cadastrais de todo mundo (não inclui foto)."),
             ("extracao_followup_atrasado", "Followup Atrasado", "Contratos parados em followup vencido (2ª/3ª tentativa), com quem está atribuído e se essa pessoa está ausente hoje."),
             ("extracao_quebra", "Quebra de Agenda (completa)", "Todas as OS da base de quebra no período, com o Assistente dono, a tratativa registrada e se houve execução depois. Liberada por padrão para Supervisor."),
         ]},
        {"chave": "historico_tratamento", "label": "Histórico de Tratamento", "desc": "Histórico de tratamentos de todos os operadores, com filtro por pessoa/data/tabulação.", "subitens": None},
        {"chave": "quebra_gestao", "label": "Gestão de Quebra de Agenda", "desc": "Fila de demandas de quebra por Assistente (pendentes x já tratadas), com filtro por data/operador/contrato/motivo.", "subitens": None},
        {"chave": "diario_bordo", "label": "Diário de Bordo", "desc": "Registro de eventos operacionais (queda de energia, banimento, evacuação, etc.), com filtro por data/classificação e exportação.", "subitens": None},
        {"chave": "confirmacao_agenda_tratar", "label": "Confirmação de Agenda", "desc": "Tratamento da fila de confirmação de agenda (módulo separado, perfil OPERADOR_CONF tem por padrão).", "subitens": None},
        {"chave": "relatorio_clima", "label": "Relatório de Clima", "desc": "Previsão do tempo dos próximos 7 dias, por DDD e cidades de destaque (módulo separado, só leitura).", "subitens": None},
        {"chave": "backlog_tratar", "label": "Tratamento de Backlog", "desc": "Fila de contratos em backlog (agenda no passado ou sem agenda), módulo separado com banco próprio.", "subitens": None},
        {"chave": "relatorio_ged", "label": "Cruzamento GED", "desc": "Relatório dos contratos que saíram da fila por venda bloqueada/não liberada/não encontrada na base GED, com extração em Excel. Upload e aplicação do cruzamento são sempre só do ADM. Desligado por padrão.", "subitens": None},
    ]),
    ("Administração", "bi-gear-fill", [
        {"chave": "modo_trabalho", "label": "Modo de Trabalho", "desc": "Configuração geral do modo de trabalho do sistema.", "subitens": None},
    ]),
    ("Outros", "bi-three-dots", [
        {"chave": "ajuda", "label": "Ajuda", "desc": "Página de ajuda com o manual explicando cada tela do sistema. Desligado por padrão para todo mundo que não é ADM.", "subitens": None},
    ]),
]


def _achatar_modulos_permissao(grupos):
    """Lista achatada (chave, label) de toda capacidade REAL (folha) do
    agrupamento acima -- item simples entra pela própria chave; item com
    subitens entra por cada subitem (a chave "guarda-chuva" do item-pai, tipo
    "dashboard" ou "extracao", não é mais uma permissão de verdade quando ele
    tem subitens -- só usada como título de agrupamento na tela)."""
    flat = []
    for _grupo, _icone, itens in grupos:
        for item in itens:
            if item["subitens"]:
                for chave, label, _desc in item["subitens"]:
                    flat.append((chave, f"{item['label']} — {label}"))
            else:
                flat.append((item["chave"], item["label"]))
    return flat


MODULOS_PERMISSAO = _achatar_modulos_permissao(GRUPOS_PERMISSAO)
PERFIS_CONFIGURAVEIS = ["OPERADOR", "SUPERVISOR", "ANALISTA", "ANALISTA_II", "ASSISTENTE_CLARO", "OPERADOR_CONF"]  # ADM sempre tem tudo, não entra aqui


def _garantir_permissoes_assistente_claro():
    """Auto-migração (idempotente): cria o perfil ASSISTENTE_CLARO com acesso
    só a Nova Tarefa, Buscar Contrato e Tratamento de Quebra -- pedido do
    Bruno, 2026-09-10. Diferente do Assistente (Operador) normal: não passa
    por Atendimento nem tem controle de jornada (login/logout/pausas -- por
    não ter a permissão "pausas", nunca é obrigado a passar por
    /turno/entrar, então nunca aparece em Supervisão, HC Ativo, faltas nem
    presença, que são todos baseados em quem tem sessão/escala registrada).
    Só roda se ainda não tiver NENHUMA permissão salva pra esse perfil, pra
    nunca sobrescrever ajuste manual do ADM feito depois."""
    conn = conectar()
    existe = conn.execute("SELECT 1 FROM permissoes WHERE perfil='ASSISTENTE_CLARO' LIMIT 1").fetchone()
    if not existe:
        liberadas = {"nova_tarefa", "buscar_contrato", "tratamento_quebra"}
        for chave, _label in MODULOS_PERMISSAO:
            conn.execute(
                "INSERT INTO permissoes (perfil, modulo, permitido) VALUES (?, ?, ?)",
                ("ASSISTENTE_CLARO", chave, 1 if chave in liberadas else 0)
            )
        conn.commit()
    conn.close()


_garantir_permissoes_assistente_claro()


def _garantir_permissoes_operador_conf():
    """Auto-migração (idempotente): cria o perfil OPERADOR_CONF com acesso só
    à Confirmação de Agenda -- pedido do Bruno, 2026-09-16. Módulo à parte,
    banco próprio, não conta em HC Ativo/absenteísmo/escala/jornada pelo
    mesmo motivo do ASSISTENTE_CLARO acima (sem a permissão "pausas", nunca
    passa por /turno/entrar). Só roda se ainda não tiver NENHUMA permissão
    salva pra esse perfil, pra nunca sobrescrever ajuste manual do ADM."""
    conn = conectar()
    existe = conn.execute("SELECT 1 FROM permissoes WHERE perfil='OPERADOR_CONF' LIMIT 1").fetchone()
    if not existe:
        liberadas = {"confirmacao_agenda_tratar"}
        for chave, _label in MODULOS_PERMISSAO:
            conn.execute(
                "INSERT INTO permissoes (perfil, modulo, permitido) VALUES (?, ?, ?)",
                ("OPERADOR_CONF", chave, 1 if chave in liberadas else 0)
            )
        conn.commit()
    conn.close()


_garantir_permissoes_operador_conf()


def _garantir_permissao_extracao_quebra():
    """Auto-migração (idempotente): libera a Extração de Quebra de Agenda
    pro SUPERVISOR -- pedido do Bruno, 2026-10-16 ("para a supervisora
    analisar"). Só roda se essa permissão ainda não tiver NENHUMA linha
    salva, pra nunca sobrescrever ajuste manual feito depois na tela."""
    conn = conectar()
    existe = conn.execute("SELECT 1 FROM permissoes WHERE modulo='extracao_quebra' LIMIT 1").fetchone()
    if not existe:
        conn.execute("INSERT INTO permissoes (perfil, modulo, permitido) VALUES ('SUPERVISOR', 'extracao_quebra', 1)")
        conn.commit()
    conn.close()


_garantir_permissao_extracao_quebra()


def _garantir_tabelas_diario_bordo():
    """Auto-migração: Diário de Bordo (semelhante às Notas de Versão, mas
    pra eventos operacionais do dia a dia -- queda de energia, banimento,
    evacuação, etc.) -- pedido do Bruno, 2026-09-10. Classificação vem de
    uma tabela separada, configurável pelo ADM (mesmo padrão já usado pros
    motivos de ausência customizados), semeada com os 7 valores iniciais que
    o Bruno passou."""
    conn = conectar()
    try:
        conn.execute("""
            CREATE TABLE diario_bordo (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                data_evento TEXT,
                classificacao TEXT,
                detalhamento TEXT,
                data_hora_inicio TEXT,
                data_hora_fim TEXT,
                duracao_minutos INTEGER,
                criado_por TEXT,
                criado_em TEXT
            )
        """)
        conn.commit()
    except sqlite3.OperationalError:
        pass

    try:
        conn.execute("""
            CREATE TABLE diario_bordo_classificacoes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT UNIQUE NOT NULL,
                ativo INTEGER DEFAULT 1
            )
        """)
        conn.commit()
        # Só semeia na criação da tabela (primeira vez) -- se o ADM já tiver
        # desativado ou removido algum desses depois, não volta sozinho.
        classificacoes_iniciais = [
            "NETSMS", "NETSALES", "WI-FI", "FALTA DE ENERGIA",
            "BANIMENTOS", "EVACUAÇÃO (BRIGADA DE INCÊNDIO)", "INFRAESTRUTURA",
        ]
        for nome in classificacoes_iniciais:
            conn.execute("INSERT INTO diario_bordo_classificacoes (nome, ativo) VALUES (?, 1)", (nome,))
        conn.commit()
    except sqlite3.OperationalError:
        pass
    conn.close()


_garantir_tabelas_diario_bordo()


# Coordenadas fixas das cidades atendidas -- usado pra buscar a previsão do
# tempo (Open-Meteo). Cadastro simples, só precisa mexer se uma cidade nova
# entrar na operação (pedido do Bruno, 2026-09-10).
COORDENADAS_CIDADES = {
    "AMERICANA": (-22.7375, -47.3317), "ARACATUBA": (-21.2081, -50.4328),
    "ARARAQUARA": (-21.7845, -48.1780), "BAURU": (-22.3246, -49.0871),
    "CAMPINAS": (-22.9099, -47.0626), "FRANCA": (-20.5386, -47.4008),
    "GUARUJA": (-23.9931, -46.2564), "HORTOLANDIA": (-22.8582, -47.2200),
    "INDAIATUBA": (-23.0908, -47.2181), "ITAPETININGA": (-23.5917, -48.0531),
    "JACAREI": (-23.3053, -45.9658), "JAU": (-22.2964, -48.5578),
    "LIMEIRA": (-22.5647, -47.4017), "MARILIA": (-22.2139, -49.9458),
    "MOGI GUACU": (-22.3706, -46.9425), "MOGI MIRIM": (-22.4319, -46.9581),
    "PAULINIA": (-22.7614, -47.1547), "PINDAMONHANGABA": (-22.9236, -45.4614),
    "PIRACICABA": (-22.7253, -47.6492), "PRAIA GRANDE": (-24.0084, -46.4022),
    "RIBEIRAO PRETO": (-21.1775, -47.8103), "RIO CLARO": (-22.4064, -47.5611),
    "SANTA BARBARA D OESTE": (-22.7547, -47.4142), "SANTOS": (-23.9608, -46.3336),
    "SAO CARLOS": (-22.0175, -47.8908), "SAO JOSE DO RIO PRETO": (-20.8113, -49.3758),
    "SAO JOSE DOS CAMPOS": (-23.2237, -45.9009), "SAO VICENTE": (-23.9631, -46.3919),
    "SOROCABA": (-23.5015, -47.4526), "TAUBATE": (-23.0264, -45.5553),
    "VALINHOS": (-22.9708, -46.9958), "VINHEDO": (-23.0300, -46.9758),
    "VOTORANTIM": (-23.5467, -47.4392),
}

# Tradução simplificada do "weathercode" da Open-Meteo (WMO) pra texto em
# português -- não é a lista inteira, só os códigos mais comuns.
WEATHERCODE_TEXTO = {
    0: "Céu limpo", 1: "Poucas nuvens", 2: "Parcialmente nublado", 3: "Nublado",
    45: "Neblina", 48: "Neblina com geada",
    51: "Garoa fraca", 53: "Garoa moderada", 55: "Garoa forte",
    61: "Chuva fraca", 63: "Chuva moderada", 65: "Chuva forte",
    80: "Pancadas de chuva fracas", 81: "Pancadas de chuva moderadas", 82: "Pancadas de chuva fortes",
    95: "Tempestade", 96: "Tempestade com granizo", 99: "Tempestade forte com granizo",
}


def _garantir_tabela_previsao_tempo():
    """Auto-migração: cache local da previsão do tempo (Open-Meteo),
    atualizado 1x por dia por uma Scheduled Task (ver
    tarefa_atualizar_previsao_tempo.py) -- a tela de agendamento consulta só
    esse cache, nunca a internet na hora (pedido do Bruno, 2026-09-10,
    testado com sucesso de conectividade do PythonAnywhere)."""
    conn = conectar()
    try:
        conn.execute("""
            CREATE TABLE previsao_tempo (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cidade TEXT,
                data TEXT,
                temp_min REAL,
                temp_max REAL,
                chance_chuva INTEGER,
                condicao TEXT,
                atualizado_em TEXT,
                UNIQUE(cidade, data)
            )
        """)
        conn.commit()
    except sqlite3.OperationalError:
        pass
    conn.close()


_garantir_tabela_previsao_tempo()


def _garantir_coluna_desativado_em():
    """Auto-migração: registra a data do 'Desligar' simples (ativo=0, sem
    excluir o cadastro) -- antes só existia data pro Excluir de verdade
    (excluido_em), então o gráfico de HC Ativo nunca refletia um
    desligamento simples, só contava quando alguém era excluído por
    completo. Bug real encontrado pelo Bruno, 2026-09-10 (Bruno Teste e
    Susana desligados mas sem refletir no gráfico).

    Preenchimento retroativo: quem já estava desligado (ativo=0) ANTES
    dessa correção nunca teve essa data salva -- não tem como saber o dia
    exato retroativamente, então usa a data de hoje como aproximação (o
    gráfico passa a refletir corretamente a partir de agora; se precisar
    do dia exato pra alguém específico, corrige direto pelo console)."""
    conn = conectar()
    try:
        conn.execute("ALTER TABLE usuarios ADD COLUMN desativado_em TEXT")
        conn.commit()
        hoje_aproximado = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE usuarios SET desativado_em=? WHERE ativo=0 AND excluido=0 AND desativado_em IS NULL",
            (hoje_aproximado,)
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass
    conn.close()


_garantir_coluna_desativado_em()


def _garantir_tabela_calendario_feriados():
    """Auto-migração: calendário de feriados/folgas coletivas (dias onde a
    operação inteira não trabalha) -- pedido do Bruno, 2026-09-11, depois
    de notar que 07/09 (feriado nacional) contou 100% de absenteísmo, mesmo
    com todo mundo tendo registrado 'Feriado' individualmente. Em vez de
    depender do registro pessoa a pessoa (que ainda contava como ausência
    no indicador), um dia marcado aqui zera 'escalados' pro dia inteiro na
    conta de Escalados x Presentes x Ausentes -- ninguém conta como
    ausente, o dia simplesmente não entra no cálculo de absenteísmo."""
    conn = conectar()
    try:
        conn.execute("""
            CREATE TABLE calendario_feriados (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                data TEXT UNIQUE NOT NULL,
                tipo TEXT,
                descricao TEXT,
                criado_por TEXT,
                criado_em TEXT
            )
        """)
        conn.commit()
    except sqlite3.OperationalError:
        pass
    conn.close()


_garantir_tabela_calendario_feriados()


CIDADES_DDD_SEED = [
    ("AMERICANA", 19),
    ("APARECIDA", 12),
    ("ARACATUBA", 18),
    ("ARARAQUARA", 16),
    ("ARARAS", 19),
    ("BAURU", 14),
    ("BOTUCATU", 14),
    ("CACAPAVA", 12),
    ("CACHOEIRA PAULISTA", 12),
    ("CAMPINAS", 19),
    ("CAPIVARI", 19),
    ("COSMOPOLIS", 19),
    ("CRUZEIRO", 12),
    ("CUBATAO", 13),
    ("ELIAS FAUSTO", 19),
    ("FRANCA", 16),
    ("GUARUJA", 13),
    ("HORTOLANDIA", 19),
    ("INDAIATUBA", 19),
    ("ITAPETININGA", 15),
    ("JACAREI", 12),
    ("JAU", 14),
    ("LIMEIRA", 19),
    ("LORENA", 12),
    ("MARILIA", 14),
    ("MOGI GUACU", 19),
    ("MOGI MIRIM", 19),
    ("MONTE MOR", 19),
    ("PAULINIA", 19),
    ("PINDAMONHANGABA", 12),
    ("PIRACICABA", 19),
    ("PORTO FELIZ", 15),
    ("PRAIA GRANDE", 13),
    ("PRESIDENTE PRUDENTE", 18),
    ("RAFARD", 19),
    ("RIBEIRAO PRETO", 16),
    ("RIO CLARO", 19),
    ("SANTA BARBARA D OESTE", 19),
    ("SANTOS", 13),
    ("SAO CARLOS", 16),
    ("SAO JOSE DO RIO PRETO", 17),
    ("SAO JOSE DOS CAMPOS", 12),
    ("SAO VICENTE", 13),
    ("SERTAOZINHO", 16),
    ("SOROCABA", 15),
    ("SUMARE", 19),
    ("TAUBATE", 12),
    ("VALINHOS", 19),
    ("VINHEDO", 19),
    ("VOTORANTIM", 15),
]


def _garantir_tabela_cidades_ddd():
    """Auto-migração: mapeamento cidade -> DDD (pedido do Bruno, 2026-09-11,
    planilha própria de referência) -- usado na Detalhamento Lista de
    Trabalho pra agrupar por DDD de forma confiável, em vez de extrair do
    telefone do cliente (que pode ter DDD de outra região por causa de
    portabilidade/mudança). Semeia só na criação da tabela (primeira vez);
    se o ADM editar/adicionar depois, não volta a sobrescrever."""
    conn = conectar()
    try:
        conn.execute("""
            CREATE TABLE cidades_ddd (
                cidade TEXT PRIMARY KEY,
                ddd INTEGER NOT NULL
            )
        """)
        conn.commit()
        conn.executemany(
            "INSERT INTO cidades_ddd (cidade, ddd) VALUES (?, ?)",
            CIDADES_DDD_SEED
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass
    conn.close()


_garantir_tabela_cidades_ddd()


def _garantir_tabela_historico_importacao_mailing():
    """Auto-migração: registra quem importou e quando, só pra contratos
    NOVOS (nunca vistos antes) -- pedido do Bruno, 2026-09-11, depois de
    notar que a timeline do cliente não mostrava a criação de contratos
    vindos de importação de planilha (só os criados via Nova Tarefa tinham
    esse registro). Não dá pra recuperar retroativamente o que já foi
    importado antes dessa migração -- só entra daqui pra frente."""
    conn = conectar()
    try:
        conn.execute("""
            CREATE TABLE historico_importacao_mailing (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contrato TEXT,
                importado_por TEXT,
                importado_em TEXT
            )
        """)
        conn.commit()
    except sqlite3.OperationalError:
        pass
    conn.close()


_garantir_tabela_historico_importacao_mailing()


def atualizar_previsao_tempo_todas_cidades():
    """Busca a previsão dos próximos 7 dias pra cada cidade cadastrada em
    COORDENADAS_CIDADES e salva/atualiza no cache local -- chamada pela
    Scheduled Task diária (tarefa_atualizar_previsao_tempo.py). Retorna
    (sucesso_qtd, falha_qtd)."""
    import urllib.request
    import urllib.error
    import json as json_lib

    conn = conectar()
    sucesso, falha = 0, 0
    agora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for cidade, (lat, lon) in COORDENADAS_CIDADES.items():
        url = (
            f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
            f"&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max,weathercode"
            f"&timezone=America/Sao_Paulo&forecast_days=7"
        )
        try:
            with urllib.request.urlopen(url, timeout=15) as resposta:
                dados = json_lib.loads(resposta.read().decode("utf-8"))
            diario = dados.get("daily", {})
            datas = diario.get("time", [])
            for i, data_str in enumerate(datas):
                temp_max = diario["temperature_2m_max"][i]
                temp_min = diario["temperature_2m_min"][i]
                chance_chuva = diario["precipitation_probability_max"][i]
                codigo = diario["weathercode"][i]
                condicao = WEATHERCODE_TEXTO.get(codigo, "—")
                conn.execute("""
                    INSERT INTO previsao_tempo (cidade, data, temp_min, temp_max, chance_chuva, condicao, atualizado_em)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(cidade, data) DO UPDATE SET
                        temp_min=excluded.temp_min, temp_max=excluded.temp_max,
                        chance_chuva=excluded.chance_chuva, condicao=excluded.condicao,
                        atualizado_em=excluded.atualizado_em
                """, (cidade, data_str, temp_min, temp_max, chance_chuva, condicao, agora_str))
            conn.commit()
            sucesso += 1
        except Exception as erro:
            print(f"Falha ao buscar previsão de {cidade}: {erro}")
            falha += 1

    conn.close()
    return sucesso, falha


@app.route("/previsao-tempo")
@login_required
def previsao_tempo_consultar():
    """Endpoint leve (JSON) pra tela de agendamento consultar a previsão do
    tempo de uma cidade numa data -- sempre lê do cache local
    (previsao_tempo), nunca chama a internet na hora."""
    cidade = (request.args.get("cidade") or "").strip().upper()
    data = (request.args.get("data") or "").strip()
    if not cidade or not data:
        return jsonify({"encontrado": False})

    conn = conectar()
    linha = conn.execute(
        "SELECT temp_min, temp_max, chance_chuva, condicao FROM previsao_tempo WHERE cidade=? AND data=?",
        (cidade, data)
    ).fetchone()
    conn.close()

    if not linha:
        return jsonify({"encontrado": False})
    return jsonify({
        "encontrado": True,
        "temp_min": round(linha["temp_min"]),
        "temp_max": round(linha["temp_max"]),
        "chance_chuva": linha["chance_chuva"],
        "condicao": linha["condicao"],
    })


def _migrar_permissoes_granulares():
    """Auto-migração (idempotente, roda 1x a cada subida): a reestruturação da
    tela de Permissões em sub-itens (2026-08-26) quebrou 4 módulos
    "guarda-chuva" -- dashboard, tratamento_gpon, qualidade e extracao -- em
    sub-permissões mais específicas. Pra ninguém perder acesso que já tinha,
    copia o valor equivalente pro(s) sub-item(ns) correspondente(s), só
    quando a sub-permissão ainda não tem nenhuma linha salva (nunca
    sobrescreve um ajuste que o ADM já tenha feito manualmente depois dessa
    migração)."""
    conn = conectar()

    def _valor(perfil, modulo):
        linha = conn.execute("SELECT permitido FROM permissoes WHERE perfil=? AND modulo=?", (perfil, modulo)).fetchone()
        return linha["permitido"] if linha else 0

    def _definir_se_ausente(perfil, modulo, valor):
        existe = conn.execute("SELECT 1 FROM permissoes WHERE perfil=? AND modulo=?", (perfil, modulo)).fetchone()
        if not existe:
            conn.execute("INSERT INTO permissoes (perfil, modulo, permitido) VALUES (?, ?, ?)", (perfil, modulo, valor))

    for perfil in PERFIS_CONFIGURAVEIS:
        # Dashboard: a visão sempre foi decidida só pelo perfil (nunca pela
        # matriz, e sem NENHUM bloqueio de acesso) -- migra pro equivalente
        # exato de antes, agora como permissão de verdade.
        _definir_se_ausente(perfil, "dashboard_pessoal", 1 if perfil in ("OPERADOR", "ANALISTA") else 0)
        _definir_se_ausente(perfil, "dashboard_equipe", 1 if perfil in ("SUPERVISOR", "ANALISTA_II") else 0)

        # GPON: quem já tinha a permissão antiga "tratamento_gpon" mantém a
        # capacidade de Tratamento; a Visualização de fila nunca foi
        # controlada pela matriz (era só ADM, hardcoded) -- começa zerada, o
        # ADM decide quem mais ganha.
        _definir_se_ausente(perfil, "gpon_tratamento", _valor(perfil, "tratamento_gpon"))
        _definir_se_ausente(perfil, "gpon_fila", 0)

        # Qualidade: "gestão" (ver e monitorar a equipe) era hardcoded só pra
        # ADM/SUPERVISOR; "própria" dependia da permissão antiga "qualidade"
        # pra quem não é ADM/SUPERVISOR.
        valor_qualidade = _valor(perfil, "qualidade")
        if perfil == "SUPERVISOR":
            _definir_se_ausente(perfil, "qualidade_gestao", valor_qualidade)
            _definir_se_ausente(perfil, "qualidade_propria", 0)
        else:
            _definir_se_ausente(perfil, "qualidade_gestao", 0)
            _definir_se_ausente(perfil, "qualidade_propria", valor_qualidade)

        # Extração: Login/Logout/Pausas era hardcoded SUPERVISOR/ADM; as
        # outras 4 dependiam da mesma permissão antiga "extracao" -- inclusive
        # a de GPON, que na verdade nunca checava permissão nenhuma (só
        # bloqueava Operador por perfil, direto no código) -- fecha essa
        # brecha aqui.
        valor_extracao = _valor(perfil, "extracao")
        _definir_se_ausente(perfil, "extracao_pontualidade", 1 if perfil == "SUPERVISOR" else 0)
        _definir_se_ausente(perfil, "extracao_tratamentos", valor_extracao)
        _definir_se_ausente(perfil, "extracao_gpon", 0 if perfil == "OPERADOR" else valor_extracao)
        _definir_se_ausente(perfil, "extracao_ausencias", valor_extracao)
        _definir_se_ausente(perfil, "extracao_usuarios", valor_extracao)
        _definir_se_ausente(perfil, "extracao_followup_atrasado", 1 if perfil == "SUPERVISOR" else 0)

    conn.commit()
    conn.close()


_migrar_permissoes_granulares()


def _garantir_permissao_historico_tratamento():
    """Auto-migração (idempotente): a tela nova de Histórico de Tratamento
    (pedida pelo Bruno, 2026-08-27) nasce liberada por padrão pro SUPERVISOR
    (é quem pediu a tela) -- os outros perfis configuráveis começam
    desmarcados, o ADM decide se libera pra mais alguém depois pela tela de
    Permissões. Nunca sobrescreve se a linha já existir (ex: ADM já mexeu)."""
    conn = conectar()
    existe = conn.execute(
        "SELECT 1 FROM permissoes WHERE perfil='SUPERVISOR' AND modulo='historico_tratamento'"
    ).fetchone()
    if not existe:
        conn.execute(
            "INSERT INTO permissoes (perfil, modulo, permitido) VALUES ('SUPERVISOR', 'historico_tratamento', 1)"
        )
        conn.commit()
    conn.close()


_garantir_permissao_historico_tratamento()


def _garantir_permissao_agendado_executado():
    """Auto-migração (idempotente): a tela nova "Agendado x Executado" (pedida
    pelo Bruno, 2026-08-28) nasce liberada por padrão pro OPERADOR (acompanha
    o próprio) e pro SUPERVISOR (acompanha a equipe) -- os outros perfis
    configuráveis começam desmarcados, o ADM decide se libera pra mais alguém
    depois. Nunca sobrescreve se a linha já existir."""
    conn = conectar()
    for perfil in ("OPERADOR", "SUPERVISOR"):
        existe = conn.execute(
            "SELECT 1 FROM permissoes WHERE perfil=? AND modulo='agendado_executado'", (perfil,)
        ).fetchone()
        if not existe:
            conn.execute(
                "INSERT INTO permissoes (perfil, modulo, permitido) VALUES (?, 'agendado_executado', 1)", (perfil,)
            )
    conn.commit()
    conn.close()


_garantir_permissao_agendado_executado()


def _garantir_permissao_tratamento_quebra():
    """Auto-migração (idempotente): o módulo novo de Tratamento de Quebra
    (pedido pelo Bruno, 2026-08-29 -- operador trata as próprias quebras de
    D-1) nasce liberado por padrão pro OPERADOR (é quem trata) -- os outros
    perfis configuráveis começam desmarcados, o ADM decide se libera pra
    mais alguém depois. Nunca sobrescreve se a linha já existir."""
    conn = conectar()
    existe = conn.execute(
        "SELECT 1 FROM permissoes WHERE perfil='OPERADOR' AND modulo='tratamento_quebra'"
    ).fetchone()
    if not existe:
        conn.execute(
            "INSERT INTO permissoes (perfil, modulo, permitido) VALUES ('OPERADOR', 'tratamento_quebra', 1)"
        )
        conn.commit()
    conn.close()


_garantir_permissao_tratamento_quebra()


def _garantir_permissao_quebra_gestao():
    """Auto-migração (idempotente): a tela nova de Gestão de Quebra de Agenda
    (/gestao/quebra, pedida pelo Bruno em 2026-08-29 -- 2ª rodada de ajustes
    da tela de quebra) nasce liberada por padrão pro SUPERVISOR (mesmo
    padrão do Histórico de Tratamento) -- os outros perfis configuráveis
    começam desmarcados, o ADM decide se libera pra mais alguém depois pela
    tela de Permissões. Nunca sobrescreve se a linha já existir."""
    conn = conectar()
    existe = conn.execute(
        "SELECT 1 FROM permissoes WHERE perfil='SUPERVISOR' AND modulo='quebra_gestao'"
    ).fetchone()
    if not existe:
        conn.execute(
            "INSERT INTO permissoes (perfil, modulo, permitido) VALUES ('SUPERVISOR', 'quebra_gestao', 1)"
        )
        conn.commit()
    conn.close()


_garantir_permissao_quebra_gestao()


@app.route("/admin/usuarios/gerenciar")
@login_required
@perfil_required("ADM")
def admin_usuarios_gerenciar():
    """Tela do ADM pra corrigir nome/login/email/celular/perfil de quem já
    está cadastrado -- pedido do Bruno, 2026-09-16, depois de perfis
    errados na importação (ex: OPERADOR_CONF importado como OPERADOR)
    passarem despercebidos e só aparecerem como sintoma em outro lugar
    (HC Ativo contando gente que não devia)."""
    conn = conectar()
    usuarios = conn.execute(
        "SELECT * FROM usuarios WHERE COALESCE(excluido,0)=0 ORDER BY ativo DESC, nome"
    ).fetchall()
    conn.close()
    return render_template(
        "AdminUsuariosGerenciar.html", usuarios=usuarios,
        perfis_existentes=PERFIS_CONFIGURAVEIS + ["ADM", "VISUALIZACAO"]
    )


@app.route("/admin/usuarios/<int:usuario_id>/atualizar-dados", methods=["POST"])
@login_required
@perfil_required("ADM")
def admin_usuarios_atualizar_dados(usuario_id):
    import urllib.parse
    usuario_logado = usuario_atual()

    nome = (request.form.get("nome") or "").strip()
    login_novo = (request.form.get("login") or "").strip()
    email = (request.form.get("email") or "").strip()
    celular = (request.form.get("celular") or "").strip()
    perfil = (request.form.get("perfil") or "").strip()

    if not nome or not login_novo or not perfil:
        return redirect("/admin/usuarios/gerenciar?erro=" + urllib.parse.quote("Nome, login e perfil são obrigatórios."))

    perfis_validos = PERFIS_CONFIGURAVEIS + ["ADM", "VISUALIZACAO"]
    if perfil not in perfis_validos:
        return redirect("/admin/usuarios/gerenciar?erro=" + urllib.parse.quote("Perfil inválido."))

    if usuario_id == usuario_logado["id"] and perfil != usuario_logado["perfil"]:
        return redirect("/admin/usuarios/gerenciar?erro=" + urllib.parse.quote("Você não pode trocar o próprio perfil."))

    conn = conectar()
    try:
        conn.execute(
            "UPDATE usuarios SET nome=?, login=?, email=?, celular=?, perfil=? WHERE id=?",
            (nome, login_novo, email, celular, perfil, usuario_id)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return redirect("/admin/usuarios/gerenciar?erro=" + urllib.parse.quote("Esse login já está em uso por outra pessoa."))
    conn.close()
    return redirect("/admin/usuarios/gerenciar")


@app.route("/admin/usuarios/<int:usuario_id>/resetar-senha", methods=["POST"])
@login_required
@perfil_required("ADM")
def admin_usuarios_resetar_senha(usuario_id):
    """Igual /supervisao/resetar_senha, mas redireciona pra Gerenciar
    Usuários em vez de Supervisão -- pedido do Bruno, 2026-09-16: com
    OPERADOR_CONF e ASSISTENTE_CLARO não aparecendo em Supervisão (de
    propósito), o ADM precisava de outro caminho pra resetar a senha
    deles. Redundante com a rota de Supervisão de propósito -- não mexi
    naquela pra não arriscar afetar o fluxo do Supervisor."""
    conn = conectar()
    alvo = conn.execute("SELECT * FROM usuarios WHERE id=?", (usuario_id,)).fetchone()
    if not alvo:
        conn.close()
        return redirect("/admin/usuarios/gerenciar")

    nova_senha_hash = generate_password_hash(alvo["login"])
    conn.execute(
        "UPDATE usuarios SET senha=?, precisa_trocar_senha=1 WHERE id=?",
        (nova_senha_hash, usuario_id)
    )
    conn.commit()
    conn.close()
    return redirect("/admin/usuarios/gerenciar")


@app.route("/admin/usuarios/<int:usuario_id>/desligar", methods=["POST"])
@login_required
@perfil_required("ADM")
def admin_usuarios_desligar(usuario_id):
    usuario_logado = usuario_atual()
    if usuario_id == usuario_logado["id"]:
        return "Você não pode desligar o próprio usuário.", 400

    conn = conectar()
    alvo = conn.execute("SELECT * FROM usuarios WHERE id=?", (usuario_id,)).fetchone()
    if not alvo:
        conn.close()
        return redirect("/admin/usuarios/gerenciar")

    agora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("UPDATE usuarios SET ativo=0, desativado_em=? WHERE id=?", (agora_str, usuario_id))
    _encerrar_sessao_e_redistribuir_tarefas(conn, alvo, agora_str)

    conn.commit()
    conn.close()
    return redirect("/admin/usuarios/gerenciar")


@app.route("/admin/usuarios/<int:usuario_id>/reativar", methods=["POST"])
@login_required
@perfil_required("ADM")
def admin_usuarios_reativar(usuario_id):
    conn = conectar()
    conn.execute("UPDATE usuarios SET ativo=1, desativado_em=NULL WHERE id=?", (usuario_id,))
    conn.commit()
    conn.close()
    return redirect("/admin/usuarios/gerenciar")


@app.route("/admin/usuarios/<int:usuario_id>/excluir", methods=["POST"])
@login_required
@perfil_required("ADM")
def admin_usuarios_excluir(usuario_id):
    solicitante = usuario_atual()
    if usuario_id == solicitante["id"]:
        return "Você não pode excluir o próprio usuário.", 400

    conn = conectar()
    alvo = conn.execute("SELECT * FROM usuarios WHERE id=?", (usuario_id,)).fetchone()
    if not alvo:
        conn.close()
        return redirect("/admin/usuarios/gerenciar")

    agora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "UPDATE usuarios SET ativo=0, excluido=1, excluido_em=?, excluido_por=? WHERE id=?",
        (agora_str, solicitante["nome"], usuario_id)
    )
    _encerrar_sessao_e_redistribuir_tarefas(conn, alvo, agora_str)

    conn.commit()
    conn.close()
    return redirect("/admin/usuarios/gerenciar")


@app.route("/admin/permissoes")
@login_required
@perfil_required("ADM")
def admin_permissoes():
    conn = conectar()
    matriz = {perfil: {} for perfil in PERFIS_CONFIGURAVEIS}
    for linha in conn.execute("SELECT perfil, modulo, permitido FROM permissoes").fetchall():
        if linha["perfil"] in matriz:
            matriz[linha["perfil"]][linha["modulo"]] = linha["permitido"]
    conn.close()
    return render_template("AdminPermissoes.html", grupos=GRUPOS_PERMISSAO, matriz=matriz)


@app.route("/admin/permissoes/salvar", methods=["POST"])
@login_required
@perfil_required("ADM")
def admin_permissoes_salvar():
    conn = conectar()
    for perfil in PERFIS_CONFIGURAVEIS:
        for chave, _label in MODULOS_PERMISSAO:
            permitido = 1 if request.form.get(f"{perfil}__{chave}") else 0
            conn.execute("""
                INSERT INTO permissoes (perfil, modulo, permitido) VALUES (?, ?, ?)
                ON CONFLICT(perfil, modulo) DO UPDATE SET permitido=excluded.permitido
            """, (perfil, chave, permitido))
    conn.commit()
    conn.close()
    return redirect("/admin/permissoes")


# ==========================
# SCRIPTS DE ATENDIMENTO (config. pelo ADM)
# ==========================

# Campos do formulário de "Gerar Script de Atendimento" (tela Cliente.html) que
# podem ser inseridos no texto dos scripts usando a sintaxe [token]. A ordem
# aqui é a ordem em que aparecem no combobox da tela de administração.
CAMPOS_SCRIPT_ATENDIMENTO = [
    ("protocolo", "Protocolo"),
    ("cliente", "Nome do cliente"),
    ("contrato", "Número do contrato"),
    ("cidade", "Cidade"),
    ("operador", "Primeiro nome do operador"),
    ("consultor", "Consultor / Consultora (escolhido na tela de Atendimento)"),
    ("atual_produto_tv", "Produto atual - TV"),
    ("atual_tv", "Valor atual - TV"),
    ("atual_produto_virtua", "Produto atual - Virtua (Internet)"),
    ("atual_virtua", "Valor atual - Virtua (Internet)"),
    ("atual_produto_fone", "Produto atual - Fone"),
    ("atual_fone", "Valor atual - Fone"),
    ("atual_total", "Valor atual - Total"),
    ("novo_produto_tv", "Novo produto - TV"),
    ("novo_valor_tv", "Novo valor - TV"),
    ("novo_produto_virtua", "Novo produto - Virtua (Internet)"),
    ("novo_valor_virtua", "Novo valor - Virtua (Internet)"),
    ("novo_produto_fone", "Novo produto - Fone"),
    ("novo_valor_fone", "Novo valor - Fone"),
    ("novo_valor_total", "Novo valor - Total"),
]

_TEXTO_SCRIPT_PADRAO = """Olá, Sr(a). [cliente], tudo bem? Meu nome é [operador], [consultor] da Claro, e falo em nome da empresa.
Estou entrando em contato porque realizamos uma atualização na rede da sua região e agora é necessário substituir o cabo coaxial pela fibra óptica no seu endereço. Essa troca vai garantir mais estabilidade, melhor qualidade de internet e menor interferência no sinal.

Além disso, seu plano será atualizado.
Atualmente o seu plano é:
TV: [atual_produto_tv] - R$ [atual_tv]
INTERNET: [atual_produto_virtua] - R$ [atual_virtua]
FONE: [atual_produto_fone] - R$ [atual_fone]
VALOR TOTAL: R$ [atual_total]

PARA
TV: [novo_produto_tv] - Valor R$ [novo_valor_tv]
INTERNET: [novo_produto_virtua] - Valor R$ [novo_valor_virtua]
FONE: [novo_produto_fone] - Valor R$ [novo_valor_fone]
VALOR TOTAL: R$ [novo_valor_total]

A troca é totalmente gratuita, não renova fidelidade e inclui a substituição dos equipamentos.
O protocolo do meu atendimento é [protocolo] e pode ser verificado pelo app minha Claro ou se preferir diretamente no site.
https://www.claro.com.br/faq/atendimento/como-consultar-meus-protocolos"""


def _garantir_tabela_scripts_atendimento():
    """Cria a tabela de scripts (se não existir) e semeia com exatamente 4 linhas
    na primeira vez -- a 1ª linha já nasce com o texto que era hardcoded na tela,
    convertido pra sintaxe de campos entre colchetes; as outras 3 nascem com um
    texto de exemplo pro ADM editar."""
    conn = conectar()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scripts_atendimento (
            id INTEGER PRIMARY KEY,
            nome_botao TEXT NOT NULL,
            texto TEXT NOT NULL
        )
    """)
    conn.commit()
    existentes = conn.execute("SELECT COUNT(*) as n FROM scripts_atendimento").fetchone()["n"]
    if existentes == 0:
        texto_exemplo = "Edite esse script em Administração > Scripts de Atendimento, usando os campos entre colchetes (ex: [cliente], [protocolo]) pra puxar os dados do formulário."
        conn.executemany(
            "INSERT INTO scripts_atendimento (id, nome_botao, texto) VALUES (?, ?, ?)",
            [
                (1, "Script padrão", _TEXTO_SCRIPT_PADRAO),
                (2, "Script 2", texto_exemplo),
                (3, "Script 3", texto_exemplo),
                (4, "Script 4", texto_exemplo),
            ],
        )
        conn.commit()
    conn.close()


_garantir_tabela_scripts_atendimento()


@app.route("/admin/scripts_atendimento")
@login_required
@perfil_required("ADM")
def admin_scripts_atendimento():
    conn = conectar()
    scripts = conn.execute("SELECT * FROM scripts_atendimento ORDER BY id").fetchall()
    conn.close()
    return render_template("AdminScriptsAtendimento.html", scripts=scripts, campos=CAMPOS_SCRIPT_ATENDIMENTO)


@app.route("/admin/scripts_atendimento/salvar", methods=["POST"])
@login_required
@perfil_required("ADM")
def admin_scripts_atendimento_salvar():
    conn = conectar()
    for i in range(1, 5):
        nome_botao = (request.form.get(f"nome_botao_{i}") or f"Script {i}").strip()
        texto = request.form.get(f"texto_{i}") or ""
        conn.execute("""
            INSERT INTO scripts_atendimento (id, nome_botao, texto) VALUES (?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET nome_botao=excluded.nome_botao, texto=excluded.texto
        """, (i, nome_botao, texto))
    conn.commit()
    conn.close()
    return redirect("/admin/scripts_atendimento")


# ==========================
# INFORMAÇÕES (NOTÍCIAS)
# ==========================

PERFIS_QUE_POSTAM_NOTICIA = ("ADM", "SUPERVISOR", "ANALISTA", "ANALISTA_II")


def sanitizar_html_noticia(html):
    """Limpeza leve: tira <script> e atributos on*= (onclick, onerror, etc.).
    Não é um sanitizador robusto de verdade -- serve como rede de segurança
    básica, já que só ADM/Supervisor/Analista conseguem publicar (pessoal de
    confiança da própria operação, não é conteúdo aberto pra qualquer um)."""
    import re
    html = re.sub(r"<script.*?</script>", "", html, flags=re.IGNORECASE | re.DOTALL)
    html = re.sub(r'\son\w+\s*=\s*"[^"]*"', "", html, flags=re.IGNORECASE)
    html = re.sub(r"\son\w+\s*=\s*'[^']*'", "", html, flags=re.IGNORECASE)
    return html


@app.route("/noticias")
@login_required
def noticias():
    usuario = usuario_atual()
    conn = conectar()
    lista = conn.execute("""
        SELECT n.*,
            EXISTS(SELECT 1 FROM noticias_leituras l WHERE l.noticia_id=n.id AND l.usuario_id=?) as lida
        FROM noticias n WHERE n.ativo=1 ORDER BY n.data_publicacao DESC
    """, (usuario["id"],)).fetchall()
    conn.close()
    pode_postar = usuario["perfil"] in PERFIS_QUE_POSTAM_NOTICIA
    pode_ver_relatorio = usuario["perfil"] in ("ADM", "SUPERVISOR")
    return render_template("Noticias.html", lista=lista, pode_postar=pode_postar, pode_ver_relatorio=pode_ver_relatorio,
                            noticia_selecionada=None)


@app.route("/noticias/nova")
@login_required
def noticias_nova():
    usuario = usuario_atual()
    if usuario["perfil"] not in PERFIS_QUE_POSTAM_NOTICIA:
        return "Seu perfil não pode publicar notícias.", 403
    return render_template("NoticiaNova.html")


@app.route("/noticias/salvar", methods=["POST"])
@login_required
def noticias_salvar():
    usuario = usuario_atual()
    if usuario["perfil"] not in PERFIS_QUE_POSTAM_NOTICIA:
        return "Seu perfil não pode publicar notícias.", 403

    titulo = (request.form.get("titulo") or "").strip()
    conteudo_html = sanitizar_html_noticia(request.form.get("conteudo_html") or "")

    if not titulo or not conteudo_html.strip():
        return redirect("/noticias/nova")

    conn = conectar()
    conn.execute("""
        INSERT INTO noticias (titulo, conteudo_html, autor, data_publicacao, ativo)
        VALUES (?, ?, ?, ?, 1)
    """, (titulo, conteudo_html, usuario["nome"], datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()
    return redirect("/noticias")


@app.route("/noticias/<int:noticia_id>/editar")
@login_required
def noticias_editar_form(noticia_id):
    usuario = usuario_atual()
    if usuario["perfil"] not in PERFIS_QUE_POSTAM_NOTICIA:
        return "Seu perfil não pode editar notícias.", 403
    conn = conectar()
    noticia = conn.execute("SELECT * FROM noticias WHERE id=?", (noticia_id,)).fetchone()
    conn.close()
    if not noticia:
        return redirect("/noticias")
    return render_template("NoticiaNova.html", noticia=noticia)


@app.route("/noticias/<int:noticia_id>/editar/salvar", methods=["POST"])
@login_required
def noticias_editar_salvar(noticia_id):
    usuario = usuario_atual()
    if usuario["perfil"] not in PERFIS_QUE_POSTAM_NOTICIA:
        return "Seu perfil não pode editar notícias.", 403

    titulo = (request.form.get("titulo") or "").strip()
    conteudo_html = sanitizar_html_noticia(request.form.get("conteudo_html") or "")
    if not titulo or not conteudo_html.strip():
        return redirect(f"/noticias/{noticia_id}/editar")

    conn = conectar()
    conn.execute("UPDATE noticias SET titulo=?, conteudo_html=? WHERE id=?", (titulo, conteudo_html, noticia_id))
    conn.commit()
    conn.close()
    return redirect(f"/noticias/{noticia_id}")


@app.route("/noticias/<int:noticia_id>/excluir", methods=["POST"])
@login_required
def noticias_excluir(noticia_id):
    usuario = usuario_atual()
    if usuario["perfil"] not in PERFIS_QUE_POSTAM_NOTICIA:
        return "Seu perfil não pode excluir notícias.", 403
    conn = conectar()
    conn.execute("UPDATE noticias SET ativo=0 WHERE id=?", (noticia_id,))
    conn.commit()
    conn.close()
    return redirect("/noticias")


@app.route("/noticias/<int:noticia_id>")
@login_required
def noticias_ver(noticia_id):
    usuario = usuario_atual()
    conn = conectar()
    noticia = conn.execute("SELECT * FROM noticias WHERE id=?", (noticia_id,)).fetchone()
    if not noticia:
        conn.close()
        return redirect("/noticias")

    conn.execute("""
        INSERT OR IGNORE INTO noticias_leituras (noticia_id, usuario_id, data_leitura)
        VALUES (?, ?, ?)
    """, (noticia_id, usuario["id"], datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()

    lista = conn.execute("""
        SELECT n.*,
            EXISTS(SELECT 1 FROM noticias_leituras l WHERE l.noticia_id=n.id AND l.usuario_id=?) as lida
        FROM noticias n WHERE n.ativo=1 ORDER BY n.data_publicacao DESC
    """, (usuario["id"],)).fetchall()
    conn.close()

    pode_postar = usuario["perfil"] in PERFIS_QUE_POSTAM_NOTICIA
    pode_ver_relatorio = usuario["perfil"] in ("ADM", "SUPERVISOR")
    return render_template("Noticias.html", lista=lista, pode_postar=pode_postar, pode_ver_relatorio=pode_ver_relatorio,
                            noticia_selecionada=noticia)


@app.route("/noticias/relatorio")
@login_required
def noticias_relatorio():
    usuario = usuario_atual()
    if usuario["perfil"] not in ("ADM", "SUPERVISOR"):
        return "Seu perfil não pode ver esse relatório.", 403
    conn = conectar()
    lista = conn.execute("""
        SELECT n.id, n.titulo, n.data_publicacao, n.autor,
            (SELECT COUNT(*) FROM noticias_leituras l WHERE l.noticia_id=n.id) as qtd_lida
        FROM noticias n WHERE n.ativo=1 ORDER BY n.data_publicacao DESC
    """).fetchall()
    total_usuarios = conn.execute("SELECT COUNT(*) c FROM usuarios WHERE ativo=1").fetchone()["c"]
    conn.close()
    return render_template("NoticiaRelatorio.html", lista=lista, total_usuarios=total_usuarios)


@app.route("/noticias/<int:noticia_id>/leitores")
@login_required
def noticias_leitores(noticia_id):
    usuario = usuario_atual()
    if usuario["perfil"] not in ("ADM", "SUPERVISOR"):
        return "Seu perfil não pode ver esse relatório.", 403
    conn = conectar()
    noticia = conn.execute("SELECT * FROM noticias WHERE id=?", (noticia_id,)).fetchone()
    if not noticia:
        conn.close()
        return redirect("/noticias/relatorio")

    todos = conn.execute("SELECT * FROM usuarios WHERE ativo=1 ORDER BY nome").fetchall()
    leituras = {
        l["usuario_id"]: l["data_leitura"]
        for l in conn.execute("SELECT usuario_id, data_leitura FROM noticias_leituras WHERE noticia_id=?", (noticia_id,)).fetchall()
    }
    conn.close()

    leram = [u for u in todos if u["id"] in leituras]
    nao_leram = [u for u in todos if u["id"] not in leituras]
    return render_template("NoticiaLeitores.html", noticia=noticia, leram=leram, nao_leram=nao_leram, leituras=leituras)


# ==========================
# QUALIDADE (MONITORIA)
# ==========================
# Duas sub-permissões (2026-08-26): "qualidade_gestao" (ver monitorias de
# todo mundo + criar novas -- antes hardcoded pra ADM/SUPERVISOR) e
# "qualidade_propria" (ver só o próprio histórico). Substituem a antiga
# permissão única "qualidade" e a lista fixa PERFIS_QUE_MONITORAM.


@app.route("/qualidade")
@login_required
def qualidade():
    usuario = usuario_atual()
    eh_gestor = perfil_pode(usuario["perfil"], "qualidade_gestao")
    if not (eh_gestor or perfil_pode(usuario["perfil"], "qualidade_propria")):
        return "Acesso restrito.", 403
    conn = conectar()

    if eh_gestor:
        data_fim = request.args.get("data_fim") or hoje_str()
        data_inicio = request.args.get("data_inicio") or (datetime.strptime(data_fim, "%Y-%m-%d") - timedelta(days=30)).strftime("%Y-%m-%d")

        monitorias = conn.execute("""
            SELECT m.*, u.nome AS operador_nome, s.nome AS supervisor_nome
            FROM monitorias m
            JOIN usuarios u ON u.id = m.operador_id
            JOIN usuarios s ON s.id = m.supervisor_id
            WHERE date(m.data_monitoria) BETWEEN ? AND ?
            ORDER BY m.data_monitoria DESC
        """, (data_inicio, data_fim)).fetchall()

        total_monitorias = len(monitorias)
        nota_consolidada = round(sum(m["nota_final"] for m in monitorias) / total_monitorias, 1) if total_monitorias else 0.0
        total_desvios = sum(
            sum(1 for chave, _, _ in PERGUNTAS_MONITORIA if m[chave] == 0)
            for m in monitorias
        )
        aceites_realizados = sum(1 for m in monitorias if m["aceite_status"] == "ACEITO")
        aceites_pendentes = total_monitorias - aceites_realizados

        # Dispersão: agendamentos x nota, por operador monitorado no período
        agendamentos_por_operador = {}
        notas_por_operador = {}
        for m in monitorias:
            notas_por_operador.setdefault(m["operador_id"], []).append(m["nota_final"])

        pontos_dispersao = []
        for operador_id, notas in notas_por_operador.items():
            op = conn.execute("SELECT nome FROM usuarios WHERE id=?", (operador_id,)).fetchone()
            qtd_agendamentos = conn.execute("""
                SELECT COUNT(*) c FROM atividades
                WHERE operador=? AND tabulacao='Agendado' AND date(data_inicio) BETWEEN ? AND ?
            """, (op["nome"], data_inicio, data_fim)).fetchone()["c"]
            pontos_dispersao.append({
                "operador": op["nome"],
                "agendamentos": qtd_agendamentos,
                "nota": round(sum(notas) / len(notas), 1),
            })

        media_nota = round(sum(p["nota"] for p in pontos_dispersao) / len(pontos_dispersao), 1) if pontos_dispersao else 0
        media_agendamentos = round(sum(p["agendamentos"] for p in pontos_dispersao) / len(pontos_dispersao), 1) if pontos_dispersao else 0

        for p in pontos_dispersao:
            nota_alta = p["nota"] >= media_nota
            agenda_alta = p["agendamentos"] >= media_agendamentos
            if nota_alta and agenda_alta:
                p["quadrante"] = "A"
            elif agenda_alta and not nota_alta:
                p["quadrante"] = "B"
            elif not nota_alta and not agenda_alta:
                p["quadrante"] = "C"
            else:
                p["quadrante"] = "D"

        # Evolução mensal da nota por pessoa, no ano escolhido (independente do
        # filtro de data principal -- essa visão é sempre por ano inteiro)
        ano_selecionado = int(request.args.get("ano") or datetime.now().year)
        evolucao_rows = conn.execute("""
            SELECT u.nome AS operador, CAST(strftime('%m', m.data_monitoria) AS INTEGER) AS mes, AVG(m.nota_final) AS media_nota
            FROM monitorias m
            JOIN usuarios u ON u.id = m.operador_id
            WHERE strftime('%Y', m.data_monitoria) = ?
            GROUP BY u.nome, mes
            ORDER BY u.nome, mes
        """, (str(ano_selecionado),)).fetchall()

        dados_por_operador_evolucao = {}
        for linha in evolucao_rows:
            dados_por_operador_evolucao.setdefault(linha["operador"], {})[linha["mes"]] = round(linha["media_nota"], 1)

        series_evolucao = [
            {"operador": operador, "valores": [meses.get(mes) for mes in range(1, 13)]}
            for operador, meses in dados_por_operador_evolucao.items()
        ]

        anos_com_monitoria = conn.execute("""
            SELECT DISTINCT CAST(strftime('%Y', data_monitoria) AS INTEGER) AS ano FROM monitorias ORDER BY ano DESC
        """).fetchall()
        anos_disponiveis = [a["ano"] for a in anos_com_monitoria if a["ano"]]
        if ano_selecionado not in anos_disponiveis:
            anos_disponiveis.append(ano_selecionado)
        anos_disponiveis = sorted(set(anos_disponiveis), reverse=True)

        conn.close()
        return render_template(
            "QualidadeGestao.html",
            data_inicio=data_inicio, data_fim=data_fim,
            total_monitorias=total_monitorias, nota_consolidada=nota_consolidada,
            total_desvios=total_desvios, aceites_realizados=aceites_realizados, aceites_pendentes=aceites_pendentes,
            monitorias=monitorias, pontos_dispersao=pontos_dispersao,
            media_nota=media_nota, media_agendamentos=media_agendamentos,
            series_evolucao=series_evolucao, ano_selecionado=ano_selecionado, anos_disponiveis=anos_disponiveis,
        )

    # Visão do Operador/Analista: histórico próprio
    minhas_monitorias = conn.execute("""
        SELECT m.*, s.nome AS supervisor_nome
        FROM monitorias m
        JOIN usuarios s ON s.id = m.supervisor_id
        WHERE m.operador_id = ?
        ORDER BY m.data_monitoria DESC
    """, (usuario["id"],)).fetchall()
    conn.close()
    return render_template("QualidadeOperador.html", minhas_monitorias=minhas_monitorias)


@app.route("/qualidade/nova")
@login_required
def qualidade_nova():
    usuario = usuario_atual()
    if not perfil_pode(usuario["perfil"], "qualidade_gestao"):
        return "Seu perfil não pode criar monitorias.", 403
    conn = conectar()
    operadores = conn.execute("SELECT id, nome FROM usuarios WHERE ativo=1 AND perfil IN ('OPERADOR','ANALISTA') ORDER BY nome").fetchall()
    conn.close()
    return render_template("QualidadeNova.html", operadores=operadores, perguntas=PERGUNTAS_MONITORIA)


@app.route("/qualidade/metricas/<int:usuario_id>")
@login_required
def qualidade_metricas(usuario_id):
    usuario = usuario_atual()
    if not perfil_pode(usuario["perfil"], "qualidade_gestao"):
        return {"erro": "sem permissão"}, 403

    conn = conectar()
    alvo = conn.execute("SELECT id, nome FROM usuarios WHERE id=?", (usuario_id,)).fetchone()
    if not alvo:
        conn.close()
        return {"erro": "usuário não encontrado"}, 404

    data_fim = hoje_str()
    data_inicio = (datetime.strptime(data_fim, "%Y-%m-%d") - timedelta(days=90)).strftime("%Y-%m-%d")

    metricas = calcular_metricas_operador(conn, alvo["id"], alvo["nome"], data_inicio, data_fim)
    media_geral = calcular_media_geral_metricas(conn, data_inicio, data_fim)
    conn.close()

    return {"metricas": metricas, "media_geral": media_geral, "periodo": {"inicio": data_inicio, "fim": data_fim}}


@app.route("/qualidade/salvar", methods=["POST"])
@login_required
def qualidade_salvar():
    usuario = usuario_atual()
    if not perfil_pode(usuario["perfil"], "qualidade_gestao"):
        return "Seu perfil não pode criar monitorias.", 403

    operador_id = request.form.get("operador_id")
    if not operador_id:
        return redirect("/qualidade/nova")

    respostas = {}
    for chave, _, _ in PERGUNTAS_MONITORIA:
        respostas[chave] = 1 if request.form.get(chave) == "sim" else 0

    nota_final = calcular_nota_monitoria(respostas)

    conn = conectar()

    # Snapshot dos indicadores comparativos NESSE momento -- fica gravado junto
    # com a monitoria, pra não mudar retroativamente se os números "ao vivo"
    # do operador mudarem depois (importante pro feedback registrado fazer sentido no futuro).
    alvo = conn.execute("SELECT id, nome FROM usuarios WHERE id=?", (operador_id,)).fetchone()
    metricas_json = media_geral_json = None
    if alvo:
        data_fim_metricas = hoje_str()
        data_inicio_metricas = (datetime.strptime(data_fim_metricas, "%Y-%m-%d") - timedelta(days=90)).strftime("%Y-%m-%d")
        metricas = calcular_metricas_operador(conn, alvo["id"], alvo["nome"], data_inicio_metricas, data_fim_metricas)
        media_geral = calcular_media_geral_metricas(conn, data_inicio_metricas, data_fim_metricas)
        metricas_json = json.dumps(metricas, ensure_ascii=False)
        media_geral_json = json.dumps(media_geral, ensure_ascii=False) if media_geral else None

    conn.execute("""
        INSERT INTO monitorias (
            operador_id, supervisor_id, data_monitoria,
            q1_abordagem, q2_agilidade, q3_cordialidade, q4_oferta, q5_fechamento, q6_contato, q7_tabulacao,
            nota_final, itens_pontuados, pontos_fortes, feedback_plano_acao, informacoes_adicionais,
            aceite_status, visualizado_pelo_operador, metricas_json, media_geral_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDENTE', 0, ?, ?)
    """, (
        operador_id, usuario["id"], datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        respostas["q1_abordagem"], respostas["q2_agilidade"], respostas["q3_cordialidade"],
        respostas["q4_oferta"], respostas["q5_fechamento"], respostas["q6_contato"], respostas["q7_tabulacao"],
        nota_final,
        (request.form.get("itens_pontuados") or "").strip(),
        (request.form.get("pontos_fortes") or "").strip(),
        (request.form.get("feedback_plano_acao") or "").strip(),
        (request.form.get("informacoes_adicionais") or "").strip(),
        metricas_json, media_geral_json,
    ))
    conn.commit()
    conn.close()
    return redirect("/qualidade")


@app.route("/qualidade/<int:monitoria_id>")
@login_required
def qualidade_detalhe(monitoria_id):
    usuario = usuario_atual()
    conn = conectar()
    m = conn.execute("""
        SELECT m.*, u.nome AS operador_nome, s.nome AS supervisor_nome
        FROM monitorias m
        JOIN usuarios u ON u.id = m.operador_id
        JOIN usuarios s ON s.id = m.supervisor_id
        WHERE m.id=?
    """, (monitoria_id,)).fetchone()

    if not m:
        conn.close()
        return redirect("/qualidade")

    eh_dono = usuario["id"] == m["operador_id"]
    eh_gestor = perfil_pode(usuario["perfil"], "qualidade_gestao")
    if not (eh_dono or eh_gestor):
        conn.close()
        return "Você não tem acesso a essa monitoria.", 403

    if eh_dono and not m["visualizado_pelo_operador"]:
        conn.execute("UPDATE monitorias SET visualizado_pelo_operador=1 WHERE id=?", (monitoria_id,))
        conn.commit()

    conn.close()

    metricas = json.loads(m["metricas_json"]) if m["metricas_json"] else None
    media_geral = json.loads(m["media_geral_json"]) if m["media_geral_json"] else None

    return render_template("QualidadeDetalhe.html", m=m, perguntas=PERGUNTAS_MONITORIA, eh_dono=eh_dono,
                            metricas=metricas, media_geral=media_geral)


@app.route("/qualidade/<int:monitoria_id>/aceitar", methods=["POST"])
@login_required
def qualidade_aceitar(monitoria_id):
    usuario = usuario_atual()
    conn = conectar()
    m = conn.execute("SELECT * FROM monitorias WHERE id=?", (monitoria_id,)).fetchone()
    if not m or m["operador_id"] != usuario["id"]:
        conn.close()
        return "Você não pode dar aceite nessa monitoria.", 403

    observacao = (request.form.get("aceite_observacao") or "").strip()
    conn.execute("""
        UPDATE monitorias SET aceite_status='ACEITO', aceite_observacao=?, aceite_data=? WHERE id=?
    """, (observacao, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), monitoria_id))
    conn.commit()
    conn.close()
    return redirect(f"/qualidade/{monitoria_id}")


@app.route("/qualidade/extracao")
@login_required
def qualidade_extracao():
    usuario = usuario_atual()
    if not perfil_pode(usuario["perfil"], "qualidade_gestao"):
        return "Seu perfil não pode extrair esses dados.", 403

    data_inicio = request.args.get("data_inicio")
    data_fim = request.args.get("data_fim")
    if not data_inicio or not data_fim:
        return "Informe data de início e fim.", 400

    conn = conectar()
    linhas = conn.execute("""
        SELECT m.*, u.nome AS operador_nome, s.nome AS supervisor_nome
        FROM monitorias m
        JOIN usuarios u ON u.id = m.operador_id
        JOIN usuarios s ON s.id = m.supervisor_id
        WHERE date(m.data_monitoria) BETWEEN ? AND ?
        ORDER BY m.data_monitoria DESC
    """, (data_inicio, data_fim)).fetchall()
    conn.close()

    registros = []
    for m in linhas:
        registros.append({
            "Operador": m["operador_nome"],
            "Supervisor": m["supervisor_nome"],
            "Data": m["data_monitoria"],
            "Abordagem": "Sim" if m["q1_abordagem"] else "Não",
            "Agilidade": "Sim" if m["q2_agilidade"] else "Não",
            "Cordialidade": "Sim" if m["q3_cordialidade"] else "Não",
            "Oferta": "Sim" if m["q4_oferta"] else "Não",
            "Fechamento": "Sim" if m["q5_fechamento"] else "Não",
            "Contato": "Sim" if m["q6_contato"] else "Não",
            "Tabulação": "Sim" if m["q7_tabulacao"] else "Não",
            "Nota Final": m["nota_final"],
            "Itens Pontuados": m["itens_pontuados"],
            "Pontos Fortes": m["pontos_fortes"],
            "Feedback / Plano de Ação": m["feedback_plano_acao"],
            "Informações Adicionais": m["informacoes_adicionais"],
            "Status do Aceite": m["aceite_status"],
            "Observação do Aceite": m["aceite_observacao"],
            "Data do Aceite": m["aceite_data"],
        })

    df = pd.DataFrame(registros)
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Monitorias")
    buffer.seek(0)

    nome_arquivo = f"extracao_monitorias_{data_inicio}_a_{data_fim}.xlsx"
    return send_file(
        buffer, as_attachment=True, download_name=nome_arquivo,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )




@app.route("/tarefas/cidades")
@login_required
@perfil_required("ADM")
def tarefas_cidades():
    conn = conectar()
    cidades = conn.execute("SELECT * FROM cidades ORDER BY nome").fetchall()
    conn.close()
    return render_template("TarefaCidades.html", cidades=cidades)


@app.route("/tarefas/cidades/salvar", methods=["POST"])
@login_required
@perfil_required("ADM")
def tarefas_cidades_salvar():
    nome = (request.form.get("nome") or "").strip()
    if nome:
        conn = conectar()
        conn.execute("""
            INSERT INTO cidades (nome, ativo) VALUES (?, 1)
            ON CONFLICT(nome) DO UPDATE SET ativo=1
        """, (nome,))
        conn.commit()
        conn.close()
    return redirect("/tarefas/cidades")


@app.route("/tarefas/cidades/<int:cidade_id>/alternar", methods=["POST"])
@login_required
@perfil_required("ADM")
def tarefas_cidades_alternar(cidade_id):
    conn = conectar()
    conn.execute("UPDATE cidades SET ativo = 1 - ativo WHERE id=?", (cidade_id,))
    conn.commit()
    conn.close()
    return redirect("/tarefas/cidades")


# Módulo Confirmação de Agenda -- pedido do Bruno, 2026-09-16. Vive inteiro
# em confirmacao_agenda.py (rotas, banco próprio, lógica de distribuição);
# aqui só registra o blueprint. Import feito só agora (não lá em cima, no
# topo do arquivo) de propósito -- o módulo importa login_required/
# permissao_required/usuario_atual DESTE arquivo, e precisa que essas
# funções já estejam definidas quando ele for carregado, senão dá erro de
# import circular.
from confirmacao_agenda import bp_confirmacao_agenda
app.register_blueprint(bp_confirmacao_agenda)

# Módulo Relatório de Clima -- pedido do Bruno, 2026-09-17. Mesmo padrão do
# blueprint acima: vive inteiro em relatorio_clima.py, só lê o crm.db
# (nunca escreve), aqui só registra.
from relatorio_clima import bp_relatorio_clima
app.register_blueprint(bp_relatorio_clima)

# Módulo Tratamento de Backlog -- pedido do Bruno, 2026-09-19. Mesmo padrão
# dos blueprints acima: vive inteiro em backlog.py, banco próprio
# (backlog.db), só lê o crm.db pra resolver DDD na importação e grava uma
# atividade real em crm.db só no fechamento AGENDADO (combinado com o
# Bruno). Aqui só registra.
from backlog import bp_backlog
app.register_blueprint(bp_backlog)

# Módulo Cruzamento com a Base GED -- pedido do Bruno, 2026-10-16. Mesmo
# padrão dos blueprints acima: vive inteiro em cruzamento_ged.py (rotas,
# tabelas ged_* no crm.db, tela GedCruzamento.html). Tira da fila
# (status BLOQUEADO_GED) o que não está com venda liberada no GED e devolve
# sozinho quando libera. Também registra a função ged_alerta() usada pelo
# aviso das telas de atendimento (_alerta_ged.html).
from cruzamento_ged import bp_cruzamento_ged
app.register_blueprint(bp_cruzamento_ged)

# Sincronização dos agendamentos com a planilha da Confirmação de Agenda
# (reagendamentos feitos fora do CRM) -- pedido do Bruno, 2026-10-16. Roda
# dentro de admin_importacao_confirmacao_agenda; aqui só registra a tela
# /agendados/reagendamentos-externos.
from sincroniza_agenda import bp_sincroniza_agenda
app.register_blueprint(bp_sincroniza_agenda)

# Painel de Custos da Esteira de Migração (visão exclusiva do ADM) -- pedido
# do Bruno, 2026-10-16. Vive em painel_custos.py (tabelas custos_mensais /
# custos_config no crm.db, semeadas com a planilha histórica na 1ª subida).
from painel_custos import bp_painel_custos
app.register_blueprint(bp_painel_custos)

# Painel de Saúde do Sistema (visão do ADM) -- pedido do Bruno, 2026-10-17.
# saude_sistema.py: rotinas agendadas, erros 500 (sinal got_request_exception,
# ligado no registro do blueprint) e fila do dia com alertas no Telegram, tudo
# alimentado pelo pulso do Cron do Supabase (/interno/verificar_envio_diario).
from saude_sistema import bp_saude
app.register_blueprint(bp_saude)

# Sincronização de DADOS produção -> broma (pedido do Bruno, 2026-10-17).
# Na produção expõe /interno/sincronia/exportar (protegido por token em
# ~/token_sincronia.txt); na broma, a tela /admin/sincronia busca e aplica.
from sincronia_teste import bp_sincronia
app.register_blueprint(bp_sincronia)

# Backup diário dos bancos no Supabase Storage (2026-10-17) -- substitui o
# backup no Google Drive. Tela em /admin/backups; rotina na Scheduled Task
# tarefa_backup_nuvem.py, acompanhada pelo Monitor.
from backup_nuvem import bp_backup_nuvem
app.register_blueprint(bp_backup_nuvem)

# Resumo do Enfileiramento por bloco de prioridade -- subpágina do Dashboard
# (2026-10-17), mesma trava da visão de equipe.
from resumo_enfileiramento import bp_resumo_enfileiramento
app.register_blueprint(bp_resumo_enfileiramento)


# ==========================
# AMBIENTE DE TESTE -- conta separada no PythonAnywhere (2026-10-15)
# ==========================
# Substituiu o antigo "ambiente espelho" (cópia rodando dentro da mesma web
# app, 2026-09-25), removido em 2026-10-16 junto com as rotas /espelho/* e o
# botão do Portal.
#
# Este bloco fica de propósito NO FINAL do app.py: as funções redefinidas
# aqui substituem as versões lá de cima (o Python procura o nome na hora da
# chamada), então todo lugar que manda coisa pra fora -- rotinas, botões de
# teste, relatórios -- passa a cair no bloqueio, sem precisar mexer em cada
# chamada. Qualquer integração externa NOVA que for criada daqui pra frente
# precisa ser adicionada ao bloqueio abaixo.
# Conta de teste separada (PythonAnywhere gratuito, decidido em 2026-10-15):
# o mesmo bloqueio vale pra ela. Quem cria o AMBIENTE_TESTE.flag é o
# preparar_ambiente_teste.py; a pasta de produção nunca tem nenhum dos dois
# arquivos, então em produção os dois ficam False e nada aqui é trocado.
EH_AMBIENTE_TESTE = os.path.exists(os.path.join(PASTA_BASE, "AMBIENTE_TESTE.flag"))

EH_AMBIENTE_SEGURO = EH_AMBIENTE_TESTE

if EH_AMBIENTE_SEGURO:
    _NOME_AMBIENTE = "TESTE"
    _MSG_BLOQUEIO_AMBIENTE = "Bloqueado em ambiente de teste (sem comunicação externa)."

    def chamar_funcao_supabase(project_url, anon_key, nome_funcao, payload):
        print(f"[{_NOME_AMBIENTE}] chamada ao Supabase bloqueada: {nome_funcao}", file=sys.stderr)
        return False, _MSG_BLOQUEIO_AMBIENTE

    def enviar_mensagem_telegram(token, chat_id, texto):
        print(f"[{_NOME_AMBIENTE}] envio direto ao Telegram bloqueado", file=sys.stderr)
        return False, _MSG_BLOQUEIO_AMBIENTE

    def escrever_arquivo_relatorio(pasta_destino, corpo_html):
        print(f"[{_NOME_AMBIENTE}] gravação do relatório de e-mail bloqueada", file=sys.stderr)
        return False, _MSG_BLOQUEIO_AMBIENTE

    # Faixa de aviso em TODA página HTML. Injetada aqui, na resposta, e não
    # no layout.html de propósito: nove templates (Portal, Login, os painéis
    # de cotas, o relatório de clima, os layouts do backlog e da confirmação)
    # não estendem o layout, e são justamente os primeiros que a pessoa vê.
    # Fazendo na resposta, qualquer página -- inclusive as que forem criadas
    # depois -- fica coberta sem precisar lembrar de marcar cada uma.
    #
    # Fica fixa no rodapé porque a .sidebar é position:fixed com 100vh: uma
    # faixa no topo cobriria o logo do menu.
    _FAIXA_AMBIENTE = (
        '<div style="position:fixed;left:0;right:0;bottom:0;z-index:99999;'
        'background:#B45309;color:#fff;font-family:system-ui,-apple-system,'
        'Segoe UI,Roboto,sans-serif;font-size:13px;font-weight:600;'
        'letter-spacing:.3px;text-align:center;padding:7px 12px;'
        'box-shadow:0 -2px 10px rgba(0,0,0,.25);">'
        f'AMBIENTE DE {_NOME_AMBIENTE} &nbsp;&middot;&nbsp; dados copiados da produção'
        ' &nbsp;&middot;&nbsp; nada aqui afeta o sistema real</div>'
    )

    @app.after_request
    def _injetar_faixa_ambiente(resposta):
        # direct_passthrough = resposta de arquivo (send_file/send_from_directory):
        # ler o corpo dela quebraria o envio, então passa direto.
        if resposta.direct_passthrough:
            return resposta
        if not (resposta.content_type or "").startswith("text/html"):
            return resposta
        try:
            corpo = resposta.get_data(as_text=True)
            if "</body>" in corpo:
                resposta.set_data(corpo.replace("</body>", _FAIXA_AMBIENTE + "</body>", 1))
        except Exception as erro:  # nunca derrubar uma página por causa da faixa
            print(f"[{_NOME_AMBIENTE}] não consegui injetar a faixa: {erro}", file=sys.stderr)
        return resposta


@app.context_processor
def injetar_ambiente_teste():
    return {"eh_ambiente_teste": EH_AMBIENTE_TESTE}


if __name__ == "__main__":
    ip_local = _ip_na_rede_local()

    threading.Thread(target=_rotina_encerramento_automatico, daemon=True).start()
    print("Rotina de encerramento automático de jornada (18h30) iniciada em segundo plano.")

    threading.Thread(target=_rotina_aniversarios, daemon=True).start()
    print("Rotina de postagem automática de aniversários iniciada em segundo plano.")

    threading.Thread(target=_rotina_email_envio_automatico, daemon=True).start()
    print("Rotina de envio automático do relatório por e-mail iniciada em segundo plano.")

    threading.Thread(target=_rotina_telegram_supabase_envio_automatico, daemon=True).start()
    print("Rotina de envio automático do relatório via Telegram/Supabase iniciada em segundo plano.")

    threading.Thread(target=_rotina_supabase_consulta_periodica, daemon=True).start()
    print("Rotina de atualização periódica pra consulta via Telegram (8h-18h) iniciada em segundo plano.")

    try:
        from waitress import serve
        print("Servidor de produção (Waitress) rodando.")
        print(f"  Nesta máquina: http://127.0.0.1:5000")
        print(f"  Na rede (outros computadores/celulares no mesmo Wi-Fi): http://{ip_local}:5000")
        print("Ctrl+C pra parar")
        serve(app, host="0.0.0.0", port=5000, threads=16)
    except ImportError:
        print("AVISO: pacote 'waitress' não encontrado — rodando com o servidor de")
        print("desenvolvimento do Flask (não recomendado com várias pessoas usando ao mesmo tempo).")
        print("Instale com: pip install waitress")
        print(f"  Na rede: http://{ip_local}:5000")
        app.run(host="0.0.0.0", port=5000, debug=True)
