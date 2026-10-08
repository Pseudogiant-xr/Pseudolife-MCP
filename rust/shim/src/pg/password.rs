//! Default password-file behavior follows PostgreSQL libpq 18.6 fe-connect.c.
use super::{Dsn, Error};
use std::{
    fmt,
    fs::File,
    io::{self, BufRead, BufReader, Write},
    path::{Path, PathBuf},
};

/// A snapshot can bind fixtures without changing process-global credentials.
#[derive(Clone, Default)]
pub struct PasswordEnvironment {
    pub password: Option<Vec<u8>>,
    pub default_file: Option<PathBuf>,
    pub default_user: Option<String>,
}
impl fmt::Debug for PasswordEnvironment {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("PasswordEnvironment")
            .field("password_present", &self.password.is_some())
            .field("default_file_present", &self.default_file.is_some())
            .finish_non_exhaustive()
    }
}
impl PasswordEnvironment {
    pub fn from_environment() -> Self {
        let (default_user, home) = platform_defaults();
        Self {
            password: environment_password(),
            default_file: home.map(default_file),
            default_user,
        }
    }
}

#[cfg(unix)]
fn default_file(home: PathBuf) -> PathBuf {
    use std::{ffi::OsString, os::unix::ffi::OsStringExt};
    let mut bytes = home.into_os_string().into_vec();
    bytes.truncate(1023);
    bytes.extend_from_slice(b"/.pgpass");
    bytes.truncate(1023);
    PathBuf::from(OsString::from_vec(bytes))
}
#[cfg(windows)]
fn default_file(home: PathBuf) -> PathBuf {
    home.join("postgresql").join("pgpass.conf")
}
#[cfg(unix)]
fn environment_password() -> Option<Vec<u8>> {
    use std::os::unix::ffi::OsStringExt;
    std::env::var_os("PGPASSWORD").map(|value| value.into_vec())
}
#[cfg(windows)]
fn environment_password() -> Option<Vec<u8>> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Globalization::{CP_ACP, WideCharToMultiByte};
    let value = std::env::var_os("PGPASSWORD")?;
    let wide: Vec<_> = value.encode_wide().collect();
    if wide.is_empty() {
        return Some(Vec::new());
    }
    let count = i32::try_from(wide.len()).ok()?;
    // libpq's getenv() consumes the Windows ANSI representation.
    unsafe {
        let size = WideCharToMultiByte(CP_ACP, 0, wide.as_ptr(), count,
            std::ptr::null_mut(), 0, std::ptr::null(), std::ptr::null_mut());
        if size <= 0 {
            return None;
        }
        let mut bytes = vec![0; size as usize];
        let written = WideCharToMultiByte(CP_ACP, 0, wide.as_ptr(), count,
            bytes.as_mut_ptr(), size, std::ptr::null(), std::ptr::null_mut());
        (written == size).then_some(bytes)
    }
}
#[cfg(unix)]
fn platform_defaults() -> (Option<String>, Option<PathBuf>) {
    use std::{
        ffi::{CStr, OsString},
        os::unix::ffi::OsStringExt,
    };
    let mut record = std::mem::MaybeUninit::<libc::passwd>::uninit();
    let mut scratch = [0; 1024];
    let mut result = std::ptr::null_mut();
    // libpq uses the effective UID and a 1024-byte getpwuid_r scratch buffer.
    let found = unsafe {
        libc::getpwuid_r(
            libc::geteuid(), record.as_mut_ptr(), scratch.as_mut_ptr(),
            scratch.len(), &mut result,
        ) == 0 && !result.is_null()
    };
    let (user, home) = if found {
        // The successful call populated both pointers inside the live scratch buffer.
        unsafe {
            let record = record.assume_init();
            let user = CStr::from_ptr(record.pw_name).to_str().ok().map(str::to_owned);
            let home = OsString::from_vec(CStr::from_ptr(record.pw_dir).to_bytes().to_vec());
            (user, Some(PathBuf::from(home)))
        }
    } else {
        (None, None)
    };
    let home = std::env::var_os("HOME")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .or(home);
    (user, home)
}
#[cfg(windows)]
fn platform_defaults() -> (Option<String>, Option<PathBuf>) {
    use std::{ffi::OsString, os::windows::ffi::OsStringExt};
    use windows_sys::Win32::{
        Globalization::{CP_ACP, MultiByteToWideChar},
        System::WindowsProgramming::GetUserNameA,
        UI::Shell::{CSIDL_APPDATA, SHGFP_TYPE_CURRENT, SHGetFolderPathA},
    };
    let mut username = [0; 257];
    let mut count = username.len() as u32;
    let user = unsafe {
        (GetUserNameA(username.as_mut_ptr(), &mut count) != 0)
            .then(|| username.iter().position(|byte| *byte == 0))
            .flatten()
            .and_then(|length| String::from_utf8(username[..length].to_vec()).ok())
    };
    let mut path = [0; 260];
    // The source contract is Shell32's current caller Roaming AppData folder.
    // APPDATA environment redirection does not bind this lookup.
    let home = unsafe {
        if SHGetFolderPathA(std::ptr::null_mut(), CSIDL_APPDATA as i32,
            std::ptr::null_mut(), SHGFP_TYPE_CURRENT as u32, path.as_mut_ptr()) != 0 {
            None
        } else {
            path.iter().position(|byte| *byte == 0).and_then(|length| {
                let mut wide = [0; 260];
                let written = MultiByteToWideChar(CP_ACP, 0, path.as_ptr(), length as i32,
                    wide.as_mut_ptr(), wide.len() as i32);
                (written > 0).then(|| PathBuf::from(OsString::from_wide(&wide[..written as usize])))
            })
        }
    };
    (user, home)
}

pub(super) fn configure(
    dsn: &Dsn,
    environment: &PasswordEnvironment,
) -> Result<tokio_postgres::Config, Error> {
    let mut config = dsn.config().clone();
    if config.get_user().is_none_or(str::is_empty) {
        config.user(
            environment.default_user.as_deref()
                .filter(|user| !user.is_empty()).ok_or(Error::Connection)?,
        );
    }
    if config.get_dbname().is_none_or(str::is_empty) {
        config.dbname(config.get_user().ok_or(Error::Connection)?.to_owned());
    }
    if config.get_password().is_some() {
        return Ok(config);
    }
    // Presence of an empty DSN value suppresses the environment, not the file.
    let password = if dsn.password_present {
        None
    } else {
        environment.password.as_deref()
    };
    if let Some(password) = password.filter(|password| !password.is_empty()) {
        config.password(password);
    } else if let Some(path) = &environment.default_file {
        let [tokio_postgres::config::Host::Tcp(host)] = config.get_hosts() else {
            return Err(Error::InvalidOption("host"));
        };
        let tokens = [
            host.as_bytes(), dsn.password_port.as_bytes(),
            config.get_dbname().ok_or(Error::Connection)?.as_bytes(),
            config.get_user().ok_or(Error::Connection)?.as_bytes(),
        ];
        if let Some(password) = read_password(path, tokens, &mut io::stderr().lock())
            .filter(|password| !password.is_empty())
        {
            config.password(password);
        }
    }
    Ok(config)
}

fn read_password(
    path: &Path,
    tokens: [&[u8]; 4],
    warnings: &mut impl Write,
) -> Option<Vec<u8>> {
    let file = File::open(path).ok()?;
    #[cfg(unix)]
    {
        use std::os::unix::{ffi::OsStrExt, fs::PermissionsExt};
        let metadata = file.metadata().ok()?;
        let warning = if !metadata.is_file() {
            Some(b" is not a plain file\n".as_slice())
        } else if metadata.permissions().mode() & 0o077 != 0 {
            Some(b" has group or world access; permissions should be u=rw (0600) or less\n".as_slice())
        } else {
            None
        };
        if let Some(warning) = warning {
            let _ = warnings.write_all(b"WARNING: password file \"");
            let _ = warnings.write_all(path.as_os_str().as_bytes());
            let _ = warnings.write_all(b"\"");
            let _ = warnings.write_all(warning);
            return None;
        }
    }
    #[cfg(windows)]
    let _ = warnings;
    #[cfg(unix)]
    let reader = BufReader::new(file);
    #[cfg(windows)]
    let reader = BufReader::new(TextReader {
        reader: BufReader::new(file), pending: None, eof: false,
    });
    parse_password(reader, tokens).ok().flatten()
}

// fopen(..., "r") in the Windows CRT folds CRLF and treats control-Z as EOF.
#[cfg(windows)]
struct TextReader<R> {
    reader: R,
    pending: Option<u8>,
    eof: bool,
}
#[cfg(windows)]
impl<R: io::Read> io::Read for TextReader<R> {
    fn read(&mut self, output: &mut [u8]) -> io::Result<usize> {
        let mut written = 0;
        while written < output.len() && !self.eof {
            let mut byte = [0];
            if let Some(pending) = self.pending.take() {
                byte[0] = pending;
            } else if self.reader.read(&mut byte)? == 0 {
                self.eof = true;
                break;
            }
            if byte[0] == 0x1a {
                self.eof = true;
                break;
            }
            if byte[0] == b'\r' {
                let mut next = [0];
                if self.reader.read(&mut next)? != 0 {
                    if next[0] == b'\n' {
                        byte[0] = b'\n';
                    } else {
                        self.pending = Some(next[0]);
                    }
                }
            }
            output[written] = byte[0];
            written += 1;
        }
        Ok(written)
    }
}

fn match_field<'a>(input: &'a [u8], token: &[u8]) -> Option<&'a [u8]> {
    if input.starts_with(b"*:") {
        return Some(&input[2..]);
    }
    let mut offset = 0;
    for &byte in token {
        if input.get(offset) == Some(&b'\\') {
            offset += 1;
        }
        if input.get(offset) != Some(&byte) {
            return None;
        }
        offset += 1;
    }
    (input.get(offset) == Some(&b':')).then(|| &input[offset + 1..])
}
fn match_line(mut line: &[u8], tokens: [&[u8]; 4]) -> Option<Vec<u8>> {
    if line.first() == Some(&b'#') {
        return None;
    }
    while matches!(line.last(), Some(b'\r' | b'\n')) {
        line = &line[..line.len() - 1];
    }
    if line.is_empty() {
        return None;
    }
    for token in tokens {
        line = match_field(line, token)?;
    }
    let mut password = Vec::new();
    let mut bytes = line.iter().copied();
    while let Some(byte) = bytes.next() {
        if byte == b':' {
            break;
        }
        password.push(if byte == b'\\' {
            bytes.next().unwrap_or(byte)
        } else {
            byte
        });
    }
    Some(password)
}

fn parse_password(
    mut reader: impl BufRead,
    tokens: [&[u8]; 4],
) -> io::Result<Option<Vec<u8>>> {
    let mut line = Vec::new();
    let mut capacity = 256;
    loop {
        // Mirror libpq's extensible fgets buffer, including C-string truncation.
        while capacity - line.len() <= 128 {
            capacity *= 2;
        }
        let mut chunk = Vec::new();
        let mut eof = false;
        let room = capacity - line.len() - 1;
        while chunk.len() < room {
            let available = reader.fill_buf()?;
            if available.is_empty() {
                eof = true;
                break;
            }
            let limit = available.len().min(room - chunk.len());
            let length = available[..limit].iter()
                .position(|byte| *byte == b'\n')
                .map_or(limit, |offset| offset + 1);
            let newline = available[length - 1] == b'\n';
            chunk.extend_from_slice(&available[..length]);
            reader.consume(length);
            if newline {
                break;
            }
        }
        if chunk.is_empty() {
            break;
        }
        let length = chunk.iter().position(|byte| *byte == 0).unwrap_or(chunk.len());
        line.extend_from_slice(&chunk[..length]);
        if line.last() != Some(&b'\n') && !eof {
            continue;
        }
        if let Some(password) = match_line(&line, tokens) {
            return Ok(Some(password));
        }
        line.clear();
    }
    Ok(None)
}

#[cfg(test)]
mod tests {
    use super::*;
    const TOKENS: [&[u8]; 4] = [b"localhost", b"5432", b"fixture_db", b"fixture_user"];

    struct Fixture(PathBuf);
    impl Fixture {
        fn new(contents: &[u8]) -> Self {
            let root = std::env::temp_dir().join(format!("pseudolife-pgpass-{}", uuid::Uuid::new_v4()));
            std::fs::create_dir(&root).unwrap();
            let fixture = Self(root);
            std::fs::write(fixture.path(), contents).unwrap();
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                std::fs::set_permissions(fixture.path(), std::fs::Permissions::from_mode(0o600)).unwrap();
            }
            fixture
        }
        fn path(&self) -> PathBuf { self.0.join("password-file") }
        fn environment(&self, password: Option<&[u8]>) -> PasswordEnvironment {
            PasswordEnvironment {
                password: password.map(<[u8]>::to_vec),
                default_file: Some(self.path()),
                default_user: Some("fixture_user".into()),
            }
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }
    fn resolve(text: &str, environment: &PasswordEnvironment) -> tokio_postgres::Config {
        configure(&Dsn::parse(text).unwrap(), environment).unwrap()
    }

    #[test]
    fn password_precedence_distinguishes_empty_from_absent() {
        let fixture = Fixture::new(b"localhost:5432:fixture_db:fixture_user:file-value\n");
        let base = "host=localhost user=fixture_user dbname=fixture_db";
        let environment = fixture.environment(Some(b"env-value"));
        assert_eq!(resolve(base, &environment).get_password(), Some(b"env-value".as_slice()));
        assert_eq!(resolve(&format!("{base} password=dsn-value"), &environment).get_password(), Some(b"dsn-value".as_slice()));
        assert_eq!(resolve(&format!("{base} password=''"), &environment).get_password(), Some(b"file-value".as_slice()));
        assert_eq!(resolve(base, &fixture.environment(Some(b""))).get_password(), Some(b"file-value".as_slice()));
        assert_eq!(resolve(base, &fixture.environment(None)).get_password(), Some(b"file-value".as_slice()));
        assert_eq!(resolve("postgres://fixture_user:@localhost/fixture_db", &environment).get_password(), Some(b"file-value".as_slice()));
        assert_eq!(resolve("postgres://fixture_user:authority@localhost/fixture_db?password=", &environment).get_password(), Some(b"file-value".as_slice()));
        std::fs::remove_file(fixture.path()).unwrap();
        assert_eq!(resolve(&format!("{base} password=''"), &environment).get_password(), None);
        assert_eq!(resolve(base, &fixture.environment(Some(b""))).get_password(), None);
    }

    #[test]
    fn user_and_database_defaults_share_the_effective_user() {
        let fixture = Fixture::new(b"localhost:5432:fixture_user:fixture_user:default-value\n");
        for text in ["host=localhost", "host=localhost user='' dbname=''"] {
            let config = resolve(text, &fixture.environment(None));
            assert_eq!(config.get_user(), Some("fixture_user"));
            assert_eq!(config.get_dbname(), Some("fixture_user"));
            assert_eq!(config.get_password(), Some(b"default-value".as_slice()));
        }
    }

    #[test]
    fn port_matching_keeps_dsn_text_and_socket_port_keeps_its_number() {
        let fixture = Fixture::new(b"localhost:5432:*:*:canonical\nlocalhost:05432:*:*:textual\n");
        for text in ["host=localhost port=05432", "postgres://localhost:05432", "postgres://localhost?port=05432"] {
            let config = resolve(text, &fixture.environment(None));
            assert_eq!(config.get_ports(), &[5432]);
            assert_eq!(config.get_password(), Some(b"textual".as_slice()));
        }
    }

    #[test]
    fn matching_retains_first_entry_comments_escapes_and_empty_password() {
        let contents = b"#localhost:5432:fixture_db:fixture_user:comment\n*:5432:*:*:first\\:part\\\\tail:ignored\nlocalhost:5432:fixture_db:fixture_user:second\n";
        assert_eq!(parse_password(&contents[..], TOKENS).unwrap(), Some(b"first:part\\tail".to_vec()));
        let empty_first = b"*:*:*:*:\n*:*:*:*:later\n";
        assert_eq!(parse_password(&empty_first[..], TOKENS).unwrap(), Some(Vec::new()));
        assert_eq!(match_line(b"\\*:5432:db\\:part:user\\\\part:escaped\\\r\n", [b"*", b"5432", b"db:part", b"user\\part"]), Some(b"escaped\\".to_vec()));
        assert_eq!(match_line(b" localhost:5432:fixture_db:fixture_user:value", TOKENS), None);
        assert_eq!(match_line(b"localhost:5432:fixture_db:fixture_user: trailing space \r\n", TOKENS), Some(b" trailing space ".to_vec()));
    }

    #[test]
    fn extensible_lines_and_c_string_chunks_follow_libpq() {
        let mut contents = b"#".to_vec();
        contents.extend(vec![b'x'; 8192]);
        contents.extend_from_slice(b"\nlocalhost:5432:fixture_db:fixture_user:final");
        assert_eq!(parse_password(&contents[..], TOKENS).unwrap(), Some(b"final".to_vec()));
        let contents = b"localhost:5432:fixture_db:fixture_user:prefix\0discarded\ncontinued\n";
        assert_eq!(parse_password(&contents[..], TOKENS).unwrap(), Some(b"prefixcontinued".to_vec()));
    }

    #[cfg(unix)]
    #[test]
    fn descriptor_permissions_ignore_insecure_file_and_preserve_warning_bytes() {
        use std::os::unix::fs::{PermissionsExt, symlink};
        let fixture = Fixture::new(b"*:*:*:*:value\n");
        for mode in [0o600, 0o400, 0o700] {
            std::fs::set_permissions(fixture.path(), std::fs::Permissions::from_mode(mode)).unwrap();
            let mut warnings = Vec::new();
            assert_eq!(read_password(&fixture.path(), TOKENS, &mut warnings), Some(b"value".to_vec()));
            assert!(warnings.is_empty());
        }
        std::fs::set_permissions(fixture.path(), std::fs::Permissions::from_mode(0o640)).unwrap();
        let mut warnings = Vec::new();
        assert_eq!(read_password(&fixture.path(), TOKENS, &mut warnings), None);
        let expected = format!("WARNING: password file \"{}\" has group or world access; permissions should be u=rw (0600) or less\n", fixture.path().display());
        assert_eq!(warnings, expected.as_bytes());
        std::fs::set_permissions(fixture.path(), std::fs::Permissions::from_mode(0o600)).unwrap();
        let link = fixture.0.join("link");
        symlink(fixture.path(), &link).unwrap();
        assert_eq!(read_password(&link, TOKENS, &mut Vec::new()), Some(b"value".to_vec()));
        std::fs::remove_file(fixture.path()).unwrap();
        let mut warnings = Vec::new();
        assert_eq!(read_password(&fixture.path(), TOKENS, &mut warnings), None);
        assert!(warnings.is_empty());
        assert_eq!(read_password(&fixture.0, TOKENS, &mut warnings), None);
        assert_eq!(warnings, format!("WARNING: password file \"{}\" is not a plain file\n", fixture.0.display()).as_bytes());
    }

    #[cfg(windows)]
    #[test]
    fn default_file_uses_the_supplied_shell_folder_and_no_permission_warning() {
        assert_eq!(default_file(PathBuf::from(r"C:\fixture\Roaming")), PathBuf::from(r"C:\fixture\Roaming\postgresql\pgpass.conf"));
        let fixture = Fixture::new(b"*:*:*:*:value\r\n*:*:*:*:later\n");
        let mut warnings = Vec::new();
        assert_eq!(read_password(&fixture.path(), TOKENS, &mut warnings), Some(b"value".to_vec()));
        assert!(warnings.is_empty());
        std::fs::write(fixture.path(), b"\x1a*:*:*:*:hidden\n").unwrap();
        assert_eq!(read_password(&fixture.path(), TOKENS, &mut warnings), None);
    }

    #[test]
    fn debug_never_contains_password_or_fixture_path() {
        let fixture = Fixture::new(b"*:*:*:*:value\n");
        let debug = format!("{:?}", fixture.environment(Some(b"secret-fixture")));
        assert!(!debug.contains("secret-fixture"));
        assert!(!debug.contains(&fixture.path().to_string_lossy().to_string()));
    }

    #[test]
    fn custom_passfile_controls_keep_named_refusals() {
        assert_eq!(
            Dsn::parse("host=localhost passfile=fixture").unwrap_err(),
            Error::UnsupportedOption("passfile".into()),
        );
        assert_eq!(
            Dsn::parse("postgres://localhost?passfile=fixture").unwrap_err(),
            Error::UnsupportedOption("passfile".into()),
        );
        let environment = super::super::TlsEnvironment {
            ambient_controls: vec!["PGPASSFILE".into()],
            ..Default::default()
        };
        assert_eq!(environment.validate(), Err(Error::AmbientControl("PGPASSFILE".into())));
    }
}
