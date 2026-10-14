"""agent/gateway.py — YOUR control plane. CONTRACTS.md section 4, exactly.

READ agent/README.md FIRST — it maps all five files in this directory to what
each is scored on. This file is the one CONTRACTS.md calls "the trusted
envelope's untrusted half": every single MCP / A2A / DISCOVER command your
agent's model wants to make passes through `Gateway.decide` before it is
allowed to happen.

WHY THERE IS NO `execute()` METHOD ON `GatewayContext` (read this before you
go looking for one — there isn't one, and that is not an oversight)
----------------------------------------------------------------------------
CONTRACTS.md section 4's trusted envelope, reproduced here because it is the
one diagram worth memorising:

    [ trusted ]   loop emits a raw action line
         v
    [ trusted ]   INTERCEPT + CANONICALISE -> Command        (kit/loop/agent.py)
         v
    [ UNTRUSTED ] Gateway.decide(cmd) -> Decision             <- THIS FILE
         v
    [ trusted ]   ENFORCE: honour the Decision, meter it,
                  apply the active mutation, execute the
                  ToolCall or refuse it                       (the arena)
         v
    [ trusted ]   RECORD the authoritative L1 event, then
                  RENDER the Observation                      (the arena)
         v
    [ trusted ]   the model sees the Observation

`decide()` returns a *decision*, never a *result*. You cannot reach a tool
server, a file, a socket, or a clock from in here — there is nothing to
call. Two things follow from that, and both matter more than they look:

  1. YOUR TRACE CANNOT BE FORGED. Every `command` / `decision` / `enforced`
     / `tool_call` / `tool_result` L1 event (CONTRACTS.md 5.2) is written by
     the arena, from what the arena itself actually did — never from
     anything you claimed happened. A student gateway that wanted to lie
     about having blocked an attack ("I totally denied that, trust me")
     simply has no channel to lie through: the only thing you ever hand
     back is this one small `Decision` value, and the arena is the one that
     turns it into history.
  2. NOBODY CAN ACCUSE YOU OF A CALL YOU DID NOT AUTHORISE, either. Because
     `decide()` is the ONLY door a command can walk through on its way to
     actually running, a prosecutor's `enforcement_failure` claim against
     you has exactly one thing to point at: the `Decision` you returned for
     that specific `cmd_id`. There is no ambiguity about "maybe the loop
     called the tool directly" — CONTRACTS.md 4.2 removed that path on
     purpose, and kit/loop/agent.py's own module docstring names the same
     invariant from the other side (the loop never imports this module,
     never sees a `Decision`, never executes anything itself).

The cost of that guarantee is that this file is PURE: synchronous, no I/O,
no threads, no `sleep`, 250 ms wall-clock deadline (RULES.md section 3).
Raising anything, returning something that is not a valid `Decision`, or
missing the deadline is treated by the arena as a DENIED command PLUS a 2
credit penalty PLUS an `integrity` event that hands the prosecutor a free
`enforcement_failure` — CONTRACTS.md 4.1's charging table, reproduced in
agent/README.md's own table. Getting this file to just plainly return valid
`Decision` values, every time, is worth more than getting it clever.

IMPLEMENTED POLICY
----------------------------------------------------------------------------
ROUTE selects a header replica, preserves explicit version anchors and upgrades
slides.search. ADMIT checks live leases and write provenance. AUTHORIZE checks
learner ownership, scopes, signed peer cards and per-hop delegation tokens.
BUDGET validates masks, narrows catalogue dumps and reserves future-round credits.
Missing A2A tokens fail closed; the simplified spar harness supplies no tokens.

Stdlib only. No network, no randomness, no wall-clock reads, no sleeping —
none of that would even survive the kernel sandbox (CONTRACTS.md 12), but
the point is this file has no reason to want any of it in the first place.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping, Protocol, runtime_checkable

# kit.mcp.types is a collaborator's file (workspace hard rule 2: import it,
# degrade gracefully). It is present as of this writing and is core, stable
# infrastructure (CONTRACTS.md 3.1) — but this module must still not fail to
# IMPORT if a concurrent edit ever breaks it transiently. When it is
# unavailable, `Decision.call` type-checking is skipped (not enforced), and
# `Gateway.decide` falls back to a minimal local dict-shaped stand-in so the
# rest of this file — everything that does not need a *real* ToolCall — still
# runs.
try:
    from kit.mcp.types import ToolCall
    _TOOLCALL_AVAILABLE = True
except ImportError:  # pragma: no cover - collaborator file
    ToolCall = Any  # type: ignore[assignment, misc]
    _TOOLCALL_AVAILABLE = False

# kit.loop.agent is also a collaborator's file, used only by this module's
# own __main__ demo (to build real Commands the same way the arena's trusted
# canonicaliser would) — never by decide() itself, which never touches the
# loop. Degraded the same way.
try:
    from kit.loop.agent import canonicalise_action as _canonicalise_action
except ImportError:  # pragma: no cover - collaborator file
    _canonicalise_action = None

from agent.telemetry import RecordingGatewayContext, Telemetry
from agent.strategy import cheap_mask, pick_replica, successor_of
from kit.mcp.specs import TOOL_SPECS, A2A_PEERS, cost
from kit.mcp.a2a import AGENT_CARDS, admit_skill, verify_delegation

__all__ = [
    "COMMAND_KINDS",
    "DECISION_VERDICTS",
    "Command",
    "Decision",
    "GatewayContext",
    "Gateway",
]

# CONTRACTS.md 4.1: `Command.kind` — "mcp" | "a2a" | "discover". An "answer"
# action is NEVER a Command (kit/loop/agent.py's own module docstring: "an
# answer is not a tool call routed to a server, so it never becomes a
# Command at all") — it is handled entirely by the loop/arena and never
# reaches `Gateway.decide`.
COMMAND_KINDS: frozenset[str] = frozenset({"mcp", "a2a", "discover"})

# CONTRACTS.md 4.1: `Decision.verdict` — the closed three-member set.
DECISION_VERDICTS: frozenset[str] = frozenset({"forward", "deny", "rewrite"})


@dataclass(frozen=True, slots=True)
class Command:
    """CONTRACTS.md 4.1, field for field — "canonicalised by the arena
    BEFORE the student sees it". You never build one of these from your own
    agent's raw text; the arena's canonicaliser (kit/loop/agent.py's
    `canonicalise_action`, run inside the trusted envelope) already did that
    work and minted `cmd_id` by the time `decide()` sees it. The
    `from_action_dict` classmethod below exists only so this file's own demo
    (and your local tests, if you write any) can build a realistic `Command`
    without duplicating the arena's canonicalisation logic."""

    cmd_id: str
    kind: str  # "mcp" | "a2a" | "discover" — see COMMAND_KINDS
    raw: str
    server: str
    tool: str
    args: dict
    fields: tuple[str, ...]
    headers: dict
    lease_id: str | None
    call_index: int

    def __post_init__(self) -> None:
        if not isinstance(self.cmd_id, str) or not self.cmd_id:
            raise ValueError(f"Command.cmd_id must be a non-empty str, got {self.cmd_id!r}")
        if self.kind not in COMMAND_KINDS:
            raise ValueError(f"Command.kind must be one of {sorted(COMMAND_KINDS)}, got {self.kind!r}")
        if not isinstance(self.server, str) or not self.server:
            raise ValueError(f"Command.server must be a non-empty str, got {self.server!r}")
        if not isinstance(self.tool, str) or not self.tool:
            raise ValueError(f"Command.tool must be a non-empty str, got {self.tool!r}")
        if not isinstance(self.args, dict):
            raise ValueError(f"Command.args must be a dict, got {type(self.args).__name__}")
        if not isinstance(self.headers, dict):
            raise ValueError(f"Command.headers must be a dict, got {type(self.headers).__name__}")
        if (
            not isinstance(self.call_index, int)
            or isinstance(self.call_index, bool)
            or self.call_index < 0
        ):
            raise ValueError(f"Command.call_index must be a non-negative int, got {self.call_index!r}")

    @classmethod
    def from_action_dict(cls, action: Mapping[str, Any], *, cmd_id: str) -> "Command":
        """Build a `Command` from the dict shape `kit.loop.agent.canonicalise_action`
        returns (`kind, raw, server, tool, args, fields, headers, lease_id,
        call_index` — everything except the arena-minted `cmd_id`, supplied
        here as a keyword). Raises `ValueError` if `action["kind"] ==
        "answer"` — an answer is never a Command (see the module docstring).
        This is a convenience for tests/demos, not something the real arena
        calls: the trusted envelope mints `cmd_id` itself and constructs the
        real `Command` on its own side of the boundary."""
        kind = action.get("kind")
        if kind == "answer":
            raise ValueError(
                "an 'answer' action never becomes a Command (kit/loop/agent.py: "
                "\"an answer is not a tool call routed to a server\") — do not "
                "route it through Gateway.decide at all"
            )
        return cls(
            cmd_id=cmd_id,
            kind=kind,
            raw=action["raw"],
            server=action["server"],
            tool=action["tool"],
            args=dict(action.get("args", {})),
            fields=tuple(action.get("fields", ())),
            headers=dict(action.get("headers", {})),
            lease_id=action.get("lease_id"),
            call_index=action.get("call_index", 0),
        )

    def to_dict(self) -> dict:
        return {
            "cmd_id": self.cmd_id,
            "kind": self.kind,
            "raw": self.raw,
            "server": self.server,
            "tool": self.tool,
            "args": dict(self.args),
            "fields": list(self.fields),
            "headers": dict(self.headers),
            "lease_id": self.lease_id,
            "call_index": self.call_index,
        }


@dataclass(frozen=True, slots=True)
class Decision:
    """CONTRACTS.md 4.1, field for field.

    Validated strictly (`__post_init__`) because a *structurally* invalid
    `Decision` is charged exactly like a raised exception — CONTRACTS.md
    4.1's charging table: "malformed Decision (schema-invalid) -> 2 cr
    penalty, command denied." Failing loudly HERE, in your own process
    during development, is strictly better than discovering it live in a
    duel as an unexplained penalty.

    `verdict == "deny"` requires a non-empty `reason` (CONTRACTS.md 4.1:
    "required when verdict == 'deny'; shown in the combat log") and
    forbids `call` — a real denial has nothing left to carry out.
    `verdict` in `("forward", "rewrite")` requires `call` to be set — the
    arena executes exactly that `ToolCall`, nothing else, per the trusted
    envelope's whole point (see the module docstring)."""

    verdict: str  # "forward" | "deny" | "rewrite" — see DECISION_VERDICTS
    reason: str | None = None
    call: "ToolCall | None" = None
    quarantine: bool = False
    note: str | None = None

    def __post_init__(self) -> None:
        if self.verdict not in DECISION_VERDICTS:
            raise ValueError(
                f"Decision.verdict must be one of {sorted(DECISION_VERDICTS)}, got {self.verdict!r}"
            )
        if self.verdict == "deny":
            if not isinstance(self.reason, str) or not self.reason.strip():
                raise ValueError("Decision.verdict=='deny' requires a non-empty 'reason'")
            if self.call is not None:
                raise ValueError("Decision.verdict=='deny' must not carry a 'call' — there is nothing to run")
        else:  # forward | rewrite
            if self.call is None:
                raise ValueError(f"Decision.verdict=={self.verdict!r} requires 'call' to be set")
            if _TOOLCALL_AVAILABLE and not isinstance(self.call, ToolCall):
                raise ValueError(
                    f"Decision.call must be a kit.mcp.types.ToolCall instance, got {type(self.call).__name__}"
                )
        if not isinstance(self.quarantine, bool):
            raise ValueError(f"Decision.quarantine must be a bool, got {self.quarantine!r}")
        if self.note is not None and not isinstance(self.note, str):
            raise ValueError(f"Decision.note must be a str or None, got {self.note!r}")

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "call": self.call.to_dict() if self.call is not None and hasattr(self.call, "to_dict") else self.call,
            "quarantine": self.quarantine,
            "note": self.note,
        }


@runtime_checkable
class GatewayContext(Protocol):
    """CONTRACTS.md 4.2 — "read-only, arena-provided". Note what this is
    NOT: unlike `Command`/`Decision` above, CONTRACTS.md writes this as a
    plain `class`, not a `@dataclass(frozen=True, ...)`. That is a real
    signal, not an inconsistency — a `GatewayContext` is a LIVE VIEW the
    arena owns and updates as the duel actually progresses (credits get
    spent, rounds advance, leases mint and expire), not a frozen snapshot
    you receive once. This file therefore models it as a structural
    `Protocol`: the arena hands your `Gateway.__init__` some object shaped
    like this (see `agent/telemetry.py`'s `RecordingGatewayContext` for a
    concrete example your own tests/demos can build), and you read
    `self.ctx.<field>` fresh every time `decide()` runs — NEVER cache a
    context field into a local variable across calls; if you need memory
    that persists across the duel (a cache, a budget tracker, drift notes),
    keep it on YOUR OWN `Gateway` instance attributes instead (CONTRACTS.md
    4.3: "Gateway is instantiated once per duel... persists across the 10
    rounds").

    There is deliberately no `execute` method here — see the module
    docstring's "WHY THERE IS NO execute() METHOD" section. `emit` is the
    ONE thing you can do that has any side effect at all, and it writes to
    `own_telemetry` only: your own side, never scored, never shown to the
    opponent (`agent/telemetry.py` wraps it)."""

    act: str  # "learner:sv-0417" — WHOM YOU SERVE. Authority derives from this.
    sub: str  # "agent:vlearn-tutor" — what you are. NOT authority.
    scopes: frozenset[str]  # {"wiki.read"}; writes need "wiki.write:progress" etc.
    credits: int  # remaining this duel
    round: int
    call_index: int
    leases: tuple[str, ...]  # live lease ids, arena-tracked
    history: tuple[Mapping[str, Any], ...]  # YOUR OWN prior (Command, Decision, outcome) triples this duel

    def emit(self, name: str, **payload: Any) -> None: ...


class Gateway:
    """One control plane per duel; context is reread on every decision."""

    def __init__(self, ctx: GatewayContext) -> None:
        self.ctx = ctx
        self._telemetry = Telemetry(ctx)

        self._credits_authorised = 0
        self._denied_cmd_ids: set[str] = set()
        self._used_tokens: set[str] = set()
        self._rate_calls: list[tuple[int, tuple[str, str]]] = []

    @staticmethod
    def _mapping(value: Any) -> Mapping:
        if isinstance(value, Mapping):
            return value
        if hasattr(value, "to_dict"):
            return value.to_dict()
        return {}

    def _outcomes(self):
        """Read arena-owned command/decision/outcome triples, never request data."""
        for entry in self.ctx.history:
            if not isinstance(entry, Mapping):
                continue
            command = self._mapping(entry.get("command"))
            outcome = self._mapping(entry.get("outcome"))
            yield command, outcome

    def decide(self, cmd: Command) -> Decision:
        """Route, admit, authorize and budget using only the live context."""
        self._telemetry.decision_seen(cmd)
        if any(not isinstance(k, str) for k in cmd.headers):
            return self.deny(cmd, "invalid header name")
        headers = {k.lower(): v for k, v in cmd.headers.items()}
        args = dict(cmd.args)

        # JOB 1 — ROUTE. Body data never selects a transport replica.
        if any(args.get(k) for k in ("route", "_route", "replica")):
            return self.deny(cmd, "replica routing must use the trusted header")
        server, tool = successor_of(cmd.server, cmd.tool) or (cmd.server, cmd.tool)
        spec = TOOL_SPECS.get((server, tool))
        if spec is None:
            return self.deny(cmd, "unknown server or tool")
        if (server in A2A_PEERS) != (cmd.kind == "a2a"):
            return self.deny(cmd, "command kind does not match transport")
        replica = pick_replica(path_id=args.get("path_id"), known_drifting=False).replica
        # An explicit version anchor is a legitimate request for that version.
        anchor = args.get("anchor")
        if isinstance(anchor, str):
            parts = anchor.split("/")
            if len(parts) > 1 and parts[1] in ("w", "c"):
                replica = parts[1]
        headers["mcp-replica"] = replica
        headers.pop("x-mcp-body-route", None)
        routed = replace(cmd, server=server, tool=tool, args=args, headers=headers)

        # JOB 2 — ADMIT. Refuse predictable protocol failures for free.
        if spec.needs_lease and (not cmd.lease_id or cmd.lease_id not in self.ctx.leases):
            return self.deny(cmd, "get_frame requires a live lease")
        outcomes = list(self._outcomes())
        if spec.is_write:
            target = args.get("learner") or args.get("act") or args.get("anchor")
            if server == "progress" and target != self.ctx.act:
                return self.deny(cmd, "progress write requires the current learner as target")
            etag = None
            for previous, outcome in outcomes:
                if (previous.get("server"), previous.get("tool")) == ("registry", "provenance"):
                    if self._mapping(previous.get("args")).get("anchor") == target and outcome.get("ok"):
                        etag = outcome.get("etag")
            if not etag or headers.get("if-match") != etag:
                return self.deny(cmd, "write requires If-Match from matching provenance")
            if not isinstance(headers.get("idempotency-key"), str) or not headers["idempotency-key"].strip():
                return self.deny(cmd, "write requires an idempotency key")
            for previous, outcome in outcomes:
                if (previous.get("server"), previous.get("tool"), previous.get("args")) != (server, tool, args):
                    continue
                old_headers = self._mapping(previous.get("headers"))
                old_etag = next((v for k, v in old_headers.items() if isinstance(k, str) and k.lower() == "if-match"), None)
                error = self._mapping(outcome.get("error"))
                if outcome.get("ok"):
                    return self.deny(cmd, "write already committed")
                if error.get("code") == "conflict" and old_etag == headers.get("if-match"):
                    return self.deny(cmd, "conflicting write must refresh provenance before retry")

        # JOB 3 — AUTHORIZE. Identity never substitutes for learner authority.
        for key in ("learner", "learner_id", "act", "subject"):
            if args.get(key) is not None and args[key] != self.ctx.act:
                return self.deny(cmd, "target learner is not the learner in ctx.act")
        if isinstance(args.get("target"), str) and args["target"].lower().startswith("learner:") and args["target"] != self.ctx.act:
            return self.deny(cmd, "target learner is not the learner in ctx.act")
        required_scope = f"wiki.write:{server}" if spec.is_write else "wiki.read"
        if required_scope not in self.ctx.scopes:
            return self.deny(cmd, "required scope is not granted: " + required_scope)
        token_id = None
        if headers.get("x-server-fingerprint") is not None:
            pins = getattr(self.ctx, "server_fingerprints", {})
            if not isinstance(pins, Mapping) or pins.get(server) != headers["x-server-fingerprint"]:
                return self.deny(cmd, "server fingerprint is not pinned by the context")
        if headers.get("x-card-signature") is not None:
            return self.deny(cmd, "detached card signature cannot replace a signed Agent Card")
        if server in A2A_PEERS:
            if headers.get("aud") not in (server, "a2a:" + server):
                return self.deny(cmd, "delegation audience does not match peer")
            card = args.get("agent_card", AGENT_CARDS[server])
            skill = "role_of" if (server, tool) == ("roster", "lookup_learner") else tool
            admission = admit_skill(card, skill)
            if not admission.admitted or admission.peer != server:
                return self.deny(cmd, "peer card or declared skill is not admitted")
            token = args.get("delegation", headers.get("delegation"))
            if token is None:
                return self.deny(cmd, "A2A requires a signed delegation token")
            verified = verify_delegation(token, aud="a2a:" + server,
                call_index=cmd.call_index, expected_act=self.ctx.act,
                seen_token_ids=self._used_tokens)
            if not verified.admitted:
                return self.deny(cmd, "delegation rejected: " + str(verified.reason))
            token_id = self._mapping(token).get("token_id")
            if token_id is None:
                token_id = getattr(token, "token_id", None)

        # JOB 4 — BUDGET. Narrow catalogue dumps and reserve future rounds.
        fields = tuple(cmd.fields)
        if (server, tool) in (("registry", "list_servers"), ("glossary", "list_terms")) and (not fields or "*" in fields):
            fields = cheap_mask(server, tool, ("name",) if server == "registry" else ("term",))
        elif fields == ("*",):
            fields = cheap_mask(server, tool, spec.default_fields)
        if server != cmd.server or tool != cmd.tool:
            fields = tuple(f for f in fields if f in spec.all_fields)
        if any(f not in spec.all_fields for f in fields):
            return self.deny(cmd, "unknown field in mask")
        if spec.rate_limit:
            maximum, window = spec.rate_limit
            recent = [(r, key) for r, key in self._rate_calls
                      if r > self.ctx.round - window]
            if sum(key == (server, tool) for _, key in recent) >= maximum:
                return self.deny(cmd, "tool rate window exhausted")
        rows = args.get("limit", 5) if spec.row_weight else 0
        if not isinstance(rows, int) or isinstance(rows, bool) or (rows < 1 and spec.row_weight):
            return self.deny(cmd, "invalid query row limit")
        estimated = cost(server, tool, fields, n_rows=rows)
        reserve = max(0, 10 - self.ctx.round) * 8
        if estimated > self.ctx.credits - reserve:
            narrowed = cheap_mask(server, tool, spec.default_fields)
            if cost(server, tool, narrowed, n_rows=rows) < estimated:
                fields = narrowed
                estimated = cost(server, tool, fields, n_rows=rows)
        if estimated > self.ctx.credits - reserve:
            return self.deny(cmd, "insufficient credits after reserving future rounds")
        routed = replace(routed, fields=fields)
        self._credits_authorised += estimated
        self._rate_calls = self._rate_calls + [(self.ctx.round, (server, tool))]
        if token_id is not None:
            self._used_tokens = self._used_tokens | {token_id}
        decision = Decision(verdict="forward" if routed == cmd else "rewrite",
                            call=self._to_tool_call(routed))
        self._telemetry.decision_made(cmd, decision)
        return decision

    def deny(self, cmd: Command, reason: str) -> Decision:
        """Construct a free refusal and record the reason in own telemetry."""
        self._denied_cmd_ids.add(cmd.cmd_id)
        decision = Decision(verdict="deny", reason=reason)
        self._telemetry.decision_made(cmd, decision)
        return decision

    def _to_tool_call(self, cmd: Command) -> "ToolCall":
        """`Command` -> the `ToolCall` (CONTRACTS.md 3.1) the arena will
        actually execute on a `forward`/`rewrite` verdict. When
        `kit.mcp.types` is unavailable (see the module-level import guard),
        falls back to a plain dict carrying the identical fields — `Decision`
        accepts it either way (the `ToolCall` isinstance check inside
        `Decision.__post_init__` only runs when the real class loaded)."""
        fields = {
            "server": cmd.server,
            "tool": cmd.tool,
            "args": dict(cmd.args),
            "fields": cmd.fields,
            "headers": dict(cmd.headers),
            "lease_id": cmd.lease_id,
            "call_index": cmd.call_index,
        }
        if _TOOLCALL_AVAILABLE:
            return ToolCall(**fields)
        return fields  # type: ignore[return-value]
