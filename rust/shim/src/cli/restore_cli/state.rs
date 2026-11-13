//! The state archive check: the archive must be a complete gzip file whose
//! tar members cannot escape the state volume when GNU tar in the daemon
//! image extracts them.
//!
//! The reader is never more permissive than that `tar`: every header it
//! cannot read is a refusal (GNU tar would skip to the next header and
//! extract members never checked here), the archive must end at a header
//! boundary or with zero blocks and nothing but zeros after them, and a
//! member's data must be present to the end of its last block. Names are the
//! raw header bytes with the ustar prefix, GNU long names and links (`L`,
//! `K`) and POSIX extended headers (`x`: `path`, `linkpath` and `size`)
//! applied as `tar` applies them, and numeric fields read as `tar` reads them. Archive features whose effect on names or
//! sizes this reader does not model (sparse members, a global extended
//! header that sets a name or size, Solaris extended headers) are refused
//! with their own reason. A deflate error, a link graph too deep to walk and
//! two long names or two long links for one member are refusals too.
use std::{collections::HashMap, path::Path};

use super::gzip::{self, Stop};

const BLOCK: u128 = 512;

/// Why the archive is refused.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum Refusal {
    /// Unreadable, incomplete, or a member could escape the state volume.
    Unsafe,
    /// It uses a tar feature this check does not model (named).
    Unsupported(&'static str),
}

/// One member as `tar` extracts it: its type, name and link target.
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
    /// Character, block or FIFO.
    fn is_special(&self) -> bool {
        matches!(self.kind, b'3' | b'4' | b'6')
    }
}

/// How the member list ended.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum End {
    /// Still reading.
    Open,
    /// The end-of-archive marker, or the stream's end at a header boundary.
    Done,
    /// Refused.
    Error(Refusal),
}

/// The tar reader, fed the decompressed stream in order.
struct Reader {
    /// Bytes of the stream seen so far.
    seen: u128,
    /// Where the next header is read.
    next: u128,
    header: Vec<u8>,
    members: Vec<Member>,
    end: End,
    /// After the end-of-archive marker: every later byte must be zero.
    trailing: bool,
    /// GNU long records read so far for the header after them, outermost
    /// first: the record's kind and its data up to the first NUL.
    pending: Vec<(u8, Vec<u8>)>,
    /// The extended header (`x`) for the next member, once read.
    pax: Option<Pax>,
    /// The current long or extended record being read.
    long: Option<Long>,
}

struct Long {
    kind: u8,
    /// The record's own length in bytes (its data is padded to blocks).
    length: u128,
    left: u128,
    data: Vec<u8>,
    ended: bool,
}

/// The fields of one extended header that change what `tar` extracts.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
struct Pax {
    path: Option<Vec<u8>>,
    linkpath: Option<Vec<u8>>,
    size: Option<u128>,
}

/// An extended header larger than this is refused (real ones are small).
const MAX_PAX: u128 = 1024 * 1024;

/// `nts`: up to the first NUL.
fn nts(field: &[u8]) -> &[u8] {
    field
        .iter()
        .position(|byte| *byte == 0)
        .map_or(field, |at| &field[..at])
}

/// C `isspace` in the C locale.
fn c_space(byte: u8) -> bool {
    matches!(byte, b' ' | b'\t' | b'\n' | 0x0b | 0x0c | b'\r')
}

/// A numeric field as GNU tar's `from_header` reads it: base-256 when the
/// first byte says so; otherwise one leading NUL skipped (an older tar's
/// overflow), then spaces, then octal digits that end at the field's end, a
/// NUL or a space. `None` is a field `tar` refuses or reads otherwise (a
/// `+` or `-` base-64 value, an underscore, a `0o` prefix): restore refuses
/// it rather than guess where `tar` would read the next header.
fn gnu_number(field: &[u8]) -> Option<i128> {
    if matches!(field[0], 0o200 | 0o377) {
        let mut number: i128 = 0;
        for byte in &field[1..] {
            number = (number << 8) + i128::from(*byte);
        }
        if field[0] == 0o377 {
            number -= 1i128 << (8 * (field.len() - 1));
        }
        return Some(number);
    }
    let mut at = usize::from(field[0] == 0);
    while at < field.len() && c_space(field[at]) {
        at += 1;
    }
    let start = at;
    let mut number: i128 = 0;
    while at < field.len() && (b'0'..=b'7').contains(&field[at]) {
        number = number.checked_mul(8)? + i128::from(field[at] - b'0');
        at += 1;
    }
    if at == start || (at < field.len() && field[at] != 0 && !c_space(field[at])) {
        return None;
    }
    Some(number)
}

/// A field `tar` reads only for some member types, or that a writer left
/// empty: all NULs or spaces.
fn blank(field: &[u8]) -> bool {
    field.iter().all(|byte| *byte == 0 || c_space(*byte))
}

/// The unsigned and signed sums, the checksum field read as spaces.
fn checksums(header: &[u8]) -> (i128, i128) {
    let bytes = header[..148].iter().chain(&header[156..]);
    let unsigned: i128 = 256 + bytes.clone().map(|b| i128::from(*b)).sum::<i128>();
    let signed: i128 = 256 + bytes.map(|b| i128::from(*b as i8)).sum::<i128>();
    (unsigned, signed)
}

fn round_up(count: u128) -> u128 {
    count.div_ceil(BLOCK) * BLOCK
}

enum Header {
    /// A member, and its stored data size (before any `size` override).
    Member(Member, u128),
    /// A GNU long name (`L`) or link (`K`) record, or an extended header
    /// (`x`, `g`), and its data length in bytes.
    Record(u8, u128),
    Refused(Refusal),
}

/// One 512-byte header.
fn parse_header(header: &[u8]) -> Header {
    let unsafe_ = Header::Refused(Refusal::Unsafe);
    let Some(stored) = gnu_number(&header[148..156]) else {
        return unsafe_;
    };
    let (unsigned, signed) = checksums(header);
    if stored != unsigned && stored != signed {
        return unsafe_;
    }
    // The size decides where `tar` reads the next header: it must parse
    // exactly as `tar` parses it. Mode, ids, time and device numbers must
    // too, or be left blank.
    let Some(size) = gnu_number(&header[124..136]).and_then(|size| u128::try_from(size).ok())
    else {
        return unsafe_;
    };
    for (from, to) in [
        (100, 108),
        (108, 116),
        (116, 124),
        (136, 148),
        (329, 337),
        (337, 345),
    ] {
        let field = &header[from..to];
        if gnu_number(field).is_none() && !blank(field) {
            return unsafe_;
        }
    }
    let mut kind = header[156];
    let mut name = nts(&header[..100]).to_vec();
    let linkname = nts(&header[157..257]).to_vec();
    let prefix = nts(&header[345..500]);
    match kind {
        b'S' => return Header::Refused(Refusal::Unsupported("a sparse member")),
        b'X' => return Header::Refused(Refusal::Unsupported("a Solaris extended header")),
        b'L' | b'K' | b'x' | b'g' => return Header::Record(kind, size),
        _ => {}
    }
    if kind == 0 && name.ends_with(b"/") {
        kind = b'5';
    }
    if !prefix.is_empty() {
        let mut joined = prefix.to_vec();
        joined.push(b'/');
        joined.extend_from_slice(&name);
        name = joined;
    }
    if kind == b'5' {
        while name.last() == Some(&b'/') {
            name.pop();
        }
    }
    Header::Member(
        Member {
            kind,
            name,
            linkname,
        },
        size,
    )
}

/// The records of an extended header (`<length> <key>=<value>\n`, the
/// length counting the whole record); `None` when malformed.
fn pax_records(data: &[u8]) -> Option<Vec<(Vec<u8>, Vec<u8>)>> {
    let mut records = Vec::new();
    let mut rest = data;
    while !rest.is_empty() {
        let space = rest.iter().position(|byte| *byte == b' ')?;
        let digits = &rest[..space];
        if digits.is_empty() || digits.len() > 9 || !digits.iter().all(u8::is_ascii_digit) {
            return None;
        }
        let length: usize = std::str::from_utf8(digits).ok()?.parse().ok()?;
        if length <= space + 1 || length > rest.len() || rest[length - 1] != b'\n' {
            return None;
        }
        let body = &rest[space + 1..length - 1];
        let equals = body.iter().position(|byte| *byte == b'=')?;
        if equals == 0 {
            return None;
        }
        records.push((body[..equals].to_vec(), body[equals + 1..].to_vec()));
        rest = &rest[length..];
    }
    Some(records)
}

/// A decimal `size` value.
fn pax_size(value: &[u8]) -> Option<u128> {
    if value.is_empty() || value.len() > 30 || !value.iter().all(u8::is_ascii_digit) {
        return None;
    }
    std::str::from_utf8(value).ok()?.parse().ok()
}

/// Keys that change a member's name or data layout: honoured, or refused.
fn pax(kind: u8, data: &[u8]) -> Result<Option<Pax>, Refusal> {
    let records = pax_records(data).ok_or(Refusal::Unsafe)?;
    let mut fields = Pax::default();
    for (key, value) in records {
        if key.starts_with(b"GNU.sparse.") {
            return Err(Refusal::Unsupported("a sparse member"));
        }
        let changes = matches!(&key[..], b"path" | b"linkpath" | b"size");
        if kind == b'g' {
            if changes {
                return Err(Refusal::Unsupported(
                    "a global extended header that sets a name or size",
                ));
            }
            continue;
        }
        // GNU tar cuts a name at a NUL; this check would read past it.
        if matches!(&key[..], b"path" | b"linkpath") && value.contains(&0) {
            return Err(Refusal::Unsafe);
        }
        // An empty value deletes the keyword: the header's own field stands.
        match &key[..] {
            b"path" => fields.path = (!value.is_empty()).then_some(value),
            b"linkpath" => fields.linkpath = (!value.is_empty()).then_some(value),
            b"size" if value.is_empty() => fields.size = None,
            b"size" => fields.size = Some(pax_size(&value).ok_or(Refusal::Unsafe)?),
            _ => {}
        }
    }
    Ok((kind == b'x').then_some(fields))
}

impl Reader {
    fn new() -> Self {
        Self {
            seen: 0,
            next: 0,
            header: Vec::with_capacity(512),
            members: Vec::new(),
            end: End::Open,
            trailing: false,
            pending: Vec::new(),
            pax: None,
            long: None,
        }
    }

    fn refuse(&mut self, refusal: Refusal) {
        self.end = End::Error(refusal);
    }

    /// The header at `self.next` is complete: decide it.
    fn take_header(&mut self) {
        let header = std::mem::take(&mut self.header);
        if header.iter().all(|byte| *byte == 0) {
            if !self.pending.is_empty() || self.pax.is_some() {
                // A long or extended record with no member after it.
                return self.refuse(Refusal::Unsafe);
            }
            if self.members.is_empty() {
                return self.refuse(Refusal::Unsafe);
            }
            self.trailing = true;
            return;
        }
        match parse_header(&header) {
            Header::Member(mut member, stored) => {
                // The record nearest the header patches last; a directory
                // loses its trailing slashes.
                for (kind, data) in self.pending.drain(..).rev() {
                    if kind == b'L' {
                        member.name = data;
                    } else {
                        member.linkname = data;
                    }
                }
                let mut size = stored;
                if let Some(pax) = self.pax.take() {
                    if let Some(path) = pax.path {
                        member.name = path;
                    }
                    if let Some(linkpath) = pax.linkpath {
                        member.linkname = linkpath;
                    }
                    if let Some(override_) = pax.size {
                        size = override_;
                    }
                }
                if member.kind == b'5' {
                    while member.name.last() == Some(&b'/') {
                        member.name.pop();
                    }
                }
                // Regular files and unknown types carry data; the other
                // types carry none, whatever their size field says.
                let data = if matches!(member.kind, b'1' | b'2' | b'3' | b'4' | b'5' | b'6') {
                    0
                } else {
                    size
                };
                self.members.push(member);
                self.next += BLOCK + round_up(data);
            }
            Header::Record(kind, length) => {
                let pax = matches!(kind, b'x' | b'g');
                if pax && (length > MAX_PAX || self.pax.is_some()) {
                    return self.refuse(Refusal::Unsafe);
                }
                if !pax && self.pending.iter().any(|(pending, _)| *pending == kind) {
                    // GNU tar takes the last of two such records, which a
                    // harmless first one could hide.
                    return self.refuse(Refusal::Unsupported(
                        "two GNU long names (or long links) for one member",
                    ));
                }
                if kind == b'x' && !self.pending.is_empty()
                    || matches!(kind, b'L' | b'K') && self.pax.is_some()
                {
                    // GNU long records and an extended header for one
                    // member: which wins is not modelled here.
                    return self.refuse(Refusal::Unsupported(
                        "both GNU long names and an extended header for one member",
                    ));
                }
                self.next += BLOCK;
                self.long = Some(Long {
                    kind,
                    length,
                    left: round_up(length),
                    data: Vec::new(),
                    // Only the record's own bytes are kept.
                    ended: false,
                });
                if let Some(long) = &mut self.long {
                    long.data.reserve(length.min(MAX_PAX) as usize);
                }
                if length == 0 {
                    self.end_long();
                }
            }
            Header::Refused(refusal) => self.refuse(refusal),
        }
    }

    /// The record's data is read: it waits for the next header.
    fn end_long(&mut self) {
        let mut long = self.long.take().expect("a record is being read");
        match long.kind {
            b'L' | b'K' => self.pending.push((long.kind, long.data)),
            kind => {
                long.data.truncate(long.length as usize);
                match pax(kind, &long.data) {
                    Ok(fields) => self.pax = fields,
                    Err(refusal) => self.refuse(refusal),
                }
            }
        }
    }

    fn feed(&mut self, mut bytes: &[u8]) {
        while self.end == End::Open && !bytes.is_empty() {
            if self.trailing {
                if bytes.iter().any(|byte| *byte != 0) {
                    return self.refuse(Refusal::Unsafe);
                }
                self.seen += bytes.len() as u128;
                return;
            }
            if let Some(long) = &mut self.long {
                let take = long.left.min(bytes.len() as u128) as usize;
                let part = &bytes[..take];
                if matches!(long.kind, b'L' | b'K') {
                    if !long.ended {
                        match part.iter().position(|byte| *byte == 0) {
                            Some(at) => {
                                long.data.extend_from_slice(&part[..at]);
                                long.ended = true;
                            }
                            None => long.data.extend_from_slice(part),
                        }
                    }
                } else {
                    long.data.extend_from_slice(part);
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

    /// The stream ended: settle the reader.
    fn finish(&mut self) {
        if self.end != End::Open {
            return;
        }
        let incomplete = self.long.is_some()
            || !self.pending.is_empty()
            || self.pax.is_some()
            || !self.header.is_empty()
            || self.seen < self.next
            || self.members.is_empty();
        self.end = if incomplete {
            End::Error(Refusal::Unsafe)
        } else {
            End::Done
        };
    }
}

/// Recursion and work bounds for the link walk: a graph past them is
/// refused rather than walked.
const MAX_DEPTH: usize = 400;
const MAX_STEPS: usize = 50_000_000;

/// The link walk found an escape, or a graph too large to walk.
struct Escape;

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
    /// Resolve `parts` through the symlinks seen so far: `Ok(None)` for a
    /// loop, which the filesystem cannot traverse either.
    fn resolve(
        &mut self,
        parts: Vec<Vec<u8>>,
        seen: &[Vec<u8>],
        depth: usize,
    ) -> Result<Option<Vec<Vec<u8>>>, Escape> {
        if depth > MAX_DEPTH {
            return Err(Escape);
        }
        let mut current: Vec<Vec<u8>> = Vec::new();
        for part in parts {
            self.steps += 1;
            if self.steps > MAX_STEPS {
                return Err(Escape);
            }
            if part.is_empty() || part == b"." {
                continue;
            }
            if part == b".." {
                if current.pop().is_none() {
                    return Err(Escape);
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
                return Err(Escape);
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

    fn check(&mut self, member: &Member) -> Result<(), Escape> {
        let name = normpath(&member.name);
        if name.starts_with(b"/")
            || split(&member.name).iter().any(|part| part == b"..")
            || name.contains(&b'\\')
        {
            return Err(Escape);
        }
        if member.is_special() {
            return Err(Escape);
        }
        if member.is_symlink() || member.is_hardlink() {
            if member.linkname.starts_with(b"/") || member.linkname.contains(&b'\\') {
                return Err(Escape);
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

fn check_members(members: &[Member]) -> Result<(), Refusal> {
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
    result.map_err(|Escape| Refusal::Unsafe)
}

pub(super) fn validate(path: &Path) -> Result<(), Refusal> {
    let mut reader = Reader::new();
    let read = gzip::stream(path, |chunk| {
        reader.feed(chunk);
        Ok::<(), Stop>(())
    });
    if read.is_err() {
        return Err(Refusal::Unsafe);
    }
    reader.finish();
    match reader.end {
        End::Done => check_members(&reader.members),
        End::Error(refusal) => Err(refusal),
        End::Open => unreachable!("finish settles the reader"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn numbers_are_read_as_gnu_tar_reads_them() {
        for (field, expected) in [
            (&b"0000644\0"[..], Some(0o644)),
            (b"  17\0", Some(0o17)),
            (b"\x000000017 ", Some(0o17)),
            (b"17 junk", Some(0o17)),
            (b"0000017", Some(0o17)),
            (b"", None),
            (b"   \0", None),
            (b"0o17\0", None),
            (b"0_7\0", None),
            (b"+7\0", None),
            (b"-7\0", None),
            (b"8\0", None),
            (b"\x1c7\0", None),
            (b"17x", None),
        ] {
            let mut padded = field.to_vec();
            if padded.is_empty() {
                padded.push(0);
            }
            assert_eq!(gnu_number(&padded), expected, "{field:?}");
        }
        assert_eq!(gnu_number(&[0o200, 0, 0, 1, 0]), Some(256));
        assert_eq!(gnu_number(&[0o377, 0xff, 0xff, 0xff, 0xff]), Some(-1));
        assert!(blank(b"\0\0\0") && blank(b"  \0") && !blank(b"0"));
    }

    #[test]
    fn a_size_tar_reads_differently_cannot_hide_a_member() {
        // GNU tar skips the leading NUL and reads 512 bytes of data, so the
        // FIFO after the decoy is a member; Python's reading took size 0.
        let mut hidden = header(b"A", b'0', 0, b"");
        hidden[124..136].copy_from_slice(b"\x000000001000 ");
        let sum: u32 = {
            let mut h = hidden.clone();
            h[148..156].copy_from_slice(b"        ");
            h.iter().map(|b| u32::from(*b)).sum()
        };
        hidden[148..156].copy_from_slice(format!("{sum:06o}\0 ").as_bytes());
        // Read as Python reads it, `A` is empty and `DECOY`'s 512 bytes of
        // data are the FIFO's header, which is never checked. GNU tar reads
        // `DECOY`'s header as `A`'s data and extracts the FIFO.
        let decoy = header(b"DECOY", b'0', 512, b"");
        assert_eq!(
            verdict(&[
                hidden,
                decoy,
                header(b"EVIL-fifo", b'6', 0, b""),
                END.to_vec()
            ]),
            Err(Refusal::Unsafe)
        );
        // A size tar refuses or reads as base-64 is refused outright.
        for bad in [&b"0o_00000001\0"[..], b"+0000000001\0", b"0000000_001\0"] {
            let mut odd = header(b"x", b'0', 0, b"");
            odd[124..136].copy_from_slice(bad);
            odd[148..156].copy_from_slice(b"        ");
            let sum: u32 = odd.iter().map(|b| u32::from(*b)).sum();
            odd[148..156].copy_from_slice(format!("{sum:06o}\0 ").as_bytes());
            assert_eq!(
                verdict(&[odd, END.to_vec()]),
                Err(Refusal::Unsafe),
                "{bad:?}"
            );
        }
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
    fn a_long_name_is_cut_at_its_first_nul_and_a_second_one_is_refused() {
        let mut content = b"first\0rest".to_vec();
        content.push(0);
        let mut record = header(b"././@LongLink", b'L', content.len(), b"");
        record.extend(data(&content));
        let reader = read(&[record.clone(), header(b"x", b'0', 0, b""), END.to_vec()]);
        assert_eq!(reader.end, End::Done);
        assert_eq!(reader.members[0].name, b"first");
        // GNU tar takes the last of two long names; a harmless first one
        // must not stand in for it.
        let reader = read(&[
            record,
            long(b'L', b"second"),
            header(b"x", b'0', 0, b""),
            END.to_vec(),
        ]);
        assert_eq!(
            reader.end,
            End::Error(Refusal::Unsupported(
                "two GNU long names (or long links) for one member"
            ))
        );
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
            let mut all = vec![header(b"first", b'0', 0, b"")];
            all.extend(parts);
            assert_eq!(read(&all).end, End::Error(Refusal::Unsafe), "{case}");
        }
    }

    fn pax_record(key: &str, value: &[u8]) -> Vec<u8> {
        // "<len> key=value\n", the length counting itself.
        let body_len = key.len() + value.len() + 3;
        let mut len = body_len + 1;
        while format!("{len}").len() + body_len != len {
            len += 1;
        }
        let mut record = format!("{len} {key}=").into_bytes();
        record.extend_from_slice(value);
        record.push(b'\n');
        record
    }

    fn pax(kind: u8, records: &[(&str, &[u8])]) -> Vec<u8> {
        let body: Vec<u8> = records
            .iter()
            .flat_map(|(key, value)| pax_record(key, value))
            .collect();
        let mut out = header(b"./PaxHeaders/x", kind, body.len(), b"");
        out.extend(data(&body));
        out
    }

    fn verdict(parts: &[Vec<u8>]) -> Result<(), Refusal> {
        let reader = read(parts);
        match reader.end {
            End::Done => check_members(&reader.members),
            End::Error(refusal) => Err(refusal),
            End::Open => unreachable!(),
        }
    }

    #[test]
    fn pax_path_linkpath_and_size_are_applied() {
        let long = vec![b'p'; 150];
        let reader = read(&[
            pax(b'x', &[("path", &long), ("mtime", b"1.5")]),
            header(b"short", b'0', 1, b""),
            data(b"x"),
            pax(b'x', &[("linkpath", b"target")]),
            header(b"link", b'2', 0, b"ignored"),
            // A size override: 600 bytes of data although the header says 1.
            pax(b'x', &[("size", b"600")]),
            header(b"big", b'0', 1, b""),
            data(&[b'z'; 600]),
            header(b"after", b'0', 0, b""),
            END.to_vec(),
        ]);
        assert_eq!(reader.end, End::Done);
        let names: Vec<_> = reader
            .members
            .iter()
            .map(|m| (m.name.clone(), m.linkname.clone()))
            .collect();
        assert_eq!(
            names,
            vec![
                (long, Vec::new()),
                (b"link".to_vec(), b"target".to_vec()),
                (b"big".to_vec(), Vec::new()),
                (b"after".to_vec(), Vec::new()),
            ]
        );
    }

    #[test]
    fn a_pax_path_that_escapes_is_refused_even_behind_a_harmless_name() {
        assert_eq!(
            verdict(&[
                pax(b'x', &[("path", b"../escape")]),
                header(b"harmless", b'0', 0, b""),
                END.to_vec(),
            ]),
            Err(Refusal::Unsafe)
        );
        assert_eq!(
            verdict(&[
                pax(b'x', &[("linkpath", b"/etc")]),
                header(b"link", b'2', 0, b"ok"),
                END.to_vec(),
            ]),
            Err(Refusal::Unsafe)
        );
    }

    #[test]
    fn archive_features_not_modelled_are_refused_by_name() {
        let plain = || header(b"x", b'0', 0, b"");
        assert_eq!(
            verdict(&[pax(b'g', &[("path", b"x")]), plain(), END.to_vec()]),
            Err(Refusal::Unsupported(
                "a global extended header that sets a name or size"
            ))
        );
        // A global header that changes nothing else is read through.
        assert_eq!(
            verdict(&[pax(b'g', &[("comment", b"hi")]), plain(), END.to_vec()]),
            Ok(())
        );
        assert_eq!(
            verdict(&[
                pax(b'x', &[("GNU.sparse.name", b"x")]),
                plain(),
                END.to_vec()
            ]),
            Err(Refusal::Unsupported("a sparse member"))
        );
        assert_eq!(
            verdict(&[header(b"s", b'S', 0, b""), END.to_vec()]),
            Err(Refusal::Unsupported("a sparse member"))
        );
        assert_eq!(
            verdict(&[header(b"s", b'X', 0, b""), END.to_vec()]),
            Err(Refusal::Unsupported("a Solaris extended header"))
        );
        assert_eq!(
            verdict(&[
                long(b'L', b"n"),
                pax(b'x', &[("path", b"m")]),
                plain(),
                END.to_vec()
            ]),
            Err(Refusal::Unsupported(
                "both GNU long names and an extended header for one member"
            ))
        );
    }

    #[test]
    fn a_nul_inside_a_pax_name_is_refused() {
        // GNU tar cuts `l\0/safe/link` to `l`, a symlink to ../outside.
        assert_eq!(
            verdict(&[
                pax(
                    b'x',
                    &[("path", b"l\0/safe/link"), ("linkpath", b"../outside")]
                ),
                header(b"x", b'2', 0, b"ok"),
                END.to_vec(),
            ]),
            Err(Refusal::Unsafe)
        );
        assert_eq!(
            verdict(&[
                pax(b'x', &[("linkpath", b"ok\0/../../x")]),
                header(b"x", b'2', 0, b"ok"),
                END.to_vec(),
            ]),
            Err(Refusal::Unsafe)
        );
    }

    #[test]
    fn a_malformed_pax_record_is_refused() {
        let mut bad = header(b"./PaxHeaders/x", b'x', 12, b"");
        bad.extend(data(b"99 path=xx\n"));
        assert_eq!(
            verdict(&[bad, header(b"x", b'0', 0, b""), END.to_vec()]),
            Err(Refusal::Unsafe)
        );
        assert_eq!(
            verdict(&[
                pax(b'x', &[("size", b"12a")]),
                header(b"x", b'0', 0, b""),
                END.to_vec()
            ]),
            Err(Refusal::Unsafe)
        );
    }

    #[test]
    fn anything_tar_could_read_past_is_refused() {
        let first = || header(b"first", b'0', 0, b"");
        // A bad header after the first member: GNU tar skips to the next
        // header and extracts what follows, so it is refused, not an end.
        let mut corrupt = header(b"second", b'0', 0, b"");
        corrupt[148] = b'7';
        assert_eq!(
            verdict(&[
                first(),
                corrupt,
                header(b"../evil", b'0', 0, b""),
                END.to_vec()
            ]),
            Err(Refusal::Unsafe)
        );
        // Bytes after the end marker must be zeros.
        assert_eq!(
            verdict(&[first(), END.to_vec(), header(b"../evil", b'0', 0, b"")]),
            Err(Refusal::Unsafe)
        );
        assert_eq!(verdict(&[first(), END.to_vec(), vec![0; 9216]]), Ok(()));
        // A stream ending at a header boundary is complete; mid-header not.
        assert_eq!(verdict(&[first()]), Ok(()));
        assert_eq!(verdict(&[first(), vec![1u8; 100]]), Err(Refusal::Unsafe));
        // Truncated member data, and an archive with no member at all.
        assert_eq!(
            verdict(&[header(b"f", b'0', 600, b""), vec![0; 512]]),
            Err(Refusal::Unsafe)
        );
        assert_eq!(verdict(&[END.to_vec()]), Err(Refusal::Unsafe));
        assert_eq!(verdict(&[]), Err(Refusal::Unsafe));
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
