//! Python-oracle origin validation. The returned authority keeps its spelling.
use std::net::{IpAddr, Ipv6Addr};
use unicode_normalization::UnicodeNormalization;

pub const DEFAULT_URL: &str = "http://127.0.0.1:8765";
pub const INVALID_URL_MESSAGE: &str = "[shim] invalid PSEUDOLIFE_MCP_DAEMON_URL; use an http(s) origin without credentials, a path, query, or fragment.";

pub fn from_environment() -> Result<String, &'static str> {
    validate(&std::env::var("PSEUDOLIFE_MCP_DAEMON_URL").unwrap_or_else(|_| DEFAULT_URL.to_owned()))
}

pub fn validate(value: &str) -> Result<String, &'static str> {
    let invalid = || INVALID_URL_MESSAGE;
    if value.chars().any(|c| c.is_whitespace() || c < '\u{20}') {
        return Err(invalid());
    }
    let (scheme, remainder) = value.split_once("://").ok_or_else(invalid)?;
    let scheme = scheme.to_ascii_lowercase();
    if !matches!(scheme.as_str(), "http" | "https") {
        return Err(invalid());
    }
    let end = remainder.find(['/', '?', '#']).unwrap_or(remainder.len());
    let authority = &remainder[..end];
    let suffix = &remainder[end..];
    // Empty query/fragment delimiters are accepted by urllib.parse.urlsplit.
    let (without_fragment, fragment) = suffix.split_once('#').unwrap_or((suffix, ""));
    let (path, query) = without_fragment
        .split_once('?')
        .unwrap_or((without_fragment, ""));
    if !matches!(path, "" | "/")
        || !query.is_empty()
        || !fragment.is_empty()
        || authority.contains('@')
    {
        return Err(invalid());
    }
    if !authority.is_ascii() {
        let candidate = authority.replace([':', '?', '#'], "");
        let normalized: String = candidate.nfkc().collect();
        if normalized != candidate && normalized.contains(['/', '?', '#', '@', ':']) {
            return Err(invalid());
        }
    }
    let (hostname, port) = host_port(authority).ok_or_else(invalid)?;
    if hostname.is_empty()
        || port.is_some_and(|p| {
            !p.is_empty() && (!p.bytes().all(|c| c.is_ascii_digit()) || p.parse::<u16>().is_err())
        })
    {
        return Err(invalid());
    }
    Ok(format!("{scheme}://{authority}"))
}

fn host_port(authority: &str) -> Option<(&str, Option<&str>)> {
    if authority.contains('[') || authority.contains(']') {
        let (_, rest) = authority.split_once('[')?;
        let (host, rest) = rest.split_once(']')?;
        if let Some(future) = host.strip_prefix('v') {
            let (version, address) = future.split_once('.')?;
            if version.is_empty()
                || !version.bytes().all(|c| c.is_ascii_hexdigit())
                || address.is_empty()
            {
                return None;
            }
        } else {
            // ipaddress accepts an IPv6 scope identifier, but never bracketed IPv4.
            let bare = host.split_once('%').map_or(host, |(ip, _)| ip);
            bare.parse::<Ipv6Addr>().ok()?;
            if host.ends_with('%') || host.matches('%').count() > 1 {
                return None;
            }
        }
        let port = rest.split_once(':').map(|(_, port)| port);
        Some((host, port))
    } else {
        Some(
            authority
                .split_once(':')
                .map_or((authority, None), |(host, port)| (host, Some(port))),
        )
    }
}

pub fn is_loopback(value: &str) -> bool {
    let authority = value
        .split_once("://")
        .map_or("", |(_, rest)| rest.split('/').next().unwrap_or(""));
    let Some((host, _)) = host_port(authority) else {
        return false;
    };
    if host.eq_ignore_ascii_case("localhost") {
        return true;
    }
    let bare = host.split_once('%').map_or(host, |(ip, _)| ip);
    match bare.parse::<IpAddr>() {
        Ok(IpAddr::V4(ip)) => ip.is_loopback(),
        Ok(IpAddr::V6(ip)) => {
            ip.is_loopback() || ip.to_ipv4_mapped().is_some_and(|ip| ip.is_loopback())
        }
        Err(_) => false,
    }
}
