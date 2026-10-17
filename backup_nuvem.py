"""
Backup diário dos bancos na nuvem (Supabase Storage) -- pedido do Bruno,
2026-10-17, substituindo o backup no Google Drive, que parou porque o token
do Google expirou (app OAuth em modo "Testing" = token de 7 dias).

POR QUE SUPABASE
  * já é o mesmo projeto usado pelo relatório do Telegram e pelo pulso do
    Monitor -- nenhuma conta nova;
  * a chave não expira (diferente do token OAuth do Google);
  * plano grátis: 1 GB e arquivos de até 50 MB. O zip dos bancos dá ~14 MB.

O QUE GUARDA
  Bucket privado "crm-backups" (sem nenhuma política de acesso: só a chave
  secreta lê ou grava).
    diario/backup_db_AAAAMMDD_HHMM.zip   -> os 14 mais recentes
    mensal/backup_db_AAAAMM.zip          -> o primeiro de cada mês, 6 meses
  Conteúdo: todos os .db (raiz e pastas de dados, ex.
  painel_cotas_dados/historico_resumo.db) copiados pela API de backup do
  SQLite (cópia consistente com gente usando), conferidos com
  integrity_check antes de subir, + backup.json com data e versão.
  Código não entra: código está no git. Arquivos enviados (uploads/) também
  não: são grandes e reconstruíveis; se um dia precisar, entra aqui.

ONDE FICA A CHAVE
  ~/supabase_backup_key.txt (fora da pasta do projeto, nunca vai pro git),
  colada pelo editor do Files. É a "secret key" (sb_secret_...) ou a antiga
  "service_role" do projeto. A URL do projeto vem da configuração que o CRM
  já tem (configuracoes.supabase_project_url).

COMO RODA
  * Scheduled Task diária:  tarefa_backup_nuvem.py  (registra no Monitor;
    se falhar ou não rodar, o Monitor avisa no Telegram uma vez só).
  * Tela Administração -> Backups na Nuvem: lista, baixa, "fazer agora".
  * Console (sem depender do site):
        python3 backup_nuvem.py                 faz um backup agora
        python3 backup_nuvem.py --listar        lista o que tem na nuvem
        python3 backup_nuvem.py --restaurar N   restaura o item N da lista
  * No ambiente de TESTE (broma) fica desligado: senão os dados da broma
    entrariam na rotação e empurrariam backups reais da produção para fora.
"""
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime

PASTA_BASE_BKP = os.path.dirname(os.path.abspath(__file__))
PASTA_HOME_BKP = os.path.dirname(PASTA_BASE_BKP)
ARQUIVO_CHAVE = os.path.join(PASTA_HOME_BKP, "supabase_backup_key.txt")
BUCKET = "crm-backups"
MANTER_DIARIOS = 14
MANTER_MENSAIS = 6
LIMITE_BYTES = 50 * 1024 * 1024
PASTAS_COM_BANCO = ("painel_cotas_dados", "dados", "painel_dados")


class ErroBackup(Exception):
    pass


def eh_teste():
    return os.path.exists(os.path.join(PASTA_BASE_BKP, "AMBIENTE_TESTE.flag"))


def ler_chave():
    try:
        with open(ARQUIVO_CHAVE, encoding="utf-8") as f:
            chave = f.read().strip()
    except OSError:
        return None
    return chave or None


def url_projeto():
    url = os.environ.get("CRM_SUPABASE_URL_BACKUP")
    if url:
        return url.rstrip("/")
    try:
        conn = sqlite3.connect(os.path.join(PASTA_BASE_BKP, "crm.db"), timeout=10)
        linha = conn.execute("SELECT supabase_project_url FROM configuracoes WHERE id=1").fetchone()
        conn.close()
    except sqlite3.Error:
        return None
    return (linha[0] or "").strip().rstrip("/") if linha and linha[0] else None


def configuracao():
    """(url, chave, problema) -- problema é None se está tudo certo."""
    if eh_teste():
        return None, None, "desligado no ambiente de teste (para não misturar dados da broma com os backups da produção)"
    url, chave = url_projeto(), ler_chave()
    if not url:
        return None, None, "URL do Supabase não configurada no CRM (configurações do Telegram/Supabase)"
    if not chave:
        return None, None, f"falta a chave secreta em {ARQUIVO_CHAVE}"
    return url, chave, None


# ---------------------------------------------------------------------------
# Supabase Storage (REST)
# ---------------------------------------------------------------------------

def _req(url, chave, metodo, caminho, corpo=None, tipo=None, extra=None, timeout=120):
    headers = {"apikey": chave, "Authorization": f"Bearer {chave}", "User-Agent": "crm-rsi-backup"}
    if tipo:
        headers["Content-Type"] = tipo
    headers.update(extra or {})
    if isinstance(corpo, (dict, list)):
        corpo = json.dumps(corpo).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{url}/storage/v1/{caminho}", data=corpo, method=metodo, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as erro:
        detalhe = erro.read()[:300].decode("utf-8", "replace")
        if erro.code in (401, 403):
            raise ErroBackup(f"o Supabase recusou a chave (HTTP {erro.code}) -- confira o {os.path.basename(ARQUIVO_CHAVE)}: {detalhe}")
        raise ErroBackup(f"Supabase respondeu HTTP {erro.code}: {detalhe}")
    except (urllib.error.URLError, TimeoutError, OSError) as erro:
        raise ErroBackup(f"não consegui falar com o Supabase: {erro}")


def enviar(url, chave, caminho, dados):
    _req(url, chave, "POST", f"object/{BUCKET}/{urllib.parse.quote(caminho)}", dados,
         tipo="application/zip", extra={"x-upsert": "true"}, timeout=300)


def listar_pasta(url, chave, pasta):
    resp = _req(url, chave, "POST", f"object/list/{BUCKET}",
                {"prefix": pasta + "/", "limit": 1000, "offset": 0,
                 "sortBy": {"column": "name", "order": "desc"}})
    itens = []
    for obj in json.loads(resp or b"[]"):
        if not obj.get("name") or obj.get("id") is None:  # subpasta / placeholder
            continue
        itens.append({"caminho": f"{pasta}/{obj['name']}", "nome": obj["name"], "pasta": pasta,
                      "tamanho": int((obj.get("metadata") or {}).get("size") or 0),
                      "criado_em": obj.get("created_at")})
    return sorted(itens, key=lambda i: i["nome"], reverse=True)


def apagar(url, chave, caminhos):
    if caminhos:
        _req(url, chave, "DELETE", f"object/{BUCKET}", {"prefixes": list(caminhos)})


def baixar(url, chave, caminho):
    return _req(url, chave, "GET", f"object/{BUCKET}/{urllib.parse.quote(caminho)}", timeout=300)


def listar_tudo(url, chave):
    return listar_pasta(url, chave, "diario") + listar_pasta(url, chave, "mensal")


# ---------------------------------------------------------------------------
# Montagem do pacote
# ---------------------------------------------------------------------------

def bancos_do_sistema():
    bancos = sorted(n for n in os.listdir(PASTA_BASE_BKP) if n.endswith(".db"))
    for pasta in PASTAS_COM_BANCO:
        raiz = os.path.join(PASTA_BASE_BKP, pasta)
        for atual, _, nomes in os.walk(raiz):
            bancos += sorted(os.path.relpath(os.path.join(atual, n), PASTA_BASE_BKP)
                             for n in nomes if n.endswith(".db"))
    return bancos


def _versao_git():
    try:
        from sincronia_teste import versao_git
        return versao_git(PASTA_BASE_BKP)
    except Exception:
        return None


def montar_pacote():
    """Devolve (bytes do zip, lista de bancos)."""
    bancos = bancos_do_sistema()
    if "crm.db" not in bancos:
        raise ErroBackup("crm.db não encontrado")
    preparo = tempfile.mkdtemp(prefix="bkp_nuvem_")
    buffer = io.BytesIO()
    try:
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            for nome in bancos:
                copia = os.path.join(preparo, nome.replace("/", "__"))
                origem = sqlite3.connect(os.path.join(PASTA_BASE_BKP, nome), timeout=30)
                alvo = sqlite3.connect(copia)
                origem.backup(alvo)
                alvo.execute("PRAGMA journal_mode=DELETE")
                resultado = alvo.execute("PRAGMA integrity_check").fetchone()[0]
                alvo.close()
                origem.close()
                if resultado != "ok":
                    raise ErroBackup(f"{nome} falhou no integrity_check: {resultado}")
                z.write(copia, nome)
                os.remove(copia)
            z.writestr("backup.json", json.dumps({
                "gerado_em": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "versao_codigo": _versao_git(), "bancos": bancos,
            }))
    finally:
        shutil.rmtree(preparo, ignore_errors=True)
    return buffer.getvalue(), bancos


def fazer_backup(agora=None):
    """Monta, envia e aplica a rotação. Devolve um resumo em texto."""
    url, chave, problema = configuracao()
    if problema:
        raise ErroBackup(problema)
    agora = agora or datetime.now()
    dados, bancos = montar_pacote()
    if len(dados) > LIMITE_BYTES:
        raise ErroBackup(f"o pacote ficou com {len(dados) / 1048576:.1f} MB, acima do limite de 50 MB do plano grátis")

    diario = f"diario/backup_db_{agora:%Y%m%d_%H%M}.zip"
    enviar(url, chave, diario, dados)

    mensais = listar_pasta(url, chave, "mensal")
    nome_mes = f"backup_db_{agora:%Y%m}.zip"
    mensal_novo = not any(m["nome"] == nome_mes for m in mensais)
    if mensal_novo:
        enviar(url, chave, f"mensal/{nome_mes}", dados)
        mensais = listar_pasta(url, chave, "mensal")

    diarios = listar_pasta(url, chave, "diario")
    apagar(url, chave, [d["caminho"] for d in diarios[MANTER_DIARIOS:]] +
                       [m["caminho"] for m in mensais[MANTER_MENSAIS:]])
    total = sum(i["tamanho"] for i in diarios[:MANTER_DIARIOS] + mensais[:MANTER_MENSAIS])
    return (f"{diario.split('/')[-1]} ({len(dados) / 1048576:.1f} MB, {len(bancos)} bancos)"
            f"{' + cópia mensal' if mensal_novo else ''} · na nuvem: {total / 1048576:.0f} MB")


# ---------------------------------------------------------------------------
# Restauração (só pelo console, de propósito)
# ---------------------------------------------------------------------------

def restaurar(dados_zip):
    """Troca os bancos atuais pelos do zip. Guarda os atuais em backups/ antes."""
    with zipfile.ZipFile(io.BytesIO(dados_zip)) as z:
        nomes = [n for n in z.namelist() if n.endswith(".db")]
        if "crm.db" not in nomes:
            raise ErroBackup("o zip não tem crm.db")
        real = os.path.realpath(PASTA_BASE_BKP)
        for n in nomes:
            if not os.path.realpath(os.path.join(PASTA_BASE_BKP, n)).startswith(real + os.sep):
                raise ErroBackup(f"caminho suspeito no zip: {n}")
        preparo = os.path.join(PASTA_BASE_BKP, ".restauro_preparo")
        shutil.rmtree(preparo, ignore_errors=True)
        os.makedirs(preparo)
        try:
            for n in nomes:
                z.extract(n, preparo)
                conn = sqlite3.connect(os.path.join(preparo, n))
                resultado = conn.execute("PRAGMA integrity_check").fetchone()[0]
                conn.close()
                if resultado != "ok":
                    raise ErroBackup(f"{n} do backup está corrompido; nada foi alterado")

            pasta_bkp = os.path.join(PASTA_BASE_BKP, "backups")
            os.makedirs(pasta_bkp, exist_ok=True)
            seguranca = os.path.join(pasta_bkp, f"antes_do_restore_{datetime.now():%Y%m%d_%H%M%S}.zip")
            with zipfile.ZipFile(seguranca, "w", zipfile.ZIP_DEFLATED) as zs:
                for n in bancos_do_sistema():
                    copia = os.path.join(preparo, "_atual_" + n.replace("/", "__"))
                    o = sqlite3.connect(os.path.join(PASTA_BASE_BKP, n), timeout=30)
                    a = sqlite3.connect(copia)
                    o.backup(a)
                    a.close()
                    o.close()
                    zs.write(copia, n)
                    os.remove(copia)

            for n in nomes:
                destino = os.path.join(PASTA_BASE_BKP, n)
                os.makedirs(os.path.dirname(destino), exist_ok=True)
                for sufixo in ("-wal", "-shm", "-journal"):
                    if os.path.exists(destino + sufixo):
                        os.remove(destino + sufixo)
                os.replace(os.path.join(preparo, n), destino)
        finally:
            shutil.rmtree(preparo, ignore_errors=True)
    return nomes, seguranca


def _main():
    os.environ["TZ"] = "America/Sao_Paulo"
    try:
        import time
        time.tzset()
    except AttributeError:
        pass
    args = sys.argv[1:]
    url, chave, problema = configuracao()
    if problema:
        sys.exit(f"ERRO: {problema}")
    try:
        if not args:
            print("Backup enviado:", fazer_backup())
            return
        itens = listar_tudo(url, chave)
        if args[0] == "--listar":
            for i, item in enumerate(itens, 1):
                print(f"  {i:2}) {item['caminho']:42} {item['tamanho'] / 1048576:6.1f} MB")
            print("\nPara restaurar: python3 backup_nuvem.py --restaurar <número>")
            return
        if args[0] == "--restaurar" and len(args) > 1:
            item = itens[int(args[1]) - 1]
            print(f"Vai SUBSTITUIR os bancos atuais pelos de {item['caminho']}.")
            print("Os atuais ficam guardados em backups/antes_do_restore_*.zip.")
            if input("Digite SIM para continuar: ").strip() != "SIM":
                print("Cancelado.")
                return
            nomes, seguranca = restaurar(baixar(url, chave, item["caminho"]))
            print(f"Restaurados: {', '.join(nomes)}")
            print(f"Estado anterior guardado em {seguranca}")
            print("Agora: aba Web -> Reload.")
            return
        print(__doc__)
    except (ErroBackup, IndexError, ValueError) as erro:
        sys.exit(f"ERRO: {erro}")


# ---------------------------------------------------------------------------
# Tela (Administração -> Backups na Nuvem)
# ---------------------------------------------------------------------------
try:  # pelo console / Scheduled Task o Flask não é necessário
    from flask import Blueprint
except ImportError:
    Blueprint = None

if Blueprint:
    import re
    from flask import abort, redirect, render_template, request, send_file

    bp_backup_nuvem = Blueprint("backup_nuvem", __name__)
    _CAMINHO_VALIDO = re.compile(r"^(diario|mensal)/backup_db_[0-9_]+\.zip$")

    @bp_backup_nuvem.route("/admin/backups")
    def tela_backups():
        from app import login_required, perfil_required

        @login_required
        @perfil_required("ADM")
        def _tela():
            url, chave, problema = configuracao()
            itens, erro_lista = [], None
            if not problema:
                try:
                    itens = listar_tudo(url, chave)
                except ErroBackup as e:
                    erro_lista = str(e)
                for i in itens:  # created_at vem em UTC; mostra no horário do CRM
                    try:
                        i["criado_local"] = datetime.fromisoformat(i["criado_em"].replace("Z", "+00:00")) \
                            .astimezone().strftime("%d/%m %H:%M")
                    except (AttributeError, ValueError):
                        i["criado_local"] = ""
            execucoes = []
            try:
                conn = sqlite3.connect(os.path.join(PASTA_BASE_BKP, "crm.db"), timeout=10)
                conn.row_factory = sqlite3.Row
                execucoes = conn.execute("SELECT inicio, fim, ok, detalhe FROM saude_rotinas "
                                         "WHERE rotina='backup_nuvem' ORDER BY id DESC LIMIT 10").fetchall()
                conn.close()
            except sqlite3.Error:
                pass
            return render_template("BackupsNuvem.html", problema=problema, itens=itens, erro_lista=erro_lista,
                                   execucoes=execucoes, total=sum(i["tamanho"] for i in itens),
                                   ok=request.args.get("ok"), erro=request.args.get("erro"),
                                   manter_diarios=MANTER_DIARIOS, manter_mensais=MANTER_MENSAIS)
        return _tela()

    @bp_backup_nuvem.route("/admin/backups/agora", methods=["POST"])
    def backup_agora():
        from app import login_required, perfil_required

        @login_required
        @perfil_required("ADM")
        def _agora():
            from saude_sistema import registrar_rotina
            try:
                with registrar_rotina("backup_nuvem"):
                    resumo = fazer_backup()
            except Exception as e:
                return redirect("/admin/backups?erro=" + urllib.parse.quote(str(e)[:300]))
            return redirect("/admin/backups?ok=" + urllib.parse.quote("Backup enviado: " + resumo))
        return _agora()

    @bp_backup_nuvem.route("/admin/backups/baixar")
    def backup_baixar():
        from app import login_required, perfil_required

        @login_required
        @perfil_required("ADM")
        def _baixar():
            caminho = request.args.get("caminho", "")
            if not _CAMINHO_VALIDO.match(caminho):
                abort(400)
            url, chave, problema = configuracao()
            if problema:
                abort(404)
            try:
                dados = baixar(url, chave, caminho)
            except ErroBackup as e:
                return redirect("/admin/backups?erro=" + urllib.parse.quote(str(e)[:300]))
            return send_file(io.BytesIO(dados), mimetype="application/zip", as_attachment=True,
                             download_name=caminho.split("/")[-1])
        return _baixar()


if __name__ == "__main__":
    _main()
