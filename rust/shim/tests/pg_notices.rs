#![forbid(unsafe_code)]
#[path = "common/pg_wire.rs"]
#[allow(dead_code)]
mod pg_wire;
use pseudolife_stdio::pg::{Dsn, Session, TlsEnvironment};

const COUNT: usize = 4100;

fn assert_order(notices: &[tokio_postgres::error::DbError], start: usize) {
    for (index, notice) in notices.iter().enumerate() {
        assert_eq!(
            notice.message(),
            format!("fixture-warning-{:04}", start + index)
        );
        assert_eq!(
            notice.parsed_severity(),
            Some(tokio_postgres::error::Severity::Warning)
        );
    }
}

#[tokio::test]
async fn real_session_receiver_retains_every_collection_notice_in_order() {
    let fixture = pg_wire::notices(COUNT).await;
    let dsn = Dsn::parse(&fixture.dsn).unwrap();
    let mut session = Session::open_in(&dsn, &TlsEnvironment::default())
        .await
        .unwrap();
    // Old notices must not leak into this operation's complete collection.
    session
        .client()
        .batch_execute("FIXTURE NOTICE BURST")
        .await
        .unwrap();
    let collection = session.collect_notices();
    collection
        .client()
        .batch_execute("FIXTURE NOTICE BURST")
        .await
        .unwrap();
    let notices = collection.take_notices();
    assert!(collection.take_notices().is_empty());
    drop(collection);
    session.close().await.unwrap();
    fixture.task.await.unwrap();
    assert_eq!(
        notices.len(),
        COUNT,
        "the receiver must retain all 4100 wire notices"
    );
    assert_order(&notices, 0);
}

#[tokio::test]
async fn normal_long_lived_session_stays_bounded_without_drain() {
    let fixture = pg_wire::notices(COUNT).await;
    let dsn = Dsn::parse(&fixture.dsn).unwrap();
    let session = Session::open_in(&dsn, &TlsEnvironment::default())
        .await
        .unwrap();
    for _ in 0..2 {
        session
            .client()
            .batch_execute("FIXTURE NOTICE BURST")
            .await
            .unwrap();
    }
    let notices = session.take_notices();
    session.close().await.unwrap();
    fixture.task.await.unwrap();
    assert_eq!(
        notices.len(),
        4096,
        "normal sessions retain a bounded newest suffix"
    );
    assert_order(&notices, COUNT - 4096);
}

#[tokio::test]
async fn dropping_an_undrained_collection_restores_the_receiver_limit() {
    let fixture = pg_wire::notices(COUNT).await;
    let dsn = Dsn::parse(&fixture.dsn).unwrap();
    let mut session = Session::open_in(&dsn, &TlsEnvironment::default())
        .await
        .unwrap();
    let collection = session.collect_notices();
    collection
        .client()
        .batch_execute("FIXTURE NOTICE BURST")
        .await
        .unwrap();
    drop(collection);
    let trimmed = session.take_notices();
    session
        .client()
        .batch_execute("FIXTURE NOTICE BURST")
        .await
        .unwrap();
    let bounded = session.take_notices();
    session.close().await.unwrap();
    fixture.task.await.unwrap();
    assert_eq!(trimmed.len(), 4096);
    assert_order(&trimmed, COUNT - 4096);
    assert_eq!(bounded.len(), 4096);
    assert_order(&bounded, COUNT - 4096);
}
