use super::*;
use futures::FutureExt;
use serde_json::json;

// The transport is always injected. No test opens a socket or resolves a host.
fn response(status: u16, headers: &[(&str, &str)], body: &str) -> reqwest::Response {
    let mut response = hyper::Response::builder().status(status);
    for (name, value) in headers {
        response = response.header(*name, *value);
    }
    response.body(body.to_owned()).unwrap().into()
}

fn complete_release() -> Value {
    let names = [
        "RELEASE-READY.json",
        "SHA256SUMS",
        "pseudolife-compose-0.25.0.tar.gz",
        "pseudolife-mcp-0.25.0-aarch64-apple-darwin.tar.gz",
        "pseudolife-mcp-0.25.0-aarch64-unknown-linux-gnu.tar.gz",
        "pseudolife-mcp-0.25.0-x86_64-apple-darwin.tar.gz",
        "pseudolife-mcp-0.25.0-x86_64-pc-windows-msvc.zip",
        "pseudolife-mcp-0.25.0-x86_64-unknown-linux-gnu.tar.gz",
    ];
    json!({
        "tag_name": "v0.25.0", "draft": false, "prerelease": false,
        "assets": names.map(|name| json!({"name": name, "state": "uploaded", "size": 100}))
    })
}

#[tokio::test]
async fn github_release_notice_requires_one_uploaded_nonempty_ready_marker() {
    let complete = complete_release();
    let mut noticed = Vec::new();
    for condition in ["missing", "uploading", "duplicate", "empty"] {
        let mut data = complete.clone();
        let assets = data["assets"].as_array_mut().unwrap();
        assert_eq!(assets[0]["name"], "RELEASE-READY.json");
        match condition {
            "missing" => {
                assets.remove(0);
            }
            "uploading" => assets[0]["state"] = json!("starter"),
            "duplicate" => assets.push(assets[0].clone()),
            "empty" => assets[0]["size"] = json!(0),
            _ => unreachable!(),
        }
        let result = fetch(200, &[], &data.to_string()).await;
        if !update_notice("0.24.0", result.latest.as_deref(), None, "pl").is_empty() {
            noticed.push(condition);
        }
    }
    assert!(
        noticed.is_empty(),
        "noticed invalid ready markers: {noticed:?}"
    );
    let latest = fetch(200, &[], &complete.to_string()).await.latest;
    assert!(!update_notice("0.24.0", latest.as_deref(), None, "pl").is_empty());
}

async fn fetch(status: u16, headers: &[(&str, &str)], body: &str) -> FetchResult {
    let response = response(status, headers, body);
    fetch_latest(&reqwest::Client::new(), |_| async { Ok::<_, ()>(response) }).await
}

#[tokio::test]
async fn github_release_payload_replaces_pypi() {
    assert_eq!(
        fetch(200, &[], &complete_release().to_string())
            .await
            .latest
            .as_deref(),
        Some("0.25.0")
    );
    assert!(
        fetch(200, &[], r#"{"info":{"version":"9.0.0"}}"#)
            .await
            .latest
            .is_none()
    );
}

#[tokio::test]
async fn incomplete_releases_wait_for_the_complete_uploaded_set() {
    let complete = complete_release();
    let mut absent = complete.clone();
    absent["assets"] = json!([]);
    let result = fetch(200, &[], &absent.to_string()).await;
    assert!(result.latest.is_none());
    assert_eq!(result.delay(21600.0, false), 21600.0);
    for index in 0..complete["assets"].as_array().unwrap().len() {
        let mut partial = complete.clone();
        partial["assets"].as_array_mut().unwrap().remove(index);
        let result = fetch(200, &[], &partial.to_string()).await;
        assert!(result.latest.is_none(), "missing asset {index}");
        assert_eq!(result.delay(21600.0, false), 21600.0);
        for (key, bad) in [("state", json!("starter")), ("size", json!(0))] {
            let mut uploading = complete.clone();
            uploading["assets"][index][key] = bad;
            assert!(
                fetch(200, &[], &uploading.to_string())
                    .await
                    .latest
                    .is_none()
            );
        }
    }
    assert_eq!(
        fetch(200, &[], &complete.to_string())
            .await
            .latest
            .as_deref(),
        Some("0.25.0")
    );
}

#[tokio::test]
async fn rejects_drafts_prereleases_and_malformed_tags() {
    for key in ["draft", "prerelease"] {
        for bad in [json!(true), Value::Null, json!("false"), json!(0)] {
            let mut data = complete_release();
            data[key] = bad;
            assert!(fetch(200, &[], &data.to_string()).await.latest.is_none());
        }
    }
    for tag in [
        "0.25.0",
        "vv0.25.0",
        "v0.25.0-rc1",
        "v0.25.0rc1",
        "v0.25.0+build",
        "v1",
        "v1.2.3.4.5",
        "v1..2",
        "v0.25.0\n",
        "v18446744073709551616.0",
    ] {
        let mut data = complete_release();
        data["tag_name"] = json!(tag);
        assert!(
            fetch(200, &[], &data.to_string()).await.latest.is_none(),
            "{tag:?}"
        );
    }
}

#[tokio::test]
async fn request_is_unauthenticated_github_get() {
    let client = reqwest::Client::new();
    fetch_latest(&client, |request| async move {
        assert_eq!(request.method(), reqwest::Method::GET);
        assert_eq!(
            request.url().as_str(),
            "https://api.github.com/repos/Pseudogiant-xr/Pseudolife-MCP/releases/latest"
        );
        assert!(request.body().is_none());
        assert_eq!(request.timeout(), Some(&Duration::from_secs(5)));
        assert!(request.headers().get("authorization").is_none());
        assert_eq!(request.headers()["user-agent"], "pseudolife-daemon");
        assert_eq!(request.headers()["accept"], "application/vnd.github+json");
        Ok::<_, ()>(response(200, &[], &complete_release().to_string()))
    })
    .await;
}

#[tokio::test]
async fn rate_limits_respect_headers_cap_and_normal_schedule() {
    for status in [403, 429] {
        for (headers, interval, expected) in [
            (vec![("retry-after", "120")], 60.0, 120.0),
            (vec![("retry-after", "7200")], 60.0, 3600.0),
            (vec![("retry-after", "120")], 21600.0, 21600.0),
            (vec![("retry-after", "0")], 60.0, 60.0),
            (vec![], 21600.0, 21600.0),
            (vec![("retry-after", "invalid")], 21600.0, 21600.0),
        ] {
            let result = fetch(status, &headers, "rate limited").await;
            assert!(result.latest.is_none());
            assert_eq!(result.delay(interval, false), expected);
            assert_eq!(result.delay(interval, true), expected);
        }
        let result = fetch(status, &[("x-ratelimit-reset", "9999999999")], "").await;
        assert_eq!(result.delay(60.0, false), 3600.0);
        let result = fetch(
            status,
            &[("retry-after", "120"), ("x-ratelimit-reset", "9999999999")],
            "",
        )
        .await;
        assert_eq!(result.delay(60.0, false), 120.0);
    }
}

#[tokio::test]
async fn errors_preserve_the_last_good_health_snapshot_and_notice() {
    let check = ReleaseCheck::default();
    check.record(Some("0.25.0".into()), 100.0);
    for (status, body) in [
        (404, "missing"),
        (500, "error"),
        (200, "not JSON"),
        (200, "{}"),
        (302, "redirect"),
    ] {
        let result = fetch(status, &[], body).await;
        assert!(result.latest.is_none());
        assert_eq!(result.delay(21600.0, false), 900.0);
        assert_eq!(result.delay(21600.0, true), 21600.0);
        check.record(result.latest, 110.0);
        assert_eq!(check.snapshot().latest_release.as_deref(), Some("0.25.0"));
        assert_eq!(check.snapshot().checked_at, 100.0);
        assert!(
            !update_notice(
                "0.24.0",
                check.snapshot().latest_release.as_deref(),
                None,
                "pl"
            )
            .is_empty()
        );
    }
    let offline = fetch_latest(&reqwest::Client::new(), |_| async {
        Err::<reqwest::Response, _>(())
    })
    .await;
    assert!(offline.latest.is_none());
}

#[tokio::test]
async fn pending_fetch_does_not_block_readers_and_shutdown_cancels_it() {
    let check = ReleaseCheck::default();
    check.record(Some("0.25.0".into()), 100.0);
    let (stop, receiver) = watch::channel(false);
    let calls = std::cell::Cell::new(0);
    let work = check.run_with_fetch(21600.0, receiver, || {
        calls.set(calls.get() + 1);
        std::future::pending::<FetchResult>()
    });
    tokio::pin!(work);
    assert!(futures::poll!(&mut work).is_pending());
    assert_eq!(calls.get(), 1);
    assert_eq!(check.snapshot().latest_release.as_deref(), Some("0.25.0"));
    assert!(
        !update_notice(
            "0.24.0",
            check.snapshot().latest_release.as_deref(),
            None,
            "pl"
        )
        .is_empty()
    );
    stop.send(true).unwrap();
    assert!(work.now_or_never().is_some());
    assert_eq!(calls.get(), 1);
    assert_eq!(check.snapshot().checked_at, 100.0);
}

#[test]
fn complete_asset_metadata_is_exact_and_version_bound() {
    for version in ["1.2", "01.02.003", "1.2.3.4", "18446744073709551615.0"] {
        let data: Value =
            serde_json::from_str(&complete_release().to_string().replace("0.25.0", version))
                .unwrap();
        assert_eq!(release_metadata(&data).latest.as_deref(), Some(version));
    }
    let mut data = complete_release();
    data["tag_name"] = json!("v0.26.0");
    assert!(release_metadata(&data).latest.is_none());
    let mut data = complete_release();
    let duplicate = data["assets"][0].clone();
    data["assets"].as_array_mut().unwrap().push(duplicate);
    assert!(release_metadata(&data).latest.is_none());
    let mut data = complete_release();
    data["assets"]
        .as_array_mut()
        .unwrap()
        .push(json!({"name": "unrelated"}));
    assert_eq!(release_metadata(&data).latest.as_deref(), Some("0.25.0"));
    for bad in [Value::Null, json!({}), json!("assets")] {
        let mut data = complete_release();
        data["assets"] = bad;
        assert!(release_metadata(&data).latest.is_none());
    }
}

#[test]
fn rate_limit_dates_fallback_and_past_headers_use_a_fixed_clock() {
    let now = 1704067200.0; // 2024-01-01T00:00:00Z
    for (pairs, expected) in [
        (
            vec![("retry-after", "Mon, 01 Jan 2024 00:02:00 GMT")],
            120.0,
        ),
        (vec![("retry-after", "Sun, 31 Dec 2023 23:59:00 GMT")], 0.0),
        (
            vec![("retry-after", "Mon, 01 Jan 2024 02:00:00 GMT")],
            3600.0,
        ),
        (vec![("x-ratelimit-reset", "1704067320")], 120.0),
        (vec![("x-ratelimit-reset", "1704067100")], 0.0),
        (
            vec![("retry-after", "bad"), ("x-ratelimit-reset", "1704067320")],
            120.0,
        ),
        (
            vec![("retry-after", "0"), ("x-ratelimit-reset", "1704067320")],
            0.0,
        ),
        (
            vec![("retry-after", "-1"), ("x-ratelimit-reset", "bad")],
            0.0,
        ),
        (vec![("retry-after", "1.5")], 0.0),
    ] {
        let response = response(429, &pairs, "");
        assert_eq!(
            rate_limit_wait(response.headers(), now),
            expected,
            "{pairs:?}"
        );
    }
}

#[test]
fn accepted_release_order_matches_numeric_client_tuples() {
    for (running, latest, newer) in [
        ("0.9.0", "0.10.0", true),
        ("01.02.003", "1.2.3", false),
        ("1.2", "1.2.0", true),
        ("1.2.0", "1.2", false),
        ("1.2.0-rc1", "1.2.0", false),
        ("1.2.0", "1.2.1", true),
        ("0.26.0", "0.25.0", false),
    ] {
        assert_eq!(
            !update_notice(running, Some(latest), None, "pl").is_empty(),
            newer
        );
    }
}
