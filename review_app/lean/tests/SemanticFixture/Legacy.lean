import Lean

namespace Old

/-- Original non-module definitions are public. -/
def ordinary : Nat := 1
private def hidden : Nat := 2

syntax (name := lemmaCommand) declModifiers "lemma " declId declSig declVal : command
macro_rules
  | `($mods:declModifiers lemma $id:declId $sig:declSig $val:declVal) =>
    `($mods:declModifiers theorem $id:declId $sig:declSig $val:declVal)

lemma aLemma : True := True.intro

end Old
