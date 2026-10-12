//! Runtime identity fields from the pinned Python CLI and side-by-side layout.
use serde_json::Value;
use std::{
    fs,
    path::{Path, PathBuf},
};

fn env_path(name: &str) -> Option<PathBuf> {
    std::env::var_os(name)
        .filter(|value| !value.is_empty())
        .map(|value| PathBuf::from(value).components().collect())
}

fn runtime_root() -> Option<PathBuf> {
    if let (Some(root), Some(launcher)) = (
        env_path("PSEUDOLIFE_SHIM_RUNTIMES"),
        env_path("PSEUDOLIFE_SHIM_LAUNCHER"),
    ) {
        if launcher
            .extension()
            .is_some_and(|extension| extension.eq_ignore_ascii_case("exe"))
            != cfg!(windows)
        {
            return None;
        }
        return Some(root.components().collect());
    }
    let home = env_path("USERPROFILE")
        .or_else(|| env_path("HOME"))
        .or_else(dirs::home_dir)?;
    let data = if cfg!(windows) {
        env_path("LOCALAPPDATA").unwrap_or_else(|| home.join("AppData").join("Local"))
    } else {
        env_path("XDG_DATA_HOME").unwrap_or_else(|| home.join(".local").join("share"))
    };
    Some(data.join("pseudolife-mcp").join("runtimes"))
}

fn runtime_sequence(name: &str) -> Option<u32> {
    // CPython 3.11 / Unicode 14: runtimes.py's six Unicode decimal digits.
    const ZEROS: &[u32] = &[
        0x30, 0x660, 0x6f0, 0x7c0, 0x966, 0x9e6, 0xa66, 0xae6, 0xb66, 0xbe6, 0xc66, 0xce6, 0xd66,
        0xde6, 0xe50, 0xed0, 0xf20, 0x1040, 0x1090, 0x17e0, 0x1810, 0x1946, 0x19d0, 0x1a80, 0x1a90,
        0x1b50, 0x1bb0, 0x1c40, 0x1c50, 0xa620, 0xa8d0, 0xa900, 0xa9d0, 0xa9f0, 0xaa50, 0xabf0,
        0xff10, 0x104a0, 0x10d30, 0x11066, 0x110f0, 0x11136, 0x111d0, 0x112f0, 0x11450, 0x114d0,
        0x11650, 0x116c0, 0x11730, 0x118e0, 0x11950, 0x11c50, 0x11d50, 0x11da0, 0x16a60, 0x16ac0,
        0x16b50, 0x1d7ce, 0x1d7d8, 0x1d7e2, 0x1d7ec, 0x1d7f6, 0x1e140, 0x1e2f0, 0x1e950, 0x1fbf0,
    ];
    // Python's regex $ also matches immediately before one final newline.
    let name = name.strip_suffix('\n').unwrap_or(name);
    if name.chars().count() != 6 {
        return None;
    }
    name.chars().try_fold(0, |sequence, c| {
        let digit = ZEROS
            .iter()
            .find_map(|zero| (c as u32).checked_sub(*zero).filter(|n| *n < 10))?;
        Some(sequence * 10 + digit)
    })
}

fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(value) => value.as_f64().is_none_or(|value| value != 0.0),
        Value::String(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
        Value::Object(value) => !value.is_empty(),
    }
}

pub(super) fn python_text(value: &Value, nested: bool) -> String {
    match value {
        Value::Null => "None".into(),
        Value::Bool(value) => if *value { "True" } else { "False" }.into(),
        Value::String(value) => {
            if nested {
                super::mode_repr(value)
            } else {
                value.clone()
            }
        }
        Value::Number(value) => {
            let raw = value.to_string();
            if !raw.contains(['.', 'e', 'E']) {
                return raw;
            }
            value
                .as_f64()
                .map(|value| {
                    crate::float_repr::finite_float(value).unwrap_or_else(|_| {
                        if value.is_sign_negative() {
                            "-inf"
                        } else {
                            "inf"
                        }
                        .into()
                    })
                })
                .unwrap_or(raw)
        }
        Value::Array(values) => format!(
            "[{}]",
            values
                .iter()
                .map(|value| python_text(value, true))
                .collect::<Vec<_>>()
                .join(", ")
        ),
        Value::Object(values) => format!(
            "{{{}}}",
            values
                .iter()
                .map(|(key, value)| format!(
                    "{}: {}",
                    super::mode_repr(key),
                    python_text(value, true)
                ))
                .collect::<Vec<_>>()
                .join(", ")
        ),
    }
}

fn runtime_line() -> Option<String> {
    let executable = std::env::current_exe().ok()?;
    if !executable.is_file()
        || executable.parent()?.file_name()? != if cfg!(windows) { "Scripts" } else { "bin" }
    {
        return None;
    }
    let directory = fs::canonicalize(executable.parent()?.parent()?).ok()?;
    runtime_line_at(&runtime_root()?, &directory)
}

fn runtime_line_at(root: &Path, directory: &Path) -> Option<String> {
    runtime_line_from_paths(
        fs::read_dir(root)
            .ok()?
            .flatten()
            .map(|entry| entry.path())
            .collect(),
        |path| path == directory || fs::canonicalize(path).ok().as_deref() == Some(directory),
    )
}

fn runtime_line_from_paths(
    paths: Vec<PathBuf>,
    same_path: impl Fn(&Path) -> bool,
) -> Option<String> {
    let mut candidates: Vec<_> = paths
        .into_iter()
        .filter_map(|path| {
            let sequence = runtime_sequence(path.file_name()?.to_str()?)?;
            Some((sequence, path))
        })
        .collect();
    // Python list_runtimes sorts stably by sequence: numeric ties retain
    // directory enumeration order, and non-numeric names are excluded.
    candidates.sort_by_key(|(sequence, _)| *sequence);
    for (_, path) in candidates {
        if !path.is_dir() || !same_path(&path) {
            continue;
        }
        let Some(marker) = fs::read(path.join("runtime.json"))
            .ok()
            .and_then(|raw| serde_json::from_slice::<Value>(&raw).ok())
        else {
            continue;
        };
        let Some(marker) = marker.as_object() else {
            continue;
        };
        let console = path.join(if cfg!(windows) {
            "Scripts/pseudolife-mcp.exe"
        } else {
            "bin/pseudolife-mcp"
        });
        if !console.is_file() {
            continue;
        }
        let origin = if let Some(commit) = marker.get("source_commit").filter(|value| truthy(value))
        {
            format!("source commit {}", python_text(commit, false))
        } else {
            format!(
                "source {}",
                marker
                    .get("source")
                    .filter(|value| truthy(value))
                    .map(|value| python_text(value, false))
                    .unwrap_or_else(|| "unknown".into())
            )
        };
        return Some(format!("runtime {} ({origin})\n", path.display()));
    }
    None
}

pub(super) fn text() -> String {
    let mut text = format!("pseudolife-mcp {}\n", env!("CARGO_PKG_VERSION"));
    if let Some(runtime) = runtime_line() {
        text.push_str(&runtime);
    }
    text
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn runtime_names_match_python_decimal_digits_and_final_newline() {
        for name in ["000001", "０００００１", "٠٠٠٠٠١", "000001\n"] {
            assert!(runtime_sequence(name).is_some());
        }
        for name in ["00001", "0000001", "¹²³⁴⁵⁶", "000001\n\n", "manual"] {
            assert!(runtime_sequence(name).is_none());
        }
    }
    #[cfg(unix)]
    fn alias_fixture(names: &[&str], marker: &[u8]) -> (PathBuf, PathBuf) {
        let root = std::env::temp_dir().join(format!("runtime-order-{}", uuid::Uuid::new_v4()));
        let directory = root.join("installed");
        fs::create_dir_all(directory.join("bin")).unwrap();
        fs::write(directory.join("bin/pseudolife-mcp"), b"console").unwrap();
        fs::write(directory.join("runtime.json"), marker).unwrap();
        for name in names {
            std::os::unix::fs::symlink(&directory, root.join(name)).unwrap();
        }
        (root, fs::canonicalize(directory).unwrap())
    }

    #[cfg(unix)]
    #[test]
    fn runtime_aliases_use_numeric_order_and_skip_non_numeric_names() {
        let (root, directory) =
            alias_fixture(&["000009", "000002", "manual"], br#"{"source":"fixture"}"#);
        let actual = runtime_line_at(&root, &directory);
        let expected = format!(
            "runtime {} (source fixture)\n",
            root.join("000002").display()
        );
        fs::remove_dir_all(root).unwrap();
        assert_eq!(actual.as_deref(), Some(expected.as_str()));
    }

    fn marker_fixture(names: &[(&str, Option<&[u8]>)]) -> PathBuf {
        let root = std::env::temp_dir().join(format!("runtime-markers-{}", uuid::Uuid::new_v4()));
        for (name, marker) in names {
            let path = root.join(name);
            let console = path.join(if cfg!(windows) {
                "Scripts/pseudolife-mcp.exe"
            } else {
                "bin/pseudolife-mcp"
            });
            fs::create_dir_all(console.parent().unwrap()).unwrap();
            fs::write(console, b"console").unwrap();
            if let Some(marker) = marker {
                fs::write(path.join("runtime.json"), marker).unwrap();
            }
        }
        root
    }

    #[test]
    fn ordered_candidates_keep_numeric_ties_in_enumeration_order() {
        let root = marker_fixture(&[
            ("０００００２", Some(br#"{"source":"first"}"#)),
            ("000002", Some(br#"{"source":"second"}"#)),
            ("000009", Some(br#"{"source":"higher"}"#)),
            ("manual", Some(b"{}")),
        ]);
        let paths = ["manual", "000009", "０００００２", "000002"]
            .map(|name| root.join(name))
            .to_vec();
        let actual = runtime_line_from_paths(paths, |_| true);
        let expected = format!(
            "runtime {} (source first)\n",
            root.join("０００００２").display()
        );
        fs::remove_dir_all(root).unwrap();
        assert_eq!(actual.as_deref(), Some(expected.as_str()));
    }

    #[test]
    fn damaged_matching_marker_does_not_hide_later_valid_runtime() {
        for marker in [
            Some(b"{".as_slice()),
            Some(b"[]"),
            Some(b"null"),
            Some(b"\xff"),
            None,
        ] {
            let root = marker_fixture(&[
                ("000001", marker),
                ("000002", Some(br#"{"source":"later"}"#)),
            ]);
            let paths = vec![root.join("000001"), root.join("000002")];
            let actual = runtime_line_from_paths(paths, |_| true);
            let expected = format!("runtime {} (source later)\n", root.join("000002").display());
            fs::remove_dir_all(root).unwrap();
            assert_eq!(actual.as_deref(), Some(expected.as_str()));
        }
    }

    #[test]
    fn python_float_shortest_ties() {
        // Python writer: pseudolife_memory/cli.py:129-131 (runtime source fields).
        let mut wrong = Vec::new();
        for (bits, expected) in [
            (0x430c6bf526340002, "1000000000000000.2"),
            (0xc308130f222a4572, "-847044394961070.2"),
            (0x42d526daef896bc8, "93026504287663.12"),
        ] {
            let value = serde_json::json!(f64::from_bits(bits));
            let actual = python_text(&value, false);
            let nested = python_text(&serde_json::json!([value]), false);
            if actual != expected || nested != format!("[{expected}]") {
                wrong.push(format!("{bits:016x}: {actual}, {nested} != {expected}"));
            }
        }
        assert!(wrong.is_empty(), "{}", wrong.join("\n"));
    }
    #[test]
    fn manifest_field_rendering_uses_python_truth_and_representation() {
        for (value, rendered) in [
            (serde_json::json!(true), "True"),
            (serde_json::json!(["a", false]), "['a', False]"),
            (serde_json::json!({"key": "value"}), "{'key': 'value'}"),
        ] {
            assert_eq!(python_text(&value, false), rendered);
        }
        assert_eq!(
            python_text(&serde_json::from_str::<Value>("1e-7").unwrap(), false),
            "1e-07"
        );
        assert_eq!(
            python_text(&serde_json::from_str::<Value>("1e16").unwrap(), false),
            "1e+16"
        );
        for value in [
            serde_json::json!(null),
            serde_json::json!(false),
            serde_json::json!(0),
            serde_json::json!(""),
            serde_json::json!([]),
            serde_json::json!({}),
        ] {
            assert!(!truthy(&value));
        }
    }
}
