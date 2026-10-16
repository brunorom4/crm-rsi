"""
Marca como NAO_TRATADO qualquer contrato da Confirmacao de Agenda que
continuava PENDENTE no fim do dia (ninguem tratou a tempo) -- feito pra
rodar 1x por dia, no fim do expediente, via Scheduled Task do
PythonAnywhere.

COMO CONFIGURAR NO PYTHONANYWHERE:
  1. Aba "Tasks"
  2. "Scheduled Tasks" -> "Daily"
  3. Horario: fim do expediente, ex: 21:00 UTC (18:00 de Brasilia)
  4. Comando:
     python3.11 /home/crmrsi/CRM_RSI/tarefa_confirmacao_nao_tratado.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from confirmacao_agenda import marcar_nao_tratados_do_dia
from datetime import datetime

agora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
qtd = marcar_nao_tratados_do_dia()
print(f"[{agora_str}] Confirmação de Agenda: {qtd} contrato(s) marcado(s) como NAO_TRATADO.")
