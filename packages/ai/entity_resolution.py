"""Entity resolution with an alias table (spec section 25).

Resolution order, and it matters:

    1. exact normalised alias
    2. customer-configured aliases
    3. fuzzy candidate search
    4. optional model candidate search
    5. human-approved resolution

An entity is never permanently merged because a model said the names look alike.
A model suggestion produces a *pending* alias that a human approves or rejects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

from packages.ai.privacy import AIDisabledError, TenantAISettings
from packages.ai.provider import AIProvider
from packages.ai.schemas import EntityResolutionSuggestion
from packages.domain.dates import utc_now
from packages.ingestion.normalization import normalize_name
from packages.matching.fuzzy import jaro_winkler_similarity, trigram_similarity

__all__ = [
    "Alias",
    "AliasStatus",
    "CanonicalEntity",
    "EntityResolver",
    "Resolution",
    "new_entity",
]


class AliasStatus:
    APPROVED = "APPROVED"
    PENDING = "PENDING"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class Alias:
    alias: str
    normalized_alias: str
    entity_id: UUID
    status: str = AliasStatus.APPROVED
    source: str = "manual"
    suggested_by: str | None = None
    approved_by: UUID | None = None
    created_at: datetime | None = None


@dataclass(slots=True)
class CanonicalEntity:
    id: UUID
    tenant_id: UUID
    name: str
    normalized_name: str = ""

    def __post_init__(self) -> None:
        if not self.normalized_name:
            self.normalized_name = normalize_name(self.name) or self.name.upper()


@dataclass(frozen=True, slots=True)
class Resolution:
    entity: CanonicalEntity | None
    method: str
    confidence: float
    requires_human_approval: bool = False
    pending_alias: Alias | None = None
    explanation: str = ""


@dataclass(slots=True)
class EntityResolver:
    """Resolves an observed counterparty name to a canonical entity."""

    tenant_id: UUID
    entities: dict[UUID, CanonicalEntity] = field(default_factory=dict)
    aliases: list[Alias] = field(default_factory=list)
    fuzzy_threshold: float = 0.93
    suggest_threshold: float = 0.85

    def add_entity(self, entity: CanonicalEntity) -> None:
        self.entities[entity.id] = entity

    def add_alias(self, alias: str, entity_id: UUID, *, approved_by: UUID | None = None) -> Alias:
        record = Alias(
            alias=alias,
            normalized_alias=normalize_name(alias) or alias.upper(),
            entity_id=entity_id,
            status=AliasStatus.APPROVED,
            source="manual",
            approved_by=approved_by,
            created_at=utc_now(),
        )
        self.aliases.append(record)
        return record

    def resolve(
        self,
        observed: str | None,
        *,
        provider: AIProvider | None = None,
        settings: TenantAISettings | None = None,
    ) -> Resolution:
        normalized = normalize_name(observed)
        if not normalized:
            return Resolution(None, "none", 0.0, explanation="No counterparty name supplied.")

        # 1. exact match against a canonical entity name
        for entity in self._sorted_entities():
            if entity.normalized_name == normalized:
                return Resolution(entity, "exact_name", 1.0, explanation="Exact name match.")

        # 2. approved alias
        for alias in sorted(self.aliases, key=lambda a: a.normalized_alias):
            if alias.status == AliasStatus.APPROVED and alias.normalized_alias == normalized:
                alias_entity = self.entities.get(alias.entity_id)
                if alias_entity is not None:
                    return Resolution(
                        alias_entity,
                        "approved_alias",
                        1.0,
                        explanation=f"'{alias.alias}' is an approved alias.",
                    )

        # 3. fuzzy search over canonical entities
        best_entity, best_score = None, 0.0
        for entity in self._sorted_entities():
            score = max(
                jaro_winkler_similarity(normalized, entity.normalized_name),
                trigram_similarity(normalized, entity.normalized_name),
            )
            if score > best_score:
                best_entity, best_score = entity, score

        if best_entity is not None and best_score >= self.fuzzy_threshold:
            return Resolution(
                best_entity,
                "fuzzy",
                round(best_score, 4),
                requires_human_approval=True,
                pending_alias=self._pending(normalized, observed or "", best_entity.id, "fuzzy"),
                explanation=(
                    f"'{observed}' is {best_score:.0%} similar to "
                    f"'{best_entity.name}'. A human must confirm before this "
                    "becomes a permanent alias."
                ),
            )

        # 4. optional model candidate search
        if provider is not None and settings is not None and self.entities:
            try:
                envelope = provider.resolve_entity(
                    self.tenant_id,
                    {
                        "counterparty_name": observed,
                        "normalized_counterparty": normalized,
                        "candidate_entities": [e.name for e in self._sorted_entities()[:20]],
                    },
                    settings,
                )
            except AIDisabledError:
                envelope = None

            if envelope is not None and envelope.usable:
                suggestion = envelope.suggestion
                if (
                    isinstance(suggestion, EntityResolutionSuggestion)
                    and suggestion.is_same_entity
                    and suggestion.confidence >= self.suggest_threshold
                ):
                    target = self._entity_by_name(suggestion.canonical_entity)
                    if target is not None:
                        return Resolution(
                            None,
                            "ai_suggested",
                            suggestion.confidence,
                            requires_human_approval=True,
                            pending_alias=self._pending(
                                normalized, observed or "", target.id, "ai"
                            ),
                            explanation=(
                                f"The assistant suggests '{observed}' is "
                                f"'{target.name}'. Entities are never merged on a "
                                "model's word alone; approve the alias to apply it."
                            ),
                        )

        return Resolution(
            None,
            "unresolved",
            round(best_score, 4),
            explanation=f"No canonical entity matched '{observed}'.",
        )

    def approve_pending(self, alias: Alias, approved_by: UUID) -> Alias:
        """A human turns a pending alias into an approved one."""
        approved = Alias(
            alias=alias.alias,
            normalized_alias=alias.normalized_alias,
            entity_id=alias.entity_id,
            status=AliasStatus.APPROVED,
            source=alias.source,
            suggested_by=alias.suggested_by,
            approved_by=approved_by,
            created_at=utc_now(),
        )
        self.aliases = [a for a in self.aliases if a.normalized_alias != alias.normalized_alias]
        self.aliases.append(approved)
        return approved

    def _pending(self, normalized: str, observed: str, entity_id: UUID, source: str) -> Alias:
        return Alias(
            alias=observed,
            normalized_alias=normalized,
            entity_id=entity_id,
            status=AliasStatus.PENDING,
            source=source,
            suggested_by=source,
            created_at=utc_now(),
        )

    def _sorted_entities(self) -> list[CanonicalEntity]:
        # Deterministic order, so an ambiguous fuzzy result is stable.
        return sorted(self.entities.values(), key=lambda e: (e.normalized_name, str(e.id)))

    def _entity_by_name(self, name: str) -> CanonicalEntity | None:
        normalized = normalize_name(name)
        for entity in self._sorted_entities():
            if entity.normalized_name == normalized:
                return entity
        return None


def new_entity(tenant_id: UUID, name: str) -> CanonicalEntity:
    return CanonicalEntity(id=uuid4(), tenant_id=tenant_id, name=name)
