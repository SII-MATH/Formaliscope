(function (root) {
  "use strict";

  const KEYWORDS = new Set([
    "theorem", "lemma", "def", "abbrev", "structure", "class", "instance",
    "inductive", "axiom", "example", "where", "by", "fun", "let", "have",
    "show", "from", "match", "with", "do", "import", "open", "namespace",
    "section", "end", "variable", "universe", "noncomputable", "private",
    "protected", "partial", "unsafe", "deriving", "extends", "if", "then",
    "else", "exact", "intro", "apply", "classical"
  ]);
  const TYPES = new Set([
    "Prop", "Type", "Sort", "Nat", "Int", "Rat", "Real", "Bool", "String", "Fin"
  ]);
  const TOKEN_RE = /(\/-[\s\S]*?-\/)|(--[^\n]*)|("(?:[^"\\]|\\.)*")|\b([A-Za-z_][A-Za-z0-9_']*)\b|(?<![\w.])(\d+)\b/g;

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function normalizeSource(source, options) {
    const maxBlankLines = Math.max(0, Number(options && options.maxBlankLines) || 1);
    const lines = String(source == null ? "" : source)
      .replace(/\r\n?/g, "\n")
      .split("\n")
      .map((line) => line.replace(/[\t \u00a0\u2000-\u200b\u202f\u205f\u3000]+$/g, ""));
    const output = [];
    let blanks = 0;
    for (const line of lines) {
      if (/^[\t \u00a0\u2000-\u200b\u202f\u205f\u3000]*$/.test(line)) {
        blanks += 1;
        if (output.length && blanks <= maxBlankLines) output.push("");
      } else {
        blanks = 0;
        output.push(line);
      }
    }
    while (output.length && output[output.length - 1] === "") output.pop();
    return output.join("\n");
  }

  function toHtml(source, options) {
    const normalized = normalizeSource(source, options);
    let output = "";
    let cursor = 0;
    TOKEN_RE.lastIndex = 0;
    for (let token = TOKEN_RE.exec(normalized); token; token = TOKEN_RE.exec(normalized)) {
      output += escapeHtml(normalized.slice(cursor, token.index));
      const [match, blockComment, lineComment, stringLiteral, word, number] = token;
      const escaped = escapeHtml(match);
      if (blockComment) output += `<span class="c">${escaped}</span>`;
      else if (lineComment) {
        output += /BODY|TABLET NODE|PROOF MASKED|HELPER-OF/.test(lineComment)
          ? `<span class="mk">${escaped}</span>`
          : `<span class="c">${escaped}</span>`;
      } else if (stringLiteral) output += `<span class="s">${escaped}</span>`;
      else if (word === "sorry" || word === "admit") output += `<span class="bad">${escaped}</span>`;
      else if (KEYWORDS.has(word)) output += `<span class="k">${escaped}</span>`;
      else if (TYPES.has(word)) output += `<span class="t">${escaped}</span>`;
      else if (number) output += `<span class="n">${escaped}</span>`;
      else output += escaped;
      cursor = token.index + match.length;
    }
    return output + escapeHtml(normalized.slice(cursor));
  }

  root.Stage3Lean = Object.freeze({ escapeHtml, normalizeSource, toHtml });
})(typeof window === "undefined" ? globalThis : window);
