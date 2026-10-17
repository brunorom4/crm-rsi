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

# Registro no Painel de Saúde do Sistema (/admin/saude) -- 2026-10-17.
from saude_sistema import registrar_rotina

agora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
with registrar_rotina("previsao_tempo"):
    sucesso, falha = atualizar_previsao_tempo_todas_cidades()
    if sucesso == 0 and falha:
        raise RuntimeError(f"nenhuma cidade atualizada ({falha} falha(s))")
print(f"[{agora_str}] Previsão do tempo atualizada: {sucesso} cidade(s) com sucesso, {falha} falha(s).")
