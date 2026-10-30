"""Console graph and recall reads: compare HTTP status, body and bank state."""


def register(h):
    paths = [
        '/api/graph', '/api/graph?entity=Alpha',
        '/api/graph?entity=Alpha&to=Gamma&depth=2&include_facts=false',
        '/api/graph?scope=read-demo', '/api/graph?scope=missing', '/api/graph?limit=2',
        '/api/graph?limit=0', '/api/graph?limit=-1',
        '/api/graph?limit=%D9%A2', '/api/graph?limit=1_0',
        '/api/graph?depth=bad&include_facts=bad&limit=bad',
        '/api/graph?entity=missing', '/api/graph/projects',
        '/api/graph/digest', '/api/graph/path',
        '/api/graph/path?source=Alpha&target=Gamma',
        '/api/graph/path?source=Alpha&target=Gamma&max_hops=1',
        '/api/graph/path?source=Alpha&target=missing',
        '/api/graph/entity-provenance',
        '/api/graph/entity-provenance?entity=Alpha',
        '/api/graph/entity-provenance?entity=Alpha&limit=-1',
        '/api/graph/entity-provenance?entity=Alpha&limit=999999999999999999999999',
        '/api/graph/entity-provenance?entity=Alpha&limit=-999999999999999999999999',
        '/api/graph/entity-provenance?entity=Alpha&limit=' + '9' * 4300,
        '/api/graph/entity-provenance?entity=Alpha&limit=' + '9' * 4301,
        '/api/wiki', '/api/wiki?entity=Alpha', '/api/wiki?entity=Alias',
        '/api/recall', '/api/recall?q=%20&hops=-2&top_k=bad',
        '/api/recall?q=Alpha&hops=1&top_k=2',
        '/api/recall?q=Alpha&hops=1&top_k=999999999999999999999999',
        '/api/dream/status',
    ]
    scenarios = {'console-graph-reads': {
        'config': None,
        'seed': [h.fact('Alpha', 'color', 'blue'),
                 h.relate('Alpha', 'related_to', 'Beta'),
                 h.relate('Beta', 'related_to', 'Gamma'),
                 h.alias('Alpha', 'Alias')],
        'ops': [{'op': 'rest', 'method': 'GET', 'path': p} for p in paths],
    }}

    scenarios['console-graph-dossier'] = {
        'config': 'memory:\n  scopes:\n    rollup:\n      read-demo: umbrella\n',
        'seed': [
            *scenarios['console-graph-reads']['seed'],
            h.store('Alpha is blue and connects to Beta.', source='read-demo'),
            h.world_set('Alpha', 'reference', 'external fact', source_url='https://example.com/alpha'),
            h.sql("INSERT INTO entity_sources(entity_id,source,count,origin,updated_at) SELECT id,'read-demo',2,'manual',1700000000 FROM entities WHERE canonical='alpha'", after_stop=True),
            h.sql("INSERT INTO memory_traces(entity_norm,attribute_norm,entry_id,created_at) SELECT 'alpha','color',id,1700000000 FROM entries", after_stop=True),
            h.sql("INSERT INTO entity_proposals(kind,entity_id,reason,created_at) SELECT 'junk',id,'fixture',1700000000 FROM entities WHERE canonical='alpha'", after_stop=True),
            h.sql("INSERT INTO meta(key,value) VALUES ('graph_digest','{\"timestamp\":1700000000,\"nodes\":3}'::jsonb) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value", after_stop=True),
        ],
        'ops': [{'op': 'rest', 'method': 'GET', 'path': p} for p in [
            '/api/wiki?entity=Alias', '/api/graph/entity-provenance?entity=Alias&limit=1',
            '/api/graph?scope=read-demo', '/api/graph/projects', '/api/graph/digest',
        ]],
    }
    return scenarios
