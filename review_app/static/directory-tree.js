(function (root) {
  "use strict";
  function matchesPath(file, path) {
    return !path || file === path || (typeof file === "string" && file.startsWith(path + "/"));
  }
  function buildTree(cards) {
    const nodes = new Map([["", {path:"", name:"全部源码", count:0, children:[], file:false}]]);
    for (const card of cards) {
      const file = card.module_file || card.lean?.file;
      if (!file) continue;
      nodes.get("").count++;
      const parts = file.split("/");
      for (let i = 1; i <= parts.length; i++) {
        const path = parts.slice(0, i).join("/"), parent = parts.slice(0, i-1).join("/");
        if (!nodes.has(path)) {
          nodes.set(path, {path, name:parts[i-1], count:0, children:[], file:i === parts.length});
          nodes.get(parent).children.push(path);
        }
        nodes.get(path).count++;
      }
    }
    for (const node of nodes.values()) node.children.sort((a,b) =>
      Number(nodes.get(a).file) - Number(nodes.get(b).file) || a.localeCompare(b));
    return nodes;
  }
  root.StatementDirectories = Object.freeze({matchesPath, buildTree});
})(typeof window === "undefined" ? globalThis : window);
