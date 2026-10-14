/-
Copyright (c) 2026 Formaliscope contributors.
Released under Apache 2.0 license.

Export compiler-owned declarations from Lean 4.32.2 build artifacts. No source
scope or visibility rules are duplicated here. Source ranges come from Lean's
DeclarationRanges extension; its own lexer is used only to label user-written
headers and distinguish them from constructors, projections and generated code.

Run inside the audited Lake package:
  lake env lean --run /absolute/path/ExportSemantic.lean /repo /file-list
The file list contains one repo-relative .lean source path per line.
-/
import Lean

open Lean

namespace Formaliscope

private def jsonObject (fields : List (String × Json)) : Json := Json.mkObj fields
private def jsonStrings (xs : Array String) : Json := toJson xs

private def moduleOfFile (file : String) : Name :=
  file.dropEnd 5 |>.toString.splitOn "/" |>.foldl Name.str .anonymous

private def fileOfModule (mod : Name) : String :=
  String.intercalate "/" (mod.components.map (·.toString)) ++ ".lean"

/-- The only unsafe operation mirrors Lean's persistent-extension deserializer.
The extension's registered name and the exact supported compiler version are
checked before interpreting its serialized entry type. -/
private unsafe def mapEntries (data : ModuleData) (ext : MapDeclarationExtension α) : Array (Name × α) :=
  match data.entries.find? (fun e => e.1 == ext.name) with
  | some (_, entries) => unsafeCast entries
  | none => #[]

private unsafe def structures (data : ModuleData) : Array StructureInfo :=
  match data.entries.find? (fun e => privateToUserName e.1 == `Lean.structureExt) with
  | some (_, entries) => unsafeCast entries
  | none => #[]

private def checkedPosition (map : FileMap) (p : Position) (file : String) : IO String.Pos.Raw := do
  let byte := map.ofPosition p
  unless map.toPosition byte == p do
    throw <| IO.userError s!"invalid compiler source position in {file}: {p}"
  return byte

private def tokenText (stx : Syntax) : String :=
  if stx.isIdent then stx.getId.toString else stx.getAtomVal

private def lex (env : Environment) (ctx : Parser.InputContext) (start stop : String.Pos.Raw) :
    IO (Array Syntax) := do
  let mut state := { Parser.mkParserState ctx.inputString with pos := start }
  let parser := Parser.andthenFn Parser.whitespace (Parser.tokenFn [])
  let mut result := #[]
  while state.pos < stop do
    let previous := state.pos
    state := parser.run ctx { env, options := {} } (Parser.getTokenTable env) state
    if state.errorMsg.isSome || state.pos <= previous then
      throw <| IO.userError s!"cannot lex compiler declaration range in {ctx.fileName} at {previous.byteIdx}"
    let stx := state.stxStack.back
    if stx.getPos?.getD stop < stop then result := result.push stx
  return result

private def afterDoc (env : Environment) (ctx : Parser.InputContext) (start : String.Pos.Raw) : IO String.Pos.Raw := do
  let parser := Parser.andthenFn Parser.whitespace (Parser.optional Parser.Command.docComment).fn
  let state := parser.run ctx {env, options := {}} (Parser.getTokenTable env)
    { Parser.mkParserState ctx.inputString with pos := start }
  if state.errorMsg.isSome then
    throw <| IO.userError s!"cannot read compiler-attached documentation in {ctx.fileName}"
  return state.pos

private def modifiers : List String :=
  ["public", "private", "protected", "noncomputable", "meta", "unsafe", "partial", "nonrec"]
private def kinds : List String :=
  ["def", "theorem", "lemma", "structure", "class", "abbrev", "inductive", "coinductive", "opaque", "axiom", "constant", "instance"]

/-- Label a compiler-recorded user declaration. This never discovers names from
source: a matching declaration and range must already exist in the compiler.
Nested source quotations and local definitions cannot create entries. -/
private def header? (tokens : Array Syntax) : Option (String × Syntax × Option Syntax) := Id.run do
  let mut i := 0
  if tokenText (tokens[i]?.getD .missing) == "@[" then
    let mut nesting := 1
    i := i + 1
    while nesting > 0 && i < tokens.size do
      let text := tokenText tokens[i]!
      if text == "[" || text == "@[" then nesting := nesting + 1
      if text == "]" then nesting := nesting - 1
      i := i + 1
  while i < tokens.size && modifiers.contains (tokenText tokens[i]!) do i := i + 1
  -- `local instance` and `scoped instance` are ordinary compiler declarations.
  if tokenText (tokens[i]?.getD .missing) == "local" || tokenText (tokens[i]?.getD .missing) == "scoped" then i := i + 1
  let some keyword := tokens[i]? | return none
  let kind := tokenText keyword
  if !kinds.contains kind then return none
  if kind == "instance" then return some (kind, keyword, none)
  i := i + 1
  if kind == "class" && ["inductive", "abbrev"].contains (tokenText (tokens[i]?.getD .missing)) then i := i + 1
  return some (kind, keyword, tokens[i]?)

private def nameMatches (compilerName : Name) (id : Syntax) : Bool :=
  let full := privateToUserName compilerName.eraseMacroScopes |>.toString
  let written := id.getId.toString
  let written := if written.startsWith "_root_." then written.drop 7 |>.toString else written
  full == written || full.endsWith ("." ++ written)

private def eligibleConstant (info : ConstantInfo) : Bool :=
  match info with
  | .ctorInfo _ | .recInfo _ | .quotInfo _ => false
  | _ => true

/-- Read field type snippets from Lean's structure syntax tree. Parser extensions
outside Init may make a type impossible to reparse; compiled projection types
remain available as an explicit fallback, and are never used for visibility. -/
private partial def fieldTypes (source : String) (stx : Syntax) : Array (Name × String) := Id.run do
  let simple := stx.isOfKind ``Parser.Command.structSimpleBinder
  let binder := [``Parser.Command.structExplicitBinder, ``Parser.Command.structImplicitBinder,
                 ``Parser.Command.structInstBinder].contains stx.getKind
  if simple || binder then
    let names := if simple then #[stx[1]] else stx[2].getArgs
    let signature := if simple then stx[2] else stx[3]
    let typeSpec := if signature.isOfKind ``Parser.Command.optDeclSig then signature[1][0] else signature[1]
    let typeStx := typeSpec[1]
    let some range := typeStx.getRange? | return #[]
    let text := String.Pos.Raw.extract source range.start range.stop
    return names.filterMap fun name => if name.isIdent then some (name.getId, text) else none
  return stx.getArgs.foldl (fun result child => result ++ fieldTypes source child) #[]

private def sourceFieldTypes (env : Environment) (ctx : Parser.InputContext) (keyword : Syntax) : Array (Name × String) :=
  let state := Parser.Command.structure.fn.run ctx {env, options := {}} (Parser.getTokenTable env)
    { Parser.mkParserState ctx.inputString with pos := keyword.getPos?.getD 0 }
  if state.hasError then #[] else fieldTypes ctx.inputString state.stxStack.back

private def importsJson (data : ModuleData) : Json :=
  jsonStrings (data.imports.foldl (fun names imp =>
    let name := imp.module.toString
    if names.contains name then names else names.push name) #[])

private unsafe def readParts (mod : Name) : IO (ModuleData × ModuleData × Array CompactedRegion) := do
  let path ← findOLean mod
  let first ← readModuleData path
  let paths := if first.1.isModule then
    #[path, OLeanLevel.server.adjustFileName path, OLeanLevel.private.adjustFileName path]
  else #[path]
  for part in paths do
    unless ← part.pathExists do throw <| IO.userError s!"missing compiler artifact: {part}"
  let parts ← readModuleDataParts paths
  let some (full, _) := parts.back? | throw <| IO.userError s!"missing compiler data: {mod}"
  return (first.1, full, parts.map (·.2) |>.push first.2)

private unsafe def exportModule (repo : System.FilePath) (file : String) (env : Environment) : IO (Json × Array Name) := do
  let mod := moduleOfFile file
  let (publicData, data, regions) ← readParts mod
  let source ← IO.FS.readFile (repo / file)
  let ctx := Parser.mkInputContext source file (normalizeLineEndings := false)
  let ranges := mapEntries data declRangeExt
  if ranges.isEmpty && !data.constants.isEmpty then
    throw <| IO.userError s!"compiler declaration ranges are unavailable for nonempty module {mod}"
  let docs := mapEntries data docStringExt
  let structInfos := structures data
  let mut declarations : Array Json := #[]
  for (compilerName, range) in ranges do
    let some info := data.constants.find? (·.name == compilerName) | continue
    if !eligibleConstant info then continue
    let rangeStart ← checkedPosition ctx.fileMap range.range.pos file
    let rangeEnd ← checkedPosition ctx.fileMap range.range.endPos file
    let selectionStart ← checkedPosition ctx.fileMap range.selectionRange.pos file
    let selectionEnd ← checkedPosition ctx.fileMap range.selectionRange.endPos file
    let start ← afterDoc env ctx rangeStart
    let tokens ← lex env ctx start selectionEnd
    let some (kind, keyword, id?) := header? tokens | continue
    -- Fields and synthetic helpers can carry their enclosing declaration's
    -- range; only the actual header name/instance keyword can own a card.
    if kind == "instance" then
      if selectionStart != keyword.getPos?.getD selectionEnd then
        let some id := tokens.find? (·.getPos? == some selectionStart) | continue
        if !id.isIdent || !nameMatches compilerName id then continue
    else
      let some id := id? | continue
      if id.getPos? != some selectionStart || !id.isIdent || !nameMatches compilerName id then continue
    let some first := tokens[0]? | throw <| IO.userError s!"empty declaration header: {compilerName}"
    let start := first.getPos?.getD start
    if start >= rangeEnd then throw <| IO.userError s!"empty declaration range: {compilerName}"
    let isPublic := !isPrivateName compilerName && publicData.constNames.contains compilerName
    let userName := privateToUserName compilerName.eraseMacroScopes
    let doc := (docs.find? (·.1 == compilerName)).map (·.2) |>.getD ""
    let mut fields : Array Json := #[]
    let originalTypes := if kind == "structure" || kind == "class" then sourceFieldTypes env ctx keyword else #[]
    if let some structInfo := structInfos.find? (·.structName == compilerName) then
      for field in structInfo.fieldNames do
        let some fieldInfo := structInfo.fieldInfo.find? (·.fieldName == field) | continue
        let some proj := data.constants.find? (·.name == fieldInfo.projFn) | continue
        let fieldType := (originalTypes.find? (·.1 == field)).map (·.2) |>.getD (toString proj.type)
        fields := fields.push <| jsonObject [("name", toJson field.toString), ("type", toJson fieldType)]
    declarations := declarations.push <| jsonObject [
      ("name", toJson userName.toString), ("compiler_name", toJson compilerName.toString),
      ("kind", toJson kind),
      ("name_generated", toJson (kind == "instance" && selectionStart == keyword.getPos?.getD selectionEnd)),
      ("visibility", toJson (if isPublic then "public" else "private")),
      ("start_byte", toJson start.byteIdx), ("end_byte", toJson rangeEnd.byteIdx),
      ("line", toJson (ctx.fileMap.toPosition start).line),
      ("namespace", jsonStrings (userName.getPrefix.components.toArray.map (·.toString))),
      ("doc", toJson doc), ("fields", toJson fields)]
  -- Keep the mapped regions alive until every value has been copied into JSON.
  let _ := regions.size
  declarations := declarations.qsort (fun a b =>
    (a.getObjValAs? Nat "start_byte").toOption.getD 0 < (b.getObjValAs? Nat "start_byte").toOption.getD 0)
  return (jsonObject [("file", toJson file), ("module", toJson mod.toString),
    ("imports", importsJson data), ("declarations", toJson declarations)], data.imports.map (·.module))

private unsafe def contextModules (repo : System.FilePath) (pending : List Name)
    (seen : Std.HashSet Name) (result : Array Json) : IO (Array Json) := do
  match pending with
  | [] => return result
  | mod :: remaining =>
    if seen.contains mod then return ← contextModules repo remaining seen result
    let seen := seen.insert mod
    let file := fileOfModule mod
    if !(← (repo / file).pathExists) then return ← contextModules repo remaining seen result
    let (_, data, regions) ← readParts mod
    let entry := jsonObject [("file", toJson file), ("module", toJson mod.toString), ("imports", importsJson data)]
    let _ := regions.size
    contextModules repo (data.imports.toList.map (·.module) ++ remaining) seen (result.push entry)

unsafe def run (repo filesPath : System.FilePath) : IO Json := do
  unless Lean.versionString == "4.32.2" do
    throw <| IO.userError s!"semantic exporter supports Lean 4.32.2; found {Lean.versionString}"
  initSearchPath (← findSysroot)
  enableInitializersExecution
  let env ← importModules #[{module := `Init}] {} (loadExts := true)
  let files := (← IO.FS.readFile filesPath).splitOn "\n" |>.filter (!·.isEmpty)
  let mut modules : Array Json := #[]
  let mut imports : Array Name := #[]
  let mut selected : Std.HashSet Name := {}
  for file in files do
    let (entry, deps) ← exportModule repo file env
    modules := modules.push entry
    imports := imports ++ deps
    selected := selected.insert (moduleOfFile file)
  let context ← contextModules repo imports.toList selected #[]
  return jsonObject [("schema", toJson "formaliscope-lean-semantic-index.v1"),
    ("lean_version", toJson Lean.versionString), ("modules", toJson modules),
    ("context_modules", toJson context)]

end Formaliscope

unsafe def main (args : List String) : IO UInt32 := do
  match args with
  | [repo, files] =>
    try
      let result ← Formaliscope.run repo files
      (← IO.getStdout).putStrLn result.compress
      return 0
    catch e =>
      (← IO.getStderr).putStrLn s!"Formaliscope semantic export failed: {e}"
      return 1
  | _ =>
    (← IO.getStderr).putStrLn "usage: ExportSemantic.lean REPO FILE_LIST"
    return 1
