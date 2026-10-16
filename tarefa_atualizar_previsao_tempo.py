"""
Busca a previsao do tempo dos proximos 7 dias pra cada cidade atendida
(Open-Meteo, gratuita, sem chave) e atualiza o cache local -- feito pra
rodar 1x por dia via Scheduled Task do PythonAnywhere. A tela de
agendamento consulta so esse cache, nunca a internet na hora.

COMO CONFIGURAR NO PYTHONANYWHERE:
  1. Aba "Tasks"
  2. "Scheduled Tasks" -> "Daily"
  3. Horario: qualquer cedo da manha, ex: 08:00 UTC (05:00 de Brasilia) --
     nao precisa ser exato, so precisa rodar 1x por dia antes do pessoal
     comecar a agendar
  4. Comando:
     python3.11 /home/crmrsi/CRM_RSI/tarefa_atualizar_previsao_tempo.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import atualizar_previsao_tempo_todas_cidades
from datetime import datetime

agora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
sucesso, falha = atualizar_previsao_tempo_todas_cidades()
print(f"[{agora_str}] Previsão do tempo atualizada: {sucesso} cidade(s) com sucesso, {falha} falha(s).")
