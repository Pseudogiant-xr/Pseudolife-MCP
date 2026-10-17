"""Saved opinions must follow the evidence and policy that produced them."""
import pytest
from tests.test_queue_judges_service import (  # noqa: F401
    svc, pg_conn, pg_url, _link, _LinkJudge, _pending_link, _CandidateJudge, _propose,
)


def _judge():
    return _LinkJudge({('alpha-tool', 'uses', 'beta-lib'): ('accept', .99, None)})


def test_model_change_rejudges_pending_opinion(svc):
    svc.config.memory.deep_dream.link_judge_mode = 'shadow'
    _link(svc, 'alpha-tool', 'uses', 'beta-lib')
    first = _judge()
    assert svc.deep_dream_judge_links(first)['judged'] == 1
    assert svc.deep_dream_judge_links(first)['judged'] == 0
    second = _judge()
    second.model = 'different-judge'
    assert svc.deep_dream_judge_links(second)['judged'] == 1


def test_mode_change_rejudges_before_applying(svc):
    cfg = svc.config.memory.deep_dream
    cfg.link_judge_mode = 'shadow'
    pid = _link(svc, 'alpha-tool', 'uses', 'beta-lib')
    judge = _judge()
    svc.deep_dream_judge_links(judge)
    cfg.link_judge_mode = 'auto'
    result = svc.deep_dream_judge_links(judge)
    assert result['judged'] == 1 and result['applied'] == 1
    assert _pending_link(svc, pid) is None


def test_evidence_changed_during_inference_cannot_apply(svc):
    svc.config.memory.deep_dream.link_judge_mode = 'auto'
    pid = _link(svc, 'alpha-tool', 'uses', 'beta-lib')

    class ChangingJudge(_LinkJudge):
        def judge_links(self, rows):
            svc.graph_relate('alpha-tool', 'uses', 'gamma-lib')
            return super().judge_links(rows)

    judge = ChangingJudge({('alpha-tool', 'uses', 'beta-lib'): ('accept', .99, None)})
    result = svc.deep_dream_judge_links(judge)
    assert result.get('applied', 0) == 0
    assert _pending_link(svc, pid) is not None


def test_evidence_changed_while_signing_cannot_apply(svc, monkeypatch):
    """The queue is signed with the lock released, so a write can land
    between the evidence read and the stale check. The model is shown the
    packs that were signed, and validate() re-signs under the lock, so the
    opinion is neither recorded nor applied."""
    svc.config.memory.deep_dream.link_judge_mode = 'auto'
    pid = _link(svc, 'alpha-tool', 'uses', 'beta-lib')
    original = svc._enrich_link_proposals_from
    def racing(rows, evidence):
        if not svc._lock.locked():       # the unlocked signing pass
            svc.graph_relate('alpha-tool', 'uses', 'gamma-lib')
        return original(rows, evidence)
    monkeypatch.setattr(svc, '_enrich_link_proposals_from', racing)
    result = svc.deep_dream_judge_links(_judge())
    assert result.get('applied', 0) == 0
    row = _pending_link(svc, pid)
    assert row is not None and row['judge_verdict'] is None


def test_changed_evidence_invalidates_saved_opinion(svc):
    svc.config.memory.deep_dream.link_judge_mode = 'shadow'
    _link(svc, 'alpha-tool', 'uses', 'beta-lib')
    judge = _judge()
    assert svc.deep_dream_judge_links(judge)['judged'] == 1
    svc.graph_relate('alpha-tool', 'uses', 'gamma-lib')
    assert svc.deep_dream_judge_links(judge)['judged'] == 1


def test_explicit_requeue_is_bounded_and_preserves_terminal_decisions(svc):
    svc.config.memory.deep_dream.link_judge_mode = 'shadow'
    ids = [_link(svc, f'tool-{i}', 'uses', f'library-{i}') for i in range(4)]
    judge = _LinkJudge({(f'tool-{i}', 'uses', f'library-{i}'): ('accept', .99, None)
                        for i in range(4)})
    assert svc.deep_dream_judge_links(judge)['judged'] == 4
    svc.graph_reject_proposal(ids[-1])
    assert svc.review_rejudge('link', limit=2)['requeued'] == 2
    assert svc.review_rejudge('link', limit=2)['requeued'] == 1
    assert svc.review_rejudge('link', limit=2)['requeued'] == 0
    assert svc._storage.get_proposal(ids[-1])['status'] == 'rejected'


def test_candidate_memo_follows_model_and_full_input(svc):
    cfg = svc.config.memory.deep_dream
    cfg.candidate_judge_mode = 'shadow'
    rows = [{'src': 'alpha', 'dst': 'beta', 'similarity': .8,
             'src_snippets': ['original evidence'], 'dst_snippets': ['context']}]
    judge = _CandidateJudge({('alpha', 'beta'): ('leave', .9, None, None, None)})
    assert svc.deep_dream_judge_candidates(judge, candidates=rows)['judged'] == 1
    assert svc.deep_dream_judge_candidates(judge, candidates=rows)['judged'] == 0
    rows[0]['src_snippets'] = ['corrected evidence']
    assert svc.deep_dream_judge_candidates(judge, candidates=rows)['judged'] == 1
    judge.model = 'new-candidate-judge'
    assert svc.deep_dream_judge_candidates(judge, candidates=rows)['judged'] == 1


def test_entity_reject_rolls_back_if_pair_closure_fails(svc, monkeypatch):
    import pytest
    pid = _propose(svc, 'alpha service', 'alpha svc')
    def fail(*args, **kwargs):
        raise RuntimeError('injected pair closure failure')
    monkeypatch.setattr(svc._storage, 'dismiss_pair', fail)
    with pytest.raises(RuntimeError, match='injected pair'):
        svc.graph_reject_entity_proposal(pid)
    assert svc._storage.get_entity_proposal(pid)['status'] == 'pending'
    assert svc._storage.recent_entity_decisions() == []


def test_changed_served_identity_invalidates_opinion(svc):
    svc.config.memory.deep_dream.link_judge_mode = 'shadow'
    _link(svc, 'alpha-tool', 'uses', 'beta-lib')
    judge = _judge()
    judge.served_model = 'model-one'
    assert svc.deep_dream_judge_links(judge)['judged'] == 1
    judge.served_model = 'model-two'
    assert svc.deep_dream_judge_links(judge)['judged'] == 1


def test_analyzer_reconciliation_runs_when_filing_disabled(svc, monkeypatch):
    cfg = svc.config.memory.deep_dream
    cfg.analyzer_file_duplicates = False
    called = []
    monkeypatch.setattr(svc._storage, 'reconcile_analyzer_proposals',
                        lambda **kw: called.append(kw) or {'closed': 0})
    svc.analyzer_duplicate_tick()
    assert len(called) == 1
    cfg.judges_enabled = False
    svc.analyzer_duplicate_tick()
    assert len(called) == 1


def test_unknown_served_id_cannot_authorize_merge(svc):
    from tests.test_queue_judges_service import _MergeJudge, _SecondJudge
    cfg = svc.config.memory.deep_dream
    cfg.judge_mode = 'auto'
    cfg.judge_second_opinion = True
    svc.store('omega svc runs the omega ingestion', source='t')
    svc.store('omega service is the deployed omega daemon', source='t')
    pid = _propose(svc, 'omega svc', 'omega service')
    verdict = {('omega svc', 'omega service'): ('accept', .99)}
    first, second = _MergeJudge(verdict), _SecondJudge(verdict)
    second.served_model = None
    svc.deep_dream_judge(first)
    result = svc.deep_dream_judge(first, second_extractor=second)
    assert result['auto_accepted'] == 0
    assert svc._storage.get_entity_proposal(pid)['status'] == 'pending'


@pytest.mark.parametrize('mode,verdict,c1,c2', [('auto-reject', 'reject', .6, .9),
                                                 ('auto', 'reject', .6, .9),
                                                 ('auto', 'accept', .9, .9)])
def test_second_vote_cannot_pair_with_a_first_verdict_requeued_mid_call(
        svc, mode, verdict, c1, c2):
    # The evidence signature strips every judge* key, so it cannot see a
    # requeue clear the first verdict while the second model is thinking.
    from tests.test_queue_judges_service import _MergeJudge, _SecondJudge
    cfg = svc.config.memory.deep_dream
    cfg.judge_mode = mode
    cfg.judge_second_opinion = True
    cfg.judge_reject_min_confidence = .8
    cfg.judge_reject_min_confidence_2 = .7
    cfg.judge_accept_min_confidence = .6
    svc.store('omega svc runs the omega ingestion', source='t')
    svc.store('omega service is the deployed omega daemon', source='t')
    pid = _propose(svc, 'omega svc', 'omega service')
    pair = ('omega svc', 'omega service')
    first = _MergeJudge({pair: (verdict, c1)})
    assert svc.deep_dream_judge(first)['judged'] == 1

    class RequeueingJudge(_SecondJudge):
        def judge_merges(self, proposals):
            assert svc.review_rejudge('merge', limit=1)['requeued'] == 1
            return super().judge_merges(proposals)

    result = svc.deep_dream_judge(first, second_extractor=RequeueingJudge({pair: (verdict, c2)}))
    assert result['second_opinions'] == 0
    assert result['auto_rejected'] == 0 and result['auto_accepted'] == 0
    row = svc._storage.get_entity_proposal(pid)
    assert row['status'] == 'pending'
    assert row['judge_verdict'] is None and row['judge2_verdict'] is None
    assert svc.deep_dream_judge(first)['judged'] == 1        # re-judged from scratch


def test_candidate_retries_saved_action_without_new_model_call(svc, monkeypatch):
    cfg = svc.config.memory.deep_dream
    cfg.candidate_judge_mode = 'auto'
    for name in ('alpha', 'beta'):
        svc._storage.ensure_entity(name, display=name)
    rows = [{'src': 'alpha', 'dst': 'beta', 'similarity': .8}]
    judge = _CandidateJudge({('alpha', 'beta'): ('dismiss', .99, None, None, None)})
    original = svc._graph_dismiss_duplicate_locked
    def fail(*args, **kwargs):
        raise RuntimeError('injected action failure')
    monkeypatch.setattr(svc, '_graph_dismiss_duplicate_locked', fail)
    assert 'error' in svc.deep_dream_judge_candidates(judge, candidates=rows)
    monkeypatch.setattr(svc, '_graph_dismiss_duplicate_locked', original)
    def unexpected(rows):
        raise AssertionError('completed model work must be replayed')
    monkeypatch.setattr(judge, 'judge_candidates', unexpected)
    assert svc.deep_dream_judge_candidates(judge, candidates=rows)['dismissed'] == 1


class _RequeueingCandidateJudge(_CandidateJudge):
    """A human's review_rejudge lands while the model call is in flight."""

    def __init__(self, svc, verdicts):
        super().__init__(verdicts)
        self.svc = svc

    def judge_candidates(self, rows):
        assert self.svc.review_rejudge('candidate', limit=1)['requeued'] == 1
        return super().judge_candidates(rows)


def test_candidate_requeue_during_model_call_is_not_written_back(svc):
    svc.config.memory.deep_dream.candidate_judge_mode = 'shadow'
    verdicts = {('alpha', 'beta'): ('leave', .9, None, None, None),
                ('gamma', 'delta'): ('leave', .9, None, None, None)}
    first = [{'src': 'alpha', 'dst': 'beta', 'similarity': .8}]
    assert svc.deep_dream_judge_candidates(
        _CandidateJudge(verdicts), candidates=first)['judged'] == 1
    assert 'alpha|beta' in svc._storage.get_meta('deep_candidate_verdicts')['pairs']

    class Judge(_RequeueingCandidateJudge):
        def judge_candidates(self, rows):
            out = super().judge_candidates(rows)
            assert 'alpha|beta' not in svc._storage.get_meta(
                'deep_candidate_verdicts')['pairs']
            return out

    other = [{'src': 'gamma', 'dst': 'delta', 'similarity': .8}]
    assert svc.deep_dream_judge_candidates(
        Judge(svc, verdicts), candidates=other)['judged'] == 1
    pairs = svc._storage.get_meta('deep_candidate_verdicts')['pairs']
    assert 'alpha|beta' not in pairs and 'delta|gamma' in pairs
    # The human's rejudge takes effect: the pair goes back to the model.
    assert svc.deep_dream_judge_candidates(
        _CandidateJudge(verdicts), candidates=first)['judged'] == 1


def test_candidate_requeue_during_model_call_blocks_saved_dismiss_replay(svc, monkeypatch):
    svc.config.memory.deep_dream.candidate_judge_mode = 'auto'
    for name in ('alpha', 'beta', 'gamma', 'delta'):
        svc._storage.ensure_entity(name, display=name)
    rows = [{'src': 'alpha', 'dst': 'beta', 'similarity': .8},
            {'src': 'gamma', 'dst': 'delta', 'similarity': .8}]
    verdicts = {('alpha', 'beta'): ('dismiss', .99, None, None, None),
                ('gamma', 'delta'): ('leave', .9, None, None, None)}
    original = svc._graph_dismiss_duplicate_locked
    def fail(*args, **kwargs):
        raise RuntimeError('injected action failure')
    monkeypatch.setattr(svc, '_graph_dismiss_duplicate_locked', fail)
    assert 'error' in svc.deep_dream_judge_candidates(
        _CandidateJudge(verdicts), candidates=rows[:1])
    saved = svc._storage.get_meta('deep_candidate_verdicts')['pairs']['alpha|beta']
    assert saved['reply'] and not saved['complete']
    monkeypatch.setattr(svc, '_graph_dismiss_duplicate_locked', original)

    class Judge(_RequeueingCandidateJudge):
        def judge_candidates(self, rows):
            # alpha/beta replays its saved reply; only gamma/delta is sent.
            assert [(r['src'], r['dst']) for r in rows] == [('gamma', 'delta')]
            return super().judge_candidates(rows)

    result = svc.deep_dream_judge_candidates(Judge(svc, verdicts), candidates=rows)
    assert result['dismissed'] == 0 and result['remaining'] == 1
    assert not svc._storage.dismissed_pairs()
    assert 'alpha|beta' not in svc._storage.get_meta('deep_candidate_verdicts')['pairs']


def _scan_candidates(svc, monkeypatch, rows, ts):
    """Route the watermark path: a deep apply at ``ts`` whose scan finds ``rows``."""
    monkeypatch.setattr(svc, 'deep_dream', lambda apply=False: {'candidates': rows})
    svc._storage.set_meta('deep_last_apply', {'ts': ts})


def test_candidate_watermark_stays_open_after_a_requeue_during_the_model_call(
        svc, monkeypatch):
    svc.config.memory.deep_dream.candidate_judge_mode = 'shadow'
    verdicts = {('alpha', 'beta'): ('leave', .9, None, None, None),
                ('gamma', 'delta'): ('leave', .9, None, None, None)}
    first = {'src': 'alpha', 'dst': 'beta', 'similarity': .8}
    _scan_candidates(svc, monkeypatch, [first], 1.0)
    assert svc.deep_dream_judge_candidates(_CandidateJudge(verdicts))['judged'] == 1
    assert svc._storage.get_meta('deep_candidates_judged')['complete']
    _scan_candidates(svc, monkeypatch,
                     [first, {'src': 'gamma', 'dst': 'delta', 'similarity': .8}], 2.0)
    result = svc.deep_dream_judge_candidates(_RequeueingCandidateJudge(svc, verdicts))
    assert result['judged'] == 1 and result['remaining'] == 1
    assert not svc._storage.get_meta('deep_candidates_judged')['complete']
    assert svc.deep_dream_judge_candidates(_CandidateJudge(verdicts))['judged'] == 1


def test_candidate_all_judged_watermark_respects_a_requeue_after_the_read(
        svc, monkeypatch):
    import pseudolife_memory.graph as graph
    svc.config.memory.deep_dream.candidate_judge_mode = 'shadow'
    rows = [{'src': 'alpha', 'dst': 'beta', 'similarity': .8}]
    judge = _CandidateJudge({('alpha', 'beta'): ('leave', .9, None, None, None)})
    _scan_candidates(svc, monkeypatch, rows, 1.0)
    assert svc.deep_dream_judge_candidates(judge)['judged'] == 1
    _scan_candidates(svc, monkeypatch, rows, 2.0)
    # No model call on this tick: the requeue lands in the first unlocked
    # step after the memo read (norm_name keys every candidate).
    original, armed = graph.norm_name, [True]
    def requeue_once_unlocked(name):
        if armed[0] and not svc._lock.locked():
            armed[0] = False
            assert svc.review_rejudge('candidate', limit=1)['requeued'] == 1
        return original(name)
    monkeypatch.setattr(graph, 'norm_name', requeue_once_unlocked)
    assert svc.deep_dream_judge_candidates(judge)['reason'] == 'all_judged'
    assert not armed[0]
    monkeypatch.setattr(graph, 'norm_name', original)
    assert not svc._storage.get_meta('deep_candidates_judged')['complete']
    assert svc.deep_dream_judge_candidates(judge)['judged'] == 1


def test_legacy_analyzer_entity_rejection_is_reconciled(svc):
    pid = _propose(svc, 'old service', 'old svc')
    svc._storage.conn.execute(
        "UPDATE entity_proposals SET status='rejected', reason='analyzer-duplicate', "
        "decided_at=1 WHERE id=%s", (pid,))
    svc._storage.conn.commit()
    assert svc.reconcile_analyzer_proposals(limit=1)['closed'] == 1
    assert ('old-service', 'old-svc') in svc._storage.dismissed_pairs()
    assert svc.reconcile_analyzer_proposals(limit=1)['closed'] == 0


@pytest.mark.parametrize('stale_probe', [False, True])
def test_candidate_replay_rejudges_after_served_model_change(svc, monkeypatch, stale_probe):
    cfg = svc.config.memory.deep_dream
    cfg.candidate_judge_mode = 'auto'
    for name in ('alpha', 'beta'):
        svc._storage.ensure_entity(name, display=name)
    rows = [{'src': 'alpha', 'dst': 'beta', 'similarity': .8}]
    judge = _CandidateJudge({('alpha', 'beta'): ('dismiss', .99, None, None, None)})
    judge.served_model = 'first-model'
    original = svc._graph_dismiss_duplicate_locked
    def fail(*args, **kwargs):
        raise RuntimeError('injected action failure')
    monkeypatch.setattr(svc, '_graph_dismiss_duplicate_locked', fail)
    assert 'error' in svc.deep_dream_judge_candidates(judge, candidates=rows)
    monkeypatch.setattr(svc, '_graph_dismiss_duplicate_locked', original)
    if stale_probe:
        from pseudolife_memory.memory.review_judgments import record_response_identity
        cached = svc._storage.get_meta('review_response_identity_v1_candidate')
        with svc._lock:
            record_response_identity(svc, 'candidate', cached['policy'], 'replacement-model')
    else:
        judge.served_model = 'replacement-model'
    judge._v = {('alpha', 'beta'): ('leave', .99, None, None, None)}
    result = svc.deep_dream_judge_candidates(judge, candidates=rows)
    assert result['dismissed'] == 0
    assert not svc._storage.dismissed_pairs()


def test_merge_first_and_second_opinions_share_one_batch_budget(svc):
    from tests.test_queue_judges_service import _MergeJudge
    cfg = svc.config.memory.deep_dream
    cfg.judge_mode = 'shadow'
    cfg.judge_second_opinion = True
    pairs = [('alpha svc', 'alpha service'), ('beta svc', 'beta service')]
    _propose(svc, *pairs[0])
    judge = _MergeJudge({pair: ('leave', .5) for pair in pairs})
    svc.deep_dream_judge(judge, limit=1)
    _propose(svc, *pairs[1])
    result = svc.deep_dream_judge(judge, limit=1)
    assert result['judged'] + result['second_opinions'] == 1
    assert result['pending_unjudged'] == 1


def test_link_batch_does_not_project_full_evidence_per_verdict(svc, monkeypatch):
    svc.config.memory.deep_dream.link_judge_mode = 'shadow'
    for i in range(8):
        _link(svc, f'tool-{i}', 'uses', f'library-{i}')
    judge = _LinkJudge({(f'tool-{i}', 'uses', f'library-{i}'): ('leave', .5, None)
                        for i in range(8)})
    original = svc._enrich_link_proposals_from
    projections = []
    def observe(rows, evidence):
        projections.append(len(rows))
        return original(rows, evidence)
    monkeypatch.setattr(svc, '_enrich_link_proposals_from', observe)
    assert svc.deep_dream_judge_links(judge)['judged'] == 8
    assert len(projections) <= 3


def test_candidate_cannot_dismiss_a_new_pending_relationship(svc):
    svc.config.memory.deep_dream.candidate_judge_mode = 'auto'
    for name in ('alpha', 'beta'):
        svc._storage.ensure_entity(name, display=name)
    rows = [{'src': 'alpha', 'dst': 'beta', 'similarity': .8}]
    class ChangingJudge(_CandidateJudge):
        def judge_candidates(self, rows):
            svc.graph_propose_links([{'src': 'alpha', 'dst': 'beta', 'relation': 'uses'}])
            return super().judge_candidates(rows)
    judge = ChangingJudge({('alpha', 'beta'): ('dismiss', .99, None, None, None)})
    result = svc.deep_dream_judge_candidates(judge, candidates=rows)
    assert result.get('dismissed', 0) == 0
    assert not svc._storage.dismissed_pairs()


def test_snapshot_failure_retries_on_next_ordinary_junk_tick(svc, monkeypatch):
    from tests.test_queue_judges_service import _junk, _JunkJudge
    svc.config.memory.deep_dream.junk_judge_mode = 'auto'
    pid, _ = _junk(svc, 'file-a.py, file-b.py')
    judge = _JunkJudge({'file-a.py, file-b.py': ('delete', .99)})
    original = svc._write_graph_snapshot_locked
    monkeypatch.setattr(svc, '_write_graph_snapshot_locked', lambda: None)
    first = svc.deep_dream_judge_junk(judge)
    assert first['applied'] == 0
    assert svc._storage.get_entity_proposal(pid)['status'] == 'pending'
    monkeypatch.setattr(svc, '_write_graph_snapshot_locked', original)
    assert svc.deep_dream_judge_junk(judge)['applied'] == 1


@pytest.mark.parametrize('change,expected', [('access_count', 1), ('unrelated_entry', 1),
                                          ('text', 0), ('embedding', 0)])
def test_candidate_ignores_serving_metadata_but_not_evidence_changes(svc, change, expected):
    svc.config.memory.deep_dream.candidate_judge_mode = 'auto'
    svc.store('alpha and beta have unrelated owners', source='t')
    for name in ('alpha', 'beta'):
        svc._storage.ensure_entity(name, display=name)
    entry = svc._storage.load_entries()[0]
    class ChangingJudge(_CandidateJudge):
        def judge_candidates(self, rows):
            import numpy as np
            values = {'access_count': 42, 'text': 'alpha and beta are the same service',
                      'embedding': np.zeros_like(entry['embedding'])}
            if change == 'unrelated_entry':
                svc.store('gamma and delta are independent projects', source='t')
            else:
                with svc._lock:
                    svc._storage.update_entry(entry['id'], **{change: values[change]})
            return super().judge_candidates(rows)
    judge = ChangingJudge({('alpha', 'beta'): ('dismiss', .99, None, None, None)})
    result = svc.deep_dream_judge_candidates(judge, candidates=[
        {'src': 'alpha', 'dst': 'beta', 'similarity': .8}])
    assert result.get('dismissed', 0) == expected


def test_automatic_link_rejection_reopens_on_policy_change(svc):
    cfg = svc.config.memory.deep_dream
    cfg.link_judge_mode = 'auto'
    pid = _link(svc, 'alpha-tool', 'uses', 'beta-lib')
    judge = _LinkJudge({('alpha-tool', 'uses', 'beta-lib'): ('reject', .99, None)})
    assert svc.deep_dream_judge_links(judge)['applied'] == 1
    assert svc.deep_dream_judge_links(judge)['judged'] == 0
    cfg.link_judge_mode = 'shadow'
    reconsidered = svc.deep_dream_judge_links(judge)
    assert reconsidered['judged'] == 1
    assert reconsidered['reconsideration']['reopened'] == 1
    assert svc._storage.get_proposal(pid)['status'] == 'pending'
    audit = svc.graph_review()['automatic_decisions']
    assert any(row['state'] == 'reopened' and row['proposal_id'] == pid for row in audit)
    assert all('proposal' not in row and 'candidate' not in row for row in audit)


def test_human_confirmation_keeps_automatic_link_rejection_closed(svc):
    cfg = svc.config.memory.deep_dream
    cfg.link_judge_mode = 'auto'
    pid = _link(svc, 'alpha-tool', 'uses', 'beta-lib')
    judge = _LinkJudge({('alpha-tool', 'uses', 'beta-lib'): ('reject', .99, None)})
    assert svc.deep_dream_judge_links(judge)['applied'] == 1
    svc.graph_reject_proposal(pid)
    cfg.link_judge_mode = 'shadow'
    assert svc.deep_dream_judge_links(judge)['judged'] == 0
    assert svc._storage.get_proposal(pid)['status'] == 'rejected'


def test_automatic_candidate_dismissal_reopens_on_model_change(svc):
    svc.config.memory.deep_dream.candidate_judge_mode = 'auto'
    for name in ('alpha', 'beta'):
        svc._storage.ensure_entity(name, display=name)
    rows = [{'src': 'alpha', 'dst': 'beta', 'similarity': .8}]
    judge = _CandidateJudge({('alpha', 'beta'): ('dismiss', .99, None, None, None)})
    assert svc.deep_dream_judge_candidates(judge, candidates=rows)['dismissed'] == 1
    assert svc.deep_dream_judge_candidates(judge, candidates=rows)['judged'] == 0
    judge.served_model = 'new-model'
    judge._v = {('alpha', 'beta'): ('leave', .9, None, None, None)}
    svc.deep_dream_judge_candidates(judge, candidates=rows)
    assert not svc._storage.dismissed_pairs()


def test_graph_reply_from_changed_served_model_cannot_apply(svc):
    svc.config.memory.deep_dream.link_judge_mode = 'auto'
    pid = _link(svc, 'alpha-tool', 'uses', 'beta-lib')
    class ChangingJudge(_LinkJudge):
        served_model = 'before-call'
        def judge_links(self, rows):
            self.served_model = 'after-call'
            return super().judge_links(rows)
    judge = ChangingJudge({('alpha-tool', 'uses', 'beta-lib'): ('accept', .99, None)})
    result = svc.deep_dream_judge_links(judge)
    assert result.get('applied', 0) == 0
    assert svc._storage.get_proposal(pid)['judge_verdict'] is None


def test_automatic_merge_rejection_reopens_but_cannot_adopt_human_dismissal(svc):
    from tests.test_queue_judges_service import _MergeJudge
    cfg = svc.config.memory.deep_dream
    cfg.judge_mode = 'auto-reject'
    pairs = [('alpha svc', 'alpha service'), ('beta svc', 'beta service')]
    ids = [_propose(svc, *pair) for pair in pairs]
    svc.graph_dismiss_duplicate(*pairs[1])
    judge = _MergeJudge({pair: ('reject', .99) for pair in pairs})
    assert svc.deep_dream_judge(judge)['auto_rejected'] == 2
    assert svc.deep_dream_judge(judge)['judged'] == 0
    cfg.judge_mode = 'shadow'
    result = svc.deep_dream_judge(judge)
    assert result['reconsideration']['reopened'] == 1
    assert svc._storage.get_entity_proposal(ids[0])['status'] == 'pending'
    assert svc._storage.get_entity_proposal(ids[1])['status'] == 'rejected'
    assert ('beta-service', 'beta-svc') in svc._storage.dismissed_pairs()


def test_stale_link_rejection_cannot_rewrite_an_accepted_decision(svc):
    pid = _link(svc, 'alpha-tool', 'uses', 'beta-lib')
    assert svc.graph_accept_proposal(pid)['accepted']
    assert not svc.graph_reject_proposal(pid)['rejected']
    assert svc._storage.get_proposal(pid)['status'] == 'accepted'


def test_candidate_batch_defers_rows_changed_by_earlier_proposal(svc):
    svc.config.memory.deep_dream.candidate_judge_mode = 'auto'
    for name in ('alpha', 'beta', 'gamma'):
        svc._storage.ensure_entity(name, display=name)
    rows = [{'src': 'alpha', 'dst': dst, 'similarity': .8} for dst in ('beta', 'gamma')]
    judge = _CandidateJudge({
        ('alpha', 'beta'): ('propose', .99, 'uses', None, None),
        ('alpha', 'gamma'): ('dismiss', .99, None, None, None),
    })
    result = svc.deep_dream_judge_candidates(judge, candidates=rows)
    assert result['proposed'] == 1
    assert result['dismissed'] == 0 and result['remaining'] == 1
    assert not svc._storage.dismissed_pairs()


def test_response_only_model_change_reconsiders_prior_graph_decisions(svc):
    svc.config.memory.deep_dream.link_judge_mode = 'auto'
    first_id = _link(svc, 'alpha-tool', 'uses', 'beta-lib')
    verdicts = {(src, 'uses', dst): ('reject', .99, None)
                for src, dst in [('alpha-tool', 'beta-lib'), ('gamma-tool', 'delta-lib')]}
    class ResponseJudge(_LinkJudge):
        def __init__(self, response):
            super().__init__(verdicts)
            self.served_model, self.response = None, response
        def judge_links(self, rows):
            self.served_model = self.response
            return super().judge_links(rows)
    assert svc.deep_dream_judge_links(ResponseJudge('model-a'))['applied'] == 1
    _link(svc, 'gamma-tool', 'uses', 'delta-lib')
    assert svc.deep_dream_judge_links(ResponseJudge('model-b'))['applied'] == 1
    result = svc.deep_dream_judge_links(ResponseJudge('model-b'))
    assert result['reconsideration']['reopened'] == 1
    assert svc._storage.conn.execute(
        'SELECT judge_model FROM edge_proposals WHERE id=%s', (first_id,)).fetchone()[0] == 'model-b'


def test_completed_candidate_memo_reuses_response_identity_without_rejudging(svc):
    svc.config.memory.deep_dream.candidate_judge_mode = 'shadow'
    rows = [{'src': 'alpha', 'dst': 'beta', 'similarity': .8}]
    class ResponseJudge(_CandidateJudge):
        def __init__(self):
            super().__init__({('alpha', 'beta'): ('leave', .9, None, None, None)})
            self.served_model = None
        def judge_candidates(self, rows):
            self.served_model = 'model-a'
            return super().judge_candidates(rows)
    assert svc.deep_dream_judge_candidates(ResponseJudge(), candidates=rows)['judged'] == 1
    assert svc.deep_dream_judge_candidates(ResponseJudge(), candidates=rows)['judged'] == 0


@pytest.mark.parametrize('probe', [None, 'model-a'])
def test_response_only_model_change_reconsiders_candidate_dismissal(svc, probe):
    svc.config.memory.deep_dream.candidate_judge_mode = 'auto'
    for name in ('alpha', 'beta', 'gamma', 'delta'):
        svc._storage.ensure_entity(name, display=name)
    first = [{'src': 'alpha', 'dst': 'beta', 'similarity': .8}]
    other = [{'src': 'gamma', 'dst': 'delta', 'similarity': .8}]
    class ResponseJudge(_CandidateJudge):
        def __init__(self, response, verdict):
            super().__init__({('alpha', 'beta'): (verdict, .99, None, None, None),
                              ('gamma', 'delta'): ('leave', .9, None, None, None)})
            self.served_model, self.response = probe, response
        def judge_candidates(self, rows):
            self.served_model = self.response
            return super().judge_candidates(rows)
    svc.deep_dream_judge_candidates(ResponseJudge('model-a', 'dismiss'), candidates=first)
    svc.deep_dream_judge_candidates(ResponseJudge('model-b', 'leave'), candidates=other)
    result = svc.deep_dream_judge_candidates(ResponseJudge('model-b', 'leave'), candidates=first)
    assert result['reconsideration']['reopened'] == 1
    assert not svc._storage.dismissed_pairs()


def test_single_vote_marker_does_not_inherit_another_rows_second_judge(svc):
    from tests.test_queue_judges_service import _MergeJudge, _SecondJudge
    from pseudolife_memory.memory.review_decisions import decision_record
    cfg = svc.config.memory.deep_dream
    cfg.judge_mode = 'auto-reject'
    cfg.judge_second_opinion = True
    old = ('alpha svc', 'alpha service')
    new = ('beta svc', 'beta service')
    _propose(svc, *old)
    first = _MergeJudge({old: ('accept', .9), new: ('reject', .99)})
    svc.deep_dream_judge(first)
    pid = _propose(svc, *new)
    svc.deep_dream_judge(first, second_extractor=_SecondJudge({old: ('reject', .99)}))
    index = svc._storage.get_meta('automatic_review_decisions_v1')
    marker = decision_record(svc._storage, index['active'][f'merge:{pid}']['decision_id'])
    assert marker['second_model'] is None and marker['second_policy'] is None


def test_actual_response_rotation_is_not_hidden_by_an_older_probe(svc):
    svc.config.memory.deep_dream.link_judge_mode = 'auto'
    pid = _link(svc, 'alpha-tool', 'uses', 'beta-lib')
    verdicts = {(src, 'uses', dst): ('reject', .99, None)
                for src, dst in [('alpha-tool', 'beta-lib'), ('gamma-tool', 'delta-lib')]}
    class ResponseJudge(_LinkJudge):
        def __init__(self, response):
            super().__init__(verdicts)
            self.served_model, self.response = 'model-a', response
        def judge_links(self, rows):
            self.served_model = self.response
            return super().judge_links(rows)
    svc.deep_dream_judge_links(ResponseJudge('model-a'))
    _link(svc, 'gamma-tool', 'uses', 'delta-lib')
    svc.deep_dream_judge_links(ResponseJudge('model-b'))
    result = svc.deep_dream_judge_links(ResponseJudge('model-b'))
    assert result['reconsideration']['reopened'] == 1
    assert svc._storage.get_proposal(pid)['status'] == 'pending'


# ── v56: the link second opinion follows its first verdict ────────────────

def _link_second_opinion(svc, verdict, c1, c2, *, two_vote='shadow'):
    from tests.test_queue_judges_service import _SecondLinkJudge, _ServedLinkJudge
    cfg = svc.config.memory.deep_dream
    cfg.link_judge_mode = 'auto' if two_vote == 'auto' else 'shadow'
    cfg.judge_second_opinion = True
    cfg.link_judge_two_vote = two_vote
    key = ('alpha-tool', 'uses', 'beta-lib')
    pid = _link(svc, *key)
    first = _ServedLinkJudge({key: (verdict, c1, None)})
    assert svc.deep_dream_judge_links(first)['judged'] == 1
    second = _SecondLinkJudge({key: (verdict, c2, None)})
    out = svc.deep_dream_judge_links(first, second_extractor=second)
    assert out['second_opinions'] == 1
    return pid, first, second, out


def test_requeue_clears_a_link_rows_second_opinion_with_its_first(svc):
    pid, _first, _second, _out = _link_second_opinion(svc, 'accept', .7, .8)
    assert _pending_link(svc, pid)['judge2_verdict'] == 'accept'
    assert svc.review_rejudge('link', limit=1)['requeued'] == 1
    row = _pending_link(svc, pid)
    assert row['judge_verdict'] is None and row['judge2_verdict'] is None
    assert row['judge2_model'] is None and row['judged2_at'] is None


def test_requeue_clears_the_first_opinions_served_stamp_with_its_verdict(svc):
    pid, _first, _second, _out = _link_second_opinion(svc, 'accept', .7, .8)
    stamp = lambda: svc._storage.conn.execute(  # noqa: E731
        'SELECT judge_served_model FROM edge_proposals WHERE id=%s', (pid,)).fetchone()[0]
    assert stamp() == 'stub-link-judge'
    assert svc.review_rejudge('link', limit=1)['requeued'] == 1
    assert stamp() is None


def test_a_stale_first_link_verdict_takes_its_second_opinion_with_it(svc):
    pid, first, _second, _out = _link_second_opinion(svc, 'accept', .7, .8)
    svc.graph_relate('alpha-tool', 'uses', 'gamma-lib')    # the evidence moves
    out = svc.deep_dream_judge_links(first)
    assert out['judged'] == 1
    assert _pending_link(svc, pid)['judge2_verdict'] is None


def test_two_vote_link_reject_records_its_second_judge_and_reopens_clean(svc):
    from pseudolife_memory.memory.review_decisions import decision_record
    pid, first, second, out = _link_second_opinion(svc, 'reject', .65, .8,
                                                   two_vote='auto')
    assert out['two_vote_applied'] == 1
    assert svc._storage.get_proposal(pid)['status'] == 'rejected'
    index = svc._storage.get_meta('automatic_review_decisions_v1')
    marker = decision_record(svc._storage, index['active'][f'link:{pid}']['decision_id'])
    assert marker['second_model'] == 'stub-link-judge-2' and marker['second_policy']
    # A policy change reopens the automatic reject with no opinion left on it.
    svc.config.memory.deep_dream.link_two_vote_min_confidence = .75
    again = svc.deep_dream_judge_links(_LinkJudge({}))
    assert again['reconsideration']['reopened'] == 1
    row = svc._storage.conn.execute(
        'SELECT status, judge2_verdict, judge2_model, judge2_relation '
        'FROM edge_proposals WHERE id=%s', (pid,)).fetchone()
    assert row[0] == 'pending' and row[1:] == (None, None, None)


# ── v56: the merge tiebreak follows the votes it broke ────────────────────

def _tiebroken(svc, *, tiebreak='auto'):
    from tests.test_queue_judges_service import _split, _ThirdJudge
    pair = ('chi svc', 'chi harness')
    pid, first = _split(svc, pair, ('accept', .7), ('reject', .75),
                        mode='auto-reject', tiebreak=tiebreak)
    third = _ThirdJudge({pair: ('reject', .85)})
    out = svc.deep_dream_judge(first, third_extractor=third)
    assert out['third_opinions'] == 1
    return pid, first, third, out


def test_requeue_clears_a_merge_rows_third_opinion_with_its_first(svc):
    pid, _first, _third, _out = _tiebroken(svc, tiebreak='shadow')
    assert svc._storage.get_entity_proposal(pid)['judge3_verdict'] == 'reject'
    assert svc.review_rejudge('merge', limit=1)['requeued'] == 1
    row = svc._storage.conn.execute(
        'SELECT judge_verdict, judge2_verdict, judge3_verdict, judge3_confidence, '
        'judge3_model, judged3_at FROM entity_proposals WHERE id=%s', (pid,)).fetchone()
    assert row == (None,) * 6


def test_a_stale_first_merge_verdict_takes_its_third_opinion_with_it(svc):
    pid, first, _third, _out = _tiebroken(svc, tiebreak='shadow')
    svc.config.memory.deep_dream.judge_snippet_max_chars = 300   # the policy moves
    svc.deep_dream_judge(first)
    row = svc._storage.get_entity_proposal(pid)
    assert row['judge2_verdict'] is None and row['judge3_verdict'] is None


def test_third_vote_cannot_pair_with_a_second_verdict_requeued_mid_call(svc):
    from tests.test_queue_judges_service import _split, _ThirdJudge
    pair = ('chi svc', 'chi harness')
    pid, first = _split(svc, pair, ('accept', .7), ('reject', .75), mode='auto-reject')

    class RequeueingJudge(_ThirdJudge):
        def judge_merges(self, proposals):
            assert svc.review_rejudge('merge', limit=1)['requeued'] == 1
            return super().judge_merges(proposals)

    out = svc.deep_dream_judge(first, third_extractor=RequeueingJudge({pair: ('reject', .9)}))
    assert out['third_opinions'] == 0 and out['tiebreak_applied'] == 0
    row = svc._storage.get_entity_proposal(pid)
    assert row['status'] == 'pending' and row['judge3_verdict'] is None


def test_tiebreak_reject_records_its_third_judge_and_reopens_clean(svc):
    from pseudolife_memory.memory.review_decisions import decision_record
    pid, first, third, out = _tiebroken(svc)
    assert out['tiebreak_applied'] == 1
    index = svc._storage.get_meta('automatic_review_decisions_v1')
    marker = decision_record(svc._storage, index['active'][f'merge:{pid}']['decision_id'])
    assert marker['third_model'] == 'stub-merge-judge-3' and marker['third_policy']
    # The second vote was taken on an earlier tick; its endpoint identity
    # rides the row and lands in the decision beside the third's.
    assert marker['second_model'] == 'stub-merge-judge-2' and marker['second_policy']
    assert marker['second_policy'] != marker['third_policy']
    # The third endpoint starts serving another model: the reject reopens.
    from pseudolife_memory.memory.review_judgments import record_response_identity
    with svc._lock:
        record_response_identity(svc, 'merge_third', marker['third_policy'], 'another-model')
    again = svc.deep_dream_judge(first)
    assert again['reconsideration']['reopened'] == 1
    row = svc._storage.conn.execute(
        'SELECT status, judge2_verdict, judge3_verdict, judge3_model '
        'FROM entity_proposals WHERE id=%s', (pid,)).fetchone()
    assert row[0] == 'pending' and row[1:] == (None, None, None)


def test_tiebreak_reject_reopens_when_the_second_endpoint_changes_model(svc):
    from pseudolife_memory.memory.review_decisions import decision_record
    from pseudolife_memory.memory.review_judgments import record_response_identity
    pid, first, third, out = _tiebroken(svc)
    assert out['tiebreak_applied'] == 1
    index = svc._storage.get_meta('automatic_review_decisions_v1')
    marker = decision_record(svc._storage, index['active'][f'merge:{pid}']['decision_id'])
    with svc._lock:
        record_response_identity(svc, 'merge_second', marker['second_policy'], 'another-model')
    again = svc.deep_dream_judge(first)
    assert again['reconsideration']['reopened'] == 1
    assert svc._storage.get_entity_proposal(pid)['status'] == 'pending'


def test_a_stale_first_merge_verdict_clears_the_votes_endpoints(svc):
    pid, first, _third, _out = _tiebroken(svc, tiebreak='shadow')
    cols = ('judge2_served_model, judge2_policy, judge3_served_model, judge3_policy')
    stored = svc._storage.conn.execute(
        f'SELECT {cols} FROM entity_proposals WHERE id=%s', (pid,)).fetchone()
    assert stored[0] == 'stub-merge-judge-2' and stored[2] == 'stub-merge-judge-3'
    assert stored[1] and stored[3]
    svc.config.memory.deep_dream.judge_snippet_max_chars = 300   # the policy moves
    svc.deep_dream_judge(first)
    assert svc._storage.conn.execute(
        f'SELECT {cols} FROM entity_proposals WHERE id=%s', (pid,)).fetchone() == (None,) * 4


# ── review round 2: a later vote follows its own endpoint's identity ──────

def _endpoint_now_serves(svc, queue, policy, model='another-model'):
    from pseudolife_memory.memory.review_judgments import record_response_identity
    with svc._lock:
        record_response_identity(svc, queue, policy, model)


def test_a_third_vote_whose_endpoint_changed_model_is_cleared_not_applied(svc):
    pid, first, third, _out = _tiebroken(svc, tiebreak='shadow')
    row = svc._storage.conn.execute(
        'SELECT judge3_policy FROM entity_proposals WHERE id=%s', (pid,)).fetchone()
    _endpoint_now_serves(svc, 'merge_third', row[0])
    svc.config.memory.deep_dream.judge_tiebreak_mode = 'auto'
    out = svc.deep_dream_judge(first)
    assert out.get('tiebreak_applied', 0) == 0
    row = svc._storage.get_entity_proposal(pid)
    assert row['status'] == 'pending' and row['judge3_verdict'] is None
    assert row['judge2_verdict'] == 'reject'           # the second vote stands
    calls = third.calls
    svc.deep_dream_judge(first, third_extractor=third)   # re-asked
    assert third.calls == calls + 1


def test_a_second_vote_whose_endpoint_changed_model_takes_the_third_with_it(svc):
    pid, first, _third, _out = _tiebroken(svc, tiebreak='shadow')
    row = svc._storage.conn.execute(
        'SELECT judge2_policy FROM entity_proposals WHERE id=%s', (pid,)).fetchone()
    _endpoint_now_serves(svc, 'merge_second', row[0])
    svc.config.memory.deep_dream.judge_tiebreak_mode = 'auto'
    out = svc.deep_dream_judge(first)
    assert out.get('tiebreak_applied', 0) == 0
    row = svc._storage.get_entity_proposal(pid)
    assert row['status'] == 'pending' and row['judge_verdict'] == 'accept'
    assert row['judge3_verdict'] is None
    # Cleared, then asked again in the same tick (the test hook asks the
    # first extractor): the old vote and its tails are gone.
    again = svc._storage.conn.execute(
        'SELECT judge2_model, judge_note FROM entity_proposals WHERE id=%s',
        (pid,)).fetchone()
    assert again[0] == 'stub-merge-judge'
    assert 'stub-merge-judge-2' not in again[1] and '| 3rd (' not in again[1]


def test_a_promoted_link_two_vote_skips_votes_from_a_changed_endpoint(svc):
    from tests.test_queue_judges_service import _two_vote_links
    pids, first, out = _two_vote_links(svc, two_vote='shadow')
    pid = pids[('k-a', 'uses', 'k-b')]
    policy = svc._storage.conn.execute(
        'SELECT judge2_policy FROM edge_proposals WHERE id=%s', (pid,)).fetchone()[0]
    _endpoint_now_serves(svc, 'link_second', policy)
    svc.config.memory.deep_dream.link_judge_two_vote = 'auto'
    again = svc.deep_dream_judge_links(first)
    assert again.get('two_vote_applied', 0) == 0
    assert svc._storage.get_proposal(pid)['status'] == 'pending'
    row = _pending_link(svc, pid)
    # Cleared, then asked again in the same tick (the test hook asks the
    # first extractor, whose same-model pair applies nothing).
    assert row['judge_verdict'] == 'accept'
    assert row['judge2_model'] == 'stub-link-judge'
    assert 'stub-link-judge-2' not in row['judge_note']


def test_the_apply_paths_hold_the_moved_endpoint_rule_on_their_own(svc, monkeypatch):
    """Belt and braces: with refresh()'s clearing switched off, neither
    the tiebreak nor the link two-vote gate applies a vote whose endpoint
    has since served another model."""
    from pseudolife_memory.memory.review_judgments import ReviewJudgments
    from tests.test_queue_judges_service import _two_vote_links
    monkeypatch.setattr(ReviewJudgments, '_clear_moved_votes', lambda self: None)
    pid, first, _third, _out = _tiebroken(svc, tiebreak='shadow')
    policy = svc._storage.conn.execute(
        'SELECT judge3_policy FROM entity_proposals WHERE id=%s', (pid,)).fetchone()[0]
    _endpoint_now_serves(svc, 'merge_third', policy)
    svc.config.memory.deep_dream.judge_tiebreak_mode = 'auto'
    assert svc.deep_dream_judge(first).get('tiebreak_applied', 0) == 0
    assert svc._storage.get_entity_proposal(pid)['status'] == 'pending'
    pids, lfirst, _ = _two_vote_links(svc, two_vote='shadow')
    lpid = pids[('k-a', 'uses', 'k-b')]
    policy = svc._storage.conn.execute(
        'SELECT judge2_policy FROM edge_proposals WHERE id=%s', (lpid,)).fetchone()[0]
    _endpoint_now_serves(svc, 'link_second', policy)
    svc.config.memory.deep_dream.link_judge_two_vote = 'auto'
    assert svc.deep_dream_judge_links(lfirst).get('two_vote_applied', 0) == 0


def test_a_vote_with_no_recorded_served_model_is_not_moved():
    """Only two KNOWN names that differ make a later vote moved. A vote
    whose reply named no model is stored with no served model; calling it
    moved would clear and re-ask it every tick (one model call per row per
    tick) instead of leaving it to the existing unknown-identity refusal."""
    from types import SimpleNamespace

    from pseudolife_memory.memory.review_judgments import vote_endpoint_moved

    meta = {"review_response_identity_v1_merge_second": {"policy": "p", "model": "model-b"}}
    svc = SimpleNamespace(_storage=SimpleNamespace(get_meta=meta.get))
    row = {"judge2_policy": "p"}
    assert vote_endpoint_moved(svc, "merge", {**row, "judge2_served_model": None}, "second") is False
    assert vote_endpoint_moved(svc, "merge", {**row, "judge2_served_model": "model-b"}, "second") is False
    assert vote_endpoint_moved(svc, "merge", {**row, "judge2_served_model": "model-a"}, "second") is True
