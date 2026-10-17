"""
Painel de Saúde do Sistema -- pedido do Bruno, 2026-10-17. Visão do ADM em /admin/saude.

Três visões:
  1. Rotinas agendadas: o encerramento automático, os relatórios do Telegram,
     a previsão do tempo etc. rodaram hoje?
  2. Erros 500 por hora: todo erro não tratado numa requisição é gravado aqui
     (caso real: o upload do GED quebrou só na produção, por causa do Python 3.10,
     e só apareceu quando alguém reclamou).
  3. Fila do mailing e dos followups ao longo do dia, com alerta no Telegram
     se a operação travar ou a fila disparar.

Nada roda em segundo plano no servidor (thread não sobrevive no WSGI do
PythonAnywhere). O "pulso" é o Cron do Supabase que JÁ chama
/interno/verificar_envio_diario a cada 10-15 min para o relatório diário: a
mesma chamada passa a executar pulso() -> grava a foto da fila, checa os
alertas e limpa histórico antigo. Ao abrir o painel, o ADM também dispara um
pulso (a foto do momento sempre aparece, mesmo se o cron estiver parado --
e o próprio cron parado vira alerta).

Rotinas: cada Scheduled Task registra o início/fim com registrar_rotina() (ver
os tarefa_*.py). Para ter leitura já no primeiro dia, o painel também usa os
sinais que o banco já guardava: sessões encerradas como AUTOMATICO, a data da
última atualização da previsão do tempo e a data do último envio do relatório
diário.

Alertas saem pela mesma Edge Function do relatório diário (enviar-relatorio).
No ambiente de teste a chamada ao Supabase é bloqueada pelo app.py
(AMBIENTE_TESTE.flag), então nada chega no grupo real.
"""
import json
import os
import sqlite3
import traceback
from contextlib import contextmanager
from datetime import datetime, timedelta, time

from flask import Blueprint, render_template, request, redirect, session, got_request_exception

bp_saude = Blueprint("saude_sistema", __name__)

PASTA_BASE_SAUDE = os.path.dirname(os.path.abspath(__file__))
DIAS = ["SEG", "TER", "QUA", "QUI", "SEX", "SAB", "DOM"]

# Rotinas monitoradas. "horario" = quando costuma rodar (hora de Brasília);
# "tolerancia_min" = quanto depois disso ainda não é atraso.
ROTINAS = [
    {"chave": "encerramento_automatico", "nome": "Encerramento automático de jornada",
     "como": "Scheduled Task diária (tarefa_encerramento_automatico.py)", "horario": "18:30",
     "dias": DIAS, "tolerancia_min": 45},
    {"chave": "previsao_tempo", "nome": "Atualização da previsão do tempo",
     "como": "Scheduled Task diária (tarefa_atualizar_previsao_tempo.py)", "horario": "04:00",
     "dias": DIAS, "tolerancia_min": 120},
    {"chave": "relatorio_telegram", "nome": "Relatório diário no Telegram",
     "como": "Cron do Supabase -> /interno/verificar_envio_diario", "horario": None,
     "dias": None, "tolerancia_min": 45},
    {"chave": "relatorio_clima", "nome": "Relatório de clima no Telegram",
     "como": "Scheduled Task diária (tarefa_relatorio_clima_telegram.py)", "horario": None,
     "dias": DIAS, "tolerancia_min": 60},
    {"chave": "confirmacao_nao_tratado", "nome": "Fechamento da Confirmação de Agenda (não tratados)",
     "como": "Scheduled Task diária (tarefa_confirmacao_nao_tratado.py)", "horario": "18:00",
     "dias": DIAS, "tolerancia_min": 60},
    {"chave": "backup_nuvem", "nome": "Backup diário dos bancos na nuvem",
     "como": "Scheduled Task diária (tarefa_backup_nuvem.py) -> Supabase Storage", "horario": "03:00",
     "dias": DIAS, "tolerancia_min": 90},
]

CONFIG_PADRAO = {
    "alertas_ativos": "1",
    "horario_inicio": "08:00",
    "horario_fim": "18:00",
    "min_operadores_travou": "3",       # com pelo menos N operadores logados...
    "min_parado_travou": "30",          # ...e nenhuma tratativa há N minutos -> fila travou
    "limite_fila_baixa": "200",         # contratos disponíveis abaixo disso -> mailing acabando
    "limite_followup_vencido": "300",   # followups vencidos acima disso -> disparou
    "limite_erros_hora": "5",           # erros 500 na última hora
    "cooldown_min": "120",              # não repete o mesmo alerta dentro desse intervalo
    "pulso_parado_min": "40",           # sem pulso do Supabase há mais que isso, em horário de operação
}


def _conectar(timeout=5):
    conn = sqlite3.connect(os.path.join(PASTA_BASE_SAUDE, "crm.db"), timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
    return conn


def garantir_tabelas_saude():
    conn = _conectar()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS saude_rotinas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rotina TEXT, inicio TEXT, fim TEXT, ok INTEGER, detalhe TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_saude_rotinas ON saude_rotinas(rotina, inicio);
        CREATE TABLE IF NOT EXISTS saude_erros (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            quando TEXT, metodo TEXT, rota TEXT, usuario TEXT, tipo TEXT, mensagem TEXT, trecho TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_saude_erros ON saude_erros(quando);
        CREATE TABLE IF NOT EXISTS saude_fila (
            slot TEXT PRIMARY KEY,              -- YYYY-MM-DD HH:MM, arredondado a 10 min
            registrado_em TEXT,
            pendentes INTEGER, disponiveis INTEGER, enfileirados INTEGER, em_andamento INTEGER,
            followups_vencidos INTEGER, bloqueados_ged INTEGER, gpon INTEGER,
            tratativas_ultima_hora INTEGER, operadores_online INTEGER, origem TEXT
        );
        CREATE TABLE IF NOT EXISTS saude_alertas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            quando TEXT, tipo TEXT, mensagem TEXT, enviado INTEGER, erro TEXT
        );
        CREATE TABLE IF NOT EXISTS saude_config (chave TEXT PRIMARY KEY, valor TEXT);
    """)
    for k, v in CONFIG_PADRAO.items():
        conn.execute("INSERT OR IGNORE INTO saude_config (chave, valor) VALUES (?, ?)", (k, v))
    conn.commit()
    conn.close()


garantir_tabelas_saude()


def _config(conn):
    cfg = dict(CONFIG_PADRAO)
    cfg.update({r["chave"]: r["valor"] for r in conn.execute("SELECT chave, valor FROM saude_config")})
    return cfg


def _int(cfg, chave):
    try:
        return int(float(cfg.get(chave) or CONFIG_PADRAO[chave]))
    except (TypeError, ValueError):
        return int(CONFIG_PADRAO[chave])


def _hora(txt, padrao):
    try:
        return datetime.strptime(txt, "%H:%M").time()
    except (TypeError, ValueError):
        return padrao


# ==========================
# 1) ROTINAS
# ==========================

@contextmanager
def registrar_rotina(chave):
    """Usar nas Scheduled Tasks:
           with registrar_rotina("encerramento_automatico"):
               encerrar_jornadas_esquecidas()
    Grava início e fim; se der exceção, grava ok=0 com o erro e repassa a
    exceção (o log da Task continua mostrando o erro como antes)."""
    inicio = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rid = None
    try:
        conn = _conectar()
        rid = conn.execute("INSERT INTO saude_rotinas (rotina, inicio, ok) VALUES (?, ?, NULL)", (chave, inicio)).lastrowid
        conn.commit()
        conn.close()
    except Exception:
        pass
    try:
        yield
    except Exception as erro:
        _fechar_rotina(rid, chave, inicio, False, f"{type(erro).__name__}: {erro}")
        raise
    _fechar_rotina(rid, chave, inicio, True, None)


def _fechar_rotina(rid, chave, inicio, ok, detalhe):
    try:
        conn = _conectar()
        fim = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if rid:
            conn.execute("UPDATE saude_rotinas SET fim=?, ok=?, detalhe=? WHERE id=?", (fim, 1 if ok else 0, detalhe, rid))
        else:
            conn.execute("INSERT INTO saude_rotinas (rotina, inicio, fim, ok, detalhe) VALUES (?, ?, ?, ?, ?)",
                         (chave, inicio, fim, 1 if ok else 0, detalhe))
        conn.commit()
        conn.close()
    except Exception:
        pass


def registrar_evento_rotina(chave, ok=True, detalhe=None):
    """Registro pontual (sem duração) -- usado pelo pulso do Supabase."""
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn = _conectar()
        conn.execute("INSERT INTO saude_rotinas (rotina, inicio, fim, ok, detalhe) VALUES (?, ?, ?, ?, ?)",
                     (chave, agora, agora, 1 if ok else 0, detalhe))
        conn.commit()
        conn.close()
    except Exception:
        pass


def status_rotinas(conn, agora=None):
    agora = agora or datetime.now()
    hoje = agora.strftime("%Y-%m-%d")
    dia_semana = DIAS[agora.weekday()]
    cfg_app = conn.execute("SELECT * FROM configuracoes WHERE id=1").fetchone()
    cfg_app = dict(cfg_app) if cfg_app else {}

    def ultimo_registro(chave):
        return conn.execute("SELECT * FROM saude_rotinas WHERE rotina=? ORDER BY id DESC LIMIT 1", (chave,)).fetchone()

    def registro_hoje(chave):
        return conn.execute("SELECT * FROM saude_rotinas WHERE rotina=? AND substr(inicio,1,10)=? ORDER BY id DESC LIMIT 1",
                            (chave, hoje)).fetchone()

    lista = []
    for r in ROTINAS:
        item = {**r, "estado": "?", "quando": None, "detalhe": None, "ultima": None}
        horario, dias = r["horario"], r["dias"]
        ativo = True

        if r["chave"] == "relatorio_telegram":
            ativo = bool(cfg_app.get("supabase_telegram_ativo"))
            horario = cfg_app.get("supabase_telegram_horario_envio") or "18:00"
            dias = (cfg_app.get("supabase_telegram_dias_semana") or "SEG,TER,QUA,QUI,SEX").split(",")
            item["horario"], item["dias"] = horario, dias
        if r["chave"] == "encerramento_automatico":
            ativo = cfg_app.get("encerramento_automatico_ativo") in (1, None)

        reg = registro_hoje(r["chave"])
        ult = ultimo_registro(r["chave"])
        item["ultima"] = (ult["fim"] or ult["inicio"]) if ult else None

        # Sinais que já existiam antes do painel (valem mesmo sem o registro)
        sinal = None
        if r["chave"] == "encerramento_automatico":
            s = conn.execute("SELECT MAX(logout_em) FROM sessoes WHERE encerrado_por='AUTOMATICO' AND substr(logout_em,1,10)=?",
                             (hoje,)).fetchone()[0]
            if s:
                sinal = (s, "sessões encerradas pelo sistema hoje")
            if not item["ultima"]:
                item["ultima"] = conn.execute("SELECT MAX(logout_em) FROM sessoes WHERE encerrado_por='AUTOMATICO'").fetchone()[0]
        elif r["chave"] == "previsao_tempo":
            s = conn.execute("SELECT MAX(atualizado_em) FROM previsao_tempo").fetchone()[0]
            if s and s[:10] == hoje:
                sinal = (s, "previsão gravada hoje")
            if not item["ultima"]:
                item["ultima"] = s
        elif r["chave"] == "relatorio_telegram":
            if cfg_app.get("supabase_telegram_ultimo_envio_data") == hoje:
                sinal = (hoje, "envio registrado hoje")
            if not item["ultima"]:
                item["ultima"] = cfg_app.get("supabase_telegram_ultimo_envio_data")

        hora_prevista = _hora(horario, None) if horario else None
        previsto_hoje = (dias is None) or (dia_semana in dias)
        atrasado = False
        if hora_prevista:
            limite = datetime.combine(agora.date(), hora_prevista) + timedelta(minutes=r["tolerancia_min"])
            atrasado = agora > limite

        if not ativo:
            item["estado"], item["detalhe"] = "desativado", "Desligado na configuração do CRM."
        elif reg and reg["ok"] == 0:
            item["estado"], item["quando"], item["detalhe"] = "erro", reg["inicio"], reg["detalhe"]
        elif reg and reg["ok"] == 1:
            item["estado"], item["quando"] = "ok", reg["fim"] or reg["inicio"]
        elif reg and reg["ok"] is None:
            item["estado"], item["quando"], item["detalhe"] = "rodando", reg["inicio"], "Começou e ainda não registrou o fim."
        elif sinal:
            item["estado"], item["quando"], item["detalhe"] = "ok", sinal[0], sinal[1]
        elif not previsto_hoje:
            item["estado"], item["detalhe"] = "nao_hoje", "Não roda hoje."
        elif hora_prevista and not atrasado:
            item["estado"], item["detalhe"] = "aguardando", f"Prevista para {horario}."
        elif hora_prevista and atrasado:
            item["estado"], item["detalhe"] = "atrasada", f"Prevista para {horario} e sem registro hoje."
        else:
            item["estado"], item["detalhe"] = "sem_registro", "Sem registro hoje (horário não definido no CRM)."
        lista.append(item)
    return lista


def status_pulso(conn, agora=None):
    agora = agora or datetime.now()
    ult = conn.execute("SELECT MAX(inicio) FROM saude_rotinas WHERE rotina='pulso_supabase'").fetchone()[0]
    minutos = None
    if ult:
        minutos = int((agora - datetime.strptime(ult, "%Y-%m-%d %H:%M:%S")).total_seconds() // 60)
    pulsos_hoje = conn.execute("SELECT COUNT(*) FROM saude_rotinas WHERE rotina='pulso_supabase' AND substr(inicio,1,10)=?",
                               (agora.strftime("%Y-%m-%d"),)).fetchone()[0]
    return {"ultimo": ult, "minutos": minutos, "hoje": pulsos_hoje}


# ==========================
# 2) ERROS 500
# ==========================

def _registrar_erro(sender, exception, **extra):
    """Ligado ao sinal got_request_exception do Flask: roda quando uma rota
    estoura exceção não tratada (a página continua mostrando o 500 de sempre).
    Nunca pode derrubar nada -- qualquer falha aqui é engolida."""
    try:
        tb = traceback.format_exception(type(exception), exception, exception.__traceback__)
        trecho = "".join(tb[-4:])[-1500:]
        usuario = None
        try:
            uid = session.get("usuario_id")
            if uid:
                c = _conectar(timeout=2)
                u = c.execute("SELECT nome FROM usuarios WHERE id=?", (uid,)).fetchone()
                c.close()
                usuario = u["nome"] if u else f"id {uid}"
        except Exception:
            pass
        conn = _conectar(timeout=2)
        conn.execute("""
            INSERT INTO saude_erros (quando, metodo, rota, usuario, tipo, mensagem, trecho)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), request.method, request.path, usuario,
              type(exception).__name__, str(exception)[:500], trecho))
        conn.commit()
        conn.close()
    except Exception:
        pass


@bp_saude.record_once
def _ligar_sinal_de_erro(state):
    got_request_exception.connect(_registrar_erro, state.app, weak=False)


def erros_por_hora(conn, agora=None, horas=24):
    agora = agora or datetime.now()
    inicio = (agora - timedelta(hours=horas - 1)).replace(minute=0, second=0, microsecond=0)
    contagem = {r[0]: r[1] for r in conn.execute("""
        SELECT substr(quando, 1, 13) AS h, COUNT(*) FROM saude_erros WHERE quando >= ? GROUP BY 1
    """, (inicio.strftime("%Y-%m-%d %H:%M:%S"),))}
    serie = []
    for i in range(horas):
        h = inicio + timedelta(hours=i)
        chave = h.strftime("%Y-%m-%d %H")
        serie.append({"hora": h.strftime("%d/%m %Hh"), "qtd": contagem.get(chave, 0)})
    return serie


def erros_agrupados(conn, dias=7):
    desde = (datetime.now() - timedelta(days=dias)).strftime("%Y-%m-%d %H:%M:%S")
    return conn.execute("""
        SELECT rota, tipo, COUNT(*) AS qtd, MAX(quando) AS ultimo, MIN(quando) AS primeiro,
               (SELECT mensagem FROM saude_erros e2 WHERE e2.rota = e.rota AND e2.tipo = e.tipo ORDER BY id DESC LIMIT 1) AS mensagem,
               (SELECT trecho FROM saude_erros e2 WHERE e2.rota = e.rota AND e2.tipo = e.tipo ORDER BY id DESC LIMIT 1) AS trecho,
               (SELECT usuario FROM saude_erros e2 WHERE e2.rota = e.rota AND e2.tipo = e.tipo ORDER BY id DESC LIMIT 1) AS usuario
        FROM saude_erros e WHERE quando >= ?
        GROUP BY rota, tipo ORDER BY ultimo DESC LIMIT 50
    """, (desde,)).fetchall()


# ==========================
# 3) FILA
# ==========================

def foto_da_fila(conn, agora=None):
    agora = agora or datetime.now()
    agora_str = agora.strftime("%Y-%m-%d %H:%M:%S")
    um = lambda sql, *p: conn.execute(sql, p).fetchone()[0] or 0
    status = {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM mailing GROUP BY status")}
    disponiveis = um("""
        SELECT COUNT(*) FROM mailing WHERE status='PENDENTE'
          AND cidade NOT IN (SELECT cidade FROM cidades_bloqueio_distribuicao WHERE bloqueado=1)
          AND contrato NOT IN (SELECT contrato FROM contratos_blacklist)
    """)
    vencidos = um("""
        SELECT COUNT(DISTINCT f.id) FROM followup f
        JOIN mailing m ON (f.id_mailing = m.id OR (f.id_mailing IS NULL AND m.contrato = f.contrato))
        WHERE f.status='PENDENTE' AND f.data_retorno <= ? AND m.status='ENFILEIRADO'
    """, agora_str)
    tratativas = um("SELECT COUNT(*) FROM atividades WHERE data_fim >= ?",
                    (agora - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"))
    online = um("""
        SELECT COUNT(DISTINCT s.usuario_id) FROM sessoes s JOIN usuarios u ON u.id = s.usuario_id
        WHERE date(s.login_em) = ? AND s.logout_em IS NULL AND u.perfil = 'OPERADOR'
          AND NOT EXISTS (SELECT 1 FROM pausas p WHERE p.usuario_id = s.usuario_id AND p.fim_real IS NULL
                          AND date(p.inicio) = date(s.login_em))
    """, agora.strftime("%Y-%m-%d"))
    return {
        "pendentes": status.get("PENDENTE", 0), "disponiveis": disponiveis,
        "enfileirados": status.get("ENFILEIRADO", 0), "em_andamento": status.get("EM_ANDAMENTO", 0),
        "followups_vencidos": vencidos, "bloqueados_ged": status.get("BLOQUEADO_GED", 0),
        "gpon": status.get("AGUARDANDO_ANALISE_GPON", 0) + status.get("EM_ANALISE_GPON", 0),
        "tratativas_ultima_hora": tratativas, "operadores_online": online,
    }


def gravar_foto(conn, foto, origem, agora=None):
    agora = agora or datetime.now()
    slot = agora.replace(minute=agora.minute - agora.minute % 10, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M")
    conn.execute("""
        INSERT OR IGNORE INTO saude_fila (slot, registrado_em, pendentes, disponiveis, enfileirados, em_andamento,
            followups_vencidos, bloqueados_ged, gpon, tratativas_ultima_hora, operadores_online, origem)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (slot, agora.strftime("%Y-%m-%d %H:%M:%S"), foto["pendentes"], foto["disponiveis"], foto["enfileirados"],
          foto["em_andamento"], foto["followups_vencidos"], foto["bloqueados_ged"], foto["gpon"],
          foto["tratativas_ultima_hora"], foto["operadores_online"], origem))


def tratativas_por_hora(conn, dia):
    return {int(r[0]): r[1] for r in conn.execute("""
        SELECT CAST(substr(data_fim, 12, 2) AS INTEGER), COUNT(*) FROM atividades
        WHERE substr(data_fim, 1, 10) = ? GROUP BY 1
    """, (dia,))}


# ==========================
# ALERTAS
# ==========================

def _enviar_telegram(texto):
    """Usa a mesma Edge Function do relatório diário. Devolve (ok, erro)."""
    try:
        import app as app_mod
        conn = _conectar()
        cfg = conn.execute("SELECT supabase_project_url, supabase_anon_key FROM configuracoes WHERE id=1").fetchone()
        conn.close()
        if not cfg or not cfg["supabase_project_url"] or not cfg["supabase_anon_key"]:
            return False, "Supabase não configurado (tela Relatório via Telegram)."
        return app_mod.enviar_relatorio_telegram_via_supabase(cfg["supabase_project_url"], cfg["supabase_anon_key"], texto)
    except Exception as erro:
        return False, str(erro)


def _alertar(conn, cfg, tipo, mensagem, agora):
    cooldown = _int(cfg, "cooldown_min")
    recente = conn.execute("SELECT 1 FROM saude_alertas WHERE tipo=? AND quando >= ? AND enviado=1 LIMIT 1",
                           (tipo, (agora - timedelta(minutes=cooldown)).strftime("%Y-%m-%d %H:%M:%S"))).fetchone()
    if recente:
        return None
    texto = f"⚠️ CRM RSI — Monitor\n{mensagem}\n({agora.strftime('%d/%m %H:%M')})"
    ok, erro = _enviar_telegram(texto)
    conn.execute("INSERT INTO saude_alertas (quando, tipo, mensagem, enviado, erro) VALUES (?, ?, ?, ?, ?)",
                 (agora.strftime("%Y-%m-%d %H:%M:%S"), tipo, mensagem, 1 if ok else 0, None if ok else str(erro)[:300]))
    conn.commit()
    return ok


def avaliar_alertas(conn, foto, agora=None, enviar=True):
    """Devolve a lista de alertas que valem agora: (tipo, mensagem). Se enviar=True
    e os alertas estiverem ligados, manda (respeitando o intervalo mínimo)."""
    agora = agora or datetime.now()
    cfg = _config(conn)
    alertas = []
    em_operacao = (agora.weekday() < 5 and
                   _hora(cfg["horario_inicio"], time(8)) <= agora.time() < _hora(cfg["horario_fim"], time(18)))

    if em_operacao:
        minutos = _int(cfg, "min_parado_travou")
        tratou = conn.execute("SELECT COUNT(*) FROM atividades WHERE data_fim >= ?",
                              ((agora - timedelta(minutes=minutos)).strftime("%Y-%m-%d %H:%M:%S"),)).fetchone()[0]
        if foto["operadores_online"] >= _int(cfg, "min_operadores_travou") and tratou == 0:
            alertas.append(("fila_parada", f"Fila parada: {foto['operadores_online']} operadores logados e nenhuma tratativa nos últimos {minutos} min."))
        if foto["disponiveis"] < _int(cfg, "limite_fila_baixa"):
            alertas.append(("fila_baixa", f"Mailing acabando: só {foto['disponiveis']} contratos disponíveis para distribuir."))
        if foto["followups_vencidos"] > _int(cfg, "limite_followup_vencido"):
            alertas.append(("followup_vencido", f"Followups vencidos acima do limite: {foto['followups_vencidos']} (limite {cfg['limite_followup_vencido']})."))
        else:
            antes = conn.execute("SELECT followups_vencidos FROM saude_fila WHERE slot <= ? ORDER BY slot DESC LIMIT 1",
                                 ((agora - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M"),)).fetchone()
            if antes and antes[0] and antes[0] >= 30 and foto["followups_vencidos"] >= antes[0] * 1.5:
                alertas.append(("followup_disparou", f"Followups vencidos dispararam: {antes[0]} → {foto['followups_vencidos']} em 1 hora."))

    erros_h = conn.execute("SELECT COUNT(*) FROM saude_erros WHERE quando >= ?",
                           ((agora - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),)).fetchone()[0]
    if erros_h >= _int(cfg, "limite_erros_hora"):
        rotas = [r[0] for r in conn.execute("""SELECT rota FROM saude_erros WHERE quando >= ? GROUP BY rota ORDER BY COUNT(*) DESC LIMIT 3""",
                                             ((agora - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),))]
        alertas.append(("erros_500", f"{erros_h} erros 500 na última hora ({', '.join(rotas)})."))

    for r in status_rotinas(conn, agora):
        if r["estado"] in ("atrasada", "erro"):
            alertas.append((f"rotina_{r['chave']}", f"Rotina '{r['nome']}': {('falhou — ' + (r['detalhe'] or '')) if r['estado'] == 'erro' else r['detalhe']}"))

    if enviar and cfg.get("alertas_ativos") == "1":
        for tipo, msg in alertas:
            _alertar(conn, cfg, tipo, msg, agora)
    return alertas


def _limpar_historico(conn, agora):
    conn.execute("DELETE FROM saude_fila WHERE slot < ?", ((agora - timedelta(days=60)).strftime("%Y-%m-%d"),))
    conn.execute("DELETE FROM saude_erros WHERE quando < ?", ((agora - timedelta(days=90)).strftime("%Y-%m-%d"),))
    conn.execute("DELETE FROM saude_rotinas WHERE inicio < ?", ((agora - timedelta(days=180)).strftime("%Y-%m-%d"),))
    conn.execute("DELETE FROM saude_alertas WHERE quando < ?", ((agora - timedelta(days=180)).strftime("%Y-%m-%d"),))


def pulso(origem="supabase", agora=None, enviar=True):
    """Chamado pelo Cron do Supabase (via /interno/verificar_envio_diario) e
    ao abrir o painel. Grava a foto da fila (1 por 10 min), avalia alertas e
    limpa histórico velho. Nunca levanta exceção."""
    agora = agora or datetime.now()
    try:
        conn = _conectar()
        if origem == "supabase":
            conn.execute("INSERT INTO saude_rotinas (rotina, inicio, fim, ok) VALUES ('pulso_supabase', ?, ?, 1)",
                         (agora.strftime("%Y-%m-%d %H:%M:%S"),) * 2)
        foto = foto_da_fila(conn, agora)
        gravar_foto(conn, foto, origem, agora)
        alertas = avaliar_alertas(conn, foto, agora, enviar=enviar and origem == "supabase")
        if agora.minute < 15:
            _limpar_historico(conn, agora)
        conn.commit()
        conn.close()
        return {"foto": foto, "alertas": alertas}
    except Exception as erro:
        print(f"[saude_sistema] pulso falhou: {erro}")
        return None


# ==========================
# ROTAS
# ==========================

@bp_saude.route("/admin/saude")
def painel_saude():
    from app import login_required, perfil_required

    @login_required
    @perfil_required("ADM")
    def _tela():
        agora = datetime.now()
        resultado = pulso(origem="painel", agora=agora, enviar=False) or {}
        conn = _conectar()
        cfg = _config(conn)
        dia = request.args.get("dia") or agora.strftime("%Y-%m-%d")
        fotos = [dict(r) for r in conn.execute("SELECT * FROM saude_fila WHERE substr(slot,1,10)=? ORDER BY slot", (dia,))]
        tph = tratativas_por_hora(conn, dia)
        ctx = {
            "agora": agora, "dia": dia, "cfg": cfg,
            "rotinas": status_rotinas(conn, agora),
            "pulso": status_pulso(conn, agora),
            "pulso_parado_min": _int(cfg, "pulso_parado_min"),
            "foto": resultado.get("foto") or foto_da_fila(conn, agora),
            "alertas_agora": resultado.get("alertas") or [],
            "fotos": fotos,
            "tratativas_hora": [{"hora": f"{h:02d}h", "qtd": tph.get(h, 0)} for h in range(7, 20)],
            "erros_hora": erros_por_hora(conn, agora),
            "erros_24h": conn.execute("SELECT COUNT(*) FROM saude_erros WHERE quando >= ?",
                                      ((agora - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S"),)).fetchone()[0],
            "erros": erros_agrupados(conn),
            "historico_alertas": conn.execute("SELECT * FROM saude_alertas ORDER BY id DESC LIMIT 30").fetchall(),
            "historico_rotinas": conn.execute("""SELECT * FROM saude_rotinas WHERE rotina != 'pulso_supabase'
                                                  ORDER BY id DESC LIMIT 30""").fetchall(),
            "dias_com_fotos": [r[0] for r in conn.execute("SELECT DISTINCT substr(slot,1,10) FROM saude_fila ORDER BY 1 DESC LIMIT 30")],
            "ok": request.args.get("ok"), "erro": request.args.get("erro"),
        }
        conn.close()
        return render_template("SaudeSistema.html", **ctx)
    return _tela()


@bp_saude.route("/admin/saude/config", methods=["POST"])
def salvar_config_saude():
    from app import login_required, perfil_required

    @login_required
    @perfil_required("ADM")
    def _salvar():
        import urllib.parse
        conn = _conectar()
        for chave in CONFIG_PADRAO:
            if chave == "alertas_ativos":
                valor = "1" if request.form.get("alertas_ativos") else "0"
            else:
                valor = (request.form.get(chave) or "").strip()
                if not valor:
                    continue
            conn.execute("INSERT INTO saude_config (chave, valor) VALUES (?, ?) ON CONFLICT(chave) DO UPDATE SET valor=excluded.valor",
                         (chave, valor))
        conn.commit()
        conn.close()
        return redirect("/admin/saude?ok=" + urllib.parse.quote("Configuração dos alertas salva.") + "#alertas")
    return _salvar()


@bp_saude.route("/admin/saude/teste-alerta", methods=["POST"])
def teste_alerta():
    from app import login_required, perfil_required

    @login_required
    @perfil_required("ADM")
    def _teste():
        import urllib.parse
        ok, erro = _enviar_telegram("✅ CRM RSI — Monitor\nAlerta de teste: se você está lendo isto, os alertas chegam no Telegram.")
        conn = _conectar()
        conn.execute("INSERT INTO saude_alertas (quando, tipo, mensagem, enviado, erro) VALUES (?, 'teste', 'Alerta de teste', ?, ?)",
                     (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 1 if ok else 0, None if ok else str(erro)[:300]))
        conn.commit()
        conn.close()
        if ok:
            return redirect("/admin/saude?ok=" + urllib.parse.quote("Alerta de teste enviado para o Telegram.") + "#alertas")
        return redirect("/admin/saude?erro=" + urllib.parse.quote(f"Não consegui enviar: {erro}") + "#alertas")
    return _teste()
