"""Default passthrough parser for tools without custom parsing."""

from __future__ import annotations

from code_review_benchmark.parsers.base import BaseParser
from code_review_benchmark.parsers.base import ParsedComment
from code_review_benchmark.parsers.base import ParsedReview


class DefaultParser(BaseParser):
    """Passes all comments through as a single 'raw' section."""

    def default_sections(self) -> dict[str, bool]:
        return {"raw": True}

    def parse(self, review_comments: list[dict]) -> ParsedReview:
        comments = [
            ParsedComment(
                body=c.get("body", ""),
                path=c.get("path"),
                line=c.get("line"),
                created_at=c.get("created_at", ""),
                section="raw",
                severity=None,
            )
            for c in review_comments
        ]
        return ParsedReview(sections={"raw": comments})
