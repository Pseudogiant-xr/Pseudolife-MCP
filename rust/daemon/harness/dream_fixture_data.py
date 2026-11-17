"""Generated fixture data; do not edit by hand.

Source commit: 7f2e6eacf77343e1d446ecbff38eee9447afbce6.
Capture command: python baseline_capture.py <baseline-checkout> <output-directory>
The baseline dream_cursor_oracle.seed/make_service and PostgresStorage produced
these responses and row changes for test_dream_cursor_cancel.py's two fixtures.
RELATIONS records the same constructor's builtin relation rows.
"""

RELATIONS = (('depends-on', 'src requires dst to function', True, None),
 ('part-of', 'src is a component of dst', True, None),
 ('hosts', 'src is the host/platform for dst', False, None),
 ('runs-on', 'src executes on host/platform dst', False, 'hosts'),
 ('uses', 'src makes use of dst', False, None),
 ('configures', 'src sets configuration for dst', False, None),
 ('stores-data-in', 'src persists its data in dst', False, None),
 ('implements', 'src (a source file) realizes concept/role dst', False, None),
 ('related-to', 'untyped catch-all association', False, None),
 ('prefers', 'src (a task-type) prefers approach/tool dst (positive lesson)', False, None),
 ('avoids', 'src (a task-type) should avoid dead-end dst (negative lesson)', False, None))

RECOVERY = {'panic': {'fixture': {'meta': {'dream_ack_secret_v1': '1111111111111111111111111111111111111111111111111111111111111111'},
                       'entries': [{'text': 'panic recovery probe', 'ts': 1.0}]},
           'response': {'cursor': 0.0,
                        'count': 1,
                        'entries': [{'text': 'panic recovery probe',
                                     'timestamp': 1.0,
                                     'episode_id': None,
                                     'db_id': 1,
                                     'dream_id': None,
                                     'source': 'notes',
                                     'authority': None,
                                     'distortion_tolerance': None}],
                        'commit_token': 'v1.eyJiIjoicG9zdGdyZXMiLCJnIjoiMDJkNDQ5YTMxZmJiMjY3YzhmMzUyZTk5NjhhNzllM2U1ZmM5NWMxYmJlYWE1MDJmZDY0NTRlYmRlNWE0YmVkYyIsImkiOlsxXSwidCI6MS4wfQ.FFvnplxRz9pBtrP9ywj4h65s85rsDG2-UXHCm-K2uuw'},
           'updates': [{'table': 'meta', 'key': 'writer_lease_epoch', 'values': {'value': 2}}]},
 'cancel': {'fixture': {'meta': {'dream_ack_secret_v1': '1111111111111111111111111111111111111111111111111111111111111111'},
                        'entries': [{'text': 'cancellation probe', 'ts': 1.0}]},
            'response': {'cursor': 1.0, 'count': 0, 'entries': []},
            'updates': [{'table': 'entries', 'key': 1, 'values': {'dream_state': 'acknowledged'}},
                        {'table': 'meta', 'key': 'cortex_dream_cursor', 'values': {'value': 1.0}},
                        {'table': 'meta', 'key': 'writer_lease_epoch', 'values': {'value': 2}}]}}
