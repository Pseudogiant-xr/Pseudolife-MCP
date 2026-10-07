"""Length normalization changes only exact JSON tokens at authorized value paths."""
import json
import re

from .harness import Policy, _matches, _segment, strict_json_loads


def spans(text):
    """Parse once without losing whitespace, Unicode, escapes or duplicate keys."""
    strict_json_loads(text)
    decoder, found = json.JSONDecoder(), {}
    def value(index, path):
        while text[index].isspace(): index += 1
        start = index
        if text[index] == '{':
            index += 1
            while True:
                while text[index].isspace(): index += 1
                if text[index] == '}': index += 1; break
                key, index = decoder.raw_decode(text, index)
                while text[index].isspace(): index += 1
                if text[index] != ':': raise ValueError("invalid object separator")
                index = value(index + 1, path + (key,))
                while text[index].isspace(): index += 1
                if text[index] == '}': index += 1; break
                if text[index] != ',': raise ValueError("invalid object separator")
                index += 1
        elif text[index] == '[':
            index, ordinal = index + 1, 0
            while True:
                while text[index].isspace(): index += 1
                if text[index] == ']': index += 1; break
                index = value(index, path + (ordinal,))
                ordinal += 1
                while text[index].isspace(): index += 1
                if text[index] == ']': index += 1; break
                if text[index] != ',': raise ValueError("invalid array separator")
                index += 1
        else:
            _, index = decoder.raw_decode(text, index)
        found[path] = (start, index)
        return index
    end = value(0, ())
    if text[end:].strip(): raise ValueError("extra JSON data")
    return found


def string_boundaries(token):
    """Map decoded string character boundaries to its original escaped token."""
    boundaries, index = [1], 1
    while index < len(token) - 1:
        if token[index] == '\\':
            if token[index + 1] == 'u':
                unit = int(token[index + 2:index + 6], 16)
                index += 6
                if 0xD800 <= unit <= 0xDBFF:
                    if token[index:index + 2] != '\\u': raise ValueError("invalid surrogate")
                    index += 6
            else:
                index += 2
        else:
            index += 1
        boundaries.append(index)
    if len(boundaries) != len(json.loads(token)) + 1:
        raise ValueError("escaped string mapping failed")
    return boundaries


def replacements(text, original, normalized, *, source_text_paths=Policy().source_text_paths):
    # `normalized` is the trusted result of the operation's declared Normalizer,
    # never candidate-supplied replacement instructions or a rewritten body.
    locations, changes = spans(text), []
    def visit(old, new, path):
        if isinstance(old, dict) and isinstance(new, dict) and old.keys() == new.keys():
            for key in old: visit(old[key], new[key], path + (key,))
        elif isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
            for index, (left, right) in enumerate(zip(old, new)): visit(left, right, path + (index,))
        elif (isinstance(old, str) and isinstance(new, str) and
              _matches('/body/' + '/'.join(_segment(str(p)) for p in path), source_text_paths)):
            canonical = lambda value: value.replace('\r\n', '\n').replace('\r', '\n')
            if canonical(old) != canonical(new):
                raise ValueError("length adjustment outside authorized normalization")
            start, end = locations[path]
            boundaries = string_boundaries(text[start:end])
            # Only newline escapes are canonicalized; other source bytes,
            # including Unicode escapes and surrounding whitespace, survive.
            for match in re.finditer(r'\r\n|\r|\n', old):
                changes.append((start + boundaries[match.start()],
                                start + boundaries[match.end()], '\\n'))
        elif type(old) is type(new) and old == new:
            return
        elif path == ('name',) and isinstance(old, str) and isinstance(new, str):
            from .registration_policy import name_replacement
            suffix_start, replacement = name_replacement(original, normalized)
            start, end = locations[path]
            boundaries = string_boundaries(text[start:end])
            changes.append((start + boundaries[suffix_start], start + boundaries[len(old)],
                            json.dumps(replacement, ensure_ascii=False)[1:-1]))
        elif (isinstance(old, str) and isinstance(new, str) and len(path) >= 3
              and path[-1] == 'text' and path[-3] == 'content'):
            # JSON text is itself escaped inside the MCP envelope: adjust only
            # the inner authorized spans, retaining every outer escape elsewhere.
            start, end = locations[path]
            token, boundaries = text[start:end], string_boundaries(text[start:end])
            for left, right, replacement in replacements(old, strict_json_loads(old), strict_json_loads(new),
                                                         source_text_paths=()):
                escaped = json.dumps(replacement, ensure_ascii=False)[1:-1]
                changes.append((start + boundaries[left], start + boundaries[right], escaped))
        elif isinstance(new, str) and re.fullmatch(r'<[^>]+>(?::\d+)?', new):
            start, end = locations[path]
            changes.append((start, end, json.dumps(new, ensure_ascii=False)))
        else:
            raise ValueError("length adjustment outside authorized normalization")
    visit(original, normalized, ())
    validate_disjoint(changes)
    return changes


def validate_disjoint(changes):
    previous = -1
    for start, end, _ in sorted(changes):
        if start < previous or end <= start:
            raise ValueError("duplicate or overlapping wire normalization spans")
        previous = end


def normalize_content_length(response, raw, original):
    declared = response.get('headers', {}).get('content-length')
    if declared is None: return
    if not re.fullmatch(r'\d+', declared) or int(declared) != len(raw):
        raise ValueError("declared Content-Length differs from entity bytes")
    text = raw.decode('utf-8')
    # SSE framing remains byte exact except authorized JSON token spans within
    # individual data envelopes. No serializer delta is taken for the envelope.
    if 'text/event-stream' in response['headers'].get('content-type', ''):
        offset, changes = 0, []
        for line in text.splitlines(keepends=True):
            if line.startswith('data:'):
                prefix = len(line) - len(line[5:].lstrip(' '))
                payload = line[prefix:].rstrip('\r\n')
                changes.extend((offset + prefix + a, offset + prefix + b, replacement)
                               for a, b, replacement in replacements(payload, strict_json_loads(payload), response['body']))
            offset += len(line)
    elif 'application/json' in response['headers'].get('content-type', '') and raw:
        changes = replacements(text, original, response['body'])
    else:
        changes = []
    validate_disjoint(changes)
    adjustment = sum(len(replacement.encode('utf-8')) - len(text[start:end].encode('utf-8'))
                     for start, end, replacement in changes)
    compared = int(declared) + adjustment
    response['content_length'] = {'raw': int(declared), 'adjustment': adjustment, 'compared': compared}
    response['headers']['content-length'] = str(compared)
