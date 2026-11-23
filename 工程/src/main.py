# -*- coding: utf-8 -*-
"""TermX 启动脚本（PyInstaller 入口）。"""
import sys


def main():
    from termx.app import main as run
    return run()


if __name__ == "__main__":
    sys.exit(main())
