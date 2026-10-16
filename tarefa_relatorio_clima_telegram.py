"""
Manda o resumo diario da previsao do tempo (7 dias, por DDD + cidades de
destaque) pro Telegram -- pedido do Bruno, 2026-09-17. So LE dados que ja
existem (previsao_tempo, coletada por tarefa_atualizar_previsao_tempo.py),
nao escreve nada. Usa a mesma Edge Function do Supabase que os outros
relatorios do Telegram ja usam (importa de app.py, nao duplica).

COMO CONFIGURAR NO PYTHONANYWHERE:
  1. Aba "Tasks"
  2. "Scheduled Tasks" -> "Daily"
  3. Horario: cedo da manha, antes do expediente comecar (ex: 09:00 UTC =
     06:00 de Brasilia, ou ajuste pro horario que preferir)
  4. Comando:
     python3.11 /home/crmrsi/CRM_RSI/tarefa_relatorio_clima_telegram.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sqlite3
from datetime import datetime
from relatorio_clima import montar_relatorio_clima, montar_texto_telegram
from app import enviar_relatorio_telegram_via_supabase, conectar

agora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

conn = conectar()
config = conn.execute("SELECT supabase_project_url, supabase_anon_key FROM configuracoes WHERE id=1").fetchone()
conn.close()

if not config or not config["supabase_project_url"] or not config["supabase_anon_key"]:
    print(f"[{agora_str}] Supabase não configurado -- pulando envio do relatório de clima.")
else:
    relatorio = montar_relatorio_clima()
    texto = montar_texto_telegram(relatorio)

    sucesso, erro = enviar_relatorio_telegram_via_supabase(
        config["supabase_project_url"], config["supabase_anon_key"], texto
    )
    if sucesso:
        print(f"[{agora_str}] Relatório de clima enviado com sucesso.")
    else:
        print(f"[{agora_str}] Falha ao enviar relatório de clima -- {erro}")
