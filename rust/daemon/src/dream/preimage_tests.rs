//! The dream journals a claim's rollback pre-image from the slot as the
//! writer finds it under the write lock, so a forget that lands while the
//! claim waits for the lock is not journaled as the state it replaced.

use std::collections::HashSet;
use std::path::PathBuf;
use std::time::Duration;

use super::*;
use crate::config::{Config, DaemonEnv};
use crate::embed::Embedder;

const ENTITY: &str = "Preimage Fixture";
const ATTRIBUTE: &str = "owner";

fn fixture_dsn() -> String {
    let dsn = std::env::var("PL_BANK_IDENTITY_TEST_DSN").expect("disposable fixture bank DSN");
    let parsed = crate::storage::parse_dsn(&dsn).expect("parse fixture DSN");
    assert!(
        matches!(parsed.get_hosts(), [tokio_postgres::config::Host::Tcp(host)] if host == "127.0.0.1")
    );
    assert!(
        parsed
            .get_dbname()
            .is_some_and(|name| name.starts_with("pl_cf_pgs_identity_"))
    );
    dsn
}

async fn fixture_service() -> (Arc<Service>, Arc<Ready>) {
    let dsn = fixture_dsn();
    let model =
        PathBuf::from(std::env::var_os("PL_SHADOW_EMBED_FIXTURE").expect("offline embedder"));
    let mut config = Config::default();
    config.embedding.model_name = "pseudolife-test/dream-preimage-fixture".into();
    // Pinned, though defaults: slot matching would take the write lock the
    // interleave test holds, and quarantine routing reads the slot early.
    config.memory.cortex.dream_slot_match_threshold = 0.0;
    config.dream.quarantine_low_trust = false;
    let service = Arc::new(Service::new(
        config,
        DaemonEnv::from_env(&|_| None).unwrap(),
        model.parent().unwrap().to_owned(),
        dsn.clone(),
        false,
    ));
    service
        .use_embedder(Arc::new(
            Embedder::load_config(&service.config.embedding, Some(&model), 1).unwrap(),
        ))
        .await;
    let principals = Arc::new(crate::auth::PrincipalStore::new());
    let board = Arc::new(crate::board::Board::new(
        service.config.coordination.clone(),
        dsn,
        service.clone(),
        principals,
    ));
    service.install(&board);
    let ready = service.ensure_init().await.unwrap();
    (service, ready)
}

fn claim(entity: &str, attribute: &str, value: &str, op: Option<&str>) -> Claim {
    Claim {
        entity: entity.into(),
        attribute: attribute.into(),
        value: value.into(),
        confidence: 0.9,
        origin: "agent".into(),
        value_coerced: false,
        op: op.map(str::to_string),
        stance: None,
        quote: None,
        speaker: None,
        source: None,
    }
}

fn pass<'a>(
    svc: &'a Arc<Service>,
    ready: &'a Arc<Ready>,
    caller: &'a Caller,
    run_id: i64,
) -> Pass<'a> {
    Pass {
        svc,
        ready,
        cfg: &svc.config.dream,
        caller,
        writer: "fixture",
        entries: Vec::new(),
        batch_text: String::new(),
        batch_has_date: false,
        run_id: Some(run_id),
        c: Counts::new(),
        constraint_idx: Vec::new(),
    }
}

/// One journal row: action, op ("" for a scalar), prev_kind, prev_value,
/// prev_status.
type JournalRow = (
    String,
    String,
    Option<String>,
    Option<String>,
    Option<String>,
);

async fn journal_rows(ready: &Ready, run_id: i64) -> Vec<JournalRow> {
    crate::txn::with_client(ready.storage.client(), |c| async move {
        c.query(
            "SELECT action, COALESCE(op, ''), prev_kind, prev_value, prev_status \
             FROM dream_run_slots WHERE run_id = $1 ORDER BY seq",
            &[&run_id],
        )
        .await
    })
    .await
    .unwrap()
    .iter()
    .map(|r| (r.get(0), r.get(1), r.get(2), r.get(3), r.get(4)))
    .collect()
}

#[tokio::test]
#[ignore = "run by startup_ci.py with a disposable PostgreSQL bank and the offline embedder"]
async fn a_forget_while_a_claim_waits_for_the_lock_is_not_its_pre_image() {
    let (svc, ready) = fixture_service().await;
    let caller = Caller {
        header_session: None,
        transport_session: None,
    };
    crate::fact_set::cortex_write(
        &svc,
        &ready,
        &caller,
        "fixture".into(),
        CortexWriteArgs::new(ENTITY, ATTRIBUTE, "seeded"),
    )
    .await
    .unwrap();
    let run_id = start_run(&ready, 0.0, 0, "fixture").await.unwrap();
    let mut pass = pass(&svc, &ready, &caller, run_id);
    let dreamed = claim(ENTITY, ATTRIBUTE, "dreamed", None);
    let known = HashSet::new();
    let mut new_entities = Vec::new();

    // The claim embeds its texts and then queues on the write lock the test
    // holds; the forget runs under that lock, so it lands after anything the
    // claim read before its embedding and before its write. The slot text is
    // cached from the seed write, so only the claim text costs a forward pass.
    let held = ready.write.lock().await;
    let before = ready.embedder.forwards().unwrap();
    let (r, s) = (&ready, &svc);
    let forget = async move {
        let embedded = tokio::time::timeout(Duration::from_secs(20), async {
            while r.embedder.forwards().unwrap() < before + 1 {
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await;
        let removed = r.cortex.write(|c| c.forget(ENTITY, Some(ATTRIBUTE)));
        crate::fact_set::save_cortex(s, r).await.unwrap();
        drop(held);
        (embedded, removed)
    };
    let (claimed, (embedded, removed)) = tokio::join!(
        pass.claim(&dreamed, None, None, &known, &mut new_entities),
        forget
    );
    claimed.unwrap();
    embedded.expect("the claim never embedded its texts");
    assert_eq!(removed, 1, "the seeded fact was not forgotten");
    let journal = journal_rows(&ready, run_id).await;

    // The dreamed fact went into an empty slot, so the rollback retires it.
    finish_run_row(&ready, run_id, "committed", Some(0.0), 1, json!({}))
        .await
        .unwrap();
    let rolled = crate::dream::rollback::rollback(&svc, &ready, &caller, "fixture", None)
        .await
        .unwrap();
    assert!(
        ready.cortex.snapshot().lookup(ENTITY, ATTRIBUTE).is_none(),
        "the rollback left the dreamed fact in place: {rolled}; journal {journal:?}"
    );
    assert_eq!(
        journal,
        [("inserted".into(), String::new(), None, None, None)],
        "the journal holds the forgotten fact as the slot's pre-image"
    );
}

/// Every writer a claim reaches journals the slot it found: a scalar
/// supersede, a member add to a set and a member removal from it.
#[tokio::test]
#[ignore = "run by startup_ci.py with a disposable PostgreSQL bank and the offline embedder"]
async fn each_claim_writer_journals_the_slot_it_found() {
    let (svc, ready) = fixture_service().await;
    let caller = Caller {
        header_session: None,
        transport_session: None,
    };
    let mut seed = CortexWriteArgs::new(ENTITY, ATTRIBUTE, "seeded");
    seed.confidence = 0.5;
    crate::fact_set::cortex_write(&svc, &ready, &caller, "fixture".into(), seed)
        .await
        .unwrap();
    let member = crate::set_members::SetAddArgs::new(ENTITY, "members", "first");
    crate::set_members::set_add(&svc, &ready, &caller, "fixture".into(), member)
        .await
        .unwrap();
    let run_id = start_run(&ready, 0.0, 0, "fixture").await.unwrap();
    let mut pass = pass(&svc, &ready, &caller, run_id);
    let known = HashSet::new();
    let mut new_entities = Vec::new();
    for c in [
        claim(ENTITY, ATTRIBUTE, "dreamed", None),
        claim(ENTITY, "members", "second", Some("add")),
        claim(ENTITY, "members", "first", Some("remove")),
    ] {
        pass.claim(&c, None, None, &known, &mut new_entities)
            .await
            .unwrap();
    }
    // The offline embedder may match the added member to the existing one
    // (`member_confirmed`); either way no member holds its value yet.
    let some = |s: &str| Some(s.to_string());
    let rows: Vec<_> = journal_rows(&ready, run_id)
        .await
        .into_iter()
        .map(|(_, op, kind, value, status)| (op, kind, value, status))
        .collect();
    assert_eq!(
        rows,
        [
            (
                String::new(),
                some("scalar"),
                some("seeded"),
                some("current")
            ),
            ("add".into(), some("set"), None, None),
            ("remove".into(), some("set"), some("first"), some("current")),
        ]
    );
}
