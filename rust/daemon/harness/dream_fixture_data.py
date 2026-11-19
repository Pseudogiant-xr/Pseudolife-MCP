"""Baseline recovery data for the dream-cursor harness.

Source commit: 7f2e6eacf77343e1d446ecbff38eee9447afbce6.
A one-time capture compared full catalogs and table rows after fixture seeding.
The baseline dream_cursor_oracle.seed/make_service and PostgresStorage produced
the responses and row changes for test_dream_cursor_cancel.py's two fixtures.
"""

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
