"""
AMBIENTE ESPELHO do CRM RSI -- pedido do Bruno, 2026-09-25.

Fica em /home/crmrsi/espelho_dispatcher.py (FORA das pastas CRM_RSI e
CRM_RSI_ESPELHO, pra não ser copiado por backup/promoção nem sobrescrito ao
subir versão nova) e é ligado por um bloco no fim do arquivo WSGI.

COMO FUNCIONA

  * A web app continua sendo uma só. Este módulo "embrulha" a produção: toda
    requisição passa por aqui antes.
  * Se o navegador tem o cookie "crm_espelho" com assinatura válida, a
    requisição vai pro ESPELHO (pasta CRM_RSI_ESPELHO, banco próprio). Senão,
    vai pra produção, exatamente como antes.
  * O cookie só é emitido pela rota /espelho/entrar do app.py de produção,
    que exige login de ADM. Um cookie forjado/editado é ignorado (assinatura
    HMAC com uma chave guardada em /home/crmrsi/.crm_espelho_segredo).
  * /espelho/sair é tratado AQUI (não no app), então funciona mesmo que o
    espelho esteja quebrado.
  * Roteando por cookie, e não por um caminho tipo /espelho/..., nenhuma URL
    do sistema precisa mudar -- os redirect("/...") e os href="/..." dos
    templates continuam funcionando no espelho.

ISOLAMENTO (o que impede o espelho de mexer na produção)

  Os dois sistemas têm módulos com o MESMO nome (app, backlog,
  confirmacao_agenda, painel_cotas, relatorio_clima...) e vários deles fazem
  "from app import ..." dentro das funções, na hora da requisição. Se nada
  fosse feito, o backlog do espelho chamaria o conectar() da PRODUÇÃO. Por
  isso, durante uma requisição do espelho:
    - sys.modules passa a apontar pros módulos do espelho;
    - sys.path troca a pasta da produção pela do espelho;
    - o diretório atual (cwd) vira a pasta do espelho (cobre qualquer caminho
      relativo que ainda exista no código, ex: "painel_dados/").
  Tudo é restaurado no fim da requisição (try/finally). Isso depende de cada
  worker do PythonAnywhere atender UMA requisição por vez -- que é como o
  PythonAnywhere funciona (workers de uma thread só).

  Além disso: cookie de sessão com nome e chave próprios, e as funções que
  falam com Supabase/Telegram/pasta de e-mail são trocadas por versões que
  só devolvem "bloqueado".

RELOAD

  O espelho só é carregado na PRIMEIRA requisição de espelho em cada worker
  (não na subida da produção). Se o app.py novo do espelho tiver erro, a
  produção sobe normalmente e o espelho mostra uma página com o erro.
"""
import hashlib
import hmac
import html
import importlib
import os
import re
import secrets
import sys
import threading
import time
import traceback
from contextlib import contextmanager

NOME_COOKIE = "crm_espelho"
NOME_COOKIE_SESSAO_ESPELHO = "crm_espelho_sessao"
DURACAO_COOKIE_SEG = 12 * 3600  # entra de manhã, vale o dia de trabalho

# Funções do app.py que falam com o mundo externo -- no espelho viram no-op.
# (O app.py novo já faz isso sozinho quando acha o ESPELHO.flag; aqui é a
# segunda trava, que vale até pra versões antigas do app.py.)
FUNCOES_EXTERNAS_BLOQUEADAS = (
    "chamar_funcao_supabase",
    "enviar_mensagem_telegram",
    "escrever_arquivo_relatorio",
)


def _bloqueada(nome):
    def funcao(*args, **kwargs):
        print(f"[ESPELHO] {nome} bloqueada (sem comunicação externa)", file=sys.stderr)
        return False, "Bloqueado no ambiente espelho (sem comunicação externa)."
    funcao.__name__ = nome
    return funcao


def _carregar_segredo(caminho):
    """Chave estável entre workers e reloads: criada uma vez, depois só lida."""
    for _ in range(50):
        try:
            fd = os.open(caminho, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            with open(caminho, "r", encoding="ascii") as arquivo:
                valor = arquivo.read().strip()
            if len(valor) >= 64:
                return valor.encode("ascii")
            time.sleep(0.05)  # outro worker está escrevendo agora
            continue
        valor = secrets.token_hex(32)
        with os.fdopen(fd, "w", encoding="ascii") as arquivo:
            arquivo.write(valor)
        return valor.encode("ascii")
    raise RuntimeError(f"Não consegui ler a chave do espelho em {caminho}")


def _mesma_pasta(a, b):
    try:
        return os.path.realpath(a) == os.path.realpath(b)
    except Exception:
        return False


class DispatcherEspelho:
    def __init__(self, app_producao, pasta_producao, pasta_espelho, arquivo_segredo):
        self.app_producao = app_producao
        self.pasta_producao = os.path.realpath(pasta_producao)
        self.pasta_espelho = os.path.realpath(pasta_espelho)
        if self.pasta_producao == self.pasta_espelho:
            raise ValueError("A pasta do espelho não pode ser a mesma da produção.")
        self._segredo = _carregar_segredo(arquivo_segredo)
        self._trava = threading.RLock()
        self._app_espelho = None
        self._modulos_espelho = {}
        self._erro_carga = None

    # ------------------------------------------------------------------
    # Cookie assinado
    # ------------------------------------------------------------------
    def _assinar(self, texto):
        return hmac.new(self._segredo, texto.encode("utf-8"), hashlib.sha256).hexdigest()

    def _gerar_token(self):
        expira = int(time.time()) + DURACAO_COOKIE_SEG
        return f"{expira}.{self._assinar(f'espelho|{expira}')}"

    def _token_valido(self, token):
        try:
            expira_txt, assinatura = token.split(".", 1)
            expira = int(expira_txt)
        except (ValueError, AttributeError):
            return False
        if expira < time.time():
            return False
        return hmac.compare_digest(assinatura, self._assinar(f"espelho|{expira}"))

    def _cookie_valido(self, environ):
        from werkzeug.http import parse_cookie
        return self._token_valido(parse_cookie(environ).get(NOME_COOKIE, ""))

    @staticmethod
    def _eh_https(environ):
        return (environ.get("wsgi.url_scheme") == "https"
                or environ.get("HTTP_X_FORWARDED_PROTO", "").lower() == "https")

    def _emissor_de_cookie(self, environ):
        """Função entregue ao app.py (via environ) pra ligar o espelho numa resposta."""
        seguro = self._eh_https(environ)

        def emitir(resposta):
            resposta.set_cookie(
                NOME_COOKIE, self._gerar_token(), max_age=DURACAO_COOKIE_SEG,
                path="/", httponly=True, secure=seguro, samesite="Lax",
            )
        return emitir

    # ------------------------------------------------------------------
    # Entrada WSGI
    # ------------------------------------------------------------------
    def __call__(self, environ, start_response):
        caminho = environ.get("PATH_INFO", "")
        disponivel = os.path.isfile(os.path.join(self.pasta_espelho, "app.py"))

        if caminho == "/espelho/sair":
            return self._sair(environ, start_response)

        if disponivel and self._cookie_valido(environ):
            try:
                return self._servir_espelho(environ, start_response)
            except Exception:
                # Nunca deixa um problema do espelho virar erro "sem página"
                return self._pagina_erro(start_response, "Erro inesperado ao atender o espelho",
                                         traceback.format_exc())

        environ["crm_espelho.disponivel"] = disponivel
        if disponivel:
            environ["crm_espelho.emitir_cookie"] = self._emissor_de_cookie(environ)
        return self.app_producao(environ, start_response)

    def _sair(self, environ, start_response):
        cookie = f"{NOME_COOKIE}=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax"
        if self._eh_https(environ):
            cookie += "; Secure"
        start_response("302 Found", [
            ("Location", "/portal"), ("Set-Cookie", cookie),
            ("Content-Type", "text/plain; charset=utf-8"), ("Content-Length", "0"),
        ])
        return [b""]

    # ------------------------------------------------------------------
    # Isolamento de módulos / caminhos
    # ------------------------------------------------------------------
    @staticmethod
    def _retirar_modulos_da_pasta(pasta):
        """Tira de sys.modules (e devolve) todo módulo cujo arquivo está na pasta."""
        prefixo = pasta + os.sep
        retirados = {}
        for nome, modulo in list(sys.modules.items()):
            arquivo = getattr(modulo, "__file__", None)
            if arquivo and os.path.realpath(arquivo).startswith(prefixo):
                retirados[nome] = sys.modules.pop(nome)
        return retirados

    def _sys_path_do_espelho(self):
        return [self.pasta_espelho] + [
            p for p in sys.path
            if p not in ("", ".") and not _mesma_pasta(p, self.pasta_producao)
            and not _mesma_pasta(p, self.pasta_espelho)
        ]

    @contextmanager
    def _contexto_espelho(self, modulos_espelho):
        modulos_producao = self._retirar_modulos_da_pasta(self.pasta_producao)
        path_original = list(sys.path)
        cwd_original = os.getcwd()
        try:
            sys.modules.update(modulos_espelho)
            sys.path[:] = self._sys_path_do_espelho()
            os.chdir(self.pasta_espelho)
            importlib.invalidate_caches()
            yield
        finally:
            # guarda também módulos do espelho importados pela primeira vez agora
            self._modulos_espelho = self._retirar_modulos_da_pasta(self.pasta_espelho)
            sys.path[:] = path_original
            os.chdir(cwd_original)
            sys.modules.update(modulos_producao)
            importlib.invalidate_caches()

    # ------------------------------------------------------------------
    # Carga do espelho (preguiçosa, uma vez por worker)
    # ------------------------------------------------------------------
    def _carregar_espelho(self):
        try:
            with self._contexto_espelho({}):
                modulo = importlib.import_module("app")
                if not _mesma_pasta(os.path.dirname(modulo.__file__), self.pasta_espelho):
                    raise RuntimeError(f"O 'app' carregado veio de {modulo.__file__}, não do espelho.")
                self._preparar(modulo, modulo.app)
                app_espelho = modulo.app
        except BaseException:
            self._erro_carga = traceback.format_exc()
            self._modulos_espelho = {}
            print("[ESPELHO] falha ao carregar o espelho:\n" + self._erro_carga, file=sys.stderr)
            return
        self._app_espelho = app_espelho
        print("[ESPELHO] espelho carregado neste worker.", file=sys.stderr)

    def _preparar(self, modulo, app):
        modulo.EH_ESPELHO = True
        for nome in FUNCOES_EXTERNAS_BLOQUEADAS:
            if hasattr(modulo, nome):
                setattr(modulo, nome, _bloqueada(nome))

        # Sessão própria: nome de cookie diferente e chave diferente da produção
        app.secret_key = self._assinar("chave-sessao-flask-espelho")
        app.config["SESSION_COOKIE_NAME"] = NOME_COOKIE_SESSAO_ESPELHO

        # Versões antigas do app.py (Flask('CRM RSI') sem root_path) resolvem
        # templates/static pelo cwd -- garante que apontem pro espelho.
        if not _mesma_pasta(app.root_path, self.pasta_espelho):
            app.root_path = self.pasta_espelho
            app.__dict__.pop("jinja_loader", None)
            app.__dict__.pop("jinja_env", None)
        pasta_upload = app.config.get("UPLOAD_FOLDER")
        if pasta_upload and not os.path.isabs(pasta_upload):
            app.config["UPLOAD_FOLDER"] = os.path.join(self.pasta_espelho, pasta_upload)

    # ------------------------------------------------------------------
    # Atendimento
    # ------------------------------------------------------------------
    def _servir_espelho(self, environ, start_response):
        with self._trava:
            if self._app_espelho is None and self._erro_carga is None:
                self._carregar_espelho()
            if self._erro_carga:
                return self._pagina_erro(
                    start_response,
                    "O espelho não carregou (a produção NÃO foi afetada)",
                    self._erro_carga,
                    "Corrija os arquivos na pasta do espelho e clique em Reload na aba Web.",
                )

            capturado = {}
            escrito = []

            def start_response_local(status, headers, exc_info=None):
                capturado["status"] = status
                capturado["headers"] = list(headers)
                return escrito.append

            environ["crm_espelho.no_espelho"] = True
            with self._contexto_espelho(self._modulos_espelho):
                resposta = self._app_espelho(environ, start_response_local)
                try:
                    # consome o corpo ainda DENTRO do isolamento
                    corpo = b"".join(escrito) + b"".join(resposta)
                finally:
                    if hasattr(resposta, "close"):
                        resposta.close()

        status = capturado.get("status", "500 INTERNAL SERVER ERROR")
        headers = capturado.get("headers", [])
        corpo, headers = self._marcar_como_espelho(corpo, headers)
        start_response(status, headers)
        return [corpo]

    @staticmethod
    def _marcar_como_espelho(corpo, headers):
        tipo = next((v for k, v in headers if k.lower() == "content-type"), "")
        codificado = any(k.lower() == "content-encoding" for k, _ in headers)
        if not tipo.lower().startswith("text/html") or codificado:
            return corpo, headers
        try:
            texto = corpo.decode("utf-8")
        except UnicodeDecodeError:
            return corpo, headers
        faixa = (
            '<div id="faixa-espelho" style="position:fixed;left:0;right:0;bottom:0;z-index:2147483647;'
            'background:#e08600;color:#fff;font:700 13px/1.2 system-ui,sans-serif;padding:7px 12px;'
            'text-align:center;box-shadow:0 -2px 8px rgba(0,0,0,.25)">'
            'AMBIENTE ESPELHO &middot; dados de teste, sem Telegram/Supabase/e-mail &middot; '
            '<a href="/espelho/sair" style="color:#fff;text-decoration:underline">Voltar para a produção</a>'
            '</div>'
            '<div style="position:fixed;inset:0;border:4px solid #e08600;pointer-events:none;'
            'z-index:2147483646"></div>'
        )
        novo, n = re.subn(r"(<body\b[^>]*>)", lambda m: m.group(1) + faixa, texto, count=1, flags=re.I)
        if n == 0:
            return corpo, headers
        novo = re.sub(r"(<title\b[^>]*>)", r"\1[ESPELHO] ", novo, count=1, flags=re.I)
        corpo = novo.encode("utf-8")
        headers = [(k, v) for k, v in headers if k.lower() != "content-length"]
        headers.append(("Content-Length", str(len(corpo))))
        return corpo, headers

    @staticmethod
    def _pagina_erro(start_response, titulo, detalhe, dica=""):
        pagina = f"""<!doctype html><html lang="pt-br"><head><meta charset="utf-8">
<title>[ESPELHO] Erro</title></head>
<body style="font-family:system-ui,sans-serif;margin:24px;background:#fff8ef">
<h2 style="color:#8a4b00">{html.escape(titulo)}</h2>
<p>{html.escape(dica)}</p>
<p><a href="/espelho/sair"><b>Voltar para a produção</b></a></p>
<pre style="background:#222;color:#eee;padding:14px;overflow:auto;font-size:12px">{html.escape(detalhe)}</pre>
</body></html>""".encode("utf-8")
        start_response("500 INTERNAL SERVER ERROR", [
            ("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(pagina))),
        ])
        return [pagina]


def envolver_com_espelho(app_producao, pasta_producao, pasta_espelho, arquivo_segredo=None):
    """Chamado no fim do WSGI. Devolve o novo 'application'."""
    if arquivo_segredo is None:
        arquivo_segredo = os.path.join(os.path.dirname(os.path.realpath(pasta_producao)),
                                       ".crm_espelho_segredo")
    return DispatcherEspelho(app_producao, pasta_producao, pasta_espelho, arquivo_segredo)
