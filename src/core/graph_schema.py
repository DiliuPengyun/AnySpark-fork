"""Neo4j Graph Schema — node labels, relationship types, constraints."""

import re

ENTITY_LABELS = {
    "character": "Character",
    "location": "Location",
    "item": "Item",
    "skill": "Skill",
    "organization": "Organization",
    "race": "Race",
    "concept": "Concept",
    "event": "Event",
    # Narrative logic — independent label, not an Entity subtype
    "constraint": "Constraint",
}


def entity_label(entity_type: str) -> str:
    """Convert entity type string to Neo4j label (CamelCase). Custom types auto-capitalized.
    Validated to prevent Cypher injection."""
    known = ENTITY_LABELS.get(entity_type)
    if known:
        return known
    # Sanitize custom types: only allow alphanumeric + underscore
    label = entity_type.title().replace("_", "")
    if not re.match(r"^[A-Za-z][A-Za-z0-9]*$", label):
        raise ValueError(f"Invalid entity type for Neo4j label: {entity_type!r}")
    return label

RELATIONSHIP_TYPES = [
    "KNOWS", "BELONGS_TO", "LOCATED_AT", "OWNS", "ANTAGONIST",
    "ALLY", "FAMILY", "ROMANTIC", "MASTER_OF", "MENTOR_OF",
    "KILLED", "SAVED", "LOVES", "CAUSES",
    "BEFORE", "AFTER", "FORESHADOWS", "RESOLVES", "PARTICIPATES_IN",
    # P0: Graph edgification — connect Foreshadow/Timeline/Snapshot to Entity
    "INVOLVES",       # Foreshadow/Timeline → Entity
    "HAS_PHASE",      # Entity → Snapshot (character phase)
    "DEPENDS_ON",     # Foreshadow → Foreshadow (dependency chain, P2)
    # Narrative logic — constraint governance
    "GOVERNS",        # Constraint → Entity (which entity a rule applies to)
]

CONSTRAINTS = [
    "CREATE CONSTRAINT entity_id IF NOT EXISTS FOR (e:Entity) REQUIRE e.id IS UNIQUE",
    "CREATE CONSTRAINT project_id IF NOT EXISTS FOR (p:Project) REQUIRE p.id IS UNIQUE",
    "CREATE CONSTRAINT timeline_id IF NOT EXISTS FOR (t:Timeline) REQUIRE t.id IS UNIQUE",
    "CREATE CONSTRAINT snapshot_id IF NOT EXISTS FOR (s:Snapshot) REQUIRE s.id IS UNIQUE",
    "CREATE CONSTRAINT chapter_id IF NOT EXISTS FOR (c:Chapter) REQUIRE c.id IS UNIQUE",
    "CREATE CONSTRAINT constraint_id IF NOT EXISTS FOR (c:Constraint) REQUIRE c.id IS UNIQUE",
]

INDEXES = [
    "CREATE INDEX entity_name IF NOT EXISTS FOR (e:Entity) ON (e.name)",
    "CREATE INDEX entity_type IF NOT EXISTS FOR (e:Entity) ON (e.entity_type)",
    "CREATE INDEX entity_project IF NOT EXISTS FOR (e:Entity) ON (e.project_id)",
    "CREATE INDEX snapshot_char IF NOT EXISTS FOR (s:Snapshot) ON (s.character_id)",
    "CREATE FULLTEXT INDEX entity_text IF NOT EXISTS FOR (e:Entity) ON EACH [e.name, e.aliases]",
    "CREATE INDEX constraint_project IF NOT EXISTS FOR (c:Constraint) ON (c.project_id)",
]

PROPERTY_KEYS = [
    "id", "entity_type", "name", "aliases",
    "data", "project_id", "created_at", "updated_at",
    "label", "time_point", "time_order", "description",
    "chapter_ref", "content", "title",
]
