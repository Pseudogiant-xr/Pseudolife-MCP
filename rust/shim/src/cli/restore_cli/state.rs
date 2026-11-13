//! `docker_backup_restore.validate_state`: the state archive must be a
//! complete gzip file whose tar members cannot escape the state volume.
//!
//! The members are read as CPython 3.11's `tarfile` reads them (`frombuf`,
//! `_proc_builtin`, `TarFile.next`): a bad or short header past the first
//! one ends the archive quietly, a member's data must be present to the end
//! of its last block, non-regular supported types skip no data, and names are
//! the raw header bytes (`surrogateescape` keeps them one-to-one). GNU long
//! names and links (`L`, `K`, which GNU tar writes for any path over 100
//! bytes) patch the header after them as `_proc_gnulong` does. Members this
//! leaf does not read (PAX, sparse files, a negative size), long-name chains
//! and link graphs deep enough to near Python's recursion limit are `Unsure`.
//! A deflate error is a refusal, as the oracle catches `zlib.error`.
use std::{collections::HashMap, path::Path};

use super::{
    Verdict,
    gzip::{self, Stop},
};

const BLOCK: u128 = 512;

/// One member as `tarfile` reports it: normalized `type`, name, linkname.
#[derive(Debug, Clone)]
struct Member {
    kind: u8,
    name: Vec<u8>,
    linkname: Vec<u8>,
}

impl Member {
    fn is_symlink(&self) -> bool {
        self.kind == b'2'
    }
    fn is_hardlink(&self) -> bool {
        self.kind == b'1'
    }
    /// `isdev() or isfifo()`: character, block or FIFO.
    fn is_special(&self) -> bool {
        matches!(self.kind, b'3' | b'4' | b'6')
    }
}

/// How the member list ended (`TarFile.next` raising or returning None).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum End {
    /// Still reading.
    Open,
    /// `next()` returned None.
    Done,
    /// `ReadError`: a refusal.
    Error,
    /// A member this leaf does not read.
    Unsure,
}

/// The tar reader, fed the decompressed stream in order.
struct Reader {
    /// Bytes of the stream seen so far.
    seen: u128,
    /// `TarFile.offset`: where the next header is read.
    next: u128,
    /// Whether `next` was reached by skipping data (`offset != tell()`),
    /// which makes `next()` check that the byte before it exists.
    skipped: bool,
    header: Vec<u8>,
    members: Vec<Member>,
    end: End,
    /// GNU long records read so far for the header after them, outermost
    /// first: the record's kind and its data up to the first NUL.
    pending: Vec<(u8, Vec<u8>)>,
    /// The current long record: data bytes still to read, its kind, and
    /// whether its NUL was seen (later bytes are read but not kept).
    long: Option<Long>,
}

struct Long {
    kind: u8,
    left: u128,
    data: Vec<u8>,
    ended: bool,
}

/// GNU long records before one header; CPython recurses once per record.
const MAX_LONG_CHAIN: usize = 32;

/// `nts`: up to the first NUL.
fn nts(field: &[u8]) -> &[u8] {
    field
        .iter()
        .position(|byte| *byte == 0)
        .map_or(field, |at| &field[..at])
}

/// `nti`: `Err` is `InvalidHeaderError`.
fn nti(field: &[u8]) -> Result<i128, ()> {
    if matches!(field[0], 0o200 | 0o377) {
        let mut number: i128 = 0;
        for byte in &field[1..] {
            number = (number << 8) + i128::from(*byte);
        }
        if field[0] == 0o377 {
            number -= 1i128 << (8 * (field.len() - 1));
        }
        return Ok(number);
    }
    let text = nts(field);
    if !text.is_ascii() {
        return Err(());
    }
    octal(text)
}

/// `int(text.strip() or "0", 8)` for ASCII text.
fn octal(text: &[u8]) -> Result<i128, ()> {
    let space = |byte: &u8| {
        matches!(
            byte,
            b'\t' | b'\n' | 0x0b | 0x0c | b'\r' | 0x1c..=0x1f | b' '
        )
    };
    let Some(start) = text.iter().position(|byte| !space(byte)) else {
        return Ok(0);
    };
    let end = text.iter().rposition(|byte| !space(byte)).unwrap() + 1;
    let mut digits = &text[start..end];
    let negative = digits[0] == b'-';
    if matches!(digits[0], b'+' | b'-') {
        digits = &digits[1..];
    }
    // int() accepts the base's own prefix, and an underscore after it.
    let prefixed = digits.len() >= 2 && digits[0] == b'0' && matches!(digits[1], b'o' | b'O');
    if prefixed {
        digits = &digits[2..];
        if digits.first() == Some(&b'_') {
            digits = &digits[1..];
        }
    }
    let octal_digit = |byte: u8| (b'0'..=b'7').contains(&byte);
    if digits.is_empty() || !octal_digit(digits[0]) || !octal_digit(digits[digits.len() - 1]) {
        return Err(());
    }
    let mut number: i128 = 0;
    for (index, byte) in digits.iter().enumerate() {
        if octal_digit(*byte) {
            number = number.checked_mul(8).ok_or(())? + i128::from(byte - b'0');
        } else if *byte == b'_' && octal_digit(digits[index + 1]) {
        } else {
            return Err(());
        }
    }
    Ok(if negative { -number } else { number })
}

/// `calc_chksums`: unsigned and signed sums, the checksum field as spaces.
fn checksums(header: &[u8]) -> (i128, i128) {
    let bytes = header[..148].iter().chain(&header[156..]);
    let unsigned: i128 = 256 + bytes.clone().map(|b| i128::from(*b)).sum::<i128>();
    let signed: i128 = 256 + bytes.map(|b| i128::from(*b as i8)).sum::<i128>();
    (unsigned, signed)
}

/// `TarInfo._block`.
fn round_up(count: u128) -> u128 {
    count.div_ceil(BLOCK) * BLOCK
}

enum Header {
    Member(Member, u128),
    /// A GNU long name (`L`) or link (`K`) record and its data length
    /// (`_block(size)`), before the V7, prefix and directory rules.
    Long(u8, u128),
    /// `InvalidHeaderError`.
    Invalid,
    Unsure,
}

/// `frombuf` then `_proc_member` for one 512-byte header at `offset`;
/// the member and the next header's offset.
fn parse_header(header: &[u8], offset: u128) -> Header {
    let Ok(stored) = nti(&header[148..156]) else {
        return Header::Invalid;
    };
    let (unsigned, signed) = checksums(header);
    if stored != unsigned && stored != signed {
        return Header::Invalid;
    }
    let mut size = 0;
    for (at, range) in [
        (100, 108),
        (108, 116),
        (116, 124),
        (124, 136),
        (136, 148),
        (329, 337),
        (337, 345),
    ]
    .into_iter()
    .enumerate()
    {
        match nti(&header[range.0..range.1]) {
            Ok(value) if at == 3 => size = value,
            Ok(_) => {}
            Err(()) => return Header::Invalid,
        }
    }
    let mut kind = header[156];
    let mut name = nts(&header[..100]).to_vec();
    let linkname = nts(&header[157..257]).to_vec();
    let prefix = nts(&header[345..500]);
    // PAX headers and sparse members are not read here.
    if matches!(kind, b'x' | b'g' | b'X' | b'S') {
        return Header::Unsure;
    }
    if matches!(kind, b'L' | b'K') {
        // A negative size reads to the end of the stream (`read(-n)`).
        let Ok(size) = u128::try_from(size) else {
            return Header::Unsure;
        };
        return Header::Long(kind, round_up(size));
    }
    if kind == 0 && name.ends_with(b"/") {
        kind = b'5';
    }
    let strip = |name: &mut Vec<u8>| {
        while name.last() == Some(&b'/') {
            name.pop();
        }
    };
    if kind == b'5' {
        strip(&mut name);
    }
    if !prefix.is_empty() {
        let mut joined = prefix.to_vec();
        joined.push(b'/');
        joined.extend_from_slice(&name);
        name = joined;
    }
    if kind == b'5' {
        strip(&mut name);
    }
    let data = offset + BLOCK;
    // Regular files and unknown types skip their data blocks; the other
    // supported types skip none, whatever their size field says.
    let skips = !matches!(kind, b'1' | b'2' | b'3' | b'4' | b'5' | b'6');
    let next = if skips {
        let Ok(size) = u128::try_from(size) else {
            return Header::Unsure;
        };
        data + round_up(size)
    } else {
        data
    };
    Header::Member(
        Member {
            kind,
            name,
            linkname,
        },
        next,
    )
}

impl Reader {
    fn new() -> Self {
        Self {
            seen: 0,
            next: 0,
            skipped: false,
            header: Vec::with_capacity(512),
            members: Vec::new(),
            end: End::Open,
            pending: Vec::new(),
            long: None,
        }
    }

    /// The header at `self.next` is complete: decide it.
    fn take_header(&mut self) {
        let header = std::mem::take(&mut self.header);
        // After a long record any header error is `SubsequentHeaderError`,
        // a `ReadError` wherever it occurs.
        let subsequent = !self.pending.is_empty();
        if header.iter().all(|byte| *byte == 0) {
            // EOFHeaderError: the end, even at offset 0.
            self.end = if subsequent { End::Error } else { End::Done };
            return;
        }
        match parse_header(&header, self.next) {
            Header::Member(mut member, next) => {
                // `_proc_gnulong` patches on the way out of its recursion:
                // the record nearest the header first, each followed by
                // `removesuffix("/")` on a directory's name.
                for (kind, data) in self.pending.drain(..).rev() {
                    if kind == b'L' {
                        member.name = data;
                    } else {
                        member.linkname = data;
                    }
                    if member.kind == b'5' && member.name.last() == Some(&b'/') {
                        member.name.pop();
                    }
                }
                self.members.push(member);
                self.skipped = next != self.next + BLOCK;
                self.next = next;
            }
            Header::Long(kind, length) => {
                if self.pending.len() >= MAX_LONG_CHAIN {
                    self.end = End::Unsure;
                    return;
                }
                // The record's data is read in sequence (`fileobj.read`),
                // and the header it patches follows it directly.
                self.next += BLOCK;
                self.skipped = false;
                self.long = Some(Long {
                    kind,
                    left: length,
                    data: Vec::new(),
                    ended: false,
                });
                if length == 0 {
                    self.end_long();
                }
            }
            Header::Invalid if subsequent => self.end = End::Error,
            Header::Invalid => {
                self.end = if self.next == 0 {
                    End::Error
                } else {
                    End::Done
                };
            }
            Header::Unsure => self.end = End::Unsure,
        }
    }

    /// The long record's data is read: it waits for the next header.
    fn end_long(&mut self) {
        let long = self.long.take().expect("a long record is being read");
        self.pending.push((long.kind, long.data));
    }

    fn feed(&mut self, mut bytes: &[u8]) {
        while self.end == End::Open && !bytes.is_empty() {
            if let Some(long) = &mut self.long {
                let take = long.left.min(bytes.len() as u128) as usize;
                if !long.ended {
                    let part = &bytes[..take];
                    match part.iter().position(|byte| *byte == 0) {
                        Some(at) => {
                            long.data.extend_from_slice(&part[..at]);
                            long.ended = true;
                        }
                        None => long.data.extend_from_slice(part),
                    }
                }
                long.left -= take as u128;
                self.seen += take as u128;
                self.next = self.seen;
                bytes = &bytes[take..];
                if long.left == 0 {
                    self.end_long();
                }
                continue;
            }
            let start = self.seen;
            if start < self.next {
                // Data being skipped.
                let skip = (self.next - start).min(bytes.len() as u128) as usize;
                self.seen += skip as u128;
                bytes = &bytes[skip..];
                continue;
            }
            let want = 512 - self.header.len();
            let take = want.min(bytes.len());
            self.header.extend_from_slice(&bytes[..take]);
            self.seen += take as u128;
            bytes = &bytes[take..];
            if self.header.len() == 512 {
                self.take_header();
            }
        }
    }

    /// The stream ended: settle the header being read.
    fn finish(&mut self) {
        if self.end != End::Open {
            return;
        }
        if self.long.is_some() || !self.pending.is_empty() {
            // A short read of a long record's data is quiet, but the header
            // after it is then empty or short: `SubsequentHeaderError`.
            self.end = End::Error;
            return;
        }
        if self.seen < self.next {
            // Only skipped data can end early: `seek(offset - 1)` then
            // `read(1)` found nothing ("unexpected end of data").
            debug_assert!(self.skipped);
            self.end = End::Error;
            return;
        }
        // An empty or short header: "empty file" at offset 0, else the end.
        self.end = if self.next == 0 {
            End::Error
        } else {
            End::Done
        };
    }
}

/// Recursion and work bounds for the link walk, far inside CPython's
/// default recursion limit of 1000 frames.
const MAX_DEPTH: usize = 400;
const MAX_STEPS: usize = 50_000_000;

enum Walk {
    /// `ValueError` or `RecursionError`: a refusal.
    Refuse,
    Unsure,
}

struct Links {
    map: HashMap<Vec<u8>, Vec<u8>>,
    steps: usize,
}

fn split(path: &[u8]) -> Vec<Vec<u8>> {
    path.split(|byte| *byte == b'/')
        .map(<[u8]>::to_vec)
        .collect()
}

/// `posixpath.normpath`.
fn normpath(path: &[u8]) -> Vec<u8> {
    if path.is_empty() {
        return b".".to_vec();
    }
    let mut initial = usize::from(path.starts_with(b"/"));
    if path.starts_with(b"//") && !path.starts_with(b"///") {
        initial = 2;
    }
    let mut parts: Vec<&[u8]> = Vec::new();
    for part in path.split(|byte| *byte == b'/') {
        if part.is_empty() || part == b"." {
            continue;
        }
        if part != b".." || (initial == 0 && parts.is_empty()) || parts.last() == Some(&&b".."[..])
        {
            parts.push(part);
        } else if !parts.is_empty() {
            parts.pop();
        }
    }
    let mut result = vec![b'/'; initial];
    result.extend_from_slice(&parts.join(&b'/'));
    if result.is_empty() {
        b".".to_vec()
    } else {
        result
    }
}

/// `posixpath.dirname`.
fn dirname(path: &[u8]) -> Vec<u8> {
    let cut = path
        .iter()
        .rposition(|byte| *byte == b'/')
        .map_or(0, |at| at + 1);
    let mut head = path[..cut].to_vec();
    if !head.is_empty() && head.iter().any(|byte| *byte != b'/') {
        while head.last() == Some(&b'/') {
            head.pop();
        }
    }
    head
}

impl Links {
    /// `resolve(parts, seen)`: `Ok(None)` for a loop.
    fn resolve(
        &mut self,
        parts: Vec<Vec<u8>>,
        seen: &[Vec<u8>],
        depth: usize,
    ) -> Result<Option<Vec<Vec<u8>>>, Walk> {
        if depth > MAX_DEPTH {
            return Err(Walk::Unsure);
        }
        let mut current: Vec<Vec<u8>> = Vec::new();
        for part in parts {
            self.steps += 1;
            if self.steps > MAX_STEPS {
                return Err(Walk::Unsure);
            }
            if part.is_empty() || part == b"." {
                continue;
            }
            if part == b".." {
                if current.pop().is_none() {
                    return Err(Walk::Refuse);
                }
                continue;
            }
            current.push(part);
            let key = current.join(&b'/');
            let Some(target) = self.map.get(&key).cloned() else {
                continue;
            };
            if seen.contains(&key) {
                return Ok(None);
            }
            if target.starts_with(b"/") || target.contains(&b'\\') {
                return Err(Walk::Refuse);
            }
            let mut next = current[..current.len() - 1].to_vec();
            next.extend(split(&target));
            let mut seen = seen.to_vec();
            seen.push(key);
            match self.resolve(next, &seen, depth + 1)? {
                Some(resolved) => current = resolved,
                None => return Ok(None),
            }
        }
        Ok(Some(current))
    }

    fn check(&mut self, member: &Member) -> Result<(), Walk> {
        let name = normpath(&member.name);
        if name.starts_with(b"/")
            || split(&member.name).iter().any(|part| part == b"..")
            || name.contains(&b'\\')
        {
            return Err(Walk::Refuse);
        }
        if member.is_special() {
            return Err(Walk::Refuse);
        }
        if member.is_symlink() || member.is_hardlink() {
            if member.linkname.starts_with(b"/") || member.linkname.contains(&b'\\') {
                return Err(Walk::Refuse);
            }
            let mut parts = if member.is_symlink() {
                split(&dirname(&name))
            } else {
                Vec::new()
            };
            parts.extend(split(&member.linkname));
            self.resolve(parts, std::slice::from_ref(&name), 1)?;
        }
        self.resolve(split(&name), &[], 1)?;
        Ok(())
    }
}

fn check_members(members: &[Member]) -> Verdict {
    let mut links = Links {
        map: members
            .iter()
            .filter(|member| member.is_symlink())
            .map(|member| (normpath(&member.name), member.linkname.clone()))
            .collect(),
        steps: 0,
    };
    let result = (|| {
        for member in members {
            links.check(member)?;
        }
        // The transient link graph while members are extracted in order.
        links.map.clear();
        for member in members {
            let name = normpath(&member.name);
            links.resolve(split(&name), &[], 1)?;
            if member.is_symlink() {
                links.map.insert(name, member.linkname.clone());
            } else {
                links.map.remove(&name);
            }
            links.check(member)?;
        }
        Ok(())
    })();
    match result {
        Ok(()) => Verdict::Pass,
        Err(Walk::Refuse) => Verdict::Refuse,
        Err(Walk::Unsure) => Verdict::Unsure,
    }
}

pub(super) fn validate(path: &Path) -> Verdict {
    let mut reader = Reader::new();
    let read = gzip::stream(path, |chunk| {
        reader.feed(chunk);
        Ok::<(), Stop>(())
    });
    if let Err(stop) = read {
        return stop.into();
    }
    reader.finish();
    match reader.end {
        End::Done => check_members(&reader.members),
        End::Error => Verdict::Refuse,
        End::Unsure => Verdict::Unsure,
        End::Open => unreachable!("finish settles the reader"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn octal_fields_follow_python_int() {
        for (text, expected) in [
            (&b"0000644 "[..], Ok(0o644)),
            (b"  17", Ok(0o17)),
            (b"", Ok(0)),
            (b"   ", Ok(0)),
            (b"0o_17", Ok(0o17)),
            (b"0O7", Ok(7)),
            (b"-0o7", Ok(-7)),
            (b"+7", Ok(7)),
            (b"0_7", Ok(7)),
            (b"\x0b7\x0c", Ok(7)),
            (b"_17", Err(())),
            (b"1__7", Err(())),
            (b"17_", Err(())),
            (b"0o", Err(())),
            (b"8", Err(())),
            (b"1 7", Err(())),
        ] {
            assert_eq!(octal(text), expected, "{text:?}");
        }
    }

    #[test]
    fn base_256_fields_are_signed() {
        assert_eq!(nti(&[0o200, 0, 0, 1, 0]), Ok(256));
        assert_eq!(nti(&[0o377, 0xff, 0xff, 0xff, 0xff]), Ok(-1));
        assert_eq!(nti(b"12\x0034"), Ok(0o12));
        assert_eq!(nti(b"\xe9"), Err(()));
    }

    /// A ustar header (checksum computed), as the harness row builds them.
    fn header(name: &[u8], kind: u8, size: usize, linkname: &[u8]) -> Vec<u8> {
        let mut buf = vec![0u8; 512];
        buf[..name.len()].copy_from_slice(name);
        buf[100..108].copy_from_slice(b"0000644\0");
        buf[108..116].copy_from_slice(b"0000000\0");
        buf[116..124].copy_from_slice(b"0000000\0");
        buf[124..136].copy_from_slice(format!("{size:011o}\0").as_bytes());
        buf[136..148].copy_from_slice(b"00000000000\0");
        buf[156] = kind;
        buf[157..157 + linkname.len()].copy_from_slice(linkname);
        buf[257..265].copy_from_slice(b"ustar\x0000");
        buf[148..156].copy_from_slice(b"        ");
        let sum: u32 = buf.iter().map(|b| u32::from(*b)).sum();
        buf[148..156].copy_from_slice(format!("{sum:06o}\0 ").as_bytes());
        buf
    }

    fn data(content: &[u8]) -> Vec<u8> {
        let mut out = content.to_vec();
        out.resize(content.len().div_ceil(512) * 512, 0);
        out
    }

    fn long(kind: u8, value: &[u8]) -> Vec<u8> {
        let mut record = header(b"././@LongLink", kind, value.len() + 1, b"");
        let mut content = value.to_vec();
        content.push(0);
        record.extend(data(&content));
        record
    }

    fn read(parts: &[Vec<u8>]) -> Reader {
        let mut reader = Reader::new();
        // Fed in uneven chunks: records may straddle any boundary.
        let stream = parts.concat();
        for chunk in stream.chunks(77) {
            reader.feed(chunk);
        }
        reader.finish();
        reader
    }

    const END: [u8; 1024] = [0; 1024];

    #[test]
    fn gnu_long_names_and_links_patch_the_next_header() {
        let name = vec![b'n'; 150];
        let target = vec![b't'; 130];
        let reader = read(&[
            long(b'L', &name),
            header(b"short", b'0', 1, b""),
            data(b"x"),
            long(b'K', &target),
            long(b'L', b"dir//"),
            header(b"d/", b'5', 0, b"ignored"),
            END.to_vec(),
        ]);
        assert_eq!(reader.end, End::Done);
        let names: Vec<_> = reader
            .members
            .iter()
            .map(|m| (m.kind, m.name.clone(), m.linkname.clone()))
            .collect();
        assert_eq!(
            names,
            vec![
                (b'0', name, Vec::new()),
                // Each level removes one slash from a directory's name.
                (b'5', b"dir".to_vec(), target),
            ]
        );
    }

    #[test]
    fn a_long_name_is_cut_at_its_first_nul_and_outermost_wins() {
        let mut content = b"first\0rest".to_vec();
        content.push(0);
        let mut record = header(b"././@LongLink", b'L', content.len(), b"");
        record.extend(data(&content));
        let reader = read(&[
            record,
            long(b'L', b"second"),
            header(b"x", b'0', 0, b""),
            END.to_vec(),
        ]);
        assert_eq!(reader.end, End::Done);
        assert_eq!(reader.members[0].name, b"first");
    }

    #[test]
    fn any_header_problem_after_a_long_record_is_a_read_error() {
        let name = b"a-long-name".to_vec();
        for (case, parts) in [
            ("end blocks", vec![long(b'L', &name), END.to_vec()]),
            ("stream ends", vec![long(b'L', &name)]),
            (
                "data cut short",
                vec![long(b'L', &vec![b'n'; 600])[..700].to_vec()],
            ),
            ("short header", vec![long(b'L', &name), vec![1u8; 100]]),
            (
                "bad checksum",
                vec![long(b'L', &name), {
                    let mut h = header(b"x", b'0', 0, b"");
                    h[148] = b'7';
                    h
                }],
            ),
        ] {
            // Not the first member: a bare bad header there ends quietly.
            let mut all = vec![header(b"first", b'0', 0, b"")];
            all.extend(parts);
            assert_eq!(read(&all).end, End::Error, "{case}");
        }
        let chain: Vec<_> = (0..=MAX_LONG_CHAIN).map(|_| long(b'L', b"n")).collect();
        assert_eq!(read(&chain).end, End::Unsure);
    }

    #[test]
    fn normpath_and_dirname_match_posixpath() {
        for (path, expected) in [
            (&b""[..], &b"."[..]),
            (b"a//b/./c/", b"a/b/c"),
            (b"../a", b"../a"),
            (b"a/../..", b".."),
            (b"/../a", b"/a"),
            (b"//a", b"//a"),
            (b"///a", b"/a"),
            (b"./", b"."),
        ] {
            assert_eq!(normpath(path), expected, "{path:?}");
        }
        assert_eq!(dirname(b"a/b"), b"a");
        assert_eq!(dirname(b"b"), b"");
        assert_eq!(dirname(b"/b"), b"/");
        assert_eq!(dirname(b"a//b"), b"a");
    }
}
