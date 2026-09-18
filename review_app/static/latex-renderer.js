"use strict";
(() => {
  const escapeHtml = (value) => String(value ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");

  const macros = {
    Ext: "\\mathrm{Ext}", Hom: "\\mathrm{Hom}", Tor: "\\mathrm{Tor}",
    Ker: "\\mathrm{ker}", Coker: "\\mathrm{coker}", colim: "\\operatorname{colim}",
    iso: "\\cong", Sus: "\\Sigma", sma: "\\wedge", AF: "\\mathrm{AF}",
    fto: ["\\xrightarrow{#1}", 1], HF: "\\mathrm{H}\\mathbb{F}_2",
    SynSS: "{}^{\\mathrm{syn}\\!}E",
    ESS: ["{}^{#1\\!}E", 1], ZESS: ["{}^{#1\\!}Z", 1], BESS: ["{}^{#1\\!}B", 1],
  };
  "ABCDEFGHIJKLMNOPQRSTUVWXYZ".split("").forEach((letter) => {
    macros[`b${letter}`] = `\\mathbb{${letter}}`;
    macros[`cal${letter}`] = `\\mathcal{${letter}}`;
  });

  window.MathJax = {
    tex: {
      inlineMath: [["\\(", "\\)"], ["$", "$"]],
      displayMath: [["\\[", "\\]"], ["$$", "$$"]],
      packages: {"[+]": ["noerrors", "noundefined"]},
      macros,
    },
    svg: {fontCache: "global"},
    startup: {typeset: false},
  };

  const MATH = /(\$\$[\s\S]*?\$\$|\$(?:\\.|[^$\n])*?\$|\\\[[\s\S]*?\\\]|\\\([\s\S]*?\\\)|\\begin\{(?:equation\*?|align\*?|gather\*?|multline\*?)\}[\s\S]*?\\end\{(?:equation\*?|align\*?|gather\*?|multline\*?)\})/g;
  const STRUCTURE = /\uE000(\d+)\uE001|\\begin\{(?:enumerate|itemize)\}(?:\[[^\]]*\])?|\\end\{(?:enumerate|itemize)\}|\\item(?:\[[^\]]*\])?|\\begin\{(?:theorem|lemma|definition|proposition|corollary|remark|proof)\}|\\end\{(?:theorem|lemma|definition|proposition|corollary|remark|proof)\}|\\cite(?:\[[^\]]*\])?\{[^}]*\}|\\(?:nondepref|ref|noderef)\{[^}]*\}|\\(?:emph|textbf)\{[^{}]*\}|\\\\/g;

  function extractMath(source) {
    const values = [];
    return {
      values,
      text: source.replace(MATH, (value) => {
        const index = values.push(value) - 1;
        return `\uE000${index}\uE001`;
      }),
    };
  }

  function citation(token) {
    const match = token.match(/^\\cite(?:\[([^\]]*)\])?\{([^}]*)\}$/);
    if (!match) return escapeHtml(token);
    const keys = match[2].split(",").map((item) => item.trim()).filter(Boolean).join(", ");
    const detail = match[1]?.replace(/~/g, " ").trim();
    return `<span class="latex-citation">[${escapeHtml(keys)}${detail ? ` · ${escapeHtml(detail)}` : ""}]</span>`;
  }

  function reference(token) {
    const match = token.match(/^\\(?:nondepref|ref|noderef)\{([^}]*)\}$/);
    return match ? `<span class="latex-reference">§ ${escapeHtml(match[1])}</span>` : escapeHtml(token);
  }

  function mathHtml(value) {
    if (/\\xymatrix\s*\{/.test(value)) {
      return `<details class="latex-diagram"><summary>Commutative diagram · LaTeX source</summary><pre>${escapeHtml(value)}</pre></details>`;
    }
    return escapeHtml(value);
  }

  function toHtml(value) {
    let source = String(value ?? "").replace(/\r\n?/g, "\n");
    source = source.replace(/\[\{\s*(\\cite(?:\[[^\]]*\])?\{[^}]*\})\s*\}\]/g, "$1");
    source = source.replace(/\\begin\{enumerate\}(?:\[[^\]]*\])?\s*\\setcounter\{enumi\}\{(\d+)\}/g,
      (_, value) => `\\begin{enumerate}[start=${Number(value) + 1}]`);
    source = source.replace(/\\leavevmode\b/g, "");
    source = source.replace(/\\vspace\*?\{[^}]*\}/g, "");
    const extracted = extractMath(source);
    const stack = [];
    let cursor = 0;
    let output = '<div class="latex-content">';

    for (const match of extracted.text.matchAll(STRUCTURE)) {
      output += escapeHtml(extracted.text.slice(cursor, match.index)).replace(/~/g, "&nbsp;");
      const token = match[0];
      cursor = match.index + token.length;
      if (match[1] !== undefined) {
        output += mathHtml(extracted.values[Number(match[1])]);
        continue;
      }
      const beginList = token.match(/^\\begin\{(enumerate|itemize)\}(?:\[([^\]]*)\])?$/);
      if (beginList) {
        const tag = beginList[1] === "enumerate" ? "ol" : "ul";
        const start = beginList[2]?.match(/(?:^|,)\s*start\s*=\s*(\d+)/)?.[1];
        output += `<${tag} class="latex-list"${start && tag === "ol" ? ` start="${start}"` : ""}>`;
        stack.push({tag, item: false});
        continue;
      }
      const endList = token.match(/^\\end\{(enumerate|itemize)\}$/);
      if (endList) {
        const list = stack.pop();
        if (list) output += `${list.item ? "</li>" : ""}</${list.tag}>`;
        continue;
      }
      if (/^\\item/.test(token)) {
        const list = stack[stack.length - 1];
        const labelMatch = token.match(/^\\item(?:\[([^\]]*)\])?$/);
        const hasEmptyLabel = labelMatch && labelMatch[1] === "";
        if (list && hasEmptyLabel && !list.item) continue;
        if (!list) { output += "<br>• "; continue; }
        if (list.item) output += "</li>";
        output += "<li>";
        list.item = true;
        if (labelMatch?.[1]) output += `<span class="latex-item-label">${escapeHtml(labelMatch[1])}</span> `;
        continue;
      }
      const beginBlock = token.match(/^\\begin\{(theorem|lemma|definition|proposition|corollary|remark|proof)\}$/);
      if (beginBlock) {
        output += `<div class="latex-environment latex-${beginBlock[1]}"><span class="latex-environment-label">${beginBlock[1]}</span>`;
        continue;
      }
      if (/^\\end\{(?:theorem|lemma|definition|proposition|corollary|remark|proof)\}$/.test(token)) {
        output += "</div>";
        continue;
      }
      if (token.startsWith("\\cite")) output += citation(token);
      else if (/^\\(?:emph|textbf)\{/.test(token)) {
        const formatted = token.match(/^\\(emph|textbf)\{([^}]*)\}$/);
        output += formatted?.[1] === "emph"
          ? `<em>${escapeHtml(formatted[2])}</em>` : `<strong>${escapeHtml(formatted?.[2] || "")}</strong>`;
      } else if (token === "\\\\") output += "<br>";
      else output += reference(token);
    }
    output += escapeHtml(extracted.text.slice(cursor)).replace(/~/g, "&nbsp;");
    while (stack.length) {
      const list = stack.pop();
      output += `${list.item ? "</li>" : ""}</${list.tag}>`;
    }
    return `${output}</div>`;
  }

  async function typeset(elements) {
    const mathjax = window.MathJax;
    if (!mathjax?.typesetPromise) return false;
    for (const element of elements.filter(Boolean)) {
      try {
        mathjax.typesetClear?.([element]);
        await mathjax.typesetPromise([element]);
        element.classList.remove("latex-render-error");
      } catch (error) {
        element.classList.add("latex-render-error");
        console.warn("Stage 3 LaTeX render failed", error);
      }
    }
    return true;
  }

  window.Stage3Latex = Object.freeze({toHtml, typeset, macros: Object.freeze(macros)});
})();
