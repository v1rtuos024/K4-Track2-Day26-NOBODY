"""Answer assembly and an opt-in environment adapter for kit.loop.Agent.

Pass GuardedEnvironment(your_environment, required_fields=ask['require']) as
Agent.env. Append prompt.md to the harness prompt at construction. Neither
integration requires changing kit/. spar.py does not use this adapter.
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Iterable
from agent.guardrails import (
    check_grounding, scan_for_injected_instructions, redact,
    verify_arithmetic, abstention_policy,
)


def _text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def prepare_answer(answer: Mapping[str, Any], results: Iterable[Mapping], *,
                   required_fields: Iterable[str] = (), has_conflict: bool = False,
                   conflict_resolved: bool = False) -> dict:
    """Build an answer from clean successful tool results of this exchange.

    Reject unsafe sources, validate citations and required fields, redact every
    output field, and check numeric prose/structured values against cited rows.
    Semantic entailment and automatic conflict discovery remain model duties.
    """
    sources = {}
    private_values = []
    partial_anchors = set()
    for result in results:
        if not result.get('ok'):
            continue
        rows = result.get('rows') or []
        private = False
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            meta = row.get('meta') or {}
            if not isinstance(meta, Mapping):
                meta = {}
            fields = meta.get('private_fields') or row.get('private_fields') or {}
            if isinstance(fields, Mapping):
                private_values.extend(v for v in fields.values() if isinstance(v, str))
            if meta.get('private') or row.get('private'):
                private = True
                private_values.extend(v for k, v in row.items() if isinstance(v, str) and k != 'anchor')
        blob = _text([{k: v for k, v in row.items() if k not in ('meta', 'private_fields')}
                      if isinstance(row, Mapping) else row for row in rows])
        if private or scan_for_injected_instructions(blob).suspicious:
            continue
        anchors = result.get('anchors') or ()
        if result.get('partial'):
            partial_anchors.update(anchors)
        for anchor in anchors:
            if isinstance(anchor, str):
                sources[anchor] = blob
    clean = dict(answer)
    def redacted(value):
        if isinstance(value, str):
            return redact(value, private_values=private_values).redacted_text
        if isinstance(value, Mapping):
            return {k: redacted(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [redacted(v) for v in value]
        return value
    clean = redacted(clean)
    cited = clean.get('cited_anchors') or []
    if isinstance(cited, str):
        cited = [cited]
    clean['cited_anchors'] = cited
    grounding = check_grounding(clean, sources)
    # A structured anchor must be the same retrieved anchor contract as citations.
    anchor_invalid = 'anchor' in clean and clean['anchor'] not in cited
    evidence = [sources[a] for a in cited if isinstance(a, str) and a in sources]
    numeric_fields = {k: v for k, v in clean.items()
                      if k not in ('cited_anchors', 'anchor', 'text')}
    arithmetic = verify_arithmetic(str(clean.get('text', '')) + ' ' + _text(numeric_fields), evidence=evidence)
    suspicious = scan_for_injected_instructions(str(clean.get('text', ''))).suspicious
    partial = any(a in partial_anchors for a in cited if isinstance(a, str))
    resolution = clean.get('conflict_resolution')
    conflict_resolved = conflict_resolved and isinstance(resolution, str) and bool(resolution.strip())
    if anchor_invalid or abstention_policy(grounding, answer=clean, required_fields=required_fields,
            has_conflict=has_conflict, conflict_resolved=conflict_resolved,
            arithmetic=arithmetic, suspicious=suspicious, partial=partial):
        text = 'Chưa đủ bằng chứng an toàn để trả lời phần được yêu cầu.'
        if has_conflict and not conflict_resolved:
            text += ' Các nguồn mâu thuẫn; chưa xác định được nguồn phù hợp.'
        return {'text': text, 'cited_anchors': [], 'abstained': True}
    if has_conflict and conflict_resolved:
        clean['text'] = str(clean['text']) + ' Các nguồn có mâu thuẫn. ' + resolution
    return clean


class GuardedEnvironment:
    """Intercept answers before the underlying environment records them.

    One instance per exchange. Tool results are copied before quarantine so
    internal evidence accounting never changes the arena's recorded results.
    """
    def __init__(self, env, *, required_fields=(), has_conflict=False, conflict_resolved=False):
        self.env = env
        self.required_fields = tuple(required_fields)
        self.has_conflict = has_conflict
        self.conflict_resolved = conflict_resolved
        self.results = []

    def execute(self, action: dict) -> dict:
        if action.get('kind') == 'answer':
            clean = prepare_answer(action.get('args') or {}, self.results,
                required_fields=self.required_fields, has_conflict=self.has_conflict,
                conflict_resolved=self.conflict_resolved)
            # Agent.run returns this same action's args; update before recording.
            action['args'] = clean
            action['raw'] = 'ANSWER ' + json.dumps(clean, ensure_ascii=False)
            return self.env.execute(action)
        observation = self.env.execute(action)
        self.results.append(dict(observation))
        rows = observation.get('rows') or []
        private = any(isinstance(row, Mapping) and
                      (row.get('private') or isinstance(row.get('meta'), Mapping) and row['meta'].get('private'))
                      for row in rows)
        if private or scan_for_injected_instructions(_text(rows)).suspicious:
            return {**observation, 'rows': [], 'anchors': [], 'quarantined': True}
        clean_rows = []
        for row in rows:
            if not isinstance(row, Mapping):
                clean_rows.append(row)
                continue
            clean_row = {k: v for k, v in row.items() if k != 'private_fields'}
            if isinstance(clean_row.get('meta'), Mapping):
                clean_row['meta'] = {k: v for k, v in clean_row['meta'].items() if k != 'private_fields'}
            clean_rows.append(clean_row)
        return {**observation, 'rows': clean_rows}


def create_agent(model, env, *, task: str, team_prompt: str, required_fields=(),
                 has_conflict=False, conflict_resolved=False, limits=None, clock=None):
    """Construct the real kit loop with both team prompt and answer enforcement.

    Load prompt.md outside the pure guardrail path and pass its text here.
    The wrapped environment remains responsible for gateway enforcement.
    """
    from kit.loop.agent import Agent
    from kit.loop.prompt import render_system_prompt
    kwargs = {} if clock is None else {'clock': clock}
    return Agent(model, GuardedEnvironment(env, required_fields=required_fields,
                    has_conflict=has_conflict, conflict_resolved=conflict_resolved),
                 task=task, limits=limits,
                 system_prompt=render_system_prompt(limits=limits) + '\n\n' + team_prompt,
                 **kwargs)
