"""
Backup diário dos bancos no Supabase Storage (ver backup_nuvem.py) --
substitui o antigo backup no Google Drive (2026-10-17).

COMO CONFIGURAR NO PYTHONANYWHERE:
  Aba "Tasks" -> "Scheduled Tasks" -> Daily, 06:00 UTC (03:00 de Brasília) -- mesmo python das outras tasks
  Comando:
     python3.11 /home/crmrsi/CRM_RSI/tarefa_backup_nuvem.py

Registra no Monitor (rotina "backup_nuvem"): se falhar ou não rodar, o
Monitor avisa no Telegram uma vez (respeitando o intervalo dos alertas).
"""
import os
import sys
import time

os.environ["TZ"] = "America/Sao_Paulo"  # mesmo relógio do app.py
try:
    time.tzset()
except AttributeError:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime
from backup_nuvem import fazer_backup
from saude_sistema import registrar_rotina

with registrar_rotina("backup_nuvem"):
    resumo = fazer_backup()
print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Backup na nuvem: {resumo}")
