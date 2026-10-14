"""Pure answer checks for retrieved data, privacy and supported numeric claims.

Anchor membership is structural, not proof that a source entails a claim.
Numeric checks compare values/precision and explicit arithmetic; they do not
verify factual meaning. Supply retrieved evidence and known private values.
"""

from __future__ import annotations

import re
import ast
import unicodedata
from decimal import Decimal, InvalidOperation, localcontext
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

# kit.world.anchor is a collaborator's file (workspace hard rule 2). Present
# and stable as of this writing; degraded gracefully so `check_grounding`
# still runs (with the anchor-syntax leg of the check skipped, not silently
# treated as passing) if it is ever briefly unimportable.
try:
    from kit.world.anchor import Anchor, AnchorSyntaxError
    _ANCHOR_AVAILABLE = True
except ImportError:  # pragma: no cover - collaborator file
    Anchor = None  # type: ignore[assignment]
    AnchorSyntaxError = ValueError  # type: ignore[assignment, misc]
    _ANCHOR_AVAILABLE = False

__all__ = [
    "GroundingResult",
    "check_grounding",
    "InjectionScanResult",
    "scan_for_injected_instructions",
    "RedactionResult",
    "redact",
    "ArithmeticCheckResult",
    "verify_arithmetic",
    "abstention_policy",
]


# ---------------------------------------------------------------------------
# 1. GROUNDING — real, working.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GroundingResult:
    grounded: bool
    cited: tuple[str, ...]
    ungrounded: tuple[str, ...]  # cited, syntactically valid, but never retrieved this exchange
    malformed: tuple[str, ...]  # cited but not even valid Anchor syntax


def check_grounding(
    answer: Mapping[str, Any],
    retrieved_anchors: Iterable[str],
    *,
    require_citation: bool = True,
) -> GroundingResult:
    """"Every claim traces to a returned anchor" (this task's own brief),
    made concrete: every string in `answer["cited_anchors"]` must (a) parse
    as valid `ns:slug[/rev][/idx][#span]` syntax (`kit.world.anchor.Anchor`)
    and (b) be a member of `retrieved_anchors` — the anchors YOUR exchange
    actually got back from a `tool_result` this round, not anchors you
    recognise from having seen them before, and not anchors you are
    inferring exist.

    `retrieved_anchors` is YOUR responsibility to assemble honestly — the
    right source is the union of every `tool_result.anchors` your agent
    received this exchange (CONTRACTS.md 5.2's `tool_result` event field),
    never something wider like "every anchor this world index contains".
    Passing a wider set than what you actually retrieved makes this
    function agree with citations that are `ungrounded` in the sense that
    actually matters (CONTRACTS.md 6.1's rubric class) even though this
    function would call them grounded.

    Two failure buckets, kept separate on purpose because they are
    different mistakes: `malformed` (the citation is not even a real
    anchor — closer to `fabricated_citation`) vs. `ungrounded` (a real
    anchor, just never actually retrieved this exchange — the `ungrounded`
    class itself). `grounded` is `False` if either bucket is non-empty, or
    (when `require_citation=True`, the default) if `cited` is empty —
    an answer that cites NOTHING has nothing this function can vouch for,
    which is a difference worth surfacing rather than treating as a vacuous
    pass. Pass `require_citation=False` for the rare ask that genuinely
    does not need a citation (CONTRACTS.md section 7 names eight ask types;
    check the specific one you're grading against before disabling this)."""
    cited = tuple(answer.get("cited_anchors") or ())
    retrieved = frozenset(retrieved_anchors)

    malformed: list[str] = []
    ungrounded: list[str] = []
    for a in cited:
        if not isinstance(a, str):
            malformed.append(str(a))
            continue
        if _ANCHOR_AVAILABLE:
            try:
                Anchor.parse(a)
            except AnchorSyntaxError:
                malformed.append(a)
                continue
        if a not in retrieved:
            ungrounded.append(a)

    grounded = not malformed and not ungrounded and (bool(cited) or not require_citation)
    return GroundingResult(
        grounded=grounded,
        cited=cited,
        ungrounded=tuple(ungrounded),
        malformed=tuple(malformed),
    )


# ---------------------------------------------------------------------------
# 2. INJECTED-INSTRUCTION DETECTION.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InjectionScanResult:
    suspicious: bool
    matched_patterns: tuple[str, ...]


def _normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    return " ".join("".join(c for c in text if unicodedata.category(c) != "Cf").casefold().split())


_INJECTION_PATTERNS = {
    "instruction_override": r"(?:ignore|disregard|forget)\s+(?:(?:all|every|your|the|any)\s+)*(?:previous|prior|above|earlier|system)\s+(?:instructions?|rules?|prompts?)",
    "system_impersonation": r"system\s+override|(?:\[system\]|<\|(?:system|im_start)\|>|<system>)",
    "vietnamese_override": r"bỏ qua\s+(?:(?:mọi|tất cả|các)\s+)*(?:hướng dẫn|chỉ thị|quy tắc)",
    "secret_exfiltration": r"(?:print|reveal|expose|send|output|tiết lộ|in ra)\b.{0,100}(?:api[_ -]?key|password|secret|private|mật khẩu|riêng tư|biến môi trường)",
    "authority_override": r"(?:you must now|as the system|developer message|also record this for|instead,? record)",
}


def scan_for_injected_instructions(text: str) -> InjectionScanResult:
    """Flag directive-shaped retrieved text, including Unicode obfuscation.

    A match means quarantine the source, not obey it or refuse a clean question.
    Security teaching examples may match too; they remain quoted data.
    """
    normalised = _normalise(text)
    hits = tuple(name for name, pattern in _INJECTION_PATTERNS.items()
                 if re.search(pattern, normalised))
    return InjectionScanResult(bool(hits), hits)


@dataclass(frozen=True, slots=True)
class RedactionResult:
    redacted_text: str
    hits: tuple[str, ...]


def redact(text: str, *, private_values: Iterable[str] = ()) -> RedactionResult:
    """Remove known private excerpts and common contact/credential forms.

    Known values must come from retrieved private metadata, not corpus-wide I/O.
    Unknown private prose cannot be inferred reliably from text alone.
    """
    hits = []
    output = unicodedata.normalize("NFKC", text)
    for value in sorted({v for v in private_values if isinstance(v, str) and v.strip()}, key=len, reverse=True):
        # Match normalised whitespace/case without exposing the value in telemetry.
        pattern = r"\s+".join(re.escape(part) for part in unicodedata.normalize("NFKC", value).split())
        output, count = re.subn(pattern, "[REDACTED]", output, flags=re.IGNORECASE)
        if count:
            hits.append("private_value")
        # Catch contiguous excerpts long enough to trigger the privacy detector.
        words = unicodedata.normalize("NFKC", value).split()
        for start in range(len(words)):
            chunk = []
            for word in words[start:]:
                chunk.append(word)
                if len(" ".join(chunk)) >= 40:
                    break
            if len(" ".join(chunk)) < 40:
                continue
            excerpt = r"\s+".join(re.escape(word) for word in chunk)
            output, count = re.subn(excerpt, "[REDACTED]", output, flags=re.IGNORECASE)
            if count:
                hits.append("private_excerpt")
    patterns = {
        "email": r"(?<![\w.])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?!\w)",
        "phone": r"(?<!\w)(?:\+?\d[\d ()-]{7,}\d)(?!\w)",
        "credential": r"\b(?:[A-Z][A-Z0-9_]*API_KEY|api[_ -]?key|password|mật khẩu)\s*[:=]\s*[^\s,;]+",
    }
    for name, pattern in patterns.items():
        def substitute(match):
            if name == "phone":
                digits = sum(c.isdigit() for c in match.group())
                if not 9 <= digits <= 15 or not any(c in match.group() for c in "+-() "):
                    return match.group()
            hits.append(name)
            return "[REDACTED]"
        output = re.sub(pattern, substitute, output, flags=re.IGNORECASE)
    return RedactionResult(output, tuple(dict.fromkeys(hits)))


@dataclass(frozen=True, slots=True)
class ArithmeticCheckResult:
    checked: bool
    ok: bool | None
    detail: str


_NUMBER_RE = re.compile(r"(?<![\w.])(?P<value>-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)(?P<unit>[kKmMbB%]?)(?!\w|\.\d)")
_EQUATION_RE = re.compile(r"(?P<expr>-?\d+(?:\.\d+)?(?:\s*[+*/×÷−-]\s*-?\d+(?:\.\d+)?)+)\s*=\s*(?P<result>-?\d+(?:\.\d+)?)")


def _numbers(text: str) -> tuple[str, ...]:
    # Anchor identifiers are not numeric factual claims.
    text = re.sub(r"\b[A-Z][A-Za-z]*:[\w/#.-]+", "", text)
    return tuple(match['value'] for match in _NUMBER_RE.finditer(text))


def _numeric_claims(text: str):
    text = re.sub(r"\b[A-Z][A-Za-z]*:[\w/#.-]+", "", text)
    return tuple((Decimal(m['value']), m['unit'].casefold()) for m in _NUMBER_RE.finditer(text))


def _decimal_expression(expression: str) -> Decimal:
    expression = expression.replace("×", "*").replace("÷", "/").replace("−", "-")
    tree = ast.parse(expression, mode="eval")
    if sum(1 for _ in ast.walk(tree)) > 64:
        raise ValueError("expression too large")
    def calculate(node):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return Decimal(ast.get_source_segment(expression, node))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            value = calculate(node.operand)
            return -value if isinstance(node.op, ast.USub) else value
        if isinstance(node, ast.BinOp):
            left, right = calculate(node.left), calculate(node.right)
            if isinstance(node.op, ast.Add): return left + right
            if isinstance(node.op, ast.Sub): return left - right
            if isinstance(node.op, ast.Mult): return left * right
            if isinstance(node.op, ast.Div): return left / right
        raise ValueError("unsupported arithmetic")
    with localcontext() as ctx:
        ctx.prec = 50
        return calculate(tree.body)


def verify_arithmetic(text: str, *, evidence: Iterable[str] | None = None) -> ArithmeticCheckResult:
    """Check explicit equations and numeric precision against cited source text.

    Without source evidence, ordinary numbers remain unverified. A computed
    number is supported only when its operands are supported and math agrees.
    Numeric occurrence alone cannot prove the surrounding factual claim.
    """
    numbers = _numbers(text)
    equations = tuple(_EQUATION_RE.finditer(text))
    computed = set()
    for equation in equations:
        try:
            if _decimal_expression(equation['expr']) != Decimal(equation['result']):
                return ArithmeticCheckResult(True, False, "incorrect equation")
            computed.add(equation.span('result'))
        except (ValueError, SyntaxError, InvalidOperation, ArithmeticError):
            return ArithmeticCheckResult(True, False, "invalid or unsupported equation")
    if not numbers:
        return ArithmeticCheckResult(True, True, "no numeric claims")
    if evidence is None:
        return ArithmeticCheckResult(bool(equations), None,
                                     "equations checked; factual numeric claims require evidence")
    supported = [claim for source in evidence for claim in _numeric_claims(source)]
    # Strip only calculated RHS tokens; all operands still need evidence.
    claims = text
    for start, end in sorted(computed, reverse=True):
        claims = claims[:start] + " " * (end-start) + claims[end:]
    for value, unit in _numeric_claims(claims):
        if not any(unit == source_unit and value == source and
                   value.as_tuple().exponent >= source.as_tuple().exponent
                   for source, source_unit in supported):
            return ArithmeticCheckResult(True, False, "numeric value or precision unsupported by cited evidence")
    return ArithmeticCheckResult(True, True, "numeric values, precision and explicit equations checked")


def abstention_policy(
    grounding: GroundingResult, *, required_fields: Iterable[str] = (),
    answer: Mapping[str, Any] | None = None, has_conflict: bool = False,
    conflict_resolved: bool = False, arithmetic: ArithmeticCheckResult | None = None,
    suspicious: bool = False, partial: bool = False,
) -> bool:
    """Abstain on missing evidence or an unresolved required claim.

    A quarantined source does not force refusal when independent clean evidence
    answers the question. The caller marks suspicious only for evidence in use.
    """
    if not grounding.grounded or suspicious or partial or (has_conflict and not conflict_resolved):
        return True
    if arithmetic is not None and arithmetic.ok is not True:
        return True
    if required_fields:
        if answer is None:
            return True
        if any(key not in answer or answer[key] is None or answer[key] == "" or answer[key] == []
               for key in required_fields):
            return True
    return False
