use super::*;

struct Home(PathBuf);
impl Home {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!("wait-mail-record-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&path).unwrap();
        Self(path)
    }
}
impl Drop for Home {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).unwrap();
    }
}

#[test]
fn watermark_grammar_is_unsigned_ascii_with_numeric_zero_padding_and_no_digit_limit() {
    for raw in [b"0".as_slice(), b"12", b"123456789012345678901234567890"] {
        assert_eq!(Integer::parse(raw).unwrap().text().as_bytes(), raw);
    }
    assert!(Integer::parse("1".repeat(4301).as_bytes()).is_some());
    for raw in [
        b"".as_slice(),
        b"+12",
        b"-12",
        b"1_2",
        b" 12",
        b"12\t",
        b"12\n",
        b"\xc2\xa012",
        "١٢".as_bytes(),
    ] {
        assert!(Integer::parse(raw).is_none(), "{raw:?}");
    }
    assert_eq!(Integer::parse(b"012").unwrap().text(), "12");
    assert!(
        Integer::parse(b"123456789012345678901234567890").unwrap()
            > Integer::parse(b"9999999999999999999").unwrap()
    );
}

#[test]
fn digest_requires_lf_framing_and_seen_strips_writer_whitespace() {
    let home = Home::new();
    let digest = home.0.join("record.txt");
    let seen = digest.with_extension("seen");
    for raw in [b"12\npeer\n".as_slice(), b"0\n", b"012\npeer\n"] {
        fs::write(&digest, raw).unwrap();
        assert!(read_digest(&digest).is_ok());
    }
    for raw in [
        b"12".as_slice(),
        b"12\r\npeer\n",
        b"12\npeer",
        b"+12\npeer\n",
        b"1_2\npeer\n",
        b" 12\npeer\n",
    ] {
        fs::write(&digest, raw).unwrap();
        assert_eq!(
            read_digest(&digest).unwrap_err().kind(),
            io::ErrorKind::InvalidData
        );
    }
    assert_eq!(read_seen(&seen).unwrap(), Integer::zero());
    fs::write(&seen, b"12\n").unwrap();
    assert_eq!(read_seen(&seen).unwrap().text(), "12");
    for raw in [b"12".as_slice(), b"12\r\n", b"12\n\n", b" 12\n"] {
        fs::write(&seen, raw).unwrap();
        assert_eq!(read_seen(&seen).unwrap().text(), "12");
    }
    fs::write(&seen, b"\n").unwrap();
    assert_eq!(read_seen(&seen).unwrap(), Integer::zero());
    for raw in [b"+12\n".as_slice(), b"1_2\n"] {
        fs::write(&seen, raw).unwrap();
        assert_eq!(
            read_seen(&seen).unwrap_err().kind(),
            io::ErrorKind::InvalidData
        );
    }
}

#[test]
fn ring_requires_two_lf_records_and_has_no_interpreter_width_limit() {
    let home = Home::new();
    let path = home.0.join("record.ring");
    fs::write(&path, b"1234567890123\nrung anyone\n").unwrap();
    assert_eq!(read_ring(&path).unwrap().unwrap().0.text(), "1234567890123");
    fs::write(&path, b"00042\nrung anyone\n").unwrap();
    assert_eq!(read_ring(&path).unwrap().unwrap().0.text(), "42");
    for raw in [
        b"12\nrung anyone".as_slice(),
        b"12\r\nrung anyone\n",
        b"12\nrung anyone\r\n",
        b"12\nrung anyone\nextra\n",
        b" 1 2 \nrung anyone\n",
        b"+12\nrung anyone\n",
    ] {
        fs::write(&path, raw).unwrap();
        assert_eq!(
            read_ring(&path).unwrap_err().kind(),
            io::ErrorKind::InvalidData
        );
    }
    fs::write(&path, b"12\nplain anyone\n").unwrap();
    assert!(read_ring(&path).unwrap().is_none());
}

#[test]
fn corrupt_seen_is_not_overwritten_or_treated_as_zero() {
    let home = Home::new();
    let path = home.0.join("record.seen");
    fs::write(&path, b"+12\n").unwrap();
    assert_eq!(
        mark_seen(&path, &Integer::parse(b"13").unwrap())
            .unwrap_err()
            .error
            .kind(),
        io::ErrorKind::InvalidData
    );
    assert_eq!(fs::read(&path).unwrap(), b"+12\n");
    assert_eq!(fs::read_dir(&home.0).unwrap().count(), 1);
}

#[test]
fn temporary_exhaustion_is_bounded_and_preserves_colliding_files() {
    let home = Home::new();
    let path = home.0.join("record.seen");
    let collision = home.0.join(".tmp-occupied.seen");
    fs::write(&collision, b"keep").unwrap();
    let mut attempts = 0;
    let error = temporary_with(&path, false, || {
        attempts += 1;
        "occupied".into()
    })
    .unwrap_err();
    assert_eq!(attempts, 16);
    assert_eq!(
        error_text(&error, None, false),
        "temporary file creation exhausted after 16 collisions"
    );
    assert_eq!(fs::read(collision).unwrap(), b"keep");
    assert!(!path.exists());
}

#[test]
fn output_errors_are_direct_without_shutdown_retry() {
    struct Failure {
        writes: usize,
        flushes: usize,
        fail_write: bool,
    }
    impl Write for Failure {
        fn write(&mut self, raw: &[u8]) -> io::Result<usize> {
            self.writes += 1;
            if self.fail_write {
                Err(io::Error::other("write failed"))
            } else {
                Ok(raw.len())
            }
        }
        fn flush(&mut self) -> io::Result<()> {
            self.flushes += 1;
            Err(io::Error::other("flush failed"))
        }
    }
    for fail_write in [false, true] {
        let mut output = Failure {
            writes: 0,
            flushes: 0,
            fail_write,
        };
        assert!(write_output(&mut output, b"peer\n").is_err());
        assert_eq!(output.writes, 1);
        assert_eq!(output.flushes, usize::from(!fail_write));
    }
}
