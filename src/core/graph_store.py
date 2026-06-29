"""Neo4j Graph Store — replaces SQLite KnowledgeStore with graph-native operations."""

import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from neo4j import Driver, GraphDatabase

from .graph_schema import CONSTRAINTS, INDEXES, entity_label
from .knowledge import CharacterSnapshot, Entity, EntityType, Foreshadow, Relation, RelationType, TimelineEvent

logger = logging.getLogger(__name__)


env_path = Path(__file__).parent.parent.parent / ".env"
load_dotenv(env_path)

# ── Shared Neo4j driver singleton (avoid creating a new driver per GraphStore) ──
_shared_driver: Driver | None = None
_last_connect_attempt: float = 0.0  # timestamp of last connection attempt
RECONNECT_INTERVAL: float = 60.0    # seconds between reconnection retries


def _get_driver() -> Driver | None:
    global _shared_driver, _last_connect_attempt
    if _shared_driver is None:
        # Reconnection guard: don't hammer Neo4j if it's down
        import time
        now = time.time()
        if _last_connect_attempt > 0 and (now - _last_connect_attempt) < RECONNECT_INTERVAL:
            return None
        _last_connect_attempt = now

        uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
        user = os.getenv("NEO4J_USER", "neo4j")
        password = os.getenv("NEO4J_PASSWORD", "novel_agent_2024!")
        try:
            _shared_driver = GraphDatabase.driver(uri, auth=(user, password))
            _shared_driver.verify_connectivity()
            logger.info("Neo4j connected successfully")
        except Exception as e:
            logger.warning(f"Neo4j unavailable, graph features degraded: {e}")
            _shared_driver = None
    return _shared_driver


def close_shared_driver():
    """Close the shared driver on application shutdown."""
    global _shared_driver
    if _shared_driver is not None:
        try:
            _shared_driver.close()
        except (OSError, RuntimeError):
            pass
        _shared_driver = None


class GraphStore:
    def __init__(self, project_id: str = "default"):
        self.project_id = project_id
        self._driver: Driver | None = _get_driver()

    def _run(self, query: str, params: dict = None):
        if self._driver is None:
            logger.debug("Neo4j unavailable, returning empty result")
            return []
        try:
            with self._driver.session() as session:
                return list(session.run(query, params or {}))
        except Exception as e:
            logger.warning("Neo4j query failed: %s", e)
            return []

    def _run_single(self, query: str, params: dict = None):
        r = self._run(query, params)
        return r[0] if r else None

    def init_schema(self):
        try:
            self._run("MERGE (p:Project {id: $pid}) SET p.name = 'default'", {"pid": self.project_id})
        except Exception:
            pass
        for c in CONSTRAINTS + INDEXES:
            try:
                self._run(c)
            except Exception as e:
                logger.debug("Schema constraint/index failed: %s", e)

    def close(self):
        # No-op: shared driver is managed globally via close_shared_driver()
        pass


    # ── Batch Operations ──

    def batch_write(self, operations: list[dict]):
        if self._driver is None:
            logger.warning("batch_write skipped: Neo4j unavailable")
            return
        with self._driver.session() as session:
            with session.begin_transaction() as tx:
                for op in operations:
                    try:
                        if op["type"] == "add_entity":
                            self._tx_add_entity(tx, op["entity"])
                        elif op["type"] == "update_entity":
                            self._tx_update_entity(tx, op["id"], op["data"])
                        elif op["type"] == "add_relation":
                            self._tx_add_relation(tx, op["relation"])
                        elif op["type"] == "add_foreshadow":
                            self._tx_add_foreshadow(tx, op["foreshadow"])
                    except Exception as e:
                        logger.warning(f"batch_write operation failed ({op.get('type', '?')}): {e}")
                tx.commit()

    def batch_add_entities(self, entities: list[Entity]):
        if not entities:
            return
        params = []
        for e in entities:
            params.append({
                "id": e.id, "type": e.type, "name": e.name,
                "aliases": e.aliases,
                "data": json.dumps(e.data, ensure_ascii=False),
            })
        with self._driver.session() as session:
            session.run("""
                UNWIND $batch AS item
                MERGE (e:Entity {id: item.id, project_id: $pid})
                SET e.entity_type = item.type, e.name = item.name,
                    e.aliases = item.aliases, e.data = item.data,
                    e.updated_at = $now, e.created_at = coalesce(e.created_at, $now)
                WITH e
                MERGE (p:Project {id: $pid})
                MERGE (e)-[:BELONGS_TO_PROJECT]->(p)
            """, {"batch": params, "pid": self.project_id, "now": datetime.now().isoformat()})

    def batch_add_relations(self, relations: list["Relation"]):
        if not relations:
            return
        from collections import defaultdict
        by_type = defaultdict(list)
        for rel in relations:
            by_type[rel.type.upper()].append(rel)
        with self._driver.session() as session:
            now = datetime.now().isoformat()
            pid = self.project_id
            for rel_type, rels in by_type.items():
                params = []
                for rel in rels:
                    params.append({
                        "from_id": rel.from_entity, "to_id": rel.to_entity,
                        "rid": rel.id,
                        "data": json.dumps(rel.data, ensure_ascii=False),
                    })
                try:
                    session.run("""
                        UNWIND $batch AS item
                        MATCH (a:Entity {id: item.from_id, project_id: $pid})
                        MATCH (b:Entity {id: item.to_id, project_id: $pid})
                        MERGE (a)-[r:""" + rel_type + """]->(b)
                        SET r.id = item.rid, r.data = item.data,
                            r.project_id = $pid, r.updated_at = $now,
                            r.created_at = coalesce(r.created_at, $now)
                    """, {"batch": params, "pid": pid, "now": now})
                except Exception as e:
                    logger.warning("batch_add_relations failed for type %s: %s", rel_type, e)

    def batch_add_foreshadows(self, foreshadows: list["Foreshadow"]):
        if not foreshadows:
            return
        params = []
        for fs in foreshadows:
            params.append({
                "id": fs.id, "text": fs.text, "hint": fs.hint,
                "er": fs.expected_resolution, "r": fs.resolved, "rt": fs.resolution_text,
                "data": json.dumps({"text": fs.text, "hint": fs.hint,
                                    "expected_resolution": fs.expected_resolution,
                                    "resolved": fs.resolved, "resolution_text": fs.resolution_text,
                                    "related_entities": fs.related_entities,
                                    "related_events": fs.related_events}, ensure_ascii=False),
            })
        with self._driver.session() as session:
            session.run("""
                UNWIND $batch AS item
                CREATE (s:Snapshot:Fore {
                    id: item.id, character_id: '', time_point: '',
                    time_order: 0, label: 'foreshadow', data: item.data,
                    description: '', project_id: $pid,
                    text: item.text, hint: item.hint,
                    expected_resolution: item.er, resolved: item.r,
                    resolution_text: item.rt
                })
            """, {"batch": params, "pid": self.project_id})
            # P0-1: Create INVOLVES edges for batch foreshadows
            for fs in foreshadows:
                for eid in fs.related_entities:
                    if eid:
                        session.run("""
                            MATCH (f:Fore {id: $fid, project_id: $pid})
                            MATCH (e:Entity {id: $eid, project_id: $pid})
                            MERGE (f)-[:INVOLVES]->(e)
                        """, {"fid": fs.id, "eid": eid, "pid": self.project_id})

    def _tx_add_entity(self, tx, entity: Entity):
        label = entity_label(entity.type)
        tx.run(f"""
            MERGE (e:Entity:{label} {{id: $id}})
            SET e.entity_type = $type, e.name = $name, e.aliases = $aliases,
                e.data = $data, e.project_id = $pid, e.updated_at = $now,
                e.created_at = coalesce(e.created_at, $now)
            MERGE (p:Project {{id: $pid}})
            MERGE (e)-[:BELONGS_TO_PROJECT]->(p)
        """, {
            "id": entity.id, "type": entity.type, "name": entity.name,
            "aliases": entity.aliases,
            "data": json.dumps(entity.data, ensure_ascii=False),
            "pid": self.project_id, "now": datetime.now().isoformat(),
        })

    def _tx_update_entity(self, tx, entity_id: str, data: dict):
        tx.run("""
            MATCH (e:Entity {id: $id, project_id: $pid})
            SET e.data = $data, e.updated_at = $now
        """, {"id": entity_id, "pid": self.project_id,
              "data": json.dumps(data, ensure_ascii=False),
              "now": datetime.now().isoformat()})

    def _tx_add_relation(self, tx, relation: "Relation"):
        rel_type = relation.type.upper()
        tx.run(f"""
            MATCH (a:Entity {{id: $from_id, project_id: $pid}})
            MATCH (b:Entity {{id: $to_id, project_id: $pid}})
            MERGE (a)-[r:{rel_type}]->(b)
            SET r.id = $rid, r.data = $data, r.project_id = $pid
        """, {
            "from_id": relation.from_entity, "to_id": relation.to_entity,
            "rid": relation.id, "pid": self.project_id,
            "data": json.dumps(relation.data, ensure_ascii=False),
        })

    def _tx_add_foreshadow(self, tx, fs: "Foreshadow"):
        tx.run("""
            CREATE (s:Snapshot:Fore {
                id: $id, character_id: '', time_point: '',
                time_order: 0, label: 'foreshadow',
                data: $data, description: '', project_id: $pid,
                text: $text, hint: $hint, expected_resolution: $er,
                resolved: $r, resolution_text: $rt
            })
        """, {
            "id": fs.id, "text": fs.text, "hint": fs.hint,
            "er": fs.expected_resolution, "r": fs.resolved, "rt": fs.resolution_text,
            "data": json.dumps({"text": fs.text, "hint": fs.hint}, ensure_ascii=False),
            "pid": self.project_id,
        })
        # P0-1: Create INVOLVES edges within transaction
        for eid in fs.related_entities:
            if eid:
                tx.run("""
                    MATCH (f:Fore {id: $fid, project_id: $pid})
                    MATCH (e:Entity {id: $eid, project_id: $pid})
                    MERGE (f)-[:INVOLVES]->(e)
                """, {"fid": fs.id, "eid": eid, "pid": self.project_id})

    # ── Entity CRUD ──

    def add_entity(self, entity: Entity) -> Entity:
        label = entity_label(entity.type)
        self._run(f"""
            MERGE (e:Entity:{label} {{id: $id}})
            SET e.entity_type = $type, e.name = $name, e.aliases = $aliases,
                e.data = $data, e.project_id = $pid, e.updated_at = $now,
                e.created_at = coalesce(e.created_at, $now)
            MERGE (p:Project {{id: $pid}})
            MERGE (e)-[:BELONGS_TO_PROJECT]->(p)
        """, {
            "id": entity.id, "type": entity.type, "name": entity.name,
            "aliases": entity.aliases,
            "data": json.dumps(entity.data, ensure_ascii=False),
            "pid": self.project_id, "now": datetime.now().isoformat(),
        })
        return entity

    def get_entity(self, entity_id: str) -> Entity | None:
        r = self._run_single(
            "MATCH (e:Entity {id: $id, project_id: $pid}) RETURN e",
            {"id": entity_id, "pid": self.project_id}
        )
        if not r:
            return None
        return self._row_to_entity(r["e"])

    def get_entity_by_name(self, name: str) -> Entity | None:
        r = self._run_single("""
            MATCH (e:Entity {project_id: $pid}) WHERE e.name = $name
            RETURN e LIMIT 1
        """, {"name": name, "pid": self.project_id})
        if r:
            return self._row_to_entity(r["e"])
        # Alias lookup: limit 1 to stay deterministic even if aliases are dirty
        # (multiple entities sharing one alias shouldn't crash callers).
        r2 = self._run_single("""
            MATCH (e:Entity {project_id: $pid}) WHERE $name IN e.aliases
            RETURN e LIMIT 1
        """, {"name": name, "pid": self.project_id})
        if r2:
            return self._row_to_entity(r2["e"])
        # Last-resort: case-insensitive name match (LLMs frequently drift on casing)
        r3 = self._run_single("""
            MATCH (e:Entity {project_id: $pid}) WHERE toLower(e.name) = toLower($name)
            RETURN e LIMIT 1
        """, {"name": name, "pid": self.project_id})
        if r3:
            return self._row_to_entity(r3["e"])
        return None

    def list_entities(self, entity_type: str | None = None) -> list[Entity]:
        if entity_type:
            label = entity_label(entity_type)
            rows = self._run(
                f"MATCH (e:{label} {{project_id: $pid}}) RETURN e ORDER BY e.name",
                {"pid": self.project_id}
            )
        else:
            rows = self._run(
                "MATCH (e:Entity {project_id: $pid}) RETURN e ORDER BY e.entity_type, e.name",
                {"pid": self.project_id}
            )
        return [self._row_to_entity(r["e"]) for r in rows]

    def update_entity(self, entity_id: str, data: dict,
                      name: str | None = None,
                      aliases: list[str] | None = None) -> bool:
        """Update an entity's data, and optionally its name and/or aliases.

        - ``data`` is always replaced with the provided dict (merge is the
          caller's responsibility).
        - ``name``/``aliases`` are only touched when explicitly non-None.
        """
        set_clauses = ["e.data = $data", "e.updated_at = $now"]
        params = {
            "id": entity_id, "pid": self.project_id,
            "data": json.dumps(data, ensure_ascii=False),
            "now": datetime.now().isoformat(),
        }
        if name is not None:
            set_clauses.append("e.name = $name")
            params["name"] = name
        if aliases is not None:
            set_clauses.append("e.aliases = $aliases")
            params["aliases"] = aliases
        result = self._run(f"""
            MATCH (e:Entity {{id: $id, project_id: $pid}})
            SET {", ".join(set_clauses)}
            RETURN count(e) as cnt
        """, params)
        return result[0]["cnt"] > 0 if result else False

    def delete_entity(self, entity_id: str) -> bool:
        self._run("MATCH (e:Entity {id: $id, project_id: $pid}) DETACH DELETE e",
                  {"id": entity_id, "pid": self.project_id})
        return True

    # ── Relation CRUD ──

    def add_relation(self, relation: Relation) -> Relation:
        rel_type = relation.type.upper()
        self._run(f"""
            MATCH (a:Entity {{id: $from_id, project_id: $pid}})
            MATCH (b:Entity {{id: $to_id, project_id: $pid}})
            MERGE (a)-[r:{rel_type}]->(b)
            SET r.id = $rid, r.data = $data, r.project_id = $pid
        """, {
            "from_id": relation.from_entity, "to_id": relation.to_entity,
            "rid": relation.id, "pid": self.project_id,
            "data": json.dumps(relation.data, ensure_ascii=False),
        })
        return relation

    def list_relations(self, entity_id: str | None = None) -> list[Relation]:
        if entity_id:
            rows = self._run("""
                MATCH (a:Entity {id: $eid, project_id: $pid})-[r]-(b:Entity {project_id: $pid})
                RETURN a, r, b
            """, {"eid": entity_id, "pid": self.project_id})
        else:
            rows = self._run("""
                MATCH (a:Entity {project_id: $pid})-[r]-(b:Entity {project_id: $pid})
                RETURN a, r, b
            """, {"pid": self.project_id})

        rels = []
        for row in rows:
            a, r, b = row["a"], row["r"], row["b"]
            rels.append(Relation(
                id=r.get("id", str(uuid.uuid4())[:8]),
                from_entity=a["id"],
                to_entity=b["id"],
                type=RelationType(r.type.lower()),
                data=json.loads(r.get("data", "{}")) if r.get("data") else {},
            ))
        return rels

    def get_neighbors(self, entity_id: str, depth: int = 1) -> list[dict]:
        depth = max(1, min(int(depth), 10))  # clamp to [1, 10] to prevent Cypher injection
        rows = self._run(f"""
            MATCH (a:Entity {{id: $eid, project_id: $pid}})-[r*1..{depth}]-(b:Entity {{project_id: $pid}})
            RETURN DISTINCT b, [rel in r | type(rel)] as path_types
        """, {"eid": entity_id, "pid": self.project_id})
        return [{"entity": self._row_to_entity(r["b"]), "path": list(r["path_types"])} for r in rows]

    def get_path(self, from_id: str, to_id: str, max_depth: int = 3) -> list[dict]:
        max_depth = max(1, min(int(max_depth), 10))  # clamp to [1, 10]
        rows = self._run(f"""
            MATCH path = shortestPath(
                (a:Entity {{id: $from_id, project_id: $pid}})-[*1..{max_depth}]-(b:Entity {{id: $to_id, project_id: $pid}})
            )
            WHERE all(node IN nodes(path) WHERE NOT node:Project)
            RETURN nodes(path) as nodes, relationships(path) as rels, length(path) as hops
        """, {"from_id": from_id, "to_id": to_id, "pid": self.project_id})
        if not rows:
            return []
        result = []
        for row in rows:
            nodes = [{"id": n["id"], "name": n.get("name", ""), "type": n.get("entity_type", "")} for n in row["nodes"]]
            edges = [{"type": r.type, "from": r.start_node["id"], "to": r.end_node["id"]} for r in row["rels"]]
            result.append({"nodes": nodes, "edges": edges, "hops": row["hops"]})
        return result

    # ── Graph-specific queries ──

    def find_relationships(self, from_id: str, to_id: str, max_depth: int = 3) -> list[dict]:
        return self.get_path(from_id, to_id, max_depth)

    def get_entity_network(self, entity_id: str, depth: int = 2) -> dict:
        nodes_set = {}
        edges_set = {}

        rows = self._run(f"""
            MATCH (a:Entity {{id: $eid, project_id: $pid}})-[r*1..{depth}]-(b:Entity {{project_id: $pid}})
            WHERE NOT b:Project
            UNWIND r as rel
            RETURN DISTINCT startNode(rel) as sn, rel, endNode(rel) as en
        """, {"eid": entity_id, "pid": self.project_id})

        for row in rows:
            sn, r, en = row["sn"], row["rel"], row["en"]
            for n in [sn, en]:
                if n["id"] not in nodes_set:
                    nodes_set[n["id"]] = {"id": n["id"], "name": n.get("name", ""), "type": n.get("entity_type", ""),
                                          "data": json.loads(n.get("data", "{}")) if n.get("data") else {}}
            ekey = f"{sn['id']}|{en['id']}|{r.type}"
            if ekey not in edges_set:
                edges_set[ekey] = {"from": sn["id"], "to": en["id"], "type": r.type}

        return {"nodes": list(nodes_set.values()), "edges": list(edges_set.values())}

    def find_share_connections(self, entity_ids: list[str]) -> list[dict]:
        rows = self._run("""
            MATCH (a:Entity {project_id: $pid})-[r]-(b:Entity {project_id: $pid})
            WHERE a.id IN $ids AND b.id IN $ids
            RETURN a.id as from_id, b.id as to_id, type(r) as rel_type
        """, {"ids": entity_ids, "pid": self.project_id})
        return [{"from": r["from_id"], "to": r["to_id"], "type": r["rel_type"]} for r in rows]

    # ── Foreshadows ──

    def add_foreshadow(self, fs: Foreshadow) -> Foreshadow:
        self._run("""
            CREATE (s:Snapshot:Fore {
                id: $id, character_id: $cid, time_point: $tp,
                time_order: $to, label: $label, data: $data,
                description: $desc, project_id: $pid,
                text: $text, hint: $hint, expected_resolution: $er,
                resolved: $r, resolution_text: $rt
            })
        """, {
            "id": fs.id, "cid": "", "tp": "", "to": 0, "label": "foreshadow",
            "data": json.dumps({"text": fs.text, "hint": fs.hint, "expected_resolution": fs.expected_resolution,
                                "resolved": fs.resolved, "resolution_text": fs.resolution_text,
                                "related_entities": fs.related_entities, "related_events": fs.related_events},
                               ensure_ascii=False),
            "desc": "", "pid": self.project_id,
            "text": fs.text, "hint": fs.hint, "er": fs.expected_resolution,
            "r": fs.resolved, "rt": fs.resolution_text,
        })
        # P0-1: Create INVOLVES edges from foreshadow to related entities
        for eid in fs.related_entities:
            if eid:
                self._run("""
                    MATCH (f:Fore {id: $fid, project_id: $pid})
                    MATCH (e:Entity {id: $eid, project_id: $pid})
                    MERGE (f)-[:INVOLVES]->(e)
                """, {"fid": fs.id, "eid": eid, "pid": self.project_id})
        return fs

    def list_foreshadows(self, resolved: bool | None = None) -> list[Foreshadow]:
        if resolved is not None:
            rows = self._run(
                "MATCH (f:Fore {project_id: $pid}) WHERE f.resolved = $r RETURN f ORDER BY f.created_at",
                {"pid": self.project_id, "r": resolved}
            )
        else:
            rows = self._run(
                "MATCH (f:Fore {project_id: $pid}) RETURN f ORDER BY f.created_at",
                {"pid": self.project_id}
            )
        result = []
        for row in rows:
            n = row["f"]
            result.append(Foreshadow(
                id=n["id"], text=n.get("text", ""), hint=n.get("hint", ""),
                expected_resolution=n.get("expected_resolution", ""),
                resolved=n.get("resolved", False),
                resolution_text=n.get("resolution_text", ""),
                related_entities=json.loads(n.get("data", "{}")).get("related_entities", []) if n.get("data") else [],
                related_events=json.loads(n.get("data", "{}")).get("related_events", []) if n.get("data") else [],
            ))
        return result

    def resolve_foreshadow(self, fs_id: str, resolution_text: str) -> bool:
        self._run(
            "MATCH (f:Fore {id: $id, project_id: $pid}) SET f.resolved = true, f.resolution_text = $rt",
            {"id": fs_id, "pid": self.project_id, "rt": resolution_text}
        )
        return True

    # ── Snapshots ──

    def add_snapshot(self, snapshot: CharacterSnapshot) -> CharacterSnapshot:
        self._run("""
            CREATE (s:Snapshot {
                id: $sid, character_id: $cid, time_point: $tp,
                time_order: $to, label: $label,
                data: $data, description: $desc, project_id: $pid,
                phase: $phase, phase_key: $phase_key,
                is_current: $is_current
            })
        """, {
            "sid": snapshot.id, "cid": snapshot.character_entity_id,
            "tp": snapshot.time_point, "to": snapshot.time_order,
            "label": snapshot.label,
            "data": json.dumps(snapshot.data, ensure_ascii=False),
            "desc": snapshot.description, "pid": self.project_id,
            "phase": snapshot.phase or "",
            "phase_key": snapshot.phase_key or "",
            "is_current": bool(snapshot.is_current),
        })
        # P0-3: Create HAS_PHASE edge from entity to snapshot
        if snapshot.character_entity_id:
            self._run("""
                MATCH (e:Entity {id: $cid, project_id: $pid})
                MATCH (s:Snapshot {id: $sid, project_id: $pid})
                MERGE (e)-[:HAS_PHASE]->(s)
            """, {"cid": snapshot.character_entity_id, "sid": snapshot.id, "pid": self.project_id})
        # Only one phase can be "current" per character at a time.
        if snapshot.is_current:
            self._run("""
                MATCH (s:Snapshot {character_id: $cid, project_id: $pid})
                WHERE s.id <> $sid AND s.is_current = true
                SET s.is_current = false
            """, {"cid": snapshot.character_entity_id,
                  "sid": snapshot.id, "pid": self.project_id})
        self._run("""
            MATCH (e:Entity {id: $cid, project_id: $pid})
            MATCH (s:Snapshot {id: $sid, project_id: $pid})
            MERGE (e)-[:HAS_SNAPSHOT]->(s)
        """, {"cid": snapshot.character_entity_id, "sid": snapshot.id, "pid": self.project_id})
        return snapshot

    def update_snapshot(self, snapshot_id: str, updates: dict) -> bool:
        """Partial update of a Snapshot node. Only provided keys are touched.

        ``data`` must be a full dict (will be JSON-serialized). Other keys
        (phase, phase_key, is_current, label, description, time_point,
        time_order) are primitive and set as-is.

        When ``is_current`` is flipped to true, all other snapshots of the
        same character are cleared of that flag.
        """
        if not updates:
            return False
        primitive_keys = [
            "phase", "phase_key",
            "label", "description", "time_point",
        ]
        set_clauses: list[str] = []
        params: dict = {"sid": snapshot_id, "pid": self.project_id}
        for k in primitive_keys:
            if k in updates:
                set_clauses.append(f"s.{k} = ${k}")
                params[k] = updates[k] if updates[k] is not None else ""
        if "time_order" in updates and updates["time_order"] is not None:
            set_clauses.append("s.time_order = $time_order")
            params["time_order"] = int(updates["time_order"])
        if "data" in updates and isinstance(updates["data"], dict):
            set_clauses.append("s.data = $data")
            params["data"] = json.dumps(updates["data"], ensure_ascii=False)
        if "is_current" in updates and updates["is_current"] is not None:
            set_clauses.append("s.is_current = $is_current")
            params["is_current"] = bool(updates["is_current"])
        if not set_clauses:
            return False
        self._run(f"MATCH (s:Snapshot {{id: $sid, project_id: $pid}}) SET {', '.join(set_clauses)}", params)
        if params.get("is_current"):
            # Find this snapshot's character_id, then clear is_current on siblings.
            rows = self._run(
                "MATCH (s:Snapshot {id: $sid, project_id: $pid}) RETURN s.character_id AS cid",
                {"sid": snapshot_id, "pid": self.project_id},
            )
            if rows and rows[0].get("cid"):
                self._run("""
                    MATCH (o:Snapshot {character_id: $cid, project_id: $pid})
                    WHERE o.id <> $sid AND o.is_current = true
                    SET o.is_current = false
                """, {"cid": rows[0]["cid"], "sid": snapshot_id, "pid": self.project_id})
        return True

    def list_snapshots(self, character_entity_id: str | None = None,
                       time_point: str | None = None) -> list[CharacterSnapshot]:
        if character_entity_id:
            rows = self._run(
                "MATCH (s:Snapshot {character_id: $cid, project_id: $pid}) RETURN s ORDER BY s.time_order",
                {"cid": character_entity_id, "pid": self.project_id}
            )
        elif time_point:
            rows = self._run(
                "MATCH (s:Snapshot {time_point: $tp, project_id: $pid}) RETURN s ORDER BY s.time_order",
                {"tp": time_point, "pid": self.project_id}
            )
        else:
            rows = self._run(
                "MATCH (s:Snapshot {project_id: $pid}) RETURN s ORDER BY s.time_order",
                {"pid": self.project_id}
            )
        return [self._row_to_snapshot(r["s"]) for r in rows]

    def _row_to_snapshot(self, n) -> CharacterSnapshot:
        phase = n.get("phase")
        # Lazy backfill: snapshots created with the legacy schema have no
        # phase field at all. Distinguish from an explicitly-empty phase so
        # the frontend can label them "未分阶段".
        if phase is None:
            phase = "未分阶段"
        return CharacterSnapshot(
            id=n["id"], character_entity_id=n.get("character_id", ""),
            time_point=n.get("time_point", ""),
            time_order=n.get("time_order", 0),
            label=n.get("label", ""),
            data=json.loads(n.get("data", "{}") or "{}"),
            description=n.get("description", ""),
            phase=phase,
            phase_key=n.get("phase_key", "") or "",
            is_current=bool(n.get("is_current", False)),
        )

    def get_current_phase(self, character_entity_id: str) -> CharacterSnapshot | None:
        """Return the current phase card for a character, or None.

        Phase selection is **order-based and decoupled from chapters**:
        1. If any snapshot has ``is_current=True``, return it (the latest one
           if multiple, defensively).
        2. Otherwise fall back to the snapshot with the highest ``time_order``
           (the most recent phase in the arc timeline).
        3. Returns None when the character has no phase snapshots at all.
        """
        snaps = self.list_snapshots(character_entity_id=character_entity_id)
        if not snaps:
            return None
        current = [s for s in snaps if s.is_current]
        if current:
            current.sort(key=lambda s: s.time_order, reverse=True)
            return current[0]
        snaps.sort(key=lambda s: s.time_order, reverse=True)
        return snaps[0]

    def delete_snapshot(self, snapshot_id: str) -> bool:
        self._run("MATCH (s:Snapshot {id: $id, project_id: $pid}) DETACH DELETE s",
                  {"id": snapshot_id, "pid": self.project_id})
        return True

    # ── Timeline ──

    def add_timeline_event(self, event: TimelineEvent) -> TimelineEvent:
        self._run("""
            CREATE (t:Timeline {
                id: $id, time_point: $tp, label: $label,
                time_order: $to, description: $desc,
                chapter_ref: $cr, event_entity_id: $eid,
                project_id: $pid
            })
        """, {
            "id": event.id, "tp": event.time_point, "label": event.label,
            "to": event.time_order, "desc": event.description,
            "cr": event.chapter_ref, "eid": event.event_entity_id,
            "pid": self.project_id,
        })
        # P0-2: Create INVOLVES edge from timeline event to entity
        if event.event_entity_id:
            self._run("""
                MATCH (t:Timeline {id: $tid, project_id: $pid})
                MATCH (e:Entity {id: $eid, project_id: $pid})
                MERGE (t)-[:INVOLVES]->(e)
            """, {"tid": event.id, "eid": event.event_entity_id, "pid": self.project_id})
        return event

    def list_timeline_events(self) -> list[TimelineEvent]:
        rows = self._run(
            "MATCH (t:Timeline {project_id: $pid}) RETURN t ORDER BY t.time_order",
            {"pid": self.project_id}
        )
        return [TimelineEvent(
            id=r["t"]["id"], time_point=r["t"].get("time_point", ""),
            label=r["t"].get("label", ""),
            time_order=r["t"].get("time_order", 0),
            description=r["t"].get("description", ""),
            chapter_ref=r["t"].get("chapter_ref", ""),
            event_entity_id=r["t"].get("event_entity_id", ""),
        ) for r in rows]

    def delete_timeline_event(self, event_id: str) -> bool:
        self._run("MATCH (t:Timeline {id: $id, project_id: $pid}) DELETE t",
                  {"id": event_id, "pid": self.project_id})
        return True

    def get_all_time_points(self) -> list[dict]:
        rows = self._run(
            "MATCH (t:Timeline {project_id: $pid}) RETURN t.label as label, t.time_point as tp, t.time_order as to ORDER BY to",
            {"pid": self.project_id}
        )
        return [{"time_point": r["tp"], "label": r["label"]} for r in rows]

    # ── Consistency Check ──

    def check_consistency(self) -> dict:
        """Run deterministic Cypher queries to find factual contradictions.

        Returns:
            dict with:
              - contradictions: list of deterministic conflicts found
              - stats: entity/relation/foreshadow counts for LLM semantic check
        """
        issues = []

        # 1. Location conflict: same entity located_at two different places
        rows = self._run("""
            MATCH (e:Entity {project_id: $pid})-[r1:LOCATED_AT]->(loc1:Entity {project_id: $pid})
            MATCH (e)-[r2:LOCATED_AT]->(loc2:Entity {project_id: $pid})
            WHERE loc1.id <> loc2.id
            RETURN e.name as entity, loc1.name as loc_a, loc2.name as loc_b
        """, {"pid": self.project_id})
        for r in rows:
            issues.append({
                "type": "location_conflict",
                "severity": "high",
                "description": f"实体「{r['entity']}」同时位于「{r['loc_a']}」和「{r['loc_b']}」",
            })

        # 2. Temporal contradiction: A before B and A after B
        rows = self._run("""
            MATCH (a:Entity {project_id: $pid})-[r1:BEFORE]->(b:Entity {project_id: $pid})
            MATCH (b)-[r2:BEFORE]->(a)
            WHERE a.id <> b.id
            RETURN a.name as ea, b.name as eb
        """, {"pid": self.project_id})
        for r in rows:
            issues.append({
                "type": "temporal_conflict",
                "severity": "high",
                "description": f"时序矛盾: 「{r['ea']}」先于「{r['eb']}」又后于「{r['eb']}」",
            })

        # 3. Relationship contradiction: antagonist AND ally for same pair
        rows = self._run("""
            MATCH (a:Entity {project_id: $pid})-[r]-(b:Entity {project_id: $pid})
            WITH a, b, collect(type(r)) as types
            WHERE size(types) > 1 AND
                  (('ANTAGONIST' IN types AND 'ALLY' IN types) OR
                   ('ANTAGONIST' IN types AND 'FAMILY' IN types))
            RETURN a.name as ea, b.name as eb, types
        """, {"pid": self.project_id})
        for r in rows:
            types_str = ", ".join(r["types"])
            issues.append({
                "type": "relationship_conflict",
                "severity": "medium",
                "description": f"关系矛盾: 「{r['ea']}」↔「{r['eb']}」同时具有关系 {types_str}",
            })

        # 4. Owner without owned entity (orphan OWNS)
        rows = self._run("""
            MATCH (o:Entity {project_id: $pid})-[r:OWNS]->(i:Entity {project_id: $pid})
            WHERE i.entity_type = 'item' AND i.name = ''
            RETURN o.name as owner, i.id as orphan_id
        """, {"pid": self.project_id})
        for r in rows:
            issues.append({
                "type": "orphan_relation",
                "severity": "low",
                "description": f"「{r['owner']}」拥有一个未命名的物品({r['orphan_id'][:8]})",
            })

        # ── P1-1: Path-aware consistency checks (activates graph traversal) ──

        # 5. Isolated entities — no edges at all
        rows = self._run("""
            MATCH (e:Entity {project_id: $pid})
            WHERE NOT (e)-[]-()
            RETURN e.name as name, e.entity_type as type
        """, {"pid": self.project_id})
        for r in rows:
            issues.append({
                "type": "isolated_entity",
                "severity": "medium",
                "description": f"实体「{r['name']}」（{r.get('type', '?')}）无任何关系连接，是完全孤立的",
            })

        # 6. Unresolved foreshadows without entity links (P0-1 edge missing)
        rows = self._run("""
            MATCH (f:Fore {project_id: $pid, resolved: false})
            WHERE NOT (f)-[:INVOLVES]->()
            RETURN f.text as text, f.id as fid
        """, {"pid": self.project_id})
        for r in rows:
            issues.append({
                "type": "unlinked_foreshadow",
                "severity": "low",
                "description": f"未回收伏笔「{r['text'][:30]}…」未关联任何实体，无法图查询追踪",
            })

        # 7. Disconnected character pairs — no relationship path within 3 hops
        rows = self._run("""
            MATCH (a:Entity:Character {project_id: $pid}),
                  (b:Entity:Character {project_id: $pid})
            WHERE a.id < b.id
            AND NOT EXISTS {
                MATCH path = shortestPath(
                    (a)-[:KNOWS|ALLY|FAMILY|ANTAGONIST|ROMANTIC|MASTER_OF|MENTOR_OF|KILLED|SAVED|LOVES*1..3]-(b)
                )
            }
            RETURN a.name as name_a, b.name as name_b
            LIMIT 10
        """, {"pid": self.project_id})
        for r in rows:
            issues.append({
                "type": "disconnected_characters",
                "severity": "low",
                "description": f"角色「{r['name_a']}」与「{r['name_b']}」在关系图中无任何路径连接",
            })

        stats = {
            "entity_count": len(self.list_entities()),
            "relation_count": len(self.list_relations()),
            "foreshadow_count": len(self.list_foreshadows()),
            "issues_found": len(issues),
        }

        return {"contradictions": issues, "stats": stats}

    # ── P1-3: Bridge character discovery (activates get_neighbors + path analysis) ──

    def find_bridge_characters(self) -> list[dict]:
        """Find bridge characters whose removal would disconnect relationship paths.

        Uses approximate betweenness: finds characters that are the sole intermediary
        between two otherwise-disconnected characters.
        """
        rows = self._run("""
            MATCH (a:Entity:Character {project_id: $pid}),
                  (b:Entity:Character {project_id: $pid})
            WHERE a.id < b.id
            AND NOT (a)-[:KNOWS|ALLY|FAMILY|ANTAGONIST|ROMANTIC|MASTER_OF|MENTOR_OF|KILLED|SAVED|LOVES]-(b)
            MATCH path = shortestPath(
                (a)-[:KNOWS|ALLY|FAMILY|ANTAGONIST|ROMANTIC|MASTER_OF|MENTOR_OF|KILLED|SAVED|LOVES*1..4]-(b)
            )
            WHERE length(path) = 2
            RETURN nodes(path)[1] as bridge, collect([a.name, b.name]) as pairs
        """, {"pid": self.project_id})
        result = []
        for r in rows:
            bridge = r["bridge"]
            pairs = r["pairs"]
            result.append({
                "entity_id": bridge["id"],
                "entity_name": bridge.get("name", ""),
                "bridge_count": len(pairs),
                "would_disconnect": pairs[:5],
                "warning": f"移除「{bridge.get('name', '')}」将断开 {len(pairs)} 对角色之间的关系路径",
            })
        return result

    # ── P1-4: Causal chain rewrite protection (activates graph reachability) ──

    def find_downstream_impact(self, event_id: str) -> dict:
        """Find all downstream elements affected by modifying a timeline event.

        Traverses the graph from the given event to find:
        - Later timeline events involving the same entities
        - Unresolved foreshadows involving the same entities
        """
        result = {"affected_events": [], "affected_foreshadows": [], "affected_entities": []}
        event = self._run_single(
            "MATCH (t:Timeline {id: $tid, project_id: $pid}) RETURN t",
            {"tid": event_id, "pid": self.project_id}
        )
        if not event:
            return result
        time_order = event["t"].get("time_order", 0)
        # Find entities involved in this event (via P0-2 INVOLVES edges)
        rows = self._run("""
            MATCH (t:Timeline {id: $tid, project_id: $pid})-[:INVOLVES]->(e:Entity)
            RETURN e.id as eid, e.name as ename
        """, {"tid": event_id, "pid": self.project_id})
        entity_ids = [r["eid"] for r in rows]
        entity_names = [r["ename"] for r in rows]
        if not entity_ids:
            return result
        # Find later timeline events involving the same entities
        later = self._run("""
            MATCH (t2:Timeline {project_id: $pid})-[:INVOLVES]->(e:Entity)
            WHERE e.id IN $eids AND t2.time_order > $to
            RETURN DISTINCT t2.id as tid, t2.label as label,
                   t2.chapter_ref as cr, t2.time_order as to2
            ORDER BY t2.time_order
        """, {"eids": entity_ids, "to": time_order, "pid": self.project_id})
        result["affected_events"] = [
            {"id": r["tid"], "label": r["label"], "chapter_ref": r["cr"]}
            for r in later
        ]
        # Find unresolved foreshadows involving the same entities (via P0-1 INVOLVES)
        fores = self._run("""
            MATCH (f:Fore {project_id: $pid, resolved: false})-[:INVOLVES]->(e:Entity)
            WHERE e.id IN $eids
            RETURN DISTINCT f.id as fid, f.text as text
        """, {"eids": entity_ids, "pid": self.project_id})
        result["affected_foreshadows"] = [
            {"id": r["fid"], "text": r["text"]} for r in fores
        ]
        result["affected_entities"] = entity_names
        return result

    # ── P1-5: Chapter entity coverage analysis (forgotten character tracking) ──

    def find_forgotten_characters(self, current_time_order: int = 0,
                                  threshold: int = 5) -> list[dict]:
        """Find characters who haven't appeared in recent timeline events.

        Args:
            current_time_order: The current timeline position.
            threshold: Steps without appearance to be considered "forgotten".
        """
        rows = self._run("""
            MATCH (e:Entity:Character {project_id: $pid})
            OPTIONAL MATCH (t:Timeline)-[:INVOLVES]->(e)
            WITH e, max(t.time_order) as last_appearance
            WHERE last_appearance IS NULL
               OR last_appearance < $current - $threshold
            RETURN e.id as eid, e.name as name,
                   coalesce(last_appearance, -1) as last_seen,
                   e.data as data
            ORDER BY last_seen ASC
        """, {"pid": self.project_id, "current": current_time_order,
               "threshold": threshold})
        result = []
        for r in rows:
            data = r.get("data", "{}")
            if isinstance(data, str):
                data = json.loads(data) if data else {}
            elif data is None:
                data = {}
            result.append({
                "entity_id": r["eid"],
                "name": r["name"],
                "last_seen_time_order": r["last_seen"],
                "important": bool(data.get("important") or data.get("role")),
                "steps_absent": (current_time_order - r["last_seen"]
                                 if r["last_seen"] >= 0 else None),
            })
        return result

    # ── P2-11: Foreshadow dependency graph (DEPENDS_ON edges + cycle detection) ──

    def add_foreshadow_dependency(self, from_id: str, to_id: str) -> bool:
        """Create a DEPENDS_ON edge: foreshadow `from_id` depends on `to_id`.

        `to_id` must be resolved before `from_id` makes sense narratively.
        """
        self._run("""
            MATCH (f1:Fore {id: $fid, project_id: $pid})
            MATCH (f2:Fore {id: $tid, project_id: $pid})
            MERGE (f1)-[:DEPENDS_ON]->(f2)
        """, {"fid": from_id, "tid": to_id, "pid": self.project_id})
        return True

    def detect_foreshadow_cycles(self) -> list[dict]:
        """Detect circular dependencies in the foreshadow dependency graph."""
        rows = self._run("""
            MATCH path = (f:Fore {project_id: $pid})-[:DEPENDS_ON*1..10]->(f)
            RETURN f.id as fid, f.text as text,
                   [n in nodes(path) | n.id] as cycle_ids
        """, {"pid": self.project_id})
        return [
            {"id": r["fid"], "text": r["text"], "cycle": r["cycle_ids"]}
            for r in rows
        ]

    def get_foreshadow_resolution_order(self) -> list[dict]:
        """Generate topological sort of foreshadow resolution order.

        Foreshadows with no unresolved dependencies come first.
        Detects cycles and flags remaining foreshadows as cyclic.
        """
        fores = self.list_foreshadows(resolved=False)
        if not fores:
            return []
        dep_rows = self._run("""
            MATCH (f:Fore {project_id: $pid, resolved: false})-[:DEPENDS_ON]->(dep:Fore {resolved: false})
            RETURN f.id as fid, dep.id as depid
        """, {"pid": self.project_id})
        deps = {f.id: set() for f in fores}
        for r in dep_rows:
            deps[r["fid"]].add(r["depid"])
        # Kahn's algorithm for topological sort
        result = []
        fs_by_id = {f.id: f for f in fores}
        resolved_set = set()
        while len(resolved_set) < len(fores):
            ready = [
                fid for fid in deps
                if fid not in resolved_set
                and deps[fid].issubset(resolved_set)
            ]
            if not ready:
                cyclic = [fid for fid in deps if fid not in resolved_set]
                for fid in cyclic:
                    f = fs_by_id.get(fid)
                    result.append({
                        "id": fid, "text": f.text if f else "",
                        "warning": "存在循环依赖，无法确定回收顺序",
                    })
                break
            for fid in ready:
                f = fs_by_id.get(fid)
                result.append({
                    "id": fid, "text": f.text if f else "",
                    "dependencies": list(deps[fid]),
                })
                resolved_set.add(fid)
        return result

    # ── P2-13: Missing relationship detection (activates get_path) ──

    def find_missing_relations(self, entity_ids: list[str]) -> list[dict]:
        """Detect pairs of entities with no relationship path between them.

        Useful for checking if characters in the same scene have some
        connection (even indirect) in the relationship graph.
        """
        from itertools import combinations
        result = []
        for a, b in combinations(entity_ids, 2):
            path = self.get_path(a, b, max_depth=3)
            if not path:
                ea = self.get_entity(a)
                eb = self.get_entity(b)
                result.append({
                    "entity_a": {"id": a, "name": ea.name if ea else a},
                    "entity_b": {"id": b, "name": eb.name if eb else b},
                    "warning": f"「{ea.name if ea else a}」与「{eb.name if eb else b}」之间无任何关系路径",
                })
        return result

    # ── P2-14: Worldbuilding completeness metrics ──

    def get_worldbuilding_metrics(self) -> dict:
        """Compute graph topology metrics for worldbuilding health assessment."""
        entity_count = len(self.list_entities())
        relation_count = len(self.list_relations())
        max_edges = entity_count * (entity_count - 1) / 2 if entity_count > 1 else 1
        density = relation_count / max_edges if max_edges > 0 else 0
        isolated = self._run("""
            MATCH (e:Entity {project_id: $pid})
            WHERE NOT (e)-[]-()
            RETURN e.name as name, e.entity_type as type
        """, {"pid": self.project_id})
        components = self._run("""
            MATCH (e:Entity {project_id: $pid})
            OPTIONAL MATCH (e)-[*1..3]-(connected:Entity {project_id: $pid})
            WITH e, collect(DISTINCT connected.id) as component
            RETURN e.id as eid, e.name as name, size(component) as comp_size
            ORDER BY comp_size DESC
        """, {"pid": self.project_id})
        largest = max((r["comp_size"] for r in components), default=0)
        frag_ratio = round(1 - (largest / entity_count), 3) if entity_count > 0 else 0
        return {
            "entity_count": entity_count,
            "relation_count": relation_count,
            "density": round(density, 3),
            "isolated_entities": [
                {"name": r["name"], "type": r.get("type", "")} for r in isolated
            ],
            "isolated_count": len(isolated),
            "largest_component_size": largest,
            "fragmentation_ratio": frag_ratio,
            "health_assessment": (
                "良好" if density > 0.15 and len(isolated) == 0
                else "一般" if density > 0.05
                else "稀疏"
            ),
        }

    # ── P2-15: Character perspective subgraph (POV-aware) ──

    def get_pov_subgraph(self, character_id: str) -> dict:
        """Return the subgraph visible from a character's perspective.

        Includes direct relationships, entities from shared timeline events,
        and foreshadows involving this character.
        """
        direct = self._run("""
            MATCH (c:Entity {id: $cid, project_id: $pid})-[r]-(other:Entity {project_id: $pid})
            RETURN DISTINCT other.id as oid, other.name as name,
                   other.entity_type as type, type(r) as rel_type
        """, {"cid": character_id, "pid": self.project_id})
        event_entities = self._run("""
            MATCH (t:Timeline {project_id: $pid})-[:INVOLVES]->(c:Entity {id: $cid})
            MATCH (t)-[:INVOLVES]->(other:Entity)
            WHERE other.id <> $cid
            RETURN DISTINCT other.id as oid, other.name as name,
                   other.entity_type as type
        """, {"cid": character_id, "pid": self.project_id})
        char_fores = self._run("""
            MATCH (f:Fore {project_id: $pid})-[:INVOLVES]->(c:Entity {id: $cid})
            RETURN f.id as fid, f.text as text, f.resolved as resolved
        """, {"cid": character_id, "pid": self.project_id})
        nodes = {}
        for r in direct:
            nodes[r["oid"]] = {
                "id": r["oid"], "name": r["name"],
                "type": r.get("type", ""), "connection": "direct",
            }
        for r in event_entities:
            if r["oid"] not in nodes:
                nodes[r["oid"]] = {
                    "id": r["oid"], "name": r["name"],
                    "type": r.get("type", ""), "connection": "event",
                }
        return {
            "pov_character_id": character_id,
            "visible_entities": list(nodes.values()),
            "visible_foreshadows": [
                {"id": r["fid"], "text": r["text"], "resolved": r["resolved"]}
                for r in char_fores
            ],
            "visibility_scope": len(nodes),
        }

    # ── P2-6: Character knowledge horizon (time-annotated edges) ──

    def add_temporal_relation(self, from_id: str, to_id: str, rel_type: str,
                              since_chapter: int) -> bool:
        """Create a time-annotated relationship edge.

        The since_chapter property records when this relationship was
        established, enabling time-aware context filtering.
        """
        rel_upper = rel_type.upper()
        self._run(f"""
            MATCH (a:Entity {{id: $from_id, project_id: $pid}})
            MATCH (b:Entity {{id: $to_id, project_id: $pid}})
            MERGE (a)-[r:{rel_upper}]->(b)
            SET r.since_chapter = $chapter, r.project_id = $pid
        """, {"from_id": from_id, "to_id": to_id,
              "chapter": since_chapter, "pid": self.project_id})
        return True

    def get_character_knowledge(self, character_id: str, at_chapter: int) -> dict:
        """Query what a character knows at a given chapter.

        Returns relationships and entities the character was aware of
        up to and including the given chapter number.
        """
        rels = self._run("""
            MATCH (c:Entity {id: $cid, project_id: $pid})-[r]-(other:Entity {project_id: $pid})
            WHERE r.since_chapter IS NULL OR r.since_chapter <= $chapter
            RETURN other.id as oid, other.name as name,
                   other.entity_type as type, type(r) as rel_type,
                   r.since_chapter as since
        """, {"cid": character_id, "chapter": at_chapter, "pid": self.project_id})
        known_entities = [
            {"id": r["oid"], "name": r["name"],
             "type": r.get("type", ""),
             "relationship": r["rel_type"],
             "known_since_chapter": r.get("since")}
            for r in rels
        ]
        events = self._run("""
            MATCH (t:Timeline {project_id: $pid})-[:INVOLVES]->(c:Entity {id: $cid})
            WHERE t.chapter_ref IS NOT NULL
            AND t.chapter_ref =~ '#[0-9]+'
            AND toInteger(replace(t.chapter_ref, '#', '')) <= $chapter
            RETURN t.id as tid, t.label as label, t.chapter_ref as cr
            ORDER BY t.time_order
        """, {"cid": character_id, "chapter": at_chapter, "pid": self.project_id})
        known_events = [
            {"id": r["tid"], "label": r["label"], "chapter": r["cr"]}
            for r in events
        ]
        return {
            "character_id": character_id,
            "at_chapter": at_chapter,
            "known_entities": known_entities,
            "known_events": known_events,
        }

    # ── 4D Map: Time-aware entity state query ──

    def get_entity_state_at_time(self, entity_id: str, time_order: int) -> dict:
        """Get an entity's complete state at a specific timeline position.

        Returns phase, relationships, location, events, and active foreshadows
        filtered to the given time_order.
        """
        entity = self.get_entity(entity_id)
        if not entity:
            return {"error": "Entity not found"}
        result = {
            "entity_id": entity_id, "entity_name": entity.name,
            "entity_type": entity.type, "at_time_order": time_order,
        }
        # Phase: snapshot with largest time_order <= T
        if entity.type == EntityType.CHARACTER:
            phase_row = self._run_single("""
                MATCH (e:Entity {id: $eid, project_id: $pid})-[:HAS_PHASE]->(s:Snapshot)
                WHERE s.time_order <= $to
                RETURN s ORDER BY s.time_order DESC LIMIT 1
            """, {"eid": entity_id, "to": time_order, "pid": self.project_id})
            if phase_row:
                snap = self._row_to_snapshot(phase_row["s"])
                result["phase"] = {
                    "phase": snap.phase, "label": snap.label,
                    "data": snap.data, "description": snap.description,
                }
        # Relationships established by this time
        rels = self._run("""
            MATCH (e:Entity {id: $eid, project_id: $pid})-[r]-(other:Entity {project_id: $pid})
            WHERE r.since_chapter IS NULL OR r.since_chapter <= $to
            RETURN other.id as oid, other.name as oname, other.entity_type as otype,
                   type(r) as rel_type, r.since_chapter as since
        """, {"eid": entity_id, "to": time_order, "pid": self.project_id})
        result["relationships"] = [
            {"entity_id": r["oid"], "name": r["oname"], "type": r.get("otype", ""),
             "relationship": r["rel_type"], "since_chapter": r.get("since")}
            for r in rels
        ]
        # Location at this time
        loc_row = self._run_single("""
            MATCH (e:Entity {id: $eid, project_id: $pid})-[r:LOCATED_AT]->(loc:Entity {project_id: $pid})
            RETURN loc.id as lid, loc.name as lname
        """, {"eid": entity_id, "pid": self.project_id})
        if loc_row:
            result["location"] = {"id": loc_row["lid"], "name": loc_row["lname"]}
        # Timeline events up to this time
        events = self._run("""
            MATCH (t:Timeline {project_id: $pid})-[:INVOLVES]->(e:Entity {id: $eid})
            WHERE t.time_order <= $to
            RETURN t.id as tid, t.label as label, t.time_order as to2,
                   t.chapter_ref as cr, t.description as desc
            ORDER BY t.time_order
        """, {"eid": entity_id, "to": time_order, "pid": self.project_id})
        result["events"] = [
            {"id": r["tid"], "label": r["label"], "time_order": r["to2"],
             "chapter_ref": r["cr"], "description": r.get("desc", "")}
            for r in events
        ]
        # Active foreshadows at this time
        fores = self._run("""
            MATCH (f:Fore {project_id: $pid, resolved: false})-[:INVOLVES]->(e:Entity {id: $eid})
            RETURN f.id as fid, f.text as text
        """, {"eid": entity_id, "pid": self.project_id})
        result["active_foreshadows"] = [
            {"id": r["fid"], "text": r["text"]} for r in fores
        ]
        return result

    def get_map_at_time(self, time_order: int) -> dict:
        """Get the location map with character positions at a specific timeline position.

        Returns locations, which characters are at each location, and all
        timeline events at this time.
        """
        # All location entities
        locs = self._run("""
            MATCH (loc:Entity:Location {project_id: $pid})
            RETURN loc.id as lid, loc.name as lname, loc.data as data
        """, {"pid": self.project_id})
        locations = []
        for r in locs:
            data = r.get("data", "{}")
            if isinstance(data, str):
                data = json.loads(data) if data else {}
            locations.append({
                "id": r["lid"], "name": r["lname"],
                "type": data.get("locationType", data.get("type", "other")),
                "data": data,
            })
        # Characters at each location at this time
        char_locs = self._run("""
            MATCH (t:Timeline {project_id: $pid, time_order: $to})
            MATCH (t)-[:INVOLVES]->(e:Entity:Character {project_id: $pid})
            OPTIONAL MATCH (e)-[:LOCATED_AT]->(loc:Entity {project_id: $pid})
            RETURN e.id as cid, e.name as cname,
                   loc.id as lid, loc.name as lname
        """, {"to": time_order, "pid": self.project_id})
        characters_at_locations = {}
        for r in char_locs:
            lid = r.get("lid") or "unknown"
            if lid not in characters_at_locations:
                characters_at_locations[lid] = {
                    "location_name": r.get("lname") or "未知",
                    "characters": [],
                }
            characters_at_locations[lid]["characters"].append({
                "id": r["cid"], "name": r["cname"],
            })
        # Events at this time
        events_at = self._run("""
            MATCH (t:Timeline {project_id: $pid, time_order: $to})
            RETURN t.id as tid, t.label as label, t.chapter_ref as cr,
                   t.description as desc
        """, {"to": time_order, "pid": self.project_id})
        return {
            "at_time_order": time_order,
            "locations": locations,
            "characters_at_locations": characters_at_locations,
            "events_at_time": [
                {"id": r["tid"], "label": r["label"],
                 "chapter_ref": r["cr"], "description": r.get("desc", "")}
                for r in events_at
            ],
        }

    # ── Full graph: Complete book visualization ──

    def get_full_graph(self, at_time_order: int | None = None) -> dict:
        """Return the complete graph — all nodes and edges for full-book visualization.

        Includes Entity nodes, Timeline nodes, Foreshadow nodes, and all edges
        between them (relationships, INVOLVES, HAS_PHASE).

        When ``at_time_order`` is set, only timeline nodes with time_order <= T
        are included, and edges are filtered to those established by that time.
        """
        node_set = {}
        edges = []
        time_filter = at_time_order is not None
        params: dict = {"pid": self.project_id}
        if time_filter:
            params["to"] = at_time_order
        # All entities (always included — they exist across time)
        entity_rows = self._run("""
            MATCH (e:Entity {project_id: $pid})
            RETURN e.id as id, e.name as name, e.entity_type as type, e.data as data
        """, {"pid": self.project_id})
        for r in entity_rows:
            data = r.get("data", "{}")
            if isinstance(data, str):
                data = json.loads(data) if data else {}
            node_set[r["id"]] = {
                "id": r["id"], "name": r["name"],
                "type": r.get("type", "unknown"), "data": data,
            }
        # All entity-entity relationships (filter by timeline if time_filter)
        if time_filter:
            # Only include edges whose since_chapter corresponds to a timeline
            # event with time_order <= at_time_order, or edges with no since_chapter
            rel_rows = self._run("""
                MATCH (a:Entity {project_id: $pid})-[r]->(b:Entity {project_id: $pid})
                WHERE r.since_chapter IS NULL
                   OR r.since_chapter <= $to
                RETURN a.id as from_id, b.id as to_id, type(r) as rel_type,
                       r.since_chapter as since
            """, params)
        else:
            rel_rows = self._run("""
                MATCH (a:Entity {project_id: $pid})-[r]->(b:Entity {project_id: $pid})
                RETURN a.id as from_id, b.id as to_id, type(r) as rel_type,
                       r.since_chapter as since
            """, {"pid": self.project_id})
        for r in rel_rows:
            edges.append({
                "from": r["from_id"], "to": r["to_id"],
                "type": r["rel_type"], "since": r.get("since"),
            })
        # Timeline nodes + INVOLVES edges
        if time_filter:
            tl_rows = self._run("""
                MATCH (t:Timeline {project_id: $pid})
                WHERE t.time_order <= $to
                OPTIONAL MATCH (t)-[:INVOLVES]->(e:Entity {project_id: $pid})
                RETURN t.id as tid, t.label as label, t.chapter_ref as cr,
                       t.time_order as to2, t.description as desc,
                       collect(e.id) as eids
                ORDER BY t.time_order
            """, params)
        else:
            tl_rows = self._run("""
                MATCH (t:Timeline {project_id: $pid})
                OPTIONAL MATCH (t)-[:INVOLVES]->(e:Entity {project_id: $pid})
                RETURN t.id as tid, t.label as label, t.chapter_ref as cr,
                       t.time_order as to2, t.description as desc,
                       collect(e.id) as eids
                ORDER BY t.time_order
            """, {"pid": self.project_id})
        for r in tl_rows:
            node_set[r["tid"]] = {
                "id": r["tid"], "name": r["label"] or r["cr"] or "事件",
                "type": "timeline",
                "data": {
                    "chapter_ref": r["cr"], "time_order": r["to2"],
                    "description": r.get("desc", ""),
                },
            }
            for eid in r["eids"]:
                if eid:
                    edges.append({"from": r["tid"], "to": eid, "type": "TIMELINE_INVOLVES"})
        # Foreshadow nodes + INVOLVES edges (always include all)
        fs_rows = self._run("""
            MATCH (f:Fore {project_id: $pid})
            OPTIONAL MATCH (f)-[:INVOLVES]->(e:Entity {project_id: $pid})
            RETURN f.id as fid, f.text as text, f.resolved as resolved,
                   collect(e.id) as eids
        """, {"pid": self.project_id})
        for r in fs_rows:
            node_set[r["fid"]] = {
                "id": r["fid"], "name": (r["text"] or "伏笔")[:20],
                "type": "foreshadow",
                "data": {"resolved": r["resolved"]},
            }
            for eid in r["eids"]:
                if eid:
                    edges.append({"from": r["fid"], "to": eid, "type": "FORESHADOW_INVOLVES"})
        # HAS_PHASE edges (Entity → Snapshot) — filter by time_order
        if time_filter:
            phase_rows = self._run("""
                MATCH (e:Entity {project_id: $pid})-[:HAS_PHASE]->(s:Snapshot {project_id: $pid})
                WHERE s.time_order <= $to
                RETURN e.id as eid, s.id as sid, s.phase as phase, s.time_order as sto
            """, params)
        else:
            phase_rows = self._run("""
                MATCH (e:Entity {project_id: $pid})-[:HAS_PHASE]->(s:Snapshot {project_id: $pid})
                RETURN e.id as eid, s.id as sid, s.phase as phase, s.time_order as sto
            """, {"pid": self.project_id})
        for r in phase_rows:
            node_set[r["sid"]] = {
                "id": r["sid"], "name": r["phase"] or "阶段",
                "type": "snapshot", "data": {"time_order": r.get("sto", 0)},
            }
            edges.append({"from": r["eid"], "to": r["sid"], "type": "HAS_PHASE"})
        return {
            "nodes": list(node_set.values()),
            "edges": edges,
            "stats": {
                "node_count": len(node_set),
                "edge_count": len(edges),
            },
        }

    # ── Summary ──

    def get_knowledge_summary(self) -> str:
        entities = self.list_entities()
        relations = self.list_relations()
        self.list_foreshadows()

        lines = ["## 知识库总览\n"]
        for etype in EntityType.BUILTIN:
            type_entities = [e for e in entities if e.type == etype]
            if type_entities:
                lines.append(f"### {etype}（{len(type_entities)}个）")
                for e in type_entities:
                    aliases = f" (别名: {', '.join(e.aliases)})" if e.aliases else ""
                    lines.append(f"- **{e.name}**{aliases}")
                    for k, v in list(e.data.items())[:5]:
                        lines.append(f"  - {k}: {v}")
                lines.append("")
        if relations:
            lines.append(f"### 关系（{len(relations)}条）")
            for r in relations:
                fe = next((e.name for e in entities if e.id == r.from_entity), r.from_entity)
                te = next((e.name for e in entities if e.id == r.to_entity), r.to_entity)
                lines.append(f"- {fe} --[{r.type}]--> {te}")
        return "\n".join(lines)

    # ── P3: Graph insights for Autopilot ──

    def get_graph_insights(self) -> dict:
        """Generate actionable insights from graph analysis for Autopilot writing decisions.

        Returns:
            dict with:
              - forgotten_characters: characters not appearing recently
              - unresolved_foreshadows: foreshadows waiting for resolution
              - disconnected_pairs: character pairs with no relationship path
              - bridge_characters: key connectors in the relationship graph
              - underutilized_locations: locations with few events
              - suggestions: list of writing suggestions based on analysis
        """
        insights = {
            "forgotten_characters": [],
            "unresolved_foreshadows": [],
            "disconnected_pairs": [],
            "bridge_characters": [],
            "underutilized_locations": [],
            "suggestions": [],
        }

        # 1. Forgotten characters (not appeared in last 5 timeline events)
        timeline_events = self.list_timeline_events()
        if timeline_events:
            max_order = max(e.time_order for e in timeline_events)
            forgotten = self.find_forgotten_characters(max_order, threshold=5)
            important_forgotten = [c for c in forgotten if c.get("important")]
            insights["forgotten_characters"] = important_forgotten[:5]
            if important_forgotten:
                names = ", ".join(c["name"] for c in important_forgotten[:3])
                insights["suggestions"].append({
                    "type": "warning",
                    "priority": "high",
                    "message": f"重要角色已多章未出场：{names}。考虑在下一章让他们露面或提及。"
                })

        # 2. Unresolved foreshadows
        fores = self.list_foreshadows(resolved=False)
        if fores:
            insights["unresolved_foreshadows"] = [
                {"id": f.id, "text": f.text[:50], "related_entities": f.related_entities}
                for f in fores[:10]
            ]
            if len(fores) > 3:
                insights["suggestions"].append({
                    "type": "reminder",
                    "priority": "medium",
                    "message": f"有 {len(fores)} 个伏笔尚未回收，注意适时推进伏笔线。"
                })

        # 3. Disconnected character pairs
        chars = self.list_entities(entity_type="character")
        if len(chars) > 2 and len(chars) <= 30:  # Only for reasonable sizes
            char_ids = [c.id for c in chars]
            missing = self.find_missing_relations(char_ids)
            insights["disconnected_pairs"] = missing[:5]
            if missing:
                insights["suggestions"].append({
                    "type": "info",
                    "priority": "low",
                    "message": f"发现 {len(missing)} 对角色之间没有任何关系路径，可以考虑添加间接联系。"
                })

        # 4. Bridge characters
        bridges = self.find_bridge_characters()
        insights["bridge_characters"] = bridges[:5]
        if bridges:
            names = ", ".join(b["entity_name"] for b in bridges[:3])
            insights["suggestions"].append({
                "type": "info",
                "priority": "medium",
                "message": f"关键枢纽角色：{names}。这些角色连接多个关系链，修改时需谨慎。"
            })

        # 5. Underutilized locations
        locs = self.list_entities(entity_type="location")
        if locs:
            loc_usage = {}
            for loc in locs:
                events = self._run("""
                    MATCH (t:Timeline {project_id: $pid})-[:INVOLVES]->(e:Entity {id: $eid})
                    RETURN count(t) as cnt
                """, {"eid": loc.id, "pid": self.project_id})
                loc_usage[loc.name] = events[0]["cnt"] if events else 0
            unused = [name for name, cnt in loc_usage.items() if cnt == 0]
            if unused:
                insights["underutilized_locations"] = unused[:5]
                insights["suggestions"].append({
                    "type": "info",
                    "priority": "low",
                    "message": f"有 {len(unused)} 个地点从未在时间线事件中使用过：{', '.join(unused[:3])}。"
                })

        # ── P3-ext: Confidence scores (top 5 weakest + overall) ──
        from .narrative_logic import ConfidenceScorer
        scorer = ConfidenceScorer(self)
        all_scores = scorer.score_all()
        insights["confidence_scores"] = [
            {
                "entity_id": s.entity_id,
                "entity_name": s.entity_name,
                "entity_type": s.entity_type,
                "confidence": s.confidence,
                "stars": s.stars,
                "recommendation": s.recommendation,
            }
            for s in all_scores[:20]
        ]
        # Only flag the weakest (confidence < 0.3) as suggestions
        weak = [s for s in all_scores if s.confidence < 0.3]
        if weak:
            names = ", ".join(s.entity_name for s in weak[:3])
            insights["suggestions"].append({
                "type": "warning",
                "priority": "medium",
                "message": f"设定薄弱的实体：{names}。建议增加出场或关联。"
            })

        # ── P3-ext: Constraint violations ──
        from .narrative_logic import ConstraintChecker, ConstraintStore
        constraint_store = ConstraintStore(self)
        constraints = constraint_store.list(active_only=True)
        if constraints:
            checker = ConstraintChecker(self)
            violations = checker.check_all()
            insights["constraint_violations"] = [
                {
                    "constraint_id": v.constraint_id,
                    "description": v.description,
                    "severity": v.severity,
                    "violations": v.violations[:5],
                }
                for v in violations
            ]
            if violations:
                hard_count = sum(1 for v in violations if v.severity == "hard")
                insights["suggestions"].append({
                    "type": "warning",
                    "priority": "high",
                    "message": f"{len(violations)} 条叙事约束被违反（{hard_count} 条硬约束）。使用 check_constraints 查看详情。"
                })

        return insights

    @staticmethod
    def _row_to_entity(node) -> Entity:
        etype = EntityType(node.get("entity_type", "character"))
        aliases = node.get("aliases")
        if isinstance(aliases, str):
            aliases = json.loads(aliases)
        elif aliases is None:
            aliases = []
        data = node.get("data")
        if isinstance(data, str):
            data = json.loads(data)
        elif data is None:
            data = {}
        return Entity(
            id=node["id"],
            type=etype,
            name=node.get("name", ""),
            aliases=aliases,
            data=data,
        )


def get_store(book_id: str) -> GraphStore:
    store = GraphStore(book_id)
    store.init_schema()
    return store
