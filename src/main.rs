//! The Yelo CLI, ported from the Python package in src/yelo; tests/test_cli_golden.py judges it.

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if let [flag] = args.as_slice() && flag == "--version" {
        println!("yelo {}", env!("CARGO_PKG_VERSION"));
        return;
    }
    eprintln!("yelo: {}: not ported yet", args.first().map_or("", String::as_str));
    std::process::exit(2);
}
