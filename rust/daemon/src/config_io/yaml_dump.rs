//! Byte-exact port of PyYAML 6.0.3's
//! `yaml.safe_dump(data, default_flow_style=False, sort_keys=False)`.
//!
//! The pipeline mirrors PyYAML's: `SafeRepresenter` turns each value into a
//! tagged scalar or collection, the serializer resolves implicit tags with the
//! `SafeLoader` YAML 1.1 resolvers, and the emitter lays out block style with
//! `indent=2`, `width=80`, `allow_unicode=False` and no explicit document
//! markers.
//!
//! The emitter walks the tree directly instead of buffering events; PyYAML's
//! event lookahead only detects empty collections and empty documents, which
//! the tree answers directly. Paths that `safe_dump` with these options cannot
//! reach are omitted: anchors and aliases (the tree has no shared nodes),
//! non-empty flow collections (`default_flow_style=False`), literal and folded
//! block scalars (no representer requests them), canonical output, and
//! version or tag directives.

use crate::config::Node;

/// Dumps `node` exactly as `yaml.safe_dump(node, default_flow_style=False,
/// sort_keys=False)` does.
pub(crate) fn safe_dump(node: &Node) -> String {
    let mut emitter = Emitter::new();
    // expect_document_start: the first document is implicit (no `---`).
    emitter.expect_node(node, Context::ROOT);
    // expect_document_end
    emitter.write_indent();
    // expect_document_start at StreamEndEvent
    if emitter.open_ended {
        emitter.write_indicator("...", true, false, false);
        emitter.write_indent();
    }
    emitter.out
}

// ---------------------------------------------------------------------------
// Representer and resolver
// ---------------------------------------------------------------------------

pub(super) const TAG_NULL: &str = "tag:yaml.org,2002:null";
pub(super) const TAG_BOOL: &str = "tag:yaml.org,2002:bool";
pub(super) const TAG_INT: &str = "tag:yaml.org,2002:int";
pub(super) const TAG_FLOAT: &str = "tag:yaml.org,2002:float";
pub(super) const TAG_STR: &str = "tag:yaml.org,2002:str";
const TAG_SEQ: &str = "tag:yaml.org,2002:seq";
const TAG_MAP: &str = "tag:yaml.org,2002:map";
pub(super) const TAG_MERGE: &str = "tag:yaml.org,2002:merge";
pub(super) const TAG_TIMESTAMP: &str = "tag:yaml.org,2002:timestamp";
const TAG_VALUE: &str = "tag:yaml.org,2002:value";
const TAG_YAML: &str = "tag:yaml.org,2002:yaml";
const TAG_PREFIX: &str = "tag:yaml.org,2002:";

/// A represented scalar: its tag, text, and the serializer's `implicit` pair.
struct Scalar {
    tag: &'static str,
    text: Vec<char>,
    /// `(plain text resolves to tag, quoted text resolves to tag)`.
    implicit: (bool, bool),
}

impl Scalar {
    fn new(tag: &'static str, value: String) -> Self {
        // Serializer.serialize_node: the quoted form always resolves to str.
        // A string this daemon's own start-up loader would read as another
        // type (`1e5`, `0o17`) is never plain, so a rewrite never changes
        // what the next start reads (a declared byte difference).
        let plain = resolve(&value) == tag && !(tag == TAG_STR && boot_reads_other_type(&value));
        let implicit = (plain, tag == TAG_STR);
        Self {
            tag,
            text: value.chars().collect(),
            implicit,
        }
    }
}

/// The daemon's start-up loader (`config::resolve_scalar`, YAML 1.2) reads
/// this plain text as something other than a string.
pub(super) fn boot_reads_other_type(text: &str) -> bool {
    !matches!(
        crate::config::resolve_scalar(
            text.to_string(),
            yaml_rust2::scanner::TScalarStyle::Plain,
            None
        ),
        Ok(Node::Str(_))
    )
}

/// SafeRepresenter for scalar nodes; `None` for collections.
fn represent_scalar(node: &Node) -> Option<Scalar> {
    let (tag, value) = match node {
        Node::Null => (TAG_NULL, "null".to_owned()),
        Node::Bool(b) => (TAG_BOOL, if *b { "true" } else { "false" }.to_owned()),
        Node::Int(i) => (TAG_INT, i.to_string()),
        Node::Float(f) => (TAG_FLOAT, represent_float(*f)),
        Node::Str(s) => (TAG_STR, s.clone()),
        Node::MergeKey => (TAG_STR, "<<".to_owned()),
        Node::Seq(_) | Node::Map(_) => return None,
    };
    Some(Scalar::new(tag, value))
}

/// The tag a node carries; collections are always `!!seq` / `!!map`.
fn node_tag(node: &Node) -> &'static str {
    match node {
        Node::Null => TAG_NULL,
        Node::Bool(_) => TAG_BOOL,
        Node::Int(_) => TAG_INT,
        Node::Float(_) => TAG_FLOAT,
        Node::Str(_) | Node::MergeKey => TAG_STR,
        Node::Seq(_) => TAG_SEQ,
        Node::Map(_) => TAG_MAP,
    }
}

/// SafeRepresenter.represent_float.
fn represent_float(f: f64) -> String {
    if f.is_nan() {
        return ".nan".to_owned();
    }
    if f == f64::INFINITY {
        return ".inf".to_owned();
    }
    if f == f64::NEG_INFINITY {
        return "-.inf".to_owned();
    }
    // Python's `repr`, shared with the JSON writer (CPython's tie rules).
    let value = crate::pyjson::float_repr(f).to_ascii_lowercase();
    // `repr(1e17)` is `1e+17`, which is not a valid YAML 1.1 float.
    if !value.contains('.') && value.contains('e') {
        value.replacen('e', ".0e", 1)
    } else {
        value
    }
}

/// BaseResolver.resolve for a scalar with `implicit=(True, False)`, using the
/// implicit resolvers `resolver.py` registers on `Resolver` (and so on
/// `SafeLoader`), dispatched on the first character in registration order.
pub(super) fn resolve(value: &str) -> &'static str {
    type Matcher = fn(&[u8]) -> bool;
    let candidates: &[(&'static str, Matcher)] = match value.chars().next() {
        None => &[(TAG_NULL, is_null)],
        Some(first) => match first {
            'y' | 'Y' | 't' | 'T' | 'f' | 'F' | 'o' | 'O' => &[(TAG_BOOL, is_bool)],
            'n' | 'N' => &[(TAG_BOOL, is_bool), (TAG_NULL, is_null)],
            '-' | '+' => &[(TAG_FLOAT, is_float), (TAG_INT, is_int)],
            '.' => &[(TAG_FLOAT, is_float)],
            '0'..='9' => &[
                (TAG_FLOAT, is_float),
                (TAG_INT, is_int),
                (TAG_TIMESTAMP, is_timestamp),
            ],
            '<' => &[(TAG_MERGE, is_merge)],
            '~' => &[(TAG_NULL, is_null)],
            '=' => &[(TAG_VALUE, is_value)],
            '!' | '&' | '*' => &[(TAG_YAML, is_yaml)],
            _ => &[],
        },
    };
    let bytes = value.as_bytes();
    for (tag, matcher) in candidates {
        // `re.match(r'^(?:...)$')`: `$` also matches before a final newline.
        if matcher(bytes) || bytes.last() == Some(&b'\n') && matcher(&bytes[..bytes.len() - 1]) {
            return tag;
        }
    }
    TAG_STR
}

fn is_bool(s: &[u8]) -> bool {
    matches!(
        s,
        b"yes"
            | b"Yes"
            | b"YES"
            | b"no"
            | b"No"
            | b"NO"
            | b"true"
            | b"True"
            | b"TRUE"
            | b"false"
            | b"False"
            | b"FALSE"
            | b"on"
            | b"On"
            | b"ON"
            | b"off"
            | b"Off"
            | b"OFF"
    )
}

fn is_null(s: &[u8]) -> bool {
    // `re.X` drops the pattern's spaces, leaving an empty alternative.
    matches!(s, b"~" | b"null" | b"Null" | b"NULL" | b"")
}

fn is_merge(s: &[u8]) -> bool {
    s == b"<<"
}

fn is_value(s: &[u8]) -> bool {
    s == b"="
}

fn is_yaml(s: &[u8]) -> bool {
    matches!(s, b"!" | b"&" | b"*")
}

/// A forward-only cursor for the anchored resolver patterns. None of the
/// patterns needs backtracking: each greedy run stops at a character the
/// following token cannot start with.
struct Cursor<'a> {
    s: &'a [u8],
    pos: usize,
}

impl<'a> Cursor<'a> {
    fn new(s: &'a [u8]) -> Self {
        Self { s, pos: 0 }
    }

    fn peek(&self) -> Option<u8> {
        self.s.get(self.pos).copied()
    }

    fn at_end(&self) -> bool {
        self.pos == self.s.len()
    }

    fn eat(&mut self, pred: impl Fn(u8) -> bool) -> bool {
        match self.peek() {
            Some(c) if pred(c) => {
                self.pos += 1;
                true
            }
            _ => false,
        }
    }

    fn eat_byte(&mut self, b: u8) -> bool {
        self.eat(|c| c == b)
    }

    fn eat_str(&mut self, lit: &[u8]) -> bool {
        if self.s[self.pos..].starts_with(lit) {
            self.pos += lit.len();
            true
        } else {
            false
        }
    }

    fn eat_while(&mut self, pred: impl Fn(u8) -> bool) -> usize {
        let start = self.pos;
        while self.eat(&pred) {}
        self.pos - start
    }

    fn eat_sign(&mut self) {
        self.eat(|c| c == b'-' || c == b'+');
    }

    /// `(?::[0-5]?[0-9])+`
    fn eat_sexagesimal(&mut self) -> bool {
        let mut groups = 0;
        while self.peek() == Some(b':') {
            let save = self.pos;
            self.pos += 1;
            let two = matches!(
                (self.peek(), self.s.get(self.pos + 1)),
                (Some(b'0'..=b'5'), Some(b'0'..=b'9'))
            );
            if two {
                self.pos += 2;
            } else if !self.eat(is_digit) {
                self.pos = save;
                break;
            }
            groups += 1;
        }
        groups > 0
    }

    /// `(?:[eE][-+][0-9]+)?` followed by the end of input.
    fn eat_optional_exponent_then_end(&mut self) -> bool {
        if self.eat(|c| c == b'e' || c == b'E')
            && (!self.eat(|c| c == b'-' || c == b'+') || self.eat_while(is_digit) == 0)
        {
            return false;
        }
        self.at_end()
    }
}

fn is_digit(c: u8) -> bool {
    c.is_ascii_digit()
}

fn is_digit_or_underscore(c: u8) -> bool {
    c.is_ascii_digit() || c == b'_'
}

fn is_float(s: &[u8]) -> bool {
    // [-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?
    let alt1 = || {
        let mut c = Cursor::new(s);
        c.eat_sign();
        c.eat(is_digit)
            && {
                c.eat_while(is_digit_or_underscore);
                c.eat_byte(b'.')
            }
            && {
                c.eat_while(is_digit_or_underscore);
                c.eat_optional_exponent_then_end()
            }
    };
    // \.[0-9][0-9_]*(?:[eE][-+][0-9]+)?
    let alt2 = || {
        let mut c = Cursor::new(s);
        c.eat_byte(b'.') && c.eat(is_digit) && {
            c.eat_while(is_digit_or_underscore);
            c.eat_optional_exponent_then_end()
        }
    };
    // [-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*
    let alt3 = || {
        let mut c = Cursor::new(s);
        c.eat_sign();
        c.eat(is_digit)
            && {
                c.eat_while(is_digit_or_underscore);
                c.eat_sexagesimal()
            }
            && c.eat_byte(b'.')
            && {
                c.eat_while(is_digit_or_underscore);
                c.at_end()
            }
    };
    // [-+]?\.(?:inf|Inf|INF)
    let alt4 = || {
        let mut c = Cursor::new(s);
        c.eat_sign();
        c.eat_byte(b'.')
            && (c.eat_str(b"inf") || c.eat_str(b"Inf") || c.eat_str(b"INF"))
            && c.at_end()
    };
    // \.(?:nan|NaN|NAN)
    let alt5 = || matches!(s, b".nan" | b".NaN" | b".NAN");
    alt1() || alt2() || alt3() || alt4() || alt5()
}

fn is_int(s: &[u8]) -> bool {
    // [-+]?0b[0-1_]+
    let alt1 = || {
        let mut c = Cursor::new(s);
        c.eat_sign();
        c.eat_str(b"0b") && c.eat_while(|b| b == b'0' || b == b'1' || b == b'_') > 0 && c.at_end()
    };
    // [-+]?0[0-7_]+
    let alt2 = || {
        let mut c = Cursor::new(s);
        c.eat_sign();
        c.eat_byte(b'0') && c.eat_while(|b| matches!(b, b'0'..=b'7' | b'_')) > 0 && c.at_end()
    };
    // [-+]?(?:0|[1-9][0-9_]*)
    let alt3 = || {
        let mut c = Cursor::new(s);
        c.eat_sign();
        if c.eat_byte(b'0') {
            return c.at_end();
        }
        c.eat(|b| matches!(b, b'1'..=b'9')) && {
            c.eat_while(is_digit_or_underscore);
            c.at_end()
        }
    };
    // [-+]?0x[0-9a-fA-F_]+
    let alt4 = || {
        let mut c = Cursor::new(s);
        c.eat_sign();
        c.eat_str(b"0x") && c.eat_while(|b| b.is_ascii_hexdigit() || b == b'_') > 0 && c.at_end()
    };
    // [-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+
    let alt5 = || {
        let mut c = Cursor::new(s);
        c.eat_sign();
        c.eat(|b| matches!(b, b'1'..=b'9')) && {
            c.eat_while(is_digit_or_underscore);
            c.eat_sexagesimal() && c.at_end()
        }
    };
    alt1() || alt2() || alt3() || alt4() || alt5()
}

fn is_timestamp(s: &[u8]) -> bool {
    let four_digits = |c: &mut Cursor| (0..4).all(|_| c.eat(is_digit));
    let one_or_two_digits = |c: &mut Cursor| {
        let ok = c.eat(is_digit);
        c.eat(is_digit);
        ok
    };
    let two_digits = |c: &mut Cursor| c.eat(is_digit) && c.eat(is_digit);
    let is_blank = |b: u8| b == b' ' || b == b'\t';

    // [0-9]{4}-[0-9]{2}-[0-9]{2}
    let alt1 = || {
        let mut c = Cursor::new(s);
        four_digits(&mut c)
            && c.eat_byte(b'-')
            && two_digits(&mut c)
            && c.eat_byte(b'-')
            && two_digits(&mut c)
            && c.at_end()
    };
    // [0-9]{4}-[0-9][0-9]?-[0-9][0-9]?(?:[Tt]|[ \t]+)[0-9][0-9]?:[0-9]{2}:[0-9]{2}
    // (?:\.[0-9]*)?(?:[ \t]*(?:Z|[-+][0-9][0-9]?(?::[0-9][0-9])?))?
    let alt2 = || {
        let mut c = Cursor::new(s);
        let head = four_digits(&mut c)
            && c.eat_byte(b'-')
            && one_or_two_digits(&mut c)
            && c.eat_byte(b'-')
            && one_or_two_digits(&mut c)
            && (c.eat(|b| b == b'T' || b == b't') || c.eat_while(is_blank) > 0)
            && one_or_two_digits(&mut c)
            && c.eat_byte(b':')
            && two_digits(&mut c)
            && c.eat_byte(b':')
            && two_digits(&mut c);
        if !head {
            return false;
        }
        if c.eat_byte(b'.') {
            c.eat_while(is_digit);
        }
        if c.at_end() {
            return true;
        }
        // The optional zone group must consume the rest of the input.
        c.eat_while(is_blank);
        if c.eat_byte(b'Z') {
            return c.at_end();
        }
        if !c.eat(|b| b == b'-' || b == b'+') || !one_or_two_digits(&mut c) {
            return false;
        }
        if c.at_end() {
            return true;
        }
        c.eat_byte(b':') && two_digits(&mut c) && c.at_end()
    };
    alt1() || alt2()
}

// ---------------------------------------------------------------------------
// Emitter
// ---------------------------------------------------------------------------

const BEST_INDENT: usize = 2;
const BEST_WIDTH: usize = 80;

/// `'\n\x85  '`
fn is_break(ch: char) -> bool {
    matches!(ch, '\n' | '\u{85}' | '\u{2028}' | '\u{2029}')
}

/// `'\0 \t\r\n\x85  '`
fn is_blank_or_break(ch: char) -> bool {
    matches!(ch, '\0' | ' ' | '\t' | '\r') || is_break(ch)
}

/// The `expect_node` context flags.
#[derive(Clone, Copy, Default)]
struct Context {
    root: bool,
    mapping: bool,
    simple_key: bool,
}

impl Context {
    const ROOT: Self = Self {
        root: true,
        mapping: false,
        simple_key: false,
    };
    const SEQUENCE: Self = Self {
        root: false,
        mapping: false,
        simple_key: false,
    };
    const MAPPING: Self = Self {
        root: false,
        mapping: true,
        simple_key: false,
    };
    const SIMPLE_KEY: Self = Self {
        root: false,
        mapping: true,
        simple_key: true,
    };
}

/// Emitter.analyze_scalar's result (the `allow_flow_plain` and `allow_block`
/// fields are omitted: no flow scalar or block scalar style is reachable).
struct ScalarAnalysis {
    empty: bool,
    multiline: bool,
    allow_block_plain: bool,
    allow_single_quoted: bool,
}

#[derive(Clone, Copy, PartialEq)]
enum Style {
    Plain,
    SingleQuoted,
    DoubleQuoted,
}

struct Emitter {
    out: String,
    indents: Vec<Option<usize>>,
    indent: Option<usize>,
    context: Context,
    column: usize,
    whitespace: bool,
    indention: bool,
    open_ended: bool,
}

impl Emitter {
    fn new() -> Self {
        Self {
            out: String::new(),
            indents: Vec::new(),
            indent: None,
            context: Context::default(),
            column: 0,
            whitespace: true,
            indention: true,
            open_ended: false,
        }
    }

    fn increase_indent(&mut self, flow: bool, indentless: bool) {
        self.indents.push(self.indent);
        match self.indent {
            None => self.indent = Some(if flow { BEST_INDENT } else { 0 }),
            Some(indent) if !indentless => self.indent = Some(indent + BEST_INDENT),
            Some(_) => {}
        }
    }

    fn pop_indent(&mut self) {
        self.indent = self.indents.pop().expect("indent stack underflow");
    }

    // Node handlers.

    fn expect_node(&mut self, node: &Node, context: Context) {
        self.context = context;
        match node {
            Node::Seq(items) => {
                // Collection tags are always implicit, so process_tag is a no-op.
                if items.is_empty() {
                    self.expect_empty_flow_collection("[", "]");
                } else {
                    self.expect_block_sequence(items);
                }
            }
            Node::Map(pairs) => {
                if pairs.is_empty() {
                    self.expect_empty_flow_collection("{", "}");
                } else {
                    self.expect_block_mapping(pairs);
                }
            }
            _ => {
                let scalar = represent_scalar(node).expect("non-collection node is a scalar");
                let analysis = analyze_scalar(&scalar.text);
                let style = self.choose_scalar_style(&analysis, scalar.implicit.0);
                self.process_tag(&scalar, style);
                // expect_scalar
                self.increase_indent(true, false);
                self.process_scalar(&scalar.text, style);
                self.pop_indent();
            }
        }
    }

    /// expect_flow_sequence / expect_flow_mapping reaching their end event
    /// immediately.
    fn expect_empty_flow_collection(&mut self, open: &str, close: &str) {
        self.write_indicator(open, true, true, false);
        self.increase_indent(true, false);
        self.pop_indent();
        self.write_indicator(close, false, false, false);
    }

    fn expect_block_sequence(&mut self, items: &[Node]) {
        let indentless = self.context.mapping && !self.indention;
        self.increase_indent(false, indentless);
        for item in items {
            self.write_indent();
            self.write_indicator("-", true, false, true);
            self.expect_node(item, Context::SEQUENCE);
        }
        self.pop_indent();
    }

    fn expect_block_mapping(&mut self, pairs: &[(Node, Node)]) {
        self.increase_indent(false, false);
        for (key, value) in pairs {
            self.write_indent();
            if check_simple_key(key) {
                self.expect_node(key, Context::SIMPLE_KEY);
                self.write_indicator(":", false, false, false);
            } else {
                self.write_indicator("?", true, false, true);
                self.expect_node(key, Context::MAPPING);
                self.write_indent();
                self.write_indicator(":", true, false, true);
            }
            self.expect_node(value, Context::MAPPING);
        }
        self.pop_indent();
    }

    // Tag and scalar processors.

    fn process_tag(&mut self, scalar: &Scalar, style: Style) {
        if (style == Style::Plain && scalar.implicit.0)
            || (style != Style::Plain && scalar.implicit.1)
        {
            return;
        }
        // Reached only by a non-str scalar whose text cannot be plain.
        let tag = prepare_tag(scalar.tag);
        self.write_indicator(&tag, true, false, false);
    }

    fn choose_scalar_style(&self, analysis: &ScalarAnalysis, implicit_plain: bool) -> Style {
        let simple_key = self.context.simple_key;
        if implicit_plain
            && !(simple_key && (analysis.empty || analysis.multiline))
            && analysis.allow_block_plain
        {
            return Style::Plain;
        }
        if analysis.allow_single_quoted && !(simple_key && analysis.multiline) {
            return Style::SingleQuoted;
        }
        Style::DoubleQuoted
    }

    fn process_scalar(&mut self, text: &[char], style: Style) {
        let split = !self.context.simple_key;
        match style {
            Style::DoubleQuoted => self.write_double_quoted(text, split),
            Style::SingleQuoted => self.write_single_quoted(text, split),
            Style::Plain => self.write_plain(text, split),
        }
    }

    // Writers.

    fn write_raw(&mut self, text: &[char]) {
        self.column += text.len();
        self.out.extend(text);
    }

    fn write_raw_str(&mut self, text: &str) {
        self.column += text.chars().count();
        self.out.push_str(text);
    }

    fn write_indicator(
        &mut self,
        indicator: &str,
        need_whitespace: bool,
        whitespace: bool,
        indention: bool,
    ) {
        if !self.whitespace && need_whitespace {
            self.out.push(' ');
            self.column += 1;
        }
        self.out.push_str(indicator);
        self.column += indicator.chars().count();
        self.whitespace = whitespace;
        self.indention = self.indention && indention;
        self.open_ended = false;
    }

    fn write_indent(&mut self) {
        let indent = self.indent.unwrap_or(0);
        if !self.indention || self.column > indent || (self.column == indent && !self.whitespace) {
            self.write_line_break(None);
        }
        if self.column < indent {
            self.whitespace = true;
            self.out
                .extend(std::iter::repeat_n(' ', indent - self.column));
            self.column = indent;
        }
    }

    fn write_line_break(&mut self, data: Option<char>) {
        self.out.push(data.unwrap_or('\n'));
        self.whitespace = true;
        self.indention = true;
        self.column = 0;
    }

    /// Writes the line breaks in `text[start..end]` the way the quoted and
    /// plain writers do: an extra break before a leading `\n`, then each break.
    fn write_breaks(&mut self, text: &[char], start: usize, end: usize) {
        if text[start] == '\n' {
            self.write_line_break(None);
        }
        for &br in &text[start..end] {
            if br == '\n' {
                self.write_line_break(None);
            } else {
                self.write_line_break(Some(br));
            }
        }
    }

    fn write_single_quoted(&mut self, text: &[char], split: bool) {
        self.write_indicator("'", true, false, false);
        let len = text.len();
        let mut spaces = false;
        let mut breaks = false;
        let mut start = 0;
        let mut end = 0;
        while end <= len {
            let ch = text.get(end).copied();
            if spaces {
                if ch != Some(' ') {
                    if start + 1 == end
                        && self.column > BEST_WIDTH
                        && split
                        && start != 0
                        && end != len
                    {
                        self.write_indent();
                    } else {
                        self.write_raw(&text[start..end]);
                    }
                    start = end;
                }
            } else if breaks {
                if !ch.is_some_and(is_break) {
                    self.write_breaks(text, start, end);
                    self.write_indent();
                    start = end;
                }
            } else if ch.is_none_or(|c| c == ' ' || c == '\'' || is_break(c)) && start < end {
                self.write_raw(&text[start..end]);
                start = end;
            }
            if ch == Some('\'') {
                self.write_raw_str("''");
                start = end + 1;
            }
            if let Some(c) = ch {
                spaces = c == ' ';
                breaks = is_break(c);
            }
            end += 1;
        }
        self.write_indicator("'", false, false, false);
    }

    fn write_double_quoted(&mut self, text: &[char], split: bool) {
        self.write_indicator("\"", true, false, false);
        let len = text.len();
        let mut start = 0;
        let mut end = 0;
        while end <= len {
            let ch = text.get(end).copied();
            // allow_unicode=False: everything outside printable ASCII is escaped.
            let escape = ch.is_none_or(|c| {
                matches!(
                    c,
                    '"' | '\\' | '\u{85}' | '\u{2028}' | '\u{2029}' | '\u{FEFF}'
                ) || !(' '..='~').contains(&c)
            });
            if escape {
                if start < end {
                    self.write_raw(&text[start..end]);
                    start = end;
                }
                if let Some(c) = ch {
                    self.write_raw_str(&escape_char(c));
                    start = end + 1;
                }
            }
            if 0 < end
                && end + 1 < len
                && (ch == Some(' ') || start >= end)
                // Python's `end - start` is -1 right after an escape.
                && self.column as isize + (end as isize - start as isize) > BEST_WIDTH as isize
                && split
            {
                if start < end {
                    self.write_raw(&text[start..end]);
                    start = end;
                }
                self.write_raw_str("\\");
                self.write_indent();
                self.whitespace = false;
                self.indention = false;
                if text[start] == ' ' {
                    self.write_raw_str("\\");
                }
            }
            end += 1;
        }
        self.write_indicator("\"", false, false, false);
    }

    fn write_plain(&mut self, text: &[char], split: bool) {
        if self.context.root {
            self.open_ended = true;
        }
        if text.is_empty() {
            return;
        }
        if !self.whitespace {
            self.write_raw_str(" ");
        }
        self.whitespace = false;
        self.indention = false;
        let len = text.len();
        let mut spaces = false;
        let mut breaks = false;
        let mut start = 0;
        let mut end = 0;
        while end <= len {
            let ch = text.get(end).copied();
            if spaces {
                if ch != Some(' ') {
                    if start + 1 == end && self.column > BEST_WIDTH && split {
                        self.write_indent();
                        self.whitespace = false;
                        self.indention = false;
                    } else {
                        self.write_raw(&text[start..end]);
                    }
                    start = end;
                }
            } else if breaks {
                if !ch.is_some_and(is_break) {
                    self.write_breaks(text, start, end);
                    self.write_indent();
                    self.whitespace = false;
                    self.indention = false;
                    start = end;
                }
            } else if ch.is_none_or(|c| c == ' ' || is_break(c)) {
                self.write_raw(&text[start..end]);
                start = end;
            }
            if let Some(c) = ch {
                spaces = c == ' ';
                breaks = is_break(c);
            }
            end += 1;
        }
    }
}

/// Emitter.check_simple_key for a mapping key. The serializer always sets a
/// tag, so its `!!` form counts toward the 128-character limit.
fn check_simple_key(key: &Node) -> bool {
    let mut length = prepare_tag(node_tag(key)).len();
    let simple = match key {
        Node::Seq(items) => items.is_empty(),
        Node::Map(pairs) => pairs.is_empty(),
        _ => {
            let scalar = represent_scalar(key).expect("non-collection node is a scalar");
            length += scalar.text.len();
            let analysis = analyze_scalar(&scalar.text);
            !analysis.empty && !analysis.multiline
        }
    };
    length < 128 && simple
}

/// Emitter.prepare_tag for the `tag:yaml.org,2002:` tags, whose suffixes
/// need no escaping.
fn prepare_tag(tag: &str) -> String {
    let suffix = tag.strip_prefix(TAG_PREFIX).expect("standard tag");
    format!("!!{suffix}")
}

/// Emitter.ESCAPE_REPLACEMENTS, then `\xXX`, `\uXXXX` or `\UXXXXXXXX`.
fn escape_char(ch: char) -> String {
    let short = match ch {
        '\0' => '0',
        '\x07' => 'a',
        '\x08' => 'b',
        '\t' => 't',
        '\n' => 'n',
        '\x0B' => 'v',
        '\x0C' => 'f',
        '\r' => 'r',
        '\x1B' => 'e',
        '"' => '"',
        '\\' => '\\',
        '\u{85}' => 'N',
        '\u{A0}' => '_',
        '\u{2028}' => 'L',
        '\u{2029}' => 'P',
        _ => {
            let code = ch as u32;
            return if code <= 0xFF {
                format!("\\x{code:02X}")
            } else if code <= 0xFFFF {
                format!("\\u{code:04X}")
            } else {
                format!("\\U{code:08X}")
            };
        }
    };
    format!("\\{short}")
}

/// Emitter.analyze_scalar with `allow_unicode=False`.
fn analyze_scalar(scalar: &[char]) -> ScalarAnalysis {
    if scalar.is_empty() {
        return ScalarAnalysis {
            empty: true,
            multiline: false,
            allow_block_plain: true,
            allow_single_quoted: true,
        };
    }

    let mut block_indicators = false;
    let mut line_breaks = false;
    let mut special_characters = false;

    let mut leading_space = false;
    let mut leading_break = false;
    let mut trailing_space = false;
    let mut trailing_break = false;
    let mut break_space = false;
    let mut space_break = false;

    if scalar.starts_with(&['-', '-', '-']) || scalar.starts_with(&['.', '.', '.']) {
        block_indicators = true;
    }

    let len = scalar.len();
    let mut preceded_by_whitespace = true;
    let mut followed_by_whitespace = len == 1 || is_blank_or_break(scalar[1]);
    let mut previous_space = false;
    let mut previous_break = false;

    for (index, &ch) in scalar.iter().enumerate() {
        if index == 0 {
            if "#,[]{}&*!|>'\"%@`".contains(ch) {
                block_indicators = true;
            }
            if (ch == '?' || ch == ':') && followed_by_whitespace {
                block_indicators = true;
            }
            if ch == '-' && followed_by_whitespace {
                block_indicators = true;
            }
        } else {
            if ch == ':' && followed_by_whitespace {
                block_indicators = true;
            }
            if ch == '#' && preceded_by_whitespace {
                block_indicators = true;
            }
        }

        if is_break(ch) {
            line_breaks = true;
        }
        // With allow_unicode=False every non-printable or non-ASCII character
        // is special.
        if !(ch == '\n' || (' '..='~').contains(&ch)) {
            special_characters = true;
        }

        if ch == ' ' {
            if index == 0 {
                leading_space = true;
            }
            if index == len - 1 {
                trailing_space = true;
            }
            if previous_break {
                break_space = true;
            }
            previous_space = true;
            previous_break = false;
        } else if is_break(ch) {
            if index == 0 {
                leading_break = true;
            }
            if index == len - 1 {
                trailing_break = true;
            }
            if previous_space {
                space_break = true;
            }
            previous_space = false;
            previous_break = true;
        } else {
            previous_space = false;
            previous_break = false;
        }

        preceded_by_whitespace = is_blank_or_break(ch);
        followed_by_whitespace = index + 2 >= len || is_blank_or_break(scalar[index + 2]);
    }

    let mut allow_block_plain = true;
    let mut allow_single_quoted = true;
    if leading_space || leading_break || trailing_space || trailing_break {
        allow_block_plain = false;
    }
    if break_space {
        allow_block_plain = false;
        allow_single_quoted = false;
    }
    if space_break || special_characters {
        allow_block_plain = false;
        allow_single_quoted = false;
    }
    if line_breaks || block_indicators {
        allow_block_plain = false;
    }

    ScalarAnalysis {
        empty: false,
        multiline: line_breaks,
        allow_block_plain,
        allow_single_quoted,
    }
}

#[cfg(test)]
#[path = "yaml_dump_tests.rs"]
mod tests;
