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
    for key in ('review_mode', 'review_contract', 'modules', 'source_origin'):
        if key in snapshot:
            protected[key] = snapshot[key]
    return _digest(protected)


def validate_snapshot(snapshot: dict) -> None:
    schema = snapshot.get("schema")
    if schema not in {LEGACY_SNAPSHOT_SCHEMA, SNAPSHOT_SCHEMA}:
        raise ValueError(f"unsupported snapshot schema: {schema!r}")
    cards = snapshot.get("cards")
    if not isinstance(cards, list) or any(not isinstance(card, dict) for card in cards):
        raise ValueError("snapshot cards must be a list of objects")
    ids = [card.get("id") for card in cards]
    if any(not isinstance(card_id, str) or not card_id for card_id in ids) or len(ids) != len(set(ids)):
        raise ValueError("snapshot card IDs must be non-empty and unique")
    if snapshot.get("digest") != calculate_snapshot_digest(snapshot):
        raise ValueError("snapshot digest does not match its card content")
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
    nl_digest = _digest({
        "kind": card.get("kind"),
        "title": card.get("title"),
        "statement": card.get("statement"),
    })
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
            names = LEAN_RE.findall(body)
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
    payload = compile_snapshot(repo)
    if require_clean and payload["source_dirty"]:
        raise ValueError("reviewed source checkout has uncommitted or untracked changes")
    previous = None
    if output.is_file():
        try:
            previous = normalize_snapshot(json.loads(output.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            previous = None
    payload["comparison"] = compare_snapshots(previous, payload)
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(output)
    return payload
