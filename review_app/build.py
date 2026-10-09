"""Compile Blueprint links and local Lean source into an immutable review snapshot.

This is a source locator, not a Lean parser or a proof checker. Unresolved names
stay visibly unresolved so a reviewer never mistakes a guess for evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from .snapshot_artifacts import candidate_output_path, write_candidate_artifact

NODE_ENVIRONMENTS = "definition theorem lemma proposition corollary remark example construction".split()
NODE_RE = re.compile(
    r"\\begin\{(" + "|".join(NODE_ENVIRONMENTS) + r")\}(?:\[([^]]*)\])?(.*?)\\end\{\1\}",
    re.DOTALL,
)
LABEL_RE = re.compile(r"\\label\{([^}]+)\}")
LEAN_RE = re.compile(r"\\lean\{([^}]+)\}")
USES_RE = re.compile(r"\\uses\{([^}]+)\}")
INPUT_RE = re.compile(r"\\input\{([^}]+)\}")
CHAPTER_RE = re.compile(r"\\chapter\{([^}]+)\}")
DECL_RE = re.compile(
    r"^\s*(?:@\[[^]]*\]\s*)*(?:(?:private|protected|noncomputable|partial|unsafe)\s+)*"
    r"(?:def|theorem|lemma|structure|class|abbrev|inductive|opaque|constant|instance)\s+"
    r"([\w'.«»₀-₉]+)(?=\s|\{|\(|:|$)"
)
NAMESPACE_RE = re.compile(r"^\s*namespace\s+([\w'.]+)\s*$")
END_RE = re.compile(r"^\s*end(?:\s+[\w'.]+)?\s*$")
SECTION_RE = re.compile(r"^\s*section(?:\s+[\w'.]+)?\s*$")
META_RE = re.compile(r"\\(?:label|lean|leanok|mathlibok|notready|uses|proves)\b(?:\{[^}]*\})?")
PROOF_RE = re.compile(r"\\begin\{proof\}.*?\\end\{proof\}", re.DOTALL)
SNAPSHOT_SCHEMA = "kip126-review-snapshot.v2"
LEGACY_SNAPSHOT_SCHEMA = "kip126-review-snapshot.v1"
CURRENT_FINGERPRINT_SCHEME = "kip126-review-content.v1"
LEGACY_FINGERPRINT_SCHEME = "kip126-review-legacy.v1"


def lean_references(body: str) -> list[str]:
    """Expand each Blueprint Lean tag's comma-separated declaration names."""
    return [name.strip() for group in LEAN_RE.findall(body)
            for name in group.split(",") if name.strip()]


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def calculate_snapshot_digest(snapshot: dict) -> str:
    if snapshot.get("schema") == LEGACY_SNAPSHOT_SCHEMA:
        # Keep the exact v1 digest contract so already deployed snapshots remain
        # readable. v2 additionally protects provenance fields used for audit.
        return _digest({"cards": snapshot.get("cards"), "schema": snapshot.get("schema")})
    protected = {
        "schema": snapshot.get("schema"),
        "fingerprint_scheme": snapshot.get("fingerprint_scheme"),
        "source_commit": snapshot.get("source_commit"),
        "source_dirty": snapshot.get("source_dirty"),
        "dependency_lock_digest": snapshot.get("dependency_lock_digest"),
        "unlinked_nodes": snapshot.get("unlinked_nodes"),
        "cards": snapshot.get("cards"),
    }
    for key in ('review_mode', 'review_contract', 'modules', 'source_origin', 'enrichment_topics',
                'repository', 'datasets', 'default_dataset', 'source_environment', 'context_modules',
                'reuse', 'reuse_disabled', 'reuse_input_digest'):
        if key in snapshot:
            protected[key] = snapshot[key]
    return _digest(protected)


def validate_snapshot(snapshot: dict) -> None:
    def reject_internal(value):
        if isinstance(value, dict):
            if 'expectation_assessment' in value:
                raise ValueError('internal Agent assessments must not enter reviewer snapshots')
            for item in value.values():
                reject_internal(item)
        elif isinstance(value, list):
            for item in value:
                reject_internal(item)
    reject_internal(snapshot)
    schema = snapshot.get("schema")
    from .repositories import COLLECTION_SCHEMA, dataset_id, repository_config
    if schema == COLLECTION_SCHEMA:
        if 'reuse_disabled' in snapshot and type(snapshot['reuse_disabled']) is not bool:
            raise ValueError('reuse_disabled must be a boolean')
        children = snapshot.get('datasets')
        if not isinstance(children, list) or not children or any(not isinstance(item, dict) for item in children):
            raise ValueError('collection datasets must be a non-empty list')
        for item in children:
            if item.get('schema') == COLLECTION_SCHEMA or item.get('review_mode') != 'statement' or 'repository' not in item:
                raise ValueError('collection children must be repository Statement snapshots')
            validate_snapshot(item)
        keys = [dataset_id(item) for item in children]
        if len(keys) != len(set(keys)) or snapshot.get('default_dataset') not in keys:
            raise ValueError('collection dataset IDs must be unique and include its default')
        default = children[keys.index(snapshot['default_dataset'])]
        if (snapshot.get('cards') != [] or snapshot.get('review_mode') != 'statement' or
                snapshot.get('source_commit') != default['source_commit'] or
                snapshot.get('source_dirty') != any(item['source_dirty'] for item in children) or
                snapshot.get('digest') != calculate_snapshot_digest(snapshot)):
            raise ValueError('invalid collection metadata or digest')
        return
    if schema not in {LEGACY_SNAPSHOT_SCHEMA, SNAPSHOT_SCHEMA}:
        raise ValueError(f"unsupported snapshot schema: {schema!r}")
    cards = snapshot.get("cards")
    if not isinstance(cards, list) or any(not isinstance(card, dict) for card in cards):
        raise ValueError("snapshot cards must be a list of objects")
    ids = [card.get("id") for card in cards]
    if any(not isinstance(card_id, str) or not card_id for card_id in ids) or len(ids) != len(set(ids)):
        raise ValueError("snapshot card IDs must be non-empty and unique")
    if 'repository' in snapshot:
        config = repository_config(snapshot['repository'])
        if not re.fullmatch('[0-9a-f]{40}', str(snapshot.get('source_commit', ''))):
            raise ValueError('repository snapshot must record an exact source commit')
        prefix = 'statement::' + config['id'] + '::'
        known_ids = set(ids)
        if snapshot.get('review_mode') != 'statement' or any(
                card['id'] != prefix + card.get('declaration', '') + ('::file=' + card['module_file'] if card.get('private') else '') or
                any(item not in known_ids for item in card.get('dependencies', [])) for card in cards):
            raise ValueError('repository declaration IDs and dependencies must be locally qualified')
        if any('legacy_card_id' in card and (config['id'] != 'kip126' or
                card['legacy_card_id'] != 'statement::' + card['declaration']) for card in cards):
            raise ValueError('legacy card aliases must identify the original KIP126 declaration')
    from .enrichment_v2 import DEFAULT_TOPICS, validate_public_annotation, validate_topics
    topics = validate_topics(snapshot.get('enrichment_topics', DEFAULT_TOPICS))
    for card in cards:
        if 'blueprint_references' in card:
            references = card['blueprint_references']
            keys = {'title', 'statement', 'label', 'chapter', 'blueprint_file',
                    'blueprint_line', 'declarations'}
            if not isinstance(references, list) or any(
                    not isinstance(reference, dict) or set(reference) != keys or
                    any(not isinstance(reference[key], str) for key in
                        ('title', 'statement', 'label', 'chapter', 'blueprint_file')) or
                    not reference['statement'].strip() or
                    type(reference['blueprint_line']) is not int or reference['blueprint_line'] < 1 or
                    not isinstance(reference['declarations'], list) or
                    any(not isinstance(name, str) or not name for name in reference['declarations']) or
                    card.get('declaration') not in reference['declarations']
                    for reference in references):
                raise ValueError('invalid attributed Blueprint references')
        enrichment = card.get('enrichment')
        if isinstance(enrichment, dict) and enrichment.get('schema') == 'statement-enrichment.v2':
            validate_public_annotation(enrichment, topics)
            if enrichment['declaration_id'] != card['id']:
                raise ValueError('public annotation declaration does not match its card')
    if snapshot.get("digest") != calculate_snapshot_digest(snapshot):
        raise ValueError("snapshot digest does not match its card content")
    from .reuse import validate_reuse
    validate_reuse(snapshot)
    if schema == SNAPSHOT_SCHEMA:
        if not isinstance(snapshot.get("source_dirty"), bool):
            raise ValueError("snapshot must record whether the reviewed source was dirty")
        if snapshot.get("fingerprint_scheme") != CURRENT_FINGERPRINT_SCHEME:
            raise ValueError("snapshot fingerprint scheme is missing or unsupported")
        for card in cards:
            nl_digest, lean_digest, fingerprint = _content_fingerprint(
                card, snapshot.get("dependency_lock_digest"))
            if (card.get("fingerprint_scheme") != CURRENT_FINGERPRINT_SCHEME or
                    card.get("fingerprints", {}).get(CURRENT_FINGERPRINT_SCHEME) != card.get("fingerprint") or
                    card.get("nl_digest") != nl_digest or
                    card.get("lean_digest") != lean_digest or
                    card.get("fingerprint") != fingerprint):
                raise ValueError(f"invalid fingerprint metadata for card {card.get('id')!r}")


def _content_fingerprint(card: dict, dependency_lock_digest: str | None) -> tuple[str, str, str]:
    natural_language = {
        "kind": card.get("kind"),
        "title": card.get("title"),
        "statement": card.get("statement"),
    }
    if card.get('blueprint_references'):
        # Relocating a node does not change its mathematical review basis.
        natural_language['blueprint_references'] = [
            {key: reference[key] for key in ('title', 'statement', 'declarations')}
            for reference in card['blueprint_references']]
    nl_digest = _digest(natural_language)
    lean = card.get("lean")
    lean_digest = _digest({
        "declaration": card.get("declaration"),
        "source_status": card.get("source_status"),
        "source": lean.get("source") if lean else None,
        "dependency_lock_digest": dependency_lock_digest
        if card.get("source_status") == "external" else None,
    })
    fingerprint = _digest({
        "scheme": CURRENT_FINGERPRINT_SCHEME,
        "nl_digest": nl_digest,
        "lean_digest": lean_digest,
    })
    return nl_digest, lean_digest, fingerprint


def normalize_snapshot(snapshot: dict) -> dict:
    validate_snapshot(snapshot)
    normalized = deepcopy(snapshot)
    if normalized["schema"] == LEGACY_SNAPSHOT_SCHEMA:
        # v1 cards already contain the cleaned NL and declaration source. Use
        # those fields to upgrade the in-memory review basis without rewriting
        # the immutable v1 artifact or losing its old positional fingerprint.
        normalized["fingerprint_scheme"] = CURRENT_FINGERPRINT_SCHEME
        for card in normalized["cards"]:
            legacy_fingerprint = card["fingerprint"]
            nl_digest, lean_digest, fingerprint = _content_fingerprint(card, None)
            card.update({
                "nl_digest": nl_digest,
                "lean_digest": lean_digest,
                "fingerprint_scheme": CURRENT_FINGERPRINT_SCHEME,
                "fingerprint": fingerprint,
                "fingerprints": {
                    CURRENT_FINGERPRINT_SCHEME: fingerprint,
                    LEGACY_FINGERPRINT_SCHEME: legacy_fingerprint,
                },
            })
    return normalized


def _clean_statement(body: str) -> str:
    body = PROOF_RE.sub("", body)
    body = META_RE.sub("", body)
    return "\n".join(line.rstrip() for line in body.strip().splitlines()).strip()


def _dependency_lock_digest(repo: Path) -> str | None:
    manifest = repo / "lake-manifest.json"
    if not manifest.is_file():
        return None
    return _digest(json.loads(manifest.read_text(encoding="utf-8")))


def fingerprints_match(left: dict, right: dict) -> bool:
    """Whether two cards share an identical review basis in any known scheme."""
    left_values = left.get("fingerprints") or {
        left.get("fingerprint_scheme", LEGACY_FINGERPRINT_SCHEME): left.get("fingerprint")
    }
    right_values = right.get("fingerprints") or {
        right.get("fingerprint_scheme", LEGACY_FINGERPRINT_SCHEME): right.get("fingerprint")
    }
    return any(value and right_values.get(scheme) == value for scheme, value in left_values.items())


def compare_snapshots(previous: dict | None, current: dict) -> dict[str, int]:
    from .repositories import COLLECTION_SCHEMA, datasets, dataset_id
    if current.get('schema') == COLLECTION_SCHEMA or (previous or {}).get('schema') == COLLECTION_SCHEMA:
        old_sets = {dataset_id(item): item for item in datasets(previous)} if previous else {}
        new_sets = {dataset_id(item): item for item in datasets(current)}
        total = dict(unchanged=0, changed=0, added=0, removed=0)
        for key in old_sets.keys() | new_sets.keys():
            if key in new_sets:
                values = compare_snapshots(old_sets.get(key), new_sets[key])
            else:
                values = dict(unchanged=0, changed=0, added=0, removed=len(old_sets[key]['cards']))
            for field, count in values.items():
                total[field] += count
        return total
    old = {card["id"]: card for card in (previous or {}).get("cards", [])}
    new = {card["id"]: card for card in current["cards"]}
    common = old.keys() & new.keys()
    return {
        "unchanged": sum(fingerprints_match(old[key], new[key]) for key in common),
        "changed": sum(not fingerprints_match(old[key], new[key]) for key in common),
        "added": len(new.keys() - old.keys()),
        "removed": len(old.keys() - new.keys()),
    }


def _source_index(repo: Path) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for path in sorted((repo / "KIP126").rglob("*.lean")):
        lines = path.read_text(encoding="utf-8").splitlines()
        namespace: list[str] = []
        scopes: list[str] = []
        locations: list[tuple[str, int]] = []
        for number, line in enumerate(lines):
            match = NAMESPACE_RE.match(line)
            if match:
                namespace.extend(match.group(1).split("."))
                scopes.append("namespace:" + match.group(1))
                continue
            if SECTION_RE.match(line):
                scopes.append("section")
                continue
            if END_RE.match(line):
                if scopes:
                    scope = scopes.pop()
                    if scope.startswith("namespace:"):
                        del namespace[-len(scope.removeprefix("namespace:").split(".")):]
                continue
            match = DECL_RE.match(line)
            if match:
                name = match.group(1)
                fqn = name if name.startswith(("KIP126.", "CategoryTheory.")) else ".".join([*namespace, name])
                locations.append((fqn, number))
        for offset, (fqn, first) in enumerate(locations):
            last = locations[offset + 1][1] if offset + 1 < len(locations) else len(lines)
            excerpt = "\n".join(lines[first:min(last, first + 100)]).rstrip()
            index[fqn] = {
                "file": str(path.relative_to(repo)), "line": first + 1,
                "source": excerpt, "truncated": last - first > 100,
            }
            if re.match(r"^\s*(?:structure|class)\s+", lines[first]):
                for field_line in range(first + 1, last):
                    field = re.match(r"^\s{2,}([\w₀-₉]+)\s*:", lines[field_line])
                    if field:
                        index[f"{fqn}.{field.group(1)}"] = {
                            "file": str(path.relative_to(repo)), "line": field_line + 1,
                            "source": excerpt, "truncated": last - first > 100,
                        }
    return index


def compile_snapshot(repo: Path) -> dict:
    content = (repo / "blueprint/src/content.tex").read_text(encoding="utf-8")
    index = _source_index(repo)
    dependency_lock_digest = _dependency_lock_digest(repo)
    cards = []
    seen_labels: set[str] = set()
    for chapter_input in INPUT_RE.findall(content):
        path = repo / "blueprint/src" / (chapter_input + ".tex")
        source = path.read_text(encoding="utf-8")
        chapter_match = CHAPTER_RE.search(source)
        chapter = chapter_match.group(1) if chapter_match else path.stem
        for match in NODE_RE.finditer(source):
            kind, title, body = match.groups()
            label_match = LABEL_RE.search(body)
            if not label_match:
                continue
            label = label_match.group(1)
            if label in seen_labels:
                raise ValueError(f"duplicate Blueprint label: {label}")
            seen_labels.add(label)
            names = lean_references(body)
            uses = [item.strip() for group in USES_RE.findall(body) for item in group.split(",") if item.strip()]
            statement = _clean_statement(body)
            for name in names:
                name = name.strip()
                local = index.get(name)
                card = {
                    "id": f"{label}::{name}", "label": label, "declaration": name,
                    "kind": kind, "title": title or label, "chapter": chapter,
                    "blueprint_file": str(path.relative_to(repo)),
                    "blueprint_line": source.count("\n", 0, match.start()) + 1,
                    "statement": statement, "dependencies": uses,
                    "lean": local,
                    "source_status": "local" if local else ("external" if not name.startswith("KIP126.") else "unresolved"),
                }
                legacy_fingerprint = _digest({
                    "statement": body, "declaration": name, "lean": local,
                })
                nl_digest, lean_digest, fingerprint = _content_fingerprint(
                    card, dependency_lock_digest)
                card.update({
                    "nl_digest": nl_digest,
                    "lean_digest": lean_digest,
                    "fingerprint_scheme": CURRENT_FINGERPRINT_SCHEME,
                    "fingerprint": fingerprint,
                    "fingerprints": {
                        CURRENT_FINGERPRINT_SCHEME: fingerprint,
                        LEGACY_FINGERPRINT_SCHEME: legacy_fingerprint,
                    },
                })
                cards.append(card)
    payload = {
        "schema": SNAPSHOT_SCHEMA,
        "fingerprint_scheme": CURRENT_FINGERPRINT_SCHEME,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_commit": _git_head(repo),
        "source_dirty": _git_dirty(repo),
        "dependency_lock_digest": dependency_lock_digest,
        "cards": cards,
        "unlinked_nodes": len(seen_labels) - len({card["label"] for card in cards}),
    }
    payload["digest"] = calculate_snapshot_digest(payload)
    return payload


def _git_head(repo: Path) -> str:
    import subprocess
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()


def _git_dirty(repo: Path) -> bool:
    import subprocess
    return bool(subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=normal"], cwd=repo, text=True
    ).strip())


def write_snapshot(repo: Path, output: Path, *, require_clean: bool = False) -> dict:
    """Build a new candidate artifact; install_snapshot activates review evidence."""
    output = candidate_output_path(output, source_tree=repo)
    payload = compile_snapshot(repo)
    if require_clean and payload["source_dirty"]:
        raise ValueError("reviewed source checkout has uncommitted or untracked changes")
    payload["comparison"] = compare_snapshots(None, payload)
    write_candidate_artifact(payload, output, source_tree=repo)
    return payload
