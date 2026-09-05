"""AI is advisory. It never owns financial truth.

Everything in this package obeys three rules:

* an AI response is validated against a schema or it produces no state change;
* an AI response can never create an approved financial action;
* AI can be switched off per tenant, and the platform still works.
"""

from packages.ai.document_understanding import (
    DocumentParser,
    ExtractedDocument,
    ExtractedField,
    NullDocumentParser,
    verification_required,
)
from packages.ai.entity_resolution import (
    Alias,
    AliasStatus,
    CanonicalEntity,
    EntityResolver,
    Resolution,
    new_entity,
)
from packages.ai.exception_classification import (
    ClassificationOutcome,
    attach_suggestion,
    classify_with_ai,
)
from packages.ai.investigation import (
    InvestigationAnswer,
    deterministic_narrative,
    gather_evidence,
    investigate,
)
from packages.ai.privacy import (
    TASK_FIELD_ALLOWLIST,
    AIDisabledError,
    DataRegionViolation,
    MinimizedPayload,
    TenantAISettings,
    prepare_payload,
    redact_value,
)
from packages.ai.prompts import PROMPT_VERSIONS, SYSTEM_PROMPT, build_prompt
from packages.ai.provider import (
    AIProvider,
    DeterministicProvider,
    HTTPProvider,
    NullProvider,
    build_provider,
)
from packages.ai.schemas import (
    AICallRecord,
    AIResponseEnvelope,
    EntityResolutionSuggestion,
    EvidenceSummary,
    ExceptionClassificationSuggestion,
    ExtractedReferenceSuggestion,
    InvestigationEvidence,
    ResolutionProposal,
    SchemaValidationFailure,
)

__all__ = [
    "AICallRecord",
    "AIDisabledError",
    "AIProvider",
    "AIResponseEnvelope",
    "Alias",
    "AliasStatus",
    "CanonicalEntity",
    "ClassificationOutcome",
    "DataRegionViolation",
    "DeterministicProvider",
    "DocumentParser",
    "EntityResolutionSuggestion",
    "EntityResolver",
    "EvidenceSummary",
    "ExceptionClassificationSuggestion",
    "ExtractedDocument",
    "ExtractedField",
    "ExtractedReferenceSuggestion",
    "HTTPProvider",
    "InvestigationAnswer",
    "InvestigationEvidence",
    "MinimizedPayload",
    "NullDocumentParser",
    "NullProvider",
    "PROMPT_VERSIONS",
    "Resolution",
    "ResolutionProposal",
    "SYSTEM_PROMPT",
    "SchemaValidationFailure",
    "TASK_FIELD_ALLOWLIST",
    "TenantAISettings",
    "attach_suggestion",
    "build_prompt",
    "build_provider",
    "classify_with_ai",
    "deterministic_narrative",
    "gather_evidence",
    "investigate",
    "new_entity",
    "prepare_payload",
    "redact_value",
    "verification_required",
]
