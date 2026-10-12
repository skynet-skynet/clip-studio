"""Точка входа собранной программы ClipStudio.exe.

Двойной щелчок — запускает интерфейс отдельным окном. С аргументами работает как CLI:
  ClipStudio.exe status
  ClipStudio.exe mcp              # MCP-сервер для Claude
  ClipStudio.exe connect-claude
"""
import multiprocessing
import sys

from clipstudio.cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main(sys.argv[1:])
