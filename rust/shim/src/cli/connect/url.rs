//! `urllib.parse.urlsplit` (CPython 3.11) for the printable-ASCII domain, and
//! connect's three uses of it: the target's validation
//! (`codex_connection._validated_daemon_url`), `_shown_url` and
//! `_is_loopback_url`. Anything outside that domain defers.
use super::Defer;
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};

pub(super) struct Split {
    pub scheme: String,
    pub netloc: String,
    pub path: String,
    pub query: String,
    pub fragment: String,
}

const SCHEME_CHARS: &str = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+-.";

/// `Ok(Err(()))` is Python's ValueError.
pub(super) fn urlsplit(url: &str) -> Result<Result<Split, ()>, Defer> {
    if !url.chars().all(|c| ('!'..='~').contains(&c)) {
        return Err(Defer);
    }
    let mut scheme = String::new();
    let mut rest = url;
    if let Some(i) = url.find(':')
        && i > 0
        && url.as_bytes()[0].is_ascii_alphabetic()
        && url[..i].chars().all(|c| SCHEME_CHARS.contains(c))
    {
        scheme = url[..i].to_ascii_lowercase();
        rest = &url[i + 1..];
    }
    let mut netloc = String::new();
    if let Some(after) = rest.strip_prefix("//") {
        let delim = after.find(['/', '?', '#']).unwrap_or(after.len());
        netloc = after[..delim].to_owned();
        rest = &after[delim..];
        let open = netloc.contains('[');
        let close = netloc.contains(']');
        if open != close {
            return Ok(Err(()));
        }
        if open {
            let bracketed = netloc
                .split_once('[')
                .map_or("", |(_, b)| b.split_once(']').map_or(b, |(h, _)| h));
            if bracketed.starts_with('v') || bracketed.contains('%') {
                return Err(Defer);
            }
            if bracketed.parse::<Ipv6Addr>().is_err() {
                if bracketed.parse::<Ipv4Addr>().is_ok() {
                    return Ok(Err(()));
                }
                return Err(Defer);
            }
        }
    }
    let (rest, fragment) = rest.split_once('#').unwrap_or((rest, ""));
    let (path, query) = rest.split_once('?').unwrap_or((rest, ""));
    Ok(Ok(Split {
        scheme,
        netloc,
        path: path.to_owned(),
        query: query.to_owned(),
        fragment: fragment.to_owned(),
    }))
}

impl Split {
    fn hostinfo(&self) -> (&str, Option<&str>) {
        let hostinfo = self
            .netloc
            .rsplit_once('@')
            .map_or(self.netloc.as_str(), |(_, h)| h);
        let (hostname, port) = match hostinfo.split_once('[') {
            Some((_, bracketed)) => {
                let (hostname, port) = bracketed.split_once(']').unwrap_or((bracketed, ""));
                (hostname, port.split_once(':').map_or("", |(_, p)| p))
            }
            None => hostinfo.split_once(':').unwrap_or((hostinfo, "")),
        };
        (hostname, (!port.is_empty()).then_some(port))
    }

    pub(super) fn hostname(&self) -> Option<String> {
        let (hostname, _) = self.hostinfo();
        // A zone after `%` keeps its case.
        (!hostname.is_empty()).then(|| match hostname.split_once('%') {
            Some((host, zone)) => format!("{}%{zone}", host.to_ascii_lowercase()),
            None => hostname.to_ascii_lowercase(),
        })
    }

    /// `Err(())` is Python's ValueError from `.port`.
    pub(super) fn port(&self) -> Result<Option<u32>, ()> {
        match self.hostinfo().1 {
            None => Ok(None),
            Some(port) => {
                if !port.bytes().all(|b| b.is_ascii_digit()) {
                    return Err(());
                }
                let digits = port.trim_start_matches('0');
                if digits.len() > 5 {
                    return Err(());
                }
                let value: u32 = if digits.is_empty() {
                    0
                } else {
                    digits.parse().map_err(|_| ())?
                };
                if value > 65535 {
                    return Err(());
                }
                Ok(Some(value))
            }
        }
    }

    fn userinfo(&self) -> Option<(&str, Option<&str>)> {
        self.netloc
            .rsplit_once('@')
            .map(|(userinfo, _)| match userinfo.split_once(':') {
                Some((user, password)) => (user, Some(password)),
                None => (userinfo, None),
            })
    }
}

/// `codex_connection._validated_daemon_url`: `Ok(Err(()))` is SetupError.
pub(super) fn validated(url: &str) -> Result<Result<String, ()>, Defer> {
    let parsed = match urlsplit(url)? {
        Ok(parsed) => parsed,
        Err(()) => return Ok(Err(())),
    };
    if parsed.port().is_err() {
        return Ok(Err(()));
    }
    let credentials = parsed.userinfo().is_some_and(|(user, password)| {
        !user.is_empty() || password.is_some_and(|p| !p.is_empty())
    });
    if !matches!(parsed.scheme.as_str(), "http" | "https")
        || parsed.hostname().is_none()
        || credentials
        || !parsed.query.is_empty()
        || !parsed.fragment.is_empty()
        || !matches!(parsed.path.as_str(), "" | "/")
    {
        return Ok(Err(()));
    }
    Ok(Ok(format!(
        "{}://{}",
        parsed.scheme,
        parsed.netloc.trim_end_matches('/')
    )))
}

/// `_shown_url`: scheme and host[:port] only.
pub(super) fn shown(value: &str) -> Result<String, Defer> {
    const UNREADABLE: &str = "<unreadable URL>";
    let parsed = match urlsplit(value)? {
        Ok(parsed) => parsed,
        Err(()) => return Ok(UNREADABLE.to_owned()),
    };
    let Ok(port) = parsed.port() else {
        return Ok(UNREADABLE.to_owned());
    };
    let Some(host) = parsed.hostname() else {
        return Ok(UNREADABLE.to_owned());
    };
    if parsed.scheme.is_empty() {
        return Ok(UNREADABLE.to_owned());
    }
    let host = if host.contains(':') {
        format!("[{host}]")
    } else {
        host
    };
    Ok(match port {
        Some(port) if port != 0 => format!("{}://{host}:{port}", parsed.scheme),
        _ => format!("{}://{host}", parsed.scheme),
    })
}

/// `daemon_url._is_loopback_url` for an already validated URL.
pub(super) fn is_loopback(url: &str) -> Result<bool, Defer> {
    let parsed = urlsplit(url)?.map_err(|()| Defer)?;
    let host = parsed.hostname().unwrap_or_default();
    if host == "localhost" {
        return Ok(true);
    }
    Ok(match host.parse::<IpAddr>() {
        Ok(IpAddr::V4(ip)) => ip.is_loopback(),
        Ok(IpAddr::V6(ip)) => {
            ip.is_loopback() || ip.to_ipv4_mapped().is_some_and(|ip| ip.is_loopback())
        }
        Err(_) => false,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn validation_and_display_follow_urlsplit() {
        assert_eq!(
            validated("http://100.64.0.2:8765/").unwrap(),
            Ok("http://100.64.0.2:8765".into())
        );
        assert_eq!(validated("http://h:8765/mcp").unwrap(), Err(()));
        assert_eq!(validated("ftp://h").unwrap(), Err(()));
        assert_eq!(validated("http://h:99999").unwrap(), Err(()));
        assert_eq!(
            validated("http://[::1]:8765").unwrap(),
            Ok("http://[::1]:8765".into())
        );
        assert_eq!(
            shown("http://User:pw@Host.Example:8765/x?y").unwrap(),
            "http://host.example:8765"
        );
        assert_eq!(shown("100.64.0.2:8765").unwrap(), "<unreadable URL>");
        assert_eq!(shown("localhost:8765").unwrap(), "<unreadable URL>");
        assert_eq!(shown("http://h:0").unwrap(), "http://h");
        assert_eq!(shown("http://[::1]:1").unwrap(), "http://[::1]:1");
        assert!(shown("http://é").is_err());
        assert!(is_loopback("http://127.0.0.1:1").unwrap());
        assert!(!is_loopback("http://127.1:1").unwrap());
        assert!(is_loopback("http://LOCALHOST:1").unwrap());
        assert!(is_loopback("http://[::ffff:127.0.0.1]:1").unwrap());
    }
}
