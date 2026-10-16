use super::*;

#[test]
fn numeric_options_use_finite_ascii_decimal_and_exponents() {
    for (raw, expected) in [
        ("0.025", 0.025),
        ("1e-2", 0.01),
        ("+2E+0", 2.0),
        (".5", 0.5),
        ("1.", 1.0),
    ] {
        assert_eq!(python_float(raw), Some(expected), "{raw}");
    }
    for raw in [
        "",
        " 0.025",
        "0.025 ",
        "٠.٠٢٥",
        "1_0e-3",
        "inf",
        "NaN",
        "1e999",
        "1e",
        "1e+",
        "--1",
        "1.2.3",
        "0x10",
        "1\n",
    ] {
        assert!(python_float(raw).is_none(), "{raw}");
        let error = parse(vec!["--timeout".into(), raw.into()]).err().unwrap();
        assert!(error.starts_with("argument --timeout:"), "{raw}: {error}");
    }
    for raw in ["0", "-1", "86401"] {
        assert_eq!(
            parse(vec![format!("--timeout={raw}").into()])
                .err()
                .unwrap(),
            "--timeout must be more than 0 and at most 86400 seconds"
        );
    }
    for raw in ["0.009", "60.01"] {
        assert_eq!(
            parse(vec!["--interval".into(), raw.into()]).err().unwrap(),
            "--interval must be from 0.01 to 60 seconds"
        );
    }
}

#[test]
fn terminal_dimensions_use_positive_ascii_integers() {
    assert_eq!(columns_value("80"), Some(80));
    assert_eq!(columns_value("1"), Some(1));
    for raw in [
        "",
        "0",
        "-1",
        "+80",
        " 80",
        "80\n",
        "8_0",
        "٨٠",
        "1e2",
        "1.0",
        "999999999999999999999999999999",
    ] {
        assert_eq!(columns_value(raw), None, "{raw}");
    }
    assert_eq!(help(80), super::super::HELP);
    assert_eq!(
        usage(80),
        super::super::HELP.split("\n\n").next().unwrap().to_owned() + "\n"
    );
    // Small widths retain words rather than reproducing argparse's thresholds.
    assert!(help(1).contains("addressed\nmail"));
    assert!(usage(1).contains("pseudolife-mcp\nwait-mail"));
}
