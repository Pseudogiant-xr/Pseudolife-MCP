use super::*;

fn github_release() -> Value {
    let names = [
        "RELEASE-READY.json",
        "SHA256SUMS",
        "pseudolife-compose-0.25.0.tar.gz",
        "pseudolife-mcp-0.25.0-x86_64-unknown-linux-gnu.tar.gz",
        "pseudolife-mcp-0.25.0-aarch64-unknown-linux-gnu.tar.gz",
        "pseudolife-mcp-0.25.0-x86_64-apple-darwin.tar.gz",
        "pseudolife-mcp-0.25.0-aarch64-apple-darwin.tar.gz",
        "pseudolife-mcp-0.25.0-x86_64-pc-windows-msvc.zip",
    ];
    json!({
        "tag_name": "v0.25.0", "draft": false, "prerelease": false,
        "assets": names.map(|name| json!({"name": name, "state": "uploaded", "size": 100}))
    })
}

#[tokio::test]
async fn github_discovery_requires_one_uploaded_nonempty_ready_marker() {
    let complete = github_release();
    let mut accepted = Vec::new();
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
        if discovery_reply(200, data.to_string())
            .await
            .unwrap()
            .is_some()
        {
            accepted.push(condition);
        }
    }
    assert!(
        accepted.is_empty(),
        "accepted invalid ready markers: {accepted:?}"
    );
    assert_eq!(
        discovery_reply(200, complete.to_string())
            .await
            .unwrap()
            .as_deref(),
        Some("0.25.0")
    );
}

async fn discovery_reply(status: u16, body: String) -> Result<Option<String>> {
    discovery_reply_with_headers(status, body, &[]).await
}

async fn discovery_reply_with_headers(
    status: u16,
    body: String,
    headers: &[(&str, &str)],
) -> Result<Option<String>> {
    let mut response = http::Response::builder().status(status);
    for (name, value) in headers {
        response = response.header(*name, *value);
    }
    let response = response.body(body).unwrap();
    fetch_latest_with(&reqwest::Client::new(), |_| async { Ok(response.into()) }).await
}

#[tokio::test]
async fn github_discovery_redirect_names_a_published_tag_or_current_client() {
    for status in [301, 302, 304, 307, 308] {
        let error = discovery_reply(status, String::new()).await.unwrap_err();
        assert_eq!(error.exit_code, 2);
        assert!(error.message.contains(&format!("HTTP {status}")));
        assert!(error.message.contains("--tag"), "{error}");
        assert!(error.message.contains("current client"), "{error}");
    }
}

#[tokio::test]
async fn github_discovery_rejects_an_oversized_advertised_body() {
    let error = discovery_reply_with_headers(200, String::new(), &[("content-length", "1048577")])
        .await
        .unwrap_err();
    assert_eq!(error.exit_code, 2);
    assert!(error.message.contains("size limit"), "{error}");
}

#[tokio::test]
async fn github_discovery_bounds_a_body_that_never_finishes() {
    use futures::StreamExt;
    use std::time::Duration;

    for status in [200, 403] {
        // An unbounded reader reaches the pending tail instead of checking size.
        let stream = futures::stream::iter([vec![b' '; 600_000], vec![b' '; 600_000]])
            .map(Ok::<_, std::io::Error>)
            .chain(futures::stream::pending());
        let response = http::Response::builder()
            .status(status)
            .body(reqwest::Body::wrap_stream(stream))
            .unwrap();
        let error = tokio::time::timeout(
            Duration::from_secs(2),
            fetch_latest_with(&reqwest::Client::new(), |_| async { Ok(response.into()) }),
        )
        .await
        .expect("discovery waited for the oversized body's end")
        .unwrap_err();
        assert_eq!(error.exit_code, 2);
        assert!(error.message.contains("size limit"), "{error}");
    }
}

#[test]
fn github_discovery_uses_the_daemons_complete_asset_rule() {
    fn predicate(source: &str) -> String {
        let source = source.replace("\r\n", "\n");
        let (_, function) = source.split_once("\nfn complete_assets(").unwrap();
        function.split_once("\n}").unwrap().0.to_owned()
    }
    let client = predicate(include_str!("mod.rs"));
    assert!(client.contains(r#""RELEASE-READY.json".to_owned()"#));
    assert_eq!(
        client,
        predicate(include_str!("../../../../daemon/src/release_check.rs")),
        "update the client and daemon asset acceptance rules together"
    );
}

#[test]
fn github_discovery_uses_the_daemons_release_tag_rule() {
    let client = include_str!("mod.rs").replace("\r\n", "\n");
    let daemon = include_str!("../../../../daemon/src/release_check.rs").replace("\r\n", "\n");
    let tag = r#"data["tag_name"].as_str().and_then(|s| s.strip_prefix('v'))"#;
    let compact = |source: &str| source.split_whitespace().collect::<String>();
    assert!(compact(&client).contains(&compact(tag)) && compact(&daemon).contains(&compact(tag)));
    // The daemon's shape check also permits letters and .+-. Its component
    // check below is what narrows the accepted version to digits and dots.
    let shape = daemon
        .split_once("fn version_shape(value: &str) -> bool {")
        .unwrap()
        .1
        .split_once("\n}")
        .unwrap()
        .0;
    assert_eq!(
        compact(shape),
        compact(
            r#"(1..=32).contains(&value.len()) && value.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b".+-".contains(&b))"#
        )
    );
    fn rejection_rule(source: &str) -> &str {
        let (_, tail) = source
            .split_once("let parts: Vec<_> = version.split('.').collect();\n")
            .unwrap();
        tail.split_once("\n    {").unwrap().0
    }
    assert_eq!(
        rejection_rule(&client),
        rejection_rule(&daemon).replace(
            "!version_shape(version)",
            "!(1..=32).contains(&version.len())"
        ),
    );
}

#[test]
fn github_discovery_routes_explicit_tags_and_unpinned_requests() {
    let mut update = Update::new(Options::parse(vec![]).unwrap());
    update.release_lookup = Some(Box::new(|| Ok(Some("0.25.0".into()))));
    assert_eq!(update.target_version().unwrap().as_deref(), Some("0.25.0"));
    update.o.tag = Some("0.24.0".into());
    update.release_lookup = Some(Box::new(|| panic!("a pinned update tried discovery")));
    assert_eq!(update.target_version().unwrap().as_deref(), Some("0.24.0"));
}

fn no_release_update() -> Update {
    let mut update = Update::new(Options::parse(vec![]).unwrap());
    update.o.as_json = true;
    update.release_lookup = Some(Box::new(|| Ok(None)));
    update
}

#[test]
fn github_discovery_check_exits_three_for_incomplete_release() {
    let mut update = no_release_update();
    assert_eq!(update.run_with(|update| update.check("client")), 3);
    assert_eq!(
        update.report.steps,
        [
            "no newer release with all native assets is published yet. Name a published release with --tag <version> to install it now."
        ]
    );
    assert!(update.report.target.is_none());
}

#[test]
fn github_discovery_release_does_not_deploy_an_incomplete_release() {
    let mut update = no_release_update();
    update.command_probe = Some(|args| {
        assert_eq!(args[0], "inspect");
        assert!(args.iter().any(|arg| arg.contains(".Config.Labels")));
        (0, "{}".into())
    });
    update.health_probe = Some(|| panic!("the update continued past release discovery"));
    update.deploy_release(None, None, None).unwrap();
    assert_eq!(update.report.steps, [NO_PUBLISHED_RELEASE]);
    assert!(update.report.target.is_none());
    assert!(update.report.clients.is_none());
    assert!(!update.release_started);
}

#[tokio::test]
async fn github_discovery_requests_fixed_unauthenticated_endpoint() {
    let result = fetch_latest_with(&reqwest::Client::new(), |request| async move {
        assert_eq!(request.method(), reqwest::Method::GET);
        assert_eq!(
            request.url().as_str(),
            "https://api.github.com/repos/Pseudogiant-xr/Pseudolife-MCP/releases/latest"
        );
        assert!(request.body().is_none());
        assert!(request.headers().get("authorization").is_none());
        assert_eq!(request.headers()["accept"], "application/vnd.github+json");
        assert_eq!(request.headers()["user-agent"], "pseudolife-mcp");
        assert_eq!(request.timeout(), Some(&std::time::Duration::from_secs(5)));
        Ok(http::Response::new(github_release().to_string()).into())
    })
    .await
    .unwrap();
    assert_eq!(result.as_deref(), Some("0.25.0"));
}

#[tokio::test]
async fn github_discovery_waits_for_assets_and_rediscovers_completed_release() {
    let complete = github_release();
    let mut missing = complete.clone();
    missing.as_object_mut().unwrap().remove("assets");
    let mut empty = complete.clone();
    empty["assets"] = json!([]);
    let mut partial = complete.clone();
    partial["assets"].as_array_mut().unwrap().pop();
    let mut duplicate = complete.clone();
    let assets = duplicate["assets"].as_array_mut().unwrap();
    assets.push(assets[0].clone());
    let mut uploading = complete.clone();
    uploading["assets"][0]["state"] = json!("starter");
    let mut zero_size = complete.clone();
    zero_size["assets"][0]["size"] = json!(0);
    let mut different_version = complete.clone();
    different_version["assets"][2]["name"] = json!("pseudolife-compose-0.26.0.tar.gz");

    let mut wrongly_accepted = Vec::new();
    for (why, mut data) in [
        ("absent assets", missing),
        ("empty assets", empty),
        ("partial upload", partial),
        ("duplicate name", duplicate),
        ("upload still in progress", uploading),
        ("zero bytes", zero_size),
        ("wrong asset version", different_version),
    ] {
        data["info"] = json!({"version": "0.25.0"});
        if discovery_reply(200, data.to_string())
            .await
            .unwrap()
            .is_some()
        {
            wrongly_accepted.push(why);
        }
    }
    let after_retry = discovery_reply(200, complete.to_string()).await.unwrap();
    assert!(
        wrongly_accepted.is_empty() && after_retry.as_deref() == Some("0.25.0"),
        "accepted before completion: {wrongly_accepted:?}; release after retry: {after_retry:?}"
    );
}

#[tokio::test]
async fn github_discovery_rejects_drafts_and_prereleases() {
    let mut wrongly_accepted = Vec::new();
    for flag in ["draft", "prerelease"] {
        let mut data = github_release();
        data["info"] = json!({"version": "0.25.0"});
        data[flag] = json!(true);
        if discovery_reply(200, data.to_string())
            .await
            .unwrap()
            .is_some()
        {
            wrongly_accepted.push(flag);
        }
    }
    assert!(
        wrongly_accepted.is_empty(),
        "accepted: {wrongly_accepted:?}"
    );
    assert_eq!(discovery_reply(404, String::new()).await.unwrap(), None);
}

#[tokio::test]
async fn github_discovery_reports_malformed_metadata_and_unavailable_service() {
    let did_report = |reply: Result<Option<String>>, detail| {
        reply.is_err_and(|error| {
            error.exit_code == 2
                && error.message.contains(detail)
                && error.message.contains("Nothing was changed")
        })
    };
    let mut missed_errors = Vec::new();
    let data = github_release();
    for (field, bad) in [
        ("draft", json!("false")),
        ("prerelease", Value::Null),
        ("tag_name", json!("0.25.0")),
        ("tag_name", json!("vv0.25.0")),
        ("tag_name", json!("v1")),
        ("tag_name", json!("v1.2.3.4.5")),
        ("tag_name", json!("v0.+25.0")),
        ("tag_name", json!("v0.25.0-rc1")),
        ("tag_name", json!("v18446744073709551616.0")),
        ("tag_name", json!("v000000000000000000000000000000000.1")),
        ("assets", json!("uploading")),
    ] {
        let mut changed = data.clone();
        let case = format!("{field}={bad}");
        changed[field] = bad;
        let reply = discovery_reply(200, changed.to_string()).await;
        if !did_report(reply, "invalid release metadata") {
            missed_errors.push(case);
        }
    }
    for (status, body, detail) in [
        (200, "{", "invalid release metadata"),
        (
            403,
            r#"{"message":"You have exceeded a secondary rate limit."}"#,
            "rate limit",
        ),
        (403, r#"{"message":"Forbidden"}"#, "HTTP 403"),
        (429, "{}", "rate limit"),
        (503, "{}", "HTTP 503"),
        (302, "", "HTTP 302"),
    ] {
        let reply = discovery_reply(status, body.to_owned()).await;
        if !did_report(reply, detail) {
            missed_errors.push(format!("HTTP {status} {body:?}"));
        }
    }
    for header in [("x-ratelimit-remaining", "0"), ("retry-after", "120")] {
        let reply = discovery_reply_with_headers(403, "{}".to_owned(), &[header]).await;
        if !did_report(reply, "rate limit") {
            missed_errors.push(format!("HTTP 403 with {}: {}", header.0, header.1));
        }
    }
    assert!(
        missed_errors.is_empty(),
        "wrong or missing errors: {missed_errors:?}"
    );
}

#[tokio::test]
#[ignore = "run the real loopback transport on the Linux and Windows dev machines"]
async fn github_discovery_handles_network_failures_and_redirects_over_loopback() {
    use std::time::Duration;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;

    let client = discovery_client().no_proxy().build().unwrap();
    let mut missed_errors = Vec::new();
    for (case, detail) in [
        ("offline", "could not reach GitHub"),
        ("stalled headers", "timed out"),
        ("stalled body", "timed out"),
        ("redirect", "HTTP 302"),
    ] {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}/releases/latest", listener.local_addr().unwrap());
        let redirect_url = format!("{url}/elsewhere");
        let server = tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.unwrap();
            drop(listener);
            let mut request = [0; 2048];
            assert!(stream.read(&mut request).await.unwrap() > 0);
            match case {
                "stalled headers" => std::future::pending::<()>().await,
                "stalled body" => {
                    stream
                        .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 12\r\n\r\n{")
                        .await
                        .unwrap();
                    std::future::pending::<()>().await;
                }
                "redirect" => {
                    let reply = format!(
                        "HTTP/1.1 302 Found\r\nLocation: {redirect_url}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                    );
                    stream.write_all(reply.as_bytes()).await.unwrap();
                }
                _ => stream.shutdown().await.unwrap(),
            }
        });
        let result = fetch_latest_with(&client, |mut request| {
            *request.url_mut() = url.parse().unwrap();
            *request.timeout_mut() = Some(if detail == "timed out" {
                Duration::from_millis(250)
            } else {
                Duration::from_secs(2)
            });
            client.execute(request)
        })
        .await;
        if detail == "timed out" {
            server.abort();
        } else {
            tokio::time::timeout(Duration::from_secs(2), server)
                .await
                .unwrap()
                .unwrap();
        }
        if !result.is_err_and(|error| error.exit_code == 2 && error.message.contains(detail)) {
            missed_errors.push(case);
        }
    }
    assert!(
        missed_errors.is_empty(),
        "wrong or missing errors: {missed_errors:?}"
    );
}

#[test]
fn failed_health_endpoint_hint_uses_only_valid_loopback_publications() {
    for (ports, url) in [
        ("0.0.0.0:9876\n[::]:9876\n", "http://127.0.0.1:9876"),
        ("127.0.0.1:9876", "http://127.0.0.1:9876"),
        ("[::1]:9876", "http://[::1]:9876"),
    ] {
        let hint = release::health_endpoint_hint(ports);
        assert!(hint.contains(&format!("--daemon-url {url}")));
        // Port output is Docker-host-local even with a remote DOCKER_HOST or context.
        assert!(hint.contains("on the Docker host"));
        assert!(hint.contains("If Docker runs on this machine"));
    }
    for ports in [
        "",
        "10.0.0.7:9876",
        "127.0.0.1:0",
        "127.0.0.1:65536",
        "garbage:9876",
        "127.0.0.1:+9876",
    ] {
        let hint = release::health_endpoint_hint(ports);
        assert!(hint.contains("--daemon-url"));
        assert!(!hint.contains("--daemon-url http://"));
    }
}

#[test]
fn native_update_client_release_never_guesses_when_endpoint_has_no_version() {
    for health in [
        None,
        Some(json!({"status":"ok"})),
        Some(json!({"version":""})),
    ] {
        for tag in [None, Some("0.16.0")] {
            let error = client_release_target(health.as_ref(), tag).unwrap_err();
            assert_eq!(error.exit_code, 2);
            assert!(error.message.contains("--daemon-url"));
        }
    }
    let health = json!({"version":"0.15.1"});
    assert_eq!(
        client_release_target(Some(&health), None).unwrap(),
        "0.15.1"
    );
    let checkout = json!({"version":"0.15.1+checkout"});
    assert!(client_release_target(Some(&checkout), None).is_err());
    assert_eq!(
        client_release_target(Some(&checkout), Some("0.15.1")).unwrap(),
        "0.15.1"
    );
}

fn verify_bank_identity(config: &Value, running: &Value) -> Result<()> {
    engine::verify_bank_identity_with(config, running, |name| {
        Some(json!([
            {"Name":name,"Labels":{"com.docker.volume.anonymous":""}}
        ]))
    })
}
use std::io::{Cursor, Read, Seek, SeekFrom, Write};

#[cfg(windows)]
#[test]
fn native_update_external_paths_keep_drive_unc_and_unicode_without_verbatim_prefixes() {
    for (input, expected) in [
        (
            r"\\?\C:\checkout λ\ops\backup.ps1",
            r"C:\checkout λ\ops\backup.ps1",
        ),
        (
            r"\\?\UNC\server\share\checkout λ",
            r"\\server\share\checkout λ",
        ),
        (r"C:\checkout\ops", r"C:\checkout\ops"),
    ] {
        assert_eq!(seams::command_path(input.into()), PathBuf::from(expected));
    }
}
struct Temp(PathBuf);

#[test]
#[ignore = "subprocess fixture for isolated proxy environment"]
fn native_update_proxy_child() {
    let url = std::env::var("UPDATE_PROXY_TEST_URL").unwrap();
    let error = seams::block_on(async {
        let client = discovery_client().build().unwrap();
        fetch_latest_with(&client, |mut request| {
            *request.url_mut() = url.parse().unwrap();
            *request.timeout_mut() = Some(std::time::Duration::from_secs(2));
            client.execute(request)
        })
        .await
    })
    .unwrap_err();
    if url.starts_with("http:") {
        // The proxy replied with the old metadata shape. Discovery reached
        // the fixture and refused that response as a GitHub release.
        assert!(
            error.message.contains("invalid release metadata"),
            "{error}"
        );
    } else {
        // The fixture refuses CONNECT without contacting a release host.
        assert!(error.message.contains("could not reach GitHub"), "{error}");
    }
}

#[test]
fn native_update_release_discovery_honors_http_and_https_proxy() {
    use std::{
        net::TcpListener,
        process::Command,
        time::{Duration, Instant},
    };
    for scheme in ["http", "https"] {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let proxy = format!("http://{}", listener.local_addr().unwrap());
        let server = std::thread::spawn(move || {
            let deadline = Instant::now() + Duration::from_secs(4);
            loop {
                match listener.accept() {
                    Ok((mut stream, _)) => {
                        // Windows accepts inherit the listener's nonblocking
                        // mode; wait for the client's request bytes normally.
                        stream.set_nonblocking(false).unwrap();
                        stream
                            .set_read_timeout(Some(Duration::from_secs(2)))
                            .unwrap();
                        let mut request = Vec::new();
                        let mut byte = [0];
                        while !request.ends_with(b"\r\n\r\n") && request.len() < 8192 {
                            if stream.read(&mut byte).unwrap_or(0) == 0 {
                                break;
                            }
                            request.push(byte[0]);
                        }
                        let body = br#"{"info":{"version":"1.2.3"}}"#;
                        let reply = if request.starts_with(b"CONNECT ") {
                            b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".to_vec()
                        } else {
                            [format!("HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n", body.len()).into_bytes(), body.to_vec()].concat()
                        };
                        stream.write_all(&reply).unwrap();
                        return request;
                    }
                    Err(e)
                        if e.kind() == std::io::ErrorKind::WouldBlock
                            && Instant::now() < deadline =>
                    {
                        std::thread::sleep(Duration::from_millis(10));
                    }
                    _ => return Vec::new(),
                }
            }
        });
        let mut child = Command::new(std::env::current_exe().unwrap());
        for name in [
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "no_proxy",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
        ] {
            child.env_remove(name);
        }
        let result = child
            .args([
                "--exact",
                "cli::update::tests::native_update_proxy_child",
                "--ignored",
            ])
            .env(
                if scheme == "http" {
                    "HTTP_PROXY"
                } else {
                    "HTTPS_PROXY"
                },
                proxy,
            )
            .env(
                "UPDATE_PROXY_TEST_URL",
                format!("{scheme}://release.invalid/json"),
            )
            .output()
            .unwrap();
        let request = server.join().unwrap();
        assert!(
            result.status.success(),
            "{}",
            String::from_utf8_lossy(&result.stdout)
        );
        let expected = if scheme == "http" {
            "GET http://release.invalid/json HTTP/1.1"
        } else {
            "CONNECT release.invalid:443 HTTP/1.1"
        };
        assert!(
            request.starts_with(expected.as_bytes()),
            "proxy did not receive {scheme} discovery"
        );
        assert!(
            !String::from_utf8_lossy(&request)
                .to_ascii_lowercase()
                .contains("authorization:")
        );
    }
}
impl Temp {
    fn new() -> Self {
        let p = std::env::temp_dir().join(format!(
            "native-update-test-{}",
            uuid::Uuid::new_v4().simple()
        ));
        fs::create_dir(&p).unwrap();
        Self(p)
    }
}
impl Drop for Temp {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn checkout_schema_fixture() -> (Temp, PathBuf) {
    let repo = Temp::new();
    let manifest = repo.0.join(super::RELEASE_MANIFEST);
    fs::create_dir_all(manifest.parent().unwrap()).unwrap();
    fs::write(
        manifest,
        "[package]\nname = \"pseudolife-stdio\"\nversion = \"0.99.1\"\n\n[dependencies]\nversion = \"0.0.1\"\n",
    )
    .unwrap();
    // A decoy: pyproject.toml leaves with the Python package and is not read.
    fs::write(
        repo.0.join("pyproject.toml"),
        "[project]\nversion = \"0.0.9\"\n",
    )
    .unwrap();
    let plan = repo.0.join("rust/daemon/src/storage/schema_plan.json");
    fs::create_dir_all(plan.parent().unwrap()).unwrap();
    (repo, plan)
}

#[test]
fn checkout_release_version_reads_the_native_crate() {
    let repo = Temp::new();
    assert!(super::checkout_release_version(&repo.0).is_err());
    assert!(!super::is_checkout_root(&repo.0));
    let manifest = repo.0.join(super::RELEASE_MANIFEST);
    fs::create_dir_all(manifest.parent().unwrap()).unwrap();
    for (text, version) in [
        ("[package]\nversion = \"1.2.3\"\n", Some("1.2.3")),
        (
            "\u{feff}[package]\r\nversion = '1.2.3' # cut\r\n",
            Some("1.2.3"),
        ),
        ("[package]\nversion.workspace = true\n", None),
        ("[package]\nversion = \"\"\n", None),
        ("[workspace]\nversion = \"1.2.3\"\n", None),
    ] {
        fs::write(&manifest, text).unwrap();
        assert_eq!(
            super::checkout_release_version(&repo.0).unwrap().as_deref(),
            version,
            "{text:?}"
        );
    }
    // Invalid TOML is a broken checkout, not a manifest without a version.
    fs::write(&manifest, "[package]\nversion = \"1.2.3\"\nnot toml [\n").unwrap();
    let broken = super::checkout_release_version(&repo.0).unwrap_err();
    assert_eq!(broken.kind(), std::io::ErrorKind::InvalidData);
    assert!(broken.to_string().starts_with("not valid TOML"), "{broken}");
    // A checkout of this repository: its manifest names this crate. The
    // near miss, another project's rust/shim/Cargo.toml, is none.
    fs::write(&manifest, "[package]\nname = \"pseudolife-stdio\"\n").unwrap();
    assert!(super::is_checkout_root(&repo.0));
    fs::write(
        &manifest,
        "[package]\nname = \"another-shim\"\nversion = \"1.2.3\"\n",
    )
    .unwrap();
    assert!(!super::is_checkout_root(&repo.0));
    // A checkout from before the native release version is still one.
    fs::remove_file(&manifest).unwrap();
    fs::write(repo.0.join("pyproject.toml"), "[project]\n").unwrap();
    assert!(super::is_checkout_root(&repo.0));
}

#[test]
fn checkout_schema_names_a_missing_release_manifest() {
    let (repo, plan) = checkout_schema_fixture();
    fs::write(plan, br#"{"format":1,"schema_version":71,"steps":[]}"#).unwrap();
    fs::remove_file(repo.0.join(super::RELEASE_MANIFEST)).unwrap();
    let restore = |refused: &UpdateError| {
        refused
            .message
            .starts_with("cannot read rust/shim/Cargo.toml in this checkout (")
            && refused
                .message
                .ends_with("restore it (git checkout -- rust/shim/Cargo.toml) and retry.")
    };
    // A current checkout (rust/shim/ is there) whose manifest was deleted:
    // restore it, even beside pyproject.toml; a pull would not bring it back.
    let refused = engine::checkout_version_schema(&repo.0).unwrap_err();
    assert_eq!(refused.exit_code, 2);
    assert!(restore(&refused), "{}", refused.message);
    // No rust/shim/ at all: a checkout from before the native client
    // crate, which never had the manifest, is told to update.
    fs::remove_dir_all(repo.0.join("rust/shim")).unwrap();
    let refused = engine::checkout_version_schema(&repo.0).unwrap_err();
    assert_eq!(refused.exit_code, 2);
    assert_eq!(
        refused.message,
        "this checkout has no rust/shim/Cargo.toml, so it predates the native release version; update the checkout (git pull, or check out a newer release tag) and retry."
    );
    // The same without pyproject.toml.
    fs::remove_file(repo.0.join("pyproject.toml")).unwrap();
    let refused = engine::checkout_version_schema(&repo.0).unwrap_err();
    assert_eq!(refused.exit_code, 2);
    assert!(
        refused
            .message
            .starts_with("this checkout has no rust/shim/Cargo.toml"),
        "{}",
        refused.message
    );
    // rust/shim/ back without its manifest, and no pyproject.toml: restore.
    fs::create_dir_all(repo.0.join("rust/shim")).unwrap();
    let refused = engine::checkout_version_schema(&repo.0).unwrap_err();
    assert_eq!(refused.exit_code, 2);
    assert!(restore(&refused), "{}", refused.message);
}

#[test]
fn checkout_schema_names_the_release_manifest_without_a_version() {
    let (repo, plan) = checkout_schema_fixture();
    fs::write(plan, br#"{"format":1,"schema_version":71,"steps":[]}"#).unwrap();
    fs::write(
        repo.0.join(super::RELEASE_MANIFEST),
        "[package]\nname = \"x\"\n",
    )
    .unwrap();
    let refused = engine::checkout_version_schema(&repo.0).unwrap_err();
    assert_eq!(refused.exit_code, 2);
    assert!(
        refused
            .message
            .contains("cannot read the checkout version from rust/shim/Cargo.toml: its [package] table needs a plain `version = \"<x.y.z>\"` line"),
        "{}",
        refused.message
    );
}

#[test]
fn checkout_schema_reads_native_plan_without_python() {
    let (repo, plan) = checkout_schema_fixture();
    fs::write(plan, br#"{"format":1,"schema_version":71,"steps":[]}"#).unwrap();
    assert_eq!(
        engine::checkout_version_schema(&repo.0).unwrap(),
        ("0.99.1".to_owned(), 71),
    );
}

#[test]
fn checkout_schema_names_missing_native_plan() {
    let (repo, _) = checkout_schema_fixture();
    let refused = engine::checkout_version_schema(&repo.0).unwrap_err();
    assert_eq!(refused.exit_code, 2);
    assert!(
        refused
            .message
            .contains("cannot read rust/daemon/src/storage/schema_plan.json")
    );
    assert!(refused.message.contains("Restore that file and retry."));
}

#[test]
fn checkout_schema_refuses_non_integer_version() {
    let (repo, plan) = checkout_schema_fixture();
    for data in [
        "not JSON",
        "{}",
        r#"{"schema_version":"59"}"#,
        r#"{"schema_version":59.5}"#,
        r#"{"schema_version":9223372036854775808}"#,
    ] {
        fs::write(&plan, data).unwrap();
        let refused = engine::checkout_version_schema(&repo.0).unwrap_err();
        assert_eq!(refused.exit_code, 2);
        assert!(
            refused.message.contains("positive integer schema_version"),
            "{data}"
        );
    }
}

#[test]
fn checkout_schema_refuses_non_positive_version() {
    let (repo, plan) = checkout_schema_fixture();
    for version in [0, -1] {
        fs::write(&plan, format!(r#"{{"schema_version":{version}}}"#)).unwrap();
        let refused = engine::checkout_version_schema(&repo.0).unwrap_err();
        assert_eq!(refused.exit_code, 2);
        assert!(
            refused.message.contains("positive integer schema_version"),
            "{version}"
        );
    }
}

#[test]
#[ignore = "subprocess fixture for command-tree timeout"]
fn native_update_timeout_child() {
    use std::time::Duration;

    let home = PathBuf::from(std::env::var_os("UPDATE_TIMEOUT_HOME").unwrap());
    if std::env::var_os("UPDATE_TIMEOUT_DESCENDANT").is_some() {
        // Readiness means this process holds the lock. Only process exit (or
        // explicit fixture teardown) releases it, regardless of scheduling.
        let active = fs::OpenOptions::new()
            .create_new(true)
            .read(true)
            .write(true)
            .open(home.join("descendant.lock"))
            .unwrap();
        active.try_lock().unwrap();
        fs::write(home.join("ready"), b"ready").unwrap();
        while home.exists() && !home.join("stop").exists() {
            std::thread::sleep(Duration::from_millis(20));
        }
        drop(active);
    } else {
        let mut child = std::process::Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "cli::update::tests::native_update_timeout_child",
                "--ignored",
            ])
            .env("UPDATE_TIMEOUT_DESCENDANT", "1")
            // The lifetime lock is the oracle. Do not let inherited Windows
            // pipes keep Tokio's blocking reads alive when deliberately testing
            // broken job cleanup; that would hide the lock assertion in a hang.
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .spawn()
            .unwrap();
        let _ = child.wait();
    }
}

#[test]
fn native_update_timeout_terminates_command_descendants() {
    use std::time::{Duration, Instant};

    struct StopOnDrop<'a>(&'a Path);
    impl Drop for StopOnDrop<'_> {
        fn drop(&mut self) {
            // A broken cleanup implementation must fail without leaving its
            // surviving descendant behind, including during panic unwinding.
            let _ = fs::write(self.0.join("stop"), b"stop");
        }
    }

    let temp = Temp::new();
    let _stop = StopOnDrop(&temp.0);
    let mut env: Environment = std::env::vars().collect();
    env.insert(
        "UPDATE_TIMEOUT_HOME".into(),
        temp.0.to_string_lossy().into_owned(),
    );
    env.remove("UPDATE_TIMEOUT_DESCENDANT");
    let argv = [
        std::env::current_exe()
            .unwrap()
            .to_string_lossy()
            .into_owned(),
        "--exact".into(),
        "cli::update::tests::native_update_timeout_child".into(),
        "--ignored".into(),
    ];
    let (code, message) = std::thread::scope(|scope| {
        let (send, receive) = std::sync::mpsc::sync_channel(1);
        scope.spawn(move || {
            // The old two-second deadline raced two executable startups on
            // Windows (#1195). Match the client fixture's readiness allowance.
            let _ = send.send(run_cli(&argv, Some(&env), 30, false));
        });
        match receive.recv_timeout(Duration::from_secs(60)) {
            Ok(result) => result,
            Err(_) => {
                // Windows can retain inherited pipe handles even with null
                // stdio. A deliberate containment defect must fail and tear
                // down its fixture, not deadlock while joining the IO worker.
                fs::write(temp.0.join("stop"), b"stop").unwrap();
                panic!("command timeout did not finish within its teardown budget");
            }
        }
    });
    assert_eq!(code, 1);
    assert_eq!(message, "command timed out");
    assert!(temp.0.join("ready").is_file(), "descendant never started");
    let lifetime = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(temp.0.join("descendant.lock"))
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        match lifetime.try_lock() {
            Ok(()) => break,
            Err(fs::TryLockError::WouldBlock) => {
                assert!(
                    Instant::now() < deadline,
                    "descendant survived command timeout: lifetime lock still held"
                );
                std::thread::sleep(Duration::from_millis(20));
            }
            Err(error) => panic!("cannot check descendant lifetime lock: {error}"),
        }
    }
}
#[cfg(unix)]
#[test]
fn native_update_suspend_preserves_command_budget() {
    assert_suspend_preserves_budget("cli::update::tests::native_update_suspend_driver");
}

#[cfg(unix)]
#[allow(unsafe_code)]
pub(super) fn assert_suspend_preserves_budget(driver: &str) {
    use std::process::{Child, Command, Stdio};
    use std::time::{Duration, Instant};

    struct Owned<'a>(Child, &'a Path);
    impl Drop for Owned<'_> {
        fn drop(&mut self) {
            let _ = fs::write(self.1.join("finish"), b"finish");
            if self.0.try_wait().ok().flatten().is_some() {
                return;
            }
            if let Ok(pid) = fs::read_to_string(self.1.join("ready"))
                && let Ok(pid) = pid.parse::<i32>()
            {
                // SAFETY: the fixture wrote its owned process-group id to this
                // unique directory; resume it so teardown can release it.
                unsafe { libc::kill(-pid, libc::SIGCONT) };
            }
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }
    let root = Temp::new();
    let child = Command::new(std::env::current_exe().unwrap())
        .args(["--exact", driver, "--ignored", "--quiet"])
        .env("UPDATE_SUSPEND_ROOT", &root.0)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let mut owned = Owned(child, &root.0);
    let deadline = Instant::now() + Duration::from_secs(30);
    while !root.0.join("ready").exists() {
        assert!(
            owned.0.try_wait().unwrap().is_none(),
            "runner exited before readiness"
        );
        assert!(Instant::now() < deadline, "command did not become ready");
        std::thread::sleep(Duration::from_millis(20));
    }
    // SAFETY: signal and wait only for this still-live owned driver process.
    assert_eq!(unsafe { libc::kill(owned.0.id() as i32, libc::SIGTSTP) }, 0);
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        let mut status = 0;
        let got = unsafe {
            libc::waitpid(
                owned.0.id() as i32,
                &mut status,
                libc::WNOHANG | libc::WUNTRACED,
            )
        };
        assert!(got >= 0);
        if got != 0 {
            assert!(
                libc::WIFSTOPPED(status),
                "runner exited instead of stopping"
            );
            break;
        }
        assert!(Instant::now() < deadline, "runner did not stop");
        std::thread::sleep(Duration::from_millis(20));
    }
    // The command's five-second budget must not include these six seconds in
    // a verified stopped state. Keep it blocked briefly after resume so an
    // expired timer cannot be hidden by a simultaneously completed command.
    std::thread::sleep(Duration::from_secs(6));
    assert_eq!(unsafe { libc::kill(owned.0.id() as i32, libc::SIGCONT) }, 0);
    let observe = Instant::now() + Duration::from_millis(300);
    while Instant::now() < observe {
        assert!(
            owned.0.try_wait().unwrap().is_none(),
            "suspension consumed the command timeout"
        );
        std::thread::sleep(Duration::from_millis(20));
    }
    fs::write(root.0.join("finish"), b"finish").unwrap();
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        if let Some(status) = owned.0.try_wait().unwrap() {
            assert!(
                status.success(),
                "resumed command did not finish successfully"
            );
            break;
        }
        assert!(Instant::now() < deadline, "resumed command did not finish");
        std::thread::sleep(Duration::from_millis(20));
    }
}

#[cfg(unix)]
#[test]
#[ignore = "subprocess fixture"]
fn native_update_suspend_driver() {
    let env: Environment = std::env::vars().collect();
    let result = run_cli(
        &[
            std::env::current_exe()
                .unwrap()
                .to_string_lossy()
                .into_owned(),
            "--exact".into(),
            "cli::update::tests::native_update_suspend_command".into(),
            "--ignored".into(),
        ],
        Some(&env),
        5,
        false,
    );
    assert_eq!(result.0, 0, "resumed command: {}", result.1);
}

#[cfg(unix)]
#[test]
#[ignore = "subprocess fixture"]
fn native_update_suspend_command() {
    use std::time::{Duration, Instant};
    let root = PathBuf::from(std::env::var_os("UPDATE_SUSPEND_ROOT").unwrap());
    fs::write(root.join("ready"), std::process::id().to_string()).unwrap();
    let deadline = Instant::now() + Duration::from_secs(60);
    while root.exists() && !root.join("finish").exists() && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(20));
    }
}

fn gzip(bytes: &[u8]) -> Vec<u8> {
    let mut gz = flate2::write::GzEncoder::new(vec![], flate2::Compression::default());
    gz.write_all(bytes).unwrap();
    gz.finish().unwrap()
}
fn archive(files: &[(&str, &[u8])]) -> Vec<u8> {
    let gz = flate2::write::GzEncoder::new(vec![], flate2::Compression::default());
    let mut tar = tar::Builder::new(gz);
    for (name, bytes) in files {
        let mut h = tar::Header::new_gnu();
        h.set_size(bytes.len() as u64);
        h.set_mode(0o644);
        h.set_cksum();
        tar.append_data(&mut h, name, Cursor::new(bytes)).unwrap();
    }
    tar.into_inner().unwrap().finish().unwrap()
}
fn sums(bytes: &[u8]) -> Vec<u8> {
    use sha2::{Digest, Sha256};
    format!(
        "{:x}  pseudolife-compose-0.21.0.tar.gz\n",
        Sha256::digest(bytes)
    )
    .into_bytes()
}

#[test]
fn native_update_plain_versions_reject_requirements() {
    for v in ["0.21.0", "1.0", "1.2.3.4"] {
        assert!(plain_release(v));
    }
    for v in ["1", "v1.0", "1.0rc1", "1.0+local", "1.2; rm", "١.٢", "1..2"] {
        assert!(!plain_release(v));
    }
}
#[test]
fn native_update_option_refusals_precede_effects() {
    for args in [
        vec!["--clients-only", "--daemon-only"],
        vec!["--shadow"],
        vec!["--checkout", "repo", "--tag", "1.2.3"],
        vec!["--unattended", "--no-backup"],
        vec!["--tag", "1.2;bad"],
        vec!["--rollback-tag", "bad/tag"],
        vec!["--no-test-login"],
        vec!["--engine", "nope"],
    ] {
        assert_eq!(
            Options::parse(args.into_iter().map(OsString::from).collect())
                .unwrap_err()
                .exit_code,
            2
        );
    }
}
#[test]
fn native_update_engine_is_opt_in() {
    assert!(Options::default().engine.is_none());
    let options = Options::parse(
        ["--engine", "rust", "--checkout", "repo", "--shadow"]
            .into_iter()
            .map(OsString::from)
            .collect(),
    )
    .unwrap();
    assert_eq!(options.engine.as_deref(), Some("rust"));
    assert!(options.shadow);
    // Release mode selects the published Rust image (G2-A).
    let release = Options::parse(
        ["--engine", "rust", "--tag", "1.2.3"]
            .into_iter()
            .map(OsString::from)
            .collect(),
    )
    .unwrap();
    assert_eq!(release.engine.as_deref(), Some("rust"));
    assert!(release.checkout.is_none());
}
#[test]
fn native_update_lock_excludes_and_releases() {
    let temp = Temp::new();
    let mut first = os_lock::Lock::new(temp.0.join("update.lock"));
    let mut second = os_lock::Lock::new(temp.0.join("update.lock"));
    assert!(matches!(first.acquire(), Ok(true)));
    assert!(matches!(second.acquire(), Ok(false)));
    first.release();
    assert!(matches!(second.acquire(), Ok(true)));
    second.release();
}
#[test]
fn native_update_dump_marker_outside_copy_only() {
    let temp = Temp::new();
    let path = temp.0.join("dump.gz");
    fs::write(
        &path,
        gzip(
            b"COPY public.entries (body) FROM stdin;\n-- PostgreSQL database dump complete\n\\.\n",
        ),
    )
    .unwrap();
    assert!(!backup::dump_complete(&path));
    fs::write(&path,gzip(b"COPY public.entries (body) FROM stdin;\nrow\n\\.\n-- PostgreSQL database dump complete\n")).unwrap();
    assert!(backup::dump_complete(&path));
    let mut broken = fs::read(&path).unwrap();
    broken.pop();
    fs::write(&path, broken).unwrap();
    assert!(!backup::dump_complete(&path));
}
#[test]
fn native_update_backup_requires_changed_complete_pair() {
    let temp = Temp::new();
    let before = backup::snapshot(&temp.0);
    let dump = temp.0.join("pseudolife_memory-20260101-000001.sql.gz");
    fs::write(&dump, gzip(b"-- PostgreSQL database dump complete\n")).unwrap();
    assert!(backup::verify(&temp.0, &before).is_err());
    let state = temp.0.join("pseudolife_state-20260101-000001.tgz");
    fs::write(&state, archive(&[("state.json", b"{}")])).unwrap();
    assert!(backup::verify(&temp.0, &before).is_ok());
    assert!(backup::verify(&temp.0, &backup::snapshot(&temp.0)).is_err());
    fs::write(&state, gzip(b"not a tar")).unwrap();
    assert!(backup::verify(&temp.0, &before).is_err());
}
#[test]
fn native_update_compose_requires_checksum_and_closed_archive() {
    let files = [
        ("docker-compose.yml", b"services: {}".as_slice()),
        ("docker-compose.ghcr.yml", b"services: {}".as_slice()),
        ("docker-compose.rust-daemon.yml", b"services: {}".as_slice()),
        ("docker-compose.rust-shadow.yml", b"services: {}".as_slice()),
        ("docker-compose.rust-ghcr.yml", b"services: {}".as_slice()),
        ("Dockerfile.extractor", b"FROM scratch".as_slice()),
    ];
    let bytes = archive(&files);
    assert_eq!(
        release::verified_compose("0.21.0", &sums(&bytes), &bytes)
            .unwrap()
            .len(),
        6
    );
    let mut corrupt = bytes.clone();
    corrupt[8] ^= 1;
    assert!(release::verified_compose("0.21.0", &sums(&bytes), &corrupt).is_err());
    let incomplete = archive(&files[..4]);
    assert!(release::verified_compose("0.21.0", &sums(&incomplete), &incomplete).is_err());
    let mut extra = files.to_vec();
    extra.push(("unexpected", b"x"));
    let bytes = archive(&extra);
    assert!(release::verified_compose("0.21.0", &sums(&bytes), &bytes).is_err());
    extra.pop();
    extra.push(files[0]);
    let bytes = archive(&extra);
    assert!(release::verified_compose("0.21.0", &sums(&bytes), &bytes).is_err());
}
#[test]
fn native_update_compose_refuses_an_archive_that_inflates_past_any_bundle() {
    let files = [
        ("docker-compose.yml", b"services: {}".as_slice()),
        ("docker-compose.ghcr.yml", b"services: {}".as_slice()),
        ("docker-compose.rust-daemon.yml", b"services: {}".as_slice()),
        ("docker-compose.rust-shadow.yml", b"services: {}".as_slice()),
        ("docker-compose.rust-ghcr.yml", b"services: {}".as_slice()),
        ("Dockerfile.extractor", b"FROM scratch".as_slice()),
    ];
    // A second gzip member after the tar: only the drain inflates it, and
    // its checksum matches, so only the unpacked-size bound can refuse it.
    let mut bomb = archive(&files);
    bomb.extend(gzip(&vec![0u8; 9 * 1024 * 1024]));
    let error = release::verified_compose("0.21.0", &sums(&bomb), &bomb).unwrap_err();
    assert!(error.to_string().contains("more than 8 MiB"), "{error}");
    // The near miss: a small trailing member still verifies.
    let mut small = archive(&files);
    small.extend(gzip(&vec![0u8; 64 * 1024]));
    assert_eq!(
        release::verified_compose("0.21.0", &sums(&small), &small)
            .unwrap()
            .len(),
        6
    );
}
#[test]
fn native_update_null_labels_and_failed_activation_are_distinct() {
    let temp = Temp::new();
    let accepted = temp.0.join(engine::SELECTION);
    let service = json!({"image":"fixture:accepted","labels":{(engine::ENGINE_LABEL):"rust",(engine::CHECKOUT_LABEL):temp.0,(engine::ACTIVATION_LABEL):"accepted"}});
    engine::write_overlay(&accepted, &service).unwrap();
    assert!(engine::validated(&service["labels"]).is_ok());
    let mut failed = service["labels"].clone();
    failed[engine::ACTIVATION_LABEL] = json!("failed");
    assert!(
        engine::validated(&failed)
            .unwrap_err()
            .message
            .contains("rollback")
    );
    assert!(engine::selection(&temp.0.join("absent")).unwrap().is_none());
    fs::write(&accepted, b"null").unwrap();
    assert!(engine::selection(&accepted).is_err());
    let running = json!({"Config":{"Labels":null}});
    assert_eq!(
        engine::rollback_service(&running, "fixture:rollback", None)["labels"]
            [engine::ENGINE_LABEL],
        "python"
    );
}
#[test]
fn native_update_mapped_bearer_preserves_colons() {
    let config = json!({"services":{DAEMON_SERVICE:{"environment":{"PSEUDOLIFE_MCP_TOKENS":" malformed, :reader, reserved:default, fixture:colon : Reader , next:other "}}}});
    assert!(engine::daemon_token(&config).is_ok_and(|v| v == "fixture:colon"));
}

#[test]
fn native_update_identity_checks_every_effective_mount() {
    let mut config = json!({"services":{DAEMON_SERVICE:{"environment":{"PSEUDOLIFE_MCP_DATABASE_URL":"fixture"},"volumes":[
        {"type":"volume","source":"state","target":"/data"},
        {"type":"bind","source":"/original/config.yaml","target":"/data/config.yaml","read_only":true}
    ]}},"volumes":{"state":{"name":"fixture-state"}}});
    let running = json!({"Config":{"Env":["PSEUDOLIFE_MCP_DATABASE_URL=fixture"]},"Mounts":[
        {"Type":"volume","Name":"fixture-state","Destination":"/data","RW":true},
        {"Type":"bind","Source":"/original/config.yaml","Destination":"/data/config.yaml","RW":false}
    ]});
    assert!(verify_bank_identity(&config, &running).is_ok());
    config["services"][DAEMON_SERVICE]["volumes"][1]["source"] = json!("/bundle/config.yaml");
    assert!(verify_bank_identity(&config, &running).is_err());
    config["services"][DAEMON_SERVICE]["volumes"][1]["source"] = json!("/original/config.yaml");
    config["services"][DAEMON_SERVICE]["volumes"][1]["read_only"] = json!(false);
    assert!(verify_bank_identity(&config, &running).is_err());
    config["services"][DAEMON_SERVICE]["volumes"]
        .as_array_mut()
        .unwrap()
        .pop();
    assert!(verify_bank_identity(&config, &running).is_err());
}

#[test]
fn native_update_mount_identity_normalizes_windows_docker_desktop_sources() {
    for actual in [
        r"c:\fixture\config.yaml",
        "c:/fixture/config.yaml",
        "/run/desktop/mnt/host/c/fixture/config.yaml",
        "//./C:/fixture/config.yaml",
        r"\\?\C:\fixture\config.yaml",
        r"C:\FIXTURE\Config.YAML",
    ] {
        let config = json!({"services":{DAEMON_SERVICE:{"environment":{"PSEUDOLIFE_MCP_DATABASE_URL":"fixture"},"volumes":[
            {"type":"volume","source":"state","target":"/data"},
            {"type":"bind","source":r"C:\fixture\config.yaml","target":"/config","read_only":true}
        ]}},"volumes":{"state":{"name":"fixture-state"}}});
        let mut running = json!({"Config":{"Env":["PSEUDOLIFE_MCP_DATABASE_URL=fixture"]},"Mounts":[
            {"Type":"volume","Name":"fixture-state","Destination":"/data","RW":true},
            {"Type":"bind","Source":actual,"Destination":"/config","RW":false}
        ]});
        assert!(verify_bank_identity(&config, &running).is_ok(), "{actual}");
        running["Mounts"][1]["Source"] = json!("/run/desktop/mnt/host/d/fixture/config.yaml");
        assert!(verify_bank_identity(&config, &running).is_err());
    }
}

#[test]
fn native_update_unc_mounts_compare_without_changing_posix_case() {
    for (expected, actual, same) in [
        (
            r"\\server\share\Config.yaml",
            r"\\?\UNC\SERVER\SHARE\config.yaml",
            true,
        ),
        (
            r"\\server\share\Config.yaml",
            "//server/share/config.yaml",
            true,
        ),
        ("/original/Config.yaml", "/original/config.yaml", false),
        (
            r"\\server\share\Config.yaml",
            r"\\other\share\config.yaml",
            false,
        ),
    ] {
        let config = json!({"services":{DAEMON_SERVICE:{"environment":{"PSEUDOLIFE_MCP_DATABASE_URL":"fixture"},"volumes":[
            {"type":"volume","source":"state","target":"/data"},
            {"type":"bind","source":expected,"target":"/config","read_only":true}
        ]}},"volumes":{"state":{"name":"fixture-state"}}});
        let running = json!({"Config":{"Env":["PSEUDOLIFE_MCP_DATABASE_URL=fixture"]},"Mounts":[
            {"Type":"volume","Name":"fixture-state","Destination":"/data","RW":true},
            {"Type":"bind","Source":actual,"Destination":"/config","RW":false}
        ]});
        assert_eq!(verify_bank_identity(&config, &running).is_ok(), same);
    }
}

#[test]
fn native_update_mount_identity_ignores_tmpfs_and_sourceless_mounts() {
    let config = json!({"services":{DAEMON_SERVICE:{"environment":{"PSEUDOLIFE_MCP_DATABASE_URL":"fixture"},"volumes":[
        {"type":"volume","source":"state","target":"/data"},
        {"type":"volume","target":"/cache"},
        {"type":"tmpfs","target":"/tmp"}
    ]}},"volumes":{"state":{"name":"fixture-state"}}});
    let running = json!({"Config":{"Env":["PSEUDOLIFE_MCP_DATABASE_URL=fixture"]},"HostConfig":{"Mounts":[{"Type":"volume","Target":"/cache"}]},"Mounts":[
        {"Type":"volume","Name":"fixture-state","Destination":"/data","RW":true},
        {"Type":"volume","Name":"generated-anonymous","Destination":"/cache","RW":true},
        {"Type":"tmpfs","Destination":"/tmp","RW":true},
        {"Type":"tmpfs","Destination":"/other-tmp","RW":true},
        {"Type":"bind","Destination":"/sourceless","RW":true}
    ]});
    assert!(verify_bank_identity(&config, &running).is_ok());
}

#[test]
fn native_update_ephemeral_mount_cannot_hide_persistent_contents() {
    for previous in ["bind", "volume"] {
        for replacement in ["tmpfs", "volume"] {
            let config = json!({"services":{DAEMON_SERVICE:{"environment":{"PSEUDOLIFE_MCP_DATABASE_URL":"fixture"},"volumes":[
                {"type":"volume","source":"state","target":"/data"},
                {"type":replacement,"target":"/cache"}
            ]}},"volumes":{"state":{"name":"fixture-state"}}});
            let mut running = json!({"Config":{"Env":["PSEUDOLIFE_MCP_DATABASE_URL=fixture"]},"Mounts":[
                {"Type":"volume","Name":"fixture-state","Destination":"/data","RW":true},
                {"Type":previous,"Name":"persistent-cache","Source":"/persistent/cache","Destination":"/cache","RW":false}
            ]});
            let refusal = verify_bank_identity(&config, &running).unwrap_err();
            assert!(refusal.message.contains("/cache") && refusal.message.contains("restore"));
            running["Mounts"][1] = json!({"Type":"tmpfs","Destination":"/cache"});
            assert!(verify_bank_identity(&config, &running).is_ok());
            running["Mounts"].as_array_mut().unwrap().pop();
            assert!(verify_bank_identity(&config, &running).is_ok());
        }
    }
}
#[test]
fn native_update_anonymous_volume_survives_second_recreation() {
    let name = "a".repeat(64);
    let mut config = json!({"services":{DAEMON_SERVICE:{"environment":{"PSEUDOLIFE_MCP_DATABASE_URL":"fixture"},"volumes":[
        {"type":"volume","source":"state","target":"/data"},
        {"type":"volume","target":"/cache"}
    ]}},"volumes":{"state":{"name":"fixture-state"}}});
    let mut running = json!({"Config":{"Env":["PSEUDOLIFE_MCP_DATABASE_URL=fixture"]},"HostConfig":{"Mounts":[{"Type":"volume","Target":"/cache"}]},"Mounts":[
        {"Type":"volume","Name":"fixture-state","Destination":"/data","RW":true},
        {"Type":"volume","Name":name,"Source":format!("/var/lib/docker/volumes/{name}/_data"),"Destination":"/cache","RW":true}
    ]});
    assert!(verify_bank_identity(&config, &running).is_ok());
    // Compose inherits the anonymous volume on the first recreate. The next
    // update sees its generated name in HostConfig, not an empty Source.
    running["HostConfig"]["Mounts"][0]["Source"] = json!(name);
    for _generation in [2, 3] {
        assert!(verify_bank_identity(&config, &running).is_ok());
    }
    config["volumes"]["cache"] = json!({"name":name});
    assert!(verify_bank_identity(&config, &running).is_err());
    config["volumes"].as_object_mut().unwrap().remove("cache");
    running["HostConfig"]["Mounts"][0]["Source"] = json!("different-volume");
    assert!(verify_bank_identity(&config, &running).is_err());
    running["HostConfig"] = Value::Null;
    assert!(verify_bank_identity(&config, &running).is_err());
}

#[test]
fn native_update_external_hash_volume_requires_anonymous_provenance() {
    let name = "a".repeat(64);
    let config = json!({"services":{DAEMON_SERVICE:{"environment":{"PSEUDOLIFE_MCP_DATABASE_URL":"fixture"},"volumes":[
        {"type":"volume","source":"state","target":"/data"}, {"type":"tmpfs","target":"/cache"}
    ]}},"volumes":{"state":{"name":"fixture-state"}}});
    let running = json!({"Config":{"Env":["PSEUDOLIFE_MCP_DATABASE_URL=fixture"]},"HostConfig":{"Mounts":[{"Type":"volume","Target":"/cache","Source":name}]},"Mounts":[
        {"Type":"volume","Name":"fixture-state","Destination":"/data","RW":true},
        {"Type":"volume","Name":name,"Source":format!("/var/lib/docker/volumes/{name}/_data"),"Destination":"/cache","RW":true}
    ]});
    for metadata in [
        None,
        Some(json!([])),
        Some(json!({})),
        Some(json!([{"Name":"other-volume","Labels":{"com.docker.volume.anonymous":""}}])),
        Some(json!([{"Name":name,"Labels":{}}])),
        Some(json!([{"Name":name,"Labels":null}])),
        Some(json!([{"Name":name,"Labels":{"com.docker.volume.anonymous":true}}])),
    ] {
        let error =
            engine::verify_bank_identity_with(&config, &running, |_| metadata.clone()).unwrap_err();
        assert!(
            error.message.contains("/cache")
                && error.message.contains("declare")
                && error.message.contains("recreate")
        );
    }
}

#[test]
fn native_update_anonymous_volume_requires_name_and_resolved_named_set() {
    for missing_name in [true, false] {
        let mut config = json!({"services":{DAEMON_SERVICE:{"environment":{"PSEUDOLIFE_MCP_DATABASE_URL":"fixture"},"volumes":[
            {"type":"volume","source":"state","target":"/data"}, {"type":"volume","target":"/cache"}
        ]}},"volumes":{"state":{"name":"fixture-state"}}});
        let mut running = json!({"Config":{"Env":["PSEUDOLIFE_MCP_DATABASE_URL=fixture"]},"HostConfig":{"Mounts":[{"Type":"volume","Target":"/cache"}]},"Mounts":[
            {"Type":"volume","Name":"fixture-state","Destination":"/data","RW":true},
            {"Type":"volume","Name":"a".repeat(64),"Destination":"/cache","RW":true}
        ]});
        if missing_name {
            running["Mounts"][1].as_object_mut().unwrap().remove("Name");
        } else {
            config["volumes"]["unresolved"] = json!({});
        }
        assert!(
            verify_bank_identity(&config, &running).is_err(),
            "missing_name={missing_name}"
        );
    }
}

#[test]
fn native_update_rollback_records_commit_after_recreation() {
    let temp = Temp::new();
    let commands = engine::rollback_commands(
        &[],
        &[],
        &temp.0.join("rollback.json"),
        &temp.0.join("accepted.json"),
        Some(&temp.0.join("selected.json")),
        Some("1.2.3"),
    );
    let command = commands.join("\n");
    let recreate = command.find("up -d --no-deps --no-build").unwrap();
    let copy = command
        .find(if cfg!(windows) { "Copy-Item" } else { "cp --" })
        .unwrap();
    let remove = command
        .find(if cfg!(windows) {
            "Remove-Item -LiteralPath"
        } else {
            "rm -f --"
        })
        .unwrap();
    assert!(recreate < copy && copy < remove);
    assert!(command.contains("Restore PSEUDOLIFE_MCP_TOKEN"));
    assert!(command.contains(if cfg!(windows) {
        "$LASTEXITCODE -eq 0"
    } else {
        "&&"
    }));
    assert!(command.contains("--pull never"));
    let tag = command.find("PSEUDOLIFE_IMAGE_TAG=").unwrap();
    assert!(tag < command.find("docker compose").unwrap());
    assert!(command[tag..].contains("1.2.3"));
}
struct FaultFile {
    data: Cursor<Vec<u8>>,
    limit: usize,
    fail_after: usize,
    writes: usize,
    fail_sync: bool,
}
impl Read for FaultFile {
    fn read(&mut self, b: &mut [u8]) -> std::io::Result<usize> {
        self.data.read(b)
    }
}
impl Seek for FaultFile {
    fn seek(&mut self, s: SeekFrom) -> std::io::Result<u64> {
        self.data.seek(s)
    }
}
impl Write for FaultFile {
    fn write(&mut self, b: &[u8]) -> std::io::Result<usize> {
        self.writes += 1;
        if self.writes > self.fail_after {
            return Err(std::io::Error::from_raw_os_error(28));
        }
        self.data.write(&b[..b.len().min(self.limit)])
    }
    fn flush(&mut self) -> std::io::Result<()> {
        panic!("buffered flush after ENOSPC is forbidden")
    }
}
impl shadow::AppendFile for FaultFile {
    fn truncate(&mut self, len: u64) -> std::io::Result<()> {
        self.data.get_mut().truncate(len as usize);
        Ok(())
    }
    fn sync(&mut self) -> std::io::Result<()> {
        if self.fail_sync {
            self.fail_sync = false;
            Err(std::io::Error::from_raw_os_error(28))
        } else {
            Ok(())
        }
    }
}
#[test]
fn native_update_enospc_append_restores_bytes_without_retry() {
    for limit in [0, 3, usize::MAX] {
        let original = b"# foreign owner and access retained\r\n";
        let mut file = FaultFile {
            data: Cursor::new(original.to_vec()),
            limit,
            fail_after: if limit == usize::MAX { 0 } else { 1 },
            writes: 0,
            fail_sync: false,
        };
        assert!(shadow::append_checked(&mut file, original, b"\nPASSWORD=fixture\n").is_err());
        assert_eq!(file.data.get_ref(), original);
        assert_eq!(file.writes, if limit == 3 { 2 } else { 1 });
    }
}
#[test]
fn native_update_short_writes_and_sync_failure_are_recoverable() {
    let original = b"existing\r\n";
    let mut file = FaultFile {
        data: Cursor::new(original.to_vec()),
        limit: 3,
        fail_after: usize::MAX,
        writes: 0,
        fail_sync: false,
    };
    shadow::append_checked(&mut file, original, b"added").unwrap();
    assert_eq!(file.data.get_ref(), b"existing\r\nadded");
    let original = file.data.get_ref().clone();
    file.fail_sync = true;
    assert!(shadow::append_checked(&mut file, &original, b"not-committed").is_err());
    assert_eq!(file.data.get_ref(), &original);
    shadow::append_checked(&mut file, &original, b"retry").unwrap();
    assert!(file.data.get_ref().ends_with(b"retry"));
}
#[test]
fn native_update_intervening_env_edits_survive() {
    let mut file = FaultFile {
        data: Cursor::new(b"intervening".to_vec()),
        limit: 3,
        fail_after: usize::MAX,
        writes: 0,
        fail_sync: false,
    };
    assert!(shadow::append_checked(&mut file, b"original", b"addition").is_err());
    assert_eq!(file.data.get_ref(), b"intervening");
    assert_eq!(file.writes, 0);
}

#[test]
fn native_update_panic_persists_failure_and_clears_stale_codex_result() {
    let temp = Temp::new();
    let path = temp.0.join("result");
    seams::write_atomic(&path, b"0\n").unwrap();
    seams::write_atomic(&path.with_extension("codex"), b"stale approval\n").unwrap();
    let mut update = Update::new(Options {
        result_file: Some(path.clone()),
        as_json: true,
        ..Options::default()
    });
    let caught = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        update.run_with(|_| panic!("synthetic transaction panic"))
    }));
    assert!(caught.is_err());
    assert_eq!(fs::read(&path).unwrap(), b"1\n");
    assert!(!path.with_extension("codex").exists());
    assert!(!update.report.ok);
    let mut retry = Update::new(Options {
        result_file: Some(path.clone()),
        as_json: true,
        ..Options::default()
    });
    assert_eq!(retry.run_with(|_| Ok(())), 0);
    assert_eq!(fs::read(&path).unwrap(), b"0\n");
}

#[test]
fn native_update_result_write_failure_preserves_primary_error_and_retry_needs_no_state_repair() {
    let temp = Temp::new();
    let blocker = temp.0.join("blocked");
    fs::write(&blocker, b"owned fixture").unwrap();
    let path = blocker.join("result");
    let options = Options {
        result_file: Some(path.clone()),
        as_json: true,
        ..Options::default()
    };
    let mut update = Update::new(options.clone());
    assert_eq!(
        update.run_with(|_| Err(UpdateError::usage("synthetic preflight refusal"))),
        2
    );
    assert_eq!(fs::read(&blocker).unwrap(), b"owned fixture");
    assert!(
        update
            .report
            .steps
            .iter()
            .any(|line| line.contains("could not write the update result file"))
    );
    fs::remove_file(&blocker).unwrap();
    fs::create_dir(&blocker).unwrap();
    let mut retry = Update::new(options);
    assert_eq!(retry.run_with(|_| Ok(())), 0);
    assert_eq!(fs::read(path).unwrap(), b"0\n");
}

#[test]
fn native_update_compose_publication_is_atomic_immutable_and_keeps_prior_versions() {
    let temp = Temp::new();
    let root = temp.0.join("compose");
    let files = [("docker-compose.yml".into(), b"verified first".to_vec())]
        .into_iter()
        .collect();
    let first = release::publish_compose(&root, "0.21.0", &files).unwrap();
    assert_eq!(
        fs::read(first.join("docker-compose.yml")).unwrap(),
        b"verified first"
    );
    assert_eq!(
        release::publish_compose(&root, "0.21.0", &files).unwrap(),
        first
    );
    let changed = [(
        "docker-compose.yml".into(),
        b"different release bytes".to_vec(),
    )]
    .into_iter()
    .collect();
    assert!(release::publish_compose(&root, "0.21.0", &changed).is_err());
    assert_eq!(
        fs::read(first.join("docker-compose.yml")).unwrap(),
        b"verified first"
    );
    assert!(
        release::publish_compose(&root, "0.21.1", &changed)
            .unwrap()
            .is_dir()
    );
    assert!(first.is_dir());
    assert_eq!(fs::read_dir(root).unwrap().count(), 2);
}

#[test]
fn maintainer_check_and_interactive_setup_keep_the_selected_target() {
    let mut update = Update::new(Options::default());
    update.o.daemon_url = "http://127.0.0.1:9876".into();
    for (check, flag) in [(true, "--check"), (false, "--yes")] {
        assert_eq!(
            update.maintainer_command(Path::new("native-cli"), "second-project-new-daemon", check),
            [
                "native-cli",
                "maintainer",
                "setup",
                flag,
                "--container",
                "second-project-new-daemon",
                "--port",
                "9876"
            ]
        );
    }
}

#[test]
fn native_update_gate_names_the_check_that_failed() {
    let ok = json!({"status":"ok","version":"0.21.0","schema":58});
    let gate = |health: Option<&Value>, read: bool| {
        release::gate_failure(health, Some("0.21.0"), Some(58), true, false, || read)
    };
    assert_eq!(gate(None, true).unwrap(), "/health did not answer");
    assert_eq!(
        gate(Some(&json!({"status":"starting"})), true).unwrap(),
        "/health reported status \"starting\" instead of \"ok\""
    );
    let mut other = ok.clone();
    other["version"] = json!("0.20.0");
    assert_eq!(
        gate(Some(&other), true).unwrap(),
        "version \"0.20.0\" instead of \"0.21.0\""
    );
    let mut other = ok.clone();
    other["schema"] = json!(57);
    assert_eq!(gate(Some(&other), true).unwrap(), "schema 57 instead of 58");
    other["schema"] = Value::Null;
    assert_eq!(
        gate(Some(&other), true).unwrap(),
        "schema null instead of 58"
    );
    let mut other = ok.clone();
    other["counters_import_required"] = json!(true);
    assert_eq!(
        gate(Some(&other), true).unwrap(),
        "counters_import_required: the daemon has not imported its counters"
    );
    assert_eq!(
        gate(Some(&ok), false).unwrap(),
        "the authenticated bank read was refused"
    );
    assert!(gate(Some(&ok), true).is_none());
    // The bank read is attempted only after every other check passed.
    let mut read = false;
    let _ = release::gate_failure(Some(&other), Some("0.21.0"), Some(58), true, false, || {
        read = true;
        true
    });
    assert!(!read);
    // A plain update skips the counters check; a shadow also checks readiness.
    assert!(
        release::gate_failure(Some(&other), Some("0.21.0"), None, false, false, || true).is_none()
    );
    assert_eq!(
        release::gate_failure(Some(&ok), None, None, true, true, || true).unwrap(),
        "the shadow is not ready read-only (read_only=null, ready=null, db=null)"
    );
}

#[test]
fn native_update_checkout_rollback_to_rust_keeps_its_model_overlay() {
    let base = vec!["-f".to_owned(), "ops/docker-compose.yml".to_owned()];
    let overlay = Path::new("ops/docker-compose.rust-daemon.yml");
    let on_models = json!({"Config":{"Env":["PSEUDOLIFE_DAEMON_MODELS_DIR=/models"]},"Mounts":[]});
    let mounted = json!({"Config":{"Env":[]},"Mounts":[{"Destination":"/models"}]});
    // An earlier checkout Rust daemon read /data/models: its rollback keeps that.
    let legacy = json!({"Config":{"Env":["PSEUDOLIFE_DAEMON_MODELS_DIR=/data/models"]},"Mounts":[{"Destination":"/data"}]});
    assert_eq!(
        engine::checkout_rollback_base(&base, overlay, "python", &on_models),
        base
    );
    assert_eq!(
        engine::checkout_rollback_base(&base, overlay, "rust", &legacy),
        base
    );
    assert_eq!(
        engine::checkout_rollback_base(&base, overlay, "rust", &json!({"Config":{"Labels":null}})),
        base
    );
    assert_eq!(
        engine::checkout_rollback_base(&base, overlay, "rust", &mounted)[2..],
        ["-f", "ops/docker-compose.rust-daemon.yml"]
    );
    let rust = engine::checkout_rollback_base(&base, overlay, "rust", &on_models);
    assert_eq!(rust[..2], base[..]);
    assert_eq!(rust[2..], ["-f", "ops/docker-compose.rust-daemon.yml"]);
    let lines = engine::rollback_commands(
        &rust,
        &[],
        Path::new("rollback.json"),
        Path::new("accepted.json"),
        None,
        None,
    );
    let restore = lines
        .iter()
        .find(|line| line.contains("docker compose"))
        .unwrap();
    assert!(
        restore.find("docker-compose.rust-daemon.yml").unwrap()
            < restore.find("rollback.json").unwrap()
    );
}

/// macOS keeps the temporary folder below a symlink (`/var` ->
/// `/private/var`), and private state refused any redirected ancestor, so a
/// scratch folder made under the raw path failed there (the hand-off's report
/// folder, the Codex marketplace refresh). Built here with a symlinked
/// temporary root on any Unix. Since #1639 a link no other account can
/// change is accepted too; the scratch folder is still made where it leads.
#[cfg(unix)]
#[test]
fn private_scratch_resolves_a_symlinked_temporary_folder() {
    use std::os::unix::fs::PermissionsExt;
    let base = fs::canonicalize(std::env::temp_dir())
        .unwrap()
        .join(format!("pseudolife-scratch-test-{}", uuid::Uuid::new_v4()));
    let real = base.join("private-var");
    fs::create_dir_all(&real).unwrap();
    // Only this user may write the folder holding the link, whatever the
    // umask (#1639: a group-writable one could have the link replaced).
    fs::set_permissions(&base, fs::Permissions::from_mode(0o700)).unwrap();
    let link = base.join("var");
    std::os::unix::fs::symlink(&real, &link).unwrap();
    // A link only this user can change no longer refuses private state (#1639).
    assert!(crate::board::state::private_dir(&link.join("direct")).is_ok());
    let made = seams::private_scratch_in(link.clone(), "pseudolife-test").unwrap();
    assert!(made.starts_with(&real), "{}", made.display());
    assert_eq!(
        fs::metadata(&made).unwrap().permissions().mode() & 0o777,
        0o700
    );
    // The root the shadow role script is written under is resolved too.
    assert_eq!(
        seams::temp_root(),
        fs::canonicalize(std::env::temp_dir()).unwrap()
    );
    // A link that cannot be resolved names the variable to change.
    let broken = base.join("broken");
    std::os::unix::fs::symlink(base.join("missing"), &broken).unwrap();
    let error = seams::private_scratch_in(broken, "pseudolife-test").unwrap_err();
    assert!(error.contains("set TMPDIR to a real folder"), "{error}");
    let _ = fs::remove_dir_all(&base);
}
