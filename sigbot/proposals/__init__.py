"""Proposal modules (paper-mode by default, never wired into run.py by this package).

Each module is stdlib-only and has a `--backtest` CLI that replays the logged tape / quotes and reports hypothetical
fills and markouts, plus a `cycle()` entry point the main session can wire in after review.
"""
