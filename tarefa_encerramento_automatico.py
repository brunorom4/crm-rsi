"""
Fecha automaticamente a sessao de quem esqueceu de bater saida (18h30) --
feito pra rodar via Scheduled Task do PythonAnywhere, ja que o WSGI nao
mantem threads em segundo plano vivas (o app.py so tenta rodar isso de novo
na proxima requisicao de alguem logado -- ver
verificar_encerramento_automatico_se_necessario() -- o que falha se
ninguem acessar o site depois das 18h30, tipo numa sexta a noite).

Reusa a MESMA funcao que o app usa (encerrar_jornadas_esquecidas), entao
respeita o toggle de ligado/desligado que o ADM configura na tela de
Pausas, sem duplicar nenhuma logica.

COMO CONFIGURAR NO PYTHONANYWHERE:
  1. Aba "Tasks"
  2. Em "Scheduled Tasks", escolher "Daily" (todo santo dia, inclusive fim
     de semana -- e justamente pra pegar sexta a noite que isso importa)
  3. Horario: 21:30 UTC (equivale a 18:30 de Brasilia, que e UTC-3 o ano
     inteiro desde 2019, sem horario de verao)
  4. Comando: python3.11 /home/crmrsi/CRM_RSI/tarefa_encerramento_automatico.py
     (ajustar o caminho se o nome da pasta for outro)
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import encerrar_jornadas_esquecidas

encerrar_jornadas_esquecidas()
