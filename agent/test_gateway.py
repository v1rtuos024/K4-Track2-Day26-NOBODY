"""Focused control-plane checks; no fixtures or kit files are modified."""
from dataclasses import replace
from agent.gateway import Command, Gateway
from agent.telemetry import RecordingGatewayContext
from kit.mcp.a2a import mint_delegation


def context(**changes):
    values = dict(act='learner:sv-0417', sub='agent:tutor', scopes=frozenset({'wiki.read'}),
                  credits=100, round=1, call_index=0, leases=(), history=())
    values.update(changes)
    return RecordingGatewayContext(**values)


def command(server='slides', tool='query', **changes):
    values = dict(cmd_id='cmd:1', kind='mcp', raw='', server=server, tool=tool,
                  args={'q': 'StateGraph'}, fields=(), headers={}, lease_id=None, call_index=0)
    values.update(changes)
    return Command(**values)


def test_route_preserves_explicit_version_and_does_not_mutate_input():
    cmd = command(headers={'Mcp-Replica': 'c'})
    d = Gateway(context()).decide(cmd)
    assert d.call.headers['mcp-replica'] == 'w'
    assert cmd.headers == {'Mcp-Replica': 'c'}
    d = Gateway(context()).decide(command('registry', 'provenance', args={'anchor': 'Deck:abcd1234/c'}))
    assert d.call.headers['mcp-replica'] == 'c'
    assert Gateway(context()).decide(command(args={'route': 'c'})).verdict == 'deny'


def test_live_lease_and_context_refresh():
    ctx = context()
    gateway = Gateway(ctx)
    cmd = command(tool='get_frame', args={'anchor': 'Frame:abcd1234/w/001'}, lease_id='lease')
    assert gateway.decide(cmd).verdict == 'deny'
    ctx.leases = ('lease',)
    assert gateway.decide(cmd).verdict != 'deny'
    ctx.leases = ()
    assert gateway.decide(cmd).verdict == 'deny'


def test_valid_write_and_conflict_retry():
    act = 'learner:sv-0417'
    provenance = {'command': {'server': 'registry', 'tool': 'provenance', 'args': {'anchor': act}},
                  'outcome': {'ok': True, 'etag': 'v1'}}
    ctx = context(scopes=frozenset({'wiki.read', 'wiki.write:progress'}), history=(provenance,))
    cmd = command('progress', 'record_mastery', args={'learner': act, 'concept': 'Concept:x'},
                  headers={'If-Match': 'v1', 'Idempotency-Key': 'once'})
    assert Gateway(ctx).decide(cmd).verdict != 'deny'
    assert Gateway(context(history=(provenance,))).decide(cmd).verdict == 'deny'
    assert Gateway(ctx).decide(replace(cmd, args={**cmd.args, 'learner': 'learner:other'})).verdict == 'deny'
    assert Gateway(ctx).decide(replace(cmd, headers={})).verdict == 'deny'
    failed = {'command': {'server': cmd.server, 'tool': cmd.tool, 'args': cmd.args, 'headers': cmd.headers},
              'outcome': {'ok': False, 'error': {'code': 'conflict'}}}
    ctx.history = (provenance, failed)
    assert Gateway(ctx).decide(cmd).verdict == 'deny'
    ctx.history += ({**provenance, 'outcome': {'ok': True, 'etag': 'v2'}},)
    assert Gateway(ctx).decide(replace(cmd, headers={'If-Match': 'v2', 'Idempotency-Key': 'once'})).verdict != 'deny'


def test_signed_delegation_identity_audience_expiry_replay():
    ctx = context()
    token = mint_delegation(ctx.act, 'a2a:curriculum-analyst', ttl=3, call_index=0)
    cmd = command('curriculum-analyst', 'which_days_cover', kind='a2a',
                  args={'concept': 'Concept:x', 'learner': ctx.act, 'delegation': token},
                  headers={'aud': 'a2a:curriculum-analyst'})
    gateway = Gateway(ctx)
    assert gateway.decide(cmd).verdict != 'deny'
    assert gateway.decide(cmd).verdict == 'deny'
    assert Gateway(ctx).decide(replace(cmd, headers={'aud': 'a2a:roster'})).verdict == 'deny'
    assert Gateway(ctx).decide(replace(cmd, call_index=4)).verdict == 'deny'
    wrong = mint_delegation('learner:other', 'a2a:curriculum-analyst', ttl=3)
    assert Gateway(ctx).decide(replace(cmd, args={**cmd.args, 'delegation': wrong})).verdict == 'deny'
    assert Gateway(ctx).decide(replace(cmd, args={'concept': 'Concept:x'})).verdict == 'deny'


def test_budget_catalogue_rate_window_and_late_release():
    gateway = Gateway(context())
    cmd = command('registry', 'list_servers', kind='discover', fields=('*',), args={})
    assert gateway.decide(cmd).call.fields == ('name',)
    assert gateway.decide(cmd).verdict == 'deny'
    assert Gateway(context()).decide(command('glossary', 'list_terms', args={})).call.fields == ('term',)
    assert Gateway(context()).decide(command(fields=('imaginary',))).verdict == 'deny'
    ctx = context(credits=5)
    gateway = Gateway(ctx)
    assert gateway.decide(command()).verdict == 'deny'
    ctx.round = 10
    assert gateway.decide(command()).verdict == 'deny'  # default query can return five rows
    assert gateway.decide(command(args={'q': 'x', 'limit': 1})).verdict != 'deny'
    assert Gateway(context()).decide(command(tool='search', fields=('title',))).call.tool == 'query'


def test_forged_card_and_token_are_denied():
    ctx = context()
    token = mint_delegation(ctx.act, 'a2a:curriculum-analyst', ttl=3).to_dict()
    cmd = command('curriculum-analyst', 'which_days_cover', kind='a2a',
                  headers={'aud': 'curriculum-analyst'},
                  args={'concept': 'Concept:x', 'delegation': token})
    forged = {**token, 'signature': '0' * 64}
    assert Gateway(ctx).decide(replace(cmd, args={**cmd.args, 'delegation': forged})).verdict == 'deny'
    assert Gateway(ctx).decide(replace(cmd, args={**cmd.args, 'agent_card': {'verified': True}})).verdict == 'deny'


def test_clean_blank_read_is_allowed_and_successful_write_not_repeated():
    assert Gateway(context()).decide(command('glossary', 'define', args={'term': 'StateGraph'})).verdict != 'deny'
    act = 'learner:sv-0417'
    cmd = command('progress', 'record_mastery', args={'learner': act, 'concept': 'Concept:x'},
                  headers={'If-Match': 'v1', 'Idempotency-Key': 'once'})
    ctx = context(scopes=frozenset({'wiki.read', 'wiki.write:progress'}), history=(
        {'command': {'server': 'registry', 'tool': 'provenance', 'args': {'anchor': act}},
         'outcome': {'ok': True, 'etag': 'v1'}},
        {'command': {'server': cmd.server, 'tool': cmd.tool, 'args': cmd.args}, 'outcome': {'ok': True}},))
    assert Gateway(ctx).decide(cmd).verdict == 'deny'
