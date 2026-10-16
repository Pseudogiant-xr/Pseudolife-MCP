mod board_fixture;
use board_fixture::Home;
use pseudolife_stdio::board::claims::{prepare, prepare_arguments};
use serde_json::json;

fn git(root: &std::path::Path, args: &[&str]) {
    let mut command = std::process::Command::new("git");
    command.arg("-C").arg(root).args(args);
    for (key, _) in std::env::vars_os() {
        if key.to_string_lossy().to_uppercase().starts_with("GIT_") {
            command.env_remove(key);
        }
    }
    assert!(command.output().unwrap().status.success());
}

#[tokio::test]
async fn board_repository_linked_worktrees_share_claim_identity_and_consume_local_inputs() {
    let home = Home::new();
    let root = home.0.join("main");
    let linked = home.0.join("linked");
    std::fs::create_dir(&root).unwrap();
    git(&root, &["init", "-q"]);
    git(
        &root,
        &[
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "fixture",
        ],
    );
    git(
        &root,
        &[
            "worktree",
            "add",
            "-q",
            "--detach",
            linked.to_str().unwrap(),
        ],
    );
    let (first, path) = prepare(root.to_str().unwrap(), "src/missing.rs")
        .await
        .unwrap();
    let (second, other) = prepare(linked.to_str().unwrap(), "src/missing.rs")
        .await
        .unwrap();
    assert_eq!(first, second);
    assert_eq!(path, other);
    let arguments = prepare_arguments(
        "memory_agents",
        json!({"action":"claim","worktree":linked,"path":"src/missing.rs","task":"fixture"})
            .as_object()
            .unwrap()
            .clone(),
    )
    .await
    .unwrap();
    assert!(!arguments.contains_key("worktree"));
    assert_eq!(arguments["repository_id"], first);
    assert_eq!(arguments["task"], "fixture");
    assert_eq!(
        prepare(root.to_str().unwrap(), ".git/config")
            .await
            .unwrap_err(),
        "invalid_claim_path"
    );
    std::fs::create_dir(root.join("directory")).unwrap();
    assert_eq!(
        prepare(root.to_str().unwrap(), "directory")
            .await
            .unwrap_err(),
        "invalid_claim_path"
    );
}

#[tokio::test]
async fn board_repository_common_ignorecase_policy_refuses_casefold_alias_ambiguity() {
    let home = Home::new();
    let root = &home.0;
    git(root, &["init", "-q"]);
    git(root, &["config", "core.ignorecase", "true"]);
    std::fs::write(root.join("Straße.txt"), "fixture").unwrap();
    let (_, path) = prepare(root.to_str().unwrap(), "STRASSE.txt")
        .await
        .unwrap();
    assert_eq!(path, "strasse.txt");
    std::fs::write(root.join("STRASSE.txt"), "fixture").unwrap();
    assert_eq!(
        prepare(root.to_str().unwrap(), "strasse.txt")
            .await
            .unwrap_err(),
        "invalid_claim_path"
    );
}

#[cfg(unix)]
#[tokio::test]
async fn board_repository_refuses_symlink_even_to_an_inside_file() {
    let home = Home::new();
    git(&home.0, &["init", "-q"]);
    std::fs::write(home.0.join("real"), "fixture").unwrap();
    std::os::unix::fs::symlink("real", home.0.join("alias")).unwrap();
    assert_eq!(
        prepare(home.0.to_str().unwrap(), "alias")
            .await
            .unwrap_err(),
        "invalid_claim_path"
    );
}
