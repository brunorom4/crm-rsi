"""
Sincronização dos DADOS da produção para o ambiente de teste (broma) --
pedido do Bruno, 2026-10-17: "um processo de atualização do banco de dados
da broma, para sempre que for necessário as duas versões ficarem
sincronizadas".

Antes: Backup Completo na produção -> baixar o zip -> subir pelo Files na
broma -> rodar preparar_ambiente_teste.py -> Reload. Cinco passos e ~30 MB
passando pelo computador do Bruno.

Agora: botão "Atualizar dados da produção" dentro da broma (ou o comando
`python sincronia_teste.py` no Bash da broma, que faz a mesma coisa se o
site estiver fora do ar). A broma busca os dados direto da produção.

COMO FUNCIONA

  PRODUÇÃO  POST /interno/sincronia/exportar   (header X-Token-Sincronia)
            - só existe na produção (na broma responde 404)
            - desligada se não houver ~/token_sincronia.txt (32+ caracteres)
            - copia os bancos com a API de backup do SQLite (cópia consistente
              mesmo com gente usando o sistema -- inclui o que está no -wal)
            - zipa bancos + pastas de dados e devolve. Código NÃO vai: código
              anda pelo git.
            - registra cada exportação no Monitor (rotina "exportacao_teste")

  BROMA     /admin/sincronia  (só ADM, só com AMBIENTE_TESTE.flag)
            - baixa o zip (o plano grátis do PythonAnywhere só acessa sites da
              lista liberada, e *.pythonanywhere.com está nela)
            - extrai os bancos numa pasta de preparo e roda integrity_check
              ANTES de mexer em qualquer coisa: se vier quebrado, a broma
              continua como estava
            - troca bancos (apagando -wal/-shm antigos antes, senão o SQLite
              aplicaria o WAL velho no banco novo) e pastas de dados
            - desarma Telegram/Supabase/e-mail (mesmas listas do
              preparar_ambiente_teste.py)
            - recarrega o site sozinho (toca no arquivo WSGI)

SEGREDO: o mesmo token nas duas contas, no arquivo ~/token_sincronia.txt
(fora da pasta do projeto, então nunca entra no git). Criado pelo console
SEM imprimir na tela e copiado entre as contas pelo editor do Files -- a
convenção de segredos do projeto (nada de token em linha de comando).
"""
import hmac
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from datetime import datetime

try:  # pelo console (python sincronia_teste.py) o Flask nem precisa estar instalado
    from flask import Blueprint, abort, after_this_request, redirect, render_template, request, send_file
except ImportError:
    Blueprint = None

PASTA_BASE_SINC = os.path.dirname(os.path.abspath(__file__))
PASTA_HOME = os.path.dirname(PASTA_BASE_SINC)
ARQUIVO_TOKEN = os.path.join(PASTA_HOME, "token_sincronia.txt")
ARQUIVO_FLAG_TESTE = os.path.join(PASTA_BASE_SINC, "AMBIENTE_TESTE.flag")
URL_PRODUCAO = os.environ.get("CRM_URL_PRODUCAO", "https://crmrsi.pythonanywhere.com")
ARQUIVO_INFO = os.path.join(PASTA_BASE_SINC, "dados", "ultima_sincronia_teste.json")

PASTAS_DE_DADOS = ("uploads", "static/fotos_perfil", "painel_dados", "painel_cotas_dados", "dados")
# JSONs de configuração versionados no git (ver .gitignore) -- não sobrescrever
# com a cópia da produção: o código do teste pode estar à frente.
PRESERVAR = ("painel_cotas_dados/config_extracao.json", "painel_cotas_dados/mapa_cidades.json")

class _SemFlask:
    def route(self, *a, **k):
        return lambda f: f


bp_sincronia = Blueprint("sincronia_teste", __name__) if Blueprint else _SemFlask()


def eh_teste():
    return os.path.exists(ARQUIVO_FLAG_TESTE)


def ler_token():
    try:
        with open(ARQUIVO_TOKEN, encoding="utf-8") as f:
            token = f.read().strip()
    except OSError:
        return None
    return token if len(token) >= 32 else None


def versao_git(pasta=PASTA_BASE_SINC):
    """Commit atual lido direto do .git (sem chamar o git)."""
    try:
        git = os.path.join(pasta, ".git")
        with open(os.path.join(git, "HEAD"), encoding="utf-8") as f:
            head = f.read().strip()
        if not head.startswith("ref:"):
            return head[:7]
        ref = head[5:].strip()
        caminho = os.path.join(git, ref)
        if os.path.exists(caminho):
            with open(caminho, encoding="utf-8") as f:
                return f.read().strip()[:7]
        with open(os.path.join(git, "packed-refs"), encoding="utf-8") as f:
            for linha in f:
                if linha.strip().endswith(ref):
                    return linha.split()[0][:7]
    except OSError:
        pass
    return None


def _tamanho(n):
    return f"{n / 1024 / 1024:.1f} MB"


# ---------------------------------------------------------------------------
# PRODUÇÃO -- exportar
# ---------------------------------------------------------------------------

def gerar_pacote(destino_zip):
    """Grava em destino_zip os bancos (cópia consistente) + pastas de dados.
    Devolve um resumo para log."""
    bancos = sorted(n for n in os.listdir(PASTA_BASE_SINC) if n.endswith(".db"))
    for pasta in PASTAS_DE_DADOS:  # ex.: painel_cotas_dados/historico_resumo.db
        for atual, _, nomes in os.walk(os.path.join(PASTA_BASE_SINC, pasta)):
            bancos += sorted(os.path.relpath(os.path.join(atual, n), PASTA_BASE_SINC)
                             for n in nomes if n.endswith(".db"))
    preparo = tempfile.mkdtemp(prefix="sinc_export_")
    try:
        with zipfile.ZipFile(destino_zip, "w", zipfile.ZIP_DEFLATED) as z:
            for nome in bancos:
                copia = os.path.join(preparo, nome.replace("/", "__"))
                origem = sqlite3.connect(os.path.join(PASTA_BASE_SINC, nome), timeout=30)
                alvo = sqlite3.connect(copia)
                origem.backup(alvo)
                alvo.execute("PRAGMA journal_mode=DELETE")  # um arquivo só, sem -wal
                alvo.close()
                origem.close()
                z.write(copia, nome)
                os.remove(copia)
            arquivos = 0
            for pasta in PASTAS_DE_DADOS:
                raiz = os.path.join(PASTA_BASE_SINC, pasta)
                if not os.path.isdir(raiz):
                    continue
                for atual, subpastas, nomes in os.walk(raiz):
                    subpastas[:] = [s for s in subpastas if s != "__pycache__"]
                    for nome in nomes:
                        if nome.endswith((".db", ".db-wal", ".db-shm", ".db-journal")):
                            continue  # bancos já foram pela API de backup
                        completo = os.path.join(atual, nome)
                        z.write(completo, os.path.relpath(completo, PASTA_BASE_SINC))
                        arquivos += 1
            z.writestr("sincronia.json", json.dumps({
                "gerado_em": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "versao_producao": versao_git(),
                "bancos": bancos,
            }))
    finally:
        shutil.rmtree(preparo, ignore_errors=True)
    return f"{len(bancos)} banco(s), {arquivos} arquivo(s) de dados, {_tamanho(os.path.getsize(destino_zip))}"


@bp_sincronia.route("/interno/sincronia/exportar", methods=["POST"])
def exportar():
    if eh_teste():
        abort(404)
    token = ler_token()
    enviado = request.headers.get("X-Token-Sincronia", "")
    if not token or not hmac.compare_digest(enviado.encode(), token.encode()):
        abort(403)

    fd, caminho = tempfile.mkstemp(prefix="sinc_", suffix=".zip")
    os.close(fd)
    try:
        resumo = gerar_pacote(caminho)
    except Exception as erro:
        os.remove(caminho)
        _registrar_monitor(False, f"{type(erro).__name__}: {erro}")
        raise
    _registrar_monitor(True, resumo)

    with open(caminho, "rb") as f:  # em memória: o arquivo some antes do envio
        conteudo = io.BytesIO(f.read())
    os.remove(caminho)
    resposta = send_file(conteudo, mimetype="application/zip", as_attachment=True,
                         download_name=f"sincronia_{datetime.now():%Y%m%d_%H%M}.zip")
    resposta.headers["X-Versao-Producao"] = versao_git() or ""
    return resposta


def _registrar_monitor(ok, detalhe):
    try:
        from saude_sistema import registrar_evento_rotina
        registrar_evento_rotina("exportacao_teste", ok=ok, detalhe=detalhe)
    except Exception as erro:
        print(f"[sincronia] não consegui registrar no Monitor: {erro}", file=sys.stderr)


# ---------------------------------------------------------------------------
# BROMA -- importar
# ---------------------------------------------------------------------------

class ErroSincronia(Exception):
    pass


def baixar_da_producao(destino_zip, token):
    req = urllib.request.Request(URL_PRODUCAO.rstrip("/") + "/interno/sincronia/exportar",
                                 data=b"", method="POST",
                                 headers={"X-Token-Sincronia": token, "User-Agent": "crm-rsi-sincronia"})
    try:
        with urllib.request.urlopen(req, timeout=240) as resp, open(destino_zip, "wb") as f:
            shutil.copyfileobj(resp, f, 1024 * 1024)
            return resp.headers.get("X-Versao-Producao") or None
    except urllib.error.HTTPError as erro:
        motivos = {403: "a produção recusou o token (confira se ~/token_sincronia.txt é igual nas duas contas)",
                   404: "a produção ainda não tem a sincronia instalada (falta o git pull + Reload lá)"}
        raise ErroSincronia(motivos.get(erro.code, f"a produção respondeu HTTP {erro.code}"))
    except (urllib.error.URLError, TimeoutError, OSError) as erro:
        raise ErroSincronia(f"não consegui falar com a produção: {erro}")


def _eh_dado(nome):
    if nome == "sincronia.json" or ("/" not in nome and nome.endswith(".db")):
        return True
    return any(nome.startswith(p + "/") for p in PASTAS_DE_DADOS)


def aplicar_pacote(caminho_zip):
    """Troca os dados da broma pelos do zip. Valida tudo antes de apagar algo."""
    real_base = os.path.realpath(PASTA_BASE_SINC)
    try:
        z = zipfile.ZipFile(caminho_zip)
    except zipfile.BadZipFile:
        raise ErroSincronia("o arquivo que veio da produção não é um zip válido")
    with z:
        nomes = [n for n in z.namelist() if not n.endswith("/")]
        if "crm.db" not in nomes:
            raise ErroSincronia("o pacote veio sem crm.db")
        for nome in nomes:
            if not _eh_dado(nome):
                raise ErroSincronia(f"arquivo inesperado no pacote: {nome}")
            alvo = os.path.realpath(os.path.join(PASTA_BASE_SINC, nome))
            if not alvo.startswith(real_base + os.sep):
                raise ErroSincronia(f"caminho suspeito no pacote: {nome}")
        info = json.loads(z.read("sincronia.json")) if "sincronia.json" in nomes else {}
        bancos = [n for n in nomes if n.endswith(".db")]

        # 1) bancos numa pasta de preparo, dentro do projeto (mesmo disco ->
        #    os.replace é uma troca instantânea) e conferidos antes da troca
        preparo = os.path.join(PASTA_BASE_SINC, ".sincronia_preparo")
        shutil.rmtree(preparo, ignore_errors=True)
        os.makedirs(preparo)
        try:
            for nome in bancos:
                z.extract(nome, preparo)
                try:
                    conn = sqlite3.connect(os.path.join(preparo, nome))
                    resultado = conn.execute("PRAGMA integrity_check").fetchone()[0]
                    conn.close()
                except sqlite3.DatabaseError as erro:
                    resultado = str(erro)
                if resultado != "ok":
                    raise ErroSincronia(f"{nome} veio corrompido ({resultado}); nada foi alterado")
            # desarma ANTES da troca: o crm.db com token do Telegram nunca
            # chega a ficar no lugar, nem se algo falhar daqui pra frente
            campos = desarmar(os.path.join(preparo, "crm.db"))

            # 2) pastas de dados (os JSONs versionados ficam como estão)
            guardados = {}
            for rel in PRESERVAR:
                caminho = os.path.join(PASTA_BASE_SINC, rel)
                if os.path.exists(caminho):
                    with open(caminho, "rb") as f:
                        guardados[rel] = f.read()
            for pasta in PASTAS_DE_DADOS:
                shutil.rmtree(os.path.join(PASTA_BASE_SINC, pasta), ignore_errors=True)
            for nome in nomes:
                if not nome.endswith(".db") and nome != "sincronia.json":
                    z.extract(nome, PASTA_BASE_SINC)
            for rel, conteudo in guardados.items():
                os.makedirs(os.path.dirname(os.path.join(PASTA_BASE_SINC, rel)), exist_ok=True)
                with open(os.path.join(PASTA_BASE_SINC, rel), "wb") as f:
                    f.write(conteudo)

            # 3) troca dos bancos (depois das pastas: alguns moram dentro delas)
            for nome in bancos:
                destino = os.path.join(PASTA_BASE_SINC, nome)
                os.makedirs(os.path.dirname(destino), exist_ok=True)
                for sufixo in ("-wal", "-shm", "-journal"):
                    if os.path.exists(destino + sufixo):
                        os.remove(destino + sufixo)
                os.replace(os.path.join(preparo, nome), destino)
        finally:
            shutil.rmtree(preparo, ignore_errors=True)
    return info, bancos, len(nomes) - len(bancos) - (1 if info else 0), campos


def desarmar(caminho_db):
    if PASTA_BASE_SINC not in sys.path:
        sys.path.insert(0, PASTA_BASE_SINC)
    from preparar_ambiente_teste import LIMPAR, DESLIGAR
    conn = sqlite3.connect(caminho_db)
    colunas = {linha[1] for linha in conn.execute("PRAGMA table_info(configuracoes)")}
    sets = [f"{c} = NULL" for c in LIMPAR if c in colunas] + [f"{c} = 0" for c in DESLIGAR if c in colunas]
    if sets:
        conn.execute(f"UPDATE configuracoes SET {', '.join(sets)}")
        conn.commit()
    conn.close()
    return len(sets)


def recarregar_site():
    """No PythonAnywhere, mexer na data do arquivo WSGI recarrega o site."""
    import glob
    usuario = os.path.basename(PASTA_HOME)
    for wsgi in glob.glob(f"/var/www/{usuario}_pythonanywhere_com_wsgi.py") or glob.glob("/var/www/*_wsgi.py"):
        try:
            os.utime(wsgi, None)
            return True
        except OSError:
            pass
    return False


def sincronizar(quem="console"):
    """Faz tudo. Devolve dict com o resumo; levanta ErroSincronia em falha prevista."""
    if not eh_teste():
        raise ErroSincronia("isto só roda no ambiente de teste (falta o AMBIENTE_TESTE.flag)")
    if os.path.realpath(PASTA_BASE_SINC) == "/home/crmrsi/CRM_RSI":
        raise ErroSincronia("esta é a pasta da PRODUÇÃO")
    token = ler_token()
    if not token:
        raise ErroSincronia(f"falta o token em {ARQUIVO_TOKEN} (mesmo conteúdo da produção, 32+ caracteres)")

    inicio = datetime.now()
    fd, caminho = tempfile.mkstemp(prefix="sinc_", suffix=".zip", dir=PASTA_BASE_SINC)
    os.close(fd)
    try:
        versao_prod = baixar_da_producao(caminho, token)
        tamanho = os.path.getsize(caminho)
        info, bancos, arquivos, campos = aplicar_pacote(caminho)
    finally:
        if os.path.exists(caminho):
            os.remove(caminho)

    resumo = {
        "quando": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "quem": quem,
        "dados_da_producao_em": info.get("gerado_em"),
        "versao_producao": versao_prod or info.get("versao_producao"),
        "versao_teste": versao_git(),
        "bancos": bancos,
        "arquivos_de_dados": arquivos,
        "tamanho": _tamanho(tamanho),
        "segundos": round((datetime.now() - inicio).total_seconds()),
        "integracoes_desarmadas": campos,
    }
    os.makedirs(os.path.dirname(ARQUIVO_INFO), exist_ok=True)
    with open(ARQUIVO_INFO, "w", encoding="utf-8") as f:
        json.dump(resumo, f, ensure_ascii=False, indent=1)
    return resumo


def ultima_sincronia():
    try:
        with open(ARQUIVO_INFO, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


@bp_sincronia.route("/admin/sincronia", methods=["GET", "POST"])
def pagina_sincronia():
    from app import login_required, perfil_required, usuario_atual

    @login_required
    @perfil_required("ADM")
    def _pagina():
        if not eh_teste():
            abort(404)
        erro = None
        if request.method == "POST":
            try:
                usuario = usuario_atual()
                sincronizar(quem=usuario["nome"] if usuario else "?")
            except ErroSincronia as e:
                erro = str(e)
            except Exception as e:
                erro = f"{type(e).__name__}: {e}"
            if not erro:
                recarregou = []

                @after_this_request
                def _recarregar(resposta):
                    # só depois de responder, senão o reload corta a própria resposta
                    resposta.call_on_close(lambda: recarregou.append(recarregar_site()))
                    return resposta
                return redirect("/admin/sincronia?ok=1")
        return render_template("SincroniaTeste.html", ultima=ultima_sincronia(), erro=erro,
                               ok=request.args.get("ok"), tem_token=bool(ler_token()),
                               versao_teste=versao_git(), url_producao=URL_PRODUCAO)
    return _pagina()


if __name__ == "__main__":
    # No Bash da broma:  cd ~/CRM_RSI && python sincronia_teste.py
    try:
        r = sincronizar(quem="console")
    except ErroSincronia as e:
        sys.exit(f"ERRO: {e}")
    print(f"Pronto em {r['segundos']}s: {', '.join(r['bancos'])} + {r['arquivos_de_dados']} arquivos ({r['tamanho']}).")
    print(f"Dados da produção de {r['dados_da_producao_em']} | código produção {r['versao_producao']} x teste {r['versao_teste']}")
    print("Site recarregado." if recarregar_site() else "Agora: aba Web -> Reload.")
