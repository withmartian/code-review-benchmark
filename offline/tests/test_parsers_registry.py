"""Tests for parser registry."""

from __future__ import annotations

from code_review_benchmark.parsers import get_parser
from code_review_benchmark.parsers.default import DefaultParser


def test_unknown_tool_returns_default_parser():
    parser = get_parser("unknown-tool")
    assert isinstance(parser, DefaultParser)


def test_coderabbit_returns_coderabbit_parser():
    from code_review_benchmark.parsers.coderabbit import CodeRabbitParser

    parser = get_parser("coderabbit")
    assert isinstance(parser, CodeRabbitParser)
