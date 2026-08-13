"""Quarantined public-source corpus candidate acquisition."""

from polytrading.corpus_intake.models import (
    AcquisitionDiagnostics,
    AcquisitionRequest,
    AcquisitionResult,
    CorpusCandidate,
    CorpusIntakeError,
    ParsedPage,
    RawPageCapture,
)

__all__ = [
    "AcquisitionDiagnostics",
    "AcquisitionRequest",
    "AcquisitionResult",
    "CorpusCandidate",
    "CorpusIntakeError",
    "ParsedPage",
    "RawPageCapture",
]
