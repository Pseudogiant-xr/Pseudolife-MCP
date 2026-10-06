#[path = "../../../../rust/shim/src/credentials/unix_accounts.rs"]
mod unix_accounts;
use users::os::unix::UserExt;
fn main() {
    let mut names = vec![
        "root".to_owned(),
        format!("phase2c-fixture-missing-{}", std::process::id()),
        "fixture\0account".to_owned(),
    ];
    if let Some(name) = users::get_current_username().and_then(|name| name.into_string().ok()) {
        names.push(name);
    }
    let positives = names
        .iter()
        .filter(|name| users::get_user_by_name(name.as_str()).is_some())
        .count();
    assert!(positives > 0, "NSS proof needs a positive account");
    let threads: Vec<_> = (0..4)
        .map(|_| {
            let names = names.clone();
            std::thread::spawn(move || {
                names.iter().all(|name| {
                    let old = users::get_user_by_name(name.as_str())
                        .map(|entry| entry.home_dir().to_path_buf());
                    unix_accounts::home_by_name(name) == old
                })
            })
        })
        .collect();
    assert!(
        threads.into_iter().all(|thread| thread.join().unwrap()),
        "NSS equivalence failed"
    );
    assert!(
        users::get_current_uid() == rustix::process::getuid().as_raw(),
        "real UID differs"
    );
    println!(
        "nss_equivalent=true real_uid_equivalent=true cases={} parallel_threads=4 positive_cases={positives}",
        names.len()
    );
}
