module
public import Lean
meta import Lean
public import Support.Context

namespace Audit

/-- 中文说明：默认私有，不能进入待审阅队列。 -/
def hidden : Nat := contextValue

public def unexposed : Nat := 1

@[expose] public
section

/-- 跨行公开区域内的中文声明。 -/
def visible : Nat := 1

set_option backward.privateInPublic true
set_option backward.privateInPublic.warn false
private def secret : Nat := 2

public def referringToPrivate : Nat := secret
set_option backward.privateInPublic false

public def universeValue.{u} (α : Type u) (x : α) : α := x

/-- Axiom with no references in its body. -/
axiom assumed : True

theorem verified : True := True.intro

structure Record where
  /-- 字段说明。 -/
  amount : Nat
  bounded : Fin (amount + 1)

class Property where
  holds : True

class inductive Flag where
  | set : Flag

inductive Tree where
  | leaf : Tree
  | node : Tree → Tree

instance : Inhabited Record := ⟨⟨0, 0⟩⟩

instance namedInstance : Inhabited Flag := ⟨Flag.set⟩

instance : Decidable (2 + 2 = 4) := isTrue (by native_decide)

mutual
  def even (n : Nat) : Bool := match n with
    | 0 => true
    | n + 1 => odd n
  def odd (n : Nat) : Bool := match n with
    | 0 => false
    | n + 1 => even n
end

macro "unusedQuotation" : command =>
  `(command| public def phantom : Nat := 4)

public def _root_.rootValue : Nat := 1

end

def hiddenAfterSection : Nat := 5

end Audit
