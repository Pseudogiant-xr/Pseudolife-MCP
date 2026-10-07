use super::*;

#[test]
fn release_closes_owner_and_allows_repeated_reacquisition() {
    let path = std::env::temp_dir().join(format!("lease-release-{}.lock", uuid::Uuid::new_v4()));
    let mut original = Lock::new(path.clone());
    let mut contender = Lock::new(path.clone());
    assert!(matches!(original.acquire(), Ok(true)));
    assert!(matches!(contender.acquire(), Ok(false)));
    original.release();
    assert!(original.file.is_none());
    assert!(matches!(contender.acquire(), Ok(true)));
    assert!(matches!(original.acquire(), Ok(false)));
    contender.release();
    assert!(matches!(original.acquire(), Ok(true)));
    original.release();
    original.release();
    assert!(original.file.is_none());
    fs::remove_file(path).unwrap();
}
