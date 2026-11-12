//! CPython 3.11's `gzip._GzipReader`, member by member, with each failure
//! classed by the exception Python raises there.
//!
//! `BadGzipFile` (an `OSError`) and `EOFError` are what restore's checks
//! catch, so they are a refusal. A deflate error is `zlib.error`, which
//! neither check catches: the oracle prints a traceback, so it is `Unsure`
//! (this leaf defers). The inflater here is miniz_oxide, not zlib; both
//! implement RFC 1951 and stop at the same malformed codes, and a stream
//! one of them rejects is never answered here.
use std::{
    fs::File,
    io::{self, Read},
    path::Path,
};

use flate2::{Crc, Decompress, FlushDecompress, Status};

use super::Verdict;

/// Why reading stopped early: a refusal, or a deferral.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum Stop {
    Refuse,
    Unsure,
}

impl From<Stop> for Verdict {
    fn from(stop: Stop) -> Self {
        match stop {
            Stop::Refuse => Verdict::Refuse,
            Stop::Unsure => Verdict::Unsure,
        }
    }
}

const CHUNK: usize = 64 * 1024;

/// `_PaddedFile` over the file: buffered reads with one-byte pushback.
struct Input {
    file: File,
    buffer: Vec<u8>,
    position: usize,
}

impl Input {
    fn fill(&mut self) -> Result<bool, Stop> {
        if self.position < self.buffer.len() {
            return Ok(true);
        }
        self.buffer.resize(CHUNK, 0);
        self.position = 0;
        loop {
            match self.file.read(&mut self.buffer) {
                Ok(count) => {
                    self.buffer.truncate(count);
                    return Ok(count > 0);
                }
                Err(error) if error.kind() == io::ErrorKind::Interrupted => {}
                // Any read error is an OSError in Python.
                Err(_) => {
                    self.buffer.clear();
                    return Err(Stop::Refuse);
                }
            }
        }
    }

    fn byte(&mut self) -> Result<Option<u8>, Stop> {
        if !self.fill()? {
            return Ok(None);
        }
        self.position += 1;
        Ok(Some(self.buffer[self.position - 1]))
    }

    fn unread(&mut self) {
        self.position -= 1;
    }

    /// `_read_exact`: EOFError on a short read.
    fn exact(&mut self, count: usize) -> Result<Vec<u8>, Stop> {
        let mut bytes = Vec::with_capacity(count);
        while bytes.len() < count {
            match self.byte()? {
                Some(byte) => bytes.push(byte),
                None => return Err(Stop::Refuse),
            }
        }
        Ok(bytes)
    }
}

/// `_read_gzip_header`: `Ok(false)` at a clean end of input.
fn header(input: &mut Input) -> Result<bool, Stop> {
    let Some(first) = input.byte()? else {
        return Ok(false);
    };
    let second = input.byte()?;
    if (first, second) != (0x1f, Some(0x8b)) {
        return Err(Stop::Refuse);
    }
    let fixed = input.exact(8)?;
    let (method, flags) = (fixed[0], fixed[1]);
    if method != 8 {
        return Err(Stop::Refuse);
    }
    if flags & 4 != 0 {
        let length = input.exact(2)?;
        input.exact(usize::from(u16::from_le_bytes([length[0], length[1]])))?;
    }
    for flag in [8, 16] {
        if flags & flag != 0 {
            // A missing terminator ends silently; the deflate read then fails.
            while !matches!(input.byte()?, None | Some(0)) {}
        }
    }
    if flags & 2 != 0 {
        input.exact(2)?;
    }
    Ok(true)
}

/// Read every member of the gzip file at `path`, passing the decompressed
/// bytes to `sink` in order. `Err` is the first failure Python would meet.
pub(super) fn stream(
    path: &Path,
    mut sink: impl FnMut(&[u8]) -> Result<(), Stop>,
) -> Result<(), Stop> {
    let file = File::open(path).map_err(|_| Stop::Refuse)?;
    let mut input = Input {
        file,
        buffer: Vec::new(),
        position: 0,
    };
    let mut output = vec![0u8; CHUNK];
    while header(&mut input)? {
        let mut inflater = Decompress::new(false);
        let mut crc = Crc::new();
        loop {
            let available = input.fill()?;
            let before = (inflater.total_in(), inflater.total_out());
            let status = inflater
                .decompress(
                    &input.buffer[input.position..],
                    &mut output,
                    FlushDecompress::None,
                )
                .map_err(|_| Stop::Unsure)?;
            let consumed = (inflater.total_in() - before.0) as usize;
            let produced = (inflater.total_out() - before.1) as usize;
            input.position += consumed;
            if produced > 0 {
                crc.update(&output[..produced]);
                sink(&output[..produced])?;
            }
            if status == Status::StreamEnd {
                break;
            }
            if consumed == 0 && produced == 0 {
                // Out of input: "Compressed file ended before the
                // end-of-stream marker" (EOFError). No progress on input
                // that is there is not a state zlib reaches.
                return Err(if available {
                    Stop::Unsure
                } else {
                    Stop::Refuse
                });
            }
        }
        let trailer = input.exact(8)?;
        let stored_crc = u32::from_le_bytes([trailer[0], trailer[1], trailer[2], trailer[3]]);
        let stored_size = u32::from_le_bytes([trailer[4], trailer[5], trailer[6], trailer[7]]);
        if stored_crc != crc.sum() || stored_size != crc.amount() {
            return Err(Stop::Refuse);
        }
        // Zero padding between and after members is skipped.
        loop {
            match input.byte()? {
                Some(0) => {}
                Some(_) => {
                    input.unread();
                    break;
                }
                None => break,
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use flate2::{Compression, write::GzEncoder};
    use std::io::Write;

    fn gz(data: &[u8]) -> Vec<u8> {
        let mut encoder = GzEncoder::new(Vec::new(), Compression::default());
        encoder.write_all(data).unwrap();
        encoder.finish().unwrap()
    }

    fn read(bytes: &[u8]) -> Result<Vec<u8>, Stop> {
        let file = super::super::tests::Scratch::new(bytes);
        let mut seen = Vec::new();
        stream(&file.0, |chunk| {
            seen.extend_from_slice(chunk);
            Ok(())
        })
        .map(|()| seen)
    }

    #[test]
    fn members_padding_and_trailers_follow_python_gzip() {
        let one = gz(b"one\n");
        let two = gz(b"two\n");
        assert_eq!(
            read(&[one.clone(), two].concat()),
            Ok(b"one\ntwo\n".to_vec())
        );
        assert_eq!(
            read(&[one.clone(), vec![0; 5]].concat()),
            Ok(b"one\n".to_vec())
        );
        assert_eq!(
            read(&[one.clone(), b"junk".to_vec()].concat()),
            Err(Stop::Refuse)
        );
        assert_eq!(read(&[one.clone(), vec![0x1f]].concat()), Err(Stop::Refuse));
        assert_eq!(read(&one[..one.len() - 3]), Err(Stop::Refuse));
        assert_eq!(read(&one[..one.len() - 9]), Err(Stop::Refuse));
        assert_eq!(read(b""), Ok(Vec::new()));
        assert_eq!(read(b"plain text\n"), Err(Stop::Refuse));
        let mut bad_crc = one.clone();
        let at = bad_crc.len() - 5;
        bad_crc[at] ^= 0xff;
        assert_eq!(read(&bad_crc), Err(Stop::Refuse));
        let mut bad_size = one.clone();
        let at = bad_size.len() - 1;
        bad_size[at] ^= 1;
        assert_eq!(read(&bad_size), Err(Stop::Refuse));
        let mut method = one;
        method[2] = 7;
        assert_eq!(read(&method), Err(Stop::Refuse));
    }

    #[test]
    fn a_deflate_error_is_unsure_as_zlib_error_escapes_python() {
        // A stored-block header whose length and complement disagree.
        let mut bytes = vec![0x1f, 0x8b, 8, 0, 0, 0, 0, 0, 0, 0xff];
        bytes.extend_from_slice(&[0x01, 0x05, 0x00, 0x00, 0x00]);
        assert_eq!(read(&bytes), Err(Stop::Unsure));
    }

    #[test]
    fn optional_header_fields_are_skipped() {
        let body = gz(b"x");
        // FEXTRA, FNAME, FCOMMENT and FHCRC before the deflate data.
        let mut bytes = vec![0x1f, 0x8b, 8, 4 | 8 | 16 | 2, 0, 0, 0, 0, 0, 0xff];
        bytes.extend_from_slice(&[2, 0, b'a', b'b']);
        bytes.extend_from_slice(b"name\0comment\0");
        bytes.extend_from_slice(&[0, 0]);
        bytes.extend_from_slice(&body[10..]);
        assert_eq!(read(&bytes), Ok(b"x".to_vec()));
    }
}
