(function (root) {
  "use strict";

  const KEYWORDS = new Set([
    "theorem", "lemma", "def", "abbrev", "structure", "class", "instance",
    "inductive", "opaque", "axiom", "constant", "example", "where", "by", "fun", "let", "have",
    "show", "from", "match", "with", "do", "import", "open", "namespace",
    "section", "end", "variable", "universe", "noncomputable", "private",
    "protected", "partial", "unsafe", "deriving", "extends", "if", "then",
    "else", "exact", "intro", "intros", "apply", "classical", "forall", "scoped"
  ]);
  const TYPES = new Set([
    "Prop", "Type", "Sort", "Nat", "Int", "Rat", "Real", "Bool", "String", "Fin"
  ]);
  const NAME_RE = /(?:[\p{L}_][\p{L}\p{N}\p{M}_']*|«[^»\n]+»)(?:\.(?:[\p{L}_][\p{L}\p{N}\p{M}_']*|«[^»\n]+»))*/uy;
  const NUMBER_RE = /\d+/y;

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function normalizedLines(source, options) {
    const maxBlankLines = Math.max(0, Number(options && options.maxBlankLines) || 1);
    const lines = String(source == null ? "" : source)
      .replace(/\r\n?/g, "\n")
      .split("\n")
      .map((text, index) => ({text:text.replace(/[\t \u00a0\u2000-\u200b\u202f\u205f\u3000]+$/g, ""), original:index}));
    const output = [];
    let blanks = 0;
    for (const line of lines) {
      if (/^[\t \u00a0\u2000-\u200b\u202f\u205f\u3000]*$/.test(line.text)) {
        blanks += 1;
        if (output.length && blanks <= maxBlankLines) output.push(line);
      } else {
        blanks = 0;
        output.push(line);
      }
    }
    while (output.length && output[output.length - 1].text === "") output.pop();
    return output;
  }

  function normalizeSource(source, options) {
    return normalizedLines(source, options).map(line=>line.text).join("\n");
  }

  function tokens(source) {
    const found=[];
    let index=0;
    while(index<source.length){
      const start=index;
      let kind="", value;
      if(source.startsWith('/-',index)){
        index+=2;let depth=1;
        while(index<source.length&&depth){
          if(source.startsWith('/-',index)){depth++;index+=2;}
          else if(source.startsWith('-/',index)){depth--;index+=2;}
          else index++;
        }
        kind='comment';
      }else if(source.startsWith('--',index)){
        const end=source.indexOf('\n',index);index=end<0?source.length:end;kind='comment';
      }else if(source[index]==="'"&&/^'(?:[^'\\\n]|\\.)'/u.test(source.slice(index))){
        const literal=/^'(?:[^'\\\n]|\\.)'/u.exec(source.slice(index))[0];index+=literal.length;kind='string';
      }else if(source[index]==='"'){
        index++;
        while(index<source.length){
          if(source[index]==='\\'){index+=2;continue;}
          if(source[index++]==='"')break;
        }
        index=Math.min(index,source.length);kind='string';
      }else{
        NAME_RE.lastIndex=index;value=NAME_RE.exec(source);
        if(value){index+=value[0].length;kind='name';}
        else{NUMBER_RE.lastIndex=index;value=NUMBER_RE.exec(source);if(value){index+=value[0].length;kind='number';}}
      }
      if(kind)found.push({start,end:index,kind,text:source.slice(start,index)});
      else index++;
    }
    return found;
  }

  function toHtml(source, options={}) {
    const lines=normalizedLines(source,options),normalized=lines.map(line=>line.text).join('\n');
    const lineStarts=[];let offset=0;
    for(const line of lines){lineStarts.push(offset);offset+=line.text.length+1;}
    let output = "", cursor = 0, lineIndex=0;
    for(const token of tokens(normalized)){
      output+=escapeHtml(normalized.slice(cursor,token.start));
      while(lineIndex+1<lineStarts.length&&lineStarts[lineIndex+1]<=token.start)lineIndex++;
      const escaped=escapeHtml(token.text);
      let css='';
      if(token.kind==='comment')css=/^--.*(?:BODY|TABLET NODE|PROOF MASKED|HELPER-OF)/.test(token.text)?'mk':'c';
      else if(token.kind==='string')css='s';
      else if(token.text==='sorry'||token.text==='admit')css='bad';
      else if(KEYWORDS.has(token.text))css='k';
      else if(TYPES.has(token.text))css='t';
      else if(token.kind==='number')css='n';
      const content=css?`<span class="${css}">${escaped}</span>`:escaped;
      if(options.symbols&&token.kind==='name'&&!KEYWORDS.has(token.text)&&!['sorry','admit','_'].includes(token.text)){
        const line=(Number(options.baseLine)||1)+(lines[lineIndex]?.original||0);
        // Python columns count Unicode characters, whereas JavaScript offsets
        // count UTF-16 code units. Convert the line prefix before sending it.
        const column=[...normalized.slice(lineStarts[lineIndex],token.start)].length+1;
        output+=`<button type="button" class="lean-symbol" data-symbol="${escaped}" data-symbol-line="${line}" data-symbol-column="${column}" data-symbol-scope="${options.scope==='module'?'module':'declaration'}" aria-label="查看 ${escaped} 的定义">${content}</button>`;
      }else output+=content;
      cursor=token.end;
    }
    return output+escapeHtml(normalized.slice(cursor));
  }

  root.Stage3Lean = Object.freeze({ escapeHtml, normalizeSource, toHtml, tokens });
})(typeof window === "undefined" ? globalThis : window);
