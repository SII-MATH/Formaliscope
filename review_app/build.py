"""Compile Blueprint links and local Lean source into an immutable review snapshot.

This is a source locator, not a Lean parser or a proof checker. Unresolved names
stay visibly unresolved so a reviewer never mistakes a guess for evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
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


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _clean_statement(body: str) -> str:
    body = PROOF_RE.sub("", body)
    body = META_RE.sub("", body)
    return "\n".join(line.rstrip() for line in body.strip().splitlines()).strip()


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
                card["fingerprint"] = _digest({
                    "statement": body, "declaration": name, "lean": local,
                })
                cards.append(card)
    payload = {
        "schema": "kip126-review-snapshot.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_commit": _git_head(repo),
        "cards": cards,
        "unlinked_nodes": len(seen_labels) - len({card["label"] for card in cards}),
    }
    payload["digest"] = _digest({"cards": cards, "schema": payload["schema"]})
    return payload


def _git_head(repo: Path) -> str:
    import subprocess
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()


def write_snapshot(repo: Path, output: Path) -> dict:
    payload = compile_snapshot(repo)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output)
    return payload
