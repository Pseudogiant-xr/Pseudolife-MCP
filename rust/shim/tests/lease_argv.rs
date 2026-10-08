#![forbid(unsafe_code)]
mod common;
use common::{LeaseHome, cli::native_text};

#[test]
fn ambiguous_option_is_classified_before_consuming_a_value() {
    for option in ["--t=30", "--t=30 with space"] {
        let home = LeaseHome::new();
        let output = home.call(&["lease", "run", "resource", "--purpose", option]);
        assert_eq!(output.status.code(), Some(2));
        assert!(output.stdout.is_empty());
        assert_eq!(
            output.stderr,
            native_text(&format!(
                "{}pseudolife-mcp lease run: error: ambiguous option: {option} could match --ttl, --timeout\n",
                include_str!("../src/cli/lease/assets/run_usage.txt")
            ))
        );
        assert_eq!(std::fs::read_dir(&home.0).unwrap().count(), 0);
    }
}
