"""Pacote de views — NÃO importa gi aqui.

O único módulo autorizado a importar `gi`/GTK é `hexavante.views.app`,
com guard try/except para operar em headless/CI. Manter este __init__
livre de gi garante isolamento testável.
"""
